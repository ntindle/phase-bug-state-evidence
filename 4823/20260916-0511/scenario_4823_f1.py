#!/usr/bin/env python3
"""Issue #4823 facet 1: parked replacement choice strands
pending_phase_transition_progress -> phase-advance freeze when the chooser
leaves the game.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (phase-rs/phase#4823, status:confirmed, area:engine,
mechanic:replacement-effects, priority:p0-softlock):
  When a player who owns a parked replacement choice
  (WaitingFor::ReplacementChoice) leaves the game, do_eliminate marks them out
  and the post-loop reconcile rewrites waiting_for to Priority{next} - but the
  coupled continuation slot pending_phase_transition_progress is left stranded
  because the engine only drains it via the
  (WaitingFor::ReplacementChoice, GameAction::ChooseReplacement) resume, which
  is now unreachable. auto_advance early-returns while PPT is Some, so the
  phase transition never completes -> soft-lock. 3+ player only.

Facet 1 (this run): pending_phase_transition_progress -> phase-advance freeze.
A step-end empty-mana drain (CR 703.4q/500.5) that pauses on a CR 616.1
EmptyManaPool ordering choice retains the rest of the APNAP queue in PPT while
parking ReplacementChoice{player}. If that player leaves, PPT is stranded.

Setup: 3-player game (facets are 3+ player only: in 2-player games eliminating
the chooser ends the game via GameOver first).
  P0: 36x Forest + 8x Upwelling + 8x Horizon Stone + 8x Elvish Spirit Guide
      (density is a test-harness convenience - the engine accepts >4-of for
      custom games).
      Upwelling: "Players don't lose unspent mana as steps and phases end."
      Horizon Stone: "If unspent mana would empty from your mana pool, that
      mana becomes colorless instead."
      Both are step-end unspent-mana replacement handlers; when P0's pool
      empties with mana in it, both apply to the same EmptyManaPool event ->
      CR 616.1 ordering choice parked for P0 (affected player), APNAP queue
      [P1, P2] retained in pending_phase_transition_progress.
      (Mana source: Elvish Spirit Guide's hand-activated ability, exiled from
      hand for {G}. Basic-land mana abilities are NOT advertised as
      activatable actions on protocol 71 - verified by diagnostic 2026-09-16;
      the ESG ability is, verified by probe 2026-09-16.)
  P1, P2: 60x Forest, do-nothing.
Trigger: once both enchantments/artifacts are on the battlefield, P0 exiles
an Elvish Spirit Guide from hand for {G} (floats it), passes priority in
PreCombatMain; all players pass -> engine enters BeginCombat -> PPT drain
pops P0 -> EmptyManaPool proposed -> replace_event finds 2 handlers ->
NeedsChoice -> ReplacementChoice{0} parked, PPT retains [P1, P2].
Action: P0 does NOT answer. P0 submits GameAction::Concede { player_id: 0 }
(always legal regardless of WaitingFor state, CR 104.3a).

Expected (bug): waiting_for rewritten to Priority{1}; PPT stays Some;
auto_advance keeps returning the stranded Priority prompt; P1/P2 can pass
priority forever but the phase/turn never advances -> soft-lock.
Expected (correct): the engine resumes or finalizes the interrupted drain for
the remaining players (phase completes, turn advances).

Assertions:
  A1 parked_choice ...... ReplacementChoice{0} observed at a phase boundary
                          with pending_phase_transition_progress non-empty in
                          the pre-concede export (pre.json).
  A2 concede_eliminates . P0's Concede accepted; P0 eliminated (in
                          eliminated_players); waiting_for rewritten to
                          Priority (no longer ReplacementChoice).
  A3 ppt_stranded ....... post-concede export still shows
                          pending_phase_transition_progress non-None.
  A4 phase_frozen ....... 90s after the concede, with P1/P2 passing priority
                          whenever offered, turn_number and phase are unchanged
                          (soft-lock) -> BUG reproduced. If the turn/phase
                          advanced -> not-reproduced.
  A5 cleanup ............ post.json exported; game state documented.

Verdict rule:
  reproduced ..... A1+A2+A3 pass and A4 shows the freeze.
  not-reproduced . A1+A2 pass but the phase/turn advanced (engine resumed the
                   drain for the remaining players on this build).
  blocked ........ setup gate never opens (no parked choice; Concede rejected;
                   game stalls before assertions).

Scope: facet 1 only. Facet 2 was tested 2026-09-09 on v0.78.0
(not-reproduced, comment published; not re-run here). Facet 3
(pending_continuation -> deferred-trigger drain freeze) is not exercised.

Evidence: evidence/4823/<run-id>/pre.json (parked choice + PPT),
mid_concede.json, post.json, run.json, manifest.sha256, summary.png,
scenario_4823_f1.py, wire_log.jsonl, scenario_run.log.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

import websockets as _wsmod
from websockets.protocol import State

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, HELLO, URL  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260916-0511"
EVDIR = f"{BACKFILL}/evidence/4823/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

FOREST = "Forest"
UPWELLING = "Upwelling"
HSTONE = "Horizon Stone"
ESG = "Elvish Spirit Guide"

P0_DECK = [(FOREST, 36), (UPWELLING, 8), (HSTONE, 8), (ESG, 8)]
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "Full",
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
    "signature_key_id": "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY",
    "signature_verified": True,
    "observed_at": "2026-09-16",
    "source": "sha256 match of pinned verified release artifacts (AGENTS.md, 2026-09-16); "
              "fresh isolated server on 127.0.0.1:9374 from this run's run dir; "
              "ServerHello v0.84.0/eb7e93e/protocol 71 observed",
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


def bf_ids(state, pid, name=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (name is None or lname(state, oid) == name.lower())]


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


async def ensure_connected(c):
    """Re-establish a dead client websocket and re-attach to the game."""
    try:
        alive = c.ws is not None and c.ws.state == State.OPEN
    except Exception:
        alive = False
    pump_dead = c._pump_task is not None and c._pump_task.done()
    if alive and not pump_dead:
        return True
    say(f"{c.name}: connection unhealthy (alive={alive} pump_dead={pump_dead}), "
        f"re-establishing")
    wire("reconnect", {"who": c.name})
    try:
        if c._pump_task:
            c._pump_task.cancel()
        try:
            await asyncio.wait_for(c.ws.close(), 5)
        except Exception:
            pass
        c.ws = await _wsmod.connect(URL, max_size=200_000_000)
        await asyncio.wait_for(c.ws.recv(), 10)  # ServerHello
        await c.ws.send(json.dumps(HELLO))
        c._pump_task = asyncio.create_task(c._pump())
        c.revision = -1
        c.latest = None
        await c.reconnect(c.game_code, c.player_token, c.full_key)
        say(f"{c.name}: re-attached to game {c.game_code}")
        wire("reconnected", {"who": c.name})
        return True
    except Exception as e:
        say(f"{c.name}: re-establish failed: {e}")
        wire("reconnect_failed", {"who": c.name, "err": str(e)})
        return False


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parked_choice", "A2_concede_eliminates", "A3_ppt_stranded",
            "A4_phase_frozen", "A5_cleanup")}
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
    obs = {
        "parked_choice": None,
        "concede_sent": False,
        "concede_accepted": False,
        "concede_rejections": [],
        "concede_at": None,
        "freeze_watch": None,   # {start_turn, start_phase, samples:[...], passes}
        "rejections": [],
        "p1_passes": 0,
        "p2_passes": 0,
        "floated": False,
    }

    def wf_of(state):
        return state.get("waiting_for") or {}

    async def try_concede():
        shapes = [
            {"type": "Concede", "data": {"player_id": 0}},
            {"type": "Concede"},
        ]
        for shape in shapes:
            obs["concede_rejections"] = []
            await submit_as_is(p0, shape)
            say(f"P0 submits Concede shape={json.dumps(shape)}")
            wire("concede_submission", {"shape": shape})
            await asyncio.sleep(2.0)
            st = p0.latest
            if st is None:
                continue
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
            n_land = hn.count(FOREST.lower())
            if tag == "P0":
                has_combo = UPWELLING.lower() in hn or HSTONE.lower() in hn
                ok = n_land >= 2 and (has_combo or mulls >= 3)
                if ok or mulls >= 5:
                    kept[tag] = True
                    await submit_as_is(c, {"type": "MulliganDecision",
                                           "data": {"choice": {"type": "Keep"}}})
                    say(f"{tag} keeps: {hn} (mulls={mulls})")
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
                    return (0 if nm == FOREST.lower() else 1, nm)
                picks = sorted(hids, key=bkey)[:count]
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
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
                        say(f"{tag} bottom rejected: {json.dumps(data)[:150]}")
                if not rej:
                    kept[f"{tag}_bottomed"] = True
                return True
        return False

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    async def p0_tick(st, acts, state):
        nonlocal obs
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
        if (wtype == "ReplacementChoice" and obs["parked_choice"] is None):
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
                    opp_desc = {
                        "rtype": resp.get("type"),
                        "n_choices": len(chs),
                        "choices": texts[:10],
                        "interactionId": str(opp.get("interactionId")),
                    }
                    wire("parked_choice_opportunity",
                         {"opportunity": json.loads(json.dumps(opp, default=str))})
                    break
            obs["parked_choice"] = {
                "turn": state.get("turn_number"),
                "phase": state.get("phase"),
                "revision": p0.revision,
                "chooser": chooser,
                "choose_replacement_advertised": cr is not None,
                "opportunity": opp_desc,
            }
            say(f"PARKED CHOICE rev={p0.revision} turn={state.get('turn_number')} "
                f"phase={state.get('phase')} chooser={chooser} "
                f"ChooseReplacement={cr is not None} opp={json.dumps(opp_desc)}")
            wire("parked_choice", obs["parked_choice"])
            try:
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                say("PRE exported")
                pre_st = json.loads(pre)["state"]
                ppt = pre_st.get("pending_phase_transition_progress")
                say(f"pre.json PPT: {json.dumps(ppt, default=str)[:400] if ppt else None}")
                wire("pre_ppt", ppt)
            except Exception as e:
                notes.append(f"pre export failed: {e}")
            obs["concede_at"] = time.time()
            accepted = await try_concede()
            obs["concede_sent"] = True
            obs["concede_accepted"] = accepted
            # freeze-watch baseline
            st2 = p0.latest
            s2 = st2["state"] if st2 else state
            obs["freeze_watch"] = {
                "start_turn": s2.get("turn_number"),
                "start_phase": s2.get("phase"),
                "start_active": s2.get("active_player"),
                "t0": time.time(),
                "samples": [],
            }
            say(f"concede submitted; accepted(no-rejection)={accepted}; "
                f"freeze baseline turn={s2.get('turn_number')} "
                f"phase={s2.get('phase')} active={s2.get('active_player')}")
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        mine = state.get("active_player") == 0
        phase = state.get("phase")
        if mine and phase in ("PreCombatMain", "PostCombatMain"):
            hn = hand_names(state, 0)
            has_up = len(bf_ids(state, 0, UPWELLING)) > 0
            has_hs = len(bf_ids(state, 0, HSTONE)) > 0
            # 1) cast Upwelling
            if not has_up and UPWELLING.lower() in hn:
                for a in acts:
                    if (a["type"] == "CastSpell"
                            and lname(state, a.get("data", {}).get("object_id"))
                            == UPWELLING.lower()):
                        say("P0 casts Upwelling")
                        wire("cast_upwelling", {"action": str(a)[:300]})
                        await submit_as_is(p0, a)
                        return
            # 2) cast Horizon Stone
            if has_up and not has_hs and HSTONE.lower() in hn:
                for a in acts:
                    if (a["type"] == "CastSpell"
                            and lname(state, a.get("data", {}).get("object_id"))
                            == HSTONE.lower()):
                        say("P0 casts Horizon Stone")
                        wire("cast_hstone", {"action": str(a)[:300]})
                        await submit_as_is(p0, a)
                        return
            # 3) float one green mana via Elvish Spirit Guide (exile from hand),
            #    then pass. NOTE: basic-land mana abilities are NOT advertised
            #    as activatable actions on protocol 71 (diagnostic 2026-09-16:
            #    only PassPriority is offered on a clean main phase); the
            #    hand-activated ESG ability IS advertised as ActivateAbility
            #    (probe 2026-09-16) and leaves {G} in the pool.
            if has_up and has_hs and not obs["floated"]:
                for a in acts:
                    if a["type"] == "ActivateAbility":
                        src = int((a.get("data") or {}).get("source_id", -1))
                        if lname(state, src) == ESG.lower():
                            say(f"P0 exiles Elvish Spirit Guide {src} (float G)")
                            wire("float_mana", {"source": src,
                                                "action": str(a)[:300]})
                            await submit_as_is(p0, a)
                            await asyncio.sleep(1.0)
                            # confirm the mana actually landed before arming
                            stc = p0.latest
                            pooled = False
                            if stc:
                                pc = [x for x in stc["state"].get("players", [])
                                      if x.get("id") == 0]
                                if pc:
                                    mp = pc[0].get("mana_pool") or {}
                                    pooled = len(mp.get("mana", [])) > 0
                            obs["floated"] = pooled
                            say(f"float confirmed in pool: {pooled}")
                            wire("float_confirmed", {"pooled": pooled})
                            return
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        # not our turn, or nothing to do: pass whenever priority is ours
        # (gated on my_priority per the #4509 lesson)
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
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
            if wtype != "Priority" or state.get("priority_player") != pid:
                return
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
            for a in acts:
                if a["type"] == "PassPriority":
                    if tag == "P1":
                        obs["p1_passes"] += 1
                    elif tag == "P2":
                        obs["p2_passes"] += 1
                    await submit_as_is(c, a)
                    return
        return tick

    async def finish():
        dur = time.time() - t_start
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
        except Exception as e:
            pre_st = None
            notes.append(f"pre reload failed: {e}")
        try:
            post_raw = await p0.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post_raw)
            post_st = json.loads(post_raw)["state"]
        except Exception as e:
            post_st = None
            notes.append(f"post export failed: {e}")

        # A1: parked choice with PPT
        pc = obs["parked_choice"]
        ppt_pre = pre_st.get("pending_phase_transition_progress") \
            if pre_st else None
        if (pc is not None and pc.get("chooser") == 0
                and pc.get("choose_replacement_advertised")
                and pre_st is not None
                and (pre_st.get("waiting_for") or {}).get("type")
                == "ReplacementChoice"
                and ppt_pre is not None):
            rem = (ppt_pre.get("remaining_players")
                   if isinstance(ppt_pre, dict) else None)
            ass["A1_parked_choice"] = "passed"
            notes.append(f"A1 passed: pre.json shows "
                         f"WaitingFor::ReplacementChoice{{chooser 0}} at turn "
                         f"{pc['turn']} phase {pc['phase']}; ChooseReplacement "
                         f"advertised with "
                         f"{(pc.get('opportunity') or {}).get('n_choices')} "
                         f"choices; pending_phase_transition_progress present, "
                         f"remaining_players={rem}")
        else:
            ass["A1_parked_choice"] = "failed"
            notes.append(f"A1 failed: parked_choice={json.dumps(pc)[:300]}; "
                         f"pre PPT present={ppt_pre is not None}")

        # A2: concede eliminates P0
        elim = None
        mid_wf = None
        try:
            mid_raw = await p0.export_state()
            with open(f"{EVDIR}/mid_concede.json", "w") as f:
                f.write(mid_raw)
            mid_st = json.loads(mid_raw)["state"]
            elim = mid_st.get("eliminated_players")
            mid_wf = (mid_st.get("waiting_for") or {}).get("type")
            obs["mid_ppt"] = mid_st.get("pending_phase_transition_progress")
            wire("mid_elim", {"eliminated": elim, "wf": mid_wf,
                              "ppt_present":
                              obs["mid_ppt"] is not None})
        except Exception as e:
            notes.append(f"mid_concede export failed: {e}")
        if (obs["concede_sent"] and obs["concede_accepted"]
                and isinstance(elim, list) and 0 in elim
                and mid_wf != "ReplacementChoice"):
            ass["A2_concede_eliminates"] = "passed"
            notes.append(f"A2 passed: Concede accepted (no rejection); "
                         f"eliminated_players={elim}; waiting_for rewritten to "
                         f"{mid_wf} (parked choice gone)")
        else:
            ass["A2_concede_eliminates"] = "failed"
            notes.append(f"A2 failed: sent={obs['concede_sent']} "
                         f"accepted={obs['concede_accepted']} elim={elim} "
                         f"mid_wf={mid_wf} "
                         f"rejections={obs['concede_rejections'][:2]}")

        # A3: PPT stranded post-concede
        ppt_post = post_st.get("pending_phase_transition_progress") \
            if post_st else None
        obs["post_ppt"] = ppt_post
        if ppt_post is not None:
            ass["A3_ppt_stranded"] = "passed"
            rem = (ppt_post.get("remaining_players")
                   if isinstance(ppt_post, dict) else None)
            notes.append(f"A3 passed (BUG): pending_phase_transition_progress "
                         f"still present in post.json; "
                         f"remaining_players={rem}")
        else:
            ass["A3_ppt_stranded"] = "failed"
            notes.append("A3 failed (no bug on this build): "
                         "pending_phase_transition_progress is None in "
                         "post.json - the engine drained/finalized it")

        # A4: phase/turn frozen while passes flow
        fw = obs["freeze_watch"]
        samples = (fw or {}).get("samples", [])
        if fw and samples:
            turns = {s["turn"] for s in samples}
            phases = {s["phase"] for s in samples}
            frozen = (turns == {fw["start_turn"]}
                      and phases == {fw["start_phase"]})
            span = samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else 0
            passes = obs["p1_passes"] + obs["p2_passes"]
            wire("freeze_watch", {"frozen": frozen, "span_s": round(span, 1),
                                  "turns": sorted(turns),
                                  "phases": sorted(phases),
                                  "passes": passes,
                                  "n_samples": len(samples)})
            if frozen and span >= 60 and passes >= 4:
                ass["A4_phase_frozen"] = "passed"
                notes.append(f"A4 passed (BUG): phase/turn frozen for "
                             f"{span:.0f}s after the concede "
                             f"(turn={fw['start_turn']} "
                             f"phase={fw['start_phase']} active="
                             f"{fw['start_active']}) while P1/P2 passed "
                             f"priority {passes}x - the phase transition never "
                             f"completed (soft-lock)")
            elif not frozen:
                ass["A4_phase_frozen"] = "failed"
                notes.append(f"A4 failed (no bug on this build): game advanced "
                             f"after the concede (turns={sorted(turns)}, "
                             f"phases={sorted(phases)})")
            else:
                ass["A4_phase_frozen"] = "failed"
                notes.append(f"A4 inconclusive: span={span:.0f}s passes={passes} "
                             f"samples={len(samples)}")
        else:
            ass["A4_phase_frozen"] = "failed"
            notes.append("A4 failed: no freeze-watch samples")

        # A5: cleanup
        if post_st is not None:
            ass["A5_cleanup"] = "passed"
            notes.append(f"A5 passed: post.json exported (turn="
                         f"{post_st.get('turn_number')} phase="
                         f"{post_st.get('phase')} active="
                         f"{post_st.get('active_player')} wf="
                         f"{(post_st.get('waiting_for') or {}).get('type')})")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 failed: no post.json")

        if (ass["A1_parked_choice"] == "passed"
                and ass["A2_concede_eliminates"] == "passed"
                and ass["A3_ppt_stranded"] == "passed"
                and ass["A4_phase_frozen"] == "passed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: facet 1 of #4823 - the chooser's "
                         "mid-choice elimination stranded "
                         "pending_phase_transition_progress; the phase "
                         "transition never completed (soft-lock) on v0.84.0")
        elif (ass["A1_parked_choice"] == "passed"
                and ass["A2_concede_eliminates"] == "passed"
                and "A4 failed (no bug on this build)" in " ".join(notes)):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: the parked choice and "
                         "elimination happened, but the phase/turn advanced - "
                         "the engine resumed the drain on v0.84.0")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: setup gate never opened or an "
                         "elimination/continuation step failed; no trustworthy "
                         "engine behavior observed")
        run = {
            "issue": 4823,
            "facet": 1,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 71, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4823_f1.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if k not in ("rejections",)},
            "verdict": verdict,
            "setup_line": "3-player game. P0: 36x Forest + 8x Upwelling + 8x "
                          "Horizon Stone + 8x Elvish Spirit Guide (engine "
                          "accepts >4-of). P1/P2: 60x Forest, do-nothing. P0 "
                          "casts Upwelling then Horizon Stone, exiles an "
                          "Elvish Spirit Guide from hand to float {G} in "
                          "PreCombatMain, passes; the BeginCombat entry's "
                          "APNAP empty-mana drain parks a CR 616.1 "
                          "EmptyManaPool ordering choice for P0 (Upwelling "
                          "retain vs Horizon Stone transform); P0 concedes "
                          "instead of answering.",
            "contract_line": "Facet 1 of #4823: after the chooser's mid-choice "
                             "elimination, the stranded "
                             "pending_phase_transition_progress must not freeze "
                             "the phase transition - the game must keep "
                             "advancing. A frozen turn/phase while priority "
                             "passes flow is the reported soft-lock.",
            "limitations": [
                "Browser UI not exercised; native engine via three human-client seats.",
                "Only facet 1 (pending_phase_transition_progress -> phase-advance "
                "freeze via a step-end empty-mana CR 616.1 choice) is tested. "
                "Facet 2 was tested 2026-09-09 on v0.78.0 (not-reproduced, comment "
                "published, not re-run here). Facet 3 (pending_continuation -> "
                "deferred-trigger drain freeze) is not exercised.",
                "The parked choice is the CR 616.1 ordering of Upwelling's retain "
                "vs Horizon Stone's transform on the same EmptyManaPool event; "
                "engine behavior for other 616.1 pairs may differ.",
                "Not tested on the original 2026-07-01 build; verdict is scoped to "
                "v0.84.0, not a fix claim.",
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
    last_health = 0.0
    freeze_done_at = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        if time.time() - last_health > 20:
            last_health = time.time()
            for hc in (p0, p1, p2):
                try:
                    await ensure_connected(hc)
                except Exception as e:
                    say(f"health check {hc.name} error: {e}")
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
            try:
                await tick(st, merged_actions(st), state)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
                try:
                    await ensure_connected(c)
                except Exception as e2:
                    say(f"post-error reconnect {tag} failed: {e2}")
        # freeze-watch sampling after the concede
        fw = obs["freeze_watch"]
        if fw is not None and p1.latest:
            s = p1.latest["state"]
            if not fw["samples"] or time.time() - fw["samples"][-1]["t"] >= 5:
                fw["samples"].append({
                    "t": time.time(),
                    "turn": s.get("turn_number"),
                    "phase": s.get("phase"),
                    "active": s.get("active_player"),
                    "wf": (s.get("waiting_for") or {}).get("type"),
                    "pp": s.get("priority_player"),
                })
            if time.time() - fw["t0"] >= 90 and freeze_done_at is None:
                freeze_done_at = time.time()
                say(f"freeze-watch complete: {len(fw['samples'])} samples, "
                    f"P1 passes={obs['p1_passes']} P2 passes={obs['p2_passes']}")
                await finish()
                return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} "
                f"up={len(bf_ids(s, 0, UPWELLING))} hs={len(bf_ids(s, 0, HSTONE))} "
                f"floated={obs['floated']} parked={obs['parked_choice'] is not None} "
                f"conceded={obs['concede_sent']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
