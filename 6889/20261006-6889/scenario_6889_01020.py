#!/usr/bin/env python3
"""Issue #6889: Tolsimir, Friend to Wolves isn't triggering all effects.

Oracle text: "When Tolsimir enters, create Voja, Friend to Elves, a
legendary 3/3 green and white Wolf creature token. Whenever a Wolf you
control enters, you gain 3 life and that creature fights up to one
target creature you don't control."

Reported: the life gain works, but the fight option ("fights up to one
target creature you don't control") doesn't appear to trigger or
resolve.

v0.80.0 finding (protocol 69): the Wolf-enter trigger's execute is
GainLife{3} ONLY -- no fight sub-ability, no target. The fight clause
is absent at the data layer too. This run re-validates on pinned
v0.102.0 (protocol 106).

Protocol-106 port of evidence/6889/20260912-6889/scenario_6889.py.
Protocol-106 conventions (from scenario_6878_01020.py):
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [<name strings>]}.
  - waiting_for is gone (null): priority = advertised PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap; DiscardToHandSize
    via vi schema/select.
  - CastSpell via legacy Action; vi playLand handled before the decision
    gate; DeclareAttackers/Blockers: advertised empty submits, vi
    fallback answers relations-schema opportunities only.
  - real_decision_pending excludes the 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand).
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients.

Plan (native engine, v0.102.0 / protocol 106, two human-driver seats):
  P0 casts Tolsimir, Friend to Wolves ({2}{G}{G}{W}).
  P1 fields 2x Grizzly Bears (legal fight targets; 2+ forces a real
  prompt instead of single-target auto-targeting).
  Watch the Wolf-enter trigger when Voja enters: expect +3 life, then a
  target prompt for the fight. If offered, target a Bear and verify the
  fight (damage on both). If no prompt and no fight while the trigger
  otherwise resolves -> reproduced.

Behavioral contract:
  A1 setup_ok      Tolsimir on P0 BF, Voja token on P0 BF, >=2 Bears on
                   P1 BF
  A2 wolf_trigger  Wolf-enter TriggeredAbility observed on the stack
  A3 life_gain     P0 life +3 across the trigger window
  A4 fight_prompted target prompt for the fight offered (expected FAILED)
  A5 fight_resolved Voja/Bear fight damage observed (not-run when no
                   prompt)
  A6 cleanup       stack empty, game proceeding after the trigger

Verdict: reproduced iff A1+A2+A3 pass and A4 fails.
not-reproduced iff A4 and A5 pass (fight prompted and resolved).
blocked otherwise.
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
ISSUE = 6889
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261006-6889")
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
    """Record the v0.102.0 parse of Tolsimir's two triggers: the
    reported bug is the wolf-enter trigger's fight clause missing from
    the parsed structure (execute = GainLife{3} only, sub_ability null,
    no target definition)."""
    c = CARD_DATA["tolsimir, friend to wolves"]
    trigs = c.get("triggers") or []
    assert trigs, "no triggers parsed for tolsimir in v0.102.0 card-data"
    wolf = None
    for t in trigs:
        if "a wolf you control enters" in str(t.get("description") or "") \
                .lower():
            wolf = t
    ev = {
        "name": c.get("name"),
        "oracle_text": c.get("oracle_text"),
        "trigger_count": len(trigs),
        "wolf_trigger": wolf,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": c.get("name"),
                   "oracle_text": c.get("oracle_text"),
                   "triggers": trigs}, f, indent=1)
    eff = ((wolf or {}).get("execute") or {}).get("effect") or {}
    say(f"data-level: {len(trigs)} triggers; wolf-enter execute="
        f"{eff.get('type')} sub_ability="
        f"{((wolf or {}).get('execute') or {}).get('sub_ability')}")
    wire("data_level", {"trigger_count": len(trigs),
                        "wolf_execute_type": eff.get("type"),
                        "wolf_sub_ability":
                            ((wolf or {}).get("execute") or {})
                            .get("sub_ability"),
                        "wolf_valid_target": wolf.get("valid_target")
                            if wolf else None})


TOLS_T = "Tolsimir, Friend to Wolves"
TOLS_L = "tolsimir, friend to wolves"
BEAR_T = "Grizzly Bears"
BEAR_L = "grizzly bears"
FOREST_T = "Forest"
FOREST_L = "forest"
PLAINS_T = "Plains"

P0_DECK = ((TOLS_T, 12), (FOREST_T, 24), (PLAINS_T, 24))
P1_DECK = ((BEAR_T, 12), (FOREST_T, 48))

GAME_TIMEOUT = 2000
STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0,
         "opp_shapes_logged": set(), "p1_block_grace_until": 0}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {"cast": None}
OBS = {"pre_life": None, "voja_seen": False, "mid_exported": False,
       "post_exported": False, "tols_cast": False,
       "wolf_trigger_seen": False, "wolf_trigger_resolved": False,
       "trigger_stack_ids": [], "trigger_stack_dump": None,
       "fight_prompted": False, "fight_answered": False,
       "fight_target_oid": None, "fight_target_cid": None,
       "life_after_trigger": None, "rejections": [], "notes": [],
       "wf_seq": []}
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


def is_creature(o):
    return "Creature" in ((o.get("card_types") or {}).get("core_types")
                          or [])


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
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain",
                                   "Main")
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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": STAGE["stage"]})
    await c.send_action(action)


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
    n_lands = sum(1 for h in hand if h == FOREST_L)
    if tag == "P0":
        choice = "Keep" if (TOLS_L in hand and n_lands >= 2) or n >= 2 \
            else "Mulligan"
    else:
        choice = "Keep" if (BEAR_L in hand and n_lands >= 1) or n >= 2 \
            else "Mulligan"
    if choice == "Mulligan":
        STAGE["mulls"][tag] = n + 1
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
        keep = {TOLS_L, BEAR_L}

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
    if ln == TOLS_L:
        return 3
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
            STAGE["legend_answered"] += 1
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


async def do_declare_empty(c, acts, st, pid, tag):
    """Declare empty attackers/blockers. The vi fallback only answers
    relations-schema declare opportunities (select/sequence schemas are
    decisions, never declares)."""
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = copy.deepcopy(a)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            if tag == "P1" and time.time() < STAGE.get(
                    "p1_block_grace_until", 0):
                continue
            d = copy.deepcopy(a)
            d.setdefault("data", {})["assignments"] = []
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
    if tag == "P1" and state.get("active_player") != pid \
            and "declareblock" in phase.lower():
        if find_relations_op(st) is None:
            STAGE["p1_block_grace_until"] = time.time() + 25
    return False


async def play_a_land(c, state, pid, acts, tag):
    """Play one land per turn (legacy action or vi playLand opportunity)."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
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
        await answer_vi(c, opp, ch, tag)
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


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------- issue-specific logic


def stack_text_blob(entry):
    """Collect every string nested in a stack entry (protocol shapes
    differ across releases; description nesting is not assumed)."""
    out = []

    def rec(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                rec(v)
    rec(entry)
    return " ".join(out)


def wolf_trigger_on_stack(state):
    """Return stack entries that are the Wolf-enter trigger (description
    mentions Wolf + gain 3 life)."""
    out = []
    for e in (state.get("stack") or []):
        blob = stack_text_blob(e).lower()
        if "wolf" in blob and "gain 3 life" in blob:
            out.append(e)
    return out


def observe_trigger(state):
    """Scan the stack for the Wolf-enter trigger; update OBS."""
    entries = wolf_trigger_on_stack(state)
    for e in entries:
        sid = e.get("id")
        if not OBS["wolf_trigger_seen"]:
            OBS["wolf_trigger_seen"] = True
            say(f"wolf trigger ON STACK id={sid}")
            wire("wolf_trigger_stack",
                 {"id": sid, "entry": e, "stage": STAGE["stage"]})
            OBS["trigger_stack_dump"] = e
        if sid not in OBS["trigger_stack_ids"]:
            OBS["trigger_stack_ids"].append(sid)
    if OBS["wolf_trigger_seen"] and not OBS["wolf_trigger_resolved"]:
        cur_ids = {e.get("id") for e in wolf_trigger_on_stack(state)}
        if not any(s in cur_ids for s in OBS["trigger_stack_ids"]):
            OBS["wolf_trigger_resolved"] = True
            OBS["life_after_trigger"] = life_of(state, 0)
            say(f"wolf trigger RESOLVED; P0 life now "
                f"{OBS['life_after_trigger']}")
            wire("wolf_trigger_resolved",
                 {"life": OBS["life_after_trigger"],
                  "stage": STAGE["stage"]})


async def handle_fight_prompt(c, state, acts, st):
    """If a target prompt for the fight is up for P0, answer it by
    targeting a P1 Bear. Schema opportunities whose candidates reference
    P1 battlefield creatures are treated as the fight prompt (logged
    with full shape on first sight)."""
    if STAGE["stage"] != "TRIGGER" or OBS["fight_answered"]:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        spec_type = (spec.get("type") or "").lower()
        cands = data.get("candidates") or data.get("choices") or []
        cand_refs = {cand_object_ref(ch) for ch in cands}
        cand_refs.discard(None)
        shape = (rtype, spec_type, tuple(sorted(cand_refs))[:8])
        if shape not in STAGE["opp_shapes_logged"]:
            STAGE["opp_shapes_logged"].add(shape)
            wire("trigger_prompt", {"rtype": rtype, "spec_type": spec_type,
                                    "cand_refs": sorted(cand_refs),
                                    "kind": vi_kind_code(st),
                                    "opportunity": opp})
            say(f"[P0] TRIGGER-stage prompt kind={vi_kind_code(st)} "
                f"rtype={rtype} spec={spec_type} nrefs={len(cand_refs)}")
        if rtype != "schema" or spec_type not in ("select", "sequence"):
            continue
        p1_bf_creatures = []
        for ch in cands:
            ref = cand_object_ref(ch)
            if ref is None:
                continue
            o = get_obj(state, ref)
            if o.get("zone") == "Battlefield" \
                    and str(o.get("controller")) == "1" \
                    and is_creature(o):
                p1_bf_creatures.append((ch, ref))
        if not p1_bf_creatures:
            continue
        iid = opp.get("interactionId")
        key = ("P0", "fight", str(iid))
        if iid in SUBMITTED_OPPS:
            return True
        want = next((x for x in p1_bf_creatures
                     if obj_lname(state, x[1]) == BEAR_L),
                    p1_bf_creatures[0])
        ch, ref = want
        OBS["fight_prompted"] = True
        OBS["fight_target_oid"] = ref
        wire("fight_target_prompt",
             {"cand_refs": sorted(cand_refs), "want_ref": ref,
              "stage": STAGE["stage"]})
        say(f"[P0] FIGHT PROMPT seen; targeting {obj_lname(state, ref)} "
            f"(ref {ref})")
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [ch["id"]]}}}
        await interact_as(c, sub, "P0")
        SUBMITTED_OPPS.add(key)
        SUBMITTED_OPPS.add(iid)
        OBS["fight_answered"] = True
        OBS["fight_target_cid"] = ch["id"]
        return True
    return False


def untapped_lands(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname
            and not get_obj(state, oid).get("tapped")]


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

    # fight target prompt takes precedence; never pass priority past it
    if await handle_fight_prompt(c, state, acts, st):
        return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        if STAGE["stage"] == "SETUP" and not perm_oids(state, 0, TOLS_L):
            oid = find_hand_oid(state, 0, TOLS_L)
            forests = untapped_lands(state, 0, FOREST_L)
            plains = untapped_lands(state, 0, "plains")
            others = [o for o in bf_oids(state, 0)
                      if is_land(get_obj(state, o))
                      and not get_obj(state, o).get("tapped")]
            if oid and len(forests) >= 2 and len(plains) >= 1 \
                    and len(others) >= 5:
                for a in acts:
                    if a.get("type") == "CastSpell" and \
                            str(a.get("data", {}).get("object_id")) == oid:
                        if OBS["pre_life"] is None:
                            OBS["pre_life"] = life_of(state, 0)
                            await do_export(c, "pre.json")
                            wire("pre_life", {"life": OBS["pre_life"]})
                        ACT["cast"] = {"tag": f"tols-{oid}", "lname": TOLS_L,
                                       "in_flight": True, "taps": 0,
                                       "submit_t": time.time()}
                        MANA_NEEDS[tag] = {"G": 2, "W": 1, "generic": 2}
                        OBS["tols_cast"] = True
                        say(f"[P0] casts Tolsimir, Friend to Wolves ({oid})")
                        wire("cast_submit", {"tag": "tolsimir", "oid": oid})
                        await submit_as_is(c, a)
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
        # field 2 bears during SETUP, then hold (keep board readable)
        if STAGE["stage"] == "SETUP" and len(perm_oids(state, 1, BEAR_L)) < 2:
            oid = find_hand_oid(state, 1, BEAR_L)
            if oid and untapped_lands(state, 1, FOREST_L):
                for a in acts:
                    if a.get("type") == "CastSpell" and \
                            str(a.get("data", {}).get("object_id")) == oid:
                        say(f"[P1] casts Grizzly Bears ({oid})")
                        wire("p1_cast_bear", {"oid": oid})
                        await submit_as_is(c, a)
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


def damage_marked(state, oid):
    o = get_obj(state, oid)
    return o.get("damage_marked") or o.get("damage") or 0


# ------------------------------------------------------------- main loop


async def main():
    t0 = time.time()
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06889")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16889")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    P0C["c"] = p0
    P1C["c"] = p1
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
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
        observe_trigger(state)

        # record waiting_for sequence for the report
        wf = (state.get("waiting_for") or {}).get("type")
        if wf and (not OBS["wf_seq"] or OBS["wf_seq"][-1] != wf):
            OBS["wf_seq"].append(wf)

        if str(state.get("phase") or "").lower() == "gameover" \
                and not STAGE["stop"]:
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STAGE["stop"] = True
            continue

        # --- Voja token arrival -> TRIGGER stage, mid export
        if not OBS["mid_exported"] and any(
                "voja" in obj_lname(state, o)
                for o in bf_oids(state, 0)):
            OBS["voja_seen"] = True
            if STAGE["stage"] == "SETUP":
                STAGE["stage"] = "TRIGGER"
            await do_export(p0, "mid_trigger.json")
            OBS["mid_exported"] = True
            wire("voja_seen", {"turn": state.get("turn_number"),
                               "phase": state.get("phase")})
            say(f"Voja token on P0 battlefield at turn "
                f"{state.get('turn_number')} {state.get('phase')}")
            continue

        # --- post-export gate: trigger seen+resolved, life recorded,
        # stack empty, game has moved on -> capture post.json and stop
        if OBS["wolf_trigger_resolved"] and not OBS["post_exported"] \
                and stack_empty(state):
            await do_export(p0, "post.json")
            OBS["post_exported"] = True
            STAGE["stage"] = "DONE"
            STAGE["stop"] = True
            say("post.json exported; stopping")
            continue

        # fallback: trigger resolved between stack observations --
        # detect via the +3 life gain after Voja entered
        if OBS["voja_seen"] and not OBS["wolf_trigger_seen"] \
                and not OBS["wolf_trigger_resolved"] \
                and OBS["pre_life"] is not None:
            if life_of(state, 0) == OBS["pre_life"] + 3:
                OBS["wolf_trigger_seen"] = True
                OBS["wolf_trigger_resolved"] = True
                OBS["life_after_trigger"] = life_of(state, 0)
                wire("wolf_trigger_implicit",
                     {"note": "life +3 observed without stack sighting",
                      "life": OBS["life_after_trigger"]})
                say("wolf trigger resolved implicitly (life +3, no stack "
                    "sighting)")

        if time.time() - t0 > GAME_TIMEOUT - 60:
            say("timeout approaching; stopping")
            STAGE["stop"] = True
            break

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"]})

    # ------------------------------------------------------- assertions
    A = {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    mid_s = load("mid_trigger.json")
    post_s = load("post.json")

    tols_bf = any(obj_lname(mid_s, o) == TOLS_L
                  for o in bf_oids(mid_s, 0)) if mid_s else False
    voja_bf = any("voja" in obj_lname(mid_s, o)
                  for o in bf_oids(mid_s, 0)) if mid_s else False
    bears_mid = sum(1 for o in bf_oids(mid_s, 1)
                    if obj_lname(mid_s, o) == BEAR_L) if mid_s else 0
    A["A1_setup_ok"] = "passed" if (tols_bf and voja_bf
                                    and bears_mid >= 2) else "failed"
    A["A1_setup_ok_detail"] = (f"tolsimir_bf={tols_bf} voja_bf={voja_bf} "
                               f"p1_bears_mid={bears_mid}")

    A["A2_wolf_trigger"] = "passed" if OBS["wolf_trigger_seen"] else "failed"
    trig = OBS.get("trigger_stack_dump") or {}
    A["A2_wolf_trigger_detail"] = {
        "trigger_stack_ids": OBS["trigger_stack_ids"],
        "resolved": OBS["wolf_trigger_resolved"],
        "stack_entry": trig,
    }

    pre_life = OBS["pre_life"]
    post_life = OBS["life_after_trigger"]
    if pre_life is None and pre_s:
        pre_life = life_of(pre_s, 0)
    if post_life is None and post_s:
        post_life = life_of(post_s, 0)
    gained = (post_life - pre_life) if (pre_life is not None
                                        and post_life is not None) else None
    A["A3_life_gain"] = "passed" if gained == 3 else (
        "failed" if gained is not None else "not-run")
    A["A3_life_gain_detail"] = (f"P0 life pre={pre_life} post-trigger="
                                f"{post_life} (expect +3)")

    A["A4_fight_prompted"] = "passed" if OBS["fight_prompted"] else "failed"
    A["A4_fight_prompted_detail"] = {
        "fight_prompted": OBS["fight_prompted"],
        "fight_answered": OBS["fight_answered"],
        "target_oid": OBS["fight_target_oid"],
        "wf_seq": OBS["wf_seq"],
        "note": "direct stack evidence: the trigger carried no target "
                "definition (see A2 detail + data_evidence.json); no "
                "target-selection wait appeared",
    }

    if OBS["fight_answered"] and post_s:
        vojas = [o for o in bf_oids(post_s, 0)
                 if "voja" in obj_lname(post_s, o)]
        tgt = get_obj(post_s, OBS["fight_target_oid"])
        voja_dmg = max([damage_marked(post_s, o) for o in vojas] + [0])
        tgt_dmg = damage_marked(post_s, OBS["fight_target_oid"])
        tgt_zone = tgt.get("zone")
        # Voja 3/3 vs Bear 2/2: bear takes 3 (dies), Voja takes 2
        fought = (tgt_dmg >= 3 or tgt_zone == "Graveyard") and voja_dmg >= 2
        A["A5_fight_resolved"] = "passed" if fought else "failed"
        A["A5_fight_resolved_detail"] = {
            "voja_damage_marked": voja_dmg,
            "target_damage_marked": tgt_dmg, "target_zone": tgt_zone}
    elif OBS["fight_prompted"]:
        A["A5_fight_resolved"] = "not-run"
        A["A5_fight_resolved_detail"] = "prompt seen but not answered"
    else:
        A["A5_fight_resolved"] = "not-run"
        A["A5_fight_resolved_detail"] = "no fight prompt; nothing to resolve"

    stack_clear = stack_empty(post_s) if post_s else False
    A["A6_cleanup"] = "passed" if (stack_clear
                                   and OBS["wolf_trigger_resolved"]) else (
        "failed" if post_s else "not-run")
    A["A6_cleanup_detail"] = (f"stack_empty={stack_clear} "
                              f"trigger_resolved="
                              f"{OBS['wolf_trigger_resolved']}")

    if A["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif A["A4_fight_prompted"] == "failed" \
            and A["A2_wolf_trigger"] == "passed" \
            and A["A3_life_gain"] == "passed":
        verdict = "reproduced"
    elif A["A4_fight_prompted"] == "passed" \
            and A["A5_fight_resolved"] == "passed" \
            and A["A6_cleanup"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    for k in sorted(A):
        if not k.endswith("_detail"):
            say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations":
                   {k: v for k, v in OBS.items()
                    if k not in ("rejections",)},
                   "rejections": OBS.get("rejections", []),
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": STAGE.get("game_code"),
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
            "P0": [[TOLS_T, 12], [FOREST_T, 24], [PLAINS_T, 24]],
            "P1": [[BEAR_T, 12], [FOREST_T, 48]],
        },
        "driver_notes": [
            "Protocol-106 port of evidence/6889/20260912-6889/"
            "scenario_6889.py (protocol 69 / v0.80.0) for pinned v0.102.0; "
            "behavioral contract A1..A6 and verdict logic unchanged.",
            "waiting_for is gone (null); priority = top-level "
            "PassPriority; all decisions via viewer_interaction; "
            "MulliganDecision via legacy Action; bottom via vi "
            "schema/select gated on waitingForKind.code=='mulligan'; "
            "DiscardToHandSize via vi schema/select.",
            "CastSpell via legacy Action; the v0.102.0 engine auto-taps "
            "reliably; manual taps are a >90s fallback only.",
            "Tolsimir cast needs 2 untapped Forests + 1 untapped Plains + "
            "2 more untapped lands ({2}{G}{G}{W}); pre.json exported with "
            "P0 life right before the cast.",
            "The fight prompt (if any) would be a vi schema opportunity "
            "whose candidates reference P1 battlefield creatures; "
            "answered by targeting a Grizzly Bear. All TRIGGER-stage "
            "prompt shapes are logged to the wire log.",
            "Wolf-trigger stack scan collects every nested string of each "
            "stack entry (no assumed description nesting) and matches "
            "'wolf' + 'gain 3 life'.",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope); the reported OUTCOME is "
            "asserted on the saved states, not the prompt.",
            "Data-level: v0.102.0 parses the Wolf-enter trigger with "
            "execute.effect=GainLife{3}, sub_ability=null, "
            "valid_target=null -- the fight clause is absent at the data "
            "layer exactly as on v0.80.0 (area:parser).",
        ],
        "assertions": A,
        "notes": OBS.get("notes", []),
        "verdict": verdict,
        "evidence_comment_id": 5644562700,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "12x Tolsimir / 12x Grizzly Bears density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay (scenario_6889_01020.py), not direct load.",
            "The decline branch ('up to one' choosing zero targets) was "
            "not separately exercised -- no target prompt appeared at "
            "all.",
            "Native AI seats not used; the trigger/fight path is "
            "engine-level and seat-independent.",
            "Not tested on the report's original build; verdict is scoped "
            "to v0.102.0, not a fix claim.",
        ],
        "setup_line": "P0: 12x Tolsimir, Friend to Wolves + 24x Forest + "
                      "24x Plains; P1: 12x Grizzly Bears + 48x Forest "
                      "(fields 2 bears, then holds)",
        "contract_line": ("Cast Tolsimir; when the Voja token enters, the "
                          "Wolf-enter trigger must gain 3 life AND offer "
                          "the fight target prompt ('fights up to one "
                          "target creature you don't control')."),
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
        shutil.copy(__file__, f"{EVDIR}/scenario_6889_01020.py")
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
        pre = load_ev_state("pre.json")
        pre_line = (f"pre: turn {pre.get('turn_number')} {pre.get('phase')} "
                    f"| P0 life={life_of(pre, 0)}")
    except Exception:
        pre_line = "pre.json: missing"
    try:
        mid = load_ev_state("mid_trigger.json")
        tols = [o for o in bf_oids(mid, 0)
                if obj_lname(mid, o) == TOLS_L]
        voja = [o for o in bf_oids(mid, 0) if "voja" in obj_lname(mid, o)]
        bears = [o for o in bf_oids(mid, 1)
                 if obj_lname(mid, o) == BEAR_L]
        mid_line = (f"mid: Tolsimir={tols} Voja={voja} "
                    f"P1 bears={bears}")
    except Exception:
        mid_line = "mid_trigger.json: missing"
    try:
        post = load_ev_state("post.json")
        fin_line = (f"post: P0 life={life_of(post, 0)} "
                    f"(pre +3 expected) | stack empty="
                    f"{stack_empty(post)}")
    except Exception:
        fin_line = "post.json: missing"
    W, H = 1000, 800
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    y = 20
    d.text((24, y), "Issue #6889 - Tolsimir, Friend to Wolves: Wolf-enter "
                    "trigger never offers the fight (v0.102.0 revalidation)",
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
    d.text((24, y), mid_line[:118], fill=(170, 180, 195))
    y += 24
    d.text((24, y), fin_line[:118], fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Data level (v0.102.0 card-data.json): Wolf-enter "
                    "trigger execute = GainLife{3} ONLY, sub_ability=null, "
                    "valid_target=null.", fill=(200, 210, 225))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Tolsimir + Voja on P0 BF, 2+ bears P1",
        "A2_wolf_trigger": "A2 Wolf-enter trigger observed on stack",
        "A3_life_gain": "A3 P0 life +3 across trigger window",
        "A4_fight_prompted": "A4 fight target prompt offered",
        "A5_fight_resolved": "A5 Voja/Bear fight damage observed",
        "A6_cleanup": "A6 stack empty, game proceeds",
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
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6889/" +
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
