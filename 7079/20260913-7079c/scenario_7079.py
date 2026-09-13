#!/usr/bin/env python3
"""Issue #7079: [Card Bug] Magnificent End: Not possible to cast when not 5 mana available.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.82.0):
  Magnificent End ({4}{W} Instant):
    "This spell costs {3} less to cast if it targets a tapped creature.
     Magnificent End deals 5 damage to target creature."

Card-data parse state on v0.82.0 (verified 2026-09-13 before the run):
  static_abilities[0] = ModifyCost Reduce {3} generic,
    spell_filter Targets(Typed Creature + Tapped), affected SelfRef.
  abilities[0] = Spell DealDamage Fixed 5 to Typed Creature.
  The target-sensitive reduction clause parses as SUPPORTED (matches the
  triage classifier's supported_aspect_defect verdict).

Reported symptom: Magnificent End cannot be cast unless {4}{W} (the printed
cost) is available, even when a tapped creature is a legal target (reduced
cost would be {1}{W}). Expected: with a tapped-creature target and {1}{W}
available, the spell should be announceable; targets announced determine
whether the reduction applies when the total cost is locked in.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Magnificent End, 28x Plains, 20x Forest.
  P1: 12x Grizzly Bears, 48x Forest.
  P0 plays lands on turns 1-2 only (holds further drops) -> exactly 2 lands
  ({W} among them) on its turn-4 main phase.
  P1 casts Grizzly Bears on turn 2, attacks with it on turn 3 (P0 blocks
  nothing) -> the Bear is tapped through P0's turn 4.
  Window: P0 turn-4 PreCombatMain priority with 2 untapped lands, a tapped
  opposing Bear, Magnificent End in hand. Printed {4}{W} is unpayable; the
  reduced {1}{W} is payable IFF the target-dependent reduction is honored
  by the cast-availability preflight.

Assertions (each passed / failed / not-run):
  A1_parse_gap      card-data v0.82.0 carries the Reduce-{3}-generic /
                    Targets(Tapped Creature) clause as supported (PASS
                    confirms the triage's supported-aspect call: the defect
                    is in runtime preflight, not a parser gap).
  A2_setup_ok       PRE: P0 PreCombatMain, Magnificent End in hand, exactly 2
                    untapped lands incl. >=1 Plains, tapped Grizzly Bears on
                    P1 BF, priority_player == 0, life 20/20.
  A3_reduced_offer  CastSpell for Magnificent End is advertised in P0's
                    legal actions during the window (printed cost unpayable,
                    reduced cost payable). Expected FAILED under the bug.
  A4_printed_offer  CONTROL: on a later P0 main phase with >=5 untapped lands
                    (>=1 Plains), CastSpell for Magnificent End IS advertised
                    (printed cost payable). Expected passed.
  A5_reduced_cast   (offer path only) the spell is cast, targets the tapped
                    Bear, pays exactly {1}{W} (2 lands), deals 5, the Bear
                    dies, the spell reaches P0's graveyard.
  A6_cleanup        POST: stack empty, game proceeds.

Verdict rule:
  reproduced    iff A2 passed and A3 failed (A4 passed strengthens; the
                reported preflight defect is observed).
  not-reproduced iff A3 passed and A5 passed (full reduced-cost path works).
  reproduced (related) iff A3 passed but A5 failed (offered, but payment or
                targeting goes wrong).
  blocked iff A2 failed (setup never reached).

Evidence: evidence/7079/<run-id>/pre.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7079.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts for this game's code only).
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-7079")
EVDIR = f"{BACKFILL}/evidence/7079/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

END = "magnificent end"
BEARS = "grizzly bears"
PLAINS = "plains"
FOREST = "forest"
LANDS = (PLAINS, FOREST)

P0_DECK = [(END, 12), (PLAINS, 28), (FOREST, 20)]
P1_DECK = [(BEARS, 12), (FOREST, 48)]

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034202bdf67b283b5805ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(started fresh by this run) + verified pin (minisign-verify "
              "of binary + signed data manifest with the repo-pinned key).",
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


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def bf_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in LANDS):
            out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [oid for oid in bf_lands(state, pid)
            if not get_obj(state, oid).get("tapped")]


def untapped_land_names(state, pid):
    return [lname(state, oid) for oid in untapped_lands(state, pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def cast_spell_oids(acts, state):
    """object_ids of CastSpell actions whose card is Magnificent End."""
    out = []
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        oid = d.get("object_id")
        if isinstance(oid, int) and lname(state, oid) == END:
            out.append((oid, a))
    return out


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def candidate_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return data.get("choices") or data.get("candidates") or []


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def wf_pending_for(state, pid):
    d = wf_of(state).get("data") or {}
    if isinstance(d.get("player"), int):
        return d["player"] == pid
    for p in d.get("pending") or []:
        if isinstance(p, dict) and p.get("player") == pid:
            return True
    return False


def stack_entries(state):
    return state.get("stack") or []


def end_on_stack(state):
    return any(lname(state, int(oid)) == END
               for oid, o in (state.get("objects", {}) or {}).items()
               if o.get("zone") == "Stack" and o.get("controller") == 0)


def stack_names(state):
    out = []
    for e in stack_entries(state):
        blob = json.dumps(e, default=str)
        m = re.search(r'"name"\s*:\s*"([^"]+)"', blob)
        out.append(m.group(1) if m else "?")
    return out


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)

async def main():
    t_start = time.time()
    notes = []
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "tick_errors": [], "life_trace": [], "offer_scans": []}
    ass = {k: "not-run" for k in
           ("A1_parse_gap", "A2_setup_ok", "A3_reduced_offer",
            "A4_printed_offer", "A5_reduced_cast", "A6_cleanup")}

    # ---- A1 (parse check) up front, from the pinned card-data.json ----
    try:
        cd_path = f"{BACKFILL}/server/releases/v0.82.0/data/card-data.json"
        cd = json.load(open(cd_path))
        c = cd[END]
        statics = c.get("static_abilities", []) or []
        clause = None
        for s in statics:
            mode = (s.get("mode") or {}).get("ModifyCost") or {}
            if mode.get("mode") == "Reduce":
                clause = s
                break
        notes.append("parse: static ModifyCost clause = "
                     + (json.dumps(clause)[:600] if clause else "NONE"))
        wire("parse_check", {"clause": clause})
        ok = False
        if clause:
            mode = clause["mode"]["ModifyCost"]
            amt = mode.get("amount") or {}
            filt = ((mode.get("spell_filter") or {}).get("properties")
                    or [{}])[0]
            inner_props = ((filt.get("filter") or {}).get("properties")
                           or [])
            ok = (amt.get("generic") == 3
                  and filt.get("type") == "Targets"
                  and any(p.get("type") == "Tapped" for p in inner_props)
                  and (clause.get("affected") or {}).get("type") == "SelfRef")
        ass["A1_parse_gap"] = "passed" if ok else "failed"
        notes.append(f"A1_parse_gap -> {ass['A1_parse_gap']} "
                     "(Reduce 3 generic, Targets Tapped Creature, SelfRef)")
    except Exception as ex:
        ass["A1_parse_gap"] = "failed"
        notes.append(f"A1_parse_gap failed: {ex}")

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    GAME = p0.game_code
    say(f"game {GAME}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    kept = {}

    ST = {"land_hold": True, "window_assessed": False,
          "offered_reduced": False, "offered_reduced_detail": None,
          "offered_printed": False, "offered_printed_detail": None,
          "end_cast": False, "end_in_flight": False, "end_oid": None,
          "end_resolved": False, "end_resolved_at": None,
          "target_answered": False, "target_auto": False,
          "target_submitted_oid": None, "bear_oid": None,
          "lands_tapped_at_cast": None,
          "pre_exported": False, "post_exported": False,
          "p1_bear_cast": False, "p1_attacked": False}
    prompt_first_seen = {}
    last_select = {}
    castdiag_done = set()

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def scan_offers(c, tag, state, acts):
        found = cast_spell_oids(acts, state)
        if found:
            detail = {"by": tag, "turn": state.get("turn_number"),
                      "phase": state.get("phase"),
                      "untapped_lands": untapped_land_names(state, 0),
                      "oids": [o for o, _a in found]}
            obs["offer_scans"].append(detail)
            wire("offer_scan", detail)
            say(f"[{tag}] CastSpell offered for Magnificent End: {detail}")
        return found

    async def do_mulligan(c, pid, tag):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in LANDS)
        mulls = kept.get(f"P{pid}_mulls", 0)
        key = END if pid == 0 else BEARS
        plains_ok = (PLAINS in hn) if pid == 0 else True
        ok = (key in hn and lands >= 2 and plains_ok) or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands; {key}={key in hn} "
                f"plains={PLAINS in hn})")
        else:
            kept[f"P{pid}_mulls"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{mulls + 1} ({lands} lands)")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        key = END if pid == 0 else BEARS
        pick = None
        for oid in hand:
            if lname(st, oid) != key:
                pick = oid
                break
        pick = pick if pick is not None else hand[0]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(pick)]}})
        say(f"{tag} bottoms {lname(st, pick)}")

    async def discard_tick(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            chs = vi_choices(opp)
            if not chs:
                continue

            def rank(ch):
                t = choice_text(ch).lower()
                if t == FOREST:
                    return 0
                if t == END and pid == 0:
                    return 2
                if t == BEARS and pid == 1:
                    return 2
                return 1

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def answer_end_target(c, st, state):
        vi = get_vi(st)
        if not vi:
            ST["target_auto"] = True
            say("[P0] no viewer interaction for target selection "
                "(auto-target path)")
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            chs = vi_choices(opp)
            if not chs:
                continue
            pick = None
            for ch in chs:
                if candidate_ref(ch) == ST["bear_oid"]:
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    if "bear" in choice_text(ch).lower():
                        pick = ch
                        break
            if pick is None:
                notes.append("target prompt: tapped bear not among "
                             "candidates: "
                             f"{[choice_text(ch)[:40] for ch in chs]}")
                return False
            ST["target_submitted_oid"] = candidate_ref(pick)
            await answer_vi(c, opp, pick, "P0")
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            ST["target_answered"] = True
            say(f"[P0] targeted tapped bear oid={ST['bear_oid']} "
                f"(submitted ref={ST['target_submitted_oid']})")
            return True
        return False

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
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

        # ---- A2: setup (from pre.json) ----
        if pre is not None:
            ph = pre.get("phase")
            tn = pre.get("turn_number")
            ul = untapped_land_names(pre, 0)
            bear = bf_id(pre, 1, BEARS)
            bear_tapped = (get_obj(pre, bear).get("tapped")
                           if bear is not None else None)
            ok = (ph == "PreCombatMain" and END in hand_lnames(pre, 0)
                  and len(ul) == 2 and PLAINS in ul
                  and bear is not None and bear_tapped
                  and pre.get("priority_player") == 0
                  and life_of(pre, 1) == 20 and life_of(pre, 0) < 20)
            notes.append(f"A2: phase={ph} turn={tn} untapped_lands={ul} "
                         f"bear={bear} tapped={bear_tapped} "
                         f"pp={pre.get('priority_player')} "
                         f"end_in_hand={END in hand_lnames(pre, 0)}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup_ok"] = "passed" if ok else "failed"

        # ---- A3: reduced offer (recorded live at the window) ----
        ass["A3_reduced_offer"] = ("passed" if ST["offered_reduced"]
                                   else "failed")
        notes.append(f"A3: offered_reduced={ST['offered_reduced']} "
                     f"detail={ST['offered_reduced_detail']}")

        # ---- A4: printed-cost control ----
        if ST["offered_printed"]:
            ass["A4_printed_offer"] = "passed"
        elif not ST["window_assessed"]:
            ass["A4_printed_offer"] = "not-run"
        else:
            ass["A4_printed_offer"] = "failed"
        notes.append(f"A4: offered_printed={ST['offered_printed']} "
                     f"detail={ST['offered_printed_detail']}")

        # ---- A5: reduced cast correct (offer path only) ----
        if ST["offered_reduced"]:
            if ST["end_resolved"] and post is not None:
                bear_gy = any(lname(post, int(oid)) == BEARS
                              for oid, o in (post.get("objects", {})
                                             or {}).items()
                              if o.get("zone") == "Graveyard"
                              and o.get("controller") == 1)
                end_gy = any(lname(post, int(oid)) == END
                             for oid, o in (post.get("objects", {})
                                            or {}).items()
                             if o.get("zone") == "Graveyard"
                             and o.get("controller") == 0)
                tapped_now = [oid for oid in bf_lands(post, 0)
                              if get_obj(post, oid).get("tapped")]
                lives = [life_of(post, 0), life_of(post, 1)]
                target_ok = (ST["target_submitted_oid"] == ST["bear_oid"]
                             or ST["target_auto"])
                ok = (bear_gy and end_gy and len(tapped_now) == 2
                      and lives == [20, 20] and target_ok)
                notes.append(f"A5: bear_in_P1_gy={bear_gy} "
                             f"end_in_P0_gy={end_gy} "
                             f"tapped_P0_lands={len(tapped_now)} (expect 2) "
                             f"lives={lives} target_ok={target_ok}")
                ass["A5_reduced_cast"] = "passed" if ok else "failed"
            else:
                ass["A5_reduced_cast"] = "failed"
                notes.append(f"A5 failed: end_resolved={ST['end_resolved']} "
                             f"post={'present' if post else 'missing'}")
        else:
            ass["A5_reduced_cast"] = "not-run"
            notes.append("A5 not-run: spell never offered at reduced cost")

        # ---- A6: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            ass["A6_cleanup"] = "passed" if stack_empty else "failed"
            notes.append(f"A6: stack_empty={stack_empty}")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 failed: post.json missing")

        # ---- verdict ----
        if ass["A2_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif ass["A3_reduced_offer"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: with a tapped-creature target "
                         "and exactly {1}{W} available, CastSpell for "
                         "Magnificent End was NOT advertised; "
                         f"printed-cost control offered={ST['offered_printed']}")
        elif ass["A5_reduced_cast"] == "passed":
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: spell offered at the "
                         "reduced cost and the full path (target tapped "
                         "bear, pay {1}{W}, 5 damage, bear dies) completed")
        elif ass["A3_reduced_offer"] == "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced (related): spell was offered "
                         "but the reduced-cost cast did not complete "
                         "correctly (A5 failed)")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7079,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.82.0 single-user server on "
                               "127.0.0.1:9374 (started fresh by this run; "
                               "its games.db holds only this run)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7079.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7079.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Magnificent End / 12x Grizzly Bears density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games).",
                "The untapped-creature-target control (spell not offered at "
                "reduced total when only untapped targets are legal) was not "
                "exercised in this run; the printed-cost control (A4) covers "
                "the positive offer path.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7079.py",
                    f"{EVDIR}/scenario_7079.py")
        RUN_LOG_DIR = f"/home/hatch/workspace/dev/phase-backfill/runs/{RUN_ID}"
        try:
            with open(f"{RUN_LOG_DIR}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            excerpt = [ln for ln in clean.splitlines() if GAME in ln]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines for "
                f"game {GAME})")
        except Exception as e:
            say(f"server.log excerpt failed: {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 980
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7079 - Magnificent End",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.82.0 (060b5d2) protocol 70 - 2026-09-13 - "
               "target-dependent {3} reduction preflight",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: {4}{W}; costs {3} less if it targets a "
               "tapped creature; deals 5 to target creature.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse_gap": "card-data: Reduce{3}/Targets(Tapped) supported",
            "A2_setup_ok": "PRE: 2 lands (1W), tapped Bear, End in hand",
            "A3_reduced_offer": "CastSpell offered w/ only {1}{W} payable",
            "A4_printed_offer": "CONTROL: offered w/ 5 mana (printed cost)",
            "A5_reduced_cast": "cast: target tapped Bear, pay {1}{W}, 5 dmg",
            "A6_cleanup": "POST stack empty",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Board across states:", fill=(200, 210, 225))
        y += 24
        for label in ("pre", "post"):
            st = states.get(label)
            if st is not None:
                ul = untapped_land_names(st, 0)
                bear = bf_id(st, 1, BEARS)
                bt = get_obj(st, bear).get("tapped") if bear else None
                line = (f"{label:>4}: life {life_of(st, 0)}/{life_of(st, 1)}  "
                        f"P0 untapped lands={ul}  bear={bear} tapped={bt}  "
                        f"stack={stack_names(st)}")
            else:
                line = f"{label:>4}: (no state)"
            d.text((36, y), line[:118], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:16]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        files = ["pre.json", "post.json", "run.json",
                 "scenario_7079.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]

        def build():
            lines = []
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    lines.append(f"{h}  {fn}")
                else:
                    say(f"manifest: MISSING {fn}")
            return lines

        # write twice: scenario_run.log is hashed LAST, after all say()
        # logging is done (no say() may follow the second write).
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")
        say("wrote manifest.sha256")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        try:
            if wtype == "MulliganDecision":
                if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                    await do_mulligan(p0, 0, "P0")
                    return
                if (find_action(acts, "SelectCards")
                        and last_select.get(0) != p0.revision):
                    last_select[0] = p0.revision
                    await do_bottom(p0, 0, "P0")
                    return
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await submit_as_is(p0, a)
                    return
            if wtype in ("DeclareAttackers", "DeclareBlockers"):
                da = find_action(acts, wtype)
                if da:
                    sub = copy.deepcopy(da)
                    if wtype == "DeclareAttackers":
                        sub["data"]["attacks"] = []
                        sub["data"]["bands"] = []
                    else:
                        sub["data"]["assignments"] = []
                    await submit_as_is(p0, sub)
                return
            # --- the reduced-cost window: precondition-gated, NOT turn-gated ---
            # fire on the first P0 main-phase priority where ALL hold:
            #   * END in P0 hand
            #   * exactly 2 untapped P0 lands including a Plains
            #     (printed {4}{W} unpayable, reduced {1}{W} payable)
            #   * a tapped opposing Bear (P1 has attacked with it)
            if (phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)
                    and not ST["window_assessed"]
                    and not ST["end_in_flight"]
                    and END in hand_lnames(state, 0)):
                ul = untapped_land_names(state, 0)
                bear = bf_id(state, 1, BEARS)
                bear_tapped = (bear is not None
                               and bool(get_obj(state, bear).get("tapped")))
                if len(ul) == 2 and PLAINS in ul and bear_tapped:
                    if not ST["pre_exported"]:
                        await export_named("pre")
                        ST["pre_exported"] = True
                    found = scan_offers(p0, "P0", state, acts)
                    ST["window_assessed"] = True
                    ST["offered_reduced"] = bool(found)
                    ST["offered_reduced_detail"] = {
                        "turn": turn, "phase": phase,
                        "untapped_lands": sorted(ul),
                        "bear_oid": bear,
                        "bear_tapped": bear_tapped,
                        "offered_oids": sorted({o for o, _a in found})}
                    ST["bear_oid"] = bear
                    wire("window_assessed", ST["offered_reduced_detail"])
                    say(f"WINDOW ASSESSED: offered_reduced="
                        f"{ST['offered_reduced']} detail="
                        f"{json.dumps(ST['offered_reduced_detail'], sort_keys=True)}")
                    if found:
                        oid, action = found[0]
                        ST["end_oid"] = oid
                        ST["lands_tapped_at_cast"] = sorted(ul)
                        await submit_as_is(p0, action)
                        ST["end_cast"] = True
                        ST["end_in_flight"] = True
                        say(f"[P0] cast Magnificent End oid={oid} "
                            f"at REDUCED cost")
                        wire("end_cast", {"oid": oid})
                        return
                    else:
                        # release the land hold: drive to 5 mana for A4
                        ST["land_hold"] = False
                        say("[P0] not offered at reduced cost; releasing "
                            "land hold for the printed-cost control")
                        wire("land_hold_released", {})
            # --- printed-cost control: >=5 untapped lands, >=1 Plains ---
            if (ST["window_assessed"] and not ST["offered_reduced"]
                    and not ST["offered_printed"]
                    and phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)):
                ul = untapped_land_names(state, 0)
                if len(ul) >= 5 and PLAINS in ul:
                    found = scan_offers(p0, "P0", state, acts)
                    if found:
                        ST["offered_printed"] = True
                        ST["offered_printed_detail"] = {
                            "turn": turn, "phase": phase,
                            "untapped_lands": ul}
                        say(f"[P0] printed-cost control: offered at 5 mana "
                            f"(turn {turn})")
                        wire("printed_offer", ST["offered_printed_detail"])
                        await export_named("post")
                        ST["post_exported"] = True
                        await finish()
                        return
            # --- target selection for the cast path ---
            if (wtype == "TargetSelection" and wf_pending_for(state, 0)
                    and ST["end_in_flight"] and not ST["target_answered"]
                    and not ST["target_auto"]):
                if await answer_end_target(p0, st, state):
                    return
            # --- resolution tracking for the cast path ---
            if ST["end_in_flight"]:
                s = state
                if end_on_stack(s):
                    if not ST.get("end_on_stack_seen"):
                        ST["end_on_stack_seen"] = True
                        say("Magnificent End seen on the stack")
                        wire("end_on_stack", {})
                elif ST.get("end_on_stack_seen"):
                    in_gy = any(lname(s, int(oid)) == END
                                for oid, o in (s.get("objects", {})
                                               or {}).items()
                                if o.get("zone") == "Graveyard"
                                and o.get("controller") == 0)
                    ST["end_resolved"] = True
                    ST["end_resolved_at"] = time.time()
                    ST["end_in_flight"] = False
                    say(f"Magnificent End left the stack (in P0 gy={in_gy})")
                    wire("end_resolved", {"in_P0_gy": in_gy})
            if ST["end_resolved"] and not ST["post_exported"]:
                if (time.time() - (ST["end_resolved_at"] or 0) > 8
                        and my_priority(state, 0)):
                    await export_named("post")
                    ST["post_exported"] = True
                    await finish()
                    return
            if await discard_tick(p0, 0, "P0", st, state):
                return
            # --- land drops: build to exactly 2, then hold for the window ---
            # retry every tick while short (no per-turn flag: a missed drop
            # self-heals next tick; the engine enforces 1 land/turn)
            if (phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)):
                n_lands = len(bf_lands(state, 0))
                if n_lands < 2 or not ST["land_hold"]:
                    la = find_action(acts, "PlayLand")
                    if la:
                        await submit_as_is(p0, la)
                        say(f"[P0] plays land (now {n_lands + 1} in play)")
                        return
                    say(f"[P0] want land but no PlayLand advertised; "
                        f"n_lands={n_lands} hold={ST['land_hold']} "
                        f"actions={[a['type'] for a in acts][:10]}")
            # --- castability diagnostic while Magnificent End is in hand ---
            if (phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)
                    and END in hand_lnames(state, 0)):
                key = ("castdiag", turn, phase)
                if key not in castdiag_done:
                    castdiag_done.add(key)
                    casts = [(a["type"], (a.get("data") or {}).get("object_id"))
                             for a in acts if "cast" in a["type"].lower()]
                    say(f"[P0] castdiag t{turn} {phase}: untapped="
                        f"{untapped_land_names(state, 0)} casts={casts}")
            # default: pass priority
            if my_priority(state, 0):
                pa = find_action(acts, "PassPriority")
                if pa:
                    await submit_as_is(p0, pa)
                    return
        except Exception as e:
            obs["tick_errors"].append(f"P0: {e!r}")
            say(f"[P0] tick error: {e!r}")

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        try:
            if wtype == "MulliganDecision":
                if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                    await do_mulligan(p1, 1, "P1")
                    return
                if (find_action(acts, "SelectCards")
                        and last_select.get(1) != p1.revision):
                    last_select[1] = p1.revision
                    await do_bottom(p1, 1, "P1")
                    return
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await submit_as_is(p1, a)
                    return
            if wtype == "DeclareAttackers" and wf_pending_for(state, 1):
                da = find_action(acts, wtype)
                if da:
                    sub = copy.deepcopy(da)
                    bears = [oid for oid in bf_ids(state, 1, BEARS)]
                    sub["data"]["attacks"] = [
                        [oid, {"type": "Player", "data": 0}] for oid in bears]
                    sub["data"]["bands"] = []
                    await submit_as_is(p1, sub)
                    if bears:
                        ST["p1_attacked"] = True
                        say(f"[P1] attacks with bears {bears}")
                    return
            if wtype == "DeclareBlockers" and wf_pending_for(state, 1):
                da = find_action(acts, wtype)
                if da:
                    sub = copy.deepcopy(da)
                    sub["data"]["assignments"] = []
                    await submit_as_is(p1, sub)
                    return
            if await discard_tick(p1, 1, "P1", st, state):
                return
            # land drop every turn (retry each tick; engine enforces 1/turn)
            if (phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 1
                    and my_priority(state, 1)):
                la = find_action(acts, "PlayLand")
                if la:
                    await submit_as_is(p1, la)
                    say("[P1] plays land")
                    return
            # cast a bear when affordable and none on BF yet
            if (phase in ("PreCombatMain", "PostCombatMain")
                    and state.get("active_player") == 1
                    and my_priority(state, 1)
                    and not ST["end_in_flight"]):
                if not bf_ids(state, 1, BEARS):
                    for a in acts:
                        if a.get("type") != "CastSpell":
                            continue
                        d = a.get("data", {}) or {}
                        oid = d.get("object_id")
                        if isinstance(oid, int) and lname(state, oid) == BEARS:
                            await submit_as_is(p1, a)
                            ST["p1_bear_cast"] = True
                            say(f"[P1] casts Grizzly Bears oid={oid}")
                            return
            if my_priority(state, 1):
                pa = find_action(acts, "PassPriority")
                if pa:
                    await submit_as_is(p1, pa)
                    return
        except Exception as e:
            obs["tick_errors"].append(f"P1: {e!r}")
            say(f"[P1] tick error: {e!r}")

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
                return
            if c.latest is None:
                continue
            st = c.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 600:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"DIAG turn={s.get('turn_number')} "
                    f"active={s.get('active_player')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"pp={s.get('priority_player')} "
                    f"life={[life_of(s, i) for i in (0, 1)]} "
                    f"bear={ST['bear_oid']} attacked={ST['p1_attacked']} "
                    f"window={ST['window_assessed']} "
                    f"offered_red={ST['offered_reduced']} "
                    f"offered_pr={ST['offered_printed']} "
                    f"end={ST['end_cast']}/{ST['end_resolved']} "
                    f"lands0={untapped_land_names(s, 0)}")
            if (s.get("turn_number") or 0) > 24 and not ST["window_assessed"]:
                notes.append("watchdog: turn 24 reached with no window; "
                             "finishing")
                break
            if (ST["window_assessed"] and not ST["offered_reduced"]
                    and not ST["offered_printed"]
                    and (s.get("turn_number") or 0) > 24):
                notes.append("watchdog: turn 24 with no printed offer; "
                             "finishing")
                break
        if not ST["post_exported"]:
            notes.append("global timeout (600s) hit before post export")
    finally:
        p0t.cancel()
        p1t.cancel()
    await finish()


asyncio.run(main())
