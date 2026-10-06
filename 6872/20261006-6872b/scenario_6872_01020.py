#!/usr/bin/env python3
"""Issue #6872 re-validation on pinned v0.102.0 (protocol 106):
The Dominion Bracelet does not give its granted activated ability to the
equipped creature.

Protocol-106 port of driver/scenario_6872_0980.py (protocol 94 / v0.98.0).
Run id for this port: 20261006-6872b (supersedes the 20261006-6872 run,
whose pre_grant export fired on P1's turn and whose grant probe ran only
once).

Oracle (pinned v0.102.0 card-data, "the dominion bracelet"):
"Equipped creature gets +1/+1 and has "{15}, Exile The Dominion Bracelet:
You control target opponent during their next turn. This ability costs {X}
less to activate, where X is this creature's power. Activate only as a
sorcery.""
Pinned v0.102.0 data fully parses this: a Continuous static ability with
modifications AddPower(1), AddToughness(1), GrantAbility{definition:
Activated, effect ControlNextTurn(Opponent), cost Composite[{15},
Exile(GrantingObject)], activation_restrictions [AsSorcery]}. The 2026-10-01
v0.98.0 run (20261001-6872, protocol 94) found REPRODUCED: the grant is
installed on Yargle's object (one ability carrying the full granted
definition) but zero ActivateAbility actions are ever advertised for the
equipped creature -- the grant never reaches the action-advertisement
layer.

Plan (two human seats, native engine, v0.102.0 / protocol 106):
  SETUP - land drops; P0 casts Yargle, Glutton of Urborg (4B, 9/3), then
          The Dominion Bracelet ({2}), then activates Equip {1} targeting
          Yargle (auto-target or the advertised target prompt).
  PROOF - on a fresh P0 PreCombatMain after the attach turn (5+ untapped
          Swamps): export pre_grant.json; probe ALL ActivateAbility
          actions sourced from the equipped Yargle. Yargle has no native
          abilities, so any advertised ActivateAbility on it is the
          granted one.
  ACT   - if advertised: submit as-is; pay the {15}-minus-power mana via
          advertised PayMana actions / the vi tapLandForMana menu; answer
          the exile-cost prompt (choose the Bracelet) and the opponent
          target prompt (choose P1's seat) from the advertised
          opportunities; watch for rejections. Export mid_grant.json with
          the ability on the stack; let it resolve; export
          post_activate.json.
  OBSERVE - advance to P1's next turn and record whether P0 is offered
          P1's decisions (control_effect). Export post.json.

Behavioral contract:
  A1 setup_ok            Bracelet attached to Yargle (attached_to nested
                         dict), Yargle 10/4, fresh P0 main
  A2 granted_exposed     ActivateAbility advertised on equipped Yargle
  A3 activation_completes ability resolved, Bracelet in Exile zone, no
                         rejection of the activation path
  A4 control_recorded    lasting control-next-turn effect registration for
                         P1 found in post states (outside ability-definition
                         blobs), or P0 observed making P1's decisions on
                         P1's next turn
  A5 cleanup             stack empty, game not over

Verdict: reproduced iff A1 passes and (A2 fails, or A2 passes but A3/A4
fail with a related failure of the same granted ability). not-reproduced
iff A1..A4 pass. Never "fixed".

Protocol-106 driver notes (v0.102.0, per scenario_6866_01020.py and
scenario_6868_01020.py):
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction
    (vi.waitingForKind.code exposes the decision kind, e.g. 'mulligan').
  - MulliganDecision answered via legacy Action, gated on the advertised
    MulliganDecision action. Bottom-after-mulligan via the vi
    schema/select opportunity, gated on waitingForKind.code == 'mulligan'
    at turn 1 / Untap. DiscardToHandSize via vi schema/select
    opportunity offering hand cards.
  - CastSpell/ActivateAbility via legacy Action (top-level merged with
    legal_actions_by_object); ActivateAbility submitted only while
    holding priority. Mana via legacy PayMana actions first, then the vi
    tapLandForMana menu driven by MANA_NEEDS. sleep(0) yield before leg
    evaluation; 5s re-tick backstop for priority-holding clients.
  - real_decision_pending excludes the noisy 106 priority-menu codes:
    passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand are NOT
    decisions; playLand vi codes are answered before the decision gate
    and land-play matching uses is_land() over every land the seats can
    hold.
  - DeclareAttackers/Blockers: relations-schema vi opportunity answered
    first (P1 blockers get a 25s grace window before the advertised empty
    submit); the fallback only answers relations-schema opportunities --
    select/sequence schemas are decisions, never declares.
  - surf_codes() filters None codes; engine-issued interactionIds echoed
    back; seat-1 target chosen for the opponent prompt, Bracelet ref for
    the exile-cost prompt.
  - Export: {"type":"ExportAuthoritativeState"}; the response data.state
    is a JSON string parsed once to {"state": {}, ...}; pre/post states
    are authoritative exports.
  - Assertions test the reported OUTCOME (grant advertised / activation /
    control effect), not the prompt.
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
ISSUE = 6872
RUN_ID = "20261006-6872b"
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
# Exact digests from validation-ledger.json "server" (pinned v0.102.0).
SERVER_IDENTITY = {
    "validated_version": "v0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "server_binary_sha256":
        "5b79f0c520e11ad1df158f72c1674a43378ff9c999b57199789a531f6d125aa8",
    "card_data_sha256":
        "eb87edbd0c90e2440fdb97404e4a40fb86ca9770439f9c045fc9d0f220605b36",
    "draft_pools_sha256":
        "e80b16721bf3da5b28580f29bbe94b8df43f02363e66647cc004bb53888040e3",
    "signature_verified": True,
}

for _f, _k in (
        ("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.102.0/data/draft-pools.json",
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
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/"
                           "data/card-data.json"))


def check_data_level():
    """Record the v0.102.0 parse of the Bracelet's granted ability
    (data-level triage): the bug is about runtime exposure, so the data
    parse must still carry the full grant definition."""
    b = CARD_DATA["the dominion bracelet"]
    grant = None
    for sa in b.get("static_abilities", []) or []:
        for m in sa.get("modifications", []) or []:
            if m.get("type") == "GrantAbility":
                grant = m.get("definition") or {}
    assert grant, "no GrantAbility modification in v0.102.0 card data"
    eff = grant.get("effect") or {}
    cost = grant.get("cost") or {}
    costs = cost.get("costs") if cost.get("type") == "Composite" else [cost]
    mana = next((c for c in costs if c.get("type") == "Mana"), {})
    exile = next((c for c in costs if c.get("type") == "Exile"), {})
    red = grant.get("cost_reduction") or {}
    restr = [r.get("type") for r in
             grant.get("activation_restrictions") or []]
    ev = {
        "name": b.get("name"),
        "oracle_text": b.get("oracle_text"),
        "grant_kind": grant.get("kind"),
        "grant_description": grant.get("description"),
        "grant_effect_type": eff.get("type"),
        "grant_effect_target_controller":
            ((eff.get("target") or {}).get("controller")),
        "grant_mana_generic": ((mana.get("cost") or {}).get("generic")),
        "grant_exile": {"type": exile.get("type"),
                        "filter": (exile.get("filter") or {}).get("type")},
        "grant_cost_reduction": red,
        "grant_activation_restrictions": restr,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": b.get("name"),
                   "oracle_text": b.get("oracle_text"),
                   "static_abilities": b.get("static_abilities")}, f, indent=1)
    say(f"data-level: grant kind={ev['grant_kind']} "
        f"effect={ev['grant_effect_type']} mana_generic="
        f"{ev['grant_mana_generic']} exile_filter="
        f"{ev['grant_exile']['filter']} restrictions={restr}")
    assert ev["grant_effect_type"] == "ControlNextTurn", \
        "grant effect no longer ControlNextTurn"
    assert ev["grant_mana_generic"] == 15, "grant mana cost changed"
    assert ev["grant_exile"]["filter"] == "GrantingObject", \
        "grant exile filter changed"
    assert "AsSorcery" in restr, "AsSorcery restriction missing"
    assert "control target opponent" in \
        (ev["grant_description"] or "").lower(), \
        "grant description changed"


YARGLE_L = "yargle, glutton of urborg"
BRACELET_L = "the dominion bracelet"
SWAMP_L = "swamp"

YARGLE_T = "Yargle, Glutton of Urborg"
BRACELET_T = "The Dominion Bracelet"
SWAMP_T = "Swamp"

P0_DECK = ((YARGLE_T, 8), (BRACELET_T, 4), (SWAMP_T, 48))
P1_DECK = ((SWAMP_T, 60),)

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {}            # {"equip": {...}, "equip_targeted": {...},
                    #  "grant": {...}}
TGT = {"grant": None}
OBS = {"grant_prompts_answered": []}
IDS = {"grant_ability": None}
SEEN_TURN = {}      # "yargle" -> turn on BF, "attached" -> attach turn
GRANT_WATCH = {"since": None}
PROOF_WATCH = {"since": None}
CONTROL_OBS = {"since": None, "turn": None, "snaps": []}


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


def perm_oids(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_swamps(state, pid):
    return sum(1 for oid in bf_oids(state, pid)
               if obj_lname(state, oid) == SWAMP_L
               and not get_obj(state, oid).get("tapped"))


def life_of(state, pid):
    return player_of(state, pid).get("life")


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


def find_int_in(obj, target):
    """Recursive int search (attached_to is a nested dict)."""
    if isinstance(obj, bool):
        return False
    if isinstance(obj, int):
        return obj == target
    if isinstance(obj, dict):
        return any(find_int_in(v, target) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(find_int_in(v, target) for v in obj)
    return False


def attached_oid(state, equip_oid, creature_oid):
    o = get_obj(state, equip_oid)
    att = o.get("attached_to")
    if att is None:
        return False
    return find_int_in(att, int(creature_oid))


def candidate_info(opp, state):
    """Choice-level detail for target prompts (reference oid, seat, name,
    zone, controller)."""
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
                try:
                    seat = int(d["seat"])
                except (TypeError, ValueError):
                    seat = d["seat"]
        if seat is None:
            seat = candidate_seat(ch)
        o = get_obj(state, ref) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller")})
    return out


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


def activate_options(state, acts, lname, want_index):
    """ActivateAbility actions sourced from the named permanent.
    Engine-issued actions preferred; ability_index used when advertised
    (fallback: listing order, wire-logged)."""
    srcs = set(perm_oids(state, 0, lname))
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
            wire("ability_index_absent",
                 {"want_index": want_index, "n_cands": len(cands)})
            if want_index < len(cands):
                return [cands[want_index]]
    return cands


def grant_options(state, acts, yargle_oid):
    """All ActivateAbility actions sourced from Yargle. Yargle has no
    native abilities, so any of these is the granted one."""
    out = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", ""))) \
            or str(a.get("_src_oid", ""))
        if src == str(yargle_oid):
            out.append(a)
    return out

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
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = ST["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h == SWAMP_L)
    if tag == "P0":
        has_piece = YARGLE_L in hand or BRACELET_L in hand
        choice = "Keep" if (n_lands >= 2 and (has_piece or n >= 2)) \
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
    Untap. Never bottoms Yargle or the Bracelet for P0."""
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

        keep = {YARGLE_L, BRACELET_L} if tag == "P0" else set()

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
    if ln in (YARGLE_L, BRACELET_L):
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


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec", {}) or {}
        if spec.get("type") == "relations":
            return opp
    return None


async def do_declare_empty(c, acts, st, pid, tag, attackers_ok=True):
    """Declare empty attackers/blockers. The vi fallback only answers
    relations-schema declare opportunities (select/sequence schemas are
    decisions like target selection, never declares). For P1 blockers, a
    25s grace window lets the relations opportunity arrive before the
    advertised empty submit."""
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
    # 106 fallback: relations-schema vi opportunity, then blind submit
    # (declare-phase gated; attackers only when attackers_ok)
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
    # arm the P1 blockers grace window while attackers exist
    if tag == "P1" and state.get("active_player") != pid \
            and "declareblock" in phase.lower():
        if find_relations_op(st) is None:
            ST["p1_block_grace_until"] = time.time() + 25
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


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    """Tap one land via the 106 tapLandForMana menu. One tap per call."""
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
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
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

# ------------------------------------------------------------- #6872 logic


def stack_has_yargle_spell(state):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if YARGLE_T in blob:
            return True
    return False


async def answer_equip_target(c, state, st):
    """Equip target prompt: choose Yargle (battlefield candidate). Handles
    both the schema target opportunity and the auto-target case (single
    legal target, engine attaches with no prompt -- detected via
    attached_oid in the main loop)."""
    if not ACT.get("equip") or ACT.get("equip_targeted"):
        return False
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    iid = opp.get("interactionId") or opp.get("id")
    if not iid or iid in SUBMITTED_OPPS:
        return False
    cands = candidate_info(opp, state)
    if not any(x["zone"] == "Battlefield" for x in cands):
        return False
    want = next((x for x in cands if x["name"].lower() == YARGLE_L), None)
    if not want:
        return False
    wire("equip_target_prompt",
         {"rtype": rtype, "spec_type": stype, "candidates": cands})
    say(f"[P0] equip target prompt: "
        f"{[(x['name'], x['ref']) for x in cands]}")
    await answer_vi(c, opp, {"id": want["choice_id"]}, "P0")
    SUBMITTED_OPPS.add(iid)
    ACT["equip_targeted"] = {"at": time.time(), "ref": want["ref"]}
    say(f"[P0] equip targets Yargle (oid={want['ref']})")
    return True


async def answer_grant_target(c, state, st):
    """Granted-ability follow-up prompts for P0: (a) the opponent target ->
    choose P1's seat; (b) the exile-cost candidate -> choose the Bracelet.
    Only engine-advertised choiceIds are used. Stays active until the grant
    ability is observed on the stack."""
    if not ACT.get("grant") or IDS.get("grant_ability"):
        return False
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    iid = opp.get("interactionId") or opp.get("id")
    if not iid or iid in SUBMITTED_OPPS:
        return False
    cands = candidate_info(opp, state)
    brace_oids = set(perm_oids(state, 0, BRACELET_L))
    want = next((x for x in cands if x["seat"] == 1), None)
    kind = "opponent"
    if not want:
        want = next((x for x in cands if x["ref"] in brace_oids), None)
        kind = "exile_cost"
    if not want:
        return False
    wire("grant_target_prompt",
         {"kind": kind, "rtype": rtype, "spec_type": stype,
          "candidates": cands})
    say(f"[P0] grant {kind} prompt: "
        f"{[(x['seat'], x['name'], x['ref']) for x in cands]}")
    await answer_vi(c, opp, {"id": want["choice_id"]}, "P0")
    SUBMITTED_OPPS.add(iid)
    OBS["grant_prompts_answered"].append({"kind": kind, "at": time.time()})
    if kind == "opponent":
        TGT["grant"] = {"at": time.time(), "seat": 1}
        say("[P0] grant ability targets P1")
    else:
        say("[P0] grant exile-cost chooses the Bracelet")
    return True


async def p0_setup(c, pid, state, acts):
    # cast Yargle first (4B), then Bracelet ({2}), then equip ({1})
    yoids = perm_oids(state, pid, YARGLE_L)
    boids = perm_oids(state, pid, BRACELET_L)
    if not yoids and not stack_has_yargle_spell(state):
        oid = next((o for o in hand_ids(state, pid)
                    if obj_lname(state, o) == YARGLE_L), None)
        a, _ = find_cast_action(acts, state, YARGLE_L)
        if a and oid and untapped_swamps(state, pid) >= 5:
            MANA_NEEDS["P0"] = {"B": 5}
            await submit_as_is(c, a)
            say(f"P0 casts Yargle, Glutton of Urborg (oid {oid})")
            wire("cast_yargle", {"oid": oid})
            return True
    if yoids and not boids:
        a, oid = find_cast_action(acts, state, BRACELET_L)
        if a and oid and untapped_swamps(state, pid) >= 2:
            MANA_NEEDS["P0"] = {"B": 2}
            await submit_as_is(c, a)
            say(f"P0 casts The Dominion Bracelet (oid {oid})")
            wire("cast_bracelet", {"oid": oid})
            return True
    if yoids and boids and not ACT.get("equip"):
        opts = activate_options(state, acts, BRACELET_L, 0)
        wire("equip_options", {"count": len(opts),
                               "all_action_types":
                               sorted({a["type"] for a in acts})})
        say(f"[P0] equip options: {len(opts)}")
        if not opts:
            return False
        # ActivateAbility MUST be submitted while holding priority.
        if not my_priority(acts):
            return False
        MANA_NEEDS["P0"] = {"B": 1}
        ACT["equip"] = {"at": time.time(),
                        "turn": state.get("turn_number")}
        await submit_as_is(c, opts[0])
        say("P0 activates Equip {1}")
        wire("equip_activated", {"turn": state.get("turn_number")})
        return True
    # retry fallback: equip was activated but never attached (fizzle or
    # silent reject) -> clear and try again on a later main
    if ACT.get("equip") and "attached" not in SEEN_TURN:
        boids = perm_oids(state, pid, BRACELET_L)
        yoids = perm_oids(state, pid, YARGLE_L)
        still_loose = [b for b in boids
                       if not any(attached_oid(state, b, y) for y in yoids)]
        if still_loose and state.get("turn_number", 0) > \
                ACT["equip"].get("turn", 0) + 1:
            say("P0 equip never attached; retrying equip")
            wire("equip_retry", {"turn": state.get("turn_number")})
            ACT.pop("equip", None)
            ACT.pop("equip_targeted", None)
    return False


def grant_desc_match(desc):
    return "control target opponent" in (desc or "").lower()


async def p0_proof(c, pid, state, acts):
    turn = state.get("turn_number")
    if "attached" not in SEEN_TURN or turn <= SEEN_TURN["attached"]:
        return False
    # The proof (export, probe, activation) only makes sense on P0's own
    # main phase: AsSorcery gates the activation, and the pre_grant export
    # must capture a fresh P0 main for A1.
    if not my_main(state, pid):
        return False
    yoids = perm_oids(state, pid, YARGLE_L)
    if not yoids:
        return False
    yoid = yoids[0]
    if not OBS.get("pre_exported"):
        if untapped_swamps(state, pid) < 5:
            return False
        await do_export(c, "pre_grant.json")
        OBS["pre_exported"] = {"at": time.time(), "turn": turn,
                               "untapped_swamps": untapped_swamps(state, pid)}
        wire("pre_grant_yargle_object",
             {"oid": yoid, "object": get_obj(state, yoid)})
        say(f"[P0] pre_grant exported (turn {turn}); Yargle oid={yoid}")
        return True
    # probe on each fresh P0 main (turn-gated re-probe): the granted
    # ability must be advertised as an activatable action whenever P0
    # could legally activate it, so a single snapshot is not enough.
    if not ACT.get("grant") and OBS.get("grant_probe_turn") != turn:
        OBS["grant_probe_turn"] = turn
        opts = grant_options(state, acts, yoid)
        yo = get_obj(state, yoid)
        obj_abilities = yo.get("abilities") or []
        wire("grant_probe",
             {"yargle_oid": yoid, "turn": turn,
              "advertised_count": len(opts),
              "object_abilities": obj_abilities,
              "all_action_types": sorted({a["type"] for a in acts})})
        say(f"[P0] grant probe (turn {turn}): advertised ActivateAbility "
            f"on Yargle = {len(opts)}; object abilities = "
            f"{len(obj_abilities)}")
        for ab in obj_abilities:
            say(f"    obj ability: {(ab.get('description') or '')[:100]}")
        OBS["grant_probe"] = {
            "at": time.time(), "turn": turn,
            "advertised_count": len(opts),
            "object_ability_descriptions":
                [(ab.get("description") or "")[:120]
                 for ab in obj_abilities],
            "object_ability_kinds":
                [ab.get("kind") for ab in obj_abilities],
        }
        if opts:
            # Yargle has no native abilities; the advertised one is the
            # granted ability. ActivateAbility MUST be submitted while
            # holding priority.
            OBS["grant_ever_advertised"] = True
            if not my_priority(acts):
                return False
            # {15} reduced by Yargle's power (10) -> {5} + exile Bracelet
            MANA_NEEDS["P0"] = {"generic": 5}
            ACT["grant"] = {"at": time.time(), "turn": turn}
            wire("grant_activated", {})
            await submit_as_is(c, opts[0])
            say("[P0] activates granted ability on equipped Yargle")
            return True
        # nothing advertised: record; the stuck watch converts this to a
        # captured failure
        if GRANT_WATCH["since"] is None:
            GRANT_WATCH["since"] = time.time()
            OBS["grant_not_offered"] = {
                "at": time.time(), "turn": turn,
                "all_action_types": sorted({a["type"] for a in acts}),
                "object_ability_descriptions":
                    OBS["grant_probe"]["object_ability_descriptions"]}
            wire("grant_not_offered", OBS["grant_not_offered"])
            say("[P0] granted ability NOT advertised on equipped Yargle")
        return False
    return False


async def p1_step(c, pid, state, acts):
    # P1 is passive draw-go: land drop only.
    if state.get("active_player") == pid and state.get("phase") in (
            "PreCombatMain", "PostCombatMain", "Main") and stack_empty(state):
        if await play_a_land(c, state, pid, acts, "P1"):
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
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True
    if await pay_tick(c, acts):
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

    # answer prompts before anything else
    if await answer_equip_target(c, state, st):
        return True
    if await answer_grant_target(c, state, st):
        return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, "P0"):
            return True
        if ST["stage"] == "SETUP":
            if await p0_setup(c, 0, state, acts):
                return True
        elif ST["stage"] == "PROOF":
            if await p0_proof(c, 0, state, acts):
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
    if await pay_tick(c, acts):
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


def strip_ability_defs(obj):
    # Strip every card-definition blob: static abilities live under
    # static_definitions / base_static_definitions on objects (and
    # static_abilities / deck_pools card records), so stripping only
    # "abilities" leaves ControlNextTurn definition tags behind.
    if isinstance(obj, dict):
        return {k: strip_ability_defs(v) for k, v in obj.items()
                if k not in ("abilities", "static_abilities",
                             "static_definitions",
                             "base_static_definitions", "ability",
                             "deck_pools", "definition")}
    if isinstance(obj, (list, tuple)):
        return [strip_ability_defs(v) for v in obj]
    return obj


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06872")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16872")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
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
                await do_export(p0, "post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True
            continue

        # --- stage transitions
        if ST["stage"] == "SETUP":
            yoids = perm_oids(state, 0, YARGLE_L)
            if yoids and "yargle" not in SEEN_TURN:
                SEEN_TURN["yargle"] = state.get("turn_number")
                say(f"Yargle on BF turn {SEEN_TURN['yargle']}")
            if yoids and "attached" not in SEEN_TURN:
                boids = perm_oids(state, 0, BRACELET_L)
                for b in boids:
                    if attached_oid(state, b, yoids[0]):
                        if not ACT.get("equip_targeted"):
                            # auto-target case: single legal target, the
                            # engine attaches with no TargetSelection prompt
                            ACT["equip_targeted"] = {"at": time.time(),
                                                     "auto": True,
                                                     "ref": yoids[0]}
                            say("[P0] equip auto-targeted Yargle (no prompt)")
                        SEEN_TURN["attached"] = state.get("turn_number")
                        say(f"Bracelet attached to Yargle turn "
                            f"{SEEN_TURN['attached']}")
                        ST["stage"] = "PROOF"
                        say("=== stage -> PROOF ===")
                        break

        # --- grant ability on the stack?
        if ACT.get("grant") and not IDS["grant_ability"]:
            for sid in stack_ids(state):
                e = stack_entry(state, sid)
                blob = json.dumps(e, default=str)
                if "control target opponent" in blob.lower() or \
                        "ControlNextTurn" in blob:
                    IDS["grant_ability"] = sid
                    wire("grant_ability_on_stack", {"sid": sid, "entry": e})
                    say(f"grant ability on stack: sid={sid}")
                    await do_export(p0, "mid_grant.json")
                    break

        # --- grant ability resolved?
        if IDS["grant_ability"] and ST["stage"] == "PROOF":
            if IDS["grant_ability"] not in stack_ids(state):
                OBS["grant_resolved_at"] = time.time()
                say("grant ability resolved (left the stack)")
                await do_export(p0, "post_activate.json")
                ST["stage"] = "CONTROL_OBSERVE"
                CONTROL_OBS["turn"] = state.get("turn_number")
                CONTROL_OBS["since"] = time.time()
                say("=== stage -> CONTROL_OBSERVE ===")

        # --- CONTROL_OBSERVE: watch P1's next turn for P0 decisions
        if ST["stage"] == "CONTROL_OBSERVE" and not ST["stop"]:
            p1_turn = (CONTROL_OBS["turn"] or 0) + 1
            if state.get("active_player") == 1 and \
                    state.get("turn_number") == p1_turn:
                st1 = st_of(p1)
                st0 = st_of(p0)
                snap = {
                    "turn": state.get("turn_number"),
                    "phase": state.get("phase"),
                    "p0_vi_kind": vi_kind_code(st0),
                    "p0_vi_canSubmit": bool(
                        (st0.get("viewer_interaction") or {})
                        .get("canSubmit")),
                    "p0_opp": len(vi_ops(st0)),
                    "p0_real_decision": real_decision_pending(st0),
                    "p1_vi_canSubmit": bool(
                        (st1.get("viewer_interaction") or {})
                        .get("canSubmit")),
                    "p1_opp": len(vi_ops(st1)),
                }
                key = (snap["turn"], snap["phase"], snap["p0_vi_kind"],
                       snap["p0_vi_canSubmit"], snap["p0_real_decision"],
                       snap["p1_vi_canSubmit"])
                if ("ctrlsnap", key) not in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.add(("ctrlsnap", key))
                    CONTROL_OBS["snaps"].append({**snap, "t": time.time()})
                    wire("control_observe", snap)
                # strong signal: P0 offered a REAL decision during P1's
                # turn (priority menus alone do not count)
                if snap["p0_real_decision"]:
                    OBS["p0_decides_for_p1"] = {
                        "vi_kind": snap["p0_vi_kind"],
                        "phase": state.get("phase"),
                        "at": time.time()}
                    wire("p0_decides_for_p1", OBS["p0_decides_for_p1"])
                    say(f"CONTROL SIGNAL: P0 deciding for P1 "
                        f"({snap['p0_vi_kind']} in {state.get('phase')})")
            # end conditions: P1's turn ended, or 150s elapsed
            if (state.get("active_player") == 0 and
                    state.get("turn_number") > p1_turn) or \
                    time.time() - CONTROL_OBS["since"] > 150:
                say("control observation window done; exporting post")
                try:
                    await do_export(p0, "post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # --- stuck watches in PROOF
        if ST["stage"] == "PROOF" and not ST["stop"]:
            since = GRANT_WATCH["since"]
            if since and time.time() - since > 90:
                say("granted ability never offered after 90s; exporting "
                    "post (captured failure)")
                wire("grant_probe_timeout", {})
                try:
                    await do_export(p0, "post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            psince = PROOF_WATCH["since"]
            if psince and time.time() - psince > 120:
                say("PROOF stalled 120s with no progress; exporting post")
                wire("proof_stall_timeout", {})
                try:
                    await do_export(p0, "post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            # arm the stall watch once pre_grant exists but nothing advances
            if OBS.get("pre_exported") and PROOF_WATCH["since"] is None \
                    and not ACT.get("grant") and GRANT_WATCH["since"] is None:
                PROOF_WATCH["since"] = time.time()
            if ACT.get("grant"):
                PROOF_WATCH["since"] = None
            # grant activation rejected outright
            if ACT.get("grant") and not IDS["grant_ability"]:
                rej_lines = [r for r in OBS.get("rejections", [])
                             if r["at"] >= ACT["grant"]["at"]]
                if rej_lines and time.time() - ACT["grant"]["at"] > 20:
                    say("grant activation rejected; exporting post")
                    wire("grant_rejected_stop",
                         {"count": len(rej_lines)})
                    OBS["grant_rejected"] = True
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
                f"stage={ST['stage']} attached={'attached' in SEEN_TURN} "
                f"grant_act={bool(ACT.get('grant'))} "
                f"untapped_swamps={untapped_swamps(s, 0)}")

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    A = obs["assert"]

    # A1: setup_ok from authoritative pre_grant.json
    pre = env_state("pre_grant.json")
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_grant.json missing (never reached proof)")
    else:
        yoids = perm_oids(pre, 0, YARGLE_L)
        boids = perm_oids(pre, 0, BRACELET_L)
        yo = get_obj(pre, yoids[0]) if yoids else {}
        attached = bool(yoids and boids and
                        attached_oid(pre, boids[0], yoids[0]))
        pw, tw = num(yo.get("power")), num(yo.get("toughness"))
        ok = attached and pw == 10 and tw == 4 and my_main(pre, 0)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        obs["notes"].append(f"A1: attached={attached} yargle={pw}/{tw} "
                            f"fresh_main={my_main(pre, 0)}")

    # A2: granted ability exposed as an activatable action on Yargle
    # (any probe across P0's mains counts; the probe re-runs every P0 main)
    probe = OBS.get("grant_probe") or {}
    advertised = probe.get("advertised_count") or 0
    ever = bool(OBS.get("grant_ever_advertised")) or advertised > 0
    descs = probe.get("object_ability_descriptions") or []
    desc_match = any(grant_desc_match(d) for d in descs)
    if ever:
        A["A2_granted_exposed"] = "passed"
        if not desc_match and descs:
            obs["notes"].append("A2: advertised actions present but object "
                                "descriptions did not match grant text "
                                "(treated as exposed; Yargle has no "
                                "natives)")
    else:
        A["A2_granted_exposed"] = "failed"
    obs["notes"].append(f"A2: ever_advertised={ever} "
                        f"last_advertised_count={advertised} "
                        f"probe_turns={OBS.get('grant_probe_turn')} "
                        f"obj_desc_match={desc_match} "
                        f"not_offered={bool(OBS.get('grant_not_offered'))}")

    # A3: activation completes (Bracelet exiled, stack clear, no rejection)
    try:
        post_a = env_state("post_activate.json")
        if post_a is None:
            raise FileNotFoundError("post_activate.json")
        brace_zones = [(oid, o.get("zone"))
                       for oid, o in post_a["objects"].items()
                       if obj_lname(post_a, oid) == BRACELET_L]
        exiled = any(z == "Exile" for _, z in brace_zones)
        stack_clear = IDS["grant_ability"] not in stack_ids(post_a)
        no_rej = not OBS.get("grant_rejected")
        ok = exiled and stack_clear and no_rej
        A["A3_activation_completes"] = "passed" if ok else "failed"
        obs["notes"].append(f"A3: bracelet_zones={brace_zones} "
                            f"stack_clear_of_grant={stack_clear} "
                            f"no_rejection={no_rej}")
    except FileNotFoundError:
        if A["A2_granted_exposed"] == "failed":
            A["A3_activation_completes"] = "not-run"
            obs["notes"].append("A3 not-run: granted ability never exposed")
        else:
            A["A3_activation_completes"] = "not-run"
            obs["notes"].append("A3 not-run: post_activate.json missing")

    # A4: control registration recorded outside ability-definition blobs,
    # or P0 observed deciding for P1 on P1's next turn.
    try:
        found = []
        sched_nonempty = False
        for path in ("post_activate.json", "post.json"):
            st2 = env_state(path)
            if st2 is None:
                continue
            if st2.get("scheduled_turn_controls"):
                sched_nonempty = True
                found.append((path, "scheduled_turn_controls"))
            raw2 = json.dumps(strip_ability_defs(st2), default=str)
            for marker in ("ControlNextTurn", "control_next_turn",
                           "controls_player", "controlled_by",
                           "ControlPlayer"):
                if marker in raw2:
                    found.append((path, marker))
        ctrl_signal = bool(OBS.get("p0_decides_for_p1"))
        if found or ctrl_signal or sched_nonempty:
            A["A4_control_recorded"] = "passed"
        elif A.get("A3_activation_completes") != "passed":
            A["A4_control_recorded"] = "not-run"
            obs["notes"].append("A4 not-run: activation did not complete")
        else:
            A["A4_control_recorded"] = "failed"
        obs["notes"].append(f"A4: markers_outside_defs={found} "
                            f"scheduled_turn_controls_nonempty="
                            f"{sched_nonempty} "
                            f"p0_decides_for_p1={ctrl_signal} "
                            f"control_snaps={len(CONTROL_OBS['snaps'])}")
    except Exception as e:
        A["A4_control_recorded"] = "not-run"
        obs["notes"].append(f"A4 eval error: {e!r}")

    # A5: cleanup
    final = env_state("post.json")
    if final is None:
        A["A5_cleanup"] = "failed"
        obs["notes"].append("post.json missing")
    else:
        game_over = str(final.get("phase") or "").lower() == "gameover"
        ok = stack_empty(final) and not game_over
        A["A5_cleanup"] = "passed" if ok else "failed"
        obs["notes"].append(f"A5: stack_empty={stack_empty(final)} "
                            f"game_over={game_over} "
                            f"turn={final.get('turn_number')}")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    a1 = A.get("A1_setup_ok")
    a2 = A.get("A2_granted_exposed")
    a3 = A.get("A3_activation_completes")
    a4 = A.get("A4_control_recorded")
    if a1 == "passed":
        if a2 == "failed":
            verdict = "reproduced"
        elif a2 == "passed" and a3 == "passed" and a4 == "passed":
            verdict = "not-reproduced"
        elif a2 == "passed" and (a3 == "failed" or a4 == "failed"):
            verdict = "reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "grant_probe": OBS.get("grant_probe"),
                   "grant_prompts_answered":
                       OBS.get("grant_prompts_answered"),
                   "p0_decides_for_p1": OBS.get("p0_decides_for_p1"),
                   "ids": IDS, "act": ACT,
                   "seen_turns": SEEN_TURN}, f, indent=2, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "observations": OBS,
                   "control_snaps": CONTROL_OBS["snaps"],
                   "ids": IDS, "seen_turns": SEEN_TURN}, f, indent=2,
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
                f"{BACKFILL}/server/releases/v0.102.0/"
                "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.102.0/data/draft-pools.json"),
            "signature_verified": SERVER_IDENTITY["signature_verified"],
        },
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [[YARGLE_T, 8], [BRACELET_T, 4], [SWAMP_T, 48]],
            "P1": [[SWAMP_T, 60]],
        },
        "driver_notes": [
            "Protocol-106 port of scenario_6872_0980.py (protocol 94 / "
            "v0.98.0) for pinned v0.102.0; behavioral contract A1..A5 and "
            "verdict logic unchanged.",
            "Conventions adopted from scenario_6866_01020.py / "
            "scenario_6868_01020.py: protocol-106 HELLO (exact match); "
            "priority = top-level PassPriority, all decisions via "
            "viewer_interaction (waiting_for is gone); MulliganDecision "
            "via legacy Action; bottom via vi schema/select gated on "
            "waitingForKind.code=='mulligan' at turn 1/Untap; "
            "DiscardToHandSize via vi schema/select.",
            "ActivateAbility submitted only while holding priority; legacy "
            "PayMana actions first, then vi tapLandForMana menus driven by "
            "MANA_NEEDS; sleep(0) yield before leg evaluation; 5s re-tick "
            "backstop for priority-holding clients.",
            "real_decision_pending excludes the 106 priority-menu codes "
            "(passPriority, tapLandForMana, untapLandForMana, castSpell, "
            "activateAbility, candidate, mana, mulliganDecision, "
            "playLand); playLand vi opportunities answered before the "
            "decision gate; land matching via is_land().",
            "Declares: relations-schema vi opportunity answered first (P1 "
            "blockers get a 25s grace window); the vi fallback only "
            "answers relations-schema opportunities (select/sequence are "
            "decisions, never declares).",
            "Granted-ability follow-ups answered from advertised "
            "opportunities only: opponent target -> P1 seat candidate; "
            "exile cost -> Bracelet reference candidate.",
            "A4 marker search excludes grant-definition blobs "
            "(abilities/static_abilities/static_definitions/deck_pools/"
            "definition stripped) so the definition's own ControlNextTurn "
            "tag cannot read as a registered control effect.",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope); the reported OUTCOME is "
            "asserted on the saved states, not the prompt.",
            "The grant probe re-runs on every P0 main phase (turn-gated): "
            "the granted ability must be advertised whenever P0 could "
            "legally activate it, so a single snapshot is insufficient "
            "evidence of absence. A2 passes if ANY probe advertised it.",
            "P0's pre_grant export and grant probe are gated on "
            "my_main(state, 0) (the first 106 attempt exported pre_grant "
            "on P1's turn, failing A1's fresh-main check).",
            "Discard diagnostic: do_discard now wire-logs the full "
            "(oid, name, rank) ranking; the first 106 attempt discarded a "
            "spare Bracelet at hand-size 8 for an undetermined reason "
            "(harmless: one Bracelet was already equipped).",
            "Card-data v0.102.0: the Bracelet still parses GrantAbility "
            "{15}/Exile(GrantingObject), cost_reduction Power, "
            "ControlNextTurn(Opponent), AsSorcery -- identical to the "
            "v0.98.0 parse; the bug is runtime exposure, not data.",
        ],
        "assertions": A,
        "notes": obs["notes"],
        "verdict": verdict,
        "evidence_comment_id": 5641216179,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x Yargle / 4x Bracelet density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay (scenario_6872_01020.py), not direct load.",
        ],
        "setup_line": "P0: 8x Yargle, Glutton of Urborg + 4x The Dominion "
                      "Bracelet + 48x Swamp; P1: 60x Swamp (passive)",
        "contract_line": ("Equip Bracelet to Yargle; the equipped creature "
                          "must expose the granted {15}/exile sorcery "
                          "ability as an activatable action, and activating "
                          "it must register control of P1's next turn."),
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
        shutil.copy(__file__, f"{EVDIR}/scenario_6872_01020.py")
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
        pre = load_ev_state("pre_grant.json")
        yoids = perm_oids(pre, 0, YARGLE_L)
        boids = perm_oids(pre, 0, BRACELET_L)
        yo = get_obj(pre, yoids[0]) if yoids else {}
        att = bool(yoids and boids and attached_oid(pre, boids[0], yoids[0]))
        pre_line = (f"pre: turn {pre.get('turn_number')} {pre.get('phase')} | "
                    f"Yargle {num(yo.get('power'))}/{num(yo.get('toughness'))} "
                    f"attached={att}")
    except Exception:
        pre_line = "pre_grant.json: missing"
    try:
        aj = json.load(open(f"{EVDIR}/assertions.json"))
        probe = aj.get("grant_probe") or {}
        post = load_ev_state("post_activate.json")
        bz = sorted({o.get("zone") for oid, o in post["objects"].items()
                     if obj_lname(post, oid) == BRACELET_L})
        post_line = (f"post_activate: granted advertised="
                     f"{probe.get('advertised_count')} | bracelet zones={bz}")
    except Exception:
        post_line = "post_activate.json: missing"
    try:
        final = load_ev_state("post.json")
        fin_line = (f"post: turn {final.get('turn_number')} "
                    f"{final.get('phase')} stack_empty="
                    f"{stack_empty(final)}")
    except Exception:
        fin_line = "post.json: missing"
    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    y = 20
    d.text((24, y), "Issue #6872 - The Dominion Bracelet grants nothing to "
                    "the equipped creature (v0.102.0 revalidation)",
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
    y += 24
    d.text((24, y), fin_line, fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Bracelet attached, Yargle 10/4, P0 main",
        "A2_granted_exposed": "A2 granted ability advertised on equipped "
                              "Yargle",
        "A3_activation_completes": "A3 ability resolved, Bracelet exiled, "
                                   "no rejection",
        "A4_control_recorded": "A4 control of P1's next turn registered / "
                               "observed",
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
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6872/" +
           run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest written ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
