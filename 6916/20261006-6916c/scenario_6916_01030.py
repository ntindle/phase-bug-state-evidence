#!/usr/bin/env python3
"""Issue #6916: Xantcha, Sleeper Agent — cannot choose which opponent gets
Xantcha; any player may activate its {3} ability.

Re-validation on v0.103.0 / protocol 106 (2026-10-06). Prior runs:
  - 20260913-6916  (v0.81.3, protocol 70): reproduced. Claim (a) choice did
    NOT reproduce (EntryControllerChoice offered, worked). Claim (b)
    reproduced STRONGER than reported: the {3} ability was never offered to
    ANY seat, not merely restricted to the controller.
  - 20260914-6916  (v0.82.0, protocol 70): re-validated, verdict unchanged.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text:
  Xantcha, Sleeper Agent ({1}{B}{R}, 5/5 Legendary Creature - Phyrexian Minion):
    "Xantcha enters under the control of an opponent of your choice.
     Xantcha attacks each combat if able and can't attack its owner or
     planeswalkers its owner controls.
     {3}: Xantcha's controller loses 2 life and you draw a card.
     Any player may activate this ability."

Reported symptoms:
  (a) the owner is NOT offered the opponent choice as Xantcha enters;
  (b) the {3} ability is available only to Xantcha's controller, not to
      every player.

Setup (native engine, three human-client seats, CommanderDraft, 3 players;
three seats so the owner has two legal opponents to choose between):
  P0: commander=[Xantcha, Sleeper Agent], main = 30x Swamp + 30x Mountain.
      Casts the commander; owns it for the whole run.
  P1: commander=[Ayula, Queen Among Bears] (never cast; inert), 60x Forest.
      Provides the any-player activation test: P1 is neither owner nor
      controller of Xantcha.
  P2: 60x Forest (no commander), inert.

Plan:
  1. P0 casts the Xantcha commander ({1}{B}{R}).
  2. While Xantcha is entering/resolving, record whether the engine offers
     P0 a choice among opponents (player candidates) and which choice is
     answered (P2 is the intended pick). If no choice is offered, that is
     recorded as-is.
  3. After Xantcha is on the battlefield, on P1's main phase P1 activates
     Xantcha's {3} ability (P1 is not the controller). Assert the
     controller (whoever controls Xantcha) loses 2 life and P1 draws a card.

Assertions:
  A1_setup_ok        Xantcha on the battlefield, owned by P0 (owner==0).
  A2_choice_offered  the engine offered P0 a choice among legal opponents
                     as Xantcha entered (>=2 player candidates recorded).
                     FAILED = the reported choice bug.
  A3_controlled_by_chosen
                     Xantcha's controller is P2 (the intended pick), i.e.
                     the choice (or default) put it under P2's control.
  A4_any_player_activate
                     P1 (non-controller, non-owner) was offered and
                     submitted Xantcha's {3} activation. FAILED = the
                     reported any-player-permission bug.
  A5_activation_effect
                     Xantcha's controller lost exactly 2 life (40->38) and
                     the activating player P1 drew exactly one card
                     (hand +1), with no other life changes.
  A6_cleanup         post.json: stack empty, game proceeds (no stall).

Verdict rule: reproduced iff A1 passes and at least one of
A2/A3/A4/A5 fails. not-reproduced iff A1-A6 all pass. blocked iff A1 fails.

Evidence: evidence/6916/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6916_01030.py, wire_log.jsonl,
scenario_run.log, server.log

Protocol-106 notes:
  - CastSpell via advertised actions as-is; mana is engine auto-tapped.
  - DeclareAttackers/DeclareBlockers prefer the relations-schema vi
    opportunity; empty declares only ever answer relations-schema
    opportunities (never select/sequence — those are decisions).
  - Re-tick backstop covers pending vi decisions, not just priority.
  - playLand is a non-decision menu code; explicit land-play runs first.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20261006-6916")
EVDIR = f"{BACKFILL}/evidence/6916/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

XANTCHA = "xantcha, sleeper agent"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
AYULA = "ayula, queen among bears"

P0_COMMANDER = [XANTCHA]
P0_MAIN = [(SWAMP, 30), (MOUNTAIN, 30)]
P1_COMMANDER = [AYULA]  # never cast; inert opponent
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

COMMANDER_FORMAT = {
    "format": "CommanderDraft",
    "starting_life": 40,
    "min_players": 3,
    "max_players": 8,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": True,
    "commander_damage_threshold": 21,
    "range_of_influence": None,
    "team_based": False,
    "sideboard_policy": {"type": "Forbidden"},
    "uses_commander": True,
    "supplies_fixed_deck": False,
    "default_deck_copy_limit": {"type": "Unlimited"},
    "allow_debug_actions": False,
}

STARTING_LIFE = 40
INTENDED_CONTROLLER = 2  # we choose P2 when a choice is offered

SERVER_IDENTITY = {
    "server_version": "0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "mode": "Full",
    "binary_sha256": "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc",
    "card_data_sha256": "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe",
    "draft_pools_sha256": "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd",
    "signature_verified": True,
    "observed_at": "2026-10-06",
    "source": "ledger server pin (minisign-verified 2026-10-06) + running "
              "isolated v0.103.0 server on 127.0.0.1:9374",
}


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


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects", {}) or {}).get(str(oid), {}) or {}


def obj_lname(state, oid):
    if oid is None:
        return "?"
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def is_land(o):
    # protocol 106 object shape: card_types = {"core_types": [...], ...}
    core = ((o.get("card_types") or {}).get("core_types") or [])
    if any(str(t).lower() == "land" for t in core):
        return True
    base = ((o.get("base_card_types") or {}).get("core_types") or [])
    if any(str(t).lower() == "land" for t in base):
        return True
    # fallback for older shapes
    t = str(o.get("type_line") or o.get("type") or "").lower()
    return "land" in t


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and is_land(o)):
            out.append(int(oid))
    return out


def find_bf_oid(state, pid, lname):
    for oid, o in (state.get("objects", {}) or {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and obj_lname(state, oid) == lname):
            return int(oid)
    return None


def xantcha_oid_any(state):
    for oid, o in (state.get("objects", {}) or {}).items():
        if o.get("zone") == "Battlefield" and obj_lname(state, oid) == XANTCHA:
            return int(oid)
    return None


def xantcha_on_stack(state):
    for e in state.get("stack", []) or []:
        if "xantcha" in json.dumps(e, default=str).lower():
            return True
    return False


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
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def candidate_seat(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def find_cast_action(acts, state, lname, oid=None):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if oid is not None and str(iv) != str(oid):
                continue
            if obj_lname(state, iv) == lname:
                return a
    return None


def st_of(c):
    return c.latest or {}


# ------------------------------------------------------------- interaction primitives
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


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def pass_priority(c, st, acts, tag):
    for a in acts:
        if a.get("type") == "PassPriority":
            key = (tag, "pass", str(c.revision))
            if key in PASSED_REV:
                return True
            PASSED_REV[key] = True
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
                iid = opp.get("interactionId") or opp.get("id")
                key = (tag, "pass", str(iid))
                if key in PASSED_REV:
                    return True
                PASSED_REV[key] = True
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  tag)
                return True
    return False


# ------------------------------------------------------------- shared driver state
ST = {}
SUBMITTED_OPPS = set()
PASSED_REV = {}
LAND_PLAYED_TURN = {}


def init_st():
    ST.update({
        "xantcha_cast": False, "xantcha_cast_turn": None,
        "xantcha_oid": None, "entered": False, "entered_at": None,
        "enter_controller": None, "enter_owner": None,
        "enter_choice_offered": False, "enter_choice_iid": None,
        "enter_choice_candidates": [], "enter_choice_pick": None,
        "pre_exported": False, "mid_exported": False,
        "post_exported": False, "post_at": None,
        "p1_activated": False, "p1_activation_turn": None,
        "pre_act": None,
        "p1_sample_turns": set(), "p1_noact_logged": set(),
        "p2_ctrl_sample": None, "p0_owner_sample": None,
        "mulls": {}, "opp_shapes_logged": set(),
    })


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = hand_lnames(st["state"], pid)
    nlands = sum(1 for n in hand if n in (SWAMP, MOUNTAIN, FOREST))
    n = ST["mulls"].get(tag, 0)
    if tag == "P0":
        choice = "Keep" if (nlands >= 3 or n >= 2) else "Mulligan"
    else:
        choice = "Keep" if (nlands >= 2 or n >= 2) else "Mulligan"
    if choice == "Mulligan":
        ST["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (lands={nlands} hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan is a vi schema/select opportunity."""
    state = st["state"]
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec", {}) or {}
        if spec.get("type") != "select":
            continue
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        # bottom prompts name cards; only treat as bottom if it looks like one
        texts = " ".join(choice_text(ch) for ch in cands).lower()
        if "bottom" not in texts and "mulligan" not in texts:
            # schema/select without bottom context: not ours
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else "?"
            if nm in (XANTCHA, AYULA):
                return (2, str(ref))
            if ref is not None and is_land(get_obj(state, ref)):
                return (1, str(ref))
            return (0, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


async def do_discard(c, acts, st, pid, tag):
    """Discard to hand size at cleanup: schema/select opportunity whose
    candidates reference cards in hand. Gated on hand > 7 so it never eats
    the entry-choice or other schema/select decisions."""
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]
        # only discard cards actually in our hand (never touch other prompts)
        picks = [o for o in hand if str(o) in ref_of][:n]
        choice_ids = [ref_of[str(o)] for o in picks]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in picks]}")
        wire("discard", {"who": tag, "oids": picks})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    # 106 may also advertise land plays as a vi playLand opportunity
    st = st_of(c)
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            if ref is not None and str(ref) in [str(x) for x in lands]:
                iid = opp.get("interactionId")
                key = (tag, "playland", str(iid), str(ref))
                if key in SUBMITTED_OPPS:
                    continue
                SUBMITTED_OPPS.add(key)
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                wire("play_land_vi", {"who": tag, "oid": str(ref)})
                await answer_vi(c, opp, ch, tag)
                return True
    return False


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


async def do_declare_empty(c, acts, st, pid, tag, phase_kind):
    """Empty attackers/blockers. vi fallback answers ONLY relations-schema
    opportunities (select/sequence schemas are decisions, never declares)."""
    state = st["state"]
    if phase_kind == "attackers":
        for a in acts:
            if a.get("type") == "DeclareAttackers":
                d = copy.deepcopy(a)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(c, d)
                say(f"[{tag}] declare no attackers")
                return True
    else:
        for a in acts:
            if a.get("type") == "DeclareBlockers":
                d = copy.deepcopy(a)
                d.setdefault("data", {})["assignments"] = []
                await submit_as_is(c, d)
                say(f"[{tag}] declare no blockers")
                return True
    # 106 fallback: relations-schema vi opportunity only
    phase = str(state.get("phase") or "")
    want = "declareattack" if phase_kind == "attackers" else "declareblock"
    if state.get("active_player") == pid and want in phase.lower():
        opp = find_relations_op(st)
        if opp is not None:
            iid = opp.get("interactionId")
            key = (tag, "declare", phase_kind, str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare empty {phase_kind} via vi relations")
            wire("declare_empty_vi", {"who": tag, "kind": phase_kind})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "relations",
                                               "data": {"relations": []}}},
                              tag)
            return True
    return False


async def do_pay_mana(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


# ------------------------------------------------------------- Xantcha-specific
async def enter_choice_tick(c, pid, tag, st, state):
    """Catch the opponent-choice prompt as Xantcha enters.

    The owner (P0) should be offered a choice among legal opponents.
    A schema opportunity with player candidates seen while Xantcha is on
    the stack (or just before it is observed on the battlefield) is the
    reported choice: pick P2, record candidates, mark done.
    """
    if not ST["xantcha_cast"] or ST["entered"]:
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if (tag, "enter-choice", str(iid)) in SUBMITTED_OPPS:
            return True
        resp = opp.get("response", {}) or {}
        # 106: the entry-controller choice is rtype "exactChoices" (not
        # "schema"); candidates carry a 'chooseEntryController' action code
        # and a player surface with the seat.
        if resp.get("type") not in ("schema", "exactChoices"):
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        seats = [candidate_seat(ch) for ch in chs]
        # 106 signal: 'chooseEntryController' surface code; fallback to the
        # player-seat heuristic from protocol 70.
        is_entry_choice = ("chooseEntryController" in codes
                           or any(s is not None for s in seats))
        if not is_entry_choice:
            continue
        # player-candidate schema opportunity while Xantcha is entering:
        # this is the reported EntryControllerChoice.
        ST["enter_choice_offered"] = True
        ST["enter_choice_iid"] = str(iid)[:16]
        ST["enter_choice_candidates"] = seats
        say(f"[{tag}] ENTER-CHOICE prompt: seats={seats} codes={sorted(codes)}")
        wire("enter_choice_prompt",
             {"who": tag, "iid": str(iid)[:16], "seats": seats,
              "codes": sorted(codes),
              "opportunity": json.loads(json.dumps(opp, default=str))})
        SUBMITTED_OPPS.add((tag, "enter-choice", str(iid)))
        pick = None
        for ch in chs:
            if candidate_seat(ch) == INTENDED_CONTROLLER:
                pick = ch
                break
        if pick is None:
            # fallback: match P2 by player-name text or candidate order
            pick = pick_entry_controller_fallback(chs, state)
        if pick is None:
            pick = chs[0]
        ST["enter_choice_pick"] = candidate_seat(pick)
        say(f"[{tag}] ENTER-CHOICE pick: seat={ST['enter_choice_pick']} "
            f"text={choice_text(pick)[:60]!r}")
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def pick_entry_controller_fallback(chs, state):
    """Pick the INTENDED_CONTROLLER candidate without candidate_seat.

    Tries (in order): a surface reference naming player 2, candidate text
    naming the seat-2 display name, else None (caller falls back to chs[0]).
    """
    for ch in chs:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            for k in ("seat", "player", "index", "player_id", "controller"):
                try:
                    if d.get(k) is not None and int(d[k]) == INTENDED_CONTROLLER:
                        return ch
                except (TypeError, ValueError):
                    pass
        t = choice_text(ch)
        if str(INTENDED_CONTROLLER) in t:
            return ch
    return None


def xantcha_ability_offered(st, acts, xoid):
    """Return ('action', action) or ('vi', opp, choice) if Xantcha's
    {3} activation is advertised to this seat, else None."""
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = d.get("source_id") or d.get("object_id")
        if a.get("_src_oid") is not None:
            try:
                src = int(a["_src_oid"])
            except (TypeError, ValueError):
                pass
        try:
            if src is not None and int(src) == int(xoid):
                return ("action", a)
        except (TypeError, ValueError):
            pass
    # 106: abilities may surface as vi opportunities with activateAbility code
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        for ch in chs:
            if "activateAbility" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            try:
                if ref is not None and int(ref) == int(xoid):
                    return ("vi", opp, ch)
            except (TypeError, ValueError):
                pass
    return None


async def activation_tick(c, pid, tag, st, acts, state):
    """Any-player activation test. P1 (non-owner, non-controller) tries to
    activate Xantcha's {3} ability on its own main phase. P0/P2 are sampled
    once each for the 'stronger than reported' observation."""
    xoid = ST["xantcha_oid"] or xantcha_oid_any(state)
    if not ST["entered"] or xoid is None:
        return False
    if not my_main(state, pid):
        return False
    if len(untapped_lands(state, pid)) < 3:
        return False
    offered = xantcha_ability_offered(st, acts, xoid)
    if tag == "P1" and not ST["p1_activated"]:
        if offered:
            pre_life = {i: life_of(state, i) for i in (0, 1, 2)}
            pre_hand = {i: len(hand_ids(state, i)) for i in (0, 1, 2)}
            ST["pre_act"] = {"life": pre_life, "hand": pre_hand,
                             "controller": get_obj(state, xoid).get("controller")}
            say(f"[P1] activating Xantcha's ability pre_life={pre_life} "
                f"pre_hand={pre_hand}")
            wire("p1_activate", {"pre_life": pre_life, "pre_hand": pre_hand,
                                 "via": offered[0]})
            if offered[0] == "action":
                await submit_as_is(c, offered[1])
            else:
                _k, opp, ch = offered
                await answer_vi(c, opp, ch, tag)
            ST["p1_activated"] = True
            ST["p1_activation_turn"] = state.get("turn_number")
            return True
        turn = state.get("turn_number")
        ST["p1_sample_turns"].add(turn)
        if turn not in ST["p1_noact_logged"]:
            ST["p1_noact_logged"].add(turn)
            # log the full advertised surface so a "no offer" claim can be
            # audited post-hoc (avoid driver-blind-spot false reproductions)
            vi_shapes = []
            for opp in vi_ops(st):
                rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
                chs = rdata.get("choices") or rdata.get("candidates") or []
                all_codes = set()
                for ch in chs:
                    all_codes.update(x for x in surf_codes(ch) if x)
                vi_shapes.append({"rtype": (opp.get("response", {}) or {}).get("type"),
                                  "codes": sorted(all_codes), "n": len(chs)})
            say(f"[P1] Xantcha on BF but NO activation advertised to P1 "
                f"(turn {turn})")
            wire("p1_no_activation_offered",
                 {"turn": turn,
                  "act_types": sorted(set(a["type"] for a in acts)),
                  "vi_shapes": vi_shapes})
        return False
    # control samples: owner (P0) and controller (P2), once each
    if tag == "P0" and ST["p0_owner_sample"] is None:
        ST["p0_owner_sample"] = {"offered": offered is not None,
                                 "turn": state.get("turn_number")}
        say(f"[P0-owner] Xantcha activation offered: {offered is not None}")
        wire("p0_owner_sample", ST["p0_owner_sample"])
    if tag == "P2" and ST["p2_ctrl_sample"] is None:
        ST["p2_ctrl_sample"] = {"offered": offered is not None,
                                "turn": state.get("turn_number")}
        say(f"[P2-controller] Xantcha activation offered: {offered is not None}")
        wire("p2_ctrl_sample", ST["p2_ctrl_sample"])
    return False


async def p2_attack_tick(c, acts, st, state, tag):
    """P2 controls Xantcha: declare it attacking P1 via the relations-schema
    opportunity (106). Falls back to an empty declare if it cannot attack."""
    if state.get("active_player") != 2:
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    xoid = ST["xantcha_oid"] or xantcha_oid_any(state)
    opp = find_relations_op(st)
    if opp is None:
        return await do_declare_empty(c, acts, st, 2, tag, "attackers")
    iid = opp.get("interactionId") or opp.get("id")
    key = (tag, "declare-atk", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}
    rels = []
    if xoid is not None:
        for e in edges:
            src = e.get("sourceId")
            cand = cands.get(src, {})
            ref = _cand_reference(cand)
            if ref is None or str(ref) != str(xoid):
                continue
            want = None
            for tid in e.get("targetIds") or []:
                if candidate_seat(cands.get(tid, {})) == 1:
                    want = tid
                    break
            if want is None and (e.get("targetIds") or []):
                want = (e.get("targetIds") or [])[0]
            if want:
                rels.append({"sourceId": src, "targetId": want,
                             "group": None})
    say(f"[P2] declaring attackers via relations: {len(rels)} rels")
    wire("p2_attack", {"n_rels": len(rels)})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "relations",
                                       "data": {"relations": rels}}},
                      tag)
    return True


async def unexpected_prompt_tick(c, pid, tag, st, state):
    """Log unexpected vi decisions; answer only if stalled >60s and the
    opportunity is not the entry-choice shape (never mask the choice)."""
    now = time.time()
    acted = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        rec = UNEXPECTED.setdefault(str(iid),
                                    {"t0": now, "done": False, "who": tag})
        if rec["done"]:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        codes = set()
        for ch in chs:
            codes.update(c for c in surf_codes(ch) if c)
        # never auto-answer the entry-choice shape or declare relations
        if "chooseEntryController" in codes:
            continue
        if resp.get("type") == "schema":
            spec = (data.get("spec") or {}).get("type")
            if spec == "relations":
                continue
            if any(candidate_seat(ch) is not None for ch in chs):
                continue
        if now - rec["t0"] < 60:
            if not rec.get("logged"):
                rec["logged"] = True
                say(f"[{tag}] UNEXPECTED PROMPT iid={str(iid)[:8]} "
                    f"codes={sorted(codes)} n={len(chs)}")
                wire("unexpected_prompt",
                     {"who": tag, "iid": str(iid)[:8],
                      "codes": sorted(codes), "n_choices": len(chs)})
            continue
        if not chs:
            continue
        pick = chs[0]
        say(f"[{tag}] auto-answering stalled prompt: "
            f"{choice_text(pick)[:60]}")
        wire("auto_answer", {"who": tag, "iid": str(iid)[:8],
                             "choice": choice_text(pick)[:80]})
        await answer_vi(c, opp, pick, tag)
        rec["done"] = True
        acted = True
    return acted


UNEXPECTED = {}


async def seat_tick(c, pid, tag, st, acts, state):
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard(c, acts, st, pid, tag):
        return
    if await do_pay_mana(c, acts, tag):
        return
    # combat declares
    wphase = str(state.get("phase") or "").lower()
    if "declareattack" in wphase and state.get("active_player") == pid:
        if tag == "P2":
            if await p2_attack_tick(c, acts, st, state, tag):
                return
        else:
            if await do_declare_empty(c, acts, st, pid, tag, "attackers"):
                return
    if "declareblock" in wphase:
        if await do_declare_empty(c, acts, st, pid, tag, "blockers"):
            return
    # the reported entry choice (owner)
    if await enter_choice_tick(c, pid, tag, st, state):
        return
    # Xantcha entered bookkeeping + mid export
    xoid = xantcha_oid_any(state)
    if xoid is not None and not ST["entered"]:
        o = get_obj(state, xoid)
        ST["entered"] = True
        ST["entered_at"] = time.time()
        ST["xantcha_oid"] = int(xoid)
        ST["enter_controller"] = o.get("controller")
        ST["enter_owner"] = o.get("owner")
        say(f"Xantcha entered: oid={xoid} owner={o.get('owner')} "
            f"controller={o.get('controller')} "
            f"choice_offered={ST['enter_choice_offered']}")
        wire("xantcha_entered",
             {"oid": int(xoid), "owner": o.get("owner"),
              "controller": o.get("controller"),
              "choice_offered": ST["enter_choice_offered"],
              "choice_pick": ST["enter_choice_pick"]})
    if (ST["entered"] and not ST["mid_exported"]
            and (ST["enter_choice_offered"]
                 or time.time() - (ST["entered_at"] or 0) > 10)):
        try:
            mid = await c.export_state()
            with open(f"{EVDIR}/mid.json", "w") as f:
                f.write(mid)
            ST["mid_exported"] = True
            say("exported MID (Xantcha entered)")
        except Exception as e:
            say(f"mid export failed: {e}")
    # pre export fallback: Xantcha on stack
    if (ST["xantcha_cast"] and not ST["pre_exported"]
            and xantcha_on_stack(state) and not ST["entered"]):
        try:
            pre = await c.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre)
            ST["pre_exported"] = True
            say("exported PRE (Xantcha on stack, fallback)")
        except Exception as e:
            say(f"pre export failed: {e}")
    if not my_priority(acts):
        # cleanup discard (and any other non-priority schema/select) runs
        # before the unexpected-prompt fallback so nothing stalls 60s.
        if await do_discard(c, acts, st, pid, tag):
            return
        await unexpected_prompt_tick(c, pid, tag, st, state)
        return
    # ---- priority ----
    if await activation_tick(c, pid, tag, st, acts, state):
        return
    # P0 casts the Xantcha commander
    if (tag == "P0" and not ST["xantcha_cast"]
            and xantcha_oid_any(state) is None
            and my_main(state, 0)
            and len(untapped_lands(state, 0)) >= 3):
        a = find_cast_action(acts, state, XANTCHA)
        if a is not None:
            say("[P0] casting Xantcha, Sleeper Agent (commander)")
            wire("cast", {"who": "P0", "name": XANTCHA})
            await submit_as_is(c, a)
            ST["xantcha_cast"] = True
            ST["xantcha_cast_turn"] = state.get("turn_number")
            await asyncio.sleep(1.0)
            try:
                cur = (c.latest or {}).get("state", {})
                if xantcha_on_stack(cur) and not ST["pre_exported"]:
                    pre = await c.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    ST["pre_exported"] = True
                    say("exported PRE (Xantcha on stack, post-cast)")
            except Exception as e:
                say(f"post-cast pre export failed: {e}")
            return
    if await play_a_land(c, state, pid, acts, tag):
        return
    await pass_priority(c, st, acts, tag)


# ------------------------------------------------------------- finish & evidence
async def finish(clients):
    p0 = clients[0]
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_choice_offered", "A3_controlled_by_chosen",
            "A4_any_player_activate", "A5_activation_effect", "A6_cleanup")}
    try:
        if not ST["post_exported"]:
            post = await p0.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
    except Exception as e:
        notes.append(f"post export failed: {e}")

    def load_state(fn):
        # export_state() returns the envelope JSON string:
        # {"state": {<game state>}, "precast_shortcut_runtime": {...}}
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                raw = open(p).read()
                env = json.loads(raw)
                if isinstance(env, str):
                    env = json.loads(env)
                st = env.get("state", env) if isinstance(env, dict) else None
                say(f"loaded {fn}.json")
                return st
        except Exception as e:
            notes.append(f"state reload failed for {fn}.json: {e}")
        return None

    pre_st = load_state("pre")
    mid_st = load_state("mid")
    post_st = load_state("post")

    if post_st is not None:
        xoid = ST["xantcha_oid"] or xantcha_oid_any(post_st)
        o = get_obj(post_st, xoid) if xoid else {}
        ok = (xoid is not None and o.get("zone") == "Battlefield"
              and o.get("owner") == 0)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A1: xantcha_oid={xoid} zone={o.get('zone')} "
                     f"owner={o.get('owner')} controller={o.get('controller')}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 failed: post.json missing")

    if ST["enter_choice_offered"]:
        ass["A2_choice_offered"] = "passed"
        notes.append(f"A2 passed: opponent-choice prompt offered "
                     f"(iid={ST['enter_choice_iid']}, candidates="
                     f"{ST['enter_choice_candidates']})")
    elif ST["entered"]:
        ass["A2_choice_offered"] = "failed"
        notes.append("A2 FAILED: Xantcha entered with NO opponent-choice "
                     "prompt offered to the owner. BUG REPRODUCED (a).")
    else:
        ass["A2_choice_offered"] = "failed"
        notes.append("A2 failed: Xantcha never entered")

    if post_st is not None and ST["entered"]:
        xoid = ST["xantcha_oid"] or xantcha_oid_any(post_st)
        ctrl = get_obj(post_st, xoid).get("controller")
        if ctrl == INTENDED_CONTROLLER:
            ass["A3_controlled_by_chosen"] = "passed"
            notes.append(f"A3 passed: Xantcha controlled by P{ctrl} "
                         f"(intended pick)")
        else:
            ass["A3_controlled_by_chosen"] = "failed"
            notes.append(f"A3 FAILED: Xantcha controlled by P{ctrl}, "
                         f"not the intended P{INTENDED_CONTROLLER}")
    else:
        ass["A3_controlled_by_chosen"] = "failed"
        notes.append("A3 failed: no Xantcha on BF")

    if ST["p1_activated"]:
        ass["A4_any_player_activate"] = "passed"
        notes.append("A4 passed: P1 (non-controller) activated Xantcha's "
                     "{3} ability")
    elif ST["entered"]:
        ass["A4_any_player_activate"] = "failed"
        notes.append("A4 FAILED: P1 (non-controller, non-owner) was never "
                     "offered Xantcha's {3} activation. BUG REPRODUCED (b).")
    else:
        ass["A4_any_player_activate"] = "failed"
        notes.append("A4 failed: Xantcha never entered")

    if post_st is not None and ST["p1_activated"] and ST["pre_act"]:
        xoid = ST["xantcha_oid"] or xantcha_oid_any(post_st)
        ctrl = get_obj(post_st, xoid).get("controller")
        life_now = {i: life_of(post_st, i) for i in (0, 1, 2)}
        hand_now = {i: len(hand_ids(post_st, i)) for i in (0, 1, 2)}
        pre = ST["pre_act"]
        life_ok = (life_now[ctrl] == pre["life"][ctrl] - 2)
        others_ok = all(life_now[i] == pre["life"][i]
                        for i in (0, 1, 2) if i != ctrl)
        draw_ok = (hand_now[1] == pre["hand"][1] + 1)
        notes.append(f"A5: controller=P{ctrl} life {pre['life']} -> "
                     f"{life_now}; hands {pre['hand']} -> {hand_now}")
        if life_ok and others_ok and draw_ok:
            ass["A5_activation_effect"] = "passed"
            notes.append("A5 passed: controller -2 life, P1 drew 1")
        else:
            ass["A5_activation_effect"] = "failed"
            notes.append("A5 FAILED: activation effect wrong "
                         f"(life_ok={life_ok} others_ok={others_ok} "
                         f"draw_ok={draw_ok})")
    elif not ST["p1_activated"]:
        ass["A5_activation_effect"] = "not-run"
        notes.append("A5 not-run: no activation happened")
    else:
        ass["A5_activation_effect"] = "failed"
        notes.append("A5 failed: post.json missing")

    if post_st is not None:
        stack_empty = not (post_st.get("stack") or [])
        ass["A6_cleanup"] = "passed" if stack_empty else "failed"
        notes.append(f"A6: stack_empty={stack_empty}")
    else:
        ass["A6_cleanup"] = "failed"
        notes.append("A6 failed: post.json missing")

    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif any(ass[k] == "failed"
             for k in ("A2_choice_offered", "A3_controlled_by_chosen",
                       "A4_any_player_activate", "A5_activation_effect")):
        verdict = "reproduced"
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    notes.append(f"verdict={verdict}")

    run = {
        "run_id": RUN_ID, "issue": 6916,
        "verdict": verdict, "validated_at": "2026-10-06",
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py",
                   "scenario": "driver/scenario_6916_01030.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6916_01030.py",
                 "rb").read()).hexdigest(),
        "format_config": "CommanderDraft (3 seats; P0/P1 commanders, P2 none)",
        "decks": {"P0": {"main": [[SWAMP, 30], [MOUNTAIN, 30]],
                         "commander": P0_COMMANDER},
                  "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                  "P2": {"main": P2_DECK, "commander": []}},
        "assertions": ass,
        "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                         for k, v in ST.items()},
        "notes": notes,
        "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                           "manifest.sha256", "summary.png",
                           "scenario_6916_01030.py", "wire_log.jsonl",
                           "scenario_run.log", "server.log"],
        "limitations": [
            "Browser UI not exercised; native engine via three human-client seats.",
            "CommanderDraft format is a test-harness fixture for 3-player "
            "games (P0 casts Xantcha from the command zone).",
            "The reported 'attacks each combat if able' clause was not "
            "asserted; only the opponent-choice entry and the any-player "
            "activation permission were tested.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_6916_01030.py",
                f"{EVDIR}/scenario_6916_01030.py")
    # server.log from the isolated run, if present
    for cand in (f"{BACKFILL}/runs/{RUN_ID}/server.log",
                 f"{BACKFILL}/runs/RUN_ID=run-20261006-211803/server.log"):
        if os.path.exists(cand):
            shutil.copy(cand, f"{EVDIR}/server.log")
            say(f"copied server.log from {cand}")
            break
    else:
        open(f"{EVDIR}/server.log", "w").write(
            "server.log unavailable for this run (shared isolated server)\n")
    say("copied scenario_6916_01030.py and server.log into EVDIR")
    render_summary(run, pre_st, mid_st, post_st)
    write_manifest()
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return run


def render_summary(run, pre_st, mid_st, post_st):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 820
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6916 - Xantcha, Sleeper Agent",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-06",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else (120, 220, 120))
    y += 34
    d.text((24, y), "Assertions (from saved states):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "Xantcha on BF, owned by P0",
        "A2_choice_offered": "opponent choice offered as Xantcha entered",
        "A3_controlled_by_chosen": "Xantcha controlled by P2 (intended)",
        "A4_any_player_activate": "P1 (non-controller) activated {3}",
        "A5_activation_effect": "controller -2 life, P1 drew 1",
        "A6_cleanup": "stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12

    def zone_of(st, label, yy):
        d.text((24, yy), f"{label}:", fill=(200, 210, 225))
        yy += 22
        oid = ST.get("xantcha_oid")
        if st is not None and oid is not None:
            o = get_obj(st, oid)
            d.text((36, yy),
                   f"xantcha oid={oid} zone={o.get('zone')} "
                   f"owner={o.get('owner')} ctrl={o.get('controller')} "
                   f"tapped={bool(o.get('tapped'))}",
                   fill=(170, 180, 195))
        else:
            d.text((36, yy), "(no state / xantcha oid unknown)",
                   fill=(120, 130, 145))
        return yy + 30

    y = zone_of(pre_st, "pre.json  (Xantcha on stack)", y)
    y = zone_of(mid_st, "mid.json  (Xantcha entered)", y)
    y = zone_of(post_st, "post.json (after P1 activation attempt)", y)
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:11]:
        d.text((36, y), n[:118], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    files = ["pre.json", "mid.json", "post.json", "run.json",
             "scenario_6916_01030.py", "wire_log.jsonl", "scenario_run.log",
             "server.log", "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


# ------------------------------------------------------------- main loop
async def main():
    init_st()
    t_start = time.time()
    p0 = PhaseClient("P06916")
    await p0.connect()
    await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                    format_config=COMMANDER_FORMAT)
    p1 = PhaseClient("P16916")
    await p1.connect()
    await p1.join(p0.game_code, cdeck(P1_COMMANDER, *P1_DECK))
    p2 = PhaseClient("P26916")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": p0.game_code,
                        "seats": {"P0": p0.player_id, "P1": p1.player_id,
                                  "P2": p2.player_id}})
    clients = [p0, p1, p2]
    tags = ["P0", "P1", "P2"]
    pids = [0, 1, 2]
    last = {}
    last_tick_at = {}
    last_diag = 0.0

    async def post_check():
        # post export: P1's activation resolved (stack empty), or Xantcha
        # entered and P1 has had >=2 distinct main-phase samples with 3+
        # untapped lands and still no activation offered (the missing offer
        # is itself the signal for claim b).
        s = (p0.latest or {}).get("state", {})
        stack_empty = not (s.get("stack") or [])
        act_done = ST["p1_activated"] and stack_empty
        no_offer_done = (ST["entered"] and not ST["p1_activated"]
                         and len(ST["p1_sample_turns"]) >= 2 and stack_empty)
        if ST["entered"] and not ST["post_exported"] and (act_done or no_offer_done):
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say(f"exported POST (act_done={act_done} "
                    f"no_offer_done={no_offer_done})")
            except Exception as e:
                say(f"post export failed: {e}")

    while time.time() - t_start < 1500:
        await asyncio.sleep(0.15)
        for c, pid, tag in zip(clients, pids, tags):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(tag))
            stale = time.time() - last_tick_at.get(tag, 0) > 5
            # 106 re-tick backstop: also re-tick on pending vi decisions
            pending_vi = bool(vi_ops(st))
            if same_rev and not stale and not pending_vi:
                continue
            last[tag] = rev
            last_tick_at[tag] = time.time()
            try:
                await seat_tick(c, pid, tag, st, merged_actions(st),
                                st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        await post_check()
        if (ST["entered"] and not ST["post_exported"]
                and time.time() - t_start > 1200):
            say("WATCHDOG: 1200s elapsed without post; finishing")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
            except Exception as e:
                say(f"watchdog post export failed: {e}")
            await finish(clients)
            return
        if ST["post_exported"] and time.time() - (ST["post_at"] or 0) > 5:
            say("post exported; finishing")
            await finish(clients)
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            acts = merged_actions(p0.latest)
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} pp={s.get('priority_player')} "
                f"P0lands={len(untapped_lands(s, 0))} "
                f"P1lands={len(untapped_lands(s, 1))} "
                f"xcast={ST['xantcha_cast']} entered={ST['entered']} "
                f"ctrl={ST['enter_controller']} choice={ST['enter_choice_offered']} "
                f"p1act={ST['p1_activated']} "
                f"pre={ST['pre_exported']} mid={ST['mid_exported']} "
                f"post={ST['post_exported']} stack={len(s.get('stack') or [])} "
                f"vi={len(vi_ops(p0.latest))}")
    say("global timeout (1500s) hit before assertions resolved")
    await finish(clients)


asyncio.run(main())
