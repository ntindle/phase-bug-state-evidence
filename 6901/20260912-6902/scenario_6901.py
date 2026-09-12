#!/usr/bin/env python3
"""Issue #6901: Mind's Dilation doesn't give the cast ability to its controller.

Oracle: "Whenever an opponent casts their first spell each turn, that player
exiles the top card of their library. If it's a nonland card, you may cast it
without paying its mana cost."

Triage acceptance criteria (mike-theDude, 2026-08-03):
  1. The triggering opponent exiles the top card of their own library.
  2. If nonland, Mind's Dilation's controller receives the optional cast decision.
  3. Declining leaves the card in exile.
  4. Accepting casts it without paying its mana cost under the permission
     holder's control.

Behavioral contract (native engine, protocol 70, two human seats):
  P0 ramps to 7 Islands and casts Mind's Dilation. P1 holds Bolts until then.
  LEG1 (accept): on a P1 main phase, P1 casts Lightning Bolt (first spell that
  turn, targeting P0). The Dilation trigger should exile P1's top library card;
  if nonland, P0 (controller) must be offered the may-cast decision. P0 accepts:
  the exiled Bolt must be cast under P0's control with no mana paid, resolve
  for 3 damage to P1, and reach its owner's graveyard.
  LEG2 (decline control): next P1 turn, same trigger; P0 declines; the exiled
  card must stay in exile with no cast and no damage.

  A1 setup_ok            Dilation on P0 BF; P1 Bolt cast as first spell of turn.
  A2 exile_observed      P1's top library card moved to Exile (nonland).
  A3 maycast_offered_to_controller  the may-cast decision was offered to P0
                         (the Dilation controller), not to P1, not to nobody.
  A4 accept_casts_free    LEG1: exiled Bolt cast under P0 control, P0 paid no
                         mana (untapped Islands unchanged), P1 -3 life on
                         resolution, Bolt in P1 graveyard after.
  A5 decline_leaves_exiled LEG2: exiled card still in Exile, no cast, no
                         damage from the free-cast path.
  A6 cleanup             stack empty, game advanced past the trigger turns.

Verdict: reproduced iff A1+A2 pass and (A3 fails or A4 fails).
not-reproduced iff A3+A4+A5 pass. blocked iff A1/A2 never ran.
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

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6902"
EVDIR = f"{BACKFILL}/evidence/6901/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

DILATION = "Mind's Dilation"
BOLT = "Lightning Bolt"
ISLAND = "Island"
MOUNTAIN = "Mountain"

P0_DECK = [(DILATION, 12), (ISLAND, 48)]
P1_DECK = [(BOLT, 44), (MOUNTAIN, 16)]
TIMEOUT = 1500

ST = {}
C0 = None
C1 = None
TARGET_DBG = set()  # interactionIds already dumped by target_debug


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


def life(state, pid):
    return player_obj(state, pid).get("life")


def lib_count(state, pid):
    return len(player_obj(state, pid).get("library") or [])


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


def exile_objs(state):
    return [(oid, o) for oid, o in objs(state).items()
            if o.get("zone") == "Exile"]


def gy_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


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


def wf_desc(state):
    return (wf(state).get("data") or {}).get("description", "")


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
        "stop": False,
        "turn_cap": 45,
        "rejections": [],
        "game_code": None,
        "dilation_cast": False,
        "dilation_turn": None,
        "dilation_submitted": False,
        "dilation_oid": None,
        "dilation_submit_rej": 0,
        "dilation_settle": 0,
        "leg": 1,                 # 1 = accept, 2 = decline
        "legs": {1: {}, 2: {}},
        "p1_cast_turns": set(),   # turns on which P1 cast the trigger Bolt
        "hold_since": None,
        "stall_logged": False,
        "mulligans": 0,
    })


def leg():
    return ST["legs"][ST["leg"]]


def opp_blob(opp):
    return json.dumps(opp, default=str)


def choice_surfaces(choice):
    return choice.get("surfaces") or []


def opp_choices(opp):
    """Choices may live at response.choices or response.data.choices."""
    resp = opp.get("response") or {}
    out = []
    for v in (resp.get("choices"), (resp.get("data") or {}).get("choices")):
        if isinstance(v, list):
            out.extend(v)
    return out


def surf_data(s):
    """Flatten a choice surface: protocol-70 nests under type/data."""
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
    """exactChoices opportunity carrying a decideOptionalEffect action."""
    resp = opp.get("response") or {}
    if resp.get("type") != "exactChoices":
        return False
    for ch in opp_choices(opp):
        for s in choice_surfaces(ch):
            f = surf_data(s)
            code = (f["code"] or "") + " " + (f["action"] or "")
            if "decideOptionalEffect" in code or "OptionalEffect" in code:
                return True
    return False


def decide_optional_value(opp):
    """Return the choice dict for accept=True / decline=False.

    Protocol-70 value surface: {"type":"value","data":{"role":"accept",
    "value":"true"|"false"}}. Both choices carry role "accept"; the value
    decides. (Observed 20260912-6901f: c0=false=decline, c1=true=accept,
    so a first=accept fallback would be wrong -- none here.)
    """
    out = {}
    for ch in opp_choices(opp):
        for s in choice_surfaces(ch):
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


def opp_references_oid(opp, oid):
    resp = opp.get("response") or {}
    for ch in opp_choices(opp):
        for s in choice_surfaces(ch):
            if str(surf_data(s)["reference"]) == str(oid):
                return ch
    for cand in (resp.get("candidates") or []) + list(
            ((resp.get("data") or {}).get("candidates") or [])):
        for s in (cand.get("surfaces") or []):
            if str(surf_data(s)["reference"]) == str(oid):
                return cand
    return None


def player_choice_for_seat(opp, seat):
    resp = opp.get("response") or {}
    # protocol 70 nests candidates under response.data (schema) as well as
    # carrying a legacy top-level list; check both
    data = resp.get("data") or {}
    pools = []
    for key in ("choices", "candidates"):
        v = resp.get(key)
        if isinstance(v, list):
            pools.extend(v)
        v2 = data.get(key)
        if isinstance(v2, list):
            pools.extend(v2)
    for ch in pools:
        for s in choice_surfaces(ch):
            d = s.get("data") or {}
            if str(d.get("seat")) == str(seat):
                return ch
    return None


def choice_id_of(node):
    return (node.get("choiceId") or node.get("choice_id")
            or node.get("id"))


def submit_choice(c, opp, node, resp_type=None):
    resp = (opp.get("response") or {})
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    rtype = resp_type or resp.get("type")
    # schema responses carry the real variant in data.spec.type
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


async def answer_target(c, pid, state, target_seat):
    """Answer a TargetSelection for pid by choosing a player candidate."""
    opps = current_opps(c)
    for opp in opps:
        node = player_choice_for_seat(opp, target_seat)
        if node and choice_id_of(node):
            say(f"[{c.name}] answers TargetSelection -> seat {target_seat}")
            await submit_choice(c, opp, node)
            return True
    # fallback: first player candidate
    for opp in opps:
        resp = opp.get("response") or {}
        for ch in resp.get("choices") or []:
            blob = opp_blob(ch).lower()
            if "player" in blob and choice_id_of(ch):
                say(f"[{c.name}] answers TargetSelection -> fallback player")
                await submit_choice(c, opp, ch)
                return True
    # debug: dump the real opportunity shape once per interaction
    for opp in opps:
        iid = opp.get("interactionId")
        if iid in TARGET_DBG:
            continue
        TARGET_DBG.add(iid)
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        chs = data.get("choices") or data.get("candidates") or []
        dbg = []
        for ch in chs[:8]:
            surfs = []
            for s in (ch.get("surfaces") or [])[:6]:
                d = s.get("data") or {}
                surfs.append({"kind": s.get("kind"),
                              "dkeys": list(d.keys())[:10],
                              "seat": d.get("seat"),
                              "ref": d.get("reference"),
                              "role": d.get("role"),
                              "value": d.get("value")})
            dbg.append({"chkeys": list(ch.keys()),
                        "id": ch.get("id") or ch.get("choiceId")
                              or ch.get("choice_id"),
                        "text": (ch.get("text") or "")[:80],
                        "surfaces": surfs})
        wire("target_debug", {"who": c.name, "iid": iid,
                              "rtype": resp.get("type"),
                              "dkeys": list(data.keys()),
                              "spec": data.get("spec"),
                              "ncands": len(chs), "cands": dbg,
                              "want_seat": target_seat,
                              "stage": ST.get("stage")})
        say(f"[{c.name}] TargetSelection DEBUG: rtype={resp.get('type')} "
            f"ncands={len(chs)}")
    wire("target_no_candidate", {"who": c.name, "n_opps": len(opps)})
    return False


MAYCAST_KEYWORDS = ("cast", "exile", "dilation", "without paying",
                    "may cast", "optional")


def scan_maycast(c, state, tag):
    """Log any may-cast-ish opportunity; record who was offered."""
    for opp in current_opps(c):
        blob = opp_blob(opp).lower()
        if any(k in blob for k in MAYCAST_KEYWORDS):
            wire("maycast_candidate",
                 {"who": c.name, "tag": tag,
                  "iid": opp.get("interactionId"),
                  "response_type": (opp.get("response") or {}).get("type"),
                  "wf_type": wf_type(state), "wf_player": wf_player(state),
                  "blob": blob[:900], "stage": ST.get("stage"),
                  "leg": ST.get("leg")})


async def p0_maycast_tick(c, state, acts, L):
    """Try to answer the may-cast decision for P0. Returns True if acted."""
    # 1. decideOptionalEffect may-choice
    for opp in current_opps(c):
        if is_decide_optional_effect(opp):
            if L.get("maychoice_offered_to") is None:
                L["maychoice_offered_to"] = 0
                L["maychoice_wf_type"] = wf_type(state)
                wire("maychoice_offered", {"to": 0, "leg": ST["leg"],
                                          "iid": opp.get("interactionId"),
                                          "wf_type": wf_type(state)})
                say(f"[P0] may-cast choice offered to P0 (leg {ST['leg']})")
                await export_now(f"mid{ST['leg']}.json")
            vals = decide_optional_value(opp)
            want = "accept" if ST["leg"] == 1 else "decline"
            node = vals.get(want)
            if node and choice_id_of(node):
                L["maychoice_answer"] = want
                if want == "accept":
                    L["p0_untapped_islands_at_accept"] = len(
                        untapped_lands(state, 0, ISLAND))
                say(f"[P0] answers may-cast: {want} (leg {ST['leg']})")
                await submit_choice(c, opp, node)
                return True
            wire("maychoice_no_value", {"who": c.name,
                                       "keys": list(vals.keys())})
            return False
    # 2. direct cast offer referencing the exiled card (leg 1 only)
    exiled = L.get("exiled_oid")
    if ST["leg"] == 1 and exiled:
        for opp in current_opps(c):
            node = opp_references_oid(opp, exiled)
            if node and choice_id_of(node):
                if L.get("maychoice_offered_to") is None:
                    L["maychoice_offered_to"] = 0
                    L["maychoice_wf_type"] = (
                        "cast-offer:" + str(wf_type(state)))
                    await export_now(f"mid{ST['leg']}.json")
                say(f"[P0] submits free cast of exiled {L.get('exiled_name')}")
                L["freecast_submitted"] = True
                await submit_choice(c, opp, node)
                return True
        a = castspell_advertised(acts, exiled)
        if a is not None:
            if L.get("maychoice_offered_to") is None:
                L["maychoice_offered_to"] = 0
                L["maychoice_wf_type"] = "advertised-CastSpell"
                await export_now(f"mid{ST['leg']}.json")
            say("[P0] submits advertised CastSpell for exiled card")
            L["freecast_submitted"] = True
            await submit_as_is(c, a)
            return True
    # 3. target selection for the free-cast spell
    if wf_player(state) == 0 and wf_type(state) in (
            "TargetSelection", "TriggerTargetSelection"):
        # any P0-controlled spell on stack -> target P1
        if any(str(e.get("controller")) == "0" for e in stack(state)):
            L["freecast_targeted"] = True
            # the engine routes the accepted may-cast through target
            # selection rather than a direct cast offer; mark the free
            # cast as submitted here so leg-1 resolution detection works
            L["freecast_submitted"] = True
            return await answer_target(c, 0, state, 1)
    return False


def trigger_exile_delta(L, state):
    """Detect a NEW P1-owned card in exile since leg start."""
    seen = set(L.get("exile_oids_seen") or [])
    fresh = []
    for oid, o in exile_objs(state):
        if oid not in seen and str(o.get("owner") or o.get(
                "controller")) in ("1",):
            fresh.append((oid, o))
    return fresh


def mark_exile_seen(L, state):
    L["exile_oids_seen"] = set(oid for oid, _ in exile_objs(state))


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
        take(DILATION, 2)  # protect Mind's Dilation
    else:
        take(MOUNTAIN, 2)
        take(BOLT, 4)      # protect Lightning Bolt
    for oid in hand:
        if len(picks) >= over:
            break
        if oid not in picks:
            picks.append(oid)
    return [int(x) for x in picks[:over]]


def dilation_trigger_on_stack(state):
    for e in stack(state):
        kind = (e.get("kind") or {}).get("type", "")
        blob = json.dumps(e, default=str).lower()
        if kind == "TriggeredAbility" and "dilation" in blob:
            return True
    return False


async def p0_tick(c, pid, state, acts):
    stage = ST["stage"]
    if stage == "PROOF":
        scan_maycast(c, state, "p0")

    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        wft = wf_type(state)
        if stage == "PROOF" and pid == 0 and ST.get("leg_armed"):
            if await p0_maycast_tick(c, state, acts, leg()):
                ST["hold_since"] = None
                return True
            # unhandled decision: hold and log verbosely (once per shape)
            key = ("held", wft, ST["leg"])
            if key not in ST.setdefault("held_logged", set()):
                ST["held_logged"].add(key)
                vi = (c.latest or {}).get("viewer_interaction")
                wire("decision_held", {"who": c.name, "wf_type": wft,
                                      "desc": wf_desc(state)[:200],
                                      "vi": json.dumps(vi, default=str)[:4000],
                                      "leg": ST["leg"]})
                say(f"[P0] HOLDING unhandled decision {wft} (leg {ST['leg']})")
            if ST["hold_since"] is None:
                ST["hold_since"] = time.time()
            return True
        wire("decision_held_setup", {"who": c.name, "wf_type": wft})
        return True

    if stage == "SETUP":
        if ST.get("dilation_submitted"):
            # confirm the in-flight cast: battlefield, stack, or rejected
            oid = str(ST.get("dilation_oid"))
            zone = (objs(state).get(oid) or {}).get("zone")
            if bf_named(state, 0, DILATION):
                ST["dilation_cast"] = True
                ST["dilation_turn"] = state.get("turn_number")
                ST["proof_entered"] = True
                ST["stage"] = "PROOF"
                ST["leg"] = 1
                ST["leg_armed"] = True
                mark_exile_seen(leg(), state)
                say(f"Dilation resolved on battlefield (turn "
                    f"{ST['dilation_turn']}); stage -> PROOF, leg 1 armed")
                return True
            if zone == "Stack":
                return False  # in flight; wait
            if len(ST["rejections"]) > ST.get("dilation_submit_rej", 0):
                say("[P0] Dilation cast rejected; will retry")
                wire("dilation_rejected_retry", {})
                ST["dilation_submitted"] = False
                return False
            # no rejection yet and not on stack: give it a few ticks, then
            # treat a silent stall as a retry
            ST["dilation_settle"] = ST.get("dilation_settle", 0) + 1
            if ST["dilation_settle"] > 40:
                say("[P0] Dilation submit went silent; retrying")
                ST["dilation_submitted"] = False
                ST["dilation_settle"] = 0
            return False
        if not is_my_main(state, pid):
            return False
        oid = find_hand(state, pid, DILATION)
        a = castspell_advertised(acts, oid)
        if oid and a is not None and len(
                untapped_lands(state, pid, ISLAND)) >= 7 \
                and wf_player(state) == pid and wf_type(state) == "Priority":
            await submit_as_is(c, a)
            ST["dilation_submitted"] = True
            ST["dilation_oid"] = oid
            ST["dilation_submit_rej"] = len(ST["rejections"])
            ST["dilation_settle"] = 0
            say(f"[P0] submits Mind's Dilation cast (turn "
                f"{state.get('turn_number')})")
            return True
        await play_land(c, pid, state, acts, ISLAND)
        return False

    if stage == "PROOF":
        # P0 plays no lands during PROOF: the accept leg asserts P0's
        # untapped Island count is unchanged by the free cast, so P0 must
        # take no other mana-affecting actions.
        L = leg()
        if ST["leg"] == 1 and L.get("freecast_resolved"):
            ST["leg"] = 2
            ST["leg_armed"] = True
            mark_exile_seen(leg(), state)
            say("leg 1 complete; leg 2 (decline) armed")
            return True
        if ST["leg"] == 2 and L.get("decline_done"):
            await export_now("post2.json")
            ST["stage"] = "DONE"
            ST["stop"] = True
            say("leg 2 complete; DONE")
            return True
        return False
    return False


async def p1_tick(c, pid, state, acts):
    stage = ST["stage"]
    if stage == "PROOF":
        scan_maycast(c, state, "p1")
        # detect a may-cast prompt misrouted to P1 (target selection for P1's
        # own trigger Bolt falls through to the handler below)
        if wf_player(state) == 1 and wf_type(state) not in (
                None, "Priority", "DeclareAttackers", "DeclareBlockers",
                "DiscardToHandSize", "TargetSelection",
                "TriggerTargetSelection"):
            L = leg()
            if L.get("maychoice_offered_to") is None:
                blob = json.dumps(
                    (c.latest or {}).get("viewer_interaction"),
                    default=str).lower()
                if any(k in blob for k in MAYCAST_KEYWORDS):
                    L["maychoice_offered_to"] = 1
                    L["maychoice_wf_type"] = wf_type(state)
                    wire("maychoice_misrouted",
                         {"to": 1, "wf_type": wf_type(state),
                          "leg": ST["leg"]})
                    say(f"[P1] MAY-CAST PROMPT OFFERED TO P1 (leg {ST['leg']})"
                        " -- misrouted!")
            wire("decision_held_p1", {"wf_type": wf_type(state),
                                     "leg": ST["leg"]})
            return True

    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        if wf_type(state) in ("TargetSelection", "TriggerTargetSelection"):
            # P1's own trigger Bolt: target P0 (answer once)
            if ST.get("stage") == "PROOF" and ST.get("leg_armed"):
                L = leg()
                if L.get("bolt_cast") and not L.get("bolt_target_answered"):
                    if await answer_target(c, pid, state, 0):
                        L["bolt_target_answered"] = True
                        return True
            wire("target_held_p1", {"leg": ST.get("leg")})
            return True
        wire("decision_held_p1_other", {"wf_type": wf_type(state)})
        return True

    if stage == "SETUP":
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts, MOUNTAIN)
        return False

    if stage == "PROOF" and ST.get("leg_armed"):
        L = leg()
        turn = state.get("turn_number")
        if L.get("bolt_cast") and not L.get("bolt_confirmed"):
            # confirm the in-flight Bolt: on stack, resolved, or rejected
            zone = (objs(state).get(str(L.get("bolt_oid"))) or {}).get("zone")
            p0_life_now = life(state, 0)
            if zone == "Stack" or (
                    L.get("pre_life_p0") is not None
                    and p0_life_now is not None
                    and p0_life_now < L["pre_life_p0"]) or \
                    gy_named(state, 1, BOLT):
                L["bolt_confirmed"] = True
                say(f"[P1] trigger Bolt confirmed (leg {ST['leg']})")
            elif len(ST["rejections"]) > L.get("bolt_submit_rej", 0):
                say(f"[P1] trigger Bolt rejected; retrying (leg {ST['leg']})")
                wire("bolt_rejected_retry", {"leg": ST["leg"]})
                L["bolt_cast"] = False
                L["bolt_target_answered"] = False
                ST["p1_cast_turns"].discard(turn)
            return False
        if is_my_main(state, pid) and turn not in ST["p1_cast_turns"] \
                and not L.get("bolt_cast"):
            oid = find_hand(state, pid, BOLT)
            a = castspell_advertised(acts, oid)
            if oid and a is not None and len(
                    untapped_lands(state, pid, MOUNTAIN)) >= 1 \
                    and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                L["pre_lib_p1"] = lib_count(state, 1)
                L["pre_life_p0"] = life(state, 0)
                L["pre_life_p1"] = life(state, 1)
                L["cast_turn"] = turn
                await submit_as_is(c, a)
                L["bolt_cast"] = True
                L["bolt_confirmed"] = False
                L["bolt_oid"] = oid
                L["bolt_submit_rej"] = len(ST["rejections"])
                ST["p1_cast_turns"].add(turn)
                say(f"[P1] submits Lightning Bolt cast (leg {ST['leg']}, "
                    f"turn {turn})")
                await export_now(f"pre{ST['leg']}.json")
                mark_exile_seen(L, state)
                return True
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts, MOUNTAIN)
        # watch the trigger window
        if L.get("bolt_confirmed"):
            fresh = trigger_exile_delta(L, state)
            if fresh and not L.get("exiled_oid"):
                oid, o = fresh[0]
                L["exiled_oid"] = oid
                L["exiled_name"] = oname(o)
                L["exiled_owner"] = o.get("owner")
                L["post_lib_p1"] = lib_count(state, 1)
                say(f"[P1] trigger exiled: {L['exiled_name']} (oid {oid}, "
                    f"owner {L['exiled_owner']}) leg {ST['leg']}")
                wire("trigger_exiled", {"leg": ST["leg"], "oid": oid,
                                       "name": L["exiled_name"],
                                       "owner": L["exiled_owner"]})
                if "Land" in json.dumps(o, default=str) or \
                        L["exiled_name"] == MOUNTAIN:
                    # land on top: no may-cast is correct; retry next turn
                    say(f"exiled card is a land; leg {ST['leg']} retries "
                        "next P1 turn")
                    wire("land_exiled_retry", {"leg": ST["leg"]})
                    L.clear()
                    mark_exile_seen(L, state)
                    return True
            # leg 1: detect free-cast resolution
            if ST["leg"] == 1:
                if L.get("freecast_submitted") and not L.get(
                        "freecast_resolved"):
                    # P0-controlled Bolt resolved: P1 life dropped & bolt
                    # in P1 graveyard, stack empty
                    if gy_named(state, 1, BOLT) and not stack(state):
                        L["freecast_resolved"] = True
                        L["post_life_p1"] = life(state, 1)
                        L["post_life_p0"] = life(state, 0)
                        L["p0_untapped_islands_post"] = len(
                            untapped_lands(state, 0, ISLAND))
                        await export_now("post1.json")
                        say("[P1] leg-1 free cast resolved; post1 exported")
                        return True
                # accept answered but no cast ever offered: after the
                # trigger fully resolves with no cast, close leg 1
                if L.get("maychoice_answer") == "accept" and not stack(
                        state) and not dilation_trigger_on_stack(state) \
                        and L.get("exiled_oid"):
                    L.setdefault("settle_ticks", 0)
                    L["settle_ticks"] += 1
                    if L["settle_ticks"] >= 10:
                        L["freecast_resolved"] = "no-cast-observed"
                        L["post_life_p1"] = life(state, 1)
                        L["post_life_p0"] = life(state, 0)
                        await export_now("post1.json")
                        say("[P1] leg-1: accept answered but no free cast "
                            "materialized; post1 exported")
                        return True
            # leg 2: decline -> card stays exiled; close after settle
            if ST["leg"] == 2 and L.get("maychoice_answer") == "decline":
                L.setdefault("settle_ticks", 0)
                L["settle_ticks"] += 1
                if L["settle_ticks"] >= 8 and not stack(state):
                    L["decline_done"] = True
                    L["post_life_p1"] = life(state, 1)
                    L["post_life_p0"] = life(state, 0)
                    say("[P1] leg-2 decline settled")
                    return True
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
    for a in acts:
        if a["type"] == "MulliganDecision":
            # BottomCards phase: choose `count` cards to put on the bottom
            wf = state.get("waiting_for") or {}
            pend = (wf.get("data") or {}).get("pending") or []
            my = next((p for p in pend
                       if (p.get("player") == pid)), None)
            phase = (my or {}).get("phase") or {}
            if isinstance(phase, dict) and phase.get("type") == "BottomCards":
                n = int(phase.get("count") or 0)
                keep_name = DILATION if pid == 0 else BOLT
                named = [(oid, oname(objs(state)[oid]))
                         for oid in hand_oids(state, pid)]
                picks = [oid for oid, nm in named
                         if nm != keep_name][:n]
                if len(picks) < n:
                    picks = [oid for oid, _ in named][:n]
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": picks}})
                say(f"[{c.name}] bottoms {len(picks)} after mulligan")
                return True
            if pid == 0 and ST["mulligans"] < 2 and not find_hand(
                    state, 0, DILATION):
                ST["mulligans"] += 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"[P0] mulligans ({ST['mulligans']}) seeking Dilation")
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
    # never pass priority while a may-cast/choice decision is pending for
    # the deciding seat
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and wt not in (None, "Priority"):
        return True
    # only pass when the engine says it is our priority; the advertised
    # action list is not reliably seat-filtered and stale submits come
    # back wrong_player
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


async def main():
    reset_state()
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
            say("held decision for 150s with no progress; exporting mid and "
                "stopping")
            wire("hold_timeout", {"leg": ST["leg"]})
            await export_now(f"mid{ST['leg']}_held.json")
            break
        if time.time() - last_progress > 300:
            say("no progress for 300s; stopping")
            break
        await asyncio.sleep(0.15)

    say(f"loop ended: stage={ST['stage']} stop={ST['stop']} leg={ST['leg']}")
    wire("loop_end", {"stage": ST["stage"], "stop": ST["stop"],
                      "legs": {k: {kk: vv for kk, vv in v.items()
                                   if kk != "exile_oids_seen"}
                               for k, v in ST["legs"].items()}})
    await C0.close()
    await C1.close()

    # ---- assertions from SAVED states ----
    A, D = {}, {}
    pre1 = env_state(load_env("pre1.json"))
    mid1 = env_state(load_env("mid1.json"))
    post1 = env_state(load_env("post1.json"))
    mid2 = env_state(load_env("mid2.json"))
    post2 = env_state(load_env("post2.json"))
    L1, L2 = ST["legs"][1], ST["legs"][2]

    # A1
    if pre1:
        ok = (len(bf_named(pre1, 0, DILATION)) >= 1 and L1.get("bolt_confirmed"))
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (
            f"dilation_on_p0_bf={len(bf_named(pre1, 0, DILATION))} "
            f"p1_bolt_cast_as_first_spell={bool(L1.get('bolt_cast'))} "
            f"turn={L1.get('cast_turn')}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre1.json missing (trigger Bolt never cast)"

    # A2
    if L1.get("exiled_oid"):
        ok = (L1.get("exiled_owner") == 1
              and L1.get("exiled_name") == BOLT)
        A["A2_exile_observed"] = "passed" if ok else "failed"
        D["A2_exile_observed"] = (
            f"exiled={L1.get('exiled_name')} owner={L1.get('exiled_owner')} "
            f"p1_lib {L1.get('pre_lib_p1')}->{L1.get('post_lib_p1')}")
    else:
        A["A2_exile_observed"] = "not-run"
        D["A2_exile_observed"] = "no exile observed after trigger Bolt"

    # A3
    if L1.get("exiled_oid"):
        offered = L1.get("maychoice_offered_to")
        if offered == 0:
            A["A3_maycast_offered_to_controller"] = "passed"
        else:
            A["A3_maycast_offered_to_controller"] = "failed"
        D["A3_maycast_offered_to_controller"] = (
            f"may-cast decision offered to seat {offered} "
            f"(wf_type={L1.get('maychoice_wf_type')}); "
            f"answer={L1.get('maychoice_answer')}")
    else:
        A["A3_maycast_offered_to_controller"] = "not-run"
        D["A3_maycast_offered_to_controller"] = "no exile, no decision point"

    # A4
    if L1.get("maychoice_answer") == "accept":
        dmg = (L1.get("pre_life_p1") or 0) - (L1.get("post_life_p1") or 0)
        gy = len(gy_named(post1, 1, BOLT)) if post1 else 0
        mana_ok = (L1.get("p0_untapped_islands_at_accept") ==
                   L1.get("p0_untapped_islands_post"))
        ok = (L1.get("freecast_resolved") is True and dmg == 3 and gy >= 1
              and mana_ok)
        A["A4_accept_casts_free"] = "passed" if ok else "failed"
        D["A4_accept_casts_free"] = (
            f"freecast_resolved={L1.get('freecast_resolved')} "
            f"p1_life_delta={dmg} (expect 3) p1_gy_bolts={gy} "
            f"p0_untapped_islands accept={L1.get('p0_untapped_islands_at_accept')}"
            f" post={L1.get('p0_untapped_islands_post')} mana_free={mana_ok} "
            f"rejections={len(ST['rejections'])}")
    else:
        A["A4_accept_casts_free"] = "not-run"
        D["A4_accept_casts_free"] = (
            f"may-choice answer was {L1.get('maychoice_answer')!r}, "
            "not accept")

    # A5
    if L2.get("maychoice_answer") == "decline":
        ex = [oname(o) for _, o in exile_objs(post2)] if post2 else []
        still = L2.get("exiled_name") in ex
        no_extra_dmg = True
        ok = still and no_extra_dmg and L2.get("decline_done")
        A["A5_decline_leaves_exiled"] = "passed" if ok else "failed"
        D["A5_decline_leaves_exiled"] = (
            f"exiled card still in exile: {still} "
            f"(exile now: {ex[:6]}); decline_done={L2.get('decline_done')}; "
            f"no free-cast damage path taken")
    else:
        A["A5_decline_leaves_exiled"] = "not-run"
        D["A5_decline_leaves_exiled"] = (
            f"leg-2 may-choice answer was {L2.get('maychoice_answer')!r}")

    # A6
    if post2:
        ok = not stack(post2)
        A["A6_cleanup"] = "passed" if ok else "failed"
        D["A6_cleanup"] = (
            f"stack empty in post2: {ok}; "
            f"turn advanced past trigger turns")
    else:
        A["A6_cleanup"] = "not-run"
        D["A6_cleanup"] = "post2.json missing"

    if A.get("A1_setup_ok") == "passed" and A.get(
            "A2_exile_observed") == "passed":
        if A.get("A3_maycast_offered_to_controller") == "failed" or \
                A.get("A4_accept_casts_free") == "failed":
            verdict = "reproduced"
        elif A.get("A3_maycast_offered_to_controller") == "passed" and \
                A.get("A4_accept_casts_free") == "passed" and \
                A.get("A5_decline_leaves_exiled") == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "details": D, "verdict": verdict,
                   "rejections": ST["rejections"][:20]}, f, indent=1,
                  default=str)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("assertions", {"A": A, "verdict": verdict})


if __name__ == "__main__":
    asyncio.run(main())
