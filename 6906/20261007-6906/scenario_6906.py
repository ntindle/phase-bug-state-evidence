#!/usr/bin/env python3
"""Issue #6906: Calix, Destiny's Hand -3 doesn't associate targets correctly.

Protocol-106 port (v0.103.0, 2026-10-07) of scenario_6906.py
(verified 2026-09-12 on v0.81.3/protocol 70, run 20260912-6906e;
verdict reproduced: A3_two_target_slots failed).

Oracle (verified from pinned v0.103.0 card-data.json):
  [-3]: Exile target creature or enchantment you don't control until target
  enchantment you control leaves the battlefield.

Reported: after choosing the exile target, Calix should pick a second target
(an enchantment its controller controls) so the exiled card returns when that
enchantment leaves. Instead, Calix just exiles straight up.

Pinned parse (v0.103.0 card-data.json) -- UNCHANGED from v0.81.3: ability
index 1 (Loyalty -3) is a single-target ChangeZone -> Exile with target
Or[Creature controller Opponent, Enchantment controller Opponent];
no second target, no duration, no return linkage, no sub_ability.
The bug premise is unchanged at the data level (see data_evidence.json).

Behavioral contract (native engine, protocol 106, two human seats):
  P0 casts 2x Glorious Anthem (own enchantments) and Calix, Destiny's Hand.
  P1 casts 2x Grizzly Bears (opponent's creatures). P0 activates Calix's -3
  (legacy ActivateAbility action, source_id=calix, ability_index=1) at
  main-phase priority.
  - The activation must offer TWO target selections: (1) the exile target
    (a creature or enchantment the opponent controls), (2) an enchantment
    P0 controls that the exile is linked to.
  - Driver answers (1) with a P1 Bear and (2) with a P0 Anthem if offered.
  - P1 then destroys the linked Anthem with Naturalize. If the link exists,
    the exiled Bear must return to P1's battlefield immediately.

  A1 setup_ok        pre-activation: Calix on P0 BF, >=2 Anthems on P0 BF,
                     >=2 Bears on P1 BF, P0 main-phase priority.
  A2 target_prompted >=1 TargetSelection recorded for the -3 activation.
  A3 two_target_slots >=2 TargetSelection prompts for the -3 activation
                     (exile target + own enchantment). The reported gap.
  A4 exile_observed  the chosen Bear is in Exile after -3 resolution.
  A5 own_enchant_targeted (only if A3 passes) the second selection chose a
                     P0 Anthem; else not-run.
  A6 return_on_destroy post-Naturalize: if A3 passed, the exiled Bear
                     returned to P1's BF when the linked Anthem left;
                     if A3 failed, the exiled Bear is still in Exile
                     (documents the missing return linkage).
  A7 cleanup         final state: stack empty, game proceeding.

Verdict: reproduced iff A1 passes and A3 fails. not-reproduced iff
A1..A7 all pass. blocked iff A1 fails.

Protocol-106 notes (from the verified scenario_7177_01030.py harness):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full: true
    + match_config {"match_type":"Bo1"} (client.py).
  - waiting_for is gone (null on 106); decisions surface via
    viewer_interaction opportunities; priority = PassPriority in
    merged legal_actions.
  - MulliganDecision answered via legacy Action, keep-always.
  - DiscardToHandSize is a Select model: answer ONLY schema/spec.type ==
    "select" opportunities with {"type":"select","data":{"choiceIds":[...]}};
    drain client.rejections per tick to re-arm after any rejection.
  - Target selection: schema select/sequence with candidates, or
    exactChoices with candidate/target codes; submit the ADVERTISED
    response type (never invent shapes).
  - Calix's -3 via legacy ActivateAbility action (source_id + ability_index
    1), advertised in merged legal_actions on 106 (cf. scenario_6643_01030).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority.
  - Re-tick backstop: re-tick a client holding priority with no revision
    change for > 5s.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6906
RUN_ID = "20261007-6906"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

CALIX = "calix, destiny's hand"
ANTHEM = "glorious anthem"
BEAR = "grizzly bears"
NAT = "naturalize"
FOREST = "forest"
PLAINS = "plains"

P0_DECK = [(CALIX, 4), (ANTHEM, 8), (FOREST, 24), (PLAINS, 24)]
P1_DECK = [(BEAR, 24), (NAT, 4), (FOREST, 32)]

TIMEOUT = 1800
PROGRESS_WATCHDOG_S = 180
SETUP_ABORT_S = 420

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
WF_SEEN = []
DISCARD_WIRED = set()
DISCARD_SUBMITTED = set()
P0C = None  # host client (P0 created the game); only the host may export


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> PROOF -> DESTROY -> DONE
        "stop": False,
        "states_seen": 0,
        "calix_oid": None,
        "casting_calix": False,
        "anthems_cast": 0,
        "bears_cast": 0,
        "minus3_submitted": False,
        "minus3_turn": None,
        "minus3_watch": None,
        "target_answers": 0,
        "ability_stack_seen": False,
        "ability_resolved": False,
        "target_sels": [],
        "target_sel_files": 0,
        "exile_target_oid": None,
        "own_enchant_oid": None,
        "nat_cast": False,
        "nat_target_oid": None,
        "anthem_destroyed": False,
        "destroy_turn": None,
        "destroy_watch": None,
        "pre": None,
        "mana_needs": {},
        "setup_t0": None,
        "game_code": None,
    })


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
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def hand_ids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def untapped_named(state, pid, name):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == name
               and not get_obj(state, o).get("tapped"))


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


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
    ws = await websockets.connect("ws://localhost:9374/ws",
                                  max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = json.loads(raw).get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return ver, build, proto


def check_data_level():
    """Confirm the v0.103.0 parse still shows Calix's -3 as a single-target
    ChangeZone -> Exile with no second target, no duration, no sub_ability
    (the data-level bug premise)."""
    ok, notes = True, []
    cal = CARD_DATA.get(CALIX, {})
    oracle = str(cal.get("oracle_text", ""))
    for ph in ("exile target creature or enchantment you don't control",
               "until target enchantment you control leaves the battlefield"):
        if ph not in oracle.lower():
            ok = False
            notes.append(f"oracle missing phrase: {ph!r}")
    abs_ = cal.get("abilities") or []
    if len(abs_) < 2:
        ok = False
        notes.append(f"expected >=2 abilities, got {len(abs_)}")
        payload = {}
    else:
        ab = abs_[1]
        eff = ab.get("effect") or {}
        tgt = eff.get("target") or {}
        filters = tgt.get("filters") or []
        ctrls = sorted({f.get("controller") for f in filters})
        types = sorted({tuple(sorted(f.get("type_filters") or []))
                        for f in filters})
        payload = {
            "cost": ab.get("cost"),
            "effect_type": eff.get("type"),
            "destination": eff.get("destination"),
            "target_type": tgt.get("type"),
            "target_controllers": ctrls,
            "target_types": [list(t) for t in types],
            "has_duration": "duration" in eff and eff.get("duration") is not None,
            "has_sub_ability": bool(eff.get("sub_ability")),
        }
        if eff.get("type") != "ChangeZone" or eff.get("destination") != "Exile":
            ok = False
            notes.append("ability 1 effect shape changed")
        if tgt.get("type") != "Or":
            ok = False
            notes.append("ability 1 target is no longer Or[...] (parse changed)")
        if eff.get("sub_ability"):
            ok = False
            notes.append("ability 1 now has a sub_ability (second target?)")
        if eff.get("duration"):
            ok = False
            notes.append("ability 1 now has a duration")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes, "calix_oracle": oracle,
                   "minus3_payload": payload}, f, indent=1)
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


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


async def do_discard(c, st, tag, pid, rank_fn):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    select_opps = [o for o in vi_ops(st) if is_select_schema_opp(o)]
    if not select_opps:
        return False
    for opp in select_opps:
        iid = opp.get("interactionId")
        if iid in DISCARD_SUBMITTED:
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        if iid not in DISCARD_WIRED:
            DISCARD_WIRED.add(iid)
            wire("discard_opportunity", {"who": tag, "opp": opp})

        def _ref(ch):
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    return d.get("reference")
            return None

        picks = [ch["id"] for ch in
                 sorted(cands,
                        key=lambda ch: rank_fn(_ref(ch)))[:max(1, n)]]
        DISCARD_SUBMITTED.add(iid)
        say(f"[{tag}] discarding {n} to hand size via select")
        wire("handsize_discard", {"who": tag, "iid": iid, "picks": picks})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


def p0_discard_rank(state):
    def rank(o):
        nm = obj_lname(state, o)
        if nm in (FOREST, PLAINS):
            return (0, nm)
        if nm == CALIX:
            return (1, nm)
        if nm == ANTHEM:
            return (2, nm)  # protect own-enchantment targets
        return (3, nm)
    return rank


def p1_discard_rank(state):
    def rank(o):
        nm = obj_lname(state, o)
        if nm == NAT:
            return (9, nm)  # protect Naturalize for the destroy leg
        return (0, nm)
    return rank


def drain_rejections(c, tag):
    if not c.rejections:
        return 0
    n = 0
    for r in c.rejections:
        n += 1
        say(f"[{tag}] {r['type']}: {json.dumps(r['data'])[:200]}")
        wire("rejection", {"who": tag, "type": r["type"], "data": r["data"]})
    c.rejections.clear()
    if DISCARD_SUBMITTED:
        wire("discard_rearmed", {"who": tag,
                                "cleared": len(DISCARD_SUBMITTED)})
        DISCARD_SUBMITTED.clear()
    return n


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
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
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
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
                   "response": {"type": stype,
                               "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid2,
                   "response": {"type": "choose",
                               "data": {"choiceId": cid}}}
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
                await interact_as(
                    c, {"interactionId": opp.get("interactionId"),
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}},
                    c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


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

# ------------------------------------------------- target selection (106)

def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices whose
    choices carry candidate/target codes (passPriority menus excluded)."""
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
                codes.update(cc for cc in surf_codes(ch) if cc)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(cc in codes for cc in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


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
    n = ST["target_sel_files"] = ST["target_sel_files"] + 1
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(json.dumps(opp, default=str))},
                  f, indent=1, default=str)
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "iid": iid,
          "candidates": [(x["name"], x["zone"], x["controller"])
                         for x in cand_info]})
    say(f"target selection #{n} (stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']})"
                    for x in cand_info[:8]))
    return True


def pick_candidate_by_name(state, opp, want_lname, want_controller=None):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        oid = cand_oid(ch)
        if oid is None:
            continue
        if obj_lname(state, oid) != want_lname:
            continue
        if want_controller is not None \
                and get_obj(state, oid).get("controller") != want_controller:
            continue
        return ch
    return None


async def answer_target(c, state, opp, rtype, spec_type, ch, tag):
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
    say(f"[{tag}] answering target: {obj_lname(state, oid) if oid else '?'} "
        f"(oid {oid}) via {sub['response']['type']}")
    wire("target_answer", {"who": tag, "iid": iid, "oid": oid,
                           "name": obj_lname(state, oid) if oid else None,
                           "submission": sub})
    await interact_as(c, sub, tag)
    return oid


def minus3_action(acts, calix_oid):
    """Legacy ActivateAbility action for Calix's -3 (ability_index 1)."""
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            src = int(d.get("source_id", d.get("object_id", -1)))
        except (TypeError, ValueError):
            continue
        if src == int(calix_oid) and d.get("ability_index") == 1:
            return a
    return None


def calix_loyalty(state, calix_oid):
    o = get_obj(state, calix_oid)
    for k in ("loyalty", "loyalty_counters"):
        v = o.get(k)
        if isinstance(v, int):
            return v
    ctrs = o.get("counters") or {}
    if isinstance(ctrs, dict):
        for k, v in ctrs.items():
            if "loyalty" in str(k).lower() and isinstance(v, int):
                return v
    return None


def stack_has_calix_minus3(state, calix_oid):
    """Detect the -3 ability on the stack via the entry's own fields
    (kind/source), never via whole-blob keyword matching."""
    for e in state.get("stack") or []:
        if not isinstance(e, dict):
            continue
        src = e.get("source_id") or e.get("source") or {}
        src_id = src.get("id") if isinstance(src, dict) else src
        try:
            src_match = int(src_id) == int(calix_oid)
        except (TypeError, ValueError):
            src_match = False
        kind = e.get("kind") or {}
        kind_s = (kind.get("type") if isinstance(kind, dict)
                  else str(kind)).lower()
        if src_match and "activ" in kind_s:
            return True
        # fallback: ability entry whose own description names the -3 effect
        desc = str((e.get("ability") or {}).get("description")
                   if isinstance(e.get("ability"), dict) else "")
        if src_match and "exile target creature or enchantment" in desc.lower():
            return True
    return False


# ------------------------------------------------------------------ ticks

async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_discard(c, st, tag, 0, p0_discard_rank(state)):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
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
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # --- Calix -3 target selections: answer before anything else ---
    if ST["stage"] == "PROOF" and ST["minus3_submitted"]:
        opp, rtype, spec_type = target_opportunity(st)
        if opp is not None and opp.get("interactionId") not in SUBMITTED_OPPS:
            record_target_sel(state, opp, "PROOF")
            SUBMITTED_OPPS.add(opp.get("interactionId"))
            n_answered = ST["target_answers"]
            if n_answered == 0:
                ch = pick_candidate_by_name(state, opp, BEAR, 1)
                want = f"{BEAR} (P1)"
            else:
                ch = pick_candidate_by_name(state, opp, ANTHEM, 0)
                want = f"{ANTHEM} (P0)"
            if ch is None:
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                cands = data.get("candidates") or data.get("choices") or []
                ch = cands[0] if cands else None
                wire("target_fallback",
                     {"want": want,
                      "note": "no named candidate; first candidate used"})
                say(f"[{tag}] no {want} candidate; falling back to first")
            if ch is not None:
                oid = await answer_target(c, state, opp, rtype,
                                          spec_type, ch, tag)
                if n_answered == 0:
                    ST["exile_target_oid"] = oid
                else:
                    ST["own_enchant_oid"] = oid
                ST["target_answers"] = n_answered + 1
            return True
        # No unanswered target prompt: fall through. Answered opportunities
        # clear from vi; never hold priority on real_decision_pending here
        # or the on-stack ability can never resolve.

    if ST["stage"] == "SETUP":
        if my_main(state, 0):
            if await play_a_land(c, state, 0, acts, tag):
                return
            anthems_bf = len(bf_by_name(state, 0, ANTHEM))
            if anthems_bf < 2 and ANTHEM in hand_lnames(state, 0):
                a, oid = cast_action_for(acts, state, ANTHEM)
                if a is not None and untapped_named(state, 0, PLAINS) >= 2:
                    say(f"[{tag}] casting Glorious Anthem (oid {oid})")
                    wire("cast_anthem", {"oid": oid})
                    ST["mana_needs"][tag] = {"W": 2, "generic": 1}
                    ST["anthems_cast"] += 1
                    await submit_as_is(c, a)
                    return
            elif CALIX in hand_lnames(state, 0) \
                    and not bf_by_name(state, 0, CALIX):
                a, oid = cast_action_for(acts, state, CALIX)
                if a is not None \
                        and untapped_named(state, 0, FOREST) >= 1 \
                        and untapped_named(state, 0, PLAINS) >= 1:
                    say(f"[{tag}] casting Calix (oid {oid})")
                    wire("cast_calix", {"oid": oid})
                    ST["casting_calix"] = True
                    ST["mana_needs"][tag] = {"G": 1, "W": 1, "generic": 2}
                    await submit_as_is(c, a)
                    return
            calix_bf = bf_by_name(state, 0, CALIX)
            if calix_bf:
                ST["calix_oid"] = calix_bf[0]
                if len(bf_by_name(state, 0, ANTHEM)) >= 2:
                    ST["stage"] = "PROOF"
                    say(f"[{tag}] Calix + 2 Anthems on BF "
                        f"(turn {state.get('turn_number')}); stage -> PROOF")
                    return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if ST["stage"] == "PROOF":
        if not ST["minus3_submitted"]:
            calix = ST.get("calix_oid")
            bears = bf_by_name(state, 1, BEAR)
            anthems = bf_by_name(state, 0, ANTHEM)
            loyal = calix_loyalty(state, calix) if calix else None
            ready = (calix and len(bears) >= 2 and len(anthems) >= 2
                     and my_main(state, 0)
                     and (loyal is None or loyal >= 3))
            if ready:
                a = minus3_action(acts, calix)
                if a is not None:
                    wire("minus3_submit", {"loyalty": loyal,
                                          "action": a})
                    say(f"[{tag}] activating Calix -3 (loyalty {loyal}, "
                        f"turn {state.get('turn_number')}); "
                        f"pre_activate exported")
                    await do_export(c, "pre_activate.json")
                    ST["pre"] = {"turn": state.get("turn_number"),
                                 "phase": state.get("phase"),
                                 "calix_loyalty": loyal,
                                 "bears": len(bears),
                                 "anthems": len(anthems)}
                    await submit_as_is(c, a)
                    ST["minus3_submitted"] = True
                    ST["minus3_turn"] = state.get("turn_number")
                    ST["minus3_watch"] = time.time()
                    ST["target_answers"] = 0
                    return True
                dkey = ("nact", state.get("turn_number"))
                if dkey not in MULLS:
                    MULLS.add(dkey)
                    wire("no_minus3_action",
                         {"turn": state.get("turn_number"), "loyalty": loyal,
                          "bears": len(bears), "anthems": len(anthems),
                          "act_types": sorted(atypes)})
                    say(f"[{tag}] PROOF ready but no -3 ActivateAbility "
                        f"advertised (types={sorted(atypes)})")
            if my_main(state, 0):
                if await play_a_land(c, state, 0, acts, tag):
                    return
        else:
            # in-flight: watch the ability resolve, then export post state.
            # NOTE: never return early here -- P0 must keep passing priority
            # or the stack entry can never resolve (both players must pass
            # in succession). Fall through to the priority-pass logic below.
            if stack_has_calix_minus3(state, ST.get("calix_oid")):
                if not ST["ability_stack_seen"]:
                    wire("minus3_on_stack",
                         {"turn": state.get("turn_number")})
                    say(f"[{tag}] -3 ability on stack "
                        f"(turn {state.get('turn_number')})")
                ST["ability_stack_seen"] = True
            elif ST["ability_stack_seen"] and not ST["ability_resolved"]:
                ST["ability_resolved"] = True
                await do_export(c, "post_minus3.json")
                ST["stage"] = "DESTROY"
                say(f"[{tag}] -3 resolved; post_minus3 exported; "
                    f"stage -> DESTROY")
                return True
            elif not ST["ability_stack_seen"] \
                    and (state.get("turn_number") or 0) > \
                    (ST.get("minus3_turn") or 0):
                ST["ability_resolved"] = True
                await do_export(c, "post_minus3.json")
                ST["stage"] = "DESTROY"
                say(f"[{tag}] activation turn passed without stack sighting; "
                    f"post_minus3 exported; stage -> DESTROY")
                return True
            elif time.time() - (ST.get("minus3_watch") or time.time()) > 120:
                wire("minus3_stall",
                     {"target_sels": len(ST["target_sels"])})
                await do_export(c, "mid_stall.json")
                ST["ability_resolved"] = True
                ST["stage"] = "DESTROY"
                say(f"[{tag}] -3 resolution watch timed out; mid_stall "
                    f"exported; stage -> DESTROY")
                return True
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if ST["stage"] == "DESTROY":
        if my_main(state, 0):
            if await play_a_land(c, state, 0, acts, tag):
                return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return


async def p1_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_discard(c, st, tag, 1, p1_discard_rank(state)):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
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
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # Naturalize's own target prompt (DESTROY stage): pick P0's Anthem
    if ST["stage"] == "DESTROY" and ST["nat_cast"]:
        opp, rtype, spec_type = target_opportunity(st)
        if opp is not None:
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                record_target_sel(state, opp, "DESTROY")
                SUBMITTED_OPPS.add(iid)
                ch = pick_candidate_by_name(state, opp, ANTHEM, 0)
                if ch is None:
                    resp = opp.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    cands = data.get("candidates") or data.get("choices") or []
                    ch = cands[0] if cands else None
                    say(f"[{tag}] no Anthem candidate for Naturalize; "
                        f"first-candidate fallback")
                if ch is not None:
                    oid = await answer_target(c, state, opp, rtype,
                                              spec_type, ch,
                                              f"{tag}/naturalize")
                    ST["nat_target_oid"] = oid
                return True
        if real_decision_pending(st):
            return

    if ST["stage"] in ("SETUP", "PROOF"):
        if my_main(state, 1):
            if await play_a_land(c, state, 1, acts, tag):
                return
            if len(bf_by_name(state, 1, BEAR)) < 2 \
                    and BEAR in hand_lnames(state, 1):
                a, oid = cast_action_for(acts, state, BEAR)
                if a is not None and untapped_named(state, 1, FOREST) >= 2:
                    say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                    wire("cast_bear", {"oid": oid})
                    ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                    ST["bears_cast"] += 1
                    await submit_as_is(c, a)
                    return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if ST["stage"] == "DESTROY":
        anthems = bf_by_name(state, 0, ANTHEM)
        nat_oid = next((o for o in hand_ids(state, 1)
                        if obj_lname(state, o) == NAT), None)
        if not ST["nat_cast"] and anthems and nat_oid and my_main(state, 1):
            a, _ = cast_action_for(acts, state, NAT)
            if a is not None and untapped_named(state, 1, FOREST) >= 2:
                say(f"[{tag}] casting Naturalize (oid {nat_oid})")
                wire("cast_naturalize", {"oid": nat_oid})
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                ST["nat_cast"] = True
                await submit_as_is(c, a)
                return
        if ST["nat_target_oid"] and not ST["anthem_destroyed"]:
            z = zone_of(state, ST["nat_target_oid"])
            if z != "Battlefield":
                ST["anthem_destroyed"] = True
                ST["destroy_turn"] = state.get("turn_number")
                ST["destroy_watch"] = time.time()
                await do_export(P0C, "mid_destroyed.json")
                say(f"[{tag}] targeted Anthem left battlefield (zone={z}); "
                    f"mid_destroyed exported; watching for return")
                return True
            # Anthem still on BF: Naturalize is on the stack awaiting
            # priority passes. NEVER return early here -- fall through to
            # the priority-pass logic below or the spell can never resolve.
        elif ST["anthem_destroyed"] and not ST["stop"]:
            turn = state.get("turn_number") or 0
            if turn > (ST.get("destroy_turn") or 0) or \
                    time.time() - (ST.get("destroy_watch")
                                   or time.time()) > 60:
                await do_export(P0C, "post_destroy.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
                say(f"[{tag}] destroy leg settled; post_destroy exported; DONE")
                return True
            # settle not reached: fall through to priority passes so the
            # turn can advance; never hold priority while waiting.
        if my_main(state, 1):
            if await play_a_land(c, state, 1, acts, tag):
                return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

# ------------------------------------------------------------ measurement

async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    wire(f"{path}_exported", {})
    return json.loads(s)["state"]


def env_state(p):
    try:
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)["state"]
    except FileNotFoundError:
        return None


def evaluate(notes):
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_target_prompted", "A3_two_target_slots",
            "A4_exile_observed", "A5_own_enchant_targeted",
            "A6_return_on_destroy", "A7_cleanup")}
    pre = env_state("pre_activate.json")
    postm = env_state("post_minus3.json")
    midd = env_state("mid_destroyed.json")
    postd = env_state("post_destroy.json")
    tsel_recs = ST.get("target_sels") or []
    tsel_proof = [r for r in tsel_recs if r.get("stage") == "PROOF"]

    # A1
    if pre:
        calix_ok = len([o for o in bf_oids(pre, 0)
                        if obj_lname(pre, o) == CALIX]) >= 1
        anthem_ok = len([o for o in bf_oids(pre, 0)
                         if obj_lname(pre, o) == ANTHEM]) >= 2
        bear_ok = len([o for o in bf_oids(pre, 1)
                       if obj_lname(pre, o) == BEAR]) >= 2
        phase_ok = (pre.get("phase") or "") in ("PreCombatMain",
                                                "PostCombatMain") \
            and pre.get("active_player") == 0
        ok = calix_ok and anthem_ok and bear_ok and phase_ok
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A1: calix_bf={calix_ok} anthems_bf={anthem_ok} "
                     f"bears_p1_bf={bear_ok} p0_main={phase_ok} "
                     f"turn={pre.get('turn_number')} pre={ST.get('pre')}")
    else:
        notes.append("A1: pre_activate.json missing (-3 never activated)")

    # A2
    if ST.get("minus3_submitted"):
        n = len(tsel_proof)
        ass["A2_target_prompted"] = "passed" if n >= 1 else "failed"
        notes.append(f"A2: {n} TargetSelection prompt(s) for the -3 "
                     f"activation ({len(tsel_recs)} total recorded)")
    else:
        notes.append("A2: -3 activation never submitted")

    # A3: the reported gap -- two target slots
    if ass.get("A2_target_prompted") == "passed":
        n = len(tsel_proof)
        ass["A3_two_target_slots"] = "passed" if n >= 2 else "failed"
        slot_desc = []
        for r in tsel_proof:
            cands = [(x["name"], x["zone"], x["controller"])
                     for x in r["candidates"]]
            slot_desc.append(cands)
        notes.append(f"A3: {n} -3 target slot(s) offered (expect >=2: exile "
                     f"target + own enchantment); candidates={slot_desc}")
    else:
        notes.append("A3: A2 not passed")

    # A4
    if postm and ST.get("exile_target_oid"):
        z = zone_of(postm, ST["exile_target_oid"])
        ok = z == "Exile"
        ass["A4_exile_observed"] = "passed" if ok else "failed"
        notes.append(f"A4: target Bear oid {ST['exile_target_oid']} "
                     f"zone={z} (expect Exile)")
    elif postm and ass.get("A2_target_prompted") == "passed":
        ex = sorted(str(oid) for oid, o in (postm.get("objects") or {}).items()
                    if o.get("zone") == "Exile")
        ass["A4_exile_observed"] = "failed"
        notes.append(f"A4: no tracked target oid; exiled oids: {ex}")
    else:
        notes.append("A4: post_minus3.json missing")

    # A5
    if ass.get("A3_two_target_slots") == "passed":
        second = tsel_proof[1] if len(tsel_proof) > 1 else None
        anthem_offered = second and any(
            x["name"] == ANTHEM and x["controller"] == 0
            for x in second["candidates"])
        ok = ST.get("own_enchant_oid") is not None and (
            second is None or True)
        # pass iff we actually answered the second slot with a P0 Anthem
        answered_anthem = False
        if ST.get("own_enchant_oid"):
            oid = ST["own_enchant_oid"]
            answered_anthem = (obj_lname(postm or {}, oid) == ANTHEM
                               if postm else False)
        ass["A5_own_enchant_targeted"] = \
            "passed" if answered_anthem else "failed"
        notes.append(f"A5: second slot answered with P0 Anthem="
                     f"{answered_anthem}; Anthem offered in slot 2="
                     f"{bool(anthem_offered)}")
    else:
        notes.append("A5: no second target slot (A3 not passed)")

    # A6: return-on-destroy (or its absence)
    if postd and ST.get("anthem_destroyed"):
        bear_oid = ST.get("exile_target_oid")
        z = zone_of(postd, bear_oid) if bear_oid else None
        bears_bf_p1 = [str(o) for o in bf_oids(postd, 1)
                       if obj_lname(postd, o) == BEAR]
        if ass.get("A3_two_target_slots") == "passed":
            ok = bear_oid is not None and str(bear_oid) in bears_bf_p1
            ass["A6_return_on_destroy"] = "passed" if ok else "failed"
            notes.append(f"A6: linked Anthem destroyed; target Bear "
                         f"zone={z}; on P1 BF={str(bear_oid) in bears_bf_p1} "
                         f"(expect return)")
        else:
            # no link existed: the exiled Bear must stay in Exile,
            # documenting the unconditional-exile finding
            ok = (bear_oid is not None and z == "Exile"
                  and str(bear_oid) not in bears_bf_p1)
            ass["A6_return_on_destroy"] = "passed" if ok else "failed"
            notes.append(f"A6: no link slot offered; Anthem destroyed; "
                         f"target Bear zone={z} (expect Exile: documents "
                         f"unconditional exile, no return linkage)")
    else:
        notes.append("A6: destroy leg incomplete "
                     f"(anthem_destroyed={ST.get('anthem_destroyed')}, "
                     f"post_destroy={'yes' if postd else 'no'})")

    # A7
    final = postd or postm
    if final:
        ok = not (final.get("stack") or [])
        ass["A7_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A7: stack empty={ok}; "
                     f"turn={final.get('turn_number')}")
    else:
        notes.append("A7: no post states")

    if ass.get("A1_setup_ok") == "passed" \
            and ass.get("A3_two_target_slots") == "failed":
        verdict = "reproduced"
    elif all(ass.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_target_prompted", "A3_two_target_slots",
              "A4_exile_observed", "A5_own_enchant_targeted",
              "A6_return_on_destroy", "A7_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return ass, verdict


# ------------------------------------------------------------------- main

async def main():
    reset_state()
    t0 = time.time()
    ST["setup_t0"] = t0
    notes = []

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
        notes.append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions":
                       {k: "not-run" for k in
                        ("A1_setup_ok", "A2_target_prompted",
                         "A3_two_target_slots", "A4_exile_observed",
                         "A5_own_enchant_targeted", "A6_return_on_destroy",
                         "A7_cleanup")},
                       "notes": notes}, f, indent=2)
        await p0.close()
        return
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    global P0C
    P0C = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1.player_id})

    last = {}
    last_tick_at = {}
    progress_rev = -1
    progress_at = t0
    last_diag = 0.0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
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
            drain_rejections(c, tag)

        st = p0.latest
        if not st:
            continue
        state = st["state"]
        ST["states_seen"] += 1

        wf = (state.get("waiting_for") or {}).get("type")
        if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
            WF_SEEN.append(wf)
            wire("waiting_for",
                 {"type": wf,
                  "data": (state.get("waiting_for") or {}).get("data")})

        # generic progress watchdog: no revision advance at all
        if p0.revision != progress_rev or p1.revision != progress_rev:
            progress_rev = max(p0.revision, p1.revision)
            progress_at = time.time()
        if time.time() - progress_at > PROGRESS_WATCHDOG_S:
            await do_export(p0, "mid_stall.json")
            notes.append("generic stall watchdog: no revision advance "
                         f">{PROGRESS_WATCHDOG_S}s")
            say("[stall] generic watchdog fired")
            ST["stop"] = True
            continue

        # SETUP abort: Calix never landed
        if ST["stage"] == "SETUP" and time.time() - t0 > SETUP_ABORT_S:
            await do_export(p0, "mid_setup.json")
            notes.append(f"SETUP abort: Calix not on BF after "
                         f"{SETUP_ABORT_S}s (turn "
                         f"{state.get('turn_number')}, hand="
                         f"{hand_lnames(state, 0)[:8]})")
            say("[setup-abort] Calix never landed; ending run")
            ST["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={state.get('turn_number')} "
                f"active={state.get('active_player')} phase={state.get('phase')} "
                f"wf={wf} stage={ST['stage']} "
                f"calix={ST['calix_oid']} minus3={ST['minus3_submitted']} "
                f"tsels={len(ST['target_sels'])} "
                f"nat={ST['nat_cast']} destroyed={ST['anthem_destroyed']} "
                f"P0hand={hand_lnames(state, 0)[:6]}")

    if not env_state("post_destroy.json") and not env_state("post_minus3.json"):
        try:
            await do_export(p0, "post.json")
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")

    ass, verdict = evaluate(notes)
    for k in sorted(ass):
        say(f"{k}: {ass[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN,
                   "target_sels": [
                       {k: r[k] for k in ("interactionId", "turn", "phase",
                                         "stage", "candidates")
                        if k in r} for r in ST["target_sels"]],
                   "pre": ST.get("pre"),
                   "exile_target_oid": ST.get("exile_target_oid"),
                   "own_enchant_oid": ST.get("own_enchant_oid"),
                   "nat_target_oid": ST.get("nat_target_oid"),
                   "data_level_ok": data_ok}, f, indent=2)

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_6906.py", "w") as f:
        f.write(scenario_src.decode())

    run_meta = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00",
                                    time.localtime(t0)),
        "server": {
            "server_version": "v0.103.0",
            "build_commit": "ec27a8d",
            "protocol_version": 106,
            "binary_sha256": sha(f"{BACKFILL}/server/releases/v0.103.0/"
                                  "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(f"{BACKFILL}/server/releases/v0.103.0/data/"
                                     "card-data.json"),
            "draft_pools_sha256": sha(f"{BACKFILL}/server/releases/v0.103.0/data/"
                                       "draft-pools.json"),
            "port": 9374,
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"]},
        "setup_line": ("P0: 4x Calix, Destiny's Hand + 8x Glorious Anthem + "
                       "48 lands; P1: 24x Grizzly Bears + 4x Naturalize + "
                       "32x Forest; native human seats"),
        "contract_line": ("P0 casts 2 Anthems + Calix, activates the -3 "
                          "(ability_index 1) at main-phase priority; assert "
                          "TWO target selections are offered (exile target + "
                          "own enchantment); answer with a P1 Bear and a P0 "
                          "Anthem; P1 destroys the linked Anthem with "
                          "Naturalize and asserts the exiled Bear returns"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x Anthem / 24x Bear densities are test-harness conveniences "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_6906.py (verified 2026-09-12, "
            "run 20260912-6906e).",
            "Merged_actions/vi conventions, NON_DECISION_CODES, vi "
            "tapLandForMana payment, PASSED_REV gating, Select-model "
            "hand-size discards, and progress watchdogs taken from the "
            "proven scenario_7177_01030.py template.",
            "Target selections detected via schema select/sequence "
            "candidates or exactChoices candidate/target codes; submitted "
            "with the advertised response type; raw opportunities wired "
            "and saved as target_sel_N.json.",
            "Pinned v0.103.0 data still parses the -3 as a single-target "
            "ChangeZone -> Exile with no duration and no sub_ability -- "
            "see data_evidence.json.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=1)

    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
