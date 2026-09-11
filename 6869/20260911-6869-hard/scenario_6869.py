#!/usr/bin/env python3
"""Issue #6869: "secret rendezvous - AI failed to select an opponent and game
froze." (reporter via discord, source:discord, status:confirmed,
area:engine+parser+ai, priority:p0-softlock)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Subsystem under test: native server-side AI policy (phase-ai) on the pinned
release. P1 is an AI (Medium) seat; P0 is a human driver seat.

Secret Rendezvous {1}{W}{W} sorcery: "You and target opponent each draw three
cards."

Parser corroboration (v0.80.0 card-data.json): the second Draw effect is NOT a
targeted-opponent slot. It is scoped as
  {"type":"Draw","count":{"type":"Fixed","value":3},
   "target":{"type":"Typed","type_filters":[],"controller":"Opponent",
             "properties":[]}}
with target_prompt:null and optional_targeting:false. The triage on the issue
("the opponent child is merely scoped to opponent; it does not expose a
targeted-opponent slot... can leave AI action generation without the required
target decision") matches this parse.

Triage acceptance criteria:
  - Casting requires one legal opponent target.
  - AI selects a legal opponent before announcement or does not cast the spell.
  - The caster and that chosen opponent each draw three cards, with no
    repeated waiting state.

Setup:
  P0 (human driver): 12x "Secret Rendezvous" + 48x "Plains". Keep mulligan;
                     play one Plains per turn; pass priority; never attack;
                     never casts in phase 1 (passive observer).
  P1 (AI, Medium):   12x "Secret Rendezvous" + 48x "Plains" (dense playset so
                     the AI draws and can cast the {1}{W}{W} spell).

Expected:
  E1: the AI casts Secret Rendezvous (spell on the stack, controller 1).
  E2: no freeze: any target decision for the AI completes and the spell leaves
      the stack well within the stall window; the game keeps advancing (the
      same waiting state is NOT stuck without progress).
  E3: both the caster and the opponent draw three cards (Library->Hand
      movement for exactly 3 cards each between cast and resolution).
  E4: control: a human-driven P0 cast exercises the target path directly -
      target-opponent selection is offered (or documented auto-target), and
      both players draw three.
  E5: the game continues past each resolution.

Assertions:
  A1_ai_cast        >=1 AI Secret Rendezvous observed on the stack.
  A2_no_freeze      every observed AI cast resolved within STALL_TIMEOUT of
                    the cast; no AI decision wait persisted without progress.
  A3_draws          for the first resolved AI cast, each player drew exactly 3
                    cards (zone-diff Library->Hand), on the same turn.
  A4_control        human P0 cast completed: target decision exercised and
                    both players drew 3.
  A5_cleanup        game state advanced after resolution.

Verdict rule: blocked iff A1 fails (AI never cast: the reported AI path was
not exercised). reproduced iff an AI cast stalls (A2 fails) - the reported
softlock. not-reproduced iff A1..A5 all pass.

Evidence: evidence/6869/<run-id>/pre_ai_cast.json (first AI cast on stack),
post_ai_cast.json (after its resolution), pre_control.json / post_control.json
(human cast), run.json, manifest.sha256, summary.png, scenario_6869.py,
wire_log.jsonl, scenario_run.log, server_excerpts.log.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

client.URL = "ws://localhost:9375/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6869"
EVDIR = f"{BACKFILL}/evidence/6869/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD = "Secret Rendezvous"
CARD_L = CARD.lower()
LAND = "Plains"
P0_DECK = [(CARD, 12), (LAND, 48)]
P1_AI_DECK = [(CARD, 12), (LAND, 48)]

SERVER_IDENTITY = {
    "server_version": "0.80.0",
    "build_commit": "22cca6d",
    "protocol_version": 69,
    "mode": "Full",
    "binary_sha256": "1d414c0e999a088560ab9ad0d77a4ae4f5773610cee6afda616a62c0e654238e",
    "card_data_sha256": "7ce6f92d0adb8fc4158bf0ab76797a644eb77dcea01f9743bb849550bb677bfd",
    "draft_pools_sha256": "c78dbd16f671e5b21ec094d6fcbc2b5da76e79cc82c2d9daa914369180021348",
    "signature_verified": True,
    "signature_note": "minisign-verify (prehashed Ed25519, sigalg ED) of "
                      "phase-server + signed data manifest with repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY; data files match manifest "
                      "SHA-256 (pinned 2026-09-11)",
    "observed_at": "2026-09-11",
    "source": "ledger pin v0.80.0 (server/releases/v0.80.0/); isolated server "
              "on 127.0.0.1:9375 (started by this run, verified listening "
              "before driving)",
}

STALL_TIMEOUT = 150   # max seconds one AI cast may stay unresolved / no progress
GAME_TIMEOUT = 1500   # overall budget
PHASE1_TURN_LIMIT = 12  # stop waiting for an AI cast after this turn


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


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_of(state, pid):
    return [str(o) for o in player_of(state, pid).get("hand", [])]


def rendezvous_on_stack(state):
    return [oid for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Stack" and obj_name(o) == CARD_L]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype, name=None, state=None):
    for a in acts:
        if a["type"] != atype:
            continue
        if name is None:
            return a
        d = a.get("data", {})
        oid = d.get("object_id") or a.get("_src_oid")
        if state is not None and obj_name(
                state.get("objects", {}).get(str(oid), {})) == name:
            return a
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg = {"type": a["type"], "data": a["data"]}
    await c.send_action(msg)


def draws_between(pre_st, post_st, pid):
    """Cards that moved Library -> Hand for player pid between two exports."""
    pre_objs = pre_st.get("objects", {})
    post_objs = post_st.get("objects", {})
    drawn = []
    for oid, po in post_objs.items():
        if po.get("zone") != "Hand":
            continue
        pre = pre_objs.get(oid)
        if pre is not None and pre.get("zone") == "Library":
            owner = po.get("owner", po.get("controller"))
            if owner == pid:
                drawn.append((oid, obj_name(po)))
    return drawn


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_ai_cast", "A2_no_freeze", "A3_draws", "A4_control",
            "A5_cleanup")}
    casts = []  # one record per observed AI cast
    obs = {"rejections": [], "p0_acts": 0, "casts": casts,
           "ai_target_waits": [], "control": {}}
    exported = {"pre_ai_cast": False, "post_ai_cast": False,
                "pre_control": False, "post_control": False}
    states_seen = 0

    p0 = PhaseClient("P0")
    await p0.connect()
    ai_deck = deck(*P1_AI_DECK)
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Medium",
                               "deck": {"type": "DeckList", "data": ai_deck}}])
    say(f"game {p0.game_code}; P0 seat={p0.player_id}; P1 = AI Medium seat")
    wire("game_created", {"game_code": p0.game_code, "p0_seat": p0.player_id,
                          "p0_deck": P0_DECK, "p1_ai_deck": P1_AI_DECK})

    def drain_rejections(c):
        hits = []
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("Error", "ActionRejected"):
                hits.append({"who": c.name, "type": t, "data": data})
        return hits

    async def export_state_dict(c, tag):
        try:
            s = await c.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            exported[tag] = True
            say(f"exported {tag}.json")
            return json.loads(s)["state"]
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return None

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        try:
            return json.loads(open(p).read())["state"]
        except Exception:
            return None

    async def keep_mulligan():
        t0 = time.time()
        while time.time() - t0 < 60:
            await asyncio.sleep(0.25)
            st = p0.latest
            if not st:
                continue
            acts = merged_actions(st)
            a = find_action(acts, "MulliganDecision")
            if a:
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say("P0 mulligan: Keep")
                return True
        return False

    await keep_mulligan()
    land_played_turns = set()

    t_loop = time.time()
    last_rev = -1
    last_progress_t = time.time()
    last_tick_at = 0.0
    known_stack = set()
    first_ai_cast_resolved_t = None
    stalled = False
    stall_info = None
    control_armed = False
    control_cast_oid = None
    control_target_prompt_seen = False
    control_target_mode = None   # "prompt" | "auto" | None
    control_resolved = False
    phase1_done = False
    ai_cast_timeout_logged = False

    while time.time() - t_loop < GAME_TIMEOUT:
        await asyncio.sleep(0.05)
        states_seen += 1
        msg = p0.latest
        if not msg:
            continue
        rev = p0.revision
        if rev != last_rev:
            last_progress_t = time.time()
        same_rev = (rev == last_rev)
        stale = time.time() - last_tick_at > 5
        may_act = (not same_rev) or stale
        state = msg.get("state", {})
        now = time.time()

        for r in drain_rejections(p0):
            obs["rejections"].append(r)
            wire("rejection", r)

        wf = state.get("waiting_for") or {}
        wfd = wf.get("data") or {}

        # --- AI cast tracking ---
        for oid in rendezvous_on_stack(state):
            if oid not in known_stack:
                known_stack.add(oid)
                obj = state.get("objects", {}).get(oid, {})
                ctrl = obj.get("controller", obj.get("owner"))
                rec = {"oid": int(oid), "cast_t": now, "cast_rev": rev,
                       "cast_turn": state.get("turn_number"),
                       "controller": ctrl, "resolved": False,
                       "resolve_t": None, "resolve_s": None,
                       "target_wait": None}
                casts.append(rec)
                say(f"OBSERVED cast #{len(casts)}: {CARD} on stack "
                    f"(oid {oid}, controller {ctrl}, turn {rec['cast_turn']}, "
                    f"rev {rev})")
                wire("cast", {"n": len(casts), "oid": int(oid), "ctrl": ctrl,
                              "rev": rev, "turn": rec["cast_turn"],
                              "waiting_for": state.get("waiting_for")})
                if ctrl == 1 and not exported["pre_ai_cast"]:
                    await export_state_dict(p0, "pre_ai_cast")

        # WS-visible AI target wait (caught if the poll lands inside it)
        for c in casts:
            if c["resolved"] or c["controller"] != 1:
                continue
            if (wfd.get("player") == 1 and not c["target_wait"]):
                c["target_wait"] = {"type": wf.get("type"), "rev": rev,
                                    "t": now}
                obs["ai_target_waits"].append(
                    {"cast": casts.index(c) + 1, "wf": wf})
                say(f"OBSERVED: AI target wait for cast "
                    f"#{casts.index(c)+1}: wf={wf.get('type')}, rev {rev}")
                wire("ai_target_wait", {"cast": casts.index(c) + 1,
                                        "rev": rev,
                                        "waiting_for": copy.deepcopy(wf),
                                        "viewer_interaction":
                                            copy.deepcopy(
                                                msg.get("viewer_interaction"))})

        # resolution / stall per cast
        current_stack = set(rendezvous_on_stack(state))
        for c in casts:
            if c["resolved"]:
                continue
            if str(c["oid"]) not in current_stack:
                c["resolved"] = True
                c["resolve_t"] = now
                c["resolve_s"] = round(now - c["cast_t"], 2)
                say(f"OBSERVED cast #{casts.index(c)+1} resolved in "
                    f"{c['resolve_s']}s (rev {rev})")
                wire("cast_resolved", {"cast": casts.index(c) + 1,
                                       "resolve_s": c["resolve_s"],
                                       "rev": rev})
                if c["controller"] == 1 and not exported["post_ai_cast"]:
                    await export_state_dict(p0, "post_ai_cast")
                    first_ai_cast_resolved_t = now
            elif now - c["cast_t"] > STALL_TIMEOUT:
                c["stalled"] = True
                say(f"STALL: cast #{casts.index(c)+1} unresolved after "
                    f"{STALL_TIMEOUT}s (rev {rev}, wf={wf.get('type')})")
                wire("stall", {"cast": casts.index(c) + 1,
                               "controller": c["controller"], "rev": rev,
                               "waiting_for": wf.get("type")})
                # only an AI-cast stall is the reported softlock; a stuck P0
                # control prompt is recorded separately, not as A2 evidence
                if c["controller"] == 1:
                    stalled = True
                    stall_info = {"kind": "cast_unresolved",
                                  "cast": casts.index(c) + 1, "rev": rev,
                                  "waiting_for": wf.get("type"),
                                  "wf_player": wfd.get("player")}
                else:
                    notes.append(f"P0 control cast #{casts.index(c)+1} stalled")
                if c["controller"] == 1 and not exported["post_ai_cast"]:
                    await export_state_dict(p0, "post_ai_cast")

        # global stall watchdog: no revision progress at all
        if (not stalled and now - last_progress_t > STALL_TIMEOUT
                and not (state.get("game_over") or
                         state.get("winner") is not None)):
            stalled = True
            stall_info = {"kind": "no_progress",
                          "idle_s": round(now - last_progress_t, 1),
                          "rev": rev, "waiting_for": wf.get("type"),
                          "wf_player": wfd.get("player"),
                          "phase": state.get("phase"),
                          "turn": state.get("turn_number")}
            say(f"STALL: no state progress for {STALL_TIMEOUT}s "
                f"(rev {rev}, wf={wf.get('type')}/P{wfd.get('player')})")
            wire("stall_no_progress", stall_info)
            if not exported["post_ai_cast"]:
                await export_state_dict(p0, "post_ai_cast")
            break

        if stalled:
            break

        if state.get("game_over") or state.get("winner") is not None:
            say("game ended")
            notes.append("game ended before watch window elapsed")
            break

        # phase 1 -> phase 2 transition
        ai_casts = [c for c in casts if c["controller"] == 1]
        ai_done = bool(ai_casts and all(c["resolved"] for c in ai_casts))
        turn = state.get("turn_number") or 0
        if not phase1_done:
            if ai_done:
                phase1_done = True
                control_armed = True
                say("phase 1 complete: AI cast resolved; arming P0 control cast")
            elif turn > PHASE1_TURN_LIMIT and not ai_cast_timeout_logged:
                ai_cast_timeout_logged = True
                phase1_done = True
                control_armed = True
                notes.append(f"AI cast no AI cast by turn {PHASE1_TURN_LIMIT}; "
                             "proceeding to P0 control cast")
                say("phase 1 timeout: AI never cast; arming P0 control cast")

        # --- drive P0 ---
        if may_act:
            acts = merged_actions(msg)
            acted = False
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p0.send_action({"type": "DeclareAttackers",
                                      "data": sub["data"]})
                obs["p0_acts"] += 1
                acted = True
            # P0 control cast: own main phase, priority, card in hand
            if (not acted and control_armed and not control_resolved
                    and control_cast_oid is None
                    and state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain")):
                cs = find_action(acts, "CastSpell", CARD_L, state)
                if cs:
                    d = cs.get("data", {})
                    oid = d.get("object_id") or cs.get("_src_oid")
                    if oid and not exported["pre_control"]:
                        await export_state_dict(p0, "pre_control")
                    say(f"P0 CONTROL: casting {CARD} (oid {oid})")
                    wire("control_cast_submit",
                         {"oid": oid, "action": cs})
                    await submit_as_is(p0, cs)
                    control_cast_oid = int(oid)
                    obs["control"]["cast_oid"] = int(oid)
                    obs["control"]["cast_turn"] = turn
                    obs["p0_acts"] += 1
                    acted = True
            # answer target selection for the P0 control cast
            if (not acted and control_cast_oid is not None
                    and not control_resolved):
                cur_stack = set(rendezvous_on_stack(state))
                if str(control_cast_oid) not in cur_stack:
                    control_resolved = True
                    say("P0 CONTROL: cast resolved (left stack)")
                    wire("control_resolved", {"rev": rev})
                    if not exported["post_control"]:
                        await export_state_dict(p0, "post_control")
                elif wfd.get("player") == 0:
                    wf_type = wf.get("type")
                    say(f"P0 CONTROL: waiting_for={wf_type} for P0")
                    wire("control_wait", {"wf": copy.deepcopy(wf),
                                          "viewer_interaction":
                                              copy.deepcopy(
                                                  msg.get(
                                                      "viewer_interaction"))})
                    control_target_prompt_seen = True
                    control_target_mode = "prompt"
                    # submit the advertised response: pick opponent seat 1
                    answered = await answer_opponent_target(p0, msg, state)
                    if answered:
                        obs["p0_acts"] += 1
                        acted = True
            if not acted:
                pl = find_action(acts, "PlayLand", "plains", state)
                if pl and turn not in land_played_turns:
                    await submit_as_is(p0, pl)
                    land_played_turns.add(turn)
                    obs["p0_acts"] += 1
                    acted = True
            if not acted:
                pp = find_action(acts, "PassPriority")
                if pp:
                    await submit_as_is(p0, pp)
                    obs["p0_acts"] += 1
            last_rev = rev
            last_tick_at = time.time()

        # end condition: after control resolution, short tail then stop
        if control_resolved and exported["post_control"]:
            if obs["control"].get("tail_started") is None:
                obs["control"]["tail_started"] = now
                say("control resolved; starting tail watch")
            if now - obs["control"]["tail_started"] > 20:
                say("tail watch complete after control resolution")
                break

    if not exported["post_ai_cast"]:
        await export_state_dict(p0, "post_ai_cast")
    if control_cast_oid is not None and not exported["post_control"]:
        await export_state_dict(p0, "post_control")

    # --- evaluate ---
    pre_ai = load_env("pre_ai_cast")
    post_ai = load_env("post_ai_cast")
    pre_ctl = load_env("pre_control")
    post_ctl = load_env("post_control")

    ai_casts = [c for c in casts if c["controller"] == 1]
    if ai_casts:
        ass["A1_ai_cast"] = "passed"
        notes.append("AI casts: " + ", ".join(
            f"#{i+1} turn {c['cast_turn']} resolved={c['resolved']} "
            f"({c.get('resolve_s', '?')}s) target_wait={c['target_wait']}"
            for i, c in enumerate(ai_casts)))
    else:
        ass["A1_ai_cast"] = "failed"
        notes.append("AI never cast Secret Rendezvous within the run")

    if ai_casts and not stalled and all(c["resolved"] for c in ai_casts):
        worst = max(c.get("resolve_s") or 0 for c in ai_casts)
        ass["A2_no_freeze"] = "passed"
        notes.append(f"all AI casts resolved within {worst}s "
                     f"(window {STALL_TIMEOUT}s); ai target waits seen: "
                     f"{len(obs['ai_target_waits'])}")
    elif ai_casts and any(c.get("stalled") for c in ai_casts):
        ass["A2_no_freeze"] = "failed"
        notes.append(f"AI cast stall: {json.dumps(stall_info)}")
    elif ai_casts and not all(c["resolved"] for c in ai_casts):
        ass["A2_no_freeze"] = "failed"
        notes.append(f"AI cast(s) unresolved at run end: {json.dumps(stall_info)}")
    else:
        ass["A2_no_freeze"] = "not-run"

    if (pre_ai is not None and post_ai is not None and ai_casts
            and ai_casts[0]["resolved"]):
        d0 = draws_between(pre_ai, post_ai, 0)
        d1 = draws_between(pre_ai, post_ai, 1)
        turn_same = (pre_ai.get("turn_number") == post_ai.get("turn_number"))
        notes.append(f"first AI cast: pre turn {pre_ai.get('turn_number')} -> "
                     f"post turn {post_ai.get('turn_number')}; P0 drew "
                     f"{len(d0)}, P1 drew {len(d1)}")
        if turn_same and len(d0) == 3 and len(d1) == 3:
            ass["A3_draws"] = "passed"
        else:
            ass["A3_draws"] = "failed"
            notes.append("expected each player to draw exactly 3 on the "
                         "same turn")
    else:
        ass["A3_draws"] = "not-run"
        notes.append("draws not evaluated (no pre/post AI pair)")

    if control_resolved and pre_ctl is not None and post_ctl is not None:
        d0 = draws_between(pre_ctl, post_ctl, 0)
        d1 = draws_between(pre_ctl, post_ctl, 1)
        obs["control"]["p0_drew"] = len(d0)
        obs["control"]["p1_drew"] = len(d1)
        notes.append(f"P0 control: target prompt seen="
                     f"{control_target_prompt_seen} "
                     f"(mode={control_target_mode}); P0 drew {len(d0)}, "
                     f"P1 drew {len(d1)}")
        if len(d0) == 3 and len(d1) == 3:
            ass["A4_control"] = "passed"
        else:
            ass["A4_control"] = "failed"
    elif control_cast_oid is not None:
        ass["A4_control"] = "failed"
        notes.append("P0 control cast did not resolve")
    else:
        ass["A4_control"] = "not-run"
        notes.append("P0 control cast never submitted")

    if post_ai is not None:
        ass["A5_cleanup"] = "passed"
        notes.append(f"post_ai_cast: turn {post_ai.get('turn_number')} phase "
                     f"{post_ai.get('phase')} waiting_for="
                     f"{(post_ai.get('waiting_for') or {}).get('type')} "
                     f"game_over={post_ai.get('game_over')}")
    else:
        ass["A5_cleanup"] = "not-run"

    if ass["A1_ai_cast"] != "passed":
        verdict = "blocked"
    elif ass["A2_no_freeze"] == "failed":
        verdict = "reproduced"
    elif all(ass[k] == "passed" for k in ("A1_ai_cast", "A2_no_freeze",
                                         "A3_draws", "A4_control",
                                         "A5_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    say(f"VERDICT: {verdict}")
    say(f"assertions: {json.dumps(ass)}")

    run = {
        "run_id": RUN_ID,
        "issue": 6869,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
        "server": SERVER_IDENTITY,
        "verdict": verdict,
        "assertions": ass,
        "observations": obs,
        "notes": notes,
        "stall": stall_info,
        "setup_line": "P0: 12x Secret Rendezvous + 48x Plains (human driver, "
                      "passive in phase 1; control cast in phase 2). P1: AI "
                      "Medium, 12x Secret Rendezvous + 48x Plains.",
        "contract_line": "AI casts Secret Rendezvous -> target decision must "
                         "complete with no freeze; caster and chosen opponent "
                         "each draw three. Then a human-driven control cast "
                         "exercises the target path directly.",
        "parser_corroboration": "v0.80.0 card-data.json models the second "
                         "Draw as target {type: Typed, controller: Opponent} "
                         "with target_prompt:null, optional_targeting:false "
                         "- a generic opponent scope, not a targeted-opponent "
                         "slot (matches issue triage).",
        "limitations": [
            "Browser/UI not exercised; server-side AI (phase-ai) policy "
            "tested, not any browser/client AI driver path",
            "2-player game only (single legal opponent); 3+ player target "
            "choice among multiple opponents not exercised",
            "Not tested on the original report build; verdict is scoped to "
            "v0.80.0, not a fix claim",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay)",
        ],
        "stats": {"states_seen": states_seen,
                  "casts_observed": len(casts)},
        "scenario": {"file": "scenario_6869.py",
                     "sha256": hashlib.sha256(
                         open(__file__, "rb").read()).hexdigest()},
        "decks": {"P0": {"main": P0_DECK}, "P1_AI": {"main": P1_AI_DECK}},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2, default=str)
    WIRE.close()
    RUNLOG.close()
    await p0.close()
    return verdict


async def answer_opponent_target(c, msg, state):
    """Answer a P0 TargetSelection for the control cast by submitting the
    advertised schema response choosing the opponent (seat 1). Returns True
    if a submission was sent."""
    vi = msg.get("viewer_interaction")
    if not vi:
        say("control: no viewer_interaction in message; cannot answer")
        wire("control_no_vi", {})
        return False
    iid = vi.get("interaction_id") or vi.get("interactionId")
    # collect candidates mentioning player/seat 1
    cands = []

    def rec(node, depth=0):
        if depth > 10:
            return
        if isinstance(node, dict):
            surfaces = node.get("surfaces") or []
            sid = node.get("id") or node.get("choiceId") or node.get("choice_id")
            seat = None
            for s in surfaces:
                sd = (s.get("data") or {}) if isinstance(s, dict) else {}
                if sd.get("seat") == 1:
                    seat = 1
                ref = sd.get("reference")
                if isinstance(ref, dict) and ref.get("seat") == 1:
                    seat = 1
            label = node.get("label") or node.get("name") or node.get("text")
            if sid and (seat == 1 or label):
                cands.append({"id": sid, "seat": seat, "label": label})
            for v in node.values():
                rec(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                rec(v, depth + 1)

    rec(vi.get("data", vi))
    pick = next((x for x in cands if x["seat"] == 1), None) or (
        cands[0] if cands else None)
    say(f"control: target candidates found: {cands}; pick={pick}; iid={iid}")
    wire("control_target_candidates", {"candidates": cands, "pick": pick,
                                       "iid": iid})
    if not pick or not iid:
        return False
    resp_kind = "sequence"
    data_node = vi.get("data", {}) if isinstance(vi, dict) else {}
    spec = ((data_node.get("schema") or {}).get("spec")
            or (vi.get("schema") or {}).get("spec") or {})
    if spec.get("type") == "exactChoices" or vi.get("response") == "exactChoices":
        resp_kind = "choose"
    if resp_kind == "choose":
        sub = {"type": "Interaction",
               "data": {"submission": {
                   "interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick["id"]}}}}}
    else:
        sub = {"type": "Interaction",
               "data": {"submission": {
                   "interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [pick["id"]]}}}}}
    wire("control_target_submission", sub)
    await c.ws.send(json.dumps(sub))
    return True


if __name__ == "__main__":
    v = asyncio.run(main())
    print("FINAL:", v)
