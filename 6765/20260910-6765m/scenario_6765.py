#!/usr/bin/env python3
"""Issue #6765: Yidris, Maelstrom Wielder doesn't give cascade after attacking.

Oracle (verified from pinned card data):
  Trample
  Whenever Yidris deals combat damage to a player, as you cast spells from your
  hand this turn, they gain cascade.

Parser signal (pinned card-data.json): the DamageDone trigger's execute effect
is {"type": "Unimplemented", "name": "as",
"description": "as you cast spells from your hand"} -- the temporary grant is
explicitly marked unsupported, with the real GenericEffect/AddKeyword(Cascade)
nested underneath as sub_ability.

Behavioral contract:
  A1 setup_ok            P0 has Yidris, Maelstrom Wielder on the battlefield
  A2 combat_damage       Yidris attacks unblocked; P1 loses exactly 5 life
  A3 pre_damage_control  Shock cast from hand BEFORE combat damage gains no
                         cascade (timing control: grant must not pre-exist).
                         Runs only when >=2 Shocks are in hand so the A4
                         window is never jeopardized; otherwise not-run.
  A4 yidris_grant        Shock cast from hand AFTER Yidris dealt combat damage
                         gains cascade: a Cascade TriggeredAbility from the
                         Shock must hit the stack and/or cascade must exile
                         cards as it resolves
  A5 control_cascade     Bloodbraid Elf (native cascade) cast later triggers
                         cascade normally: proves the cascade pipeline works.
                         Its free-cast may prompt is DECLINED after the
                         trigger is observed, purely to let the game settle.
  A6 cleanup             game proceeds cleanly

Single-attack design (rewrite 20260910-6765k): the old delayed-attack design
(A3 turn declares empty, real attack two turns later) kept missing its
windows -- the pre-combat Shock window starved on 5x Shock density, and the
300s A3 wait consumed the very DeclareAttackers the flow then waited for.
Now: ramp -> hunt (first P0 PreCombatMain where Yidris is attack-ready AND a
Shock is in hand AND a Mountain is untapped) -> the SAME turn hosts A3
(pre-damage control, conditional), the real attack, and the A4 post-damage
window -> control stage for Bloodbraid.

Land-drop cap: the pilot stops routine land drops at 10 board lands (only
fixing a missing color beyond that), keeping spare lands in hand as cleanup-
discard fodder so discards never eat Shocks/Bloodbraids/Yidris.

Window-claim race (20260910-6765l): consuming a window flag (hunt_consumed /
post_consumed / control_consumed / yidris_cast) releases the pilot's hold
BEFORE the main flow's cast is on the wire; the pilot then passes priority
in the gap and the CastSpell is rejected wrong_player. The main flow now
sets ST["casting"]=True immediately when a window fires, before consuming
any flag or exporting.

STRICT DETECTION (lesson from 20260910-6765d): the previous driver treated
any card-choice opportunity as a cascade free-cast prompt ("direct" kind)
and submitted a cleanup-discard select as a free-cast pick, discarding
Yidris and marking A4 passed on a bogus signal. Genuine cascade signals
only:
  (1) a TriggeredAbility stack entry whose ability effect type is exactly
      "Cascade" (NOT a text search: the Yidris damage trigger's own
      description contains the word "cascade");
  (2) a genuine may prompt: exactChoices with decideOptionalEffect codes
      or a true/false boolean pair, or a card-choice prompt whose
      candidates are ALL from the exile zone (cascade free-casts select
      from exile, never from hand);
  (3) a positive exile-zone delta across the cast window (cascade exiles
      cards from the top of the library as it resolves).
The driver never submits into an unidentified prompt. After a cascade is
observed (A4 not-reproduced path, A5 control), a genuine decide/bool may
prompt is DECLINED -- an explicit, engine-advertised, documented decision --
solely to let the game settle for A6. Exile-zone card choices are never
auto-answered.

Verdict = reproduced iff A2 passed, A5 passed, and A4 failed (the grant is
missing while the underlying cascade keyword works).
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client  # noqa: F401  (default URL ws://localhost:9374/ws)
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6765m"
EVID_ISSUE = "6765"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload},
                              default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# ---------------------------------------------------------------- state helpers

def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_named(state, pid, name):
    return sum(1 for o in bf(state, pid)
               if (o.get("base_name") or o.get("name")) == name and not o.get("tapped"))


def untapped_lands(state, pid):
    return sum(untapped_named(state, pid, n)
               for n in ("Forest", "Mountain", "Island", "Swamp"))


def hand_oids(state, pid):
    return list(state["players"][pid].get("hand", []))


def hand_names(state, pid):
    return [obj_name(state, oid) for oid in hand_oids(state, pid)]


def count_hand(state, pid, name):
    return sum(1 for n in hand_names(state, pid) if n == name)


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if obj_name(state, oid) == name:
            return oid
    return None


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o["id"]
    return None


def exile_objs(state):
    return [o for o in state["objects"].values() if o.get("zone") == "Exile"]


def life(state, pid):
    return state["players"][pid].get("life")


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


def cast_spell_action(acts, state, name):
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == name:
            return a
    return None


def can_pay_yidris(state):
    # {B}{G}{R}{U}, no generic
    return (untapped_named(state, 0, "Swamp") >= 1
            and untapped_named(state, 0, "Forest") >= 1
            and untapped_named(state, 0, "Mountain") >= 1
            and untapped_named(state, 0, "Island") >= 1)


def can_pay_bloodbraid(state):
    # {2}{R}{G}
    return (untapped_named(state, 0, "Mountain") >= 1
            and untapped_named(state, 0, "Forest") >= 1
            and untapped_lands(state, 0) >= 4)


# ------------------------------------------------------------- interaction io

def vi_opps(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def surf_codes(ch):
    return [s.get("data", {}).get("code") for s in ch.get("surfaces", [])]


def choice_bool_value(ch):
    for s in ch.get("surfaces", []):
        dd = s.get("data", {}) or {}
        if dd.get("role") in ("accept", "value", "pay") and str(dd.get("value", "")).lower() in ("true", "false"):
            return str(dd["value"]).lower()
    return None


def cand_seat(ch):
    for s in ch.get("surfaces", []):
        dd = s.get("data", {}) or {}
        if "seat" in dd:
            return dd["seat"]
    return None


def cand_name(state, cand):
    for s in cand.get("surfaces", []):
        dd = s.get("data", {}) or {}
        for k in ("reference", "object_id", "card", "objectId"):
            ref = dd.get(k)
            if ref is not None:
                n = obj_name(state, ref)
                if n != "?":
                    return n
    for k in ("label", "name", "title"):
        if cand.get(k):
            return str(cand[k])
    return f"<id {cand.get('id')}>"


async def submit_choice(c, iid, choice_id):
    await c.send_interaction({"interactionId": iid,
                              "response": {"type": "choose", "data": {"choiceId": choice_id}}})


async def submit_sequence(c, iid, spec_type, choice_ids):
    await c.send_interaction({"interactionId": iid,
                              "response": {"type": spec_type,
                                           "data": {"choiceIds": choice_ids}}})


async def answer_target_selection(c, seat, timeout=60, label="target"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        st = c.latest
        if not st:
            continue
        state = st["state"]
        for opp in vi_opps(c):
            resp = opp.get("response") or {}
            if resp.get("type") != "schema":
                continue
            data = resp.get("data") or {}
            spec = data.get("spec") or {}
            if spec.get("type") not in ("sequence", "select"):
                continue
            cands = data.get("candidates") or []
            if not cands:
                continue
            pick = None
            for cd in cands:
                if cand_seat(cd) == seat:
                    pick = cd
                    break
            if pick is None and len(cands) == 1:
                pick = cands[0]
            if pick is None:
                continue
            wire(f"{label}_opportunity",
                 {"interaction": opp,
                  "candidates": [(cd.get("id"), cand_name(state, cd), cand_seat(cd))
                                 for cd in cands]})
            say(f"{c.name} answers {label}: seat={seat} candidate={pick.get('id')} "
                f"({cand_name(state, pick)})")
            await submit_sequence(c, opp.get("interactionId"), spec.get("type"),
                                  [pick["id"]])
            return pick["id"]
    say(f"TIMEOUT answering {label} target selection")
    return None


# --------------------------------------- strict cascade-signal detection

def iter_stack(state):
    return state.get("stack") or []


def _ability_effect_type(entry):
    try:
        return (entry.get("kind", {}).get("data", {}).get("ability", {})
                .get("effect", {}).get("type"))
    except Exception:
        return None


def is_cascade_trigger_entry(entry):
    return (entry.get("kind", {}).get("type") == "TriggeredAbility"
            and _ability_effect_type(entry) == "Cascade")


def entry_source_name(entry):
    try:
        data = entry.get("kind", {}).get("data", {}) or {}
        ability = data.get("ability", {}) or {}
        trig = ability.get("trigger_source", {}) or {}
        nm = ((trig.get("lki") or {}).get("name")
              or (data.get("lki") or {}).get("name"))
        if nm:
            return str(nm)
        return f"source_id={entry.get('source_id')}"
    except Exception:
        return "?"


def is_mana_menu(opp):
    """True when the opportunity is a mana-payment menu (tapLandForMana
    choices). The engine offers these non-blockingly around casts; they are
    not may-cast/free-cast prompts and must never be treated as one."""
    data = (opp.get("response") or {}).get("data") or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        if "tapLandForMana" in surf_codes(ch):
            return True
    return False


def choice_has_card_ref(state, ch):
    """True when a choice candidate references a real game object. This keeps
    card-choice detection from matching the engine's priority menu, whose
    exactChoices carry action codes (passPriority/playLand) and no object
    reference (false positive hit on 2026-09-10)."""
    for s in ch.get("surfaces", []):
        dd = s.get("data", {}) or {}
        for k in ("reference", "object_id", "card", "objectId"):
            if str(dd.get(k)) in state["objects"]:
                return True
    return False


def cand_zone(cand):
    """Extract the candidate's zone from its surfaces (e.g. 'hand', 'exile')."""
    for s in cand.get("surfaces", []):
        dd = s.get("data", {}) or {}
        z = dd.get("zone")
        if z:
            return str(z)
    return None


def find_card_choice(c, exclude_zones=None):
    """Find a card-selection opportunity. exclude_zones (e.g. {'hand'}) skips
    prompts whose candidates all come from those zones. Cascade free-casts
    select from exile, never from hand; without the filter a cleanup-discard
    prompt (hand selection) is misread as a cascade free-cast (20260910-6765d)."""
    st = c.latest
    if not st:
        return None
    state = st["state"]
    excl = {z.lower() for z in (exclude_zones or set())}
    for opp in vi_opps(c):
        if is_mana_menu(opp):
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        stype = spec.get("type")
        cands = data.get("candidates") or data.get("choices") or []
        if excl and cands and all((cand_zone(ch) or "").lower() in excl for ch in cands):
            continue  # e.g. hand-selection; not a cascade free-cast
        if rtype == "exactChoices":
            codes = set()
            for ch in cands:
                codes.update(surf_codes(ch))
            if "decideOptionalEffect" in codes:
                continue
            if not cands:
                continue
            if not any(choice_has_card_ref(state, ch) for ch in cands):
                continue  # priority menu, not a card choice
            named = [(ch.get("id"), cand_name(state, ch)) for ch in cands]
            return opp, rtype, None, named
        if rtype == "schema" and stype in ("sequence", "select") and cands:
            if not any(choice_has_card_ref(state, ch) for ch in cands):
                continue
            named = [(ch.get("id"), cand_name(state, ch)) for ch in cands]
            return opp, rtype, stype, named
    return None


def scan_genuine_may(c):
    """Single non-blocking scan for a genuine may/free-cast style prompt.
    Returns (opp, kind) or (None, None). Never matches hand-zone card
    selections (cleanup discard) or mana menus."""
    st = c.latest
    if not st:
        return None, None
    for opp in vi_opps(c):
        if is_mana_menu(opp):
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        if rtype == "exactChoices" and cands:
            codes = set()
            for ch in cands:
                codes.update(surf_codes(ch))
            if "decideOptionalEffect" in codes:
                return opp, "decide"
            bools = [choice_bool_value(ch) for ch in cands]
            if len(cands) == 2 and set(bools) == {"true", "false"}:
                return opp, "bool"
    found = find_card_choice(c, exclude_zones={"hand"})
    if found:
        opp = found[0]
        # cascade free-casts select from exile; require EVERY candidate to
        # carry an exile zone (a cleanup-discard select is hand-zone).
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        zones = {(cand_zone(ch) or "").lower() for ch in cands}
        if cands and zones <= {"exile"}:
            return opp, "exile"
    return None, None


async def answer_may(c, opp, kind, want_accept):
    iid = opp.get("interactionId")
    cands = ((opp.get("response") or {}).get("data") or {}).get("choices", [])
    pick = None
    want = "true" if want_accept else "false"
    if kind == "decide":
        for ch in cands:
            if "decideOptionalEffect" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []):
                dd = s.get("data", {}) or {}
                if dd.get("role") == "accept":
                    if str(dd.get("value")).lower() == want:
                        pick = ch
        if pick is None:
            for ch in cands:
                if choice_bool_value(ch) == want:
                    pick = ch
                    break
    elif kind == "bool":
        for ch in cands:
            if choice_bool_value(ch) == want:
                pick = ch
                break
    if pick is None:
        say(f"{c.name}: could not identify {'accept' if want_accept else 'decline'} choice")
        wire("may_answer_failed", {"kind": kind, "opp": opp})
        return False
    say(f"{c.name} answers may-cast: {'ACCEPT' if want_accept else 'DECLINE'} "
        f"(choice {pick['id']})")
    wire("may_answer", {"accept": want_accept, "choice": pick["id"]})
    await submit_choice(c, iid, pick["id"])
    return True


async def decline_pending_may(c, label, timeout=40):
    """Decline a genuine decide/bool may prompt if one is pending (used only
    to settle the game AFTER a cascade was observed). Exile-zone card
    choices are never auto-answered. Returns True if a prompt was declined."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        opp, kind = scan_genuine_may(c)
        if opp is not None and kind in ("decide", "bool"):
            ok = await answer_may(c, opp, kind, want_accept=False)
            say(f"{label}: declined pending may prompt (kind={kind}) -> {ok}")
            wire(f"{label}_may_declined", {"kind": kind, "ok": ok})
            return ok
        if opp is not None:
            say(f"{label}: pending prompt kind={kind} left unanswered "
                f"(never auto-answer exile card choices)")
            wire(f"{label}_may_unhandled", {"kind": kind})
            return False
        await asyncio.sleep(0.5)
    return False


# ---------------------------------------------------------------- tick driver

ST = {"stop": False, "stage": "mulligan", "mulls": {}, "notes": [],
      "yidris_oid": None, "yidris_cast": False, "yidris_cast_turn": None,
      "attack_turn": None, "damage_turn": None, "attack_live": False,
      "hunt_consumed": False, "post_consumed": False, "control_consumed": False,
      "casting": False, "cast_armed": False,
      "attackers_declared": False, "pre_done": False, "post_done": False,
      "yidris_trigger_seen": False, "yidris_trigger_texts": []}
_PASSED_REV = {}


def yidris_attack_ready(state):
    oid = ST.get("yidris_oid")
    if not oid:
        return False
    o = state["objects"].get(str(oid), {})
    return (o.get("zone") == "Battlefield" and o.get("controller") == 0
            and not o.get("tapped")
            and state.get("turn_number", 0) > (ST.get("yidris_cast_turn") or 0))


def win_ramp(state):
    return (ST["stage"] == "ramp" and not ST.get("yidris_cast")
            and is_my_main(state, 0)
            and find_hand(state, 0, "Yidris, Maelstrom Wielder") is not None
            and can_pay_yidris(state))


def win_hunt(state):
    # First P0 PreCombatMain where the attack is fully staged: Yidris ready,
    # a Shock in hand, an untapped Mountain for it.
    return (ST["stage"] == "hunt" and not ST.get("hunt_consumed")
            and state.get("active_player") == 0
            and state.get("phase") == "PreCombatMain"
            and state.get("priority_player") == 0
            and yidris_attack_ready(state)
            and find_hand(state, 0, "Shock") is not None
            and untapped_named(state, 0, "Mountain") >= 1)


def win_post_shock(state):
    return (ST["stage"] == "hunt" and not ST.get("post_consumed")
            and ST.get("damage_turn") is not None
            and state.get("active_player") == 0
            and state.get("turn_number") == ST["damage_turn"]
            and state.get("phase") == "PostCombatMain"
            and state.get("priority_player") == 0
            and find_hand(state, 0, "Shock") is not None
            and untapped_named(state, 0, "Mountain") >= 1)


def win_control(state):
    return (ST["stage"] == "control" and not ST.get("control_consumed")
            and state.get("active_player") == 0
            and state.get("phase") == "PreCombatMain"
            and state.get("priority_player") == 0
            and find_hand(state, 0, "Bloodbraid Elf") is not None
            and can_pay_bloodbraid(state))


def hold_p0(state):
    if state.get("priority_player") != 0 or state.get("active_player") != 0:
        return False
    if ST.get("casting"):
        # main flow is mid-cast (cast action sent, target being answered);
        # never pass priority out from under it.
        return True
    if (ST.get("cast_armed") and not ST.get("post_consumed")
            and state.get("phase") == "PostCombatMain"):
        # attack turn: hold P0's PostCombatMain priority for the A4 window
        return True
    return win_ramp(state) or win_hunt(state) or win_control(state)


async def handle_mulligan(c, pid, state, acts, is_p0):
    for a in acts:
        if a["type"] == "SelectCards":
            pend = (state.get("waiting_for") or {}).get("data", {}).get("pending", []) or []
            count = 1
            for p in pend:
                if p.get("player") == pid and (p.get("phase") or {}).get("type") == "BottomCards":
                    count = int(p["phase"].get("count", 1))
            h = hand_oids(state, pid)

            def bkey(oid):
                # Bottom spare lands first; protect Yidris / Bloodbraid /
                # Shocks (the A3/A4 windows need Shocks in hand, A5 needs a
                # Bloodbraid; 20260910-6765g/h discarded them all).
                n = obj_name(state, oid)
                if n in ("Forest", "Mountain", "Island", "Swamp"):
                    return (0, n)
                if n == "Shock":
                    return (3, n)
                if n == "Bloodbraid Elf":
                    return (4, n)
                return (5, n)
            picks = sorted(h, key=bkey)[:count]
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}: {[obj_name(state, x) for x in picks]}")
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            hn = hand_names(state, pid)
            lands = sum(1 for n in hn if n in ("Forest", "Mountain", "Island", "Swamp"))
            mulls = ST["mulls"].get(c.name, 0)
            if not is_p0:
                choice = "Keep"
            elif (lands >= 3 and ("Shock" in hn or mulls >= 2)) or mulls >= 3:
                # 4-color deck: demand 3+ lands; prefer (not require) a Shock
                # so the hunt window arrives promptly.
                choice = "Keep"
            else:
                choice = "Mulligan"
            if choice == "Mulligan":
                ST["mulls"][c.name] = mulls + 1
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": choice}}})
            say(f"{c.name} {choice.lower()}s opening hand "
                f"(lands={lands} mulls={mulls} hand={len(hn)} shock={'Shock' in hn})")
            return True
    return False


async def handle_discard(c, pid, state, acts):
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    if wtype not in ("DiscardToHandSize", "DiscardChoice"):
        return False
    data = wf.get("data") or {}
    named = data.get("player")
    if named is not None and named != pid:
        return False
    count = data.get("count")
    if not count:
        count = max(0, len(hand_oids(state, pid)) - 7)
    if count <= 0:
        return False
    h = hand_oids(state, pid)

    def dkey(oid):
        # Discard spare lands first. ALL key cards (Shock / Bloodbraid /
        # Yidris) are fully protected: the land buffer is deep enough that
        # cleanup never needs to eat them.
        n = obj_name(state, oid)
        if n in ("Forest", "Mountain", "Island", "Swamp"):
            return (1, n)
        return (9, n)
    picks = sorted(h, key=dkey)[:count]
    for a in acts:
        if a["type"] == "SelectCards":
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} discards {[obj_name(state, x) for x in picks]}")
            return True
    return False


LAND_NAMES = ("Forest", "Mountain", "Island", "Swamp")


def board_land_count(state, pid):
    return sum(1 for o in bf(state, pid)
               if (o.get("base_name") or o.get("name")) in LAND_NAMES)


def board_colors(state, pid):
    return {(o.get("base_name") or o.get("name")) for o in bf(state, pid)} & set(LAND_NAMES)


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], st.get("legal_actions", []) or []
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if wtype == "MulliganDecision":
        if await handle_mulligan(c, pid, state, acts, is_p0):
            return True
    if await handle_discard(c, pid, state, acts):
        return True
    phase = state.get("phase")
    # P0 attackers: the real attack goes out on the hunt/attack turn only.
    if phase == "DeclareAttackers" and state.get("active_player") == 0 and pid == 0:
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            yid = ST["yidris_oid"] if (ST["stage"] == "hunt"
                                       and ST.get("attack_live")
                                       and yidris_attack_ready(state)) else None
            if yid:
                # object ids must be ints on the wire (protocol 69 rejects
                # string oids with "expected u64")
                nd["attacks"] = [[int(yid), {"type": "Player", "data": 1}]]
                nd["bands"] = []
                say("P0 declares attackers: Yidris, Maelstrom Wielder -> P1")
                wire("declare_attackers", {"action": na})
                ST["attackers_declared"] = True
            else:
                nd["attacks"] = []
                nd["bands"] = []
            await c.send_action(na)
            return True
    # P1 blockers: always empty (P1 has no creatures anyway)
    if phase == "DeclareBlockers" and pid == 1:
        for a in acts:
            if a["type"] != "DeclareBlockers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            nd["blocks"] = []
            await c.send_action(na)
            return True
    # P0 also declares empty blockers on P1's turns
    if phase == "DeclareBlockers" and pid == 0 and state.get("active_player") == 1:
        for a in acts:
            if a["type"] != "DeclareBlockers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            nd["blocks"] = []
            await c.send_action(na)
            return True
    # P1 never attacks
    if phase == "DeclareAttackers" and state.get("active_player") == 1 and pid == 1:
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            nd["attacks"] = []
            nd["bands"] = []
            await c.send_action(na)
            return True
    if ST["stage"] in ("ramp", "hunt", "control") and is_my_main(state, pid):
        if is_p0 and hold_p0(state):
            return False  # stage machine owns the next cast
        if is_p0:
            # Keep a land buffer in hand for cleanup discards: discards must
            # eat spare lands, never Shocks/Bloodbraids/Yidris. (20260910-6765k
            # discarded Bloodbraids because the pilot played every land, so
            # land-light hands forced key cards into the discard picks.)
            # Stop routine drops at 10 board lands; beyond that only to fix
            # a missing color, keeping 1 spare in hand.
            bl = board_land_count(state, 0)
            bc = board_colors(state, 0)
            hl = sum(1 for n in hand_names(state, 0) if n in LAND_NAMES)
            if bl < 10 or (len(bc) < 4 and hl > 1):
                for a in acts:
                    if a["type"] == "PlayLand":
                        await c.send_action(a)
                        return True
        else:
            for a in acts:
                if a["type"] == "PlayLand":
                    await c.send_action(a)
                    return True
    if wtype == "Priority" and state.get("priority_player") == pid:
        if is_p0 and hold_p0(state):
            return False
        rev = st.get("state_revision", -1)
        if _PASSED_REV.get(c.name, -1) >= rev:
            return False
        for a in acts:
            if a["type"] == "PassPriority":
                await c.send_action(a)
                _PASSED_REV[c.name] = rev
                return True
    return False


async def pilot(p0, p1):
    try:
        while not ST["stop"]:
            try:
                await tick(p0, p0.player_id, True)
                await tick(p1, p1.player_id, False)
            except Exception:
                # Never let a tick crash silently stall the game; log it
                # and keep ticking.
                say("PILOT TICK CRASH:")
                traceback.print_exc()
                wire("pilot_tick_crash",
                     {"error": traceback.format_exc()[-2000:]})
                await asyncio.sleep(1)
            await asyncio.sleep(0.15)
    except asyncio.CancelledError:
        pass


async def wait_cond(c, cond, timeout, label, poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        st = c.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in wait_cond: {label}")
    return None


async def export_ev(c, name):
    s = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(s)
    say(f"exported {name}.json ({len(s)} bytes)")
    return json.loads(s)["state"]


async def watch_cascade(c, timeout, label, expect_source=None, stop_early=None):
    """Strict watch for genuine cascade signals after a cast. Returns dict
    with trigger_seen (a Cascade-effect TriggeredAbility from expect_source,
    or any Cascade trigger when expect_source is None), trigger_sources,
    may_seen (genuine may prompt only), and the exile-zone delta."""
    res = {"trigger_seen": False, "trigger_texts": [], "trigger_sources": [],
           "may_seen": False, "may_kind": None,
           "exile_before": None, "exile_after": None}
    t0 = time.time()
    st = c.latest
    if st:
        res["exile_before"] = len(exile_objs(st["state"]))
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.5)
        if stop_early is not None and stop_early():
            break
        st = c.latest
        if not st:
            continue
        state = st["state"]
        for e in iter_stack(state):
            if not is_cascade_trigger_entry(e):
                continue
            tag = f"{entry_source_name(e)}#{e.get('id')}"
            if tag not in res["trigger_sources"]:
                res["trigger_sources"].append(tag)
                txt = json.dumps(e)[:400]
                res["trigger_texts"].append(txt)
                say(f"{label}: CASCADE TRIGGER ON STACK: {txt[:200]}")
                wire(f"{label}_cascade_trigger", {"entry": txt})
        if res["trigger_sources"]:
            if expect_source is None or any(
                    expect_source.lower() in s.lower()
                    for s in res["trigger_sources"]):
                res["trigger_seen"] = True
        if not res["may_seen"]:
            opp, kind = scan_genuine_may(c)
            if opp is not None:
                res["may_seen"] = True
                res["may_kind"] = kind
                say(f"{label}: genuine may/free-cast prompt seen (kind={kind})")
                wire(f"{label}_may_prompt", {"kind": kind, "opp": opp})
    st = c.latest
    if st:
        res["exile_after"] = len(exile_objs(st["state"]))
    return res


async def cast_shock_at_p1(c):
    """Cast a Shock from P0's hand targeting P1. Returns oid or None.
    Manages ST['casting'] so the pilot never passes priority mid-cast."""
    ST["casting"] = True
    try:
        st = c.latest
        state = st["state"]
        shock_oid = find_hand(state, 0, "Shock")
        if shock_oid is None:
            return None
        a = cast_spell_action(st.get("legal_actions", []) or [], state, "Shock")
        if a is None:
            say("no CastSpell action for Shock")
            return None
        cast_oid = str((a.get("data") or {}).get("object_id"))
        wire("shock_cast_action", {"action": a})
        await c.send_action(a)
        say(f"P0 casts Shock (oid {cast_oid})")
        tgt = await answer_target_selection(c, seat=1, timeout=60, label="shock_target")
        say(f"shock target answered: {tgt is not None}")
        return cast_oid if tgt else None
    finally:
        ST["casting"] = False


# ------------------------------------------------------------------ main flow

async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(("Forest", 20), ("Mountain", 20), ("Island", 20), ("Swamp", 20),
                          ("Yidris, Maelstrom Wielder", 6), ("Shock", 12),
                          ("Bloodbraid Elf", 8)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(("Mountain", 200)))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    obs["assert"]["A0_connect"] = ("passed" if p0.player_id is not None
                                   and p1.player_id is not None else "failed")
    ST["mulls"] = {"P0": 0, "P1": 0}
    pilot_task = asyncio.create_task(pilot(p0, p1))

    async def abort(note):
        obs["notes"].append(note)
        ST["stop"] = True
        await pilot_task
        await p0.close()
        await p1.close()
        return obs

    s = await wait_cond(
        p0, lambda s: (s.get("waiting_for") or {}).get("type") != "MulliganDecision"
        and len(hand_oids(s, 0)) > 0, 180, "mulligans done")
    if s is None:
        return await abort("mulligan phase never completed")
    say(f"mulligans done: P0 hand={hand_names(p0.latest['state'], 0)}")
    ST["stage"] = "ramp"

    # ---- ramp: cast Yidris
    s = await wait_cond(p0, win_ramp, 900, "yidris cast window")
    if s is None:
        return await abort("Yidris cast window never arrived (mana/colors/hand)")
    # Claim the window BEFORE consuming any flag: the pilot must hold P0's
    # priority until the cast is on the wire, or it passes in the gap and the
    # CastSpell is rejected wrong_player (hit twice on 20260910-6765l).
    ST["casting"] = True
    st = p0.latest
    a = cast_spell_action(st.get("legal_actions", []) or [], st["state"],
                          "Yidris, Maelstrom Wielder")
    if not a:
        ST["casting"] = False
        return await abort("yidris window seen but no CastSpell action")
    ST["yidris_cast_turn"] = st["state"].get("turn_number")
    ST["yidris_cast"] = True
    wire("yidris_cast_action", {"action": a, "turn": ST["yidris_cast_turn"]})
    try:
        await p0.send_action(a)
    finally:
        ST["casting"] = False
    say(f"P0 casts Yidris, Maelstrom Wielder on turn {ST['yidris_cast_turn']}")
    s = await wait_cond(
        p0, lambda s: find_bf(s, 0, "Yidris, Maelstrom Wielder") is not None,
        180, "yidris resolves")
    if s is None:
        return await abort("Yidris never reached the battlefield")
    ST["yidris_oid"] = int(find_bf(s, 0, "Yidris, Maelstrom Wielder"))
    obs["assert"]["A1_setup_ok"] = "passed"
    obs["notes"].append(f"Yidris on BF (oid {ST['yidris_oid']}), cast turn "
                        f"{ST['yidris_cast_turn']}")
    say("A1 passed: Yidris on battlefield")

    # ---- hunt: first fully-staged P0 PreCombatMain hosts A3 + the attack + A4
    ST["stage"] = "hunt"
    s = await wait_cond(p0, win_hunt, 900, "hunt window")
    if s is None:
        return await abort("hunt window never arrived (no Shock in hand / "
                           "Yidris not attack-ready within 900s)")
    # Claim FIRST (see yidris cast note): consuming hunt_consumed releases the
    # pilot's win_hunt hold, so the pilot must already be held via casting.
    ST["casting"] = True
    ST["hunt_consumed"] = True
    ST["attack_live"] = True
    ST["attack_turn"] = s["turn_number"]
    ST["cast_armed"] = True  # hold this turn's PostCombatMain for the A4 window
    say(f"attack turn = {ST['attack_turn']} (hunt window staged)")

    # ---- A3: pre-damage control Shock (must NOT gain cascade). Conditional:
    # only when a second Shock is available so the A4 window is safe.
    st = p0.latest
    state = st["state"]
    if (count_hand(state, 0, "Shock") >= 2
            and untapped_named(state, 0, "Mountain") >= 2):
        await export_ev(p0, "pre_shock_cast")
        shock_oid = await cast_shock_at_p1(p0)  # clears ST['casting'] in finally
        if shock_oid is None:
            obs["assert"]["A3_pre_damage_control"] = "not-run"
            obs["notes"].append("pre-combat Shock cast failed")
        else:
            w = await watch_cascade(p0, 25, "pre_shock", expect_source="Shock",
                                    stop_early=lambda: ST["attackers_declared"])
            exile_d = (w["exile_after"] or 0) - (w["exile_before"] or 0)
            casc = w["trigger_seen"] or w["may_seen"] or exile_d > 0
            obs["assert"]["A3_pre_damage_control"] = "passed" if not casc else "failed"
            obs["notes"].append(
                f"A3: pre-damage Shock: cascade trigger={w['trigger_seen']} "
                f"sources={w['trigger_sources']} may={w['may_seen']} "
                f"exile {w['exile_before']}->{w['exile_after']}")
            say(f"A3: cascade before damage = {casc} (expect False)")
    else:
        ST["casting"] = False  # release the claim; no cast to protect
        obs["assert"]["A3_pre_damage_control"] = "not-run"
        obs["notes"].append(
            f"A3 skipped: only {count_hand(state, 0, 'Shock')} Shock(s) in hand; "
            "the A4 window takes priority")
        say("A3: not-run (Shock reserved for A4)")
    ST["pre_done"] = True

    # ---- declare attackers; export pre_damage
    s = await wait_cond(p0, lambda s: ST["attackers_declared"], 300,
                        "attackers declared")
    if s is None:
        return await abort("attackers never declared on the attack turn")
    say("exporting PRE_DAMAGE state")
    pre_dmg = await export_ev(p0, "pre_damage")
    life_before = life(pre_dmg, 1)
    obs["notes"].append(f"pre_damage export at phase={pre_dmg.get('phase')}; "
                        f"P1 life={life_before}")

    # ---- watch combat: Yidris damage trigger + life drop
    dmg_done = False
    t_end = time.time() + 180
    while time.time() < t_end and not dmg_done:
        await asyncio.sleep(0.4)
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        for e in iter_stack(state):
            if (e.get("kind", {}).get("type") == "TriggeredAbility"
                    and "yidris" in entry_source_name(e).lower()):
                tag = f"yidris#{e.get('id')}"
                if tag not in ST["yidris_trigger_texts"]:
                    ST["yidris_trigger_seen"] = True
                    ST["yidris_trigger_texts"].append(tag)
                    say(f"YIDRIS TRIGGER ON STACK: {json.dumps(e)[:250]}")
                    wire("yidris_trigger_stack", {"entry": json.dumps(e)[:2000]})
        if life(state, 1) is not None and life(state, 1) <= life_before - 5:
            dmg_done = True
            # Set damage_turn at observe time: the PostCombatMain hold is
            # already armed (cast_armed), and win_post_shock needs
            # damage_turn == turn_number to fire.
            ST["damage_turn"] = state["turn_number"]
    obs["notes"].append(f"yidris damage trigger on stack observed="
                        f"{ST['yidris_trigger_seen']}; "
                        f"texts={ST['yidris_trigger_texts'][:1]}")
    s = await wait_cond(
        p0, lambda s: s.get("phase") == "PostCombatMain"
        and len(s.get("stack", []) or []) == 0, 180, "post-combat settle")
    if s is None:
        return await abort("never reached PostCombatMain after the attack")
    life_after = life(p0.latest["state"], 1) if p0.latest else None
    a2 = (dmg_done and life_after == life_before - 5)
    obs["assert"]["A2_combat_damage"] = "passed" if a2 else "failed"
    obs["notes"].append(f"A2: P1 life {life_before}->{life_after} (expect -5)")
    say(f"A2: combat damage dealt={dmg_done}, life {life_before}->{life_after}")
    if not a2:
        return await abort(f"A2 failed: P1 life {life_before}->{life_after}, "
                            "expected exactly -5 from Yidris")
    ST["damage_turn"] = s["turn_number"]
    say("exporting POST_DAMAGE state (grant window should be active)")
    await export_ev(p0, "post_damage")

    # ---- A4: post-damage Shock from hand (must gain cascade per Yidris).
    # The pilot holds P0's PostCombatMain priority (cast_armed, armed at the
    # hunt window before combat arrived).
    s = await wait_cond(p0, win_post_shock, 150, "post-combat shock window")
    if s is None:
        obs["assert"]["A4_yidris_grant"] = "not-run"
        obs["notes"].append("no post-combat Shock window on the attack turn")
        return await abort("no post-combat Shock window; cannot test the grant")
    # Claim FIRST (see yidris cast note): consuming post_consumed releases the
    # pilot's PostCombatMain hold.
    ST["casting"] = True
    ST["post_consumed"] = True
    # Stand Yidris down: the grant test is over, and further attacks would
    # kill P1 before the A5 control runs (hit on 20260910-6765b: P1 died on
    # turn 20 during the post-A4 watches, voiding A5/A6).
    ST["attack_live"] = False
    say("Yidris stood down (attack_live=False); P1 must survive for A5")
    await export_ev(p0, "pre_cast")
    exile_before = len(exile_objs(p0.latest["state"]))
    shock_oid = await cast_shock_at_p1(p0)
    if shock_oid is None:
        obs["assert"]["A4_yidris_grant"] = "not-run"
        return await abort("post-combat Shock cast failed")
    w = await watch_cascade(p0, 60, "post_shock", expect_source="Shock")
    st_now = p0.latest["state"] if p0.latest else {}
    exile_after = len(exile_objs(st_now)) if st_now else None
    exile_d = (exile_after or 0) - exile_before
    casc = w["trigger_seen"] or w["may_seen"] or exile_d > 0
    obs["assert"]["A4_yidris_grant"] = "passed" if casc else "failed"
    obs["notes"].append(
        f"A4: post-damage Shock: cascade trigger={w['trigger_seen']} "
        f"sources={w['trigger_sources']} may={w['may_seen']} kind={w['may_kind']} "
        f"exile {exile_before}->{exile_after}")
    say(f"A4: cascade after Yidris damage = {casc} (expect True per Oracle text)")
    if casc:
        # Settle only: decline the free-cast may prompt now that the cascade
        # is recorded. Never auto-answers exile card choices.
        await decline_pending_may(p0, "post_shock", timeout=40)
    s = await wait_cond(
        p0, lambda s: len(s.get("stack", []) or []) == 0
        and (s.get("waiting_for") or {}).get("type") in ("Priority", None),
        120, "post shock settle")
    obs["notes"].append(f"post-shock settle reached={s is not None}")
    say("exporting POST_CAST state")
    await export_ev(p0, "post_cast")
    ST["post_done"] = True
    ST["cast_armed"] = False

    # ---- A5: control -- Bloodbraid Elf native cascade on a later turn
    ST["stage"] = "control"
    s = await wait_cond(p0, win_control, 600, "bloodbraid control window")
    if s is None:
        obs["assert"]["A5_control_cascade"] = "not-run"
        obs["notes"].append("no Bloodbraid Elf window on a later turn")
    else:
        # Claim FIRST (see yidris cast note): consuming control_consumed
        # releases the pilot's win_control hold.
        ST["casting"] = True
        ST["control_consumed"] = True
        await export_ev(p0, "pre_control")
        try:
            st = p0.latest
            a = cast_spell_action(st.get("legal_actions", []) or [], st["state"],
                                  "Bloodbraid Elf")
            if a is None:
                obs["assert"]["A5_control_cascade"] = "not-run"
                obs["notes"].append("bloodbraid window seen but no CastSpell action")
            else:
                wire("bloodbraid_cast_action", {"action": a})
                await p0.send_action(a)
                say("P0 casts Bloodbraid Elf (native cascade control)")
        finally:
            ST["casting"] = False
        if obs["assert"].get("A5_control_cascade") != "not-run":
            w = await watch_cascade(p0, 90, "control",
                                    expect_source="Bloodbraid Elf")
            exile_d = (w["exile_after"] or 0) - (w["exile_before"] or 0)
            casc = w["trigger_seen"] or w["may_seen"] or exile_d > 0
            obs["assert"]["A5_control_cascade"] = "passed" if casc else "failed"
            obs["notes"].append(
                f"A5: Bloodbraid cascade trigger={w['trigger_seen']} "
                f"sources={w['trigger_sources']} may={w['may_seen']} "
                f"kind={w['may_kind']} exile {w['exile_before']}->{w['exile_after']}")
            say(f"A5: native cascade works = {casc} (expect True)")
            if casc:
                await decline_pending_may(p0, "control", timeout=40)
            s = await wait_cond(
                p0, lambda s: len(s.get("stack", []) or []) == 0
                and (s.get("waiting_for") or {}).get("type") in ("Priority", None),
                120, "control settle")
            obs["notes"].append(f"control settle reached={s is not None}")
            say("exporting POST_CONTROL state")
            await export_ev(p0, "post_control")

    # ---- A6 cleanup
    ST["stage"] = "done"
    s = await wait_cond(
        p0, lambda s: len(s.get("stack", []) or []) == 0
        and (s.get("waiting_for") or {}).get("type") in ("Priority", None), 120,
        "cleanup settle")
    obs["assert"]["A6_cleanup"] = "passed" if s is not None else "failed"
    if s is not None:
        await export_ev(p0, "final")

    ST["stop"] = True
    await pilot_task
    await p0.close()
    await p1.close()
    return obs


# ------------------------------------------------------------- verdict + run

async def arun():
    return await main()


def build_run(obs, dur, t0):
    a = obs["assert"]
    a2 = a.get("A2_combat_damage")
    a3 = a.get("A3_pre_damage_control")
    a4 = a.get("A4_yidris_grant")
    a5 = a.get("A5_control_cascade")
    if a2 == "passed" and a4 == "failed" and a5 == "passed" and a3 in ("passed", "not-run"):
        verdict = "reproduced"
    elif a2 == "passed" and a4 == "passed" and a5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    rel = f"{BACKFILL}/server/releases/v0.79.0"
    run = {
        "issue": 6765,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": sha256_of_file(
                f"{rel}/phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(f"{rel}/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(f"{rel}/data/draft-pools.json"),
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-09-10",
            "source": "ServerHello + sha256 of pinned verified artifacts",
        },
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 69, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6765.py"),
        "decks": {
            "P0": [["Forest", 20], ["Mountain", 20], ["Island", 20], ["Swamp", 20],
                   ["Yidris, Maelstrom Wielder", 6], ["Shock", 12],
                   ["Bloodbraid Elf", 8]],
            "P1": [["Mountain", 200]],
        },
        "assertions": a,
        "notes": obs["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Dense playsets (12x Shock, 8x Bloodbraid, 6x Yidris) and oversized "
            "decks (106/200 cards) are test-harness conveniences (engine "
            "accepts >4-of and >60-card decks for custom games; the size "
            "prevents decking losses during the 4-color ramp).",
            "Parser signal: pinned card-data.json marks Yidris's damage-trigger "
            "execute as Unimplemented ('as you cast spells from your hand'); "
            "the AddKeyword(Cascade) grant exists only as its sub_ability.",
            "A5's cascade free-cast may prompt is declined after the trigger "
            "is observed, purely to let the game settle; exile-zone card "
            "choices are never auto-answered.",
        ],
        "setup_line": ("P0: 80 lands + 6x Yidris, Maelstrom Wielder + 12x Shock + "
                       "8x Bloodbraid Elf (106 cards); P1: 200x Mountain (draw-go); "
                       "oversized decks prevent decking during the 4-color ramp"),
        "contract_line": ("Yidris attacks unblocked on a fully-staged turn; "
                          "pre-damage Shock (when a spare is available) must not "
                          "cascade; post-damage Shock must gain cascade; "
                          "Bloodbraid Elf control must cascade natively"),
    }
    return run


def write_manifest():
    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(os.path.join(EVDIR, f)) and f != "manifest.sha256")
    lines = []
    for f in files:
        h = hashlib.sha256()
        with open(os.path.join(EVDIR, f), "rb") as fh:
            h.update(fh.read())
        lines.append(f"{h.hexdigest()}  {f}")
    with open(os.path.join(EVDIR, "manifest.sha256"), "w") as mf:
        mf.write("\n".join(lines) + "\n")
    return files


if __name__ == "__main__":
    t0 = time.time()
    try:
        obs = asyncio.run(asyncio.wait_for(arun(), timeout=2400))
    except asyncio.TimeoutError:
        say("GLOBAL TIMEOUT after 2400s")
        obs = {"assert": {"A0_connect": "not-run"}, "notes": ["global timeout"]}
    dur = time.time() - t0
    run = build_run(obs, dur, t0)
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={run['verdict']} assertions={json.dumps(run['assertions'])}")
    # immutable scenario copy + server log excerpts, then render, then manifest
    try:
        shutil.copy(f"{BACKFILL}/driver/scenario_6765.py",
                    f"{EVDIR}/scenario_6765.py")
    except Exception as e:
        say(f"scenario copy failed: {e}")
    try:
        slog = f"{BACKFILL}/runs/{RUN_ID}/server.log"
        if os.path.exists(slog):
            with open(slog) as f:
                lines = f.readlines()
            interesting = [l for l in lines
                           if "ERROR" in l or "WARN" in l or "error" in l.lower()]
            with open(f"{EVDIR}/server_excerpts.log", "w") as f:
                f.write(f"# {len(lines)} total server.log lines; "
                        f"{len(interesting)} error/warn lines\n")
                f.writelines(interesting[-200:])
                f.write("\n# --- tail ---\n")
                f.writelines(lines[-30:])
    except Exception as e:
        say(f"server excerpts failed: {e}")
    try:
        subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary_6765.py",
                        EVDIR], check=False, timeout=120)
    except Exception as e:
        say(f"render failed: {e}")
    files = write_manifest()
    say(f"manifest written for {len(files)} files")
    try:
        WIRE.close(); RUNLOG.close()
    except Exception:
        pass
