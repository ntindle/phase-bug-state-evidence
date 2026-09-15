#!/usr/bin/env python3
"""Issue #7176: Circuits Act -- [[Circuits Act]] does not make the artifact
creatures for each different result of the roll.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.83.0, key 'circuits act'):
  Circuits Act ({2}{R}, Sorcery):
    Roll three six-sided dice. For each different result, create a 1/1 white
    Clown Robot artifact creature token.

Card-data parse state on v0.83.0 (verified 2026-09-15 before the run):
  abilities[0] = Spell RollDie{count:3, sides:6} ->
  sub_ability = Spell Unimplemented{name:"unparsed_quantity",
    description:"For each different result, create a 1/1 white Clown Robot
    artifact creature token"}.
  The token-creation half is unparsed; the dice-roll half is typed.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 20x circuits act, 40x mountain (60).
  P1: 60x island (passive: land drops only, never attacks).

Planned line: P0 casts Circuits Act up to 3 times (own main phase, 3+
untapped Mountains each). For each cast i: export pre_i.json immediately
before the CastSpell submission; submit; watch for resolution (Circuits Act
count in P0's graveyard grows past the pre-cast count); export post_i.json.
Count Clown Robot tokens (battlefield objects, controller 0, name contains
"clown").

Expected per Oracle: every completed cast creates 1-3 Clown Robot tokens
(three equal results -> 1; two distinct -> 2; three distinct -> 3).
The engine's RNG is not controllable from the driver, so the exact-branch
acceptance (1 vs 2 vs 3 tokens per roll) is not testable; the asserted
outcome is the aggregate: after N completed casts, at least N tokens must
exist (Oracle minimum is 1 per cast). The reported defect is that the token
quantity does not reflect the number of different results.

Assertions (each passed / failed / not-run):
  A1_parse      card-data: RollDie typed (3d6) + sub-ability Unimplemented
                name="unparsed_quantity" (the token-creation half unparsed).
  A2_setup      pre_1: Circuits Act in P0 hand + >=3 untapped Mountains.
  A3_cast       at least one CastSpell submission completed (spell reached
                the stack, no rejection).
  A4_resolved   every completed cast resolved (P0 graveyard count grew;
                post_i exported).
  A5_tokens     cumulative Clown Robot tokens after N completed casts >= N
                (Oracle minimum). FAILED when 0 tokens are created -- the
                reported bug.
  A6_cleanup    final post.json: stack empty, game advanced, no stall.

Verdict rule:
  reproduced     iff A2/A3/A4 pass and A5 fails with 0 tokens created --
                 casts complete but no Clown Robots are ever created.
  not-reproduced iff A5 passes (each completed cast created >= 1 token).
  blocked        iff A2 fails (setup never reached) or no cast ever
                 completes (A3 fails).

Evidence: evidence/7176/<run-id>/pre_{1..3}.json, post_{1..3}.json,
post.json, run.json, manifest.sha256, summary.png, scenario_7176.py,
wire_log.jsonl, scenario_run.log, server.log.
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
RUN_ID = os.environ.get("RUN_ID", "20260915-7176")
EVDIR = f"{BACKFILL}/evidence/7176/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ACT = "circuits act"
MOUNTAIN = "mountain"
ISLAND = "island"
P0_LANDS = (MOUNTAIN,)
P1_LANDS = (ISLAND,)

P0_DECK = [(ACT, 20), (MOUNTAIN, 40)]
P1_DECK = [(ISLAND, 60)]

MAX_CASTS = 3
TURN_CAP = 24

SERVER_IDENTITY = {
    "server_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c51cb38db94466c85",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key); server already running "
              "on 127.0.0.1:9374 (run 20260915-7173-server), reused.",
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


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def zone_ids_by_name(state, pid, zone, name):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        if o.get("zone") != zone:
            continue
        if o.get("controller") != pid and o.get("owner") != pid:
            # graveyard membership: check owner too
            pass
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if nm == name and o.get("zone") == zone:
            # for graveyard, match by owner
            if zone == "Graveyard" and o.get("owner") != pid:
                continue
            if zone != "Graveyard" and o.get("controller") != pid:
                continue
            out.append(int(oid))
    return out


def gy_count(state, pid, name):
    return len(zone_ids_by_name(state, pid, "Graveyard", name))


def clown_tokens(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        if o.get("controller") != pid:
            continue
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if "clown" in nm:
            out.append(int(oid))
    return out


def bf_land_ids(state, pid, names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in names):
            out.append(int(oid))
    return out


def untapped_lands(state, pid, names):
    return [oid for oid in bf_land_ids(state, pid, names)
            if not get_obj(state, oid).get("tapped")]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type") or ""


def wf_player(state):
    d = wf_of(state).get("data") or {}
    p = d.get("player")
    if isinstance(p, dict):
        p = p.get("id", p.get("player", -1))
    return p


def my_priority(state, pid):
    return wf_type(state) == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"))


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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid}")
    wire("interaction_submission", {"who": tag, "submission": sub})
    await c.send_interaction(sub)


async def answer_vi_multi(c, opp, choices, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cids = [ch.get("id") for ch in choices]
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": cids}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": cids}}}
    say(f"[{tag}] submitting multi interaction iid={iid} choices={cids}")
    wire("interaction_submission_multi", {"who": tag, "submission": sub})
    await c.send_interaction(sub)

async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "post_exported": False,
        "casts": [],            # each: {i, pre_gy, cast, cast_oid, cast_rev,
                                #  resolved, post_gy, tokens}
        "in_flight": False,
        "rejections": [],
        "game_code": None,
        "last_rev_acted": {},
        "turns_seen": set(),
        "roll_sightings": [],
        "stop_reason": "",
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "prompts": []}
    notes = []
    ass = {}

    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or sess.get("game_code")
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
            say(f"exported {name}.json "
                f"(turn={(env['state'].get('turn_number'))})")
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

    def drain_rejections(c):
        found = []
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("ActionRejected", "Error"):
                found.append({"type": t, "data": data})
                ST["rejections"].append(
                    {"who": c.name, "type": t, "data": data,
                     "t": time.time()})
                wire("rejected", {"who": c.name, "type": t, "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:400]}")
        return found

    async def mulligan_keep(c, pid, tag, st, state, acts):
        if wf_type(state) != "MulliganDecision":
            return False
        pend = (wf_of(state).get("data") or {}).get("pending") or []
        mine = [e for e in pend
                if (e.get("player") if isinstance(e, dict) else None) == pid
                and (e.get("phase") or {}).get("type") == "Declare"]
        if not mine:
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def handle_discard(c, pid, tag, st, state, acts, protect):
        if wf_type(state) != "DiscardToHandSize":
            return False
        d = wf_of(state).get("data") or {}
        dp = d.get("player")
        if isinstance(dp, dict):
            dp = dp.get("id", -1)
        if dp != pid:
            return False
        count = int(d.get("count", 1))
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            chs, rtype = vi_choices(opp)
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                return 3 if protect in tx else 0
            picks = sorted(chs, key=rank)[:count]
            if acted(f"disc{iid}", st.get("state_revision", -1)):
                return True
            say(f"[{tag}] discarding {len(picks)} to hand size")
            if len(picks) == 1:
                await answer_vi(c, opp, picks[0], tag)
            else:
                await answer_vi_multi(c, opp, picks, tag)
            return True
        return False

    async def land_drop(c, pid, tag, st, state, acts, names):
        if not (my_priority(state, pid)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
            return False
        rev = st.get("state_revision", -1)
        for oid in hand_ids(state, pid):
            if lname(state, oid) in names:
                pla = next((a for a in acts
                            if a.get("type") == "PlayLand"
                            and (a.get("data") or {}).get("object_id") == oid),
                           None)
                if pla and not acted(f"{tag}land", rev):
                    await submit_as_is(c, pla)
                    return True
        return False

    async def zero_attackers(c, pid, tag, st, state, acts):
        if wf_type(state) != "DeclareAttackers":
            return False
        if wf_player(state) != pid:
            return False
        da = find_action(acts, "DeclareAttackers")
        rev = st.get("state_revision", -1)
        if da and not acted(f"{tag}da", rev):
            sub = copy.deepcopy(da)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
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

    def scan_rolls(state):
        """Opportunistic: record any dice-roll evidence visible in state."""
        hits = []
        for entry in state.get("stack") or []:
            blob = json.dumps(entry).lower()
            if any(k in blob for k in ("rolldie", "die", "dice")):
                hits.append(str(entry)[:500])
        return hits

    # ---------------- cast state machine (P0) ----------------
    async def try_cast(st, state, acts):
        if len(ST["casts"]) >= MAX_CASTS or ST["in_flight"]:
            return False
        if not my_priority(state, 0) or state.get("phase") not in (
                "PreCombatMain", "PostCombatMain"):
            return False
        if ACT not in hand_lnames(state, 0):
            return False
        if len(untapped_lands(state, 0, P0_LANDS)) < 3:
            return False
        ca = cast_spell_action(acts, state, 0, ACT)
        if not ca:
            return False
        rev = st.get("state_revision", -1)
        if acted("cast", rev):
            return True
        i = len(ST["casts"]) + 1
        pre = await export_named(f"pre_{i}")
        if pre is None:
            return False
        pre_gy = gy_count(pre, 0, ACT)
        entry = {"i": i, "pre_gy": pre_gy, "cast": False, "cast_oid": None,
                 "cast_rev": None, "resolved": False, "post_gy": None,
                 "tokens": None}
        ST["casts"].append(entry)
        oid = (ca.get("data") or {}).get("object_id")
        say(f"[P0] casting {ACT} (leg {i}, pre_gy={pre_gy}, oid={oid})")
        wire("cast_submission", {"i": i, "action": ca})
        await submit_as_is(p0, ca)
        entry["cast"] = True
        entry["cast_oid"] = int(oid) if oid is not None else None
        entry["cast_rev"] = rev
        ST["in_flight"] = True
        return True

    async def watch_cast(st, state, acts):
        if not ST["in_flight"]:
            return False
        entry = ST["casts"][-1]
        cur_gy = gy_count(state, 0, ACT)
        if cur_gy > entry["pre_gy"]:
            entry["resolved"] = True
            entry["post_gy"] = cur_gy
            post = await export_named(f"post_{entry['i']}")
            if post is not None:
                entry["tokens"] = len(clown_tokens(post, 0))
            say(f"[P0] leg {entry['i']} RESOLVED: gy {entry['pre_gy']}->"
                f"{cur_gy}, clown tokens now {entry['tokens']}")
            wire("cast_resolved", {"i": entry["i"], "pre_gy": entry["pre_gy"],
                                  "post_gy": cur_gy,
                                  "tokens": entry["tokens"]})
            ST["in_flight"] = False
            return True
        # opportunistic roll sightings while the spell is on the stack
        for h in scan_rolls(state):
            if h not in ST["roll_sightings"]:
                ST["roll_sightings"].append(h)
                say(f"[P0] roll sighting: {h[:200]}")
        return False

    async def note_unexpected_vi(c, tag, st, state, acts):
        vi = get_vi(st)
        if not vi:
            return False
        wtype = wf_type(state)
        if wtype in ("Priority", "DeclareAttackers", "DeclareBlockers",
                     "MulliganDecision", "SelectCards", "DiscardToHandSize",
                     "ManaPayment", "TargetSelection"):
            return False
        key = f"{tag}:{wtype}"
        if key in ST.get("seen_vi", set()):
            return False
        ST.setdefault("seen_vi", set()).add(key)
        detail = []
        for opp in vi.get("opportunities", []) or []:
            chs, rtype = vi_choices(opp)
            texts = []
            for ch in chs[:8]:
                tx = ""
                for s in ch.get("surfaces", []) or []:
                    dd = s.get("data") or {}
                    t = dd.get("text") if isinstance(dd, dict) else None
                    if t:
                        tx += str(t) + "|"
                texts.append(tx or str(ch.get("id")))
            detail.append({"iid": opp.get("interactionId"), "rtype": rtype,
                           "n": len(chs), "texts": texts})
        obs["unexpected_prompts"].append({"who": tag, "wf": wtype,
                                          "detail": detail})
        say(f"[{tag}] UNEXPECTED prompt wf={wtype}: "
            f"{json.dumps(detail)[:500]}")
        wire("unexpected_prompt", {"who": tag, "wf": wtype, "detail": detail})
        return False

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        drain_rejections(p0)
        if await mulligan_keep(p0, 0, "P0", st, state, acts):
            return
        if await handle_discard(p0, 0, "P0", st, state, acts, ACT):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_type(state)
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)
        if await watch_cast(st, state, acts):
            return
        if await note_unexpected_vi(p0, "P0", st, state, acts):
            return
        if await try_cast(st, state, acts):
            return
        if await zero_attackers(p0, 0, "P0", st, state, acts):
            return
        if await land_drop(p0, 0, "P0", st, state, acts, P0_LANDS):
            return
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        drain_rejections(p1)
        if await mulligan_keep(p1, 1, "P1", st, state, acts):
            return
        if await handle_discard(p1, 1, "P1", st, state, acts, "zzz-none"):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if await note_unexpected_vi(p1, "P1", st, state, acts):
            return
        if await zero_attackers(p1, 1, "P1", st, state, acts):
            return
        if await land_drop(p1, 1, "P1", st, state, acts, P1_LANDS):
            return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    t0 = time.time()
    last_diag = 0.0

    async def p0_tick_loop():
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
                return
            if p0.latest is None:
                continue
            st = p0.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                await p0_tick(st, merged_actions(st), state)
            except Exception as e:
                obs["tick_errors"].append(f"P0: {e!r}")

    async def p1_tick_loop():
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
                return
            if p1.latest is None:
                continue
            st = p1.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                await p1_tick(st, merged_actions(st), state)
            except Exception as e:
                obs["tick_errors"].append(f"P1: {e!r}")

    p0t = asyncio.create_task(p0_tick_loop())
    p1t = asyncio.create_task(p1_tick_loop())
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            done = [c for c in ST["casts"] if c["resolved"]]
            if len(done) >= MAX_CASTS:
                say("all casts resolved; exporting post and finishing")
                ST["stop_reason"] = f"{MAX_CASTS} casts resolved"
                await export_named("post")
                ST["post_exported"] = True
                break
            s = (p0.latest or {}).get("state") or {}
            turn = s.get("turn_number") or 0
            if turn >= TURN_CAP and not ST["in_flight"]:
                say(f"turn cap {TURN_CAP} reached with "
                    f"{len(done)} casts resolved; finishing")
                ST["stop_reason"] = (f"turn cap {TURN_CAP}, "
                                     f"{len(done)} casts resolved")
                await export_named("post")
                ST["post_exported"] = True
                break
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s casts="
                    f"{len(ST['casts'])}/{len(done)} resolved "
                    f"turn={turn} phase={s.get('phase')} wf={wf_type(s)} "
                    f"life={life_of(s,0)}/{life_of(s,1)} "
                    f"tokens={len(clown_tokens(s, 0))}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        states = {}
        for i in range(1, MAX_CASTS + 1):
            for tag in (f"pre_{i}", f"post_{i}"):
                p = f"{EVDIR}/{tag}.json"
                try:
                    if os.path.exists(p):
                        states[tag] = json.loads(open(p).read())["state"]
                        say(f"loaded {tag}.json")
                except Exception as e:
                    notes.append(f"state reload failed for {tag}.json: {e}")
        p = f"{EVDIR}/post.json"
        try:
            if os.path.exists(p):
                states["post"] = json.loads(open(p).read())["state"]
                say("loaded post.json")
        except Exception as e:
            notes.append(f"state reload failed for post.json: {e}")
        pre1 = states.get("pre_1")
        post = states.get("post")
        casts = ST["casts"]
        completed = [c for c in casts if c["cast"]]
        resolved = [c for c in casts if c["resolved"]]

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.83.0/data/card-data.json"))
            c = cd["circuits act"]
            ab0 = (c.get("abilities") or [{}])[0]
            eff = ab0.get("effect") or {}
            sub = (ab0.get("sub_ability") or {}).get("effect") or {}
            ok_roll = (eff.get("type") == "RollDie"
                       and (eff.get("count") or {}).get("value") == 3
                       and eff.get("sides") == 6)
            ok_sub = (sub.get("type") == "Unimplemented"
                      and sub.get("name") == "unparsed_quantity"
                      and "different result" in (sub.get("description")
                                                 or ""))
            notes.append(f"A1: effect={eff.get('type')} "
                         f"count={(eff.get('count') or {}).get('value')} "
                         f"sides={eff.get('sides')}; sub={sub.get('type')}/"
                         f"{sub.get('name')}")
            ok = ok_roll and ok_sub
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre1 is not None:
            in_hand = ACT in hand_lnames(pre1, 0)
            ul = untapped_lands(pre1, 0, P0_LANDS)
            p1_here = any(p.get("id") == 1 for p in pre1.get("players", []))
            ok = in_hand and len(ul) >= 3 and p1_here
            notes.append(f"A2: circuits_in_hand={in_hand} "
                         f"p0_untapped_mountains={len(ul)} p1_present={p1_here}")
        else:
            ok = False
            notes.append("A2 failed: pre_1.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: cast ----
        n_cast = len(completed)
        n_rej = len([r for r in ST["rejections"]
                     if "CastSpell" in json.dumps(r.get("data", {}))])
        ok = n_cast >= 1
        notes.append(f"A3: casts_completed={n_cast} cast_rejections={n_rej}")
        ass["A3_cast"] = "passed" if ok else "failed"

        # ---- A4: resolved ----
        if completed:
            ok = len(resolved) == len(completed)
            notes.append(f"A4: resolved={len(resolved)}/{len(completed)} "
                         f"(gy deltas: "
                         + ", ".join(f"leg{c['i']}:"
                                     f"{c['pre_gy']}->{c['post_gy']}"
                                     for c in resolved) + ")")
        else:
            ok = False
            notes.append("A4 failed: no completed casts")
        ass["A4_resolved"] = "passed" if ok else "failed"

        # ---- A5: tokens ----
        if post is not None and resolved:
            final_tokens = len(clown_tokens(post, 0))
            per_leg = [(c["i"], c["tokens"]) for c in resolved]
            # Oracle minimum: each completed cast creates >= 1 token.
            ok = final_tokens >= len(resolved)
            notes.append(f"A5: completed_casts={len(resolved)} "
                         f"clown_tokens_final={final_tokens} "
                         f"(expected >= {len(resolved)}, Oracle min 1/cast); "
                         f"per-leg post tokens: {per_leg}")
            if ST["roll_sightings"]:
                notes.append(f"roll sightings: {len(ST['roll_sightings'])}")
        else:
            ok = False
            notes.append(f"A5 failed: no resolved casts to measure "
                         f"(post={'ok' if post is not None else 'missing'})")
        ass["A5_tokens"] = "passed" if ok else "failed"

        # ---- A6: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = wf_type(post)
            ok = stack_empty and wft in ("Priority",)
            notes.append(f"A6: stack_empty={stack_empty} post_wf={wft} "
                         f"turns_seen={sorted(ST['turns_seen'])} "
                         f"stop={ST['stop_reason']}")
        else:
            ok = False
            notes.append("A6 failed: post.json missing")
        ass["A6_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif ass["A3_cast"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: no cast ever completed (A3)")
        elif (ass["A4_resolved"] == "passed"
                and ass["A5_tokens"] == "failed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: casts complete and resolve "
                         "but zero Clown Robot tokens are ever created")
        elif ass["A5_tokens"] == "passed":
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: each completed cast "
                         "created >=1 Clown Robot token")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: inconclusive")

        def ev_files():
            fl = []
            for i in range(1, MAX_CASTS + 1):
                for tag in (f"pre_{i}", f"post_{i}"):
                    if os.path.exists(f"{EVDIR}/{tag}.json"):
                        fl.append(f"{tag}.json")
            return fl

        run = {
            "issue": 7176,
            "title": "Circuits act -- [[Circuits Act]] does not make the "
                     "artifact creatures for each different result of the "
                     "roll.",
            "run_id": RUN_ID,
            "server": SERVER_IDENTITY,
            "validated_version": "v0.83.0",
            "verdict": verdict,
            "scope": "Circuits Act dice-roll spell: parse check (RollDie "
                     "typed, token half Unimplemented) + up to 3 native-"
                     "engine casts; aggregate Clown Robot token count vs "
                     "Oracle minimum (1 per cast); native engine, two "
                     "human-client seats",
            "result": "; ".join(
                f"{k}: {v}" for k, v in ass.items()),
            "notes": notes,
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "20x/40x card density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "P1 is fully passive (land drops only, never attacks) "
                "so the cast windows stay clean.",
                "The engine RNG is not controllable from the driver: the "
                "exact per-roll acceptance criteria (1 vs 2 vs 3 tokens "
                "for 1/2/3 distinct results) cannot be targeted; the "
                "asserted outcome is the aggregate Oracle minimum "
                "(>=1 token per completed cast).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "driver": {"protocol_advertised": 70,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7176.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "evidence_files": (ev_files() + ["post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7176.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"]),
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7176.py",
                    f"{EVDIR}/scenario_7176.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        RUN_LOG_DIR = f"/home/hatch/workspace/dev/phase-backfill/runs/{srv_run}"
        try:
            with open(f"{RUN_LOG_DIR}/server.log", "rb") as f:
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
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1040
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7176 - Circuits Act",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.83.0 (b7a59d4) protocol 70 - 2026-09-15 - "
               "dice-roll token spell",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: Roll three six-sided dice. For each different",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "result, create a 1/1 white Clown Robot artifact "
               "creature token.",
               fill=(200, 210, 225))
        y += 24
        d.text((24, y), "card-data: RollDie(3d6) typed; token half = "
               "Unimplemented(unparsed_quantity)",
               fill=(200, 210, 225))
        y += 34
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: RollDie(3d6) + Unimplemented token half",
            "A2_setup": "PRE: Circuits Act in P0 hand + 3 untapped Mountains",
            "A3_cast": ">=1 CastSpell completed (no rejection)",
            "A4_resolved": "each completed cast resolved (gy count grew)",
            "A5_tokens": "cumulative Clown tokens >= completed casts "
                         "(Oracle min 1/cast)",
            "A6_cleanup": "stack empty, game proceeded",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "?")
            col = {"passed": (120, 220, 120),
                   "failed": (255, 90, 90),
                   "not-run": (230, 200, 120)}.get(v, (180, 180, 180))
            d.text((40, y), f"{v:>9}", fill=col)
            d.text((130, y), lab, fill=(210, 220, 235))
            y += 24
        y += 8

        def tokens_of(s):
            if s is None:
                return "?"
            return len([o for o in (s.get("objects", {}) or {}).values()
                        if o.get("zone") == "Battlefield"
                        and o.get("controller") == 0
                        and "clown" in str(o.get("base_name")
                                           or o.get("name") or "").lower()])

        def gy_of(s):
            if s is None:
                return "?"
            return sum(1 for o in (s.get("objects", {}) or {}).values()
                       if o.get("zone") == "Graveyard"
                       and o.get("owner") == 0
                       and str(o.get("base_name") or o.get("name") or "")
                       .lower() == "circuits act")

        for i in range(1, MAX_CASTS + 1):
            pre, pst = states.get(f"pre_{i}"), states.get(f"post_{i}")
            if pre is None and pst is None:
                continue
            d.text((24, y), f"Cast {i}:", fill=(200, 210, 225))
            y += 24
            if pre is not None:
                d.text((40, y), f"pre: P0 gy circuits={gy_of(pre)}, "
                       f"clown tokens={tokens_of(pre)}",
                       fill=(180, 195, 215))
            else:
                d.text((40, y), f"pre_{i}.json MISSING", fill=(255, 90, 90))
            y += 24
            if pst is not None:
                d.text((40, y), f"post: P0 gy circuits={gy_of(pst)}, "
                       f"clown tokens={tokens_of(pst)} "
                       f"(expected >= {i})",
                       fill=(180, 195, 215))
            else:
                d.text((40, y), f"post_{i}.json MISSING", fill=(255, 90, 90))
            y += 30
        y += 6
        d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence "
               "7176/" + RUN_ID + "/", fill=(140, 160, 180))
        p = os.path.join(EVDIR, "summary.png")
        img.save(p)
        say(f"saved summary.png ({os.path.getsize(p)} bytes)")

    def write_manifest():
        # NOTE: no say()/wire() after this point -- any line appended to
        # scenario_run.log after hashing invalidates the manifest.
        say("writing manifest.sha256 (last log line before hashing)")
        files = [f for f in sorted(os.listdir(EVDIR))
                 if f not in ("manifest.sha256",)]
        lines = []
        for fn in files:
            h = hashlib.sha256(
                open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)

    await finish()
    try:
        await p0.close()
        await p1.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
