#!/usr/bin/env python3
"""Issue #6771: Breakthrough card not working correctly.

Protocol-106 port (v0.102.0, 2026-10-05) of scenario_6771_v086.py
(verified 2026-09-18, run 20260918-6771, v0.86.0/protocol 72, REPRODUCED).

Reported (Discord): "it lets you draw 4 cards but doesn't make you
discard any"

Oracle (pinned v0.102.0 card-data.json), Breakthrough ({X}{U}, Sorcery):
  "Draw four cards, then choose X cards in your hand and discard the rest."

Parser state (v0.102.0 data): abilities[0].effect = Draw(Fixed 4,
Controller). The sub-abilities are Unimplemented: "unparsed_verb_arguments"
("choose X cards in your hand") -> "unparsed_verb_arguments"
("discard the rest"). Only the draw half of the spell is implemented.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, v0.102.0/protocol 106):
  P0: 12x Breakthrough + 48x Island.
      Mulligan: keep (12 copies of the spell, fine).
  P1: 60x Island dummy (plays a land, passes; never attacks/blocks).

Expected (per card text):
  RAMP: P0 plays an Island per turn; P1 plays a land, passes.
  ARM:  at P0's PreCombatMain, when a CastSpell-for-Breakthrough action is
        advertised in merged actions: export pre_cast, arm the cast.
  CAST: submit CastSpell; tap Islands via vi tapLandForMana
        (needs={U:1} then generic=X once X is announced). Breakthrough's X
        announcement comes as a ChooseXValue prompt: wire the raw 106
        opportunity, clamp the wanted X=2 into the advertised [min,max], and
        answer. Record the range, the answered value, and whether clamping
        happened.
  After the draw resolves: WATCH for any "choose X cards in your hand"
        hand-selection prompt. The bug says none appears. If such a prompt
        DOES appear, answer it keeping min(X_answered, hand) cards so
        resolution can complete and A5 can assert the final hand size.
        Wire the raw opportunity for the record.
  STOP: once Breakthrough is in P0's graveyard and the stack is empty:
        export post_cast, stop.

Assertions:
  A1 setup_ok            pre_cast: P0 PreCombatMain, Breakthrough in hand,
                         >=3 Islands on P0 battlefield, cast action was
                         engine-advertised at arm time.
  A2 x_announced         a ChooseXValue prompt was offered and answered
                         (clamped into [min,max]; value recorded).
  A3 draw_completed      post_cast: Breakthrough in P0 gy, P0 library -4 vs
                         pre, stack empty.
  A4 choose_prompted     a "choose X cards in hand" prompt appeared after
                         the draw (prior run: failed - parser unsupported).
  A5 discard_happened    post_cast: P0 hand size == X_answered (oracle);
                         bug expectation is pre-1+4 (no discard).
  A6 cleanup             post_cast: stack empty (resolution quiescent;
                         A3 establishes the game advanced past the cast).

Verdict = blocked iff A1 fails (setup never assembled) or the run stalls
          before the bug can be exercised (X prompt never answered -- A2
          fails): nothing to evaluate without the spell resolving.
Verdict = reproduced iff A1 and A2 pass and (A4 or A5) fails.
Verdict = not-reproduced iff A1..A6 all pass.
NEVER "fixed".

Evidence: evidence/6771/20261005-6771/pre_cast.json, post_cast.json,
run.json, assertions.json, data_evidence.json, manifest.sha256,
summary.png, scenario_6771.py, wire_log.jsonl, scenario_run.log,
server_excerpts.log
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

ISSUE = 6771
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261005-6771"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

SPELL = "breakthrough"
LAND = "island"

P0_DECK = [("Breakthrough", 12), ("Island", 48)]
P1_DECK = [("Island", 60)]

X_CHOSEN = 2
NEEDS_BREAKTHROUGH_U = {"U": 1}

ST = {"stage": "RAMP", "cast_armed": False, "cast_submitted": False,
      "cast_turn": None, "pre_turn": None, "castable_at_arm": False,
      "stop": False, "stall_since": None, "states_seen": 0,
      "mana_needs": {}, "x_stall": False}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}

X_SEEN = []             # interactionIds of answered X prompts
X_ANSWERED = False
X_ANSWERED_VALUE = None
X_RANGE = None
X_CLAMPED = None
X_LAST_SUBMIT = None    # (submit_time, revision) for failed-ix retry
CHOOSE_SEEN = []        # any hand-selection prompt observed after the draw
CHOOSE_RECORDED = set() # interactionIds already wired
WF_SEEN = []


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
    # covers Island and every other land the seats can hold
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


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def gy_named(state, pid, lname):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and obj_lname(state, oid) == lname]


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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def my_precombat(state, pid):
    return (state.get("phase") == "PreCombatMain"
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
    ok, notes = True, []
    g = CARD_DATA.get("breakthrough", {})
    oracle = str(g.get("oracle_text", ""))
    want_phrases = ("draw four cards",
                    "choose x cards in your hand",
                    "discard the rest")
    for ph in want_phrases:
        if ph not in oracle.lower():
            ok = False
            notes.append(f"oracle missing phrase: {ph!r}")
    payload = {}
    draw_ok = choose_unsupported = discard_unsupported = False
    abs_ = g.get("abilities") or []
    if abs_ and isinstance(abs_, list):
        eff = (abs_[0].get("effect") or {})
        cnt = (eff.get("count") or {})
        draw_ok = (eff.get("type") == "Draw"
                   and cnt.get("type") == "Fixed" and cnt.get("value") == 4
                   and (eff.get("target") or {}).get("type") == "Controller")
        payload["draw"] = {"type": eff.get("type"),
                           "count": cnt, "target": eff.get("target")}
        sub = abs_[0].get("sub_ability") or {}
        sub_eff = sub.get("effect") or {}
        choose_unsupported = (sub_eff.get("type") == "Unimplemented"
                              and "choose x cards in your hand"
                              in str(sub_eff.get("description", "")).lower())
        payload["choose_x"] = {"type": sub_eff.get("type"),
                               "description": sub_eff.get("description")}
        sub2 = sub.get("sub_ability") or {}
        sub2_eff = sub2.get("effect") or {}
        discard_unsupported = (sub2_eff.get("type") == "Unimplemented"
                               and "discard the rest"
                               in str(sub2_eff.get("description", "")).lower())
        payload["discard_rest"] = {"type": sub2_eff.get("type"),
                                   "description": sub2_eff.get("description")}
    if not draw_ok:
        ok = False
        notes.append("Draw(4, Controller) effect not in expected shape")
    if not choose_unsupported:
        ok = False
        notes.append("choose-X sub-effect unsupported marker missing")
    if not discard_unsupported:
        ok = False
        notes.append("discard-rest sub-effect unsupported marker missing")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "breakthrough_oracle": oracle,
                   "mana_cost": g.get("mana_cost"),
                   "spell_effect_payload": payload}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok


def mulligan_pending_for(state, pid):
    # MulliganDecision's waiting_for carries NO top-level data.player; pending
    # seats live in data.pending[] as {player, phase:{type:"Declare"}}.
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
    # bottom after a mulligan: bottom lands first (P0 wants spells in hand)
    count = bottom_pending_for(state, pid)
    if count and (tag, "bottom", f"rev{c.revision}") not in MULLS:
        hn = hand_ids(state, pid)

        def rank(o):
            return 0 if is_land(get_obj(state, o)) else 2
        want = sorted(hn, key=rank)[:count]
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            if not cands:
                continue
            idmap = {}
            for ch in cands:
                for s in ch.get("surfaces", []) or []:
                    d_ = s.get("data") or {}
                    if isinstance(d_, dict) and "reference" in d_:
                        try:
                            idmap[int(d_["reference"])] = ch["id"]
                        except (TypeError, ValueError):
                            pass
            picks = [idmap[o] for o in want if o in idmap] or \
                    [ch["id"] for ch in cands[:count]]
            rtype = resp.get("type")
            spec = (data.get("spec") or {}).get("type") or "sequence"
            if rtype == "schema":
                sub = {"interactionId": opp.get("interactionId"),
                       "response": {"type": spec,
                                    "data": {"choiceIds": picks}}}
            else:
                sub = {"interactionId": opp.get("interactionId"),
                       "response": {"type": "sequence",
                                    "data": {"choiceIds": picks}}}
            MULLS.add((tag, "bottom", f"rev{c.revision}"))
            say(f"[{tag}] bottoms {count} after mulligan")
            wire("bottom_cards", {"who": tag, "picks": picks})
            await interact_as(c, sub, tag)
            return True
        sc = next((a for a in acts
                   if a.get("type") in ("SelectCards", "BottomCards")), None)
        if sc:
            data = dict(sc.get("data", {}))
            key2 = "cards" if "cards" in data else \
                ("cardIds" if "cardIds" in data else "cards")
            MULLS.add((tag, "bottom", f"rev{c.revision}"))
            wire("action_submit", {"who": tag, "action": "SelectCards/bottom",
                                   "picks": want})
            await c.send_action({"type": sc["type"],
                                 "data": {key2: [int(x) for x in want]}})
            say(f"[{tag}] bottoms {count} after mulligan (action)")
            return True
    return False


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_discard_to_handsize(c, acts, st, pid, tag, keep_lands):
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

        def rank(o):
            nm = obj_lname(state, o)
            if keep_lands:
                if is_land(get_obj(state, o)):
                    return (2, nm)
                return (0, nm)
            return (0, nm)

        picks = [ch["id"] for ch in
                 sorted(cands,
                        key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


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


def can_pay(state, pid, needs):
    pool = [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")]
    return len(pool) >= sum(needs.values())


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


def x_prompt_shape(opp):
    """Compact description of the 106 ChooseXValue opportunity."""
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    return {"interactionId": opp.get("interactionId"),
            "description": str(opp.get("description", ""))[:200],
            "response_type": resp.get("type"),
            "spec_type": spec.get("type"),
            "spec_data": spec.get("data")}


async def answer_x_value(c, pid, st, tag):
    """Breakthrough's announced-X prompt on 106: check the vi opportunity
    shape live, wire it raw, clamp the wanted X=2 into the advertised
    [min,max], and answer. The range can be degenerate when available mana
    admits no larger X (v086 lesson); answering outside the range is
    silently rejected and moves no revision."""
    global X_ANSWERED, X_ANSWERED_VALUE, X_RANGE, X_CLAMPED, X_LAST_SUBMIT
    wf = (st["state"].get("waiting_for") or {})
    if wf.get("type") == "ChooseXValue":
        wire("wf_choose_x", {"data": wf.get("data")})
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if iid in X_SEEN:
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        stype = spec.get("type") or rtype
        if stype != "number":
            continue
        wire("x_opportunity", {"opp": opp, "wf_data": wf.get("data"),
                               "shape": x_prompt_shape(opp)})
        sdata = spec.get("data") or {}
        wfd = wf.get("data") or {}
        lo = sdata.get("min", wfd.get("min", 0))
        hi = sdata.get("max", wfd.get("max", lo))
        lo = 0 if lo is None else int(lo)
        hi = lo if hi is None else int(hi)
        val = max(lo, min(X_CHOSEN, hi))
        clamped = val != X_CHOSEN
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "number", "data": {"value": val}}}
        say(f"[{tag}] Breakthrough X prompt range [{lo},{hi}]; "
            f"answering X={val} (wanted {X_CHOSEN}, clamped={clamped})")
        wire("x_answer", {"iid": iid, "x": val, "clamped": clamped,
                          "range": [lo, hi]})
        X_SEEN.append(iid)
        await interact_as(c, sub, tag)
        X_LAST_SUBMIT = (time.time(), c.revision)
        X_ANSWERED = True
        X_RANGE = (lo, hi)
        X_CLAMPED = clamped
        X_ANSWERED_VALUE = val
        return True
    return False


def looks_like_hand_keep(opp):
    """Heuristic: opportunity asking to select cards from hand (the
    'choose X cards in your hand' half of Breakthrough)."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    data = resp.get("data") or {}
    text = json.dumps(opp, default=str).lower()
    if "choose" in text and ("hand" in text or "keep" in text):
        return True
    if rtype == "schema" and (data.get("spec") or {}).get("type") in ("select", "sequence"):
        for ch in data.get("candidates", []) or []:
            for s in ch.get("surfaces", []) or []:
                dd = s.get("data") or {}
                if str(dd.get("zone", "")).lower() == "hand":
                    return True
    return False


def select_response_type(opp):
    resp = opp.get("response") or {}
    spec = (resp.get("data") or {}).get("spec") or {}
    return spec.get("type") or resp.get("type") or "select"


async def answer_hand_keep(c, pid, st, tag):
    """If the engine offers a choose-X-cards-in-hand prompt after the draw,
    keep min(X_answered, hand) cards so resolution can complete and A5 can
    assert the final hand size."""
    if not ST["cast_submitted"] or ST["stop"]:
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if iid not in CHOOSE_RECORDED:
            CHOOSE_RECORDED.add(iid)
            wire("opp_after_cast", {"iid": iid, "opp": opp})
        if iid in CHOOSE_SEEN:
            continue
        if not looks_like_hand_keep(opp):
            continue
        CHOOSE_SEEN.append(iid)
        say(f"[{tag}] hand-keep prompt observed: {iid}")
        wire("hand_keep_prompt_seen", {"iid": iid})
        rtype = select_response_type(opp)
        if rtype in ("select", "sequence"):
            state = st["state"]
            oids = sorted(hand_ids(state, pid))
            nkeep = X_ANSWERED_VALUE if X_ANSWERED_VALUE is not None else X_CHOSEN
            nkeep = min(nkeep, len(oids))
            keep = [str(o) for o in oids[:nkeep]]
            sub = {"interactionId": opp.get("interactionId"),
                   "response": {"type": rtype, "data": {"choiceIds": keep}}}
            wire("keep_answer", {"iid": iid, "keep": keep})
            await interact_as(c, sub, tag)
            say(f"[{tag}] answers hand-keep: keep {keep}")
            return True
        say(f"[{tag}] hand-keep prompt has unhandled response type {rtype}")
        wire("hand_keep_unhandled_type", {"iid": iid, "rtype": rtype})
        return True
    return False


def check_failed_ix(c):
    """A rejected interaction moves no revision. If none advanced within
    ~12s of the X submission, reset the answered ids so the prompt is
    answered again."""
    global X_LAST_SUBMIT, X_ANSWERED
    li = X_LAST_SUBMIT
    if not li:
        return
    t, rev = li
    if time.time() - t > 12 and c.revision == rev:
        wire("ix_retry", {"kind": "x", "rev": rev})
        say("[retry] no revision advance 12s after X submission; "
            "resetting answered ids")
        X_SEEN.clear()
        X_ANSWERED = False
        X_LAST_SUBMIT = None


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
    if await do_discard_to_handsize(c, acts, st, 1, tag, keep_lands=False):
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
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 0, tag, keep_lands=False):
        return
    if ST["stage"] == "CASTING":
        if await answer_x_value(c, 0, st, tag):
            return
        if await answer_hand_keep(c, 0, st, tag):
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
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    if ST["stage"] == "RAMP" and my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return
    if ST["stage"] == "CASTING" and ST["cast_armed"] \
            and not ST["cast_submitted"]:
        a, oid = cast_action_for(acts, state, SPELL)
        if a is not None:
            ST["cast_turn"] = state.get("turn_number")
            say(f"[{tag}] casting Breakthrough (oid {oid})")
            wire("cast_breakthrough", {"oid": oid})
            await submit_as_is(c, a)
            ST["cast_submitted"] = True
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------------- main

async def main():
    global X_ANSWERED
    t0 = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_x_announced", "A3_draw_completed",
            "A4_choose_prompted", "A5_discard_happened", "A6_cleanup")}

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
    progress_rev = -1
    progress_at = t0
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
        check_failed_ix(p0)
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        ST["states_seen"] += 1

        wf = (state.get("waiting_for") or {}).get("type")
        if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
            WF_SEEN.append(wf)
            wire("waiting_for", {"type": wf,
                                 "data": (state.get("waiting_for") or {}).get("data")})

        # generic progress watchdog: no revision advance at all for 180s
        if p0.revision != progress_rev or p1.revision != progress_rev:
            progress_rev = max(p0.revision, p1.revision)
            progress_at = time.time()
        if time.time() - progress_at > 180:
            await do_export(p0, "mid_stall.json")
            notes.append("generic stall watchdog: no revision advance >180s")
            say("[stall] generic watchdog fired")
            ST["stop"] = True
            continue

        # ARM: P0 PreCombatMain, cast advertised in merged actions
        if ST["stage"] == "RAMP" and my_precombat(state, 0):
            acts = merged_actions(st)
            a, oid = cast_action_for(acts, state, SPELL)
            if a is not None and can_pay(state, 0, {"U": 1, "generic": X_CHOSEN}):
                pre = await do_export(p0, "pre_cast.json")
                ST["pre_turn"] = pre.get("turn_number")
                ST["cast_armed"] = True
                ST["castable_at_arm"] = True
                ST["stage"] = "CASTING"
                ST["mana_needs"]["P0"] = dict(NEEDS_BREAKTHROUGH_U)
                hand = hand_ids(pre, 0)
                wire("pre_cast", {"hand": len(hand),
                                  "library": lib_count(pre, 0),
                                  "turn": ST["pre_turn"],
                                  "islands_bf": len(bf_by_name(pre, 0, LAND))})
                say(f"[armed] pre_cast exported turn {ST['pre_turn']} "
                    f"hand={len(hand)} lib={lib_count(pre, 0)} "
                    f"islands={len(bf_by_name(pre, 0, LAND))}")

        # RAMP abort: the arm needs a PreCombatMain with the cast
        # advertised and X=2 affordable. Fail fast instead of ramping forever.
        if ST["stage"] == "RAMP" and time.time() - t0 > 420:
            await do_export(p0, "mid_ramp.json")
            hand = hand_ids(state, 0)
            notes.append(f"RAMP abort: cast never armed after 420s; hand "
                         f"size={len(hand)} (turn {state.get('turn_number')}, "
                         f"phase {state.get('phase')})")
            say("[ramp-abort] cast never armed; ending run")
            ST["stop"] = True
            continue

        # stall watchdogs
        if ST["cast_armed"] and not ST["cast_submitted"]:
            if ST["stall_since"] is None:
                ST["stall_since"] = time.time()
            elif time.time() - ST["stall_since"] > 120:
                await do_export(p0, "mid_stall.json")
                notes.append("stall watchdog: cast armed but never "
                             "submitted after 120s")
                say("[stall] cast-submission watchdog fired")
                ST["stop"] = True
        elif ST["cast_submitted"] and not X_ANSWERED:
            if ST["stall_since"] is None:
                ST["stall_since"] = time.time()
            elif time.time() - ST["stall_since"] > 120:
                await do_export(p0, "mid_stall.json")
                notes.append("stall watchdog: cast submitted but X never "
                             "answered after 120s")
                say("[stall] X-answer watchdog fired")
                ST["x_stall"] = True
                ST["stop"] = True
        else:
            ST["stall_since"] = None

        # X answered -> add the generic portion of the cost to the needs
        # (keep whatever of the {U} needs has already been satisfied)
        needs_p0 = ST["mana_needs"].get("P0")
        if X_ANSWERED and needs_p0 is not None \
                and "generic" not in needs_p0:
            needs_p0["generic"] = X_ANSWERED_VALUE or 0
            say(f"[X] generic cost now {X_ANSWERED_VALUE}; needs={needs_p0}")

        # post_cast: resolution observed (spell in gy, stack empty)
        if ST["cast_submitted"] and not ST["stop"] \
                and gy_named(state, 0, SPELL) and not (state.get("stack") or []):
            await asyncio.sleep(0.75)
            post = await do_export(p0, "post_cast.json")
            wire("post_cast", {"hand": len(hand_ids(post, 0)),
                               "library": lib_count(post, 0),
                               "turn": post.get("turn_number"),
                               "phase": post.get("phase"),
                               "x_answered": X_ANSWERED,
                               "x_value": X_ANSWERED_VALUE,
                               "hand_keep_prompts": len(CHOOSE_SEEN)})
            say(f"[post] post_cast exported turn {post.get('turn_number')} "
                f"phase={post.get('phase')} hand={len(hand_ids(post, 0))} "
                f"lib={lib_count(post, 0)} x={X_ANSWERED_VALUE}")
            ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre_cast.json")
    post = env_state("post_cast.json")

    # A1
    if pre is None:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre_cast.json never exported")
    else:
        ok_main = pre.get("active_player") == 0 \
            and pre.get("phase") == "PreCombatMain"
        ok_hand = any(obj_lname(pre, o) == SPELL for o in hand_ids(pre, 0))
        ok_castable = bool(ST.get("castable_at_arm"))
        ok_lands = len(bf_by_name(pre, 0, LAND)) >= 3
        ok = ok_main and ok_hand and ok_castable and ok_lands
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"pre_cast: main={ok_main} spell_in_hand={ok_hand} "
                     f"cast_advertised={ok_castable} islands_bf={len(bf_by_name(pre, 0, LAND))} "
                     f"hand={len(hand_ids(pre, 0))} lib={lib_count(pre, 0)}")

    # A2
    if X_ANSWERED:
        ass["A2_x_announced"] = "passed"
    else:
        ass["A2_x_announced"] = "failed"
    notes.append(f"x prompt offered & answered: {X_ANSWERED} "
                 f"value={X_ANSWERED_VALUE} (wanted {X_CHOSEN}) "
                 f"range={X_RANGE} clamped={X_CLAMPED} (iids={X_SEEN})")
    if ST.get("x_stall"):
        notes.append("X-answer stall watchdog fired: the ChooseXValue prompt "
                     "was never answered; the bug could not be exercised.")

    # A3
    if pre is None or post is None:
        ass["A3_draw_completed"] = "not-run"
        notes.append("pre/post pair incomplete; A3 not-run")
    else:
        in_gy = len(gy_named(post, 0, SPELL)) >= 1
        lc0, lc1 = lib_count(pre, 0), lib_count(post, 0)
        drew = (lc0 is not None and lc1 is not None and lc0 - lc1 == 4)
        empty_stack = not (post.get("stack") or [])
        obs_note = (f"draw check: spell_in_gy={in_gy} lib {lc0}->{lc1} "
                    f"(delta={lc0 - lc1 if lc0 and lc1 else '?'}) "
                    f"stack_empty={empty_stack}")
        notes.append(obs_note)
        ass["A3_draw_completed"] = \
            "passed" if (in_gy and drew and empty_stack) else "failed"

    # A4
    if post is None:
        ass["A4_choose_prompted"] = "not-run"
        notes.append("post_cast.json missing; A4 not-run")
    else:
        ass["A4_choose_prompted"] = "passed" if CHOOSE_SEEN else "failed"
        notes.append(f"hand-keep 'choose X' prompt observed after draw: "
                     f"{bool(CHOOSE_SEEN)} (count={len(CHOOSE_SEEN)})")

    # A5
    if pre is None or post is None:
        ass["A5_discard_happened"] = "not-run"
        notes.append("pre/post pair incomplete; A5 not-run")
    else:
        h0, h1 = len(hand_ids(pre, 0)), len(hand_ids(post, 0))
        expect_kept = X_ANSWERED_VALUE \
            if X_ANSWERED_VALUE is not None else X_CHOSEN
        expect_bug = h0 - 1 + 4  # cast from hand, draw 4, nothing discarded
        obs_note = (f"hand: pre={h0} post={h1}; oracle with X={expect_kept} "
                    f"expects {expect_kept}; bug expects {expect_bug}")
        notes.append(obs_note)
        ass["A5_discard_happened"] = "passed" if h1 == expect_kept else "failed"

    # A6 -- v086 semantics: resolution quiescent (stack empty). A3 already
    # establishes the game advanced past the cast (spell in gy, library -4).
    if post is not None:
        empty = not (post.get("stack") or [])
        ass["A6_cleanup"] = "passed" if empty else "failed"
        notes.append(f"post_cast: phase={post.get('phase')} "
                     f"turn={post.get('turn_number')} stack_empty={empty}")
    else:
        ass["A6_cleanup"] = "not-run"
        notes.append("post_cast.json missing; A6 not-run")

    for k in sorted(ass):
        say(f"{k}: {ass[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN,
                   "x_answered": X_ANSWERED, "x_value": X_ANSWERED_VALUE,
                   "x_range": X_RANGE, "x_clamped": X_CLAMPED,
                   "hand_keep_prompts": CHOOSE_SEEN}, f, indent=2)

    verdict = "blocked"
    if ass.get("A1_setup_ok") == "passed":
        if ass.get("A2_x_announced") != "passed":
            verdict = "blocked"
            notes.append("A2 failed: X prompt never answered -- the spell "
                         "never resolved; verdict blocked, not a bug signal.")
        elif ass.get("A4_choose_prompted") == "failed" \
                or ass.get("A5_discard_happened") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
    say("verdict:", verdict)

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_6771.py", "w") as f:
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
        "setup_line": ("P0: 12x Breakthrough + 48x Island; P1: 60x Island "
                       "dummy; native human seats"),
        "contract_line": ("cast Breakthrough ({X}{U}, X=2 wanted), answer "
                          "the ChooseXValue prompt clamped into the advertised "
                          "range, tap mana via vi; assert the draw completes "
                          "and the 'choose X cards in your hand, discard the "
                          "rest' half fires (reported broken)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Breakthrough deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_6771_v086.py (verified 2026-09-18, "
            "run 20260918-6771).",
            "Merged_actions/vi conventions, NON_DECISION_CODES incl. playLand, "
            "vi tapLandForMana payment, and PASSED_REV gating taken from the "
            "proven 20261005-6770 template.",
            "Breakthrough's X announced via a 106 number-schema vi "
            "opportunity (wired raw); X=2 wanted, clamped into the advertised "
            "[min,max]; the generic cost is paid after the answer lands.",
            "If the X prompt is never answered the run cannot exercise the "
            "bug: verdict stays blocked rather than misfiring 'reproduced'.",
            "Pinned data parses only Draw(Fixed 4); 'choose X cards in your "
            "hand' and 'discard the rest' remain Unimplemented "
            "(unparsed_verb_arguments) in v0.102.0 -- see data_evidence.json.",
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
