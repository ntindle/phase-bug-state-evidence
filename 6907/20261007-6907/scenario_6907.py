#!/usr/bin/env python3
"""Issue #6907: Nesting Grounds doesn't ask which counter to move.

Oracle (verified from pinned v0.103.0 card-data.json):
  {T}: Add {C}.
  {1}, {T}: Move a counter from target permanent you control onto a second
  target permanent. Activate only as a sorcery.

Pinned v0.103.0 parse: MoveCounters effect with counter_type null
(unspecified), count Fixed 1; source = Typed Permanent controller You;
target = Typed Permanent (any controller).

Reported (Discord 2026-08-02): a permanent with both +1/+1 and lore
counters; Nesting Grounds doesn't offer selection for which counter to
move, but seems to default to +1/+1's.

Behavioral contract (native engine, protocol 106, two human seats):
  P0 controls Nesting Grounds, Sanctuary Warden (enters with 2 shield
  counters), Gavony Township, Grizzly Bears. P1 is passive (lands only).
  - CONTROL (single counter type): Warden has only shield counters.
    Activate Nesting Grounds targeting Warden (source) -> Bears
    (destination). Expect: exactly one shield counter moves.
  - PROOF (two counter types): activate Gavony Township (Warden gains a
    +1/+1 counter; now shield + +1/+1). Activate Nesting Grounds again
    targeting Warden -> Bears. Correct behavior: a counter-type choice is
    offered; the driver answers "shield" and exactly one shield counter
    moves. Reported bug: no choice is offered; the engine defaults to
    +1/+1.

  A1 setup_ok        pre_control: NG + Warden + Township + Bears on P0 BF;
                     Warden has >=1 shield and no other counter type;
                     P0 main-phase priority.
  A2 control_move    post_control: exactly one shield counter moved
                     Warden -> Bears; no +1/+1 on either.
  A3 counter_prompted  PROOF: a counter-type choice opportunity was offered
                     during the second activation (the reported gap).
  A4 exact_move      (A3 passed) exactly one counter of the chosen type
                     moved Warden -> Bears; other types unchanged.
  A5 default_documents (A3 failed) documents which counter type the engine
                     moved by default (report says +1/+1).
  A6 cleanup         post_proof: stack empty, game proceeding.

Verdict: reproduced iff A1 passes and (A3 fails, or A3 passes but A4
fails). not-reproduced iff A1, A2, A3, A4, A6 all pass. blocked iff A1
fails (or the two-type PROOF setup never materializes).

Protocol-106 port of driver/scenario_6907.py (verified 2026-09-12, run
20260912-6907d, verdict reproduced). Conventions from scenario_6906_01030.py:
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
ISSUE = 6907
RUN_ID = "20261007-6907"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

WARDEN = "sanctuary warden"
BEAR = "grizzly bears"
NG = "nesting grounds"
TOWN = "gavony township"
FOREST = "forest"
PLAINS = "plains"

P0_DECK = [(WARDEN, 4), (BEAR, 4), (NG, 4), (TOWN, 4),
           (FOREST, 22), (PLAINS, 22)]
P1_DECK = [(FOREST, 30), (PLAINS, 30)]

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
        "stage": "SETUP",   # SETUP -> CONTROL -> PROOF_SETUP -> PROOF -> DONE
        "stop": False,
        "states_seen": 0,
        "mulligans": 0,
        "warden_oid": None,
        "bears_oid": None,
        "ng_oid": None,
        "town_oid": None,
        "warden_cast": False,
        "warden_etb_declined": False,
        "ng1_submitted": False,
        "ng1_turn": None,
        "ng1_watch": None,
        "ng1_stack_seen": False,
        "ng1_resolved": False,
        "ng_targets_answered": [],
        "town_submitted": False,
        "town_turn": None,
        "town_watch": None,
        "town_resolved": False,
        "ng2_submitted": False,
        "ng2_turn": None,
        "ng2_watch": None,
        "ng2_stack_seen": False,
        "ng2_resolved": False,
        "target_sels": [],
        "target_sel_files": 0,
        "decisions": [],
        "decision_files": 0,
        "counter_choice_offered": False,
        "counter_choice_pick": None,
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


def counters_of(state, oid):
    v = get_obj(state, oid).get("counters") or {}
    return dict(v) if isinstance(v, dict) else {}


def counter_class(k):
    kl = str(k).lower().replace("_", "").replace(" ", "")
    if "shield" in kl:
        return "shield"
    if "p1p1" in kl or kl == "+1/+1":
        return "p1p1"
    if "lore" in kl:
        return "lore"
    return kl


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


def choice_id_of(ch):
    return ch.get("id") or ch.get("choiceId") or ch.get("choice_id")


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
    """Confirm the v0.103.0 parse still shows Nesting Grounds' {1},{T}
    ability as MoveCounters with counter_type null (the data-level premise
    of the bug)."""
    ok, notes = True, []
    ng = CARD_DATA.get(NG, {})
    oracle = str(ng.get("oracle_text", ""))
    if "move a counter from target permanent you control" not in oracle.lower():
        ok = False
        notes.append("oracle missing the move-a-counter ability")
    abs_ = ng.get("abilities") or []
    payload = {}
    if len(abs_) < 2:
        ok = False
        notes.append(f"expected >=2 abilities, got {len(abs_)}")
    else:
        ab = abs_[1]
        eff = ab.get("effect") or {}
        cost = ab.get("cost") or {}
        src = eff.get("source") or {}
        tgt = eff.get("target") or {}
        payload = {
            "effect_type": eff.get("type"),
            "counter_type": eff.get("counter_type"),
            "count": eff.get("count"),
            "source": {"type": src.get("type"),
                       "controller": src.get("controller")},
            "target": {"type": tgt.get("type"),
                       "controller": tgt.get("controller")},
            "cost": cost.get("type"),
            "restrictions": [r.get("type")
                             for r in (ab.get("activation_restrictions")
                                       or [])],
        }
        if eff.get("type") != "MoveCounters":
            ok = False
            notes.append("ability 1 is no longer MoveCounters")
        if eff.get("counter_type") is not None:
            ok = False
            notes.append("ability 1 counter_type is now specified "
                         f"({eff.get('counter_type')}) -- premise changed")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes, "ng_oracle": oracle,
                   "ability1_payload": payload}, f, indent=1)
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
    want_warden = pid != 0 or WARDEN in hn
    if pid == 0 and ST["mulligans"] < 3 and not want_warden:
        ST["mulligans"] += 1
        say(f"[{tag}] mulligans ({ST['mulligans']}) seeking Warden "
            f"(hand={hn[:6]})")
        wire("mulligan", {"who": tag, "decision": "mulligan",
                          "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    else:
        say(f"[{tag}] keeps (hand={len(hn)}: {hn[:6]})")
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as SelectCards legal
    actions plus a vi schema/select opportunity. Proven pattern from
    scenario_1234_01030.py."""
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
        if nm == WARDEN:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, next((s.get('data', {}).get('reference') for s in x.get('surfaces', []) or [] if isinstance(s.get('data'), dict) and 'reference' in s.get('data')), None)) for x in picks]}")
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


def p0_discard_rank(state):
    def rank(oid):
        nm = obj_lname(state, oid)
        if nm == WARDEN:
            return (3, nm)
        if nm == TOWN:
            return (2, nm)
        if nm == NG:
            return (1, nm)
        if nm == BEAR:
            return (0, nm)
        return (-1, nm)  # lands first
    return rank


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

        ranked = sorted(cands, key=lambda ch: rank_fn(state)(_ref(ch)))
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


def activate_ability_action(acts, src_oid, preferred_index=1):
    """Legacy ActivateAbility action for src_oid; prefer preferred_index."""
    cands = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            src = int(d.get("source_id", d.get("object_id", -1)))
        except (TypeError, ValueError):
            continue
        if src == int(src_oid):
            cands.append((d.get("ability_index"), a))
    if not cands:
        return None
    for idx, a in cands:
        if idx == preferred_index:
            return a
    return cands[0][1]


def find_activate_vi(st, src_oid, text_hint):
    """vi exactChoices activateAbility choice for src_oid (fallback)."""
    cands = []
    for op in vi_ops(st):
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data") or {}
        choices = data.get("choices") or resp.get("choices") or []
        for ch in choices:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if isinstance(s.get("data"), dict)]
            if "activateAbility" not in codes:
                continue
            blob = json.dumps(ch, default=str)
            if str(src_oid) not in blob:
                continue
            cands.append((op.get("interactionId"), ch, blob))
    if not cands:
        return None
    for iid, ch, blob in cands:
        if text_hint.lower() in blob.lower():
            return iid, ch
    if len(cands) == 1:
        return cands[0][0], cands[0][1]
    return None

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
        "prompt_head": json.dumps(data.get("spec") or {},
                                  default=str)[:600],
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


async def answer_ng_target(c, state, opp, rtype, spec_type, tag):
    """Answer one Nesting Grounds target slot. Source slot (candidates
    limited to P0 permanents) -> Warden; destination slot -> Bears.
    A schema opportunity whose candidates carry NO card oids but mention
    counters is the counter-type choice, not a target prompt."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    card_cands = [ch for ch in cands if cand_oid(ch) is not None]
    if not card_cands:
        blob = json.dumps(opp, default=str).lower()
        if "counter" in blob:
            wire("counter_choice_in_target_slot",
                 {"stage": ST.get("stage"), "iid": opp.get("interactionId")})
            say(f"[{tag}] target-slot opportunity is actually a "
                f"counter-type choice; routing to counter handler")
            return await answer_counter_choice(c, state, opp, tag)
        return None
    prompt = json.dumps(data.get("spec") or {}, default=str).lower()
    p1_in_cands = any((get_obj(state, cand_oid(ch)).get("controller"))
                      == 1 for ch in card_cands)
    warden_oid = str(ST.get("warden_oid") or "")
    bears_oid = str(ST.get("bears_oid") or "")
    answered = [str(x) for x in ST.get("ng_targets_answered") or []]
    warden_avail = any(cand_oid(ch) == warden_oid for ch in card_cands)
    bears_avail = any(cand_oid(ch) == bears_oid for ch in card_cands)
    is_dest_slot = p1_in_cands or ("second" in prompt)
    want = None
    if is_dest_slot:
        if bears_oid not in answered and bears_avail:
            want = bears_oid
        elif warden_oid not in answered and warden_avail:
            want = warden_oid
    else:
        if warden_oid not in answered and warden_avail:
            want = warden_oid
        elif bears_oid not in answered and bears_avail:
            want = bears_oid
    node = pick_candidate_by_oid(state, opp, want) if want else None
    if node is None:
        node = card_cands[0]
        say(f"[{tag}] NG target: fallback to first card candidate")
    o = get_obj(state, cand_oid(node))
    say(f"[{tag}] NG target answers {obj_lname(state, cand_oid(node))} "
        f"(dest_slot={is_dest_slot})")
    wire("target_answer", {"stage": ST.get("stage"), "want": want,
                           "dest_slot": is_dest_slot,
                           "choiceId": node.get("id")})
    ST["ng_targets_answered"].append(cand_oid(node))
    spec = data.get("spec", {}) or {}
    iid = opp.get("interactionId")
    cid = node.get("id")
    if rtype == "schema" and spec_type == "sequence" and (
            str(spec.get("max")) == "2" or spec.get("count") == 2):
        other = bears_oid if want == warden_oid else warden_oid
        onode = pick_candidate_by_oid(state, opp, other)
        cids = [cid] + ([onode.get("id")] if onode else [])
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "sequence",
                                           "data": {"choiceIds": cids}}},
                           tag)
    else:
        await answer_target(c, state, opp, rtype, spec_type, node, tag)
    return cand_oid(node)


# ------------------------------------------------- decisions / counters

def is_card_choice_opp(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        if cand_oid(ch) is not None:
            return True
        codes = set(surf_codes(ch))
        if codes & {"candidate", "target"}:
            return True
    return False


def looks_like_counter_choice(opp):
    """A non-card, non-optional-effect decision opportunity that names
    counter types."""
    if is_card_choice_opp(opp):
        return False
    blob = json.dumps(opp, default=str).lower()
    if "decideoptionaleffect" in blob:
        return False
    return "counter" in blob


def record_decision(state, opp, stage):
    """Record any non-target decision opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in ST["decisions"]):
        return None
    blob = json.dumps(opp, default=str)
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "counter_like": looks_like_counter_choice(opp),
        "is_card_choice": is_card_choice_opp(opp),
        "rtype": (opp.get("response") or {}).get("type"),
        "blob_head": blob[:3000],
    }
    ST["decisions"].append(rec)
    n = ST["decision_files"] = ST["decision_files"] + 1
    with open(f"{EVDIR}/decision_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(blob)}, f, indent=1,
                  default=str)
    wire("decision_recorded",
         {"n": n, "stage": stage, "counter_like": rec["counter_like"]})
    say(f"recorded decision #{n} (stage {stage}, "
        f"counter_like={rec['counter_like']})")
    return rec


async def answer_counter_choice(c, state, opp, tag):
    """Answer a counter-type choice, preferring 'shield'. Records the pick."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    rtype = resp.get("type")
    nodes = data.get("candidates") or data.get("choices") or []
    pick = None
    for nd in nodes:
        if "shield" in json.dumps(nd, default=str).lower():
            pick = nd
            break
    if pick is None and nodes:
        pick = nodes[0]
    if pick is None:
        say(f"[{tag}] counter choice: no nodes to pick from")
        return None
    ST["counter_choice_pick"] = json.dumps(pick, default=str)[:800]
    ST["counter_choice_offered"] = True
    wire("counter_choice_pick",
         {"pick": ST["counter_choice_pick"], "stage": ST.get("stage")})
    say(f"[{tag}] counter choice offered (stage {ST.get('stage')}); "
        f"pick: {ST['counter_choice_pick'][:160]}")
    iid = opp.get("interactionId")
    cid = choice_id_of(pick)
    stype = spec.get("type") if rtype == "schema" else rtype
    if stype == "sequence":
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": [cid]}}}
    elif stype == "select":
        sub = {"interactionId": iid,
               "response": {"type": "select",
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    await interact_as(c, sub, tag)
    return cid


async def decline_warden_etb(c, state, tag):
    """Decline Sanctuary Warden's 'you may remove a counter from it' ETB
    (keeps both shield counters for the single-type control leg).
    Proven shape (scenario_301_01030.py): vi exactChoices opportunity with a
    decideOptionalEffect action code; the decline choice carries
    role=="accept", value=="false". In SETUP the only optional effect is
    the Warden ETB, so no text matching is needed."""
    for opp in vi_ops(c.latest or {}):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or []
        if not any("decideOptionalEffect" in surf_codes(ch)
                   for ch in choices):
            continue
        pick = None
        for ch in choices:
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data", {}) or {}
                if dd.get("role") == "accept" \
                        and str(dd.get("value")).lower() == "false":
                    pick = ch
                    break
            if pick is not None:
                break
        if pick is None:
            for ch in choices:
                vals = [str((s.get("data") or {}).get("value"))
                        for s in ch.get("surfaces", []) or []]
                if "decideOptionalEffect" in surf_codes(ch) \
                        and "false" in vals:
                    pick = ch
                    break
        if pick is None:
            wire("warden_etb_no_decline_choice",
                 {"iid": opp.get("interactionId"),
                  "opp": json.loads(json.dumps(opp, default=str))})
            say(f"[{tag}] Warden ETB: no decline choice found; wired opp")
            return False
        wire("warden_etb_decline", {"iid": opp.get("interactionId")})
        say(f"[{tag}] declining Warden ETB may-trigger")
        await interact_as(
            c, {"interactionId": opp.get("interactionId"),
                "response": {"type": "choose",
                             "data": {"choiceId": choice_id_of(pick)}}}, tag)
        ST["warden_etb_declined"] = True
        return True
    return False


def stack_has_ng_ability(state, ng_oid):
    """Detect the Nesting Grounds {1},{T} activation on the stack via the
    entry's own fields, never via whole-blob keyword matching."""
    for e in state.get("stack") or []:
        if not isinstance(e, dict):
            continue
        src = e.get("source_id") or e.get("source") or {}
        src_id = src.get("id") if isinstance(src, dict) else src
        try:
            src_match = int(src_id) == int(ng_oid)
        except (TypeError, ValueError):
            src_match = False
        if not src_match:
            continue
        kind = e.get("kind") or {}
        kind_s = (kind.get("type") if isinstance(kind, dict)
                  else str(kind)).lower()
        if "activ" in kind_s:
            return True
        desc = str((e.get("ability") or {}).get("description")
                   if isinstance(e.get("ability"), dict) else "")
        if "move a counter" in desc.lower():
            return True
    return False

# ------------------------------------------------------------------- ticks

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


async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_bottom(c, acts, st, 0, tag):
        return
    if await do_discard(c, st, tag, 0, p0_discard_rank):
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

    # --- Warden ETB may-trigger: decline while in SETUP/CONTROL ---
    if ST["stage"] in ("SETUP", "CONTROL") \
            and not ST["warden_etb_declined"]:
        if await decline_warden_etb(c, state, tag):
            return

    # --- Warden ETB trigger target (SETUP): the 106 engine asks
    #     TriggerTargetSelection for "remove a counter from it" before the
    #     may-choice; answer with Warden itself. ---
    if ST["stage"] == "SETUP" and not ST["warden_etb_declined"]:
        wft = (state.get("waiting_for") or {}).get("type")
        if wft == "TriggerTargetSelection":
            opp, rtype, spec_type = target_opportunity(st)
            if opp is not None \
                    and opp.get("interactionId") not in SUBMITTED_OPPS:
                # In SETUP the only trigger is Warden's ETB ("you may
                # remove a counter from it"); target Warden itself.
                record_target_sel(state, opp, "SETUP")
                SUBMITTED_OPPS.add(opp.get("interactionId"))
                ch = pick_candidate_by_oid(state, opp,
                                           ST.get("warden_oid"))
                if ch is None:
                    resp = opp.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    cands = (data.get("candidates")
                             or data.get("choices") or [])
                    ch = cands[0] if cands else None
                    say(f"[{tag}] Warden ETB: no Warden candidate; "
                        f"first-candidate fallback")
                if ch is not None:
                    say(f"[{tag}] Warden ETB trigger targets Warden "
                        f"(decline follows via may-choice)")
                    wire("warden_etb_target",
                         {"iid": opp.get("interactionId")})
                    await answer_target(c, state, opp, rtype, spec_type,
                                        ch, tag)
                return

    # --- NG target selections (CONTROL / PROOF): answer before anything ---
    if ST["stage"] in ("CONTROL", "PROOF") \
            and (ST["ng1_submitted"] or ST["ng2_submitted"]):
        opp, rtype, spec_type = target_opportunity(st)
        if opp is not None and opp.get("interactionId") not in SUBMITTED_OPPS:
            record_target_sel(state, opp, ST["stage"])
            SUBMITTED_OPPS.add(opp.get("interactionId"))
            await answer_ng_target(c, state, opp, rtype, spec_type, tag)
            return
        # No unanswered target prompt: fall through. Answered opportunities
        # clear from vi; never hold priority on real_decision_pending here
        # or the on-stack ability can never resolve.

    # --- counter-type choices and other decisions: record + answer --------
    # Skip already-answered opportunities: holding on those deadlocks the
    # game (answered target selections clear from vi; never hold on them).
    if ST["stage"] in ("CONTROL", "PROOF") and real_decision_pending(st):
        unanswered = [o for o in vi_ops(st)
                      if o.get("interactionId") not in SUBMITTED_OPPS]
        for opp in unanswered:
            rec = record_decision(state, opp, ST["stage"])
            if rec and rec["counter_like"]:
                SUBMITTED_OPPS.add(opp.get("interactionId"))
                await answer_counter_choice(c, state, opp, tag)
                return
        if unanswered:
            return  # hold: an unanswered non-counter decision is pending
        # else: everything answered, broadcast pending -> fall through

    if ST["stage"] == "SETUP":
        w = bf_by_name(state, 0, WARDEN)
        b = bf_by_name(state, 0, BEAR)
        ng = bf_by_name(state, 0, NG)
        tw = bf_by_name(state, 0, TOWN)
        if w:
            ST["warden_oid"] = w[0]
            ST["warden_cast"] = True
        if b:
            ST["bears_oid"] = b[0]
        if ng:
            ST["ng_oid"] = ng[0]
        if tw:
            ST["town_oid"] = tw[0]
        if ng and w and tw and b:
            wc = counters_of(state, w[0])
            classes = {counter_class(k) for k in wc}
            if wc.get("shield", 0) >= 1 and classes == {"shield"}:
                ST["stage"] = "CONTROL"
                say(f"board ready (turn {state.get('turn_number')}): NG + "
                    f"Warden (counters={wc}) + Township + Bears; "
                    f"stage -> CONTROL")
                return
        if my_main(state, 0):
            if await play_a_land(c, state, 0, acts, tag,
                                 prefer=[PLAINS, FOREST, NG, TOWN]):
                return
            if not b and BEAR in hand_lnames(state, 0):
                a, oid = cast_action_for(acts, state, BEAR)
                if a is not None and untapped_named(state, 0, FOREST) >= 1:
                    say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                    wire("cast_bear", {"oid": oid})
                    ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                    await submit_as_is(c, a)
                    return
            if not w and WARDEN in hand_lnames(state, 0):
                a, oid = cast_action_for(acts, state, WARDEN)
                if a is not None and untapped_named(state, 0, PLAINS) >= 2 \
                        and sum(1 for o in bf_oids(state, 0)
                                if not get_obj(state, o).get("tapped")
                                and is_land(get_obj(state, o))) >= 6:
                    say(f"[{tag}] casting Sanctuary Warden (oid {oid})")
                    wire("cast_warden", {"oid": oid})
                    ST["mana_needs"][tag] = {"W": 2, "generic": 4}
                    await submit_as_is(c, a)
                    return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if ST["stage"] == "CONTROL":
        if not ST["ng1_submitted"]:
            ng = ST.get("ng_oid")
            ready = (ng and my_main(state, 0)
                     and not get_obj(state, ng).get("tapped"))
            if ready:
                a = activate_ability_action(acts, ng, 1)
                if a is not None:
                    wire("ng1_submit", {"action": a})
                    say(f"[{tag}] activating Nesting Grounds (control, "
                        f"turn {state.get('turn_number')}); pre_control "
                        f"exported")
                    await do_export(c, "pre_control.json")
                    ST["mana_needs"][tag] = {"generic": 1}
                    await submit_as_is(c, a)
                    ST["ng1_submitted"] = True
                    ST["ng1_turn"] = state.get("turn_number")
                    ST["ng1_watch"] = time.time()
                    ST["ng_targets_answered"] = []
                    return
                f = find_activate_vi(st, ng, "move a counter")
                if f is not None:
                    iid, ch = f
                    say(f"[{tag}] activating NG via vi (control)")
                    await do_export(c, "pre_control.json")
                    ST["mana_needs"][tag] = {"generic": 1}
                    await interact_as(
                        c, {"interactionId": iid,
                            "response": {"type": "choose",
                                         "data": {"choiceId":
                                                  choice_id_of(ch)}}}, tag)
                    ST["ng1_submitted"] = True
                    ST["ng1_turn"] = state.get("turn_number")
                    ST["ng1_watch"] = time.time()
                    ST["ng_targets_answered"] = []
                    return
                dkey = ("nact1", state.get("turn_number"))
                if dkey not in MULLS:
                    MULLS.add(dkey)
                    wire("no_ng1_activation",
                         {"turn": state.get("turn_number"),
                          "act_types": sorted(atypes)})
                    say(f"[{tag}] CONTROL ready but no NG activation "
                        f"advertised (types={sorted(atypes)})")
            if my_main(state, 0):
                if await play_a_land(c, state, 0, acts, tag):
                    return
        else:
            # in-flight: watch the ability resolve, then export post state.
            # NOTE: never return early here -- P0 must keep passing priority
            # or the stack entry can never resolve (both players must pass
            # in succession). Fall through to the priority-pass logic below.
            if stack_has_ng_ability(state, ST.get("ng_oid")):
                if not ST["ng1_stack_seen"]:
                    wire("ng1_on_stack",
                         {"turn": state.get("turn_number")})
                    say(f"[{tag}] NG ability #1 on stack "
                        f"(turn {state.get('turn_number')})")
                ST["ng1_stack_seen"] = True
            elif ST["ng1_stack_seen"] and not ST["ng1_resolved"]:
                ST["ng1_resolved"] = True
                await do_export(c, "post_control.json")
                ST["stage"] = "PROOF_SETUP"
                say(f"[{tag}] NG #1 resolved; post_control exported; "
                    f"stage -> PROOF_SETUP")
                return
            elif not ST["ng1_stack_seen"] \
                    and (state.get("turn_number") or 0) > \
                    (ST.get("ng1_turn") or 0):
                ST["ng1_resolved"] = True
                await do_export(c, "post_control.json")
                ST["stage"] = "PROOF_SETUP"
                say(f"[{tag}] activation turn passed without stack sighting; "
                    f"post_control exported; stage -> PROOF_SETUP")
                return
            elif time.time() - (ST.get("ng1_watch") or time.time()) > 120:
                wire("ng1_stall",
                     {"target_sels": len(ST["target_sels"])})
                await do_export(c, "mid_stall.json")
                ST["ng1_resolved"] = True
                ST["stage"] = "PROOF_SETUP"
                say(f"[{tag}] NG #1 resolution watch timed out; mid_stall "
                    f"exported; stage -> PROOF_SETUP")
                return
            # settle not reached: fall through to priority passes
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if ST["stage"] == "PROOF_SETUP":
        if not ST["town_submitted"]:
            tw = ST.get("town_oid")
            ready = (tw and my_main(state, 0)
                     and not get_obj(state, tw).get("tapped"))
            if ready:
                a = activate_ability_action(acts, tw, 0)
                if a is None:
                    a = activate_ability_action(acts, tw, 1)
                if a is not None:
                    wire("town_submit", {"action": a})
                    say(f"[{tag}] activating Gavony Township (turn "
                        f"{state.get('turn_number')}); pre_township "
                        f"exported")
                    await do_export(c, "pre_township.json")
                    ST["mana_needs"][tag] = {"G": 1, "W": 1, "generic": 2}
                    await submit_as_is(c, a)
                    ST["town_submitted"] = True
                    ST["town_turn"] = state.get("turn_number")
                    ST["town_watch"] = time.time()
                    return
                dkey = ("ntown", state.get("turn_number"))
                if dkey not in MULLS:
                    MULLS.add(dkey)
                    say(f"[{tag}] PROOF_SETUP ready but no Township "
                        f"activation advertised")
            if my_main(state, 0):
                if await play_a_land(c, state, 0, acts, tag):
                    return
        else:
            w = ST.get("warden_oid")
            wc = counters_of(state, w) if w else {}
            if any(counter_class(k) == "p1p1" for k in wc):
                ST["town_resolved"] = True
                await do_export(c, "post_township.json")
                ST["stage"] = "PROOF"
                say(f"[{tag}] Township resolved: Warden counters={wc}; "
                    f"post_township exported; stage -> PROOF")
                return
            if (state.get("turn_number") or 0) > (ST.get("town_turn") or 0) \
                    or time.time() - (ST.get("town_watch")
                                      or time.time()) > 150:
                await do_export(c, "post_township.json")
                w2c = counters_of(state, w) if w else {}
                if any(counter_class(k) == "p1p1" for k in w2c):
                    ST["town_resolved"] = True
                    ST["stage"] = "PROOF"
                    say(f"[{tag}] Township leg settled with +1/+1 present; "
                        f"stage -> PROOF")
                else:
                    ST["stage"] = "DONE"
                    ST["stop"] = True
                    say(f"[{tag}] Township never gave Warden +1/+1 "
                        f"(counters={w2c}); cannot build two-type source; "
                        f"DONE (blocked)")
                return
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

    if ST["stage"] == "PROOF":
        if not ST["ng2_submitted"]:
            ng = ST.get("ng_oid")
            ready = (ng and my_main(state, 0)
                     and not get_obj(state, ng).get("tapped"))
            if ready:
                a = activate_ability_action(acts, ng, 1)
                if a is not None:
                    wire("ng2_submit", {"action": a})
                    say(f"[{tag}] activating Nesting Grounds (proof, "
                        f"turn {state.get('turn_number')}); pre_proof "
                        f"exported")
                    await do_export(c, "pre_proof.json")
                    ST["mana_needs"][tag] = {"generic": 1}
                    await submit_as_is(c, a)
                    ST["ng2_submitted"] = True
                    ST["ng2_turn"] = state.get("turn_number")
                    ST["ng2_watch"] = time.time()
                    ST["ng_targets_answered"] = []
                    return
                dkey = ("nact2", state.get("turn_number"))
                if dkey not in MULLS:
                    MULLS.add(dkey)
                    say(f"[{tag}] PROOF ready but no NG activation "
                        f"advertised (ng tapped="
                        f"{get_obj(state, ng).get('tapped')})")
            if my_main(state, 0):
                if await play_a_land(c, state, 0, acts, tag):
                    return
        else:
            # in-flight: watch resolution; NEVER return early -- fall
            # through to the priority-pass logic so the stack can resolve.
            if stack_has_ng_ability(state, ST.get("ng_oid")):
                if not ST["ng2_stack_seen"]:
                    wire("ng2_on_stack",
                         {"turn": state.get("turn_number")})
                    say(f"[{tag}] NG ability #2 on stack "
                        f"(turn {state.get('turn_number')})")
                ST["ng2_stack_seen"] = True
            elif ST["ng2_stack_seen"] and not ST["ng2_resolved"]:
                ST["ng2_resolved"] = True
                await do_export(c, "post_proof.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
                say(f"[{tag}] NG #2 resolved; post_proof exported; DONE")
                return
            elif not ST["ng2_stack_seen"] \
                    and (state.get("turn_number") or 0) > \
                    (ST.get("ng2_turn") or 0):
                ST["ng2_resolved"] = True
                await do_export(c, "post_proof.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
                say(f"[{tag}] activation turn passed without stack sighting; "
                    f"post_proof exported; DONE")
                return
            elif time.time() - (ST.get("ng2_watch") or time.time()) > 120:
                wire("ng2_stall",
                     {"target_sels": len(ST["target_sels"])})
                await do_export(c, "mid_stall.json")
                ST["ng2_resolved"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True
                say(f"[{tag}] NG #2 resolution watch timed out; mid_stall "
                    f"exported; DONE")
                return
            # settle not reached: fall through to priority passes
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
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard(c, st, tag, 1,
                        lambda state: (lambda oid: (0, ""))):
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
    if ST["stage"] in ("SETUP", "CONTROL", "PROOF_SETUP", "PROOF"):
        if my_main(state, 1):
            if await play_a_land(c, state, 1, acts, tag):
                return
        # P1 is passive: never cast; hold on real decisions, else pass.
        # NOTE: fall through to priority passes -- never hold priority
        # while merely watching the stack.
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

# ------------------------------------------------------------- evaluation

def class_counter_delta(pre, post, pid, name):
    """Normalized-class counter deltas for the named permanent of pid."""
    def snap(st):
        if not st:
            return None
        oids = bf_by_name(st, pid, name)
        if not oids:
            return None
        raw = counters_of(st, oids[0])
        agg = {}
        for k, v in raw.items():
            agg[counter_class(k)] = agg.get(counter_class(k), 0) + v
        return agg, raw
    a = snap(pre)
    b = snap(post)
    if a is None or b is None:
        return None, None, None
    (aa, araw), (bb, braw) = a, b
    keys = set(aa) | set(bb)
    return ({k: bb.get(k, 0) - aa.get(k, 0) for k in keys}, araw, braw)


def evaluate(notes):
    A, D = {}, {}
    pre_c = env_state("pre_control.json")
    post_c = env_state("post_control.json")
    pre_t = env_state("pre_township.json")
    post_t = env_state("post_township.json")
    pre_p = env_state("pre_proof.json")
    post_p = env_state("post_proof.json")

    # A1: setup_ok
    if pre_c:
        ng_ok = len(bf_by_name(pre_c, 0, NG)) >= 1
        w_ok = len(bf_by_name(pre_c, 0, WARDEN)) >= 1
        t_ok = len(bf_by_name(pre_c, 0, TOWN)) >= 1
        b_ok = len(bf_by_name(pre_c, 0, BEAR)) >= 1
        woids = bf_by_name(pre_c, 0, WARDEN)
        wc = counters_of(pre_c, woids[0]) if woids else {}
        classes = {counter_class(k) for k in wc}
        ctr_ok = wc.get("shield", 0) >= 1 and classes == {"shield"}
        phase_ok = (pre_c.get("phase") or "") in ("PreCombatMain",
                                                  "PostCombatMain") \
            and pre_c.get("active_player") == 0
        ok = ng_ok and w_ok and t_ok and b_ok and ctr_ok and phase_ok
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"ng={ng_ok} warden={w_ok} township={t_ok} "
                            f"bears={b_ok} warden_counters={wc} "
                            f"single_shield_type={ctr_ok} p0_main={phase_ok} "
                            f"turn={pre_c.get('turn_number')}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre_control.json missing (NG activation #1 never ran)"

    # A2: control_move
    if pre_c and post_c:
        dw, aw, bw = class_counter_delta(pre_c, post_c, 0, WARDEN)
        db, ab, bb = class_counter_delta(pre_c, post_c, 0, BEAR)
        if dw is None or db is None:
            A["A2_control_move"] = "not-run"
            D["A2_control_move"] = "warden/bears not found in pre/post control"
        else:
            ok = (dw.get("shield", 0) == -1 and db.get("shield", 0) == 1
                  and dw.get("p1p1", 0) == 0 and db.get("p1p1", 0) == 0)
            A["A2_control_move"] = "passed" if ok else "failed"
            D["A2_control_move"] = (f"warden delta={dw} {aw}->{bw}; "
                                    f"bears delta={db} {ab}->{bb}")
    else:
        A["A2_control_move"] = "not-run"
        D["A2_control_move"] = "pre/post control states missing"

    # A3: counter_prompted (PROOF stage)
    proof_counter_recs = [r for r in ST["decisions"]
                          if r.get("stage") == "PROOF"
                          and r.get("counter_like")]
    control_counter_recs = [r for r in ST["decisions"]
                            if r.get("stage") == "CONTROL"
                            and r.get("counter_like")]
    if ST["ng2_submitted"]:
        if proof_counter_recs:
            A["A3_counter_prompted"] = "passed"
            D["A3_counter_prompted"] = (
                f"{len(proof_counter_recs)} counter-type choice(s) offered "
                f"during PROOF; pick={ST.get('counter_choice_pick')}")
        else:
            A["A3_counter_prompted"] = "failed"
            D["A3_counter_prompted"] = (
                "no counter-type choice offered during PROOF activation "
                f"(decisions recorded in PROOF: "
                f"{len([r for r in ST['decisions'] if r.get('stage')=='PROOF'])})")
    else:
        A["A3_counter_prompted"] = "not-run"
        D["A3_counter_prompted"] = "NG activation #2 never submitted"

    # A4: exact_move (only meaningful if A3 passed)
    if A["A3_counter_prompted"] == "passed" and pre_p and post_p:
        dw, aw, bw = class_counter_delta(pre_p, post_p, 0, WARDEN)
        db, ab, bb = class_counter_delta(pre_p, post_p, 0, BEAR)
        if dw is None or db is None:
            A["A4_exact_move"] = "not-run"
            D["A4_exact_move"] = "warden/bears not found in pre/post proof"
        else:
            ok = (dw.get("shield", 0) == -1 and db.get("shield", 0) == 1
                  and dw.get("p1p1", 0) == 0 and db.get("p1p1", 0) == 0)
            A["A4_exact_move"] = "passed" if ok else "failed"
            D["A4_exact_move"] = (f"warden delta={dw} {aw}->{bw}; "
                                  f"bears delta={db} {ab}->{bb}")
    elif A["A3_counter_prompted"] == "passed":
        A["A4_exact_move"] = "not-run"
        D["A4_exact_move"] = "pre/post proof states missing"
    else:
        A["A4_exact_move"] = "not-run"
        D["A4_exact_move"] = "no counter choice was offered (A3 failed)"

    # A5: default_documents (only meaningful if A3 failed)
    if A["A3_counter_prompted"] == "failed" and pre_p and post_p:
        dw, aw, bw = class_counter_delta(pre_p, post_p, 0, WARDEN)
        db, ab, bb = class_counter_delta(pre_p, post_p, 0, BEAR)
        moved = [k for k, v in (dw or {}).items() if v < 0]
        A["A5_default_documents"] = "passed"
        D["A5_default_documents"] = (
            f"engine default moved counter class(es) {moved}: "
            f"warden {aw}->{bw}, bears {ab}->{bb}")
    elif A["A3_counter_prompted"] == "failed":
        A["A5_default_documents"] = "not-run"
        D["A5_default_documents"] = "pre/post proof states missing"
    else:
        A["A5_default_documents"] = "not-run"
        D["A5_default_documents"] = "counter choice was offered (A3 passed)"

    # A6: cleanup
    if post_p:
        empty = not (post_p.get("stack") or [])
        A["A6_cleanup"] = "passed" if empty else "failed"
        D["A6_cleanup"] = (f"post_proof turn={post_p.get('turn_number')} "
                           f"phase={post_p.get('phase')} "
                           f"stack_empty={empty}")
    else:
        A["A6_cleanup"] = "not-run"
        D["A6_cleanup"] = "post_proof.json missing"

    # verdict
    if A["A1_setup_ok"] == "failed" or A["A1_setup_ok"] == "not-run":
        verdict = "blocked"
    elif A["A3_counter_prompted"] == "failed":
        verdict = "reproduced"
    elif A["A3_counter_prompted"] == "passed" and A["A4_exact_move"] == "failed":
        verdict = "reproduced"
    elif all(A[k] == "passed" for k in ("A1_setup_ok", "A2_control_move",
                                        "A3_counter_prompted",
                                        "A4_exact_move", "A6_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append(f"control-stage counter-like decisions: "
                 f"{len(control_counter_recs)}")
    return A, D, verdict


# ------------------------------------------------------------------- main

async def main():
    reset_state()
    t0 = time.time()
    notes = []
    ST["setup_t0"] = t0
    await verify_server_hello()
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
                        ("A1_setup_ok", "A2_control_move",
                         "A3_counter_prompted", "A4_exact_move",
                         "A5_default_documents", "A6_cleanup")},
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

        # SETUP abort: Warden never landed
        if ST["stage"] == "SETUP" and time.time() - t0 > SETUP_ABORT_S:
            await do_export(p0, "mid_setup.json")
            notes.append(f"SETUP abort: Warden not on BF after "
                         f"{SETUP_ABORT_S}s (turn "
                         f"{state.get('turn_number')}, hand="
                         f"{hand_lnames(state, 0)[:8]})")
            say("[setup-abort] Warden never landed; ending run")
            ST["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={state.get('turn_number')} "
                f"active={state.get('active_player')} phase={state.get('phase')} "
                f"wf={wf} stage={ST['stage']} "
                f"warden={ST['warden_oid']} ng1={ST['ng1_submitted']} "
                f"town={ST['town_submitted']} ng2={ST['ng2_submitted']} "
                f"tsels={len(ST['target_sels'])} "
                f"decisions={len(ST['decisions'])} "
                f"P0hand={hand_lnames(state, 0)[:6]}")

    if not env_state("post_proof.json") and not env_state("post_control.json"):
        try:
            await do_export(p0, "post.json")
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")

    ass, det, verdict = evaluate(notes)
    for k in sorted(ass):
        say(f"{k}: {ass[k]} -- {det.get(k, '')}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "details": det, "notes": notes,
                   "wf_sequence": WF_SEEN,
                   "target_sels": [
                       {k: r[k] for k in ("interactionId", "turn", "phase",
                                         "stage", "candidates")
                        if k in r} for r in ST["target_sels"]],
                   "decisions": [
                       {k: r[k] for k in ("interactionId", "turn", "phase",
                                         "stage", "counter_like",
                                         "is_card_choice")
                        if k in r} for r in ST["decisions"]],
                   "counter_choice_pick": ST.get("counter_choice_pick"),
                   "data_level_ok": data_ok}, f, indent=2)

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_6907.py", "w") as f:
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
        "scenario": "driver/scenario_6907_01030.py",
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"],
                  "target_sels": len(ST["target_sels"]),
                  "decisions": len(ST["decisions"])},
        "setup_line": ("P0: 4x Sanctuary Warden + 4x Grizzly Bears + 4x "
                       "Nesting Grounds + 4x Gavony Township + 44 lands; "
                       "P1: 60 lands (passive); native human seats"),
        "contract_line": ("CONTROL: activate Nesting Grounds with Warden "
                          "holding only shield counters (Warden -> Bears); "
                          "assert exactly one shield moves. PROOF_SETUP: "
                          "activate Gavony Township (Warden gains +1/+1). "
                          "PROOF: activate Nesting Grounds again; assert a "
                          "counter-type choice is offered and answering "
                          "'shield' moves exactly one shield counter."),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "4x densities are test-harness conveniences (engine accepts >4-of "
            "for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_6907.py (verified 2026-09-12, "
            "run 20260912-6907d, verdict reproduced).",
            "Merged_actions/vi conventions, NON_DECISION_CODES, vi "
            "tapLandForMana payment, PASSED_REV gating, Select-model "
            "hand-size discards, schema-select BottomCards, and the "
            "never-hold-priority-while-watching-the-stack fall-through rule "
            "taken from the proven scenario_6906_01030.py template.",
            "NG target slots answered per-prompt (source slot -> Warden, "
            "destination slot -> Bears); a schema opportunity with no card "
            "candidates but counter text is routed to the counter-choice "
            "handler. Counter choices prefer 'shield'.",
            "Pinned v0.103.0 data still parses the {1},{T} ability as "
            "MoveCounters with counter_type null -- see data_evidence.json.",
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
