#!/usr/bin/env python3
"""Issue #6898: Cemetery Prowler reduces cost too much.

Reported: with ONE exiled instant, Cemetery Prowler reduces spell costs by
at least {3} instead of {1}. Oracle:

  Vigilance
  Whenever this creature enters or attacks, exile a card from a graveyard.
  Spells you cast cost {1} less to cast for each card type they share with
  cards exiled with this creature.

ENGINE-DATA CORRECTION (verified 2026-09-12 from pinned card-data.json):
Thunderous Wrath is parsed as {4}{R}{R} (generic 4 + two red shards), NOT
oracle {4}{R}. So the discriminator below is calibrated to engine data:
  correct  = pay {3}{R}{R} = 5 lands tapped
  reported bug (reduction >= {3}) = pay <= {1}{R}{R} = <= 3 lands tapped
The parent task's "exactly 4 Mountains" assumed oracle {4}{R}; that
assumption is wrong for this engine build and is NOT used here.

Suspected defect: the static ModifyCost is parsed with
dynamic_count ObjectCount(Typed Card) -- it counts exiled CARDS, not unique
shared card TYPES. Probe 2 (second instant exiled with the SAME Prowler via
its attack trigger) distinguishes: correct behavior still pays 5 (Instant
counted once); the card-counting bug pays 4.

Behavioral contract (native engine v0.81.1 / protocol 70, two human seats):
  Setup: P0 casts Lightning Bolt at P1 (instant -> P0 graveyard), then casts
  Cemetery Prowler; its ETB trigger exiles the Bolt.
  Proof 1: on a fresh P0 turn (0 tapped P0 lands) with >=5 untapped lands
  (>=2 Mountains), export pre.json, cast Thunderous Wrath at P1, let it
  resolve, export post.json. Tapped-P0-lands delta = mana actually paid.
  Probe 2: P0 casts a second Bolt at P1, attacks with Prowler; the attack
  trigger exiles Bolt #2 with the SAME Prowler. Fresh turn, pre2.json, cast
  Wrath #2 at P1, resolve, post2.json.

  A1 setup_ok       Prowler on P0 BF, exactly 1 exiled card, it is Lightning
                    Bolt (an instant).
  A2 wrath1_cast    Wrath #1 cast and resolved; P1 life dropped by exactly 5.
  A3 reduction_exact tapped P0 lands for Wrath #1 == 5 (reduction exactly {1}).
  A4 second_exile   second instant exiled with the same Prowler (2 exiled
                    cards, both Lightning Bolt).
  A5 no_multiply    Wrath #2 still taps exactly 5 P0 lands (unique shared
                    type counted once).
  A6 cleanup        stack empty in post2.json, game proceeded, no stall.

Verdict proposal: reproduced iff A3 fails (taps != 5); not-reproduced iff A3
(and A5 when run) pass; blocked iff setup never completed. Verdict is a
proposal only; the assertion table is authoritative.
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6898b"
EVDIR = f"{BACKFILL}/evidence/6898/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

PROWLER = "Cemetery Prowler"
BOLT = "Lightning Bolt"
WRATH = "Thunderous Wrath"
MOUNTAIN = "Mountain"
FOREST = "Forest"

P0_DECK = [(PROWLER, 8), (BOLT, 10), (WRATH, 8), (MOUNTAIN, 18), (FOREST, 16)]
P1_DECK = [(FOREST, 60)]
TIMEOUT = 1500

ST = {}
SUBMITTED_IIDS = set()
MIRACLE_SHAPES_LOGGED = set()


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


def life(state, pid):
    for p in (state.get("players") or []):
        if p.get("player_id") == pid or p.get("id") == pid \
                or p.get("seat") == pid:
            return p.get("life")
    return None


def objs(state):
    return state.get("objects") or {}


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


def lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if oname(o) in (MOUNTAIN, FOREST)]


def untapped_lands(state, pid):
    return [oid for oid, o in lands(state, pid) if not o.get("tapped")]


def untapped_mountains(state, pid):
    return [oid for oid, o in lands(state, pid)
            if oname(o) == MOUNTAIN and not o.get("tapped")]


def tapped_lands(state, pid):
    return [(oid, oname(o)) for oid, o in lands(state, pid)
            if o.get("tapped")]


def gy_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


def exiled(state, pid=None):
    out = []
    for oid, o in objs(state).items():
        if o.get("zone") == "Exile" and (pid is None or
                                         o.get("controller") == pid):
            out.append((oid, o))
    return out


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_player(state):
    return ((state.get("waiting_for") or {}).get("data") or {}).get("player")


def wf_desc(state):
    return ((state.get("waiting_for") or {}).get("data") or {}
            ).get("description", "")


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


def get_vi(st):
    vi = (st or {}).get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def current_opps(c):
    st = c.latest if c else None
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref, seat = None, None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if "seat" in d:
                    seat = d["seat"]
        o = objs(state).get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": (ch.get("text") or "")[:160]})
    return out


def build_submission(opp, choice_ids):
    """Build an Interaction submission matching the advertised response
    shape (AGENTS.md: exactChoices -> choose/choiceId; schema sequence ->
    sequence/choiceIds). choice_ids: list (possibly empty)."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    spec = ((resp.get("data") or {}).get("spec")) or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": list(choice_ids)}}
    if rtype == "exactChoices":
        if len(choice_ids) == 1:
            return {"type": "choose",
                    "data": {"choiceId": choice_ids[0]}}
        return None
    return None

# ---------------------------------------------------------------- prompts

def is_exile_prompt(state):
    d = wf_desc(state).lower()
    return ("exile" in d and "graveyard" in d)


def is_damage_prompt(state):
    d = wf_desc(state).lower()
    return ("damage" in d and "target" in d)


def is_exile_choice(state):
    """Prowler's 'exile a card from a graveyard' surfaces on protocol 70 as
    waiting_for type EffectZoneChoice with ChangeZone/Graveyard->Exile data
    (observed 2026-09-12; description is empty)."""
    d = (state.get("waiting_for") or {}).get("data") or {}
    return (d.get("effect_kind") == "ChangeZone"
            and str(d.get("destination", "")).lower() == "exile"
            and str(d.get("zone", "")).lower() == "graveyard")


async def answer_target_prompt(c, pid, state):
    """Handle (Trigger)TargetSelection for seat pid. Fingerprints:
    - Prowler exile trigger: desc mentions exile+graveyard -> choose Bolt.
    - Bolt/Wrath 'deals N damage to any target': desc mentions
      damage+target -> choose P1 (seat 1).
    Returns True if the prompt was pending (answered or deliberately held)."""
    wt = wf_type(state)
    if wt not in ("TargetSelection", "TriggerTargetSelection",
                  "EffectZoneChoice") \
            or wf_player(state) != pid:
        return False
    opps = current_opps(c)
    for opp in opps:
        iid = opp.get("interactionId")
        if iid in SUBMITTED_IIDS:
            wire("prompt_reopen_hold", {"iid": iid, "stage": ST.get("stage")})
            return True
        cands = candidate_info(opp, state)
        want = None
        kind = None
        awaiting = ST.get("awaiting_target")
        def is_gy(x):
            return str(x["zone"]).lower() == "graveyard"
        if awaiting == "exile" or is_exile_choice(state) or \
                is_exile_prompt(state):
            kind = "exile"
            # prefer a Lightning Bolt in MY graveyard
            want = next((x for x in cands
                         if x["name"] == BOLT and is_gy(x)
                         and x["controller"] == pid), None)
            if want is None:
                # fallback: any card in my graveyard
                want = next((x for x in cands
                             if is_gy(x)
                             and x["controller"] == pid), None)
            if want is None:
                wire("exile_prompt_no_bolt",
                     {"iid": iid, "cands": cands, "stage": ST.get("stage")})
                say(f"[{c.name}] exile prompt: no GY candidate; holding")
                return True
        elif awaiting == "damage":
            kind = "damage"
            want = next((x for x in cands if x["seat"] == 1), None)
            if want is None:
                wire("damage_prompt_no_p1",
                     {"iid": iid, "cands": cands, "stage": ST.get("stage")})
                say(f"[{c.name}] damage prompt: no P1 candidate; holding")
                return True
        elif is_damage_prompt(state):
            kind = "damage"
            want = next((x for x in cands if x["seat"] == 1), None)
            if want is None:
                wire("damage_prompt_no_p1",
                     {"iid": iid, "cands": cands, "stage": ST.get("stage")})
                say(f"[{c.name}] damage prompt: no P1 candidate; holding")
                return True
        elif cands and all(x["zone"] == "Graveyard" for x in cands):
            # undescribed graveyard-choice prompt: treat as the exile prompt
            kind = "exile"
            want = next((x for x in cands
                         if x["name"] == BOLT and x["controller"] == pid),
                        None) or next(
                (x for x in cands if x["controller"] == pid), None)
            if want is None:
                wire("exile_prompt_no_bolt",
                     {"iid": iid, "cands": cands, "stage": ST.get("stage")})
                return True
        else:
            continue  # not one of ours; try next opp
        sub = build_submission(opp, [want["choice_id"]])
        if sub is None:
            wire("prompt_unanswerable_shape",
                 {"iid": iid, "kind": kind, "stage": ST.get("stage"),
                  "rtype": (opp.get("response") or {}).get("type")})
            say(f"[{c.name}] {kind} prompt shape not answerable; holding")
            return True
        wire("interaction_submit",
             {"who": c.name, "iid": iid, "kind": kind,
              "response": sub, "want": want["ref"],
              "stage": ST.get("stage")})
        await c.send_interaction({"interactionId": iid, "response": sub})
        SUBMITTED_IIDS.add(iid)
        ST.setdefault("settle", {})[c.name] = c.revision
        ST["awaiting_target"] = None
        say(f"[{c.name}] answered {kind} prompt: "
            f"{want['name']} ref={want['ref']}")
        if kind == "exile":
            ST["exile_answers"] = ST.get("exile_answers", 0) + 1
        return True
    # a TargetSelection names us but no opportunity matched: hold, don't pass.
    # Dump the viewer_interaction once per (stage, wf_type) for diagnosis.
    dbg_key = (ST.get("stage"), wf_type(state))
    if dbg_key not in ST.setdefault("target_debug_logged", set()):
        ST["target_debug_logged"].add(dbg_key)
        vi = (c.latest or {}).get("viewer_interaction")
        wire("target_no_opp_debug",
             {"who": c.name, "stage": ST.get("stage"),
              "wf": (c.latest or {}).get("state", {}).get("waiting_for"),
              "vi": json.dumps(vi, default=str)[:3000]})
    wire("target_held_no_opp",
         {"who": c.name, "stage": ST.get("stage"), "wf_type": wf_type(state),
          "desc": wf_desc(state)[:160]})
    return True


async def maybe_decline_miracle(c, pid, state):
    """Defensive: Thunderous Wrath has Miracle {R}. If a may-cast/miracle
    opportunity appears for this seat, decline it explicitly (never cast for
    miracle -- it would corrupt the tapped-land accounting). Triggers on
    wf_type == "MiracleReveal" (observed 2026-09-12: empty description, so
    text matching alone misses it) or on "miracle" in choice texts/desc.
    Returns True if a miracle opportunity was pending."""
    if wf_player(state) != pid:
        return False
    wt = wf_type(state)
    opps = current_opps(c)
    for opp in opps:
        iid = opp.get("interactionId")
        rdata = (opp.get("response") or {}).get("data", {}) or {}
        choices = rdata.get("choices") or rdata.get("candidates") or []
        cands = candidate_info(opp, state)
        texts = " ".join((x["text"] or "") for x in cands).lower()
        desc = wf_desc(state).lower()
        is_miracle = (wt == "MiracleReveal"
                      or "miracle" in texts or "miracle" in desc)
        if not is_miracle:
            continue
        if iid in SUBMITTED_IIDS:
            return True
        if iid not in MIRACLE_SHAPES_LOGGED:
            MIRACLE_SHAPES_LOGGED.add(iid)
            raw_cands = []
            for ch in choices:
                raw_cands.append({
                    "id": ch.get("id"),
                    "text": ch.get("text"),
                    "surfaces": [
                        {"type": s.get("type"), "data": s.get("data")}
                        for s in ch.get("surfaces", []) or []]})
            wire("miracle_prompt_shape",
                 {"iid": iid, "wf_type": wt,
                  "wf_data": json.dumps(
                      (state.get("waiting_for") or {}).get("data") or {},
                      default=str)[:800],
                  "cands": cands, "stage": ST.get("stage"),
                  "raw_candidates": raw_cands})
        # Decline = the decideOptionalEffect / accept=false candidate,
        # identified by action-surface code + value surface, never by text
        # (observed 2026-09-12: castSpellAsMiracle auto/manual are the cast
        # options; texts are empty on all choices).
        decl_id = None
        for ch in choices:
            codes = [((s.get("data") or {}).get("code"))
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            vals = [(((s.get("data") or {}).get("role")),
                     ((s.get("data") or {}).get("value")))
                    for s in ch.get("surfaces", []) or []
                    if s.get("type") == "value"]
            if "decideOptionalEffect" in codes and ("accept", "false") in vals:
                decl_id = ch.get("id")
                break
        if decl_id is None:
            wire("miracle_ambiguous_decline",
                 {"iid": iid, "cands": cands, "stage": ST.get("stage")})
            say(f"[{c.name}] miracle decline ambiguous; holding")
            return True
        sub = build_submission(opp, [decl_id])
        if sub is None:
            wire("miracle_unanswerable", {"iid": iid, "stage": ST.get("stage")})
            say(f"[{c.name}] miracle decline shape unanswerable; holding")
            return True
        wire("interaction_submit",
             {"who": c.name, "iid": iid, "kind": "miracle_decline",
              "response": sub, "stage": ST.get("stage")})
        await c.send_interaction({"interactionId": iid, "response": sub})
        SUBMITTED_IIDS.add(iid)
        ST.setdefault("settle", {})[c.name] = c.revision
        ST["miracle_declines"] = ST.get("miracle_declines", 0) + 1
        say(f"[{c.name}] declined miracle (iid {iid})")
        return True
    return False


def discard_picks(state, pid):
    """Preference-ordered discard list for DiscardToHandSize."""
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

    take(FOREST, 0 if pid == 1 else 2)
    take(MOUNTAIN, 2)
    take(PROWLER, 1)
    # keep Wraths for the proofs; keep a Bolt until probe 2 is done
    take(WRATH, 0 if ST.get("stage") in ("DONE",) else 2)
    take(BOLT, 0 if ST.get("stage") in ("PROOF2", "PROOF2_CAST", "DONE")
         else 1)
    # anything left over
    for oid in hand:
        if len(picks) >= over:
            break
        if oid not in picks:
            picks.append(oid)
    return [int(x) for x in picks[:over]]


async def play_land(c, pid, state, acts):
    lid = find_hand(state, pid, FOREST) or find_hand(state, pid, MOUNTAIN)
    if not lid:
        return False
    la = next((x for x in acts if x["type"] == "PlayLand"
               and str(x.get("data", {}).get("object_id")) == lid), None)
    if la:
        await submit_as_is(c, la)
        return True
    return False

# ------------------------------------------------------------------ drivers

C0 = None
C1 = None


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> PROOF1 -> PROOF1_CAST -> SETUP2 ->
                            # ATTACK1 -> PROOF2 -> PROOF2_CAST -> DONE
        "stop": False,
        "turn_cap": 70,
        "rejections": [],
        "game_code": None,
        "bolt1_cast": False,
        "prowler1_cast": False,
        "exile1_done": False,
        "wrath1_cast": False,
        "life1_pre": None,
        "taps1": None,
        "bolt2_cast": False,
        "attack1_done": False,
        "exile2_done": False,
        "wrath2_cast": False,
        "life2_pre": None,
        "taps2": None,
        "awaiting_target": None,
        "target_debug_logged": set(),
        "held_debug_logged": set(),
        "exile_answers": 0,
        "miracle_declines": 0,
        "stall_logged": False,
    })
    SUBMITTED_IIDS.clear()
    MIRACLE_SHAPES_LOGGED.clear()


def prowler_ready_for_proof(state):
    """Fresh-turn gate for a Wrath cast: 0 tapped P0 lands (untap step ran),
    >=5 untapped lands, >=2 untapped Mountains, Wrath in hand."""
    if tapped_lands(state, 0):
        return False
    if len(untapped_lands(state, 0)) < 5:
        return False
    if len(untapped_mountains(state, 0)) < 2:
        return False
    return find_hand(state, 0, WRATH) is not None


async def p0_tick(c, pid, state, acts):
    # 1. pending decisions for this seat take absolute precedence
    if await answer_target_prompt(c, pid, state):
        return True
    if await maybe_decline_miracle(c, pid, state):
        return True
    # DeclareAttackers/DeclareBlockers surface as waiting_for types on
    # protocol 70: they are actionable steps, not decisions to hold.
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        dbg_key = ("held", ST.get("stage"), wf_type(state))
        if dbg_key not in ST.setdefault("held_debug_logged", set()):
            ST["held_debug_logged"].add(dbg_key)
            vi = (c.latest or {}).get("viewer_interaction")
            wire("held_decision_debug",
                 {"who": c.name, "stage": ST.get("stage"),
                  "wf": (c.latest or {}).get("state", {}).get("waiting_for"),
                  "vi": json.dumps(vi, default=str)[:3000]})
        wire("decision_held", {"who": c.name, "wf_type": wf_type(state),
                               "desc": wf_desc(state)[:160],
                               "stage": ST.get("stage")})
        return True

    stage = ST["stage"]
    if stage == "SETUP":
        if not is_my_main(state, pid):
            return False
        # goal A: Bolt#1 into own graveyard
        if not ST["bolt1_cast"]:
            if not gy_named(state, 0, BOLT) and not exiled(state, 0):
                oid = find_hand(state, pid, BOLT)
                a = castspell_advertised(acts, oid)
                if a and untapped_mountains(state, pid):
                    ST["awaiting_target"] = "damage"
                    await submit_as_is(c, a)
                    ST["bolt1_cast"] = True
                    say("P0 casts Lightning Bolt #1 at P1")
                    return True
        # goal B: Prowler#1 onto battlefield
        if ST["bolt1_cast"] and not ST["prowler1_cast"]:
            if not bf_named(state, pid, PROWLER):
                oid = find_hand(state, pid, PROWLER)
                a = castspell_advertised(acts, oid)
                if a:
                    await submit_as_is(c, a)
                    ST["prowler1_cast"] = True
                    say("P0 casts Cemetery Prowler #1")
                    return True
        # goal C: exile checkpoint (ETB trigger resolved)
        if ST["prowler1_cast"] and not ST["exile1_done"]:
            ex = exiled(state, 0)
            if len(ex) == 1 and oname(ex[0][1]) == BOLT:
                await export_now("exiles.json")
                ST["exile1_done"] = True
                ST["stage"] = "PROOF1"
                say("ETB exile checkpoint saved; stage -> PROOF1")
                return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "PROOF1":
        if not is_my_main(state, pid):
            return False
        await play_land(c, pid, state, acts)
        if prowler_ready_for_proof(state):
            oid = find_hand(state, pid, WRATH)
            a = castspell_advertised(acts, oid)
            if a:
                ST["life1_pre"] = life(state, 1)
                await export_now("pre.json")
                ST["awaiting_target"] = "damage"
                await submit_as_is(c, a)
                ST["wrath1_cast"] = True
                ST["stage"] = "PROOF1_CAST"
                say(f"P0 casts Thunderous Wrath #1 (P1 life {ST['life1_pre']})")
                return True
        return False

    if stage == "PROOF1_CAST":
        # wait for resolution: exactly -5 life on P1
        if ST["life1_pre"] is not None and life(state, 1) == \
                ST["life1_pre"] - 5:
            await export_now("post.json")
            s = json.loads(open(f"{EVDIR}/post.json").read())
            ps = s["state"] if isinstance(s.get("state"), dict) \
                else json.loads(s["state"])
            ST["taps1"] = {"total": len(tapped_lands(ps, 0)),
                           "mountains": sum(
                               1 for _, n in tapped_lands(ps, 0)
                               if n == MOUNTAIN)}
            ST["stage"] = "SETUP2"
            say(f"Wrath #1 resolved; taps={ST['taps1']}; stage -> SETUP2")
            return True
        return False

    if stage == "SETUP2":
        if not is_my_main(state, pid):
            return False
        # goal: Bolt#2 into own graveyard (for the attack-trigger exile)
        if not ST["bolt2_cast"]:
            if not gy_named(state, 0, BOLT):
                oid = find_hand(state, pid, BOLT)
                a = castspell_advertised(acts, oid)
                if a and untapped_mountains(state, pid):
                    ST["awaiting_target"] = "damage"
                    await submit_as_is(c, a)
                    ST["bolt2_cast"] = True
                    say("P0 casts Lightning Bolt #2 at P1")
                    return True
            else:
                ST["bolt2_cast"] = True
        if ST["bolt2_cast"]:
            ST["stage"] = "ATTACK1"
            say("stage -> ATTACK1")
            return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "ATTACK1":
        # declare the Prowler attack at DeclareAttackers
        if (state.get("active_player") == 0
                and (state.get("phase") or "") == "DeclareAttackers"
                and not ST["attack1_done"]):
            prow = bf_named(state, 0, PROWLER)
            for a in acts:
                if a["type"] == "DeclareAttackers" and prow:
                    sub = copy.deepcopy(a)
                    sub["data"]["attacks"] = [
                        [int(prow[0]), {"type": "Player", "data": 1}]]
                    sub["data"]["bands"] = []
                    await submit_as_is(c, sub)
                    ST["attack1_done"] = True
                    say("P0 attacks with Cemetery Prowler")
                    return True
        # attack trigger resolved -> second exile checkpoint
        if ST["attack1_done"] and not ST["exile2_done"]:
            ex = exiled(state, 0)
            names = sorted(oname(o) for _, o in ex)
            if len(ex) == 2 and names == [BOLT, BOLT]:
                await export_now("exiles2.json")
                ST["exile2_done"] = True
                ST["stage"] = "PROOF2"
                say("attack-trigger exile checkpoint saved; stage -> PROOF2")
                return True
        return False

    if stage == "PROOF2":
        if not is_my_main(state, pid):
            return False
        await play_land(c, pid, state, acts)
        if prowler_ready_for_proof(state):
            oid = find_hand(state, pid, WRATH)
            a = castspell_advertised(acts, oid)
            if a:
                ST["life2_pre"] = life(state, 1)
                await export_now("pre2.json")
                ST["awaiting_target"] = "damage"
                await submit_as_is(c, a)
                ST["wrath2_cast"] = True
                ST["stage"] = "PROOF2_CAST"
                say(f"P0 casts Thunderous Wrath #2 (P1 life {ST['life2_pre']})")
                return True
        return False

    if stage == "PROOF2_CAST":
        if ST["life2_pre"] is not None and life(state, 1) == \
                ST["life2_pre"] - 5:
            await export_now("post2.json")
            s = json.loads(open(f"{EVDIR}/post2.json").read())
            ps = s["state"] if isinstance(s.get("state"), dict) \
                else json.loads(s["state"])
            ST["taps2"] = {"total": len(tapped_lands(ps, 0)),
                           "mountains": sum(
                               1 for _, n in tapped_lands(ps, 0)
                               if n == MOUNTAIN)}
            ST["stage"] = "DONE"
            ST["stop"] = True
            say(f"Wrath #2 resolved; taps={ST['taps2']}; DONE")
            return True
        return False

    return False


async def p1_tick(c, pid, state, acts):
    if await answer_target_prompt(c, pid, state):
        return True
    if await maybe_decline_miracle(c, pid, state):
        return True
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        wire("decision_held", {"who": c.name, "wf_type": wf_type(state),
                               "stage": ST.get("stage")})
        return True
    if not is_my_main(state, pid):
        # still must answer DeclareBlockers when it is P1's step
        if (state.get("phase") or "") == "DeclareBlockers":
            for a in acts:
                if a["type"] == "DeclareBlockers":
                    sub = copy.deepcopy(a)
                    sub["data"]["assignments"] = []
                    await submit_as_is(c, sub)
                    return True
        return False
    await play_land(c, pid, state, acts)
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
    # empty attackers fallback (P0 only attacks in ATTACK1; P1 never)
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if (state.get("phase") or "") == "DeclareBlockers" and pid == 1:
        for a in acts:
            if a["type"] == "DeclareBlockers":
                sub = copy.deepcopy(a)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if wf_player(state) == pid and wf_type(state) in (
            "TargetSelection", "TriggerTargetSelection"):
        wire("target_held", {"who": c.name, "stage": ST.get("stage")})
        return True
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
        if not ST["stall_logged"] and turn > 20 and ST["stage"] in (
                "SETUP", "PROOF1", "SETUP2"):
            ST["stall_logged"] = True
            sstate = (st0.get("state") or {}) if st0 else {}
            hand = [oname(objs(sstate).get(oid, {}))
                    for oid in hand_oids(sstate, 0)]
            say(f"SETUP STALL? turn {turn} stage {ST['stage']}: "
                f"P0 hand={hand} bf={[oname(o) for _, o in bf(sstate, 0)]} "
                f"untapped={len(untapped_lands(sstate, 0))} "
                f"wf={sstate.get('waiting_for')}")
            wire("setup_stall", {"turn": turn, "hand": hand,
                                 "stage": ST["stage"]})
        if time.time() - last_progress > 300:
            say("no progress for 300s; stopping")
            for c in (C0, C1):
                st = c.latest
                if st:
                    wire("stall_state",
                         {"who": c.name,
                          "wf": st.get("state", {}).get("waiting_for"),
                          "phase": st.get("state", {}).get("phase"),
                          "acts": [a.get("type")
                                   for a in st.get("legal_actions", [])],
                          "stage": ST["stage"]})
            break
        await asyncio.sleep(0.15)

    say(f"loop ended: stage={ST['stage']} stop={ST['stop']}")
    wire("loop_end", {"stage": ST["stage"], "stop": ST["stop"]})
    await C0.close()
    await C1.close()

    # ---- assertions from SAVED states ----
    A, D = {}, {}
    ex1 = env_state(load_env("exiles.json"))
    pre = env_state(load_env("pre.json"))
    post = env_state(load_env("post.json"))
    ex2 = env_state(load_env("exiles2.json"))
    pre2 = env_state(load_env("pre2.json"))
    post2 = env_state(load_env("post2.json"))

    # A1
    if ex1:
        prow_bf = len(bf_named(ex1, 0, PROWLER)) > 0
        ex = exiled(ex1, 0)
        ex_names = sorted(oname(o) for _, o in ex)
        ok = prow_bf and len(ex) == 1 and ex_names == [BOLT]
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"prowler_on_bf={prow_bf} exiled_count={len(ex)} "
                            f"exiled_names={ex_names}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "exiles.json missing (ETB exile never checkpointed)"

    # A2
    if post and ST["life1_pre"] is not None:
        l_after = life(post, 1)
        ok = l_after == ST["life1_pre"] - 5
        A["A2_wrath1_cast"] = "passed" if ok else "failed"
        D["A2_wrath1_cast"] = (f"p1_life {ST['life1_pre']} -> {l_after} "
                               f"(delta {ST['life1_pre'] - l_after if l_after is not None else 'n/a'})")
    else:
        A["A2_wrath1_cast"] = "not-run"
        D["A2_wrath1_cast"] = "post.json missing or life1_pre unset"

    # A3
    if post and ST["taps1"] is not None:
        t = ST["taps1"]["total"]
        A["A3_reduction_exact"] = "passed" if t == 5 else "failed"
        D["A3_reduction_exact"] = (
            f"tapped_p0_lands={t} (mountains={ST['taps1']['mountains']}); "
            f"engine cost {4}{{R}}{{R}}={{6}}; correct reduction {{1}} -> "
            f"pay {{3}}{{R}}{{R}} = 5 lands")
    else:
        A["A3_reduction_exact"] = "not-run"
        D["A3_reduction_exact"] = "post.json missing"

    # A4
    if ex2:
        ex = exiled(ex2, 0)
        ex_names = sorted(oname(o) for _, o in ex)
        prow_bf = len(bf_named(ex2, 0, PROWLER)) > 0
        ok = prow_bf and len(ex) == 2 and ex_names == [BOLT, BOLT]
        A["A4_second_exile"] = "passed" if ok else "failed"
        D["A4_second_exile"] = (f"prowler_on_bf={prow_bf} exiled_count={len(ex)} "
                                f"exiled_names={ex_names}")
    else:
        A["A4_second_exile"] = "not-run"
        D["A4_second_exile"] = ("exiles2.json missing "
                                "(attack-trigger exile never checkpointed)")

    # A5
    if post2 and ST["taps2"] is not None:
        t = ST["taps2"]["total"]
        A["A5_no_multiply"] = "passed" if t == 5 else "failed"
        D["A5_no_multiply"] = (
            f"tapped_p0_lands={t} (mountains={ST['taps2']['mountains']}); "
            f"2 instants exiled with same Prowler; unique shared type "
            f"Instant counts once -> still 5")
    else:
        A["A5_no_multiply"] = "not-run"
        D["A5_no_multiply"] = "post2.json missing"

    # A6
    if post2:
        stack_empty = not (post2.get("stack") or [])
        ok = stack_empty and ST["stage"] == "DONE"
        A["A6_cleanup"] = "passed" if ok else "failed"
        D["A6_cleanup"] = (f"stack_empty={stack_empty} final_stage={ST['stage']} "
                           f"turn={post2.get('turn_number')}")
    elif post:
        stack_empty = not (post.get("stack") or [])
        A["A6_cleanup"] = "passed" if stack_empty else "failed"
        D["A6_cleanup"] = (f"post2 missing; post.json stack_empty={stack_empty} "
                           f"final_stage={ST['stage']}")
    else:
        A["A6_cleanup"] = "not-run"
        D["A6_cleanup"] = "no post state exported"

    if A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif A["A3_reduction_exact"] == "failed":
        verdict = "reproduced"
    elif A["A3_reduction_exact"] == "passed" and (
            A["A5_no_multiply"] in ("passed", "not-run")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    say("ASSERTIONS: " + json.dumps(A, indent=1))
    say("VERDICT (proposal): " + verdict)

    # ---- run.json ----
    sh = ST["server_hello"]
    run = {
        "issue": 6898,
        "run_id": RUN_ID,
        "title": "Cemetery Prowler reduces cost too much",
        "server": {
            "observed_hello": sh,
            "server_version": sh.get("server_version"),
            "build_commit": sh.get("build_commit"),
            "protocol_version": sh.get("protocol_version"),
            "mode": sh.get("mode"),
        },
        "binary_sha256":
            "d186daaea35a9fa0bb5acac52b82bbd6ba47eaebef9203cb49b402783a02313e",
        "card_data_sha256":
            "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
        "draft_pools_sha256":
            "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "game_code": ST["game_code"],
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "decisions": {
            "bolt1_cast": ST["bolt1_cast"],
            "prowler1_cast": ST["prowler1_cast"],
            "exile_answers": ST["exile_answers"],
            "miracle_declines": ST["miracle_declines"],
            "bolt2_cast": ST["bolt2_cast"],
            "attack1_done": ST["attack1_done"],
            "wrath1_cast": ST["wrath1_cast"],
            "wrath2_cast": ST["wrath2_cast"],
            "life1_pre": ST["life1_pre"],
            "life2_pre": ST["life2_pre"],
            "taps1": ST["taps1"],
            "taps2": ST["taps2"],
            "rejections": ST["rejections"],
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": ("proposal only, from A3 (+A5); the assertion table "
                         "is authoritative"),
        "limitations": [
            "Thunderous Wrath parsed as {4}{R}{R} (engine card-data), not "
            "oracle {4}{R}; discriminator calibrated to 5 lands correct.",
            "Tapped-land delta counts all P0 lands (engine auto-tap may use "
            "Forests for generic); mountain-only counts recorded as detail.",
            "Miracle {R} on Thunderous Wrath declined defensively if offered; "
            "decline count in decisions.",
            "Single shared server; no games.db copy (server-owned).",
        ],
        "stats": {
            "final_stage": ST["stage"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")

    # ---- scenario copy, server excerpts ----
    with open(f"{BACKFILL}/driver/scenario_6898.py") as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6898.py", "w") as f:
        f.write(src)
    say("copied scenario_6898.py to evidence")

    excerpts = []
    try:
        slog = open(f"{RUNDIR}/server.log").read().splitlines()
        gc = ST["game_code"] or ""
        excerpts = [ln for ln in slog if gc and gc in ln][-500:]
    except Exception as e:
        excerpts = [f"server log unreadable: {e}"]
    with open(f"{EVDIR}/server_excerpts.log", "w") as f:
        f.write("\n".join(excerpts) + "\n")
    say(f"wrote server_excerpts.log ({len(excerpts)} lines)")

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

    def st_of(fn):
        e = load_env(fn)
        s = env_state(e)
        return s

    W, H = 1040, 900
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
    d.text((24, y), "#6898 — Cemetery Prowler reduces cost too much",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv.get('server_version')} ({srv.get('build_commit')}, "
           f"protocol {srv.get('protocol_version')}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']} (proposal)",
           fill=DIM)
    y += 30

    lines = [
        "Setup: P0 Bolt->face (GY), Prowler ETB exiles the Bolt.",
        "Proof: fresh P0 turn, cast Thunderous Wrath (engine cost {4}{R}{R}=6).",
        "Correct = pay {3}{R}{R} = 5 lands tapped. Reported bug (>=3 reduction)",
        "would tap <=3 lands. Probe 2: 2nd instant exiled w/ same Prowler via",
        "attack trigger; correct still 5 (Instant counted once).",
    ]
    for ln in lines:
        d.text((24, y), ln, fill=TEXT)
        y += 20
    y += 8

    def taps(fn):
        s = st_of(fn)
        if not s:
            return "n/a"
        tl = tapped_lands(s, 0)
        m = sum(1 for _, n in tl if n == MOUNTAIN)
        return f"{len(tl)} total ({m} Mountains)"

    def exile_info(fn):
        s = st_of(fn)
        if not s:
            return "n/a"
        ex = exiled(s, 0)
        return f"{len(ex)}: " + ", ".join(sorted(oname(o) for _, o in ex))

    def plife(fn, pid):
        s = st_of(fn)
        return str(life(s, pid)) if s else "n/a"

    rows = [
        ("exiles.json (after ETB)", f"Prowler on BF; exiled = {exile_info('exiles.json')}"),
        ("pre.json -> post.json (Wrath #1)",
         f"P1 life {plife('pre.json', 1)} -> {plife('post.json', 1)}; "
         f"P0 lands tapped = {taps('post.json')}"),
        ("exiles2.json (after attack trigger)",
         f"exiled = {exile_info('exiles2.json')}"),
        ("pre2.json -> post2.json (Wrath #2)",
         f"P1 life {plife('pre2.json', 1)} -> {plife('post2.json', 1)}; "
         f"P0 lands tapped = {taps('post2.json')}"),
    ]
    d.rectangle([16, y, W - 16, y + 30 + len(rows) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Observed (from saved states)", fill=YELLOW)
    y += 34
    for tag, val in rows:
        d.text((28, y), tag, fill=TEXT)
        d.text((28, y + 20), val[:150], fill=DIM)
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
           "States: pre/post/pre2/post2/exiles/exiles2.json + manifest.sha256",
           fill=DIM)
    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, flush=True)


def write_manifest():
    import hashlib
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
    import hashlib
    from PIL import Image
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
        im = Image.open(os.path.join(EVDIR, "summary.png"))
        im.verify()
        print("VALIDATE: summary.png opens OK", flush=True)
    except Exception as e:
        print(f"VALIDATE FAIL: summary.png: {e}", flush=True)
        ok = False
    print("VALIDATE: " + ("ALL OK" if ok else "FAILURES"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
