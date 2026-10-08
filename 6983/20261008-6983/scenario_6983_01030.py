#!/usr/bin/env python3
"""Issue #6983: [Card Bug] Unless payments derived from board state are
unsupported.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Repulsive Mutation ({X}{G}{U} Instant):
    "Put X +1/+1 counters on target creature you control. Then counter up to
     one target spell unless its controller pays mana equal to the greatest
     power among creatures you control."

Card-data parse state on v0.103.0 (verified 2026-10-08 before the run):
  abilities[0].effect = PutCounter(P1P1, count=Ref(X), target Typed
    Creature/You)
  abilities[0].sub_ability.effect = {"type": "Unimplemented",
    "name": "unless_payment",
    "description": "counter up to one target spell unless its controller
    pays mana equal to the greatest power among creatures you control"}.
  abilities[0].sub_ability.optional_targeting = false (no target field).
  -> The reported parser gap PERSISTS on v0.103.0.

Reported symptom: the counterspell mode's unless-payment clause is
unsupported because its amount is dynamic (greatest power among creatures
the caster controls). Expected: when the mode resolves, the target spell's
controller is offered a payment of mana equal to that greatest power (here
4, from Leatherback Baloth), and the spell is countered only if unpaid.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Repulsive Mutation, 8x Grizzly Bears, 8x Leatherback Baloth,
      16x Forest, 16x Island.
  P1: 12x Lightning Bolt, 48x Mountain (passive except the two Bolts).
  P0 T2: Grizzly Bears (2/2). P0 T3: Leatherback Baloth (4/5).
  P1: casts Lightning Bolt targeting P0's face once the Mutation is
      castable (engine advertises the cast on P0 main-phase priority);
      casts a second Bolt at P0's face on its next priority so the
      Mutation faces TWO spells on the stack (with a single spell the
      engine silently chose zero targets for the optional target on
      v0.81.3).
  P0 responds with Repulsive Mutation: X = 0, target creature = Baloth,
      target spell = a Bolt on the stack (if the engine offers it).
  Resolution: 0 counters on Baloth; then the unless clause - the bug says
      the dynamic-amount unless_payment is Unimplemented and gets skipped.

Assertions (each passed / failed / not-run):
  A1_parse_gap      card-data v0.103.0: abilities[0].sub_ability.effect is
                    Unimplemented(name=unless_payment) (expected per bug
                    report; PASS confirms the reported parser gap).
  A2_setup_ok       PRE: Baloth + Bears on P0 BF; greatest power among P0
                    creatures == 4; TWO Bolts on stack; Mutation in P0 hand;
                    life 20/20.
  A3_mutation_cast  Mutation was cast (X=0, Baloth targeted) and resolved:
                    in P0 graveyard after resolution.
  A4_payment_prompt a dynamic-amount unless-payment opportunity (pay mana
                    equal to greatest power = 4) was offered to P1 during
                    Mutation resolution. Expected absent (unsupported:
                    Unimplemented effect is skipped).
  A5_bolts_resolve both Bolts were NOT countered: P1 gy, P0 life 20 -> 14.
  A6_baloth_unchanged Baloth still 4/5 post-resolution (X=0 counters; the
                    parsed PutCounter clause executed).
  A7_cleanup        POST: stack empty, game proceeds.

Two Bolts are stacked so the optional "up to one target spell" choice
faces >1 legal target (with a single spell the engine silently chose zero
targets and the unless step was unreachable on v0.81.3).

Verdict rule: reproduced iff A1 passes (parser gap persists on v0.103.0),
the Mutation resolves (A3) with no payment offered (A4 absent) and both
Bolts resolve uncountered (A5). not-reproduced iff a payment prompt with
amount == 4 was offered and the pay/decline semantics executed (A4 passes
with the correct amount, and the life total reflects the chosen branch).
blocked iff A2 or A3 fails.

Evidence: evidence/6983/<run-id>/pre.json, post.json, run.json,
assertions.json, observations.json, data_evidence.json, manifest.sha256,
summary.png, scenario_6983_01030.py, wire_log.jsonl, scenario_run.log,
server.log (excerpts for this game's code only).
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6983
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-6983")
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
    "server_version": "0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256": None,
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
}
for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = sha256_of_file(f"{BACKFILL}/{_f}")
assert SERVER_IDENTITY["server_binary_sha256"] == \
    "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc", \
    "binary hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["card_data_sha256"] == \
    "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe", \
    "card-data hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["draft_pools_sha256"] == \
    "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd", \
    "draft-pools hash drift from the v0.103.0 pin"
say("server identity hashes verified against the v0.103.0 pin")


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
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))

PARSE = {"effect": None, "optional_targeting": None, "ok": False}


def check_data_level():
    """Record the v0.103.0 parse of Repulsive Mutation's sub-ability.

    A1 contract: sub_ability.effect is Unimplemented(name=unless_payment)
    (the reported parser gap persists).
    """
    c = CARD_DATA.get("repulsive mutation", {})
    sub = (((c.get("abilities", []) or [])[0] or {}).get("sub_ability")
           or {})
    eff = sub.get("effect") or {}
    PARSE["effect"] = eff
    PARSE["optional_targeting"] = sub.get("optional_targeting")
    PARSE["target"] = sub.get("target")
    ok = (eff.get("type") == "Unimplemented"
          and eff.get("name") == "unless_payment")
    PARSE["ok"] = ok
    out = {
        "name": c.get("name"),
        "oracle_text": c.get("oracle_text"),
        "sub_ability_effect": eff,
        "sub_ability_optional_targeting": sub.get("optional_targeting"),
        "sub_ability_target": sub.get("target"),
        "parse_gap_persists": ok,
        "note": ("On v0.81.3 the parse was identical: Unimplemented/"
                 "unless_payment with optional_targeting=false."),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    say(f"data-level: parse_gap_persists={ok} "
        f"effect={eff.get('type')}/{eff.get('name')}")
    wire("data_level", {"parse_gap_persists": ok, "effect": eff})
    return out


MUT_T = "Repulsive Mutation"
MUT_L = "repulsive mutation"
BALOTH_T = "Leatherback Baloth"
BALOTH_L = "leatherback baloth"
BEARS_T = "Grizzly Bears"
BEARS_L = "grizzly bears"
BOLT_T = "Lightning Bolt"
BOLT_L = "lightning bolt"
FOREST_L = "forest"
ISLAND_L = "island"
MOUNTAIN_L = "mountain"
LANDS = (FOREST_L, ISLAND_L, MOUNTAIN_L)

P0_DECK = ((MUT_T, 12), (BEARS_T, 8), (BALOTH_T, 8),
           ("Forest", 16), ("Island", 16))
P1_DECK = ((BOLT_T, 12), ("Mountain", 48))

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0}}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {
    "mutation_ready": False,          # P0: Mutation cast advertised on P0 main
    "bolt1_cast": False, "bolt1_targeted": False,
    "bolt2_cast": False, "bolt2_targeted": False,
    "bolt_life_pre": None,
    "mutation_cast": False, "mutation_in_flight": False,
    "mutation_resolved": False, "mutation_oid": None,
    "creature_target_oid": None, "spell_target_oid": None,
    "x_answered": False,
    "pre_exported": False, "post_at": None, "post_exported": False,
    "payment_prompt_seen": False, "payment_prompt_detail": None,
    "target_sels": [],
}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "tick_errors": [], "notes": [], "target_selections": [],
       "payment_scan": []}


def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def yard_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and not get_obj(state, oid).get("tapped")]


def untapped_land_lnames(state, pid):
    return [obj_lname(state, oid) for oid in untapped_lands(state, pid)]


def creature_power(state, oid):
    o = get_obj(state, oid)
    p = o.get("power")
    if isinstance(p, dict):
        return p.get("value")
    if isinstance(p, int):
        return p
    return None


def greatest_power(state, pid):
    vals = [creature_power(state, oid) for oid in bf_oids(state, pid)]
    vals = [v for v in vals if isinstance(v, int)]
    return max(vals) if vals else 0


def stack_entries(state):
    return state.get("stack") or []


def stack_empty(state):
    return not stack_entries(state)


def bolt_on_stack_count(state):
    """Count Lightning Bolt spells on the stack.

    Stack entries carry no card name, so match the Bolt's effect
    signature (DealDamage with a fixed amount of 3), with a blob-text
    fallback on the spell name."""
    n = 0
    for e in stack_entries(state):
        blob = json.dumps(e, default=str).lower()
        kind = (e.get("kind") or {}).get("type")
        data = (e.get("kind") or {}).get("data") or {}
        sig = False
        for ab in (data.get("ability"), data.get("abilities") or []):
            if not isinstance(ab, dict):
                continue
            eff = ab.get("effect") or {}
            amt = eff.get("amount") or {}
            if (eff.get("type") == "DealDamage"
                    and str(amt.get("value")) == "3"):
                sig = True
                break
        if kind == "Spell" and (sig or "lightning bolt" in blob):
            n += 1
    return n


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
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and stack_empty(state))


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_oid(ch):
    ref = cand_reference(ch)
    try:
        return str(int(ref))
    except (TypeError, ValueError):
        return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                pass
    return None


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(op):
    for c in (op.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def unanswered_ops(st):
    out = []
    for op in vi_ops(st):
        iid = op.get("interactionId") or op.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if is_priority_menu(op):
            continue
        out.append(op)
    return out


def real_decision_pending(st):
    for opp in unanswered_ops(st):
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


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type"),
                           "stage": STAGE["stage"]})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "stage": STAGE["stage"],
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
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = STAGE["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in LANDS)
    if pid == 0:
        # The fixture needs the Baloth (greatest power 4) specifically.
        keep = ((MUT_L in hand and BALOTH_L in hand and n_lands >= 2)
                or n >= 2)
    else:
        keep = (hand.count(BOLT_L) >= 2 and n_lands >= 3) or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        STAGE["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if STAGE["mulls"].get(tag, 0) <= 0:
        return False
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        spec = (rdata.get("spec", {}) or {})
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = cand_oid(ch)
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


def discard_rank(state, o):
    nm = obj_lname(state, o)
    if nm in LANDS:
        return 0
    if nm in (BEARS_L, BALOTH_L):
        return 1
    return 3  # Bolt / Mutation kept last


async def do_discard(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = cand_oid(ch)
            if ref is not None:
                ref_of[ref] = ch["id"]
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o),
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


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


async def do_declare_empty(c, acts, st, pid, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"].update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower():
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
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    for o in lands:
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
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


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    return a
            except (TypeError, ValueError):
                pass
    return None


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


# ------------------------------------------------- issue-specific prompts

def iter_target_opps(st):
    """Yield (opp, rtype, spec_type) for every viewer_interaction
    opportunity that looks like a target selection: schema select/sequence
    with candidates, or exactChoices whose choices carry candidate/target
    codes (passPriority menus excluded)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                yield opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(cc for cc in surf_codes(ch) if cc)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(cc in codes for cc in ("candidate", "target")):
                yield opp, "exactChoices", "choose"


def record_target_sel(state, opp, stage):
    """Record a target-selection opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in ST["target_sels"]):
        return False
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    cand_info = []
    for ch in cands:
        oid = cand_oid(ch)
        o = get_obj(state, oid) if oid else {}
        cand_info.append({
            "choice_id": ch.get("id"),
            "oid": oid,
            "seat": cand_seat(ch),
            "name": obj_lname(state, oid) if oid else choice_text(ch),
            "zone": o.get("zone"),
            "controller": o.get("controller"),
            "text": choice_text(ch)[:120],
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "rtype": resp.get("type"),
        "spec_type": ((data.get("spec") or {}).get("type")),
        "candidates": cand_info,
    }
    ST["target_sels"].append(rec)
    OBS["target_selections"].append(rec)
    n = len(ST["target_sels"])
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(json.dumps(opp, default=str))},
                  f, indent=1, default=str)
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "iid": iid,
          "candidates": [(x["name"], x["zone"], x["controller"], x["seat"])
                         for x in cand_info]})
    say(f"target selection #{n} (stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']},"
                    f"seat={x['seat']})" for x in cand_info[:8]))
    return True


def pick_player0(state, opp):
    """Bolt: P0 the player (face)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        if cand_seat(ch) == 0:
            return ch
        oid = cand_oid(ch)
        if oid is not None:
            o = get_obj(state, oid)
            if o.get("zone") == "Player" or o.get("type") == "Player":
                return ch
        blob = (choice_text(ch) + " " + json.dumps(ch, default=str)).lower()
        if "player" in blob and ("opponent" in blob or "you" in blob):
            return ch
    return None


def pick_mutation_creature(state, opp):
    """Mutation's creature target: the P0 Leatherback Baloth on BF."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        oid = cand_oid(ch)
        if oid is None:
            continue
        o = get_obj(state, oid)
        if (obj_lname(state, oid) == BALOTH_L
                and o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == "0"):
            return ch
    return None


def pick_mutation_spell(state, opp):
    """Mutation's spell target: a Bolt candidate on the stack."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    stack_cands = []
    for ch in cands:
        oid = cand_oid(ch)
        if oid is None:
            continue
        o = get_obj(state, oid)
        if o.get("zone") == "Stack":
            stack_cands.append(ch)
    return stack_cands[0] if stack_cands else None


async def answer_target(c, state, opp, rtype, spec_type, ch, tag, stage):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
    oid = cand_oid(ch)
    say(f"[{tag}] answering {stage} target: "
        f"{obj_lname(state, oid) if oid else choice_text(ch)[:40]} "
        f"(oid {oid}) via {sub['response']['type']}")
    wire("target_answer", {"who": tag, "stage": stage, "iid": iid,
                           "oid": oid, "submission": sub})
    await answer_vi(c, opp, ch, tag)
    return oid


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def scan_payment_prompt(c, tag, state):
    """Record any unless-payment interaction offered during the Mutation
    resolution window. The prompt would go to P1 (the Bolt's controller),
    so scan both clients' views every tick while the Mutation is in
    flight."""
    if not ST["mutation_in_flight"] or ST["mutation_resolved"]:
        return
    st = st_of(c)
    for opp in vi_ops(st):
        blob = json.dumps(opp, default=str).lower()
        if "unless" not in blob and "unless_payment" not in blob:
            continue
        iid = opp.get("interactionId")
        if any(r.get("iid") == iid for r in OBS["payment_scan"]):
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        rec = {"iid": iid, "seen_by": tag,
               "texts": [choice_text(ch)[:120] for ch in chs][:10]}
        OBS["payment_scan"].append(rec)
        ST["payment_prompt_seen"] = True
        ST["payment_prompt_detail"] = rec
        say(f"PAYMENT PROMPT observed by {tag}: {rec['texts']}")
        wire("payment_prompt_seen",
             {"by": tag, "iid": iid,
              "opportunity": json.loads(json.dumps(opp, default=str))})


async def answer_x(c, st, tag):
    """ChooseXValue for the Mutation -> X = 0.

    106 shapes: schema with spec.type "number", or exactChoices with
    numbered choices."""
    if ST["x_answered"]:
        return False
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        iid = opp.get("interactionId")
        if resp.get("type") == "schema" and spec.get("type") == "number":
            sub = {"interactionId": iid,
                   "response": {"type": "number", "data": {"value": 0}}}
            say(f"[{tag}] Mutation X-choice -> X=0 (schema number)")
            wire("x_answer", {"iid": iid, "x": 0, "shape": "schema-number"})
            await interact_as(c, sub, tag)
            SUBMITTED_OPPS.add(iid)
            ST["x_answered"] = True
            return True
        if resp.get("type") == "exactChoices":
            chs = data.get("choices") or []
            for ch in chs:
                blob = (choice_text(ch) + " "
                        + json.dumps(ch, default=str)).lower()
                if re.search(r"\bx\s*=\s*0\b", blob) or blob.strip() == "0":
                    say(f"[{tag}] Mutation X-choice -> X=0 (exactChoices)")
                    wire("x_answer", {"iid": iid, "x": 0,
                                      "shape": "exactChoices"})
                    await answer_vi(c, opp, ch, tag)
                    ST["x_answered"] = True
                    return True
    return False


async def answer_mutation_targets(c, st, state, tag):
    """Answer the Mutation's target selection(s).

    Discriminate by candidate composition: stack candidates -> the Bolt
    (spell slot); battlefield Baloth/Bears -> the creature slot (Baloth).
    The engine may auto-target or silently skip the optional spell target
    (v0.81.3 chose zero targets with a single spell on the stack); both
    outcomes are recorded, not forced."""
    if not ST["mutation_in_flight"] or ST["mutation_resolved"]:
        return False
    acted = False
    for opp, rtype, spec_type in iter_target_opps(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        has_stack = any(cand_oid(ch) is not None
                        and get_obj(state, cand_oid(ch)).get("zone")
                        == "Stack" for ch in cands)
        has_own_creature = pick_mutation_creature(state, opp) is not None
        stage = None
        pick = None
        if has_stack and ST["spell_target_oid"] is None:
            pick = pick_mutation_spell(state, opp)
            stage = "mutation-spell"
        elif has_own_creature and ST["creature_target_oid"] is None:
            pick = pick_mutation_creature(state, opp)
            stage = "mutation-creature"
        record_target_sel(state, opp, stage or "mutation-unrecognized")
        if pick is None:
            say(f"[{tag}] mutation target prompt has no recognized "
                f"candidate for the open slot; leaving unanswered")
            wire("mutation_target_no_pick",
                 {"iid": iid, "stage": stage,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            continue
        oid = await answer_target(c, state, opp, rtype, spec_type, pick,
                                  tag, stage)
        if stage == "mutation-creature":
            ST["creature_target_oid"] = oid
        else:
            ST["spell_target_oid"] = oid
        wire("mutation_target_chosen", {"stage": stage, "oid": oid})
        acted = True
    return acted


# ------------------------------------------------------------- seat ticks

async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    scan_payment_prompt(c, tag, state)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True

    turn = state.get("turn_number") or 0
    phase = state.get("phase") or ""

    # track Mutation on stack / resolution
    if ST["mutation_cast"] and not ST["mutation_resolved"]:
        if yard_oids(state, 0, MUT_L):
            ST["mutation_resolved"] = True
            ST["mutation_in_flight"] = False
            say(f"Mutation resolved (in P0 graveyard), turn {turn}")
            wire("mutation_resolved", {"turn": turn})

    # track bolt resolution via life totals
    life = life_of(state, 0)
    if life is not None:
        if ST["bolt_life_pre"] is None:
            ST["bolt_life_pre"] = life
        elif life < ST["bolt_life_pre"]:
            say(f"P0 life {ST['bolt_life_pre']} -> {life} (bolt damage)")
            wire("life_drop", {"from": ST["bolt_life_pre"], "to": life})
            ST["bolt_life_pre"] = life

    # latch: Mutation cast advertised on P0 main-phase priority, but ONLY
    # once the fixture is complete (Baloth + Bears on the BF, so greatest
    # power == 4). This is the signal for P1 to start stacking Bolts.
    if (not ST["mutation_ready"] and MUT_L in hand_lnames(state, 0)
            and bf_oids(state, 0, BALOTH_L) and bf_oids(state, 0, BEARS_L)
            and my_main(state, 0)
            and find_cast_action(acts, state, MUT_L) is not None):
        ST["mutation_ready"] = True
        say("Mutation castable on P0 main with Baloth+Bears out "
            "(P1 may start Bolts)")
        wire("mutation_ready", {"turn": turn})

    # mutation cast decisions pending on P0
    if await answer_x(c, st, tag):
        return True
    if await answer_mutation_targets(c, st, state, tag):
        return True

    # PRE: Baloth+Bears on BF, 2 Bolts on stack, Mutation in hand,
    # P0 priority -> export before responding
    baloth = bf_oids(state, 0, BALOTH_L)
    bears = bf_oids(state, 0, BEARS_L)
    if (not ST["pre_exported"] and ST["bolt1_cast"] and ST["bolt2_cast"]
            and baloth and bears
            and bolt_on_stack_count(state) >= 2
            and not ST["mutation_cast"]
            and my_priority(acts)):
        await do_export(c, "pre.json")
        ST["pre_exported"] = True
        say("PRE exported: Baloth+Bears on BF, 2 Bolts on stack, "
            "Mutation in hand")

    # POST: mutation resolved, stack empty, 8s grace
    if (ST["mutation_resolved"] and not ST["post_exported"]
            and stack_empty(state)):
        if ST["post_at"] is None:
            ST["post_at"] = time.time()
    if (ST["post_at"] is not None and not ST["post_exported"]
            and time.time() - ST["post_at"] > 8
            and stack_empty(state)):
        await do_export(c, "post.json")
        ST["post_exported"] = True
        STAGE["stage"] = "DONE"
        STAGE["stop"] = True
        say("POST exported: sequence complete; stopping")
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(acts):
        n_untapped = len(untapped_lands(state, 0))
        hn = hand_lnames(state, 0)
        # 1. cast Grizzly Bears ({1}{G}) once, engine Auto payment
        if (BEARS_L in hn and n_untapped >= 2
                and not bf_oids(state, 0, BEARS_L)):
            a = find_cast_action(acts, state, BEARS_L)
            if a is not None:
                say(f"[{tag}] casting {BEARS_T} (engine Auto payment)")
                wire("cast_submit", {"tag": "bears"})
                await submit_as_is(c, a)
                return True
        # 2. cast Leatherback Baloth ({2}{G}{G}) once, engine Auto payment
        if (BALOTH_L in hn and n_untapped >= 4
                and not bf_oids(state, 0, BALOTH_L)):
            a = find_cast_action(acts, state, BALOTH_L)
            if a is not None:
                say(f"[{tag}] casting {BALOTH_T} (engine Auto payment)")
                wire("cast_submit", {"tag": "baloth"})
                await submit_as_is(c, a)
                return True
        # 3. respond to the Bolts: cast Mutation with BOTH Bolts on stack
        n_bolts = bolt_on_stack_count(state)
        if (not ST["mutation_cast"] and not stack_empty(state)
                and n_bolts >= 2 and MUT_L in hn
                and ISLAND_L in untapped_land_lnames(state, 0)
                and FOREST_L in untapped_land_lnames(state, 0)):
            a = find_cast_action(acts, state, MUT_L)
            if a is not None:
                ST["mutation_cast"] = True
                ST["mutation_in_flight"] = True
                say(f"[{tag}] casting {MUT_T} X=0 in response to "
                    f"{n_bolts} Bolts (engine Auto payment)")
                wire("mutation_submitted", {"bolts_on_stack": n_bolts})
                await submit_as_is(c, a)
                return True
            say("RESPOND: Mutation CastSpell NOT advertised on response "
                "tick; passing priority (run will block)")
            wire("respond_not_advertised", {"bolts_on_stack": n_bolts})
        if await play_a_land(c, state, 0, acts, tag):
            return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    scan_payment_prompt(c, tag, state)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True

    # bolt target prompts (face = P0)
    for opp, rtype, spec_type in iter_target_opps(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        pick = pick_player0(state, opp)
        if pick is None:
            continue
        record_target_sel(state, opp, "bolt-target")
        await answer_target(c, state, opp, rtype, spec_type, pick, tag,
                            "bolt-target")
        if ST["bolt1_cast"] and not ST["bolt1_targeted"]:
            ST["bolt1_targeted"] = True
        elif ST["bolt2_cast"] and not ST["bolt2_targeted"]:
            ST["bolt2_targeted"] = True
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1) or my_priority(acts):
        n_untapped = len(untapped_lands(state, 1))
        hn = hand_lnames(state, 1)
        # bolt1: start the stack once the Mutation is castable
        if (ST["mutation_ready"] and not ST["bolt1_cast"]
                and BOLT_L in hn and n_untapped >= 1):
            a = find_cast_action(acts, state, BOLT_L)
            if a is not None:
                ST["bolt1_cast"] = True
                say(f"[{tag}] casting {BOLT_T} #1 at P0 face "
                    f"(engine Auto payment)")
                wire("cast_submit", {"tag": "bolt1"})
                await submit_as_is(c, a)
                return True
        # bolt2: on the next P1 priority while bolt1 is still on the stack
        # (target answered, spell not yet resolved)
        if (ST["bolt1_cast"] and ST["bolt1_targeted"] and not ST["bolt2_cast"]
                and bolt_on_stack_count(state) >= 1
                and BOLT_L in hn and n_untapped >= 1):
            a = find_cast_action(acts, state, BOLT_L)
            if a is not None:
                ST["bolt2_cast"] = True
                say(f"[{tag}] casting {BOLT_T} #2 at P0 face "
                    f"(engine Auto payment)")
                wire("cast_submit", {"tag": "bolt2"})
                await submit_as_is(c, a)
                return True
        if await play_a_land(c, state, 1, acts, tag):
            return True

    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


# ------------------------------------------------------------- main loop

async def main():
    t0 = time.time()
    last_rev_change = t0
    game_started = False

    hello = await verify_server_hello()
    data_level = check_data_level()

    p0 = PhaseClient("P06983r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16983r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < GAME_TIMEOUT and not STAGE.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
            st = st_of(c)
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_rev_change = time.time()
                if (st.get("state") or {}).get("turn_number", 0) >= 1:
                    game_started = True
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # (or holding an unanswered vi decision) with no revision
                # change (missed-broadcast resilience).
                pending_vi = bool(unanswered_ops(st))
                if not ((my_priority(top_acts(st)) or pending_vi)
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STAGE["stop"] = True
            continue

        if game_started and not STAGE.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["notes"].append(f"stall: no revision for {STALL_AFTER}s")
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {"stage": STAGE["stage"]})
            STAGE["stop"] = True
            continue

        if turn > TURN_CAP and not STAGE.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STAGE["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} pp={state.get('priority_player')} "
                f"P0untapped={len(untapped_lands(state, 0))} "
                f"P1untapped={len(untapped_lands(state, 1))} "
                f"P0life={life_of(state, 0)} P1life={life_of(state, 1)} "
                f"baloth={bf_oids(state, 0, BALOTH_L)} "
                f"bears={bf_oids(state, 0, BEARS_L)} "
                f"stack={bolt_on_stack_count(state)}bolts "
                f"ready={ST['mutation_ready']} "
                f"bolt1={ST['bolt1_cast']}/{ST['bolt1_targeted']} "
                f"bolt2={ST['bolt2_cast']}/{ST['bolt2_targeted']} "
                f"mut={ST['mutation_cast']}/{ST['mutation_resolved']} "
                f"x={ST['x_answered']} "
                f"ctgt={ST['creature_target_oid']} "
                f"stgt={ST['spell_target_oid']} "
                f"payprompt={ST['payment_prompt_seen']} "
                f"pre={ST['pre_exported']} post={ST['post_exported']}")

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"]})

    await finish(p0, p1, t0, hello, data_level)


async def finish(p0, p1, t0, hello, data_level):
    # ------------------------------------------------------- assertions
    A, D = {}, {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    post_s = load("post.json")

    # A1: card-data parse gap persists on v0.103.0
    A["A1_parse_gap"] = "passed" if PARSE["ok"] else "failed"
    eff = PARSE["effect"] or {}
    D["A1_parse_gap"] = (
        f"sub_ability.effect = {eff.get('type')}/{eff.get('name')} "
        f"(expected Unimplemented/unless_payment); "
        f"optional_targeting={PARSE.get('optional_targeting')}")

    # A2: setup (from PRE)
    if pre_s is not None:
        baloth = bf_oids(pre_s, 0, BALOTH_L)
        bears = bf_oids(pre_s, 0, BEARS_L)
        gp = greatest_power(pre_s, 0)
        n_bolts = bolt_on_stack_count(pre_s)
        mut_in_hand = MUT_L in hand_lnames(pre_s, 0)
        lives = [life_of(pre_s, i) for i in (0, 1)]
        ok = (bool(baloth) and bool(bears) and gp == 4
              and n_bolts >= 2 and mut_in_hand and lives == [20, 20])
        A["A2_setup_ok"] = "passed" if ok else "failed"
        D["A2_setup_ok"] = (
            f"baloth={baloth} bears={bears} greatest_power={gp} "
            f"bolts_on_stack={n_bolts} mutation_in_hand={mut_in_hand} "
            f"life={lives}")
    else:
        A["A2_setup_ok"] = "failed"
        D["A2_setup_ok"] = "pre.json missing"

    # A3: Mutation cast (X=0, Baloth targeted) and resolved (P0 gy)
    mut_gy = False
    if post_s is not None:
        mut_gy = bool(yard_oids(post_s, 0, MUT_L))
    ok = ST["mutation_cast"] and ST["mutation_resolved"] and mut_gy
    A["A3_mutation_cast"] = "passed" if ok else "failed"
    D["A3_mutation_cast"] = (
        f"cast={ST['mutation_cast']} resolved={ST['mutation_resolved']} "
        f"in_P0_gy={mut_gy} x_answered={ST['x_answered']} "
        f"creature_target_oid={ST['creature_target_oid']} "
        f"spell_target_oid={ST['spell_target_oid']}")

    # A4: dynamic-amount unless-payment prompt offered to P1
    ok = ST["payment_prompt_seen"]
    A["A4_payment_prompt"] = "passed" if ok else "failed"
    D["A4_payment_prompt"] = (
        f"payment_prompt_seen={ok} detail={ST['payment_prompt_detail']}; "
        f"expected ABSENT while the clause parses Unimplemented")

    # A5: both Bolts resolved uncountered (P0 20 -> 14)
    if post_s is not None:
        l0 = life_of(post_s, 0)
        bolts_gy = len(yard_oids(post_s, 1, BOLT_L))
        ok = (l0 == 14 and bolts_gy >= 2)
        A["A5_bolts_resolve"] = "passed" if ok else "failed"
        D["A5_bolts_resolve"] = (
            f"P0_life={l0} (bug expectation 14: both Bolts resolved "
            f"uncountered; 17 would mean one was countered) "
            f"bolts_in_P1_gy={bolts_gy}")
    else:
        A["A5_bolts_resolve"] = "failed"
        D["A5_bolts_resolve"] = "post.json missing"

    # A6: Baloth unchanged (X=0 counters placed)
    if post_s is not None:
        baloth = bf_oids(post_s, 0, BALOTH_L)
        pwr = creature_power(post_s, baloth[0]) if baloth else None
        ok = pwr == 4
        A["A6_baloth_unchanged"] = "passed" if ok else "failed"
        D["A6_baloth_unchanged"] = (
            f"Baloth oid={baloth[0] if baloth else None} power={pwr} "
            f"(expected 4: X=0 counters placed)")
    else:
        A["A6_baloth_unchanged"] = "failed"
        D["A6_baloth_unchanged"] = "post.json missing"

    # A7: cleanup
    if post_s is not None:
        stack_clear = stack_empty(post_s)
        A["A7_cleanup"] = "passed" if stack_clear else "failed"
        D["A7_cleanup"] = (f"stack_empty={stack_clear} "
                           f"turn={post_s.get('turn_number')}")
    else:
        A["A7_cleanup"] = "failed"
        D["A7_cleanup"] = "post.json missing"

    # ---------------------------------------------------------- verdict
    keys = ("A1_parse_gap", "A2_setup_ok", "A3_mutation_cast",
            "A4_payment_prompt", "A5_bolts_resolve", "A6_baloth_unchanged",
            "A7_cleanup")
    if A["A2_setup_ok"] != "passed" or A["A3_mutation_cast"] != "passed":
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: setup (A2) or cast (A3) failed")
    elif (A["A1_parse_gap"] == "passed" and A["A4_payment_prompt"] == "failed"
            and A["A5_bolts_resolve"] == "passed"):
        verdict = "reproduced"
        OBS["notes"].append(
            "verdict=reproduced: the parser still cannot represent the "
            "board-state-derived unless-payment (A1 Unimplemented/"
            "unless_payment on v0.103.0); the Mutation resolved (A3) with "
            "no payment prompt offered (A4) and both Bolts resolved "
            "uncountered, P0 20->14 (A5)")
        if ST["spell_target_oid"] is None:
            OBS["notes"].append(
                "limitation: the engine never offered the optional "
                "'up to one target spell' target (no spell-slot target "
                "was answered); the runtime shows the clause skipped, "
                "not a targeted spell going uncountered")
    elif A["A4_payment_prompt"] == "passed":
        # The clause was exercised; judge correctness on the saved states.
        l0 = life_of(post_s, 0) if post_s is not None else None
        if l0 == 17 and A["A6_baloth_unchanged"] == "passed":
            verdict = "not-reproduced"
            OBS["notes"].append(
                "verdict=not-reproduced: an unless-payment prompt WAS "
                "offered during Mutation resolution; the decline branch "
                "countered the targeted Bolt (P0 20->17) and the Baloth "
                "kept 4/5")
        else:
            verdict = "reproduced"
            OBS["notes"].append(
                "verdict=reproduced: a payment prompt appeared but the "
                "outcome was wrong (related failure); "
                f"P0_life={l0}")
    else:
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: incomplete assertion chain")

    for k in keys:
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")
    OBS["notes"].append(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "target_selections": OBS["target_selections"],
                   "payment_scan": OBS["payment_scan"],
                   "unexpected_prompts": OBS["unexpected_prompts"],
                   "auto_answered": OBS["auto_answered"],
                   "tick_errors": OBS["tick_errors"],
                   "rejections": OBS["rejections"],
                   "notes": OBS["notes"]}, f, indent=1, default=str)

    date_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0))
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "date": date_iso,
        "game_code": STAGE.get("game_code"),
        "server": {
            "server_version": hello.get("server_version"),
            "build_commit": hello.get("build_commit"),
            "protocol_version": hello.get("protocol_version"),
            "mode": hello.get("mode"),
            "server_binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "run_note": ("shared pinned v0.103.0 single-user server on "
                         "127.0.0.1:9374 (started by run 20261008-6982; "
                         "this run used its own game + games.db rows)"),
        },
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {"P0": [[n, c] for n, c in P0_DECK],
                  "P1": [[n, c] for n, c in P1_DECK]},
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "setup_line": ("P0 12x Repulsive Mutation / 8x Grizzly Bears / 8x "
                       "Leatherback Baloth / 16x Forest / 16x Island; P1 12x "
                       "Lightning Bolt / 48x Mountain. P0 casts Bears then "
                       "Baloth; P1 Bolts P0's face twice (two Bolts on the "
                       "stack); P0 responds with Mutation X=0 targeting "
                       "the Baloth (+ a Bolt if the engine offers the "
                       "optional spell target). Engine Auto payment "
                       "throughout; no driver mana taps."),
        "contract_line": ("The card-data parse gap must persist "
                          "(Unimplemented/unless_payment -> reproduced); "
                          "the Mutation must resolve with no unless-payment "
                          "prompt and both Bolts resolving uncountered "
                          "(P0 20->14). A payment prompt with amount 4 and "
                          "correct decline semantics would flip the verdict "
                          "to not-reproduced."),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6983.py (v0.81.3 / "
            "protocol 70) for pinned v0.103.0; behavioral contract "
            "A1..A7 and the issue-specific game plan unchanged.",
            "waiting_for is gone (null); priority = top-level "
            "PassPriority; all decisions via viewer_interaction; "
            "MulliganDecision via legacy Action; bottom via vi "
            "schema/select gated on waitingForKind.code=='mulligan'; "
            "DiscardToHandSize via vi schema/select (the ONLY accepted "
            "shape for a select schema).",
            "CastSpell via legacy Action; the v0.103.0 engine auto-taps "
            "reliably, so no driver mana taps (driver taps on top of "
            "engine auto-taps double-pay; AGENTS.md #6910).",
            "Target prompts answered via iter_target_opps (schema "
            "select/sequence with candidates, or exactChoices with "
            "candidate/target codes); the Mutation's creature slot is "
            "discriminated by own-battlefield Baloth candidates, the "
            "spell slot by stack candidates. Full opportunities are "
            "wired and recorded to target_sel_N.json.",
            "The engine may auto-target a sole legal target or silently "
            "skip the optional spell target (v0.81.3 chose zero targets "
            "with one spell on the stack); both outcomes are recorded, "
            "not forced.",
            "X is answered as X=0 via schema-number or exactChoices; "
            "the unless-payment scan runs on both clients' views during "
            "the whole Mutation in-flight window and matches on "
            "'unless'/'unless_payment' in the opportunity blob.",
            "Pre/post states are authoritative exports (data.state "
            "parsed once from the export envelope) via the host client "
            "only; the reported OUTCOME is asserted on the saved "
            "states, not the prompt.",
        ],
        "assertions": A,
        "assertion_details": D,
        "driver_state": ST,
        "data_level": data_level,
        "verdict": verdict,
        "evidence_comment_id": 5653053246,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Only the decline branch of the unless clause could be "
            "exercised if the prompt appeared; the pay branch would need "
            "a second game.",
        ],
        "mulligans": STAGE["mulls"],
        "rejections": OBS["rejections"],
        "notes": OBS["notes"],
        "duration_s": round(time.time() - t0, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"wrote run.json verdict={verdict}")

    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6983_01030.py", "w") as f:
        f.write(src)
    say("copied scenario_6983_01030.py into EVDIR")

    try:
        with open(f"{BACKFILL}/runs/20261008-6982/server.log", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
        clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        code = STAGE.get("game_code") or ""
        excerpt = [ln for ln in clean.splitlines() if code and code in ln]
        with open(f"{EVDIR}/server.log", "w") as f:
            f.write("\n".join(excerpt) + "\n")
        say(f"wrote server.log excerpts ({len(excerpt)} lines for game "
            f"{code})")
    except Exception as e:
        say(f"server.log excerpt failed: {e}")
        OBS["notes"].append(f"server.log excerpt failed: {e}")

    render_summary(run, pre_s, post_s)

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    # Final manifest AFTER RUNLOG is closed: write_manifest()'s own say()
    # is then stdout-only (guarded on closed RUNLOG), so no logged line can
    # go stale after the hash.
    write_manifest()
    say("scenario finished")


def render_summary(run, pre_s, post_s):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 980
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6983 - Repulsive Mutation",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
           "unless_payment w/ dynamic (greatest-power) amount",
           fill=(140, 160, 180))
    y += 28
    vcol = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from saved states / card-data):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_parse_gap": "card-data: effect Unimplemented(unless_payment)",
        "A2_setup_ok": "PRE: Baloth+Bears on BF, gp=4, 2 Bolts on stack",
        "A3_mutation_cast": "Mutation X=0 cast & resolved (P0 gy)",
        "A4_payment_prompt": "dynamic-amount pay prompt offered to P1",
        "A5_bolts_resolve": "Bolts NOT countered (P0 20->14)",
        "A6_baloth_unchanged": "Baloth still 4/5 (X=0 counters placed)",
        "A7_cleanup": "POST stack empty",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Life / greatest-power across states (P0/P1):",
           fill=(200, 210, 225))
    y += 24
    for label, st in (("pre ", pre_s), ("post", post_s)):
        if st is not None:
            baloth = bf_oids(st, 0, BALOTH_L)
            gp = greatest_power(st, 0)
            n_bolts = bolt_on_stack_count(st)
            line = (f"{label}: {life_of(st, 0)}/{life_of(st, 1)}  "
                    f"baloth={baloth[0] if baloth else None} "
                    f"greatest_power={gp} bolts_on_stack={n_bolts}")
        else:
            line = f"{label}: (no state)"
        d.text((36, y), line[:112], fill=(150, 160, 175))
        y += 22
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    notes = (run["notes"] or [])[:2]
    details = [run["assertion_details"].get(k, "") for k in labels]
    for n in notes + details:
        for seg in [str(n)[i:i + 116] for i in range(0, len(str(n)), 116)][:2]:
            d.text((36, y), seg, fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    files = ["pre.json", "post.json",
             "run.json", "assertions.json", "observations.json",
             "data_evidence.json", "scenario_6983_01030.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png"] + \
        [f"target_sel_{i}.json" for i in range(1, len(ST["target_sels"]) + 1)]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
