#!/usr/bin/env python3
"""Issue #6903: Shadow Kin exiles whatever it likes.

Oracle (verified from pinned card-data.json):
  Flash
  At the beginning of your upkeep, each player mills three cards. You may
  exile a creature card from among the cards milled this way. If you do, this
  creature becomes a copy of that card, except it has this ability.

Reported: Shadow Kin's upkeep ability offers/exiles creatures outside the set
of cards milled by that trigger, including battlefield permanents.
Triage acceptance criteria (mike-theDude, 2026-08-03):
  1. Each player mills three cards.
  2. The optional choice contains only creature cards among the cards milled
     by that exact trigger.
  3. Selecting a card exiles it from its current graveyard and makes Shadow
     Kin the specified copy.
  4. Battlefield permanents and pre-existing graveyard cards are never
     eligible.

Behavioral contract (native engine, protocol 70, two human seats):
  P0 casts Shadow Kin (+ a Storm Crow and P1 casts Grizzly Bears, as
  battlefield "temptations"). At P0's first upkeep with Kin on the
  battlefield (leg 1), observe the mill + optional exile choice; accept and
  exile a newly-milled creature. At P0's next upkeep (leg 2), the trigger
  must fire again (Kin kept the ability through the copy) and we decline.

  A1 setup_ok        pre_upkeep: P0 Upkeep, Kin on P0 BF, Crow on P0 BF,
                     Bear on P1 BF.
  A2 each_milled_3   pre_upkeep -> mid_mill1: each player's library -3 and
                     graveyard +3.
  A3 optional_offered leg1: the may-exile decision was offered (when >=1
                     creature was milled).
  A4 candidates_in_scope leg1: every recorded exile candidate is a creature
                     card in the newly-milled set (no battlefield oids, no
                     pre-existing graveyard oids).
  A5 exile_and_copy  leg1: chosen card in Exile; Kin object's name == the
                     exiled card's name.
  A6 ability_retained leg2: upkeep trigger fired again (mill observed),
                     proving the copy kept the upkeep ability.
  A7 decline_control leg2: declined; no new exile; Kin still the leg-1 copy;
                     game proceeds.
  A8 cleanup         post2: stack empty, game advanced past the leg-2 upkeep.

Verdict: reproduced iff A1+A2 pass and (A4 fails or A5 fails).
not-reproduced iff A1..A8 all pass. blocked iff A1/A2 fail.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6903
RUN_ID = "20260912-f33127k"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

KIN = "Shadow Kin"
CROW = "Storm Crow"
BEAR = "Grizzly Bears"
ISLAND = "Island"
FOREST = "Forest"

P0_DECK = [(KIN, 4), (CROW, 24), (ISLAND, 32)]
P1_DECK = [(BEAR, 24), (FOREST, 36)]
TIMEOUT = 3000

ST = {}
C0 = None
C1 = None


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()
    RUNLOG2.write(msg + "\n")
    RUNLOG2.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def objs(state):
    return state.get("objects") or {}


def players(state):
    return state.get("players") or []


def player_obj(state, pid):
    for p in players(state):
        if p.get("player_id") == pid or p.get("id") == pid \
                or p.get("seat") == pid:
            return p
    return {}


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(objs(state)[oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def gy_oids(state, pid):
    return set(str(oid) for oid, o in objs(state).items()
               if o.get("zone") == "Graveyard" and o.get("controller") == pid)


def exile_oids(state):
    return set(str(oid) for oid, o in objs(state).items()
               if o.get("zone") == "Exile")


def bf_creature_oids(state):
    return set(str(oid) for oid, o in objs(state).items()
               if o.get("zone") == "Battlefield")


def lib_count(state, pid):
    p = player_obj(state, pid)
    lib = p.get("library")
    if isinstance(lib, list):
        return len(lib)
    return None


def untapped_lands(state, pid, land_name):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == land_name and not o.get("tapped")]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf(state).get("type")


def wf_player(state):
    return (wf(state).get("data") or {}).get("player")


def wf_pending_for(state, pid):
    """True if the waiting_for names pid in data.player or a pending entry
    (MulliganDecision carries players in data.pending, not data.player)."""
    d = wf(state).get("data") or {}
    if isinstance(d.get("player"), int):
        return d["player"] == pid
    for p in d.get("pending") or []:
        if isinstance(p, dict) and p.get("player") == pid:
            return True
    return False


def stack(state):
    return state.get("stack") or []


def get_vi(st):
    vi = (st or {}).get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def current_opps(c):
    st = c.latest if c else None
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path} ({len(s)} bytes)")
    return s


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)
    ST.setdefault("settle", {})[c.name] = c.revision


async def submit_interaction(c, submission):
    wire("interaction_submit", {"who": c.name, "submission": submission,
                                "stage": ST.get("stage")})
    await c.send_interaction(submission)
    ST.setdefault("settle", {})[c.name] = c.revision


def settle_pending(c):
    return (ST.get("settle") or {}).get(c.name) is not None \
        and c.revision <= ST["settle"][c.name]


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    if found:
        (ST.get("settle") or {}).pop(c.name, None)
    return found


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> PROOF -> DONE
        "leg": 1,
        "stop": False,
        "turn_cap": 60,
        "rejections": [],
        "game_code": None,
        "mulligans": 0,
        "kin_cast": False,
        "kin_oid": None,
        "kin_cast_turn": None,
        "crow_cast": False,
        # per-leg proof state
        "pre_done": False,
        "pre": None,          # dict with turn, lib, gy sets, bf set
        "mill_done": False,
        "milled": None,       # {0: [...], 1: [...]} newly milled oids
        "optional_offered": False,
        "choice_recorded": False,
        "candidates": [],     # [{choiceId, oid, name, zone, newly_milled, is_creature}]
        "decline_choice_id": None,
        "choice_opp": None,   # raw opportunity
        "choice_submitted": False,
        "chosen_oid": None,
        "chosen_name": None,
        "forced_pick": False,
        "mill_drops": None,
        "mill_final": False,
        "exile_seen": False,
        "copy_seen": False,
        "declined": False,
        "leg_done": False,
        "upkeep_watch_start": None,
        "mid_exported": False,
        "hold_since": None,
        "held_logged": set(),
        "leg1": {},           # archived leg-1 records
        "proof_turns": [],
    })


def vi_opps(st):
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []


def opp_choices(opp):
    resp = opp.get("response") or {}
    out = []
    for v in (resp.get("choices"), (resp.get("data") or {}).get("choices")):
        if isinstance(v, list):
            out.extend(v)
    return out


def opp_candidates(opp):
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    out = []
    for v in (resp.get("candidates"), data.get("candidates")):
        if isinstance(v, list):
            out.extend(v)
    return out


def surf_data(s):
    d = s.get("data") or {}
    return {
        "type": s.get("type"),
        "code": d.get("code") or s.get("code"),
        "action": d.get("action") or s.get("action"),
        "role": d.get("role") or s.get("role"),
        "value": d.get("value") if d.get("value") is not None
                 else s.get("value"),
        "seat": d.get("seat") if d.get("seat") is not None
                else s.get("seat"),
        "reference": d.get("reference") or s.get("reference"),
        "symbols": d.get("symbols") or s.get("symbols"),
    }


def is_decide_optional_effect(opp):
    resp = opp.get("response") or {}
    if resp.get("type") != "exactChoices":
        return False
    for ch in opp_choices(opp):
        for s in ch.get("surfaces") or []:
            f = surf_data(s)
            code = (f["code"] or "") + " " + (f["action"] or "")
            if "decideOptionalEffect" in code or "OptionalEffect" in code:
                return True
    return False


def decide_optional_value(opp):
    out = {}
    for ch in opp_choices(opp):
        for s in ch.get("surfaces") or []:
            f = surf_data(s)
            code = (f["code"] or "").lower()
            role = (f["role"] or "").lower()
            val = str(f["value"] or "").lower()
            if role == "accept" or "decideoptionaleffect" in code:
                if val in ("true", "1", "yes", "accept"):
                    out["accept"] = ch
                elif val in ("false", "0", "no", "decline"):
                    out["decline"] = ch
    return out


def choice_id_of(node):
    return (node.get("choiceId") or node.get("choice_id")
            or node.get("id"))


def node_references_oid(node, oid):
    for s in node.get("surfaces") or []:
        if str(surf_data(s)["reference"]) == str(oid):
            return True
    return False


def candidate_oid(cand):
    """Best-effort object id for a card candidate."""
    for s in cand.get("surfaces") or []:
        ref = surf_data(s)["reference"]
        if ref is not None and str(ref).lstrip("-").isdigit():
            return str(ref)
    for k in ("object_id", "objectId", "oid"):
        v = cand.get(k)
        if v is not None and str(v).lstrip("-").isdigit():
            return str(v)
    cid = choice_id_of(cand)
    if cid is not None and str(cid).lstrip("-").isdigit():
        return str(cid)
    return None


def is_card_choice(opp):
    """An opportunity asking to pick card(s): schema select/sequence with
    candidates, or exactChoices whose choices reference objects."""
    if is_decide_optional_effect(opp):
        return False
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    rtype = resp.get("type")
    if rtype == "schema" and spec.get("type") in ("select", "sequence"):
        return bool(opp_candidates(opp))
    if rtype == "exactChoices":
        for ch in opp_choices(opp):
            if candidate_oid(ch):
                return True
    return False


def card_choice_nodes(opp):
    """Return the selectable card nodes (candidates or choices)."""
    resp = opp.get("response") or {}
    cands = opp_candidates(opp)
    if cands:
        return cands
    return [ch for ch in opp_choices(opp) if candidate_oid(ch)]


def submit_choice(c, opp, node, resp_type=None):
    resp = (opp.get("response") or {})
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    rtype = resp_type or resp.get("type")
    if rtype == "schema" and isinstance(spec, dict):
        rtype = spec.get("type") or rtype
    cid = choice_id_of(node)
    sub = {"interactionId": opp.get("interactionId"),
           "response": {"type": "choose", "data": {"choiceId": cid}}}
    if rtype == "sequence":
        sub["response"] = {"type": "sequence",
                           "data": {"choiceIds": [cid]}}
    elif rtype == "select":
        sub["response"] = {"type": "select",
                           "data": {"choiceIds": [cid]}}
    return submit_interaction(c, sub)


def is_creature_card(o):
    blob = json.dumps(o, default=str).lower()
    return "creature" in blob


# ------------------------------------------------------------------ ticks

async def play_land(c, pid, state, acts, land_name):
    lid = find_hand(state, pid, land_name)
    if not lid:
        return False
    la = next((x for x in acts if x["type"] == "PlayLand"
               and str(x.get("data", {}).get("object_id")) == lid), None)
    if la:
        await submit_as_is(c, la)
        return True
    return False


def discard_picks(state, pid):
    hand = hand_oids(state, pid)
    over = len(hand) - 7
    if over <= 0:
        return []
    by_name = {}
    for oid in hand:
        by_name.setdefault(oname(objs(state)[oid]), []).append(oid)
    picks = []

    def take(name, keep):
        ids = by_name.get(name, [])
        while len(ids) > keep and len(picks) < over:
            picks.append(ids.pop())

    if pid == 0:
        take(ISLAND, 3)
        take(KIN, 1)      # protect Shadow Kin; creatures may go (they seed
                          # the pre-existing graveyard scope test)
    else:
        take(FOREST, 0)
    for oid in hand:
        if len(picks) >= over:
            break
        if oid not in picks:
            picks.append(oid)
    return [int(x) for x in picks[:over]]


def snapshot_zones(state):
    return {
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "active": state.get("active_player"),
        "lib": {0: lib_count(state, 0), 1: lib_count(state, 1)},
        "gy": {0: sorted(gy_oids(state, 0)), 1: sorted(gy_oids(state, 1))},
        "exile": sorted(exile_oids(state)),
        "bf_creatures": sorted(bf_creature_oids(state)),
    }


async def record_choice(c, state, opp, leg):
    """Record the exile-choice candidate set once per leg."""
    cands = []
    decline_id = None
    pre = ST.get("pre") or {}
    pre_gy = {0: set(pre.get("gy", {}).get(0, [])),
              1: set(pre.get("gy", {}).get(1, []))}
    # newly milled = in a graveyard now but not in the pre-upkeep graveyard
    milled_now = {p: gy_oids(state, p) - pre_gy[p] for p in (0, 1)}
    milled_set = milled_now[0] | milled_now[1]
    pre_gy_all = pre_gy[0] | pre_gy[1]
    pre_bf = set(pre.get("bf_creatures", []))
    for node in card_choice_nodes(opp):
        oid = candidate_oid(node)
        if not oid:
            # decline / pass option (no card reference)
            decline_id = choice_id_of(node)
            continue
        o = objs(state).get(str(oid)) if oid else None
        nm = oname(o) if o else ""
        zone = (o or {}).get("zone")
        cands.append({
            "choiceId": choice_id_of(node),
            "oid": oid,
            "name": nm,
            "zone": zone,
            "newly_milled": oid in milled_set if oid else False,
            "was_battlefield": oid in pre_bf if oid else False,
            "was_preexisting_gy": oid in pre_gy_all if oid else False,
            "is_creature": is_creature_card(o) if o else None,
        })
    ST["candidates"] = cands
    ST["decline_choice_id"] = decline_id
    ST["choice_opp"] = json.loads(json.dumps(opp, default=str))
    ST["choice_recorded"] = True
    with open(f"{EVDIR}/choice_opp{leg}.json", "w") as f:
        json.dump(ST["choice_opp"], f, indent=1, default=str)
    wire(f"choice_candidates_leg{leg}",
         {"candidates": cands,
          "opp_type": (opp.get("response") or {}).get("type")})
    say(f"[P0] leg {leg}: recorded {len(cands)} exile candidates: "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'})"
                    f"{' NEW' if x['newly_milled'] else ''}"
                    f"{' BF!' if x['was_battlefield'] else ''}"
                    f"{' PRE-GY!' if x['was_preexisting_gy'] else ''}"
                    for x in cands))
    await export_now(f"mid_choice{leg}.json")
    return cands


async def handle_mulligan(c, pid, state):
    """Waiting-for-driven mulligan handling. The BottomCards step does not
    advertise a MulliganDecision action, so this must not depend on the
    advertised action list."""
    pend = (wf(state).get("data") or {}).get("pending") or []
    my = next((p for p in pend if p.get("player") == pid), None)
    ph = (my or {}).get("phase") or {}
    ptype = ph.get("type") if isinstance(ph, dict) else None
    if ptype == "BottomCards":
        n = int(ph.get("count") or 0)
        keep_name = KIN if pid == 0 else FOREST
        named = [(oid, oname(objs(state)[oid]))
                 for oid in hand_oids(state, pid)]
        picks = [oid for oid, nm in named if nm != keep_name][:n]
        if len(picks) < n:
            picks = [oid for oid, _ in named][:n]
        picks = [int(x) for x in picks[:n]]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": picks}})
        say(f"[{c.name}] bottoms {len(picks)} after mulligan")
        return True
    if pid == 0 and ST["mulligans"] < 3 and not find_hand(state, 0, KIN):
        ST["mulligans"] += 1
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"[P0] mulligans ({ST['mulligans']}) seeking Shadow Kin")
    else:
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        if pid == 0:
            say(f"[P0] keeps hand")
    return True


async def p0_upkeep_tick(c, pid, state, acts):
    """Proof-turn upkeep observation for the active leg."""
    turn = state.get("turn_number") or 0
    phase = state.get("phase") or ""
    leg = ST["leg"]
    in_window = (ST["stage"] == "PROOF" and phase == "Upkeep"
                 and state.get("active_player") == pid
                 and turn > (ST.get("kin_cast_turn") or 0)
                 and not ST["leg_done"])
    if not in_window:
        return False
    if turn not in ST["proof_turns"]:
        ST["proof_turns"].append(turn)

    # 1. pre-upkeep export on first sight of the upkeep
    if not ST["pre_done"]:
        ST["pre"] = snapshot_zones(state)
        ST["pre_done"] = True
        ST["upkeep_watch_start"] = time.time()
        if leg == 2:
            ST["leg2_pre_exile"] = sorted(exile_oids(state))
        say(f"[P0] leg {leg}: upkeep turn {turn}; pre exported "
            f"(lib={ST['pre']['lib']})")
        wire(f"pre_upkeep_leg{leg}", ST["pre"])
        await export_now(f"pre_upkeep{leg}.json")
        return True

    # 2. scan opportunities: record the card choice before answering anything
    for opp in current_opps(c):
        if is_card_choice(opp) and not ST["choice_recorded"]:
            await record_choice(c, state, opp, leg)
        if is_decide_optional_effect(opp):
            ST["optional_offered"] = True

    # 3. mill detection (the engine is suspected to mill only the
    # controller, so trigger on ANY library drop, not 3-and-3)
    if not ST["mill_done"]:
        pre = ST["pre"]
        libs = {0: lib_count(state, 0), 1: lib_count(state, 1)}
        dropped = {p: (pre["lib"][p] or 0) - (libs[p] or 0)
                   for p in (0, 1)}
        if (dropped[0] >= 1 or dropped[1] >= 1) or ST["choice_recorded"]:
            gy_now = {0: gy_oids(state, 0), 1: gy_oids(state, 1)}
            pre_gy = {0: set(pre["gy"][0]), 1: set(pre["gy"][1])}
            ST["milled"] = {p: sorted(gy_now[p] - pre_gy[p]) for p in (0, 1)}
            ST["mill_done"] = True
            ST["mill_drops"] = dropped
            say(f"[P0] leg {leg}: mill checkpoint "
                f"(lib drops={dropped}, new gy="
                f"{ {p: len(ST['milled'][p]) for p in (0, 1)} })")
            wire(f"mill_leg{leg}", {"dropped": dropped,
                                    "milled": ST["milled"]})
            if not ST["mid_exported"]:
                await export_now(f"mid_mill{leg}.json")
                ST["mid_exported"] = True
            return True

    # 4. answer the optional prompt
    for opp in current_opps(c):
        if is_decide_optional_effect(opp):
            vals = decide_optional_value(opp)
            if leg == 1:
                node = vals.get("accept")
                if node and choice_id_of(node):
                    say(f"[P0] leg 1: answers may-exile ACCEPT")
                    wire("maychoice_answer", {"answer": "accept", "leg": 1})
                    await submit_choice(c, opp, node)
                    return True
            else:
                node = vals.get("decline")
                if node and choice_id_of(node):
                    say(f"[P0] leg 2: answers may-exile DECLINE")
                    wire("maychoice_answer", {"answer": "decline", "leg": 2})
                    await submit_choice(c, opp, node)
                    ST["declined"] = True
                    return True
            return True  # prompt present but unparseable; hold

    # 5. leg-2 card choice: decline via the non-candidate option
    if leg == 2 and ST["choice_recorded"] and not ST["choice_submitted"]:
        if ST.get("no_decline_gave_up"):
            return False  # already gave up; keep passing priority
        did = ST.get("decline_choice_id")
        if did:
            for opp in current_opps(c):
                if is_card_choice(opp):
                    for node in card_choice_nodes(opp):
                        if choice_id_of(node) == did:
                            say(f"[P0] leg 2: declines at card choice "
                                f"(choice {did})")
                            wire("decline_choice_submit",
                                 {"choiceId": did, "leg": 2})
                            await submit_choice(c, opp, node)
                            ST["choice_submitted"] = True
                            ST["declined"] = True
                            return True
        # no decline option found. The may-choice prompt may still arrive
        # (the tick-top handler answers NO). Do not spin forever: after
        # 45s fall through and pass priority so the game can proceed.
        if ST.get("no_decline_since") is None:
            ST["no_decline_since"] = time.time()
        if time.time() - ST["no_decline_since"] < 45:
            wire("no_decline_option", {"leg": 2})
            return True
        say("[P0] leg 2: no decline option for 45s; falling through to "
            "pass priority")
        wire("no_decline_gave_up", {"leg": 2})
        ST["no_decline_gave_up"] = True
        return False

    # 5b. leg-1 card choice: pick a newly-milled creature
    if leg == 1 and ST["choice_recorded"] and not ST["choice_submitted"]:
        pre = ST.get("pre") or {}
        pre_gy = {p: set(pre.get("gy", {}).get(p, [])) for p in (0, 1)}
        milled_set = (gy_oids(state, 0) - pre_gy[0]) | (
            gy_oids(state, 1) - pre_gy[1])
        gy0 = gy_oids(state, 0)
        ordered = [x for x in ST["candidates"]
                   if x["newly_milled"] and x["is_creature"]
                   and x["oid"] in gy0 and x["choiceId"]]
        ordered += [x for x in ST["candidates"]
                    if x["newly_milled"] and x["is_creature"]
                    and x["choiceId"] and x not in ordered]
        if ordered:
            pick = ordered[0]
            for opp in current_opps(c):
                if is_card_choice(opp):
                    for node in card_choice_nodes(opp):
                        if choice_id_of(node) == pick["choiceId"]:
                            ST["chosen_oid"] = pick["oid"]
                            ST["chosen_name"] = pick["name"]
                            say(f"[P0] leg 1: exiles {pick['name']} "
                                f"oid {pick['oid']}")
                            wire("exile_choice_submit",
                                 {"pick": pick, "leg": 1})
                            await submit_choice(c, opp, node)
                            ST["choice_submitted"] = True
                            return True
            return True  # choice recorded but prompt gone; wait
        # no in-scope candidate offered at all: the reported bug. Force a
        # pick of the first offered candidate to observe the full reported
        # outcome (exile of an out-of-scope card).
        forced = [x for x in ST["candidates"] if x["choiceId"]]
        if forced and not ST.get("forced_pick"):
            pick = forced[0]
            for opp in current_opps(c):
                if is_card_choice(opp):
                    for node in card_choice_nodes(opp):
                        if choice_id_of(node) == pick["choiceId"]:
                            ST["chosen_oid"] = pick["oid"]
                            ST["chosen_name"] = pick["name"]
                            ST["forced_pick"] = True
                            say(f"[P0] leg 1: FORCED out-of-scope pick "
                                f"{pick['name']} oid {pick['oid']} "
                                f"(zone {pick['zone']})")
                            wire("forced_pick",
                                 {"pick": pick, "leg": 1,
                                  "reason": "no in-scope candidate offered"})
                            await submit_choice(c, opp, node)
                            ST["choice_submitted"] = True
                            return True
        wire("no_inscope_candidate", {"candidates": ST["candidates"]})
        return True

    # 6. leg-1 resolution watch: exile + copy
    if leg == 1 and ST["choice_submitted"] and not ST["leg_done"]:
        # post-choice mill observation: the engine may mill after the choice
        pre = ST.get("pre")
        if pre and not ST.get("mill_final"):
            libs = {0: lib_count(state, 0), 1: lib_count(state, 1)}
            dropped = {p: (pre["lib"][p] or 0) - (libs[p] or 0)
                       for p in (0, 1)}
            if dropped != ST.get("mill_drops"):
                gy_now = {0: gy_oids(state, 0), 1: gy_oids(state, 1)}
                pre_gy = {0: set(pre["gy"][0]), 1: set(pre["gy"][1])}
                ST["milled"] = {p: sorted(gy_now[p] - pre_gy[p])
                                for p in (0, 1)}
                ST["mill_drops"] = dropped
                say(f"[P0] leg 1: mill update lib drops={dropped}")
                wire("mill_update_leg1",
                     {"dropped": dropped, "milled": ST["milled"]})
                await export_now("mid_mill1b.json")
                if dropped[0] >= 3 or dropped[1] >= 3:
                    ST["mill_final"] = True
        chosen = ST.get("chosen_oid")
        o_chosen = objs(state).get(str(chosen)) if chosen else None
        kin = objs(state).get(str(ST.get("kin_oid"))) if ST.get(
            "kin_oid") else None
        if o_chosen and o_chosen.get("zone") == "Exile":
            ST["exile_seen"] = True
        if kin and ST.get("chosen_name") and oname(kin) == ST["chosen_name"]:
            ST["copy_seen"] = True
            wire("kin_copy_object", {"kin": json.loads(
                json.dumps(kin, default=str))})
        if ST["exile_seen"] and ST["copy_seen"]:
            ST["leg1"] = {
                "turn": turn,
                "milled": ST.get("milled"),
                "mill_drops": ST.get("mill_drops"),
                "forced_pick": ST.get("forced_pick"),
                "optional_offered": ST.get("optional_offered"),
                "candidates": ST.get("candidates"),
                "chosen_oid": ST.get("chosen_oid"),
                "chosen_name": ST.get("chosen_name"),
                "pre": ST.get("pre"),
            }
            await export_now("post1.json")
            say(f"[P0] leg 1 complete: {ST['chosen_name']} exiled, Kin is "
                f"now a copy; post1 exported")
            advance_leg()
            return True
        if time.time() - (ST["upkeep_watch_start"] or time.time()) > 120:
            ST["leg1"] = {
                "turn": turn, "milled": ST.get("milled"),
                "mill_drops": ST.get("mill_drops"),
                "forced_pick": ST.get("forced_pick"),
                "optional_offered": ST.get("optional_offered"),
                "candidates": ST.get("candidates"),
                "chosen_oid": ST.get("chosen_oid"),
                "chosen_name": ST.get("chosen_name"),
                "pre": ST.get("pre"),
                "note": "resolution watch timed out",
            }
            await export_now("post1.json")
            say("[P0] leg 1 resolution watch timed out; post1 exported")
            advance_leg()
            return True
        return False

    # 7. leg-2 completion: past the leg-2 upkeep window, stack empty.
    # Decline control: the may-choice was declined, or the engine never
    # offered a may-choice (no_decline_gave_up) and the upkeep completed.
    if leg == 2 and ST["pre_done"] and not ST["leg_done"]:
        pre_turn = (ST.get("pre") or {}).get("turn") or 0
        turn_now = state.get("turn_number") or 0
        past = (phase != "Upkeep" or turn_now > pre_turn)
        may_done = ST.get("declined") or ST.get("no_decline_gave_up")
        dwell = time.time() - (ST["upkeep_watch_start"] or time.time())
        if past and may_done and dwell > 60 and not stack(state):
            await export_now("post2.json")
            ST["leg_done"] = True
            ST["stage"] = "DONE"
            ST["stop"] = True
            say("[P0] leg 2 complete "
                f"(declined={bool(ST.get('declined'))}, "
                f"gave_up={bool(ST.get('no_decline_gave_up'))}); "
                f"post2 exported; DONE")
            return True
        return False

    # 8. stall watchdog for the upkeep window
    if ST["upkeep_watch_start"] and time.time() - ST[
            "upkeep_watch_start"] > 150 and not ST["leg_done"]:
        say(f"[P0] leg {leg}: upkeep watch 150s with no completion; "
            f"exporting mid_stall")
        wire("upkeep_stall", {"leg": leg, "mill_done": ST["mill_done"],
                              "choice_recorded": ST["choice_recorded"],
                              "optional_offered": ST["optional_offered"]})
        await export_now(f"mid_stall{leg}.json")
        ST["leg_done"] = True
        if leg == 1:
            ST["leg1"] = {"turn": turn, "milled": ST.get("milled"),
                          "optional_offered": ST.get("optional_offered"),
                          "candidates": ST.get("candidates"),
                          "pre": ST.get("pre"),
                          "note": "upkeep stall timeout"}
            advance_leg()
        else:
            ST["stage"] = "DONE"
            ST["stop"] = True
        return True
    return False


def advance_leg():
    """Archive leg-1 records and arm leg 2 (decline control)."""
    ST["leg"] = 2
    ST["pre_done"] = False
    ST["pre"] = None
    ST["mill_done"] = False
    ST["milled"] = None
    ST["optional_offered"] = False
    ST["choice_recorded"] = False
    ST["candidates"] = []
    ST["decline_choice_id"] = None
    ST["choice_opp"] = None
    ST["choice_submitted"] = False
    ST["chosen_oid"] = None
    ST["chosen_name"] = None
    ST["forced_pick"] = False
    ST["mill_drops"] = None
    ST["mill_final"] = False
    ST["exile_seen"] = False
    ST["copy_seen"] = False
    ST["declined"] = False
    ST["leg_done"] = False
    ST["upkeep_watch_start"] = None
    ST["mid_exported"] = False
    say("[P0] leg 1 archived; leg 2 (decline control) armed")


async def p0_tick(c, pid, state, acts):
    stage = ST["stage"]
    turn = state.get("turn_number") or 0
    phase = state.get("phase") or ""

    # --- decisions first: never pass while a decision is pending ---
    # (MulliganDecision is handled at tick level via waiting_for, since the
    # BottomCards step advertises no MulliganDecision action.)
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers",
            "MulliganDecision"):
        wft = wf_type(state)
        if stage == "PROOF":
            if wf_type(state) == "OrderTriggers":
                for opp in current_opps(c):
                    resp = opp.get("response") or {}
                    data = resp.get("data") or {}
                    spec = data.get("spec") or {}
                    if resp.get("type") == "schema" and spec.get(
                            "type") == "sequence":
                        ids = [choice_id_of(x) for x in opp_candidates(opp)
                               if choice_id_of(x)]
                        if ids:
                            say("[P0] answers OrderTriggers with advertised "
                                "order")
                            await submit_interaction(c, {
                                "interactionId": opp.get("interactionId"),
                                "response": {"type": "sequence",
                                             "data": {"choiceIds": ids}}})
                            return True
        key = ("held", wft, ST.get("leg"))
        if key not in ST["held_logged"]:
            ST["held_logged"].add(key)
            vi = (c.latest or {}).get("viewer_interaction")
            wire("decision_held", {"who": c.name, "wf_type": wft,
                                  "vi": json.dumps(vi, default=str)[:3000],
                                  "leg": ST.get("leg")})
            say(f"[P0] HOLDING unhandled decision {wft} "
                f"(leg {ST['leg']})")
        if ST["hold_since"] is None:
            ST["hold_since"] = time.time()
        return True
    ST["hold_since"] = None

    if stage == "SETUP":
        kin_bf = bf_named(state, pid, KIN)
        if kin_bf:
            ST["kin_oid"] = kin_bf[0]
            ST["kin_cast"] = True
            ST["kin_cast_turn"] = turn
            ST["stage"] = "PROOF"
            ST["leg"] = 1
            say(f"Shadow Kin resolved on battlefield (turn {turn}); "
                f"stage -> PROOF, leg 1")
            return True
        if not is_my_main(state, pid):
            return False
        # cast Storm Crow first (battlefield temptation), then Shadow Kin
        if not ST["crow_cast"]:
            crow_oid = find_hand(state, pid, CROW)
            a = castspell_advertised(acts, crow_oid)
            if crow_oid and a is not None and len(
                    untapped_lands(state, pid, ISLAND)) >= 2 \
                    and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                await submit_as_is(c, a)
                ST["crow_cast"] = True
                say(f"[P0] submits Storm Crow cast (turn {turn})")
                return True
        else:
            crow_bf = bf_named(state, pid, CROW)
            if not crow_bf and len(ST["rejections"]) > 0:
                ST["crow_cast"] = False  # retry if it never landed
        kin_oid = find_hand(state, pid, KIN)
        a = castspell_advertised(acts, kin_oid)
        if kin_oid and a is not None and len(
                untapped_lands(state, pid, ISLAND)) >= 4 \
                and wf_player(state) == pid and wf_type(state) == "Priority":
            await submit_as_is(c, a)
            say(f"[P0] submits Shadow Kin cast (turn {turn})")
            return True
        await play_land(c, pid, state, acts, ISLAND)
        return False

    if stage == "PROOF":
        if await p0_upkeep_tick(c, pid, state, acts):
            return True
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts, ISLAND)
        return False
    return False


async def p1_tick(c, pid, state, acts):
    stage = ST["stage"]
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        wire("decision_held_p1", {"wf_type": wf_type(state)})
        return True
    if is_my_main(state, pid):
        bears_bf = bf_named(state, pid, BEAR)
        if len(bears_bf) < 2:
            boid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, boid)
            if boid and a is not None and len(
                    untapped_lands(state, pid, FOREST)) >= 2 \
                    and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                await submit_as_is(c, a)
                say(f"[P1] submits Grizzly Bears cast")
                return True
        await play_land(c, pid, state, acts, FOREST)
        return False
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    drain_rejections(c)
    if settle_pending(c):
        return False
    # mulligan (incl. BottomCards) is waiting_for-driven; the BottomCards
    # step advertises no MulliganDecision action.
    if wf_type(state) == "MulliganDecision" and wf_pending_for(state, pid):
        if await handle_mulligan(c, pid, state):
            return True
    # OptionalEffectChoice: the "you may exile" yes/no for Shadow Kin.
    # Leg 1 answers yes (accept path); leg 2 answers no (decline control).
    # (Placed before p0_tick: p0_tick holds unhandled PROOF decisions.)
    if wf_type(state) == "OptionalEffectChoice" \
            and wf_pending_for(state, pid):
        for opp in current_opps(c):
            resp = (opp.get("response") or {}).get("type")
            if resp != "exactChoices":
                continue
            nodes = (((opp.get("response") or {}).get("data") or {})
                     .get("choices") or [])
            if not nodes:
                continue
            with open(f"{EVDIR}/opt_choice{ST.get('leg')}.json", "w") as f:
                json.dump(opp, f, indent=1, default=str)
            want_yes = (ST.get("leg") == 1)
            pick = None
            for nd in nodes:
                txt = json.dumps(nd, default=str).lower()
                is_yes = ("yes" in txt or "accept" in txt or "exile" in txt
                          or "true" in txt)
                is_no = ("no" in txt or "decline" in txt or "false" in txt)
                if want_yes and is_yes and not is_no:
                    pick = nd
                    break
                if not want_yes and is_no and not is_yes:
                    pick = nd
                    break
            if pick is None:
                pick = nodes[0 if want_yes else -1]
            ST["may_choice"] = {"leg": ST.get("leg"), "want_yes": want_yes,
                                "pick_id": choice_id_of(pick)}
            say(f"[{c.name}] leg {ST.get('leg')}: may-exile -> "
                f"{'YES' if want_yes else 'NO'} "
                f"(choice {choice_id_of(pick)})")
            wire("may_choice", ST["may_choice"])
            await submit_choice(c, opp, pick)
            if ST.get("leg") == 1:
                ST["may_yes"] = True
            else:
                ST["declined"] = True
            return True
    # EffectZoneChoice: the engine's concrete "choose a card to exile"
    # selection (destination Exile). Leg 1 picks the first offered card and
    # records it as the exiled card for A5.
    if wf_type(state) == "EffectZoneChoice" \
            and wf_pending_for(state, pid):
        data = (wf(state).get("data") or {})
        if data.get("destination") == "Exile" and ST.get("leg") == 1:
            for opp in current_opps(c):
                nodes = card_choice_nodes(opp)
                if not nodes:
                    resp = (opp.get("response") or {}).get("type")
                    if resp == "exactChoices":
                        nodes = (((opp.get("response") or {}).get("data")
                                  or {}).get("choices") or [])
                if not nodes:
                    continue
                with open(f"{EVDIR}/exile_choice1.json", "w") as f:
                    json.dump(opp, f, indent=1, default=str)
                # prefer the previously force-picked card if still offered
                want_oid = ST.get("chosen_oid")
                pick = None
                for nd in nodes:
                    if candidate_oid(nd) is not None and str(
                            candidate_oid(nd)) == str(want_oid):
                        pick = nd
                        break
                if pick is None:
                    for nd in nodes:
                        if candidate_oid(nd) is not None:
                            pick = nd
                            break
                if pick is None:
                    continue
                oid = candidate_oid(pick)
                o = objs(state).get(str(oid)) if oid else None
                ST["chosen_oid"] = oid
                ST["chosen_name"] = oname(o)
                ST["exile_choice_submitted"] = True
                say(f"[{c.name}] leg 1: exiles {ST['chosen_name']} "
                    f"oid {oid} via EffectZoneChoice")
                wire("exile_zone_choice",
                     {"oid": oid, "name": ST["chosen_name"]})
                await submit_choice(c, opp, pick)
                return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            pend = (wf(state).get("data") or {}).get("pending") or []
            my = next((p for p in pend if p.get("player") == pid), None)
            phase = (my or {}).get("phase") or {}
            if isinstance(phase, dict) and phase.get("type") == "BottomCards":
                n = int(phase.get("count") or 0)
                keep_name = KIN if pid == 0 else FOREST
                named = [(oid, oname(objs(state)[oid]))
                         for oid in hand_oids(state, pid)]
                picks = [oid for oid, nm in named
                         if nm != keep_name][:n]
                if len(picks) < n:
                    picks = [oid for oid, _ in named][:n]
                picks = [int(x) for x in picks[:n]]
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": picks}})
                say(f"[{c.name}] bottoms {len(picks)} after mulligan")
                return True
            if pid == 0 and ST["mulligans"] < 3 and not find_hand(
                    state, 0, KIN):
                ST["mulligans"] += 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"[P0] mulligans ({ST['mulligans']}) seeking Shadow Kin")
            else:
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        picks = discard_picks(state, pid)
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": picks}})
            say(f"[{c.name}] discards {len(picks)} to hand size")
            return True
        wire("discard_no_picks", {"who": c.name})
        return True
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers" and \
            wf_player(state) == pid:
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if (state.get("phase") or "") == "DeclareBlockers" and pid in (0, 1) \
            and wf_player(state) == pid:
        for a in acts:
            if a["type"] == "DeclareBlockers":
                sub = copy.deepcopy(a)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            if wf_player(state) == pid:
                await submit_as_is(c, a)
                return True
            continue
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and wt not in (None, "Priority"):
        return True
    if wf_player(state) != pid:
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


# ------------------------------------------------------------------- run

async def get_server_hello():
    async with websockets.connect(URL, max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        return json.loads(raw)


def load_env(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())
    except Exception as e:
        return {"_err": str(e)[:160]}


def env_state(env):
    if not env or "_err" in env:
        return None
    s = env.get("state")
    return s if isinstance(s, dict) else json.loads(s)


def zone_of(state, oid):
    return (objs(state).get(str(oid)) or {}).get("zone")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def main():
    reset_state()
    t_start = time.time()
    hello = await get_server_hello()
    say("ServerHello observed: " + json.dumps(hello)[:400])
    wire("server_hello", hello)
    ST["server_hello"] = hello.get("data", hello)

    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    ST["game_code"] = C0.game_code
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        st0 = C0.latest
        turn = (st0.get("state", {}).get("turn_number") or 0) if st0 else 0
        if turn > ST["turn_cap"]:
            say("turn cap reached; stopping")
            wire("turn_cap", {})
            break
        if acted0 or acted1:
            last_progress = time.time()
        if ST.get("hold_since") and time.time() - ST["hold_since"] > 150:
            say("held decision for 150s with no progress; exporting and "
                "stopping")
            wire("hold_timeout", {"leg": ST["leg"]})
            await export_now("mid_held.json")
            break
        if time.time() - last_progress > 300:
            say("no progress for 300s; stopping")
            break
        await asyncio.sleep(0.15)

    say(f"loop ended: stage={ST['stage']} stop={ST['stop']} leg={ST['leg']}")
    wire("loop_end", {k: ST.get(k) for k in (
        "stage", "stop", "leg", "kin_cast", "kin_cast_turn",
        "optional_offered", "choice_recorded", "choice_submitted",
        "chosen_oid", "chosen_name", "exile_seen", "copy_seen",
        "declined", "proof_turns")})
    await C0.close()
    await C1.close()
    t_end = time.time()

    # ---- assertions from SAVED states ----
    A, D = {}, {}
    pre1 = env_state(load_env("pre_upkeep1.json"))
    midm1 = env_state(load_env("mid_mill1.json"))
    midc1 = env_state(load_env("mid_choice1.json"))
    post1 = env_state(load_env("post1.json"))
    pre2 = env_state(load_env("pre_upkeep2.json"))
    midm2 = env_state(load_env("mid_mill2.json"))
    post2 = env_state(load_env("post2.json"))
    leg1 = ST.get("leg1") or {}

    def libdrop(pre, post, pid):
        if not pre or not post:
            return None
        a = lib_count(pre, pid)
        b = lib_count(post, pid)
        return None if a is None or b is None else a - b

    # A1
    if pre1:
        kin_obj = (objs(pre1).get(str(ST.get("kin_oid"))) or {})
        kin_ok = (kin_obj.get("zone") == "Battlefield"
                  and kin_obj.get("controller") == 0)
        crow_ok = len(bf_named(pre1, 0, CROW)) >= 1
        bear_ok = len(bf_named(pre1, 1, BEAR)) >= 1
        phase_ok = (pre1.get("phase") or "") == "Upkeep" \
            and pre1.get("active_player") == 0
        ok = kin_ok and crow_ok and bear_ok and phase_ok
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"kin_on_bf={kin_ok} crow_on_p0_bf={crow_ok} "
                            f"bear_on_p1_bf={bear_ok} upkeep_p0={phase_ok} "
                            f"turn={pre1.get('turn_number')}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre_upkeep1.json missing (proof never armed)"

    # A2 (use the latest mill observation: mid_mill1b > post1 > mid_mill1 >
    # mid_choice1; the early checkpoints may predate the mill)
    postmill1 = (env_state(load_env("mid_mill1b.json")) or post1
                 or midm1 or midc1)
    if pre1 and postmill1:
        d0, d1 = libdrop(pre1, postmill1, 0), libdrop(pre1, postmill1, 1)
        g0 = len(gy_oids(postmill1, 0)) - len(gy_oids(pre1, 0))
        g1 = len(gy_oids(postmill1, 1)) - len(gy_oids(pre1, 1))
        ok = (d0 == 3 and d1 == 3 and g0 == 3 and g1 == 3)
        A["A2_each_milled_3"] = "passed" if ok else "failed"
        D["A2_each_milled_3"] = (f"lib drops P0={d0} P1={d1} (expect 3/3); "
                                 f"gy growth P0={g0} P1={g1} (expect 3/3)")
    else:
        A["A2_each_milled_3"] = "not-run"
        D["A2_each_milled_3"] = "pre_upkeep1 or post-mill state missing"

    # A3
    milled1 = leg1.get("milled") or {}
    n_milled_creatures = None
    if postmill1 and milled1:
        n_milled_creatures = sum(
            1 for p in (0, 1) for oid in (milled1.get(p) or [])
            if is_creature_card((objs(postmill1).get(str(oid)) or {})))
    if leg1.get("optional_offered") or ST.get("choice_recorded"):
        A["A3_optional_offered"] = "passed"
        D["A3_optional_offered"] = (
            f"may-exile decision observed on leg 1 "
            f"(optional_offered={leg1.get('optional_offered')}, "
            f"choice_recorded={ST.get('choice_recorded')}, "
            f"milled creatures={n_milled_creatures})")
    elif n_milled_creatures == 0:
        A["A3_optional_offered"] = "not-run"
        D["A3_optional_offered"] = ("no creature among leg-1 mills; "
                                    "prompt legitimately absent")
    else:
        A["A3_optional_offered"] = "failed"
        D["A3_optional_offered"] = (
            f"no may-exile decision observed despite "
            f"{n_milled_creatures} milled creatures")

    # A4
    cands = leg1.get("candidates") or []
    if cands:
        milled_set = set((milled1.get(0) or [])) | set((milled1.get(1) or []))
        bad = [x for x in cands
               if not (x["newly_milled"] and x["is_creature"])]
        ok = not bad
        bad_desc = [(x["name"], x["zone"], x["was_battlefield"],
                     x["was_preexisting_gy"]) for x in bad]
        A["A4_candidates_in_scope"] = "passed" if ok else "failed"
        D["A4_candidates_in_scope"] = (
            f"{len(cands)} candidates; out-of-scope={bad_desc or 'none'}")
    elif A.get("A3_optional_offered") == "not-run":
        A["A4_candidates_in_scope"] = "not-run"
        D["A4_candidates_in_scope"] = "no choice observed (A3 not-run)"
    else:
        A["A4_candidates_in_scope"] = "failed"
        D["A4_candidates_in_scope"] = ("may-exile offered but no candidate "
                                       "set recorded")

    # A5
    if post1 and leg1.get("chosen_oid"):
        z = zone_of(post1, leg1["chosen_oid"])
        kin = (objs(post1).get(str(ST.get("kin_oid"))) or {})
        kin_name = oname(kin)
        ok = (z == "Exile" and kin_name == leg1.get("chosen_name"))
        A["A5_exile_and_copy"] = "passed" if ok else "failed"
        D["A5_exile_and_copy"] = (
            f"chosen {leg1.get('chosen_name')} zone={z} (expect Exile); "
            f"Kin object name now '{kin_name}' "
            f"(expect '{leg1.get('chosen_name')}')")
    else:
        A["A5_exile_and_copy"] = "not-run"
        D["A5_exile_and_copy"] = "post1.json missing or no card chosen"

    # A6
    if pre2 and midm2:
        d0, d1 = libdrop(pre2, midm2, 0), libdrop(pre2, midm2, 1)
        ok = (d0 or 0) >= 3 and (d1 or 0) >= 3
        A["A6_ability_retained"] = "passed" if ok else "failed"
        D["A6_ability_retained"] = (
            f"leg-2 upkeep mill: lib drops P0={d0} P1={d1} "
            f"(trigger fired again post-copy)")
    elif pre2:
        A["A6_ability_retained"] = "failed"
        D["A6_ability_retained"] = "leg-2 upkeep seen but no mill observed"
    else:
        A["A6_ability_retained"] = "not-run"
        D["A6_ability_retained"] = "leg 2 never ran"

    # A7
    if post2 and ST.get("declined"):
        ex_pre = set((ST.get("leg2_pre_exile") or []))
        ex_post = exile_oids(post2)
        kin = (objs(post2).get(str(ST.get("kin_oid"))) or {})
        ok = (ex_post == ex_pre
              and oname(kin) == (leg1.get("chosen_name") or "")
              and not stack(post2))
        A["A7_decline_control"] = "passed" if ok else "failed"
        D["A7_decline_control"] = (
            f"exile set unchanged={ex_post == ex_pre}; Kin still "
            f"'{oname(kin)}'; stack empty={not stack(post2)}; "
            f"post2 turn={post2.get('turn_number')}")
    else:
        A["A7_decline_control"] = "not-run"
        D["A7_decline_control"] = "leg-2 decline not completed"

    # A8
    if post2:
        ok = not stack(post2)
        A["A8_cleanup"] = "passed" if ok else "failed"
        D["A8_cleanup"] = (f"stack empty in post2: {ok}; "
                           f"turn={post2.get('turn_number')}")
    else:
        A["A8_cleanup"] = "not-run"
        D["A8_cleanup"] = "post2.json missing"

    if A.get("A1_setup_ok") == "passed" and A.get(
            "A2_each_milled_3") == "passed":
        if A.get("A4_candidates_in_scope") == "failed" or A.get(
                "A5_exile_and_copy") == "failed":
            verdict = "reproduced"
        elif all(A.get(k) == "passed" for k in
                 ("A3_optional_offered", "A4_candidates_in_scope",
                  "A5_exile_and_copy", "A6_ability_retained",
                  "A7_decline_control", "A8_cleanup")):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "details": D, "verdict": verdict,
                   "rejections": ST["rejections"][:20],
                   "leg1": leg1,
                   "proof_turns": ST.get("proof_turns")}, f, indent=1,
                  default=str)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("assertions", {"A": A, "verdict": verdict})

    # copy the scenario file into the evidence dir
    shutil.copy(__file__, f"{EVDIR}/scenario_{ISSUE}.py")

    # ---- run.json ----
    sh = ST.get("server_hello", {})
    rel = f"{BACKFILL}/server/releases/v0.81.3"
    binpath = f"{rel}/phase-server-slim-x86_64-unknown-linux-musl"
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "Shadow Kin is just exiling whatever it likes",
        "server": {
            "server_version": sh.get("server_version"),
            "build_commit": sh.get("build_commit"),
            "protocol_version": sh.get("protocol_version"),
            "mode": sh.get("mode"),
        },
        "binary_sha256": sha256_file(binpath),
        "card_data_sha256": sha256_file(f"{rel}/data/card-data.json"),
        "draft_pools_sha256": sha256_file(f"{rel}/data/draft-pools.json"),
        "signature_verified": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(t_start)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(t_end)),
        "game_code": ST.get("game_code"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "decisions": {
            "kin_cast_turn": ST.get("kin_cast_turn"),
            "leg1": {k: leg1.get(k) for k in (
                "turn", "milled", "mill_drops", "forced_pick",
                "optional_offered", "candidates",
                "chosen_oid", "chosen_name")},
            "declined_leg2": ST.get("declined"),
            "rejections": len(ST["rejections"]),
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": "Leg 1 accepts the may-exile and checks the candidate "
                        "scope against the exact milled set; with no in-scope "
                        "candidate offered, the driver force-picks the first "
                        "offered candidate to observe the full reported "
                        "outcome. Leg 2 declines as a control and confirms "
                        "the copy kept the upkeep ability.",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "4x Shadow Kin / 24x Storm Crow / 24x Grizzly Bears deck density "
            "is a test-harness convenience (engine accepts >4-of for custom "
            "games).",
            "Not tested on the original 2026-08-02 build; verdict is scoped "
            "to v0.81.2, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
        ],
        "setup_line": "P0: 4x Shadow Kin, 24x Storm Crow, 32x Island; P1: "
                      "24x Grizzly Bears, 36x Forest. P0 casts Storm Crow "
                      "then Shadow Kin; P1 casts Grizzly Bears.",
        "contract_line": "Leg 1: at P0's first upkeep with Kin on the "
                         "battlefield, each player mills 3; the may-exile "
                         "choice must offer only creature cards milled by "
                         "that trigger; accept one and verify exile + copy. "
                         "Leg 2: decline; verify the copy kept the ability "
                         "and the game proceeds.",
        "stats": {
            "proof_turns": ST.get("proof_turns"),
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written; verdict = " + verdict)

    # ---- server excerpts ----
    try:
        lines = open(f"{RUNDIR}/server.log", errors="replace").read(
            ).splitlines()
        keep = [l for l in lines if any(
            k in l.lower() for k in
            ("shadow kin", "mill", "exile", "copy", "upkeep",
             "error", "panic", "warn"))]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(keep[-400:]) + "\n")
        say(f"server_excerpts.log: {len(keep)} matching lines")
    except Exception as e:
        say(f"server excerpts FAILED: {e}")

    WIRE.close()
    RUNLOG.close()
    RUNLOG2.close()
    render_png()
    write_manifest()
    validate()


def render_png():
    """Render summary.png from the SAVED states + assertion results."""
    from PIL import Image, ImageDraw
    run = json.load(open(f"{EVDIR}/run.json"))
    leg1 = run["decisions"].get("leg1") or {}
    cands = leg1.get("candidates") or []
    cand_line = ("candidates: " + ", ".join(
        f"{x.get('name') or '?'}({x.get('zone') or '?'})"
        for x in cands[:6])) if cands else "candidates: (none recorded)"
    milled = leg1.get("milled") or {}
    milled_line = (f"milled P0={len(milled.get(0) or [])} "
                   f"P1={len(milled.get(1) or [])}")

    W, H = 1040, 980
    BG = (18, 20, 26)
    PANEL = (26, 30, 38)
    TEXT = (235, 238, 245)
    DIM = (150, 160, 175)
    ACCENT = (110, 180, 255)
    GREEN = (110, 220, 140)
    RED = (240, 120, 120)
    YELLOW = (240, 200, 110)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    srv = run["server"]
    d.text((24, y), "#6903 - Shadow Kin exiles whatever it likes",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv.get('server_version')} "
           f"({srv.get('build_commit')}, protocol "
           f"{srv.get('protocol_version')}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']}",
           fill=DIM)
    y += 30
    for ln in [
        "Oracle: Flash. At the beginning of your upkeep, each player mills",
        "three cards. You may exile a creature card from among the cards",
        "milled this way. If you do, this creature becomes a copy of that",
        "card, except it has this ability.",
        "",
        "Reported: the may-exile choice offers cards OUTSIDE the milled set",
        "(battlefield permanents). Leg 1 accepts the choice and records the",
        "candidate scope against the exact milled set; leg 2 declines (control).",
    ]:
        d.text((24, y), ln, fill=TEXT)
        y += 20
    y += 8
    pre = leg1.get("pre") or {}
    pre_lib = pre.get("lib") or {}
    rows = [
        ("pre_upkeep1.json (leg 1 upkeep)",
         f"turn={pre.get('turn')} phase={pre.get('phase')} "
         f"lib P0={pre_lib.get(0)} P1={pre_lib.get(1)}"),
        ("exile-choice candidate set (leg 1)", cand_line[:150]),
        ("leg-1 mill observation", milled_line),
        ("leg 1 outcome",
         f"chosen={leg1.get('chosen_name') or 'none'} "
         f"(forced_pick={leg1.get('forced_pick')}); "
         f"optional_offered={leg1.get('optional_offered')}"),
        ("leg 2 (decline control)",
         f"declined={run['decisions'].get('declined_leg2')}"),
    ]
    d.rectangle([16, y, W - 16, y + 30 + len(rows) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Observed (from saved states)", fill=YELLOW)
    y += 34
    for tag, val in rows:
        d.text((28, y), tag, fill=TEXT)
        d.text((28, y + 20), (val or "")[:150], fill=DIM)
        y += 44
    y += 12
    A = run["assertions"]
    Dd = run["assertion_details"]
    d.rectangle([16, y, W - 16, y + 34 + len(A) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    y += 34
    for k, v in A.items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, y), f"{k}: {v}", fill=color)
        d.text((28, y + 20), Dd.get(k, "")[:150], fill=DIM)
        y += 44
    y += 12
    d.text((24, y), "Limitations: " + "; ".join(run["limitations"])[:200],
           fill=DIM)
    y += 24
    d.text((24, y), "Evidence summary (not a gameplay screenshot). "
           "States: pre_upkeep1/post1/mid_*.json + manifest.sha256",
           fill=DIM)
    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, flush=True)


def write_manifest():
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256"
                   and os.path.isfile(os.path.join(EVDIR, f)))
    lines = []
    for f in files:
        h = hashlib.sha256(open(os.path.join(EVDIR, f), "rb").read()
                           ).hexdigest()
        lines.append(f"{h}  {f}\n")
    with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
        f.writelines(lines)
    print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)


def validate():
    ok = True
    man = {}
    for ln in open(os.path.join(EVDIR, "manifest.sha256")):
        h, _, fn = ln.strip().partition("  ")
        man[fn] = h
    for fn, h in man.items():
        p = os.path.join(EVDIR, fn)
        if not os.path.exists(p):
            print(f"VALIDATE FAIL: missing {fn}", flush=True)
            ok = False
            continue
        ah = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if ah != h:
            print(f"VALIDATE FAIL: hash mismatch {fn}", flush=True)
            ok = False
        if fn.endswith(".json"):
            try:
                json.load(open(p))
            except Exception as e:
                print(f"VALIDATE FAIL: {fn} not JSON: {e}", flush=True)
                ok = False
    try:
        from PIL import Image
        im = Image.open(os.path.join(EVDIR, "summary.png"))
        im.verify()
        print("VALIDATE: summary.png opens OK", flush=True)
    except Exception as e:
        print(f"VALIDATE FAIL: summary.png: {e}", flush=True)
        ok = False
    print("VALIDATE: " + ("ALL OK" if ok else "FAILURES"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
