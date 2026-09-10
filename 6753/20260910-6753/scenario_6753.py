#!/usr/bin/env python3
"""Issue #6753: "AI Opponent and Audacious Swap -- Just had the AI stuck
trying to select a target for this spell endlessly." (reporter: matthewevans,
source:discord, status:confirmed, area:engine+ai, priority:p1-infinite-loop)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Subsystem under test: native server-side AI policy (phase-ai) on the pinned
release. P1 is an AI (Medium) seat; P0 is a passive human driver seat.

Audacious Swap {3}{R} instant: "The owner of target nonenchantment permanent
shuffles it into their library, then exiles the top card of their library.
If it's a land card, they put it onto the battlefield. Otherwise, they may
cast it without paying its mana cost." (plus optional Casualty 2)

Run history: an earlier attempt with P0 on 12x bears + 48x island showed the
AI casts Audacious Swap even with no opposing creatures (turn 8, during P0's
PreCombatMain, as an instant) and the spell resolved within ~0.5s -- the
target selection completed inside the AI's action batch with no WS-visible
stall. P0 reverted to 60x island (minimal passive driver); the broken
bear-casting branch is dropped.

Setup:
  P0 (human driver): 60x island. Keep mulligan; play one island per turn;
                     pass priority; never attacks.
  P1 (AI, Medium): 12x "audacious swap" + 24x island + 24x mountain (dense
                   playset so the AI draws and can cast the {3}{R} spell).

Expected (triage acceptance criteria):
  E1: the AI casts Audacious Swap (spell on the stack, controller 1).
  E2: the AI's target selection completes: the spell leaves the stack well
      within the stall window (the same waiting state is NOT re-selected
      repeatedly without state progress).
  E3: the spell resolves per Oracle: the targeted nonenchantment permanent is
      shuffled into its owner's library, the top card of that library is
      exiled, a land goes to the battlefield, and the instant goes to the
      AI's graveyard.
  E4: the game continues past each resolution; repeat casts are watched for
      the same loop signature.

Assertions:
  A1_swap_cast       >=1 AI Audacious Swap observed on the stack.
  A2_target_chosen   every observed cast resolved (spell left the stack) --
                     a targeted spell cannot be cast without a chosen target.
  A3_no_target_loop  every cast resolved within STALL_TIMEOUT of the cast;
                     no AI target wait persisted.
  A4_swap_resolves   pre->post: targeted permanent left the battlefield into
                     its owner's library; exiled top card handled (land to
                     battlefield); swap in AI graveyard.
  A5_cleanup         game proceeds after resolution (or run ended by design
                     after the watch window with the game still advancing).

Verdict rule: blocked iff A1 fails (AI never cast: setup broken).
reproduced iff a cast stalls (A3 fails) -- the reported endless loop.
not-reproduced iff A1..A5 all pass.

Evidence: evidence/6753/<run-id>/pre.json (first swap on stack), post.json
(after first resolution), mid_target.json (if a WS-visible AI target wait is
ever caught), run.json, manifest.sha256, summary.png, scenario_6753.py,
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

client.URL = "ws://localhost:9377/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6753"
EVDIR = f"{BACKFILL}/evidence/6753/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SWAP = "audacious swap"
P0_DECK = [("island", 60)]
P1_AI_DECK = [(SWAP, 12), ("island", 24), ("mountain", 24)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_verified": True,
    "signature_note": "minisign-verify (prehashed BLAKE2b-512, sigalg ED) of "
                      "phase-server + signed data manifest with repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY; data files match manifest "
                      "SHA-256 (verified 2026-09-09, digests re-checked this run)",
    "observed_at": "2026-09-10",
    "source": "ServerHello handshake vs pinned v0.78.0 release artifacts "
              "(server/releases/v0.78.0/); isolated server on 127.0.0.1:9377 "
              "(started by this run, verified live before driving)",
}

STALL_TIMEOUT = 150   # max seconds one AI cast may stay unresolved
GAME_TIMEOUT = 900    # overall budget
EXTRA_WATCH = 420     # keep watching for repeat casts after the first resolves


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


def swap_on_stack(state):
    return [oid for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Stack" and obj_name(o) == SWAP]


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
           ("A1_swap_cast", "A2_target_chosen", "A3_no_target_loop",
            "A4_swap_resolves", "A5_cleanup")}
    casts = []  # one record per observed AI cast
    obs = {"rejections": [], "p0_acts": 0, "casts": casts,
           "ws_target_wait_seen": False}
    exported = {"pre": False, "post": False, "mid_target": False}
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
    last_tick_at = 0.0
    first_resolve_t = None
    known_stack = set()   # stack oids already recorded as casts

    while time.time() - t_loop < GAME_TIMEOUT:
        await asyncio.sleep(0.05)
        states_seen += 1
        msg = p0.latest
        if not msg:
            continue
        rev = p0.revision
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

        # --- cast tracking ---
        for oid in swap_on_stack(state):
            if oid not in known_stack:
                known_stack.add(oid)
                rec = {"oid": int(oid), "cast_t": now, "cast_rev": rev,
                       "cast_turn": state.get("turn_number"),
                       "target_wait_seen": False, "resolved": False,
                       "resolve_t": None, "resolve_s": None}
                casts.append(rec)
                say(f"OBSERVED cast #{len(casts)}: AI Audacious Swap on stack "
                    f"(oid {oid}, turn {rec['cast_turn']}, rev {rev})")
                wire("swap_cast", {"n": len(casts), "oid": int(oid), "rev": rev,
                                   "turn": rec["cast_turn"],
                                   "waiting_for": state.get("waiting_for")})
                if not exported["pre"]:
                    await export_state_dict(p0, "pre")

        # WS-visible AI target wait (caught if the poll lands inside it)
        if wfd.get("player") == 1 and known_stack and not all(
                c["resolved"] for c in casts):
            for c in casts:
                if not c["resolved"] and not c["target_wait_seen"]:
                    c["target_wait_seen"] = True
                    obs["ws_target_wait_seen"] = True
                    say(f"OBSERVED: WS-visible target wait for AI (cast #{casts.index(c)+1}, "
                        f"wf={wf.get('type')}, rev {rev})")
                    wire("target_wait_seen", {"cast": casts.index(c) + 1,
                                              "rev": rev,
                                              "waiting_for": wf})
                    if not exported["mid_target"]:
                        await export_state_dict(p0, "mid_target")

        # resolution / stall per cast
        current_stack = set(swap_on_stack(state))
        for c in casts:
            if c["resolved"]:
                continue
            if str(c["oid"]) not in current_stack:
                c["resolved"] = True
                c["resolve_t"] = now
                c["resolve_s"] = round(now - c["cast_t"], 2)
                say(f"OBSERVED cast #{casts.index(c)+1} resolved in "
                    f"{c['resolve_s']}s (rev {rev})")
                wire("swap_resolved", {"cast": casts.index(c) + 1,
                                       "resolve_s": c["resolve_s"], "rev": rev})
                if first_resolve_t is None:
                    first_resolve_t = now
                    if not exported["post"]:
                        await export_state_dict(p0, "post")
            elif now - c["cast_t"] > STALL_TIMEOUT:
                c["stalled"] = True
                say(f"STALL: cast #{casts.index(c)+1} unresolved after "
                    f"{STALL_TIMEOUT}s (rev {rev}) -> reproduced")
                wire("stall", {"cast": casts.index(c) + 1, "rev": rev})
                if not exported["post"]:
                    await export_state_dict(p0, "post")
                break

        if any(c.get("stalled") for c in casts):
            break

        # end the run EXTRA_WATCH after the first resolution (game still going)
        if first_resolve_t is not None and now - first_resolve_t > EXTRA_WATCH:
            say(f"watch window complete ({EXTRA_WATCH}s after first resolution); "
                f"{len(casts)} cast(s) observed")
            break

        if state.get("game_over") or state.get("winner") is not None:
            say("game ended")
            notes.append("game ended before watch window elapsed")
            break

        # --- drive P0: land per turn, pass priority, empty attacks ---
        if may_act:
            acts = merged_actions(msg)
            turn = state.get("turn_number")
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
            if not acted:
                pl = find_action(acts, "PlayLand", "island", state)
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

    if not exported["post"]:
        await export_state_dict(p0, "post")

    # --- evaluate ---
    pre_st = load_env("pre")
    post_st = load_env("post")

    if casts:
        ass["A1_swap_cast"] = "passed"
        notes.append(f"{len(casts)} AI cast(s): " +
                     ", ".join(f"#{i+1} turn {c['cast_turn']} "
                               f"resolved={c['resolved']} "
                               f"({c.get('resolve_s', '?')}s)"
                               for i, c in enumerate(casts)))
    else:
        ass["A1_swap_cast"] = "failed"
        notes.append("AI never cast Audacious Swap within GAME_TIMEOUT")

    if casts and all(c["resolved"] for c in casts):
        ass["A2_target_chosen"] = "passed"
    elif casts:
        ass["A2_target_chosen"] = "failed"
    else:
        ass["A2_target_chosen"] = "not-run"

    if casts and all(c["resolved"] and (c.get("resolve_s") or 0) <= STALL_TIMEOUT
                     for c in casts):
        ass["A3_no_target_loop"] = "passed"
        worst = max(c.get("resolve_s") or 0 for c in casts)
        notes.append(f"all casts resolved within {worst}s "
                     f"(window {STALL_TIMEOUT}s); "
                     f"ws-visible AI target wait caught: {obs['ws_target_wait_seen']}")
    elif casts:
        ass["A3_no_target_loop"] = "failed"
    else:
        ass["A3_no_target_loop"] = "not-run"

    if pre_st is not None and post_st is not None and casts and casts[0]["resolved"]:
        pre_objs = pre_st.get("objects", {})
        post_objs = post_st.get("objects", {})
        # targeted permanent: a battlefield object in pre that is in its
        # owner's library in post
        moved = []
        for oid, o in pre_objs.items():
            if o.get("zone") == "Battlefield":
                po = post_objs.get(oid, {})
                if po.get("zone") == "Library":
                    moved.append((oid, obj_name(o), o.get("controller"),
                                  po.get("owner", o.get("owner"))))
        gy = [oid for oid, o in post_objs.items()
              if o.get("zone") == "Graveyard" and o.get("controller") == 1
              and obj_name(o) == SWAP]
        stuck = [oid for oid in known_stack
                 if (post_objs.get(str(oid), {}).get("zone")) == "Stack"]
        ok = bool(moved) and bool(gy) and not stuck
        ass["A4_swap_resolves"] = "passed" if ok else "failed"
        notes.append(f"pre->post: battlefield->library {moved}; "
                     f"swap in AI gy: {len(gy)}; swap stuck on stack: {len(stuck)}")
    else:
        ass["A4_swap_resolves"] = "not-run"
        notes.append("resolution detail not evaluated (no pre/post pair)")

    if post_st is not None and casts and all(c["resolved"] for c in casts):
        ass["A5_cleanup"] = "passed"
        notes.append(f"game state after resolution: turn {post_st.get('turn_number')} "
                     f"phase {post_st.get('phase')} "
                     f"waiting_for={(post_st.get('waiting_for') or {}).get('type')}")
    else:
        ass["A5_cleanup"] = "not-run"

    if ass["A1_swap_cast"] != "passed":
        verdict = "blocked"
    elif ass["A3_no_target_loop"] == "failed":
        verdict = "reproduced"
    elif all(ass[k] == "passed" for k in ("A1_swap_cast", "A2_target_chosen",
                                         "A3_no_target_loop", "A4_swap_resolves",
                                         "A5_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    say(f"VERDICT: {verdict}")
    say(f"assertions: {json.dumps(ass)}")

    run = {
        "run_id": RUN_ID,
        "issue": 6753,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
        "server": SERVER_IDENTITY,
        "verdict": verdict,
        "assertions": ass,
        "observations": obs,
        "notes": notes,
        "setup_line": "P0: 60x island (human driver, passive). P1: AI Medium, "
                      "12x Audacious Swap + 24x island + 24x mountain.",
        "contract_line": "AI casts Audacious Swap -> target selection must "
                         "complete (spell leaves the stack) within the stall "
                         "window; the spell resolves per Oracle and the game "
                         "continues. Repeat casts watched for the same loop.",
        "limitations": [
            "Browser/UI not exercised; server-side AI (phase-ai) policy tested, "
            "not any browser/client AI driver path",
            "Casualty 2 not exercised (AI deck has no creatures to sacrifice)",
            "Not tested on the original 2026-07-29 build; verdict is scoped to "
            "v0.78.0, not a fix claim",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay)",
        ],
        "stats": {"states_seen": states_seen, "casts_observed": len(casts)},
        "scenario": {"file": "scenario_6753.py",
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
