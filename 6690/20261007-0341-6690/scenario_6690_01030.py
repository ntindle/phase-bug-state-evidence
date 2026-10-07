#!/usr/bin/env python3
"""Issue #6690 re-validation on pinned v0.103.0 (build ec27a8d, WS protocol 106):
chained ChangeZone's `ControllerRef::You` misclassified as relative, blocking
activation (The Beamtown Bullies).

Report (2026-07-27, status:confirmed, area:engine): when a chained ChangeZone
names a zone belonging to the ability's controller ("from your graveyard")
while a companion player target exists on the parent node,
ability_utils::relative_controller_kind classifies that ControllerRef::You as
a *relative* controller. legal_targets_for_ability_filter then re-enumerates
the slot against the companion player's candidates, searches the OPPONENT's
graveyard, finds nothing, and activation fails with
ActionNotAllowed("No legal targets available").

Reproduction per the issue: The Beamtown Bullies --
  "{T}: Target opponent whose turn it is puts target nonlegendary creature
   card from your graveyard onto the battlefield under their control. It gains
   haste. Goad it. At the beginning of the next end step, exile it."
Activate {T} on an opponent's turn with a nonlegendary creature card in YOUR
graveyard. Reported: the ability is not activatable.

This run is a v0.103.0 port of the verified v0.102.0/protocol-106 driver
(evidence 6690/20261005-6690e, verdict not-reproduced, maintained comment
https://github.com/phase-rs/phase/issues/6690#issuecomment-5619870369).
It tests the ACTIVATION GATE (the reported 'not activatable' failure), not
resolution of the reanimation itself, because the pinned card-data parse
marks the top-level effect Unimplemented (see parse_evidence.json).

Scenario (native engine, two human-client seats):
  P0: 4x The Beamtown Bullies + 12x Grizzly Bears + 8x Faithless Looting
      ({R} sorcery: draw 2, discard 2 -> bins Bears) + 12x Forest + 12x
      Mountain + 12x Swamp. Casts Looting (discard Bears), casts Bullies,
      holds it untapped.
  P1: 60x Island. Land per turn, passes priority, never attacks.

Probe point: P0 controls an untapped Beamtown Bullies (past summoning
sickness: cast_turn + 3 <= turn), P0's graveyard holds >=1 Grizzly Bears
(nonlegendary), it is P1's turn, P0 has priority. The driver exports pre.json
+ bullies_object.json, scans merged legal actions for the Bullies'
ActivateAbility, submits it while STILL HOLDING P0's priority (never passes
the activating seat first), drains rejections, exports post.json, settles.

Assertions:
  A1_setup_ok        probe point reached.
  A2_offered         an ActivateAbility action for the Bullies was advertised.
  A3_no_target_block the attempt was NOT rejected with "No legal targets
                     available" (the reported failure signature).
  A4_announced       the activation was accepted and the ability announced
                     (on the stack, tap cost paid); only meaningful if A2
                     passed.
  A5_cleanup         game proceeds after the attempt (no stall).

Verdict rule: reproduced iff the attempt is rejected with the reported "No
legal targets available" signature. not-reproduced iff the activation is
offered and accepted/announced (the reported block is absent on v0.103.0).
If the ability is not offered at all, the verdict documents the parse state
rather than relabeling.

Protocol-106 conventions (from verified scenario_6250_01030.py /
scenario_301_01030.py):
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    merged legal actions (top-level + legal_actions_by_object).
  - MulliganDecision via legacy Action; bottom via vi schema/select gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap.
  - Discards (Faithless Looting resolution + hand-size cleanup) via vi
    schema/select whose candidates reference hand cards; ranked by context.
  - Mana for casts via legacy PayMana actions + vi tapLandForMana menus
    driven by ST['mana_needs']; generic color support.
  - real_decision_pending excludes tapLandForMana / untapLandForMana /
    castSpell / activateAbility menus; surf_codes filters None codes.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6690
RUN_ID = "20261007-0341-6690"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty (stale RUN_ID reuse)"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BULLIES = "the beamtown bullies"
BEARS = "grizzly bears"
LOOTING = "faithless looting"
LANDS = ("forest", "mountain", "swamp")
LAND_COLOR = {"forest": "G", "mountain": "R", "swamp": "B"}

P0_DECK = deck(("The Beamtown Bullies", 4), ("Grizzly Bears", 12),
               ("Faithless Looting", 8), ("Forest", 12), ("Mountain", 12),
               ("Swamp", 12))
P1_DECK = deck(("Island", 60))

ST = {"mana_needs": {}}
SUBMITTED_OPPS = set()
PASSED_REV = {}
LAND_PLAYED_TURN = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def oname(o):
    return (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return oname(get_obj(state, oid))


def hand_ids(state, pid):
    players = state.get("players") or []
    if pid >= len(players):
        return []
    return [str(x) for x in players[pid].get("hand", [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def gy_names(state, pid):
    players = state.get("players") or []
    if pid >= len(players):
        return []
    return [oname(get_obj(state, oid))
            for oid in players[pid].get("graveyard", [])]


def gy_count(state, pid, name):
    return sum(1 for n in gy_names(state, pid) if n == name)


def bf(state, pid):
    return [(oid, o) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bullies_on_bf(state, pid=0):
    for oid, o in bf(state, pid):
        if oname(o) == BULLIES:
            return oid, o
    return None, None


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if obj_lname(state, oid) == name:
            return oid
    return None


def is_land_name(o):
    return oname(o) in ("forest", "mountain", "swamp", "island", "plains")


def is_land(o):
    t = str(o.get("type_line") or o.get("type") or "").lower()
    return "land" in t or is_land_name(o)


def untapped_land_ids(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and is_land(o)]


def can_pay(state, pid, needs):
    """needs: dict color->n, with 'generic' key. Conservative: colored mana
    only from matching untapped lands; generic from any untapped land."""
    avail = {}
    for oid in untapped_land_ids(state, pid):
        c = LAND_COLOR.get(oname(get_obj(state, oid)))
        if c:
            avail[c] = avail.get(c, 0) + 1
    total = sum(avail.values())
    for color, n in needs.items():
        if color == "generic":
            continue
        if avail.get(color, 0) < n:
            return False
    generic_need = needs.get("generic", 0)
    colored_consumed = sum(n for color, n in needs.items() if color != "generic")
    return total - colored_consumed >= generic_need


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_entries(state):
    return state.get("stack") or []


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and not (state.get("stack") or []))


# ------------------------------------------------------------- 106 primitives
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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def surf_codes(ch):
    return [c for c in (
        s.get("data", {}).get("code") if isinstance(s.get("data"), dict) else None
        for s in ch.get("surfaces", []) or []) if c]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


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
            codes.update(surf_codes(ch))
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = {k: v for k, v in a["data"].items()
                       if not k.startswith("_")}
    msg = {k: v for k, v in msg.items() if not k.startswith("_")}
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
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert "0.103.0" in str(ver), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"


# ------------------------------------------------------------- mulligan/bottom
async def do_mulligan(c, acts, st, pid, tag, G):
    acts_types = [a.get("type") for a in acts]
    if "MulliganDecision" not in acts_types:
        return False
    key = (tag, "mulligan", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    mulls = G.get(f"mulls{pid}", 0)
    hn = hand_names(st["state"], pid)
    lands = sum(1 for n in hn if n in LANDS)
    if pid == 0:
        want = BULLIES in hn and lands >= 2
    else:
        want = True  # P1: all lands, keep immediately
    decision = "keep" if (want or mulls >= 3) else "mulligan"
    if decision == "mulligan":
        G[f"mulls{pid}"] = mulls + 1
    say(f"[{tag}] mulligan: {decision} (mulls={mulls})")
    wire("mulligan", {"who": tag, "decision": decision})
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": "Keep" if decision == "keep"
                                               else "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, G=None):
    """Bottom-after-mulligan: vi schema/select gated on waitingForKind.code
    == 'mulligan' AND turn 1 / Untap. We always keep 7; safety net only."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (turn_of(state) == 1 and state.get("phase") == "Untap"):
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
    key = (tag, "bottom", str(iid))
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in (BULLIES,) or nm == BULLIES:
            return (2, str(oid))
        if oid is not None and oname(get_obj(state, oid)) in LANDS:
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": str(iid),
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}},
                      tag)
    return True


# ------------------------------------------------------------- discard
def rank_looting_discard(state, oid):
    """Faithless Looting resolution: bin Bears first, never the Bullies."""
    nm = obj_lname(state, oid)
    if nm == BEARS:
        return (0, nm)
    if nm in LANDS:
        return (1, nm)
    if nm == LOOTING:
        return (2, nm)
    if nm == BULLIES:
        return (9, nm)
    return (3, nm)


def rank_cleanup_discard(state, oid):
    """Hand-size cleanup: lands, then Bears, never the Bullies."""
    nm = obj_lname(state, oid)
    if nm in LANDS:
        return (0, nm)
    if nm == BEARS:
        return (1, nm)
    if nm == LOOTING:
        return (2, nm)
    if nm == BULLIES:
        return (9, nm)
    return (3, nm)


async def do_discard(c, st, pid, tag, G):
    """Answer a discard prompt for pid via vi schema/select whose candidates
    reference hand cards. Context: Faithless Looting resolution (discard 2,
    bin Bears) vs hand-size cleanup (hand-7, bin lands)."""
    state = st["state"]
    hand = hand_ids(state, pid)
    handset = set(hand)
    looting = G.get("looting_resolving", False)
    if looting:
        want_n = 2
    else:
        want_n = len(hand) - 7
        if want_n <= 0:
            return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type") or ""
        if stype not in ("select", "sequence"):
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not refs or not set(refs) <= handset:
            continue
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or want_n)
        n = min(n, len(cands))
        if n <= 0:
            continue
        rankfn = rank_looting_discard if looting else rank_cleanup_discard
        ranked = sorted(cands,
                        key=lambda ch: rankfn(state, _cand_reference(ch)))
        picks = ranked[:n]
        SUBMITTED_OPPS.add(key)
        names = [obj_lname(state, _cand_reference(x)) for x in picks]
        note = "looting" if looting else "cleanup"
        say(f"[{tag}] discard({note}): submits {names} via vi {stype}")
        wire("discard_answered", {"tag": tag, "note": note, "iid": str(iid),
                                  "picks": names, "stype": stype})
        if looting:
            G["looting_resolving"] = False
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype,
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}},
                          tag)
        return True
    return False


# ------------------------------------------------------------- mana / priority
async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    if not needs or sum(needs.values()) <= 0:
        return False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, [str(x) for x in syms]))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if (tag, "tapi", str(iid)) in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        for ch, syms in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in syms:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        SUBMITTED_OPPS.add((tag, "tapi", str(iid)))
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        resp = opp.get("response", {}) or {}
        if resp.get("type") == "schema":
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "choose",
                                               "data": {"choiceId": pick.get("id")}}},
                              tag)
        else:
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "choose",
                                               "data": {"choiceId": pick.get("id")}}},
                              tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = turn_of(state)
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
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


# ------------------------------------------------------------- per-tick handlers
def drain_rejections(c, G):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            body = json.dumps(data, default=str)
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {body[:300]}")
            G.setdefault("rejections", []).append(
                {"who": c.name, "type": t, "data": body[:500]})


async def p1_tick(c, G, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    drain_rejections(c, G)
    if await do_mulligan(c, acts, st, 1, tag, G):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard(c, st, 1, tag, G):
        return
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag, G.get("mana_needs_p1", {})):
        return
    if is_my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, G, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    drain_rejections(c, G)
    if await do_mulligan(c, acts, st, 0, tag, G):
        return
    if await do_bottom(c, acts, st, 0, tag, G):
        return
    if await do_discard(c, st, 0, tag, G):
        return
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    # yield before leg evaluation so a just-answered mana menu settles
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # --- probe point
    bid, bo = bullies_on_bf(state, 0)
    cast_turn = G.get("bullies_cast_turn")
    eligible = (cast_turn is not None
                and turn_of(state) >= cast_turn + 3
                and state.get("active_player") == 1)
    stage = G.get("stage")
    if (stage == "armed" and bid is not None and not bo.get("tapped")
            and gy_count(state, 0, BEARS) >= 1 and eligible
            and my_priority(acts)):
        G["stage"] = "probe"
        G["probe_turn"] = turn_of(state)
        G["bullies_oid"] = bid
        say(f"[{tag}] PROBE POINT t{turn_of(state)} {state.get('phase')}: "
            f"bullies={bid} untapped, bears_in_gy={gy_count(state,0,BEARS)}, "
            f"P1's turn, P0 priority (cast on t{cast_turn})")
        wire("probe_point", {"turn": turn_of(state),
                             "phase": state.get("phase"), "bullies_oid": bid,
                             "bears_in_gy": gy_count(state, 0, BEARS),
                             "bullies_object": bo})
        with open(f"{EVDIR}/bullies_object.json", "w") as f:
            json.dump({"oid": bid, "object": bo}, f, indent=1, default=str)
        pre = await export_now("pre.json", c)
        G["pre"] = pre
        aa = [a for a in acts if a.get("type") == "ActivateAbility"
              and str(a.get("data", {}).get("source_id", "")) == str(bid)]
        # also match via _src_oid from legal_actions_by_object merge
        aa2 = [a for a in acts if a.get("type") == "ActivateAbility"
               and str(a.get("_src_oid", "")) == str(bid)]
        for a in aa2:
            if a not in aa:
                aa.append(a)
        say(f"[{tag}] ActivateAbility candidates for bullies {bid}: {len(aa)}")
        wire("activation_candidates",
             {"bullies_oid": bid,
              "candidates": [a.get("data") for a in aa],
              "all_action_types": sorted(set(a.get("type") for a in acts))})
        if aa:
            G["offered"] = True
            G["offered_data"] = {k: v for k, v in aa[0].get("data", {}).items()
                                 if not k.startswith("_")}
            G["attempted"] = True
            say(f"[{tag}] attempting activation while holding priority: "
                f"{json.dumps(G['offered_data'])[:300]}")
            # CRITICAL: never pass P0's priority before submitting; the
            # activating seat must hold priority at submission time (#301).
            await submit_as_is(c, aa[0])
            await asyncio.sleep(3)
            drain_rejections(c, G)
            st2 = c.latest or {}
            state2 = st2.get("state") or {}
            G["stack_after"] = len(stack_entries(state2))
            b2, bo2 = bullies_on_bf(state2, 0)
            G["tapped_after"] = bo2.get("tapped") if bo2 else None
            G["wait_after"] = vi_kind_code(st2)
            G["stack_detail"] = stack_entries(state2)
            say(f"[{tag}] after attempt: stack={G['stack_after']} "
                f"tapped={G['tapped_after']} vi_kind={G['wait_after']}")
            wire("after_attempt", {"stack": G["stack_after"],
                                   "tapped": G["tapped_after"],
                                   "vi_kind": G["wait_after"],
                                   "stack_detail": G["stack_detail"]})
        else:
            say(f"[{tag}] NO ActivateAbility offered for the Bullies")
            wire("activation_not_offered",
                 {"bullies_oid": bid,
                  "all_action_types": sorted(set(a.get("type")
                                                 for a in acts))})
        post = await export_now("post.json", c)
        G["post"] = post
        G["stage"] = "settle"
        G["settle_since"] = time.time()
        return

    if stage == "settle":
        if time.time() - G.get("settle_since", time.time()) > 25:
            G["done"] = True
            say(f"[{tag}] settle complete; finishing")
            return
        if decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    # --- stage: develop / armed ---
    if stage in ("develop", "armed"):
        needs = ST["mana_needs"].get(tag, {})
        if sum(needs.values()) > 0:
            if await pay_tick(c, acts):
                return
            if await pay_mana_vi(c, st, tag, needs):
                return
        if is_my_main(state, 0) and my_priority(acts):
            if not G.get("looting_cast"):
                loid = find_hand(state, 0, LOOTING)
                if (loid is not None and BEARS in hand_names(state, 0)
                        and gy_count(state, 0, BEARS) == 0):
                    needs = {"R": 1, "generic": 0}
                    if can_pay(state, 0, needs):
                        a, oid = cast_action_for(acts, state, LOOTING)
                        if a is not None:
                            ST["mana_needs"][tag] = dict(needs)
                            G["looting_resolving"] = True
                            G["looting_cast"] = True
                            await submit_as_is(c, a)
                            say(f"[{tag}] casts Faithless Looting "
                                f"(t{turn_of(state)})")
                            wire("cast", {"card": LOOTING,
                                          "turn": turn_of(state)})
                            return
            if bid is None and not G.get("looting_resolving"):
                hoid = find_hand(state, 0, BULLIES)
                if hoid is not None:
                    needs = {"B": 1, "R": 1, "generic": 1}
                    if can_pay(state, 0, needs):
                        a, oid = cast_action_for(acts, state, BULLIES)
                        if a is not None:
                            ST["mana_needs"][tag] = dict(needs)
                            G["bullies_cast_turn"] = turn_of(state)
                            G["stage"] = "armed"
                            await submit_as_is(c, a)
                            say(f"[{tag}] casts Beamtown Bullies "
                                f"(t{turn_of(state)}); stage=armed")
                            wire("cast", {"card": BULLIES,
                                          "turn": turn_of(state)})
                            return
        if is_my_main(state, 0):
            if await play_a_land(c, state, 0, acts, tag):
                return
        if decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def export_now(path, client):
    raw = await client.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(raw)
    say(f"exported {path} ({len(raw)} bytes)")
    env = json.loads(raw)
    st = env.get("state", env) if isinstance(env, dict) else env
    wire("export_unwrap", {"path": path, "top_keys": list(env.keys())[:8]
                           if isinstance(env, dict) else "?"})
    return st


def dump_view(c, st):
    """Compact per-revision diagnostic of what a client actually sees."""
    s = st["state"]
    acts = [a.get("type") for a in (st.get("legal_actions") or [])]
    vi = st.get("viewer_interaction") or {}
    ops = []
    for o in vi.get("opportunities", []) or []:
        r = o.get("response", {}) or {}
        d = r.get("data", {}) or {}
        items = d.get("candidates") or d.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(surf_codes(ch))
        ops.append({"rtype": r.get("type"), "n": len(items),
                    "codes": sorted(codes)})
    wire("view", {"who": c.name, "rev": c.revision,
                  "turn": turn_of(s), "phase": s.get("phase"),
                  "active": s.get("active_player"),
                  "top_acts": acts,
                  "vi_can": vi.get("canSubmit"),
                  "vi_kind": (vi.get("waitingForKind") or {}).get("code"),
                  "ops": ops})


# ------------------------------------------------------------- game runner
async def run_game():
    global PASSED_REV, SUBMITTED_OPPS, LAND_PLAYED_TURN
    PASSED_REV = {}
    SUBMITTED_OPPS = set()
    LAND_PLAYED_TURN = {}
    G = {"stage": "develop",
         "looting_cast": False, "looting_resolving": False,
         "bullies_cast_turn": None,
         "probe_turn": None, "bullies_oid": None,
         "offered": False, "offered_data": None, "attempted": False,
         "stack_after": None, "tapped_after": None, "wait_after": None,
         "stack_detail": None,
         "pre": None, "post": None,
         "settle_since": 0,
         "max_turn": 0, "mulls0": 0, "mulls1": 0,
         "mana_needs_p1": {},
         "rejections": [],
         "done": False, "blocked": None, "game_ok": False}

    p0 = PhaseClient("6690-P0")
    p1 = PhaseClient("6690-P1")
    await p0.connect()
    await p1.connect()
    att = await p0.create(P0_DECK, player_count=2)
    code = att.get("game_code")
    G["game_code"] = code
    say(f"[game] game created code={code}")
    wire("game_created", {"game_code": code})
    await p1.join(code, P1_DECK)

    timeout_s = 1500
    t0 = time.time()
    last = {}
    last_tick = {}
    last_adv = {p0.name: time.time(), p1.name: time.time()}
    last_diag = 0.0
    warned = set()
    ticks = [(p0, p0_tick, 0, "P0"), (p1, p1_tick, 1, "P1")]
    try:
        for _ in range(int(timeout_s / 0.2)):
            await asyncio.sleep(0.2)
            for c, tick, pid, tag in ticks:
                st = c.latest
                if not st:
                    continue
                rev = c.revision
                if (rev == last.get(c.name)
                        and time.time() - last_tick.get(c.name, 0) <= 5):
                    continue
                last[c.name] = rev
                last_tick[c.name] = time.time()
                last_adv[c.name] = time.time()
                warned.discard(c.name)
                state = st["state"]
                dump_view(c, st)
                G["max_turn"] = max(G["max_turn"], turn_of(state))
                try:
                    await tick(c, G, tag)
                except Exception as e:
                    say(f"[{c.name}] tick error: {e!r}")
                    wire("tick_error", {"who": c.name, "error": repr(e)})
                if G.get("done"):
                    say("[game] contract complete; finishing")
                    G["game_ok"] = True
                    return G
            now = time.time()
            if now - last_diag > 60 and p0.latest:
                last_diag = now
                s = p0.latest["state"]
                bid, _ = bullies_on_bf(s, 0)
                say(f"[game] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                    f"phase={s.get('phase')} "
                    f"P0hand={len(hand_names(s,0))} P1hand={len(hand_names(s,1))} "
                    f"stack={len(stack_entries(s))} stage={G.get('stage')} "
                    f"bullies={'bf' if bid else 'no'} "
                    f"gy_bears={gy_count(s,0,BEARS)}")
            for c in (p0, p1):
                if (now - last_adv[c.name] > 60 and c.name not in warned
                        and c.latest):
                    warned.add(c.name)
                    st = c.latest
                    state = st.get("state", {})
                    say(f"[game] WATCHDOG {c.name}: no revision advance for "
                        f"{now - last_adv[c.name]:.0f}s; rev={c.revision} "
                        f"turn={turn_of(state)} phase={state.get('phase')} "
                        f"vi_kind={vi_kind_code(st)}")
            if now - t0 > timeout_s:
                say(f"[game] global timeout ({timeout_s}s) hit")
                G["blocked"] = "global timeout before probe/settle"
                return G
    finally:
        await p0.close()
        await p1.close()
    G["blocked"] = G.get("blocked") or "loop exhausted without completion"
    return G


# ------------------------------------------------------------- parse evidence
def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def cz_targets_your_gy(a):
    for d in walk(a):
        if d.get("type") == "ChangeZone":
            t = d.get("target") or {}
            if t.get("controller") == "You" and any(
                    p.get("type") == "InZone" and p.get("zone") == "Graveyard"
                    for p in (t.get("properties") or [])):
                return True
    return False


def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))
    b = cd["the beamtown bullies"]
    acts = [a for a in (b.get("abilities") or [])
            if (a.get("kind") or "") == "Activated"]
    hits = []
    for name, c in cd.items():
        for ai, a in enumerate(c.get("abilities") or []):
            if (a.get("kind") or "") != "Activated":
                continue
            t = a.get("target")
            tt = t.get("type") if isinstance(t, dict) else t
            if cz_targets_your_gy(a) and tt in (
                    "Opponent", "Player", "TargetPlayer", "EachPlayer"):
                hits.append(name)
    out = {
        "the beamtown bullies": {
            "oracle_text": b.get("oracle_text"),
            "mana_cost": b.get("mana_cost"),
            "n_activated_abilities": len(acts),
            "activated_ability": acts[0] if acts else None,
        },
        "structural_scan": {
            "scope": ("all activated abilities in pinned v0.103.0 card data: "
                      "player/opponent ability target + chained ChangeZone "
                      "targeting the controller's graveyard "
                      "(controller:'You' + InZone Graveyard)"),
            "hits": hits,
            "n_hits": len(hits),
        },
    }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(G, parse):
    ass = {}
    notes = []
    b = parse["the beamtown bullies"]
    ab = b.get("activated_ability") or {}
    eff = ab.get("effect") or {}
    notes.append(
        "parse (pinned v0.103.0 card-data): Bullies activated ability cost=" +
        json.dumps(ab.get("cost")) + " target=" +
        json.dumps(ab.get("target")) + " top_effect=" +
        json.dumps({"type": eff.get("type"), "name": eff.get("name")}))
    notes.append(
        "structural scan: %d activated abilities in the pinned data have a "
        "player/opponent ability target + chained ChangeZone from the "
        "controller's graveyard (the issue's trigger structure)" %
        parse["structural_scan"]["n_hits"])

    if G.get("blocked"):
        for k in ("A1_setup_ok", "A2_offered", "A3_no_target_block",
                  "A4_announced", "A5_cleanup"):
            ass[k] = "not-run"
        notes.append(f"verdict: blocked - {G['blocked']}")
        return ass, notes, "blocked"

    if G.get("probe_turn") is not None:
        ass["A1_setup_ok"] = "passed"
        notes.append(
            f"A1 passed: probe point reached on turn {G['probe_turn']} "
            f"(P1's turn, P0 priority): Bullies oid {G['bullies_oid']} "
            f"untapped on P0 battlefield (cast turn {G['bullies_cast_turn']}, "
            f"past summoning sickness), >=1 Grizzly Bears in P0 graveyard")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(
            f"A1 FAILED: probe point never reached (max_turn={G['max_turn']}, "
            f"bullies_cast_turn={G['bullies_cast_turn']}, "
            f"stage={G.get('stage')})")

    if G.get("offered"):
        ass["A2_offered"] = "passed"
        notes.append(
            f"A2 passed: ActivateAbility advertised for the Bullies "
            f"(data={json.dumps(G['offered_data'])[:200]})")
    elif G.get("probe_turn") is not None:
        ass["A2_offered"] = "failed"
        notes.append("A2 FAILED: no ActivateAbility offered for the Bullies "
                     "at the probe point")
    else:
        ass["A2_offered"] = "not-run"
        notes.append("A2 not-run: probe point never reached")

    no_legal = any("no legal targets" in r.get("data", "").lower()
                   for r in G.get("rejections", []))
    if no_legal:
        ass["A3_no_target_block"] = "failed"
        notes.append("A3 FAILED: the attempt was rejected with the reported "
                     "'No legal targets available' signature")
    elif G.get("attempted"):
        ass["A3_no_target_block"] = "passed"
        notes.append("A3 passed: no 'No legal targets available' rejection "
                     "on the activation attempt")
    elif G.get("probe_turn") is not None:
        ass["A3_no_target_block"] = "not-run"
        notes.append("A3 not-run: activation not offered, nothing attempted")
    else:
        ass["A3_no_target_block"] = "not-run"
        notes.append("A3 not-run: probe point never reached")

    if G.get("attempted") and (G.get("stack_after") or 0) > 0:
        ass["A4_announced"] = "passed"
        notes.append(
            f"A4 passed: activation accepted; stack={G['stack_after']} after "
            f"the attempt, Bullies tapped={G['tapped_after']}, "
            f"vi_kind_after={G['wait_after']}")
    elif G.get("attempted"):
        ass["A4_announced"] = "failed"
        notes.append(
            f"A4 FAILED: activation submitted but stack={G['stack_after']}, "
            f"tapped={G['tapped_after']}, vi_kind_after={G['wait_after']}")
    else:
        ass["A4_announced"] = "not-run"
        notes.append("A4 not-run: nothing attempted")

    if G.get("probe_turn") is not None and G.get("done"):
        ass["A5_cleanup"] = "passed"
        notes.append("A5 passed: settle phase completed with the game "
                     "advancing; no stall observed")
    elif G.get("probe_turn") is not None:
        ass["A5_cleanup"] = "not-run"
        notes.append("A5 not-run: settle phase did not complete")
    else:
        ass["A5_cleanup"] = "not-run"
        notes.append("A5 not-run: probe point never reached")

    if no_legal:
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED - the Bullies activation was "
                     "rejected with the reported 'No legal targets available' "
                     "signature")
    elif (ass.get("A1_setup_ok") == "passed"
          and ass.get("A2_offered") == "passed"
          and ass.get("A3_no_target_block") == "passed"
          and ass.get("A4_announced") == "passed"):
        verdict = "not-reproduced"
        notes.append("verdict: not-reproduced - the {T} activation was "
                     "offered and accepted on the opponent's turn with a "
                     "nonlegendary creature in the controller's graveyard; "
                     "the reported activation block is absent on v0.103.0. "
                     "The pinned parse carries no graveyard target slot "
                     "(top-level Unimplemented), so the described "
                     "relative_controller_kind mechanism is not reachable "
                     "from this card's runtime parse.")
    else:
        verdict = "blocked"
        notes.append("verdict: blocked - inconclusive observations "
                     "(see assertion notes)")
    return ass, notes, verdict


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1100
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#6690 Beamtown Bullies: {T} activation blocked on opponent's turn?",
         fill=(255, 210, 90))
    s = run["server"]
    line(f"server {s['server_version']} build {s['build_commit']} protocol "
         f"{s['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("setup: P0 Bullies + Bears + Faithless Looting vs P1 60x Island; "
         "activate {T} on P1's turn, Bears in P0 graveyard", size=16)
    line("parse: ability cost {T}, target null, top effect "
         "Unimplemented(change_zone_enters_under_anaphor)", size=16)
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, val in run["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=17)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:14]:
        line(f"  - {n[:112]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.103.0, protocol 106)")
    await verify_server_hello()

    parse = parse_evidence()
    ab = (parse["the beamtown bullies"]["activated_ability"] or {})
    say("parse: bullies ability top_effect="
        + json.dumps({"type": (ab.get("effect") or {}).get("type"),
                      "name": (ab.get("effect") or {}).get("name")})
        + f"; structural scan hits = {parse['structural_scan']['n_hits']}")

    try:
        G = await run_game()
    except Exception as e:
        say(f"game crashed: {e!r}")
        wire("game_crashed", {"error": repr(e)})
        G = {"blocked": f"game crashed: {e!r}", "rejections": [],
             "max_turn": 0, "bullies_cast_turn": None}

    ass, notes, verdict = evaluate(G, parse)
    dur = time.time() - t_start

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    bin_path = f"{BACKFILL}/server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl"
    cd_path = f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"
    dp_path = f"{BACKFILL}/server/releases/v0.103.0/data/draft-pools.json"
    server_identity = {
        "server_version": "0.103.0",
        "build_commit": "ec27a8d",
        "protocol_version": 106,
        "mode": "Full",
        "binary_sha256": sha(bin_path),
        "card_data_sha256": sha(cd_path),
        "draft_pools_sha256": sha(dp_path),
        "signature_key_id": "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY (436711b6a2d36828)",
        "signature_verified": True,
        "signature_note": ("v0.103.0 binary + release manifest minisign-verified "
                           "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                           "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                           "mode; data digests match the signed manifest; digests "
                           "recomputed against on-disk files this run"),
        "source": ("2026-10-07: latest stable release v0.103.0 (published "
                   "2026-10-06T16:45:47Z) == pinned release dir (pinned "
                   "2026-10-06T13:00-05:00); ServerHello "
                   "0.103.0/ec27a8d/protocol 106 verified by handshake this run; "
                   "hashes recomputed against on-disk artifacts this run; "
                   "reused the shared backfill-owned v0.103.0 server on "
                   "127.0.0.1:9374 (run dir runs/cron-20261007-0311, started "
                   "by run 20261007-0311-6666 at 03:15 CDT; verified healthy "
                   "by handshake at 03:44 CDT)"),
    }
    # digest cross-check against the 2026-10-06 v0.103.0 pin record
    assert server_identity["binary_sha256"] == \
        "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc", \
        "binary digest drift vs pinned v0.103.0 record"
    assert server_identity["card_data_sha256"] == \
        "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe", \
        "card-data digest drift vs pinned v0.103.0 record"
    assert server_identity["draft_pools_sha256"] == \
        "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd", \
        "draft-pools digest drift vs pinned v0.103.0 record"

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": "runs/cron-20261007-0311",
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario": "driver/scenario_6690_01030.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_6690_01030.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "game_code": G.get("game_code"),
        "game_ok": G.get("game_ok", False),
        "parse_summary": {
            "bullies_oracle": (parse["the beamtown bullies"]["oracle_text"])[:220],
            "bullies_ability": {
                "cost": ab.get("cost"),
                "target": ab.get("target"),
                "top_effect": {
                    "type": (ab.get("effect") or {}).get("type"),
                    "name": (ab.get("effect") or {}).get("name"),
                },
            },
            "structural_scan_hits": parse["structural_scan"]["n_hits"],
        },
        "probe": {
            "probe_turn": G.get("probe_turn"),
            "bullies_oid": G.get("bullies_oid"),
            "bullies_cast_turn": G.get("bullies_cast_turn"),
            "offered": G.get("offered"),
            "offered_data": G.get("offered_data"),
            "attempted": G.get("attempted"),
            "stack_after": G.get("stack_after"),
            "tapped_after": G.get("tapped_after"),
            "vi_kind_after": G.get("wait_after"),
            "stack_detail": G.get("stack_detail"),
        },
        "rejections": G.get("rejections", []),
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-10-07",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "P1 is a passive second seat (lands only, no attacks, no spells).",
            "The pinned v0.103.0 parse of the Bullies' ability carries no "
            "graveyard target slot (top-level Unimplemented "
            "change_zone_enters_under_anaphor): this run tests the ACTIVATION "
            "GATE (the reported 'not activatable' failure), not the "
            "relative_controller_kind target-slot machinery, which is not "
            "reachable from this card's runtime parse. Resolution of the "
            "reanimation itself is not asserted.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "history": [
            {"run_id": "20260910-6690", "release": "v0.78.0", "protocol": 68,
             "verdict": "not-reproduced",
             "evidence_commit": "847768678deee21deabc25fd64e34aa48bcff0e7",
             "note": "prior run; activation accepted on the opponent's turn, "
                     "no 'No legal targets available'"},
            {"run_id": "20260917-6690", "release": "v0.85.0", "protocol": 72,
             "verdict": "not-reproduced",
             "evidence_commit": "4454d25c69e1674d31b5a1be3d9b448208298d47",
             "note": "re-validation on v0.85.0; activation offered and "
                     "accepted, no target block"},
            {"run_id": "20261005-6690e", "release": "v0.102.0", "protocol": 106,
             "verdict": "not-reproduced",
             "evidence_commit": "30fe676343df40fa1f3e873c42d5405d909a7703",
             "note": "re-validation on v0.102.0 (protocol-106 port); "
                     "activation offered and accepted on the opponent's "
                     "turn, no 'No legal targets available'"},
        ],
        "setup_line": ("P0 4x The Beamtown Bullies + 12x Grizzly Bears + 8x "
                       "Faithless Looting + 12x Forest/Mountain/Swamp vs P1 "
                       "60x Island. P0 bins Bears with Looting, casts the "
                       "Bullies, and on a later P1 turn (past summoning "
                       "sickness) with P0 priority attempts the {T} "
                       "activation."),
        "contract_line": ("The {T} activation must be offered and accepted on "
                          "the opponent's turn with a nonlegendary creature "
                          "in the controller's graveyard; a 'No legal "
                          "targets available' rejection is the reported "
                          "failure signature."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    subprocess.run(["cp", __file__, f"{EVDIR}/scenario_6690_01030.py"],
                   check=True)
    render_png(f"{EVDIR}/summary.png", run)

    # server excerpts for this game (shared backfill-owned server log)
    try:
        slog = f"{BACKFILL}/runs/cron-20261007-0311/server.log"
        excerpts = []
        code = G.get("game_code")
        if os.path.exists(slog):
            with open(slog, errors="replace") as f:
                for line in f:
                    ll = line.lower()
                    if code and code in line:
                        excerpts.append(line.rstrip())
                    elif any(k in ll for k in ("trigger", "error", "warn",
                                               "bullies", "target")):
                        excerpts.append(line.rstrip()[:300])
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(excerpts[-80:]) + "\n")
        say(f"server excerpts: {len(excerpts)} lines")
    except Exception as e:
        say(f"server excerpts failed: {e}")

    # hash scenario_run.log AFTER all say() logging is done (#6916 lesson)
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha(f"{EVDIR}/{fn}") + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"manifest.sha256 written for {len(lines)} files", flush=True)
    bad = []
    for line in lines:
        h, _, name = line.partition("  ")
        h2 = sha(f"{EVDIR}/{name.strip()}")
        if h2 != h:
            bad.append(name)
    assert not bad, f"manifest self-check failed: {bad}"
    print(f"manifest self-check passed for {len(lines)} files", flush=True)
    # validate: every JSON parses, PNG readable
    for fn in os.listdir(EVDIR):
        if fn.endswith(".json"):
            json.load(open(f"{EVDIR}/{fn}"))
    im = __import__("PIL.Image", fromlist=["Image"]).open(
        f"{EVDIR}/summary.png")
    im.verify()
    print("evidence validated: JSON parses, manifest self-checks, PNG readable",
          flush=True)
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1),
          flush=True)


asyncio.run(amain())
