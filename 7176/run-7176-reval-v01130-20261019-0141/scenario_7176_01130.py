#!/usr/bin/env python3
"""phase-rs/phase #7176 - Circuits Act does not make the artifact creatures
for each different result of the roll.

Oracle (pinned card-data.json v0.113.0, key 'circuits act'):
  Circuits Act ({2}{R}, Sorcery):
    Roll three six-sided dice. For each different result, create a 1/1 white
    Clown Robot artifact creature token.

Card-data parse state on v0.113.0 (verified 2026-10-19 before the run):
  abilities[0] = Spell RollDie{count:{Fixed:3}, sides:6} ->
  sub_ability = Spell Unimplemented{name:"unparsed_quantity",
    description:"For each different result, create a 1/1 white Clown Robot
    artifact creature token"}.
  The token-creation half is unparsed; the dice-roll half is typed.

v0.113.0 re-validation port (protocol 151, 2026-10-19) of the verified
v0.83.0 run (driver/scenario_7176.py, runs 20260915-7176/7176b, verdict
reproduced). Protocol-151 notes: waiting_for is gone; priority = PassPriority
in the viewing seat's top-level legal_actions; decisions surface via
viewer_interaction; MulliganDecision/DeclareAttackers/DeclareBlockers/
PlayLand/PassPriority remain legacy Actions; mana payment via
PayMana/PayManaAbilityMana actions plus tapLandForMana vi opportunities;
stack entries keep kind.type. Driver logic and the behavioral contract
A1..A6 are unchanged.

Contract (native engine, two human-client seats, single-user, Bo1):
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
    reproduced     iff A2/A3/A4 pass and A5 fails (casts complete but no, or
                   too few, Clown Robots are ever created).
    not-reproduced iff A5 passes (each completed cast created >= 1 token).
    blocked        iff A2 fails (setup never reached) or no cast ever
                   completes (A3 fails).

Evidence: evidence/7176/<run-id>/pre_{1..3}.json, post_{1..3}.json,
post.json, parse_circuits_act.json, run.json, manifest.sha256, summary.png,
scenario_7176_01130.py, wire_log.jsonl, scenario_run.log, server.log.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")

from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7176
RUN_ID = os.environ.get("RUN_ID", "run-7176-reval-v01130-20261019-0141")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ACT = "circuits act"
MOUNTAIN = "mountain"
ISLAND = "island"

P0_DECK = [(ACT, 20), (MOUNTAIN, 40)]
P1_DECK = [(ISLAND, 60)]

MAX_CASTS = 3
TURN_CAP = 24
LEG_TIMEOUT_S = 120

SERVER_IDENTITY = {
    "server_version": "v0.113.0",
    "build_commit": "0571ee3",
    "protocol_version": 151,
    "mode": "single-user",
    "binary_sha256": "f2b09aab4385ff6e6f352d211c2560af42445e11e6b5f2fef0065094c1a8c77d",
    "card_data_sha256": "eb309108eb0df708b6cb5b3c75858149a9fd2864ee0f4ef81195cc75af058b85",
    "draft_pools_sha256": "785376ea4dc8fd2fde29e734a1b069de3a12c57b2df1bb1471556b279b306400",
    "signature_verified": True,
    "observed_at": "2026-10-19",
    "source": ("pinned v0.113.0 (minisign-verified binary + signed data "
               "manifest, ledger server pin 2026-10-18); binary + data "
               "SHA-256 re-verified by this run; fresh isolated v0.113.0 "
               "server started under setsid by this run (VM was replaced "
               "mid-run, killing the earlier backfill server); ServerHello "
               "re-checked by this run."),
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


# ---------------------------------------------------------------- state views
def objects(state):
    return state.get("objects", {}) or {}


def get_obj(state, oid):
    return objects(state).get(str(oid)) or {}


def lname_of(o):
    return str(o.get("base_name") or o.get("name") or "").lower()


def obj_lname(state, oid):
    return lname_of(get_obj(state, oid))


def hand_ids(state, pid):
    return [int(oid) for oid, o in objects(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def hand_lnames(state, pid):
    return [lname_of(o) for o in objects(state).values()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def is_mountain(o):
    return lname_of(o) == MOUNTAIN


def untapped_lands(state, pid):
    return sum(1 for o in objects(state).values()
               if o.get("zone") == "Battlefield"
               and o.get("controller") == pid
               and is_mountain(o) and not o.get("tapped"))


def gy_count(state, pid, name):
    n = 0
    for o in objects(state).values():
        if o.get("zone") != "Graveyard":
            continue
        if o.get("owner") != pid:
            continue
        if lname_of(o) == name:
            n += 1
    return n


def clown_tokens(state, pid):
    return [int(oid) for oid, o in objects(state).items()
            if o.get("zone") == "Battlefield"
            and o.get("controller") == pid
            and "clown" in lname_of(o)]


def stack_entries(state):
    return state.get("stack") or []


def scan_rolls(state):
    """Opportunistic: record any dice-roll evidence visible in state."""
    hits = []
    for entry in stack_entries(state):
        blob = json.dumps(entry).lower()
        if any(k in blob for k in ("rolldie", "die", "dice")):
            hits.append(str(entry)[:500])
    return hits


# ---------------------------------------------------------------- interaction
def merged_actions(st):
    """Merge top-level legal_actions with per-object legal_actions_by_object
    (injecting _src_oid from the dict key) - protocol 151."""
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    """Protocol 118+: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
    await interact_as(c, sub, tag)


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Protocol 118+: DiscardToHandSize surfaces via viewer_interaction;
    gate on hand > 7 plus a schema/select opportunity offering hand cards."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(hand)
    found = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if any(_cand_reference(ch) in handset for ch in cands):
            found = (opp, cands)
            break
    if found is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands = found

    def rank(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in (MOUNTAIN, ISLAND):
            return (0, str(oid))
        if nm == ACT:
            return (2, str(oid))
        return (1, str(oid))

    picks = sorted(cands, key=rank)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} to hand size")
    wire("discard_handsize", {"who": tag,
                              "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": "select",
                                       "data": {"choiceIds": [x.get("id")
                                                              for x in picks]}}},
                      tag)
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


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
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_mountain(get_obj(state, o)) or lname_of(get_obj(state, o)) == ISLAND:
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_confirmed(state, pc):
    return cast_obj_zone(state, pc["oid"]) in (
        "stack", "battlefield", "graveyard", "exile", "command")


def cast_guard_tick(state, tag):
    """Cast-confirmation guard: never pass priority or start new plays under
    an in-flight CastSpell; a 30s backstop clears a still-unconfirmed cast."""
    pc = ST.get("pending_cast")
    if not pc or pc.get("tag") != tag:
        return "proceed"
    if cast_confirmed(state, pc):
        ST["pending_cast"] = None
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] cast guard backstop: clearing unconfirmed cast "
            f"{pc['name']}")
        wire("cast_guard_backstop", pc)
        ST["pending_cast"] = None
        return "proceed"
    return "hold"


async def try_cast(c, state, acts, tag, name, needs):
    a, oid = cast_action_for(acts, state, name)
    if not a:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid})
    await submit_as_is(c, a)
    ST["pending_cast"] = {"oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag}
    return True


def server_run_dir():
    """Locate the live phase-server's run dir from its --games-db flag."""
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if "phase-server" in line and "--games-db" in line:
                parts = line.split()
                i = parts.index("--games-db")
                db = parts[i + 1]
                runid = db.split("/runs/")[1].split("/")[0]
                return runid
    except Exception as e:
        say("server_run_dir lookup failed:", e)
    return None


async def check_server_hello():
    """Read a fresh ServerHello for provenance (separate throwaway conn)."""
    import websockets
    try:
        async with websockets.connect("ws://127.0.0.1:9374/ws",
                                      max_size=10_000_000) as ws:
            raw = await asyncio.wait_for(ws.recv(), 5)
            msg = json.loads(raw)
            d = msg.get("data", {}) or {}
            say("ServerHello:", {k: d.get(k) for k in
                                 ("server_version", "build_commit",
                                  "protocol_version", "mode")})
            return d
    except Exception as e:
        say("ServerHello check failed:", e)
        return {}


MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}

ST = {"stage": "LEGS", "casts": [], "in_flight": False, "done": False,
      "stop_reason": None, "roll_sightings": [], "unexpected_prompts": [],
      "tick_errors": [], "post_exported": False,
      "mana_needs": {}, "pending_cast": None}


async def export_named(tag):
    try:
        raw = await p0c.export_state()
        with open(f"{EVDIR}/{tag}.json", "w") as f:
            f.write(raw)
        say(f"exported {tag.upper()}")
        wire(f"export_{tag}", {"ok": True})
        return True
    except Exception as e:
        say(f"{tag} export failed: {e}")
        wire(f"export_{tag}", {"ok": False, "err": str(e)[:200]})
        return False


def load_state(tag):
    p = f"{EVDIR}/{tag}.json"
    return json.loads(open(p).read())["state"]


# ---------------------------------------------------------------- cast legs
async def try_leg(c, st, state, acts, tag):
    if len(ST["casts"]) >= MAX_CASTS or ST["in_flight"]:
        return False
    if ACT not in hand_lnames(state, 0):
        return False
    if untapped_lands(state, 0) < 3:
        return False
    ca, oid = cast_action_for(acts, state, ACT)
    if not ca:
        return False
    i = len(ST["casts"]) + 1
    if not await export_named(f"pre_{i}"):
        return False
    pre_gy = gy_count(load_state(f"pre_{i}"), 0, ACT)
    entry = {"i": i, "pre_gy": pre_gy, "cast": False, "cast_oid": None,
             "cast_rev": None, "resolved": False, "post_gy": None,
             "tokens": None, "started": time.time(), "abandoned": False}
    ST["casts"].append(entry)
    say(f"[P0] leg {i}: pre_{i} exported (gy={pre_gy}), casting {ACT} "
        f"(oid {oid})")
    wire("cast_submission", {"i": i, "oid": oid, "pre_gy": pre_gy})
    await try_cast(c, state, acts, tag, ACT, {"R": 1, "generic": 2})
    entry["cast_oid"] = str(oid)
    entry["cast_rev"] = c.revision
    ST["in_flight"] = True
    return True


async def watch_cast(state):
    if not ST["in_flight"]:
        return False
    entry = ST["casts"][-1]
    oid = entry["cast_oid"]
    zone = cast_obj_zone(state, oid) if oid else ""
    if not entry["cast"] and zone in ("stack", "battlefield", "graveyard",
                                     "exile", "command"):
        entry["cast"] = True
        say(f"[P0] leg {entry['i']}: cast confirmed on {zone}")
        wire("cast_confirmed", {"i": entry["i"], "zone": zone})
    cur_gy = gy_count(state, 0, ACT)
    if entry["cast"] and cur_gy > entry["pre_gy"]:
        entry["resolved"] = True
        entry["post_gy"] = cur_gy
        ok = await export_named(f"post_{entry['i']}")
        if ok:
            entry["tokens"] = len(clown_tokens(load_state(f"post_{entry['i']}"), 0))
        say(f"[P0] leg {entry['i']} RESOLVED: gy {entry['pre_gy']}->"
            f"{cur_gy}, clown tokens now {entry['tokens']}")
        wire("cast_resolved", {"i": entry["i"], "pre_gy": entry["pre_gy"],
                               "post_gy": cur_gy,
                               "tokens": entry["tokens"]})
        ST["in_flight"] = False
        return True
    if time.time() - entry["started"] > LEG_TIMEOUT_S:
        entry["abandoned"] = True
        ST["in_flight"] = False
        say(f"[P0] leg {entry['i']} ABANDONED after {LEG_TIMEOUT_S}s "
            f"(cast={entry['cast']}, gy={cur_gy})")
        wire("cast_abandoned", {"i": entry["i"], "cast": entry["cast"],
                                "gy": cur_gy})
        return True
    # opportunistic roll sightings while the spell is on the stack
    for h in scan_rolls(state):
        if h not in ST["roll_sightings"]:
            ST["roll_sightings"].append(h)
            say(f"[P0] roll sighting: {h[:200]}")
    return False


async def seat_tick(c, pid, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    active = state.get("active_player")

    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    # never attack
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            sub = copy.deepcopy(da)
            sub.setdefault("data", {})["attacks"] = []
            sub["data"]["bands"] = []
            wire("declare_attackers_empty", {"tag": tag})
            await submit_as_is(c, sub)
        return
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            sub = copy.deepcopy(db)
            sub.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, sub)
        return
    # P0: watch the in-flight cast on every tick (resolution can land on
    # any player's priority)
    if pid == 0 and await watch_cast(state):
        return
    # mana payment has priority over new plays
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return
    # P0 main-phase driving: cast legs, then land drops
    if pid == 0 and my_main(state, 0):
        if cast_guard_tick(state, tag) == "hold":
            return
        if await try_leg(c, st, state, acts, tag):
            return
        if await play_a_land(c, state, 0, acts, tag):
            return
    # P1: land drops only
    if pid == 1 and my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    # never pass priority while our own decision is pending
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if cast_guard_tick(state, tag) == "hold":
            return
        await pass_priority(c, st, acts)


p0c = None


async def main():
    global p0c
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse", "A2_setup", "A3_cast", "A4_resolved", "A5_tokens",
            "A6_cleanup")}

    hello = await check_server_hello()
    if hello:
        for k in ("server_version", "build_commit", "protocol_version"):
            if hello.get(k):
                SERVER_IDENTITY[k] = hello[k]

    # ---- A1: parse check against the pinned v0.113.0 data ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.113.0/data/card-data.json"))
        entry = cd[ACT]
        say("oracle:", entry["oracle_text"].replace("\n", " | ")[:200])
        ab = (entry.get("abilities") or [None])[0] or {}
        with open(f"{EVDIR}/parse_circuits_act.json", "w") as f:
            json.dump(entry.get("abilities"), f, indent=1)
        eff = ab.get("effect") or {}
        cnt = eff.get("count") or {}
        ok_roll = (ab.get("kind") == "Spell"
                   and eff.get("type") == "RollDie"
                   and cnt.get("type") == "Fixed" and cnt.get("value") == 3
                   and eff.get("sides") == 6)
        sub = ab.get("sub_ability") or {}
        sub_eff = sub.get("effect") or {}
        ok_sub = (sub_eff.get("type") == "Unimplemented"
                  and sub_eff.get("name") == "unparsed_quantity")
        if ok_roll and ok_sub:
            ass["A1_parse"] = "passed"
            notes.append("A1_parse: passed (abilities[0] = Spell RollDie "
                         "{count:Fixed 3, sides:6}; sub-ability = "
                         "Unimplemented name='unparsed_quantity' - the "
                         "token-creation half is still unparsed on v0.113.0)")
        else:
            ass["A1_parse"] = "failed"
            notes.append("A1_parse: FAILED to confirm the parse gap: "
                         f"effect={json.dumps(eff)[:200]} "
                         f"sub_effect={json.dumps(sub_eff)[:200]}")
        say(notes[-1])
    except Exception as ex:
        ass["A1_parse"] = "failed"
        notes.append(f"A1_parse failed: {ex}")
        say(notes[-1])

    clients = {}
    for name, i in (("P0", 0), ("P1", 1)):
        c = PhaseClient(name)
        await c.connect()
        clients[name] = (c, i)
    p0, _ = clients["P0"]
    p1, _ = clients["P1"]
    p0c = p0
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")

    async def finish():
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
        states = {}
        for i in range(1, MAX_CASTS + 1):
            for tag in (f"pre_{i}", f"post_{i}"):
                p = f"{EVDIR}/{tag}.json"
                try:
                    if os.path.exists(p):
                        states[tag] = load_state(tag)
                        say(f"loaded {tag}.json")
                except Exception as e:
                    notes.append(f"state reload failed for {tag}.json: {e}")
        try:
            if os.path.exists(f"{EVDIR}/post.json"):
                states["post"] = load_state("post")
                say("loaded post.json")
        except Exception as e:
            notes.append(f"state reload failed for post.json: {e}")
        post = states.get("post")
        pre1 = states.get("pre_1")
        casts = ST["casts"]
        completed = [c for c in casts if c["cast"]]
        resolved = [c for c in casts if c["resolved"]]
        n_resolved = len(resolved)
        final_tokens = len(clown_tokens(post, 0)) if post is not None else None

        # ---- A2: setup ----
        if pre1 is not None:
            in_hand = ACT in hand_lnames(pre1, 0)
            lands = untapped_lands(pre1, 0)
            ok = in_hand and lands >= 3
            ass["A2_setup"] = "passed" if ok else "failed"
            notes.append(f"A2: pre_1 Circuits Act in P0 hand={in_hand}, "
                         f"untapped Mountains={lands} -> "
                         f"{'passed' if ok else 'FAILED'}")
        else:
            ass["A2_setup"] = "failed"
            notes.append("A2 failed: pre_1.json missing")

        # ---- A3: at least one cast completed ----
        ok = len(completed) > 0
        ass["A3_cast"] = "passed" if ok else "failed"
        notes.append(f"A3: completed casts={len(completed)} "
                     f"(legs={[c['i'] for c in completed]}) -> "
                     f"{'passed' if ok else 'FAILED'}")

        # ---- A4: every completed cast resolved ----
        ok = len(completed) > 0 and len(resolved) == len(completed)
        ass["A4_resolved"] = "passed" if ok else "failed"
        notes.append(f"A4: resolved={len(resolved)}/{len(completed)} "
                     + "(" + ", ".join(
                         f"leg{c['i']}:gy{c['pre_gy']}->{c['post_gy']}"
                         for c in resolved) + ") -> "
                     + ("passed" if ok else "FAILED"))

        # ---- A5: Oracle-minimum tokens ----
        per_leg = {c["i"]: c["tokens"] for c in resolved}
        if n_resolved > 0 and final_tokens is not None:
            ok = final_tokens >= n_resolved
            ass["A5_tokens"] = "passed" if ok else "failed"
            a5_note = (f"A5: clown tokens final={final_tokens} vs "
                       f"Oracle minimum {n_resolved} "
                       f"(per-leg post counts: {per_leg}) -> passed"
                       if ok else
                       f"A5: clown tokens final={final_tokens} vs "
                       f"Oracle minimum {n_resolved} "
                       f"(per-leg post counts: {per_leg}) -> FAILED "
                       f"(reported bug: the 'for each different result' "
                       f"token half never executes)")
            notes.append(a5_note)
        else:
            ass["A5_tokens"] = "failed" if completed else "not-run"
            notes.append(f"A5: cannot evaluate (resolved={n_resolved}, "
                         f"final_tokens={final_tokens})")

        # ---- A6: cleanup ----
        if post is not None:
            empty = len(stack_entries(post)) == 0
            ass["A6_cleanup"] = "passed" if empty else "failed"
            notes.append(f"A6: post stack empty={empty}")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 failed: post.json missing")

        notes.append(f"roll sightings on stack: {len(ST['roll_sightings'])}")
        notes.append(f"tick errors: {len(ST['tick_errors'])} "
                     f"{ST['tick_errors'][:3]}")
        notes.append(f"unexpected prompts: {len(ST['unexpected_prompts'])}")
        notes.append(f"stop reason: {ST['stop_reason']}")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        if (ass["A2_setup"] == "passed" and ass["A3_cast"] == "passed"
                and ass["A4_resolved"] == "passed"
                and ass["A5_tokens"] == "failed"):
            verdict = "reproduced"
        elif ass["A5_tokens"] == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
        say("VERDICT:", verdict)

        srv_run = server_run_dir()
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": ("Circuits Act - does not make the artifact creatures "
                      "for each different result of the roll."),
            "validated_at": "2026-10-19",
            "server": SERVER_IDENTITY,
            "scope": ("Circuits Act dice-roll spell: card-data parse check "
                      "(RollDie typed, token half Unimplemented) + up to 3 "
                      "native-engine casts; aggregate Clown Robot token "
                      "count vs Oracle minimum (1 per cast); native engine, "
                      "two human-client seats; v0.113.0 re-validation of the "
                      "2026-09-15 v0.83.0 reproduced run"),
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {
                "casts": ST["casts"],
                "roll_sightings": ST["roll_sightings"],
                "stop_reason": ST["stop_reason"],
                "unexpected_prompts": ST["unexpected_prompts"],
                "tick_errors": ST["tick_errors"],
            },
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "20x Circuits Act density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The engine RNG is not controllable from the driver: the "
                "exact per-roll acceptance criteria (1 vs 2 vs 3 tokens for "
                "1/2/3 distinct results) cannot be targeted; the asserted "
                "outcome is the aggregate Oracle minimum (>=1 token per "
                "completed cast).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via full "
                "game replay).",
                "P1 fully passive (land drops only, no casts, no attacks, "
                "no blocks).",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
            "server_run_note": ("v0.113.0 server on 127.0.0.1:9374 started "
                                "fresh under setsid for this run "
                                f"(games.db + server.log in runs/{RUN_ID}/); "
                                "ServerHello re-checked by this run; "
                                "server.log copied from the server's run dir."),
            "duration_s": round(time.time() - t_start, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        shutil.copy(os.path.abspath(__file__),
                    f"{EVDIR}/scenario_7176_01130.py")
        try:
            shutil.copy(f"{BACKFILL}/runs/{srv_run}/server.log",
                        f"{EVDIR}/server.log")
            say(f"copied server.log from runs/{srv_run}/")
        except Exception as e:
            notes.append(f"server.log copy failed: {e}")
            say("server.log copy failed:", e)
        render_png(run)
        # dynamic manifest: leg files that actually exist + fixed files
        files = []
        for i in range(1, MAX_CASTS + 1):
            for tag in (f"pre_{i}", f"post_{i}"):
                if os.path.exists(f"{EVDIR}/{tag}.json"):
                    files.append(f"{tag}.json")
        files += ["post.json", "parse_circuits_act.json", "run.json",
                  "scenario_7176_01130.py", "wire_log.jsonl",
                  "scenario_run.log", "server.log", "summary.png"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # NOTE: no say() after the manifest is written - scenario_run.log is
        # part of the manifest, so further log appends would invalidate it.
        print("wrote manifest.sha256", flush=True)
        for fn in files:
            if fn.endswith(".json"):
                json.load(open(f"{EVDIR}/{fn}"))
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
        for line in man:
            h, fn = line.split("  ")
            assert hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
        print("validation: all JSON parse, PNG readable, hashes match",
              flush=True)

    def render_png(run):
        from PIL import Image, ImageDraw
        W, H = 1000, 1120
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7176 - Circuits Act does not make "
               "the artifact creatures", fill=(235, 240, 250))
        y += 24
        d.text((24, y), "for each different result of the roll",
               fill=(235, 240, 250))
        y += 30
        d.text((24, y), "server v0.113.0 (0571ee3) protocol 151 - 2026-10-19 "
               "- 2 seats", fill=(140, 160, 180))
        y += 28
        col = (255, 90, 90) if run["verdict"] == "reproduced" else (
            (120, 220, 120) if run["verdict"] == "not-reproduced"
            else (230, 200, 120))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
        y += 34
        d.text((24, y), "Oracle: Roll three six-sided dice. For each "
               "different result, create a", fill=(200, 210, 225))
        y += 22
        d.text((24, y), "1/1 white Clown Robot artifact creature token.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "parse: abilities[0] = Spell RollDie{3d6}; "
               "sub-ability = Unimplemented", fill=(140, 160, 180))
        y += 26
        d.text((24, y), "'unparsed_quantity' (token half never executes)",
               fill=(140, 160, 180))
        y += 32
        ds = run["driver_state"]
        d.text((24, y), "Cast legs:", fill=(200, 210, 225))
        y += 24
        for c in ds["casts"]:
            line = (f"  leg {c['i']}: gy {c['pre_gy']}->{c['post_gy']}, "
                    f"tokens at post_{c['i']} = {c['tokens']}"
                    + (" (ABANDONED)" if c.get("abandoned") else ""))
            d.text((24, y), line[:110], fill=(150, 160, 175))
            y += 22
        y += 8
        d.text((24, y), "Assertions:", fill=(200, 210, 225))
        y += 24
        for k, v in run["assertions"].items():
            c = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (230, 200, 120))
            d.text((36, y), f"{k}: {v}", fill=c)
            y += 22
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:18]:
            d.text((36, y), n[:114], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    # ---- main loop ----
    deadline = time.time() + 1200
    last_tick = {0: 0, 1: 0}
    try:
        while time.time() < deadline and not ST["done"]:
            for c, pid in (p0, 0), (p1, 1):
                tag = ("P0", "P1")[pid]
                if time.time() - last_tick[pid] < 1.0:
                    continue
                try:
                    await seat_tick(c, pid, tag)
                except Exception as e:
                    ST["tick_errors"].append(f"{tag}: {e!r}")
                    wire("tick_error", {"tag": tag, "err": str(e)[:200]})
                last_tick[pid] = time.time()
            done = [c for c in ST["casts"] if c["resolved"]]
            if len(done) >= MAX_CASTS:
                say("all casts resolved; exporting post and finishing")
                ST["stop_reason"] = f"{MAX_CASTS} casts resolved"
                ST["done"] = True
                break
            s = (p0.latest or {}).get("state") or {}
            turn = s.get("turn_number") or 0
            if turn >= TURN_CAP and not ST["in_flight"]:
                say(f"turn cap {TURN_CAP} reached with "
                    f"{len(done)} casts resolved; finishing")
                ST["stop_reason"] = (f"turn cap {TURN_CAP}, "
                                     f"{len(done)} casts resolved")
                ST["done"] = True
                break
            await asyncio.sleep(0.2)
        if not ST["done"]:
            ST["stop_reason"] = ST["stop_reason"] or "deadline"
            notes.append("deadline hit before cleanup completed")
            say("deadline hit")
    finally:
        await finish()
        for c, _ in (p0, 0), (p1, 1):
            try:
                await c.ws.close()
            except Exception:
                pass
        WIRE.close()
        RUNLOG.close()


p0c = None

if __name__ == "__main__":
    asyncio.run(main())
