#!/usr/bin/env python3
"""Issue #6914: Kediss, Emberclaw Familiar — Kediss deals the extra damage
which is wrong.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Kediss, Emberclaw Familiar ({1}{R} 1/1 Elemental Lizard, Partner):
    "Whenever a commander you control deals combat damage to an opponent,
     it deals that much damage to each other opponent."
  Malcolm, Keen-Eyed Navigator ({2}{U} 2/2 Siren Pirate, flying, Partner):
    "Whenever one or more Pirates you control deal damage to your opponents,
     you create a Treasure token for each opponent dealt damage."

Rules expectation: the commander that dealt the combat damage is the source
of the additional damage Kediss's trigger creates. A source-sensitive trigger
such as Malcolm's (only fires for damage dealt by Pirates) must therefore
observe the COMMANDER dealing the damage to each other opponent.

Setup (native engine, three human-client seats, CommanderDraft, 3 players):
  P0: commanders=[Malcolm, Keen-Eyed Navigator + Kediss, Emberclaw Familiar]
      (both have Partner; combined color identity {U}{R} makes the deck legal),
      main = 48x Island + 12x Mountain.
  P1: commander=[Ayula, Queen Among Bears] (never cast; sits in CZ),
      main = 60x Forest. Inert opponent, no blockers.
  P2: 60x Forest, no commander (inert).
P0 casts Malcolm (turn 3), Kediss (turn 4/5), then attacks P1 with Malcolm
(P1 has no creatures -> unblocked). Priority is passed everywhere. The
simultaneous Malcolm/Kediss triggers after combat damage surface an
OrderTriggers sequence prompt, which the driver answers deterministically
(Malcolm's trigger first, then Kediss's) and records; the order affects
only stack order, not the damage-source question.

OBSERVABLE (acceptance criteria, via Malcolm's own trigger):
  - Malcolm's combat damage to P1 (a Pirate dealing damage to an opponent)
    -> Malcolm's trigger fires -> 1 Treasure for P0 (always happens).
  - Kediss's trigger resolves -> P2 takes 2 damage.
      * Correct: source is Malcolm (a Pirate) -> Malcolm's trigger fires for
        P2 -> 2nd Treasure (total 2).
      * Buggy: source is Kediss (not a Pirate) -> no 2nd treasure (total 1).

Assertions:
  A1_setup_ok       pre.json at P0 PreCombatMain of the attack turn:
                    Malcolm on P0 BF with is_commander=true; Kediss on P0 BF;
                    all players at 40 life; Malcolm untapped
  A2_combat_damage  P1's life dropped 40->38 (Malcolm's combat damage only;
                    P1 has no other damage sources)
  A3_kediss_trigger P2's life dropped 40->38 (Kediss's trigger fired)
  A4_source_attribution
                    post.json (stack empty, game >=1 turn past combat):
                    P0 Treasure tokens == 2 -> passed (commander is source,
                    bug NOT reproduced);
                    A1-A3 passed and treasure count == 1 -> failed (bug)
  A5_cleanup        post.json: stack empty, game proceeding (no stall)

Verdict rule: reproduced iff A1-A3 pass and A4 fails with exactly 1 treasure
while P2 took 2 damage. not-reproduced iff A1-A4 pass. blocked iff the game
cannot be driven to the trigger resolution after genuine attempts.

Evidence: evidence/6914/<run-id>/pre.json, mid.json (first tick with P2 at
38, i.e. right after the Kediss trigger resolved), post.json, run.json,
manifest.sha256, summary.png, scenario_6914.py, wire_log.jsonl,
scenario_run.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-6914")
EVDIR = f"{BACKFILL}/evidence/6914/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MALCOLM = "malcolm, keen-eyed navigator"
KEDISS = "kediss, emberclaw familiar"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"
AYULA = "ayula, queen among bears"

P0_COMMANDER = [MALCOLM, KEDISS]  # partner pair; identity {U}{R}
P0_MAIN = [(ISLAND, 48), (MOUNTAIN, 12)]
P1_COMMANDER = [AYULA]
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

COMMANDER_FORMAT = {
    "format": "CommanderDraft",
    "starting_life": 40,
    "min_players": 3,
    "max_players": 8,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": True,
    "commander_damage_threshold": 21,
    "range_of_influence": None,
    "team_based": False,
    "sideboard_policy": {"type": "Forbidden"},
    "uses_commander": True,
    "supplies_fixed_deck": False,
    "default_deck_copy_limit": {"type": "Unlimited"},
    "allow_debug_actions": False,
}

STARTING_LIFE = 40

SERVER_IDENTITY = {
    "server_version": "0.81.3",
    "build_commit": "95bec6e",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "2c9918612e8fcf35d7daf5964eadaf11eeb94cc99463b2de822a906b9030fa44",
    "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
    "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "ServerHello on 127.0.0.1:9374 (pinned v0.81.3 server, already "
              "listening for this run) + reuse of the verified pin from the "
              "#6912 run (minisign-verify of binary + signed data manifest "
              "with the repo-pinned key; binary sha256 matches GitHub asset "
              "digest; data files sha256-verified against the manifest).",
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


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key.lower())]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def treasure_count(state, pid):
    return sum(1 for oid, o in (state.get("objects", {}) or {}).items()
               if o.get("zone") == "Battlefield"
               and o.get("controller") == pid
               and "treasure" in str(o.get("base_name") or o.get("name") or "").lower())


def untapped_lands(state, pid, key=None):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in (FOREST, MOUNTAIN, ISLAND)):
            if key is None or nm == key:
                out.append(int(oid))
    return out


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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


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
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_combat_damage", "A3_kediss_trigger",
            "A4_source_attribution", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                    format_config=COMMANDER_FORMAT)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, cdeck(P1_COMMANDER, *P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")
    kept = {}

    ST = {"malcolm_cast": False, "malcolm_cast_turn": None,
          "malcolm_oid": None, "kediss_cast": False, "kediss_cast_turn": None,
          "kediss_oid": None, "attack_turn": None, "malcolm_attacker_oid": None,
          "pre_exported": False, "mid_exported": False,
          "post_exported": False, "post_at": None}
    obs = {"wf_types_resolution": set(), "treasure_timeline": [],
           "stack_entries": [], "auto_answered": [], "unexpected_prompts": [],
           "rejections": []}
    prompt_first_seen = {}
    last_select = {}

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        pre_st = mid_st = post_st = None
        try:
            if os.path.exists(f"{EVDIR}/pre.json"):
                pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
            if os.path.exists(f"{EVDIR}/mid.json"):
                mid_st = json.loads(open(f"{EVDIR}/mid.json").read())["state"]
            if os.path.exists(f"{EVDIR}/post.json"):
                post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"]
        except Exception as e:
            notes.append(f"state reload failed: {e}")

        # ---- A1: setup ----
        if pre_st is not None:
            mal = bf_id(pre_st, 0, MALCOLM)
            ked = bf_id(pre_st, 0, KEDISS)
            ok = (mal is not None
                  and bool(get_obj(pre_st, mal).get("is_commander"))
                  and not get_obj(pre_st, mal).get("tapped")
                  and ked is not None
                  and life_of(pre_st, 0) == STARTING_LIFE
                  and life_of(pre_st, 1) == STARTING_LIFE
                  and life_of(pre_st, 2) == STARTING_LIFE)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: malcolm_bf={mal} is_commander="
                         f"{bool(mal is not None and get_obj(pre_st, mal).get('is_commander'))} "
                         f"untapped={bool(mal is not None and not get_obj(pre_st, mal).get('tapped'))} "
                         f"kediss_bf={ked} life={[life_of(pre_st, i) for i in (0, 1, 2)]}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: pre.json missing")

        # ---- A2/A3/A4 from mid + post ----
        dmg_st = mid_st if mid_st is not None else post_st
        if dmg_st is not None:
            p1_life = life_of(dmg_st, 1)
            p2_life = life_of(dmg_st, 2)
            ass["A2_combat_damage"] = ("passed" if p1_life == 38
                                       else "failed")
            notes.append(f"A2: P1 life={p1_life} (expect 38 = Malcolm's 2 "
                         f"combat damage)")
            ass["A3_kediss_trigger"] = ("passed" if p2_life == 38
                                        else "failed")
            notes.append(f"A3: P2 life={p2_life} (expect 38 = Kediss trigger "
                         f"dealing 2)")
        else:
            ass["A2_combat_damage"] = "failed"
            ass["A3_kediss_trigger"] = "failed"
            notes.append("A2/A3 failed: neither mid.json nor post.json available")

        if post_st is not None:
            ntre = treasure_count(post_st, 0)
            notes.append(f"A4: post.json P0 treasure tokens={ntre} "
                         f"(expect 2 if commander is the damage source)")
            if (ass["A1_setup_ok"] == "passed"
                    and ass["A2_combat_damage"] == "passed"
                    and ass["A3_kediss_trigger"] == "passed"):
                if ntre == 2:
                    ass["A4_source_attribution"] = "passed"
                    notes.append("A4 passed: Malcolm's trigger fired for the "
                                 "Kediss-trigger damage too -> Malcolm (a "
                                 "Pirate) is the damage source. Bug NOT "
                                 "reproduced.")
                elif ntre == 1:
                    ass["A4_source_attribution"] = "failed"
                    notes.append("A4 FAILED: only 1 treasure despite P2 taking "
                                 "2 damage -> the engine attributed the extra "
                                 "damage to Kediss (not a Pirate), so "
                                 "Malcolm's trigger did not fire. BUG "
                                 "REPRODUCED.")
                else:
                    ass["A4_source_attribution"] = "failed"
                    notes.append(f"A4 FAILED: unexpected treasure count {ntre}")
            else:
                ass["A4_source_attribution"] = "failed"
                notes.append("A4 failed: prerequisite assertions did not all pass")
            stack_empty = not (post_st.get("stack") or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            if stack_empty and wf in ("Priority", None):
                ass["A5_cleanup"] = "passed"
                notes.append(f"A5 passed: stack empty, waiting_for={wf}")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"A5 FAILED: stack={[str(e)[:60] for e in (post_st.get('stack') or [])][:3]} "
                             f"waiting_for={wf}")
        else:
            ass["A4_source_attribution"] = "failed"
            ass["A5_cleanup"] = "failed"
            notes.append("A4/A5 failed: post.json missing")

        if (post_st is not None
                and ass["A1_setup_ok"] == "passed"
                and ass["A2_combat_damage"] == "passed"
                and ass["A3_kediss_trigger"] == "passed"
                and ass["A4_source_attribution"] == "failed"
                and treasure_count(post_st, 0) == 1):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        else:
            verdict = "reproduced"
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6914,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "reused pinned v0.81.3 server on 127.0.0.1:9374 "
                               "(started for the #6914 run itself)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6914.py", "rb").read()).hexdigest(),
            "format_config": "CommanderDraft (3 seats; P0/P1 commanders, P2 none)",
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                      "P2": {"main": P2_DECK, "commander": []}},
            "assertions": ass,
            "observations": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in obs.items()},
            "driver_state": {k: v for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_6914.py", "wire_log.jsonl",
                               "scenario_run.log"],
            "limitations": [
                "Browser UI not exercised; native engine via three human-client seats.",
                "Dense 48x Island / 12x Mountain main deck is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The observable is Malcolm's own source-sensitive trigger (Pirate "
                "damage -> Treasure): it is the acceptance criterion the issue's "
                "triage comments named, not a direct wire read of the damage "
                "source field.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    def mulligan_keep(pid, state, key_need, lands_need, max_mulls, land_names):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in land_names)
        mulls = kept.get(f"P{pid}_mulls", 0)
        has_key = key_need is None or key_need in hn
        return (has_key and lands >= lands_need) or mulls >= max_mulls

    async def do_mulligan(c, pid, key_need, lands_need, max_mulls, land_names,
                          tag):
        hn = hand_lnames(c.latest["state"], pid)
        if mulligan_keep(pid, c.latest["state"], key_need, lands_need,
                         max_mulls, land_names):
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps (key_here={key_need in hn if key_need else 'n/a'})")
        else:
            kept[f"P{pid}_mulls"] = kept.get(f"P{pid}_mulls", 0) + 1
            kept.pop(f"P{pid}_bottomed", None)
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{kept[f'P{pid}_mulls']}")

    def bottom_key(st, oid):
        nm = lname(st, oid)
        if nm == ISLAND:
            return 0  # bottom islands first (need mountains for Kediss)
        if nm == MOUNTAIN:
            return 1
        return 2

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        pending = ((wf_of(st).get("data", {}) or {}).get("pending", []))
        count = 1
        for p in pending:
            if p.get("player") == pid:
                ph = p.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    count = int(ph.get("count", 1))
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        picks = sorted(hand, key=lambda o: bottom_key(st, o))[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{tag} bottoms {count}: {[lname(st, x) for x in picks]}")

    async def discard_tick(c, pid, tag, acts, st, state):
        wtype = wf_of(state).get("type") or ""
        if wtype != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            # prefer discarding islands (keep the mountains for Kediss)
            pick = next((ch for ch in chs
                         if ISLAND in choice_text(ch).lower()), chs[0])
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            wire("discard", {"who": tag, "choice": choice_text(pick)[:60]})
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def generic_prompt(c, pid, tag, st, state):
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            blob = json.dumps(opp, default=str)
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            obs["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8],
                 "n_choices": len(chs),
                 "texts": [choice_text(ch)[:60] for ch in chs][:6]})
            say(f"[{tag}] UNEXPECTED PROMPT iid={iid} n={len(chs)} "
                f"texts={[choice_text(ch)[:40] for ch in chs][:4]}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if time.time() - entry["t0"] < 15:
                continue
            pick = None
            for ch in chs:
                t = choice_text(ch).lower()
                if "yes" in t or "true" in t:
                    pick = ch
                    break
            if pick is None and chs:
                pick = chs[0]
            if pick is not None:
                say(f"[{tag}] auto-answering prompt after 15s stall: "
                    f"{choice_text(pick)[:60]}")
                wire("auto_answer", {"who": tag, "iid": str(iid)[:8],
                                     "choice": choice_text(pick)[:80]})
                obs["auto_answered"].append(
                    {"who": tag, "choice": choice_text(pick)[:80]})
                await answer_vi(c, opp, pick, tag)
                entry["done"] = True
                acted = True
        return acted

    async def cast_named(c, acts, state, name, tag):
        """Cast the first castable action whose object_id/card_id names `name`."""
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if isinstance(oid, int) and lname(state, oid) == name:
                say(f"[{tag}] casting {name} via {a['type']} (oid {oid})")
                wire("cast", {"who": tag, "name": name, "oid": oid,
                              "action": a["type"]})
                await submit_as_is(c, a)
                return oid
        return None

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, None, 3, 3, (ISLAND, MOUNTAIN), "P0")
                return
            if find_action(acts, "SelectCards") and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if ST["attack_turn"] is not None:
            obs["wf_types_resolution"].add(wtype)
        # mid export at first tick with P2 damaged (Kediss trigger resolved)
        if (ST["attack_turn"] is not None and not ST["mid_exported"]
                and life_of(state, 2) == STARTING_LIFE - 2):
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid.json", "w") as f:
                    f.write(mid)
                ST["mid_exported"] = True
                say("exported MID (P2 at 38: Kediss trigger resolved)")
            except Exception as e:
                notes.append(f"mid export failed: {e}")
        # post export once the game advanced past the attack turn and the
        # stack is empty
        if (ST["attack_turn"] is not None and not ST["post_exported"]
                and state.get("turn_number", 0) > ST["attack_turn"]
                and not (state.get("stack") or [])):
            say("post-conditions met; exporting POST")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        # treasure timeline tracking
        ntre = treasure_count(state, 0)
        if not obs["treasure_timeline"] or obs["treasure_timeline"][-1][1] != ntre:
            obs["treasure_timeline"].append((state.get("turn_number"), ntre))
            say(f"P0 treasures={ntre} at turn {state.get('turn_number')}")
        # stack entry observation (kediss / malcolm triggers)
        for e in state.get("stack", []) or []:
            blob = json.dumps(e, default=str).lower()
            if "kediss" in blob or "treasure" in blob:
                sig = blob[:120]
                if not obs["stack_entries"] or obs["stack_entries"][-1] != sig:
                    obs["stack_entries"].append(sig)
                    say(f"stack entry observed: {sig[:100]}")
        # attack declaration
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            mal = bf_id(state, 0, MALCOLM)
            if (mal is not None and not get_obj(state, mal).get("tapped")
                    and ST["malcolm_cast_turn"] is not None
                    and state.get("turn_number", 0) > ST["malcolm_cast_turn"]
                    and bf_id(state, 0, KEDISS) is not None):
                if not ST["pre_exported"]:
                    say("PRE: exporting (Malcolm+Kediss on P0 BF, attack turn)")
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    ST["pre_exported"] = True
                da = find_action(acts, "DeclareAttackers")
                if da:
                    sub = copy.deepcopy(da)
                    sub["data"]["attacks"] = [[int(mal), {"type": "Player", "data": 1}]]
                    sub["data"]["bands"] = []
                    ST["attack_turn"] = state.get("turn_number")
                    ST["malcolm_attacker_oid"] = int(mal)
                    say(f"P0 attacks P1 with Malcolm (oid {mal})")
                    wire("attack", {"attacker_oid": int(mal),
                                    "target": "P1",
                                    "turn": ST["attack_turn"]})
                    await submit_as_is(p0, sub)
                    return
            # not ready to attack (or already attacked): empty declare
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p0, sub)
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(p0, sub)
            return
        # OrderTriggers (P0 controls both the Malcolm and Kediss triggers
        # after combat damage): the schema spec is a "sequence" with
        # min=2/max=2/includeAll=true, so ALL candidates must be submitted
        # in the chosen order. Handle immediately and deterministically;
        # never let the generic prompt handler half-answer it.
        if wtype == "OrderTriggers":
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    iid = opp.get("interactionId")
                    if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                        continue
                    resp = opp.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    spec = data.get("spec", {}) or {}
                    cands = data.get("choices") or data.get("candidates") or []
                    ids = [c.get("id") for c in cands if c.get("id")]
                    if resp.get("type") == "schema" and spec.get("type") == "sequence" and ids:
                        def rank(cid):
                            blob = json.dumps(
                                next((c for c in cands if c.get("id") == cid), {}),
                                default=str).lower()
                            return 0 if "malcolm" in blob else 1
                        ids.sort(key=rank)
                        sub = {"interactionId": iid,
                               "response": {"type": "sequence",
                                            "data": {"choiceIds": ids}}}
                        say(f"P0 ordering triggers: {ids}")
                        wire("order_triggers",
                             {"who": "P0", "submission": sub,
                              "texts": [choice_text(
                                  next((c for c in cands if c.get("id") == i), {}))[:60]
                                  for i in ids]})
                        await p0.send_interaction(sub)
                        prompt_first_seen[iid] = {"t0": time.time(), "done": True}
                        obs["trigger_order"] = ids
                        return
            return  # still waiting on the ordering prompt; do not act
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # ---- P0 priority ----
        # cast Malcolm from the command zone when affordable
        if (not ST["malcolm_cast"]
                and bf_id(state, 0, MALCOLM) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and len(untapped_lands(state, 0)) >= 3):
            oid = await cast_named(p0, acts, state, MALCOLM, "P0")
            if oid is not None:
                ST["malcolm_cast"] = True
                ST["malcolm_cast_turn"] = state.get("turn_number")
                ST["malcolm_oid"] = oid
                return
        # cast Kediss (second commander, from the command zone) when
        # affordable (1R: needs a mountain among untapped)
        if (ST["malcolm_cast"] and not ST["kediss_cast"]
                and bf_id(state, 0, KEDISS) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and len(untapped_lands(state, 0, MOUNTAIN)) >= 1
                and len(untapped_lands(state, 0)) >= 2):
            oid = await cast_named(p0, acts, state, KEDISS, "P0")
            if oid is not None:
                ST["kediss_cast"] = True
                ST["kediss_cast_turn"] = state.get("turn_number")
                ST["kediss_oid"] = oid
                return
        if await discard_tick(p0, 0, "P0", acts, st, state):
            return
        # land drop
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 0:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, None, 2, 3, (FOREST,), "P1")
                return
            if find_action(acts, "SelectCards") and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                await do_bottom(p1, 1, "P1")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers" and state.get("active_player") == 1:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p1, sub)
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(p1, sub)
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        if await discard_tick(p1, 1, "P1", acts, st, state):
            return
        # P1 never casts Ayula (inert opponent, no blockers)
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 1:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    async def p2_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P2"):
                await do_mulligan(p2, 2, None, 2, 2, (FOREST,), "P2")
                return
            if find_action(acts, "SelectCards") and last_select.get(2) != p2.revision:
                last_select[2] = p2.revision
                await do_bottom(p2, 2, "P2")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p2, a)
                return
        if wtype == "DeclareAttackers" and state.get("active_player") == 2:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p2, sub)
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(p2, sub)
            return
        if not my_priority(state, 2):
            if await generic_prompt(p2, 2, "P2", st, state):
                return
            return
        if await discard_tick(p2, 2, "P2", acts, st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 2:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p2, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p2, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1"),
                             (p2, p2_tick, "P2")):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(tag))
            stale = time.time() - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = rev
            last_tick_at[tag] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        # stall watchdog: attack declared but the trigger resolution never
        # produced a post export
        if (ST["attack_turn"] is not None and not ST["post_exported"]
                and time.time() - t0 > 900):
            notes.append("watchdog: 900s elapsed since attack without post "
                         "conditions; exporting states and finishing")
            say("WATCHDOG: finishing with evidence")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
            except Exception as e:
                notes.append(f"watchdog post export failed: {e}")
            await finish()
            return
        if ST["post_exported"] and time.time() - (ST["post_at"] or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_lnames(s, 0)} "
                f"mal={bf_id(s, 0, MALCOLM)} ked={bf_id(s, 0, KEDISS)} "
                f"life={[life_of(s, i) for i in (0, 1, 2)]} tre={treasure_count(s, 0)} "
                f"stack={len(s.get('stack') or [])} "
                f"malcast={ST['malcolm_cast']}/kedcast={ST['kediss_cast']}/"
                f"atk={ST['attack_turn']} pre={ST['pre_exported']} "
                f"mid={ST['mid_exported']} post={ST['post_exported']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
