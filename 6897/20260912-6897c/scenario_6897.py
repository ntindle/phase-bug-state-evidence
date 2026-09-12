#!/usr/bin/env python3
"""Issue #6897: Lockjaw, Slobbering Teleporter - reflexive target prompt loops.

Reported (Discord, status:confirmed, area:engine+frontend, p0-softlock):
"Constantly prompting the user to select up to one nonland permanent, and
then when the choice is made ... it loops through priority and asks again."

Triage clarified the card is Lockjaw, Slobbering Teleporter (the issue
ingestion misidentified Mister Fantastic). Oracle (verified from pinned
card-data.json):

  Vigilance
  At the beginning of combat on your turn, if you've cast a noncreature
  spell this turn, put a +1/+1 counter on Lockjaw. When you do, Lockjaw
  and up to one other target creature you control can't be blocked
  this turn.

The parser marks the reflexive trigger supported (PutCounter on SelfRef,
then a sub-ability granting CantBeBlocked to SelfRef and to ParentTarget
with multi_target {min:0, max:1} "up to one other target creature you
control"). The failure is the supported optional-target continuation not
completing: after the choice is made, priority cycles and the same
selection prompt returns.

Behavioral contract (native engine v0.81.1 / protocol 70, two human seats):
  Game A (choose one): P0 controls Lockjaw + Storm Crow, casts Divination
  (noncreature) in PreCombatMain, passes to combat. Beginning-of-combat
  trigger puts a +1/+1 counter on Lockjaw; the reflexive "when you do"
  prompts for up to one other target creature you control. Driver answers
  with the Crow. Expected: the prompt appears exactly once, the choice
  completes, Lockjaw and the Crow can't be blocked this turn, priority
  advances without reopening the prompt.
  Game B (choose zero): same setup, driver answers the prompt with an
  empty selection. Expected: completes with no loop.

  A1 setup_ok        Lockjaw + Crow on P0 BF, Divination in hand pre-cast,
                     PreCombatMain, life 20/20
  A2 counter_placed  Lockjaw carries a +1/+1 counter after the trigger
  A3 prompt_once     reflexive target prompt occurred exactly once
                     (no reopen, same or new interaction id)
  A4 choice_completes after answering, the reflexive trigger resolves and
                     the game reaches DeclareAttackers with no second prompt
  A5 unblockable     Lockjaw and the chosen Crow carry CantBeBlocked for
                     the turn (state markers and/or DeclareBlockers offer)
  A6 cleanup         stack empty, game proceeds past combat, no stall
  B0 setup/counter   same setup and counter assertions (zero-choice game)
  B1 prompt_once     prompt occurred exactly once
  B2 zero_completes  empty answer accepted; trigger resolves, Lockjaw
                     unblockable, no loop
  B3 cleanup         stack empty, game proceeds

Verdict: reproduced iff A3 or B1 fails (the reported prompt loop).
not-reproduced iff every assertion passes. blocked iff setup never ran.
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
RUN_ID = "20260912-6897c"
EVDIR = f"{BACKFILL}/evidence/6897/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

LOCKJAW = "Lockjaw, Slobbering Teleporter"
CROW = "Storm Crow"
DIVINATION = "Divination"
ISLAND = "Island"
P0_DECK = [(LOCKJAW, 4), (CROW, 8), (DIVINATION, 12), (ISLAND, 36)]
P1_DECK = [(CROW, 12), (ISLAND, 48)]
TIMEOUT = 1500
LOOP_CAP = 3  # max reflexive answers before declaring the loop reproduced

ST = {}
SUBMITTED_IIDS = set()
RESULTS = {}


def reset_game(mode):
    ST.clear()
    ST.update({
        "mode": mode,          # "one" or "zero"
        "prefix": "a" if mode == "one" else "b",
        "stage": "SETUP",      # SETUP -> TRIGGER -> ATTACK -> BLOCK -> DONE
        "stop": False,
        "turn_cap": 40,
        "divination_cast": False,
        "pre_exported": False,
        "lockjaw_oid": None,
        "crow_oid": None,      # chosen other creature (game A)
        "prompt_count": 0,     # distinct reflexive TargetSelection opps
        "prompt_iids": [],
        "reopen_count": 0,     # same-iid TargetSelection seen again
        "answers": 0,
        "loop": False,
        "post_trigger_exported": False,
        "attack_declared": False,
        "block_action_logged": False,
        "block_shape": None,
        "post_exported": False,
        "p1_life_pre_attack": None,
        "rejections": [],
        "game_code": None,
    })
    SUBMITTED_IIDS.clear()


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


def untapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == ISLAND and not o.get("tapped")]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_player(state):
    return ((state.get("waiting_for") or {}).get("data") or {}).get("player")


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
                           "stage": ST.get("stage"), "mode": ST.get("mode")})
    await c.send_action(action)
    # settle guard: don't submit again for this client until a newer state
    # revision is seen (kills the back-to-back-cast-on-stale-state race that
    # produced a rejected Lockjaw cast in the first 6897 attempt). A
    # rejection clears the guard (drain_rejections) so we can retry.
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
                              "stage": ST.get("stage"), "mode": ST.get("mode")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    if found:
        # a rejection means no newer state is coming for the in-flight
        # submit; clear the settle guard so the client can re-sync + retry
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
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": (ch.get("text") or "")[:120]})
    return out


def is_reflexive_prompt(opp, state):
    """Lockjaw's reflexive 'up to one other target creature you control'
    prompt. The engine surfaces it as waiting_for type
    "TriggerTargetSelection" (protocol 70; legacy "TargetSelection" also
    accepted) whose description carries the CantBeBlocked grant. Fingerprint
    on the description plus at least one P0-battlefield creature candidate
    so unrelated target prompts never match."""
    wf = state.get("waiting_for") or {}
    desc = (wf.get("data") or {}).get("description", "")
    if "be blocked" not in desc:
        return False
    cands = candidate_info(opp, state)
    if not cands:
        return False
    return any(x["controller"] == 0 and x["zone"] == "Battlefield"
               for x in cands)


PROMPT_SHAPE_LOGGED = set()


async def answer_reflexive(c, state):
    """Count and answer the reflexive up-to-one target prompt. Returns True
    if a reflexive prompt was pending (whether answered or held).

    Observed 2026-09-12: the prompt surfaces as waiting_for type
    "TriggerTargetSelection" (not "TargetSelection") with a
    viewer_interaction opportunity carrying the legal creature candidates;
    the target slot is optional (min 0)."""
    if wf_type(state) not in ("TargetSelection", "TriggerTargetSelection") \
            or wf_player(state) != 0:
        return False
    opps = current_opps(c)
    if not any(is_reflexive_prompt(o, state) for o in opps):
        wire("reflexive_wf_no_matching_opp",
             {"mode": ST.get("mode"), "wf_type": wf_type(state),
              "n_opps": len(opps),
              "opp_shapes": [
                  {"iid": o.get("interactionId"),
                   "rtype": (o.get("response") or {}).get("type"),
                   "n_cands": len(candidate_info(o, state))}
                  for o in opps]})
    for opp in opps:
        iid = opp.get("interactionId")
        resp = opp.get("response") or {}
        if not is_reflexive_prompt(opp, state):
            continue
        if iid in SUBMITTED_IIDS:
            ST["reopen_count"] += 1
            wire("prompt_reopen", {"iid": iid,
                                   "reopen_count": ST["reopen_count"],
                                   "stage": ST.get("stage"),
                                   "mode": ST.get("mode")})
            say(f"[{ST['mode']}] reflexive prompt REOPENED "
                f"(iid {iid}, reopen #{ST['reopen_count']})")
            return True  # hold; already answered this one
        ST["prompt_count"] += 1
        ST["prompt_iids"].append(iid)
        cands = candidate_info(opp, state)
        rtype = resp.get("type")
        spec = ((resp.get("data") or {}).get("spec")) or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if iid not in PROMPT_SHAPE_LOGGED:
            PROMPT_SHAPE_LOGGED.add(iid)
            wire("reflexive_prompt", {"iid": iid, "rtype": rtype,
                                      "spec_type": spec_type,
                                      "spec": spec,
                                      "candidates": cands,
                                      "mode": ST["mode"]})
            say(f"[{ST['mode']}] reflexive prompt #{ST['prompt_count']}: "
                f"rtype={rtype} spec={spec_type} "
                f"cands={[(x['name'], x['ref']) for x in cands]}")
        # choose the Crow (game A) or nothing (game B)
        want = None
        if ST["mode"] == "one":
            want = next((x for x in cands if x["name"] == CROW), None) \
                or next((x for x in cands
                         if x["name"] != LOCKJAW), None)
        else:
            # zero mode: prefer an explicit decline/done candidate
            want = next((x for x in cands
                         if (x["text"] or "").lower().strip() in
                         ("done", "no more targets", "decline", "none",
                          "finish", "pass", "skip")
                         or "decline" in (x["text"] or "").lower()), None)
        sub = None
        if rtype == "schema" and spec_type in ("sequence", "select"):
            ids = [want["choice_id"]] if want else []
            sub = {"type": spec_type, "data": {"choiceIds": ids}}
        elif rtype == "exactChoices":
            if want:
                sub = {"type": "choose",
                       "data": {"choiceId": want["choice_id"]}}
            else:
                # look for an explicit decline/none choice
                decl = next((x for x in cands
                             if "decline" in (x["text"] or "").lower()
                             or (x["text"] or "").lower() in
                             ("none", "no target", "no targets")), None)
                if decl:
                    sub = {"type": "choose",
                           "data": {"choiceId": decl["choice_id"]}}
        if sub is None:
            wire("prompt_unanswerable_shape",
                 {"iid": iid, "rtype": rtype, "spec_type": spec_type,
                  "mode": ST["mode"]})
            say(f"[{ST['mode']}] prompt shape not answerable; holding")
            return True
        if ST["answers"] >= LOOP_CAP:
            wire("loop_cap_reached", {"prompt_count": ST["prompt_count"],
                                      "mode": ST["mode"]})
            return True
        wire("interaction_submit",
             {"who": c.name, "iid": iid, "response": sub,
              "want": (want or {}).get("ref"),
              "stage": ST.get("stage"), "mode": ST.get("mode")})
        await c.send_interaction({"interactionId": iid, "response": sub})
        SUBMITTED_IIDS.add(iid)
        ST.setdefault("settle", {})[c.name] = c.revision
        ST["answers"] += 1
        if ST["mode"] == "one" and want:
            ST["crow_oid"] = want["ref"]
        what = ("Crow " + str(want["ref"])) if ST["mode"] == "one" and want \
            else (("DECLINE:" + str((want or {}).get("text")))
                  if want else "EMPTY")
        say(f"[{ST['mode']}] answered reflexive prompt #{ST['prompt_count']} "
            f"with {what} (answer #{ST['answers']})")
        if ST["prompt_count"] >= LOOP_CAP:
            ST["loop"] = True
            say(f"[{ST['mode']}] LOOP CAP REACHED "
                f"({ST['prompt_count']} prompts)")
        return True
    return False


def reflexive_trigger_on_stack(state):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if "can't be blocked" in blob and LOCKJAW in blob:
            return True
    return False


def counter_trigger_on_stack(state):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if "+1/+1 counter" in blob and LOCKJAW in blob:
            return True
    return False


C0 = None
C1 = None


async def p0_tick(c, pid, state, acts):
    # reflexive prompt has absolute priority
    if await answer_reflexive(c, state):
        return True
    if not is_my_main(state, pid):
        return False
    px = ST["prefix"]
    lj_bf = bf_named(state, pid, LOCKJAW)
    crow_bf = bf_named(state, pid, CROW)
    untapped = untapped_lands(state, pid)
    if ST["stage"] == "SETUP":
        # 1. land drop first
        lid = find_hand(state, pid, ISLAND)
        la = next((x for x in acts if x["type"] == "PlayLand"
                   and str(x.get("data", {}).get("object_id")) == lid),
                  None) if lid else None
        # 2. cast Crow (need 1 crow on BF)
        if not crow_bf and len(untapped) >= 2:
            oid = find_hand(state, pid, CROW)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say(f"[{ST['mode']}] P0 casts Storm Crow")
                return True
        # 3. cast Lockjaw (need {1}{U}: 2 untapped islands)
        if not lj_bf and len(untapped) >= 2:
            oid = find_hand(state, pid, LOCKJAW)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say(f"[{ST['mode']}] P0 casts Lockjaw")
                return True
        # 4. both out + Divination in hand + 3 untapped -> export pre, cast
        if lj_bf and crow_bf and not ST["divination_cast"]:
            ST["lockjaw_oid"] = lj_bf[0]
            if len(untapped) >= 3:
                oid = find_hand(state, pid, DIVINATION)
                a = castspell_advertised(acts, oid)
                if a:
                    if not ST["pre_exported"]:
                        await export_now(f"{px}_pre.json")
                        ST["pre_exported"] = True
                    await submit_as_is(c, a)
                    ST["divination_cast"] = True
                    ST["stage"] = "TRIGGER"
                    say(f"[{ST['mode']}] P0 casts Divination; stage -> TRIGGER")
                    return True
        if la:
            await submit_as_is(c, la)
            return True
        return False
    return False


async def p1_tick(c, pid, state, acts):
    if await answer_reflexive(c, state):
        return True
    if not is_my_main(state, pid):
        return False
    # land drop
    lid = find_hand(state, pid, ISLAND)
    la = next((x for x in acts if x["type"] == "PlayLand"
               and str(x.get("data", {}).get("object_id")) == lid),
              None) if lid else None
    # cast Crow blockers (2 untapped islands); never attack with them
    if len(untapped_lands(state, pid)) >= 2:
        oid = find_hand(state, pid, CROW)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say(f"[{ST['mode']}] P1 casts Storm Crow (blocker)")
            return True
    if la:
        await submit_as_is(c, la)
        return True
    return False


async def declare_attackers(c, pid, state, acts, attackers):
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = [
                [int(oid), {"type": "Player", "data": 1}] for oid in attackers]
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            say(f"[{ST['mode']}] P0 attacks with {attackers}")
            return True
    return False


async def inspect_blockers(c, pid, state, acts):
    """P1: log the advertised DeclareBlockers action, scan it for any
    assignment touching Lockjaw / the chosen Crow, then declare empty."""
    for a in acts:
        if a["type"] == "DeclareBlockers":
            if not ST["block_action_logged"]:
                ST["block_action_logged"] = True
                blob = json.dumps(a, default=str)
                ST["block_shape"] = {
                    "keys": sorted(a.get("data", {}).keys()),
                    "mentions_lockjaw": str(ST["lockjaw_oid"]) in blob,
                    "mentions_crow": str(ST.get("crow_oid")) in blob,
                    "len": len(blob),
                }
                wire("declare_blockers_action",
                     {"data_keys": ST["block_shape"]["keys"],
                      "mentions_lockjaw": ST["block_shape"][
                          "mentions_lockjaw"],
                      "mentions_crow": ST["block_shape"]["mentions_crow"],
                      "mode": ST["mode"]})
                say(f"[{ST['mode']}] DeclareBlockers: keys="
                    f"{ST['block_shape']['keys']} "
                    f"mentions_lockjaw={ST['block_shape']['mentions_lockjaw']} "
                    f"mentions_crow={ST['block_shape']['mentions_crow']}")
                with open(f"{EVDIR}/{ST['prefix']}_declare_blockers.json",
                          "w") as f:
                    json.dump(a, f, indent=1, default=str)
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            say(f"[{ST['mode']}] P1 declares no blockers")
            return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    drain_rejections(c)
    if settle_pending(c):
        # a submit is in flight; wait for its state update before acting
        # again (prevents double-acting on a stale view)
        return False
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        n = max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                     for x in picks]}})
            return True
    px = ST["prefix"]

    # loop evidence: cap reached -> export and stop this game
    if ST["loop"] and not ST["post_exported"]:
        await export_now(f"{px}_post_loop.json")
        ST["post_exported"] = True
        ST["stage"] = "DONE"
        ST["stop"] = True
        say(f"[{ST['mode']}] post-loop state exported; stopping game")
        return True

    # TRIGGER stage: watch for resolution -> DeclareAttackers
    if ST["stage"] == "TRIGGER" and pid == 0:
        if not (state.get("stack") or []) and \
                (state.get("phase") or "") == "DeclareAttackers" and \
                state.get("active_player") == 0:
            if not ST["post_trigger_exported"]:
                await export_now(f"{px}_post_trigger.json")
                ST["post_trigger_exported"] = True
                ST["stage"] = "ATTACK"
                say(f"[{ST['mode']}] trigger resolved, at DeclareAttackers; "
                    f"stage -> ATTACK")
            return True

    # ATTACK stage: P0 declares attackers
    if ST["stage"] == "ATTACK" and pid == 0 \
            and state.get("active_player") == 0 \
            and (state.get("phase") or "") == "DeclareAttackers":
        lj = bf_named(state, 0, LOCKJAW)
        crows = bf_named(state, 0, CROW)
        attackers = lj + crows[:1]
        if attackers and not ST["attack_declared"]:
            if not ST["p1_life_pre_attack"]:
                ST["p1_life_pre_attack"] = life(state, 1)
            if await declare_attackers(c, pid, state, acts, attackers):
                ST["attack_declared"] = True
                ST["stage"] = "BLOCK"
                return True

    # BLOCK stage: P1 inspects + declares empty; then watch for post
    if ST["stage"] == "BLOCK" and pid == 1 \
            and (state.get("phase") or "") == "DeclareBlockers":
        if await inspect_blockers(c, pid, state, acts):
            return True
    if ST["stage"] == "BLOCK" and pid == 0 \
            and not (state.get("stack") or []) \
            and (state.get("phase") or "") in ("PostCombatMain", "End"):
        if not ST["post_exported"]:
            await export_now(f"{px}_post.json")
            ST["post_exported"] = True
            ST["stage"] = "DONE"
            ST["stop"] = True
            say(f"[{ST['mode']}] post.json exported; stopping game")
        return True

    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
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
        wire("target_held", {"who": c.name, "stage": ST.get("stage"),
                             "mode": ST.get("mode"),
                             "wf_type": wf_type(state)})
        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def run_game(mode):
    reset_game(mode)
    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    ST["game_code"] = C0.game_code
    say(f"[{mode}] game {C0.game_code}; seats {C0.player_id}/{C1.player_id}")
    wire("game_start", {"game_code": C0.game_code, "mode": mode,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    t0 = time.time()
    last_progress = t0
    timeout = 750
    turn = 0
    while time.time() - t0 < timeout and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        st0 = C0.latest
        if st0:
            turn = st0.get("state", {}).get("turn_number") or 0
            if turn > ST["turn_cap"]:
                say(f"[{mode}] turn cap reached; stopping")
                wire("turn_cap", {"mode": mode})
                break
        if acted0 or acted1:
            last_progress = time.time()
        if turn > 15 and ST["stage"] == "SETUP" and not ST.get("stall_logged"):
            ST["stall_logged"] = True
            st = C0.latest or {}
            sstate = st.get("state") or {}
            hand = [(oname(sstate.get("objects", {}).get(oid, {})),
                     oid) for oid in hand_oids(sstate, 0)]
            say(f"[{mode}] SETUP STALL turn {turn}: P0 hand={hand} "
                f"bf={[oname(o) for _, o in bf(sstate, 0)]}")
            wire("setup_stall", {"mode": mode, "turn": turn, "hand": hand})
        if time.time() - last_progress > 240:
            say(f"[{mode}] no progress for 240s; dumping stall info")
            for c in (C0, C1):
                st = c.latest
                if st:
                    wire("stall_state",
                         {"who": c.name, "mode": mode,
                          "wf": st.get("state", {}).get("waiting_for"),
                          "phase": st.get("state", {}).get("phase"),
                          "acts": [a.get("type")
                                   for a in st.get("legal_actions", [])],
                          "stage": ST["stage"]})
            break
        await asyncio.sleep(0.15)

    res = {k: ST[k] for k in ("mode", "prefix", "game_code", "stage",
                              "prompt_count", "prompt_iids", "reopen_count",
                              "answers", "loop", "lockjaw_oid", "crow_oid",
                              "pre_exported", "post_trigger_exported",
                              "attack_declared", "block_action_logged",
                              "block_shape", "post_exported",
                              "p1_life_pre_attack", "rejections")}
    wire("game_end", res)
    await C0.close()
    await C1.close()
    C0 = C1 = None
    return res


def load_env(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())
    except Exception as e:
        return {"_err": str(e)[:120]}


def env_state(env):
    if not env or "_err" in env:
        return None
    s = env.get("state")
    return s if isinstance(s, dict) else json.loads(s)


def has_p1p1_counter(state, oid):
    o = (state.get("objects") or {}).get(str(oid), {})
    blob = json.dumps(o.get("counters"), default=str)
    return "P1P1" in blob or "+1/+1" in blob


def has_cantbeblocked(state, oid):
    o = (state.get("objects") or {}).get(str(oid), {})
    blob = json.dumps(o, default=str)
    return "CantBeBlocked" in blob


async def main():
    results = {}
    for mode in ("one", "zero"):
        results[mode] = await run_game(mode)

    # ---- assertions ----
    A, D = {}, {}

    def ev(prefix, name):
        return load_env(f"{prefix}_{name}.json")

    for mode, px, keys in (
            ("one", "a", ("A1_setup_ok", "A2_counter_placed",
                           "A3_prompt_once", "A4_choice_completes",
                           "A5_unblockable", "A6_cleanup")),
            ("zero", "b", ("B0_setup_ok", "B0_counter_placed",
                            "B1_prompt_once", "B2_zero_completes",
                            "B2b_unblockable", "B3_cleanup"))):
        r = results[mode]
        pre_s = env_state(ev(px, "pre"))
        post_t_s = env_state(ev(px, "post_trigger"))
        post_s = env_state(ev(px, "post"))
        k_setup, k_counter, k_prompt, k_complete, k_unblock, k_cleanup = keys

        # setup
        lj_bf = len(bf_named(pre_s, 0, LOCKJAW)) > 0 if pre_s else False
        crow_bf = len(bf_named(pre_s, 0, CROW)) > 0 if pre_s else False
        div_hand = find_hand(pre_s, 0, DIVINATION) is not None if pre_s \
            else False
        l0 = life(pre_s, 0) if pre_s else None
        l1 = life(pre_s, 1) if pre_s else None
        ok = lj_bf and crow_bf and div_hand and l0 == 20 and l1 == 20
        A[k_setup] = "passed" if ok else "failed"
        D[k_setup + "_detail"] = (
            f"lockjaw_bf={lj_bf} crow_bf={crow_bf} "
            f"div_in_hand={div_hand} life=({l0},{l1})")

        # counter placed
        lj_oid = r["lockjaw_oid"]
        c_ok = has_p1p1_counter(post_t_s, lj_oid) if post_t_s else False
        A[k_counter] = "passed" if c_ok else (
            "not-run" if not post_t_s else "failed")
        D[k_counter + "_detail"] = (
            f"lockjaw_oid={lj_oid} p1p1={c_ok} "
            f"post_trigger={'yes' if post_t_s else 'no'}")

        # prompt exactly once
        pc = r["prompt_count"]
        A[k_prompt] = "passed" if pc == 1 else "failed"
        D[k_prompt + "_detail"] = (
            f"prompt_count={pc} reopens={r['reopen_count']} "
            f"answers={r['answers']} loop_flag={r['loop']}")

        # choice completes / trigger resolves
        trig_ok = post_t_s is not None and not r["loop"] \
            and r["post_trigger_exported"]
        A[k_complete] = "passed" if trig_ok else (
            "failed" if r["loop"] or pc == 0 else "not-run")
        D[k_complete + "_detail"] = (
            f"post_trigger_exported={r['post_trigger_exported']} "
            f"attack_declared={r['attack_declared']}")

        # unblockable markers
        marks = []
        if post_t_s:
            oids = [lj_oid] + ([r["crow_oid"]] if r["crow_oid"] else [])
            marks = [(oid, has_cantbeblocked(post_t_s, oid))
                     for oid in oids if oid]
        bs = r["block_shape"] or {}
        offer_clean = (bs.get("mentions_lockjaw") is False) and (
            bs.get("mentions_crow") is False)
        want_n = 2 if mode == "one" else 1
        both = len(marks) == want_n and all(m[1] for m in marks)
        A[k_unblock] = "passed" if (both or offer_clean) else (
            "failed" if post_t_s else "not-run")
        D[k_unblock + "_detail"] = (
            f"markers={marks} block_offer_clean={offer_clean} "
            f"block_keys={bs.get('keys')}")

        # cleanup
        stack_empty = not (post_s.get("stack") or []) if post_s else False
        A[k_cleanup] = "passed" if (
            stack_empty and r["post_exported"]) else (
            "not-run" if not post_s else "failed")
        D[k_cleanup + "_detail"] = (
            f"post_exported={r['post_exported']} stack_empty={stack_empty} "
            f"final_stage={r['stage']}")

    say("ASSERTIONS: " + json.dumps(A, indent=1))

    if A["A1_setup_ok"] == "failed" or A["B0_setup_ok"] == "failed":
        verdict = "blocked"
    elif A["A3_prompt_once"] == "failed" or A["B1_prompt_once"] == "failed":
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    assertions = {"assertions": A, "details": D, "verdict": verdict,
                  "games": results}
    json.dump(assertions, open(f"{EVDIR}/assertions.json", "w"), indent=1)
    say("VERDICT: " + verdict)
    wire("final", {"verdict": verdict})
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
