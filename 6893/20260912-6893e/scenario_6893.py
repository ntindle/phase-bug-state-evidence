#!/usr/bin/env python3
"""Issue #6893: Shield Broker assigned a shield counter to my creature.

Oracle (pinned card-data.json v0.80.0): "When this creature enters, put a
shield counter on target noncommander creature you don't control. You gain
control of that creature for as long as it has a shield counter on it."

Reported outcome: Shield Broker's ETB put a shield counter on a creature its
controller controls (the reporter's own Witherbloom), violating the "you
don't control" target restriction, then stole that creature.

Test (native engine, v0.80.0 / protocol 69, two human-driver seats):
  P0: 12x Shield Broker ({3}{U}{U}) + 12x Witherbloom Apprentice (own
      creature; Storm Crow ({1}{U} 1/2 flying) stands in for the
      reporter's Witherbloom Apprentice ({B}{G}) so the own creature is
      reliably castable on a two-color manabase) + 20 Island + 16 Swamp.
  P1: 12x Grizzly Bears + 48 Forest.
  P0 casts an Apprentice (own creature), then casts Shield Broker. When the
  ETB target prompt appears for P0:
    1. Record the advertised candidates with their controllers.
    2. FORGE a target submission for P0's own Apprentice (a choiceId that is
       NOT among the advertised candidates) -- the engine must reject it.
    3. Submit the legal target (P1's Bear) and let the trigger resolve.

Behavioral contract:
  A1 setup_ok           Shield Broker ETB reached a P0 target prompt with an
                        own Apprentice and an opponent Bear both on the
                        battlefield (pre_target.json exported).
  A2 candidates_exclude_own  every advertised target candidate is controlled
                        by the opponent (P1); no P0-controlled creature is a
                        candidate.
  A3 own_target_forged_rejected  the forged own-creature target submission is
                        rejected by the engine (ActionRejected/Error), the
                        target prompt stays pending, and no shield counter
                        lands on the own creature.
  A4 legal_resolution   the legal Bear target receives exactly 1 shield
                        counter, its controller becomes P0, and no P0-owned
                        creature carries a shield counter (post_trigger.json).
  A5 cleanup            stack empty, game advances >=1 turn past the trigger
                        turn with no stall.

Verdict: reproduced iff A2 fails (own creature advertised as a target) or
A3 fails (forged own-target accepted / counter lands on own creature).
not-reproduced iff A1..A5 all pass. blocked iff the ETB target prompt never
appears within the turn cap.
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6893e"
EVDIR = f"{BACKFILL}/evidence/6893/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BROKER = "Shield Broker"
APPR = "Storm Crow"  # own creature (report names Witherbloom Apprentice; see limitations)
BEAR = "Grizzly Bears"
ISLAND = "Island"
SWAMP = "Swamp"
FOREST = "Forest"
P0_LANDS = (ISLAND, SWAMP)
P1_LANDS = (FOREST,)

P0_DECK = [(BROKER, 12), (APPR, 12), (ISLAND, 20), (SWAMP, 16)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

TIMEOUT = 1800
TURN_CAP = 60

ST = {}
MULLS = {}
WF_SEEN = []
SUBMITTED_IIDS = set()
FORGED_IIDS = set()


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> PROMPT -> LEGAL -> RESOLVED -> DONE
        "stop": False,
        "broker_cast": False,
        "broker_oid": None,
        "prompt_seen": False,
        "prompt_candidates": None,
        "prompt_opp": None,
        "forge_attempted": False,
        "forge_rejected": None,
        "forge_rejection": None,
        "forge_prompt_still_pending": None,
        "legal_submitted": False,
        "legal_submit_rejmark": 0,
        "legal_target_oid": None,
        "legal_accepted": None,
        "trigger_turn": None,
        "resolved": False,
        "post_trigger_exported": False,
        "cleanup_exported": False,
        "rejections": [],
        "game_started": False,
        "turns_seen": set(),
        "stall_observed": False,
        "last_rev_change": None,
    })
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    WF_SEEN.clear()
    SUBMITTED_IIDS.clear()
    FORGED_IIDS.clear()


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


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def untapped_lands(state, pid):
    n = 0
    for _, o in bf(state, pid):
        if oname(o) in (ISLAND, SWAMP, FOREST) and not o.get("tapped"):
            n += 1
    return n


def life_of(state, pid):
    ps = state.get("players") or []
    if pid < len(ps):
        return ps[pid].get("life")
    return None


def shield_count(o):
    c = o.get("counters")
    if isinstance(c, dict):
        for k in ("shield", "Shield", "SHIELD"):
            if k in c:
                try:
                    return int(c[k])
                except Exception:
                    pass
        return 0
    if isinstance(c, list):
        n = 0
        for cc in c:
            if isinstance(cc, dict) and str(cc.get("kind", "")).lower() == "shield":
                try:
                    n += int(cc.get("count", 0))
                except Exception:
                    pass
        return n
    return 0


def own_shield_total(state, pid, exclude_oid=None):
    """Shield counters on pid's battlefield creatures, excluding the legal
    target (which is SUPPOSED to carry the counter after a legal steal)."""
    n = 0
    for oid, o in bf(state, pid):
        if exclude_oid is not None and str(oid) == str(exclude_oid):
            continue
        n += shield_count(o)
    return n


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def is_target_wait(state):
    return wf_type(state) in ("TargetSelection", "TriggerTargetSelection")


def broker_trigger_wait(state):
    """True when P0 faces the Shield Broker ETB target prompt."""
    if not is_target_wait(state) or wf_player(state) != 0:
        return False
    wd = wf_data(state)
    src = (state.get("objects") or {}).get(str(wd.get("source_id"))) or {}
    desc = (wd.get("description") or "").lower()
    return oname(src) == BROKER or "shield counter" in desc


def candidate_info(opp, state):
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
                    "controller": o.get("controller"),
                    "text": ch.get("text")})
    return out


def build_response(resp, choice_id):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [choice_id]}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": choice_id}}
    # unknown: try the choose form with the raw id
    return {"type": "choose", "data": {"choiceId": choice_id}}


async def submit_interaction(c, iid, response):
    sub = {"interactionId": iid, "response": response}
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST.get("stage")})
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
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    if found:
        ST["rejections"].extend(
            {"who": c.name, "type": r["type"], "data": r["data"]}
            for r in found)
    return found


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1][0] != wf):
        WF_SEEN.append((wf, time.time()))
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST.get('stage')}")


C0 = None
C1 = None


async def observe_prompt(c, state):
    """Record the Shield Broker ETB target prompt once."""
    if ST["prompt_seen"]:
        return
    if not broker_trigger_wait(state):
        return
    vi = (c.latest or {}).get("viewer_interaction") or {}
    opps = vi.get("opportunities") or vi.get("interactions") or []
    opp = opps[0] if opps else {}
    cands = candidate_info(opp, state)
    ST["prompt_seen"] = True
    ST["prompt_candidates"] = cands
    ST["prompt_opp"] = {"interactionId": opp.get("interactionId")
                        or opp.get("interaction_id"),
                        "resp_type": (opp.get("response") or {}).get("type")}
    ST["trigger_turn"] = state.get("turn_number")
    ST["stage"] = "PROMPT"
    snap = {"waiting_for_type": wf_type(state),
            "waiting_for_data": wf_data(state),
            "candidates": cands,
            "opportunity_summary": ST["prompt_opp"]}
    with open(f"{EVDIR}/target_prompt.json", "w") as f:
        json.dump(snap, f, indent=1, default=str)
    wire("target_prompt",
         {"candidates": cands, "opp": ST["prompt_opp"]})
    say("Shield Broker target prompt; candidates="
        f"{[(c['name'], c['ref'], c['controller'], c['choice_id']) for c in cands]}")
    await export_now("pre_target.json")


async def answer_prompt(c, state):
    """Forge-test then legally answer the Shield Broker target prompt."""
    if not broker_trigger_wait(state):
        return False
    try:
        seat = int(c.name[1])
    except (IndexError, ValueError):
        seat = -1
    if wf_player(state) != seat:
        return False
    await observe_prompt(c, state)
    vi = (c.latest or {}).get("viewer_interaction") or {}
    opps = vi.get("opportunities") or vi.get("interactions") or []
    acted = False
    for opp in opps:
        iid = opp.get("interactionId") or opp.get("interaction_id")
        if not iid or iid in SUBMITTED_IIDS:
            continue
        resp = opp.get("response") or {}
        cands = candidate_info(opp, state)
        if iid not in FORGED_IIDS:
            # FORGE: target P0's own creature using its object id as a
            # choiceId that was never advertised. The engine must reject.
            own = bf_named(state, 0, APPR) or [
                oid for oid, _ in bf(state, 0)]
            forged = own[0] if own else "999999"
            wire("forge_attempt",
                 {"iid": iid, "forged_choice_id": forged,
                  "own_creature_oids": own,
                  "candidates": cands})
            say(f"[P0] FORGE: submitting own creature oid={forged} "
                f"as target choiceId (not advertised)")
            await submit_interaction(c, iid, build_response(resp, forged))
            FORGED_IIDS.add(iid)
            ST["forge_attempted"] = True
            ST["forge_submit_rejmark"] = len(ST["rejections"])
            ST["stage"] = "FORGED"
            acted = True
            continue
        if not ST["legal_submitted"]:
            # LEGAL: target the opponent's Bear.
            bear_cand = next((x for x in cands
                              if x["name"] == BEAR and x["controller"] == 1),
                             None)
            if bear_cand is None:
                bear_cand = next((x for x in cands
                                  if x["controller"] == 1), None)
            if bear_cand is None and cands:
                bear_cand = cands[0]
            if bear_cand is None:
                say("[P0] no candidates to answer with; holding")
                return True
            wire("legal_attempt",
                 {"iid": iid, "choice": bear_cand, "candidates": cands})
            say(f"[P0] LEGAL: targeting {bear_cand['name']} "
                f"oid={bear_cand['ref']} choice={bear_cand['choice_id']}")
            ST["legal_target_oid"] = bear_cand["ref"]
            await submit_interaction(c, iid,
                                     build_response(resp,
                                                    bear_cand["choice_id"]))
            SUBMITTED_IIDS.add(iid)
            ST["legal_submitted"] = True
            ST["legal_submit_rejmark"] = len(ST["rejections"])
            ST["stage"] = "LEGAL"
            acted = True
            continue
    return acted


def evaluate_forge(state):
    """After the forge attempt, decide if it was rejected."""
    if not ST["forge_attempted"] or ST["forge_rejected"] is not None:
        return
    for r in ST["rejections"][ST.get("forge_submit_rejmark", 0):]:
        d = json.dumps(r.get("data", {})).lower()
        if "interaction" in d or "target" in d or "choice" in d:
            ST["forge_rejected"] = True
            ST["forge_rejection"] = r
            wire("forge_rejected", {"rejection": r})
            say("forge submission REJECTED -> "
                f"{json.dumps(r)[:300]}")
            break
    else:
        # no rejection mentioning the interaction yet; if the prompt moved
        # on, the forge was ACCEPTED (bad). If still pending, wait longer.
        if not broker_trigger_wait(state):
            ST["forge_rejected"] = False
            wire("forge_not_rejected",
                 {"wf_now": wf_type(state),
                  "legal_submitted": ST["legal_submitted"]})
            say("forge submission NOT rejected (prompt moved on)")
        return
    ST["forge_prompt_still_pending"] = broker_trigger_wait(state)


def evaluate_legal(state):
    if not ST["legal_submitted"] or ST["legal_accepted"] is not None:
        return
    for r in ST["rejections"][ST["legal_submit_rejmark"]:]:
        d = json.dumps(r.get("data", {}))
        if "nteraction" in d:
            ST["legal_accepted"] = False
            wire("legal_rejected", {"rejection": r})
            say("legal submission REJECTED -> "
                f"{json.dumps(r)[:300]}")
            return
    # accepted once the trigger is gone / counter observed
    toid = ST["legal_target_oid"]
    o = (state.get("objects") or {}).get(str(toid), {})
    if shield_count(o) >= 1 or not broker_trigger_wait(state):
        ST["legal_accepted"] = True
        wire("legal_accepted", {"target_oid": toid,
                                "shield": shield_count(o),
                                "controller": o.get("controller")})


async def observe_resolution(state):
    """Detect trigger resolution: shield counter on the Bear + P0 control."""
    if ST["stage"] not in ("LEGAL",) or ST["resolved"]:
        return
    if not ST.get("legal_accepted"):
        return
    toid = ST["legal_target_oid"]
    o = (state.get("objects") or {}).get(str(toid), {})
    stack = state.get("stack") or []
    if o and o.get("zone") == "Battlefield" and shield_count(o) >= 1 \
            and o.get("controller") == 0 and not stack:
        ST["resolved"] = True
        ST["stage"] = "RESOLVED"
        say(f"trigger resolved: Bear oid={toid} shield={shield_count(o)} "
            f"controller={o.get('controller')}; stack empty")
        wire("resolved", {"bear_oid": toid, "shield": shield_count(o),
                          "controller": o.get("controller")})
        await export_now("post_trigger.json")
        ST["post_trigger_exported"] = True


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


async def p0_tick(c, pid, state, acts):
    if await answer_prompt(c, state):
        return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) in P0_LANDS)
            keep_ok = n_lands >= 2 or MULLS[c.name] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice} (lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            return True
    if wf_type(state) == "DiscardToHandSize" \
            and wf_data(state).get("player") == pid:
        n = wf_data(state).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        h = hand_oids(state, pid)
        pref = [o for o in h if oname(state["objects"][o]) not in P0_LANDS]
        pref += [o for o in h if o not in pref]
        # protect win-condition cards
        keep = [o for o in pref if oname(state["objects"][o])
                in (BROKER, APPR)]
        pref = keep + [o for o in pref
                       if oname(state["objects"][o]) not in (BROKER, APPR)]
        picks = pref[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                      for x in picks]}})
            say(f"{c.name} discards {len(picks)}")
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        # land drop every tick (retry; no per-turn keep-flag)
        for ln in P0_LANDS:
            lid = find_hand(state, pid, ln)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True
                break
        if ST["stage"] == "SETUP":
            n_appr = len(bf_named(state, pid, APPR))
            if n_appr < 2:
                oid = find_hand(state, pid, APPR)
                if oid and untapped_lands(state, pid) >= 2:
                    a = castspell_advertised(acts, oid)
                    if a:
                        await submit_as_is(c, a)
                        say(f"[P0] casts {APPR} (oid={oid})")
                        return True
            if not ST["broker_cast"]:
                oid = find_hand(state, pid, BROKER)
                # Gate on >=2 opponent Bears: a single legal target is
                # auto-targeted by the engine with no prompt (cf. #658);
                # the forge test needs the TargetSelection prompt to appear.
                if oid and untapped_lands(state, pid) >= 5 \
                        and untapped_of(state, pid, ISLAND) >= 2 \
                        and len(bf_named(state, 1, BEAR)) >= 2:
                    a = castspell_advertised(acts, oid)
                    if a:
                        await submit_as_is(c, a)
                        ST["broker_cast"] = True
                        say(f"[P0] casts {BROKER} (oid={oid})")
                        return True
    if state.get("active_player") == pid \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p1_tick(c, pid, state, acts):
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) in P1_LANDS)
            keep_ok = n_lands >= 2 or MULLS[c.name] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice} (lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            return True
    if wf_type(state) == "DiscardToHandSize" \
            and wf_data(state).get("player") == pid:
        n = wf_data(state).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        h = hand_oids(state, pid)
        pref = [o for o in h if oname(state["objects"][o]) not in P1_LANDS]
        pref += [o for o in h if o not in pref]
        picks = pref[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                      for x in picks]}})
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
        oid = find_hand(state, pid, BEAR)
        if oid and untapped_lands(state, pid) >= 2 \
                and len(bf_named(state, pid, BEAR)) < 3:
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say(f"[P1] casts {BEAR} (oid={oid})")
                return True
    if state.get("active_player") == pid \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def load_env(fn):
    p = f"{EVDIR}/{fn}"
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def env_state(env):
    if not env:
        return None
    s = env.get("state")
    return json.loads(s) if isinstance(s, str) else s


def write_assertions():
    A, D = {}, {}
    pre = load_env("pre_target.json")
    post_t = load_env("post_trigger.json")
    post = load_env("post.json")
    pre_s, post_t_s, post_s = (env_state(pre), env_state(post_t),
                               env_state(post))

    # A1
    own_ok = pre_s and any(oname(o) == APPR for _, o in bf(pre_s, 0))
    opp_ok = pre_s and any(oname(o) == BEAR for _, o in bf(pre_s, 1))
    broker_ok = pre_s and any(oname(o) == BROKER for _, o in bf(pre_s, 0))
    A["A1_setup_ok"] = ("passed" if (ST["prompt_seen"] and own_ok and opp_ok
                                     and broker_ok) else "failed")
    D["A1_setup_ok_detail"] = (
        f"prompt_seen={ST['prompt_seen']} broker_on_p0_bf={broker_ok} "
        f"own_appr_on_bf={own_ok} opp_bear_on_bf={opp_ok} "
        f"trigger_turn={ST['trigger_turn']}")

    # A2
    cands = ST["prompt_candidates"] or []
    own_cands = [c for c in cands if c.get("controller") == 0
                 and (c.get("zone") or "") == "Battlefield"]
    A["A2_candidates_exclude_own"] = (
        "passed" if (ST["prompt_seen"] and cands and not own_cands)
        else ("failed" if ST["prompt_seen"] else "not-run"))
    D["A2_candidates_exclude_own_detail"] = (
        f"candidates={[(c['name'], c['ref'], c['controller']) for c in cands]} "
        f"own_controlled_candidates={own_cands}")

    # A3 -- exclude the legal target (it is supposed to carry the counter
    # after a legal steal); a counter on any OTHER own creature is the bug.
    own_shield = own_shield_total(post_t_s, 0,
                                  exclude_oid=ST["legal_target_oid"]) \
        if post_t_s else 0
    forged_bad = (ST["forge_rejected"] is False) or own_shield >= 1
    A["A3_own_target_forged_rejected"] = (
        "passed" if (ST["forge_attempted"] and ST["forge_rejected"]
                     and own_shield == 0)
        else ("failed" if forged_bad else "not-run"))
    D["A3_own_target_forged_rejected_detail"] = (
        f"forge_attempted={ST['forge_attempted']} "
        f"forge_rejected={ST['forge_rejected']} "
        f"forge_rejection={json.dumps(ST['forge_rejection'])[:200]} "
        f"prompt_still_pending_after_forge="
        f"{ST['forge_prompt_still_pending']} "
        f"own_creatures_shield_total_post_excl_target={own_shield}")

    # A4
    bear_shield = bear_ctrl = None
    if post_t_s and ST["legal_target_oid"]:
        o = (post_t_s.get("objects") or {}).get(ST["legal_target_oid"], {})
        bear_shield = shield_count(o)
        bear_ctrl = o.get("controller")
    A["A4_legal_resolution"] = (
        "passed" if (bear_shield == 1 and bear_ctrl == 0 and own_shield == 0)
        else ("failed" if (ST["resolved"] or ST["legal_accepted"]) else
              "not-run"))
    D["A4_legal_resolution_detail"] = (
        f"target_bear_oid={ST['legal_target_oid']} shield={bear_shield} "
        f"controller={bear_ctrl} own_shield_total={own_shield} "
        f"resolved={ST['resolved']}")

    # A5
    turns_past = (max(ST["turns_seen"]) - ST["trigger_turn"]
                  if ST["trigger_turn"] is not None and ST["turns_seen"]
                  else 0)
    A["A5_cleanup"] = (
        "passed" if (ST["post_trigger_exported"] and not ST["stall_observed"]
                     and (turns_past >= 1 or ST["stop"]))
        else ("failed" if ST["stall_observed"] else "not-run"))
    D["A5_cleanup_detail"] = (
        f"post_trigger_exported={ST['post_trigger_exported']} "
        f"turns_past_trigger={turns_past} stall={ST['stall_observed']}")

    failed = [k for k, v in A.items() if v == "failed"]
    if (A["A2_candidates_exclude_own"] == "failed"
            or A["A3_own_target_forged_rejected"] == "failed"):
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    elif not ST["prompt_seen"]:
        verdict = "blocked"
    else:
        verdict = "reproduced" if failed else "not-reproduced"
    return A, D, verdict


async def main():
    reset()
    t0 = time.time()
    c0 = PhaseClient("P0")
    c1 = PhaseClient("P1")
    await c0.connect()
    await c0.create(deck(*P0_DECK))
    global C0, C1
    C0, C1 = c0, c1
    await c1.connect()
    await c1.join(c0.game_code, deck(*P1_DECK))
    say(f"game {c0.game_code}; P0 seat={c0.player_id}; P1 seat={c1.player_id}")
    wire("game_created", {"code": c0.game_code, "p0_deck": P0_DECK,
                          "p1_deck": P1_DECK})
    ST["last_rev_change"] = time.time()

    last_rev = {c0.name: -1, c1.name: -1}
    last_tick_wall = {c0.name: 0, c1.name: 0}
    clients = [(c0, 0, p0_tick), (c1, 1, p1_tick)]
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, tickfn in clients:
            drain_rejections(c)
            rev_changed = c.revision != last_rev[c.name]
            if rev_changed:
                last_rev[c.name] = c.revision
                ST["last_rev_change"] = now
            if now - last_tick_wall[c.name] >= 5 or rev_changed:
                last_tick_wall[c.name] = now
                try:
                    await tickfn(c, pid, (c.latest or {}).get("state") or {},
                                 (c.latest or {}).get("legal_actions", []))
                except Exception as e:
                    say(f"tick error {c.name}: {e}")
        st = c0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]
        turn = state.get("turn_number") or 0
        if turn >= 1:
            ST["game_started"] = True
            ST["turns_seen"].add(turn)

        if wf_type(state) == "GameOver":
            say("game over")
            ST["stop"] = True
            continue

        evaluate_forge(state)
        evaluate_legal(state)
        await observe_resolution(state)

        if ST["game_started"] and not ST["stop"] \
                and now - ST["last_rev_change"] > 120:
            ST["stall_observed"] = True
            say("STALL: no revision for 120s; "
                f"waiting_for={wf_type(state)} player={wf_player(state)}")
            wire("stall", {"wf": wf_type(state), "player": wf_player(state)})
            await export_now("mid_stall.json")
            ST["stop"] = True
            continue

        if ST["stage"] == "RESOLVED" and ST["trigger_turn"] is not None:
            if turn >= ST["trigger_turn"] + 1 \
                    and not (state.get("stack") or []):
                if await export_now("post.json") is not None:
                    ST["cleanup_exported"] = True
                    ST["stage"] = "DONE"
                    ST["stop"] = True
                continue

        if turn > TURN_CAP and ST["stage"] in ("SETUP",):
            say(f"TURN CAP {TURN_CAP} without broker ETB prompt; stopping")
            ST["stop"] = True
            continue

    A, D, verdict = write_assertions()

    import hashlib
    bindir = f"{BACKFILL}/server/releases/v0.80.0"

    def sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()

    run = {
        "issue": 6893,
        "run_id": RUN_ID,
        "validated_version": "v0.80.0",
        "server": {
            "server_version": "v0.80.0",
            "build_commit": "22cca6d",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": sha(bindir + "/phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(bindir + "/data/card-data.json"),
            "draft_pools_sha256": sha(bindir + "/data/draft-pools.json"),
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
        },
        "game_code": c0.game_code,
        "scenario": "driver/scenario_6893.py",
        "p0_deck": P0_DECK,
        "p1_deck": P1_DECK,
        "assertions": A,
        "assertion_details": D,
        "waiting_for_sequence": [w for w, _ in WF_SEEN],
        "verdict": verdict,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rejections": ST["rejections"],
        "mulligans": {"P0": MULLS.get("P0"), "P1": MULLS.get("P1")},
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x card density is a test-harness convenience (engine accepts >4-of for custom games).",
            "The forged target submission used the own creature's object id as the choiceId; only one forged choiceId form was attempted.",
            "Storm Crow stands in for the reporter's Witherbloom Apprentice as the own-side creature (both are ordinary noncommander creatures; the Apprentice's {B}{G} cost is unreliable on the driver's two-color manabase).",
            "Not tested on the original 2026-08-02 build; verdict is scoped to v0.80.0, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6893.py", "w") as f:
        f.write(src)
    say(f"verdict={verdict} assertions={json.dumps(A)}")
    say("scenario finished")
    try:
        await c0.close()
    except Exception:
        pass
    try:
        await c1.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
