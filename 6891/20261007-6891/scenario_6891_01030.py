#!/usr/bin/env python3
"""Issue #6891: Lathiel, the Bounteous Dawn -- "distribute up to that many
+1/+1 counters among any number of other target creatures" reportedly forces
a number of distinct targets tied to life gained.

Oracle: "Lifelink. At the beginning of each end step, if you gained life
this turn, distribute up to that many +1/+1 counters among any number of
other target creatures."

Triage acceptance criteria:
  - The controller may distribute anywhere from zero through the life gained
    total.
  - The controller may choose any number of legal other creatures consistent
    with the counters assigned.
  - Each chosen target receives at least one counter, and the total never
    exceeds life gained.
  - Opposing creatures remain optional legal targets, never mandatory.

Re-validation port of driver/scenario_6891_01020.py for the pinned
v0.103.0 release (same protocol 106 driver conventions; game logic
identical). Protocol-106 conventions (from
scenario_6878_01020.py):
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [<name strings>]}.
  - waiting_for is gone (null): priority = advertised PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap; DiscardToHandSize
    via vi schema/select.
  - CastSpell via legacy actions (engine auto-taps reliably; manual taps
    are a >90s fallback only).
  - DeclareAttackers/Blockers: advertised submits (attack with Lathiel
    alone via attacks=[[oid, {"type":"Player","data":1}]]); vi fallback
    only answers relations-schema opportunities (select/sequence schemas
    are decisions, never declares); P1 blockers get the 25s relations
    grace window.
  - play_a_land matches any land via is_land(); vi playLand choices
    answered before the decision gate.
  - real_decision_pending excludes the 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand);
    decideOptionalEffect / decideOptionalCost and schema opportunities
    are real decisions.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients.
  - Export envelope: data.state is a JSON string parsed once.

Plan (native engine, v0.103.0 / protocol 106, two human-client seats):
  P0: 12x Lathiel, the Bounteous Dawn + 12x Grizzly Bears + 18x Forest
      + 18x Plains.
  P1: 12x Grizzly Bears + 48x Forest.
  P0 ramps, casts Lathiel ({2}{G}{W} 2/2 lifelink) and exactly one Bear,
  attacks P1 with Lathiel alone (P1 declares no blockers) -> P0 gains 2
  life. At P0's end step the trigger fires. Board for the target prompt:
  P0 controls Lathiel + its own Bear; P1 controls >=1 Bear (opponent's
  creatures are legal "other" targets but must never be mandatory).
  Slot 0: target P0's own Bear. Slot 1: try the SAME Bear again (stacking
  test). The reported defect is observed iff the engine excludes the
  already-targeted Bear from slot 1's candidates (distinct-target
  requirement). If stacking is refused, attempt the decline/up-to path
  for the extra slot.

Behavioral contract:
  A1 setup_ok          Lathiel on P0 BF at the end-step trigger; P0 gained
                       life this turn (life > 20); >=1 own other creature
                       and >=1 P1 creature on BF
  A2 trigger_fired     Lathiel's end-step trigger reached a P0 target
                       selection (vi opportunity referencing BF creatures)
  A3 budget            life gained this turn == 2
  A4 slot0_accepted    submitting P0's own Bear for slot 0 is accepted
  A5 free_distribution both counters may go on one creature: slot 1's
                       candidates still include the already-targeted Bear
                       and the same-target submission is accepted; fails
                       iff the engine excludes it (distinct-target
                       requirement = reported bug)
  A6 decline_path      if stacking is refused, a decline/up-to path exists
                       for the extra slot (else the controller is forced
                       onto an opponent's creature)
  A7 cleanup           stack empty, game proceeds

Verdict: reproduced iff A2 passes and A5 fails -- the engine implements
"distribute up to N" as N distinct-target slots. not-reproduced iff all
assertions pass (free distribution works). blocked otherwise.
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
ISSUE = 6891
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261007-6891")
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
    """Record the v0.103.0 parse of Lathiel's end-step trigger. The
    reported defect is data-visible: multi_target.max tied to
    Ref(LifeGainedThisTurn) conflates the counter budget with oracle's
    independently variable 'any number' of targets."""
    h = CARD_DATA["lathiel, the bounteous dawn"]
    trigs = [t for t in (h.get("triggers") or [])
             if ((t.get("execute") or {}).get("effect") or {}).get("type")
             == "PutCounter"]
    assert trigs, "no PutCounter trigger parsed for lathiel in v0.103.0"
    eff = trigs[0]["execute"]["effect"]
    mt = trigs[0]["execute"].get("multi_target")
    ev = {
        "name": h.get("name"),
        "oracle_text": h.get("oracle_text"),
        "counter_type": eff.get("counter_type"),
        "count": eff.get("count"),
        "distribute": trigs[0]["execute"].get("distribute"),
        "multi_target": mt,
        "target": eff.get("target"),
        "condition": trigs[0].get("condition"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": h.get("name"),
                   "oracle_text": h.get("oracle_text"),
                   "triggers": trigs}, f, indent=1)
    say(f"data-level: counter={ev['counter_type']} count={ev['count']} "
        f"multi_target={json.dumps(mt, default=str)[:160]}")
    wire("data_level", {k: (v if k != "count" else str(v))
                        for k, v in ev.items()})


LATHIEL_T = "Lathiel, the Bounteous Dawn"
BEAR_T = "Grizzly Bears"
FOREST_T = "Forest"
PLAINS_T = "Plains"

LATHIEL_L = "lathiel, the bounteous dawn"
BEAR_L = "grizzly bears"
FOREST_L = "forest"
PLAINS_L = "plains"

P0_DECK = ((LATHIEL_T, 12), (BEAR_T, 12), (FOREST_T, 18), (PLAINS_T, 18))
P1_DECK = ((BEAR_T, 12), (FOREST_T, 48))

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0,
      "lathiel_cast_turn": None, "attack_done": False, "attack_turn": None,
      "pre_exported": False, "post_exported": False,
      "trigger_seen": False, "target_prompt_seen": False,
      "slots_answered": 0, "p0_bear_oid": None, "p0_bear_is_own": None,
      "slot0_accepted": None, "single_rejection": None,
      "stacking_tried": False, "stacking_accepted": None,
      "distinct_required_observed": False, "declined_extra": False,
      "empty_tried": False, "terminal_forced": False,
      "life_at_endstep": None, "life_gain": None,
      "opp_shapes_logged": set(), "p1_block_grace_until": 0,
      "own_bear_cast": False}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {"cast": None}
OBS = {"rejections": [], "notes": []}
P0C = {"c": None}
P1C = {"c": None}

# ------------------------------------------------------------- state helpers


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


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def library_of(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


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


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and not get_obj(state, oid).get("tapped")]


def stack_empty(state):
    return not (state.get("stack") or [])


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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_object_ref(cand):
    for s in (cand or {}).get("surfaces", []) or []:
        if s.get("type") == "object":
            return str((s.get("data") or {}).get("reference"))
    ref = _cand_reference(cand)
    return str(ref) if ref is not None else None


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
    n_lands = sum(1 for h in hand if h in (FOREST_L, PLAINS_L))
    if tag == "P0":
        choice = "Keep" if (LATHIEL_L in hand and n_lands >= 3) or n >= 2 \
            else "Mulligan"
    else:
        choice = "Keep" if (BEAR_L in hand and n_lands >= 2) or n >= 2 \
            else "Mulligan"
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
    Untap. Never bottoms the combo pieces."""
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

        keep = {LATHIEL_L, BEAR_L} if tag == "P0" else {BEAR_L}

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
    if ln == LATHIEL_L:
        return 3
    if ln == BEAR_L:
        return 2
    if is_land(get_obj(state, o)):
        return 1
    return 0


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln == BEAR_L:
        return 2
    if is_land(get_obj(state, o)):
        return 1
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
    if tag == "P1" and state.get("active_player") != pid \
            and "declareblock" in phase.lower():
        if find_relations_op(st) is None:
            ST["p1_block_grace_until"] = time.time() + 25
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


async def drive_mana(c, tag):
    """Mana fallback: the v0.103.0 engine auto-taps reliably, so manual
    tapping engages only if a cast sits unannounced for >90s with a tap
    menu visible (logged loudly)."""
    cs = ACT.get("cast")
    st = st_of(c)
    state = st["state"]
    if cs and cs["in_flight"]:
        if any(obj_lname(state, o) == cs["lname"] and
               get_obj(state, o).get("zone") in ("Stack", "Battlefield")
               for o in (state.get("objects") or {})):
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

# ------------------------------------------------------------- Lathiel logic


def lathiel_trigger_on_stack(state):
    """Lathiel's end-step trigger on the stack. Match on the instantiated
    ability's own fields (kind.data.ability.effect.type == PutCounter with
    counter_type P1P1) and the source object being Lathiel -- never on the
    whole nested trigger_entries blob (cf. the 2026-10-06 blob-misfire
    lesson)."""
    objs = state.get("objects") or {}
    for entry in (state.get("stack") or []):
        kind = entry.get("kind") or {}
        data = kind.get("data") or {}
        ab = data.get("ability") or {}
        eff = ab.get("effect") or {}
        if eff.get("type") != "PutCounter":
            continue
        if eff.get("counter_type") != "P1P1":
            continue
        src_oid = str(entry.get("source_id") or ab.get("source_id") or "")
        if src_oid and obj_lname(state, src_oid) == LATHIEL_L:
            return entry
    return None


def lathiel_slot_candidates(state, opp):
    """Creature oids on the battlefield (excluding Lathiel) referenced by
    this opportunity's candidates, plus the raw candidate list."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    refs = set()
    for ch in cands:
        ref = cand_object_ref(ch)
        if ref is None:
            continue
        o = get_obj(state, ref)
        if o.get("zone") != "Battlefield":
            continue
        if is_land(o):
            continue
        if obj_lname(state, ref) == LATHIEL_L:
            continue
        refs.add(ref)
    return refs, cands, data


def build_slot_response(resp, choice_id):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = (spec.get("type") or "").lower() if isinstance(spec, dict) \
        else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [choice_id]}}
    return {"type": "choose", "data": {"choiceId": choice_id}}


async def handle_lathiel(c, state, acts, st):
    """Drive Lathiel's end-step target slots for P0. Slot 0 targets P0's
    own Bear; slot 1 tries the SAME Bear again (stacking test). The
    reported defect is observed iff the engine excludes the
    already-targeted Bear from the later slot's candidates."""
    if ST["stage"] != "ENDSTEP":
        return False
    acted = False
    ops = vi_ops(st)
    # diagnostic heartbeat: what does P0's client see at ENDSTEP?
    hb_key = ("hb", ST["slots_answered"], len(ops), vi_kind_code(st))
    if hb_key not in ST["opp_shapes_logged"]:
        ST["opp_shapes_logged"].add(hb_key)
        vi = st.get("viewer_interaction") or {}
        wire("endstep_vi_heartbeat",
             {"canSubmit": vi.get("canSubmit"),
              "n_ops": len(ops),
              "kind": vi_kind_code(st),
              "phase": state.get("phase"),
              "waiting_for": (state.get("waiting_for") or {}).get("type")})
        say(f"[P0] ENDSTEP vi: canSubmit={vi.get('canSubmit')} "
            f"n_ops={len(ops)} kind={vi_kind_code(st)} "
            f"wf={(state.get('waiting_for') or {}).get('type')}")
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if not iid or iid in SUBMITTED_OPPS:
            continue
        refs, cands, data = lathiel_slot_candidates(state, opp)
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        spec = (data.get("spec") or {})
        spec_type = (spec.get("type") or "").lower()
        shape = ("lathiel", rtype, spec_type, len(cands), len(refs))
        if shape not in ST["opp_shapes_logged"]:
            ST["opp_shapes_logged"].add(shape)
            wire("lathiel_prompt",
                 {"rtype": rtype, "spec_type": spec_type, "spec": spec,
                  "ncands": len(cands), "nrefs": len(refs),
                  "opportunity": opp})
            say(f"[P0] Lathiel slot prompt rtype={rtype} spec={spec_type} "
                f"ncands={len(cands)} nrefs={len(refs)}")
            for ch in cands:
                ref = cand_object_ref(ch)
                say(f"    cand id={ch.get('id')} ref={ref} "
                    f"name={obj_lname(state, ref) if ref else '?'} "
                    f"zone={get_obj(state, ref).get('zone') if ref else '?'} "
                    f"text={choice_text(ch)[:60]}")
        if not refs:
            # Not a Lathiel target slot (e.g. a counter-allocation or
            # priority menu choice): leave to the generic handlers.
            continue
        ST["target_prompt_seen"] = True
        if not ST["pre_exported"]:
            # The prompt itself proves the trigger fired; export the pre
            # state here in case the stack scan missed the trigger window.
            ST["trigger_seen"] = True
            ST["life_at_endstep"] = life_of(state, 0)
            ST["pre_exported"] = True
            wire("trigger_via_prompt",
                 {"p0_life": ST["life_at_endstep"]})
            say(f"Lathiel target prompt seen; P0 life="
                f"{ST['life_at_endstep']}; exporting pre_trigger.json")
            await do_export(c, "pre_trigger.json")
        slot = ST["slots_answered"]
        ref_of = {}
        for ch in cands:
            ref = cand_object_ref(ch)
            if ref is not None and ref in refs:
                ref_of[ref] = ch.get("id")
        if slot == 0:
            want_ref = None
            for r in sorted(refs):
                if str(get_obj(state, r).get("controller")) == "0":
                    want_ref = r
                    break
            if want_ref is None:
                want_ref = sorted(refs)[0]
            cid = ref_of[want_ref]
            ST["p0_bear_oid"] = want_ref
            ST["p0_bear_is_own"] = (
                str(get_obj(state, want_ref).get("controller")) == "0")
            sub = {"interactionId": iid,
                   "response": build_slot_response(resp, cid)}
            wire("slot_target_attempt",
                 {"iid": iid, "slot": slot, "ref": want_ref,
                  "name": obj_lname(state, want_ref),
                  "is_own": ST["p0_bear_is_own"],
                  "choice_id": cid, "stage": ST["stage"]})
            say(f"[P0] slot 0 targets {obj_lname(state, want_ref)} "
                f"oid={want_ref} (own={ST['p0_bear_is_own']})")
            ST["_slot_pending"] = {"iid": iid, "slot": 0}
            await interact_as(c, sub, "P0")
            SUBMITTED_OPPS.add(iid)
            ST["slots_answered"] += 1
            acted = True
        else:
            want_ref = ST["p0_bear_oid"]
            if want_ref in ref_of:
                cid = ref_of[want_ref]
                ST["stacking_tried"] = True
                sub = {"interactionId": iid,
                       "response": build_slot_response(resp, cid)}
                wire("slot_target_attempt",
                     {"iid": iid, "slot": slot, "ref": want_ref,
                      "name": obj_lname(state, want_ref),
                      "stacking": True, "choice_id": cid,
                      "stage": ST["stage"]})
                say(f"[P0] slot {slot} targets SAME "
                    f"{obj_lname(state, want_ref)} oid={want_ref} "
                    f"(stacking test)")
                ST["_slot_pending"] = {"iid": iid, "slot": slot,
                                       "stacking": True}
                await interact_as(c, sub, "P0")
                SUBMITTED_OPPS.add(iid)
                ST["slots_answered"] += 1
                acted = True
                continue
            # Decisive observation: the already-targeted creature is NOT
            # offered again -- the engine demands distinct targets.
            ST["distinct_required_observed"] = True
            wire("slot_excludes_chosen_creature",
                 {"iid": iid, "slot": slot,
                  "chosen_oid": want_ref,
                  "candidates": sorted(refs)})
            say(f"[P0] slot {slot}: chosen oid={want_ref} NOT among "
                f"candidates -- distinct targets required")
            done = next((ch for ch in cands
                         if (choice_text(ch) or "").lower().strip() in
                         ("done", "no more targets", "decline", "none",
                          "finish", "pass", "skip")), None)
            if done is not None:
                sub = {"interactionId": iid,
                       "response": build_slot_response(
                           resp, done.get("id"))}
                say(f"[P0] slot {slot} declines via "
                    f"'{choice_text(done)}'")
                wire("slot_decline", {"iid": iid, "slot": slot,
                                      "choice": choice_text(done)})
                ST["_slot_pending"] = {"iid": iid, "slot": slot,
                                       "decline": True}
                await interact_as(c, sub, "P0")
                SUBMITTED_OPPS.add(iid)
                ST["slots_answered"] += 1
                ST["declined_extra"] = True
                acted = True
                continue
            if rtype == "schema" and spec_type in ("select", "sequence"):
                say(f"[P0] slot {slot}: trying empty selection (up-to path)")
                wire("slot_empty_attempt", {"iid": iid, "slot": slot})
                ST["_slot_pending"] = {"iid": iid, "slot": slot,
                                       "decline": True}
                await interact_as(
                    c, {"interactionId": iid,
                        "response": {"type": spec_type,
                                     "data": {"choiceIds": []}}}, "P0")
                SUBMITTED_OPPS.add(iid)
                ST["slots_answered"] += 1
                ST["empty_tried"] = True
                acted = True
                continue
            # Terminal: the engine demands a distinct target for this
            # slot and offers no decline -- export the forced state.
            SUBMITTED_OPPS.add(iid)
            ST["terminal_forced"] = True
            wire("forced_distinct_terminal",
                 {"iid": iid, "slot": slot,
                  "chosen_oid": want_ref,
                  "candidates": sorted(refs)})
            say(f"TERMINAL: distinct target forced for slot {slot}, no "
                f"decline path; exporting forced state")
            await do_export(c, "post.json")
            ST["post_exported"] = True
            ST["stop"] = True
            return True
    return acted


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
    env = json.loads(s)
    inner = env.get("state")
    state = json.loads(inner) if isinstance(inner, str) else inner
    say(f"exported {path}")
    return state


async def cast_lathiel(c, state, acts, tag):
    """Cast Lathiel ({2}{G}{W}) when affordable. Engine auto-taps; gate
    on untapped lands as a sanity check."""
    if perm_oids(state, 0, LATHIEL_L):
        return False
    oid = find_hand_oid(state, 0, LATHIEL_L)
    if not oid:
        return False
    ul = untapped_lands(state, 0)
    n_forest = sum(1 for o in ul if obj_lname(state, o) == FOREST_L)
    n_plains = sum(1 for o in ul if obj_lname(state, o) == PLAINS_L)
    if not (len(ul) >= 4 and n_forest >= 1 and n_plains >= 1):
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and \
                str(a.get("data", {}).get("object_id")) == str(oid):
            ACT["cast"] = {"tag": f"lathiel-{oid}", "lname": LATHIEL_L,
                           "in_flight": True, "taps": 0,
                           "submit_t": time.time()}
            MANA_NEEDS[tag] = {"G": 1, "W": 1, "generic": 2}
            ST["lathiel_cast_turn"] = state.get("turn_number")
            say(f"[{tag}] casts Lathiel, the Bounteous Dawn ({oid})")
            wire("cast_submit", {"tag": "lathiel", "oid": oid,
                                 "turn": state.get("turn_number")})
            await submit_as_is(c, a)
            return True
    return False


async def cast_own_bear(c, state, acts, tag):
    """Cast exactly one own Bear ({1}{G}) as the 'other' friendly
    creature for the distribution test."""
    if ST["own_bear_cast"] or perm_oids(state, 0, BEAR_L):
        return False
    if not perm_oids(state, 0, LATHIEL_L):
        return False
    oid = find_hand_oid(state, 0, BEAR_L)
    if not oid:
        return False
    ul = untapped_lands(state, 0)
    n_forest = sum(1 for o in ul if obj_lname(state, o) == FOREST_L)
    if not (len(ul) >= 2 and n_forest >= 1):
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and \
                str(a.get("data", {}).get("object_id")) == str(oid):
            ACT["cast"] = {"tag": f"bear-{oid}", "lname": BEAR_L,
                           "in_flight": True, "taps": 0,
                           "submit_t": time.time()}
            MANA_NEEDS[tag] = {"G": 1, "generic": 1}
            ST["own_bear_cast"] = True
            say(f"[{tag}] casts Grizzly Bears ({oid})")
            wire("cast_submit", {"tag": "own-bear", "oid": oid,
                                 "turn": state.get("turn_number")})
            await submit_as_is(c, a)
            return True
    return False


async def attack_with_lathiel(c, state, acts, tag):
    """Attack P1 with Lathiel alone (lifelink -> gain 2). Only once
    Lathiel is past summoning sickness and the board control exists.
    The submit is retried if rejected (attack_done is set only when the
    declare actually resolves)."""
    if ST["attack_done"] or ST.get("_attack_in_flight"):
        return False
    if ST["stage"] != "ATTACK":
        return False
    laths = perm_oids(state, 0, LATHIEL_L)
    if not laths:
        return False
    if state.get("turn_number", 0) <= (ST.get("lathiel_cast_turn") or 0):
        return False
    if not perm_oids(state, 0, BEAR_L):
        return False
    if not perm_oids(state, 1, BEAR_L):
        return False
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = copy.deepcopy(a)
            # 106 DeclareAttackers expects integer oids (u64), not strings
            d.setdefault("data", {}).update(
                {"attacks": [[int(laths[0]), {"type": "Player", "data": 1}]],
                 "bands": []})
            ST["_attack_in_flight"] = True
            ST["lathiel_oid"] = int(laths[0])
            ST["p0_life_pre_attack"] = life_of(state, 0)
            say(f"[P0] attacks with Lathiel alone (turn "
                f"{state.get('turn_number')})")
            wire("attack_submit", {"data": d["data"],
                                   "turn": state.get("turn_number")})
            await submit_as_is(c, {"type": "DeclareAttackers",
                                   "data": d["data"]})
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
    if await drive_mana(c, tag):
        return True

    # Lathiel target slots take precedence; never act blindly during them
    if ST["stage"] == "ENDSTEP":
        if await handle_lathiel(c, state, acts, st):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        if ST["stage"] == "SETUP":
            if await cast_lathiel(c, state, acts, tag):
                return True
            if await cast_own_bear(c, state, acts, tag):
                return True

    phase = str(state.get("phase") or "")
    if state.get("active_player") == 0 and "declareattack" in phase.lower():
        if await attack_with_lathiel(c, state, acts, tag):
            return True
        if ST.get("_attack_in_flight"):
            # attack submit pending; never overwrite it with an empty
            # declare
            return True
        if await do_declare_empty(c, acts, st, 0, tag):
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
        # Cast bears so opponent creatures exist as legal "other" targets.
        # Two bears gives the forced-distinct observation a real target.
        if len(perm_oids(state, 1, BEAR_L)) < 2:
            oid = find_hand_oid(state, 1, BEAR_L)
            ul = untapped_lands(state, 1)
            n_forest = sum(1 for o in ul
                           if obj_lname(state, o) == FOREST_L)
            if oid and len(ul) >= 2 and n_forest >= 1:
                for a in acts:
                    if a.get("type") == "CastSpell" and \
                            str(a.get("data", {}).get("object_id")) == str(oid):
                        say(f"[{tag}] casts Grizzly Bears ({oid})")
                        wire("cast_submit", {"tag": "p1-bear", "oid": oid,
                                             "turn": state.get("turn_number")})
                        await submit_as_is(c, a)
                        return True
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

# ------------------------------------------------------------- main loop


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06891")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16891")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    P0C["c"] = p0
    P1C["c"] = p1
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    no_progress_t0 = time.time()
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        progressed = False
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                # precise slot-submit rollback: a rejected slot submit did
                # not consume the slot
                pend = ST.get("_slot_pending")
                if pend and LAST_IID["iid"] == pend["iid"]:
                    ST["slots_answered"] = max(0, ST["slots_answered"] - 1)
                    if pend["slot"] == 0 and ST["slot0_accepted"] is None:
                        ST["slot0_accepted"] = False
                        ST["single_rejection"] = rej[0][1]
                    if pend.get("stacking"):
                        ST["stacking_tried"] = False
                        if ST["stacking_accepted"] is None:
                            ST["stacking_accepted"] = False
                    ST["_slot_pending"] = None
                    say(f"[{c.name}] slot {pend['slot']} submit rejected; "
                        f"rolled back to slots_answered="
                        f"{ST['slots_answered']}")
                    wire("slot_rejected_rollback",
                         {"slot": pend["slot"],
                          "rejection": rej[0][1]})
                LAST_IID["iid"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
                # attack-submit rollback: a rejected declare did not
                # happen; clear in-flight so the next DeclareAttackers
                # phase retries
                if ST.get("_attack_in_flight"):
                    ST["_attack_in_flight"] = False
                    say(f"[{c.name}] attack submit rejected; will retry")
                    wire("attack_rejected_retry", {})
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # OR holding a pending decision opportunity
                # (missed-broadcast resilience for decisions too -- a
                # target selection does not advertise PassPriority, so
                # without this the decider never re-ticks).
                acts = top_acts(st)
                holding = my_priority(acts) or bool(vi_ops(st))
                if not holding \
                        or time.time() - last_tick_at.get(c.name, 0) <= 5:
                    continue
            last_tick_at[c.name] = time.time()
            try:
                if await tick(c, tag):
                    progressed = True
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "error": f"{type(e).__name__}: {e}"})

        if progressed:
            no_progress_t0 = time.time()

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]

        if str(state.get("phase") or "").lower() == "gameover" \
                and not ST["stop"]:
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            ST["stop"] = True
            continue

        # --- attack resolution: the declare registered once Lathiel
        # appears in the state's combat attackers. This also clears
        # _attack_in_flight so P0 stops holding and passes the
        # post-declaration priority.
        if ST.get("_attack_in_flight"):
            lath_oid = ST.get("lathiel_oid")
            combat = state.get("combat") or {}
            attacking = [str(a.get("object_id"))
                         for a in (combat.get("attackers") or [])]
            if lath_oid is not None and str(lath_oid) in attacking:
                ST["_attack_in_flight"] = False
                ST["attack_done"] = True
                ST["attack_turn"] = state.get("turn_number")
                say(f"attack resolved: Lathiel attacking "
                    f"(turn {ST['attack_turn']})")
                wire("attack_resolved", {"turn": ST["attack_turn"],
                                         "oid": lath_oid})

        # --- SETUP -> ATTACK: Lathiel + own bear on BF, P1 has a bear
        if ST["stage"] == "SETUP" \
                and perm_oids(state, 0, LATHIEL_L) \
                and perm_oids(state, 0, BEAR_L) \
                and perm_oids(state, 1, BEAR_L):
            say(f"=== stage -> ATTACK (turn {state.get('turn_number')}) ===")
            wire("stage", {"to": "ATTACK", "turn": state.get("turn_number")})
            ST["stage"] = "ATTACK"

        # --- ATTACK -> ENDSTEP: end step of the attack turn
        if ST["attack_done"] and ST["stage"] == "ATTACK" \
                and (state.get("phase") or "") == "End" \
                and state.get("active_player") == 0 \
                and state.get("turn_number") == ST["attack_turn"]:
            say(f"=== stage -> ENDSTEP (turn {state.get('turn_number')}) ===")
            wire("stage", {"to": "ENDSTEP", "turn": state.get("turn_number")})
            ST["stage"] = "ENDSTEP"

        # --- pre export: Lathiel trigger on the stack at P0's End
        if ST["stage"] == "ENDSTEP" and not ST["pre_exported"]:
            trig = lathiel_trigger_on_stack(state)
            if trig is not None:
                ST["trigger_seen"] = True
                ST["life_at_endstep"] = life_of(state, 0)
                wire("trigger_on_stack",
                     {"p0_life": ST["life_at_endstep"],
                      "stack_id": trig.get("id")})
                say(f"Lathiel trigger on stack at End; P0 life="
                    f"{ST['life_at_endstep']}")
                await do_export(p0, "pre_trigger.json")
                ST["pre_exported"] = True

        # --- slot-0 acceptance: slot 0 was consumed without rejection
        # (the rollback above would have fired on a rejection)
        if ST["slot0_accepted"] is None and ST["slots_answered"] >= 1:
            if ST["slots_answered"] >= 2 or ST["distinct_required_observed"] \
                    or ST["declined_extra"] or ST["post_exported"]:
                ST["slot0_accepted"] = True
                wire("slot0_accepted", {})

        # --- resolution: trigger answered, stack empty, no P0 decision
        # pending. On 106 the plain priority menu carries
        # waitingForKind.code == 'choose', so 'choose' counts as no
        # decision here (any schema opportunity or decide* choice would
        # make real_decision_pending true).
        if ST["stage"] == "ENDSTEP" and ST["slots_answered"] >= 1 \
                and not ST["post_exported"] and not ST["terminal_forced"]:
            pst = st_of(p0)
            if stack_empty(state) and not real_decision_pending(pst) \
                    and vi_kind_code(pst) in ("", "choose"):
                post = await do_export(p0, "post.json")
                ST["post_exported"] = True
                if ST.get("stacking_tried") \
                        and ST["stacking_accepted"] is None:
                    # the same-target submit was consumed without
                    # rejection (the rollback would have fired) and the
                    # trigger resolved
                    ST["stacking_accepted"] = True
                    wire("stacking_accepted", {})
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("post.json exported; trigger resolved; stopping")
                continue

        # --- no-progress watchdog (stall detection)
        if time.time() - no_progress_t0 > 300 and not ST.get("stop"):
            say("no progress for 300s; dumping state and stopping")
            wire("stall", {"stage": ST["stage"],
                           "kind": vi_kind_code(st_of(p0))})
            for c in (p0, p1):
                cst = st_of(c)
                if cst:
                    wire("stall_state",
                         {"who": c.name,
                          "phase": cst["state"].get("phase"),
                          "turn": cst["state"].get("turn_number"),
                          "kind": vi_kind_code(cst),
                          "acts": [a.get("type")
                                   for a in top_acts(cst)],
                          "stage": ST["stage"]})
            try:
                await do_export(p0, "stalled.json")
            except Exception as e:
                say(f"stalled export failed: {e}")
            OBS["notes"].append("300s no-progress watchdog fired")
            ST["stop"] = True
            break

        if (state.get("turn_number") or 0) > 40 and not ST.get("stop"):
            say("turn cap reached; stopping")
            wire("turn_cap", {})
            OBS["notes"].append("turn cap 40 reached")
            ST["stop"] = True
            break

    say(f"loop ended: stage={ST['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": ST["stage"]})

    # ------------------------------------------------------- assertions
    A = obs["assert"]
    D = {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                env = json.load(f)
            inner = env.get("state")
            return json.loads(inner) if isinstance(inner, str) else inner
        except Exception:
            return None

    pre_s = load("pre_trigger.json")
    post_s = load("post.json")

    # A1: setup
    lath_pre = perm_oids(pre_s, 0, LATHIEL_L) if pre_s else []
    own_bears_pre = perm_oids(pre_s, 0, BEAR_L) if pre_s else []
    p1_bears_pre = perm_oids(pre_s, 1, BEAR_L) if pre_s else []
    life_pre = ST["life_at_endstep"]
    A["A1_setup_ok"] = "passed" if (pre_s and lath_pre and own_bears_pre
                                    and p1_bears_pre and life_pre is not None
                                    and life_pre > 20) else "failed"
    D["A1_setup_ok_detail"] = (
        f"lathiel_bf={bool(lath_pre)} own_bears={len(own_bears_pre)} "
        f"p1_bears={len(p1_bears_pre)} p0_life_at_trigger={life_pre}")

    # A2: trigger fired -> P0 target selection reached
    A["A2_trigger_fired"] = "passed" if ST["target_prompt_seen"] else (
        "failed" if ST["trigger_seen"] else "not-run")
    D["A2_trigger_fired_detail"] = (
        f"trigger_on_stack={ST['trigger_seen']} "
        f"target_prompt_seen={ST['target_prompt_seen']} "
        f"slots_answered={ST['slots_answered']}")

    # A3: budget == life gained == 2
    gained = (ST["life_at_endstep"] - 20) \
        if ST["life_at_endstep"] is not None else None
    ST["life_gain"] = gained
    A["A3_budget"] = "passed" if gained == 2 else (
        "failed" if gained is not None else "not-run")
    D["A3_budget_detail"] = (f"p0_life_at_endstep={ST['life_at_endstep']} "
                             f"life_gained={gained} (expected 2)")

    # A4: slot-0 single-target submission accepted
    A["A4_slot0_accepted"] = "passed" if ST["slot0_accepted"] else (
        "failed" if ST["slots_answered"] >= 1 else "not-run")
    D["A4_slot0_accepted_detail"] = (
        f"slots_answered={ST['slots_answered']} "
        f"accepted={ST['slot0_accepted']} "
        f"p0_bear_oid={ST['p0_bear_oid']} is_own={ST['p0_bear_is_own']}")

    # A5: free distribution -- both counters may go on one creature.
    # Fails iff the engine excludes the already-targeted creature from
    # the next slot's candidates (distinct-target requirement), or the
    # same-target submission is rejected.
    own_counters = p1_counters = None
    if post_s:
        own_counters = sum(
            (get_obj(post_s, oid).get("counters") or {}).get("P1P1", 0)
            for oid in perm_oids(post_s, 0, BEAR_L))
        p1_counters = sum(
            (get_obj(post_s, oid).get("counters") or {}).get("P1P1", 0)
            for oid in perm_oids(post_s, 1, BEAR_L))
    stacking_ok = (ST["stacking_tried"] and ST["stacking_accepted"]
                   and own_counters == 2 and (p1_counters or 0) == 0)
    A["A5_free_distribution"] = "passed" if stacking_ok else (
        "failed" if (ST["distinct_required_observed"]
                     or ST["stacking_accepted"] is False) else "not-run")
    D["A5_free_distribution_detail"] = (
        f"distinct_required_observed={ST['distinct_required_observed']} "
        f"stacking_tried={ST['stacking_tried']} "
        f"stacking_accepted={ST['stacking_accepted']} "
        f"own_bear_counters={own_counters} (free distribution wants 2) "
        f"p1_bear_counters={p1_counters} (expected 0)")

    # A6: decline/up-to path for the extra slot.
    decline_ok = ST["declined_extra"] or ST["empty_tried"]
    A["A6_decline_path"] = "passed" if (
        A["A5_free_distribution"] == "passed" or decline_ok) else (
        "failed" if (ST["distinct_required_observed"]
                     and ST["target_prompt_seen"] and post_s) else
        ("not-run" if not ST["target_prompt_seen"] else "failed"))
    D["A6_decline_path_detail"] = (
        f"declined_extra={ST['declined_extra']} "
        f"empty_tried={ST['empty_tried']} "
        f"terminal_forced={ST['terminal_forced']}")

    # A7: cleanup
    A["A7_cleanup"] = "passed" if (
        post_s and stack_empty(post_s)
        and ST["stage"] == "DONE") else (
        "failed" if post_s else "not-run")
    D["A7_cleanup_detail"] = (f"post stack_empty="
                              f"{stack_empty(post_s) if post_s else None} "
                              f"stage={ST['stage']}")

    if A["A1_setup_ok"] == "failed" or A["A2_trigger_fired"] == "not-run":
        verdict = "blocked"
    elif (A["A2_trigger_fired"] == "passed"
          and A["A5_free_distribution"] == "failed"):
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"rejections": OBS.get("rejections", []),
                   "notes": OBS.get("notes", []),
                   "slots_answered": ST["slots_answered"],
                   "distinct_required_observed":
                       ST["distinct_required_observed"],
                   "life_gain": ST["life_gain"]}, f, indent=1, default=str)
    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")

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
            "P0": [[LATHIEL_T, 12], [BEAR_T, 12], [FOREST_T, 18],
                   [PLAINS_T, 18]],
            "P1": [[BEAR_T, 12], [FOREST_T, 48]],
        },
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6891.py (protocol 69 / "
            "v0.80.0) for pinned v0.103.0; behavioral contract A1..A7 and "
            "verdict logic unchanged.",
            "waiting_for is gone (null); priority = top-level PassPriority; "
            "all decisions via viewer_interaction; MulliganDecision via "
            "legacy Action; bottom via vi schema/select gated on "
            "waitingForKind.code=='mulligan'; DiscardToHandSize via vi "
            "schema/select.",
            "CastSpell via legacy Action. The v0.103.0 engine auto-taps "
            "reliably; manual taps are a >90s fallback only.",
            "Lathiel's end-step trigger is detected on the stack by the "
            "instantiated ability's own fields "
            "(kind.data.ability.effect.type==PutCounter, counter_type "
            "P1P1, source object named Lathiel) -- never by matching the "
            "whole nested trigger_entries blob.",
            "Target slots are vi opportunities whose candidates reference "
            "battlefield creatures other than Lathiel. Slot 0 targets P0's "
            "own Bear; slot 1 re-targets the SAME Bear (stacking test). "
            "The reported defect is observed iff the engine excludes the "
            "already-targeted Bear from the later slot's candidates. "
            "Decline path: done-choice, else empty selection (up-to).",
            "P0 attacks with Lathiel alone via advertised DeclareAttackers "
            "(attacks=[[oid, {type:Player, data:1}]]); P1 declares empty "
            "blockers with the 25s relations grace window.",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope); the reported OUTCOME is "
            "asserted on the saved states, not the prompt.",
            "Data-level: v0.103.0 still parses the trigger with "
            "multi_target={min:0, max:Ref(LifeGainedThisTurn)} -- the "
            "target-count cap is still tied to life gained, exactly as in "
            "v0.80.0 (area:engine).",
        ],
        "assertions": A,
        "assertion_details": D,
        "notes": OBS.get("notes", []),
        "verdict": verdict,
        "evidence_comment_id": 5645285027,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "12x Lathiel / 12x Grizzly Bears density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "Distribution with 3+ counters / 3+ targets not exercised "
            "(single 2-counter scenario).",
            "States are authoritative exports, restorable only via full "
            "game replay (scenario_6891_01020.py), not direct load.",
        ],
        "setup_line": "P0: 12x Lathiel, the Bounteous Dawn + 12x Grizzly "
                      "Bears + 18x Forest + 18x Plains; P1: 12x Grizzly "
                      "Bears + 48x Forest (casts bears as legal opponent "
                      "targets)",
        "contract_line": ("Attack with Lathiel alone (lifelink, gain 2); "
                          "at the end step, target P0's own Bear for slot "
                          "0, then re-target the SAME Bear for slot 1. "
                          "Free distribution works iff the engine still "
                          "offers the already-targeted Bear and accepts "
                          "the same-target submission."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions="
        f"{json.dumps({k: v for k, v in A.items()})}")
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
        shutil.copy(__file__, f"{EVDIR}/scenario_6891_01020.py")
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
        env = json.load(f)
    inner = env.get("state")
    return json.loads(inner) if isinstance(inner, str) else inner


def render_summary(run, out_path):
    """Render the PNG from the saved evidence files only."""
    from PIL import Image, ImageDraw
    A = json.load(open(f"{EVDIR}/assertions.json"))
    notes = A.get("notes", [])
    asserts = A.get("assertions", {})
    details = A.get("details", {})
    si = run["server_identity"]
    try:
        pre = load_ev_state("pre_trigger.json")
        laths = [oid for oid, o in (pre.get("objects") or {}).items()
                 if str(o.get("base_name") or "").lower() == LATHIEL_L
                 and o.get("zone") == "Battlefield"]
        pre_line = (f"pre_trigger: turn {pre.get('turn_number')} "
                    f"{pre.get('phase')} | Lathiel BF={bool(laths)} | "
                    f"P0 life={ST.get('life_at_endstep')}")
    except Exception:
        pre_line = "pre_trigger.json: missing"
    try:
        post = load_ev_state("post.json")
        lines = []
        for oid, o in (post.get("objects") or {}).items():
            if str(o.get("base_name") or "").lower() == BEAR_L \
                    and o.get("zone") == "Battlefield":
                lines.append(f"bear({oid},ctrl={o.get('controller')}):"
                             f"{(o.get('counters') or {}).get('P1P1', 0)}")
        post_line = "post: " + " ".join(lines)
    except Exception:
        post_line = "post.json: missing"
    W, H = 1000, 880
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    y = 20
    d.text((24, y), "Issue #6891 - Lathiel, the Bounteous Dawn: distribute "
                    "up to N counters (v0.103.0 re-validation)",
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
    d.text((24, y), post_line[:120], fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Lathiel BF, life gained, both boards ready",
        "A2_trigger_fired": "A2 Lathiel end-step trigger reached P0 "
                            "target selection",
        "A3_budget": "A3 life gained this turn == 2 (counter budget)",
        "A4_slot0_accepted": "A4 slot-0 target (own Bear) accepted",
        "A5_free_distribution": "A5 both counters may stack on one creature",
        "A6_decline_path": "A6 decline/up-to path for the extra slot",
        "A7_cleanup": "A7 stack empty, game proceeds",
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
    det = details.get("A5_free_distribution_detail", "")
    if det:
        d.text((24, y), "A5: " + det[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6891/" +
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
