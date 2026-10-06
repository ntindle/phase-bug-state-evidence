#!/usr/bin/env python3
"""Issue #6893: Shield Broker -- control-effect duration lifetime.

Oracle (pinned card-data.json v0.102.0):
  Shield Broker {3}{U}{U} 3/4 -- "When this creature enters, put a shield
  counter on target noncommander creature you don't control. You gain
  control of that creature for as long as it has a shield counter on it.
  (If it would be dealt damage or destroyed, remove a shield counter from
  it instead.)"

Triage (status:confirmed) clarified summary: "Shield Broker's control
effect can reactivate when an unrelated later effect places a new shield
counter on the formerly controlled creature, even after the original
shield counter was removed and Shield Broker is in the graveyard."

Protocol-106 port of driver/scenario_6893.py (protocol 69 / v0.80.0,
correction run 20260912-6893f, verdict reproduced) for the pinned
v0.102.0 re-validation. Protocol-106 conventions (from
scenario_6891_01020.py):
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
  - DeclareAttackers/Blockers: advertised empty submits; vi fallback only
    answers relations-schema opportunities (select/sequence schemas are
    decisions, never declares); P1 blockers get the 25s relations
    grace window.
  - play_a_land matches any land via is_land(); vi playLand choices
    answered before the decision gate.
  - real_decision_pending excludes the 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand);
    decideOptionalEffect / decideOptionalCost and schema opportunities
    are real decisions.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop covers
    priority holders AND pending decisions.
  - Export envelope: data.state is a JSON string parsed once.

Plan (native engine, v0.102.0 / protocol 106, two human-client seats):
  P0 (reporter's seat): 12x Grizzly Bears (stand-in for Witherbloom
      Apprentice) + 12x Day of Judgment + 12x Forest + 12x Plains.
  P1 (opponent): 12x Shield Broker + 12x Perrie, the Pulverizer (the
      reporter's "Perry the Piledriver") + 4x Boon of Safety (fallback
      shield-counter source) + 12x Island + 8x Plains + 8x Forest.

  1. P0 plays a Bear. P1 casts Shield Broker; its ETB targets the Bear
     (vi opportunity answered explicitly; the Bear is the P0-owned BF
     creature).
  2. P0 casts Day of Judgment. The Bear's shield counter is removed
     INSTEAD of destruction (replacement); Shield Broker is destroyed.
     Control of the Bear must revert to P0 permanently.
  3. P1 casts Perrie, the Pulverizer; its ETB ("put a shield counter on
     target creature") targets the Bear. (If the counter lands elsewhere,
     P1 casts Boon of Safety targeting the Bear as the fallback source.)
     The ended Shield Broker control effect must NOT reactivate: the Bear
     stays under P0's control one full turn later.

Behavioral contract:
  A1 broker_steal      after the Broker ETB resolves, the Bear carries
                       exactly 1 shield counter and is controlled by P1
                       (pre_wipe.json).
  A2 wipe_replacement  after Day of Judgment resolves, the Bear is on the
                       battlefield with 0 shield counters (counter consumed
                       instead of destruction) (post_wipe.json).
  A3 control_reverted  after the wipe, the Bear is controlled by P0 and
                       Shield Broker is in P1's graveyard (post_wipe.json).
  A4 no_reactivation   after the later shield counter (Perrie/Boon) is on
                       the Bear, the Bear is STILL controlled by P0, in
                       both post_perrie.json and the final post.json.
                       FAILS iff control reactivates (the reported defect).
  A5 cleanup           stack empty, game advances >=1 turn past the Perrie
                       turn with no stall.

Verdict: reproduced iff A4 fails (control reactivated by the later shield
counter -- the reported defect) or A1/A2/A3 fail as a clearly identified
related failure. not-reproduced iff A1..A5 all pass. blocked otherwise.
"""
import asyncio
import copy
import glob
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
ISSUE = 6893
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261006-6893")
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
    """Record the v0.102.0 parse of Shield Broker's ETB. The reported
    defect is the duration lifetime of the chained GainControl:
    duration ForAsLongAs(RecipientHasCounters(shield, min 1)). The engine
    must end that effect permanently once the condition first becomes
    false; a later shield counter from another source must not restart
    it."""
    b = CARD_DATA["shield broker"]
    trigs = b.get("triggers") or []
    assert trigs, "no triggers parsed for shield broker in v0.102.0"
    ex = trigs[0].get("execute") or {}
    eff = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    assert eff.get("type") == "PutCounter" \
        and eff.get("counter_type") == "shield", \
        f"unexpected ETB effect: {eff.get('type')}/{eff.get('counter_type')}"
    assert sub_eff.get("type") == "GainControl", \
        f"unexpected chained effect: {sub_eff.get('type')}"
    ev = {
        "name": b.get("name"),
        "oracle_text": b.get("oracle_text"),
        "etb_effect": eff.get("type"),
        "counter_type": eff.get("counter_type"),
        "chained_effect": sub_eff.get("type"),
        "chained_target": sub_eff.get("target"),
        "chained_duration": sub.get("duration"),
        "sub_link": ex.get("sub_link"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1, default=str)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": b.get("name"),
                   "oracle_text": b.get("oracle_text"),
                   "triggers": trigs}, f, indent=1, default=str)
    say(f"data-level: ETB PutCounter(shield) -> chained GainControl "
        f"duration={json.dumps(sub.get('duration'), default=str)[:160]}")
    wire("data_level", ev)


BROKER_T = "Shield Broker"
PERRIE_T = "Perrie, the Pulverizer"
BOON_T = "Boon of Safety"
BEAR_T = "Grizzly Bears"
DOJ_T = "Day of Judgment"
ISLAND_T = "Island"
FOREST_T = "Forest"
PLAINS_T = "Plains"

BROKER_L = "shield broker"
PERRIE_L = "perrie, the pulverizer"
BOON_L = "boon of safety"
BEAR_L = "grizzly bears"
DOJ_L = "day of judgment"
ISLAND_L = "island"
FOREST_L = "forest"
PLAINS_L = "plains"

P0_DECK = ((BEAR_T, 12), (DOJ_T, 12), (FOREST_T, 12), (PLAINS_T, 12))
P1_DECK = ((BROKER_T, 12), (PERRIE_T, 12), (BOON_T, 4), (ISLAND_T, 12),
           (PLAINS_T, 8), (FOREST_T, 8))

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0,
      "broker_cast": False, "broker_etb_answered": False,
      "wipe_cast": False,
      "perrie_cast": False, "perrie_cast_t": None,
      "perrie_etb_answered": False, "perrie_missed": False,
      "boon_cast": False, "boon_answered": False,
      "perrie_turn": None, "bear_oid": None, "broker_oid": None,
      "counter_source": None,  # "perrie" | "boon"
      "rejections": [], "turns_seen": set(), "stall_observed": False,
      "stages_reached": ["SETUP"],
      "pre_exported": False, "post_exported": False,
      "opp_shapes_logged": set(), "p1_block_grace_until": 0,
      "bear_cast": False}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {}
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
        choice = "Keep" if ((BEAR_L in hand or DOJ_L in hand)
                            and n_lands >= 2) or n >= 2 \
            else "Mulligan"
    else:
        choice = "Keep" if ((BROKER_L in hand or PERRIE_L in hand)
                            and n_lands >= 2) or n >= 2 \
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

        keep = {BEAR_L, DOJ_L} if tag == "P0" else {BROKER_L, PERRIE_L, BOON_L}

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
    if ln in (BEAR_L, DOJ_L):
        return 3
    if is_land(get_obj(state, o)):
        return 1
    return 0


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln in (BROKER_L, PERRIE_L, BOON_L):
        return 3
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
    Fallback only: the v0.102.0 engine auto-taps reliably."""
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
    """Mana fallback: the v0.102.0 engine auto-taps reliably, so manual
    tapping engages only if a cast sits unannounced for >90s with a tap
    menu visible (logged loudly)."""
    cs = ACT.get(tag)
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

# ------------------------------------------------------------- 6893 logic


def set_stage(s):
    if ST["stage"] != s:
        ST["stage"] = s
        ST["stages_reached"].append(s)
        wire("stage", {"stage": s})
        say(f"STAGE -> {s}")


def shield_count(o):
    c = o.get("counters")
    if isinstance(c, dict):
        for k in ("shield", "Shield", "SHIELD"):
            if k in c:
                try:
                    return int(c[k])
                except Exception:
                    pass
        return 0
    if isinstance(c, list):
        n = 0
        for cc in c:
            if isinstance(cc, dict) \
                    and str(cc.get("kind", "")).lower() == "shield":
                try:
                    n += int(cc.get("count", 0))
                except Exception:
                    pass
        return n
    return 0


def find_bear(state):
    """The P0-owned Bear on the battlefield (the Witherbloom stand-in)."""
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield"
                and obj_lname(state, oid) == BEAR_L
                and o.get("owner") == 0):
            return str(oid), o
    return None, None


def broker_in_gy(state):
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Graveyard"
                and obj_lname(state, oid) == BROKER_L
                and o.get("controller") in (1, "1")):
            return True
    return False


def etb_creature_refs(state, opp):
    """BF non-land creature oids referenced by this opportunity's
    candidates."""
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
        refs.add(ref)
    return refs, cands, data


def build_slot_response(resp, choice_id):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = (spec.get("type") or "").lower() \
        if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [choice_id]}}
    return {"type": "choose", "data": {"choiceId": choice_id}}


async def handle_etb(c, state, acts, st, flag_key, card_label, tag):
    """Answer one ETB target prompt for P1 by choosing the P0-owned Bear
    (the Witherbloom stand-in). Only schema opportunities whose candidates
    reference BF creatures are considered; the P0-owned Bear must be among
    them. Hand-card opportunities (mulligan bottom, discard) never match
    the Battlefield filter."""
    if ST[flag_key]:
        return False
    acted = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if not iid or iid in SUBMITTED_OPPS:
            continue
        refs, cands, data = etb_creature_refs(state, opp)
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        spec = data.get("spec") or {}
        spec_type = (spec.get("type") or "").lower()
        if not (rtype == "schema" and spec_type in ("sequence", "select")):
            continue
        shape = ("etb", card_label, rtype, spec_type, len(cands), len(refs))
        if shape not in ST["opp_shapes_logged"]:
            ST["opp_shapes_logged"].add(shape)
            wire("etb_prompt", {"card": card_label, "rtype": rtype,
                                "spec_type": spec_type,
                                "ncands": len(cands), "nrefs": len(refs),
                                "opportunity": opp})
            say(f"[{tag}] {card_label} prompt rtype={rtype} spec={spec_type} "
                f"ncands={len(cands)} nrefs={len(refs)}")
        if not refs:
            continue
        bear_ref = None
        for r in sorted(refs):
            o = get_obj(state, r)
            if (obj_lname(state, r) == BEAR_L and o.get("owner") == 0
                    and o.get("zone") == "Battlefield"):
                bear_ref = r
                break
        if bear_ref is None:
            continue
        ref_of = {}
        for ch in cands:
            ref = cand_object_ref(ch)
            if ref is not None and ref in refs:
                ref_of[ref] = ch.get("id")
        cid = ref_of.get(bear_ref)
        if not cid:
            continue
        sub = {"interactionId": iid,
               "response": build_slot_response(resp, cid)}
        wire("etb_answer", {"card": card_label, "iid": iid,
                            "ref": bear_ref,
                            "name": obj_lname(state, bear_ref),
                            "choice_id": cid, "stage": ST["stage"]})
        say(f"[{tag}] {card_label} ETB targets "
            f"{obj_lname(state, bear_ref)} oid={bear_ref}")
        ST["_etb_pending"] = {"iid": iid, "flag": flag_key}
        await interact_as(c, sub, tag)
        SUBMITTED_OPPS.add(iid)
        ST[flag_key] = True
        acted = True
    return acted


async def observe_stages(p0, state):
    """Advance the 6893 stage machine from authoritative state; export
    the pre/post checkpoints."""
    stage = ST["stage"]
    bear_oid, bear = find_bear(state)

    if stage == "SETUP" and ST["broker_cast"] and bear is not None:
        if (shield_count(bear) == 1 and bear.get("controller") == 1
                and stack_empty(state)):
            ST["bear_oid"] = bear_oid
            for oid in (state.get("objects") or {}):
                o = get_obj(state, oid)
                if (o.get("zone") == "Battlefield"
                        and obj_lname(state, oid) == BROKER_L
                        and o.get("controller") == 1):
                    ST["broker_oid"] = str(oid)
            say(f"Broker ETB resolved: Bear oid={bear_oid} shield=1 "
                f"controller=1")
            wire("broker_etb_resolved", {"bear_oid": bear_oid})
            await do_export(p0, "pre_wipe.json")
            ST["pre_exported"] = True
            set_stage("BROKER_RESOLVED")

    elif stage == "BROKER_RESOLVED" and ST["wipe_cast"] and bear is not None:
        if (bear.get("zone") == "Battlefield" and shield_count(bear) == 0
                and bear.get("controller") == 0 and broker_in_gy(state)
                and stack_empty(state)):
            say("Wipe resolved: Bear survived with counter consumed, "
                "control reverted to P0, Broker in P1 graveyard")
            wire("wipe_resolved", {})
            await do_export(p0, "post_wipe.json")
            set_stage("WIPE_RESOLVED")

    elif stage == "WIPE_RESOLVED" and ST["perrie_cast"] and bear is not None:
        if stack_empty(state) and shield_count(bear) >= 1:
            ST["counter_source"] = ST["counter_source"] or "perrie"
            ST["perrie_turn"] = state.get("turn_number")
            say(f"Perrie ETB resolved: Bear shield={shield_count(bear)} "
                f"controller={bear.get('controller')}")
            wire("perrie_counter", {"bear_oid": bear_oid,
                                    "shield": shield_count(bear),
                                    "controller": bear.get("controller")})
            await do_export(p0, "post_perrie.json")
            set_stage("PERRIE_RESOLVED")
        elif stack_empty(state) and ST["perrie_turn"] is None and (
                ST["perrie_etb_answered"]
                or (ST["perrie_cast_t"] is not None
                    and time.time() - ST["perrie_cast_t"] > 60)):
            # Perrie's counter never landed on the Bear; advance so the
            # Boon of Safety fallback can supply the later counter.
            ST["perrie_missed"] = True
            ST["perrie_turn"] = state.get("turn_number")
            say("Perrie counter missed the Bear; advancing to Boon fallback")
            wire("perrie_missed", {})
            set_stage("PERRIE_RESOLVED")

    elif stage == "PERRIE_RESOLVED" and ST["boon_cast"] and bear is not None:
        if stack_empty(state) and shield_count(bear) >= 1:
            ST["counter_source"] = "boon"
            ST["perrie_turn"] = state.get("turn_number")
            say(f"Boon resolved: Bear shield={shield_count(bear)} "
                f"controller={bear.get('controller')}")
            wire("boon_counter", {"bear_oid": bear_oid,
                                  "shield": shield_count(bear),
                                  "controller": bear.get("controller")})
            await do_export(p0, "post_perrie.json")
            set_stage("PERRIE_RESOLVED_DONE")

    if ST["stage"] in ("PERRIE_RESOLVED", "PERRIE_RESOLVED_DONE") \
            and ST["perrie_turn"] is not None \
            and ST["counter_source"] is not None \
            and not ST["post_exported"]:
        turn = state.get("turn_number") or 0
        if turn >= ST["perrie_turn"] + 1 and stack_empty(state):
            post = await do_export(p0, "post.json")
            ST["post_exported"] = True
            _b2oid, b2 = find_bear(post)
            say(f"post.json exported: bear shield="
                f"{shield_count(b2) if b2 else None} controller="
                f"{b2.get('controller') if b2 else None}")
            wire("post_exported", {})
            set_stage("DONE")
            ST["stop"] = True



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


LAND_COLOR = {FOREST_L: "G", PLAINS_L: "W", ISLAND_L: "U"}


def mana_ok(state, pid, total, colors):
    ul = untapped_lands(state, pid)
    if len(ul) < total:
        return False
    have = {}
    for o in ul:
        col = LAND_COLOR.get(obj_lname(state, o))
        if col:
            have[col] = have.get(col, 0) + 1
    return all(have.get(c, 0) >= n for c, n in colors.items())


async def cast_named(c, state, acts, tag, pid, lname, total, colors,
                     flag_key, cast_label):
    """Cast one named spell via the advertised CastSpell action. The
    v0.102.0 engine auto-taps reliably; drive_mana is the >90s fallback."""
    oid = find_hand_oid(state, pid, lname)
    if not oid or not mana_ok(state, pid, total, colors):
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and \
                str(a.get("data", {}).get("object_id")) == str(oid):
            ACT[tag] = {"tag": f"{cast_label}-{oid}", "lname": lname,
                        "in_flight": True, "taps": 0,
                        "submit_t": time.time()}
            MANA_NEEDS[tag] = dict(colors)
            MANA_NEEDS[tag]["generic"] = total - sum(colors.values())
            ST[flag_key] = True
            say(f"[{tag}] casts {cast_label} ({oid})")
            wire("cast_submit", {"tag": cast_label, "oid": oid,
                                 "turn": state.get("turn_number")})
            await submit_as_is(c, a)
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
    if await drive_mana(c, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        if ST["stage"] == "SETUP" and not ST["bear_cast"]:
            if not perm_oids(state, 0, BEAR_L):
                if await cast_named(c, state, acts, tag, 0, BEAR_L, 2,
                                    {"G": 1}, "bear_cast", "Grizzly Bears"):
                    return True
        elif ST["stage"] == "BROKER_RESOLVED" and not ST["wipe_cast"]:
            if await cast_named(c, state, acts, tag, 0, DOJ_L, 4,
                                {"W": 2}, "wipe_cast", "Day of Judgment"):
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
    if not ST["broker_etb_answered"] and ST["broker_cast"]:
        if await handle_etb(c, state, acts, st, "broker_etb_answered",
                            "Shield Broker", tag):
            return True
    if not ST["perrie_etb_answered"] and ST["perrie_cast"]:
        if await handle_etb(c, state, acts, st, "perrie_etb_answered",
                            "Perrie", tag):
            return True
    if not ST["boon_answered"] and ST["boon_cast"]:
        if await handle_etb(c, state, acts, st, "boon_answered",
                            "Boon of Safety", tag):
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
        if ST["stage"] == "SETUP" and not ST["broker_cast"]:
            bear_oid, _bear = find_bear(state)
            if bear_oid is not None:
                if await cast_named(c, state, acts, tag, 1, BROKER_L, 5,
                                    {"U": 2}, "broker_cast",
                                    "Shield Broker"):
                    return True
        elif ST["stage"] == "WIPE_RESOLVED" and not ST["perrie_cast"]:
            if await cast_named(c, state, acts, tag, 1, PERRIE_L, 4,
                                {"G": 1, "W": 1, "U": 1}, "perrie_cast",
                                "Perrie, the Pulverizer"):
                ST["perrie_cast_t"] = time.time()
                return True
        elif ST["stage"] == "PERRIE_RESOLVED" and not ST["boon_cast"] \
                and ST["counter_source"] != "perrie":
            bear_oid, bear = find_bear(state)
            if bear is not None and shield_count(bear) == 0:
                if await cast_named(c, state, acts, tag, 1, BOON_L, 1,
                                    {"W": 1}, "boon_cast",
                                    "Boon of Safety"):
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
        ACT[k] = None
    log_seek = find_server_log_size()

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06893")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16893")
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
                # ETB-submit rollback: a rejected ETB answer did not
                # consume the prompt; clear the flag so it is answered
                # again.
                pend = ST.get("_etb_pending")
                if pend and LAST_IID["iid"] == pend["iid"]:
                    ST[pend["flag"]] = False
                    ST["_etb_pending"] = None
                    say(f"[{c.name}] ETB submit rejected; flag "
                        f"{pend['flag']} cleared for retry")
                    wire("etb_rejected_rollback",
                         {"flag": pend["flag"], "rejection": rej[0][1]})
                LAST_IID["iid"] = None
                OBS["rejections"].extend(
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

        # --- 6893 stage machine: observe authoritative state, export
        # checkpoints, advance stages.
        _turn_now = state.get("turn_number") or 0
        if _turn_now >= 1:
            ST["turns_seen"].add(_turn_now)
        try:
            await observe_stages(p0, state)
        except Exception as e:
            say(f"observe error: {type(e).__name__}: {e}")
            wire("observe_error", {"error": f"{type(e).__name__}: {e}"})

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

        if (state.get("turn_number") or 0) > 40 and not ST.get("stop") \
                and ST["stage"] in ("SETUP", "BROKER_RESOLVED",
                                    "WIPE_RESOLVED"):
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

    pre_wipe = load("pre_wipe.json")
    post_wipe = load("post_wipe.json")
    post_perrie = load("post_perrie.json")
    post = load("post.json")

    def bear_in(s):
        if not s:
            return None
        for oid, o in (s.get("objects") or {}).items():
            if (o.get("zone") == "Battlefield"
                    and str(o.get("base_name") or o.get("name")
                            or "").lower() == BEAR_L
                    and o.get("owner") == 0):
                return str(oid), o
        return None

    def bear_desc(b):
        if not b:
            return "no P0-owned bear on BF"
        return (f"bear oid={b[0]} shield={shield_count(b[1])} "
                f"controller={b[1].get('controller')} zone={b[1].get('zone')}")

    # A1: Broker ETB resolved -> Bear has exactly 1 shield, controlled by P1
    b = bear_in(pre_wipe)
    A["A1_broker_steal"] = (
        "passed" if (b and shield_count(b[1]) == 1
                     and b[1].get("controller") == 1)
        else ("failed" if b else "not-run"))
    D["A1_broker_steal_detail"] = bear_desc(b) + " (pre_wipe.json)"

    # A2: wipe -> Bear survived, counter consumed (0 shields)
    b = bear_in(post_wipe)
    A["A2_wipe_replacement"] = (
        "passed" if (b and b[1].get("zone") == "Battlefield"
                     and shield_count(b[1]) == 0)
        else ("failed" if b else "not-run"))
    D["A2_wipe_replacement_detail"] = (
        bear_desc(b) + " (post_wipe.json): shield counter removed instead "
        "of destruction")

    # A3: control reverted to P0; Broker in P1 graveyard
    b = bear_in(post_wipe)
    broker_gy = broker_in_gy(post_wipe) if post_wipe else False
    A["A3_control_reverted"] = (
        "passed" if (b and b[1].get("controller") == 0 and broker_gy)
        else ("failed" if b is not None else "not-run"))
    D["A3_control_reverted_detail"] = (
        f"bear_controller={b[1].get('controller') if b else None} "
        f"broker_in_p1_graveyard={broker_gy} (post_wipe.json)")

    # A4: the later shield counter must NOT reactivate the ended effect
    b_pp = bear_in(post_perrie)
    b_post = bear_in(post)
    pp_ok = (b_pp and shield_count(b_pp[1]) >= 1
             and b_pp[1].get("controller") == 0)
    post_ok = (b_post and shield_count(b_post[1]) >= 1
               and b_post[1].get("controller") == 0)
    A["A4_no_reactivation"] = (
        "passed" if (pp_ok and post_ok)
        else ("failed" if (b_pp and shield_count(b_pp[1]) >= 1) else
              "not-run"))
    D["A4_no_reactivation_detail"] = (
        f"counter_source={ST['counter_source']} "
        f"post_perrie: {bear_desc(b_pp)}; post: {bear_desc(b_post)}")

    # A5: cleanup -- stack empty, game advanced >=1 turn past Perrie turn
    turns_past = ((max(ST["turns_seen"]) - ST["perrie_turn"])
                  if ST["perrie_turn"] is not None and ST["turns_seen"]
                  else 0)
    A["A5_cleanup"] = (
        "passed" if (ST["stage"] == "DONE" and not ST["stall_observed"]
                     and turns_past >= 1 and post is not None
                     and stack_empty(post))
        else ("failed" if ST["stall_observed"] else "not-run"))
    D["A5_cleanup_detail"] = (
        f"stage={ST['stage']} turns_past_perrie={turns_past} "
        f"stall={ST['stall_observed']} stages={ST['stages_reached']}")

    if A["A4_no_reactivation"] == "failed":
        verdict = "reproduced"
    elif A["A1_broker_steal"] == "failed" \
            or A["A2_wipe_replacement"] == "failed" \
            or A["A3_control_reverted"] == "failed":
        verdict = "reproduced"  # related failure of the same card/effect
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    elif not any(v == "failed" for v in A.values()):
        verdict = "blocked"
    else:
        verdict = "reproduced"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"rejections": OBS.get("rejections", []),
                   "notes": OBS.get("notes", []),
                   "stages_reached": ST["stages_reached"],
                   "counter_source": ST["counter_source"],
                   "bear_oid": ST["bear_oid"],
                   "broker_oid": ST["broker_oid"],
                   "turns_seen": sorted(ST["turns_seen"])},
                  f, indent=1, default=str)
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
            "P0": [[BEAR_T, 12], [DOJ_T, 12], [FOREST_T, 12],
                   [PLAINS_T, 12]],
            "P1": [[BROKER_T, 12], [PERRIE_T, 12], [BOON_T, 4],
                   [ISLAND_T, 12], [PLAINS_T, 8], [FOREST_T, 8]],
        },
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6893.py (protocol 69 / "
            "v0.80.0, correction run 20260912-6893f) for pinned v0.102.0; "
            "behavioral contract A1..A5 and verdict logic unchanged.",
            "waiting_for is gone (null); priority = top-level PassPriority; "
            "all decisions via viewer_interaction; MulliganDecision via "
            "legacy Action; bottom via vi schema/select gated on "
            "waitingForKind.code=='mulligan'; DiscardToHandSize via vi "
            "schema/select.",
            "CastSpell via legacy Action. The v0.102.0 engine auto-taps "
            "reliably; manual taps are a >90s fallback only.",
            "Shield Broker's ETB and Perrie's ETB target selections are vi "
            "schema opportunities whose candidates reference battlefield "
            "creatures; the driver answers each once by choosing the "
            "P0-owned Bear (the Witherbloom stand-in).",
            "Stage machine on authoritative state: SETUP (P0 bear, P1 "
            "Broker) -> BROKER_RESOLVED (pre_wipe.json: bear 1 shield, "
            "controlled by P1) -> WIPE_RESOLVED (post_wipe.json: bear 0 "
            "shields, control reverted, Broker in P1 graveyard) -> "
            "PERRIE_RESOLVED (post_perrie.json: later shield counter on "
            "the bear) -> DONE (post.json one turn later). Boon of Safety "
            "is the fallback later-counter source if Perrie's counter "
            "misses the Bear.",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope); the reported OUTCOME -- "
            "whether the later shield counter reactivates the ended "
            "control effect -- is asserted on the saved states, not the "
            "prompt.",
            "Data-level: v0.102.0 parses Shield Broker's ETB as "
            "PutCounter(shield, 1) chaining GainControl("
            "target=ParentTarget, "
            "duration=ForAsLongAs(RecipientHasCounters(shield, min 1))) -- "
            "unchanged from v0.80.0 (area:engine).",
        ],
        "assertions": A,
        "assertion_details": D,
        "notes": OBS.get("notes", []),
        "verdict": verdict,
        "evidence_comment_id": 5645773585,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "12x card density is a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "Grizzly Bears stands in for the reporter's Witherbloom "
            "Apprentice as the stolen creature (both ordinary noncommander "
            "creatures).",
            "Perrie, the Pulverizer is the reporter's 'Perry the "
            "Piledriver' (the commander that re-entered and placed the "
            "later shield counter); Boon of Safety is a fallback "
            "shield-counter source used only if Perrie's counter does not "
            "land on the Bear.",
            "Not tested on the original 2026-08-02 build; verdict is "
            "scoped to v0.102.0, not a fix claim.",
            "States are authoritative exports, restorable only via full "
            "game replay (scenario_6893_01020.py), not direct load.",
        ],
        "setup_line": "P0: 12x Grizzly Bears + 12x Day of Judgment + "
                      "12x Forest + 12x Plains; P1: 12x Shield Broker + 12x "
                      "Perrie, the Pulverizer + 4x Boon of Safety + 12x "
                      "Island + 8x Plains + 8x Forest",
        "contract_line": ("P1 casts Shield Broker, ETB targets P0's "
                          "Bear (P1 gains control); P0 casts Day of "
                          "Judgment (Bear's shield counter consumed "
                          "instead of destruction, control reverts); P1 "
                          "casts Perrie, ETB targets the Bear (later "
                          "shield counter). The ended Shield Broker "
                          "control effect must NOT reactivate: the Bear "
                          "stays under P0's control one full turn later."),
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
        shutil.copy(__file__, f"{EVDIR}/scenario_6893_01020.py")
        say("copied driver into evidence dir")
    except Exception:
        say("scenario copy FAILED:")
        say(traceback.format_exc())
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        write_server_excerpts(log_seek)
    except Exception:
        say("write_server_excerpts FAILED:")
        say(traceback.format_exc())
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
    def bear_line(path, label):
        try:
            s = load_ev_state(path)
            parts = []
            for oid, o in (s.get("objects") or {}).items():
                if str(o.get("base_name") or "").lower() == BEAR_L \
                        and o.get("zone") == "Battlefield":
                    parts.append(f"bear({oid},ctrl={o.get('controller')},"
                                 f"sh={shield_count(o)})")
            return f"{label}: " + (" ".join(parts) or "no bear BF")
        except Exception:
            return f"{label}: missing"
    pre_line = bear_line("pre_wipe.json", "pre_wipe")
    wipe_line = bear_line("post_wipe.json", "post_wipe")
    perrie_line = bear_line("post_perrie.json", "post_perrie")
    post_line = bear_line("post.json", "post")
    W, H = 1000, 880
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    y = 20
    d.text((24, y), "Issue #6893 - Shield Broker: control-effect "
                    "duration lifetime (v0.102.0 revalidation)",
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
    d.text((24, y), pre_line[:118], fill=(170, 180, 195))
    y += 24
    d.text((24, y), wipe_line[:118], fill=(170, 180, 195))
    y += 24
    d.text((24, y), perrie_line[:118], fill=(170, 180, 195))
    y += 24
    d.text((24, y), post_line[:118], fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_broker_steal": "A1 Broker ETB: Bear 1 shield, controlled by P1",
        "A2_wipe_replacement": "A2 wipe: Bear survives, counter consumed",
        "A3_control_reverted": "A3 control reverted, Broker in P1 graveyard",
        "A4_no_reactivation": "A4 later shield counter does NOT reactivate",
        "A5_cleanup": "A5 stack empty, game advances past Perrie turn",
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
    det = details.get("A4_no_reactivation_detail", "")
    if det:
        d.text((24, y), "A4: " + det[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6893/" +
           run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


def find_server_log():
    cands = glob.glob(f"{BACKFILL}/runs/*/server.log")
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def find_server_log_size():
    slog = find_server_log()
    if slog is None:
        return None
    try:
        return os.path.getsize(slog)
    except OSError:
        return None


def write_server_excerpts(log_seek):
    slog = find_server_log()
    if slog is None or log_seek is None:
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("# server log unavailable (shared server log not found)\n")
        say("server log not found; wrote stub excerpts")
        return
    with open(slog, "rb") as f:
        f.seek(log_seek)
        tail = f.read().decode("utf-8", "replace").splitlines()
    tail = tail[-300:]
    with open(f"{EVDIR}/server_excerpts.log", "w") as f:
        f.write("\n".join(tail) + "\n")
    say(f"server excerpts written ({len(tail)} lines from {slog})")


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
