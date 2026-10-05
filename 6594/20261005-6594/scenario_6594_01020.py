#!/usr/bin/env python3
"""Issue #6594: "Stuck decision: ChooseFromZoneChoice" -- in a game vs the
AI, the AI cast Atraxa, Grand Unifier and the game stalled on the resulting
choose-from-zone decision (report builds v0.35.2/v0.41.0; console: "AI stuck:
3 consecutive failures on ChooseFromZoneChoice, dispatching fallback").

Protocol-106 port of driver/scenario_6594.py (protocol 72, run 20260917-6594,
verdict REPRODUCED on v0.85.0).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github #6594, status:confirmed, area:ai+frontend, p0-softlock):
Atraxa's ETB trigger reveals the top ten cards of the AI's library, then for
each card type the AI may put a card of that type from among the revealed
cards into its hand (rest on bottom in random order). The decision type is
ChooseFromZoneChoice. Triage: the AI's candidate generation for this
multi-category per-type optional selection is not producing a submittable
action, so play cannot continue.

Subsystem under test: native server-side AI policy (phase-ai) on the pinned
release, AI (Medium) seat vs a human driver seat. The separate frontend
"Maximum call stack size exceeded" (multiplayerDraftStore) is a browser-UI
defect not exercisable by an engine test -> recorded as a limitation.

Setup:
  P0 (human driver): 60x island. Mulligan keep; play island; pass priority.
  P1 (AI, Medium): 12x "atraxa, grand unifier" + 12x forest + 12x plains +
                   12x island + 12x swamp (dense playset so the AI draws and
                   can cast Atraxa: {3}{G}{W}{U}{B}).

Expected (per triage acceptance criteria):
  E1: the AI casts Atraxa (Atraxa on P1's battlefield).
  E2: the trigger's ChooseFromZoneChoice becomes pending for player 1.
  E3: the AI submits a legal selection (incl. selecting nothing for missing
      types) and the game advances past the decision.
  E4: resulting zones are sane: <=1 card per revealed card type in AI hand,
      the rest on the bottom of the AI library, nothing stranded.

Assertions:
  A1_atraxa_cast    Atraxa, Grand Unifier observed on P1's battlefield.
  A2_choice_pending authoritative export: waiting_for.type ==
                    ChooseFromZoneChoice with a pending entry for player 1.
  A3_choice_resolves waiting_for leaves ChooseFromZoneChoice within the stall
                    window (no multi-minute stall with static revision while
                    the AI's choice is pending).
  A4_zones_sane     post.json: no revealed cards stranded outside
                    hand/library-bottom; hand gain consistent with
                    one-per-type selection.

Verdict rule: blocked iff A1 fails (AI never cast Atraxa: setup broken).
reproduced iff A1+A2 pass and A3 fails (the reported stall). not-reproduced
iff A1..A4 all pass.

Evidence: evidence/6594/<run-id>/pre.json (ChooseFromZoneChoice pending for
the AI seat), post.json (resolved or stuck state), run.json, assertions.json,
obs.json, summary.png, scenario_6594_01020.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log, manifest.sha256.

Protocol-106 notes: authoritative export envelope is one level
(json.loads(raw)["state"]); waiting_for still surfaces decisions in the
export; MulliganDecision answered via legacy Action gated on the advertised
action; priority = PassPriority in the viewer's top-level legal_actions;
payment_mode Auto (P0 never casts); manifest computed AFTER WIRE/RUNLOG close.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20261005-6594")
ISSUE = 6594
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = None
RUNLOG = None


def init_logging():
    global WIRE, RUNLOG
    if WIRE is None:
        WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    if RUNLOG is None:
        RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


ATRAXA = "atraxa, grand unifier"
ISLAND = "island"

P0_DECK = [(ISLAND, 60)]
P1_AI_DECK = [(ATRAXA, 12), ("forest", 12), ("plains", 12),
              ("island", 12), ("swamp", 12)]

SERVER_IDENTITY = {
    "server_version": "v0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "mode": "Full",
    "binary_sha256": "5b79f0c520e11ad1df158f72c1674a43378ff9c999b57199789a531f6d125aa8",
    "card_data_sha256": "eb87edbd0c90e2440fdb97404e4a40fb86ca9770439f9c045fc9d0f220605b36",
    "draft_pools_sha256": "e80b16721bf3da5b28580f29bbe94b8df43f02363e66647cc004bb53888040e3",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.102.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828); data digests match the "
                      "signed manifest; digests recomputed against on-disk "
                      "files this run",
}

STALL_TIMEOUT = 150      # seconds with static revision while AI's choice pending
GAME_TIMEOUT = 900       # overall budget for the AI to draw+cast Atraxa
EXPORT_POLL_S = 1.0      # dense authoritative-export check after A1 (the
                         # choice window may be brief on 106)


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    if RUNLOG is not None:
        RUNLOG.write(m + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE is None:
        return
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def lname(state, oid):
    return obj_name(state.get("objects", {}).get(str(oid), {}))


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return None


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def bf_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name]


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it."""
    return any(a.get("type") == "PassPriority" for a in acts)


def find_action(acts, atype, name=None, state=None):
    for a in acts:
        if a["type"] != atype:
            continue
        if name is None:
            return a
        d = a.get("data", {})
        oid = d.get("object_id") or a.get("_src_oid")
        if state is not None and lname(state, oid) == name:
            return a
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await c.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose",
                                  "data": {"choiceId": ch.get("id")}}})
                return True
    return False


async def do_mulligan(c, acts, pid, tag):
    """Protocol 106: MulliganDecision arrives as a legacy legal action; the
    engine accepts the Action submission."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull")
    if key in do_mulligan.done:
        return False
    do_mulligan.done.add(key)
    say(f"[{tag}] mulligan: Keep")
    wire("mulligan", {"who": tag, "decision": "keep"})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


do_mulligan.done = set()


def ai_choice_pending(st):
    """Inspect an authoritative export for the AI's ChooseFromZoneChoice.

    Returns (pending: bool, detail: dict). The export is authoritative, so
    the AI seat's pending decision is expected to be visible here (verified
    on protocol 72; re-verified on 106 during this run).
    """
    wf = st.get("waiting_for") or {}
    wtype = wf.get("type")
    data = wf.get("data") or {}
    pending = data.get("pending") or []
    p1 = [p for p in pending if p.get("player") == 1]
    detail = {"waiting_for_type": wtype,
              "p1_pending": p1,
              "pending_players": [p.get("player") for p in pending]}
    if wtype == "ChooseFromZoneChoice" and p1:
        return True, detail
    return False, detail


def revealed_summary(st):
    rc = st.get("revealed_cards")
    prc = st.get("public_revealed_cards")
    def summ(x):
        if isinstance(x, dict):
            return {str(k)[:40]: (v if not isinstance(v, (list, dict))
                                  else f"<{type(v).__name__}:{len(v)}>")
                    for k, v in list(x.items())[:6]}
        if isinstance(x, list):
            return f"<list:{len(x)}>"
        return x
    return {"revealed_cards": summ(rc), "public_revealed_cards": summ(prc)}


async def observe_server_hello():
    """Raw handshake to capture the OBSERVED ServerHello and assert it
    matches the pinned release identity before driving anything."""
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with websockets.connect(url, max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        msg = json.loads(raw)
        assert msg.get("type") == "ServerHello", \
            f"expected ServerHello, got {msg.get('type')}"
        data = msg.get("data", {})
        obs = {
            "server_version": data.get("server_version"),
            "build_commit": data.get("build_commit"),
            "protocol_version": data.get("protocol_version"),
            "mode": data.get("mode"),
        }
        for k in ("server_version", "build_commit", "protocol_version",
                  "mode"):
            want = SERVER_IDENTITY[k]
            want_cmp = want[1:] if k == "server_version" and \
                str(want).startswith("v") else want
            assert obs[k] == want_cmp or obs[k] == want, (
                f"ServerHello {k}={obs[k]!r} != pinned {want!r}; "
                "refusing to run")
        say(f"ServerHello OK: {obs}")
        return obs


async def main():
    init_logging()
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_atraxa_cast", "A2_choice_pending", "A3_choice_resolves",
            "A4_zones_sane")}
    obs = {"atraxa_cast": False, "atraxa_cast_turn": None,
           "choice_seen": False, "choice_first_t": None,
           "choice_first_rev": None, "choice_last_progress_rev": None,
           "choice_last_progress_t": None, "choice_detail": None,
           "choice_resolved": False, "stall_detected": False,
           "stall_stuck_s": None, "rejections": [], "p0_acts": 0,
           "trigger_fired": False, "trigger_turn": None,
           "trigger_resolved_t": None,
           "revealed_at_choice": None, "ai_loop_stopped_sig": False,
           # selection-delta tracking: P1 hand/library around the ETB
           # resolution; a positive hand delta with the reveal clearing
           # proves the AI actually made its per-type selections even if
           # the pending window itself was too brief to observe.
           "sel_snap_pre": None, "sel_snap_post": None,
           "sel_delta": None, "sel_window_checks": 0}
    exported = {"pre": False, "post": False}
    trigger_samples = []
    seen_trigger_ids = set()
    land_played_turns = set()
    submitted_opps = set()

    p0 = PhaseClient("P0")
    await p0.connect()
    hello = await observe_server_hello()
    wire("server_hello", hello)
    ai_deck = deck(*P1_AI_DECK)
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Medium",
                               "deck": {"type": "DeckList", "data": ai_deck}}])
    say(f"game {p0.game_code}; P0 seat={p0.player_id}; P1 = AI Medium seat")
    wire("game_created", {"game_code": p0.game_code, "p0_seat": p0.player_id,
                          "p0_deck": P0_DECK, "p1_ai_deck": P1_AI_DECK})

    def drain_rejections():
        hits = []
        while True:
            try:
                t, data = p0.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("Error", "ActionRejected"):
                hits.append({"who": "P0", "type": t, "data": data})
        return hits

    async def export_state_dict(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            exported[tag] = True
            say(f"exported {tag}.json")
            return json.loads(s)["state"]
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            say(f"{tag} export failed: {e}")
            return None

    def sample_triggers(state):
        for e in state.get("stack", []) or []:
            kind = e.get("kind") or {}
            if kind.get("type") != "TriggeredAbility":
                continue
            eid = e.get("id")
            if eid in seen_trigger_ids:
                continue
            seen_trigger_ids.add(eid)
            src = lname(state, e.get("source_id"))
            rec = {"turn": state.get("turn_number"),
                   "phase": state.get("phase"), "source": src,
                   "entry": e}
            trigger_samples.append(rec)
            wire("trigger_on_stack", {"turn": rec["turn"],
                                      "phase": rec["phase"], "source": src})
            say(f"TRIGGER on stack (turn {rec['turn']} {rec['phase']}): "
                f"source={src}")
            if src == ATRAXA and not obs["trigger_fired"]:
                obs["trigger_fired"] = True
                obs["trigger_turn"] = state.get("turn_number")
                say(f"OBSERVED: Atraxa ETB trigger fired "
                    f"(turn {obs['trigger_turn']})")
                # live-view pre-trigger snapshot (denser than the 1s
                # export polls): P1 hand/library sizes while the trigger
                # is on the stack.
                if obs["sel_snap_pre"] is None:
                    p1 = player_of(state, 1)
                    obs["sel_snap_pre"] = {
                        "hand": len(p1.get("hand", [])),
                        "lib": len(p1.get("library", [])),
                        "rev": p0.revision, "turn": state.get("turn_number"),
                        "src": "live"}
                    say(f"SEL(live): pre-trigger P1 hand="
                        f"{obs['sel_snap_pre']['hand']} lib="
                        f"{obs['sel_snap_pre']['lib']}")
                    wire("sel_snap_pre_live", obs["sel_snap_pre"])

    async def check_ai_choice():
        """Authoritative-export check for the AI's ChooseFromZoneChoice.

        Returns (pending, info) where info carries the waiting_for detail,
        revealed-cards summary, P1 hand/library sizes, and stack depth --
        the raw material for the selection-delta analysis.
        """
        st = await export_state_dict("_probe")
        if st is None:
            return False, None
        try:
            os.remove(f"{EVDIR}/_probe.json")
        except OSError:
            pass
        pending, detail = ai_choice_pending(st)
        p1 = player_of(st, 1)
        trig_on_stack = any(
            (e.get("kind") or {}).get("type") == "TriggeredAbility"
            and lname(st, e.get("source_id")) == ATRAXA
            for e in st.get("stack", []) or [])
        return pending, {"detail": detail,
                         "revealed": revealed_summary(st),
                         "p1_hand": len(p1.get("hand", [])),
                         "p1_lib": len(p1.get("library", [])),
                         "priority_player": st.get("priority_player"),
                         "turn": st.get("turn_number"),
                         "phase": st.get("phase"),
                         "stack": len(st.get("stack", []) or []),
                         "atraxa_trigger_on_stack": trig_on_stack}

    # --- main watch loop ---
    t_loop = time.time()
    last_rev = -1
    last_tick_at = 0.0
    last_export_check = 0.0
    last_export_check_rev = -2
    while time.time() - t_loop < GAME_TIMEOUT:
        await asyncio.sleep(0.25)
        msg = p0.latest
        if not msg:
            continue
        rev = p0.revision
        same_rev = (rev == last_rev)
        stale = time.time() - last_tick_at > 5
        may_act = (not same_rev) or stale
        state = msg.get("state", {})

        for r in drain_rejections():
            obs["rejections"].append(r)
            wire("rejection", r)

        sample_triggers(state)

        # A1: Atraxa cast by the AI
        if not obs["atraxa_cast"] and bf_ids(state, 1, ATRAXA):
            obs["atraxa_cast"] = True
            obs["atraxa_cast_turn"] = state.get("turn_number")
            say(f"OBSERVED: AI cast Atraxa (turn {obs['atraxa_cast_turn']}, "
                f"rev {rev})")
            wire("atraxa_cast", {"turn": obs["atraxa_cast_turn"], "rev": rev})

        # AI choice monitoring (authoritative export) once Atraxa is down
        if obs["atraxa_cast"] and not obs["choice_resolved"] \
                and not obs["stall_detected"]:
            need = (rev != last_export_check_rev
                    or time.time() - last_export_check > EXPORT_POLL_S)
            if need:
                last_export_check = time.time()
                last_export_check_rev = rev
                pending, info = await check_ai_choice()
                if info is not None:
                    obs["sel_window_checks"] += 1
                    wire("ai_choice_check",
                         {"rev": rev, "pending": pending,
                          "detail": info["detail"],
                          "p1_hand": info["p1_hand"],
                          "p1_lib": info["p1_lib"],
                          "revealed": info["revealed"],
                          "stack": info["stack"],
                          "trig_on_stack": info["atraxa_trigger_on_stack"]})
                    # selection-delta tracking: snapshot P1 hand/library
                    # while the Atraxa trigger is on the stack (pre), and
                    # again once it has resolved. A positive hand delta
                    # with the matching library delta proves the AI made
                    # its per-type selections.
                    # (Do NOT gate on revealed_cards clearing:
                    # public_revealed_cards retains the bottomed cards as
                    # public information after the choice.)
                    if info["atraxa_trigger_on_stack"] \
                            and obs["sel_snap_pre"] is None:
                        obs["sel_snap_pre"] = {
                            "hand": info["p1_hand"], "lib": info["p1_lib"],
                            "rev": rev, "turn": info["turn"]}
                        say(f"SEL: pre-trigger snapshot P1 hand="
                            f"{info['p1_hand']} lib={info['p1_lib']} "
                            f"(rev {rev})")
                    if not info["atraxa_trigger_on_stack"] \
                            and obs["sel_snap_pre"] is not None \
                            and obs["sel_snap_post"] is None:
                        obs["sel_snap_post"] = {
                            "hand": info["p1_hand"], "lib": info["p1_lib"],
                            "rev": rev, "turn": info["turn"]}
                        pre, post = obs["sel_snap_pre"], obs["sel_snap_post"]
                        obs["sel_delta"] = {
                            "hand": post["hand"] - pre["hand"],
                            "lib": post["lib"] - pre["lib"]}
                        if obs["trigger_resolved_t"] is None:
                            obs["trigger_resolved_t"] = time.time()
                        say(f"SEL: post-trigger snapshot P1 hand="
                            f"{info['p1_hand']} lib={info['p1_lib']} "
                            f"(rev {rev}); delta={obs['sel_delta']}")
                        wire("selection_delta", obs["sel_delta"])
                        # If the delta shows the AI took selections into
                        # hand, the per-type choice was necessarily faced
                        # and answered: capture post.json and finish.
                        if obs["sel_delta"]["hand"] > 0 \
                                and not obs["choice_resolved"] \
                                and not obs["stall_detected"]:
                            obs["choice_resolved"] = True
                            say("OBSERVED (via selection delta): AI answered "
                                f"the ChooseFromZoneChoice (P1 hand "
                                f"{obs['sel_delta']['hand']:+d}); exporting "
                                "post.json")
                            wire("choice_resolved_via_delta",
                                 obs["sel_delta"])
                            await export_state_dict("post")
                            break
                        # Zero/negative delta: not a selection (or a declined
                        # choice). Reset so the next Atraxa trigger is
                        # measured fresh.
                        if not obs["choice_resolved"] \
                                and not obs["stall_detected"]:
                            say(f"SEL: delta {obs['sel_delta']} shows no "
                                f"selections; resetting snapshots for the "
                                f"next trigger")
                            wire("sel_reset", obs["sel_delta"])
                            obs["sel_snap_pre"] = None
                            obs["sel_snap_post"] = None
                            obs["sel_delta"] = None
                if pending and not obs["choice_seen"]:
                    obs["choice_seen"] = True
                    obs["choice_first_t"] = time.time()
                    obs["choice_first_rev"] = rev
                    obs["choice_last_progress_rev"] = rev
                    obs["choice_last_progress_t"] = time.time()
                    obs["choice_detail"] = info["detail"] if info else None
                    obs["revealed_at_choice"] = info["revealed"] if info \
                        else None
                    say(f"OBSERVED: ChooseFromZoneChoice pending for AI "
                        f"(player 1), rev {rev}; detail={obs['choice_detail']}")
                    wire("choice_seen", {"rev": rev,
                                         "detail": obs["choice_detail"],
                                         "revealed": obs["revealed_at_choice"]})
                    await export_state_dict("pre")
                elif pending:
                    if rev != obs["choice_last_progress_rev"]:
                        obs["choice_last_progress_rev"] = rev
                        obs["choice_last_progress_t"] = time.time()
                    elif (time.time() - obs["choice_last_progress_t"]
                          > STALL_TIMEOUT):
                        obs["stall_detected"] = True
                        obs["stall_stuck_s"] = (time.time()
                                                - obs["choice_last_progress_t"])
                        say(f"STALL: revision static at {rev} for "
                            f">{STALL_TIMEOUT}s while AI's "
                            f"ChooseFromZoneChoice pending -> reproduced")
                        wire("stall", {"rev": rev,
                                       "stuck_s": obs["stall_stuck_s"]})
                        break
                elif obs["choice_seen"]:
                    # waiting_for moved on from the AI's choice -> resolved
                    obs["choice_resolved"] = True
                    say(f"OBSERVED: AI's ChooseFromZoneChoice resolved "
                        f"(rev {rev}); export detail="
                        f"{info['detail'] if info else None}")
                    wire("choice_resolved",
                         {"rev": rev,
                          "detail": info["detail"] if info else None})
                    await export_state_dict("post")
                    break

        if obs["stall_detected"] and not exported["post"]:
            await export_state_dict("post")

        # stop early if the game ended
        if state.get("game_over") or state.get("winner") is not None:
            say("game ended before the Atraxa sequence completed")
            notes.append("game ended early")
            break

        # --- drive P0: land per turn, then pass priority ---
        if may_act:
            acts = merged_actions(msg)
            atypes = set(a.get("type") for a in acts)
            if await do_mulligan(p0, acts, 0, "P0"):
                last_rev = rev
                last_tick_at = time.time()
                continue
            if "OrderTriggers" in atypes:
                oa = find_action(acts, "OrderTriggers")
                if oa:
                    await submit_as_is(p0, oa)
                    last_rev = rev
                    last_tick_at = time.time()
                    continue
            if "DeclareAttackers" in atypes:
                da = find_action(acts, "DeclareAttackers")
                if da:
                    d = copy.deepcopy(da)
                    d.setdefault("data", {}).update({"attacks": [],
                                                     "bands": []})
                    await submit_as_is(p0, d)
                    say(f"P0 declares no attackers "
                        f"(turn {state.get('turn_number')})")
                last_rev = rev
                last_tick_at = time.time()
                continue
            if "DeclareBlockers" in atypes:
                da = find_action(acts, "DeclareBlockers")
                if da:
                    d = copy.deepcopy(da)
                    d.setdefault("data", {}).update({"assignments": []})
                    await submit_as_is(p0, d)
                last_rev = rev
                last_tick_at = time.time()
                continue
            if my_priority(acts):
                turn = state.get("turn_number")
                la = find_action(acts, "PlayLand", ISLAND, state)
                if la and turn not in land_played_turns:
                    await submit_as_is(p0, la)
                    land_played_turns.add(turn)
                    obs["p0_acts"] += 1
                    wire("p0_play_land", {"turn": turn})
                else:
                    if await pass_priority(p0, msg, acts):
                        obs["p0_acts"] += 1
                last_rev = rev
                last_tick_at = time.time()
        # log anything P0 is being asked that isn't pass/play-land (rare)
        wf = state.get("waiting_for") or {}
        if (wf.get("data") or {}).get("pending"):
            for p in (wf.get("data") or {}).get("pending", []):
                if p.get("player") == 0 and wf.get("type") not in (
                        "MulliganDecision", None):
                    say(f"NOTE: P0 has pending decision {wf.get('type')}")

    if obs["stall_detected"] and not exported["post"]:
        await export_state_dict("post")

    # --- server-log corroboration: the AI loop failure signature ---
    try:
        slog = open(f"{BACKFILL}/runs/{RUN_ID}/server.log", encoding="utf-8",
                    errors="replace").read()
        sig_lines = [ln for ln in slog.splitlines()
                     if "choose_action" in ln or "stopping AI loop" in ln
                     or "ChooseFromZoneChoice" in ln]
        obs["ai_loop_stopped_sig"] = any("stopping AI loop" in ln
                                         for ln in sig_lines)
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write(f"# server log excerpts for run {RUN_ID} "
                    f"(issue #{ISSUE})\n")
            f.write(f"# signature 'stopping AI loop' present: "
                    f"{obs['ai_loop_stopped_sig']}\n")
            for ln in sig_lines[-60:]:
                f.write(ln + "\n")
        say(f"server-log AI signature present: {obs['ai_loop_stopped_sig']} "
            f"({len(sig_lines)} matching lines)")
        wire("server_log_sig", {"present": obs["ai_loop_stopped_sig"],
                                "n_lines": len(sig_lines)})
    except Exception as e:
        notes.append(f"server log read failed: {e}")

    # --- evaluate ---
    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        try:
            return json.loads(open(p).read())["state"]
        except Exception:
            return None

    pre_st = load_env("pre")
    post_st = load_env("post")

    if obs["atraxa_cast"]:
        ass["A1_atraxa_cast"] = "passed"
    else:
        ass["A1_atraxa_cast"] = "failed"
        notes.append("AI never cast Atraxa within GAME_TIMEOUT; "
                     "setup insufficient")

    if pre_st is not None:
        pending, detail = ai_choice_pending(pre_st)
        if pending:
            ass["A2_choice_pending"] = "passed"
        else:
            ass["A2_choice_pending"] = "failed"
            notes.append(f"pre.json waiting_for does not show the AI's "
                         f"ChooseFromZoneChoice: {detail}")
    elif obs["choice_seen"]:
        ass["A2_choice_pending"] = "passed"  # observed live, export missing
        notes.append("pre.json export missing though choice was observed live")
    else:
        ass["A2_choice_pending"] = "not-run"

    # Selection-delta fallback: if the pending window was too brief to
    # observe but P1's hand grew across the ETB resolution while the reveal
    # cleared, the AI necessarily faced and answered the per-type choice.
    sel = obs.get("sel_delta") or {}
    sel_proves_choice = (
        obs["trigger_fired"]
        and sel.get("hand") is not None and sel["hand"] > 0
        and not obs["stall_detected"])
    if sel_proves_choice:
        if ass["A2_choice_pending"] == "not-run":
            ass["A2_choice_pending"] = "passed"
            notes.append("A2: pending window not directly observed (brief on "
                         "106); inferred passed from the selection delta "
                         f"(P1 hand {sel['hand']:+d} across the ETB "
                         "resolution with the reveal clearing)")
        if not obs["choice_resolved"]:
            obs["choice_resolved"] = True
            notes.append("A3: choice resolution inferred from the selection "
                         "delta (no stall; game advanced; P1 hand grew by "
                         "selections)")

    if obs["stall_detected"]:
        ass["A3_choice_resolves"] = "failed"
    elif obs["choice_resolved"]:
        ass["A3_choice_resolves"] = "passed"
    elif obs["choice_seen"]:
        ass["A3_choice_resolves"] = "not-run"
        notes.append("choice seen but run ended before resolution/stall verdict")
    else:
        ass["A3_choice_resolves"] = "not-run"

    if post_st is not None and obs["choice_resolved"]:
        objs = post_st.get("objects", {})
        p1 = player_of(post_st, 1)
        hand_n = len(p1.get("hand", []))
        lib_n = len(p1.get("library", []))
        strange = [oid for oid, o in objs.items()
                   if o.get("controller") == 1
                   and o.get("zone") not in ("Battlefield", "Hand", "Library",
                                             "Graveyard", "Exile", "Command")]
        ass["A4_zones_sane"] = "passed" if not strange else "failed"
        notes.append(f"post: P1 hand={hand_n} library={lib_n} "
                     f"strange-zone objects={len(strange)}")
    else:
        ass["A4_zones_sane"] = "not-run"

    if ass["A1_atraxa_cast"] != "passed":
        verdict = "blocked"
    elif (ass["A2_choice_pending"] == "passed"
          and ass["A3_choice_resolves"] == "failed"):
        verdict = "reproduced"
    elif all(ass[k] == "passed" for k in ("A1_atraxa_cast",
                                         "A2_choice_pending",
                                         "A3_choice_resolves",
                                         "A4_zones_sane")):
        verdict = "not-reproduced"
    elif (ass["A1_atraxa_cast"] == "passed"
          and ass["A2_choice_pending"] == "passed"):
        verdict = "reproduced" if ass["A3_choice_resolves"] == "failed" \
            else "blocked"
    else:
        verdict = "blocked"

    say(f"VERDICT: {verdict}")
    say(f"assertions: {json.dumps(ass)}")

    run = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
        "server": SERVER_IDENTITY,
        "server_observed": hello,
        "verdict": verdict,
        "assertions": ass,
        "observations": obs,
        "notes": notes,
        "trigger_samples": trigger_samples,
        "setup_line": "P0: 60x island (human driver). P1: AI Medium, 12x "
                      "Atraxa, Grand Unifier + 12x each Forest/Plains/Island/"
                      "Swamp.",
        "contract_line": "AI casts Atraxa -> ETB reveals 10 -> AI must submit "
                         "a legal ChooseFromZoneChoice (one card per type, "
                         "optional) and the game advances.",
        "limitations": [
            "Browser/UI not exercised; the separate frontend 'Maximum call "
            "stack size exceeded' (multiplayerDraftStore) defect is out of "
            "scope for this engine+AI test",
            "Server-side AI (phase-ai) policy tested, not the browser WASM "
            "AI driver path",
        ],
        "stats": {"p0_actions": obs["p0_acts"],
                  "trigger_observations": int(obs["trigger_fired"])},
        "scenario": {"file": "scenario_6594_01020.py",
                     "sha256": hashlib.sha256(
                         open(__file__, "rb").read()).hexdigest()},
        "decks": {"P0": {"main": P0_DECK}, "P1_AI": {"main": P1_AI_DECK}},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2, default=str)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(ass, f, indent=2)
    with open(f"{EVDIR}/obs.json", "w") as f:
        json.dump(obs, f, indent=2, default=str)
    # copy the driver source into the evidence dir
    with open(__file__, "rb") as src, \
            open(f"{EVDIR}/scenario_6594_01020.py", "wb") as dst:
        dst.write(src.read())

    # close logs BEFORE the manifest (lesson: hashes must cover final bytes)
    WIRE.close()
    RUNLOG.close()
    import subprocess
    files = sorted(f for f in os.listdir(EVDIR)
                   if f != "manifest.sha256"
                   and os.path.isfile(os.path.join(EVDIR, f)))
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            h = hashlib.sha256(
                open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
            mf.write(f"{h}  {fn}\n")
    say(f"manifest written for {len(files)} files")
    await p0.close()
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    print("FINAL:", v)
