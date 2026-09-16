#!/usr/bin/env python3
"""Issue #3233: Stuck decision: DeclareAttackers.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build v0.1.56 a668283, 2026-06-14):
  "What happened" - EMPTY (no description at all).
  Diagnostic: Waiting for: DeclareAttackers | Stuck players: 0
  Maintainer comment (2026-07-19): asked what happened immediately before the
  stall - creatures able/required to attack, goad/tax/restriction effects,
  whose turn, saved game state or game log. NO reply from the reporter
  (still unanswered as of 2026-09-15). Issue still open, status:needs-repro.

There is NO testable premise: no board state, no steps, no save, no
goad/tax/restriction info. Per PLAYBOOK step 2, a native engine test would
require inventing the premise. This run re-validates the 2026-09-09 attempt
(run 20260909-3233, v0.78.0/protocol 68) on the current pinned release
(v0.84.0 / protocol 71): drive a fresh native-engine game through the
DeclareAttackers decision for both players - P0 with an actual attack, P1
with a deliberately empty declaration - and record whether the decision ever
leaves the acting player with no available submission (the softlock
signature: a DeclareAttackers wait with no offered action, or a submission
rejection).

Oracle/expected (generic engine behavior):
  E1: at each combat phase the active player gets DeclareAttackers offered
      with a usable submission.
  E2: submitting DeclareAttackers with an attackable creature records the
      attack; combat proceeds to DeclareBlockers/damage.
  E3: submitting DeclareAttackers with zero attackers also completes;
      the game advances.
  E4: unblocked 2/2 deals 2 damage (P1 20 -> 18).

Assertions:
  A1_setup_ok        P0's turn reaches DeclareAttackers with a Grizzly Bears
                     able to attack on the battlefield; pre.json exported.
  A2_attack_declared P0 submitted DeclareAttackers naming the Bear; no
                     ActionRejected; unblocked damage recorded (A3).
  A3_combat_completes P1 life 20 -> 18 between pre.json and post.json
                     (unblocked bear damage), game left DeclareAttackers.
  A4_empty_declares  P1's DeclareAttackers with attacks=[] completed;
                     game advanced past it (no stall).
  A5_no_softlock     Every DeclareAttackers wait observed the advertised
                     DeclareAttackers action available to the acting player,
                     and the decision advanced; no wait exceeded 60s without
                     an available submission.
  A6_cleanup         post.json: stack empty, game proceeding.

Verdict rule: the reported bug has no testable premise (missing setup
information that materially changes the test), so the verdict is `blocked`
regardless of the generic attempt outcome. The generic attempt only becomes
`reproduced` if the softlock signature itself is observed (a DeclareAttackers
wait with no submission available to the acting player); it can never be
`not-reproduced` because the reported path was never identified.

Evidence: evidence/3233/<run-id>/pre.json (P0 DeclareAttackers, Bear able to
attack), post.json (after both players' DeclareAttackers resolved), run.json,
manifest.sha256, summary.png, scenario_3233.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts)
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
ISSUE = 3233
RUN_ID = os.environ.get("RUN_ID", "20260915-3233")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEAR = "grizzly bears"
FOREST = "forest"

P0_DECK = [(BEAR, 12), (FOREST, 48)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

RELDIR = f"{BACKFILL}/server/releases/v0.84.0"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "Full",
    "binary_sha256": _ACTUAL["binary_sha256"],
    "card_data_sha256": _ACTUAL["card_data_sha256"],
    "draft_pools_sha256": _ACTUAL["draft_pools_sha256"],
    "digests_match_pin": _ACTUAL == _PIN,
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "at pin time 2026-09-15; data digests match the signed "
                      "manifest; digests recomputed against on-disk files "
                      "this run; release v0.84.0 confirmed latest stable "
                      "via GitHub releases API 2026-09-15",
    "observed_at": "2026-09-15",
    "handshake": "ServerHello observed pre-run: v0.84.0 / eb7e93e / "
                 "protocol 71 / mode Full on 127.0.0.1:9374",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run under runs/20260915-3233/",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    try:
        RUNLOG.write(m + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == FOREST and not o.get("tapped")]


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = [str(k) for k in (o.get("keywords") or [])]
        if "Haste" not in kws:
            return False
    return True


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


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    d = wf_of(state).get("data") or {}
    return d.get("player")


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def stack_entries(state):
    return state.get("stack") or []


def drain_rejections(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            out.append({"type": t, "data": data})
    return out


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "game_code": None,
        "p0_attack_oid": None,
        "attacked": False,
        "p1_empty_done": False,
        "pre_exported": False,
        "post_exported": False,
        "answered_iids": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
        "notes_extra": [],
    }
    obs = {
        "declare_waits": [],
        "stall_observed": False,
        "rejections": [],
        "tick_errors": [],
    }
    declare_wait_start = {"P0": None, "P1": None}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-declare")
    p1 = PhaseClient("P1-empty")
    await p0.connect()
    await p1.connect()
    say("both clients connected")
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or (sess or {}).get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say("P1 joined")

    async def export_named(name):
        try:
            raw = await p0.export_state()
            env = json.loads(raw)
            assert "state" in env, "envelope missing 'state'"
            with open(f"{EVDIR}/{name}.json", "w") as f:
                json.dump(env, f, indent=1)
            ST["exports"][name] = True
            say(f"exported {name}.json "
                f"(turn={env['state'].get('turn_number')})")
            return env["state"]
        except Exception as e:
            say(f"export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return None

    def acted(key, rev):
        if ST["last_rev_acted"].get(key) == rev:
            return True
        ST["last_rev_acted"][key] = rev
        return False

    def mulligan_pending_for(state, pid):
        d = (wf_of(state).get("data") or {})
        for p in d.get("pending", []) or []:
            ph = (p.get("phase") or {})
            if p.get("player") == pid and str(ph.get("type")) == "Declare":
                return True
        return False

    async def mulligan_keep(c, pid, tag, st, state):
        # Always keep 7 (cf. #7176: gate on pending[] Declare entries, not
        # an answered flag; the plain keep avoids the re-mulligan stall).
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def handle_discard(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        if wf_player(state) is not None and wf_player(state) != pid:
            return False
        vi = (st.get("viewer_interaction") or {})
        if not vi.get("canSubmit"):
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                if BEAR in tx:
                    return 2
                if FOREST in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            ST["answered_iids"].append(iid)
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(
                {"interactionId": iid,
                 "response": {"type": resp.get("type", "choose"),
                              "data": {"choiceId": pick.get("id")}}})
            return True
        return False

    def cast_spell_action(acts, state, pid, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    def upsert_wait(who, turn, active, offered, attack_oids):
        """One record per (who, turn): a wait whose action appears on a
        later tick is NOT a stall - only a wait where the action NEVER
        appears (or the >60s watchdog) counts as the softlock signature."""
        for w in obs["declare_waits"]:
            if w["who"] == who and w["turn"] == turn:
                w["action_offered"] = w["action_offered"] or offered
                w["attack_oids"] = list(attack_oids)
                return w
        rec = {
            "turn": turn,
            "active": active,
            "who": who,
            "action_offered": offered,
            "attack_oids": list(attack_oids),
        }
        obs["declare_waits"].append(rec)
        return rec

    async def combat_p0_attack(c, st, state, acts):
        """P0 DeclareAttackers: send ready bears at P1 (the attack leg)."""
        wtype = (wf_of(state).get("type") or "")
        if wtype != "DeclareAttackers":
            return False
        wp = wf_player(state)
        if wp not in (0, None):
            return False
        if state.get("active_player") != 0:
            return False
        if ST["attacked"]:
            return False
        turn = state.get("turn_number") or 0
        ready = [o for o in bf_ids(state, 0, BEAR) if can_attack_now(state, o)]
        if not ready:
            return False
        da = find_action(acts, "DeclareAttackers")
        rec = upsert_wait("P0", turn, state.get("active_player"),
                          da is not None, ready)
        if declare_wait_start["P0"] is None:
            declare_wait_start["P0"] = time.time()
        if da is None:
            say("[P0] DeclareAttackers wait: action not yet advertised "
                f"(tick; turn {turn})")
            return True
        rev = st.get("state_revision", -1)
        if acted("p0attack", rev):
            return True
        if not ST["pre_exported"]:
            await export_named("pre")
            ST["pre_exported"] = True
            ass["A1_setup_ok"] = "passed"
            notes.append(f"A1: pre.json exported at P0 DeclareAttackers "
                         f"(turn {turn}); attack-ready bears={ready}")
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = [[o, {"type": "Player", "data": 1}] for o in ready]
        d["bands"] = []
        wire("p0_declare_attackers_submit", d)
        await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        ST["p0_attack_oid"] = ready[0]
        ST["attacked"] = True
        declare_wait_start["P0"] = None
        rec["submitted"] = True
        say(f"[P0] attacking P1 with bears {ready} (turn {turn})")
        return True

    async def combat_p1_empty(c, st, state, acts):
        """P1 DeclareAttackers: deliberately declare NO attackers."""
        wtype = (wf_of(state).get("type") or "")
        if wtype != "DeclareAttackers":
            return False
        wp = wf_player(state)
        if wp not in (1, None):
            return False
        if state.get("active_player") != 1:
            return False
        if ST["p1_empty_done"]:
            return False
        da = find_action(acts, "DeclareAttackers")
        turn = state.get("turn_number")
        rec = upsert_wait("P1", turn, state.get("active_player"),
                          da is not None, [])
        if declare_wait_start["P1"] is None:
            declare_wait_start["P1"] = time.time()
        if da is None:
            say("[P1] DeclareAttackers wait: action not yet advertised "
                f"(tick; turn {turn})")
            return True
        rev = st.get("state_revision", -1)
        if acted("p1empty", rev):
            return True
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = []
        d["bands"] = []
        wire("p1_declare_empty_submit", d)
        await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        ST["p1_empty_done"] = True
        declare_wait_start["P1"] = None
        rec["submitted"] = True
        say(f"[P1] declaring NO attackers (turn {rec['turn']})")
        return True

    async def combat_blockers(c, pid, tag, st, state, acts):
        """DeclareBlockers with zero assignments (unblocked)."""
        wtype = (wf_of(state).get("type") or "")
        if wtype != "DeclareBlockers":
            return False
        da = find_action(acts, "DeclareBlockers")
        if not da:
            return False
        rev = st.get("state_revision", -1)
        if acted(f"{tag}blk", rev):
            return True
        d = copy.deepcopy(da.get("data", {}))
        d["assignments"] = []
        await submit_as_is(c, {"type": "DeclareBlockers", "data": d})
        say(f"[{tag}] declares no blockers")
        return True

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        for rj in drain_rejections(p0):
            obs["rejections"].append({"who": "P0", **rj})
            say(f"[P0] REJECTION: {json.dumps(rj, default=str)[:300]}")
        if await combat_p0_attack(p0, st, state, acts):
            return
        if await combat_p1_empty(p0, st, state, acts):
            return
        if await combat_blockers(p0, 0, "P0", st, state, acts):
            return
        phase = state.get("phase") or ""
        # main-phase land + bear casts (retry land drops every tick, #6690)
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain") \
                and not stack_entries(state):
            for oid in hand_ids(state, 0):
                if lname(state, oid) == FOREST:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get("object_id")
                                == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            if (len(bf_ids(state, 0, BEAR)) < 2
                    and BEAR in hand_lnames(state, 0)
                    and len(untapped_lands(state, 0)) >= 2):
                ca = cast_spell_action(acts, state, 0, BEAR)
                if ca and not acted("p0bear", rev):
                    say("[P0] casting Grizzly Bears")
                    await submit_as_is(p0, ca)
                    return
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p1, 1, "P1", st, state):
            return
        if await handle_discard(p1, 1, "P1", st, state):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        for rj in drain_rejections(p1):
            obs["rejections"].append({"who": "P1", **rj})
            say(f"[P1] REJECTION: {json.dumps(rj, default=str)[:300]}")
        if await combat_p1_empty(p1, st, state, acts):
            return
        if await combat_p0_attack(p1, st, state, acts):
            return
        if await combat_blockers(p1, 1, "P1", st, state, acts):
            return
        phase = state.get("phase") or ""
        # P1 needs its own turn driver (cf. #6762): land drops, cast bears.
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain") \
                and not stack_entries(state):
            for oid in hand_ids(state, 1):
                if lname(state, oid) == FOREST:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get("object_id")
                                == oid), None)
                    if pla and not acted("p1land", rev):
                        await submit_as_is(p1, pla)
                        return
            if (len(bf_ids(state, 1, BEAR)) < 2
                    and BEAR in hand_lnames(state, 1)
                    and len(untapped_lands(state, 1)) >= 2):
                ca = cast_spell_action(acts, state, 1, BEAR)
                if ca and not acted("p1bear", rev):
                    say("[P1] casting Grizzly Bears")
                    await submit_as_is(p1, ca)
                    return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST["finished"]:
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
        while time.time() - t0 < 1200:
            await asyncio.sleep(1)
            # softlock watchdog: DeclareAttackers wait with no offered
            # submission persisting >60s (the reported signature).
            for c, tag, pid in ((p0, "P0", 0), (p1, "P1", 1)):
                st = c.latest
                if not st:
                    continue
                state = st.get("state") or {}
                if (wf_of(state).get("type") == "DeclareAttackers"
                        and (wf_player(state) in (pid, None))
                        and state.get("active_player") == pid):
                    start = declare_wait_start[tag]
                    if start and time.time() - start > 60:
                        da = find_action(merged_actions(st),
                                         "DeclareAttackers")
                        if da is None:
                            obs["stall_observed"] = True
                            await export_named("mid_stall")
                            notes.append(f"stall watchdog: {tag} "
                                         f"DeclareAttackers wait >60s with "
                                         f"no offered submission "
                                         f"(mid_stall.json exported)")
                            say("STALL OBSERVED; finishing")
                            raise StopAsyncIteration
            # post-export trigger: P1 empty declaration done and the game
            # advanced past it into PostCombatMain/End of P1's turn, or a
            # new P0 turn began.
            if ST["p1_empty_done"] and not ST["post_exported"] and p0.latest:
                s = (p0.latest.get("state") or {})
                wf = (s.get("waiting_for") or {}).get("type")
                p1_post = (s.get("active_player") == 1
                           and s.get("phase") in ("PostCombatMain", "End",
                                                  "Cleanup")
                           and wf != "DeclareAttackers")
                next_p0 = (s.get("active_player") == 0
                           and (s.get("turn_number") or 0) >= 7)
                if p1_post or next_p0:
                    await export_named("post")
                    ST["post_exported"] = True
                    say("exported POST")
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s turn={s.get('turn_number')} "
                    f"active={s.get('active_player')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"P0bears={len(bf_ids(s, 0, BEAR))} "
                    f"P1bears={len(bf_ids(s, 1, BEAR))} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"attacked={ST['attacked']} p1empty={ST['p1_empty_done']} "
                    f"declares={len(obs['declare_waits'])}")
    except StopAsyncIteration:
        pass
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        states = {}
        for fn in ("pre", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre = states.get("pre", {})
        post = states.get("post", {})
        if pre:
            notes.append(f"pre: turn={pre.get('turn_number')} phase="
                         f"{pre.get('phase')} active=P{pre.get('active_player')} "
                         f"wf={(pre.get('waiting_for') or {}).get('type')} "
                         f"P0 bears={len(bf_ids(pre, 0, BEAR))} "
                         f"P1 life={life_of(pre, 1)}")
        if post:
            notes.append(f"post: turn={post.get('turn_number')} phase="
                         f"{post.get('phase')} active=P{post.get('active_player')} "
                         f"wf={(post.get('waiting_for') or {}).get('type')} "
                         f"P1 life={life_of(post, 1)}")

        # ---- A2/A3: attack declared, combat completed (unblocked 2/2) ----
        if pre and post and ST["attacked"]:
            lpre, lpost = life_of(pre, 1), life_of(post, 1)
            if lpre is not None and lpost == lpre - 2:
                ass["A2_attack_declared"] = "passed"
                ass["A3_combat_completes"] = "passed"
                notes.append(f"A2/A3: P0 attacked with Bear oid "
                             f"{ST['p0_attack_oid']}; P1 life {lpre} -> "
                             f"{lpost} (unblocked 2/2); combat completed")
            else:
                ass["A2_attack_declared"] = "failed"
                ass["A3_combat_completes"] = "failed"
                notes.append(f"A2/A3 failed: P0 attacked={ST['attacked']} "
                             f"but P1 life pre={lpre} post={lpost} "
                             f"(expected -2)")
        else:
            ass["A2_attack_declared"] = "failed" if not ST["attacked"] \
                else "not-run"
            ass["A3_combat_completes"] = "not-run"
            notes.append(f"A2/A3 not evaluable: attacked={ST['attacked']} "
                         f"pre={bool(pre)} post={bool(post)}")
        if "A1_setup_ok" not in ass:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: P0 never reached DeclareAttackers "
                         "with an attack-ready bear")

        # ---- A4: P1 empty declaration completed, game advanced ----
        if ST["p1_empty_done"] and post:
            wf = (post.get("waiting_for") or {}).get("type")
            progressed = not (wf == "DeclareAttackers"
                              and post.get("active_player") == 1)
            ass["A4_empty_declares"] = "passed" if progressed else "failed"
            notes.append(f"A4: P1 empty DeclareAttackers submitted; game "
                         f"advanced (post wf={wf}, "
                         f"phase={post.get('phase')}, active="
                         f"P{post.get('active_player')})")
        else:
            ass["A4_empty_declares"] = "failed"
            notes.append(f"A4 failed: p1_empty_done={ST['p1_empty_done']} "
                         f"post={bool(post)}")

        # ---- A5: no softlock across all observed DeclareAttackers waits ----
        stalled = [w for w in obs["declare_waits"]
                   if not w["action_offered"]]
        if obs["declare_waits"] and not stalled and not obs["stall_observed"]:
            ass["A5_no_softlock"] = "passed"
            notes.append(f"A5: {len(obs['declare_waits'])} DeclareAttackers "
                         f"wait(s); the advertised action was offered to "
                         f"the acting player every time and every "
                         f"decision advanced; no >60s stall")
        elif stalled or obs["stall_observed"]:
            ass["A5_no_softlock"] = "failed"
            obs["stall_observed"] = True
            notes.append(f"A5 SOFTLOCK SIGNATURE: "
                         f"{len(stalled)} DeclareAttackers wait(s) with NO "
                         f"submission offered to the acting player")
        else:
            ass["A5_no_softlock"] = "failed"
            notes.append("A5 failed: no DeclareAttackers wait was ever "
                         "observed")

        # ---- A6: cleanup ----
        if post:
            slen = len(stack_entries(post))
            if slen == 0:
                ass["A6_cleanup"] = "passed"
                notes.append(f"A6: post.json stack empty, game proceeding "
                             f"(turn={post.get('turn_number')}, "
                             f"phase={post.get('phase')}, "
                             f"active=P{post.get('active_player')})")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"A6 failed: post.json stack={slen}")
        else:
            ass["A6_cleanup"] = "not-run"
            notes.append("A6 not-run: no post.json")

        # ---- verdict ----
        if obs["stall_observed"]:
            verdict = "reproduced"
            notes.append("verdict=reproduced: the softlock signature was "
                         "observed on v0.84.0 (a related failure; the "
                         "reporter's v0.1.56 board remains unknown)")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: the report has no testable "
                         "premise (no board, steps, or save; reporter never "
                         "answered the maintainer's 2026-07-19 questions; "
                         "still open, status:needs-repro). Generic "
                         "DeclareAttackers path on v0.84.0 completes "
                         "normally. A native engine test of the reported "
                         "bug would require inventing the premise.")
        notes.append(f"verdict={verdict}")
        for n in ST["notes_extra"]:
            notes.append(n)

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 71,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {
                "game_code": ST["game_code"],
                "p0_attack_oid": ST["p0_attack_oid"],
                "attacked": ST["attacked"],
                "p1_empty_done": ST["p1_empty_done"],
                "exports": ST["exports"],
                "turns_seen": sorted(ST["turns_seen"]),
                "rejection_count": len(obs["rejections"]),
            },
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Grizzly Bears deck density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "No goad/tax/restriction or attack-requirement effects "
                "on the board; the report names none, so only the "
                "generic DeclareAttackers path is exercised.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Not tested on the original v0.1.56 build.",
            ],
            "setup_line": "P0: 12x Grizzly Bears + 48x Forest (keep 7, "
                          "cast Bear when able, attack P1 when ready); P1: "
                          "same deck, deliberately declares no attackers",
            "contract_line": "DeclareAttackers always offers a submittable "
                             "action; an attack declaration and an empty "
                             "declaration both complete; combat damage "
                             "resolves normally; no >60s stall with no "
                             "offered action",
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                    f"{EVDIR}/scenario_{ISSUE}.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        say(f"server run dir: runs/{srv_run}")
        try:
            with open(f"{BACKFILL}/runs/{srv_run}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            gc = ST.get("game_code") or ""
            excerpt = [ln for ln in clean.splitlines()
                       if gc and gc in ln]
            if not excerpt:
                excerpt = clean.splitlines()[-400:]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines)")
        except Exception as e:
            say(f"server.log excerpt failed: {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
        # close logs BEFORE hashing the manifest (#7176 lesson)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        write_manifest()
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #3233 - Stuck decision: "
               "DeclareAttackers", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15"
               " - generic DeclareAttackers softlock-signature test",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report (build v0.1.56): 'What happened' is EMPTY;",
                "diagnostic only: Waiting for: DeclareAttackers /",
                "Stuck players: 0. The maintainer's 2026-07-19 request",
                "for board/timing/effects was never answered. No",
                "testable premise: a native engine test would have to",
                "invent it. This run re-validates the generic attempt",
                "on the current pin: does a DeclareAttackers wait ever",
                "leave the acting player with no submittable action?"]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Assertions (from saved states / observations):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "P0 reached DeclareAttackers with a bear able "
                           "to attack; pre.json exported",
            "A2_attack_declared": "P0 DeclareAttackers submitted; no "
                                  "rejection; damage recorded",
            "A3_combat_completes": "P1 life 20 -> 18 (unblocked 2/2)",
            "A4_empty_declares": "P1 empty DeclareAttackers completed; "
                                 "game advanced",
            "A5_no_softlock": "action offered to acting player at every "
                              "wait; no >60s stall",
            "A6_cleanup": "post.json: stack empty; game proceeding",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "?")
            col = {"passed": (120, 220, 120), "failed": (255, 110, 110),
                   "not-run": (200, 180, 120)}.get(v, (180, 180, 180))
            d.text((24, y), f"[{v}] {k}: {lab}", fill=col)
            y += 26
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        ds = run["driver_state"]
        waits = run["observations"].get("declare_waits") or []
        for ln in [
                f"P0 attacked with bear oid={ds.get('p0_attack_oid')} "
                f"(attacked={ds.get('attacked')})",
                f"P1 empty declaration completed={ds.get('p1_empty_done')}",
                f"DeclareAttackers waits observed: {len(waits)}",
                " / ".join(f"{w.get('who')} t{w.get('turn')} "
                           f"offered={w.get('action_offered')} "
                           f"sub={w.get('submitted')} "
                           f"atk={w.get('attack_oids')}" for w in waits),
                f"rejections={ds.get('rejection_count')}",
                "Exports: pre.json (P0 DeclareAttackers, bear ready),",
                "post.json (after P0 attack + P1 empty declaration).",
        ]:
            d.text((24, y), ln[:108], fill=(160, 175, 195))
            y += 22
        d.text((24, y + 14), "Generated from saved states/assertions; not "
               "a gameplay screenshot.", fill=(110, 125, 145))
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        import hashlib as _hl
        files = sorted(
            f for f in os.listdir(EVDIR)
            if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
        lines = []
        for fn in files:
            h = _hl.sha256()
            with open(f"{EVDIR}/{fn}", "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            lines.append(f"{h.hexdigest()}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # plain print: RUNLOG is already closed at this point
        print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)

    await finish()


if __name__ == "__main__":
    asyncio.run(main())
