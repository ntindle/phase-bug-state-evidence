#!/usr/bin/env python3
"""Issue #6772: Ultimecia, Omnipotent does not trigger the extra turn when transformed.

Protocol-106 port (v0.102.0, 2026-10-05) of scenario_6772_0980.py
(verified 2026-09-30, run 20260930-6772, v0.98.0/protocol 94).

Reported (Discord): "[[Ultimecia, Omnipotent]] does not trigger the extra turn
when transformed".

Oracle (pinned v0.102.0 card-data.json):
  Ultimecia, Time Sorceress ({3}{U}{B}, 4/5):
    "Whenever Ultimecia enters or attacks, surveil 2.
     At the beginning of your end step, you may pay {4}{U}{U}{B}{B} and exile
     eight cards from your graveyard. If you do, transform Ultimecia."
  Ultimecia, Omnipotent (7/7, menace):
    "Time Compression - When this creature transforms into Ultimecia,
     Omnipotent, take an extra turn after this one."

Pinned parse (v0.102.0 data, see data_evidence.json): front face carries a
Phase/End optional trigger (PayCost Mana {Blue,Blue,Black,Black}+4 generic
-> ChangeZone Graveyard->Exile Typed Card controller You, multi_target
min 8/max 8 -> Transform SelfRef, gated on OptionalEffectPerformed); back
face carries a Transformed trigger with an ExtraTurn child for its
controller.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, v0.102.0/protocol 106):
  P0: 12x Ultimecia, Time Sorceress + 12x Otherworldly Gaze + 18x Island +
      18x Swamp. Keeps mulligan.
  P1: 60x Island dummy (plays a land, passes; never attacks/blocks).

Expected (per card text):
  RAMP   - P0 plays a land per turn (color-balanced Island/Swamp, explicit
           play_a_land BEFORE the decision gate; playLand is a
           NON_DECISION_CODE), keeps mulligan, casts Otherworldly Gaze to
           mill (surveil 3; mill until gy>=8).
  CAST   - P0 casts Ultimecia, Time Sorceress when affordable ({3}{U}{B});
           its enters-surveil mills 2 more. No attacks (no combat needed).
  READY  - when P0 graveyard >= 8 and 8 untapped lands (4 Island + 4 Swamp
           sources) are available, P0 stops casting Gazes and passes to the
           end step.
  ACCEPT - at P0's End phase, when the optional transform prompt is pending
           for P0 (106: decideOptionalEffect-style opportunity; the raw
           opportunity is wired): export pre_transform FIRST, then accept
           (pay). DECLINE on earlier end steps only.
  PAY    - pay {4}{U}{U}{B}{B} via vi tapLandForMana (needs tracked);
           exile exactly 8 cards from P0's graveyard at the selection prompt
           (schema select, gy-referenced candidates); transform resolves.
  WATCH  - record (turn_number, active_player) across the turn boundary.
           Extra turn expected: turn T+1 active_player == 0.
           Then turn T+2 active_player == 1 (exactly one extra turn).
  STOP   - after P0's extra turn reaches its main phase and P1's following
           turn begins (turn T+2): export post_extra_turn, stop.

Assertions:
  A1 setup_ok            pre_transform: P0 End phase, Time Sorceress on P0
                         battlefield, P0 gy >= 8, >= 4 untapped Islands and
                         >= 4 untapped Swamps.
  A2 transform_resolved   post: Ultimecia, Omnipotent on P0 battlefield.
  A3 extra_turn_next      the turn after T (T+1) has active_player == 0.
  A4 extra_turn_taken     P0 reached a main phase on turn T+1.
  A5 single_extra         turn T+2 has active_player == 1.
  A6 cleanup              post_extra_turn: stack empty.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes, A2 passes, and A3 fails (transform
          happened but no extra turn followed).
Verdict = not-reproduced iff A1..A6 all pass.
NEVER "fixed".

Evidence: evidence/6772/20261005-6772/pre_transform.json,
post_extra_turn.json, assertions.json, data_evidence.json, run.json,
manifest.sha256, summary.png, scenario_6772.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402
import websockets  # noqa: E402

ISSUE = 6772
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261005-6772"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

SPELL = "ultimecia, time sorceress"
BACKFACE = "ultimecia, omnipotent"
GAZE = "otherworldly gaze"
ISLAND = "island"
SWAMP = "swamp"

P0_DECK = [("Ultimecia, Time Sorceress", 12), ("Otherworldly Gaze", 12),
           ("Island", 18), ("Swamp", 18)]
P1_DECK = [("Island", 60)]

NEEDS_ULTIMECIA = {"U": 1, "B": 1, "generic": 5}
NEEDS_GAZE = {"U": 1, "generic": 1}
NEEDS_TRANSFORM = {"U": 2, "B": 2, "generic": 4}

ST = {"cast_done": False, "pre_exported": False, "accepted": False,
      "transform_turn": None, "post_exported": False, "stop": False,
      "stall_since": None, "extra_main_seen": False, "transform_observed": False,
      "mana_needs": {}, "states_seen": 0}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
WIRED_OPPS = set()   # interactionIds of raw-wired transform candidates
WF_SEEN = []
TURN_SEQ = []        # (turn_number, active_player) transitions observed


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ------------------------------------------------------------------ helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def is_land(o):
    # covers Island/Swamp and every other land the seats can hold
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def hand_ids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def gy_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def untapped_land_count(state, pid, lname=None):
    n = 0
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped") \
                and (lname is None or obj_lname(state, o) == lname):
            n += 1
    return n


def lib_count(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return len(p.get("library", []) or [])
    return None


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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


# ------------------------------------------------------- interaction prims

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
    await c.send_interaction(sub)


async def verify_server_hello():
    ws = await websockets.connect("ws://localhost:9374/ws", max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = json.loads(raw).get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"
    return ver, build, proto


def check_data_level():
    """Pinned v0.102.0 parse: front Phase/End optional trigger (PayCost
    {UUBB}+4 -> exile 8 from own gy -> Transform self); back Transformed ->
    ExtraTurn controller."""
    ok, notes = True, []
    front = CARD_DATA.get("ultimecia, time sorceress", {})
    back = CARD_DATA.get("ultimecia, omnipotent", {})
    payload = {}
    try:
        trigs = front.get("triggers") or []
        end_trig = [t for t in trigs
                    if t.get("mode") == "Phase" and t.get("phase") == "End"]
        assert end_trig, "no Phase/End trigger on front face"
        t = end_trig[0]
        assert t.get("optional") is True, "end trigger not optional"
        ex = t.get("execute") or {}
        pay = (ex.get("effect") or {})
        assert pay.get("type") == "PayCost", "first effect not PayCost"
        cost = ((pay.get("cost") or {}).get("cost")) or {}
        shards = cost.get("shards") or []
        assert sorted(shards) == ["Black", "Black", "Blue", "Blue"], \
            f"shards {shards}"
        assert cost.get("generic") == 4, "generic != 4"
        sub1 = (ex.get("sub_ability") or {})
        e1 = sub1.get("effect") or {}
        assert e1.get("type") == "ChangeZone", "sub1 not ChangeZone"
        assert e1.get("origin") == "Graveyard" \
            and e1.get("destination") == "Exile", "sub1 zones wrong"
        mt = sub1.get("multi_target") or {}
        assert mt.get("min") == 8, "exile min != 8"
        assert (mt.get("max") or {}).get("value") == 8, "exile max != 8"
        sub2 = (sub1.get("sub_ability") or {})
        e2 = sub2.get("effect") or {}
        assert e2.get("type") == "Transform", "sub2 not Transform"
        payload["front_end_trigger"] = {
            "optional": t.get("optional"),
            "pay_cost": f"{sorted(shards)}+{cost.get('generic')} generic",
            "exile": "Graveyard->Exile own cards min8 max8",
            "transform": True,
            "description": t.get("description")}
        btrigs = back.get("triggers") or []
        bt = [x for x in btrigs if x.get("mode") == "Transformed"]
        assert bt, "no Transformed trigger on back face"
        beff = ((bt[0].get("execute") or {}).get("effect")) or {}
        assert beff.get("type") == "ExtraTurn", "back effect not ExtraTurn"
        assert (beff.get("target") or {}).get("type") == "Controller", \
            "ExtraTurn target not Controller"
        payload["back_transformed_trigger"] = {
            "effect": "ExtraTurn", "target": "Controller",
            "description": bt[0].get("description")}
    except AssertionError as e:
        ok = False
        notes.append(f"data-level check failed: {e}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "front_oracle": front.get("oracle_text"),
                   "back_oracle": back.get("oracle_text"),
                   "parsed_triggers": payload}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok


def mulligan_pending_for(state, pid):
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "Declare":
            return True
    return False


async def do_mulligan(c, acts, st, pid, tag):
    state = st["state"]
    if mulligan_pending_for(state, pid):
        key = (tag, "mull", f"rev{c.revision}")
        if key in MULLS:
            return True
        adv = next((a for a in acts if a.get("type") == "MulliganDecision"),
                   None)
        if adv is None:
            return False
        MULLS.add(key)
        hn = hand_ids(state, pid)
        say(f"[{tag}] keeps (hand={len(hn)})")
        wire("mulligan", {"who": tag, "decision": "keep",
                          "hand": [obj_lname(state, o) for o in hn]})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        return True
    return False


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        codes = set()
        for ch in cands:
            codes.update(x for x in surf_codes(ch) if x)
        blob = (json.dumps([choice_text(ch) for ch in cands])
                + str(opp.get("description", ""))).lower()
        if not any("discard" in (x or "").lower() for x in codes) \
                and "discard" not in blob:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "sequence"

        def rank(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else ""
            if nm == SPELL:
                return (0, nm)   # extra Ultimecias first
            if nm == GAZE:
                return (1, nm)
            return (2, nm)        # lands last

        picks = [ch["id"] for ch in
                 sorted(cands, key=rank)[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding {len(picks)} to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def answer_surveil(c, st, pid, tag, mill_all):
    """Surveil schema select: SELECTED candidates stay on top of the library
    and the rest go to the graveyard (verified 2026-09-11: selecting all
    milled 0). So mill_all submits an empty selection."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates", []) or []
        ids = [ch.get("id") for ch in cands if ch.get("id")]
        if not ids:
            continue
        # never confuse with the exile-8 selection: surveil shows 2-3
        # candidates; the exile prompt shows >=8 gy-referenced ones
        if len(ids) >= 8:
            continue
        pick = [] if mill_all else ids
        wire("surveil_answer", {"who": tag, "iid": iid,
                               "mill_all": mill_all, "picked": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": spec.get("type") or "select",
                                           "data": {"choiceIds": pick}}}, tag)
        say(f"[{tag}] surveil -> {'mill ' + str(len(ids)) if mill_all else 'keep on top'}")
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
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
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
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
        if needs:
            for ch, s in taps:
                for color in ("U", "B"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                # prefer Islands for generic so Swamps stay untapped
                for ch, s in taps:
                    if "U" in s:
                        pick, used = ch, "generic"
                        break
                if pick is None:
                    pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used} needs_left={needs}")
        wire("tap_land", {"who": tag, "used_for": used,
                         "needs_left": dict(needs or {})})
        iid2 = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        cid = pick.get("id")
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid2,
                   "response": {"type": stype, "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid2,
                   "response": {"type": "choose", "data": {"choiceId": cid}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
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
                await interact_as(c, {"interactionId": opp.get("interactionId"),
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag, order):
    """Explicit land-play BEFORE the decision gate. playLand is a
    NON_DECISION_CODE: offering it is never a forced decision."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for ln in order:
        for o in hand_ids(state, pid):
            if obj_lname(state, o) != ln:
                continue
            for a in acts:
                if a["type"] == "PlayLand" \
                        and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {ln}")
                    wire("play_land", {"who": tag, "oid": o, "name": ln})
                    await submit_as_is(c, a)
                    return True
    return False


def can_pay(state, pid, needs):
    return untapped_land_count(state, pid) >= sum(needs.values())


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def opp_codes(opp):
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    codes = set()
    for ch in (data.get("candidates") or data.get("choices") or []):
        codes.update(x for x in surf_codes(ch) if x)
    return codes


def find_transform_prompt(st, state, pid):
    """The optional end-step transform prompt on 106: decideOptionalEffect
    exactChoices (bare true/false, Demonstrate-style) or a schema
    decideOptionalCost surface. The exile-8 selection (schema select with
    >=8 gy-referenced candidates) is NOT this -- it is handled separately."""
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        codes = opp_codes(opp)
        blob = (str(opp.get("description", "")) + " " +
                " ".join(choice_text(ch) for ch in items)).lower()
        # exile-8 selection: exclude (handled by find_exile8)
        if rtype == "schema" and len(items) >= 8:
            gy = sum(1 for ch in items
                     if get_obj(state, _cand_reference(ch)).get("zone")
                     == "Graveyard")
            if gy >= 8:
                continue
        if "decideOptionalEffect" in codes:
            out.append(("decideOptionalEffect", opp))
        elif "decideOptionalCost" in codes:
            out.append(("decideOptionalCost", opp))
        elif ("transform" in blob or "exile eight" in blob
              or "exile 8" in blob) and rtype == "exactChoices":
            out.append(("exactChoices-transform", opp))
    for kind, opp in out:
        iid = opp.get("interactionId") or opp.get("id")
        if iid not in WIRED_OPPS:
            WIRED_OPPS.add(iid)
            wire("transform_opportunity", {"who": f"P{pid}", "kind": kind,
                                          "opp": opp,
                                          "wf": (st.get("state", {})
                                                 .get("waiting_for"))})
    return out[0] if out else (None, None)


async def answer_transform_bool(c, opp, tag, accept):
    """exactChoices true/false with ("value","accept","true"|"false")
    surfaces (106 Demonstrate style). accept=True pays and transforms."""
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    choices = data.get("choices") or []
    want = "true" if accept else "false"
    best = None
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if s.get("type") == "value" and dd.get("role") == "accept" \
                    and str(dd.get("value")).lower() == want:
                best = ch.get("choiceId") or ch.get("id")
                break
        if best:
            break
    if best is None:
        for ch in choices:
            txt = json.dumps(ch, default=str).lower()
            cid = ch.get("choiceId") or ch.get("id")
            if accept and '"true"' in txt and '"false"' not in txt:
                best = cid
                break
            if not accept and '"false"' in txt:
                best = cid
                break
    if best is None and choices:
        ch = choices[0] if accept else choices[-1]
        best = ch.get("choiceId") or ch.get("id")
    if not best:
        say(f"[{tag}] transform prompt: no {'affirmative' if accept else 'negative'} choice")
        return False
    say(f"[{tag}] transform prompt -> {'ACCEPT' if accept else 'DECLINE'} ({best})")
    wire("transform_answer", {"iid": opp.get("interactionId"), "accept": accept,
                              "choice": best})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": "choose",
                                       "data": {"choiceId": best}}}, tag)
    return True


async def answer_transform_schema(c, opp, tag, accept):
    """schema decideOptionalCost surface: the candidate itself is the
    accept; declining = answer with an empty selection if supported, else
    leave pending and let the main loop decline path handle it."""
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    cands = data.get("candidates") or []
    if not accept:
        return False
    cid = None
    for ch in cands:
        if "decideOptionalCost" in surf_codes(ch):
            cid = ch.get("id")
            break
    if cid is None and cands:
        cid = cands[0].get("id")
    if cid is None:
        return False
    say(f"[{tag}] transform prompt (schema decideOptionalCost) -> ACCEPT")
    wire("transform_answer", {"iid": opp.get("interactionId"), "accept": True,
                              "style": "decideOptionalCost", "choice": cid})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": spec.get("type") or "choose",
                                       "data": {"choiceId": cid}}}, tag)
    return True


def find_exile8(st, state):
    """The 'exile eight cards from your graveyard' selection: schema select
    whose candidates reference >=8 objects in the Graveyard."""
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates", []) or []
        gy_ids = [ch.get("id") for ch in cands
                  if ch.get("id")
                  and get_obj(state, _cand_reference(ch)).get("zone")
                  == "Graveyard"]
        if len(gy_ids) >= 8:
            return opp, gy_ids[:8]
    return None, None


async def answer_exile_eight(c, st, state, pid, tag):
    opp, picks = find_exile8(st, state)
    if opp is None:
        return False
    if len(gy_ids(state, pid)) < 8:
        wire("exile8_waiting_gy", {"gy": len(gy_ids(state, pid))})
        return "wait"
    iid = opp.get("interactionId")
    resp = opp.get("response") or {}
    spec = (resp.get("data") or {}).get("spec") or {}
    stype = spec.get("type") or "select"
    wire("exile8_answer", {"who": tag, "iid": iid, "picked": picks,
                          "stype": stype})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": stype,
                                       "data": {"choiceIds": picks}}}, tag)
    say(f"[{tag}] exile 8 from graveyard (schema select, {len(picks)})")
    return True


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


def record_turn(state):
    t = (state.get("turn_number"), state.get("active_player"))
    if not TURN_SEQ or TURN_SEQ[-1] != t:
        TURN_SEQ.append(t)
        wire("turn", {"turn": t[0], "active": t[1],
                      "phase": state.get("phase")})


def has_backface(state):
    for o in (state.get("objects") or {}).values():
        if o.get("zone") == "Battlefield" and o.get("controller") == 0:
            nm = str(o.get("base_name") or o.get("name") or "").lower()
            if "omnipotent" in nm:
                return True
            pw = o.get("power") or {}
            tu = o.get("toughness") or {}
            pv = pw.get("value") if isinstance(pw, dict) else pw
            tv = tu.get("value") if isinstance(tu, dict) else tu
            if pv == 7 and tv == 7 and "time sorceress" not in nm:
                return True
    return False


def has_frontface(state):
    return bool(bf_by_name(state, 0, SPELL))


def transform_ready(state):
    """gy>=8, 4 untapped Islands, 4 untapped Swamps, front face on BF."""
    if len(gy_ids(state, 0)) < 8:
        return False
    if untapped_land_count(state, 0, ISLAND) < 4:
        return False
    if untapped_land_count(state, 0, SWAMP) < 4:
        return False
    return has_frontface(state)


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------------ ticks

async def p1_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag, None):
        return
    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 1 \
            and not (state.get("stack") or []):
        if await play_a_land(c, state, 1, acts, tag, (ISLAND,)):
            return
    if real_decision_pending(st):
        return
    if any(a.get("type") == "PassPriority" for a in acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def answer_declare_attackers(c, st, state, tag, acts, atypes):
    """Declare no attackers. Handles three 106 surfaces: an advertised
    legal action, a viewer-interaction opportunity (schema select or
    exactChoices), and -- if neither is advertised while waiting_for
    names us -- a blind submit of the 106 action shape (the engine
    accepts it when the wait is genuine). Returns True if something was
    submitted, False if the step is held."""
    state_wf = state.get("waiting_for") or {}
    da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
    if da is not None:
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = []
        d["bands"] = []
        wire("declare_attackers_action",
             {"turn": state.get("turn_number"), "blind": False})
        say(f"[{tag}] DeclareAttackers -> none (advertised action)")
        await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return True
    # vi opportunity?
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        blob = (str(opp.get("description", "")) + " " +
                " ".join(choice_text(ch) for ch in items)).lower()
        if "attack" not in blob:
            continue
        if iid not in WIRED_OPPS:
            WIRED_OPPS.add(iid)
            wire("declare_attackers_vi", {"iid": iid, "rtype": rtype,
                                         "opp": opp, "wf": state_wf})
        if rtype == "schema":
            spec = data.get("spec") or {}
            stype = spec.get("type") or "select"
            say(f"[{tag}] DeclareAttackers -> none (vi {stype})")
            await interact_as(c, {"interactionId": opp.get("interactionId"),
                                  "response": {"type": stype,
                                               "data": {"choiceIds": []}}},
                              tag)
            return True
        # exactChoices: pick a "no attack"/"done"-ish choice, else first
        best = None
        for ch in items:
            txt = choice_text(ch).lower()
            if any(k in txt for k in ("no attack", "none", "done",
                                     "declare no", "skip")):
                best = ch.get("choiceId") or ch.get("id")
                break
        if best is None and items:
            ch0 = items[0]
            best = ch0.get("choiceId") or ch0.get("id")
        if best:
            say(f"[{tag}] DeclareAttackers -> none (vi exactChoices {best})")
            await interact_as(c, {"interactionId": opp.get("interactionId"),
                                  "response": {"type": "choose",
                                               "data": {"choiceId": best}}},
                              tag)
            return True
    # blind fallback (30s retry bucket)
    key = ("da", state.get("turn_number"), int(time.time() // 30))
    if key not in SUBMITTED_OPPS:
        SUBMITTED_OPPS.add(key)
        wire("declare_attackers_blind",
             {"turn": state.get("turn_number"),
              "phase": state.get("phase"),
              "atypes": sorted(atypes),
              "vi_opps": len(vi_ops(st)),
              "wf_data": state_wf.get("data")})
        say(f"[{tag}] DeclareAttackers -> none (blind submit)")
        await c.send_action({"type": "DeclareAttackers",
                             "data": {"attacks": [], "bands": []}})
        return True
    return False


async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return

    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    # DeclareAttackers FIRST: never attack; nothing else may preempt it.
    if wtype == "DeclareAttackers" and wplayer == 0:
        await answer_declare_attackers(c, st, state, tag, acts, atypes)
        return

    # surveil (enters-trigger and Gaze): mill until gy>=8, then keep on top
    if (wtype or "") in ("SurveilChoice",):
        if await answer_surveil(c, st, 0, tag,
                               mill_all=len(gy_ids(state, 0)) < 8):
            return
        wire("surveil_noopportunity", {})
        return

    # optional end-step transform: ACCEPT only when fully ready (the
    # pre-export must capture the meaningful pre-state first, so hold
    # until ST["pre_exported"]). DECLINE on earlier end steps; the trigger
    # fires again next turn. Never pass while it pends.
    kind, opp = find_transform_prompt(st, state, 0)
    if opp is not None:
        ready = transform_ready(state)
        if ST["pre_exported"] and not ST["accepted"] and ready:
            if kind == "decideOptionalCost":
                ok = await answer_transform_schema(c, opp, tag, accept=True)
            else:
                ok = await answer_transform_bool(c, opp, tag, accept=True)
            if ok:
                ST["accepted"] = True
                ST["mana_needs"]["P0"] = dict(NEEDS_TRANSFORM)
                say(f"[{tag}] accepted transform; needs={ST['mana_needs']['P0']}")
                return True
            return False
        if not ready and not ST["pre_exported"] and not ST["accepted"]:
            # decline: exactChoices only; schema surfaces hold for main loop
            if kind != "decideOptionalCost":
                if await answer_transform_bool(c, opp, tag, accept=False):
                    wire("optional_decline", {"turn": state.get("turn_number"),
                                             "gy": len(gy_ids(state, 0))})
                    say(f"[{tag}] declined transform (not ready yet)")
                    return True
            wire("transform_hold_schema", {"kind": kind,
                                          "turn": state.get("turn_number")})
            return False
        wire("transform_hold", {"kind": kind, "ready": ready,
                               "pre_exported": ST["pre_exported"],
                               "accepted": ST["accepted"]})
        return False

    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get("P0", {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # exile-8 selection after accepting: only engage on a real prompt
    if ST["accepted"]:
        r = await answer_exile_eight(c, st, state, 0, tag)
        if r == "wait":
            return False
        if r:
            return True

    if "OrderTriggers" in atypes:
        oa = next((a for a in acts if a["type"] == "OrderTriggers"), None)
        if oa:
            wire("action_submit", {"who": "P0", "action": "OrderTriggers/asis"})
            await submit_as_is(c, oa)
            return True

    # never pass while a P0 decision is pending
    if real_decision_pending(st):
        return

    if "DeclareBlockers" in atypes:
        return

    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 0 \
            and not (state.get("stack") or []):
        # land drop every turn: color-balanced, explicit before decision gate
        n_isle = len(bf_by_name(state, 0, ISLAND))
        n_swamp = len(bf_by_name(state, 0, SWAMP))
        order = (ISLAND, SWAMP) if n_isle <= n_swamp else (SWAMP, ISLAND)
        if await play_a_land(c, state, 0, acts, tag, order):
            return
        # cast Ultimecia when affordable (only one needed)
        if not ST["cast_done"]:
            a, oid = cast_action_for(acts, state, SPELL)
            if a is not None and can_pay(state, 0, NEEDS_ULTIMECIA):
                say(f"[P0] casting Ultimecia, Time Sorceress (oid {oid})")
                wire("cast_ultimecia", {"oid": oid})
                ST["mana_needs"]["P0"] = dict(NEEDS_ULTIMECIA)
                await submit_as_is(c, a)
                ST["cast_done"] = True
                return True
        # cast Gazes to mill until ready for the end step
        if not ST["accepted"] and not transform_ready(state):
            a, oid = cast_action_for(acts, state, GAZE)
            if a is not None and can_pay(state, 0, NEEDS_GAZE):
                say(f"[P0] casting Otherworldly Gaze (oid {oid})")
                wire("cast_gaze", {"oid": oid})
                ST["mana_needs"]["P0"] = dict(NEEDS_GAZE)
                await submit_as_is(c, a)
                return True

    if any(a.get("type") == "PassPriority" for a in acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------------- main

async def main():
    t0 = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_transform_resolved", "A3_extra_turn_next",
            "A4_extra_turn_taken", "A5_single_extra", "A6_cleanup")}

    ver, build, proto = await verify_server_hello()
    data_ok = check_data_level()
    if not data_ok:
        notes.append("data-level check FAILED -- see data_evidence.json")

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    try:
        await p0.create(deck(*P0_DECK))
    except Exception as e:
        say(f"game creation failed: {e}")
        ass["A1_setup_ok"] = "blocked"
        notes.append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": ass, "notes": notes}, f, indent=2)
        await p0.close()
        return
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    last = {}
    last_tick_at = {}
    TIMEOUT = 1500
    accept_t = None
    progress_rev = -1
    progress_at = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if rev == last.get(c.name) and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, st, tag)
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        ST["states_seen"] += 1
        record_wf(state)
        record_turn(state)

        turn = state.get("turn_number")
        phase = state.get("phase") or ""
        active = state.get("active_player")

        # generic progress watchdog: no revision advance at all for 180s
        if max(p0.revision, p1.revision) != progress_rev:
            progress_rev = max(p0.revision, p1.revision)
            progress_at = time.time()
        if time.time() - progress_at > 180:
            await do_export(p0, "mid_stall.json")
            notes.append("generic stall watchdog: no revision advance >180s")
            say("[stall] generic watchdog fired")
            ST["stop"] = True
            continue

        # PRE: optional transform prompt pending for P0 at its End phase,
        # only once fully ready (gy>=8, mana available). Export FIRST,
        # then the tick accepts.
        if not ST["pre_exported"]:
            kind, opp = find_transform_prompt(st, state, 0)
            if opp is not None and phase == "End" and active == 0 \
                    and transform_ready(state):
                pre = await do_export(p0, "pre_transform.json")
                ST["pre_exported"] = True
                ST["transform_turn"] = pre.get("turn_number")
                wire("pre_transform", {"turn": ST["transform_turn"],
                                      "gy": len(gy_ids(pre, 0)),
                                      "islands": untapped_land_count(pre, 0, ISLAND),
                                      "swamps": untapped_land_count(pre, 0, SWAMP),
                                      "frontface_bf": has_frontface(pre),
                                      "prompt_kind": kind})
                say(f"[pre] exported turn {ST['transform_turn']} "
                    f"gy={len(gy_ids(pre, 0))} kind={kind}")

        # not-ready watchdog: transform never became ready in 600s
        if not ST["pre_exported"] and time.time() - t0 > 600:
            await do_export(p0, "mid_notready.json")
            notes.append("watchdog: transform never became ready after 600s "
                         f"(turn {turn}, gy={len(gy_ids(state, 0))}, "
                         f"untapped_islands={untapped_land_count(state, 0, ISLAND)}, "
                         f"untapped_swamps={untapped_land_count(state, 0, SWAMP)}, "
                         f"frontface={has_frontface(state)})")
            say("[stall] not-ready watchdog fired")
            ST["stop"] = True
            continue

        if ST["accepted"] and accept_t is None:
            accept_t = time.time()

        # transform observed?
        if ST["accepted"] and not ST["transform_observed"] \
                and has_backface(state):
            ST["transform_observed"] = True
            wire("transform_observed", {"turn": turn, "phase": phase})
            say(f"[observed] Ultimecia, Omnipotent on battlefield (turn {turn})")

        # stall watchdog: accepted but no transform within 240s
        if accept_t and not ST["transform_observed"] \
                and time.time() - accept_t > 240:
            await do_export(p0, "mid_stall.json")
            notes.append("stall: accepted transform but no Omnipotent "
                         "face observed within 240s")
            say("[stall] watchdog fired")
            ST["stop"] = True
            continue

        T = ST["transform_turn"]
        if T is not None:
            # extra-turn main phase observation
            if turn == T + 1 and active == 0 \
                    and phase in ("PreCombatMain", "PostCombatMain"):
                if not ST["extra_main_seen"]:
                    ST["extra_main_seen"] = True
                    wire("extra_turn_main", {"turn": turn, "phase": phase})
                    say(f"[observed] P0 main phase on turn {turn} (=T+1)")
            # post export once turn T+2 begins
            if turn == T + 2 and not ST["post_exported"]:
                await asyncio.sleep(1.0)
                post = await do_export(p0, "post_extra_turn.json")
                ST["post_exported"] = True
                wire("post_extra_turn", {"turn": post.get("turn_number"),
                                        "active": post.get("active_player"),
                                        "phase": post.get("phase"),
                                        "backface_bf": has_backface(post),
                                        "stack_empty": not (post.get("stack") or [])})
                say(f"[post] exported turn {post.get('turn_number')} "
                    f"active={post.get('active_player')}")
                ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre_transform.json")
    post = env_state("post_extra_turn.json")
    T = ST["transform_turn"]

    # A1
    if pre is None:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre_transform.json never exported")
    else:
        ok_phase = pre.get("active_player") == 0 and pre.get("phase") == "End"
        ok_face = has_frontface(pre)
        ok_gy = len(gy_ids(pre, 0)) >= 8
        ok_mana = untapped_land_count(pre, 0, ISLAND) >= 4 \
            and untapped_land_count(pre, 0, SWAMP) >= 4
        ass["A1_setup_ok"] = "passed" if (ok_phase and ok_face and ok_gy
                                         and ok_mana) else "failed"
        notes.append(
            f"pre_transform: end_phase={ok_phase} frontface_bf={ok_face} "
            f"gy={len(gy_ids(pre, 0))} (need>=8) "
            f"islands={untapped_land_count(pre, 0, ISLAND)} "
            f"swamps={untapped_land_count(pre, 0, SWAMP)} (need>=4 each) "
            f"turn={pre.get('turn_number')}")

    # A2
    ass["A2_transform_resolved"] = \
        "passed" if ST.get("transform_observed") else "failed"
    notes.append(f"Omnipotent face observed on battlefield: "
                 f"{bool(ST.get('transform_observed'))}")

    def active_of(target_turn):
        for tn, ap in TURN_SEQ:
            if tn == target_turn:
                return ap
        return None

    # A3
    if T is None:
        ass["A3_extra_turn_next"] = "not-run"
        notes.append("transform turn unknown; A3 not-run")
    else:
        a1 = active_of(T + 1)
        notes.append(f"turn sequence: {TURN_SEQ}; active(T+1)={a1}")
        ass["A3_extra_turn_next"] = "passed" if a1 == 0 else "failed"

    # A4
    ass["A4_extra_turn_taken"] = "passed" if ST["extra_main_seen"] else "failed"
    notes.append(f"P0 main phase observed on turn T+1: "
                 f"{ST['extra_main_seen']}")

    # A5
    if T is None:
        ass["A5_single_extra"] = "not-run"
        notes.append("transform turn unknown; A5 not-run")
    else:
        a2 = active_of(T + 2)
        ass["A5_single_extra"] = "passed" if a2 == 1 else "failed"
        notes.append(f"active(T+2)={a2} (expect 1)")

    # A6
    if post is not None:
        empty = not (post.get("stack") or [])
        notes.append(f"post_extra_turn: turn={post.get('turn_number')} "
                     f"active={post.get('active_player')} "
                     f"phase={post.get('phase')} stack_empty={empty}")
        ass["A6_cleanup"] = "passed" if empty else "failed"
    else:
        ass["A6_cleanup"] = "not-run"
        notes.append("post_extra_turn.json missing; A6 not-run")

    for k in sorted(ass):
        say(f"{k}: {ass[k]}")
    say("WF sequence:", WF_SEEN)
    say("Turn sequence:", TURN_SEQ)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN, "turn_sequence": TURN_SEQ,
                   "transform_turn": T,
                   "transform_observed": bool(ST.get("transform_observed"))},
                  f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if ass.get("A1_setup_ok") == "passed":
        if ass.get("A2_transform_resolved") == "passed" \
                and ass.get("A3_extra_turn_next") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
    say("verdict:", verdict)

    with open(f"{EVDIR}/scenario_6772.py", "w") as f:
        f.write(scenario_src.decode())

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    run_meta = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00",
                                    time.localtime(t0)),
        "server": {
            "server_version": "v0.102.0",
            "build_commit": "e17f6fd",
            "protocol_version": 106,
            "binary_sha256": sha(f"{BACKFILL}/server/releases/v0.102.0/"
                                  f"phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(f"{BACKFILL}/server/releases/v0.102.0/data/"
                                    f"card-data.json"),
            "draft_pools_sha256": sha(f"{BACKFILL}/server/releases/v0.102.0/data/"
                                      f"draft-pools.json"),
            "port": 9374,
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": dict(P0_DECK), "P1": dict(P1_DECK)},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"]},
        "setup_line": ("P0: 12x Ultimecia, Time Sorceress + 12x Otherworldly "
                       "Gaze + 18x Island + 18x Swamp; P1: 60x Island dummy; "
                       "native human seats"),
        "contract_line": ("ramp/mill via Gaze surveil until gy>=8, cast "
                          "Ultimecia ({3}{U}{B}), at P0 End step export "
                          "pre_transform then accept the optional transform "
                          "(pay {4}{U}{U}{B}{B}, exile 8), assert an extra "
                          "turn for P0 immediately follows (reported broken)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x/18x deck densities are test-harness conveniences (engine "
            "accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_6772_0980.py (verified 2026-09-30, "
            "run 20260930-6772).",
            "Merged_actions/vi conventions, NON_DECISION_CODES incl. "
            "playLand, explicit land-play before the decision gate with an "
            "is_land() helper, and PASSED_REV gating taken from the proven "
            "20261005-6770/6771 templates.",
            "The transform prompt is detected live: decideOptionalEffect "
            "exactChoices (Demonstrate-style bare true/false) or a schema "
            "decideOptionalCost surface; the raw opportunity is wired.",
            "Exile-8 answered via schema select whose candidates reference "
            ">=8 Graveyard objects (disjoint from surveil: 2-3 candidates).",
            "Mana paid via vi tapLandForMana with tracked needs; Islands "
            "preferred for generic so Swamps stay untapped for the cost.",
            "Verdict blocked if A1 fails; reproduced iff A1+A2 pass and A3 "
            "fails (transform without the extra turn).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=1)

    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
