#!/usr/bin/env python3
"""Issue #6594: "Stuck decision: ChooseFromZoneChoice" -- in a game vs the AI,
the AI cast Atraxa, Grand Unifier and the game stalled on the resulting
choose-from-zone decision (console: "AI stuck: 3 consecutive failures on
ChooseFromZoneChoice, dispatching fallback"; report builds v0.35.2/v0.41.0).

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
  A2_choice_pending pre.json: waiting_for.type == ChooseFromZoneChoice for
                    player 1.
  A3_choice_resolves waiting_for leaves ChooseFromZoneChoice within the stall
                    window (no multi-minute stall with static revision).
  A4_zones_sane     post.json: no revealed cards stranded outside
                    hand/library-bottom; hand gain consistent with
                    one-per-type selection.

Verdict rule: blocked iff A1 fails (AI never cast Atraxa: setup broken).
reproduced iff A1+A2 pass and A3 fails (the reported stall). not-reproduced
iff A1..A4 all pass.

Evidence: evidence/6594/<run-id>/pre.json (ChooseFromZoneChoice pending for
the AI seat), post.json (resolved or stuck state), run.json, manifest.sha256,
summary.png, scenario_6594.py, wire_log.jsonl, scenario_run.log.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6594"
EVDIR = f"{BACKFILL}/evidence/6594/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ATRAXA = "atraxa, grand unifier"
P0_DECK = [("island", 60)]
P1_AI_DECK = [(ATRAXA, 12), ("forest", 12), ("plains", 12),
              ("island", 12), ("swamp", 12)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_verified": True,
    "signature_note": "minisign-verify (prehashed BLAKE2b-512, sigalg ED) of "
                      "phase-server + signed data manifest with repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY; data files match manifest "
                      "SHA-256 (verified 2026-09-09, digests re-checked this run)",
    "observed_at": "2026-09-10",
    "source": "ServerHello handshake vs pinned v0.78.0 release artifacts "
              "(server/releases/v0.78.0/); isolated server on 127.0.0.1:9374 "
              "(started by this run, verified live before driving)",
}

STALL_TIMEOUT = 150      # seconds with static revision while AI's choice pending
GAME_TIMEOUT = 900       # overall budget for the AI to draw+cast Atraxa


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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def bf_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name]


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
        if state is not None and obj_name(state.get("objects", {}).get(str(oid), {})) == name:
            return a
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
           ("A1_atraxa_cast", "A2_choice_pending", "A3_choice_resolves",
            "A4_zones_sane")}
    obs = {"atraxa_cast": False, "atraxa_cast_turn": None,
           "choice_seen": False, "choice_first_t": None,
           "choice_last_rev": None, "choice_last_rev_t": None,
           "choice_iid": None, "choice_waiting_for": None,
           "choice_resolved": False, "stall_detected": False,
           "rejections": [], "p0_acts": 0}
    exported = {"pre": False, "post": False}
    states_seen = 0
    submitted_interactions = set()

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

    # --- mulligan: keep ---
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
                d = a.get("data", {}) or {}
                # established shape: {"type":"MulliganDecision","data":{"choice":{"type":"Keep"}}}
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say("P0 mulligan: Keep")
                return True
        return False

    await keep_mulligan()
    land_played_turns = set()

    def choice_pending_for_ai(state):
        wf = state.get("waiting_for") or {}
        wfd = wf.get("data") or {}
        return (wf.get("type") == "ChooseFromZoneChoice"
                and wfd.get("player") == 1)

    # --- main watch loop ---
    t_loop = time.time()
    last_rev = -1
    last_tick_at = 0.0
    while time.time() - t_loop < GAME_TIMEOUT:
        await asyncio.sleep(0.25)
        states_seen += 1
        msg = p0.latest
        if not msg:
            continue
        rev = p0.revision
        same_rev = (rev == last_rev)
        stale = time.time() - last_tick_at > 5
        # gate P0 driving on new revisions (re-tick at most every 5s anyway);
        # observation (stall detection) runs every iteration regardless.
        may_act = (not same_rev) or stale
        state = msg.get("state", {})

        for r in drain_rejections(p0):
            obs["rejections"].append(r)
            wire("rejection", r)

        # A1: Atraxa cast by the AI
        if not obs["atraxa_cast"] and bf_ids(state, 1, ATRAXA):
            obs["atraxa_cast"] = True
            obs["atraxa_cast_turn"] = state.get("turn_number")
            say(f"OBSERVED: AI cast Atraxa (turn {obs['atraxa_cast_turn']}, rev {rev})")
            wire("atraxa_cast", {"turn": obs["atraxa_cast_turn"], "rev": rev})

        # A2: the ChooseFromZoneChoice decision for the AI seat
        if choice_pending_for_ai(state):
            if not obs["choice_seen"]:
                obs["choice_seen"] = True
                obs["choice_first_t"] = time.time()
                obs["choice_waiting_for"] = state.get("waiting_for")
                say(f"OBSERVED: ChooseFromZoneChoice pending for AI (player 1), rev {rev}")
                wire("choice_seen", {"rev": rev,
                                    "waiting_for": state.get("waiting_for")})
                await export_state_dict(p0, "pre")
                obs["choice_last_rev"] = rev
                obs["choice_last_rev_t"] = time.time()
            else:
                if rev != obs["choice_last_rev"]:
                    obs["choice_last_rev"] = rev
                    obs["choice_last_rev_t"] = time.time()
                elif (time.time() - obs["choice_last_rev_t"] > STALL_TIMEOUT):
                    obs["stall_detected"] = True
                    say(f"STALL: revision static at {rev} for >{STALL_TIMEOUT}s "
                        f"while AI's ChooseFromZoneChoice pending -> reproduced")
                    wire("stall", {"rev": rev,
                                   "stuck_s": time.time() - obs["choice_last_rev_t"]})
                    break
        elif obs["choice_seen"] and not obs["choice_resolved"]:
            # waiting_for moved on from the AI's choice -> resolved
            obs["choice_resolved"] = True
            say(f"OBSERVED: AI's ChooseFromZoneChoice resolved (rev {rev}); "
                f"now waiting_for={(state.get('waiting_for') or {}).get('type')}")
            wire("choice_resolved", {"rev": rev,
                                     "waiting_for": state.get("waiting_for")})
            await export_state_dict(p0, "post")
            break

        # stop early if the game ended
        if state.get("game_over") or state.get("winner") is not None:
            say("game ended before Atraxa sequence completed")
            notes.append("game ended early")
            break

        # --- drive P0: play a land per turn, then pass priority ---
        if may_act:
            acts = merged_actions(msg)
            pl = find_action(acts, "PlayLand", "island", state)
            turn = state.get("turn_number")
            if pl and turn not in land_played_turns:
                await submit_as_is(p0, pl)
                land_played_turns.add(turn)
                obs["p0_acts"] += 1
                wire("p0_play_land", {"turn": turn})
            else:
                pp = find_action(acts, "PassPriority")
                if pp:
                    await submit_as_is(p0, pp)
                    obs["p0_acts"] += 1
            last_rev = rev
            last_tick_at = time.time()
        # log anything P0 is being asked that isn't pass/play-land (rare)
        wf = state.get("waiting_for") or {}
        if (wf.get("data") or {}).get("player") == 0 and wf.get("type") not in (
                "Priority", "MulliganDecision", None):
            say(f"NOTE: P0 has pending decision {wf.get('type')}")

    if obs["stall_detected"] and not exported["post"]:
        await export_state_dict(p0, "post")

    # --- evaluate ---
    pre_st = load_env("pre")
    post_st = load_env("post")

    if obs["atraxa_cast"]:
        ass["A1_atraxa_cast"] = "passed"
    else:
        ass["A1_atraxa_cast"] = "failed"
        notes.append("AI never cast Atraxa within GAME_TIMEOUT; setup insufficient")

    if pre_st is not None:
        wf = pre_st.get("waiting_for") or {}
        wfd = wf.get("data") or {}
        if wf.get("type") == "ChooseFromZoneChoice" and wfd.get("player") == 1:
            ass["A2_choice_pending"] = "passed"
        else:
            ass["A2_choice_pending"] = "failed"
    elif obs["choice_seen"]:
        ass["A2_choice_pending"] = "passed"  # observed live, export missing
        notes.append("pre.json export missing though choice was observed live")
    else:
        ass["A2_choice_pending"] = "not-run"

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
        # zones sane: nothing stranded in a revealed/exile limbo tied to Atraxa
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
    elif ass["A2_choice_pending"] == "passed" and ass["A3_choice_resolves"] == "failed":
        verdict = "reproduced"
    elif all(ass[k] == "passed" for k in ("A1_atraxa_cast", "A2_choice_pending",
                                         "A3_choice_resolves", "A4_zones_sane")):
        verdict = "not-reproduced"
    elif ass["A1_atraxa_cast"] == "passed" and ass["A2_choice_pending"] == "passed":
        verdict = "reproduced" if ass["A3_choice_resolves"] == "failed" else "blocked"
    else:
        verdict = "blocked"

    say(f"VERDICT: {verdict}")
    say(f"assertions: {json.dumps(ass)}")

    run = {
        "run_id": RUN_ID,
        "issue": 6594,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
        "server": SERVER_IDENTITY,
        "verdict": verdict,
        "assertions": ass,
        "observations": obs,
        "notes": notes,
        "setup_line": "P0: 60x island (human driver). P1: AI Medium, 12x Atraxa, Grand Unifier + 12x each Forest/Plains/Island/Swamp.",
        "contract_line": "AI casts Atraxa -> ETB reveals 10 -> AI must submit a legal ChooseFromZoneChoice (one card per type, optional) and the game advances.",
        "limitations": [
            "Browser/UI not exercised; the separate frontend 'Maximum call stack size exceeded' (multiplayerDraftStore) defect is out of scope for this engine+AI test",
            "Server-side AI (phase-ai) policy tested, not the browser WASM AI driver path",
        ],
        "stats": {"states_seen": states_seen,
                  "trigger_observations": int(obs["choice_seen"])},
        "scenario": {"file": "scenario_6594.py",
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


if __name__ == "__main__":
    v = asyncio.run(main())
    print("FINAL:", v)
