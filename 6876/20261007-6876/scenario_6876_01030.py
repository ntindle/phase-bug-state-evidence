#!/usr/bin/env python3
"""Issue #6876 re-validation on pinned v0.103.0 (protocol 106):
Agent Maria Hill gets +1/+1 counters anytime she is tapped; she should
only get them when tapped to pay a teamwork cost.

Oracle (pinned v0.103.0 card-data, "agent maria hill"):
"Whenever Agent Maria Hill becomes tapped to pay a teamwork cost, put a
+1/+1 counter on her and draw a card."

Data-level triage (v0.103.0): the card parses to a single trigger
{"mode": "Taps", "valid_card": {"type": "SelfRef"}} whose description
mentions teamwork but whose structured fields carry NO teamwork
qualifier (no constraint/condition/origin) -- the parser drops the
qualifier, so the runtime fires on any tap. This run tests the runtime
behavior.

Plan (two human seats, native engine, v0.103.0 / protocol 106):
  SETUP - land drops; P0 casts Agent Maria Hill ({W} 2/1), then Grizzly
          Bears ({1}{G} 2/2). P1 passive (Plains only).
  TEAMWORK (true branch) - P0 main, Hill untapped 0 counters, Bears on BF,
          Beast Mode in hand: export pre_teamwork.json; cast Beast Mode
          ({1}{G}) targeting the Bears; pay the teamwork optional cost;
          tap ONLY Maria Hill for the TapCreatures payment. Her trigger
          (if correct) fires: exactly 1 +1/+1 counter and 1 card drawn.
          When the stack is empty, export post_teamwork.json.
  ATTACK (reported bug) - P0's next turn, attack with Maria Hill alone
          (tapped via attacking -- NOT a teamwork cost). P1 blocks nothing.
          At PostCombatMain export post_attack.json.

Behavioral contract:
  A1 setup_ok            pre_teamwork: Hill on P0 BF untapped with 0
                         counters, Bears on P0 BF, Beast Mode in P0 hand,
                         P0 PreCombatMain
  A2 teamwork_cast       Beast Mode cast with teamwork paid (Hill was the
                         only tapped creature); spell resolved to graveyard
  A3 teamwork_trigger_ok post_teamwork: Hill has exactly 1 +1/+1 counter;
                         P0 hand == pre hand (cast -1, trigger draw +1)
  A4 attack_no_trigger   post_attack: Hill still exactly 1 counter; P0
                         hand == post_teamwork hand + 1 (normal turn draw
                         only); P1 life dropped by Hill's power (unblocked)
  A5 cleanup             stack empty, game not over

Verdict: reproduced iff A1 passes and A4 fails (attack-tap fires the
trigger -- the exact reported claim), or A2 passes and A3 fails (related
trigger-qualifier failure: teamwork tap does not fire it).
not-reproduced iff A1..A5 all pass. blocked otherwise. Never "fixed".

Protocol-106 driver notes (per scenario_6866_01020.py /
scenario_6872_01020.py, pinned v0.103.0):
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction
    (vi.waitingForKind.code exposes the decision kind).
  - MulliganDecision via legacy Action; bottom via vi schema/select gated
    on waitingForKind.code=='mulligan'; DiscardToHandSize via vi
    schema/select.
  - CastSpell via legacy Action, submitted on P0 main/priority; the
    v0.103.0 engine auto-taps reliably, so manual mana taps engage only
    as a fallback if a cast sits >90s with a tap menu visible.
  - Beast Mode prompts: target selection (Bears) -> decideOptionalCost
    teamwork (pay=true) -> TapCreatures selection (Hill only). Shapes
    handled generically: schema select/sequence or exactChoices.
  - DeclareAttackers: relations-schema vi opportunity answered first
    (Hill alone vs P1); the vi fallback only answers relations-schema
    opportunities -- select/sequence schemas are decisions, never
    declares.
  - real_decision_pending excludes the 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand);
    playLand vi codes are answered before the decision gate and
    land-play matching uses is_land().
  - Export: {"type":"ExportAuthoritativeState"}; response data.state is
    a JSON string parsed once. Pre/post states are authoritative
    exports; the reported OUTCOME is asserted on the saved states, not
    the prompt.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time
import traceback

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6876
RUN_ID = "20261007-6876"
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
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))


def check_data_level():
    """Record the v0.103.0 parse of Agent Maria Hill's trigger
    (data-level triage): the reported bug is that the trigger fires on
    ANY tap, so the parse must show whether the teamwork qualifier
    survived parsing."""
    h = CARD_DATA["agent maria hill"]
    trigs = h.get("triggers") or []
    assert trigs, "no triggers parsed for agent maria hill in v0.103.0"
    t = trigs[0]
    ev = {
        "name": h.get("name"),
        "oracle_text": h.get("oracle_text"),
        "trigger_mode": t.get("mode"),
        "trigger_description": t.get("description"),
        "trigger_valid_card": t.get("valid_card"),
        "trigger_constraint": t.get("constraint"),
        "trigger_condition": t.get("condition"),
        "trigger_origin": t.get("origin"),
        "effect_type": ((t.get("execute") or {}).get("effect") or {})
        .get("type"),
        "sub_effect_type": ((((t.get("execute") or {}).get("sub_ability")
                              or {}).get("effect")) or {}).get("type"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": h.get("name"),
                   "oracle_text": h.get("oracle_text"),
                   "triggers": trigs}, f, indent=1)
    say(f"data-level: trigger mode={ev['trigger_mode']} "
        f"constraint={ev['trigger_constraint']} "
        f"condition={ev['trigger_condition']} "
        f"effect={ev['effect_type']}+{ev['sub_effect_type']}")
    assert ev["trigger_mode"] == "Taps", "trigger mode changed"
    assert ev["effect_type"] == "PutCounter", "trigger effect changed"
    assert ev["sub_effect_type"] == "Draw", "trigger sub-effect changed"
    # The qualifier check is observational, not an assert: the whole
    # point of the issue is that it may be missing.
    qualifier = any([
        ev["trigger_constraint"], ev["trigger_condition"],
        ev["trigger_origin"],
        "teamwork" in json.dumps({k: v for k, v in t.items()
                                  if k != "description"}).lower(),
    ])
    say(f"data-level: structured teamwork qualifier present={qualifier} "
        f"(description mentions teamwork="
        f"{'teamwork' in (ev['trigger_description'] or '').lower()})")
    wire("data_level", {**ev, "structured_teamwork_qualifier": qualifier})


HILL_T = "Agent Maria Hill"
BEAST_T = "Beast Mode"
BEAR_T = "Grizzly Bears"
PLAINS_T = "Plains"
FOREST_T = "Forest"

HILL_L = "agent maria hill"
BEAST_L = "beast mode"
BEAR_L = "grizzly bears"
PLAINS_L = "plains"
FOREST_L = "forest"

P0_DECK = ((HILL_T, 4), (BEAST_T, 8), (BEAR_T, 8),
           (PLAINS_T, 15), (FOREST_T, 15))
P1_DECK = ((PLAINS_T, 40),)

GAME_TIMEOUT = 2400
TEAMWORK_WATCHDOG = 600
ATTACK_WATCHDOG = 600

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0,
      "teamwork_turn": None, "attack_turn": None,
      "pre_hand": None, "post_hand": None,
      "p1_block_grace_until": 0,
      "opp_shapes_logged": set()}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {"cast": None}
OBS = {"beast_casts": 0, "teamwork_pay_subs": 0, "tap_select_subs": 0,
       "beast_stack_seen": False, "hill_trigger_stack_seen": False,
       "attack_declared": False, "teamwork_offered": False,
       "beast_target_name": None, "rejections": []}


def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def perm_oids(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def find_hand_oid(state, pid, lname):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == lname:
            return o
    return None


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "n"):
            if k in v:
                return num(v[k])
    return None


def life_of(state, pid):
    return player_of(state, pid).get("life")


def p1p1_count(o):
    """Count +1/+1 counters on an object; robust to shape variants."""
    total = 0
    ctrs = o.get("counters")
    if isinstance(ctrs, dict):
        for k, v in ctrs.items():
            if "P1P1" in str(k) or "plus_one" in str(k).lower():
                total += v if isinstance(v, int) else 1
    elif isinstance(ctrs, list):
        for c in ctrs:
            if isinstance(c, dict):
                t = str(c.get("type", c.get("kind", c.get("name", ""))))
                n = c.get("count", c.get("n", 1))
                if "P1P1" in t:
                    total += n if isinstance(n, int) else 1
            elif "P1P1" in str(c):
                total += 1
    return total


def stack_empty(state):
    if state.get("stack"):
        return False
    return not any(o.get("zone") == "Stack"
                   for o in (state.get("objects") or {}).values())


def stack_ids(state):
    return [str(e.get("id")) for e in (state.get("stack") or [])]


def stack_entry(state, sid):
    for e in (state.get("stack") or []):
        if str(e.get("id")) == str(sid):
            return e
    return None


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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


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


def opp_codes(opp):
    codes = set()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    for ch in (data.get("candidates") or data.get("choices") or []):
        codes.update(c for c in surf_codes(ch) if c)
    return codes


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
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def candidate_seat(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def cand_object_ref(cand):
    for s in (cand or {}).get("surfaces", []) or []:
        if s.get("type") == "object":
            return str((s.get("data") or {}).get("reference"))
    ref = _cand_reference(cand)
    return str(ref) if ref is not None else None


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


def relations_shape(opp):
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}
    return edges, cands


def pick_bool_choice(choices, want):
    """Pick the accept/decline choice from bare true/false 106 surfaces."""
    want_s = "true" if want else "false"
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if s.get("type") == "value" and dd.get("role") == "accept" \
                    and str(dd.get("value")).lower() == want_s:
                return ch
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if dd.get("role") in ("pay", "accept", "decision"):
                v = str(dd.get("value")).lower()
                if (want and v == "true") or (not want and v == "false"):
                    return ch
    for ch in choices:
        t = choice_text(ch).lower()
        if want and any(k in t for k in ("pay", "accept", "yes",
                                         "sacrifice")) \
                and "don't" not in t and "decline" not in t \
                and "not " not in t:
            return ch
        if not want and any(k in t for k in ("decline", "don't", "do not")):
            return ch
    for ch in choices:
        if choice_text(ch).strip().lower() == want_s:
            return ch
    return None


def object_on_stack_or_bf(state, lname):
    for o in (state.get("objects") or {}).values():
        if str(o.get("base_name") or o.get("name") or "").lower() == lname \
                and o.get("zone") in ("Stack", "Battlefield"):
            return True
    return False


def find_cast_action(acts, state, lname, oid=None):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if oid is not None and str(iv) != str(oid):
                continue
            if obj_lname(state, iv) == lname:
                return a
    return None


# ------------------------------------------------------- interaction drivers


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"]})
    await c.send_action(action)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "stage": ST["stage"],
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
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = ST["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in (PLAINS_L, FOREST_L))
    if tag == "P0":
        choice = "Keep" if (n_lands >= 2 and (HILL_L in hand or n >= 2)) \
            else "Mulligan"
    else:
        choice = "Keep" if n_lands >= 2 or n >= 2 else "Mulligan"
    if choice == "Mulligan":
        ST["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as a vi schema/select
    opportunity, gated on waitingForKind.code == 'mulligan' at turn 1 /
    Untap. Never bottoms Hill/Beast/Bears for P0."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
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
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        keep = {HILL_L, BEAST_L, BEAR_L} if tag == "P0" else set()

        def bkey(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else "?"
            if nm in keep:
                return (2, str(ref))
            if ref is not None and is_land(get_obj(state, ref)):
                return (1, str(ref))
            return (0, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


def p0_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln in (HILL_L, BEAST_L, BEAR_L):
        return 3  # never discard the combo pieces
    if is_land(get_obj(state, o)):
        return 2
    return 0


def p1_discard_rank(state, o):
    if is_land(get_obj(state, o)):
        return 2
    return 0


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
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]
        ranked = sorted(hand, key=lambda o: (rank(state, o),
                                             obj_lname(state, o)))
        pick = ranked[:n]
        wire("discard_rank",
             {"who": tag,
              "ranked": [(o, obj_lname(state, o), rank(state, o))
                         for o in ranked]})
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


async def do_legend(c, acts, st, tag):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, a)
            ST["legend_answered"] += 1
            say(f"[{tag}] legend rule: keeps first (advertised action)")
            wire("legend", {"who": tag, "style": "action"})
            return True
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or []
        if not chs:
            continue
        blob = (str(opp.get("description", "")) + " " +
                " ".join(choice_text(ch) for ch in chs)).lower()
        if "legend" not in blob:
            continue
        iid = opp.get("interactionId")
        key = (tag, "legend", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        ST["legend_answered"] += 1
        say(f"[{tag}] legend rule: keeps first (vi choice)")
        wire("legend", {"who": tag, "style": "vi"})
        await answer_vi(c, opp, chs[0], tag)
        return True
    return False


async def do_declare_empty(c, acts, st, pid, tag, attackers_ok=True):
    """Declare empty attackers/blockers. The vi fallback only answers
    relations-schema declare opportunities (select/sequence schemas are
    decisions, never declares). For P1 blockers, a 25s grace window lets
    the relations opportunity arrive before the advertised empty submit."""
    for a in acts:
        if a.get("type") == "DeclareAttackers" and attackers_ok:
            d = copy.deepcopy(a)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            if tag == "P1" and time.time() < ST.get("p1_block_grace_until", 0):
                continue  # relations blockers opp may still arrive; wait
            d = copy.deepcopy(a)
            d.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower() \
            and attackers_ok:
        opp = find_relations_op(st)
        if opp is not None:
            iid = opp.get("interactionId")
            key = (tag, "declare", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare empty via vi relations opportunity")
            wire("declare_empty_vi", {"who": tag})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "relations",
                                               "data": {"relations": []}}},
                              tag)
            return True
        key = (tag, "declare-blind", str(c.revision))
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] blind declare empty (106 shape)")
            wire("declare_empty_blind", {"who": tag})
            await c.send_action({"type": "DeclareAttackers",
                                 "data": {"attacks": [], "bands": []}})
            return True
    if tag == "P1" and state.get("active_player") != pid \
            and "declareblock" in phase.lower():
        if find_relations_op(st) is None:
            ST["p1_block_grace_until"] = time.time() + 25
    return False


async def do_declare_attackers_hill(c, acts, st, pid, tag):
    """ATTACK stage: P0 declares Agent Maria Hill alone via the
    relations-schema opportunity (106 analog of the protocol-94 relations
    declare)."""
    state = st["state"]
    if str(state.get("active_player")) != str(pid):
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    opp = find_relations_op(st)
    if opp is None:
        return False
    iid = opp.get("interactionId") or opp.get("id")
    key = (tag, "declare-atk", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    edges, cands = relations_shape(opp)
    if "attack_edges" not in ST["opp_shapes_logged"]:
        ST["opp_shapes_logged"].add("attack_edges")
        wire("declare_attackers_shape",
             {"n_edges": len(edges), "edges": edges[:6],
              "n_candidates": len(cands)})
    hill_oid = next((o for o in bf_oids(state, pid)
                     if obj_lname(state, o) == HILL_L), None)
    rels = []
    if hill_oid is not None:
        ho = get_obj(state, hill_oid)
        if not ho.get("tapped"):
            for e in edges:
                src = e.get("sourceId")
                tids = e.get("targetIds") or []
                cand = cands.get(src, {})
                ref = cand_object_ref(cand)
                if ref is None or str(ref) != str(hill_oid):
                    continue
                want_tid = None
                for tid in tids:
                    if candidate_seat(cands.get(tid, {})) == 1:
                        want_tid = tid
                        break
                if want_tid is None and tids:
                    want_tid = tids[0]
                if want_tid:
                    rels.append({"sourceId": src, "targetId": want_tid,
                                 "group": None})
    if not rels:
        sub = {"interactionId": iid,
               "response": {"type": "relations", "data": {"relations": []}}}
        wire("declare_attackers", {"who": tag, "n_rels": 0,
                                   "reason": "hill-unable",
                                   "submission": sub})
        say(f"[{tag}] declares no attackers (Hill unable, turn "
            f"{state.get('turn_number')})")
        SUBMITTED_OPPS.add(key)
        await interact_as(c, sub, tag)
        return True
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": rels}}}
    wire("declare_attackers", {"who": tag, "n_rels": len(rels),
                               "submission": sub})
    say(f"[{tag}] declares attackers: Hill alone (turn "
        f"{state.get('turn_number')})")
    SUBMITTED_OPPS.add(key)
    OBS["attack_declared"] = True
    ST["attack_turn"] = state.get("turn_number")
    await interact_as(c, sub, tag)
    return True


async def pay_mana_vi(c, st, tag, needs):
    """Tap one land via the 106 tapLandForMana menu. One tap per call.
    Fallback only: the v0.103.0 engine auto-taps reliably."""
    for opp in vi_ops(st):
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
        say(f"[{tag}] tap land for mana used_for={used} (fallback)")
        wire("tap_land", {"who": tag, "used_for": used, "fallback": True})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


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
                                                   "data": {"choiceId":
                                                             ch.get("id")}}},
                                  c.name)
                return True
    return False


async def drive_mana(c, tag):
    """Mana fallback: the v0.103.0 engine auto-taps reliably, so manual
    tapping engages only if a cast sits unannounced for >90s with a tap
    menu visible (logged loudly)."""
    cs = ACT.get("cast")
    st = st_of(c)
    state = st["state"]
    if cs and cs["in_flight"]:
        if object_on_stack_or_bf(state, cs["lname"]):
            cs["in_flight"] = False
            return False
        if time.time() - cs["submit_t"] < 90:
            return False
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) <= 0:
            return False
        if await pay_mana_vi(c, st, tag, needs):
            cs["taps"] += 1
            say(f"[{tag}] FALLBACK manual tap #{cs['taps']} for {cs['tag']} "
                f"(auto-tap did not fire in 90s)")
            wire("manual_tap", {"tag": cs["tag"], "taps": cs["taps"],
                                "needs": dict(needs), "fallback": True})
            return True
        return False
    return False


# ------------------------------------------------------------- #6876 logic


def beast_on_stack(state):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if "gets +2/+2 and gains trample" in blob:
            return True
    return False


def beast_stack_entry(state):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if "gets +2/+2 and gains trample" in blob:
            return e
    return None


def hill_trigger_on_stack(state):
    """Hill's triggered ability on the stack: a TriggeredAbility entry
    mentioning Agent Maria Hill and the draw-a-card effect."""
    for e in (state.get("stack") or []):
        kind = (e.get("kind") or {})
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        blob = json.dumps(e, default=str).lower()
        if "maria hill" in blob and "draw a card" in blob:
            if ktype in (None, "TriggeredAbility", "triggeredability"):
                return True
            return True
    return False


def bf_creature_refs(state, opp, pid):
    """Candidate (choice_id, ref_oid) pairs referencing BF creatures of
    pid in a target/tap opportunity."""
    refs = []
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = cand_object_ref(ch)
        if ref is None:
            continue
        o = get_obj(state, ref)
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller", -1)) == str(pid) \
                and "Creature" in ((o.get("card_types") or {})
                                   .get("core_types") or []):
            refs.append((ch, str(ref)))
    return refs


async def answer_beast_prompts(c, state, st):
    """TEAMWORK stage: drive Beast Mode's cast prompts on 106.
    Order: target selection (Bears) -> decideOptionalCost teamwork
    (pay=true) -> TapCreatures payment (Hill ONLY)."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        codes = opp_codes(opp)
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        if not choices:
            continue
        kind = vi_kind_code(st)

        # --- OptionalCostChoice: teamwork pay/decline -> PAY
        if "decideOptionalCost" in codes:
            if "teamwork_cost" not in ST["opp_shapes_logged"]:
                ST["opp_shapes_logged"].add("teamwork_cost")
                wire("teamwork_cost_prompt",
                     {"kind": kind, "codes": sorted(codes),
                      "n_choices": len(choices),
                      "choice_texts": [choice_text(ch)[:80]
                                       for ch in choices]})
                say(f"[P0] teamwork OptionalCostChoice (kind={kind}): "
                    f"{[choice_text(ch)[:60] for ch in choices]}")
            ch = pick_bool_choice(choices, True)
            if ch is None:
                say("[P0] teamwork cost: no pay choice found; deferring")
                wire("teamwork_cost_unmatched",
                     {"kind": kind,
                      "choices": [choice_text(x)[:80] for x in choices]})
                return True
            OBS["teamwork_offered"] = True
            SUBMITTED_OPPS.add(iid)
            OBS["teamwork_pay_subs"] += 1
            say("[P0] teamwork -> PAY (true)")
            wire("teamwork_pay", {"iid": iid})
            await answer_vi(c, opp, ch, "P0")
            return True

        # --- creature-candidate prompts: target selection (pre-pay) or
        # --- the TapCreatures payment (post-pay)
        refs = bf_creature_refs(state, opp, 0)
        if refs:
            if "creature_cand" not in ST["opp_shapes_logged"]:
                ST["opp_shapes_logged"].add("creature_cand")
                wire("creature_candidate_prompt",
                     {"kind": kind, "codes": sorted(codes),
                      "rtype": resp.get("type"),
                      "candidates": [(choice_text(ch)[:40], ref)
                                     for ch, ref in refs]})
                say(f"[P0] creature-candidate prompt (kind={kind} "
                    f"rtype={resp.get('type')}): "
                    f"{[(choice_text(ch)[:30], ref) for ch, ref in refs]}")
            if OBS["teamwork_pay_subs"] == 0:
                # target selection for Beast Mode -> the Bears (keeps
                # Beast Mode's own counter off Hill, clean signal)
                want = next(((ch, ref) for ch, ref in refs
                             if obj_lname(state, ref) == BEAR_L), None)
                if want is None:
                    say("[P0] target prompt: no Bears candidate; deferring")
                    wire("target_unmatched",
                         {"candidates": [(choice_text(ch)[:40], ref)
                                         for ch, ref in refs]})
                    return True
                ch, ref = want
                SUBMITTED_OPPS.add(iid)
                say(f"[P0] Beast Mode targets Bears (ref={ref})")
                wire("beast_target", {"ref": ref})
                await answer_vi(c, opp, ch, "P0")
                return True
            else:
                # TapCreatures payment -> ONLY Maria Hill
                want = next(((ch, ref) for ch, ref in refs
                             if obj_lname(state, ref) == HILL_L), None)
                if want is None:
                    say("[P0] tap prompt: no Hill candidate; deferring")
                    wire("tap_unmatched",
                         {"candidates": [(choice_text(ch)[:40], ref)
                                         for ch, ref in refs]})
                    return True
                ch, ref = want
                ho = get_obj(state, ref)
                if ho.get("tapped"):
                    say("[P0] tap prompt: Hill already tapped; deferring")
                    return True
                SUBMITTED_OPPS.add(iid)
                OBS["tap_select_subs"] += 1
                say(f"[P0] teamwork taps Hill ONLY (ref={ref})")
                wire("teamwork_tap_hill", {"ref": ref})
                await answer_vi(c, opp, ch, "P0")
                return True
    return False


async def p0_setup(c, pid, state, acts, tag):
    """SETUP: cast Agent Maria Hill ({W}), then Grizzly Bears ({1}{G}).
    The engine auto-taps; drive_mana is the fallback."""
    if not perm_oids(state, pid, HILL_L) \
            and not object_on_stack_or_bf(state, HILL_L):
        oid = find_hand_oid(state, pid, HILL_L)
        a = find_cast_action(acts, state, HILL_L, oid) if oid else None
        if a is not None:
            ACT["cast"] = {"tag": f"hill-{oid}", "lname": HILL_L,
                           "in_flight": True, "taps": 0,
                           "submit_t": time.time()}
            MANA_NEEDS[tag] = {"W": 1}
            say(f"[{tag}] casts Agent Maria Hill ({oid})")
            wire("cast_submit", {"tag": "hill", "oid": oid})
            await submit_as_is(c, a)
            return True
    if len(perm_oids(state, pid, BEAR_L)) < 1 \
            and not object_on_stack_or_bf(state, BEAR_L):
        oid = find_hand_oid(state, pid, BEAR_L)
        a = find_cast_action(acts, state, BEAR_L, oid) if oid else None
        if a is not None:
            ACT["cast"] = {"tag": f"bears-{oid}", "lname": BEAR_L,
                           "in_flight": True, "taps": 0,
                           "submit_t": time.time()}
            MANA_NEEDS[tag] = {"G": 1, "generic": 1}
            say(f"[{tag}] casts Grizzly Bears ({oid})")
            wire("cast_submit", {"tag": "bears", "oid": oid})
            await submit_as_is(c, a)
            return True
    return False


async def p0_teamwork_cast(c, pid, state, acts, tag):
    """TEAMWORK: cast Beast Mode ({1}{G}); prompts handled by
    answer_beast_prompts."""
    if OBS["beast_casts"] > 0:
        return False
    if object_on_stack_or_bf(state, BEAST_L):
        return False
    oid = find_hand_oid(state, pid, BEAST_L)
    a = find_cast_action(acts, state, BEAST_L, oid) if oid else None
    if a is None:
        return False
    ACT["cast"] = {"tag": f"beast-{oid}", "lname": BEAST_L,
                   "in_flight": True, "taps": 0,
                   "submit_t": time.time()}
    MANA_NEEDS[tag] = {"G": 1, "generic": 1}
    OBS["beast_casts"] += 1
    say(f"[{tag}] casts Beast Mode ({oid})")
    wire("cast_submit", {"tag": "beast", "oid": oid,
                         "turn": state.get("turn_number")})
    await submit_as_is(c, a)
    return True


async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if ST["stage"] == "ATTACK" and not OBS["attack_declared"]:
        if await do_declare_attackers_hill(c, acts, st, 0, tag):
            return True
    attackers_ok = not (ST["stage"] == "ATTACK"
                        and not OBS["attack_declared"])
    if await do_declare_empty(c, acts, st, 0, tag,
                              attackers_ok=attackers_ok):
        return True
    if await drive_mana(c, tag):
        return True

    # sleep(0) yield before leg evaluation (106 convention)
    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # answer cast prompts before anything else
    if ST["stage"] == "TEAMWORK":
        if await answer_beast_prompts(c, state, st):
            return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        if ST["stage"] == "SETUP":
            if await p0_setup(c, 0, state, acts, tag):
                return True
        elif ST["stage"] == "TEAMWORK":
            if await p0_teamwork_cast(c, 0, state, acts, tag):
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
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag, p1_discard_rank):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True
    if await drive_mana(c, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 1, acts, tag):
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


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def hill_snapshot(state):
    """(oid, tapped, counters, power) for P0's Agent Maria Hill, or None."""
    oids = perm_oids(state, 0, HILL_L)
    if not oids:
        return None
    o = get_obj(state, oids[0])
    return {"oid": oids[0], "tapped": bool(o.get("tapped")),
            "counters": p1p1_count(o), "power": num(o.get("power"))}


def beast_zone(state):
    zones = set()
    for oid, o in (state.get("objects") or {}).items():
        if obj_lname(state, oid) == BEAST_L:
            zones.add(o.get("zone"))
    return sorted(zones)


# ------------------------------------------------------------- main loop


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06876")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16876")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    teamwork_t0 = None
    attack_t0 = None
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS.setdefault("rejections", []).extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
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

        if str(state.get("phase") or "").lower() == "gameover" \
                and not ST["stop"]:
            obs["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            try:
                await do_export(p0, "post_attack.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True
            continue

        # --- SETUP -> TEAMWORK transition
        if ST["stage"] == "SETUP" and my_main(state, 0):
            hs = hill_snapshot(state)
            bears = perm_oids(state, 0, BEAR_L)
            beast_hand = find_hand_oid(state, 0, BEAST_L)
            if hs and not hs["tapped"] and hs["counters"] == 0 \
                    and bears and beast_hand:
                turn = state.get("turn_number")
                say(f"=== stage -> TEAMWORK (turn {turn}) ===")
                wire("teamwork_armed", {"turn": turn,
                                        "hand": len(hand_ids(state, 0))})
                ST["pre_hand"] = len(hand_ids(state, 0))
                ST["teamwork_turn"] = turn
                await do_export(p0, "pre_teamwork.json")
                ST["stage"] = "TEAMWORK"
                teamwork_t0 = time.time()

        # --- TEAMWORK watches
        if ST["stage"] == "TEAMWORK":
            if beast_on_stack(state):
                if not OBS["beast_stack_seen"]:
                    OBS["beast_stack_seen"] = True
                    e = beast_stack_entry(state)
                    blob = json.dumps(e, default=str)
                    # record what the spell targeted, if visible
                    tgt = None
                    for oid, o in (state.get("objects") or {}).items():
                        if obj_lname(state, oid) == BEAR_L and \
                                o.get("zone") == "Battlefield":
                            if str(oid) in blob:
                                tgt = "Grizzly Bears"
                    OBS["beast_target_name"] = tgt
                    say(f"Beast Mode on stack (target hint: {tgt})")
                    wire("beast_on_stack", {"target_hint": tgt})
            if hill_trigger_on_stack(state):
                if not OBS["hill_trigger_stack_seen"]:
                    OBS["hill_trigger_stack_seen"] = True
                    say("Hill trigger on stack")
                    wire("hill_trigger_on_stack", {})
            if OBS["beast_stack_seen"] and not beast_on_stack(state) \
                    and not hill_trigger_on_stack(state) \
                    and stack_empty(state):
                say("stack empty after Beast Mode + Hill trigger; "
                    "exporting post_teamwork")
                wire("stack_emptied_teamwork",
                     {"teamwork_pay_subs": OBS["teamwork_pay_subs"],
                      "tap_select_subs": OBS["tap_select_subs"]})
                ST["post_hand"] = len(hand_ids(state, 0))
                await do_export(p0, "post_teamwork.json")
                ST["stage"] = "ATTACK"
                attack_t0 = time.time()
                say("=== stage -> ATTACK ===")
            elif teamwork_t0 and time.time() - teamwork_t0 > TEAMWORK_WATCHDOG:
                say("TEAMWORK watchdog fired; exporting post_teamwork and "
                    "stopping")
                wire("teamwork_watchdog", dict(OBS))
                obs["notes"].append("teamwork stage watchdog fired")
                try:
                    await do_export(p0, "post_teamwork.json")
                except Exception as e:
                    say(f"post_teamwork export failed: {e}")
                ST["stop"] = True
                continue

        # --- ATTACK watches
        if ST["stage"] == "ATTACK":
            if OBS["attack_declared"]:
                atk_turn = ST["attack_turn"]
                turn = state.get("turn_number")
                phase = state.get("phase")
                if turn == atk_turn and phase == "PostCombatMain":
                    say("PostCombatMain on attack turn; exporting "
                        "post_attack")
                    await do_export(p0, "post_attack.json")
                    ST["stop"] = True
                    continue
            elif attack_t0 and time.time() - attack_t0 > ATTACK_WATCHDOG:
                say("ATTACK watchdog fired (Hill never attacked); exporting "
                    "post_attack and stopping")
                wire("attack_watchdog", dict(OBS))
                obs["notes"].append("attack stage watchdog fired")
                try:
                    await do_export(p0, "post_attack.json")
                except Exception as e:
                    say(f"post_attack export failed: {e}")
                ST["stop"] = True
                continue

    say(f"loop ended: stage={ST['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": ST["stage"], **{k: OBS[k] for k in
                                               ("beast_casts",
                                                "teamwork_pay_subs",
                                                "tap_select_subs")}})

    # ------------------------------------------------------- assertions
    A = obs["assert"]

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre = load("pre_teamwork.json")
    post = load("post_teamwork.json")
    final = load("post_attack.json")

    # A1: setup
    try:
        hs = hill_snapshot(pre) if pre else None
        a1 = (pre is not None and hs is not None
              and not hs["tapped"] and hs["counters"] == 0
              and len(perm_oids(pre, 0, BEAR_L)) >= 1
              and find_hand_oid(pre, 0, BEAST_L) is not None
              and pre.get("phase") == "PreCombatMain"
              and pre.get("active_player") == 0)
        A["A1_setup_ok"] = "passed" if a1 else "failed"
        obs["notes"].append(
            f"A1: pre turn={pre.get('turn_number') if pre else None} "
            f"hill={hs} bears={len(perm_oids(pre, 0, BEAR_L)) if pre else None} "
            f"beast_in_hand={find_hand_oid(pre, 0, BEAST_L) is not None if pre else None}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"A1 eval error: {e!r}")

    # A2: teamwork cast completed
    try:
        bz = beast_zone(post) if post else []
        hill_post = hill_snapshot(post) if post else None
        a2 = (OBS["beast_casts"] >= 1 and OBS["teamwork_pay_subs"] >= 1
              and OBS["tap_select_subs"] >= 1
              and "Graveyard" in bz
              and hill_post is not None and hill_post["tapped"])
        A["A2_teamwork_cast"] = "passed" if a2 else "failed"
        obs["notes"].append(
            f"A2: beast_casts={OBS['beast_casts']} "
            f"teamwork_pay={OBS['teamwork_pay_subs']} "
            f"tap_hill={OBS['tap_select_subs']} beast_zones={bz} "
            f"hill_tapped_post={hill_post['tapped'] if hill_post else None} "
            f"beast_target_hint={OBS['beast_target_name']}")
    except Exception as e:
        A["A2_teamwork_cast"] = "not-run"
        obs["notes"].append(f"A2 eval error: {e!r}")

    # A3: teamwork trigger fired correctly
    try:
        hill_post = hill_snapshot(post) if post else None
        hand_ok = (ST["pre_hand"] is not None and ST["post_hand"] is not None
                   and ST["post_hand"] == ST["pre_hand"])
        a3 = (hill_post is not None and hill_post["counters"] == 1
              and hand_ok)
        A["A3_teamwork_trigger_ok"] = "passed" if a3 else "failed"
        obs["notes"].append(
            f"A3: hill_counters_post={hill_post['counters'] if hill_post else None} "
            f"hand pre={ST['pre_hand']} post={ST['post_hand']}")
    except Exception as e:
        A["A3_teamwork_trigger_ok"] = "not-run"
        obs["notes"].append(f"A3 eval error: {e!r}")

    # A4: attack tap must NOT fire the trigger
    try:
        hill_final = hill_snapshot(final) if final else None
        hill_post = hill_snapshot(post) if post else None
        power = (hill_post or {}).get("power") or 3
        hand_final = len(hand_ids(final, 0)) if final else None
        hand_ok = (ST["post_hand"] is not None and hand_final is not None
                   and hand_final == ST["post_hand"] + 1)
        life_ok = (life_of(final, 1) == 20 - power) if final else False
        a4 = (hill_final is not None and hill_final["counters"] == 1
              and hand_ok and life_ok)
        A["A4_attack_no_trigger"] = "passed" if a4 else "failed"
        obs["notes"].append(
            f"A4: hill_counters_final={hill_final['counters'] if hill_final else None} "
            f"hand post={ST['post_hand']} final={hand_final} "
            f"p1_life={life_of(final, 1) if final else None} "
            f"expected={20 - power} (hill power {power})")
    except Exception as e:
        A["A4_attack_no_trigger"] = "not-run"
        obs["notes"].append(f"A4 eval error: {e!r}")

    # A5: cleanup
    try:
        if final is None:
            A["A5_cleanup"] = "failed"
            obs["notes"].append("post_attack.json missing")
        else:
            game_over = str(final.get("phase") or "").lower() == "gameover"
            ok = stack_empty(final) and not game_over
            A["A5_cleanup"] = "passed" if ok else "failed"
            obs["notes"].append(f"A5: stack_empty={stack_empty(final)} "
                                f"game_over={game_over} "
                                f"turn={final.get('turn_number')}")
    except Exception as e:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append(f"A5 eval error: {e!r}")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    a1 = A.get("A1_setup_ok")
    a2 = A.get("A2_teamwork_cast")
    a3 = A.get("A3_teamwork_trigger_ok")
    a4 = A.get("A4_attack_no_trigger")
    if a1 == "passed":
        if a2 == "failed" and not OBS["teamwork_offered"]:
            verdict = "blocked"
            obs["notes"].append("teamwork optional cost never offered; "
                                "trigger path could not be exercised")
        elif a2 == "passed" and a3 == "failed":
            verdict = "reproduced"
        elif a1 == "passed" and a2 == "passed" and a3 == "passed" \
                and a4 == "failed":
            verdict = "reproduced"
        elif all(A.get(k) == "passed" for k in
                 ("A1_setup_ok", "A2_teamwork_cast",
                  "A3_teamwork_trigger_ok", "A4_attack_no_trigger",
                  "A5_cleanup")):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "obs": {k: v for k, v in OBS.items()
                           if k != "rejections"},
                   "st": {k: v for k, v in ST.items()
                          if k != "opp_shapes_logged"}}, f, indent=2,
                  default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "observations": OBS,
                   "rejections": OBS.get("rejections", [])}, f, indent=2,
                  default=str)

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
            "P0": [[HILL_T, 4], [BEAST_T, 8], [BEAR_T, 8],
                   [PLAINS_T, 15], [FOREST_T, 15]],
            "P1": [[PLAINS_T, 40]],
        },
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6876.py (protocol 69 / v0.80.0), "
            "re-pinned from scenario_6876_01020.py (v0.102.0) to v0.103.0; "
            "behavioral contract A1..A5 and verdict logic unchanged.",
            "waiting_for is gone (null); priority = top-level PassPriority; "
            "all decisions via viewer_interaction; MulliganDecision via "
            "legacy Action; bottom via vi schema/select gated on "
            "waitingForKind.code=='mulligan'; DiscardToHandSize via vi "
            "schema/select.",
            "CastSpell via legacy Action on P0 main/priority; the v0.103.0 "
            "engine auto-taps reliably, so manual mana taps engage only as "
            "a fallback if a cast sits >90s with a tap menu visible.",
            "Beast Mode prompts answered generically: target selection -> "
            "the Bears (schema select/sequence or exactChoices); "
            "decideOptionalCost teamwork -> pay (bare true/false "
            "role=accept surfaces); TapCreatures payment -> Hill ONLY.",
            "DeclareAttackers via relations-schema vi opportunity answered "
            "first (Hill alone vs P1); the vi fallback only answers "
            "relations-schema opportunities (select/sequence are decisions, "
            "never declares). P1 declares no blockers.",
            "real_decision_pending excludes the 106 priority-menu codes; "
            "playLand vi codes answered before the decision gate; land "
            "matching via is_land().",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope); the reported OUTCOME is "
            "asserted on the saved states, not the prompt.",
            "Data-level: v0.103.0 parses Hill's trigger as mode=Taps with "
            "no structured teamwork qualifier (constraint/condition/origin "
            "all empty) -- the qualifier survives only in the description "
            "string, which is consistent with the runtime firing on any "
            "tap (area:parser).",
        ],
        "assertions": A,
        "notes": obs["notes"],
        "verdict": verdict,
        "evidence_comment_id": 5643039882,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "4x Hill / 8x Beast Mode / 8x Grizzly Bears density is a "
            "test-harness convenience (engine accepts >4-of for custom "
            "games).",
            "States are authoritative exports, restorable only via full "
            "game replay (scenario_6876_01030.py), not direct load.",
        ],
        "setup_line": "P0: 4x Agent Maria Hill + 8x Beast Mode + 8x Grizzly "
                      "Bears + 15x Plains + 15x Forest; P1: 40x Plains "
                      "(passive)",
        "contract_line": ("Cast Beast Mode with teamwork paid by tapping "
                          "only Hill: her trigger must put exactly one "
                          "+1/+1 counter and draw one card. Attacking with "
                          "Hill (a non-teamwork tap) must not fire it."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")
    sys.stdout.flush()

    # Finalize evidence BEFORE closing clients. Each step is guarded so a
    # failure is logged with a traceback instead of silently dropping the
    # rest.
    try:
        render_summary(run, f"{EVDIR}/summary.png")
    except Exception:
        say("render_summary FAILED:")
        say(traceback.format_exc())
    try:
        shutil.copy(__file__, f"{EVDIR}/scenario_6876_01030.py")
        say("copied driver into evidence dir")
    except Exception:
        say("scenario copy FAILED:")
        say(traceback.format_exc())
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        write_manifest()
    except Exception:
        say("write_manifest FAILED:")
        say(traceback.format_exc())
    try:
        RUNLOG.close()
    except Exception:
        pass

    try:
        await asyncio.wait_for(p0.close(), 15)
    except Exception as e:
        print(f"p0.close failed: {e}", flush=True)
    try:
        await asyncio.wait_for(p1.close(), 15)
    except Exception as e:
        print(f"p1.close failed: {e}", flush=True)
    return run


def load_ev_state(path):
    with open(f"{EVDIR}/{path}") as f:
        return json.load(f)["state"]


def render_summary(run, out_path):
    """Render the PNG from the saved evidence files only."""
    from PIL import Image, ImageDraw
    A = json.load(open(f"{EVDIR}/assertions.json"))
    notes = A.get("notes", [])
    asserts = A.get("assertions", {})
    si = run["server_identity"]
    try:
        pre = load_ev_state("pre_teamwork.json")
        hs = hill_snapshot(pre)
        pre_line = (f"pre_teamwork: turn {pre.get('turn_number')} "
                    f"{pre.get('phase')} | Hill tapped={hs['tapped']} "
                    f"counters={hs['counters']} | hand={ST['pre_hand']}")
    except Exception:
        pre_line = "pre_teamwork.json: missing"
    try:
        post = load_ev_state("post_teamwork.json")
        hs = hill_snapshot(post)
        post_line = (f"post_teamwork: Hill counters={hs['counters']} "
                     f"tapped={hs['tapped']} | hand {ST['pre_hand']}->"
                     f"{ST['post_hand']} | beast zones={beast_zone(post)}")
    except Exception:
        post_line = "post_teamwork.json: missing"
    try:
        final = load_ev_state("post_attack.json")
        hs = hill_snapshot(final)
        fin_line = (f"post_attack: turn {final.get('turn_number')} "
                    f"{final.get('phase')} | Hill counters={hs['counters']} "
                    f"| P1 life={life_of(final, 1)} "
                    f"| stack_empty={stack_empty(final)}")
    except Exception:
        fin_line = "post_attack.json: missing"
    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    y = 20
    d.text((24, y), "Issue #6876 - Agent Maria Hill gets counters on any tap "
                    "(v0.103.0 revalidation)", fill=(235, 240, 250))
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
    y += 24
    d.text((24, y), fin_line, fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Hill untapped 0 counters, Bears BF, "
                       "Beast in hand, P0 main",
        "A2_teamwork_cast": "A2 Beast Mode cast, teamwork paid (Hill only), "
                            "resolved to graveyard",
        "A3_teamwork_trigger_ok": "A3 teamwork tap: Hill exactly 1 counter, "
                                  "hand net 0 (draw)",
        "A4_attack_no_trigger": "A4 attack tap: Hill still 1 counter, hand "
                                "+1 (turn draw), P1 damaged",
        "A5_cleanup": "A5 stack empty, game continues",
    }
    for k, lab in labels.items():
        v = asserts.get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y),
               f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in notes[:9]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6876/" +
           run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


def write_manifest():
    # The "manifest written" log line must land in scenario_run.log BEFORE
    # its hash is recorded; RUNLOG.close() afterwards adds no more bytes.
    names = [n for n in sorted(os.listdir(EVDIR))
             if n != "manifest.sha256"
             and os.path.isfile(os.path.join(EVDIR, n))]
    say(f"manifest written ({len(names)} files)")
    lines = []
    for name in names:
        p = os.path.join(EVDIR, name)
        lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
