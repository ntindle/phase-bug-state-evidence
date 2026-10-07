#!/usr/bin/env python3
"""Issue #6868 re-validation on pinned v0.103.0 (protocol 106):
Jared Carthalion -3 doesn't add counters to his own Kavu.

Mechanical port of driver/scenario_6868_01020.py (v0.102.0 / protocol 106)
for pinned v0.103.0 (protocol 106 unchanged), with the 2026-10-07 driver
lessons baked in (engine auto-tap is the single payer for test casts;
stack-watch falls through to the priority-pass gate).
Run id for this port: 20261007-6868.

Oracle (pinned v0.103.0 card-data): "[-3]: Choose up to two target creatures.
For each of them, put a number of +1/+1 counters on it equal to the number
of colors it is."
Pinned v0.103.0 data: the -3 (ability index 1) has multi_target
{min: 0, max: Fixed 2} and its counter child is
effect=Unimplemented(name="unparsed_quantity",
description="put a number of +1/+1 counters on it equal to the number of
colors it is") (sub_ability kind=Spell, sub_link SequentialSibling) --
identical to the v0.98.0 and v0.102.0 parses. Triage classifier: two-target selection
is supported but the color-counted counter child is explicitly
Unimplemented (unsupported_aspect).

The 2026-10-01 v0.98.0 run (20261001-6868, protocol 94) found REPRODUCED:
P0 cast Jared (WUBRG), +1 -> loyalty 6 and a 3/3 all-colors Kavu token;
next P0 turn the -3 offered "up to two" targets as SEQUENTIAL per-target
prompts (schema spec max=1 per prompt) - Kavu answered first, then P1's
Grizzly Bears. Both accepted, loyalty 6->3, stack emptied - but ZERO
counters on either target (Kavu stayed 3/3, Bears stayed 2/2).

Plan (two human seats, native engine, v0.103.0 / protocol 106):
  SETUP  - land drops; P1 casts Grizzly Bears; P0 casts Jared Carthalion
           (gated on 5 distinct untapped basic-land colors).
  PLUS1  - P0 activates Jared +1 (ability_index 0) -> loyalty 6, a 3/3
           all-colors Kavu token is created.
  MINUS3 - next P0 turn: export pre_activate.json, activate -3
           (ability_index 1), answer the up-to-two target selection with
           Kavu (5 colors) + P1 Bears (1 color) - one target per prompt
           if the engine uses sequential prompts (max=1), both at once if
           a single prompt advertises max>=2. Wait for resolution; export
           post_activate.json + post.json.

Behavioral contract:
  A1 setup_ok       pre_activate.json: Jared BF, Kavu token BF (all 5
                    colors), P1 Bears BF
  A2 minus3_offered wire evidence: ActivateAbility ability_index 1
                    advertised on Jared
  A3 target_resolve both targets answered, no rejections; Jared loyalty
                    6 -> 3, stack empty post-resolution
  A4 kavu_counters  post: Kavu carries 5 +1/+1 counters (3/3 -> 8/8)
  A5 bears_counters  post: P1 Bears carries 1 +1/+1 counter (2/2 -> 3/3)
  A6 cleanup         game continues; no stuck prompt

Verdict = reproduced iff A1..A3 pass and (A4 or A5 fail: the reported
gap); not-reproduced iff A4 and A5 pass. Never "fixed".

Protocol-106 driver notes (v0.103.0, per scenario_6862_01030.py):
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction.
  - MulliganDecision answered via legacy Action, gated on the action.
  - DiscardToHandSize via vi schema/select opportunity offering hand cards.
  - CastSpell/ActivateAbility via legacy Action (merged top-level +
    legal_actions_by_object); the engine auto-taps for Auto-payment
    casts itself -- the driver never pays for test casts (MANA_NEEDS
    stays empty; pay_tick gated on outstanding non-test needs).
    sleep(0) yield before leg evaluation; re-tick backstop for
    priority-holding clients.
  - ActivateAbility MUST be submitted while holding priority (submitted
    only when the seat's top-level actions include PassPriority).
  - real_decision_pending excludes the noisy 106 priority-menu codes:
    passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand are NOT
    decisions; playLand vi codes are answered before the decision gate and
    land-play matching uses is_land() over every land the seats can hold.
  - surf_codes() filters None codes.
  - vi schema opportunities: schema/select, schema/sequence,
    exactChoices; engine-issued interactionIds echoed back; seat-0 target
    (Kavu) answered before the seat-1 target (Bears).
  - Target selection for the -3: handle both a single prompt advertising
    max>=2 (submit both choiceIds at once) and sequential per-target
    prompts (max=1: one choiceId per prompt, Kavu first).
  - Waiting for an already-answered target opportunity no longer holds
    the tick: answered targets clear from vi, so real_decision_pending
    must not fire on them (2026-10-07 driver lesson).
  - Mana: the engine auto-taps for Auto-payment casts itself, so the
    driver NEVER pays mana for the TEST casts (Jared, Bears):
    MANA_NEEDS stays empty and pay_tick is gated on outstanding needs
    (2026-10-07 double-pay gate). Setup casts are likewise unpaid.
  - Export: {"type":"ExportAuthoritativeState"}; the response data.state
    is a JSON string parsed once to {"state": {}, ...}; pre/post states
    are authoritative exports (live views omit opponent library objects).
  - Assertions test the reported OUTCOME (counters on targets), not the
    prompt.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6868
RUN_ID = "20261007-6868"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------- server identity
# Exact digests from validation-ledger.json "server" (pinned v0.103.0).
SERVER_IDENTITY = {
    "validated_version": "v0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256":
        "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc",
    "card_data_sha256":
        "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe",
    "draft_pools_sha256":
        "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd",
    "signature_verified": True,
}

for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))


def check_data_level():
    """Record the v0.103.0 parse of Jared's -3 (data-level triage)."""
    j = CARD_DATA["jared carthalion"]
    m3 = j["abilities"][1]
    mt = m3.get("multi_target") or {}
    sub = (m3.get("sub_ability") or {}).get("effect") or {}
    ev = {
        "name": j.get("name"),
        "oracle_text": j.get("oracle_text"),
        "minus3_description": m3.get("description"),
        "minus3_multi_target": mt,
        "minus3_multi_target_max": (mt.get("max") or {}).get("value")
        if isinstance(mt.get("max"), dict) else mt.get("max"),
        "minus3_counter_child": {
            "kind": (m3.get("sub_ability") or {}).get("kind"),
            "effect_type": sub.get("type"),
            "effect_name": sub.get("name"),
            "effect_description": sub.get("description"),
            "sub_link": (m3.get("sub_ability") or {}).get("sub_link"),
        },
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": j.get("name"),
                   "oracle_text": j.get("oracle_text"),
                   "minus3_ability": m3}, f, indent=1)
    say(f"data-level: minus3 multi_target={mt} counter_child={sub.get('type')}/"
        f"{sub.get('name')}")
    # The bug's triage rests on these: up-to-two target selection supported,
    # color-counted counter child explicitly Unimplemented.
    assert ev["minus3_multi_target_max"] == 2, \
        f"minus3 multi_target max changed: {ev['minus3_multi_target_max']}"
    assert ev["minus3_counter_child"]["effect_type"] == "Unimplemented", \
        "minus3 counter child no longer Unimplemented"
    assert ev["minus3_counter_child"]["effect_name"] == "unparsed_quantity", \
        "minus3 counter child name changed"


JARED = "jared carthalion"
KAVU = "kavu"
BEAR = "grizzly bears"
PLAINS = "plains"
ISLAND = "island"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
BASIC_LANDS = {PLAINS, ISLAND, SWAMP, MOUNTAIN, FOREST}

JARED_T = "Jared Carthalion"
KAVU_T = "Kavu"
BEAR_T = "Grizzly Bears"
PLAINS_T = "Plains"
ISLAND_T = "Island"
SWAMP_T = "Swamp"
MOUNTAIN_T = "Mountain"
FOREST_T = "Forest"

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "step": 0, "stop": False, "retry": False,
      "game_code": None}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {}
TGT = {"minus3": None}
PLUS1_TURN = {"turn": None}
OBS = {"target_submissions": []}
WF_NOTE = []


def st_of(c):
    return c.latest or {}

# ------------------------------------------------------------- state helpers


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def distinct_untapped_colors(state, pid):
    return {obj_lname(state, oid) for oid in bf_oids(state, pid)
            if obj_lname(state, oid) in BASIC_LANDS
            and not get_obj(state, oid).get("tapped")}


def untapped_lands(state, pid):
    return sum(1 for oid in bf_oids(state, pid)
               if is_land(get_obj(state, oid))
               and not get_obj(state, oid).get("tapped"))


def jared_oids(state):
    return [oid for oid in bf_oids(state, 0)
            if obj_lname(state, oid) == JARED]


def kavu_oid(state):
    for oid, o in (state.get("objects") or {}).items():
        if (obj_lname(state, oid) == KAVU and o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == "0"):
            return str(oid)
    return None


def p1_bear_oid(state):
    for oid, o in (state.get("objects") or {}).items():
        if (obj_lname(state, oid) == BEAR and o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == "1"):
            return str(oid)
    return None


def colors_of(o):
    c = o.get("colors") or o.get("color") or []
    if isinstance(c, str):
        return [c]
    if isinstance(c, dict):
        return [k for k, v in c.items() if v]
    return list(c)


def loyalty_of(o):
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    return None


def stack_empty(state):
    if state.get("stack"):
        return False
    return not any(o.get("zone") == "Stack"
                   for o in (state.get("objects") or {}).values())


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def merged_actions(st):
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
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")
            and state.get("active_player") == pid
            and stack_empty(state))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
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


def cand_refs(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            out.append(str(d["reference"]))
    return out


def target_opportunity(st):
    """An advertised target-selection vi opportunity (schema/select,
    schema/sequence with candidates, or exactChoices carrying
    candidate/target codes) -- excludes pass menus and optional effects."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(x for x in surf_codes(ch) if x)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(x in codes for x in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def spec_max_of(opp):
    """Best-effort max targets advertised by a schema target prompt."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec") or {}
    for blob in (spec.get("data"), spec, data):
        if isinstance(blob, dict):
            for k in ("max", "maxTargets", "max_targets"):
                v = blob.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
                if isinstance(v, dict) and v.get("type") == "Fixed":
                    return int(v.get("value", 0))
    return None


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        cands = list(d.values()) + [a.get("_src_oid")]
        for v in cands:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == lname:
                return a, iv
    return None, None


def activate_options(state, acts, want_index):
    """Jared's ActivateAbility options (by ability_index). Engine-issued
    actions preferred; ability_index used when advertised."""
    srcs = set(jared_oids(state))
    cands = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", ""))) \
            or str(a.get("_src_oid", ""))
        if src in srcs:
            cands.append(a)
    if want_index is not None:
        by_idx = [a for a in cands
                  if (a.get("data") or {}).get("ability_index") == want_index]
        if by_idx:
            return by_idx
        if cands and all((a.get("data") or {}).get("ability_index") is None
                         for a in cands):
            # ability_index not advertised on 106 actions; fall back to
            # listing order (ability order is +1, -3, -6 in card data).
            wire("ability_index_absent",
                 {"want_index": want_index, "n_cands": len(cands)})
            if want_index < len(cands):
                return [cands[want_index]]
    return cands

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
    LAST_IID["iid"] = sub.get("interactionId")
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
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hn = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def p0_discard_rank(state, o):
    ln = obj_lname(state, o)
    if is_land(get_obj(state, o)):
        return 0
    if ln == JARED:
        return 3  # never discard Jared
    return 1


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if is_land(get_obj(state, o)):
        return 0
    if ln == BEAR:
        return 3
    return 1


async def do_discard(c, acts, st, pid, tag, rank):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
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
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    ref_of[str(d["reference"])] = ch["id"]
                    break
        ranked = sorted(hand, key=lambda o: (rank(state, o),
                                             obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    return False


async def do_declare_empty(c, acts, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            dd = dict(a.get("data", {}) or {})
            dd["attacks"] = []
            dd["bands"] = []
            d["data"] = dd
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            dd = dict(a.get("data", {}) or {})
            dd["assignments"] = []
            d["data"] = dd
            await submit_as_is(c, d)
            return True
    return False


async def pay_tick(c, acts, tag):
    # 2026-10-07 double-pay gate: the engine auto-taps for Auto-payment
    # casts itself; answering a legacy PayMana action on top of that
    # double-pays. Only fire when the driver has outstanding mana needs
    # for a NON-test cast (test casts keep MANA_NEEDS empty).
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) <= 0:
        return False
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] legacy pay-mana action answered "
                f"(outstanding needs={dict(needs)})")
            wire("legacy_pay_mana", {"who": tag, "action": a.get("type"),
                                     "needs": dict(needs)})
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None,
                                                                "available"):
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
        for ch, s in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in s:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def p0_land_prefer(state, o):
    # prefer the basic color we have fewest of; need all five for WUBRG.
    ln = obj_lname(state, o)
    have = {l: sum(1 for oid in bf_oids(state, 0)
                   if obj_lname(state, oid) == l) for l in BASIC_LANDS}
    return have.get(ln, 99)


async def play_a_land(c, state, pid, acts, tag, prefer=None):
    """Play one land per turn (legacy action or vi playLand opportunity)."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    if prefer:
        lands.sort(key=lambda o: prefer(state, o))
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    # 106 may also advertise land plays as a vi playLand opportunity
    st = st_of(c)
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                ref = str(d.get("reference", ""))
                if ref in [str(x) for x in lands]:
                    iid = opp.get("interactionId")
                    key = (tag, "playland", str(iid), ref)
                    if key in SUBMITTED_OPPS:
                        continue
                    SUBMITTED_OPPS.add(key)
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                    wire("play_land_vi", {"who": tag, "oid": ref})
                    await answer_vi(c, opp, ch, tag)
                    return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
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


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]

# ------------------------------------------------------------- -3 target answering


def candidate_info(opp, state):
    """Choice-level detail for target prompts (reference oid, name,
    controller) -- mirrors the 094 driver's candidate_info."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = seat = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            if "reference" in d:
                ref = str(d["reference"])
            if "seat" in d:
                seat = d["seat"]
        o = get_obj(state, ref) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller")})
    return out


async def answer_minus3_prompt(c, state, st):
    """Answer P0's -3 target selection: Kavu (5 colors, seat 0) first,
    then P1's Grizzly Bears (1 color). Handles both a single prompt
    advertising max>=2 (submit both choiceIds at once) and sequential
    per-target prompts (max=1: one choiceId per prompt). Only
    engine-advertised choiceIds are used."""
    if TGT.get("minus3"):
        return False
    if ST["stage"] != "MINUS3" or not ACT.get("minus3"):
        return False
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    want_refs = [r for r in (kavu_oid(state), p1_bear_oid(state)) if r]
    if len(want_refs) < 2:
        return False
    answered = OBS.setdefault("minus3_answered", [])
    iid = opp.get("interactionId") or opp.get("id")
    if not iid or iid in SUBMITTED_OPPS:
        return False
    cands = candidate_info(opp, state)
    if not any(x["zone"] == "Battlefield" for x in cands):
        return False
    present = {x["ref"] for x in cands}
    remaining = [r for r in want_refs
                 if r in present and r not in answered]
    if not remaining:
        return False
    wire("minus3_target_prompt",
         {"rtype": rtype, "spec_type": stype,
          "spec_max": spec_max_of(opp), "candidates": cands})
    say(f"[P0] -3 target prompt (iid={iid}, shape={rtype}/{stype}): "
        f"{[(x['name'], x['ref'], x['controller']) for x in cands]}")
    smax = spec_max_of(opp)
    if rtype == "schema" and stype in ("sequence", "select"):
        if smax is not None and smax >= 2 and len(remaining) >= 2:
            ids = [next(x["choice_id"] for x in cands if x["ref"] == r)
                   for r in remaining[:2]]
            resp_out = {"type": stype, "data": {"choiceIds": ids}}
            chosen = remaining[:2]
        else:
            want = next(x for x in cands if x["ref"] == remaining[0])
            resp_out = {"type": stype,
                        "data": {"choiceIds": [want["choice_id"]]}}
            chosen = [remaining[0]]
    elif rtype == "exactChoices":
        want = next(x for x in cands if x["ref"] == remaining[0])
        resp_out = {"type": "choose", "data": {"choiceId": want["choice_id"]}}
        chosen = [remaining[0]]
    else:
        say(f"[P0] -3: unexpected prompt shape {rtype}/{stype}")
        wire("minus3_unexpected_shape",
             {"rtype": rtype, "spec_type": stype, "candidates": cands})
        return False
    for r in chosen:
        nm = next(x["name"] for x in cands if x["ref"] == r)
        OBS["target_submissions"].append({"ref": r, "name": nm})
    await interact_as(c, {"interactionId": iid, "response": resp_out}, "P0")
    SUBMITTED_OPPS.add(iid)
    answered.extend(chosen)
    say(f"[P0] -3: targeted "
        f"{[next(x['name'] for x in cands if x['ref'] == r) for r in chosen]} "
        f"[{len(answered)}/2]")
    if len(answered) >= 2:
        TGT["minus3"] = {"refs": want_refs, "at": time.time()}
        say("[P0] -3: both targets answered")
    return True

# ------------------------------------------------------------- P0 / P1 logic


async def p0_land_drop(c, pid, state, acts):
    return await play_a_land(c, state, pid, acts, "P0",
                             prefer=p0_land_prefer)


async def p0_setup_cast(c, pid, state, acts):
    # Jared (needs W U B R G); gate on none-on-BF (legend)
    if not jared_oids(state):
        a, oid = find_cast_action(acts, state, JARED)
        if a and len(distinct_untapped_colors(state, pid)) >= 5:
            # 2026-10-07: engine auto-taps for Auto-payment casts; the
            # driver keeps MANA_NEEDS empty for test casts.
            await submit_as_is(c, a)
            say(f"P0 casts Jared Carthalion (oid {oid})")
            wire("cast_jared", {"oid": oid})
            return True
    return False


async def p0_plus1(c, pid, state, acts):
    if ACT.get("plus1"):
        return False
    opts = activate_options(state, acts, 0)
    wire("plus1_options", {"count": len(opts),
                           "all_action_types":
                           sorted({a["type"] for a in acts})})
    say(f"[P0] Jared +1 options: {len(opts)}")
    if not opts:
        return False
    # ActivateAbility MUST be submitted while holding priority.
    if not my_priority(acts):
        return False
    ACT["plus1"] = {"at": time.time(), "turn": state.get("turn_number")}
    PLUS1_TURN["turn"] = state.get("turn_number")
    await submit_as_is(c, opts[0])
    say(f"[P0] activates Jared +1 (ability_index 0) "
        f"turn={state.get('turn_number')}")
    wire("jared_plus1", {"turn": state.get("turn_number")})
    return True


async def p0_minus3(c, pid, state, acts):
    if ACT.get("minus3") or TGT.get("minus3"):
        return False
    # one loyalty activation per permanent per turn: wait for a fresh turn
    if PLUS1_TURN.get("turn") is not None and \
            state.get("turn_number") == PLUS1_TURN["turn"]:
        return False
    if not kavu_oid(state) or not p1_bear_oid(state):
        return False
    opts = activate_options(state, acts, 1)
    wire("minus3_options", {"count": len(opts),
                            "all_action_types":
                            sorted({a["type"] for a in acts})})
    say(f"[P0] Jared -3 options: {len(opts)}")
    if not opts:
        return False
    # ActivateAbility MUST be submitted while holding priority.
    if not my_priority(acts):
        return False
    OBS["minus3_offered"] = True
    await do_export(c, "pre_activate.json")
    ACT["minus3"] = {"at": time.time(), "turn": state.get("turn_number")}
    await submit_as_is(c, opts[0])
    say(f"[P0] activates Jared -3 (ability_index 1) "
        f"turn={state.get('turn_number')}")
    wire("jared_minus3", {"turn": state.get("turn_number")})
    return True


async def p1_step(c, pid, state, acts):
    # land drop preferring Forest (Bears), then cast Bears
    if state.get("active_player") == pid and state.get("phase") in (
            "PreCombatMain", "PostCombatMain", "Main") and stack_empty(state):
        if await play_a_land(c, state, pid, acts, "P1"):
            return True
        if not p1_bear_oid(state):
            a, oid = find_cast_action(acts, state, BEAR)
            if a and untapped_lands(state, pid) >= 2:
                # 2026-10-07: engine auto-taps for Auto-payment casts.
                await submit_as_is(c, a)
                say(f"P1 casts Grizzly Bears (oid {oid})")
                wire("cast_bears", {"oid": oid})
                return True
    return False


async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    # sleep(0) yield before leg evaluation (106 convention)
    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if await answer_minus3_prompt(c, state, st):
        return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await p0_land_drop(c, 0, state, acts):
            return True
        if ST["stage"] == "SETUP":
            if await p0_setup_cast(c, 0, state, acts):
                return True
        elif ST["stage"] == "PLUS1":
            if await p0_plus1(c, 0, state, acts):
                return True
        elif ST["stage"] == "MINUS3":
            if await p0_minus3(c, 0, state, acts):
                return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag, p1_discard_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if await p1_step(c, 1, state, acts):
        return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return out

# ------------------------------------------------------------- main loop


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06868")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((JARED_T, 8), (PLAINS_T, 10), (ISLAND_T, 10),
                         (SWAMP_T, 10), (MOUNTAIN_T, 10), (FOREST_T, 10)))
    p1 = PhaseClient("P16868")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((BEAR_T, 8), (FOREST_T, 12),
                                     (PLAINS_T, 10), (ISLAND_T, 8),
                                     (SWAMP_T, 8), (MOUNTAIN_T, 8)))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej and LAST_IID["iid"] in SUBMITTED_OPPS:
                SUBMITTED_OPPS.discard(LAST_IID["iid"])
                say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                    f"after rejection")
                LAST_IID["iid"] = None
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # with no revision change (missed-broadcast resilience).
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]

        # --- stage transitions
        if ST["stage"] == "SETUP" and jared_oids(state):
            ST["stage"] = "PLUS1"
            say("=== stage -> PLUS1 ===")
        if ST["stage"] == "PLUS1" and ACT.get("plus1") and kavu_oid(state):
            ST["stage"] = "MINUS3"
            say(f"=== Kavu token on BF; stage -> MINUS3 "
                f"(turn {state.get('turn_number')}) ===")
            wire("kavu_created",
                 {"oid": kavu_oid(state),
                  "obj": get_obj(state, kavu_oid(state))})

        # --- resolution watch after -3 targets answered
        if TGT.get("minus3") and not ST["stop"]:
            zids = jared_oids(state)
            loy = loyalty_of(get_obj(state, zids[0])) if zids else None
            if loy == 3 and stack_empty(state):
                await asyncio.sleep(1.5)
                st2 = st_of(p0)
                state2 = st2["state"] if st2 else state
                if stack_empty(state2):
                    await do_export(p0, "post_activate.json")
                    try:
                        await do_export(p0, "post.json")
                    except Exception as e:
                        say(f"final post export failed: {e}")
                    ST["stop"] = True
                    say("=== DONE: -3 resolved ===")
            elif time.time() - TGT["minus3"]["at"] > 180:
                say("targets answered but no resolution after 180s; "
                    "exporting post anyway")
                wire("resolution_timeout", {})
                try:
                    await do_export(p0, "post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # --- stuck watch: activation submitted but prompt never answered
        if ST["stage"] == "MINUS3" and ACT.get("minus3") \
                and not TGT.get("minus3") \
                and time.time() - ACT["minus3"]["at"] > 180 \
                and not ST["stop"]:
            say("target prompt never answered after 180s; exporting post")
            wire("target_timeout", {})
            try:
                await do_export(p0, "post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True

        if time.time() - last_diag > 60:
            last_diag = time.time()
            s = state
            say(f"DIAG P0: rev={p0.revision} turn={s.get('turn_number')} "
                f"phase={s.get('phase')} prio={my_priority(top_acts(st_of(p0)))} "
                f"decision={real_decision_pending(st_of(p0))} "
                f"stage={ST['stage']} kavu={kavu_oid(s) is not None} "
                f"p1bear={p1_bear_oid(s) is not None} "
                f"untapped={untapped_lands(s, 0)}")

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    A = obs["assert"]

    # A1: setup_ok from authoritative pre_activate.json
    pre = env_state("pre_activate.json")
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_activate.json missing (never reached -3)")
    else:
        ko = get_obj(pre, kavu_oid(pre) or "")
        ncolors = len(set(colors_of(ko)))
        ok = bool(jared_oids(pre)) and bool(kavu_oid(pre)) \
            and ncolors == 5 and bool(p1_bear_oid(pre))
        A["A1_setup_ok"] = "passed" if ok else "failed"
        obs["notes"].append(
            f"A1: jared={bool(jared_oids(pre))} kavu={kavu_oid(pre)} "
            f"colors={colors_of(ko)} p1bear={p1_bear_oid(pre)}")

    # A2: minus3 offered (wire evidence of ability_index 1 advertisement)
    A["A2_minus3_offered"] = ("passed"
                              if OBS.get("minus3_offered") else "failed")
    obs["notes"].append(f"A2: minus3 offered={OBS.get('minus3_offered')}")

    # A3: targets answered, no rejections, loyalty 6 -> 3, stack empty
    post = env_state("post_activate.json")
    if post is None:
        A["A3_target_resolve"] = "failed"
        obs["notes"].append("post_activate.json missing")
    else:
        tgt_ok = TGT.get("minus3") is not None
        rejs = 0
        with open(f"{EVDIR}/wire_log.jsonl") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("event") == "rejected":
                    rejs += 1
        zids = jared_oids(post)
        loy = loyalty_of(get_obj(post, zids[0])) if zids else None
        ok = tgt_ok and rejs == 0 and loy == 3 and stack_empty(post)
        A["A3_target_resolve"] = "passed" if ok else "failed"
        obs["notes"].append(
            f"A3: answered={tgt_ok} submissions={OBS['target_submissions']} "
            f"rejections={rejs} loyalty={loy} stack_empty={stack_empty(post)}")

    # A4 / A5: outcome counters on the saved post state
    def norm(s):
        return "".join(ch for ch in str(s).upper() if ch.isalnum())

    def match_p1p1_entry(e):
        if isinstance(e, dict):
            t = norm(e.get("type", "")) + norm(e.get("kind", "")) + \
                norm(e.get("name", ""))
            if "P1P1" in t:
                try:
                    return int(e.get("count", e.get("amount", 1)))
                except Exception:
                    return 1
            return 0
        if isinstance(e, str) and "P1P1" in norm(e):
            return 1
        return 0

    def plus1p1_counters(o):
        v = o.get("p1p1_counters")
        if isinstance(v, (int, float)):
            return int(v), [("p1p1_counters", int(v))]
        out = []
        for key in ("counters", "counter_list"):
            v = o.get(key)
            if isinstance(v, list):
                for e in v:
                    n = match_p1p1_entry(e)
                    if n:
                        out.append((key, n))
        return sum(n for _, n in out), out

    def eval_counters(key, oid_fn, expect, base_pt):
        try:
            o = get_obj(post, oid_fn(post) or "")
            n, detail = plus1p1_counters(o)
            pt = (o.get("power"), o.get("toughness"))
            obs["notes"].append(
                f"{key}: counters={n} detail={detail} power/toughness={pt} "
                f"(expected {expect}; base {base_pt})")
            if n == expect:
                A[key] = "passed"
            elif n == 0 and pt == base_pt:
                A[key] = "failed"
            elif n == 0:
                A[key] = "not-run"
                obs["notes"].append(
                    f"{key}: AMBIGUOUS - no counter entries but P/T changed "
                    f"to {pt}; manual review required")
            else:
                A[key] = "failed"
                obs["notes"].append(f"{key}: unexpected count {n}")
        except Exception as e:
            A[key] = "not-run"
            obs["notes"].append(f"{key} eval error: {e!r}")

    eval_counters("A4_kavu_counters", kavu_oid, 5, (3, 3))
    eval_counters("A5_bears_counters", p1_bear_oid, 1, (2, 2))

    # A6: game continues; no stuck prompt
    final = env_state("post.json")
    if final is None:
        A["A6_cleanup"] = "failed"
        obs["notes"].append("post.json missing")
    else:
        ok = stack_empty(final)
        A["A6_cleanup"] = "passed" if ok else "failed"
        obs["notes"].append(f"A6: stack_empty={stack_empty(final)} "
                            f"turn={final.get('turn_number')} "
                            f"phase={final.get('phase')}")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    if A.get("A1_setup_ok") == "passed" \
            and A.get("A2_minus3_offered") == "passed" \
            and A.get("A3_target_resolve") == "passed" \
            and (A.get("A4_kavu_counters") == "failed"
                 or A.get("A5_bears_counters") == "failed"):
        verdict = "reproduced"
    elif A.get("A4_kavu_counters") == "passed" \
            and A.get("A5_bears_counters") == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "minus3_offered": OBS.get("minus3_offered"),
                   "target_submissions": OBS.get("target_submissions"),
                   "targets": TGT.get("minus3")}, f, indent=1)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "observations": OBS,
                   "targets": TGT.get("minus3")}, f, indent=1, default=str)

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": ST.get("game_code"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": {
            "validated_version": SERVER_IDENTITY["validated_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "server_binary_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/"
                "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/data/draft-pools.json"),
            "signature_verified": SERVER_IDENTITY["signature_verified"],
        },
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [[JARED_T, 8], [PLAINS_T, 10], [ISLAND_T, 10],
                   [SWAMP_T, 10], [MOUNTAIN_T, 10], [FOREST_T, 10]],
            "P1": [[BEAR_T, 8], [FOREST_T, 12], [PLAINS_T, 10],
                   [ISLAND_T, 8], [SWAMP_T, 8], [MOUNTAIN_T, 8]],
        },
        "driver_notes": [
            "Mechanical port of scenario_6868_01020.py for pinned v0.103.0 "
            "(protocol 106 unchanged); behavioral contract A1..A6 and "
            "verdict logic unchanged.",
            "2026-10-07 driver lessons: test casts (Jared, Bears) keep "
            "MANA_NEEDS empty -- the engine's Auto-payment auto-tap is the "
            "single payer (answering on top double-pays); pay_tick is gated "
            "on outstanding non-test needs. The stack/resolution watch "
            "never returns early from the tick without reaching the "
            "priority-pass gate.",
            "Conventions: CreateGameWithSettings + JoinGameWithPassword + "
            "start_when_full + Bo1; protocol-106 HELLO (exact match); "
            "priority = top-level PassPriority, all decisions via "
            "viewer_interaction (waiting_for is gone).",
            "ActivateAbility submitted only while holding priority; legacy "
            "PayMana actions gated, vi tapLandForMana menus driven only by "
            "outstanding MANA_NEEDS; sleep(0) yield before leg evaluation; "
            "5s re-tick backstop for priority-holding clients.",
            "real_decision_pending excludes the 106 priority-menu codes "
            "(passPriority, tapLandForMana, untapLandForMana, castSpell, "
            "activateAbility, candidate, mana, mulliganDecision, "
            "playLand); playLand vi opportunities answered before the "
            "decision gate; land matching via is_land(). surf_codes() "
            "filters None codes.",
            "Jared's +1/-3 selected by ability_index when the engine "
            "advertises it (fallback: listing order, wire-logged via "
            "ability_index_absent).",
            "-3 target selection via advertised vi target opportunity "
            "(schema/select, schema/sequence with candidates, or "
            "exactChoices with candidate/target codes); Kavu (seat 0) "
            "answered before P1 Bears; both choiceIds in one response if "
            "the prompt advertises max>=2, else one choiceId per prompt "
            "(sequential max=1 prompts).",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope); counters asserted on the "
            "saved post state (the reported OUTCOME), not the prompt.",
            "Card-data v0.103.0: -3 keeps multi_target max=Fixed(2); the "
            "color-counted counter child is still "
            "Unimplemented(unparsed_quantity) (SequentialSibling) -- same "
            "as the v0.102.0 parse.",
        ],
        "assertions": A,
        "notes": obs["notes"],
        "verdict": verdict,
        "evidence_comment_id": 5640479052,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x Jared density is a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_6868_01030.py), not direct load.",
        ],
        "setup_line": "P0: 8x Jared Carthalion + 10x each basic land; "
                      "P1: 8x Grizzly Bears + basics (draw-go)",
        "contract_line": "Jared +1 creates a 3/3 all-colors Kavu token; "
                         "next P0 turn -3 targets Kavu + P1 Grizzly Bears "
                         "and each gets +1/+1 counters equal to its color "
                         "count (5 on Kavu, 1 on Bears).",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")

    await render_summary(f"{EVDIR}/summary.png")
    import shutil
    shutil.copy(__file__, f"{EVDIR}/scenario_6868_01030.py")
    say("copied driver into evidence dir")
    await write_manifest()

    await p0.close()
    await p1.close()
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass


def load_ev_state(path):
    with open(f"{EVDIR}/{path}") as f:
        return json.load(f)["state"]


async def render_summary(out_path):
    """Render the PNG from the saved evidence files only."""
    from PIL import Image, ImageDraw
    A = json.load(open(f"{EVDIR}/assertions.json"))
    notes = A.get("notes", [])
    asserts = A.get("assertions", {})
    run = json.load(open(f"{EVDIR}/run.json"))
    try:
        pre = load_ev_state("pre_activate.json")
        kj = any(obj_lname(pre, oid) == JARED for oid in bf_oids(pre, 0))
        kz = kavu_oid(pre) is not None
        kc = len(set(colors_of(get_obj(pre, kavu_oid(pre) or "")))) if kz \
            else 0
        bz = p1_bear_oid(pre) is not None
        pre_line = (f"pre: turn {pre.get('turn_number')} {pre.get('phase')} | "
                    f"Jared={kj} Kavu={kz}({kc} colors) P1Bears={bz}")
    except Exception:
        pre_line = "pre_activate.json: missing"
    try:
        post = load_ev_state("post_activate.json")
        ko = get_obj(post, kavu_oid(post) or "")
        bo = get_obj(post, p1_bear_oid(post) or "")
        kv = ko.get("p1p1_counters", "?")
        bv = bo.get("p1p1_counters", "?")
        kpt = (ko.get("power"), ko.get("toughness"))
        bpt = (bo.get("power"), bo.get("toughness"))
        post_line = (f"post: Kavu +1/+1={kv} {kpt} | Bears +1/+1={bv} {bpt} | "
                     f"Jared loyalty={loyalty_of(get_obj(post, (jared_oids(post) or [''])[0]))}")
    except Exception:
        post_line = "post_activate.json: missing"
    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6868 - Jared Carthalion -3 doesn't add counters "
                    "to his own Kavu (v0.103.0 revalidation)",
           fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), pre_line, fill=(170, 180, 195))
    y += 24
    d.text((24, y), post_line, fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Jared BF, Kavu token BF (5 colors), P1 "
                       "Bears BF",
        "A2_minus3_offered": "A2 -3 (ability_index 1) advertised on Jared",
        "A3_target_resolve": "A3 both targets answered, no rejections; "
                             "loyalty 6->3, stack empty",
        "A4_kavu_counters": "A4 Kavu carries 5 +1/+1 counters (3/3 -> 8/8)",
        "A5_bears_counters": "A5 P1 Bears carries 1 +1/+1 counter (2/2 -> 3/3)",
        "A6_cleanup": "A6 game continues; no stuck prompt",
    }
    for k, lab in labels.items():
        v = asserts.get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in notes[:10]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6868/"
           + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
