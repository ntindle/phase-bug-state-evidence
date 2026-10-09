#!/usr/bin/env python3
"""Issue #6862 re-validation on pinned v0.104.0 (protocol 118):
Esper's to Magicite allows any card to become an artifact token,
not just a creature card.

Reported (Discord): "AI just exiled [[Sylvan Reclamation]] with
[[Esper's to Magicite]]. It is sitting on its battlefield."

Oracle: "Exile each opponent's graveyard. When you do, choose up to one
target creature card exiled this way. Create a token that's a copy of that
card, except it's an artifact and it loses all other card types."

Triage classifier: supported_aspect_defect on the reflexive clause
"When you do, choose up to one target creature card exiled this way."

Data level (v0.104.0 pinned card-data.json): the reflexive clause parses as
TargetOnly with And(Typed(Creature), ExiledBySource) filters, multi_target
min 0 / max 1, followed by CopyTokenOf TrackedSet with SetCardTypes
[Artifact] -- i.e. the creature restriction IS present in the parse.

History: the 2026-09-11 protocol-69 run (20260911-6862f, v0.80.0) found a
related failure -- Esper's exiled P1's graveyard but the reflexive trigger
never fired (no stack entry, no TargetSelection across 411 waiting_for
transitions), so the illegal noncreature-target path could not be reached.
The 2026-10-01 v0.98.0, 2026-10-06 v0.102.0 and 2026-10-07 v0.103.0
re-validations (protocol 106) recorded the same related failure: Esper's
resolved and exiled P1's graveyard but the reflexive trigger never fired
(no trigger stack entry, no target-choice prompt across 10 post-resolution
turns). This run re-tests on v0.104.0/protocol 118.

Plan (two human seats, native engine, protocol 118):
  SETUP - P0 plays a Swamp per turn and passes. P1 plays a land per turn
          (Mountain-first) and casts Faithless Looting whenever it holds
          one and has {R} available, discarding Bears/Reclamations first;
          ranked cleanup discards backstop the graveyard loading.
  GATE  - P0 main phase, P1 gy has >=1 Bear + >=1 Reclamation, P0 has >=4
          untapped lands -> export pre.json -> cast Esper's to Magicite
          ({3}{B}).
  RESOLVE - let Esper's resolve; export post_exile.json; the reflexive
          trigger must then offer P0 a target choice from the exiled cards.
  TARGET - record the advertised candidates (names, zones, types).
          A3: the prompt appears at all (failed on v0.80.0).
          A4: Sylvan Reclamation (instant) is NOT offered. If it IS offered
              -> illegal branch: submit it; acceptance (Reclamation artifact
              token on P0's BF) = the reported bug; rejection = offered-but-
              enforced filter defect (still a filter defect at the offer).
          Control (legal branch): choose a Bear -> an artifact-only Bear
          token must be created on P0's battlefield (A6).

Behavioral contract:
  A1 setup_ok            pre.json: P1 gy has >=1 Bear and >=1 Sylvan
                         Reclamation; P0 cast Esper's from hand with mana
  A2 exile_observed      post_exile.json: P1's graveyard emptied; the Bear
                         and Reclamation oids are in Exile
  A3 trigger_prompted    target choice offering exiled cards appears for P0
                         after Esper's resolves
  A4 noncreature_excluded Sylvan Reclamation NOT among the candidates
  A5 illegal_branch      (only if A4 failed) submit Reclamation: an artifact
                         Reclamation token on P0's BF => the reported bug
                         (failed); cleanly rejected => offered-but-enforced
                         (failed); never tried/answered => not-run
  A6 control_token       (only if A4 passed) choosing a Bear yields an
                         artifact-only Bear token on P0's battlefield
  A7 cleanup             game proceeds; stack empty; no stuck prompt

  No-prompt backstop: if 10 full turns pass after Esper's resolves with no
  reflexive-trigger prompt (or 15 wall minutes), the run exports post.json
  and stops gracefully -- the related failure is "the trigger never fires",
  so waiting for the game timeout is not the observation.

Verdict = reproduced iff the trigger never fires (related failure) or A4
fails (noncreature offered) or the illegal submission is accepted;
not-reproduced iff A3, A4, A6, A7 all pass; else blocked.

Protocol-118 driver notes (v0.104.0, ported from the verified protocol-106
scenario_6862_01020.py; interaction conventions unchanged from the 106 run):
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction.
  - MulliganDecision answered via legacy Action; deck schema
    {"main_deck": [<name strings>]} (client.py deck() already returns it).
  - DiscardToHandSize via vi schema/select opportunity offering hand cards.
  - CastSpell via legacy Action; mana via the engine's auto-pay for the
    test cast (the driver never pays for the test Esper's cast -- answering
    a legacy PayMana on top of engine auto-tap double-pays, 2026-10-07
    double-pay gate); setup casts (P1's Faithless Looting) keep the proven
    MANA_NEEDS-driven vi taps.
  - Cast-confirmation guard (protocol-118 necessity, 2026-10-09): with engine
    auto-pay there is no mana-payment phase to hold priority, so a bare
    CastSpell can lose a race to the driver's own PassPriority on the next
    tick and be silently dropped by the server. After every CastSpell the
    driver sets ST["pending_cast"] and answers only decisions/mana while it
    is unconfirmed; a 30s backstop clears a still-unconfirmed action and
    resets the scenario one-shot flag so the play re-triggers instead of
    stalling.
  - real_decision_pending excludes the noisy 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand).
  - play_a_land matches any land via is_land(); vi playLand choices
    answered before the decision gate.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients.
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
ISSUE = 6862
RUN_ID = "run-6862-reval-v01040-20261009-1041"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.104.0/data/card-data.json"))

ESPER = "espers to magicite"
BEAR = "grizzly bears"
RECLAM = "sylvan reclamation"
SWAMP = "swamp"
FOREST = "forest"
LOOTING = "faithless looting"
MOUNTAIN = "mountain"

ESPER_T = "Espers to Magicite"
BEAR_T = "Grizzly Bears"
RECLAM_T = "Sylvan Reclamation"
SWAMP_T = "Swamp"
FOREST_T = "Forest"
LOOTING_T = "Faithless Looting"
MOUNTAIN_T = "Mountain"

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "stop": False}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
MULLS = {}
MULL_BOTTOMED = {}
CAST = {}
TARGET = {"prompt_seen": False, "candidates": None, "seen_at": None,
          "illegal_tried": False, "illegal_accepted": None,
          "control_tried": False, "control_at": None}
TOKEN = {"bear": False, "reclam": False, "bear_obj": None,
         "reclam_obj": None}
LAST_IID = {"iid": None}
LOOT = {"in_flight": False, "discarded": False}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def zone_by_name(state, pid, zone, lname):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == zone
            and str(o.get("owner", o.get("controller", -1))) == str(pid)
            and str(o.get("base_name") or o.get("name") or "").lower() == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for oid in bf_oids(state, pid)
               if is_land(get_obj(state, oid))
               and not get_obj(state, oid).get("tapped"))


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


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


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


def cand_refs(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            out.append(str(d["reference"]))
    return out


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


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
    LAST_IID["iid"] = sub.get("interactionId")
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
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.104.0"), f"unexpected version {ver}"
    assert int(proto) == 118, f"unexpected protocol {proto}"
    assert str(build) == "4227122", f"unexpected build {build}"


def check_data_level():
    em = CARD_DATA.get("espers to magicite", {})
    oracle = str(em.get("oracle_text", ""))
    spell_abs = (em.get("abilities") or [{}])[0]
    sub = spell_abs.get("sub_ability") or {}
    subsub = sub.get("sub_ability") or {}
    tgt = ((sub.get("effect") or {}).get("target") or {})
    filters = tgt.get("filters", []) if tgt.get("type") == "And" else []
    type_filters = []
    for f in filters:
        if f.get("type") == "Typed":
            type_filters.extend(f.get("type_filters", []))
        if f.get("type") == "ExiledBySource":
            type_filters.append("ExiledBySource")
    mt = sub.get("multi_target") or {}
    ev = {
        "esper_oracle": oracle,
        "esper_oracle_ok": oracle.strip() == (
            "Exile each opponent's graveyard. When you do, choose up to one "
            "target creature card exiled this way. Create a token that's a "
            "copy of that card, except it's an artifact and it loses all "
            "other card types."),
        "esper_main_effect": (spell_abs.get("effect") or {}).get("type"),
        "esper_reflexive_condition": (sub.get("condition") or {}).get("type"),
        "esper_reflexive_target_filters": type_filters,
        "esper_creature_filter_present": "Creature" in type_filters,
        "esper_exiled_by_source_present": "ExiledBySource" in type_filters,
        "esper_multi_target": mt,
        "esper_copy_effect": (subsub.get("effect") or {}).get("type"),
        "esper_copy_modifications": (subsub.get("effect") or {})
        .get("additional_modifications"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    say(f"data-level: oracle ok={ev['esper_oracle_ok']} "
        f"creature_filter={ev['esper_creature_filter_present']} "
        f"exiled_by_source={ev['esper_exiled_by_source_present']} "
        f"multi_target={mt}")
    assert ev["esper_oracle_ok"], "Esper's oracle text mismatch in pinned data"
    assert ev["esper_creature_filter_present"], \
        "Creature filter missing from the v0.104.0 parse"


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    hn = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    # answer once per distinct hand: the engine can re-advertise
    # MulliganDecision at a new revision after a Keep was accepted.
    key = (tag, "mull", tuple(sorted(hn)))
    if key in SUBMITTED_OPPS:
        return True
    # Always keep: the fixture is designed to work from any 7 (P1 digs with
    # Faithless Looting; deep mulligans starve the bottom prompt).
    decision = "Keep"
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] mulligan -> {decision} ({len(hn)}: {hn})")
    wire("mulligan", {"who": tag, "decision": decision, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": decision}}})
    return True


async def do_bottom(c, acts, st, pid, tag, rank):
    """Answer bottom-after-mulligan: a vi schema/select opportunity over
    hand cards, gated to turn 1 (the only schema/select hand-card prompt
    possible then) and to seats that actually mulliganed."""
    n_mull = MULLS.get(tag, 0)
    if n_mull <= 0 or MULL_BOTTOMED.get(tag):
        return False
    state = st["state"]
    if state.get("turn_number") != 1:
        return False
    hand = hand_ids(state, pid)
    hand_set = set(hand)
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
        ref_of = {}
        for ch in cands:
            for r in cand_refs(ch):
                ref_of.setdefault(r, ch["id"])
        if not ref_of or not set(ref_of) <= hand_set:
            continue  # not a hand-card choice opportunity
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ranked = sorted(hand, key=lambda o: (rank(state, o),
                                             obj_lname(state, o)))
        pick = [o for o in ranked if o in ref_of][:n_mull]
        if len(pick) < n_mull:
            return False
        choice_ids = [ref_of[o] for o in pick]
        SUBMITTED_OPPS.add(key)
        MULL_BOTTOMED[tag] = True
        say(f"[{tag}] bottoms {n_mull}: {[obj_lname(state, o) for o in pick]}")
        wire("bottom", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                           tag)
        return True
    return False


def p1_rank(state, o):
    ln = obj_lname(state, o)
    if ln == RECLAM:
        return 0
    if ln == BEAR:
        return 1
    return 2


async def do_discard(c, acts, st, pid, tag, rank):
    if LOOT.get("in_flight"):
        return False  # Looting's own discard handler owns this window
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
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            for r in cand_refs(ch):
                ref_of.setdefault(r, ch["id"])
        ranked = sorted(hand, key=lambda o: (rank(state, o),
                                             obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                           tag)
        return True
    return False


async def do_declare_empty(c, acts, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            dd = dict(a.get("data", {}) or {})
            dd["attacks"] = []
            dd["bands"] = []
            d["data"] = dd
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            dd = dict(a.get("data", {}) or {})
            dd["assignments"] = []
            d["data"] = dd
            await submit_as_is(c, d)
            return True
    return False


async def pay_tick(c, acts, tag):
    # 2026-10-07 double-pay gate: CastSpell actions carry payment_mode Auto
    # and the engine taps lands itself; answering a legacy PayMana action on
    # top of that double-pays. Only fire when the driver has outstanding
    # mana needs for a NON-test cast (the test Esper's cast keeps
    # MANA_NEEDS empty; P1's setup Looting keeps the proven
    # MANA_NEEDS-driven vi taps).
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) <= 0:
        return False
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            say(f"[{tag}] legacy pay-mana action answered "
                f"(outstanding needs={dict(needs)})")
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
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
            if "tapLandForMana" not in codes:
                continue
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
        for ch, s in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in s:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def play_a_land(c, state, pid, acts, tag, prefer=None):
    """Play one land per turn; prefer() picks among hand lands."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    if prefer:
        lands.sort(key=lambda o: prefer(state, o))
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    st = st_of(c)
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                ref = str(d.get("reference", ""))
                if ref in [str(x) for x in lands]:
                    iid = opp.get("interactionId")
                    key = (tag, "playland", str(iid), ref)
                    if key in SUBMITTED_OPPS:
                        continue
                    SUBMITTED_OPPS.add(key)
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                    wire("play_land_vi", {"who": tag, "oid": ref})
                    await answer_vi(c, opp, ch, tag)
                    return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
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
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId":
                                                             ch.get("id")}}},
                                  c.name)
                return True
    return False


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        cands = list(d.values()) + [a.get("_src_oid")]
        for v in cands:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == lname:
                return a, iv
    return None, None


# ------------------------------------------------------------- the reflexive trigger prompt
def exile_target_opportunity(st):
    """Find a target-choice opportunity whose candidates reference
    Exile-zone objects: the Esper's reflexive trigger prompt."""
    state = st.get("state", {})
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        refs = []
        for ch in items:
            refs.extend(cand_refs(ch))
        exile_refs = [r for r in refs
                      if get_obj(state, r).get("zone") == "Exile"]
        if not exile_refs:
            continue
        # skip pure priority menus (no candidates, or passPriority codes)
        if rtype == "schema":
            spec = (data.get("spec") or {}).get("type")
            if spec in ("select", "sequence"):
                return opp, rtype, spec
        elif rtype == "exactChoices":
            codes = set()
            for ch in items:
                codes.update(x for x in surf_codes(ch) if x)
            if "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes:
                return opp, rtype, "choose"
    return None, None, None


def cand_info(state, ch):
    """(choice_id, ref_oid, name, zone) for a candidate choice."""
    refs = cand_refs(ch)
    ref = refs[0] if refs else None
    o = get_obj(state, ref) if ref else {}
    return {"choice_id": ch.get("id"), "ref": ref,
            "name": oname(o), "zone": o.get("zone")}


async def answer_trigger_prompt(c, state, acts, tag):
    """Handle P0's reflexive-trigger target choice once Esper's resolved."""
    st = st_of(c)
    opp, rtype, stype = exile_target_opportunity(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    if iid in SUBMITTED_OPPS:
        return True
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    items = data.get("candidates") or data.get("choices") or []
    cands = [cand_info(state, ch) for ch in items]
    exile_cands = [cc for cc in cands if cc["zone"] == "Exile"]
    if TARGET["candidates"] is None:
        TARGET["candidates"] = exile_cands
        TARGET["prompt_seen"] = True
        TARGET["seen_at"] = time.time()
        wire("trigger_prompt",
             {"rtype": rtype, "spec": stype,
              "candidates": exile_cands,
              "all_items": cands,
              "opportunity": opp})
        say(f"[P0] reflexive trigger prompt: rtype={rtype} spec={stype}")
        for cc in exile_cands:
            say(f"    candidate: {cc['name']} (zone={cc['zone']}, "
                f"choice={cc['choice_id']})")
        for cc in cands:
            if cc["zone"] != "Exile":
                say(f"    extra item: {cc['name']} zone={cc['zone']} "
                    f"choice={cc['choice_id']}")
        await do_export(c, "mid_target.json")

    reclam_ch = next((cc for cc in exile_cands
                      if cc["name"].lower() == RECLAM_T.lower()), None)
    bear_ch = next((cc for cc in exile_cands
                    if cc["name"].lower() == BEAR_T.lower()), None)

    # Illegal branch first: if the bug offers the noncreature, submit it.
    if reclam_ch and not TARGET["illegal_tried"]:
        pick = next(ch for ch in items
                    if ch.get("id") == reclam_ch["choice_id"])
        if rtype == "schema":
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        SUBMITTED_OPPS.add(iid)
        TARGET["illegal_tried"] = True
        say("[P0] ILLEGAL branch: submitted Sylvan Reclamation as target")
        wire("illegal_submit", {"choice": reclam_ch})
        await interact_as(c, sub, tag)
        return True
    # Legal control branch.
    if bear_ch and not TARGET["control_tried"] \
            and (not reclam_ch or TARGET["illegal_tried"]):
        pick = next(ch for ch in items
                    if ch.get("id") == bear_ch["choice_id"])
        if rtype == "schema":
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        SUBMITTED_OPPS.add(iid)
        TARGET["control_tried"] = True
        TARGET["control_at"] = time.time()
        say("[P0] CONTROL branch: submitted Grizzly Bears as target")
        wire("control_submit", {"choice": bear_ch})
        await interact_as(c, sub, tag)
        return True
    return False


# ------------------------------------------------------------- cast-confirmation guard (protocol 118)
def set_pending_cast(kind, oid, name, tag, from_zone):
    ST["pending_cast"] = {"kind": kind, "oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag,
                          "from_zone": from_zone}


def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_confirmed(state, pc):
    """True once the submitted action has verifiably taken effect."""
    zone = cast_obj_zone(state, pc["oid"])
    fz = str(pc.get("from_zone") or "").lower()
    if fz == "hand":
        # setup and test casts: leaving the hand confirms the cast fired
        return zone in ("stack", "battlefield", "graveyard", "exile",
                        "command")
    return zone in ("stack", "battlefield")


def reset_cast_attempt(stag):
    """Clear the scenario one-shot flags for a dropped action so the play
    re-triggers instead of stalling."""
    if stag == "SETUP-looting":
        LOOT["in_flight"] = False
    elif stag == "TEST-esper":
        CAST["in_flight"] = False


def cast_guard_tick(state, tag):
    """Protocol-118 cast-confirmation guard.

    Returns "hold" while our CastSpell is in flight but unconfirmed: the
    driver must not pass priority or start new plays in that window -- on
    protocol 118 a bare CastSpell can lose a race to our own PassPriority
    submitted on the next tick and be silently dropped by the server
    (observed 2026-10-09). Returns "proceed" once the action is confirmed
    (or rejected); a 30s backstop clears a still-unconfirmed action and
    resets the scenario one-shot flag so the play re-triggers instead of
    stalling. Must run before any early-return block.
    """
    pc = ST.get("pending_cast")
    if not pc:
        return "proceed"
    if CAST.get("rejected"):
        ST["pending_cast"] = None
        reset_cast_attempt(pc.get("tag"))
        say(f"[{tag}] cast rejected: {pc['name']} -- clearing for retry")
        wire("action_rejected_retry", {"name": pc["name"]})
        CAST["rejected"] = False
        return "proceed"
    if cast_confirmed(state, pc):
        ST["pending_cast"] = None
        say(f"[{tag}] action confirmed "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']}")
        wire("action_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                  "zone": cast_obj_zone(state, pc["oid"])})
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] ACTION NOT CONFIRMED after 30s "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("action_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]),
              "zone": cast_obj_zone(state, pc["oid"])})
        ST["pending_cast"] = None
        reset_cast_attempt(pc.get("tag"))
        return "proceed"
    return "hold"


# ------------------------------------------------------------- seat ticks
def p0_rank(state, o):
    # never discard Esper's; shed lands first
    if obj_lname(state, o) == ESPER:
        return 3
    if is_land(get_obj(state, o)):
        return 0
    return 1


async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    # ---- cast-confirmation guard (protocol 118): while our CastSpell is
    # in flight but unconfirmed, answer decisions and mana needs but start
    # no new plays and pass no priority. Runs before any early-return
    # block so a dropped action is always detected.
    if cast_guard_tick(state, tag) == "hold":
        if await do_mulligan(c, acts, st, 0, tag):
            return True
        if await do_bottom(c, acts, st, 0, tag, lambda s, o: 0):
            return True
        if await do_discard(c, acts, st, 0, tag, p0_rank):
            return True
        if await do_declare_empty(c, acts, tag):
            return True
        if await pay_tick(c, acts, tag):
            return True
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) > 0:
            if await pay_mana_vi(c, st, tag, needs):
                return True
        return True
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag, lambda s, o: 0):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # Esper's in flight: pass so it can resolve.
    if CAST.get("in_flight"):
        if await pass_priority(c, st, top_acts(st)):
            return True
        return True

    # reflexive trigger prompt (after resolution)
    if CAST.get("resolved"):
        if await answer_trigger_prompt(c, state, acts, tag):
            return True

    if my_main(state, 0):
        if ST["stage"] == "SETUP":
            if await play_a_land(c, state, 0, acts, tag):
                return True
        elif ST["stage"] == "CAST":
            if await cast_esper_step(c, tag, state, acts):
                return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


def p1_bottom_rank(state, o):
    # bottom lands first; never bottom the Bear/Reclamation we mulliganed for
    ln = obj_lname(state, o)
    if ln == FOREST:
        return 0
    if ln == BEAR:
        return 2
    if ln == RECLAM:
        return 3
    return 1


def p1_loot_rank(state, o):
    # Looting discard: Bears and Reclamations to the graveyard first,
    # then lands; never discard Looting itself.
    ln = obj_lname(state, o)
    if ln in (BEAR, RECLAM):
        return 0
    if is_land(get_obj(state, o)):
        return 1
    if ln == LOOTING:
        return 3
    return 2


async def do_looting_discard(c, acts, st, tag):
    """Answer P1's Faithless Looting discard-2 via vi, preferring Bear /
    Reclamation."""
    if not LOOT.get("in_flight") or LOOT.get("discarded"):
        return False
    state = st["state"]
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        codes = set()
        for ch in items:
            codes.update(x for x in surf_codes(ch) if x)
        if "passPriority" in codes or "decideOptionalEffect" in codes:
            continue
        ref_of = {}
        for ch in items:
            for r in cand_refs(ch):
                ref_of.setdefault(r, ch["id"])
        hand_refs = set(hand_ids(state, 1))
        if not ref_of or not set(ref_of) <= hand_refs:
            continue  # not a hand-card choice opportunity
        iid = opp.get("interactionId")
        key = (tag, "looting_discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ranked = sorted(hand_ids(state, 1),
                        key=lambda o: (p1_loot_rank(state, o),
                                       obj_lname(state, o)))
        picks = [o for o in ranked if o in ref_of][:2]
        if len(picks) < 2:
            return False
        choice_ids = [ref_of[o] for o in picks]
        SUBMITTED_OPPS.add(key)
        LOOT["discarded"] = True
        say(f"[{tag}] Looting discards: "
            f"{[obj_lname(state, o) for o in picks]}")
        wire("looting_discarded", {"oids": picks})
        if rtype == "schema":
            spec = (data.get("spec") or {}) or {}
            stype = spec.get("type")
            if stype not in ("select", "sequence"):
                say(f"[{tag}] Looting discard: unexpected spec {stype}")
                return False
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": choice_ids}}}
        elif rtype == "exactChoices" and len(choice_ids) == 1:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": choice_ids[0]}}}
        else:
            say(f"[{tag}] Looting discard: unexpected rtype={rtype}")
            return False
        await interact_as(c, sub, tag)
        return True
    return False


def p1_land_prefer(state, o):
    # Mountain-first (R for Looting), then Forest.
    return 0 if obj_lname(state, o) == MOUNTAIN else 1


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    # ---- cast-confirmation guard (protocol 118): same as p0 -- answer
    # decisions and mana needs, start no new plays, pass no priority.
    if cast_guard_tick(state, tag) == "hold":
        if await do_mulligan(c, acts, st, 1, tag):
            return True
        if await do_bottom(c, acts, st, 1, tag, p1_bottom_rank):
            return True
        if await do_discard(c, acts, st, 1, tag, p1_rank):
            return True
        if await do_declare_empty(c, acts, tag):
            return True
        if await pay_tick(c, acts, tag):
            return True
        if await do_looting_discard(c, acts, st, tag):
            return True
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) > 0:
            if await pay_mana_vi(c, st, tag, needs):
                return True
        return True
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag, p1_bottom_rank):
        return True
    if await do_discard(c, acts, st, 1, tag, p1_rank):
        return True
    if await do_declare_empty(c, acts, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True
    if await do_looting_discard(c, acts, st, tag):
        return True

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # P1 setup: play a land (Mountain-first), then cast Faithless Looting
    # to load the graveyard with Bears/Reclamations. Never attacks.
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag, prefer=p1_land_prefer):
            return True
        if not LOOT["in_flight"]:
            loot_oid = next((o for o in hand_ids(state, 1)
                             if obj_lname(state, o) == LOOTING), None)
            if loot_oid is not None:
                a = None
                for act in acts:
                    if "cast" not in str(act.get("type", "")).lower():
                        continue
                    vals = list((act.get("data") or {}).values()) \
                        + [act.get("_src_oid")]
                    for v in vals:
                        try:
                            if int(v) == int(loot_oid):
                                a = act
                                break
                        except (TypeError, ValueError):
                            continue
                    if a is not None:
                        break
                has_r = any(
                    obj_lname(state, oid) == MOUNTAIN
                    and not get_obj(state, oid).get("tapped")
                    for oid in bf_oids(state, 1))
                if a is not None and has_r:
                    LOOT["in_flight"] = True
                    LOOT["discarded"] = False
                    MANA_NEEDS[tag] = {"R": 1}
                    say(f"[P1] casts Faithless Looting (oid={loot_oid})")
                    wire("cast_looting", {"oid": loot_oid})
                    await submit_as_is(c, a)
                    set_pending_cast("cast", loot_oid, LOOTING_T,
                                     "SETUP-looting", "Hand")
                    return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


def vi_cast_choice(st, oid):
    """A viewer_interaction castSpell choice for the exact object: match the
    object surface's reference id."""
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None


def menu_cast_spells(st):
    """(choice_id -> (object reference, name, zone)) of castSpell choices."""
    out = {}
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            ref = name = zone = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and s.get("type") == "object":
                    ref = str(d.get("reference"))
                    name = d.get("name")
                    zone = d.get("zone")
            out[ch.get("id")] = (ref, name, zone)
    return out


async def cast_esper_step(c, tag, state, acts):
    st = st_of(c)
    if CAST.get("in_flight") or CAST.get("done"):
        return False
    esper_oid = next((o for o in hand_ids(state, 0)
                      if obj_lname(state, o) == ESPER), None)
    if esper_oid is None:
        say(f"[{tag}] CAST: no Esper's in hand; aborting")
        wire("cast_no_esper_in_hand",
             {"hand": [obj_lname(state, o) for o in hand_ids(state, 0)]})
        ST["stop"] = True
        return False
    a = None
    for act in acts:
        if "cast" not in str(act.get("type", "")).lower():
            continue
        vals = list((act.get("data") or {}).values()) + [act.get("_src_oid")]
        for v in vals:
            try:
                if int(v) == int(esper_oid):
                    a = act
                    break
            except (TypeError, ValueError):
                continue
        if a is not None:
            break
    vich = vi_cast_choice(st, esper_oid)
    if not CAST.get("menu_logged"):
        CAST["menu_logged"] = True
        menu = menu_cast_spells(st)
        wire("cast_menu", {"esper_oid": esper_oid,
                           "legacy_cast_action": a,
                           "vi_cast_choice": bool(vich),
                           "menu_cast_spells": menu,
                           "n_vi_ops": len(vi_ops(st)),
                           "legacy_types": sorted(
                               {x.get("type") for x in acts})})
        say(f"[{tag}] CAST menu: legacy={bool(a)} vi={bool(vich)} "
            f"menu={[(v[0], v[1], v[2]) for v in menu.values()]}")
    if a is None and vich is None:
        return False
    if untapped_lands(state, 0) < 4:
        return False
    await do_export(c, "pre.json")
    CAST.update({"oid": str(esper_oid), "in_flight": True, "stack_seen": False,
                 "resolved": False, "rejected": False, "done": False,
                 "rev_at_submit": c.revision,
                 "wall_at_submit": time.time()})
    # 2026-10-07 double-pay gate: the driver never pays mana for the test
    # cast -- the engine's auto-tap is the single payer. MANA_NEEDS stays
    # empty for Esper's to Magicite {3}{B}.
    wire("esper_cast", {"oid": esper_oid,
                        "via": "legal_actions" if a else "viewer_interaction"})
    say(f"[P0] casts Esper's to Magicite (oid={esper_oid})")
    if a is not None:
        await submit_as_is(c, a)
    else:
        opp, ch = vich
        await answer_vi(c, opp, ch, tag)
    set_pending_cast("cast", esper_oid, ESPER_T, "TEST-esper", "Hand")
    return True


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return out


# ------------------------------------------------------------- main
async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}
        MULLS[k] = 0
        MULL_BOTTOMED[k] = False

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06862")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((ESPER_T, 12), (SWAMP_T, 48)))
    p1 = PhaseClient("P16862")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code,
                  deck((LOOTING_T, 12), (BEAR_T, 8), (RECLAM_T, 8),
                       (MOUNTAIN_T, 16), (FOREST_T, 16)))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej and c is p0 and CAST.get("in_flight") \
                    and not CAST.get("stack_seen"):
                CAST["rejected"] = True
                say("[P0] Esper's cast rejected?!")
            if rej and LAST_IID["iid"] in SUBMITTED_OPPS:
                SUBMITTED_OPPS.discard(LAST_IID["iid"])
                say(f"[{c.name}] resync after rejection")
                LAST_IID["iid"] = None
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]

        # Faithless Looting resolution bookkeeping
        if LOOT.get("in_flight") and LOOT.get("discarded"):
            if not any(obj_lname(state, oid) == LOOTING
                       and get_obj(state, oid).get("zone") == "Stack"
                       for oid in state.get("objects", {})):
                LOOT["in_flight"] = False
                say("Looting resolved (discard done)")

        # SETUP -> CAST gate
        if ST["stage"] == "SETUP" and my_main(state, 0):
            n_bear = len(zone_by_name(state, 1, "Graveyard", BEAR))
            n_recl = len(zone_by_name(state, 1, "Graveyard", RECLAM))
            un = untapped_lands(state, 0)
            has_esper = any(obj_lname(state, o) == ESPER
                            for o in hand_ids(state, 0))
            if n_bear >= 1 and n_recl >= 1 and un >= 4 and has_esper:
                ST["stage"] = "CAST"
                SUBMITTED_OPPS.clear()
                say(f"=== stage -> CAST (bear_gy={n_bear} recl_gy={n_recl} "
                    f"untapped={un}) ===")
                wire("setup_ready", {"bear_gy": n_bear, "recl_gy": n_recl,
                                     "untapped": un})
                continue

        # Esper's resolution watch
        if CAST.get("in_flight"):
            o = get_obj(state, CAST["oid"])
            if o.get("zone") == "Stack" and not CAST.get("stack_seen"):
                CAST["stack_seen"] = True
                wire("esper_stack_seen", {"oid": CAST["oid"]})
                say(f"Esper's {CAST['oid']} on stack")
            if CAST.get("stack_seen") and o.get("zone") != "Stack":
                CAST["in_flight"] = False
                CAST["resolved"] = True
                CAST["done"] = True
                CAST["resolved_turn"] = state.get("turn_number")
                CAST["resolved_wall"] = time.time()
                gy_bear = zone_by_name(state, 1, "Graveyard", BEAR)
                gy_recl = zone_by_name(state, 1, "Graveyard", RECLAM)
                ex_bear = zone_by_name(state, 1, "Exile", BEAR)
                ex_recl = zone_by_name(state, 1, "Exile", RECLAM)
                wire("esper_resolved",
                     {"gy_bear": gy_bear, "gy_recl": gy_recl,
                      "ex_bear": ex_bear, "ex_recl": ex_recl})
                say(f"Esper's resolved: P1 gy bear={len(gy_bear)} "
                    f"recl={len(gy_recl)}; exile bear={len(ex_bear)} "
                    f"recl={len(ex_recl)}")
                await do_export(p0, "post_exile.json")
                ST["stage"] = "TARGET"

        # no-prompt backstop: the related failure under test is "the
        # reflexive trigger never fires". Once 10 full turns have passed
        # since resolution with no prompt (or 15 wall-clock minutes, covering
        # a stalled game), export post.json and stop gracefully. The
        # v0.103.0 run lacked this stop and had to be hand-evaluated.
        if CAST.get("resolved") and not TARGET["prompt_seen"] \
                and not ST["stop"]:
            rt = CAST.get("resolved_turn")
            rt_wall = CAST.get("resolved_wall")
            turn_ok = rt is not None \
                and state.get("turn_number", 0) >= rt + 10
            wall_ok = rt_wall is not None \
                and time.time() - rt_wall > 900
            if turn_ok or wall_ok:
                say(f"no trigger prompt 10 turns post-resolution "
                    f"(resolved turn {rt}, now turn "
                    f"{state.get('turn_number')}); stopping gracefully")
                wire("no_prompt_backstop",
                     {"resolved_turn": rt,
                      "turn": state.get("turn_number")})
                try:
                    await do_export(p0, "post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # illegal acceptance watch: Reclamation artifact token on P0's BF
        if TARGET["illegal_tried"] and TARGET["illegal_accepted"] is None:
            for oid in bf_oids(state, 0):
                o = get_obj(state, oid)
                if o.get("is_token") and obj_lname(state, oid) == RECLAM:
                    TARGET["illegal_accepted"] = True
                    TOKEN["reclam"] = True
                    TOKEN["reclam_obj"] = {k: o.get(k) for k in
                                           ("card_name", "base_name",
                                            "is_token", "card_types",
                                            "card_type", "types",
                                            "power", "toughness")}
                    say("ILLEGAL branch ACCEPTED: Reclamation token on BF!")
                    wire("illegal_token", {"oid": oid,
                                           "obj": TOKEN["reclam_obj"]})
                    break

        # control token watch: Bear artifact token on P0's BF
        if TARGET["control_tried"] and not TOKEN["bear"]:
            for oid in bf_oids(state, 0):
                o = get_obj(state, oid)
                if o.get("is_token") and obj_lname(state, oid) == BEAR:
                    TOKEN["bear"] = True
                    TOKEN["bear_obj"] = {k: o.get(k) for k in
                                         ("card_name", "base_name",
                                          "is_token", "card_types",
                                          "card_type", "types",
                                          "power", "toughness")}
                    wire("control_token", {"oid": oid,
                                           "obj": TOKEN["bear_obj"]})
                    say(f"control token seen: {TOKEN['bear_obj']}")
                    break

        # finish: token observed (control) or illegal accepted
        if (TOKEN["bear"] or TARGET["illegal_accepted"]) and not ST["stop"]:
            stack_empty = not any(o.get("zone") == "Stack"
                                  for o in state["objects"].values())
            if stack_empty:
                await asyncio.sleep(2)
                await do_export(p0, "post.json")
                ST["stop"] = True
                say("=== DONE ===")

        # prompt seen but never answerable: stop gracefully
        if TARGET["prompt_seen"] and TARGET["seen_at"] \
                and not TARGET["illegal_tried"] and not TARGET["control_tried"] \
                and not ST["stop"] \
                and time.time() - TARGET["seen_at"] > 120:
            say("trigger prompt seen but no branch taken after 120s; stopping")
            wire("prompt_unanswerable",
                 {"candidates": TARGET["candidates"]})
            try:
                await do_export(p0, "post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True
        if TARGET.get("control_at") and not TOKEN["bear"] \
                and not ST["stop"] \
                and time.time() - TARGET["control_at"] > 90:
            say("control branch: no token after 90s; exporting post anyway")
            wire("control_timeout", {})
            try:
                await do_export(p0, "post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True
        if TARGET["illegal_tried"] and TARGET["illegal_accepted"] is None \
                and not ST["stop"] and TARGET["seen_at"] \
                and time.time() - TARGET["seen_at"] > 150:
            # illegal submission neither accepted (token) nor rejected
            # outright: treat as offered-but-unanswered; stop gracefully.
            say("illegal branch submitted but no token after 150s; stopping")
            wire("illegal_timeout", {})
            try:
                await do_export(p0, "post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True

        if time.time() - last_diag > 90:
            last_diag = time.time()
            s = state
            say(f"DIAG P0: rev={p0.revision} turn={s.get('turn_number')} "
                f"phase={s.get('phase')} prio={my_priority(top_acts(st_of(p0)))} "
                f"decision={real_decision_pending(st_of(p0))} "
                f"stage={ST['stage']} cast={CAST} "
                f"prompt={TARGET['prompt_seen']} "
                f"gy_bear={len(zone_by_name(s, 1, 'Graveyard', BEAR))} "
                f"gy_recl={len(zone_by_name(s, 1, 'Graveyard', RECLAM))} "
                f"untapped={untapped_lands(s, 0)}")

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    A = obs["assert"]
    pre = env_state("pre.json")
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre.json never exported (setup gate not reached)")
    else:
        n_bear = len(zone_by_name(pre, 1, "Graveyard", BEAR))
        n_recl = len(zone_by_name(pre, 1, "Graveyard", RECLAM))
        obs["notes"].append(
            f"pre.json: turn={pre.get('turn_number')} phase={pre.get('phase')} "
            f"p1_gy bear={n_bear} recl={n_recl}")
        A["A1_setup_ok"] = ("passed" if (n_bear >= 1 and n_recl >= 1
                                         and CAST.get("rev_at_submit"))
                            else "failed")

    post_ex = env_state("post_exile.json")
    if post_ex is None:
        A["A2_exile_observed"] = ("failed" if CAST.get("resolved")
                                  else "not-run")
    else:
        gy_bear = len(zone_by_name(post_ex, 1, "Graveyard", BEAR))
        gy_recl = len(zone_by_name(post_ex, 1, "Graveyard", RECLAM))
        ex_bear = len(zone_by_name(post_ex, 1, "Exile", BEAR))
        ex_recl = len(zone_by_name(post_ex, 1, "Exile", RECLAM))
        obs["notes"].append(
            f"post_exile.json: P1 gy bear={gy_bear} recl={gy_recl}; "
            f"exile bear={ex_bear} recl={ex_recl}")
        A["A2_exile_observed"] = ("passed"
                                  if (gy_bear == 0 and gy_recl == 0
                                      and ex_bear >= 1 and ex_recl >= 1)
                                  else "failed")

    A["A3_trigger_prompted"] = ("passed" if TARGET["prompt_seen"]
                                else ("failed" if CAST.get("resolved")
                                      else "not-run"))
    cands = TARGET["candidates"] or []
    cnames = [c["name"] for c in cands]
    A["A4_noncreature_excluded"] = ("passed" if TARGET["prompt_seen"]
                                    and not any("reclamation" in n.lower()
                                                for n in cnames)
                                    else ("failed" if TARGET["prompt_seen"]
                                          and any("reclamation" in n.lower()
                                                  for n in cnames)
                                          else "not-run"))
    if TARGET["illegal_tried"]:
        if TARGET["illegal_accepted"] is True:
            A["A5_illegal_branch"] = "failed"
        elif TARGET["illegal_accepted"] is False:
            A["A5_illegal_branch"] = "passed"
        else:
            A["A5_illegal_branch"] = "not-run"
    else:
        A["A5_illegal_branch"] = "not-run"
    bear_obj = TOKEN.get("bear_obj") or {}
    ctypes = bear_obj.get("card_types") or bear_obj.get("card_type") or {}
    core = ctypes.get("core_types") if isinstance(ctypes, dict) else None
    A["A6_control_token"] = ("passed" if TOKEN["bear"]
                             and core == ["Artifact"]
                             else ("failed" if TARGET["control_tried"]
                                   and not TOKEN["bear"]
                                   else "not-run"))
    try:
        post = env_state("post.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        obs["notes"].append(f"post.json: stack_empty={stack_empty} "
                            f"turn={post.get('turn_number')} "
                            f"phase={post.get('phase')}")
        A["A7_cleanup"] = ("passed" if stack_empty else "failed")
    except Exception as e:
        A["A7_cleanup"] = "not-run"
        obs["notes"].append(f"post.json missing ({e}); A7 not-run")

    obs["notes"].append(f"candidates offered: {cnames}")
    obs["notes"].append(f"illegal branch: tried={TARGET['illegal_tried']} "
                        f"accepted={TARGET['illegal_accepted']}")
    obs["notes"].append(f"control token obj: {TOKEN['bear_obj']}")
    obs["notes"].append("protocol-118 driver (v0.104.0): mulligan via legacy "
                        "MulliganDecision action (always keep; deep mulligans "
                        "starve the bottom prompt); P1 loads its graveyard "
                        "with Faithless Looting (cast via legacy Action, "
                        "{R} via legacy PayMana/vi tapLandForMana, discard-2 "
                        "via vi schema/select ranked Bear/Reclamation > "
                        "lands); DiscardToHandSize via vi schema/select; "
                        "CastSpell via legacy Action with vi castSpell-choice "
                        "fallback; the test Esper's cast keeps MANA_NEEDS "
                        "empty (engine auto-pay is the single payer; "
                        "2026-10-07 double-pay gate); cast-confirmation "
                        "guard holds priority/no-new-plays until each cast "
                        "is confirmed on the stack (2026-10-09 protocol-118 "
                        "race fix); reflexive trigger prompt detected "
                        "by vi opportunity candidates referencing Exile-zone "
                        "objects; 5s re-tick backstop.")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    if A["A3_trigger_prompted"] == "failed" \
            or A["A4_noncreature_excluded"] == "failed" \
            or TARGET["illegal_accepted"] is True:
        verdict = "reproduced"
    elif (A["A3_trigger_prompted"] == "passed"
          and A["A4_noncreature_excluded"] == "passed"
          and A["A6_control_token"] == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "candidates": cands,
                   "illegal": {"tried": TARGET["illegal_tried"],
                               "accepted": TARGET["illegal_accepted"]},
                   "control_token": TOKEN["bear_obj"],
                   "reclam_token": TOKEN["reclam_obj"]}, f, indent=1)

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": ST.get("game_code"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": {
            "validated_version": "v0.104.0",
            "build_commit": "4227122",
            "protocol_version": 118,
            "server_binary_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.104.0/"
                "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.104.0/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.104.0/data/draft-pools.json"),
            "signature_verified": True,
        },
        "driver": {"protocol_advertised": 118,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [[ESPER_T, 12], [SWAMP_T, 48]],
            "P1": [[LOOTING_T, 12], [BEAR_T, 8], [RECLAM_T, 8],
                   [MOUNTAIN_T, 16], [FOREST_T, 16]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x playset densities are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_6862_01040.py), not direct load.",
            "Native AI seats not used; the reflexive trigger is engine-level and seat-independent.",
        ],
        "setup_line": "P0: 12x Espers to Magicite + 48x Swamp (land-drop, pass); "
                      "P1: 12x Faithless Looting + 8x Grizzly Bears + 8x "
                      "Sylvan Reclamation + 16x Mountain + 16x Forest "
                      "(land-drop; Looting discards Bears/Reclamations; "
                      "ranked cleanup discards backstop)",
        "contract_line": ("Esper's to Magicite exiles P1's graveyard; the "
                          "reflexive trigger offers exiled creature cards "
                          "only (Sylvan Reclamation excluded); choosing a "
                          "Bear creates an artifact-only Bear token."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")

    await render_summary(f"{EVDIR}/summary.png")
    shutil.copy(__file__, f"{EVDIR}/scenario_6862_01040.py")
    say("copied driver into evidence dir")
    await write_manifest()

    await p0.close()
    await p1.close()
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass


def load_ev_state(path):
    with open(f"{EVDIR}/{path}") as f:
        return json.load(f)["state"]


async def render_summary(out_path):
    """Render the PNG from the saved evidence files only."""
    from PIL import Image, ImageDraw
    A = json.load(open(f"{EVDIR}/assertions.json"))
    notes = A.get("notes", [])
    asserts = A.get("assertions", {})
    run = json.load(open(f"{EVDIR}/run.json"))
    try:
        pre = load_ev_state("pre.json")
        nb = len(zone_by_name(pre, 1, "Graveyard", BEAR))
        nr = len(zone_by_name(pre, 1, "Graveyard", RECLAM))
        pre_line = (f"pre: turn {pre.get('turn_number')} {pre.get('phase')} | "
                    f"P1 gy bear={nb} recl={nr}")
    except Exception:
        pre_line = "pre.json: missing"
    try:
        post_ex = load_ev_state("post_exile.json")
        eb = len(zone_by_name(post_ex, 1, "Exile", BEAR))
        er = len(zone_by_name(post_ex, 1, "Exile", RECLAM))
        ex_line = f"post-exile: P1 exile bear={eb} recl={er}"
    except Exception:
        ex_line = "post_exile.json: missing"
    W, H = 1000, 800
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6862 - Esper's to Magicite noncreature target "
                    "(v0.104.0 revalidation)", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), pre_line, fill=(170, 180, 195))
    y += 26
    d.text((24, y), ex_line, fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: P1 gy has Bear + Reclamation at cast",
        "A2_exile_observed": "A2 P1 graveyard exiled (Bear/Recl in Exile)",
        "A3_trigger_prompted": "A3 reflexive trigger offers exiled cards",
        "A4_noncreature_excluded": "A4 Sylvan Reclamation NOT offered",
        "A5_illegal_branch": "A5 illegal submit rejected (if attempted)",
        "A6_control_token": "A6 Bear choice -> artifact-only Bear token",
        "A7_cleanup": "A7 stack empty; game proceeds",
    }
    for k, lab in labels.items():
        v = asserts.get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in notes[:10]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6862/"
           + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
