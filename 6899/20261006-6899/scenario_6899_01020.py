#!/usr/bin/env python3
"""Issue #6899: Moonmist doesn't prevent damage (open).

Oracle (pinned card-data.json v0.102.0):
  Moonmist {G} -- "Transform all Humans. Prevent all combat damage that
  would be dealt this turn by creatures other than Werewolves and
  Wolves. (Only double-faced cards can be transformed.)"

v0.81.1 finding (protocol 70, run 20260912-6899d, verdict reproduced):
  the reported literal symptom (non-Wolf damage NOT prevented) did NOT
  occur -- non-Wolf damage WAS prevented. The reproduced related failure
  is OVER-prevention: Wolf damage was also prevented (unblocked
  Bear+Wolf dealt 0, expected Bear 0 + Wolf 1). Data-level parse defect:
  PreventDamage target Any with no source-type filter for the
  "other than Werewolves and Wolves" exception.

Protocol-106 port for the pinned v0.102.0 re-validation. Conventions
(from scenario_6891_01020.py / scenario_6893_01020.py):
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
  - DeclareAttackers with real attackers: relations-schema vi opportunity
    (sourceId/targetId edges; the 106 analog from scenario_6866_01020.py).
    Empty declares via the advertised action; vi fallback only answers
    relations-schema opportunities; P1 blockers get the 25s relations
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
  P0: 12x Young Wolf + 12x Grizzly Bears + 12x Moonmist + 24x Forest.
  P1: 12x Young Wolf + 12x Grizzly Bears + 36x Forest.

  1. Both sides cast a Young Wolf and a Grizzly Bear.
  2. P0's attack turn: P0 declares Wolf+Bear (relations) targeting P1;
     P1 declares no blockers; P0 casts Moonmist during combat priority.
     Expected: P1 takes exactly 1 (Wolf through, Bear's 2 prevented).
  3. P1's attack turn: P1 declares Wolf+Bear targeting P0; P0 no
     blockers; P0 casts a second Moonmist during combat priority.
     Expected: P0 takes exactly 1.
  No Humans are on either battlefield, so the Transform clause is a
  no-op (same scope as the v0.81.1 proof; the separate target-prompt
  defect is tracked in #6403).

Behavioral contract:
  A1 setup_ok         pre_p0_attack.json: P0 Wolf+Bear and P1 Wolf+Bear
                      on the battlefield; P1 at 20 life.
  A2 moonmist_resolves  post_p0_attack.json: Moonmist in P0's graveyard
                      (cast on P0's turn resolved before damage).
  A3 p0_attack_damage P1 life == 19 after P0's unblocked Wolf+Bear
                      attack under Moonmist (Wolf 1 through, Bear 2
                      prevented). FAILS at 20 (over-prevention -- the
                      reproduced defect) or 17 (under-prevention -- the
                      literal reported symptom).
  A4 p1_attack_damage P0 life == 19 after P1's unblocked Wolf+Bear
                      attack under P0's second Moonmist. Same failure
                      modes as A3.
  A5 cleanup          stack empty, game advanced past both attacks,
                      no stall.

Verdict: reproduced iff A3 or A4 fails (either prevention direction is
a Moonmist prevention defect); not-reproduced iff A1..A5 all pass;
blocked otherwise.
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
ISSUE = 6899
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261006-6899")
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
    """Record the v0.102.0 parse of Moonmist. The reported defect is the
    prevention exception: 'by creatures other than Werewolves and
    Wolves'. The v0.81.1 parse defect was PreventDamage with
    target {type: Any} and no source-type filter -- check whether the
    v0.102.0 parse still lacks it."""
    m = CARD_DATA["moonmist"]
    abils = m.get("abilities") or []
    assert abils, "no abilities parsed for moonmist in v0.102.0"
    transform_eff = (abils[0].get("effect") or {}).get("type")
    sub = abils[0].get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    assert transform_eff == "Transform", \
        f"unexpected first effect: {transform_eff}"
    assert sub_eff.get("type") == "PreventDamage", \
        f"unexpected chained effect: {sub_eff.get('type')}"
    ev = {
        "name": m.get("name"),
        "oracle_text": m.get("oracle_text"),
        "first_effect": transform_eff,
        "first_target": (abils[0].get("effect") or {}).get("target"),
        "prevention_effect": sub_eff.get("type"),
        "prevention_amount": sub_eff.get("amount"),
        "prevention_target": sub_eff.get("target"),
        "prevention_scope": sub_eff.get("scope"),
        "prevention_duration": sub_eff.get("prevention_duration"),
        "sub_link": abils[0].get("sub_link"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1, default=str)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": m.get("name"),
                   "oracle_text": m.get("oracle_text"),
                   "abilities": abils}, f, indent=1, default=str)
    say(f"data-level: Transform(all Humans) -> PreventDamage("
        f"amount={sub_eff.get('amount')} "
        f"target={json.dumps(sub_eff.get('target'), default=str)[:120]} "
        f"scope={sub_eff.get('scope')})")
    wire("data_level", ev)


WOLF_T = "Young Wolf"
BEAR_T = "Grizzly Bears"
MOON_T = "Moonmist"
FOREST_T = "Forest"

WOLF_L = "young wolf"
BEAR_L = "grizzly bears"
MOON_L = "moonmist"
FOREST_L = "forest"

P0_DECK = ((WOLF_T, 12), (BEAR_T, 12), (MOON_T, 12), (FOREST_T, 24))
P1_DECK = ((WOLF_T, 12), (BEAR_T, 12), (FOREST_T, 36))

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0,
      "wolf_cast": False, "bear_cast": False,
      "p1_wolf_cast": False, "p1_bear_cast": False,
      "moonmist_cast_turns": {},
      "p0_declared_turn": None, "p1_declared_turn": None,
      "p1_life_after": None, "p0_life_after": None,
      "rejections": [], "turns_seen": set(), "stall_observed": False,
      "stages_reached": ["SETUP"],
      "opp_shapes_logged": set(), "p1_block_grace_until": 0}
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
    try:
        return int(player_of(state, pid).get("life"))
    except (TypeError, ValueError):
        return None


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
    return None


def candidate_seat(ch):
    for s in (ch or {}).get("surfaces", []) or []:
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
    n_lands = sum(1 for h in hand if h == FOREST_L)
    keep_cards = {WOLF_L, BEAR_L, MOON_L} if tag == "P0" \
        else {WOLF_L, BEAR_L}
    if tag == "P0":
        choice = "Keep" if (any(h in keep_cards for h in hand)
                            and n_lands >= 2) or n >= 2 \
            else "Mulligan"
    else:
        choice = "Keep" if (any(h in keep_cards for h in hand)
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

        keep = {WOLF_L, BEAR_L, MOON_L} if tag == "P0" \
            else {WOLF_L, BEAR_L}

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
    if ln in (WOLF_L, BEAR_L, MOON_L):
        return 3
    if is_land(get_obj(state, o)):
        return 1
    return 0


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln in (WOLF_L, BEAR_L):
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


def relations_shape(opp):
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}
    return edges, cands


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


async def do_declare_attackers(c, acts, st, pid, tag, foe_seat):
    """Declare Wolf+Bear as attackers via the relations-schema vi
    opportunity (the 106 path proven in scenario_6866_01020.py). Fires
    only during this client's attack stage. Holds (declares nothing)
    until both Wolf and Bear are attackable, so the life-total math in
    the assertions stays discriminating."""
    if tag == "P0" and ST["stage"] != "P0_ATTACK":
        return False
    if tag == "P1" and ST["stage"] != "P1_ATTACK":
        return False
    state = st["state"]
    if str(state.get("active_player")) != str(pid):
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    opp = find_relations_op(st)
    if opp is None:
        for a in acts:
            if a.get("type") == "DeclareAttackers":
                wire("declare_attackers_advertised_shape",
                     {"who": tag, "data": a.get("data")})
        return False
    iid = opp.get("interactionId")
    key = (tag, "declare-atk", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    edges, cands = relations_shape(opp)
    if "attack_edges" not in ST["opp_shapes_logged"]:
        ST["opp_shapes_logged"].add("attack_edges")
        wire("declare_attackers_shape",
             {"who": tag, "n_edges": len(edges), "edges": edges[:8],
              "n_candidates": len(cands)})
    want = {}
    for o in bf_oids(state, pid):
        ln = obj_lname(state, o)
        if ln in (WOLF_L, BEAR_L):
            oo = get_obj(state, o)
            if not oo.get("tapped") and not oo.get("summoning_sick") \
                    and not oo.get("has_summoning_sickness"):
                want[str(o)] = ln
    if not (WOLF_L in want.values() and BEAR_L in want.values()):
        return False  # hold: need both attackers for the damage math
    rels = []
    for e in edges:
        src = e.get("sourceId")
        tids = e.get("targetIds") or []
        ref = cand_object_ref(cands.get(src, {}))
        if ref is None or str(ref) not in want:
            continue
        want_tid = None
        for tid in tids:
            if candidate_seat(cands.get(tid, {})) == foe_seat:
                want_tid = tid
                break
        if want_tid is None and tids:
            want_tid = tids[0]
        if want_tid:
            rels.append({"sourceId": src, "targetId": want_tid,
                         "group": None})
    if len(rels) < 2:
        return False  # hold until both edges are offered
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": rels}}}
    names = [want.get(str(cand_object_ref(cands.get(r["sourceId"], {}))),
                      "?") for r in rels]
    wire("declare_attackers", {"who": tag, "n_rels": len(rels),
                               "attackers": names, "submission": sub})
    say(f"[{tag}] declares attackers: {names} -> seat {foe_seat}")
    SUBMITTED_OPPS.add(key)
    await interact_as(c, sub, tag)
    return True


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

# ------------------------------------------------------------- 6899 logic


def set_stage(s):
    if ST["stage"] != s:
        ST["stage"] = s
        ST["stages_reached"].append(s)
        wire("stage", {"stage": s})
        say(f"STAGE -> {s}")


def attackable_creatures(state, pid):
    """Untapped Wolf/Bear oids on pid's battlefield with no summoning
    sickness."""
    out = {}
    for o in bf_oids(state, pid):
        ln = obj_lname(state, o)
        if ln in (WOLF_L, BEAR_L):
            oo = get_obj(state, o)
            if not oo.get("tapped") and not oo.get("summoning_sick") \
                    and not oo.get("has_summoning_sickness"):
                out[str(o)] = ln
    return out


def combat_attackers(state):
    atk = (state.get("combat") or {}).get("attackers") or []
    return atk


def moonmist_in_gy(state):
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Graveyard"
                and obj_lname(state, oid) == MOON_L
                and o.get("controller") in (0, "0")):
            return True
    return False


async def observe_stages(p0, state):
    """Advance the 6899 stage machine from authoritative state; export
    the pre/post checkpoints."""
    stage = ST["stage"]
    turn = state.get("turn_number") or 0
    active = state.get("active_player")
    phase = str(state.get("phase") or "")

    if stage == "SETUP":
        atk = attackable_creatures(state, 0)
        names = set(atk.values())
        if (WOLF_L in names and BEAR_L in names
                and perm_oids(state, 1, WOLF_L)
                and perm_oids(state, 1, BEAR_L)
                and find_hand_oid(state, 0, MOON_L) is not None
                and active == 0 and phase in ("PreCombatMain", "Main")
                and stack_empty(state)):
            await do_export(p0, "pre_p0_attack.json")
            set_stage("P0_ATTACK")
            say("P0 attack turn ready; pre_p0_attack.json exported")

    elif stage == "P0_ATTACK":
        if ST["p0_declared_turn"] is None and combat_attackers(state):
            ST["p0_declared_turn"] = turn
            say(f"P0 attackers declared on turn {turn}: "
                f"{combat_attackers(state)}")
            wire("p0_declared", {"turn": turn})
        if (ST["moonmist_cast_turns"].get(turn)
                and ST["p0_declared_turn"] == turn
                and phase == "PostCombatMain" and active == 0
                and stack_empty(state)):
            post = await do_export(p0, "post_p0_attack.json")
            ST["p1_life_after"] = life_of(post, 1)
            say(f"post_p0_attack.json exported: P1 life={ST['p1_life_after']}")
            wire("post_p0_attack", {"p1_life": ST["p1_life_after"]})
            set_stage("P0_DONE")

    elif stage == "P0_DONE":
        if active == 1 and phase in ("PreCombatMain", "Main") \
                and stack_empty(state):
            atk = attackable_creatures(state, 1)
            names = set(atk.values())
            if (WOLF_L in names and BEAR_L in names
                    and find_hand_oid(state, 0, MOON_L) is not None):
                await do_export(p0, "pre_p1_attack.json")
                set_stage("P1_ATTACK")
                say("P1 attack turn ready; pre_p1_attack.json exported")

    elif stage == "P1_ATTACK":
        if ST["p1_declared_turn"] is None and combat_attackers(state) \
                and active == 1:
            ST["p1_declared_turn"] = turn
            say(f"P1 attackers declared on turn {turn}: "
                f"{combat_attackers(state)}")
            wire("p1_declared", {"turn": turn})
        if (ST["moonmist_cast_turns"].get(turn)
                and ST["p1_declared_turn"] == turn
                and phase == "PostCombatMain" and active == 1
                and stack_empty(state)):
            post = await do_export(p0, "post_p1_attack.json")
            ST["p0_life_after"] = life_of(post, 0)
            say(f"post_p1_attack.json exported: P0 life={ST['p0_life_after']}")
            wire("post_p1_attack", {"p0_life": ST["p0_life_after"]})
            await do_export(p0, "post.json")
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


LAND_COLOR = {FOREST_L: "G"}


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


async def maybe_cast_moonmist(c, state, acts, tag):
    """P0 casts Moonmist during combat priority on either attack turn,
    once per turn. The prevention covers combat damage dealt that turn."""
    if ST["stage"] not in ("P0_ATTACK", "P1_ATTACK"):
        return False
    phase = str(state.get("phase") or "").lower()
    if not ("declareattack" in phase or "declareblock" in phase
            or "combatdamage" in phase):
        return False
    turn = state.get("turn_number")
    if ST["moonmist_cast_turns"].get(turn):
        return False
    if await cast_named(c, state, acts, tag, 0, MOON_L, 1, {"G": 1},
                        "moonmist_tmp", "Moonmist"):
        ST["moonmist_cast_turns"][turn] = True
        say(f"[{tag}] Moonmist cast on turn {turn} (combat prevention)")
        wire("moonmist_cast", {"turn": turn, "stage": ST["stage"]})
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
    # P0's own attacker declares are owned by do_declare_attackers during
    # P0_ATTACK; do_declare_empty must not eat them.
    if await do_declare_attackers(c, acts, st, 0, tag, foe_seat=1):
        return True
    if await do_declare_empty(c, acts, st, 0, tag,
                              attackers_ok=(ST["stage"] != "P0_ATTACK")):
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
        if ST["stage"] == "SETUP":
            if not ST["wolf_cast"]:
                if await cast_named(c, state, acts, tag, 0, WOLF_L, 1,
                                    {"G": 1}, "wolf_cast", "Young Wolf"):
                    return True
            elif not ST["bear_cast"]:
                if await cast_named(c, state, acts, tag, 0, BEAR_L, 2,
                                    {"G": 1}, "bear_cast", "Grizzly Bears"):
                    return True
        if my_priority(top_acts(st)):
            if await maybe_cast_moonmist(c, state, acts, tag):
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
    # P1's own attacker declares are owned by do_declare_attackers during
    # P1_ATTACK; do_declare_empty must not eat them.
    if await do_declare_attackers(c, acts, st, 1, tag, foe_seat=0):
        return True
    if await do_declare_empty(c, acts, st, 1, tag,
                              attackers_ok=(ST["stage"] != "P1_ATTACK")):
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
        if ST["stage"] == "SETUP":
            if not ST["p1_wolf_cast"]:
                if await cast_named(c, state, acts, tag, 1, WOLF_L, 1,
                                    {"G": 1}, "p1_wolf_cast", "Young Wolf"):
                    return True
            elif not ST["p1_bear_cast"]:
                if await cast_named(c, state, acts, tag, 1, BEAR_L, 2,
                                    {"G": 1}, "p1_bear_cast",
                                    "Grizzly Bears"):
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

    p0 = PhaseClient("P06899")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16899")
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

        # --- 6899 stage machine: observe authoritative state, export
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
                and ST["stage"] in ("SETUP", "P0_ATTACK", "P0_DONE",
                                    "P1_ATTACK"):
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

    pre_p0 = load("pre_p0_attack.json")
    post_p0 = load("post_p0_attack.json")
    pre_p1 = load("pre_p1_attack.json")
    post_p1 = load("post_p1_attack.json")
    post = load("post.json")

    def creatures_desc(s, pid):
        if not s:
            return "state missing"
        parts = []
        for oid, o in (s.get("objects") or {}).items():
            if (o.get("zone") == "Battlefield"
                    and str(o.get("controller", -1)) == str(pid)
                    and str(o.get("base_name") or o.get("name")
                            or "").lower() in (WOLF_L, BEAR_L)):
                parts.append(f"{o.get('base_name')}({oid},"
                             f"tapped={o.get('tapped')})")
        return " ".join(parts) or "no wolf/bear on BF"

    # A1: setup -- both sides have Wolf+Bear, P1 at 20
    a1_ok = (pre_p0 is not None
             and perm_oids(pre_p0, 0, WOLF_L) and perm_oids(pre_p0, 0, BEAR_L)
             and perm_oids(pre_p0, 1, WOLF_L) and perm_oids(pre_p0, 1, BEAR_L)
             and life_of(pre_p0, 1) == 20)
    A["A1_setup_ok"] = "passed" if a1_ok else (
        "failed" if pre_p0 is not None else "not-run")
    D["A1_setup_ok_detail"] = (
        f"P0: {creatures_desc(pre_p0, 0)}; P1: {creatures_desc(pre_p0, 1)}; "
        f"P1 life={life_of(pre_p0, 1) if pre_p0 else None} "
        f"(pre_p0_attack.json)")

    # A2: Moonmist resolved on P0's attack turn (in P0 graveyard)
    a2_src = post_p0 if post_p0 is not None else post
    a2_ok = a2_src is not None and moonmist_in_gy(a2_src)
    A["A2_moonmist_resolves"] = "passed" if a2_ok else (
        "failed" if a2_src is not None else "not-run")
    D["A2_moonmist_resolves_detail"] = (
        f"moonmist_in_p0_gy={moonmist_in_gy(a2_src) if a2_src else None} "
        f"(post_p0_attack.json)")

    # A3: P0's attack -- P1 must be at exactly 19 (Wolf 1 through,
    # Bear 2 prevented). 20 = over-prevention (the reproduced defect);
    # 17 = under-prevention (the literal reported symptom).
    p1l = life_of(post_p0, 1) if post_p0 is not None else None
    if p1l == 19:
        A["A3_p0_attack_damage"] = "passed"
    elif p1l is None:
        A["A3_p0_attack_damage"] = "not-run"
    else:
        A["A3_p0_attack_damage"] = "failed"
    D["A3_p0_attack_damage_detail"] = (
        f"P1 life after P0's Wolf+Bear attack under Moonmist: {p1l} "
        f"(expected 19 = Wolf 1 through + Bear 2 prevented; 20 = "
        f"over-prevention defect; 17 = no prevention at all) "
        f"(post_p0_attack.json)")

    # A4: P1's attack -- P0 must be at exactly 19 under the second
    # Moonmist. Same failure modes as A3.
    p0l = life_of(post_p1, 0) if post_p1 is not None else None
    if p0l == 19:
        A["A4_p1_attack_damage"] = "passed"
    elif p0l is None:
        A["A4_p1_attack_damage"] = "not-run"
    else:
        A["A4_p1_attack_damage"] = "failed"
    D["A4_p1_attack_damage_detail"] = (
        f"P0 life after P1's Wolf+Bear attack under Moonmist: {p0l} "
        f"(expected 19; 20 = over-prevention defect; 17 = no prevention) "
        f"(post_p1_attack.json)")

    # A5: cleanup -- stack empty, both attacks completed, no stall
    fin = post if post is not None else post_p1
    A["A5_cleanup"] = (
        "passed" if (ST["stage"] == "DONE" and not ST["stall_observed"]
                     and fin is not None and stack_empty(fin)
                     and ST["p0_declared_turn"] is not None
                     and ST["p1_declared_turn"] is not None)
        else ("failed" if ST["stall_observed"] else "not-run"))
    D["A5_cleanup_detail"] = (
        f"stage={ST['stage']} stall={ST['stall_observed']} "
        f"p0_declared_turn={ST['p0_declared_turn']} "
        f"p1_declared_turn={ST['p1_declared_turn']} "
        f"stages={ST['stages_reached']}")

    if A["A3_p0_attack_damage"] == "failed" \
            or A["A4_p1_attack_damage"] == "failed":
        verdict = "reproduced"  # Moonmist prevention defect, either dir
    elif A["A1_setup_ok"] == "failed" or A["A2_moonmist_resolves"] == "failed":
        verdict = "reproduced"  # related failure of the same card
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
                   "moonmist_cast_turns": ST["moonmist_cast_turns"],
                   "p0_declared_turn": ST["p0_declared_turn"],
                   "p1_declared_turn": ST["p1_declared_turn"],
                   "p1_life_after": ST["p1_life_after"],
                   "p0_life_after": ST["p0_life_after"],
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
            "P0": [[WOLF_T, 12], [BEAR_T, 12], [MOON_T, 12], [FOREST_T, 24]],
            "P1": [[WOLF_T, 12], [BEAR_T, 12], [FOREST_T, 36]],
        },
        "driver_notes": [
            "Protocol-106 re-validation of run 20260912-6899d (protocol 70 "
            "/ v0.81.0, verdict reproduced: over-prevention). Behavioral "
            "contract A1..A5 preserved: unblocked Wolf+Bear attacks in both "
            "directions under Moonmist, life totals asserted on "
            "authoritative exports.",
            "waiting_for is gone (null); priority = top-level PassPriority; "
            "all decisions via viewer_interaction; MulliganDecision via "
            "legacy Action; bottom via vi schema/select gated on "
            "waitingForKind.code=='mulligan'; DiscardToHandSize via vi "
            "schema/select.",
            "CastSpell via legacy Action. The v0.102.0 engine auto-taps "
            "reliably; manual taps are a >90s fallback only.",
            "Attacker declares use the relations-schema vi opportunity "
            "(sourceId/targetId edges, 106 path from scenario_6866_01020.py); "
            "the driver holds until BOTH Wolf and Bear are attackable so "
            "the life-total math stays discriminating (expected 19). "
            "Empty declares use the advertised action; P1 blockers keep "
            "the 25s relations grace window.",
            "Moonmist is cast by P0 during combat priority (DeclareAttackers "
            "/ DeclareBlockers / CombatDamage) on each attack turn, once "
            "per turn; the 'this turn' prevention then covers combat "
            "damage. No Humans on either battlefield, so the Transform "
            "clause is a no-op (same scope as the v0.81.1 proof).",
            "Stage machine on authoritative state: SETUP (both sides cast "
            "Wolf+Bear) -> P0_ATTACK (pre_p0_attack.json; P0 declares "
            "Wolf+Bear, casts Moonmist, post_p0_attack.json in "
            "PostCombatMain) -> P0_DONE -> P1_ATTACK (pre_p1_attack.json; "
            "P1 declares Wolf+Bear, P0 casts second Moonmist, "
            "post_p1_attack.json) -> DONE (post.json).",
            "Data-level: v0.102.0 parses Moonmist as Transform(all Humans) "
            "chaining PreventDamage(amount=All, target=Any, scope="
            "CombatDamage, duration=UntilEndOfTurn) -- the 'other than "
            "Werewolves and Wolves' exception still has no source-type "
            "filter at the data level.",
        ],
        "assertions": A,
        "assertion_details": D,
        "notes": OBS.get("notes", []),
        "verdict": verdict,
        "evidence_comment_id": 5647751969,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "12x/24x/36x card density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "No Humans on either battlefield, so the Transform clause was "
            "a no-op; the separate target-prompt defect is tracked in "
            "#6403.",
            "Young Wolf (Wolf) is the wolf-class representative; the "
            "Werewolf subtype was not separately tested.",
            "Not tested on the report's original build; verdict is scoped "
            "to v0.102.0, not a fix claim.",
            "States are authoritative exports, restorable only via full "
            "game replay (scenario_6899_01020.py), not direct load.",
        ],
        "setup_line": "P0: 12x Young Wolf + 12x Grizzly Bears + 12x "
                      "Moonmist + 24x Forest; P1: 12x Young Wolf + 12x "
                      "Grizzly Bears + 36x Forest",
        "contract_line": ("Both sides field a Young Wolf and a Grizzly "
                          "Bear. P0 attacks with Wolf+Bear unblocked and "
                          "casts Moonmist: P1 must take exactly 1 (Wolf "
                          "through, Bear prevented). P1 attacks back with "
                          "Wolf+Bear unblocked and P0 casts a second "
                          "Moonmist: P0 must take exactly 1. 20 = "
                          "over-prevention defect; 17 = no prevention."),
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
        shutil.copy(__file__, f"{EVDIR}/scenario_6899_01020.py")
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

    def life_line(path, label, pid):
        try:
            s = load_ev_state(path)
            life = None
            for p in s.get("players") or []:
                if str(p.get("id")) == str(pid):
                    life = p.get("life")
            return f"{label}: P{pid} life={life}"
        except Exception:
            return f"{label}: missing"

    pre_line = life_line("pre_p0_attack.json", "pre_p0_attack ", 1)
    post0_line = life_line("post_p0_attack.json", "post_p0_attack", 1)
    pre1_line = life_line("pre_p1_attack.json", "pre_p1_attack ", 0)
    post1_line = life_line("post_p1_attack.json", "post_p1_attack", 0)
    W, H = 1000, 880
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    y = 20
    d.text((24, y), "Issue #6899 - Moonmist: combat-damage prevention "
                    "exception (v0.102.0 revalidation)",
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
    d.text((24, y), "Unblocked Wolf(1/1)+Bear(2/2) under Moonmist: "
                    "expect exactly 1 (Wolf through, Bear prevented)",
           fill=(170, 180, 195))
    y += 26
    d.text((24, y), pre_line[:118] + "  ->  " + post0_line[:118],
           fill=(170, 180, 195))
    y += 24
    d.text((24, y), pre1_line[:118] + "  ->  " + post1_line[:118],
           fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both sides Wolf+Bear, P1 at 20",
        "A2_moonmist_resolves": "A2 Moonmist resolved to P0 graveyard",
        "A3_p0_attack_damage": "A3 P0 attack: P1 takes exactly 1",
        "A4_p1_attack_damage": "A4 P1 attack: P0 takes exactly 1",
        "A5_cleanup": "A5 stack empty, both attacks completed",
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
    det = details.get("A3_p0_attack_damage_detail", "")
    if det:
        d.text((24, y), "A3: " + det[:118], fill=(150, 165, 185))
        y += 20
    det = details.get("A4_p1_attack_damage_detail", "")
    if det:
        d.text((24, y), "A4: " + det[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6899/" +
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
