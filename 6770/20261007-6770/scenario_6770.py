#!/usr/bin/env python3
"""Issue #6770: Manabond card not working correctly.

Protocol-106 run (v0.103.0, 2026-10-07); ports 20261005-6770 (v0.102.0)
(verified 2026-09-18, runs 20260918-6770 (v0.86.0/proto 72) and 20261005-6770 (v0.102.0/proto 106), REPRODUCED).

Reported (Discord): "it triggers at the end of the turn, discard correctly
the hand but put also the lands on the grave instead of battlefield"

Oracle (pinned v0.103.0 card-data.json, verified 2026-10-07):
  Manabond ({G}, Enchantment):
    "At the beginning of your end step, you may reveal your hand and put all
     land cards from it onto the battlefield. If you do, discard your hand."
Parser state (v0.103.0 data): trigger mode Phase/End, optional=true.
  RevealHand (target Any) -> ChangeZoneAll (Typed Land, controller=null,
  origin=null -> Battlefield) -> Discard (HandSize of Controller,
  conditioned on OptionalEffectPerformed). The ChangeZoneAll target still
  records only `land` with no hand zone / controller scope, and the discard
  counts the controller's current hand -- the shape that previously moved
  the lands to the graveyard instead of the battlefield.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, v0.102.0/protocol 106):
  P0: 12x Manabond + 12x Grizzly Bears + 36x Forest.
      Mulligan: keep hands with >=1 land, mulligan up to 3 times if 0 lands;
      BottomCards: bottom non-lands first (Bears/Manabond before lands).
  P1: 60x Forest dummy (plays a land, passes; never attacks/blocks).

Expected (per card text):
  RAMP: P0 plays Forest, casts Manabond turn 1 ({G} via tapped Forests).
        DECLINES every Manabond may-choice.
  DECLINE: at the first P0 end step where the hand holds >=3 lands and
        >=1 non-land: export pre_decline, DECLINE the may-choice (bare
        "false", per the #7195 driver lesson), export post_decline once the
        stack empties (before the cleanup discard).
  ACCEPT: at the NEXT P0 end step: export pre_accept, ACCEPT the may-choice
        ("true"), export post_accept once P1's turn begins (active_player==1
        and turn advanced) after the discard resolves.
  STOP after post_accept.

Assertions:
  A1 setup_ok            pre_decline: Manabond on P0 BF, hand >=3 lands + >=1 Bear.
  A2 decline_no_move     post_decline: same hand objects, same BF land count.
  A3 accept_lands_battlefield  every land object in pre_accept P0 hand is on
        P0's battlefield in post_accept (the reported outcome is their
        absence there), and none of them is in the graveyard.
  A4 accept_rest_discarded     every non-land object in pre_accept P0 hand is
        in P0's graveyard in post_accept.
  A5 cleanup             post_accept: stack empty, game advanced past the turn.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and (A3 or A4) fails.
Verdict = not-reproduced iff A1..A5 all pass.
NEVER "fixed".

Evidence: evidence/6770/20261007-6770/pre_decline.json, post_decline.json,
pre_accept.json, post_accept.json, run.json, assertions.json, data_evidence.json,
manifest.sha256, summary.png, scenario_6770.py, wire_log.jsonl, scenario_run.log,
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
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

ISSUE = 6770
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261007-6770"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

MANABOND = "manabond"
BEAR = "grizzly bears"
FOREST = "forest"

P0_DECK = [("Manabond", 12), ("Grizzly Bears", 12), ("Forest", 36)]
P1_DECK = [("Forest", 60)]

NEEDS_MANABOND = {"G": 1}

END_PHASES = ("End", "EndStep")
POST_END_PHASES = ("End", "EndStep", "Cleanup")

ST = {"stage": "RAMP", "stop": False, "accept_turn": None,
      "decline_turn": None, "decline_exported": False,
      "accept_exported": False, "accept_answered": False,
      "stall_since": None, "states_seen": 0, "mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


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
    # covers Forest and every other land the seats can hold
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


def life(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
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


def may_choice_opp(st):
    """Return the vi opportunity carrying the Manabond decideOptionalEffect
    may-choice, or None. On 106 the choices are bare true/false."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return opp
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
    ws = await websockets.connect(URL, max_size=1_000_000)
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
    ok, notes = True, []
    g = CARD_DATA.get(MANABOND, {})
    oracle = str(g.get("oracle_text", ""))
    if "beginning of your end step" not in oracle.lower() \
            or "land cards from it onto the battlefield" not in oracle.lower() \
            or "discard your hand" not in oracle.lower():
        ok = False
        notes.append("manabond oracle text shape missing")
    payload_ok = False
    payload_summary = None
    for t in g.get("triggers", []) or []:
        ex = t.get("execute") or {}
        if str(t.get("mode", "")).lower() == "phase":
            eff = ex.get("effect") or {}
            if eff.get("type") == "RevealHand":
                sub = (ex.get("sub_ability") or {}).get("effect") or {}
                sub2 = ((ex.get("sub_ability") or {}).get("sub_ability")
                        or {}).get("effect") or {}
                payload_ok = (sub.get("type") == "ChangeZoneAll"
                              and sub.get("destination") == "Battlefield"
                              and sub2.get("type") == "Discard")
                payload_summary = {
                    "reveal": eff.get("type"),
                    "move": {"type": sub.get("type"),
                             "destination": sub.get("destination"),
                             "target": sub.get("target")},
                    "discard": {"type": sub2.get("type"),
                                "condition": sub2.get("condition")},
                    "optional": ex.get("optional"),
                }
    if not payload_ok:
        ok = False
        notes.append("manabond trigger effect payload missing from parsed data")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "manabond_oracle": oracle,
                   "mana_cost": g.get("mana_cost"),
                   "trigger_payload": payload_summary,
                   "raw_triggers": g.get("triggers")}, f, indent=1)
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
        hn = hand_ids(state, pid)
        lands = sum(1 for o in hn if is_land(get_obj(state, o)))
        mulls = ST.setdefault("mulls_" + tag, 0)
        adv = next((a for a in acts if a.get("type") == "MulliganDecision"),
                   None)
        if adv is None:
            return False
        MULLS.add(key)
        if lands == 0 and mulls < 3:
            ST["mulls_" + tag] = mulls + 1
            say(f"[{tag}] mulligans #{mulls + 1} (0 lands)")
            wire("mulligan", {"who": tag, "decision": "mulligan",
                              "n": mulls + 1})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
        else:
            MULLS.add((tag, "keep"))
            say(f"[{tag}] keeps (lands={lands})")
            wire("mulligan", {"who": tag, "decision": "keep",
                              "hand": [obj_lname(state, o) for o in hn]})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
        return True
    # bottom after a mulligan: bottom non-lands first
    count = bottom_pending_for(state, pid)
    if count and (tag, "bottom", f"rev{c.revision}") not in MULLS:
        hn = hand_ids(state, pid)

        def rank(o):
            nm = obj_lname(state, o)
            if nm == BEAR:
                return 0
            if nm == MANABOND:
                return 1
            return 2
        want = sorted(hn, key=rank)[:count]
        # prefer vi select surface
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
        # fallback: advertised SelectCards/BottomCards action
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
                # discard Bears/Manabond first, keep lands for the test
                if nm == BEAR:
                    return (0, nm)
                if nm == MANABOND:
                    return (1, nm)
                return (2, nm)
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


def manabond_prompt_shape(opp):
    data = (opp.get("response") or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    return [(ch.get("id"), choice_text(ch)[:60],
             [[s.get("type"), (s.get("data") or {}).get("role"),
               (s.get("data") or {}).get("value")]
              for s in ch.get("surfaces", []) or []])
            for ch in chs]


async def answer_manabond_may(c, st, want, tag):
    """Answer the Manabond decideOptionalEffect may-choice with want
    ('true'/'false'). On 106 the choices are bare literal true/false with
    (value, accept, true|false) surfaces -- pick via role=accept/value with
    text fallbacks, per the #7195 driver lesson."""
    opp = may_choice_opp(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    if iid in SUBMITTED_OPPS:
        return True
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    wire("manabond_prompt_shape",
         {"iid": iid, "want": want, "choices": manabond_prompt_shape(opp)})
    pick = None
    for ch in chs:
        for s in ch.get("surfaces", []) or []:
            d_ = s.get("data") or {}
            if (str(d_.get("value", "")).lower() == want
                    and str(d_.get("role", "")).lower()
                    in ("accept", "pay", "decision", "copy", "value")):
                pick = ch["id"]
                break
        if pick:
            break
    if pick is None:
        for ch in chs:
            t = choice_text(ch).lower().strip()
            if want == "false" and (t in ("false", "no", "decline")
                                    or "decline" in t or "don't" in t
                                    or "do not" in t):
                pick = ch["id"]
                break
            if want == "true" and (t in ("true", "yes", "accept")
                                   or "accept" in t):
                pick = ch["id"]
                break
    if pick is None:
        say(f"[{tag}] manabond may: no {want} choice identified; deferring")
        wire("manabond_no_choice_found",
             {"want": want,
              "choices": [(ch.get("id"), choice_text(ch)) for ch in chs]})
        return True
    SUBMITTED_OPPS.add(iid)
    say(f"[{tag}] Manabond may-choice answered {want} (choice {pick})")
    wire("manabond_answered", {"iid": iid, "want": want, "choice": pick})
    rtype = resp.get("type")
    if rtype == "schema":
        spec = (data.get("spec", {}) or {}).get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": spec, "data": {"choiceIds": [pick]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": pick}}}
    await interact_as(c, sub, tag)
    return True


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
    # cleanup discard: Bears/Manabond first, keep lands for the test
    if await do_discard_to_handsize(c, acts, st, 0, tag, keep_lands=True):
        return

    # Manabond may-choice: RAMP always declines; DECLINE/ACCEPT hold the
    # answer until the main loop has exported the pre-decision state.
    # Answering first would let the engine resolve before the export.
    opp = may_choice_opp(st)
    if opp is not None:
        if ST["stage"] == "RAMP":
            if await answer_manabond_may(c, st, "false", tag):
                return
        elif ST["stage"] == "DECLINE" and ST["decline_exported"]:
            if await answer_manabond_may(c, st, "false", tag):
                return
        elif ST["stage"] == "ACCEPT" and ST["accept_exported"]:
            if await answer_manabond_may(c, st, "true", tag):
                ST["accept_answered"] = True
                return
        else:
            wire("may_hold_for_pre_export", {"stage": ST["stage"]})
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
        if bf_by_name(state, 0, MANABOND):
            ST["mana_needs"][tag] = {}
        elif await pay_mana_vi(c, st, tag, needs):
            return

    if my_main(state, 0) and ST["stage"] == "RAMP":
        if await play_a_land(c, state, 0, acts, tag):
            return
        # cast Manabond ({G}) once
        if not bf_by_name(state, 0, MANABOND) \
                and any(obj_lname(state, o) == MANABOND
                        for o in hand_ids(state, 0)) \
                and can_pay(state, 0, NEEDS_MANABOND):
            a, oid = cast_action_for(acts, state, MANABOND)
            if a is not None:
                ST["mana_needs"][tag] = dict(NEEDS_MANABOND)
                say(f"[{tag}] casting Manabond (oid {oid})")
                wire("cast_manabond", {"oid": oid})
                await submit_as_is(c, a)
                return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------------- main

async def main():
    t0 = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_decline_no_move",
            "A3_accept_lands_battlefield", "A4_accept_rest_discarded",
            "A5_cleanup")}

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
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        ST["states_seen"] += 1

        def hand_counts(s):
            h = hand_ids(s, 0)
            lands = [x for x in h if is_land(get_obj(s, x))]
            nonl = [x for x in h if not is_land(get_obj(s, x))]
            return h, lands, nonl

        # RAMP -> DECLINE: armed when setup complete at PostCombatMain
        if ST["stage"] == "RAMP" \
                and state.get("active_player") == 0 \
                and (state.get("phase") or "") == "PostCombatMain" \
                and bf_by_name(state, 0, MANABOND):
            h, lands, nonl = hand_counts(state)
            if len(lands) >= 3 and len(nonl) >= 1:
                ST["stage"] = "DECLINE"
                say(f"[decline-armed] turn {state.get('turn_number')} hand "
                    f"lands={len(lands)} nonlands={len(nonl)}")

        may_pending = may_choice_opp(st) is not None

        # RAMP abort: the DECLINE arming needs >=3 lands + >=1 non-land in
        # hand at a P0 PostCombatMain. With a 3-land opener, hand lands bleed
        # down (1 land played/turn vs ~0.6 drawn) and the arm can never
        # recover -- the 20260918-6770 run armed on turn 1 off a 4-land
        # opener. Fail fast instead of declining forever.
        if ST["stage"] == "RAMP" and time.time() - t0 > 420:
            await do_export(p0, "mid_ramp.json")
            h, lands, nonl = hand_counts(state)
            notes.append(f"RAMP abort: DECLINE never armed after 420s; "
                         f"hand lands={len(lands)} nonlands={len(nonl)} "
                         f"(turn {state.get('turn_number')}, phase "
                         f"{state.get('phase')})")
            say("[ramp-abort] setup never armed; ending run")
            ST["stop"] = True
            continue

        # stall watchdog: Manabond may-choice unanswered >90s
        if may_pending:
            if ST["stall_since"] is None:
                ST["stall_since"] = time.time()
            elif time.time() - ST["stall_since"] > 90:
                await do_export(p0, "mid_stall.json")
                notes.append("stall watchdog: Manabond may-choice "
                             "unanswered >90s")
                say("[stall] watchdog fired")
                ST["stop"] = True
        else:
            ST["stall_since"] = None

        # DECLINE branch: pre export while the choice is pending in the end
        # step (tick holds the answer until this export lands)
        if ST["stage"] == "DECLINE" and not ST["decline_exported"] \
                and may_pending \
                and (state.get("phase") or "") in END_PHASES:
            pre = await do_export(p0, "pre_decline.json")
            ST["decline_turn"] = pre.get("turn_number")
            h, lands, nonl = hand_counts(pre)
            wire("pre_decline", {"hand": h, "lands": lands, "nonlands": nonl,
                                 "turn": ST["decline_turn"]})
            ST["decline_exported"] = True
            say(f"[pre_decline] turn {ST['decline_turn']}: lands={len(lands)} "
                f"nonlands={len(nonl)}")
        # post export: stack empty in the end step, BEFORE the cleanup
        # discard can move anything (so A2 compares decline-only movement)
        if ST["stage"] == "DECLINE" and ST["decline_exported"] \
                and not (state.get("stack") or []) \
                and (state.get("phase") or "") in END_PHASES \
                and not may_pending:
            await asyncio.sleep(0.75)
            post = await do_export(p0, "post_decline.json")
            ST["stage"] = "ACCEPT"
            say(f"[post_decline] phase={post.get('phase')} "
                f"turn={post.get('turn_number')}; arming ACCEPT")

        # ACCEPT branch: pre export while the choice is pending, then answer
        # TRUE next tick
        if ST["stage"] == "ACCEPT" and not ST["accept_exported"] \
                and may_pending \
                and (state.get("phase") or "") in END_PHASES:
            pre = await do_export(p0, "pre_accept.json")
            ST["accept_turn"] = pre.get("turn_number")
            h, lands, nonl = hand_counts(pre)
            wire("pre_accept", {"hand": h, "lands": lands, "nonlands": nonl,
                                "turn": ST["accept_turn"]})
            ST["accept_exported"] = True
            say(f"[pre_accept] turn {ST['accept_turn']}: lands={len(lands)} "
                f"nonlands={len(nonl)}")
        # post export: first P1 turn after the accept was answered (trigger
        # resolved, discard done, game advanced)
        if ST["stage"] == "ACCEPT" and ST["accept_answered"] \
                and state.get("active_player") == 1 \
                and (state.get("turn_number") or 0) > (ST.get("accept_turn") or 0):
            await asyncio.sleep(0.75)
            post = await do_export(p0, "post_accept.json")
            wire("post_accept", {"phase": post.get("phase"),
                                 "turn": post.get("turn_number")})
            say(f"[post_accept] phase={post.get('phase')} "
                f"turn={post.get('turn_number')}")
            ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre_d = env_state("pre_decline.json")
    post_d = env_state("post_decline.json")
    pre_a = env_state("pre_accept.json")
    post_a = env_state("post_accept.json")

    # A1
    if pre_d is None:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre_decline.json never exported")
    else:
        mb = bf_by_name(pre_d, 0, MANABOND)
        hd = hand_ids(pre_d, 0)
        ln = [x for x in hd if is_land(get_obj(pre_d, x))]
        nl = [x for x in hd if not is_land(get_obj(pre_d, x))]
        ok = (len(mb) >= 1 and len(ln) >= 3 and len(nl) >= 1)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"pre_decline: manabond_bf={len(mb)} "
                     f"hand_lands={len(ln)} hand_nonlands={len(nl)} "
                     f"life={[life(pre_d, 0), life(pre_d, 1)]}")

    # A2: decline moved nothing
    if pre_d is None or post_d is None:
        ass["A2_decline_no_move"] = "not-run"
        notes.append("decline pre/post pair incomplete; A2 not-run")
    else:
        h0 = sorted(int(x) for x in hand_ids(pre_d, 0))
        h1 = sorted(int(x) for x in hand_ids(post_d, 0))
        bf0 = sorted(bf_by_name(pre_d, 0, FOREST))
        bf1 = sorted(bf_by_name(post_d, 0, FOREST))
        same = (h0 == h1 and bf0 == bf1)
        ass["A2_decline_no_move"] = "passed" if same else "failed"
        notes.append(f"decline: hand_same={h0 == h1} bf_lands_same={bf0 == bf1}")

    # A3/A4: accept branch zone changes
    if pre_a is None or post_a is None:
        ass["A3_accept_lands_battlefield"] = "not-run"
        ass["A4_accept_rest_discarded"] = "not-run"
        notes.append("accept pre/post pair incomplete; A3/A4 not-run")
    else:
        ha = hand_ids(pre_a, 0)
        lands_a = [int(x) for x in ha if is_land(get_obj(pre_a, x))]
        nonl_a = [int(x) for x in ha if not is_land(get_obj(pre_a, x))]
        ob = post_a.get("objects", {}) or {}
        zones = {int(oid): o.get("zone") for oid, o in ob.items()}
        ctrls = {int(oid): o.get("controller") for oid, o in ob.items()}

        bf_lands = [x for x in lands_a
                    if zones.get(x) == "Battlefield" and ctrls.get(x) == 0]
        gy_lands = [x for x in lands_a if zones.get(x) == "Graveyard"]
        gy_nonl = [x for x in nonl_a
                   if zones.get(x) == "Graveyard" and ctrls.get(x) == 0]
        notes.append(
            f"accept: hand_lands={len(lands_a)} hand_nonlands={len(nonl_a)}; "
            f"post: lands->bf={len(bf_lands)} lands->gy={len(gy_lands)} "
            f"nonlands->gy={len(gy_nonl)}")
        ass["A3_accept_lands_battlefield"] = \
            "passed" if (len(bf_lands) == len(lands_a) and not gy_lands) \
            else "failed"
        ass["A4_accept_rest_discarded"] = \
            "passed" if len(gy_nonl) == len(nonl_a) else "failed"
        move_map = {x: zones.get(x) for x in lands_a + nonl_a}
        notes.append(f"accept zone map: {move_map}")

    # A5
    if post_a is not None:
        empty = not (post_a.get("stack") or [])
        adv = (post_a.get("turn_number") or 0) > (ST.get("accept_turn") or 0) \
            or post_a.get("active_player") == 1
        ass["A5_cleanup"] = "passed" if (empty and adv) else "failed"
        notes.append(f"post_accept: phase={post_a.get('phase')} "
                     f"turn={post_a.get('turn_number')} "
                     f"stack_empty={empty} advanced={adv}")
    else:
        ass["A5_cleanup"] = "not-run"
        notes.append("post_accept.json missing; A5 not-run")

    for k in sorted(ass):
        say(f"{k}: {ass[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes}, f, indent=2)

    verdict = "blocked"
    if ass.get("A1_setup_ok") not in ("failed", "blocked", "not-run"):
        if ass.get("A3_accept_lands_battlefield") == "failed" \
                or ass.get("A4_accept_rest_discarded") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
    say("verdict:", verdict)

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_6770.py", "w") as f:
        f.write(scenario_src.decode())

    binary_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.103.0/"
             f"phase-server-slim-x86_64-unknown-linux-musl", "rb").read()).hexdigest()
    card_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json", "rb").read()).hexdigest()
    draft_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.103.0/data/draft-pools.json", "rb").read()).hexdigest()
    run_meta = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00", time.localtime(t0)),
        "server": {
            "server_version": "v0.103.0",
            "build_commit": "ec27a8d",
            "protocol_version": 106,
            "binary_sha256": binary_sha,
            "card_data_sha256": card_sha,
            "draft_pools_sha256": draft_sha,
            "run_dir": "runs/run-20261007-1011 (shared live server, phase-server PID 5775)",
            "port": 9374,
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": dict(P0_DECK), "P1": dict(P1_DECK)},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"]},
        "setup_line": ("P0: 12x Manabond + 12x Grizzly Bears + 36x Forest; "
                       "P1: 60x Forest dummy; native human seats"),
        "contract_line": ("cast Manabond ({G}), ramp with decline-every-turn, "
                          "DECLINE at first armed end step (no movement), "
                          "ACCEPT at next end step; assert lands hit the "
                          "battlefield and the rest of the hand is discarded"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Manabond/Grizzly Bears deck density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_6770_0860.py (verified 2026-09-18, "
            "run 20260918-6770).",
            "Manabond's may-choice answered via 106 decideOptionalEffect "
            "bare true/false choice (role=accept/value, text fallbacks), per "
            "the #7195 driver lesson; gated per-stage with the answer held "
            "until the pre-decision export lands.",
            "Payment via vi tapLandForMana with needs={G:1}; legacy PayMana/ "
            "PayManaAbilityMana honored as fallback.",
            "playLand included in NON_DECISION_CODES (per the #7195 lesson); "
            "land plays via legal_actions PlayLand matched by _src_oid with "
            "the is_land() helper (card_types.core_types contains 'Land').",
            "post_decline exported in the end step with the stack empty and "
            "before the cleanup discard, so A2 compares decline-only "
            "movement.",
            "post_accept exported at the first P1 turn after accept_turn "
            "(trigger resolved, discard done, game advanced).",
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
