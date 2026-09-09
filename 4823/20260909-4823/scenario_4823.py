#!/usr/bin/env python3
"""Issue #4823: Parked replacement choice - pre-existing strand-freezes when
the chooser leaves the game (team-draw skip facet).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build per 2026-07-01, status:confirmed, area:engine,
mechanic:replacement-effects, priority:p0-softlock):
  When a player who owns a parked replacement choice
  (WaitingFor::ReplacementChoice) leaves the game, do_eliminate marks them out
  and the post-loop reconcile rewrites waiting_for to Priority{next} - but
  coupled continuation slots are left stranded because the engine only drains
  them via the (WaitingFor::ReplacementChoice, GameAction::ChooseReplacement)
  resume, which is now unreachable.

Facet 2 (this run): pending_team_draw_step -> next living player's draw
silently skipped. execute_draw (turns.rs:1319-1330) seeds [active_player]
only when the queue is empty; a draw-step draw can park on a CR 616.1
competing-draw replacement. If the drawer leaves, the stale [dead drawer]
entry survives, and the next living player's execute_draw sees a non-empty
queue -> does not seed -> drain_pending_team_draw_step (turns.rs:1345-1352)
drains the dead entry -> that player's turn-based CR 504.1 draw is skipped.

Setup: 3-player standard game (facets are 3+ player only: in 2-player games
eliminating the chooser ends the game via GameOver first).
  P0: 8x Stinkweed Imp (dredge 5), 8x Golgari Grave-Troll (dredge 6),
      8x Faithless Looting, 36x Mountain. Mulligan to Mountain + Looting +
      Imp + Troll; turn 1 Mountain, Looting, discard Imp + Troll.
  P1, P2: 60x Forest, do-nothing.
Trigger: P0's second turn Draw phase: the turn-based draw meets two
competing dredge replacements -> CR 616.1 -> WaitingFor::ReplacementChoice{0}
(ChooseReplacement advertised; exactChoices opportunity with choices
['0','1'], the two dredge replacement orderings - verified in the 2026-09-09
probe on v0.78.0).
Action: P0 does NOT answer. P0 submits GameAction::Concede { player_id: 0 },
which per engine source (crates/engine/src/types/actions.rs, v0.78.0) is
"always legal regardless of priority or WaitingFor state" and intentionally
absent from legal_actions enumeration (CR 104.3a).

Expected (bug): P0 eliminated; waiting_for rewritten to Priority{next};
pending_team_draw_step still holds [0]; on P1's next draw step execute_draw
does not seed [1] and drains the dead [0] entry -> P1 draws 0 cards.
Expected (fixed/correct): P1 draws 1 card at their draw step.

Assertions:
  A1 parked_choice ...... P0's Draw phase parks WaitingFor::ReplacementChoice
                          with player 0 as chooser; ChooseReplacement advertised;
                          the opportunity shows the two competing dredge
                          replacement candidates; pre.json exported.
  A2 concede_eliminates . P0's Concede accepted; P0 eliminated (waiting_for no
                          longer references player 0's choice; P0 never the
                          active player again); mid_concede.json exported.
  A3 game_continues ..... no softlock: P1's turn begins and advances past
                          Draw within 120s of the concede (60s watchdog for a
                          wait with no actionable submission never fires).
  A4 next_draw_skipped .. P1's turn-based draw at their first post-elimination
                          Draw phase: P1 hand/library unchanged across the
                          draw step (0 cards drawn) -> BUG reproduced. P1
                          drawing 1 -> not-reproduced.
  A5 cleanup ............ post.json: game proceeding (stack empty or game
                          advanced to P2's turn), no stall.

Verdict rule:
  reproduced ..... A1+A2+A3 pass and A4 shows P1 drew 0 (the reported skip).
  not-reproduced . A1+A2+A3 pass and A4 shows P1 drew exactly 1 (the engine
                   cleaned/resumed the slot on this build).
  blocked ........ setup gate never opens (no parked choice; Concede rejected
                   in all shapes; game softlocks before assertions).

Scope: facet 2 only. Facet 1 (pending_phase_transition_progress ->
phase-advance freeze via step-end empty-mana CR 616.1 choice) and facet 3
(pending_continuation -> deferred-trigger drain freeze) are not exercised;
recorded in limitations and the run note for future runs.

Evidence: evidence/4823/<run-id>/pre.json (parked choice), mid_concede.json,
post.json, run.json, manifest.sha256, summary.png, scenario_4823.py,
wire_log.jsonl, scenario_run.log.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-4823"
EVDIR = f"{BACKFILL}/evidence/4823/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

IMP = "Stinkweed Imp"
TROLL = "Golgari Grave-Troll"
LOOTING = "Faithless Looting"
MOUNTAIN = "Mountain"
FOREST = "Forest"

P0_DECK = [(IMP, 8), (TROLL, 8), (LOOTING, 8), (MOUNTAIN, 36)]
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "v0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "sha256 match of pinned verified release artifacts (ledger, 2026-09-09); "
              "fresh isolated server on 127.0.0.1:9374 from this run's run dir; "
              "ServerHello v0.78.0/4de7224/protocol 68 observed",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_names(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def gy_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("graveyard", [])]


def bf_ids(state, pid, name=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (name is None or lname(state, oid) == name.lower())]


def lib_count(state, pid):
    return len(player_of(state, pid).get("library", []))


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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parked_choice", "A2_concede_eliminates", "A3_game_continues",
            "A4_next_draw_skipped", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=3)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} P2={p2.player_id}")

    kept = {}
    looting_cast = False
    pre_exported = False
    post_exported = False
    mid_exported = False

    obs = {
        "parked_choice": None,     # {turn, revision, choices, choose_replacement_advertised}
        "concede_sent": False,
        "concede_accepted": False,
        "concede_rejections": [],
        "concede_at": None,
        "p0_player_record": None,  # raw player dict right after elimination
        "eliminated_players_mid": None,  # eliminated_players slot in mid export
        "team_draw_slot_pre": None,      # pending_team_draw_step in pre
        "team_draw_slot_mid": None,      # pending_team_draw_step in mid
        "team_draw_slot_post": None,     # pending_team_draw_step in post
        "p0_active_after": False,
        "saw_nonzero_active_post_concede": False,
        "stall_observed": False,
        "p1_turn": None,           # turn_number of P1's first post-elimination turn
        "p1_upkeep_counts": None,   # (hand, library) at P1 upkeep
        "p1_draw_counts": None,     # (hand, library) at first P1 Draw observation
        "p1_postdraw_counts": None,  # (hand, library) at P1 PreCombatMain
        "rejections": [],
        "choice_opportunity": None,
    }
    shapes_logged = set()
    submitted_iids = set()
    noact_wait_start = {}

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def wf_of(state):
        return state.get("waiting_for") or {}

    async def try_concede():
        """Submit Concede in engine-supported shape; returns True if accepted
        (no rejection observed within a short window is treated as sent)."""
        shapes = [
            {"type": "Concede", "data": {"player_id": 0}},
            {"type": "Concede"},
        ]
        for shape in shapes:
            # drain recent rejections first
            obs["concede_rejections"] = []
            await submit_as_is(p0, shape)
            say(f"P0 submits Concede shape={json.dumps(shape)}")
            wire("concede_submission", {"shape": shape})
            await asyncio.sleep(2.0)
            st = p0.latest
            if st is None:
                continue
            # check inbox for ActionRejected mentioning Concede
            rejected = False
            while True:
                try:
                    t, data = p0.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t in ("Error", "ActionRejected"):
                    blob = json.dumps(data)
                    obs["rejections"].append(blob[:300])
                    wire("rejection", {"type": t, "data": blob[:500]})
                    if "oncede" in blob:
                        rejected = True
                        obs["concede_rejections"].append(blob[:300])
                        say(f"Concede rejected: {blob[:200]}")
            if not rejected:
                return True
        return False

    async def mulligan_tick(c, pid, acts, state, tag):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_names(state, pid)
            mulls = kept.get(f"{tag}_mulls", 0)
            if tag == "P0":
                # Keep only if the post-bottom hand still holds the combo:
                # bottoming takes `mulls` cards (Mountains first), so we need
                # mulls+1 Mountains to keep one for casting Looting, plus
                # Looting + both dredge cards (never bottomed).
                n_mtn = hn.count(MOUNTAIN.lower())
                ok = (LOOTING.lower() in hn and IMP.lower() in hn
                      and TROLL.lower() in hn and n_mtn >= mulls + 1)
                if ok or mulls >= 5:
                    kept[tag] = True
                    await submit_as_is(c, {"type": "MulliganDecision",
                                           "data": {"choice": {"type": "Keep"}}})
                    say(f"{tag} keeps: {hn} (mulls={mulls}, combo_ok={ok})")
                else:
                    kept[f"{tag}_mulls"] = mulls + 1
                    await submit_as_is(c, {"type": "MulliganDecision",
                                           "data": {"choice": {"type": "Mulligan"}}})
                    say(f"{tag} mulligans #{mulls + 1}: {hn}")
            else:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps 7")
            return True
        if wf_of(state).get("type") == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(f"{tag}_bottomed"):
                pending = (wf_of(state).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if isinstance(ph, dict) and ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hids = hand_ids(state, pid)

                def bkey(oid):
                    nm = lname(state, oid)
                    return (0 if nm == MOUNTAIN.lower() else 1, nm)
                picks = sorted(hids, key=bkey)[:count]
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                wire("mulligan_bottom", {"who": tag, "count": count,
                                         "cards": [lname(state, x) for x in picks]})
                # only latch when the submission sticks (a count mismatch is
                # rejected invalid_action and must be retried next tick)
                await asyncio.sleep(1.5)
                rej = False
                while True:
                    try:
                        t, data = c.inbox.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if t in ("Error", "ActionRejected") \
                            and "electCards" in json.dumps(data):
                        rej = True
                        say(f"{tag} bottom rejected: "
                            f"{json.dumps(data)[:150]}")
                        wire("bottom_rejected", {"who": tag,
                                                 "data": str(data)[:200]})
                if not rej:
                    kept[f"{tag}_bottomed"] = True
                return True
        return False

    async def generic_decision(c, pid, tag, acts, state, wtype):
        if "Discard" in wtype:
            hids = hand_ids(state, pid)

            def rank(oid):
                nm = lname(state, oid)
                if nm in (IMP.lower(), TROLL.lower()):
                    return (0, nm)
                return (2, nm)
            hids.sort(key=rank)
            picks = hids[:2]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{tag} discards {[lname(state, x) for x in picks]}")
            wire("discard", {"who": tag,
                             "cards": [lname(state, x) for x in picks]})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
                say(f"{tag} submits advertised OrderTriggers")
                return True
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                dd = sub.setdefault("data", {})
                for k in ("attacks", "attackers", "blocks", "blockers",
                          "assignments"):
                    if k in dd:
                        dd[k] = [] if isinstance(dd[k], list) else {}
                await c.send_action({"type": wtype, "data": dd})
                say(f"{tag} declares empty {wtype}")
                return True
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(c, ad)
                say(f"{tag} submits advertised AssignCombatDamage")
                return True
        return False

    async def p0_tick(st, acts, state):
        nonlocal looting_cast, pre_exported
        if obs["concede_sent"]:
            return  # eliminated: observe only
        if await mulligan_tick(p0, 0, acts, state, "P0"):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        wtype = wf_of(state).get("type") or ""
        # --- the parked replacement choice: export pre, then concede ---
        if (wtype == "ReplacementChoice"
                and not pre_exported
                and state.get("phase") == "Draw"
                and state.get("active_player") == 0):
            wf = wf_of(state)
            chooser = wf.get("player", (wf.get("data") or {}).get("player"))
            cr = find_action(acts, "ChooseReplacement")
            vi = get_vi(st)
            opp_desc = None
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    resp = opp.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    chs = data.get("choices") or data.get("candidates") or []
                    texts = [choice_text(ch) for ch in chs]
                    spec = data.get("spec") or {}
                    opp_desc = {
                        "rtype": resp.get("type"),
                        "spec": spec.get("type") if isinstance(spec, dict) else None,
                        "n_choices": len(chs),
                        "choices": texts[:10],
                        "interactionId": str(opp.get("interactionId")),
                    }
                    wire("parked_choice_opportunity",
                         {"opportunity": json.loads(json.dumps(opp, default=str))})
                    break
            obs["parked_choice"] = {
                "turn": state.get("turn_number"),
                "revision": p0.revision,
                "chooser": chooser,
                "choose_replacement_advertised": cr is not None,
                "opportunity": opp_desc,
            }
            say(f"PARKED CHOICE rev={p0.revision} turn={state.get('turn_number')} "
                f"chooser={chooser} ChooseReplacement={cr is not None} "
                f"opp={json.dumps(opp_desc)}")
            wire("parked_choice", obs["parked_choice"])
            try:
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_exported = True
                say("PRE exported (parked ReplacementChoice{0} at P0 Draw phase)")
            except Exception as e:
                notes.append(f"pre export failed: {e}")
            obs["concede_at"] = time.time()
            accepted = await try_concede()
            obs["concede_sent"] = True
            obs["concede_accepted"] = accepted
            say(f"concede submitted; accepted(no-rejection)={accepted}")
            return
        if await generic_decision(p0, 0, "P0", acts, state, wtype):
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        phase = state.get("phase")
        hn = hand_names(state, 0)
        if (not looting_cast and LOOTING.lower() in hn
                and phase in ("PreCombatMain", "PostCombatMain")
                and any(lname(state, o) == MOUNTAIN.lower()
                        and not get_obj(state, o).get("tapped")
                        for o in bf_ids(state, 0))):
            for a in acts:
                if (a["type"] == "CastSpell"
                        and lname(state, a.get("data", {}).get("object_id"))
                        == LOOTING.lower()):
                    say("P0 casts Faithless Looting")
                    wire("cast_looting", a)
                    await submit_as_is(p0, a)
                    looting_cast = True
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                resp = opp.get("response", {}) or {}
                if resp.get("type") != "exactChoices":
                    continue
                chs = resp.get("data", {}).get("choices", []) or []
                pc = next((ch for ch in chs
                           if "pass" in choice_text(ch).lower()), None)
                if pc is not None and opp.get("interactionId") not in submitted_iids:
                    sub = {"interactionId": opp["interactionId"], "response":
                           {"type": "choose", "data": {"choiceId": pc.get("id")}}}
                    await p0.send_interaction(sub)
                    submitted_iids.add(opp["interactionId"])
                    return

    async def pN_tick(c, pid, tag):
        async def tick(st, acts, state):
            if await mulligan_tick(c, pid, acts, state, tag):
                return
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await submit_as_is(c, a)
                    return
            wtype = wf_of(state).get("type") or ""
            if await generic_decision(c, pid, tag, acts, state, wtype):
                return
            # A4 measurement: P1's first post-elimination turn
            if tag == "P1" and obs["concede_sent"] and obs["p1_turn"] is None \
                    and state.get("active_player") == 1:
                obs["p1_turn"] = state.get("turn_number")
                say(f"P1's post-elimination turn begins: turn {obs['p1_turn']}")
            if tag == "P1" and obs["p1_turn"] is not None \
                    and state.get("turn_number") == obs["p1_turn"] \
                    and state.get("active_player") == 1:
                counts = (len(hand_ids(state, 1)), lib_count(state, 1))
                ph = state.get("phase")
                if ph == "Upkeep" and obs["p1_upkeep_counts"] is None:
                    obs["p1_upkeep_counts"] = counts
                    say(f"P1 upkeep counts hand={counts[0]} lib={counts[1]}")
                    wire("p1_upkeep_counts", {"hand": counts[0],
                                              "library": counts[1]})
                elif ph == "Draw" and obs["p1_draw_counts"] is None:
                    obs["p1_draw_counts"] = counts
                    say(f"P1 draw-step counts hand={counts[0]} lib={counts[1]}")
                    wire("p1_draw_counts", {"hand": counts[0],
                                            "library": counts[1]})
                elif ph == "PreCombatMain" and obs["p1_postdraw_counts"] is None:
                    obs["p1_postdraw_counts"] = counts
                    say(f"P1 precombat counts hand={counts[0]} lib={counts[1]}")
                    wire("p1_postdraw_counts", {"hand": counts[0],
                                                "library": counts[1]})
            if wtype != "Priority" or state.get("priority_player") != pid:
                return
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
            for a in acts:
                if a["type"] == "PassPriority":
                    await submit_as_is(c, a)
                    return
        return tick

    def mesa_snapshot_unused(state, oid):
        return get_obj(state, oid)

    async def finish():
        nonlocal post_exported, mid_exported
        dur = time.time() - t_start
        # All exports via P0: P0's session created the game and stays host even
        # after elimination ("Only the game host can export authoritative state").
        if obs["concede_sent"] and not mid_exported:
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid_concede.json", "w") as f:
                    f.write(mid)
                mid_exported = True
            except Exception as e:
                notes.append(f"mid_concede export failed: {e}")
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"final post export failed: {e}")
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            mid_st = json.loads(open(f"{EVDIR}/mid_concede.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/mid_concede.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            pre_st, mid_st, post_st = None, None, None
            notes.append(f"state reload failed: {e}")

        # A1: parked choice
        pc = obs["parked_choice"]
        if pre_st is not None:
            obs["team_draw_slot_pre"] = pre_st.get("pending_team_draw_step")
        if pc is not None and pc.get("chooser") == 0 \
                and pc.get("choose_replacement_advertised") \
                and (pc.get("opportunity") or {}).get("n_choices", 0) >= 2 \
                and pre_st is not None \
                and (pre_st.get("waiting_for") or {}).get("type") == "ReplacementChoice":
            ass["A1_parked_choice"] = "passed"
            notes.append(f"A1 passed: pre.json shows WaitingFor::ReplacementChoice "
                         f"with chooser=0 at P0's Draw phase (turn {pc['turn']}); "
                         f"ChooseReplacement advertised; opportunity choices="
                         f"{(pc['opportunity'] or {}).get('choices')}; "
                         f"pending_team_draw_step={obs['team_draw_slot_pre']}")
        else:
            ass["A1_parked_choice"] = "failed"
            notes.append(f"A1 failed: parked_choice={json.dumps(pc)[:300]}; "
                         f"pre has ReplacementChoice="
                         f"{(pre_st.get('waiting_for') or {}).get('type') if pre_st else None}")

        # A2: concede eliminates P0
        p0_rec = None
        elim = None
        if mid_st is not None:
            p0_rec = player_of(mid_st, 0)
            obs["p0_player_record"] = {k: v for k, v in p0_rec.items()
                                       if k not in ("hand", "library", "graveyard")}
            elim = mid_st.get("eliminated_players")
            obs["eliminated_players_mid"] = elim
            obs["team_draw_slot_mid"] = mid_st.get("pending_team_draw_step")
        elim_flag = None
        if isinstance(p0_rec, dict):
            for k in ("eliminated", "out_of_game", "left_game", "is_out",
                      "conceded", "active"):
                if k in p0_rec:
                    elim_flag = (k, p0_rec[k])
                    break
        mid_wf = (mid_st.get("waiting_for") or {}).get("type") if mid_st else None
        if (obs["concede_sent"] and obs["concede_accepted"]
                and isinstance(elim, list) and 0 in elim
                and mid_wf != "ReplacementChoice"
                and not obs["p0_active_after"]):
            ass["A2_concede_eliminates"] = "passed"
            notes.append(f"A2 passed: Concede accepted (no rejection); "
                         f"eliminated_players={elim}; P0's parked choice gone "
                         f"(mid waiting_for={mid_wf}); P0 never the active "
                         f"player again; P0 player record flag="
                         f"{elim_flag or 'no explicit flag field'}; "
                         f"pending_team_draw_step(mid)={obs['team_draw_slot_mid']}")
            wire("p0_player_record_mid", p0_rec or {})
        else:
            ass["A2_concede_eliminates"] = "failed"
            notes.append(f"A2 failed: sent={obs['concede_sent']} "
                         f"accepted={obs['concede_accepted']} "
                         f"eliminated_players={elim} mid_wf={mid_wf} "
                         f"p0_active_after={obs['p0_active_after']} "
                         f"rejections={obs['concede_rejections'][:2]}")

        # A3: game continues (no softlock)
        if (obs["p1_turn"] is not None
                and obs["p1_postdraw_counts"] is not None
                and not obs["stall_observed"]):
            ass["A3_game_continues"] = "passed"
            notes.append(f"A3 passed: P1's turn {obs['p1_turn']} began after the "
                         f"concede and advanced past the Draw phase; no >60s "
                         f"actionless wait")
        else:
            ass["A3_game_continues"] = "failed"
            notes.append(f"A3 failed: p1_turn={obs['p1_turn']} "
                         f"postdraw={obs['p1_postdraw_counts']} "
                         f"stall={obs['stall_observed']}")

        # A4: P1's turn-based draw skipped?
        up, dr, pd = (obs["p1_upkeep_counts"], obs["p1_draw_counts"],
                      obs["p1_postdraw_counts"])
        if post_st is not None:
            obs["team_draw_slot_post"] = post_st.get("pending_team_draw_step")
        slot_note = (f"pending_team_draw_step: pre={obs['team_draw_slot_pre']} "
                     f"mid={obs['team_draw_slot_mid']} "
                     f"post={obs['team_draw_slot_post']}")
        wire("team_draw_slot", {"pre": obs["team_draw_slot_pre"],
                                "mid": obs["team_draw_slot_mid"],
                                "post": obs["team_draw_slot_post"]})
        if up is not None and dr is not None and pd is not None:
            drawn = dr[0] - up[0]          # hand delta by first Draw observation
            milled = up[1] - dr[1]        # library delta
            drawn2 = pd[0] - up[0]        # hand delta by PreCombatMain
            wire("p1_draw_deltas", {"by_draw_phase": drawn,
                                    "by_precombat": drawn2,
                                    "library_delta": milled})
            if drawn == 0 and drawn2 == 0 and milled == 0:
                ass["A4_next_draw_skipped"] = "passed"
                notes.append(f"A4 passed (BUG): P1's turn-based draw SKIPPED - "
                             f"hand {up[0]}->{dr[0]}->{pd[0]}, library "
                             f"{up[1]}->{dr[1]}->{pd[1]} across turn "
                             f"{obs['p1_turn']}'s draw step (expected +1 hand, "
                             f"-1 library per CR 504.1). {slot_note}")
            elif drawn == 1 or drawn2 == 1:
                ass["A4_next_draw_skipped"] = "failed"
                notes.append(f"A4 failed (no bug on this build): P1 drew normally - "
                             f"hand {up[0]}->{dr[0]}->{pd[0]}, library "
                             f"{up[1]}->{dr[1]}->{pd[1]}. {slot_note}")
            else:
                ass["A4_next_draw_skipped"] = "failed"
                notes.append(f"A4 inconclusive: unexpected deltas hand "
                             f"{up[0]}->{dr[0]}->{pd[0]} library "
                             f"{up[1]}->{dr[1]}->{pd[1]}. {slot_note}")
        else:
            ass["A4_next_draw_skipped"] = "failed"
            notes.append(f"A4 failed: missing counts upkeep={up} draw={dr} "
                         f"precombat={pd}. {slot_note}")

        # A5: cleanup
        if post_st is not None:
            slen = len(post_st.get("stack", []) or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            active = post_st.get("active_player")
            if not obs["stall_observed"] and active in (1, 2):
                ass["A5_cleanup"] = "passed"
                notes.append(f"A5 passed: game proceeding (active P{active}, "
                             f"turn={post_st.get('turn_number')}, "
                             f"phase={post_st.get('phase')}, stack={slen}, wf={wf})")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"A5 failed: active={active} stall={obs['stall_observed']} "
                             f"wf={wf} stack={slen}")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 failed: no post.json")

        if (ass["A1_parked_choice"] == "passed"
                and ass["A2_concede_eliminates"] == "passed"
                and ass["A3_game_continues"] == "passed"
                and ass["A4_next_draw_skipped"] == "passed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: the parked replacement choice was "
                         "eliminated with the chooser; the next living player's "
                         "turn-based draw was silently skipped on v0.78.0 "
                         "(pending_team_draw_step strand, facet 2 of #4823)")
        elif (ass["A1_parked_choice"] == "passed"
                and ass["A2_concede_eliminates"] == "passed"
                and ass["A3_game_continues"] == "passed"
                and "A4 failed (no bug on this build)" in " ".join(notes)):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: P1 drew normally after P0's "
                         "mid-choice elimination; the team-draw slot was cleaned "
                         "on v0.78.0")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: setup gate never opened or an "
                         "elimination/continuation step failed; no trustworthy "
                         "engine behavior observed")
        run = {
            "issue": 4823,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4823.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "setup_line": "3-player standard game. P0: 8x Stinkweed Imp (dredge 5) + "
                          "8x Golgari Grave-Troll (dredge 6) + 8x Faithless Looting + "
                          "36x Mountain (mulligan to Mountain+Looting+Imp+Troll; turn 1 "
                          "Mountain, Looting, discard Imp+Troll). P1/P2: 60x Forest, "
                          "do-nothing. At P0's 2nd-turn Draw phase the turn-based draw "
                          "parks on the CR 616.1 competing-dredge choice "
                          "(ReplacementChoice{0}); P0 concedes instead of answering.",
            "contract_line": "Facet 2 of #4823: after the chooser's mid-choice elimination, "
                             "the next living player's turn-based draw must still happen "
                             "(CR 504.1); a silent skip means pending_team_draw_step was "
                             "stranded on the dead player.",
            "limitations": [
                "Browser UI not exercised; native engine via three human-client seats.",
                "Only facet 2 (pending_team_draw_step -> next player's draw skipped) "
                "is tested. Facet 1 (pending_phase_transition_progress -> phase-advance "
                "freeze via a step-end empty-mana CR 616.1 choice) and facet 3 "
                "(pending_continuation -> deferred-trigger drain freeze) are not "
                "exercised: no parsed card in the pinned dataset offers the required "
                "competing empty-mana or deferred-drain replacement choices.",
                "The parked choice is the CR 616.1 ordering of two dredge replacements "
                "(the report's 'competing-draw replacement'); engine behavior for other "
                "616.1 pairs may differ.",
                "8x dredge-card / 8x looting deck density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "Not tested on the original 2026-07-01 build; verdict is scoped to "
                "v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
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

    t0 = time.time()
    p1t = await pN_tick(p1, 1, "P1")
    p2t = await pN_tick(p2, 2, "P2")
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1t, "P1"), (p2, p2t, "P2")):
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
            state = st["state"]
            # track whether P0 is ever the active player again post-concede.
            # Require observing a non-P0 active player first so a stale
            # pre-concede revision (active still 0) cannot false-positive.
            if obs["concede_sent"]:
                if state.get("active_player") != 0:
                    obs["saw_nonzero_active_post_concede"] = True
                elif obs["saw_nonzero_active_post_concede"]:
                    obs["p0_active_after"] = True
            # generic no-actionable-submission stall watchdog (60s)
            wf = (state.get("waiting_for") or {})
            wtype = wf.get("type") or ""
            wf_player = wf.get("player", (wf.get("data") or {}).get("player"))
            vi = st.get("viewer_interaction") or {}
            actionable = (vi.get("canSubmit")
                          or any(a["type"] not in ("PassPriority",)
                                 for a in merged_actions(st)))
            key = (tag, wtype, wf_player)
            if not actionable:
                if key not in noact_wait_start:
                    noact_wait_start[key] = time.time()
                elif time.time() - noact_wait_start[key] > 60:
                    obs["stall_observed"] = True
                    say(f"STALL: {tag} {wtype} (player {wf_player}) >60s with no "
                        f"actionable submission")
                    wire("stall", {"tag": tag, "wtype": wtype,
                                   "wf_player": wf_player})
                    try:
                        mid = await p0.export_state()
                        with open(f"{EVDIR}/mid_stall.json", "w") as f:
                            f.write(mid)
                    except Exception as e:
                        notes.append(f"mid_stall export failed: {e}")
                    await finish()
                    return
            else:
                noact_wait_start.pop(key, None)
            try:
                await tick(st, merged_actions(st), state)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        # mid export shortly after the concede once P1's turn has begun
        # (via P0: the game host; non-host exports are rejected)
        if (obs["concede_sent"] and not mid_exported
                and obs["p1_turn"] is not None):
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid_concede.json", "w") as f:
                    f.write(mid)
                mid_exported = True
                say("MID exported (post-concede, P1's turn begun)")
            except Exception as e:
                notes.append(f"mid_concede export failed: {e}")
        # done once P1's turn advanced past the draw step
        if obs["p1_postdraw_counts"] is not None and not post_exported:
            say("P1 post-draw counts captured; exporting post and finishing")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} looting={looting_cast} "
                f"parked={obs['parked_choice'] is not None} "
                f"conceded={obs['concede_sent']} p1_turn={obs['p1_turn']} "
                f"p1counts={obs['p1_upkeep_counts']}/{obs['p1_draw_counts']}/"
                f"{obs['p1_postdraw_counts']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
