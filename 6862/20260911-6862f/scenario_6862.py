#!/usr/bin/env python3
"""Issue #6862: Esper's to Magicite allows any card to become an artifact
token, not just a creature card.

Reported (Discord): "AI just exiled [[Sylvan Reclamation]] with
[[Esper's to Magicite]]. It is sitting on its battlefield."

Oracle: "Exile each opponent's graveyard. When you do, choose up to one
target creature card exiled this way. Create a token that's a copy of that
card, except it's an artifact and it loses all other card types."

Triage: the supported target AST says `creature + cards exiled by source`,
so the illegal noncreature selection points to runtime target-filter
enforcement or candidate enumeration.

Plan (two human seats, native engine):
  SETUP  - land drops; P1 casts Grizzly Bears, attacks with them; P0 casts
           Grave Titan and blocks Bears -> 2+ Bear cards in P1's graveyard.
  LOAD   - P1 holds cards (no land drops / casts / attacks) until a cleanup
           discard puts Sylvan Reclamation into P1's graveyard.
  READY  - P0 main phase, Esper's to Magicite in hand, >=4 untapped lands
           (incl. a Swamp) -> export pre.json -> cast Esper's to Magicite.
  RESOLVE- let Esper's resolve (P1 graveyard exiled), then its reflexive
           "choose up to one target creature card exiled this way" trigger
           must prompt P0 with a TargetSelection.
  TARGET - record the advertised candidates (names, zones, types).
           A2: Bear creature cards are offered.
           A3: Sylvan Reclamation (instant) is NOT offered. If it IS offered
               -> illegal branch: submit it; acceptance = full bug, rejection
               = offered-but-enforced (still a filter defect at the offer).
           Control (legal branch): choose a Bear -> token must be an
           artifact-only copy of the Bear (A5).

Behavioral contract:
  A1 setup_ok            pre.json: P1 gy has >=1 Bear card and >=1 Sylvan
                         Reclamation; P0 cast Esper's from hand w/ mana
  A2 creature_offered    target prompt lists Bear card(s) as candidates
  A3 noncreature_excluded Sylvan Reclamation NOT among candidates
  A4 illegal_enforcement (only if A3 failed) submit Reclamation: rejected
                         => offered-but-enforced; accepted => full bug
  A5 control_token       choosing a Bear yields an artifact-only Bear token
                         on P0's battlefield
  A6 cleanup             game proceeds; stack empty; no stuck prompt

Verdict = reproduced iff A3 fails (noncreature offered) or A4 shows the
illegal choice accepted; not-reproduced iff A2, A3, A5, A6 pass.
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

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6862f"
EVID_ISSUE = "6862"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ESPER = "Espers to Magicite"
RECLAM = "Sylvan Reclamation"
BEAR = "Grizzly Bears"
TITAN = "Grave Titan"
SWAMP = "Swamp"
FOREST = "Forest"
PLAINS = "Plains"

ST = {"stage": "SETUP", "stop": False, "retry": False,
      "reclam_cast_seen": False, "tried_reclam_cast": False}


def reset_globals():
    """Reinitialize all per-game state before a new attempt."""
    global C0
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False,
               "reclam_cast_seen": False, "tried_reclam_cast": False})
    CAST.clear()
    P1MODE.clear()
    P1MODE.update({"hold": False})
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    TARGET.clear()
    TARGET.update({"prompt_seen": False, "candidates": None, "shape": None,
                   "answered": False, "illegal_tried": False,
                   "illegal_accepted": None, "control_tried": False,
                   "token_oid": None})
    TOKEN.clear()
    TOKEN.update({"seen": False})
    C0 = None
CAST = {}            # Esper's cast tracking
P1MODE = {"hold": False}   # True once bears are dead: P1 holds for discard
MULLS = {"P0": 0, "P1": 0}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
TARGET = {"prompt_seen": False, "candidates": None, "shape": None,
          "answered": False, "illegal_tried": False, "illegal_accepted": None,
          "control_tried": False, "token_oid": None}
TOKEN = {"seen": False}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def owner_of(o, pid):
    return o.get("owner") == pid or o.get("controller") == pid


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def zone_oids(state, pid, zone, name=None):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == zone and owner_of(o, pid):
            if name is None or oname(o) == name:
                out.append(str(oid))
    return out


def untapped_land(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in (SWAMP,) and not o.get("tapped"))


def n_untapped_lands(state, pid):
    n = 0
    for _, o in bf(state, pid):
        nm = oname(o)
        if nm in (SWAMP, FOREST, PLAINS) and not o.get("tapped"):
            n += 1
    return n


def untapped_swamp(state, pid):
    return any(oname(o) == SWAMP and not o.get("tapped")
               for _, o in bf(state, pid))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data"),
                             "stage": ST["stage"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"]})
    await c.send_interaction(sub)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:260]}")
    return found


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def candidate_info(opp, state):
    """Resolve opportunity candidates to (choiceId, ref_oid, name, zone)."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                ref = str(d["reference"])
                break
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref,
                    "name": oname(o), "zone": o.get("zone"),
                    "owner": o.get("owner")})
    return out


def prompt_text(opp):
    bits = []
    for s in opp.get("surfaces", []) or []:
        if s.get("type") == "text":
            d = s.get("data") or {}
            t = d.get("text") or d.get("label") or ""
            if t:
                bits.append(str(t))
    blob = json.dumps(opp.get("response", {}).get("data", {}), default=str)
    return " | ".join(bits) + " || " + blob[:400]


async def answer_target_prompt(c, state, acts, st):
    """Handle P0's TargetSelection for the Esper's reflexive trigger."""
    vi = get_vi(st)
    if not vi:
        return False
    wf = (state.get("waiting_for") or {})
    if wf.get("type") != "TargetSelection":
        return False
    if (wf.get("data") or {}).get("player") != 0:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        zones = {c["zone"] for c in cands}
        # This is the Esper's trigger prompt iff candidates come from Exile.
        if "Exile" not in zones:
            continue
        # Illegal submission still pending on a re-offered prompt: the
        # engine did not accept it (rejected or ignored).
        if TARGET["illegal_tried"] and TARGET["illegal_accepted"] is None \
                and iid not in SUBMITTED:
            TARGET["illegal_accepted"] = False
            say("[P0] illegal submission not accepted (prompt re-offered)")
            wire("illegal_rejected_by_reoffer", {})
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        key = (rtype, spec_type, len(chs))
        if key not in SHAPES:
            SHAPES.add(key)
            wire("target_prompt", {"rtype": rtype, "spec": spec_type,
                                   "candidates": cands,
                                   "prompt": prompt_text(opp),
                                   "opportunity": opp})
            say(f"[P0] Esper's target prompt: rtype={rtype} spec={spec_type}")
            for cc in cands:
                say(f"    candidate: {cc['name']} (zone={cc['zone']}, "
                    f"choice={cc['choice_id']})")
        if TARGET["candidates"] is None:
            TARGET["candidates"] = cands
            TARGET["shape"] = key
            TARGET["prompt_seen"] = True
            TARGET["seen_at"] = time.time()
            await export_now("mid_target.json")
            say("[P0] recorded target candidates; exported mid_target.json")

        names = [c["name"] for c in cands]
        reclam_ch = next((c for c in cands if c["name"] == RECLAM), None)
        bear_ch = next((c for c in cands if c["name"] == BEAR), None)

        # Illegal branch first (only when the bug offers it).
        if reclam_ch and not TARGET["illegal_tried"]:
            if rtype == "schema" and spec_type in ("sequence", "select"):
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": [reclam_ch["choice_id"]]}}
            elif rtype == "exactChoices":
                resp_out = {"type": "choose",
                            "data": {"choiceId": reclam_ch["choice_id"]}}
            else:
                say(f"[P0] illegal branch: unexpected shape {key}; skipping")
                TARGET["illegal_tried"] = True
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            SUBMITTED.add(iid)
            TARGET["illegal_tried"] = True
            say("[P0] ILLEGAL branch: submitted Sylvan Reclamation as target")
            return True
        # Legal control branch.
        if bear_ch and not TARGET["control_tried"] and \
                (not reclam_ch or TARGET["illegal_tried"]):
            if rtype == "schema" and spec_type in ("sequence", "select"):
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": [bear_ch["choice_id"]]}}
            elif rtype == "exactChoices":
                resp_out = {"type": "choose",
                            "data": {"choiceId": bear_ch["choice_id"]}}
            else:
                say(f"[P0] control branch: unexpected shape {key}; skipping")
                TARGET["control_tried"] = True
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            SUBMITTED.add(iid)
            TARGET["control_tried"] = True
            TARGET["control_at"] = time.time()
            TARGET["answered"] = True
            say("[P0] CONTROL branch: submitted Grizzly Bears as target")
            return True
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            if is_p0:
                # Mull for an early Grave Titan (survival); Esper's is drawn
                # later with 4 copies over ~10 turns before CAST stage.
                keep = any(oname(state["objects"][o]) == TITAN
                           for o in hand_oids(state, pid)) and \
                    sum(1 for o in hand_oids(state, pid)
                        if oname(state["objects"][o]) == SWAMP) >= 2
                choice = "Keep" if (keep or MULLS["P0"] >= 2) else "Mulligan"
                if choice == "Mulligan":
                    MULLS["P0"] += 1
            else:
                lands = sum(1 for o in hand_oids(state, pid)
                            if oname(state["objects"][o]) in (FOREST, PLAINS))
                has = any(oname(state["objects"][o]) in (BEAR, RECLAM)
                          for o in hand_oids(state, pid))
                choice = "Keep" if ((lands >= 2 and has) or MULLS["P1"] >= 2) \
                    else "Mulligan"
                if choice == "Mulligan":
                    MULLS["P1"] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    # BottomCards
    for a in acts:
        if a["type"] == "SelectCards" and \
                (state.get("waiting_for") or {}).get("type") == \
                "MulliganDecision":
            pending = ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    # DiscardToHandSize: P1 prefers discarding Sylvan Reclamation (LOAD plan)
    wt0 = (state.get("waiting_for") or {}).get("type")
    if wt0 == "DiscardToHandSize":
        pend = (state.get("waiting_for") or {}).get("data") or {}
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            if not is_p0:
                pref = [o for o in h if oname(state["objects"][o]) == RECLAM]
                pref += [o for o in h if oname(state["objects"][o]) not in
                         (RECLAM, BEAR)]
                pref += [o for o in h if o not in pref]
                picks = pref[:n]
            else:
                # P0: never discard Esper's; shed lands first
                pref = [o for o in h if oname(state["objects"][o]) == SWAMP]
                pref += [o for o in h if oname(state["objects"][o]) not in
                         (SWAMP, ESPER)]
                pref += [o for o in h if o not in pref]
                picks = pref[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)} to hand size: "
                    f"{[oname(state['objects'][o]) for o in picks]}")
                return True
    # combat: P0 never attacks; P1 attacks with all Bears (unless holding).
    # Wire: attacks are (ObjectId, AttackTarget) tuples ->
    #   [[attacker_oid, {"type": "Player", "data": <player_id>}]]
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            if is_p0 or P1MODE["hold"]:
                sub["data"]["attacks"] = []
            else:
                bears = [oid for oid, o in bf(state, pid)
                         if oname(o) == BEAR and not o.get("tapped")
                         and not o.get("summoning_sick")]
                sub["data"]["attacks"] = [
                    [int(oid), {"type": "Player", "data": 0}]
                    for oid in bears]
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            say(f"{c.name} declares attackers: "
                f"{len(sub['data']['attacks'])}")
            return True
        if a["type"] == "DeclareBlockers":
            # Wire: assignments are (blocker, attacker) ObjectId tuples.
            sub = copy.deepcopy(a)
            if ("blockers_shape", pid) not in SHAPES:
                SHAPES.add(("blockers_shape", pid))
                wire("declare_blockers_shape", {"who": c.name, "action": a})
            if is_p0:
                # block each attacker with an untapped Titan; attackers are
                # the tapped Bears (they tap as part of declaration)
                attackers = [oid for oid, o in bf(state, 1)
                             if oname(o) == BEAR and o.get("tapped")]
                titans = [oid for oid, o in bf(state, pid)
                          if oname(o) == TITAN and not o.get("tapped")]
                assignments = [[int(titans[i]), int(attackers[i])]
                               for i in range(min(len(titans),
                                                  len(attackers)))]
                sub["data"]["assignments"] = assignments
                say(f"[P0] blocks {len(assignments)} attackers")
                wire("block_plan", {"attackers": attackers,
                                    "titans": titans,
                                    "assignments": assignments})
            else:
                sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    # P0's Esper's target prompt
    if is_p0 and await answer_target_prompt(c, state, acts, st):
        return True
    # never pass while P0 has a decision pending
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                        "ManaPayment", "ChooseXValue", "DiscardChoice") \
            and wplayer == 0:
        return False
    # Esper's cast in flight: take no main-phase actions, but still pass
    # priority so the spell can resolve (holding here deadlocks: after
    # casting, the caster gets priority first).
    esper_in_flight = False
    if is_p0 and CAST.get("in_flight"):
        oid = CAST.get("oid")
        o = state["objects"].get(str(oid), {})
        on_stack = o.get("zone") == "Stack"
        if on_stack:
            CAST["stack_seen"] = True
        resolved = CAST.get("stack_seen") and not on_stack
        esper_in_flight = not resolved and not CAST.get("rejected")
    if is_p0 and is_my_main(state, pid) and not esper_in_flight:
        if ST["stage"] == "SETUP":
            if await p0_setup(c, pid, state, acts):
                return True
        elif ST["stage"] == "CAST":
            if await p0_cast_step(c, pid, state, acts):
                return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    # default: pass priority
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p0_setup(c, pid, state, acts):
    # land drop
    lid = find_hand(state, pid, SWAMP)
    for a in acts:
        if a["type"] == "PlayLand" and lid and str(
                a.get("data", {}).get("object_id")) == lid:
            await submit_as_is(c, a)
            return True
    # cast Grave Titan once 6 untapped lands are available
    if n_untapped_lands(state, 0) >= 6:
        oid = find_hand(state, pid, TITAN)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say("P0 casts Grave Titan")
            return True
    return False


async def p0_cast_step(c, pid, state, acts):
    # READY gate already checked in main loop; cast here.
    if CAST.get("in_flight") or CAST.get("done"):
        return False
    if not is_my_main(state, pid):
        return False
    oid = find_hand(state, pid, ESPER)
    a = castspell_advertised(acts, oid)
    if not oid or not a:
        return False
    if n_untapped_lands(state, 0) < 4 or not untapped_swamp(state, 0):
        return False
    await export_now("pre.json")
    CAST.update({"oid": str(oid), "in_flight": True, "stack_seen": False,
                 "rejected": False, "done": False,
                 "rev_at_submit": c.revision})
    await submit_as_is(c, a)
    say(f"P0 casts Esper's to Magicite (oid={oid})")
    wire("esper_cast", {"oid": oid})
    return True


async def p1_step(c, pid, state, acts):
    hold = P1MODE["hold"]
    # land drop (not while holding)
    if not hold:
        lid = find_hand(state, pid, FOREST) or find_hand(state, pid, PLAINS)
        for a in acts:
            if a["type"] == "PlayLand" and lid and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    # cast Bears (not while holding); one per tick is fine
    if not hold and is_my_main(state, pid):
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a and n_untapped_lands(state, 1) >= 2:
            await submit_as_is(c, a)
            say("P1 casts Grizzly Bears")
            return True
    return False


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((ESPER, 4), (TITAN, 12), (SWAMP, 44)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((BEAR, 8), (RECLAM, 8),
                                    (FOREST, 22), (PLAINS, 22)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1500
    esper_resolve_watch = {"armed": False}
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej:
                if is_p0 and CAST.get("in_flight") and \
                        not CAST.get("stack_seen"):
                    CAST["rejected"] = True
                    say("[P0] Esper's cast rejected?!")
                if LAST_SUBMIT["iid"] in SUBMITTED:
                    SUBMITTED.discard(LAST_SUBMIT["iid"])
                    LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # --- game ended: finalize if the trigger sequence completed,
        # otherwise flag a retry (e.g. P0 died during setup)
        if (state.get("waiting_for") or {}).get("type") == "GameOver" \
                and not ST["stop"]:
            if TOKEN["seen"] or TARGET["illegal_accepted"] is not None \
                    or TARGET["control_tried"]:
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            else:
                ST["retry"] = True
                ST["stop"] = True
                obs["notes"].append(
                    "game ended before Esper's trigger sequence; retry")
                say("game over before trigger -> retrying with new game")

        # --- SETUP bookkeeping: bears dead -> P1 hold mode
        if ST["stage"] == "SETUP":
            n_bears_gy = len(zone_oids(state, 1, "Graveyard", BEAR))
            n_reclam_gy = len(zone_oids(state, 1, "Graveyard", RECLAM))
            if n_bears_gy >= 2 and not P1MODE["hold"]:
                P1MODE["hold"] = True
                say(f"=== P1 hold mode (bears in gy={n_bears_gy}) ===")
                wire("p1_hold", {"bears_gy": n_bears_gy})
            if n_bears_gy >= 2 and n_reclam_gy >= 1:
                ST["stage"] = "CAST"
                SUBMITTED.clear()
                say(f"=== stage -> CAST (bears={n_bears_gy} "
                    f"reclam={n_reclam_gy}) ===")

        # --- Esper's resolution watch
        if CAST.get("in_flight") and CAST.get("stack_seen"):
            o = state["objects"].get(str(CAST["oid"]), {})
            if o.get("zone") != "Stack":
                CAST["in_flight"] = False
                CAST["done"] = True
                gy = zone_oids(state, 1, "Graveyard")
                ex = zone_oids(state, 1, "Exile")
                wire("esper_resolved", {"p1_gy_remaining": len(gy),
                                       "p1_exile": len(ex),
                                       "exile_names": sorted(
                                           {oname(state["objects"][x])
                                            for x in ex})})
                say(f"Esper's resolved: P1 gy={len(gy)} exile={len(ex)}")
                await export_now("post_exile.json")
                ST["stage"] = "TARGET"

        # --- token watch (control branch)
        if TARGET["control_tried"] and not TOKEN["seen"]:
            for oid, o in state["objects"].items():
                if o.get("zone") == "Battlefield" and \
                        o.get("controller") == 0 and \
                        oname(o) == BEAR and o.get("is_token"):
                    TOKEN["seen"] = True
                    TOKEN["oid"] = oid
                    TOKEN["obj"] = {k: o.get(k) for k in
                                    ("card_name", "name", "is_token",
                                     "card_type", "types", "subtypes",
                                     "power", "toughness")}
                    wire("control_token", TOKEN["obj"])
                    say(f"control token seen: {TOKEN['obj']}")
                    break

        # --- illegal acceptance watch: Reclamation token on P0's BF
        if TARGET["illegal_tried"] and \
                TARGET["illegal_accepted"] is None:
            for oid, o in state["objects"].items():
                if o.get("zone") == "Battlefield" and \
                        o.get("controller") == 0 and \
                        oname(o) == RECLAM and o.get("is_token"):
                    TARGET["illegal_accepted"] = True
                    say("ILLEGAL branch ACCEPTED: Reclamation token on BF!")
                    wire("illegal_token", {"oid": oid})
                    break

        # --- finish: token observed (control) or illegal accepted
        if (TOKEN["seen"] or TARGET["illegal_accepted"]) and \
                not ST["stop"]:
            stack_empty = not any(o.get("zone") == "Stack"
                                  for o in state["objects"].values())
            if stack_empty:
                await asyncio.sleep(2)
                await export_now("post.json")
                ST["stop"] = True
                say("=== DONE ===")
        # --- target prompt seen but never answerable: give up gracefully
        if TARGET["prompt_seen"] and not TARGET["answered"] \
                and not TARGET["illegal_tried"] and not TARGET["control_tried"] \
                and not ST["stop"] and TARGET.get("seen_at") \
                and time.time() - TARGET["seen_at"] > 120:
            say("target prompt unanswerable after 120s; stopping")
            wire("target_unanswerable", {"candidates": TARGET["candidates"]})
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True
        if TARGET.get("control_at") and not TOKEN["seen"] \
                and not ST["stop"] \
                and time.time() - TARGET["control_at"] > 90:
            say("control branch: no token after 90s; exporting post anyway")
            wire("control_timeout", {})
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True

        # --- CAST stage entry: export pre.json happens inside p0_cast_step

    # ------------------------------------------------------- evaluate
    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    pre = load_state("pre.json")
    A["A1_setup_ok"] = ("passed"
                        if len(zone_oids(pre, 1, "Graveyard", BEAR)) >= 1
                        and len(zone_oids(pre, 1, "Graveyard", RECLAM)) >= 1
                        else "failed")
    cands = TARGET["candidates"] or []
    cnames = [c["name"] for c in cands]
    A["A2_creature_offered"] = ("passed" if BEAR in cnames
                                else ("failed" if TARGET["prompt_seen"]
                                      else "not-run"))
    A["A3_noncreature_excluded"] = ("passed" if TARGET["prompt_seen"]
                                    and RECLAM not in cnames
                                    else ("failed" if TARGET["prompt_seen"]
                                          and RECLAM in cnames
                                          else "not-run"))
    if RECLAM in cnames:
        A["A4_illegal_enforcement"] = (
            "passed" if TARGET["illegal_accepted"] is False
            else ("failed" if TARGET["illegal_accepted"] is True
                  else "not-run"))
    else:
        A["A4_illegal_enforcement"] = "not-run"
    tok = TOKEN.get("obj") or {}
    tok_types = tok.get("card_type") or {}
    core = tok_types.get("core_types") if isinstance(tok_types, dict) else None
    A["A5_control_token"] = ("passed" if TOKEN["seen"] and core == ["Artifact"]
                             else ("failed" if TARGET["control_tried"]
                                   and not TOKEN["seen"]
                                   else "not-run"))
    try:
        post = load_state("post.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        A["A6_cleanup"] = ("passed" if stack_empty else "failed")
    except Exception:
        A["A6_cleanup"] = "not-run"

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"candidates: {cnames}")
    obs["notes"].append(f"illegal branch: tried={TARGET['illegal_tried']} "
                        f"accepted={TARGET['illegal_accepted']}")
    obs["notes"].append(f"control token: {tok}")
    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "target_shape": TARGET["shape"],
                   "candidates": cands}, f, indent=2)

    # run.json for the renderer + record
    run_doc = {
        "issue": 6862,
        "run_id": RUN_ID,
        "started_at": "2026-09-11T17:13:00Z",
        "server": {
            "server_version": "0.80.0",
            "build_commit": "22cca6d",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": "1d414c0e999a088560ab9ad0d77a4ae4f5773610cee6afda616a62c0e654238e",
            "card_data_sha256": "7ce6f92d0adb8fc4158bf0ab76797a644eb77dcea01f9743bb849550bb677bfd",
            "draft_pools_sha256": "c78dbd16f671e5b21ec094d6fcbc2b5da76e79cc82c2d9daa914369180021348",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-09-11",
            "source": "ServerHello + minisign verification against repo-pinned key",
        },
        "driver": {"protocol_version": 69, "client": "driver/client.py",
                   "scenario": "driver/scenario_6862.py"},
        "scenario_sha256": hashlib.sha256(
            open(__file__, "rb").read()).hexdigest(),
        "decks": {
            "P0": [[ESPER, 4], [TITAN, 12], [SWAMP, 44]],
            "P1": [[BEAR, 8], [RECLAM, 8], [FOREST, 22], [PLAINS, 22]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "verdict": ("reproduced"
                    if A.get("A3_noncreature_excluded") == "failed"
                    or TARGET["illegal_accepted"] is True
                    else ("not-reproduced"
                          if A.get("A3_noncreature_excluded") == "passed"
                          and A.get("A5_control_token") == "passed"
                          else "blocked")),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_doc, f, indent=2)
    say(f"verdict: {run_doc['verdict']}")

    await p0.close()
    await p1.close()
    return obs, True


async def main():
    """Run attempts until one completes the trigger sequence (or attempts
    are exhausted)."""
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = {"assert": {}, "notes": [f"attempt {n} crash: {e!r}"]}, False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
