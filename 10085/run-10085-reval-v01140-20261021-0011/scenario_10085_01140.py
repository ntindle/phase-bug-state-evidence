#!/usr/bin/env python3
"""Issue #10085: Bestow form leaks into intrinsic copiable values when
copying a bestowed enchantment.

Oracle (verified from pinned v0.114.0 card-data.json):
  Springheart Nantuko: Bestow {1}{G}; Enchantment Creature -- Insect Monk.
    "Enchanted creature gets +1/+1."
    "Landfall -- Whenever a land you control enters, you may pay {1}{G}
     if this permanent is attached to a creature you control. ..."
  Copy Enchantment: "You may have this enchantment enter as a copy of any
    enchantment on the battlefield."

Reported (issue body, 2026-10-18): copying a bestowed permanent copies its
temporary Aura form into the new permanent's intrinsic characteristics.
Under CR 707.2, ordinary type-changing effects are not copiable; CR
702.103b's bestowed form is such an effect. The copied permanent should
have Springheart's intrinsic Enchantment Creature / Insect Monk
characteristics and printed abilities, without the bestowed-only Aura
subtype or granted enchant-creature keyword. Runtime reproduction was
UNRUN in the report -- this scenario runs it for the first time.

v0.114.0 re-validation port (protocol 153, 2026-10-21) of the verified
  v0.113.0 run (driver/scenario_10085_01130.py, run
  run-10085-v01130-20261019-1345, verdict reproduced). Protocol-151/153
  notes: conventions proven identical (7176 01140 run).

Behavioral contract (native engine, protocol 153, two human seats):
  P0 casts Springheart Nantuko for its bestow cost {1}{G} targeting Llanowar
  Elves; it enters as an Aura enchanting the Elves. P1 then casts Copy
  Enchantment {1}{U} and has it enter as a copy of that Aura.

  A1 setup_ok      pre: P0 BF has the Nantuko as an Aura (Aura subtype live)
                     enchanting the Elves; P1 main-phase priority with Copy
                     Enchantment castable.
  A2 copy_entered  post: P1 BF has a permanent that is a copy of Springheart
                     Nantuko (name taken).
  A3 types_correct post: the copy's live types are Enchantment + Creature,
                     subtypes include Insect/Monk, and Aura is NOT among its
                     subtypes (the reported outcome).
  A4 bug_document  (A3 failed) records the copy's actual live types.
  A5 cleanup       post: stack empty, game proceeding.

Verdict: reproduced iff A1+A2 pass and A3 fails (Aura leak documented).
not-reproduced iff A1, A2, A3 all pass. blocked iff A1 fails (bestow or
Copy Enchantment unsupported, or the setup never materializes).

Conventions (from scenario_6907_01130.py, protocol 151/153):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - merged_actions/vi, NON_DECISION_CODES, vi tapLandForMana payment,
    PASSED_REV gating, Select-model discards, schema-select BottomCards,
    stack-watch fall-through (never hold priority while watching the stack),
    host-only exports (P0C).
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
ISSUE = 10085
RUN_ID = "run-10085-reval-v01140-20261021-0011"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.114.0/data/card-data.json"))

NANTUKO = "springheart nantuko"
ELVES = "llanowar elves"
COPYE = "copy enchantment"
FOREST = "forest"
ISLAND = "island"

P0_DECK = [(NANTUKO, 4), (ELVES, 4), (FOREST, 52)]
P1_DECK = [(COPYE, 4), (ISLAND, 56)]

TIMEOUT = 1800
PROGRESS_WATCHDOG_S = 180
SETUP_ABORT_S = 600

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
WF_SEEN = []
DISCARD_WIRED = set()
DISCARD_SUBMITTED = set()
SEEN_OPP_BLOBS = set()
P0C = None  # host client (P0 created the game); only the host may export


def init_st():
    ST.update({
        "state": None,
        "stop": False,
        "stage": "SETUP",
        "mulligans": 0,
        "elves_cast": False,
        "nantuko_cast": False,      # CastSpell submitted (bestow mode)
        "bestow_chosen": False,
        "nantuko_bestowed": False,  # aura on BF enchanting Elves
        "nantuko_oid": None,
        "elves_oid": None,
        "copy_cast": False,
        "copy_choice_done": False,
        "copy_entered": False,
        "pre_exported": False,
        "post_exported": False,
        "pending_cast": None,       # "nantuko" | "copy"
        "pending_target_for": None, # "nantuko" | "copy"
        "cast_guard": {},           # tag -> {"oid":..,"name":..,"until":..}
        "target_sels": [],
        "target_sel_files": 0,
        "decisions": [],
        "decision_files": 0,
        "mana_needs": {},
        "states_seen": 0,
        "game_code": None,
        "t0": time.time(),
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


def untapped_lands(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def live_types(o):
    ct = o.get("card_types") or {}
    return (set(ct.get("core_types") or []), set(ct.get("subtypes") or []))


def type_summary(o):
    core, subs = live_types(o)
    return f"core={sorted(core)} sub={sorted(subs)}"


def attached_to_of(o):
    for k in ("attached_to", "attachedTo", "attaching", "attached"):
        if o.get(k) is not None:
            return k, o.get(k)
    for k, v in o.items():
        if "attach" in str(k).lower() and v:
            return k, v
    return None, None

# ------------------------------------------------------- interaction prims

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
    assert str(ver).startswith("0.114.0"), f"unexpected version {ver}"
    assert int(proto) == 153, f"unexpected protocol {proto}"
    assert str(build) == "ac67cbb", f"unexpected build {build}"
    return ver, build, proto


def check_data_level():
    """Confirm the v0.113.0 parse still carries the data-level premises of
    the bug: Springheart Nantuko is Enchantment Creature -- Insect Monk with
    Bestow {1}{G}; Copy Enchantment has the enter-as-copy oracle text."""
    ok, notes = True, []
    sn = CARD_DATA.get("springheart nantuko", {})
    ct = sn.get("card_type") or {}
    if set(ct.get("core_types") or []) != {"Enchantment", "Creature"}:
        ok = False
        notes.append(f"nantuko core_types changed: {ct.get('core_types')}")
    if set(ct.get("subtypes") or []) != {"Insect", "Monk"}:
        ok = False
        notes.append(f"nantuko subtypes changed: {ct.get('subtypes')}")
    kw = sn.get("keywords") or []
    if not any("Bestow" in k for k in kw):
        ok = False
        notes.append("nantuko Bestow keyword missing")
    ce = CARD_DATA.get("copy enchantment", {})
    oracle = str(ce.get("oracle_text") or "")
    if "enter as a copy of any enchantment" not in oracle:
        ok = False
        notes.append("copy enchantment oracle text changed")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "nantuko_card_type": ct,
                   "nantuko_keywords": kw,
                   "copy_enchantment_oracle": oracle}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok


# ------------------------------------------------- mulligan / bottom / discard

def mulligan_pending_for(state, pid):
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "Declare":
            return True
    return False


def bottom_pending_for(state, pid):
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "BottomCards":
            return int(ph.get("count", 1) or 1)
    return 0


def p0_wants(hn):
    return (ELVES in hn and NANTUKO in hn and FOREST in hn)


def p1_wants(hn):
    return (COPYE in hn and hn.count(ISLAND) >= 2)


async def do_mulligan(c, acts, st, pid, tag):
    state = st["state"]
    if not mulligan_pending_for(state, pid):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return True
    adv = next((a for a in acts if a.get("type") == "MulliganDecision"),
               None)
    if adv is None:
        return False
    MULLS.add(key)
    hn = hand_lnames(state, pid)
    want = p0_wants(hn) if pid == 0 else p1_wants(hn)
    if not want and ST["mulligans"] < 3:
        ST["mulligans"] += 1
        say(f"[{tag}] mulligans ({ST['mulligans']}) (hand={hn[:7]})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    else:
        say(f"[{tag}] keeps (hand={len(hn)}: {hn[:7]})")
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    target = None
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
        target = (opp, cands, spec)
        break
    if target is None:
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                oid = d.get("reference")
                break
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in (NANTUKO, COPYE):
            return (2, str(oid))
        if nm == ELVES:
            return (1, str(oid))
        return (0, str(oid))  # lands first

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {n}")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds":
                                                [x.get("id")
                                                 for x in picks]}}}, tag)
    return True


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


async def do_discard(c, st, tag, pid):
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
            wire("discard_opportunity", {"who": tag,
                                         "opp": json.loads(json.dumps(opp, default=str))})

        def _ref(ch):
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    return d.get("reference")
            return None

        def rank(oid):
            nm = obj_lname(state, oid) if oid else "?"
            if nm in (NANTUKO, COPYE):
                return (2, nm)
            if nm == ELVES:
                return (1, nm)
            return (0, nm)  # lands first

        ranked = sorted(cands, key=lambda ch: rank(_ref(ch)))
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if len(picks) < n:
            continue
        DISCARD_SUBMITTED.add(iid)
        say(f"[{tag}] discarding {n} to hand size")
        wire("discard_submit", {"who": tag, "iid": iid, "n": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False

# ------------------------------------------------------------------ payment

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


async def play_a_land(c, state, pid, acts, tag, prefer=None):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    cands = []
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            cands.append(o)
    if prefer:
        cands.sort(key=lambda o: (prefer.index(obj_lname(state, o))
                                  if obj_lname(state, o) in prefer else 99))
    for o in cands:
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


def arm_cast_guard(tag, oid, name):
    ST["cast_guard"][tag] = {"oid": oid, "name": name,
                             "until": time.time() + 30}


def cast_hold(tag, state, pid):
    """True while this seat must hold: its CastSpell is in flight and the
    object has not left the hand yet (or the 30s backstop has not fired)."""
    g = ST["cast_guard"].get(tag)
    if not g:
        return False
    if time.time() > g["until"]:
        ST["cast_guard"].pop(tag, None)
        return False
    if g["oid"] not in hand_ids(state, pid):
        ST["cast_guard"].pop(tag, None)
        return False
    return True


# ------------------------------------------------- target selection

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


def pick_candidate_by_oid(state, opp, want_oid):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        if cand_oid(ch) == str(want_oid):
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
    await interact_as(c, sub, tag)
    return cand_oid(ch)


def record_decision(state, opp, stage, note):
    iid = opp.get("interactionId")
    blob = json.dumps(opp, default=str)
    if blob in SEEN_OPP_BLOBS:
        return
    SEEN_OPP_BLOBS.add(blob)
    n = ST["decision_files"] = ST["decision_files"] + 1
    with open(f"{EVDIR}/decision_{n}.json", "w") as f:
        json.dump({"stage": stage, "note": note, "turn": state.get("turn_number"),
                   "phase": state.get("phase"),
                   "opportunity": json.loads(blob)}, f, indent=1)
    ST["decisions"].append({"stage": stage, "note": note, "iid": iid})
    wire("decision_seen", {"n": n, "stage": stage, "note": note, "iid": iid})
    say(f"[{stage}] decision #{n}: {note}")

# ------------------------------------------------- decisions: bestow / copy

async def answer_choice(c, opp, ch, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = ch.get("id") or ch.get("choiceId")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)


def is_target_like(opp):
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    if rtype == "schema":
        spec = data.get("spec", {}) or {}
        stype = spec.get("type")
        return stype in ("select", "sequence") and bool(data.get("candidates"))
    if rtype == "exactChoices":
        chs = data.get("choices") or []
        codes = set()
        for ch in chs:
            codes.update(cc for cc in surf_codes(ch) if cc)
        return bool(chs) and "passPriority" not in codes \
            and "decideOptionalEffect" not in codes \
            and any(cc in codes for cc in ("candidate", "target"))
    return False


def cast_actions_for(acts, state, key):
    out = []
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
                    out.append((a, iv))
                    break
    return out


async def handle_decisions(c, st, tag, pid):
    """Answer scenario decisions. Returns True if an answer was submitted."""
    state = st["state"]
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        blob = json.dumps(opp, default=str).lower()

        # --- alternative cast choice (e.g. bestow): exactChoices whose
        # choices carry a chooseAlternativeCast action surface plus a value
        # surface with role=castCost, value=normal|alternative. The blob
        # carries NO "bestow" text -- match on the action code instead.
        # (observed 2026-10-19: choice ids like <game>.0.646.c0/c1.)
        if pid == 0 and ST["pending_cast"] == "nantuko" \
                and not ST["bestow_chosen"]:
            is_alt_cast_opp = False
            alt_pick, normal_pick = None, None
            for ch in items:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if not isinstance(d, dict):
                        continue
                    if d.get("code") == "chooseAlternativeCast":
                        is_alt_cast_opp = True
                    if s.get("type") == "value" \
                            and d.get("role") == "castCost":
                        if d.get("value") == "alternative":
                            alt_pick = ch
                        elif d.get("value") == "normal":
                            normal_pick = ch
            if is_alt_cast_opp:
                record_decision(state, opp, ST["stage"],
                                "alternative_cast_choice")
                pick = alt_pick or normal_pick or items[0]
                ST["bestow_chosen"] = True
                ST["pending_target_for"] = "nantuko"
                say(f"[{tag}] choosing alternative cast cost "
                    f"(alt_pick={pick is alt_pick})")
                wire("alternative_cast_pick",
                     {"alt": pick is alt_pick,
                      "choice_id": pick.get("id")})
                await answer_choice(c, opp, pick, tag)
                return True

        # --- optional "you may have it enter as a copy" (P1, Copy in flight)
        codes = set()
        for ch in items:
            codes.update(cc for cc in surf_codes(ch) if cc)
        if pid == 1 and "decideoptionaleffect" in codes \
                and "copy" in blob and not ST["copy_choice_done"]:
            record_decision(state, opp, ST["stage"],
                            "copy_enter_optional_effect")
            # choose the accept/yes branch
            pick = None
            for ch in items:
                t = (choice_text(ch) + " " +
                     json.dumps(ch, default=str)).lower()
                if any(w in t for w in ("yes", "accept", "enter as a copy",
                                        "have it enter")):
                    pick = ch
                    break
            if pick is None:
                pick = items[0]
                say(f"[{tag}] copy optional: fallback to first choice")
            say(f"[{tag}] accepting copy-enter optional effect: "
                f"{choice_text(pick)[:80]}")
            ST["pending_target_for"] = "copy"
            await answer_choice(c, opp, pick, tag)
            return True

        # --- landfall "you may pay" optional trigger (P0): decline it; it is
        # not part of this scenario and must not stall the game
        if pid == 0 and "decideoptionaleffect" in codes \
                and "landfall" in blob:
            record_decision(state, opp, ST["stage"], "landfall_declined")
            pick = None
            for ch in items:
                t = (choice_text(ch) + " " +
                     json.dumps(ch, default=str)).lower()
                if any(w in t for w in ("decline", "no ", "no,", "not pay",
                                        "don't", "do not")):
                    pick = ch
                    break
            if pick is None and len(items) >= 2:
                pick = items[-1]
            elif pick is None:
                pick = items[0]
            say(f"[{tag}] declining landfall optional trigger")
            await answer_choice(c, opp, pick, tag)
            return True

        # --- Copy Enchantment: chooseReplacement option branch (P1).
        # The replacement choice offers bare optionIndex values (no card
        # refs); the state's waiting_for ReplacementChoice data carries the
        # ordered descriptions, e.g. [0]="You may have ~ enter as a copy of
        # any enchantment on the battlefield." [1]="Decline". Map the copy
        # branch index -> the choice with that optionIndex value. The
        # follow-up "which enchantment" selection is answered by the
        # target-like branch below (pending_target_for stays "copy").
        if pid == 1 and ST["pending_target_for"] == "copy":
            wf = state.get("waiting_for") or {}
            wfd = wf.get("data") or {}
            descs = [str(c.get("description", ""))
                     for c in wfd.get("candidates") or []]
            copy_idx = None
            for i, desc in enumerate(descs):
                dl = desc.lower()
                if "decline" in dl:
                    continue
                if "copy" in dl:
                    copy_idx = i
                    break
            if copy_idx is not None:
                for ch in items:
                    surfs = ch.get("surfaces", []) or []
                    if not any(isinstance(s.get("data"), dict)
                               and s.get("data", {}).get("code")
                               == "chooseReplacement"
                               for s in surfs):
                        continue
                    val = None
                    for s in surfs:
                        d = s.get("data") or {}
                        if s.get("type") == "value" \
                                and isinstance(d, dict) \
                                and d.get("role") == "optionIndex":
                            val = str(d.get("value"))
                    if val == str(copy_idx):
                        record_decision(state, opp, ST["stage"],
                                        "copy_replacement_branch")
                        say(f"[{tag}] choosing copy branch "
                            f"(optionIndex {copy_idx})")
                        wire("copy_branch_pick",
                             {"option_index": copy_idx,
                              "choice_id": ch.get("id"),
                              "descriptions": descs})
                        await answer_choice(c, opp, ch, tag)
                        return True

        # --- target / copy-target selection
        if is_target_like(opp) and ST["pending_target_for"] in ("nantuko",
                                                                 "copy"):
            want = None
            if ST["pending_target_for"] == "nantuko" and pid == 0:
                elves = bf_by_name(state, 0, ELVES)
                want = str(elves[0]) if elves else None
                note = "nantuko_aura_target"
            elif ST["pending_target_for"] == "copy" and pid == 1:
                want = str(ST["nantuko_oid"]) if ST["nantuko_oid"] else None
                note = "copy_enchantment_choice"
            else:
                continue
            ch = pick_candidate_by_oid(state, opp, want) if want else None
            if ch is None:
                # log candidates once, then wait (don't answer blind)
                record_decision(state, opp, ST["stage"],
                                f"{note}_want_missing_want={want}")
                continue
            record_decision(state, opp, ST["stage"], note)
            rtype = resp.get("type")
            spec = (data.get("spec") or {}).get("type") or "select"
            say(f"[{tag}] {note}: answering "
                f"{obj_lname(state, cand_oid(ch))} (oid {cand_oid(ch)})")
            SUBMITTED_OPPS.add(iid)
            await answer_target(c, state, opp, rtype, spec, ch, tag)
            if ST["pending_target_for"] == "copy":
                ST["pending_target_for"] = None
                ST["copy_choice_done"] = True
            elif ST["pending_target_for"] == "nantuko":
                ST["pending_target_for"] = None
            return True
    return False


# ------------------------------------------------------------------ exports

async def do_export(c, name):
    env_str = await c.export_state()
    env = json.loads(env_str)
    path = f"{EVDIR}/{name}"
    with open(path, "w") as f:
        json.dump(env, f, indent=1)
    say(f"exported {name} ({len(env_str)} chars)")
    wire("export", {"file": name, "bytes": len(env_str)})
    return env


def env_state(name):
    p = f"{EVDIR}/{name}"
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)["state"]

# ------------------------------------------------------------------ ticks

def refresh_board_flags(state):
    elves = bf_by_name(state, 0, ELVES)
    if elves and not ST["elves_oid"]:
        ST["elves_oid"] = elves[0]
        ST["elves_cast"] = True
        say(f"board: Llanowar Elves on P0 BF (oid {elves[0]})")
    nk = bf_by_name(state, 0, NANTUKO)
    for o in nk:
        core, subs = live_types(get_obj(state, o))
        if "Aura" in subs and not ST["nantuko_bestowed"]:
            ST["nantuko_oid"] = o
            ST["nantuko_bestowed"] = True
            k, v = attached_to_of(get_obj(state, o))
            say(f"board: Nantuko bestowed as Aura (oid {o}), "
                f"{type_summary(get_obj(state, o))}, attach {k}={v}")
            wire("nantuko_bestowed", {"oid": o,
                                      "types": type_summary(get_obj(state, o)),
                                      "attach": [k, str(v)]})
        elif "Aura" not in subs and ST["nantuko_cast"] \
                and not ST["nantuko_bestowed"]:
            # cast resolved as a plain creature: bestow path never engaged
            say(f"board: Nantuko on BF WITHOUT Aura subtype "
                f"({type_summary(get_obj(state, o))}) -- bestow not engaged")
            wire("nantuko_plain_creature", {"oid": o,
                                            "types": type_summary(get_obj(state, o))})


async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_bottom(c, acts, st, 0, tag):
        return
    if await do_discard(c, st, tag, 0):
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
    if await handle_decisions(c, st, tag, 0):
        return
    if cast_hold(tag, state, 0):
        return
    refresh_board_flags(state)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag, prefer=[FOREST]):
            return
        hn = hand_lnames(state, 0)
        # T1: Llanowar Elves
        if not ST["elves_cast"] and ELVES in hn:
            cands = cast_actions_for(acts, state, ELVES)
            if cands and untapped_named(state, 0, FOREST) >= 1:
                a, oid = cands[0]
                say(f"[{tag}] casting Llanowar Elves (oid {oid})")
                wire("cast_elves", {"oid": oid})
                ST["mana_needs"][tag] = {"G": 1}
                await submit_as_is(c, a)
                arm_cast_guard(tag, oid, "Llanowar Elves")
                return
        # T2: Springheart Nantuko, bestowed
        if ST["elves_cast"] and not ST["nantuko_cast"] and NANTUKO in hn:
            cands = cast_actions_for(acts, state, NANTUKO)
            if cands and untapped_lands(state, 0) >= 2:
                # prefer a bestow-flavored cast action when several exist
                pick = cands[0]
                for a, oid in cands:
                    if "bestow" in json.dumps(a, default=str).lower():
                        pick = (a, oid)
                        break
                a, oid = pick
                say(f"[{tag}] casting Springheart Nantuko for bestow "
                    f"(oid {oid})")
                wire("cast_nantuko", {"oid": oid,
                                      "action": json.loads(json.dumps(a, default=str))})
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                ST["nantuko_cast"] = True
                ST["pending_cast"] = "nantuko"
                ST["pending_target_for"] = "nantuko"  # optimistic; bestow
                # choice (if any) comes first and re-arms this
                await submit_as_is(c, a)
                arm_cast_guard(tag, oid, "Springheart Nantuko")
                return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p1_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard(c, st, tag, 1):
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
    if await handle_decisions(c, st, tag, 1):
        return
    if cast_hold(tag, state, 1):
        return

    # copy-entry detection: a Nantuko-named permanent on P1's battlefield
    if ST["copy_cast"] and not ST["post_exported"]:
        for oid in bf_oids(state, 1):
            nm = str(get_obj(state, oid).get("name")
                     or get_obj(state, oid).get("base_name") or "").lower()
            if "nantuko" in nm and not (state.get("stack") or []):
                say(f"[{tag}] copy detected on BF (oid {oid}): "
                    f"{type_summary(get_obj(state, oid))}")
                ST["copy_entered"] = True
                await do_export(P0C, "post.json")  # host-only export
                ST["post_exported"] = True
                ST["stage"] = "ASSERT"
                ST["stop"] = True
                return

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag, prefer=[ISLAND]):
            return
        hn = hand_lnames(state, 1)
        if ST["nantuko_bestowed"] and not ST["copy_cast"] and COPYE in hn:
            if not ST["pre_exported"]:
                await do_export(P0C, "pre.json")
                ST["pre_exported"] = True
                say("[P1] pre.json exported before Copy Enchantment cast")
            cands = cast_actions_for(acts, state, COPYE)
            if cands and untapped_named(state, 1, ISLAND) >= 2:
                a, oid = cands[0]
                say(f"[{tag}] casting Copy Enchantment (oid {oid})")
                wire("cast_copy", {"oid": oid})
                ST["mana_needs"][tag] = {"U": 1, "generic": 1}
                ST["copy_cast"] = True
                ST["pending_cast"] = "copy"
                ST["pending_target_for"] = "copy"  # optimistic; optional
                # prompt (if any) re-arms this
                await submit_as_is(c, a)
                arm_cast_guard(tag, oid, "Copy Enchantment")
                return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision

# ------------------------------------------------------------- evaluation

def find_copy_candidate(post):
    objs = post.get("objects", {}) if post else {}
    for oid, o in objs.items():
        if o.get("zone") != "Battlefield" or o.get("controller") != 1:
            continue
        nm = str(o.get("name") or o.get("base_name") or "").lower()
        if "nantuko" in nm:
            return oid, o
    for oid, o in objs.items():
        if o.get("zone") != "Battlefield" or o.get("controller") != 1:
            continue
        blob = json.dumps(o).lower()
        if "copy" in blob and "nantuko" in blob:
            return oid, o
    return None, None


def evaluate(notes):
    A, D = {}, {}
    pre = env_state("pre.json")
    post = env_state("post.json")

    # A1: setup_ok -- bestowed Nantuko-Aura enchanting Elves in pre
    if pre:
        nk = bf_by_name(pre, 0, NANTUKO)
        aura_ok = False
        attach_info = None
        for o in nk:
            core, subs = live_types(get_obj(pre, o))
            if "Aura" in subs:
                aura_ok = True
                k, v = attached_to_of(get_obj(pre, o))
                attach_info = f"{k}={v}"
                break
        elves_ok = len(bf_by_name(pre, 0, ELVES)) >= 1
        phase_ok = (pre.get("phase") or "") in ("PreCombatMain",
                                                "PostCombatMain")
        ok = aura_ok and elves_ok and phase_ok
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"nantuko_aura={aura_ok} attach={attach_info} "
                            f"elves_on_bf={elves_ok} p1_main={phase_ok} "
                            f"turn={pre.get('turn_number')}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre.json missing (Copy Enchantment cast never ran)"

    # A2: copy entered as a copy of Springheart Nantuko
    copy_oid, copy_obj = find_copy_candidate(post) if post else (None, None)
    if post and copy_obj is not None:
        A["A2_copy_entered"] = "passed"
        D["A2_copy_entered"] = (f"oid={copy_oid} name="
                                f"{copy_obj.get('name') or copy_obj.get('base_name')}")
        with open(f"{EVDIR}/copy_object.json", "w") as f:
            json.dump(copy_obj, f, indent=1, default=str)
    elif post:
        A["A2_copy_entered"] = "failed"
        p1bf = [(o, obj_lname(post, o)) for o in bf_oids(post, 1)]
        D["A2_copy_entered"] = f"no Nantuko copy on P1 BF; P1 BF={p1bf}"
    else:
        A["A2_copy_entered"] = "not-run"
        D["A2_copy_entered"] = "post.json missing"

    # A3: the reported outcome -- copy's live types must NOT include Aura
    if copy_obj is not None:
        core, subs = live_types(copy_obj)
        aura_leak = "Aura" in subs
        type_ok = ({"Enchantment", "Creature"} <= core
                   and {"Insect", "Monk"} <= subs
                   and not aura_leak)
        A["A3_types_correct"] = "passed" if type_ok else "failed"
        D["A3_types_correct"] = (f"{type_summary(copy_obj)} "
                                 f"aura_leak={aura_leak}")
        k, v = attached_to_of(copy_obj)
        D["A3_types_correct"] += f" attach={k}={v}"
    else:
        A["A3_types_correct"] = "not-run"
        D["A3_types_correct"] = "copy object not found"

    # A4: document the bug shape when A3 failed
    if A.get("A3_types_correct") == "failed":
        A["A4_bug_documented"] = "passed"
        D["A4_bug_documented"] = ("Aura subtype leaked into the copy's live "
                                  f"types: {type_summary(copy_obj)} -- "
                                  "matches the reported intrinsic_copiable_values leak")
    else:
        A["A4_bug_documented"] = "not-run"
        D["A4_bug_documented"] = "A3 did not fail"

    # A5: cleanup
    if post:
        stack_empty = not (post.get("stack") or [])
        A["A5_cleanup"] = "passed" if stack_empty else "failed"
        D["A5_cleanup"] = f"stack_empty={stack_empty}"
    else:
        A["A5_cleanup"] = "not-run"
        D["A5_cleanup"] = "post.json missing"

    if A.get("A1_setup_ok") == "passed" and A.get("A2_copy_entered") == "passed" \
            and A.get("A3_types_correct") == "failed":
        verdict = "reproduced"
    elif A.get("A1_setup_ok") == "passed" and A.get("A2_copy_entered") == "passed" \
            and A.get("A3_types_correct") == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return A, D, verdict


def scenario_sha():
    h = hashlib.sha256()
    with open(__file__, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


async def main():
    global P0C
    init_st()
    t0 = time.time()
    notes = []
    ver, build, proto = await verify_server_hello()
    if not check_data_level():
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
                        ("A1_setup_ok", "A2_copy_entered",
                         "A3_types_correct", "A4_bug_documented",
                         "A5_cleanup")},
                       "notes": notes}, f, indent=2)
        await p0.close()
        return
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
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
            for r in c.rejections:
                wire("rejected", {"who": c.name, "type": r["type"],
                                  "data": r["data"],
                                  "stage": ST.get("stage")})
                say(f"[{c.name}] {r['type']}: "
                    f"{json.dumps(r['data'])[:300]}")
            c.rejections.clear()

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

        if not ST["nantuko_bestowed"] and time.time() - t0 > SETUP_ABORT_S:
            await do_export(p0, "mid_setup.json")
            notes.append(f"SETUP abort: bestowed Nantuko never landed after "
                         f"{SETUP_ABORT_S}s (turn {state.get('turn_number')}, "
                         f"bestow_chosen={ST['bestow_chosen']}, "
                         f"P0hand={hand_lnames(state, 0)[:8]})")
            say("[setup-abort] bestowed Nantuko never landed; ending run")
            ST["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={state.get('turn_number')} "
                f"active={state.get('active_player')} phase={state.get('phase')} "
                f"wf={wf} elves={ST['elves_cast']} "
                f"bestow_chosen={ST['bestow_chosen']} "
                f"bestowed={ST['nantuko_bestowed']} copy_cast={ST['copy_cast']} "
                f"copy_entered={ST['copy_entered']} "
                f"P0hand={hand_lnames(state, 0)[:5]} "
                f"P1hand={hand_lnames(state, 1)[:5]}")

    if not ST["post_exported"]:
        try:
            await do_export(p0, "post.json")
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")

    ass, det, verdict = evaluate(notes)
    for k in sorted(ass):
        say(f"{k}: {ass[k]} -- {det.get(k, '')}")
    say("verdict:", verdict)
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "details": det, "verdict": verdict,
                   "notes": notes}, f, indent=2)

    # run.json
    import datetime
    run_json = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "started_at": datetime.datetime.fromtimestamp(
            t0, datetime.timezone.utc).isoformat(),
        "finished_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "server_version": ver,
        "build_commit": build,
        "protocol_version": proto,
        "binary_sha256": "025c26c78762d8fc0bed47a7774749465c3e40751df73310cd7dee6ed5d506ef",
        "card_data_sha256": "7fe886cc031d9bd6532317572f84f65da66941ebdf6a43d337b625fbc369548e",
        "draft_pools_sha256": "4a0b44a9af0881f924b4120a43fe04947dd673aa867e796917b696d7ea26ba59",
        "game_code": ST["game_code"],
        "decks": {"P0": [list(x) for x in P0_DECK],
                  "P1": [list(x) for x in P1_DECK]},
        "scenario": os.path.basename(__file__),
        "scenario_sha256": scenario_sha(),
        "verdict": verdict,
        "assertions": ass,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_json, f, indent=2)

    # PNG summary from the saved states/assertions
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, f"{BACKFILL}/driver/render_summary.py",
             EVDIR, str(ISSUE),
             "Bestow form leaks into intrinsic copiable values"],
            capture_output=True, text=True, timeout=120)
        say("render stdout:", (r.stdout or "")[:300])
        say("render stderr:", (r.stderr or "")[:300])
        png = f"{EVDIR}/summary.png"
        if os.path.exists(png):
            from PIL import Image
            im = Image.open(png)
            im.verify()
            say(f"summary.png OK ({os.path.getsize(png)} bytes)")
            wire("png_ok", {"bytes": os.path.getsize(png)})
        else:
            notes.append("summary.png missing after render")
    except Exception as e:
        notes.append(f"PNG render failed: {e}")
        say("PNG render failed:", e)

    # manifest
    import hashlib as hl
    man = {}
    for fn in sorted(os.listdir(EVDIR)):
        p = f"{EVDIR}/{fn}"
        if os.path.isfile(p) and fn != "manifest.sha256":
            h = hl.sha256()
            with open(p, "rb") as f:
                h.update(f.read())
            man[fn] = h.hexdigest()
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn, h in man.items():
            f.write(f"{h}  {fn}\n")
    say(f"manifest written ({len(man)} files)")

    await p0.close()
    await p1.close()
    WIRE.close()
    RUNLOG.close()
    print(f"VERDICT={verdict}")


if __name__ == "__main__":
    asyncio.run(main())
