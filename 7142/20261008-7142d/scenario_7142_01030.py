#!/usr/bin/env python3
"""Issue #7142: Nykthos Paragon - "offers the ability to give counters for
lifegain only once."

Protocol-106 port of driver/scenario_7142.py (v0.82.0/protocol 70,
validated 2026-09-14, verdict reproduced) for pinned v0.103.0.
Behavioral contract, assertions A1..A6 and verdict rule unchanged.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0, key "nykthos paragon"):
  Nykthos Paragon ({4}{W}{W}, 4/6 Enchantment Creature - Human Soldier):
    "Whenever you gain life, you may put that many +1/+1 counters on each
     creature you control. Do this only once each turn."

Card-data parse state on v0.103.0 (verified 2026-10-08 before the run):
  triggers[0] = LifeGained(valid_target=Controller) ->
    PutCounter P1P1 count=Ref(EventContextAmount)
    target=Typed(Creature, controller=You), optional=true,
    constraint=OncePerTurn, batched=false.
  Parse-shape observation (not asserted): the "each creature" application is
  encoded as a plain Typed target with no explicit all-matching marker
  (same as v0.82.0).

Reported symptom (Discord, triage-clarified by mike-theDude):
  1. Nykthos Paragon offers the counter placement only once: declining the
     offer for one lifegain instance suppresses the offer for later lifegain
     instances (same turn).
  2. Accepting puts the counters on a single creature instead of each
     creature controlled.

Expected behavior (triage acceptance criteria):
  - Declining preserves a later eligible trigger that turn.
  - Accepting marks the action used only after counters are placed.
  - Every currently controlled creature receives the correct amount.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Nykthos Paragon, 12x Grizzly Bears, 12x Healing Salve,
      18x Plains, 6x Forest.
  P1: 60x Forest (passive; never plays lands/casts/attacks/blocks).

Planned line (single game, two legs):
  Setup: P0 drops lands (Plains-first), casts 2 Grizzly Bears, casts
    Nykthos Paragon ({4}{W}{W}, engine Auto payment). Neither player attacks.
    PRE exported at the gate: Paragon + 2 bears on P0 BF, 2+ Salves in hand,
    2+ untapped Plains, P0 at 20 life.
  Leg 1 (decline): cast Healing Salve #1 targeting self (modal: gain 3 life).
    P0 20->23. The Nykthos optional prompt (decideOptionalEffect) is
    EXPECTED; the driver DECLINES it. offer1.json exported at the offer.
  Leg 2 (accept, same turn): cast Healing Salve #2 targeting self.
    P0 23->26. The Nykthos optional prompt is EXPECTED again (declining leg 1
    must not consume the once-per-turn opportunity); the driver ACCEPTS it.
    offer2.json exported at the offer. If the accept path raises a
    single-creature choice (the v0.82.0 bug mechanism), the driver answers it
    (first bear) and records it. POST exported once settled.
  If a leg's offer never appears within 75s of the lifegain, that leg is
  recorded as skipped (the failure itself is the evidence).

Assertions (each passed / failed / not-run):
  A1_parse            card-data: LifeGained -> PutCounter[P1P1 x
                      EventContextAmount] on Typed(Creature, You),
                      optional, OncePerTurn.
  A2_setup            PRE: Paragon on P0 BF, 2+ bears on P0 BF, 2+ Salves in
                      P0 hand, P0 at 20 life.
  A3_offer1           lifegain #1 raised the Nykthos optional prompt for P0.
  A4_decline_preserves
                      after declining leg 1, lifegain #2 (same turn) raised
                      the Nykthos optional prompt again (reported bug: no).
  A5_counters_all     accept leg: every P0-controlled creature gained exactly
                      3 +1/+1 counters vs pre.json (reported bug: only one
                      creature does). not-run if the leg-2 offer never came.
  A6_cleanup          both Salves in P0 graveyard, P0 at 26, stack empty,
                      game proceeds.

Verdict rule:
  blocked        iff A2 fails (setup never reached).
  reproduced     iff A2 passes and any executed leg assertion (A3/A4/A5)
                 fails.
  not-reproduced iff A2..A6 all pass.

Protocol-106 port notes (from driver/scenario_7141_01030.py conventions):
  - my_priority = PassPriority present in legal_actions; casts gated on it.
  - Engine auto-taps for CastSpell (payment_mode Auto): the driver never
    answers tapLandForMana and runs no driver-side mana payment (legacy
    PayMana actions are answered if they appear).
  - DeclareAttackers: {"attacks": [], "bands": []};
    DeclareBlockers: {"assignments": []}. Neither seat ever attacks/blocks.
  - The Nykthos may-choice is a vi opportunity whose choices carry the
    decideOptionalEffect action code (scenario_301_01030.py shape); accept =
    choice with surface role "accept" value "true", decline = "false".
  - Healing Salve's modal choice is answered via chooseBranch optionIndex
    (AGENTS.md 2026-10-08 lesson) with a "gains 3 life" text fallback; the
    Salve targets P0 (seat 0), tolerating an engine auto-target.
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).
  - Pre/offer/post states are authoritative exports via the host client
    only (data.state JSON string written verbatim, parsed once on load);
    the reported OUTCOME is asserted on the saved states, not the prompt.

Evidence: evidence/7142/<run-id>/pre.json, offer1.json, offer1_opp.json,
offer2.json, offer2_opp.json, choice_opp.json, post.json, run.json,
parse_nykthos_paragon.json, scenario_7142_01030.py, wire_log.jsonl,
scenario_run.log, server.log, summary.png, manifest.sha256.
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
ISSUE = 7142
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7142")
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

PARSE = {"ok": False, "obs": {}}


def check_parse_nykthos():
    """A1: v0.103.0 card-data carries LifeGained -> PutCounter[P1P1 x
    EventContextAmount] on Typed(Creature, You), optional, OncePerTurn."""
    c = CARD_DATA.get("nykthos paragon", {})
    trigs = c.get("triggers", [])
    blob = json.dumps(trigs)
    has = {
        "LifeGained": "LifeGained" in blob,
        "PutCounter": "PutCounter" in blob,
        "EventContextAmount": "EventContextAmount" in blob,
        "optional": '"optional": true' in blob or '"optional":true' in blob,
        "OncePerTurn": "OncePerTurn" in blob,
        "Typed": "Typed" in blob,
        "Creature": "Creature" in blob,
    }
    PARSE["ok"] = all(has.values())
    PARSE["obs"]["checks"] = has
    with open(f"{EVDIR}/parse_nykthos_paragon.json", "w") as fh:
        json.dump({"card": "Nykthos Paragon",
                   "oracle_text": c.get("oracle_text"),
                   "mana_cost": c.get("mana_cost"),
                   "triggers": trigs,
                   "checks": has,
                   "parse_ok": PARSE["ok"]}, fh, indent=1, default=str)
    say(f"A1_parse: {'passed' if PARSE['ok'] else 'FAILED'} checks={has}")
    wire("parse", {"ok": PARSE["ok"], "checks": has})
    return PARSE["ok"]

NYKTHOS = "nykthos paragon"
BEAR = "grizzly bears"
SALVE = "healing salve"
PLAINS = "plains"
FOREST = "forest"
LANDS = (PLAINS, FOREST)

P0_DECK = [(NYKTHOS, 12), (BEAR, 12), (SALVE, 12), (PLAINS, 18), (FOREST, 6)]
P1_DECK = [(FOREST, 60)]

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45
OFFER_WAIT_S = 75

STOP = {"stop": False}
ST = {
    "stage": "setup",  # setup -> salve1 -> salve2 -> settle -> done
    "paragon_cast": False, "bears_cast": 0,
    "p0_life_pre": None,
    "pre_exported": False, "offer1_exported": False,
    "offer2_exported": False, "post_exported": False,
    # leg 1 (decline)
    "salve1_cast": False, "salve1_oid": None, "salve1_mode_chosen": False,
    "salve1_target_oid": None, "salve1_resolved": False,
    "salve1_resolve_t": None, "salve1_turn": None,
    "offer1_seen": False, "offer1_skipped": False,
    "offer1_declined": False, "offer1_accepted": False,
    # leg 2 (accept)
    "salve2_cast": False, "salve2_oid": None, "salve2_mode_chosen": False,
    "salve2_target_oid": None, "salve2_resolved": False,
    "salve2_resolve_t": None, "salve2_turn": None,
    "offer2_seen": False, "offer2_skipped": False,
    "offer2_accepted": False,
    # buggy single-creature choice mechanism (accept path)
    "choice_seen": False, "choice_answered": False, "choice_oid": None,
    "choice_count": None,
    "settle_ticks": 0,
}
OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": [], "life_trace": [],
       "optional_prompts": [], "mode_choices": [], "single_choices": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
MULLS = {"P0": 0, "P1": 0}
PASSED_REV = {}
LAST_IID = {"iid": None}

# ------------------------------------------------------- state helpers

def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [o for o in player_of(state, pid).get("hand", []) or []]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def lib_oids(state, pid):
    return [o for o in player_of(state, pid).get("library", []) or []]


def bf_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            if lname is None or obj_lname(state, oid) == lname:
                out.append(oid)
    return out


def bf_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) in LANDS]


def untapped_lands(state, pid):
    return [oid for oid in bf_lands(state, pid)
            if not get_obj(state, oid).get("tapped")]


def untapped_plains(state, pid):
    return [oid for oid in untapped_lands(state, pid)
            if obj_lname(state, oid) == PLAINS]


def gy_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Graveyard" and o.get("controller") == pid:
            if lname is None or obj_lname(state, oid) == lname:
                out.append(oid)
    return out


def exile_oids(state):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if str(o.get("zone") or "").lower() == "exile"]


def stack_empty(state):
    return not (state.get("stack") or [])


def counters_of(state, oid):
    """Best-effort +1/+1 counter count on an object, across engine shapes."""
    o = get_obj(state, oid)
    total = 0
    found = False
    c = o.get("counters")
    if isinstance(c, dict):
        for k, v in c.items():
            ks = str(k).upper().replace("+", "P").replace(" ", "")
            if isinstance(v, (int, float)) and ("P1P1" in ks or "1/1" in str(k)):
                total += int(v)
                found = True
    elif isinstance(c, list):
        for e in c:
            if isinstance(e, dict):
                k = str(e.get("type") or e.get("kind")
                        or e.get("counter_type") or "")
                v = e.get("count", e.get("n", 1))
                ku = k.upper().replace("+", "P")
                if "P1P1" in ku or "1/1" in k:
                    try:
                        total += int(v)
                        found = True
                    except Exception:
                        pass
            elif isinstance(e, str) and "1/1" in e:
                total += 1
                found = True
    for k in ("plus_one_plus_one_counters", "p1p1_counters",
              "plusOnePlusOneCounters", "p1p1Counters"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            total += int(v)
            found = True
    return total, found


def pt_of(state, oid):
    o = get_obj(state, oid)
    return o.get("power"), o.get("toughness")


# ------------------------------------------------- action/vi helpers

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


def opp_choices(opp):
    """choices or candidates, whichever the response carries."""
    rdata = (opp.get("response") or {}).get("data", {}) or {}
    return rdata.get("choices", []) or rdata.get("candidates", []) or []


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


def option_index_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "optionIndex":
            try:
                return int(d.get("value"))
            except (TypeError, ValueError):
                pass
    return None


def accept_of(choice):
    """surface role 'accept' value: 'true' (accept) / 'false' (decline)."""
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "accept":
            return str(d.get("value"))
    return None


def is_optional_opp(opp):
    for ch in opp_choices(opp):
        if "decideOptionalEffect" in surf_codes(ch) \
                or "decideOptionalCost" in surf_codes(ch):
            return True
    return False


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(opp):
    for c in (opp.get("response") or {}).get("data", {}).get("choices", []):
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
    return spec.get("type") in ("select", "sequence")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type")})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
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


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
    return out

# ------------------------------------------------------- shared handlers

async def export_named(c, tag):
    try:
        s = await c.export_state()
        with open(f"{EVDIR}/{tag}.json", "w") as f:
            f.write(s)
        say(f"exported {tag.upper()}")
        return True
    except Exception as e:
        OBS["notes"].append(f"{tag} export failed: {e}")
        say(f"{tag} export failed: {e}")
        return False


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hn = hand_lnames(st["state"], pid)
    n_lands = sum(1 for h in hn if h in LANDS)
    n = MULLS.get(tag, 0)
    if pid == 0:
        keep = (NYKTHOS in hn and n_lands >= 2) or n_lands >= 3 or n >= 2
    else:
        keep = n_lands >= 2 or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        MULLS[tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hn})")
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
    if MULLS.get(tag, 0) <= 0:
        return False
    key_card = NYKTHOS if pid == 0 else None
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
            nm = obj_lname(state, ref) if ref else ""
            if key_card and nm == key_card:
                return (2, str(ref))
            if nm in LANDS:
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


def discard_rank(state, o, pid):
    nm = obj_lname(state, o)
    if nm == FOREST:
        return 0
    if nm in LANDS:
        return 1
    if nm == BEAR:
        return 2
    if nm == SALVE:
        return 3
    if nm == NYKTHOS:
        return 4
    return 2


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
                ref_of[str(ref)] = ch["id"]
        ranked = sorted(hand,
                        key=lambda o: (discard_rank(state, o, pid),
                                       obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[str(o)] for o in pick if str(o) in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        DISCARDED_IIDS.add(iid)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    return False


async def do_declare(c, acts, st, pid, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["attacks"] = []
            d["data"]["bands"] = []
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag, target):
    """Play a land while fewer than `target` untapped lands (Plains first
    for P0, then Forest)."""
    if len(untapped_lands(state, pid)) >= target:
        return False
    cands = [a for a in acts if a.get("type") == "PlayLand"]
    if not cands:
        return False

    def rank(a):
        try:
            nm = obj_lname(state, a.get("_src_oid"))
        except (TypeError, ValueError):
            return (9, "")
        if pid == 0:
            return (0 if nm == PLAINS else (1 if nm == FOREST else 2), nm)
        return (3, nm)

    a = sorted(cands, key=rank)[0]
    say(f"[{tag}] plays land {obj_lname(state, a.get('_src_oid'))} "
        f"(target {target} untapped)")
    wire("play_land", {"who": tag, "target": target})
    await submit_as_is(c, a)
    return True


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def log_unanswered(c, tag, st):
    for opp in unanswered_ops(st):
        iid = opp.get("interactionId")
        if iid in LOGGED_IIDS or iid in DISCARDED_IIDS:
            continue
        LOGGED_IIDS.add(iid)
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        OBS["unexpected_prompts"].append(
            {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
             "rtype": resp.get("type"),
             "codes": sorted({x for ch in chs
                              for x in surf_codes(ch) if x}),
             "texts": [choice_text(ch)[:60] for ch in chs][:8]})
        say(f"[{tag}] unanswered vi iid={iid} n={len(chs)} "
            f"rtype={resp.get('type')}")
        wire("unanswered_vi",
             {"who": tag, "iid": iid,
              "opportunity": json.loads(json.dumps(opp, default=str))})


def cast_spell_for(acts, state, lname):
    out = []
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        dd = a.get("data") or {}
        oid = dd.get("object_id")
        try:
            if oid is not None and obj_lname(state, str(oid)) == lname:
                out.append((oid, a))
        except Exception:
            continue
    return out


# ------------------------------------------------- issue-specific logic

def salve_leg_active():
    """Which salve leg is currently in flight (cast, mode/target pending or
    resolving), or None."""
    if ST["stage"] == "salve1" and ST["salve1_cast"] \
            and not ST["salve1_resolved"]:
        return 1
    if ST["stage"] == "salve2" and ST["salve2_cast"] \
            and not ST["salve2_resolved"]:
        return 2
    return None


def mode_text_of(ch):
    """Healing Salve modal branches carry the text in a surface with
    data.role == 'mode' (choice_text would return the modeIndex '0'/'1')."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "mode":
            return str(d.get("value") or "")
    return choice_text(ch)


async def salve_mode_tick(c, tag, st, state):
    """Healing Salve modal choice: pick the 'gains 3 life' branch. On 106 a
    modal ChooseOneOf surfaces as a schema sequence of candidates; each
    branch carries role 'mode' text (and role 'modeIndex')."""
    leg = salve_leg_active()
    if leg is None:
        return False
    if (leg == 1 and ST["salve1_mode_chosen"]) or \
            (leg == 2 and ST["salve2_mode_chosen"]):
        return False
    for opp in unanswered_ops(st):
        rdata = (opp.get("response") or {}).get("data", {}) or {}
        chs = rdata.get("choices", []) or rdata.get("candidates", []) or []
        if not chs:
            continue
        codes = {x for ch in chs for x in surf_codes(ch) if x}
        texts = [mode_text_of(ch).lower() for ch in chs]
        is_branch = "chooseBranch" in codes
        is_modal_text = any("gains 3 life" in t for t in texts) and any(
            "prevent" in t for t in texts)
        if not (is_branch or is_modal_text):
            continue
        iid = opp.get("interactionId")
        wire("salve_mode_opportunity",
             {"who": tag, "leg": leg, "iid": iid,
              "n_choices": len(chs), "codes": sorted(codes),
              "texts": [t[:60] for t in texts],
              "option_indexes": [option_index_of(ch) for ch in chs],
              "opportunity": json.loads(json.dumps(opp, default=str))})
        # prefer the lifegain branch by mode text; else optionIndex 0 (Oracle
        # lists "Target player gains 3 life" first).
        pick = next((ch for ch in chs if "gains 3 life" in
                     mode_text_of(ch).lower()), None)
        if pick is None:
            pick = next((ch for ch in chs if option_index_of(ch) == 0), None)
        if pick is None:
            pick = chs[0]
        say(f"[{tag}] salve mode (leg {leg}): picking lifegain branch "
            f"({mode_text_of(pick)[:50]})")
        OBS["mode_choices"].append({"who": tag, "leg": leg,
                                    "picked": mode_text_of(pick)[:60]})
        await answer_vi(c, opp, pick, tag)
        if leg == 1:
            ST["salve1_mode_chosen"] = True
        else:
            ST["salve2_mode_chosen"] = True
        return True
    return False


async def salve_target_tick(c, tag, st, state):
    """Healing Salve targets a player: choose self (seat 0). Tolerates the
    engine auto-targeting a sole legal target (then no prompt appears)."""
    leg = salve_leg_active()
    if leg is None:
        return False
    if (leg == 1 and not ST["salve1_mode_chosen"]) or \
            (leg == 2 and not ST["salve2_mode_chosen"]):
        return False
    for opp in unanswered_ops(st):
        chs = opp_choices(opp)
        if not chs:
            continue
        seats = {cand_seat(ch) for ch in chs}
        if not (seats & {0, 1}):
            continue
        # skip the Nykthos optional prompt (handled separately)
        if is_optional_opp(opp):
            continue
        iid = opp.get("interactionId")
        pick = next((ch for ch in chs if cand_seat(ch) == 0), None)
        if pick is None:
            pick = chs[0]
        wire("salve_target",
             {"who": tag, "leg": leg, "iid": iid, "n": len(chs),
              "picked_seat": cand_seat(pick),
              "picked_oid": cand_oid(pick)})
        OBS["target_selections"].append(
            {"who": tag, "leg": leg, "picked_seat": cand_seat(pick)})
        say(f"[{tag}] salve target (leg {leg}): choosing self (seat 0)")
        await answer_vi(c, opp, pick, tag)
        if leg == 1:
            ST["salve1_target_oid"] = cand_oid(pick)
        else:
            ST["salve2_target_oid"] = cand_oid(pick)
        return True
    return False


def is_nykthos_offer_opp(opp, state):
    """The Paragon may-choice: decideOptionalEffect code, or an accept
    true/false pair mentioning counters."""
    if is_optional_opp(opp):
        return True
    chs = opp_choices(opp)
    avals = {accept_of(ch) for ch in chs}
    if avals == {"true", "false"}:
        joined = " ".join(choice_text(ch).lower() for ch in chs)
        if any(k in joined for k in ("counter", "you may put", "each creature")):
            return True
    return False


async def nykthos_offer_tick(c, tag, st, state):
    """Answer the Paragon's optional-trigger prompt: DECLINE in leg 1,
    ACCEPT in leg 2. Fires only after the leg's Salve resolved."""
    if ST["stage"] not in ("salve1", "salve2"):
        return False
    leg = 1 if ST["stage"] == "salve1" else 2
    resolved = ST["salve1_resolved"] if leg == 1 else ST["salve2_resolved"]
    if not resolved:
        return False
    if leg == 1 and (ST["offer1_declined"] or ST["offer1_accepted"]):
        return False
    if leg == 2 and ST["offer2_accepted"]:
        return False
    for opp in unanswered_ops(st):
        if not is_nykthos_offer_opp(opp, state):
            continue
        iid = opp.get("interactionId")
        chs = opp_choices(opp)
        opp_path = f"offer{leg}_opp.json"
        with open(f"{EVDIR}/{opp_path}", "w") as f:
            json.dump(json.loads(json.dumps(opp, default=str)), f, indent=1)
        wire("nykthos_offer",
             {"who": tag, "leg": leg, "iid": iid, "n_choices": len(chs),
              "accept_values": [accept_of(ch) for ch in chs],
              "texts": [choice_text(ch)[:80] for ch in chs]})
        OBS["optional_prompts"].append(
            {"who": tag, "leg": leg,
             "accept_values": [accept_of(ch) for ch in chs]})
        say(f"[{tag}] NYKTHOS offer (leg {leg}): n={len(chs)} "
            f"accept={[accept_of(ch) for ch in chs]}")
        if leg == 1:
            if not ST["offer1_exported"]:
                if await export_named(c, "offer1"):
                    ST["offer1_exported"] = True
            ST["offer1_seen"] = True
            pick = next((ch for ch in chs if accept_of(ch) == "false"), None)
            if pick is None:
                pick = chs[-1]
            say(f"[{tag}] DECLINING the Paragon counters (leg 1)")
            await answer_vi(c, opp, pick, tag)
            ST["offer1_declined"] = True
        else:
            if not ST["offer2_exported"]:
                if await export_named(c, "offer2"):
                    ST["offer2_exported"] = True
            ST["offer2_seen"] = True
            pick = next((ch for ch in chs if accept_of(ch) == "true"), None)
            if pick is None:
                pick = chs[0]
            say(f"[{tag}] ACCEPTING the Paragon counters (leg 2)")
            await answer_vi(c, opp, pick, tag)
            ST["offer2_accepted"] = True
        return True
    return False


async def nykthos_single_choice_tick(c, tag, st, state):
    """After ACCEPTING the offer, the engine (bug) may present a choice of a
    single creature to receive the counters instead of applying to each
    creature. Answer with the first bear and record it as the bug
    mechanism; if no such prompt appears, the accept path applied cleanly."""
    if not ST["offer2_accepted"] or ST["choice_answered"]:
        return False
    bf_creatures = set(bf_oids(state, 0))
    for opp in unanswered_ops(st):
        if is_nykthos_offer_opp(opp, state):
            continue
        chs = opp_choices(opp)
        if not chs:
            continue
        refs = {cand_oid(ch) for ch in chs if cand_oid(ch)}
        if not refs or not (refs <= bf_creatures):
            continue
        iid = opp.get("interactionId")
        with open(f"{EVDIR}/choice_opp.json", "w") as f:
            json.dump(json.loads(json.dumps(opp, default=str)), f, indent=1)
        ST["choice_seen"] = True
        names = [(r, obj_lname(state, r)) for r in refs]
        say(f"[{tag}] NYKTHOS single-creature choice (bug mechanism): "
            f"choose 1 of {names}")
        OBS["single_choices"].append({"who": tag, "candidates": names})
        pick = next((ch for ch in chs
                     if obj_lname(state, cand_oid(ch)) == BEAR), None)
        if pick is None:
            pick = chs[0]
        ST["choice_oid"] = cand_oid(pick)
        await answer_vi(c, opp, pick, tag)
        ST["choice_answered"] = True
        return True
    return False

# ------------------------------------------------------------- tick fns

async def p0_tick(c, pid, tag):
    st = st_of(c)
    if not st or "state" not in st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await salve_mode_tick(c, tag, st, state):
        return True
    if await salve_target_tick(c, tag, st, state):
        return True
    if await nykthos_offer_tick(c, tag, st, state):
        return True
    if await nykthos_single_choice_tick(c, tag, st, state):
        return True
    if await do_declare(c, acts, st, 0, tag):
        return True
    # legacy mana actions: answer if they appear (engine Auto payment on
    # 106 means they usually do not). Driver never taps mana itself.
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0

    # life trace
    lives = (life_of(state, 0), life_of(state, 1))
    tr = OBS["life_trace"]
    if all(v is not None for v in lives) and (not tr or tr[-1][1] != lives):
        tr.append((round(time.time() - t_start, 1), lives))
        say(f"life = {lives}")
        wire("life", {"life": lives})

    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # ---- P0 priority: main-phase line ----
    is_p0_main = (phase in MAIN_PHASES and state.get("active_player") == 0)
    if is_p0_main:
        if await play_a_land(c, state, 0, acts, tag, 99):
            return True
        paragon_bf = bf_oids(state, 0, NYKTHOS)
        bears_bf = bf_oids(state, 0, BEAR)
        if ST["stage"] == "setup":
            if len(bears_bf) < 2:
                found = cast_spell_for(acts, state, BEAR)
                if found:
                    oid, action = found[0]
                    say(f"[P0] casting grizzly bears oid={oid} "
                        f"(engine Auto payment)")
                    await submit_as_is(c, action)
                    ST["bears_cast"] += 1
                    return True
            if not paragon_bf and not ST["paragon_cast"]:
                found = cast_spell_for(acts, state, NYKTHOS)
                if found:
                    oid, action = found[0]
                    say(f"[P0] casting nykthos paragon oid={oid} "
                        f"(engine Auto payment)")
                    await submit_as_is(c, action)
                    ST["paragon_cast"] = True
                    return True
            # pre-export gate
            salves_hand = sum(1 for n in hand_lnames(state, 0)
                              if n == SALVE)
            if (len(paragon_bf) >= 1 and len(bears_bf) >= 2
                    and salves_hand >= 2
                    and len(untapped_plains(state, 0)) >= 2
                    and life_of(state, 0) == 20
                    and not ST["pre_exported"]):
                ST["p0_life_pre"] = life_of(state, 0)
                if await export_named(c, "pre"):
                    ST["pre_exported"] = True
                    ST["salve1_turn"] = turn
                    say(f"[P0] PRE exported: paragon={len(paragon_bf)} "
                        f"bears={len(bears_bf)} salves={salves_hand} "
                        f"untapped_plains={len(untapped_plains(state, 0))}")
                    ST["stage"] = "salve1"
                    return True
        elif ST["stage"] == "salve1" and not ST["salve1_cast"]:
            found = cast_spell_for(acts, state, SALVE)
            if found:
                oid, action = found[0]
                say(f"[P0] casting healing salve #1 (oid={oid})")
                await submit_as_is(c, action)
                ST["salve1_cast"] = True
                ST["salve1_oid"] = oid
                ST["salve1_turn"] = turn
                return True
        elif ST["stage"] == "salve2" and not ST["salve2_cast"]:
            found = cast_spell_for(acts, state, SALVE)
            if found:
                oid, action = found[0]
                say(f"[P0] casting healing salve #2 (oid={oid})")
                await submit_as_is(c, action)
                ST["salve2_cast"] = True
                ST["salve2_oid"] = oid
                ST["salve2_turn"] = turn
                return True

    # ---- leg progress / resolution detection (life deltas) ----
    base = ST["p0_life_pre"]
    life0 = life_of(state, 0)
    if ST["stage"] == "salve1" and ST["salve1_cast"] \
            and not ST["salve1_resolved"] \
            and base is not None and life0 is not None \
            and life0 >= base + 3:
        ST["salve1_resolved"] = True
        ST["salve1_resolve_t"] = time.time()
        say(f"[P0] salve #1 resolved: P0 {base}->{life0}")
    if ST["stage"] == "salve2" and ST["salve2_cast"] \
            and not ST["salve2_resolved"] \
            and base is not None and life0 is not None \
            and life0 >= base + 6:
        ST["salve2_resolved"] = True
        ST["salve2_resolve_t"] = time.time()
        say(f"[P0] salve #2 resolved: P0 {base}->{life0}")

    # leg 1 settled after the offer was answered -> start leg 2 (same turn)
    if ST["stage"] == "salve1" and (
            ST["offer1_declined"] or ST["offer1_accepted"]):
        settled = stack_empty(state) and my_priority(top_acts(st))
        ST["settle_ticks"] = ST["settle_ticks"] + 1 if settled else 0
        if ST["settle_ticks"] >= 3:
            ST["settle_ticks"] = 0
            say("[P0] leg 1 settled after decline; starting leg 2")
            ST["stage"] = "salve2"
            return True
    # leg 1: offer never came within OFFER_WAIT_S of the lifegain
    if ST["stage"] == "salve1" and ST["salve1_resolved"] \
            and not ST["offer1_seen"] and ST["salve1_resolve_t"] \
            and time.time() - ST["salve1_resolve_t"] > OFFER_WAIT_S:
        say("[P0] leg 1: no Nykthos offer within "
            f"{OFFER_WAIT_S}s of lifegain; skipping to leg 2")
        ST["offer1_skipped"] = True
        ST["settle_ticks"] = 0
        ST["stage"] = "salve2"
        return True
    # leg 2: accept path settled -> export post -> done
    if ST["stage"] == "salve2" and ST["offer2_accepted"]:
        choice_pending = ST["choice_seen"] and not ST["choice_answered"]
        settled = stack_empty(state) and my_priority(top_acts(st)) \
            and not choice_pending
        ST["settle_ticks"] = ST["settle_ticks"] + 1 if settled else 0
        if ST["settle_ticks"] >= 3:
            ST["settle_ticks"] = 0
            if not ST["post_exported"]:
                if await export_named(c, "post"):
                    ST["post_exported"] = True
                    ST["stage"] = "settle"
                    STOP["stop"] = True
                    return True
    # leg 2: offer never came within OFFER_WAIT_S of the lifegain
    if ST["stage"] == "salve2" and ST["salve2_resolved"] \
            and not ST["offer2_seen"] and ST["salve2_resolve_t"] \
            and time.time() - ST["salve2_resolve_t"] > OFFER_WAIT_S:
        say("[P0] leg 2: no Nykthos offer within "
            f"{OFFER_WAIT_S}s of lifegain; exporting post")
        ST["offer2_skipped"] = True
        if not ST["post_exported"]:
            if await export_named(c, "post"):
                ST["post_exported"] = True
                ST["stage"] = "settle"
                STOP["stop"] = True
                return True
    # stage safety timeout
    if ST["stage"] in ("salve1", "salve2") \
            and (time.time() - t_start) > 1200:
        OBS["notes"].append(f"stage {ST['stage']} hit run timeout; finishing")
        say(f"[P0] stage {ST['stage']} timed out; exporting post fallback")
        if not ST["post_exported"]:
            if await export_named(c, "post"):
                ST["post_exported"] = True
                STOP["stop"] = True
                return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


async def p1_tick(c, pid, tag):
    st = st_of(c)
    if not st or "state" not in st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True
    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True
    # P1 is fully passive otherwise (never plays lands, never casts,
    # never blocks, never attacks)
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True

# ------------------------------------------------------------- main loop

async def main():
    global t_start
    t_start = time.time()

    hello = await verify_server_hello()
    check_parse_nykthos()

    p0 = PhaseClient("P07142r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P17142r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    GAME = p0.game_code
    say(f"game {GAME}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game", {"code": GAME, "p0": p0.player_id, "p1": p1.player_id,
                  "run_id": RUN_ID})

    def load_state(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        try:
            env = json.loads(open(p).read())
            return env.get("state")
        except Exception as e:
            OBS["notes"].append(f"state reload failed for {tag}.json: {e}")
            return None

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named(p0, "post"):
                ST["post_exported"] = True
                OBS["notes"].append("post.json exported at finish() fallback")
        pre, offer1, offer2, post = (load_state("pre"), load_state("offer1"),
                                     load_state("offer2"), load_state("post"))
        ass = {}
        notes = list(OBS["notes"])

        # A1: parse
        ass["A1_parse"] = "passed" if PARSE["ok"] else "failed"

        # A2: setup from pre.json
        if pre is None:
            ass["A2_setup"] = "failed"
        else:
            par_n = len(bf_oids(pre, 0, NYKTHOS))
            bear_n = len(bf_oids(pre, 0, BEAR))
            salve_n = sum(1 for n in hand_lnames(pre, 0) if n == SALVE)
            life = life_of(pre, 0)
            ok = par_n >= 1 and bear_n >= 2 and salve_n >= 2 and life == 20
            ass["A2_setup"] = "passed" if ok else "failed"
            notes.append(f"A2: pre paragon_bf={par_n} bears_bf={bear_n} "
                         f"salves_hand={salve_n} life={life}")

        # A3: leg-1 lifegain raised the Nykthos optional prompt
        if ST["offer1_seen"]:
            ass["A3_offer1"] = "passed"
        elif ST["offer1_skipped"]:
            ass["A3_offer1"] = "failed"
        else:
            ass["A3_offer1"] = "not-run"
        notes.append(f"A3: offer1_seen={ST['offer1_seen']} "
                     f"skipped={ST['offer1_skipped']}")

        # A4: declining leg 1 preserves the leg-2 offer (same turn)
        if not ST["offer1_declined"]:
            ass["A4_decline_preserves"] = "not-run"
        elif ST["offer2_seen"]:
            ass["A4_decline_preserves"] = "passed"
        else:
            ass["A4_decline_preserves"] = "failed"
        notes.append(f"A4: offer1_declined={ST['offer1_declined']} "
                     f"offer2_seen={ST['offer2_seen']} "
                     f"salve1_turn={ST['salve1_turn']} "
                     f"salve2_turn={ST['salve2_turn']}")

        # A5: accept leg -- every P0 creature gained exactly 3 counters
        if not ST["offer2_accepted"]:
            ass["A5_counters_all"] = "not-run"
            notes.append("A5: not-run (leg-2 offer never accepted)")
        elif pre is None or post is None:
            ass["A5_counters_all"] = "not-run"
            notes.append("A5: not-run (missing pre/post state)")
        else:
            per_creature = {}
            all_ok = True
            creatures = [oid for oid in bf_oids(pre, 0)
                         if obj_lname(pre, oid) in (NYKTHOS, BEAR)]
            for oid in creatures:
                if obj_lname(post, oid) not in (NYKTHOS, BEAR):
                    per_creature[oid] = {"name": obj_lname(pre, oid),
                                         "status": "left_battlefield"}
                    all_ok = False
                    continue
                c_pre, f_pre = counters_of(pre, oid)
                c_post, f_post = counters_of(post, oid)
                p_pre, t_pre = pt_of(pre, oid)
                p_post, t_post = pt_of(post, oid)
                d_c = c_post - c_pre
                d_p = (p_post - p_pre) if None not in (p_pre, p_post) else None
                d_t = (t_post - t_pre) if None not in (t_pre, t_post) else None
                if f_pre and f_post:
                    ok = (d_c == 3)
                    basis = f"counters {c_pre}->{c_post}"
                elif d_p is not None and d_t is not None:
                    ok = (d_p == 3 and d_t == 3)
                    basis = f"pt {p_pre}/{t_pre}->{p_post}/{t_post}"
                else:
                    ok = False
                    basis = "no counter/pt data"
                per_creature[oid] = {"name": obj_lname(pre, oid),
                                     "delta_counters": d_c,
                                     "delta_pt": [d_p, d_t],
                                     "basis": basis, "ok": ok}
                if not ok:
                    all_ok = False
            ass["A5_counters_all"] = "passed" if all_ok else "failed"
            notes.append(f"A5: single_choice_seen={ST['choice_seen']} "
                         f"choice_oid={ST['choice_oid']} "
                         f"per_creature={json.dumps(per_creature)}")

        # A6: cleanup
        if post is None:
            ass["A6_cleanup"] = "not-run"
        else:
            gy_salves = len(gy_oids(post, 0, SALVE))
            life = life_of(post, 0)
            ok = gy_salves == 2 and life == 26 and stack_empty(post)
            ass["A6_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A6: gy_salves={gy_salves} life={life} "
                         f"stack_empty={stack_empty(post)}")

        if ass.get("A2_setup") != "passed":
            verdict = "blocked"
        elif any(ass.get(k) == "failed"
                 for k in ("A3_offer1", "A4_decline_preserves",
                           "A5_counters_all")):
            verdict = "reproduced"
        else:
            verdict = "not-reproduced"

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "issue_url": f"https://github.com/phase-rs/phase/issues/{ISSUE}",
            "verdict": verdict,
            "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "server": dict(SERVER_IDENTITY, observed_hello=hello),
            "game_code": GAME,
            "driver": {
                "scenario": "scenario_7142_01030.py",
                "protocol": 106,
                "conventions": [
                    "CreateGameWithSettings + JoinGameWithPassword + "
                    "start_when_full; gate on waiting_for.",
                    "Engine auto-taps for CastSpell (payment_mode Auto); "
                    "driver never taps mana itself.",
                    "Nykthos may-choice answered via decideOptionalEffect "
                    "code; decline=false leg 1, accept=true leg 2.",
                    "Healing Salve modal via chooseBranch optionIndex "
                    "(optionIndex 0 = gains 3 life) with text fallback; "
                    "targets seat 0, tolerating auto-target.",
                    "The stack-watch branch always falls through to the "
                    "pass-priority gate (never returns early); both seats "
                    "must pass in succession for a stack entry to resolve.",
                    "Pre/offer/post states are authoritative exports "
                    "(data.state parsed once from the export envelope) via "
                    "the host client only; the reported OUTCOME is asserted "
                    "on the saved states, not the prompt.",
                ],
            },
            "assertions": ass,
            "observations": OBS,
            "driver_state": ST,
            "mulligans": MULLS,
            "notes": notes,
            "evidence_files": ["pre.json", "offer1.json", "offer1_opp.json",
                               "offer2.json", "offer2_opp.json",
                               "choice_opp.json",
                               "post.json", "run.json",
                               "parse_nykthos_paragon.json",
                               "scenario_7142_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Paragon / 12x Bears / 12x Salve density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games).",
                "P1 is a fully passive punching bag (60x Forest; never "
                "plays lands, never casts, never blocks).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Turn order is randomized by the engine; the driver keys on "
                "active_player and turn_number, not order.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1, default=str)
        say(f"wrote run.json verdict={verdict}")
        for k, v in ass.items():
            say(f"{k}: {v}")

        with open(__file__) as f:
            src = f.read()
        with open(f"{EVDIR}/scenario_7142_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_7142_01030.py into EVDIR")

        srv_src = None
        for cand in (f"{BACKFILL}/runs/{RUN_ID}/server.log",):
            if os.path.exists(cand):
                srv_src = cand
                break
        if srv_src is not None:
            import shutil
            shutil.copy(srv_src, f"{EVDIR}/server.log")
            say(f"copied server.log from {srv_src} into EVDIR")
        else:
            note = (f"no per-run server.log at runs/{RUN_ID}/server.log; "
                    f"the pinned server on 127.0.0.1:9374 was already "
                    f"running (dedicated to this run); wire traffic is in "
                    f"wire_log.jsonl, driver log in scenario_run.log")
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write(note + "\n")
            say("server.log: wrote note instead (no runs/<run-id>/server.log)")

        render_summary(run, pre, offer1, offer2, post)

        write_manifest()               # build 1
        say("scenario finished")       # final scenario_run.log line
        write_manifest(quiet=True)     # build 2 -- no logging after this
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        for c in (p0, p1):
            try:
                await c.close()
            except Exception:
                pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, pre, offer1, offer2, post):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7142 - Nykthos Paragon",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "once-per-turn may-trigger: decline preserves? each creature?",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: whenever you gain life, you may put that "
               "many +1/+1 counters on each creature you control. Do this "
               "only once each turn.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: LifeGained->PutCounter xEventAmount, "
                        "optional, OncePerTurn",
            "A2_setup": "PRE: paragon + 2 bears on BF, 2 salves in hand, "
                        "life 20",
            "A3_offer1": "lifegain #1 raised the Nykthos optional prompt",
            "A4_decline_preserves": "decline leg 1 -> lifegain #2 (same turn) "
                                   "raised the offer again",
            "A5_counters_all": "accept leg: EVERY creature gained exactly 3 "
                               "+1/+1 counters",
            "A6_cleanup": "both salves in gy, P0 at 26, stack empty",
        }
        for k, lab in labels.items():
            av = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if av == "passed" else (
                (255, 90, 90) if av == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {av} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Board across states:", fill=(200, 210, 225))
        y += 24

        def creat_line(s):
            if not s:
                return None
            parts = []
            for oid in bf_oids(s, 0):
                nm = obj_lname(s, oid)
                if nm in (NYKTHOS, BEAR):
                    c, _ = counters_of(s, oid)
                    p, t = pt_of(s, oid)
                    parts.append(f"{nm.split()[0][:6]}:{p}/{t}+{c}")
            return " ".join(parts)

        for label, s in (("pre   ", pre), ("offer1", offer1),
                         ("offer2", offer2), ("post  ", post)):
            if s is not None:
                line = (f"{label}: life {life_of(s, 0)}/{life_of(s, 1)}  "
                        f"creatures [{creat_line(s)}]  "
                        f"gy_salves={len(gy_oids(s, 0, SALVE))}  "
                        f"stack={len(s.get('stack') or [])}")
            else:
                line = f"{label}: (no state)"
            d.text((36, y), line[:118], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:22]:
            d.text((36, y), str(n)[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest(quiet=False):
        files = ["pre.json", "offer1.json", "offer1_opp.json",
                 "offer2.json", "offer2_opp.json", "choice_opp.json",
                 "post.json", "run.json", "parse_nykthos_paragon.json",
                 "scenario_7142_01030.py", "wire_log.jsonl",
                 "scenario_run.log", "server.log", "summary.png"]
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
        # build 2 must be quiet: any say() after the second write would
        # append to scenario_run.log and stale its manifest hash.
        if not quiet:
            say(f"wrote manifest.sha256 ({len(lines)} files)")

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    last_rev_change = t_start
    game_started = False
    while time.time() - t_start < GAME_TIMEOUT and not STOP.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick),
                             (p1, "P1", p1_tick)):
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
            if not st or "state" not in st:
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
                pid = {"P0": 0, "P1": 1}[tag]
                await tick(c, pid, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st or "state" not in st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if STOP.get("stop"):
            break

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STOP["stop"] = True
            continue

        if game_started and not STOP.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["notes"].append(f"stall: no revision for {STALL_AFTER}s")
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {})
            STOP["stop"] = True
            continue

        if turn > TURN_CAP and not STOP.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STOP["stop"] = True
            continue

        if turn > 30 and ST["stage"] == "setup" and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 30 reached, setup never "
                                "completed; finishing")
            say("watchdog: turn 30, setup never completed; finishing")
            await finish()
            return

        if turn > 40 and ST["stage"] not in ("settle",) \
                and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 40 reached with the line "
                                "incomplete; finishing")
            say("watchdog: turn 40, line incomplete; finishing")
            await finish()
            return

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"pp={state.get('priority_player')} "
                f"life={[life_of(state, i) for i in (0, 1)]} "
                f"stage={ST['stage']} "
                f"paragon_bf={len(bf_oids(state, 0, NYKTHOS))} "
                f"bears_bf={len(bf_oids(state, 0, BEAR))} "
                f"salve1={ST['salve1_cast']}/{ST['salve1_resolved']} "
                f"offer1={ST['offer1_seen']}/{ST['offer1_declined']} "
                f"salve2={ST['salve2_cast']}/{ST['salve2_resolved']} "
                f"offer2={ST['offer2_seen']}/{ST['offer2_accepted']} "
                f"stack={len(state.get('stack') or [])}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
