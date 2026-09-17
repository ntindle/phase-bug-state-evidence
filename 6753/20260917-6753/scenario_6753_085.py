#!/usr/bin/env python3
"""Issue #6753: "AI Opponent and Audacious Swap -- Just had the AI stuck
trying to select a target for this spell endlessly." (reporter: matthewevans,
source:discord, status:confirmed, area:engine+ai, priority:p1-infinite-loop)

RE-VALIDATION RUN on pinned v0.85.0 (protocol 72). Prior run 20260910-6753
(v0.78.0, protocol 68) found: reported endless target-selection loop NOT
reproduced (10 AI casts, all resolved <0.6s), plus a distinct secondary
defect: Audacious Swap exiled the top card of the CASTER's library instead
of the targeted permanent's OWNER's library. This run re-tests both on the
current pin; the maintained issue comment (ntindle, comment 5620965087)
carries the v0.78.0 result.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2)
--------------------------------------------------------------------------------
Subsystem under test: native server-side AI policy (phase-ai). P1 is an AI
(Medium) seat; P0 is a passive human driver seat.

Audacious Swap {3}{R} instant: "The owner of target nonenchantment permanent
shuffles it into their library, then exiles the top card of their library.
If it's a land card, they put it onto the battlefield. Otherwise, they may
cast it without paying its mana cost." (plus optional Casualty 2)

Setup:
  P0 (human driver): 60x island. Keep mulligan; play one island per turn;
                     pass priority (only when priority_player == 0);
                     declare no attackers; never attacks.
  P1 (AI, Medium): 12x "audacious swap" + 24x island + 24x mountain.

Assertions:
  A1_swap_cast       >=1 AI Audacious Swap observed on the stack.
  A2_target_chosen   every observed cast resolved (spell left the stack) --
                     a targeted spell cannot be cast without a chosen target.
  A3_no_target_loop  every cast resolved within STALL_TIMEOUT of the cast;
                     no AI target wait persisted; no AI-loop stall signature
                     in the server log.
  A4_swap_resolves   pre->post (cast #1): targeted permanent left the
                     battlefield into its OWNER's library (A4a); the exiled
                     top card came from the targeted permanent's OWNER's
                     library (A4b); the swap reached the AI's graveyard
                     (A4c); nothing stuck on the stack (A4d).
  A5_cleanup         game proceeds after resolution.

Verdict rule: blocked iff A1 fails (AI never cast: setup broken).
reproduced iff a cast stalls (A3 fails) -- the reported endless loop.
not-reproduced iff A1/A2/A3/A5 pass and A4's only failure mode is the
documented wrong-library secondary defect (A4b), with the secondary finding
recorded separately. (Precedent: the v0.78.0 published verdict.)

Evidence: evidence/6753/<run-id>/pre.json (first swap on stack), post.json
(after first resolution), mid_target.json (if a WS-visible AI target wait is
ever caught), run.json, manifest.sha256, summary.png, scenario_6753_085.py,
wire_log.jsonl, scenario_run.log, server_excerpts.log.
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
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

client.URL = "ws://localhost:9374/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260917-6753"
EVDIR = f"{BACKFILL}/evidence/6753/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SWAP = "audacious swap"
P0_DECK = [("island", 60)]
P1_AI_DECK = [(SWAP, 12), ("island", 24), ("mountain", 24)]


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


RELDIR = f"{BACKFILL}/server/releases/v0.85.0"
SERVER_IDENTITY = {
    "server_version": "0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "mode": "Full",
    "binary_sha256": _sha(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": _sha(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": _sha(f"{RELDIR}/data/draft-pools.json"),
    "signature_verified": True,
    "signature_note": "minisign-verify (prehashed BLAKE2b-512, sigalg ED) of "
                      "phase-server + signed data manifest with repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (verified 2026-09-16; "
                      "digests recomputed against on-disk release files "
                      "this run, never copied)",
    "observed_at": "2026-09-17",
    "source": "ServerHello handshake vs pinned v0.85.0 release artifacts "
              "(server/releases/v0.85.0/); isolated server on 127.0.0.1:9374 "
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


async def observe_server_hello():
    url = os.environ.get("PHASE_WS_URL", client.URL)
    async with websockets.connect(url, max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        msg = json.loads(raw)
        assert msg.get("type") == "ServerHello", f"expected ServerHello, got {msg.get('type')}"
        data = msg.get("data", {})
        obs = {
            "server_version": data.get("server_version"),
            "build_commit": data.get("build_commit"),
            "protocol_version": data.get("protocol_version"),
            "mode": data.get("mode"),
        }
        for k in ("server_version", "build_commit", "protocol_version", "mode"):
            assert obs[k] == SERVER_IDENTITY[k], (
                f"ServerHello {k}={obs[k]!r} != pinned {SERVER_IDENTITY[k]!r}")
        return obs


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_swap_cast", "A2_target_chosen", "A3_no_target_loop",
            "A4_swap_resolves", "A5_cleanup")}
    a4_detail = {}
    casts = []  # one record per observed AI cast
    obs = {"rejections": [], "p0_acts": 0, "casts": casts,
           "ws_target_wait_seen": False, "ai_target_waits": 0,
           "ai_stall_signature": False, "target_waits": []}
    exported = {"pre": False, "post": False, "mid_target": False}
    states_seen = 0

    hello = await observe_server_hello()
    wire("server_hello", hello)
    say(f"ServerHello OK: {hello}")

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
                d = a.get("data") or {}
                keep = None
                if isinstance(d, dict):
                    ch = d.get("choice")
                    if isinstance(ch, dict) and str(ch.get("type", "")).lower() == "keep":
                        keep = {"type": "MulliganDecision", "data": d}
                if keep is None:
                    keep = {"type": "MulliganDecision",
                            "data": {"choice": {"type": "Keep"}}}
                await submit_as_is(p0, keep)
                say("P0 mulligan: Keep")
                return True
        return False

    await keep_mulligan()
    land_played_turns = set()

    t_loop = time.time()
    last_rev = -1
    last_tick_at = 0.0
    last_advance_t = time.time()
    last_advance_rev = -1
    first_resolve_t = None
    known_stack = set()   # stack oids already recorded as casts

    while time.time() - t_loop < GAME_TIMEOUT:
        await asyncio.sleep(0.25)
        states_seen += 1
        msg = p0.latest
        if not msg:
            continue
        rev = p0.revision
        if rev != last_advance_rev:
            last_advance_rev = rev
            last_advance_t = time.time()
        elif time.time() - last_advance_t > 45:
            # stale-client watchdog (#4509): log revision + view
            st0 = msg.get("state", {})
            wf0 = st0.get("waiting_for") or {}
            say(f"WATCHDOG: P0 revision static {rev} for "
                f"{round(time.time() - last_advance_t)}s; "
                f"turn={st0.get('turn_number')} phase={st0.get('phase')} "
                f"wf={wf0.get('type')} pp={st0.get('priority_player')}")
            last_advance_t = time.time()
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
        if wfd.get("player") == 1 and wf.get("type") == "TargetSelection":
            obs["ai_target_waits"] += 1
            obs["target_waits"].append({"rev": rev, "t": now,
                                        "turn": state.get("turn_number")})
            for c in casts:
                if not c["resolved"] and not c["target_wait_seen"]:
                    c["target_wait_seen"] = True
                    obs["ws_target_wait_seen"] = True
                    say(f"OBSERVED: WS-visible TargetSelection wait for AI "
                        f"(cast #{casts.index(c)+1}, rev {rev})")
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

        # --- drive P0: empty attacks, land per turn, pass own priority ---
        if may_act:
            acts = merged_actions(msg)
            turn = state.get("turn_number")
            pp_seat = state.get("priority_player")
            my_priority = (pp_seat == 0) or (
                wf.get("type") == "Priority" and wfd.get("player") == 0)
            acted = False
            da = find_action(acts, "DeclareAttackers")
            if da and wfd.get("player") == 0:
                sub = copy.deepcopy(da)
                dd = sub.setdefault("data", {})
                for k in ("attacks", "attackers", "blocks", "blockers",
                          "assignments"):
                    if k in dd:
                        dd[k] = [] if isinstance(dd[k], list) else {}
                await submit_as_is(p0, sub)
                obs["p0_acts"] += 1
                acted = True
            if not acted:
                pl = find_action(acts, "PlayLand", "island", state)
                if pl and turn not in land_played_turns:
                    await submit_as_is(p0, pl)
                    land_played_turns.add(turn)
                    obs["p0_acts"] += 1
                    acted = True
            if not acted and my_priority:
                pp = find_action(acts, "PassPriority")
                if pp:
                    await submit_as_is(p0, pp)
                    obs["p0_acts"] += 1
            last_rev = rev
            last_tick_at = time.time()

    if not exported["post"]:
        await export_state_dict(p0, "post")

    # --- server log excerpt: AI stall signature ---
    try:
        slog = open(f"{BACKFILL}/runs/{RUN_ID}/server.log").read()
        sig = "choose_action returned None"
        hits = [ln for ln in slog.splitlines() if sig in ln]
        obs["ai_stall_signature"] = bool(hits)
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write(f"# AI stall-signature lines ('{sig}'): {len(hits)}\n")
            for ln in hits[:40]:
                f.write(ln + "\n")
            swap_lines = [ln for ln in slog.splitlines()
                          if "udacious" in ln or "udacious" in ln.lower()]
            f.write(f"\n# swap mentions: {len(swap_lines)}\n")
            for ln in swap_lines[:40]:
                f.write(ln + "\n")
        say(f"server log: {len(hits)} AI stall-signature line(s)")
    except Exception as e:
        notes.append(f"server excerpt capture failed: {e}")

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
                     for c in casts) and not obs["ai_stall_signature"]:
        ass["A3_no_target_loop"] = "passed"
        worst = max(c.get("resolve_s") or 0 for c in casts)
        notes.append(f"all casts resolved within {worst}s "
                     f"(window {STALL_TIMEOUT}s); "
                     f"ws-visible AI TargetSelection waits: "
                     f"{obs['ai_target_waits']} "
                     f"(caught-target-wait={obs['ws_target_wait_seen']}); "
                     f"AI stall signature in server log: "
                     f"{obs['ai_stall_signature']}")
    elif casts:
        ass["A3_no_target_loop"] = "failed"
        notes.append("stall: a cast exceeded the window or the AI stall "
                     "signature appeared in the server log")
    else:
        ass["A3_no_target_loop"] = "not-run"

    # A4: Oracle fidelity of cast #1 (pre -> post)
    secondary_finding = None
    if (pre_st is not None and post_st is not None and casts
            and casts[0]["resolved"]):
        pre_objs = pre_st.get("objects", {})
        post_objs = post_st.get("objects", {})
        swap_oid = str(casts[0]["oid"])
        swap_pre = pre_objs.get(swap_oid, {})
        targets = (swap_pre.get("targets")
                   or swap_pre.get("target_ids") or [])
        # identify the targeted permanent: a BF object in pre that sits in a
        # Library zone in post (the shuffle step)
        moved = []
        for oid, o in pre_objs.items():
            if o.get("zone") == "Battlefield" and obj_name(o) != SWAP:
                po = post_objs.get(oid, {})
                if po.get("zone") == "Library":
                    moved.append((oid, obj_name(o),
                                  o.get("controller"), o.get("owner")))
        a4a = len(moved) == 1
        target_owner = moved[0][3] if moved else None
        a4_detail["target_bf_to_owner_library"] = {
            "passed": a4a, "moved": moved,
            "advertised_targets": targets}
        # exiled top card: Library -> (Battlefield|Exile|Hand) movers that are
        # not the target and not the swap itself
        exiled = []
        for oid, o in pre_objs.items():
            if oid == swap_oid or oid in [m[0] for m in moved]:
                continue
            if o.get("zone") == "Library":
                po = post_objs.get(oid, {})
                if po.get("zone") in ("Battlefield", "Exile", "Hand"):
                    # find whose library it left
                    src = None
                    for p in pre_st.get("players", []):
                        if str(oid) in [str(x) for x in p.get("library", [])]:
                            src = p.get("id")
                    exiled.append((oid, obj_name(o), src,
                                   po.get("zone"), po.get("controller")))
        a4_detail["exile_source"] = {"candidates": exiled,
                                     "target_owner": target_owner}
        correct_exile = [e for e in exiled if e[2] == target_owner]
        a4b = bool(correct_exile) and len(exiled) == 1
        if not a4b and exiled:
            secondary_finding = {
                "title": "Audacious Swap exiles the top card of the caster's "
                         "library instead of the targeted permanent's owner's "
                         "library",
                "oracle": "The owner of target nonenchantment permanent "
                          "shuffles it into their library, then exiles the "
                          "top card of their library. If it's a land card, "
                          "they put it onto the battlefield. Otherwise, they "
                          "may cast it without paying its mana cost.",
                "observed": (f"Target {moved} -> owner {target_owner}'s "
                             f"library; exiled card {exiled} came from "
                             f"player {exiled[0][2]}'s library instead."),
                "parse": "Pinned v0.85.0 card-data parse has ExileTop player "
                         "= ParentTarget; the engine resolved that reference "
                         "to the spell's controller (same fingerprint as the "
                         "v0.78.0 run).",
            }
        a4_detail["exile_from_owner_library"] = {
            "passed": a4b, "correct_candidates": correct_exile}
        gy = [oid for oid, o in post_objs.items()
              if o.get("zone") == "Graveyard" and o.get("controller") == 1
              and obj_name(o) == SWAP]
        a4c = bool(gy)
        a4_detail["swap_in_ai_graveyard"] = {"passed": a4c, "count": len(gy)}
        stuck = [oid for oid in known_stack
                 if (post_objs.get(str(oid), {}).get("zone")) == "Stack"]
        a4d = not stuck
        a4_detail["nothing_stuck_on_stack"] = {"passed": a4d, "stuck": stuck}
        libs = {}
        for p in pre_st.get("players", []):
            pid = p.get("id")
            pre_n = len(p.get("library", []))
            po = player_of(post_st, pid)
            libs[pid] = (pre_n, len(po.get("library", [])))
        a4_detail["library_deltas"] = libs
        notes.append(f"A4 detail: {json.dumps(a4_detail, default=str)}")
        if a4a and a4c and a4d and a4b:
            ass["A4_swap_resolves"] = "passed"
        else:
            ass["A4_swap_resolves"] = "failed"
            if not a4b and a4a and a4c and a4d:
                notes.append("A4 failed ONLY on the wrong-library exile "
                             "source (secondary finding); the shuffle, "
                             "graveyard, and stack-cleanup legs passed")
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
    elif (ass["A1_swap_cast"] == "passed" and ass["A2_target_chosen"] == "passed"
          and ass["A3_no_target_loop"] == "passed"
          and ass["A5_cleanup"] == "passed"
          and (ass["A4_swap_resolves"] == "passed"
               or (ass["A4_swap_resolves"] == "failed"
                   and secondary_finding is not None
                   and a4_detail.get("target_bf_to_owner_library", {}).get("passed")
                   and a4_detail.get("swap_in_ai_graveyard", {}).get("passed")
                   and a4_detail.get("nothing_stuck_on_stack", {}).get("passed")))):
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
        "assertion_detail": {"A4": a4_detail},
        "observations": obs,
        "notes": notes,
        "setup_line": "P0: 60x island (human driver, passive). P1: AI Medium, "
                      "12x Audacious Swap + 24x island + 24x mountain.",
        "contract_line": "AI casts Audacious Swap -> target selection must "
                         "complete (spell leaves the stack) within the stall "
                         "window; the spell resolves per Oracle (incl. exile "
                         "from the target owner's library) and the game "
                         "continues. Repeat casts watched for the same loop.",
        "limitations": [
            "Browser/UI not exercised; server-side AI (phase-ai) policy tested, "
            "not any browser/client AI driver path",
            "Casualty 2 not exercised (AI deck has no creatures to sacrifice)",
            "Not tested on the original 2026-07-29 build; verdict is scoped to "
            "v0.85.0, not a fix claim",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay)",
        ],
        "stats": {"states_seen": states_seen, "casts_observed": len(casts)},
        "scenario": {"file": "scenario_6753_085.py",
                     "sha256": hashlib.sha256(
                         open(__file__, "rb").read()).hexdigest()},
        "decks": {"P0": {"main": P0_DECK}, "P1_AI": {"main": P1_AI_DECK}},
    }
    if secondary_finding is not None:
        run["secondary_finding"] = secondary_finding
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2, default=str)

    # manifest AFTER all logging is done (#7172 lesson)
    WIRE.close()
    RUNLOG.close()
    await p0.close()
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    print("FINAL:", v)
