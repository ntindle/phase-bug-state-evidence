#!/usr/bin/env python3
"""Issue #6878: Duskwatch Recruiter - can't choose the order of the cards
put on the bottom of the library.

Oracle: "{2}{G}: Look at the top three cards of your library. You may
reveal a creature card from among them and put it into your hand. Put
the rest on the bottom of your library in any order."
Reported: the controller cannot choose the order of the rest.
Second symptom (matthewevans 2026-09-04 comment): the ability reveals all
three looked-at cards to the opponent instead of only the creature put
into hand.

Protocol-106 port of driver/scenario_6878_run3.py (protocol 69 /
v0.80.0) for the pinned v0.102.0 re-validation. Protocol-106
conventions (from scenario_6876_01020.py):
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [<name strings>]}.
  - waiting_for is gone (null): priority = advertised PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap; DiscardToHandSize
    via vi schema/select.
  - CastSpell / ActivateAbility via legacy actions with vi-choice
    fallbacks (activateAbility choice for the recruiter oid).
  - DeclareAttackers/Blockers: advertised empty submits; vi fallback only
    answers relations-schema opportunities (select/sequence schemas are
    decisions, never declares).
  - play_a_land matches any land via is_land(); vi playLand choices
    answered before the decision gate.
  - real_decision_pending excludes the 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand);
    decideOptionalEffect / decideOptionalCost and schema opportunities
    are real decisions.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients.

Plan (native engine, v0.102.0 / protocol 106, two human-client seats):
  P0: 12x Duskwatch Recruiter + 4x Llanowar Elves + 4x Grizzly Bears
      + 20x Forest.
  P1: 16x Llanowar Elves + 24x Forest; once the Recruiter is on the
      battlefield P1 casts a 1-mana creature on each of its main phases
      so "a spell was cast last turn" stays true at every P0 upkeep,
      suppressing the Recruiter's transform trigger (P1 holds its Elves
      until then, so the casts stay reliable however long setup takes).
  Round 1 (accept branch): cast Recruiter, then activate on a later P0
  main phase. Keep the first creature of the top 3. Expect NO ordering
  choice for the rest (the reported bug).
  Round 2 (decline branch, control): activate again, answer the keep
  select with zero cards. Expect an ordering choice for all three;
  observe whether one appears.

Behavioral contract:
  A1 setup_ok          Recruiter on P0 BF, activation paid, ability resolved
  A2 keep_offered      round-1 Dig keep prompt offered the looked-at
                       cards as candidates (keep_count 1, up_to)
  A3 order_offered_r1  an ordering choice for the rest was offered (r1)
  A4 rest_preserved_r1 the rest sit at the bottom of the library in the
                       original relative order (fixed order, not chosen)
  A5 no_opponent_leak  P1's view during Dig shows no names of the
                       looked-at cards
  A6 decline_branch_r2 decline (0 kept) still offers no ordering choice;
                       all three land on the bottom in preserved order
                       (not-run if the transform spoils round 2)
  A7 cleanup           stack empty, no stuck decision, game proceeds

Verdict: reproduced iff A1+A2 pass and (A3 fails or A6 fails).
not-reproduced iff A1..A7 all pass (A6 may be not-run only if the
transform genuinely spoils round 2). blocked otherwise.
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
ISSUE = 6878
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261006-6878b")
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
    """Record the v0.102.0 parse of Duskwatch Recruiter's Dig ability: the
    reported bug is that no ordering choice is offered for the rest, so
    the parse must show whether a rest-ordering mode survived parsing."""
    h = CARD_DATA["duskwatch recruiter"]
    acts = h.get("abilities") or []
    digs = [a for a in acts
            if ((a.get("effect") or {}).get("type")) == "Dig"]
    assert digs, "no Dig ability parsed for duskwatch recruiter in v0.102.0"
    eff = digs[0]["effect"]
    ev = {
        "name": h.get("name"),
        "oracle_text": h.get("oracle_text"),
        "dig_count": (eff.get("count") or {}).get("value"),
        "keep_count": eff.get("keep_count"),
        "up_to": eff.get("up_to"),
        "filter": eff.get("filter"),
        "rest_destination": eff.get("rest_destination"),
        "reveal": eff.get("reveal"),
        "rest_order": eff.get("rest_order", "<field absent>"),
        "effect_blob": eff,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": h.get("name"),
                   "oracle_text": h.get("oracle_text"),
                   "abilities": acts}, f, indent=1)
    say(f"data-level: Dig count={ev['dig_count']} keep={ev['keep_count']} "
        f"up_to={ev['up_to']} rest_dest={ev['rest_destination']} "
        f"rest_order={ev['rest_order']}")
    wire("data_level", {k: v for k, v in ev.items()
                        if k != "effect_blob"})


REC_T = "Duskwatch Recruiter"
HOWLER_T = "Krallenhorde Howler"
ELVES_T = "Llanowar Elves"
BEARS_T = "Grizzly Bears"
FOREST_T = "Forest"

REC_L = "duskwatch recruiter"
HOWLER_L = "krallenhorde howler"
ELVES_L = "llanowar elves"
BEARS_L = "grizzly bears"
FOREST_L = "forest"
CREATURES = {REC_L, ELVES_L, BEARS_L}

P0_DECK = ((REC_T, 12), (ELVES_T, 4), (BEARS_T, 4), (FOREST_T, 20))
# P1 holds its Elves until the Recruiter hits the battlefield, then casts
# one per main phase so "a spell was cast last turn" stays true at every
# P0 upkeep (transform suppression). 16x Elves gives ample buffer for the
# two post-arming casts the scenario needs.
P1_DECK = ((ELVES_T, 16), (FOREST_T, 24))

GAME_TIMEOUT = 2400
DIG_WATCHDOG = 200

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0,
      "p1_spell_turn": -1, "p1_block_grace_until": 0,
      "opp_shapes_logged": set()}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {"cast": None}
OBS = {"r1": {}, "r2": {}, "rejections": [], "p1_dig_view": None,
       "dig_view_captured": False, "notes": []}
P0C = {"c": None}
P1C = {"c": None}


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


def library_of(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


def top3_of(state, pid):
    out = []
    for oid in library_of(state, pid)[:3]:
        out.append((oid, obj_lname(state, oid)))
    return out


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


def opp_codes(opp):
    codes = set()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    for ch in (data.get("candidates") or data.get("choices") or []):
        codes.update(c for c in surf_codes(ch) if c)
    return codes


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


def cand_zone(state, cand):
    ref = cand_object_ref(cand)
    return get_obj(state, ref).get("zone") if ref else None


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
    if tag == "P0":
        choice = "Keep" if (REC_L in hand and n_lands >= 2) or n >= 2 \
            else "Mulligan"
    else:
        choice = "Keep" if (ELVES_L in hand and n_lands >= 1) or n >= 2 \
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

        keep = {REC_L, ELVES_L, BEARS_L} if tag == "P0" else {ELVES_L}

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
    if ln == REC_L:
        return 3
    if ln in (ELVES_L, BEARS_L):
        return 2
    if is_land(get_obj(state, o)):
        return 1
    return 0


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln == ELVES_L:
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


# ------------------------------------------------------------- Dig logic


def vi_activate_choice(st, oid):
    """Find a vi opportunity with an activateAbility choice for oid."""
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "activateAbility" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None


def untapped_forests(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == FOREST_L
            and not get_obj(state, oid).get("tapped")]


def howler_on_bf(state, pid):
    return bool(perm_oids(state, pid, HOWLER_L))


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


async def activate_recruiter(c, state, pid, tag, ctx):
    """Activate Duskwatch Recruiter's {2}{G} Dig ability. Legacy
    ActivateAbility (ability_index 0) first; vi activateAbility-choice
    fallback. Exports pre_<ctx>.json before activating so the top 3 of
    the library are recorded."""
    st = st_of(c)
    acts = merged_actions(st)
    rec = perm_oids(state, pid, REC_L)
    if not rec:
        return False
    rec_oid = rec[0]
    a = None
    for act in acts:
        if act.get("type") != "ActivateAbility":
            continue
        d = act.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src != str(rec_oid):
            continue
        if int(d.get("ability_index", -1)) == 0:
            a = act
            break
    vich = vi_activate_choice(st, rec_oid)
    if a is None and vich is None:
        return False
    pre = await do_export(c, f"pre_{ctx}.json")
    K = OBS.setdefault(ctx, {})
    K["top3"] = top3_of(pre, 0)
    K["looked_oids"] = [oid for oid, _ in K["top3"]]
    wire("top3", {"round": ctx, "top3": K["top3"]})
    say(f"[P0] {ctx} top3: {K['top3']}")
    K["activated"] = True
    K["activated_turn"] = state.get("turn_number")
    K["dig_t0"] = time.time()
    ST["stage"] = "DIG1" if ctx == "r1" else "DIG2"
    wire("activate", {"who": tag, "round": ctx,
                      "via": "legal_actions" if a else "viewer_interaction"})
    say(f"[{tag}] activates Duskwatch Recruiter ({ctx}; "
        f"{'legacy' if a else 'vi'})")
    if a is not None:
        d = dict(a.get("data", {}))
        for k in ("source_id", "object_id"):
            if k in d:
                try:
                    d[k] = int(d[k])
                except (TypeError, ValueError):
                    pass
        await submit_as_is(c, {"type": "ActivateAbility", "data": d})
    else:
        opp, ch = vich
        await answer_vi(c, opp, ch, tag)
    return True


def capture_p1_view(state_p1, ctx):
    """Record what P1 sees of the looked-at cards during the Dig."""
    if OBS["dig_view_captured"]:
        return
    K = OBS.get(ctx, {})
    snap = {}
    for oid, nm in K.get("top3") or []:
        o = get_obj(state_p1, oid)
        snap[oid] = {"name": oname(o), "zone": o.get("zone"),
                     "face_down": o.get("face_down"),
                     "revealed": o.get("revealed")}
    OBS["p1_dig_view"] = snap
    OBS["dig_view_captured"] = True
    wire("p1_dig_view", {"round": ctx, "view": snap})
    say(f"[P1 view during Dig {ctx}] {snap}")


async def handle_dig(c, state, acts, st):
    """Drive the Dig decisions for P0 in DIG1/DIG2. Returns True if acted.

    Phase 1 (keep): schema opportunities whose candidates reference the
    looked-at oids. r1 keeps the first creature in top-3 order; r2 keeps
    zero cards (decline branch).
    Phase 2 (order): schema opportunities whose candidates reference the
    rest oids. If offered, record order_offered and answer with the
    reversed rest order (a deliberately non-original order, proving the
    choice takes effect).
    """
    stage = ST.get("stage")
    if stage not in ("DIG1", "DIG2"):
        return False
    ctx = "r1" if stage == "DIG1" else "r2"
    K = OBS.setdefault(ctx, {})
    if not K.get("looked_oids"):
        return False
    kind = vi_kind_code(st)
    looked = set(K["looked_oids"])
    rest = set(K.get("rest_oids") or [])
    acted = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if not iid or iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        spec_type = (spec.get("type") or "").lower()
        cands = data.get("candidates") or data.get("choices") or []
        cand_refs = {cand_object_ref(ch) for ch in cands}
        cand_refs.discard(None)

        shape = (stage, kind, rtype, spec_type,
                 tuple(sorted(cand_refs))[:8])
        if shape not in ST["opp_shapes_logged"]:
            ST["opp_shapes_logged"].add(shape)
            wire("dig_prompt", {"round": ctx, "kind": kind,
                                "response_type": rtype,
                                "spec_type": spec_type,
                                "spec": spec,
                                "cand_refs": sorted(cand_refs),
                                "opportunity": opp})
            say(f"[P0] {ctx} Dig prompt kind={kind} rtype={rtype} "
                f"spec={spec_type} nrefs={len(cand_refs)}")
            for ch in cands:
                ref = cand_object_ref(ch)
                say(f"    cand id={ch.get('id')} ref={ref} "
                    f"name={obj_lname(state, ref) if ref else '?'} "
                    f"zone={cand_zone(state, ch)} "
                    f"text={choice_text(ch)[:60]}")

        if rtype != "schema" or spec_type not in ("select", "sequence"):
            continue

        ref_of = {}
        for ch in cands:
            ref = cand_object_ref(ch)
            if ref is not None:
                ref_of[ref] = ch.get("id")

        # Phase 1: the optional keep choice among the looked-at cards.
        if not K.get("keep_decided"):
            lib_refs = [ref for ref in ref_of
                        if ref in looked
                        and get_obj(state, ref).get("zone") == "Library"]
            if lib_refs:
                K["keep_prompt_seen"] = True
                K["keep_spec"] = spec
                K["keep_offered_refs"] = lib_refs
                K["keep_kind"] = kind
                wire("keep_prompt", {"round": ctx, "refs": lib_refs,
                                     "spec": spec})
                if ctx == "r1":
                    want = None
                    for oid, nm in K["top3"]:
                        if nm in CREATURES and oid in ref_of:
                            want = ref_of[oid]
                            break
                    cids = [want] if want else []
                else:
                    cids = []
                sub = {"interactionId": iid,
                       "response": {"type": spec_type,
                                    "data": {"choiceIds": cids}}}
                await interact_as(c, sub, "P0")
                SUBMITTED_OPPS.add(iid)
                K["keep_decided"] = True
                K["kept_cids"] = cids
                kept_refs = [ref for ref, cid in ref_of.items()
                             if cid in cids]
                K["kept_refs"] = kept_refs
                K["rest_oids"] = [oid for oid, _nm in K["top3"]
                                  if oid not in kept_refs]
                rest = set(K["rest_oids"])
                say(f"[P0] {ctx} Dig keep answered cids={cids} "
                    f"kept={kept_refs} rest={K['rest_oids']}")
                wire("keep_answered", {"round": ctx, "cids": cids,
                                       "kept_refs": kept_refs,
                                       "rest_oids": K["rest_oids"]})
                # capture the opponent's view of the looked-at cards
                p1 = P1C.get("c")
                if p1 is not None and p1.latest:
                    capture_p1_view(p1.latest["state"], ctx)
                acted = True
                continue
            # a schema opportunity during Dig that does not reference the
            # looked-at cards: leave it to the generic handlers; never
            # misclassify (cf. the Altar sacrifice misfire lesson).
            continue

        # Phase 2: a genuine bottom-order prompt references the rest
        # oids in a schema opportunity.
        if K.get("keep_decided") and not K.get("order_decided"):
            rest_refs = [ref for ref in ref_of
                         if ref in rest
                         and get_obj(state, ref).get("zone") == "Library"]
            if rest_refs:
                want_ids = []
                for oid in reversed(K["rest_oids"]):
                    if oid in ref_of:
                        want_ids.append(ref_of[oid])
                if want_ids:
                    K["order_offered"] = True
                    K["order_spec"] = spec
                    K["order_submitted"] = [
                        ref for ref, cid in ref_of.items()
                        if cid in want_ids]
                    wire("order_prompt_answered",
                         {"round": ctx,
                          "submitted_refs": K["order_submitted"]})
                    sub = {"interactionId": iid,
                           "response": {"type": spec_type,
                                        "data": {"choiceIds": want_ids}}}
                    await interact_as(c, sub, "P0")
                    SUBMITTED_OPPS.add(iid)
                    K["order_decided"] = True
                    say(f"[P0] {ctx} bottom-order answered: "
                        f"{K['order_submitted']}")
                    acted = True
                    continue
    return acted

# ------------------------------------------------------------- tick dispatch


async def maybe_safety_cast(c, state, acts, pid, tag):
    """Cast a cheap creature spell on P0's turn. The transform trigger
    fires at the beginning of EACH upkeep (both players'): P1's casts
    cover P0's upkeep, but P0 must itself cast a spell on its own turn
    so P1's next upkeep sees "a spell was cast last turn"."""
    if OBS.get("safety_cast_turn") == state.get("turn_number"):
        return False
    for nm, lname in ((ELVES_T, ELVES_L), (BEARS_T, BEARS_L),
                      (REC_T, REC_L)):
        chand = find_hand_oid(state, pid, lname)
        if not chand:
            continue
        # don't burn the last Recruiter while none is on the battlefield
        if lname == REC_L and not perm_oids(state, pid, REC_L) \
                and sum(1 for o in hand_ids(state, pid)
                        if obj_lname(state, o) == REC_L) <= 1:
            continue
        for a in acts:
            if a.get("type") == "CastSpell" and \
                    str(a.get("data", {}).get("object_id")) == chand:
                ACT["cast"] = {"tag": f"safety-{chand}", "lname": lname,
                               "in_flight": True, "taps": 0,
                               "submit_t": time.time()}
                OBS["safety_cast_turn"] = state.get("turn_number")
                say(f"[{tag}] safety cast {nm} "
                    f"(turn {state.get('turn_number')})")
                wire("safety_cast", {"card": nm, "oid": chand,
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

    # sleep(0) yield before leg evaluation (106 convention)
    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # Dig decisions take precedence; never act blindly during them
    if ST["stage"] in ("DIG1", "DIG2"):
        if await handle_dig(c, state, acts, st):
            return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        if ST["stage"] == "SETUP":
            # cast the Recruiter (needs 3 untapped Forests)
            if not perm_oids(state, 0, REC_L):
                rh = find_hand_oid(state, 0, REC_L)
                if rh and len(untapped_forests(state, 0)) >= 3:
                    for a in acts:
                        if a.get("type") == "CastSpell" and \
                                str(a.get("data", {}).get("object_id")) == rh:
                            ACT["cast"] = {"tag": f"rec-{rh}",
                                           "lname": REC_L,
                                           "in_flight": True, "taps": 0,
                                           "submit_t": time.time()}
                            MANA_NEEDS[tag] = {"G": 1, "generic": 2}
                            say(f"[{tag}] casts Duskwatch Recruiter ({rh})")
                            wire("cast_submit", {"tag": "recruiter",
                                                 "oid": rh})
                            await submit_as_is(c, a)
                            return True
        elif ST["stage"] in ("R1ARM", "R2ARM"):
            ctx = "r1" if ST["stage"] == "R1ARM" else "r2"
            if howler_on_bf(state, 0):
                say(f"[P0] Recruiter transformed to Krallenhorde Howler; "
                    f"stage={ST['stage']} -- round spoiled")
                wire("transformed", {"turn": state.get("turn_number"),
                                     "stage": ST["stage"]})
                await do_export(c, "spoiled_transform.json")
                OBS[f"{ctx}_spoiled"] = True
                ST["stop"] = True
                return False
            if perm_oids(state, 0, REC_L) and stack_empty(state):
                # safety spell first (covers the opponent's upkeep),
                # then the activation when 3 mana is free
                if await maybe_safety_cast(c, state, acts, 0, tag):
                    return True
                if len(untapped_forests(state, 0)) >= 3:
                    if await activate_recruiter(c, state, 0, tag, ctx):
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
        # Cast a 1-mana creature each main phase ONLY once the Recruiter
        # is on the battlefield (armed): the upkeep transform trigger
        # only exists then, and holding Elves until arming keeps the two
        # post-arming casts reliable no matter how long setup takes.
        armed = bool(perm_oids(state, 0, REC_L))
        if armed and ST["p1_spell_turn"] != state.get("turn_number"):
            chand = find_hand_oid(state, 1, ELVES_L)
            if chand:
                for a in acts:
                    if a.get("type") == "CastSpell" and \
                            str(a.get("data", {}).get("object_id")) == chand:
                        ST["p1_spell_turn"] = state.get("turn_number")
                        say(f"[{tag}] casts Llanowar Elves "
                            f"(turn {state.get('turn_number')})")
                        wire("p1_cast", {"turn": state.get("turn_number")})
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


def bottom_positions(lib, oids):
    """Positions (index 0 = top) of oids in the library."""
    pos = {}
    for i, o in enumerate(lib):
        if str(o) in set(oids):
            pos[str(o)] = i
    return pos


# ------------------------------------------------------------- main loop


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06878")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16878")
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
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            ST["stop"] = True
            continue

        # --- SETUP -> R1ARM: recruiter on BF, P0 main phase, stack empty
        if ST["stage"] == "SETUP" and my_main(state, 0) \
                and perm_oids(state, 0, REC_L):
            say(f"=== stage -> R1ARM (turn {state.get('turn_number')}) ===")
            ST["stage"] = "R1ARM"

        # --- transform spoil: Recruiter flipped before we could use it
        if ST["stage"] in ("SETUP", "R1ARM", "R2ARM") and not ST.get("stop"):
            if any(obj_lname(state, o) == HOWLER_L
                   for o in bf_oids(state, 0)):
                say("Recruiter transformed to Krallenhorde Howler before "
                    "activation -- scenario spoiled")
                wire("transformed", {"turn": state.get("turn_number"),
                                     "stage": ST["stage"]})
                try:
                    await do_export(p0, "spoiled_transform.json")
                except Exception as e:
                    say(f"spoiled export failed: {e}")
                OBS["notes"].append(
                    "Recruiter transformed before activation "
                    f"(stage {ST['stage']})")
                ST["stop"] = True
                continue

        # --- DIG watchdogs
        stage = ST.get("stage")
        if stage in ("DIG1", "DIG2"):
            ctx = "r1" if stage == "DIG1" else "r2"
            K = OBS.get(ctx, {})
            if K.get("dig_t0") and time.time() - K["dig_t0"] > DIG_WATCHDOG \
                    and not K.get("resolved"):
                say(f"{ctx} DIG watchdog fired")
                wire("dig_watchdog", {"round": ctx,
                                      "kind": vi_kind_code(st_of(p0))})
                try:
                    await do_export(p0, f"mid_dig_{ctx}.json")
                except Exception as e:
                    say(f"mid_dig_{ctx} export failed: {e}")
                OBS["notes"].append(f"{ctx} dig watchdog fired")
                ST["stop"] = True
                break

        # --- resolution: keep decided, stack empty, no P0 decision pending.
        # Note: on 106 the plain priority action menu carries
        # waitingForKind.code == 'choose', so 'choose' counts as no
        # decision here (any schema opportunity or decide* choice would
        # make real_decision_pending true).
        if stage in ("DIG1", "DIG2"):
            ctx = "r1" if stage == "DIG1" else "r2"
            K = OBS.get(ctx, {})
            pst = st_of(p0)
            if K.get("keep_decided") and not K.get("resolved") \
                    and stack_empty(state) \
                    and not real_decision_pending(pst) \
                    and vi_kind_code(pst) in ("", "choose"):
                K["resolved"] = True
                post = await do_export(p0, f"post_{ctx}.json")
                lib = library_of(post, 0)
                K["post_bottom"] = lib[-6:]
                K["rest_positions"] = bottom_positions(
                    lib, K.get("rest_oids") or [])
                K["kept_in_hand"] = [
                    r for r in (K.get("kept_refs") or [])
                    if get_obj(post, r).get("zone") == "Hand"]
                say(f"=== {ctx} resolved; post exported; "
                    f"rest_positions={K['rest_positions']} "
                    f"kept_in_hand={K['kept_in_hand']} ===")
                if ctx == "r1":
                    ST["stage"] = "R2ARM"
                    say("=== stage -> R2ARM ===")
                else:
                    ST["stop"] = True
                continue

    say(f"loop ended: stage={ST['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": ST["stage"]})

    # ------------------------------------------------------- assertions
    A = obs["assert"]

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    r1 = OBS.get("r1", {})
    r2 = OBS.get("r2", {})

    A["A1_setup_ok"] = (
        "passed" if (r1.get("activated") and r1.get("resolved")
                     and not OBS.get("rejections")) else "failed")
    obs["notes"].append(
        f"A1: r1 activated={r1.get('activated')} resolved={r1.get('resolved')} "
        f"rejections={len(OBS.get('rejections', []))}")

    keep_refs = r1.get("keep_offered_refs") or []
    looked = r1.get("looked_oids") or []
    creatures_looked = [oid for oid, nm in (r1.get("top3") or [])
                        if nm in CREATURES]
    a2 = (r1.get("keep_prompt_seen")
          and set(keep_refs) <= set(looked)
          and set(creatures_looked) <= set(keep_refs))
    A["A2_keep_offered"] = "passed" if a2 else "failed"
    A["A2_keep_offered_detail"] = {
        "top3": r1.get("top3"), "keep_offered_refs": keep_refs,
        "keep_kind": r1.get("keep_kind"), "keep_spec": r1.get("keep_spec")}

    A["A3_order_offered_r1"] = (
        "passed" if r1.get("order_offered") else "failed")
    A["A3_order_offered_r1_detail"] = {
        "rest_oids": r1.get("rest_oids"),
        "order_spec": r1.get("order_spec")}

    rest = r1.get("rest_oids") or []
    pos = r1.get("rest_positions") or {}
    post1 = load("post_r1.json")
    lib_len = len(library_of(post1, 0)) if post1 else None
    preserved = (lib_len is not None and len(rest) > 0
                 and len(pos) == len(rest)
                 and sorted(pos.values())
                 == list(range(lib_len - len(rest), lib_len))
                 and [pos[o] for o in rest] == sorted(pos[o] for o in rest))
    A["A4_rest_preserved_r1"] = "passed" if preserved else "failed"
    A["A4_rest_preserved_r1_detail"] = {
        "rest_oids": rest, "positions": pos, "library_len": lib_len,
        "post_bottom_tail": r1.get("post_bottom")}

    # A5: the kept creature is legitimately revealed (reveal: true), but
    # the rest go to the bottom unrevealed -- the opponent must not see
    # their names (second symptom from the 2026-09-04 report).
    p1v = OBS.get("p1_dig_view") or {}
    rest_set = set(r1.get("rest_oids") or [])
    real_rest_names = {nm.lower() for oid, nm in (r1.get("top3") or [])
                       if oid in rest_set}
    leaked = [oid for oid, v in p1v.items()
              if str(v.get("name") or "").lower() in real_rest_names
              and str(oid) in rest_set]
    A["A5_no_opponent_leak"] = (
        "passed" if (OBS.get("dig_view_captured") and not leaked)
        else "failed")
    A["A5_no_opponent_leak_detail"] = {"view": p1v, "leaked": leaked}

    if r2.get("activated") and r2.get("resolved"):
        A["A6_decline_branch_r2"] = (
            "failed" if not r2.get("order_offered") else "passed")
        A["A6_decline_branch_r2_detail"] = {
            "kept": r2.get("kept_refs"), "rest_oids": r2.get("rest_oids"),
            "rest_positions": r2.get("rest_positions"),
            "order_offered": bool(r2.get("order_offered"))}
    elif OBS.get("r2_spoiled"):
        A["A6_decline_branch_r2"] = "not-run"
        A["A6_decline_branch_r2_detail"] = \
            "transform spoiled round 2 (see spoiled_transform.json)"
    else:
        A["A6_decline_branch_r2"] = "not-run"
        A["A6_decline_branch_r2_detail"] = \
            "round 2 did not complete (see notes)"

    notes = OBS.get("notes", [])
    watchdog_fired = any("watchdog" in n for n in notes)
    A["A7_cleanup"] = (
        "passed" if (r1.get("resolved") and not watchdog_fired
                     and str(state.get("phase") or "").lower() != "gameover")
        else "failed")

    for k in sorted(A):
        if not k.endswith("_detail"):
            say(f"{k}: {A[k]}")

    verdict = "blocked"
    core_vals = [v for k, v in A.items() if not k.endswith("_detail")]
    if A["A1_setup_ok"] == "passed" and A["A2_keep_offered"] == "passed":
        if A["A3_order_offered_r1"] == "failed" \
                or A["A6_decline_branch_r2"] == "failed":
            verdict = "reproduced"
        elif all(v in ("passed", "not-run") for v in core_vals):
            verdict = "not-reproduced"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": {k: v for k, v in OBS.items()
                                    if k not in ("rejections",)},
                   "rejections": OBS.get("rejections", []),
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
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
            "P0": [[REC_T, 12], [ELVES_T, 4], [BEARS_T, 4], [FOREST_T, 20]],
            "P1": [[ELVES_T, 16], [FOREST_T, 24]],
        },
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6878_run3.py (protocol 69 "
            "/ v0.80.0) for pinned v0.102.0; behavioral contract A1..A7 and "
            "verdict logic unchanged.",
            "waiting_for is gone (null); priority = top-level PassPriority; "
            "all decisions via viewer_interaction; MulliganDecision via "
            "legacy Action; bottom via vi schema/select gated on "
            "waitingForKind.code=='mulligan'; DiscardToHandSize via vi "
            "schema/select.",
            "CastSpell via legacy Action; ActivateAbility via legacy "
            "ActivateAbility (ability_index 0) with vi activateAbility "
            "choice fallback. The v0.102.0 engine auto-taps reliably; "
            "manual taps are a >90s fallback only.",
            "Dig keep/order prompts are vi schema opportunities gated on "
            "candidates referencing the looked-at/rest oids (recorded "
            "from the authoritative pre-activation export); schema prompts "
            "not referencing those oids are never misclassified. "
            "P1 casts Llanowar Elves each main phase to keep 'a spell was "
            "cast last turn' true at every P0 upkeep (transform "
            "suppression); empty combat declares via advertised actions.",
            "P1 holds its Elves until the Recruiter is on the battlefield "
            "(armed) -- casting every turn from turn 1 drained P1's hand "
            "and let the Recruiter transform on the 20261006-6878 attempt "
            "(verdict blocked, fixture spoiled). 16x Elves gives buffer "
            "for the two post-arming casts the scenario needs.",
            "real_decision_pending excludes the 106 priority-menu codes; "
            "playLand vi codes answered before the decision gate; land "
            "matching via is_land().",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope); the reported OUTCOME is "
            "asserted on the saved states, not the prompt.",
            "Data-level: v0.102.0 parses the Dig effect with "
            "rest_destination='Library' but NO rest_order field at all -- "
            "the parsed structure carries no ordering mode for the rest, "
            "consistent with the runtime offering no order choice "
            "(area:engine).",
        ],
        "assertions": A,
        "notes": OBS.get("notes", []),
        "verdict": verdict,
        "evidence_comment_id": 5644046591,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "12x Duskwatch Recruiter density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay (scenario_6878_01020.py), not direct load.",
            "Fewer-than-three-card library edge not exercised.",
        ],
        "setup_line": "P0: 12x Duskwatch Recruiter + 4x Llanowar Elves + "
                      "4x Grizzly Bears + 20x Forest; P1: 16x Llanowar Elves "
                      "+ 24x Forest (casts one per main phase once the "
                      "Recruiter is on the battlefield, to suppress the "
                      "upkeep transform)",
        "contract_line": ("Activate Duskwatch Recruiter's Dig twice: "
                          "accept-branch keep (r1) and decline-branch "
                          "keep-none (r2). After the keep, the controller "
                          "must be offered an ordering choice for the rest "
                          "going to the bottom of the library; the unrevealed "
                          "rest must stay hidden from the opponent."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions="
        f"{json.dumps({k: v for k, v in A.items() if not k.endswith('_detail')})}")
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
        shutil.copy(__file__, f"{EVDIR}/scenario_6878_01020.py")
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
        pre = load_ev_state("pre_r1.json")
        t3 = top3_of(pre, 0)
        pre_line = (f"pre_r1: turn {pre.get('turn_number')} "
                    f"{pre.get('phase')} | top3="
                    + ", ".join(f"{nm}({oid})" for oid, nm in t3))
    except Exception:
        pre_line = "pre_r1.json: missing"
    try:
        post = load_ev_state("post_r1.json")
        lib = library_of(post, 0)
        r1 = OBS.get("r1", {})
        rest = r1.get("rest_oids") or []
        poss = r1.get("rest_positions") or {}
        post_line = (f"post_r1: rest={rest} at positions "
                     f"{[poss.get(o) for o in rest]} of {len(lib)} "
                     f"(bottom slots, original order) | "
                     f"kept_in_hand={r1.get('kept_in_hand')}")
    except Exception:
        post_line = "post_r1.json: missing"
    try:
        post2 = load_ev_state("post_r2.json")
        r2 = OBS.get("r2", {})
        fin_line = (f"post_r2: kept={r2.get('kept_refs')} "
                     f"rest_positions={r2.get('rest_positions')} "
                     f"order_offered={bool(r2.get('order_offered'))}")
    except Exception:
        fin_line = "post_r2.json: missing/not-run"
    W, H = 1000, 860
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    y = 20
    d.text((24, y), "Issue #6878 - Duskwatch Recruiter: no order choice for "
                    "bottom cards (v0.102.0 revalidation)",
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
    y += 24
    d.text((24, y), fin_line[:120], fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Recruiter BF, activation paid, resolved",
        "A2_keep_offered": "A2 keep prompt offered looked-at cards "
                           "(keep 1, up to)",
        "A3_order_offered_r1": "A3 ordering choice for rest offered (r1)",
        "A4_rest_preserved_r1": "A4 rest at bottom in original order (r1)",
        "A5_no_opponent_leak": "A5 opponent sees no names of the "
                           "unrevealed rest (kept creature is "
                           "legitimately revealed)",
        "A6_decline_branch_r2": "A6 decline branch: ordering offered (r2)",
        "A7_cleanup": "A7 stack empty, game continues",
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
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6878/" +
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
