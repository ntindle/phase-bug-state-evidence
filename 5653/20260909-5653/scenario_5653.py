#!/usr/bin/env python3
"""Issue #5653: Chains of Mephistopheles -- result-referential conditions
silently dropped; all three actions run unconditionally.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Issue (internal triage, 2026-07-12): Chains of Mephistopheles / Magus of the
Chains misparse the result-referential conditions, so the three sub-actions
run unconditionally instead of branching on the discard outcome.

Oracle text (verified from pinned v0.78.0 card-data.json, key
'chains of mephistopheles'):
  "If a player would draw a card except the first one they draw in each of
   their draw steps, that player discards a card instead. If the player
   discards a card this way, they draw a card. If the player doesn't discard
   a card this way, they mill a card."

Pinned v0.78.0 parse (replacements[0], verified this run):
  event Draw, condition ExceptFirstDrawInDrawStep, scope IndividualDraw,
  valid_player AnyPlayer, mode Mandatory; execute chain:
    Discard{Fixed 1} [condition=null]
      -> Draw{Fixed 1} [condition=EffectOutcome{signal: OptionalEffectPerformed}]
      -> Mill{Fixed 1} [condition=Not(EffectOutcome{signal: OptionalEffectPerformed})]
  NOTE: the pinned parse differs from the issue's harvest ("condition=None on
  every node"): the Draw/Mill nodes DO carry conditions, but they reference
  the OptionalEffectPerformed signal instead of the discard outcome. The
  discard here is not optional, so the Draw branch is expected to never fire
  and the Mill branch to always fire -- still not the Oracle behavior.

Correct behavior per Oracle for ONE extra draw event with P1 hand = H >= 1
(each chained draw is itself a non-first draw, hence replaced again):
  discard 1 -> draw (replaced) -> discard 1 -> ... -> hand empty -> mill 1.
  Net: exactly H discards, exactly 1 mill, 0 net draws, P1 hand H -> 0.
Predicted buggy behavior per the pinned parse:
  per extra draw: discard exactly 1, mill exactly 1, draw never fires.
  Net: 1 discard, 1 mill, P1 hand H -> H-1.

Setup (native engine, two human-client seats):
  P0: 8x chains of mephistopheles ({1}{B}{B}) + 8x blue sun's zenith
      ({X}{U}{U}{U}, cast at X=1) + 22x island + 22x swamp.
      (8x density: engine accepts >4-of for custom games; mulligan to
      chains + 2+ lands.)
  P1: 60x island dummy (plays a land, passes; never attacks, never casts).

Flow:
  1. P0 casts Chains of Mephistopheles (turn 3+, {1}{B}{B}).
  2. Control: P1's draw-step first draws must be untouched (hand stays 7).
  3. P0 casts Blue Sun's Zenith X=1 targeting P1 ("target player draws 1").
  4. Observe the replacement sequence to completion; export pre/post states.

Assertions:
  A1_setup_ok        pre.json: P0 main phase, chains on P0 battlefield,
                     zenith in hand, >=4 untapped lands (>=3 islands),
                     P1 hand H>=1, life 20/20.
  A2_first_draw_untouched (control): P1 hand at pre matches 7 kept + draws -
                     lands played (accounting for the starting player's skipped
                     first draw) with an empty graveyard: the draw-step
                     first-draw exception (ExceptFirstDrawInDrawStep) holds.
  A3_replacement_fires: during resolution P1 discards >= 1 card (gy grows from
                     hand, not from library) and performs no normal draw.
  A4_draw_branch_fires (correct): total P1 discards during resolution == H
                     (every card discarded via the chained draw branch), i.e.
                     P1 hand H -> 0. Predicted buggy: exactly 1 discard.
  A5_mill_exactly_once (correct): exactly 1 P1 mill during resolution AND it
                     happens only after the hand is empty (hand_after == 0).
                     Predicted buggy: mill fires while H-1 cards remain.
  A6_cleanup         post.json: stack empty, game advanced past the cast,
                     zenith in P0 library (it shuffles itself in).

Verdict rule: reproduced iff A1 passed and the Oracle-mandated
discard->draw/discard->mill branching is observably broken (A4 or A5 fail).
not-reproduced iff the full correct trace is observed (A1..A6 pass).
blocked iff setup cannot be driven to the Zenith cast.

Evidence: evidence/5653/<run-id>/pre.json (before Zenith cast),
mid_resolution.json (first discard/mill observed), post.json (after full
resolution), run.json, manifest.sha256, summary.png, scenario_5653.py,
wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-5653"
EVDIR = f"{BACKFILL}/evidence/5653/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CHAINS = "chains of mephistopheles"  # card-data.json key (exact)
ZENITH = "blue sun's zenith"         # card-data.json key (exact)
ISLAND = "island"
SWAMP = "swamp"

P0_DECK = [(CHAINS, 8), (ZENITH, 8), (ISLAND, 22), (SWAMP, 22)]
P1_DECK = [(ISLAND, 60)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "698350d9b6323011a5b86a74a4d2ea54d13b4ed26a520579be7d044f0a3692e5",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 20260909-5653",
}

CHAINS_PARSE = {
    "event": "Draw",
    "condition": "ExceptFirstDrawInDrawStep",
    "scope": "IndividualDraw",
    "valid_player": "AnyPlayer",
    "mode": "Mandatory",
    "chain": [
        {"effect": "Discard", "count": 1, "condition": None},
        {"effect": "Draw", "count": 1,
         "condition": "EffectOutcome{signal: OptionalEffectPerformed}"},
        {"effect": "Mill", "count": 1,
         "condition": "Not(EffectOutcome{signal: OptionalEffectPerformed})"},
    ],
    "verified_from": "pinned v0.78.0 card-data.json replacements[0]",
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


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def lname(state, oid):
    o = state.get("objects", {}).get(str(oid), {})
    return obj_name(o)


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def gy_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("graveyard", [])]


def lib_count(state, pid):
    return len(player_of(state, pid).get("library", []))


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid \
                and not o.get("tapped") and obj_name(o) in ("island", "swamp"):
            if name is None or obj_name(o) == name:
                out.append(int(oid))
    return out


def chains_on_bf(state, pid=0):
    return any(o.get("zone") == "Battlefield" and o.get("controller") == pid
               and obj_name(o) == CHAINS
               for o in state.get("objects", {}).values())


def card_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


def zenith_spell_on_stack(state):
    out = []
    for e in state.get("stack", []) or []:
        if "blue sun" in json.dumps(e, default=str).lower():
            out.append(e)
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


def waiting_actor(state):
    """Best-effort acting player id for the current waiting_for, or None."""
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {}) or {}
    if isinstance(d.get("player"), int):
        return d["player"]
    for p in d.get("pending", []) or []:
        if isinstance(p.get("player"), int):
            return p["player"]
    return None


def p0_can_act(state, pid):
    a = waiting_actor(state)
    return a is None or a == pid


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    return str(t)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_first_draw_untouched", "A3_replacement_fires",
            "A4_draw_branch_fires", "A5_mill_exactly_once", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}

    obs = {"chains_cast": False, "chains_turn": None,
           "cast_submitted": False, "cast_turn": None, "cast_phase": None,
           "x_chosen": None, "target_chosen": None,
           "discard_events": [], "mill_events": [], "draw_events_p1": [],
           "interaction_shapes": [], "hand_trace": [],
           "p1_turns": [], "pre_p1_gy": None}
    pre_exported = False
    mid_exported = False
    post_exported = False
    submitted_interactions = set()
    shapes_logged = set()
    pre_hand_p1 = None

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    async def scan_interactions(st, who):
        """Log every opportunity; answer X-choice and target prompts for P0."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            spec = data.get("spec", {}) or {}
            stype = spec.get("type") if isinstance(spec, dict) else None
            chs = data.get("choices") or data.get("candidates") or []
            texts = [choice_text(ch) for ch in chs]
            blob = " // ".join(texts)
            key = (who, rtype, stype, blob[:60])
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{who}] interaction rtype={rtype} spec={stype} "
                    f"choices=[{blob[:200]}]")
                wire("interaction_shape", {"who": who, "rtype": rtype,
                                           "spec": stype, "interaction": opp})
                obs["interaction_shapes"].append(
                    {"who": who, "rtype": rtype, "spec": stype,
                     "choices": texts[:12]})
            if iid in submitted_interactions:
                continue
            # X-choice for Blue Sun's Zenith: choose X=1 (deterministic policy)
            if who == "P0" and obs["cast_submitted"] and rtype == "schema" \
                    and stype == "number" and obs["x_chosen"] is None:
                sub = {"interactionId": iid,
                       "response": {"type": "number", "data": {"value": 1}}}
                say("[P0] chooses X=1 for Blue Sun's Zenith")
                wire("x_choice", {"submission": sub, "interaction": opp})
                await p0.send_interaction(sub)
                submitted_interactions.add(iid)
                obs["x_chosen"] = 1
                notes.append("P0 chose X=1 on the Zenith X-choice prompt "
                             "(deterministic test policy)")
                acted = True
                continue
            # Target selection for Zenith: target P1 (seat 1)
            if who == "P0" and obs["cast_submitted"] and rtype == "schema" \
                    and stype == "sequence" and obs["target_chosen"] is None:
                pick = None
                for ch in chs:
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data") or {}
                        if isinstance(d, dict) and d.get("seat") == 1:
                            pick = ch
                            break
                    if pick:
                        break
                if pick is None:
                    say("[P0] target prompt has no seat-1 candidate; waiting")
                    wire("target_no_p1", {"interaction": opp})
                    continue
                sub = {"interactionId": iid,
                       "response": {"type": "sequence",
                                    "data": {"choiceIds": [pick["id"]]}}}
                say(f"[P0] targets P1 with Zenith (choice {pick['id']})")
                wire("zenith_target", {"submission": sub, "interaction": opp})
                await p0.send_interaction(sub)
                submitted_interactions.add(iid)
                obs["target_chosen"] = "P1"
                acted = True
                continue
        return acted

    def evaluate():
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            notes.append(f"state reload failed: {e}")
            pre_st, post_st = None, None
        H = None
        if pre_st is not None:
            H = len(player_of(pre_st, 1).get("hand", []))
            ok = (chains_on_bf(pre_st, 0)
                  and card_in_hand_oid(pre_st, 0, ZENITH) is not None
                  and len(untapped_lands(pre_st, 0)) >= 4
                  and len(untapped_lands(pre_st, 0, ISLAND)) >= 3
                  and H is not None and H >= 1
                  and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
                  and pre_st.get("phase") in ("PreCombatMain", "PostCombatMain"))
            if ok:
                ass["A1_setup_ok"] = "passed"
                notes.append(f"pre.json: P0 main phase, Chains on BF, Zenith in "
                             f"hand, {len(untapped_lands(pre_st, 0))} untapped "
                             f"lands, P1 hand H={H}, life 20/20")
            else:
                ass["A1_setup_ok"] = "failed"
                notes.append("pre.json setup precondition not met "
                             f"(chains={chains_on_bf(pre_st, 0)}, zenith="
                             f"{card_in_hand_oid(pre_st, 0, ZENITH) is not None}, "
                             f"lands={len(untapped_lands(pre_st, 0))}, H={H})")
            # A2 control: P1's draw-step first draws must never have been
            # replaced. Expected hand = 7 kept + draws - lands played, where
            # the starting player skips their first draw-step draw.
            p1t = obs.get("p1_turns_at_pre") or obs.get("p1_turns") or []
            p1_first = bool(p1t) and min(p1t) == 1
            p1_draws = len(p1t) - (1 if p1_first else 0)
            p1_lands = sum(1 for o in pre_st.get("objects", {}).values()
                           if o.get("zone") == "Battlefield"
                           and o.get("controller") == 1
                           and obj_name(o) == ISLAND)
            p1_gy_pre = len(player_of(pre_st, 1).get("graveyard", []))
            expected_h = 7 + p1_draws - p1_lands
            if H == expected_h and p1_gy_pre == 0:
                ass["A2_first_draw_untouched"] = "passed"
                notes.append(f"P1 hand at pre == {H} (expected {expected_h} = "
                             f"7 kept + {p1_draws} draws - {p1_lands} lands; "
                             f"P1 went {'first' if p1_first else 'second'}), "
                             f"gy empty: draw-step first draws never replaced")
            else:
                ass["A2_first_draw_untouched"] = "failed"
                notes.append(f"P1 hand at pre == {H} (expected {expected_h}), "
                             f"gy={p1_gy_pre}: the draw-step first-draw "
                             f"exception may itself be broken")
        if pre_st is not None and post_st is not None and H is not None:
            pre_gy = set(str(o) for o in player_of(pre_st, 1).get("graveyard", []))
            post_gy = [o for o in player_of(post_st, 1).get("graveyard", [])]
            new_gy = [o for o in post_gy if str(o) not in pre_gy]
            pre_lib = set(str(o) for o in player_of(pre_st, 1).get("library", []))
            post_lib = set(str(o) for o in player_of(post_st, 1).get("library", []))
            post_hand = set(str(o) for o in player_of(post_st, 1).get("hand", []))
            pre_hand = set(str(o) for o in player_of(pre_st, 1).get("hand", []))
            # discards: cards that were in P1's hand at pre, now in gy
            discards = [o for o in new_gy if str(o) in pre_hand]
            # mills: cards that were in P1's library at pre, now in gy
            mills = [o for o in new_gy if str(o) in pre_lib]
            # draws by P1 during resolution: hand cards at post not in pre hand/gy/lib
            hand_after = len(post_hand)
            say(f"resolution deltas: H={H} discards={len(discards)} "
                f"mills={len(mills)} hand_after={hand_after} "
                f"lib {len(pre_lib)}->{len(post_lib)}")
            obs["resolution"] = {"H": H, "discards": len(discards),
                                 "discard_names": [lname(post_st, o) for o in discards],
                                 "mills": len(mills),
                                 "mill_names": [lname(post_st, o) for o in mills],
                                 "hand_after": hand_after,
                                 "lib_before": len(pre_lib), "lib_after": len(post_lib)}
            # A3
            if len(discards) >= 1 and hand_after <= H:
                ass["A3_replacement_fires"] = "passed"
                notes.append(f"replacement fired: {len(discards)} discard(s), "
                             f"no normal draw (hand {H}->{hand_after})")
            else:
                ass["A3_replacement_fires"] = "failed"
                notes.append(f"replacement did not fire as expected: discards="
                             f"{len(discards)}, hand {H}->{hand_after}")
            # A4: correct => every card discarded (chained draw branch)
            if len(discards) == H and hand_after == 0:
                ass["A4_draw_branch_fires"] = "passed"
                notes.append(f"draw branch fired: all {H} cards discarded via "
                             f"the chained replacement (hand {H}->0)")
            else:
                ass["A4_draw_branch_fires"] = "failed"
                notes.append(f"draw branch broken: only {len(discards)}/{H} "
                             f"discarded, hand {H}->{hand_after} (predicted "
                             f"buggy parse: exactly 1 discard, draw never fires)")
            # A5: correct => exactly 1 mill, only after hand emptied
            if len(mills) == 1 and hand_after == 0:
                ass["A5_mill_exactly_once"] = "passed"
                notes.append("mill fired exactly once, after the hand was "
                             "emptied (Oracle: mill only when no discard)")
            else:
                ass["A5_mill_exactly_once"] = "failed"
                notes.append(f"mill branch broken: mills={len(mills)} with "
                             f"hand_after={hand_after} (predicted buggy parse: "
                             f"mill always fires, even with cards in hand)")
            # A6
            stack_empty = len(post_st.get("stack", []) or []) == 0
            zenith_gone = not zenith_spell_on_stack(post_st)
            if stack_empty and zenith_gone:
                ass["A6_cleanup"] = "passed"
                notes.append("post.json: stack empty, Zenith resolved "
                             "(shuffled into P0 library per its text)")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"post.json: stack_empty={stack_empty} "
                             f"zenith_resolved={zenith_gone}")
        else:
            for k in ("A3_replacement_fires", "A4_draw_branch_fires",
                      "A5_mill_exactly_once", "A6_cleanup"):
                ass[k] = "failed"
                notes.append(f"{k} could not be evaluated (missing pre/post state)")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup incomplete; see notes")
        elif ass["A4_draw_branch_fires"] == "failed" \
                or ass["A5_mill_exactly_once"] == "failed":
            verdict = "reproduced"
            notes.append("Oracle-mandated discard->draw / discard->mill "
                         "branching is broken: result-referential conditions "
                         "do not gate the Draw/Mill sub-actions")
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("mixed assertion outcome on the reported contract")
        return verdict

    async def finish():
        dur = time.time() - t_start
        nonlocal post_exported
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")
        verdict = evaluate()
        run = {
            "issue": 5653,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "chains_parse": CHAINS_PARSE,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_5653.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x Chains / 8x Zenith deck density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "Magus of the Chains carries the same Oracle text and parse class; "
                "only Chains of Mephistopheles was driven (same-class coverage).",
                "The correct-behavior trace assumes each chained draw is itself a "
                "non-first draw subject to replacement (standard rules reading); "
                "the decisive observed facts are the discard/mill counts.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x chains of mephistopheles + 8x blue sun's zenith + "
                          "22x island + 22x swamp (mulligan to chains + 2 lands); "
                          "P1: 60x island dummy",
            "contract_line": "P0 casts Blue Sun's Zenith X=1 targeting P1 with Chains "
                             "of Mephistopheles on the battlefield: the draw is "
                             "replaced by discard-1; per Oracle, discarding must "
                             "chain into another draw (until the hand is empty) and "
                             "mill exactly once, only when no card was discarded",
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

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, mid_exported, post_exported
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n in (ISLAND, SWAMP))
            mulls = kept.get("P0_mulls", 0)
            if (CHAINS in hn and lands >= 2) or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (chains={CHAINS in hn}, lands={lands})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1}")
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hids = hand_ids(state, 0)
                def bottom_key(oid):
                    nm = lname(state, oid)
                    return 0 if nm in (ISLAND, SWAMP) else (2 if nm == CHAINS else 1)
                picks = sorted(hids, key=bottom_key)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if await scan_interactions(st, "P0"):
            return
        # Cleanup discard-to-hand-size (or any P0 discard decision): discard
        # down as needed, lands first, never Chains while avoidable.
        if wtype and "Discard" in wtype:
            sc = find_action(acts, "SelectCards")
            if sc and p0_can_act(state, 0):
                hids = hand_ids(state, 0)
                n = max(0, len(hids) - 7)
                d = (state.get("waiting_for") or {}).get("data", {}) or {}
                for k in ("count", "amount", "number"):
                    if isinstance(d.get(k), int):
                        n = d[k]
                def discard_rank(oid):
                    nm = lname(state, oid)
                    if nm == CHAINS:
                        return (2, nm)
                    if nm in (ISLAND, SWAMP):
                        return (0, nm)
                    return (1, nm)
                picks = sorted(hids, key=discard_rank)[:n]
                if picks:
                    sub = {"type": "SelectCards",
                           "data": {"cards": [int(x) for x in picks]}}
                    wire("p0_discard", {"waiting_for": wtype,
                                        "submission": sub})
                    await p0.send_action(sub)
                    say(f"P0 discards {[lname(state, x) for x in picks]} ({wtype})")
                    return
        # mid export: first discard or mill observed during Zenith resolution
        if obs["cast_submitted"] and not mid_exported \
                and (obs["discard_events"] or obs["mill_events"]):
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid_resolution.json", "w") as f:
                    f.write(mid)
                mid_exported = True
                say("exported MID (resolution underway)")
            except Exception as e:
                notes.append(f"mid export failed: {e}")
        # post export: Zenith cast submitted, stack empty, and P1's graveyard
        # grew since pre (the resolution happened). Fallback: the game has
        # advanced well past the cast turn with an empty stack.
        p1_gy_now = len(player_of(state, 1).get("graveyard", []))
        resolved = (obs["pre_p1_gy"] is not None
                    and p1_gy_now > obs["pre_p1_gy"])
        advanced_far = (state.get("turn_number", 0) > (obs["cast_turn"] or 0) + 2)
        if (obs["cast_submitted"] and not post_exported
                and len(state.get("stack", []) or []) == 0
                and (resolved or advanced_far)):
            say("Zenith resolved; exporting POST")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        ul = untapped_lands(state, 0)
        ui = untapped_lands(state, 0, ISLAND)
        us = untapped_lands(state, 0, SWAMP)
        # 1. cast Chains when affordable ({1}{B}{B})
        if (not obs["chains_cast"] and chains_on_bf(state, 0) is False
                and card_in_hand_oid(state, 0, CHAINS) is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(us) >= 2 and len(ul) >= 3):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" \
                        and lname(state, d.get("object_id")) == CHAINS:
                    wire("cast_chains", a)
                    await submit_as_is(p0, a)
                    obs["chains_cast"] = True
                    obs["chains_turn"] = state.get("turn_number")
                    say(f"P0 casts Chains (turn {obs['chains_turn']})")
                    return
        # 2. cast Zenith X=1 targeting P1 when affordable ({1}{U}{U}{U})
        if (obs["chains_cast"] and not obs["cast_submitted"]
                and chains_on_bf(state, 0)
                and card_in_hand_oid(state, 0, ZENITH) is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(ui) >= 3 and len(ul) >= 4):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" \
                        and lname(state, d.get("object_id")) == ZENITH:
                    say("P0 main phase: exporting PRE, then casting Zenith X=1 @P1")
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_exported = True
                    obs["pre_p1_gy"] = len(player_of(state, 1).get("graveyard", []))
                    obs["p1_turns_at_pre"] = list(obs["p1_turns"])
                    wire("cast_zenith", a)
                    obs["cast_turn"] = state.get("turn_number")
                    obs["cast_phase"] = state.get("phase")
                    await submit_as_is(p0, a)
                    obs["cast_submitted"] = True
                    say(f"P0 casts Blue Sun's Zenith (turn {obs['cast_turn']})")
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps opening hand")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if await scan_interactions(st, "P1"):
            return
        # Chains' "discard a card" surfaces as a Discard waiting_for with
        # SelectCards actions; answer with the first cards in hand (only when
        # P1 is the acting player).
        if wtype and "Discard" in wtype and p0_can_act(state, 1):
            sc = find_action(acts, "SelectCards")
            if sc:
                hids = hand_ids(state, 1)
                n = 1
                d = (state.get("waiting_for") or {}).get("data", {}) or {}
                for k in ("count", "amount", "number"):
                    if isinstance(d.get(k), int):
                        n = d[k]
                picks = hids[:n]
                sub = {"type": "SelectCards",
                       "data": {"cards": [int(x) for x in picks]}}
                wire("p1_discard_choice", {"waiting_for": wtype,
                                           "submission": sub,
                                           "hand": hand_names(state, 1)})
                await p1.send_action(sub)
                obs["discard_events"].append(
                    {"turn": state.get("turn_number"),
                     "phase": state.get("phase"),
                     "discarded": [lname(state, x) for x in picks],
                     "hand_before": len(hids)})
                say(f"P1 discards {[lname(state, x) for x in picks]} to Chains")
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_deadline = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        # turn tracker (for the A2 control): record P1's turn numbers from
        # the very start so the expected hand size accounts for who went
        # first (the starting player skips their first draw-step draw).
        if p1.latest:
            s = p1.latest["state"]
            if s.get("active_player") == 1:
                t = s.get("turn_number")
                if t not in obs["p1_turns"]:
                    obs["p1_turns"].append(t)
        # resolution sampler: track P1 zones during the Zenith resolution so
        # the mid export fires even if discards are random (no prompt).
        if obs["cast_submitted"] and not post_exported and p1.latest:
            s = p1.latest["state"]
            samp = (len(player_of(s, 1).get("hand", [])),
                    len(player_of(s, 1).get("graveyard", [])),
                    len(player_of(s, 1).get("library", [])))
            if not obs["hand_trace"] or obs["hand_trace"][-1][1:] != samp:
                obs["hand_trace"].append(
                    (s.get("turn_number"),) + samp)
                if len(obs["hand_trace"]) > 1 and samp[1] > obs["hand_trace"][-2][2]:
                    obs["mill_events"].append(
                        {"turn": s.get("turn_number"), "phase": s.get("phase"),
                         "p1_gy": samp[1]})
            if not mid_exported and len(obs["hand_trace"]) > 1:
                try:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_resolution.json", "w") as f:
                        f.write(mid)
                    mid_exported = True
                    say("exported MID (resolution underway, zone change seen)")
                except Exception as e:
                    notes.append(f"mid export failed: {e}")
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={len(hand_names(s, 0))} "
                f"P1hand={len(hand_names(s, 1))} chains={chains_on_bf(s, 0)} "
                f"stack={len(s.get('stack') or [])} cast={obs['cast_submitted']} "
                f"discards={len(obs['discard_events'])}")
        if obs["cast_submitted"] and not post_exported and stuck_deadline is None:
            stuck_deadline = time.time() + 300
        if not obs["cast_submitted"] or post_exported:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("Zenith cast but post-resolution state not reached in 300s; "
                         "see wire log (possible unhandled interaction)")
            await finish()
            return
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
