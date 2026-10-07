#!/usr/bin/env python3
"""Issue #6912: Chaos Warp may have a glitch when targeting commanders.

Re-validation on pinned v0.103.0 (protocol 106) of run 20260912-6912a
(protocol 70, verdict not-reproduced).

Report (discord 2026-08-02): "Chaos Warp may have a glitch when targeting
commanders?" No concrete symptom was described (classifier: cannot_determine).

Oracle text (pinned v0.103.0 card-data.json, 'chaos warp'):
  "The owner of target permanent shuffles it into their library, then
   reveals the top card of their library. If it's a permanent card, they
   put it onto the battlefield."

Rules expectation (CR 903.9 / 903.9a): if a commander would be put into its
owner's library from anywhere, its owner may put it into the command zone
instead. The replacement is offered to the OWNER of the commander. Chaos
Warp's later instructions ("then" shuffle/reveal/put) still use the target
OWNER's library and must resolve even when the commander moves to the
command zone.

Setup (native engine, three human-client seats, CommanderDraft):
  P0: commander=[zurgo bellstriker] (never cast; sits in CZ), main = 12x
      Chaos Warp + 48x Mountain. P0 casts Chaos Warp targeting P1's commander.
  P1: commander=[ayula, queen among bears] ({1}{G} 2/2, inert here), main =
      60x Forest. P1 casts Ayula, then answers the commander-replacement
      choice as the commander OWNER.
  P2: dummy, 60x Forest, no commander.

CZ_CHOICE=zone (this run): P1 moves Ayula to the command zone.

Expected (per card text + CR 903.9a):
  E1: Ayula on P1's battlefield with is_commander=true; Chaos Warp cast by
      P0 with Ayula as the chosen target.
  E2: a commander-replacement choice is offered to P1 (the owner), not P0.
  E3: Ayula ends in the CommandZone (zone branch).
  E4: P1's library is shuffled and its top card revealed; the revealed
      permanent (a Forest) is put onto the battlefield under P1's control.
      P0's library is untouched.

Assertions:
  A1_setup_ok       pre.json at P0 PreCombatMain: Ayula on P1 BF with
                    is_commander=true, Chaos Warp in P0 hand, 3+ untapped
                    Mountains, both players at 40 life
  A2_cast_targeted  Chaos Warp was cast and the submitted target candidate
                    resolved to Ayula's battlefield oid
  A3_replacement_offered  a commander replacement choice was offered to
                    P1 (the commander owner); offered to P0 counts as FAILED
  A4_zone_correct   zone branch: Ayula zone == CommandZone/Command
  A5_owner_routing  the shuffle/reveal/put used P1's (owner's) library: a
                    revealed Forest ended on P1's BF; P0's battlefield
                    untouched
  A6_cleanup        post.json: stack empty, game proceeding (no stall)

Verdict rule: reproduced iff A1 passed and (A3 failed or A4 failed or A5
failed) - the reported commander-targeting glitch. not-reproduced iff
A1..A6 pass. blocked iff A1/A2 fail.

Evidence: evidence/6912/<run-id>/pre.json, mid.json (right after the
replacement answer), post.json, run.json, manifest.sha256, summary.png,
scenario_6912_01030.py, wire_log.jsonl, scenario_run.log

Protocol-106 port of driver/scenario_6912.py (verified 2026-09-12 on
protocol 70, run 20260912-6912a, verdict not-reproduced). Conventions taken
from the proven scenario_6910_01030.py template:
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - merged_actions/vi, PASSED_REV gating, Select-model discards
    (schema+select required), schema-select BottomCards
  - stack-watch fall-through: never hold priority while waiting on a
    resolution; always fall through to the pass gate
  - host-only exports (P0C)
  - 3 seats via CommanderDraft (proven in scenario_6916_01030.py)
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6912
RUN_ID = "20261007-6912"
CZ_CHOICE = os.environ.get("CZ_CHOICE", "zone")  # "zone" | "library"
assert CZ_CHOICE in ("zone", "library"), f"bad CZ_CHOICE={CZ_CHOICE}"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

WARP = "chaos warp"
MOUNTAIN = "mountain"
FOREST = "forest"
AYULA = "ayula, queen among bears"
ZURGO = "zurgo bellstriker"

P0_COMMANDER = [ZURGO]
P0_MAIN = [(WARP, 12), (MOUNTAIN, 48)]
P1_COMMANDER = [AYULA]
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
TIMEOUT = 1500
PROGRESS_WATCHDOG_S = 180
REPL_WATCHDOG_S = 240
POST_DELAY_S = 12

ST = {}
OBS = {}
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
        "mulligans": 0,
        "mulligans_p1": 0,
        "mulligans_p2": 0,
        "warp_cast": False,
        "warp_cast_at": None,
        "warp_target_oid": None,
        "target_answered": False,
        "repl_answered": False,
        "repl_answered_at": None,
        "frozen": False,
        "stop": False,
        "states_seen": 0,
        "mana_needs": {},
        "pre_ayula_oid": None,
        "game_code": None,
    })
    OBS.clear()
    OBS.update({        "repl_seen": None,
        "repl_chooser": None,
        "repl_choices": [],
        "repl_chosen": None,
        "repl_guess": None,
        "repl_chooser_wrong": False,
        "cast_turn": None,
        "wf_types_resolution": [],
        "reveal_prompts": [],
        "auto_answered": [],
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


def lib_count(state, pid):
    return len(player_of(state, pid).get("library", []) or [])


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or obj_lname(state, oid) == key)]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def untapped_lands(state, pid, key=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in (FOREST, MOUNTAIN)):
            if key is None or nm == key:
                out.append(int(oid))
    return out


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


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


def choice_id_of(ch):
    return ch.get("id") or ch.get("choiceId") or ch.get("choice_id")


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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


def stack_has_warp(state):
    for e in state.get("stack", []) or []:
        if "chaos warp" in json.dumps(e, default=str).lower():
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


async def answer_vi(c, opp, choice, tag):
    """Submit a vi choice with the response shape the opportunity asks for."""
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice_id_of(choice)
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={str(iid)[:8]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


async def verify_server_hello():
    import websockets
    ws = await websockets.connect("ws://127.0.0.1:9374/ws",
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
    """Document the data-level premise: Chaos Warp oracle text + parse."""
    w = CARD_DATA.get(WARP, {})
    notes = []
    notes.append(f"chaos warp oracle: {(w.get('oracle_text') or w.get('text') or '')[:120]}")
    notes.append(f"chaos warp cost: {w.get('mana_cost')}")
    a = CARD_DATA.get(AYULA, {})
    notes.append(f"ayula cost={a.get('mana_cost')} power={a.get('power')}")
    payload = {"chaos_warp": {k: w.get(k) for k in
                              ("oracle_text", "text", "mana_cost",
                               "type_line", "effects", "static_abilities",
                               "triggered_abilities", "activated_abilities")},
               "ayula": {"mana_cost": a.get("mana_cost"),
                         "power": a.get("power")},
               "notes": notes}
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(payload, f, indent=1, default=str)
    say("data-level check: " + " | ".join(notes))
    return True


# ------------------------------------------------- mulligan / bottom / discard

def mulligan_pending_for(state, pid):
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "Declare":
            return True
    return False


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
    lands = sum(1 for o in hand_ids(state, pid)
                if is_land(get_obj(state, o)))
    if pid == 0:
        keep = (WARP in hn and lands >= 3)
        mkey = "mulligans"
    elif pid == 1:
        keep = lands >= 2
        mkey = "mulligans_p1"
    else:
        keep = lands >= 2
        mkey = "mulligans_p2"
    if not keep and ST[mkey] < 3:
        ST[mkey] += 1
        say(f"[{tag}] mulligans ({ST[mkey]}) (hand={hn[:6]})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    else:
        say(f"[{tag}] keeps (hand={len(hn)}: {hn[:6]})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as SelectCards legal
    actions plus a vi schema/select opportunity."""
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
    # P0 keeps one warp; everyone bottoms lands first
    hand = hand_ids(state, pid)
    warp_oids = [o for o in hand if obj_lname(state, o) == WARP]
    keep_warp = set(warp_oids[:1])

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if oid in keep_warp:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {n} cards")
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


def discard_rank(pid):
    def rank(state):
        def _rank(oid):
            nm = obj_lname(state, oid)
            if nm == WARP:
                return (1, nm)
            return (0, nm)  # lands first
        return _rank
    return rank


async def do_discard(c, st, tag, pid):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    select_opps = [o for o in vi_ops(st) if is_select_schema_opp(o)]
    if not select_opps:
        return False
    rank = discard_rank(pid)
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
        ranked = sorted(cands, key=lambda ch: rank(state)(_cand_reference(ch)))
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

async def pay_tick(c, acts, tag=None):
    # Only pay via legacy actions when this seat has outstanding mana
    # needs (setup casts). Test casts rely on the engine's Auto payment;
    # answering here would double-pay.
    if tag is not None and sum(ST["mana_needs"].get(tag, {}).values()) <= 0:
        return False
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


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    cands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
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


# ------------------------------------------------------------------ exports

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

# ------------------------------------------------- 6912-specific decisions

def target_opp_for(ops, state, ayula_oid):
    """Find the warp target-selection opportunity and the Ayula candidate.

    Proven 106 shape (scenario_301_01030.py): schema select/sequence with
    candidates, or exactChoices whose choices carry candidate/target codes
    with passPriority NOT among the codes (excludes the ordinary priority
    menu, whose castSpell choices also carry object references).
    """
    for opp in ops:
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        if rtype not in ("exactChoices", "schema"):
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        if rtype == "schema":
            spec = (data.get("spec", {}) or {}).get("type")
            if spec not in ("select", "sequence"):
                continue
        else:
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if "passPriority" in codes:
                continue
            if not any(c in codes for c in ("candidate", "target")):
                continue
        # must look like a target pick: candidates carry references
        refs = [_cand_reference(ch) for ch in chs]
        if not any(r is not None for r in refs):
            continue
        pick = None
        for ch in chs:
            if _cand_reference(ch) == ayula_oid:
                pick = ch
                break
        if pick is None:
            for ch in chs:
                if AYULA in choice_text(ch).lower():
                    pick = ch
                    break
        if pick is not None:
            return opp, pick
    return None, None


def replacement_opp_for(ops):
    """Find the commander-replacement opportunity (protocol 106).

    Shape (verified IDENTICAL on protocol 70 and 106): exactChoices with
    exactly 2 choices, each carrying a `chooseReplacement` action code and
    an `optionIndex` value surface ("0"/"1"). The choices carry NO
    descriptive text at all -- there is nothing to text-match, so do NOT
    require any text (a "command" text filter misses it entirely).
    """
    for opp in ops:
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        if rtype not in ("exactChoices", "schema"):
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if len(chs) != 2:
            continue
        if all("chooseReplacement" in surf_codes(ch) for ch in chs):
            return opp
    return None


def option_index_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "optionIndex":
            return d.get("value")
    return None


async def answer_replacement(c, pid, tag, st, state):
    """Answer the commander replacement choice for player pid (the seat
    that can submit it). Records chooser + full opportunity for evidence."""
    wf = state.get("waiting_for") or {}
    chooser = wf.get("player", (wf.get("data") or {}).get("player"))
    if chooser is None:
        chooser = pid
    obs_time = {"turn": state.get("turn_number"), "revision": c.revision,
                "chooser": chooser}
    OBS["repl_seen"] = obs_time
    OBS["repl_chooser"] = chooser
    opp = replacement_opp_for(vi_ops(st))
    if opp is None:
        say(f"[{tag}] replacement prompt but no vi opportunity matched")
        wire("repl_no_vi", {"chooser": chooser})
        return True
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    wire("replacement_opportunity",
         {"chooser": chooser, "seat": tag,
          "opportunity": json.loads(json.dumps(opp, default=str))})
    descs = []
    for i, ch in enumerate(chs):
        blob = json.dumps(ch, default=str).lower()
        txt = choice_text(ch)
        descs.append({"i": i, "text": txt[:120],
                      "optionIndex": option_index_of(ch),
                      "mentions_command_zone": "command zone" in blob})
    OBS["repl_choices"] = descs
    say(f"[{tag}] REPLACEMENT CHOICE chooser={chooser} n={len(chs)} "
        f"descs={json.dumps(descs)}")
    if chooser == 0:
        OBS["repl_chooser_wrong"] = True
        say(f"[{tag}] replacement offered to P0 (WRONG PLAYER - owner is P1)")
    # Option mapping: evidence-backed, not a guess. Two historical runs on
    # protocol 70 with the IDENTICAL opportunity shape (exactChoices, 2x
    # chooseReplacement, optionIndex "0"/"1", no text):
    #   20260912-6912a (zone):    picked idx 0 -> Ayula ended in CommandZone
    #   20260912-6912b (library): picked idx 1 -> Ayula shuffled into library
    # Sanity-check: if any choice text mentions "command zone", it must
    # agree with the positional mapping; a disagreement is recorded.
    pick = 0 if CZ_CHOICE == "zone" else 1
    how = (f"optionIndex mapping from runs 20260912-6912a/b: idx {pick} == "
           f"{'command zone' if CZ_CHOICE == 'zone' else 'library'}")
    for i, ch in enumerate(chs):
        blob = json.dumps(ch, default=str).lower()
        if "command zone" in blob:
            say(f"[{tag}] text mentions 'command zone' on idx {i}; "
                f"positional mapping says zone==idx 0")
            wire("repl_text_sanity",
                 {"text_idx": i, "positional_zone_idx": 0,
                  "agree": i == 0})
    say(f"[{tag}] " + how)
    choice = chs[pick]
    OBS["repl_chosen"] = {"i": pick, "text": choice_text(choice)[:120],
                          "how": how}
    await answer_vi(c, opp, choice, tag)
    ST["repl_answered"] = True
    ST["repl_answered_at"] = time.time()
    ST["frozen"] = True  # freeze board development: no more land drops /
    # casts; priority passes only, so post.json is a clean
    # post-resolution snapshot
    return True


PROMPT_SEEN = {}


async def generic_prompt(c, pid, tag, st, state):
    """Log + (after a grace period) affirmatively answer unexpected
    resolution prompts for pid. Returns True if it acted."""
    acted = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in PROMPT_SEEN and PROMPT_SEEN[iid].get("done"):
            continue
        blob = json.dumps(opp, default=str)
        low = blob.lower()
        # never touch the replacement or target opportunities here
        if replacement_opp_for([opp]) is not None:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        # never treat an ordinary priority menu as a prompt: every choice's
        # action codes are routine (passPriority/castSpell/...). Auto-
        # answering these as "prompts" breaks the game flow (they are
        # handled by the pass gate instead).
        menu_codes = set()
        for ch in data.get("choices") or data.get("candidates") or []:
            menu_codes.update(x for x in surf_codes(ch) if x)
        if menu_codes and menu_codes <= NON_DECISION_CODES:
            continue
        chs = data.get("choices") or data.get("candidates") or []
        entry = PROMPT_SEEN.setdefault(
            iid, {"t0": time.time(), "done": False, "blob": blob[:800]})
        OBS["reveal_prompts"].append(
            {"who": tag, "iid": str(iid)[:8],
             "n_choices": len(chs),
             "texts": [choice_text(ch)[:60] for ch in chs][:6],
             "mentions_reveal": "reveal" in low})
        say(f"[{tag}] UNEXPECTED PROMPT iid={str(iid)[:8]} n={len(chs)} "
            f"texts={[choice_text(ch)[:40] for ch in chs][:4]}")
        wire("unexpected_prompt",
             {"who": tag,
              "opportunity": json.loads(json.dumps(opp, default=str))})
        if time.time() - entry["t0"] < 15:
            continue  # grace period: keep observing
        pick = None
        for ch in chs:
            t = choice_text(ch).lower()
            b = json.dumps(ch, default=str).lower()
            if ("reveal" in low and ("yes" in t or '"true"' in b
                                    or "reveal" in t)):
                pick = ch
                break
        if pick is None:
            for ch in chs:
                t = choice_text(ch).lower()
                if "yes" in t or "true" in t:
                    pick = ch
                    break
        if pick is None and chs:
            pick = chs[0]
        if pick is not None:
            say(f"[{tag}] auto-answering prompt after 15s stall: "
                f"{choice_text(pick)[:60]}")
            wire("auto_answer", {"who": tag, "iid": str(iid)[:8],
                                 "choice": choice_text(pick)[:80]})
            OBS["auto_answered"].append(
                {"who": tag, "choice": choice_text(pick)[:80]})
            await answer_vi(c, opp, pick, tag)
            entry["done"] = True
            acted = True
    return acted


# ------------------------------------------------------------------ ticks

async def p0_tick(c, st, tag, pid):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard(c, st, tag, pid):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            import copy
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts, tag):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    if ST["warp_cast"]:
        OBS["wf_types_resolution"].append(
            (state.get("waiting_for") or {}).get("type"))

    # target selection for the warp cast
    if ST["warp_cast"] and not ST["target_answered"]:
        ay = bf_id(state, 1, AYULA)
        if ay is not None:
            opp, pick = target_opp_for(vi_ops(st), state, ay)
            if opp is not None:
                ST["warp_target_oid"] = ay
                ST["target_answered"] = True
                say(f"[{tag}] targets Ayula (oid {ay}) with Chaos Warp")
                wire("warp_target",
                     {"target_oid": ay,
                      "candidate": {k: pick.get(k) for k in
                                    ("id", "text", "label", "name")}})
                await answer_vi(c, opp, pick, tag)
                return
            say(f"[{tag}] warp target selection pending (ayula_bf={ay})")
        # fall through: keep passing while the target prompt is up

    # replacement choice wrongly offered to P0: record + answer to keep
    # liveness (the wrong-player offer itself is the A3 failure)
    if ST["warp_cast"] and not ST["repl_answered"]:
        if replacement_opp_for(vi_ops(st)) is not None:
            wf = state.get("waiting_for") or {}
            chooser = wf.get("player", (wf.get("data") or {}).get("player"))
            if chooser in (0, None):
                say(f"[{tag}] offered the commander replacement choice "
                    f"(chooser={chooser}; owner is P1)")
                await answer_replacement(c, pid, tag, st, state)
                return

    if ST["warp_cast"] and not ST["repl_answered"]:
        if await generic_prompt(c, pid, tag, st, state):
            return

    if real_decision_pending(st):
        return

    # ---- P0 priority ----
    if not my_priority(acts):
        return
    # hold: warp in flight -> only pass (never land-drop or cast)
    if ST["warp_cast"] and not ST["repl_answered"]:
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return
    # pre-export + cast warp
    ay = bf_id(state, 1, AYULA)
    if (not ST["warp_cast"] and not ST["frozen"]
            and my_main(state, pid)
            and ay is not None
            and WARP in hand_lnames(state, pid)
            and len(untapped_lands(state, pid, MOUNTAIN)) >= 3):
        if not env_state("pre.json"):
            say("PRE: exporting (Ayula on P1 BF, warp in hand, 3+ mountains)")
            ST["pre_ayula_oid"] = ay
            await do_export(P0C, "pre.json")
        a, woid = cast_action_for(acts, state, WARP)
        if a is not None:
            ST["warp_cast"] = True
            ST["warp_cast_at"] = time.time()
            OBS["cast_turn"] = state.get("turn_number")
            say(f"[{tag}] casts Chaos Warp (oid {woid})")
            wire("cast_warp", {"oid": woid, "action": a["type"]})
            # engine auto-pays CastSpell (payment_mode Auto); set needs as a
            # backstop in case a vi mana opportunity is offered instead.
            ST["mana_needs"][tag] = {"R": 1, "generic": 2}
            await submit_as_is(c, a)
            return
    # land drop
    if not ST["frozen"] and my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, acts)
        PASSED_REV[c.name] = c.revision


async def p1_tick(c, st, tag, pid):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard(c, st, tag, pid):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            import copy
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts, tag):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # the commander replacement choice belongs to P1 (the owner)
    if ST["warp_cast"] and not ST["repl_answered"]:
        if replacement_opp_for(vi_ops(st)) is not None:
            say(f"[{tag}] answering the commander replacement choice")
            await answer_replacement(c, pid, tag, st, state)
            return

    if ST["warp_cast"] and not ST["repl_answered"]:
        if await generic_prompt(c, pid, tag, st, state):
            return

    if real_decision_pending(st):
        return

    if not my_priority(acts):
        return
    # cast Ayula from the command zone when affordable (only before the
    # warp test; never recast after the warp was cast)
    if (not ST["warp_cast"] and not ST["frozen"]
            and bf_id(state, pid, AYULA) is None
            and my_main(state, pid)
            and len(untapped_lands(state, pid, FOREST)) >= 2):
        a, aoid = cast_action_for(acts, state, AYULA)
        if a is not None:
            say(f"[{tag}] casts Ayula (commander, oid {aoid}) via {a['type']}")
            wire("cast_ayula", {"oid": aoid, "action": a["type"]})
            ST["mana_needs"][tag] = {"G": 1, "generic": 1}
            await submit_as_is(c, a)
            return
    if not ST["frozen"] and my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, acts)
        PASSED_REV[c.name] = c.revision


async def p2_tick(c, st, tag, pid):
    """Passive seat: mulligan, lands, passes. Keeps the 3-player game alive."""
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard(c, st, tag, pid):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            import copy
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if real_decision_pending(st):
        return
    if not my_priority(acts):
        return
    if not ST["frozen"] and my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, acts)
        PASSED_REV[c.name] = c.revision

# ------------------------------------------------------------- evaluation

def evaluate(notes):
    A, D = {}, {}
    pre_st = env_state("pre.json")
    mid_st = env_state("mid.json")
    post_st = env_state("post.json")

    # ---- A1: setup ----
    if pre_st is not None:
        ay = bf_id(pre_st, 1, AYULA)
        ok = (ay is not None
              and bool(get_obj(pre_st, ay).get("is_commander"))
              and WARP in hand_lnames(pre_st, 0)
              and len(untapped_lands(pre_st, 0, MOUNTAIN)) >= 3
              and life_of(pre_st, 0) == STARTING_LIFE
              and life_of(pre_st, 1) == STARTING_LIFE)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (
            f"ayula_bf={ay} is_commander="
            f"{bool(ay is not None and get_obj(pre_st, ay).get('is_commander'))} "
            f"warp_in_hand={WARP in hand_lnames(pre_st, 0)} "
            f"untapped_mtn={len(untapped_lands(pre_st, 0, MOUNTAIN))} "
            f"life={[life_of(pre_st, 0), life_of(pre_st, 1)]}")
        ST["pre_ayula_oid"] = ay
    else:
        A["A1_setup_ok"] = "failed"
        D["A1_setup_ok"] = "pre.json missing"

    # ---- A2: warp cast with Ayula as target ----
    if ST["warp_target_oid"] is not None and ST["pre_ayula_oid"] is not None:
        if ST["warp_target_oid"] == ST["pre_ayula_oid"]:
            A["A2_cast_targeted"] = "passed"
            D["A2_cast_targeted"] = (
                f"submitted target oid {ST['warp_target_oid']} == "
                f"pre-cast Ayula oid")
        else:
            A["A2_cast_targeted"] = "failed"
            D["A2_cast_targeted"] = (
                f"target oid {ST['warp_target_oid']} != Ayula oid "
                f"{ST['pre_ayula_oid']}")
    else:
        A["A2_cast_targeted"] = "failed"
        D["A2_cast_targeted"] = (
            f"warp_target_oid={ST['warp_target_oid']} "
            f"pre_ayula_oid={ST['pre_ayula_oid']}")

    # ---- A3: replacement offered to the owner (P1) ----
    if OBS["repl_seen"] is not None:
        if OBS["repl_chooser"] == 1:
            A["A3_replacement_offered"] = "passed"
            D["A3_replacement_offered"] = (
                f"ReplacementChoice offered to P1 (owner), "
                f"choices={json.dumps(OBS['repl_choices'])}, "
                f"chosen={json.dumps(OBS['repl_chosen'])}"
                + (f" [{OBS['repl_guess']}]" if OBS["repl_guess"] else ""))
        else:
            A["A3_replacement_offered"] = "failed"
            D["A3_replacement_offered"] = (
                f"replacement choice offered to player {OBS['repl_chooser']} "
                f"(expected owner P1)")
    else:
        A["A3_replacement_offered"] = "failed"
        D["A3_replacement_offered"] = (
            "no commander replacement choice was observed during Chaos Warp "
            "resolution")

    # ---- A4/A5/A6 from post ----
    if post_st is not None and pre_st is not None:
        ay = ST["pre_ayula_oid"]
        post_zone = zone_of(post_st, ay) if ay is not None else None
        p1_lib_pre = lib_count(pre_st, 1)
        p1_lib_post = lib_count(post_st, 1)
        p0_lib_pre = lib_count(pre_st, 0)
        p0_lib_post = lib_count(post_st, 0)
        p1_bf_pre = set(bf_ids(pre_st, 1))
        p1_bf_post = set(bf_ids(post_st, 1))
        p0_bf_pre = set(bf_ids(pre_st, 0))
        p0_bf_post = set(bf_ids(post_st, 0))
        new_on_p1_bf = [o for o in (p1_bf_post - p1_bf_pre)]
        new_names = sorted(obj_lname(post_st, o) for o in new_on_p1_bf)
        new_on_p0_bf = [o for o in (p0_bf_post - p0_bf_pre)]
        new_p0_names = sorted(obj_lname(post_st, o) for o in new_on_p0_bf)
        new_forests_p1 = [o for o in new_on_p1_bf
                          if obj_lname(post_st, o) == FOREST]
        notes.append(
            f"post: ayula_zone={post_zone} p1_lib {p1_lib_pre}->{p1_lib_post} "
            f"p0_lib {p0_lib_pre}->{p0_lib_post} new_on_p1_bf={new_names} "
            f"new_on_p0_bf={new_p0_names}")
        p1_hand_pre = len(hand_ids(pre_st, 1))
        p1_hand_post = len(hand_ids(post_st, 1))
        p1_gy_pre = len(player_of(pre_st, 1).get("graveyard", []) or [])
        p1_gy_post = len(player_of(post_st, 1).get("graveyard", []) or [])
        p1_draws = p1_hand_post - p1_hand_pre
        draws_sane = (p1_gy_post == p1_gy_pre and p1_draws >= 0)
        # A4 (zone branch this run)
        if post_zone in ("CommandZone", "Command"):
            A["A4_zone_correct"] = "passed"
            D["A4_zone_correct"] = ("Ayula in the command zone after choosing "
                                    "the command-zone replacement")
        else:
            A["A4_zone_correct"] = "failed"
            D["A4_zone_correct"] = (f"expected command zone, got {post_zone}")
        # A5: owner routing. Board frozen from the replacement answer on, so
        # P0's battlefield must be identical pre->post and P1's library must
        # account for exactly the warp's shuffle+reveal. Between pre and post
        # P1 may draw (draw step) and discard to hand size; both are visible
        # in the hand/graveyard deltas (frozen board: no casts, no combat
        # damage, no other graveyard interaction), so:
        #   draws = (hand_post - hand_pre) + (gy_post - gy_pre)
        # (P1's library is all Forests [+ Ayula in the library branch], so
        # the revealed card is always a permanent and must be put on BF.)
        notes.append(f"A5 accounting: p1 hand {p1_hand_pre}->{p1_hand_post} "
                     f"gy {p1_gy_pre}->{p1_gy_post}")
        p1_draws = (p1_hand_post - p1_hand_pre) + (p1_gy_post - p1_gy_pre)
        draws_sane = p1_draws >= 0 and p1_gy_post >= p1_gy_pre
        notes.append(f"A5 draws={p1_draws} draws_sane={draws_sane}")
        exp_lib = p1_lib_pre - p1_draws - 1  # revealed Forest -> BF
        p0_lib_untouched = (p0_lib_post == p0_lib_pre)
        if (draws_sane and len(new_on_p0_bf) == 0
                and len(new_forests_p1) == 1
                and len(new_on_p1_bf) == 1
                and p1_lib_post == exp_lib
                and p0_lib_untouched):
            A["A5_owner_routing"] = "passed"
            D["A5_owner_routing"] = (
                f"revealed Forest put onto P1's BF (P1 library "
                f"{p1_lib_pre}->{p1_lib_post} = -{p1_draws} draw(s) -1 "
                f"revealed); P0's battlefield untouched")
        else:
            A["A5_owner_routing"] = "failed"
            D["A5_owner_routing"] = (
                f"unexpected routing: new_on_p1_bf={new_names} "
                f"new_on_p0_bf={new_p0_names} p1_lib {p1_lib_pre}->"
                f"{p1_lib_post} (expected {exp_lib}), draws_sane={draws_sane}")
        # A6
        stack_empty = not (post_st.get("stack") or [])
        wf = (post_st.get("waiting_for") or {}).get("type")
        if stack_empty and wf in ("Priority", None):
            A["A6_cleanup"] = "passed"
            D["A6_cleanup"] = (f"stack empty, waiting_for={wf}, game "
                               f"proceeding")
        else:
            A["A6_cleanup"] = "failed"
            D["A6_cleanup"] = (
                f"stack={[str(e)[:60] for e in (post_st.get('stack') or [])][:3]} "
                f"waiting_for={wf}")
    else:
        for k in ("A4_zone_correct", "A5_owner_routing", "A6_cleanup"):
            A[k] = "failed"
            D[k] = "pre.json or post.json missing"

    if A.get("A1_setup_ok") == "passed" and any(
            A.get(k) == "failed" for k in ("A3_replacement_offered",
                                           "A4_zone_correct",
                                           "A5_owner_routing")):
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_cast_targeted", "A3_replacement_offered",
              "A4_zone_correct", "A5_owner_routing", "A6_cleanup")):
        verdict = "not-reproduced"
    elif A.get("A1_setup_ok") != "passed" or A.get("A2_cast_targeted") != "passed":
        verdict = "blocked"
    else:
        verdict = "reproduced"
    notes.append(f"verdict={verdict} cz_choice={CZ_CHOICE}")
    return A, D, verdict


# ------------------------------------------------------------------- main

async def main():
    reset_state()
    t0 = time.time()
    notes = []
    await verify_server_hello()
    data_ok = check_data_level()

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game (CommanderDraft, 3 seats)...")
    try:
        await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                        format_config=COMMANDER_FORMAT)
    except Exception as e:
        say(f"game creation failed: {e}")
        notes.append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions":
                       {k: "not-run" for k in
                        ("A1_setup_ok", "A2_cast_targeted",
                         "A3_replacement_offered", "A4_zone_correct",
                         "A5_owner_routing", "A6_cleanup")},
                       "notes": notes}, f, indent=2)
        await p0.close()
        return
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, cdeck(P1_COMMANDER, *P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    say("P2 joining...")
    await p2.join(p0.game_code, deck(*P2_DECK))
    ST["game_code"] = p0.game_code
    global P0C
    P0C = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"P2 seat={p2.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1.player_id, "p2": p2.player_id})

    clients = [(p0, p0_tick, "P0", 0), (p1, p1_tick, "P1", 1),
               (p2, p2_tick, "P2", 2)]
    last = {}
    last_tick_at = {}
    progress_rev = -1
    progress_at = t0
    last_diag = 0.0
    mid_exported = False
    post_exported = False
    post_at = None
    finished = False

    async def finish():
        nonlocal finished
        if finished:
            return
        finished = True
        if not env_state("post.json"):
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
                       "wf_sequence": WF_SEEN, "observations": OBS,
                       "data_level_ok": data_ok,
                       "cz_choice": CZ_CHOICE}, f, indent=2)
        scenario_src = open(__file__, "rb").read()
        with open(f"{EVDIR}/scenario_6912_01030.py", "w") as f:
            f.write(scenario_src.decode())

        def sha(p):
            return hashlib.sha256(open(p, "rb").read()).hexdigest()

        run_meta = {
            "run_id": RUN_ID,
            "issue": ISSUE,
            "cz_choice": CZ_CHOICE,
            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00",
                                        time.localtime(t0)),
            "server": {
                "server_version": "v0.103.0",
                "build_commit": "ec27a8d",
                "protocol_version": 106,
                "binary_sha256": sha(
                    f"{BACKFILL}/server/releases/v0.103.0/"
                    "phase-server-slim-x86_64-unknown-linux-musl"),
                "card_data_sha256": sha(
                    f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"),
                "draft_pools_sha256": sha(
                    f"{BACKFILL}/server/releases/v0.103.0/data/draft-pools.json"),
                "port": 9374,
            },
            "scenario": "driver/scenario_6912_01030.py",
            "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
            "format_config": ("CommanderDraft (>=60 cards, no singleton; "
                              "commanders placed in command zone)"),
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                      "P2": {"main": P2_DECK, "commander": []}},
            "verdict": verdict,
            "assertions": ass,
            "observations": OBS,
            "notes": notes,
            "stats": {"states_seen": ST["states_seen"]},
            "setup_line": ("P0: 12x Chaos Warp + 48x Mountain (commander Zurgo "
                           "Bellstriker, never cast); P1: 60x Forest "
                           "(commander Ayula, Queen Among Bears); P2: 60x "
                           "Forest; native human seats"),
            "contract_line": ("P0 casts Chaos Warp targeting P1's commander "
                              "Ayula; P1 (owner) answers the commander "
                              "replacement choice with the command zone "
                              "(zone branch); warp must then shuffle/reveal/put "
                              "from P1's library and put the revealed Forest "
                              "onto P1's battlefield."),
            "limitations": [
                "Browser UI not exercised; native engine via human-client seats.",
                "Dense 12x Chaos Warp / 48x Mountain decks are a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The revealed top card is random; the assertions verify the "
                "routing outcome (owner's library, permanent->BF) rather than a "
                "specific revealed card.",
                "States are authoritative exports, restorable only via full "
                "game replay (the phase-server has no standalone state-import "
                "path).",
            ],
            "driver_notes": [
                "Protocol-106 port of scenario_6912.py (verified 2026-09-12, "
                "run 20260912-6912a, verdict not-reproduced).",
                "Conventions from the proven scenario_6910_01030.py template: "
                "merged_actions/vi, PASSED_REV gating, Select-model hand-size "
                "discards (schema+select required), schema-select BottomCards, "
                "the never-hold-priority-while-watching fall-through rule, and "
                "host-only exports.",
                "CommanderDraft 3-seat setup proven in scenario_6916_01030.py.",
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

    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        for c, tick, tag, pid in clients:
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
                await tick(c, st, tag, pid)
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
            for r in c.rejections:
                wire("rejected", {"who": c.name, "type": r["type"],
                                  "data": r["data"]})
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
        if p0.revision != progress_rev or p1.revision != progress_rev \
                or p2.revision != progress_rev:
            progress_rev = max(p0.revision, p1.revision, p2.revision)
            progress_at = time.time()
        if time.time() - progress_at > PROGRESS_WATCHDOG_S:
            try:
                await do_export(p0, "mid_stall.json")
            except Exception as e:
                notes.append(f"stall export failed: {e}")
            notes.append("generic stall watchdog: no revision advance "
                         f">{PROGRESS_WATCHDOG_S}s")
            say("[stall] generic watchdog fired")
            ST["stop"] = True
            continue

        # replacement watchdog: warp cast but the replacement never answered
        if (ST["warp_cast"] and not ST["repl_answered"]
                and time.time() - (ST["warp_cast_at"] or 0) > REPL_WATCHDOG_S):
            notes.append(f"watchdog: {REPL_WATCHDOG_S}s since warp cast "
                         "without the replacement being answered; exporting "
                         "states and finishing")
            say("WATCHDOG: replacement never answered; finishing with evidence")
            try:
                await do_export(p0, "post.json")
            except Exception as e:
                notes.append(f"watchdog post export failed: {e}")
            await finish()
            break

        # mid export at the first main-loop pass after the replacement answer
        if ST["repl_answered"] and not mid_exported:
            try:
                await do_export(p0, "mid.json")
                mid_exported = True
            except Exception as e:
                notes.append(f"mid export failed: {e}")
        # post export once the resolution has had time to complete; the
        # board is frozen from the replacement answer on, so this is clean
        if (ST["repl_answered"] and not post_exported
                and time.time() - (ST["repl_answered_at"] or 0) > POST_DELAY_S):
            say("post-resolution window reached; exporting POST")
            try:
                await do_export(p0, "post.json")
                post_exported = True
                post_at = time.time()
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        if post_exported and post_at and time.time() - post_at > 5:
            say("post exported and settled; finishing")
            await finish()
            break

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={state.get('turn_number')} "
                f"active={state.get('active_player')} phase={state.get('phase')} "
                f"wf={wf} ayula={bf_id(state, 1, AYULA)} "
                f"warp={ST['warp_cast']}/tgt={ST['target_answered']}/"
                f"repl={ST['repl_answered']} frozen={ST['frozen']} "
                f"mid={mid_exported} post={post_exported} "
                f"P0hand={hand_lnames(state, 0)[:6]}")

    if not finished:
        notes.append("global timeout hit before assertions resolved")
        await finish()

    await p0.close()
    await p1.close()
    await p2.close()


if __name__ == "__main__":
    asyncio.run(main())
