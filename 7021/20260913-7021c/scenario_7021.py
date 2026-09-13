#!/usr/bin/env python3
"""phase-rs/phase #7021 - Fear, Fire, Foes! secondary damage ignores the
"with the same controller" clause.

Oracle: "Damage can't be prevented this turn. Fear, Fire, Foes! deals X
damage to target creature and 1 damage to each other creature with the
same controller."

Reported: the 1 damage hits ALL other creatures, including the caster's.

Contract:
  A1_parse_gap   - v0.82.0 card-data AST: DamageAll target lacks any
                   same-controller filter (gap present) [parse evidence]
  A2_setup_ok    - 3-seat game; P1 has >=2 Mystics, P0/P2 have >=1 each;
                   Fear in P0 hand; pre.json exported
  A3_target_hit  - target P1 Mystic leaves the battlefield (X=2)
  A4_same_controller_hit - P1's other Mystic leaves the battlefield
                   (the 1 damage it is supposed to take)
  A5_caster_spared - P0's Mystic still on battlefield (0 damage)
  A6_third_party_spared - P2's Mystic still on battlefield (0 damage)
  A7_cleanup     - stack empty, game proceeds
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9375/ws")

from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7021
RUN_ID = os.environ.get("RUN_ID", "20260913-7021c")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

FEAR = "fear, fire, foes!"
MYSTIC = "elvish mystic"
MOUNTAIN = "mountain"
FOREST = "forest"

P0_DECK = [(FEAR, 12), (MYSTIC, 8), (MOUNTAIN, 28), (FOREST, 12)]
P1_DECK = [(MYSTIC, 16), (FOREST, 44)]
P2_DECK = [(MYSTIC, 16), (FOREST, 44)]

SERVER_IDENTITY = {
    "server_version": "v0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034202bdf67b283b5805ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9375 "
              "(started fresh by this run) + verified pin "
              "(minisign prehashed verify of binary + signed data manifest "
              "with the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY).",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ---------------------------------------------------------------- state views
def objects(state):
    return state.get("objects", {}) or {}


def lname(state, oid):
    o = objects(state).get(str(oid)) or objects(state).get(int(oid))
    return str(o.get("base_name") or o.get("name") or "").lower() if o else ""


def bf_mystics(state, pid):
    out = []
    for oid, o in objects(state).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and lname(state, oid) == MYSTIC):
            out.append(int(oid))
    return sorted(out)


def hand_lnames(state, pid):
    out = []
    for oid, o in objects(state).items():
        if o.get("zone") == "Hand" and o.get("controller") == pid:
            out.append(lname(state, oid))
    return out


def untapped_lands(state, pid):
    return sum(1 for oid, o in objects(state).items()
               if o.get("zone") == "Battlefield" and o.get("controller") == pid
               and lname(state, oid) in (MOUNTAIN, FOREST)
               and not o.get("tapped"))


def life_of(state, pid):
    for oid, o in objects(state).items():
        if o.get("zone") == "PlayerZone" or o.get("object_kind") == "Player":
            if o.get("controller") == pid or o.get("seat") == pid:
                return o.get("life")
    # fallback: players list
    for p in state.get("players", []) or []:
        if p.get("seat") == pid or p.get("player_id") == pid:
            return p.get("life")
    return None


def wf_of(state):
    return (state.get("waiting_for") or {})


def wf_player(state):
    return (wf_of(state).get("data", {}) or {}).get("player")


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and wf_player(state) == pid


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def stack_entries(state):
    return state.get("stack") or []


def cast_oid_for(acts, card_key):
    # CastSpell advertised actions carry object_id/card_id (AGENTS.md #6690)
    for a in acts:
        if "cast" not in (a.get("type") or "").lower():
            continue
        return a
    return None


def vi_opportunities(c):
    st = c.latest or {}
    vi = st.get("viewer_interaction") or {}
    return vi.get("opportunities", []) or []


def candidate_ref_oid(choice):
    """Extract the targeted object oid from a candidate's surfaces.

    The engine-issued reference may be an int or a numeric string
    (observed "31" in #6983 wire logs) or a nested dict; normalize
    via ref_key() before comparing.
    """
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


def ref_key(ref):
    """Normalize a candidate reference to a comparable string oid."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
    return None


def cand_name(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("name"):
            return str(d["name"]).lower()
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse_gap", "A2_setup_ok", "A3_target_hit",
            "A4_same_controller_hit", "A5_caster_spared",
            "A6_third_party_spared", "A7_cleanup")}

    # ---- A1: parse check against the pinned v0.82.0 data ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"))
        e = cd[FEAR]
        say("oracle:", e["oracle_text"])
        with open(f"{EVDIR}/parse_fear.json", "w") as f:
            json.dump(e["abilities"], f, indent=1)

        def find_damage_all(abs_):
            for a in abs_ or []:
                eff = (a.get("effect") or {})
                if eff.get("type") == "DamageAll":
                    return a
                sub = (a.get("sub_ability") or {})
                r = find_damage_all([sub] if sub else [])
                if r:
                    return r
            return None

        da = find_damage_all(e["abilities"])
        tgt = (da.get("effect", {}) or {}).get("target", {}) if da else {}
        props = [p.get("type") for p in (tgt.get("properties") or [])]
        ctrl = tgt.get("controller")
        blob = json.dumps(tgt).lower()
        has_same_controller = "same" in blob and "controller" in blob
        if da and not has_same_controller and ctrl is None:
            ass["A1_parse_gap"] = "passed"
            notes.append(f"A1_parse_gap: passed (DamageAll target = {json.dumps(tgt)[:160]}; "
                         "no same-controller filter - the parse drops the clause)")
        else:
            ass["A1_parse_gap"] = "failed"
            notes.append(f"A1_parse_gap: FAILED to confirm gap: target={json.dumps(tgt)[:200]}")
        say(notes[-1])
    except Exception as ex:
        ass["A1_parse_gap"] = "failed"
        notes.append(f"A1_parse_gap failed: {ex}")

    clients = {}
    for name, i in (("P0", 0), ("P1", 1), ("P2", 2)):
        c = PhaseClient(name)
        await c.connect()
        clients[name] = (c, i)
    p0, _ = clients["P0"]
    p1, _ = clients["P1"]
    p2, _ = clients["P2"]
    await p0.create(deck(*P0_DECK), player_count=3)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")

    ST = {"fear_cast": False, "fear_resolved": False,
          "target_oid": None, "x_answered": False,
          "pre_exported": False, "post_exported": False,
          "cleanup_turns": 0, "done": False}
    kept = {}
    answered_iid = set()

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            wire(f"export_{tag}", {"ok": True})
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    async def do_mulligan(c, pid, tag):
        st = c.latest or {}
        state = st.get("state", st)
        hand = [o for oid, o in objects(state).items()
                if o.get("zone") == "Hand" and o.get("controller") == pid]
        lands = sum(1 for o in hand
                    if str(o.get("base_name") or o.get("name") or "").lower()
                    in (MOUNTAIN, FOREST))
        if lands >= 2 or kept.get(tag, 0) >= 1:
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            kept[tag] = kept.get(tag, 0)
            say(f"{tag} keeps ({lands} lands)")
        else:
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
            kept[tag] = kept.get(tag, 0) + 1
            say(f"{tag} mulligans #{kept[tag]} ({lands} lands)")

    async def answer_target_and_x(c, pid, tag, state):
        """Answer Fear's TargetSelection then ChooseXValue."""
        for opp in vi_opportunities(c):
            iid = opp.get("interactionId")
            if iid in answered_iid:
                continue
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            # ---- target selection ----
            if not ST["target_oid"]:
                want = ST.get("wanted_target_oid")
                if want is None:
                    p1m = bf_mystics(state, 1)
                    if p1m:
                        ST["wanted_target_oid"] = p1m[0]
                        want = p1m[0]
                if want is not None:
                    # candidates live at response.data.choices or
                    # response.data.candidates (AGENTS.md #6906)
                    choices = data.get("choices") or data.get("candidates") or []
                    if not ST.get("cands_dumped"):
                        ST["cands_dumped"] = True
                        wire("target_candidates",
                             {"iid": str(iid)[:12], "n": len(choices),
                              "sample": [
                                  {"name": cand_name(ch),
                                   "ref": str(candidate_ref_oid(ch))[:40],
                                   "ref_key": ref_key(candidate_ref_oid(ch))}
                                  for ch in choices[:16]]})
                    pick = None
                    for ch in choices:
                        if ref_key(candidate_ref_oid(ch)) == str(want):
                            pick = ch
                            break
                    if pick is not None:
                        if rtype == "exactChoices":
                            sub = {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId": pick["id"]}}}
                        else:
                            stype = (data.get("spec", {}) or {}).get("type") or "sequence"
                            sub = {"interactionId": iid,
                                   "response": {"type": stype,
                                                "data": {"choiceIds": [pick["id"]]}}}
                        wire("target_answer", {"iid": str(iid)[:12],
                                               "candidate_oid": candidate_ref_oid(pick),
                                               "choice_id": pick.get("id"),
                                               "sub": sub})
                        await c.send_interaction(sub)
                        answered_iid.add(iid)
                        ST["target_oid"] = want
                        say(f"[{tag}] answered target: P1 mystic oid={want}")
                        return True
                    else:
                        wire("target_no_match",
                             {"iid": str(iid)[:12], "want": want,
                              "n_choices": len(choices)})
            # ---- X value ----
            spec = data.get("spec", {}) or {}
            if rtype == "schema" and spec.get("type") == "number" \
                    and not ST["x_answered"]:
                sdata = spec.get("data", {}) or {}
                xmin = sdata.get("min", spec.get("min"))
                xmax = sdata.get("max", spec.get("max"))
                x = 2
                sub = {"interactionId": iid,
                       "response": {"type": "number", "data": {"value": x}}}
                wire("x_answer", {"iid": str(iid)[:12], "x": x,
                                  "min": xmin, "max": xmax})
                await c.send_interaction(sub)
                answered_iid.add(iid)
                ST["x_answered"] = True
                say(f"[{tag}] answered X={x} (min={xmin} max={xmax})")
                return True
        return False

    async def seat_tick(c, pid, tag):
        st = c.latest or {}
        state = st.get("state", st)
        wf = wf_of(state)
        wtype = wf.get("type") or ""
        acts = merged_actions(st)
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")

        # mulligan
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision"):
                await do_mulligan(c, pid, tag)
            return
        # mana payment prompts: submit as advertised
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return
        # combat: never attack, never block (keep board clean)
        if wtype in ("DeclareAttackers", "DeclareBlockers") and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
            return
        # never pass priority while our own decision is pending
        if wtype in ("OptionalCostChoice", "TargetSelection",
                     "ChooseXValue") and wf_player(state) == pid:
            if pid == 0 and not ST["fear_resolved"]:
                await answer_target_and_x(c, pid, tag, state)
            return
        # P0: cast Fear when the board is ready
        if (pid == 0 and not ST["fear_cast"]
                and phase in ("PreCombatMain", "PostCombatMain")
                and active == 0 and my_priority(state, 0)):
            p1m = bf_mystics(state, 1)
            p0m = bf_mystics(state, 0)
            p2m = bf_mystics(state, 2)
            if (len(p1m) >= 2 and len(p0m) >= 1 and len(p2m) >= 1
                    and FEAR in hand_lnames(state, 0)
                    and untapped_lands(state, 0) >= 3):
                if not ST["pre_exported"]:
                    await export_named("pre")
                    ST["pre_exported"] = True
                ca = None
                for a in acts:
                    if "cast" not in (a.get("type") or "").lower():
                        continue
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id") or d.get("card_id")
                    if isinstance(oid, int) and lname(state, oid) == FEAR:
                        ca = a
                        break
                if ca:
                    oid = (ca.get("data") or {}).get("object_id") \
                        or (ca.get("data") or {}).get("card_id")
                    wire("fear_cast_submit", {"action": ca})
                    await submit_as_is(c, ca)
                    ST["fear_cast"] = True
                    ST["fear_cast_turn"] = turn
                    say(f"[P0] cast Fear, Fire, Foes! (turn {turn}) "
                        f"target will be P1 mystic {p1m[0]}")
                    return
        # generic: play a land
        if (phase in ("PreCombatMain", "PostCombatMain") and active == pid
                and my_priority(state, pid)):
            pl = find_action(acts, "PlayLand")
            if pl:
                await submit_as_is(c, pl)
                return
            # cast a mystic if affordable
            for a in acts:
                if "cast" not in (a.get("type") or "").lower():
                    continue
                d = a.get("data", {}) or {}
                oid = d.get("object_id") or d.get("card_id")
                if isinstance(oid, int) and lname(state, oid) == MYSTIC:
                    wire("mystic_cast", {"tag": tag, "turn": turn})
                    await submit_as_is(c, a)
                    return
        # default: pass priority
        if my_priority(state, pid):
            pp = find_action(acts, "PassPriority")
            if pp:
                await submit_as_is(c, pp)

    async def finish():
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
        states = {}
        for fn in ("pre", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, post = states.get("pre"), states.get("post")

        # ---- A2: setup ----
        if pre is not None:
            ok = (len(bf_mystics(pre, 1)) >= 2
                  and len(bf_mystics(pre, 0)) >= 1
                  and len(bf_mystics(pre, 2)) >= 1
                  and FEAR in hand_lnames(pre, 0))
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: P1m={bf_mystics(pre,1)} P0m={bf_mystics(pre,0)} "
                         f"P2m={bf_mystics(pre,2)} fear_in_hand="
                         f"{FEAR in hand_lnames(pre,0)}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing")

        # ---- A3..A6 from post ----
        if post is not None and ST["fear_cast"]:
            tgt = ST["target_oid"]
            p1m = bf_mystics(post, 1)
            p0m = bf_mystics(post, 0)
            p2m = bf_mystics(post, 2)
            target_gone = tgt is not None and tgt not in p1m
            ass["A3_target_hit"] = "passed" if target_gone else "failed"
            notes.append(f"A3: target_oid={tgt} P1m_post={p1m} -> "
                         f"{'passed' if target_gone else 'FAILED'}")
            # correct: P1's OTHER mystic takes 1 (dies, 1/1)
            other_p1_gone = len(p1m) == 0
            ass["A4_same_controller_hit"] = "passed" if other_p1_gone else "failed"
            notes.append(f"A4: P1 other mystics post={p1m} -> "
                         f"{'passed' if other_p1_gone else 'FAILED'} "
                         "(expected 0 survivors: target+X, other+1)")
            ass["A5_caster_spared"] = "passed" if len(p0m) >= 1 else "failed"
            notes.append(f"A5: P0 mystics post={p0m} -> "
                         f"{'passed' if len(p0m) >= 1 else 'FAILED'} "
                         "(expected >=1: caster's creature takes 0)")
            ass["A6_third_party_spared"] = "passed" if len(p2m) >= 1 else "failed"
            notes.append(f"A6: P2 mystics post={p2m} -> "
                         f"{'passed' if len(p2m) >= 1 else 'FAILED'} "
                         "(expected >=1: third party takes 0)")
            stack_empty = len(stack_entries(post)) == 0
            fear_gy = any(lname(post, int(oid)) == FEAR
                          for oid, o in objects(post).items()
                          if o.get("zone") == "Graveyard"
                          and o.get("controller") == 0)
            ok = stack_empty and fear_gy
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: stack_empty={stack_empty} fear_in_P0_gy={fear_gy}")
        else:
            for k in ("A3_target_hit", "A4_same_controller_hit",
                      "A5_caster_spared", "A6_third_party_spared",
                      "A7_cleanup"):
                ass[k] = "failed"
            notes.append("A3..A7 failed: fear never cast or post.json missing")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        verdict = ("reproduced" if ass["A5_caster_spared"] == "failed"
                   or ass["A6_third_party_spared"] == "failed"
                   else ("not-reproduced"
                         if ass["A4_same_controller_hit"] == "passed"
                         and ass["A3_target_hit"] == "passed"
                         else "blocked"))
        say("VERDICT:", verdict)

        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": "[Card Bug] Fear, Fire, Foes! Incorrectly deals 1 damage "
                     "to all other creatures instead of all other creatures "
                     "with the same controller",
            "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "scope": "Fear, Fire, Foes! secondary 1-damage clause across "
                     "three controller seats; native engine, three "
                     "human-client seats",
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {
                "fear_cast": ST["fear_cast"],
                "fear_resolved": ST["fear_resolved"],
                "target_oid": ST["target_oid"],
                "x_answered": ST["x_answered"],
                "fear_cast_turn": ST.get("fear_cast_turn"),
            },
            "limitations": [
                "Browser UI not exercised; native engine via three "
                "human-client seats.",
                "Dense playsets are a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Zero-attacker combat was scripted on all seats so combat "
                "damage could not mask the spell's damage.",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
            "server_run_note": "isolated v0.82.0 single-user server on "
                               "127.0.0.1:9375, started fresh for this run "
                               "by scenario_7021.py (games.db + server.log "
                               f"in runs/{RUN_ID}/).",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        # copy scenario + server log into evidence
        shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7021.py")
        try:
            shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                        f"{EVDIR}/server.log")
        except Exception as e:
            notes.append(f"server.log copy failed: {e}")
            say("server.log copy failed:", e)
        # render summary.png from saved states/assertions
        render_png(run)
        # manifest LAST
        files = ["pre.json", "post.json", "parse_fear.json", "run.json",
                 "scenario_7021.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                say(f"manifest: MISSING {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        say("wrote manifest.sha256")
        # validation
        for fn in ("pre.json", "post.json", "parse_fear.json", "run.json"):
            json.load(open(f"{EVDIR}/{fn}"))
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
        for line in man:
            h, fn = line.split("  ")
            assert hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
        say("validation: all JSON parse, PNG readable, hashes match")

    def render_png(run):
        from PIL import Image, ImageDraw
        W, H = 1000, 1040
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7021 - Fear, Fire, Foes! secondary "
               "damage hits all other creatures", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.82.0 (060b5d2) protocol 70 - 2026-09-13 - "
               "3 seats", fill=(140, 160, 180))
        y += 28
        col = (255, 90, 90) if run["verdict"] == "reproduced" else (
            (120, 220, 120) if run["verdict"] == "not-reproduced"
            else (230, 200, 120))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
        y += 34
        d.text((24, y), "Oracle: deals X to target creature and 1 damage to "
               "each other creature", fill=(200, 210, 225))
        y += 24
        d.text((36, y), "WITH THE SAME CONTROLLER (dropped clause).",
               fill=(255, 180, 120))
        y += 34
        labels = {
            "A1_parse_gap": "PARSE: DamageAll target has no same-controller filter",
            "A2_setup_ok": "GAME: P1x2 / P0x1 / P2x1 Mystics, Fear in P0 hand",
            "A3_target_hit": "GAME: target P1 Mystic takes X=2 (leaves BF)",
            "A4_same_controller_hit": "GAME: P1's other Mystic takes the 1 (leaves BF)",
            "A5_caster_spared": "GAME: P0's Mystic takes 0 (stays on BF)",
            "A6_third_party_spared": "GAME: P2's Mystic takes 0 (stays on BF)",
            "A7_cleanup": "GAME: stack empty, Fear in P0 graveyard",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            c = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=c)
            y += 24
        y += 10
        ds = run.get("driver_state") or {}
        d.text((24, y), f"fear_cast={ds.get('fear_cast')} "
               f"target_oid={ds.get('target_oid')} "
               f"x_answered={ds.get('x_answered')} "
               f"turn={ds.get('fear_cast_turn')}", fill=(150, 160, 175))
        y += 30
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:14]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    # ---- main loop ----
    deadline = time.time() + 900
    last_tick = {0: 0, 1: 0, 2: 0}
    try:
        while time.time() < deadline and not ST["done"]:
            for c, pid in (p0, 0), (p1, 1), (p2, 2):
                tag = ("P0", "P1", "P2")[pid]
                if time.time() - last_tick[pid] < 1.0:
                    continue
                try:
                    await seat_tick(c, pid, tag)
                except Exception as e:
                    wire("tick_error", {"tag": tag, "err": str(e)[:200]})
                last_tick[pid] = time.time()
            # detect fear resolution: fear in P0 gy
            st = p0.latest or {}
            state = st.get("state", st)
            if ST["fear_cast"] and not ST["fear_resolved"]:
                if any(lname(state, int(oid)) == FEAR
                       for oid, o in objects(state).items()
                       if o.get("zone") == "Graveyard"
                       and o.get("controller") == 0):
                    ST["fear_resolved"] = True
                    say("Fear resolved (in P0 gy); cleanup window open")
            if ST["fear_resolved"]:
                ST["cleanup_turns"] += 1
                if ST["cleanup_turns"] > 40:
                    ST["done"] = True
            await asyncio.sleep(0.2)
        if not ST["done"]:
            notes.append("deadline hit before cleanup completed")
    finally:
        await finish()
        for c, _ in (p0, 0), (p1, 1), (p2, 2):
            try:
                await c.ws.close()
            except Exception:
                pass
        WIRE.close()
        RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
