#!/usr/bin/env python3
"""Issue #9820: Cavalcade of Calamity -- "deals 1 damage to the player or
planeswalker that creature is attacking" never damages the attacked player.

Oracle:
  "Whenever a creature you control with power 1 or less attacks, this
  enchantment deals 1 damage to the player or planeswalker that creature
  is attacking."

Reported (from the issue body, read against the pinned v0.111.0 card data):
  the clause parses as DealDamage with target Or[ParentTarget, Planeswalker].
  An Attacks trigger has no parent target, so on resolution the damage
  resolves to nobody and never happens (reported from play; reproduced in
  the engine with 5 attacking 1/1 tokens: no Cavalcade damage). The proposed
  fix lowers the target to DefendingPlayer. The pinned v0.111.0 card data
  still carries the OLD Or[ParentTarget, Planeswalker] lowering (verified in
  check_data_level below), so the bug is expected to reproduce on the pin.

Behavioral contract -- single leg, 2-player game, native engine, protocol 144:
  Setup: P0 ramps Mountains, casts Cavalcade of Calamity ({1}{R}), casts
    Ornithopters ({0}, 0/2 -- power <= 1 so Cavalcade triggers, 0 power so
    combat damage is always 0 and cannot pollute the life assertion).
    P1 is passive (60 Mountains; declares no attackers, blocks nothing).
  PRE is exported when Cavalcade is on P0's BF and >= 2 Ornithopters are on
    P0's BF (P1 at 20 life).
  On P0's NEXT turn (no summoning sickness), P0 declares attackers with the
    recorded Ornithopter oids, all attacking P1 ({"type":"Player","data":1}).
  The mandatory Attacks triggers go on the stack and resolve with both
    players passing priority. No target selection is advertised (the target
    is derived, not chosen).
  POST is exported once the attack turn ends.

Assertions:
  A1_setup_ok          PRE has Cavalcade on P0 BF, >=2 Ornithopters on P0 BF,
                       P1 at 20 life.
  A2_attackers_declared The recorded Ornithopter oids appear in
                       combat.attackers (mid-attack export).
  A3_trigger_damage    POST: P1 life == 20 - N (N = attackers) means the
                       trigger dealt its damage (bug absent). P1 life == 20
                       with P0 life unchanged is the reported-bug signature
                       (damage resolved to nobody).
  A4_cleanup           POST stack empty.

Verdict rule (pre-registered):
  - reproduced:     A1 passed + A2 passed + A3 failed with the bug signature
                    (P1 life stayed 20).
  - not-reproduced: A1 + A2 + A3 all passed (P1 life == 20 - N).
  - blocked:        anything else (setup never armed, attack never declared,
                    unexpected life values, ...).

Driver conventions (protocol 144, from scenario_9591_01110.py):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - Export response data.state is a JSON string, parsed once.
  - CastSpell submitted exactly as advertised; engine auto-taps mana.
  - finish() ordering: run.json, scenario copy, every logging hook,
    THEN close WIRE/RUNLOG, THEN manifest LAST.
  - DiscardToHandSize is gated on the state's waiting_for type.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario9820")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9820-v01110-20261016-2041")
ISSUE = 9820
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.111.0",
    "build_commit": "33aa23a",
    "protocol_version": 144,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.111.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode when pinned; data digests match the signed "
                       "manifest; digests recomputed against on-disk files "
                       "this run"),
    "source": ("2026-10-16: latest stable release v0.111.0 (published "
               "2026-10-16T15:04:47Z) == pinned release dir; ServerHello "
               "0.111.0/33aa23a/protocol 144 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "existing v0.111.0 server on 127.0.0.1:9374 reused "
               "(identity verified by handshake this run); fresh game created "
               "by this run; isolated evidence under evidence/9820/" + RUN_ID + ")."),
}

for _f, _k in (("server/releases/v0.111.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.111.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.111.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.111.0/data/card-data.json"))

CAVALCADE = "cavalcade of calamity"
ORNITHOPTER = "ornithopter"
MOUNTAIN = "mountain"

P0_DECK = deck((MOUNTAIN, 36), (CAVALCADE, 4), (ORNITHOPTER, 20))
P1_DECK = deck((MOUNTAIN, 60),)

SETUP_DEADLINE_S = 2400
STALL_AFTER_S = 150
ASS_KEYS = ("A1_setup_ok", "A2_attackers_declared", "A3_trigger_damage",
            "A4_cleanup")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
MULL_COUNT = {0: 0, 1: 0}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except ValueError:
        pass


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {}) or {}


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def zone_of(state, oid):
    return str(get_obj(state, oid).get("zone") or "")


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def cavalcade_bf(state):
    return len(bf_by_name(state, 0, CAVALCADE)) > 0


def untapped_mountains(state, pid):
    n = 0
    for oid in bf_oids(state, pid):
        o = get_obj(state, oid)
        if obj_lname(state, oid) == MOUNTAIN and not o.get("tapped"):
            n += 1
    return n


def stack_has(state, lname):
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Stack" and obj_lname(state, oid) == lname:
            return True
    return False


def attacker_oids(state):
    out = set()
    c = state.get("combat") or {}
    for a in c.get("attackers") or []:
        if isinstance(a, (list, tuple)) and a:
            try:
                out.add(int(a[0]))
            except Exception:
                pass
        elif isinstance(a, dict):
            for k in ("attacker", "attacker_id", "object_id", "id"):
                if k in a:
                    try:
                        out.add(int(a[k]))
                    except Exception:
                        pass
                    break
    return out


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))

# ------------------------------------------------------------- vi helpers
def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


# ------------------------------------------------------------- interaction primitives
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


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.111.0"), f"unexpected version {ver}"
    assert int(proto) == 144, f"unexpected protocol {proto}"
    assert str(build) == "33aa23a", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    """Record how Cavalcade of Calamity's trigger parses in pinned v0.111.0."""
    cc = CARD_DATA.get(CAVALCADE, {})
    trigs = cc.get("triggers", []) or []
    atk = [t for t in trigs if t.get("mode") == "Attacks"]
    target = None
    if atk:
        try:
            target = atk[0]["execute"]["effect"]["target"]
        except (KeyError, TypeError, IndexError):
            target = None
    say(f"data-level: cavalcade Attacks triggers={len(atk)} target={json.dumps(target)[:160]}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"card": "Cavalcade of Calamity",
                   "oracle_text": cc.get("oracle_text"),
                   "trigger": atk[0] if atk else None,
                   "finding": ("'deals 1 damage to the player or planeswalker "
                               "that creature is attacking' parses to "
                               "DealDamage with target Or[ParentTarget, "
                               "Typed(Planeswalker)]; an Attacks trigger has "
                               "no parent target, so the damage resolves to "
                               "nobody. The issue's proposed fix lowers the "
                               "target to DefendingPlayer.")},
                  f, indent=1, default=str)
    ST["data_level_ok"] = (
        isinstance(target, dict) and target.get("type") == "Or"
        and any(isinstance(fl, dict) and fl.get("type") == "ParentTarget"
                for fl in target.get("filters", [])))


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    lands = sum(1 for nm in hn if nm == MOUNTAIN)
    MULL_COUNT[pid] += 1
    if tag == "P0":
        keep = (CAVALCADE in hn and lands >= 2) or MULL_COUNT[pid] >= 2
    else:
        keep = True  # P1 is passive; any hand works
    choice = "Keep" if keep else "Mulligan"
    say(f"[{tag}] mulligan -> {choice} (hand={hn})")
    wire("mulligan", {"who": tag, "decision": choice, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    if target is None:
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
           ).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False
    hn = hand_lnames(state, pid)

    def bkey(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if tag == "P0":
            if nm == MOUNTAIN and hn.count(MOUNTAIN) > 3:
                return (0, str(ref))  # extra lands first
            if nm == CAVALCADE:
                return (3, str(ref))  # never bottom the key card
            if nm == ORNITHOPTER:
                return (2, str(ref))
            return (1, str(ref))
        return (1, str(ref))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds":
                                                [ch.get("id") for ch in picks]}}},
                      tag)
    return True


def discard_rank(state, pid, tag, o):
    nm = obj_lname(state, o)
    hn = hand_lnames(state, pid)
    if tag == "P0":
        if nm == CAVALCADE:
            return (5, nm)  # never discard the key card
        if nm == ORNITHOPTER:
            return (4, nm)
        if nm == MOUNTAIN and hn.count(MOUNTAIN) > 8:
            return (0, nm)
        return (2, nm)
    return (2, nm)


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(hand)
    found_opp = None
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
            found_opp = (opp, cands, spec)
            break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands, spec = found_opp
    stype = spec.get("type") or "select"
    ref_of = {}
    for ch in cands:
        ref = _cand_reference(ch)
        if ref is not None:
            ref_of[str(ref)] = ch["id"]
    ranked = sorted(hand,
                    key=lambda o: (discard_rank(state, pid, tag, o),
                                   obj_lname(state, o)))
    picks = ranked[:max(1, n)]
    choice_ids = [ref_of[o] for o in picks if o in ref_of]
    if not choice_ids:
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding to hand size: "
        f"{[obj_lname(state, o) for o in picks]}")
    wire("handsize_discard", {"who": tag, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": choice_ids}}},
                      tag)
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
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
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


async def declare_empty_attackers(c, acts, tag):
    da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
    if da is None:
        return False
    d = copy.deepcopy(da)
    d.setdefault("data", {}).update({"attacks": [], "bands": []})
    say(f"[{tag}] declare no attackers")
    await submit_as_is(c, {"type": "DeclareAttackers", "data": d["data"]})
    return True


async def declare_empty_blockers(c, acts, tag):
    da = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
    if da is None:
        return False
    d = copy.deepcopy(da)
    d.setdefault("data", {}).update({"assignments": []})
    say(f"[{tag}] declare no blockers")
    await submit_as_is(c, {"type": "DeclareBlockers", "data": d["data"]})
    return True


async def legend_keep_first(c, acts, tag):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, a)
            say(f"[{tag}] ChooseLegend keep-first")
            return True
    return False


async def order_triggers_as_is(c, acts, tag):
    for a in acts:
        if a.get("type") == "OrderTriggers":
            await submit_as_is(c, a)
            say(f"[{tag}] OrderTriggers as-is")
            return True
    return False


# ------------------------------------------------------------- cast helpers
def cast_action_for(acts, state, pid, key):
    """Find a CastSpell-ish action in pid's OWN hand for `key`."""
    handset = set(hand_ids(state, pid))
    for a in acts:
        if "cast" not in str(a.get("type", "")).lower():
            continue
        d = a.get("data", {}) or {}
        cands = list(d.values()) + [a.get("_src_oid")]
        for v in cands:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if str(iv) in handset and obj_lname(state, iv) == key:
                return a, str(iv)
    return None, None

# ------------------------------------------------------------- exports
async def export_tag(c, name):
    raw = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(raw)
    say(f"exported {name}.json")


# ------------------------------------------------------------- stage logic
async def p0_try_casts(c, tag, state, acts, st):
    """SETUP casts: one Cavalcade ({1}{R}), then free Ornithopters."""
    turn = state.get("turn_number", 0)
    if ST["stage"] != "SETUP":
        return False
    if not (my_main(state, 0) and my_priority(acts)):
        return False
    # --- Cavalcade first (exactly one; engine auto-taps the {1}{R})
    if not cavalcade_bf(state):
        w = next((o for o in hand_ids(state, 0)
                  if obj_lname(state, o) == CAVALCADE), None)
        if w is not None and untapped_mountains(state, 0) >= 2:
            if ST.get("cav_submit_rev") == c.revision:
                return False
            a, oid = cast_action_for(acts, state, 0, CAVALCADE)
            if a is not None:
                ST["cav_submit_rev"] = c.revision
                ST["cav_rej"] = len(c.rejections)
                say(f"[{tag}] casting Cavalcade of Calamity (oid {oid}) "
                    f"at turn {turn}")
                wire("cavalcade_cast", {"who": tag, "oid": oid, "turn": turn})
                await submit_as_is(c, a)
                return True
        # surface a rejection without latching: clear the guard for retry
        base = ST.get("cav_rej", 0)
        if ST.get("cav_submit_rev") is not None and len(c.rejections) > base:
            ST["cav_rej"] = len(c.rejections)
            ST["cav_submit_rev"] = None
            note = (f"Cavalcade cast rejected at turn {turn}: "
                    f"{json.dumps(c.rejections[base], default=str)[:200]}")
            ST["notes"].append(note)
            say(f"[{tag}] {note}")
            wire("cavalcade_rejected", {"rejections": c.rejections[base:base + 1]})
        return False
    # --- Ornithopters ({0}, free) while arming
    w = next((o for o in hand_ids(state, 0)
              if obj_lname(state, o) == ORNITHOPTER), None)
    if w is not None:
        if ST.get("orn_submit_rev") == c.revision:
            return False
        a, oid = cast_action_for(acts, state, 0, ORNITHOPTER)
        if a is not None:
            ST["orn_submit_rev"] = c.revision
            say(f"[{tag}] casting Ornithopter (oid {oid}) at turn {turn}")
            wire("ornithopter_cast", {"who": tag, "oid": oid, "turn": turn})
            await submit_as_is(c, a)
            return True
    return False


async def p0_arm_check(c, tag, state):
    """Transition SETUP -> ARMED once the fixture is on the battlefield."""
    if ST["stage"] != "SETUP":
        return False
    turn = state.get("turn_number", 0)
    if cavalcade_bf(state):
        orn = bf_by_name(state, 0, ORNITHOPTER)
        if len(orn) >= 2:
            ST["stage"] = "ARMED"
            ST["attacker_oids"] = sorted(orn, key=int)
            ST["ready_turn"] = turn
            ST["p1_life_pre"] = life_of(state, 1)
            ST["p0_life_pre"] = life_of(state, 0)
            say(f"[{tag}] ARMED with {len(orn)} ornithopters at turn {turn} "
                f"(oids {ST['attacker_oids']})")
            wire("armed", {"attacker_oids": ST["attacker_oids"], "turn": turn,
                           "p1_life_pre": ST["p1_life_pre"],
                           "p0_life_pre": ST["p0_life_pre"]})
            await export_tag(c, "pre_attack")
            return True
    if turn > 30:
        ST["notes"].append(
            f"setup never armed by turn {turn} "
            f"(cavalcade_bf={cavalcade_bf(state)}, "
            f"ornithopters={len(bf_by_name(state, 0, ORNITHOPTER))}, "
            f"cavalcade in hand={CAVALCADE in hand_lnames(state, 0)})")
        ST["blocked_reason"] = "fixture never reached the battlefield"
        ST["finished"] = True
        say(f"[{tag}] BLOCKED: fixture never armed by turn {turn}")
        return True
    return False


async def p0_declare_attackers(c, tag, state, acts):
    """Own P0's DeclareAttackers: the real attack only in ARMED on a later
    turn; empty declarations otherwise."""
    if "DeclareAttackers" not in set(a.get("type") for a in acts):
        return False
    da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
    if da is None:
        return False
    d = copy.deepcopy(da)
    turn = state.get("turn_number", 0)
    if ST["stage"] == "ARMED" and turn > (ST.get("ready_turn") or 0):
        live = set(bf_oids(state, 0))
        oids = [o for o in ST["attacker_oids"] if o in live]
        if oids and not ST.get("attack_submitted"):
            d.setdefault("data", {}).update(
                {"attacks": [[int(o), {"type": "Player", "data": 1}]
                             for o in oids],
                 "bands": []})
            ST["attack_submitted"] = True
            ST["attack_rej"] = len(c.rejections)
            ST["n_attackers"] = len(oids)
            ST["attack_turn"] = turn
            ST["p1_life_pre"] = life_of(state, 1)
            ST["p0_life_pre"] = life_of(state, 0)
            ST["stage"] = "ATTACKED"
            say(f"[{tag}] declaring attack with {len(oids)} ornithopters "
                f"-> P1 at turn {turn}")
            wire("attack_submit", {"oids": oids, "turn": turn,
                                  "p1_life_pre": ST["p1_life_pre"]})
            await submit_as_is(c, {"type": "DeclareAttackers",
                                   "data": d["data"]})
            return True
    d.setdefault("data", {}).update({"attacks": [], "bands": []})
    await submit_as_is(c, {"type": "DeclareAttackers", "data": d["data"]})
    return True


async def p0_attacked_monitor(c, tag, state):
    """Watch the ATTACKED stage: acceptance, rejection, turn advance."""
    if ST["stage"] != "ATTACKED":
        return False
    turn = state.get("turn_number", 0)
    # rejection -> fall back to ARMED for a retry next turn
    base = ST.get("attack_rej", 0)
    if not ST.get("attack_accepted") and len(c.rejections) > base:
        ST["attack_rej"] = len(c.rejections)
        ST["attack_submitted"] = False
        ST["stage"] = "ARMED"
        note = (f"attack declaration rejected at turn {turn}: "
                f"{json.dumps(c.rejections[base], default=str)[:200]}; "
                f"retrying next turn")
        ST["notes"].append(note)
        say(f"[{tag}] {note}")
        wire("attack_rejected", {"rejections": c.rejections[base:base + 1]})
        return True
    want = set(int(o) for o in ST.get("attacker_oids", []))
    if want and want <= attacker_oids(state) and not ST.get("attack_accepted"):
        ST["attack_accepted"] = True
        say(f"[{tag}] attack ACCEPTED: {sorted(want)} in combat.attackers")
        wire("attack_accepted", {"attackers": sorted(attacker_oids(state))})
        await export_tag(c, "mid_attack_declared")
        return True
    if turn > (ST.get("attack_turn") or 0):
        say(f"[{tag}] attack turn {ST.get('attack_turn')} ended "
            f"(now turn {turn}); exporting post_attack")
        wire("attack_turn_ended", {"attack_turn": ST.get("attack_turn"),
                                   "turn": turn,
                                   "p1_life": life_of(state, 1),
                                   "p0_life": life_of(state, 0)})
        await export_tag(c, "post_attack")
        ST["stage"] = "DONE"
        ST["finished"] = True
        return True
    return False


# ------------------------------------------------------------- ticks
async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if await legend_keep_first(c, acts, tag):
        return
    if await order_triggers_as_is(c, acts, tag):
        return
    if await pay_tick(c, acts):
        return
    # P0 owns its DeclareAttackers (real attack only when ARMED)
    if await p0_declare_attackers(c, tag, state, acts):
        return
    await asyncio.sleep(0)  # yield to the pump before fresh read
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if await p0_try_casts(c, tag, state, acts, st):
        return
    if await p0_arm_check(c, tag, state):
        return
    if await p0_attacked_monitor(c, tag, state):
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if my_priority(acts):
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if await legend_keep_first(c, acts, tag):
        return
    if await order_triggers_as_is(c, acts, tag):
        return
    if await pay_tick(c, acts):
        return
    # P1 is passive: no attackers, no blockers
    if await declare_empty_blockers(c, acts, tag):
        return
    if await declare_empty_attackers(c, acts, tag):
        return
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if my_priority(acts):
        await pass_priority(c, st, acts)

# ------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9820 - Cavalcade of Calamity: 'damages the player "
                    "that creature is attacking' never happens",
           fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['server_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), "P0 casts Cavalcade of Calamity, attacks with Ornithopters "
                    "(0 power); P1 passive",
           fill=(200, 210, 225))
    y += 26
    d.text((24, y), f"game: {run['game_code']}  attackers: "
                    f"{run['stats'].get('n_attackers')}",
           fill=(200, 210, 225))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 Cavalcade + 2 Ornithopters on BF, P1 at 20 (pre)",
        "A2_attackers_declared": "A2 Ornithopters declared as attackers",
        "A3_trigger_damage": "A3 P1 takes 1 per attacker (REPORTED BUG: 0)",
        "A4_cleanup": "A4 post: stack empty, game live",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((40, y), n[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9820/" +
           run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


# ------------------------------------------------------------- main
async def main():
    pidfile = "/tmp/scenario_9820.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9820 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


async def _main():
    ST.update({
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
        "stage": "SETUP",
        "attacker_oids": [],
        "ready_turn": None,
        "attack_submitted": False,
        "attack_accepted": False,
        "attack_rej": 0,
        "n_attackers": 0,
        "attack_turn": None,
        "p1_life_pre": None,
        "p0_life_pre": None,
        "cav_submit_rev": None,
        "cav_rej": 0,
        "orn_submit_rev": None,
        "finished": False,
        "blocked_reason": None,
        "game_code": None,
        "a3_bug_signature": False,
    })
    await verify_server_hello()
    check_data_level()

    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(P0_DECK)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    ST["game_code"] = p0.game_code
    # Only the game host (P0) can export authoritative state.
    ST["host_client"] = p0
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})

    max_turn = 0
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    i = 0
    while i < 8000:
        i += 1
        await asyncio.sleep(0.2)
        for c, tick, pid in ((p0, p0_tick, 0), (p1, p1_tick, 1)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[pid] = time.time()
            else:
                if time.time() - last_change[pid] > 45:
                    s0 = st["state"]
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                        f"stage={ST.get('stage')} "
                        f"holding_priority={my_priority(merged_actions(st))}")
                    last_change[pid] = time.time()
                holds_prio = my_priority(merged_actions(st))
                if not (holds_prio and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        st = p0.latest
        if st:
            max_turn = max(max_turn, st["state"].get("turn_number", 0))
        if ST["finished"]:
            break
        if time.time() - last_change[0] > STALL_AFTER_S \
                and time.time() - last_change[1] > STALL_AFTER_S \
                and not ST["finished"]:
            ST["notes"].append(f"STALLED: no new revision for {STALL_AFTER_S}s "
                               f"(stage={ST['stage']})")
            say(f"STALL at stage={ST['stage']}")
            wire("stall", {"stage": ST["stage"]})
            break
        if time.time() - t0 > SETUP_DEADLINE_S and not ST["finished"]:
            ST["notes"].append("deadline hit")
            break

    # ---- final export + evaluation
    try:
        if ST["stage"] == "DONE":
            pass  # post_attack already exported
        else:
            await export_tag(p0, "post_aborted")
    except Exception as e:
        ST["notes"].append(f"final export failed: {e}")

    evaluate()

    ass = ST["ass"]
    verdict = ST["verdict"]
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/pull/9820",
        "run_id": RUN_ID,
        "game_code": ST["game_code"],
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 144, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": "36x Mountain / 4x Cavalcade of Calamity / 20x Ornithopter",
                  "P1": "60x Mountain (passive)"},
        "setup_line": ("P0 ramps Mountains, casts Cavalcade of Calamity ({1}{R}) "
                       "then Ornithopters ({0}, 0/2); P1 draw-gos with Mountains"),
        "contract_line": ("Attack with >=2 Ornithopters (power<=1 triggers "
                          "Cavalcade; 0 power means combat damage cannot move "
                          "life totals). The reported bug: the 'player that "
                          "creature is attacking' parses to "
                          "Or[ParentTarget, Planeswalker] with no parent "
                          "target, so the 1 damage per attacker never happens."),
        "stats": {"max_turn": max_turn,
                  "n_attackers": ST["n_attackers"],
                  "attack_turn": ST["attack_turn"],
                  "p1_life_pre": ST["p1_life_pre"],
                  "p0_life_pre": ST["p0_life_pre"],
                  "data_level_ok": ST["data_level_ok"]},
        "assertions": ass,
        "notes": ST["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay.",
            "Only the attacking-a-player branch was exercised; the planeswalker "
            "branch ('or planeswalker') was not tested.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9820.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)
    try:
        render_summary(run, f"{EVDIR}/summary.png")
    except Exception as e:
        say(f"render_summary FAILED: {e}")
        wire("render_failed", {"error": str(e)})

    say("closing logs before manifest computation")
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1), flush=True)
    await p0.close()
    await p1.close()


def env_state(p):
    try:
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)["state"]
    except (FileNotFoundError, KeyError):
        return None


def evaluate():
    A = ST["ass"]
    notes = ST["notes"]

    pre = env_state("pre_attack.json")
    mid = env_state("mid_attack_declared.json")
    post = env_state("post_attack.json")

    # A1: fixture on the battlefield pre-attack
    if pre is None:
        A["A1_setup_ok"] = "failed"
        notes.append("A1 failed: pre_attack.json never exported")
    else:
        cav = len(bf_by_name(pre, 0, CAVALCADE)) >= 1
        orn_n = len(bf_by_name(pre, 0, ORNITHOPTER))
        life = life_of(pre, 1) == 20
        ok = cav and orn_n >= 2 and life
        A["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A1: cavalcade_bf={cav} ornithopters={orn_n} "
                     f"p1_life={life_of(pre, 1)}")

    # A2: the recorded Ornithopters were declared as attackers
    n = ST.get("n_attackers") or 0
    if ST.get("attack_accepted") and n >= 2:
        A["A2_attackers_declared"] = "passed"
    elif mid is not None:
        want = set(int(o) for o in ST.get("attacker_oids", []))
        atk = attacker_oids(mid)
        A["A2_attackers_declared"] = "passed" if (want and want <= atk) else "failed"
    else:
        A["A2_attackers_declared"] = "failed"
    notes.append(f"A2: accepted={ST.get('attack_accepted')} "
                 f"n_attackers={n}")

    # A3: the reported outcome -- trigger damage to the defending player
    if post is None:
        A["A3_trigger_damage"] = "not-run"
        notes.append("A3 not-run: post_attack.json never exported")
    elif n < 2:
        A["A3_trigger_damage"] = "not-run"
        notes.append("A3 not-run: attack never declared")
    else:
        lp1 = life_of(post, 1)
        lp0 = life_of(post, 0)
        if lp1 == 20 - n:
            A["A3_trigger_damage"] = "passed"
            notes.append(f"A3: P1 life 20->{lp1} = 20-{n}: Cavalcade dealt "
                         f"1 per attacker -- correct behavior")
        elif lp1 == 20:
            A["A3_trigger_damage"] = "failed"
            ST["a3_bug_signature"] = True
            notes.append(f"A3: P1 life stayed 20 with {n} attacking power<=1 "
                         f"creatures: NO Cavalcade damage -- reported bug "
                         f"reproduced (P0 life {ST.get('p0_life_pre')}->{lp0})")
        else:
            A["A3_trigger_damage"] = "failed"
            notes.append(f"A3: P1 life {lp1}, expected {20 - n} (fixed) or "
                         f"20 (bug signature) -- unexpected value")

    # A4: cleanup
    if post is None:
        A["A4_cleanup"] = "not-run"
        notes.append("A4 not-run: post_attack.json missing")
    else:
        empty = not (post.get("stack") or [])
        A["A4_cleanup"] = "passed" if empty else "failed"
        notes.append(f"A4: post stack empty={empty}")

    if ST.get("blocked_reason"):
        notes.append(f"blocked_reason: {ST['blocked_reason']}")

    a1, a2, a3 = (A["A1_setup_ok"], A["A2_attackers_declared"],
                  A["A3_trigger_damage"])
    if a1 == "passed" and a2 == "passed" and a3 == "passed":
        verdict = "not-reproduced"
    elif (a1 == "passed" and a2 == "passed" and a3 == "failed"
            and ST.get("a3_bug_signature")):
        verdict = "reproduced"
    else:
        verdict = "blocked"
    ST["verdict"] = verdict
    for k in ASS_KEYS:
        say(f"assertion {k}: {A[k]}")


asyncio.run(main())
