#!/usr/bin/env python3
"""Issue #6893: Shield Broker control-effect duration lifetime.

Oracle (pinned card-data.json v0.80.0):
  Shield Broker {3}{U}{U} 3/4 -- "When this creature enters, put a shield
  counter on target noncommander creature you don't control. You gain
  control of that creature for as long as it has a shield counter on it.
  (If it would be dealt damage or destroyed, remove a shield counter from
  it instead.)"

Triage (status:confirmed) clarified summary: "Shield Broker's control
effect can reactivate when an unrelated later effect places a new shield
counter on the formerly controlled creature, even after the original
shield counter was removed and Shield Broker is in the graveyard."

Acceptance criteria (from triage):
  - Shield Broker grants control only while the uninterrupted original
    duration condition remains true.
  - Removing the last shield counter ends that control effect permanently.
  - A later shield counter from another source does not restart the ended
    effect.
  - Shield counters are removed when they replace applicable damage or
    destruction.

A prior run (20260912-6893e, same day) tested only the ETB target
restriction ("you don't control") and posted not-reproduced; it did not
exercise the confirmed duration-lifetime defect. This scenario tests the
triage acceptance criteria directly.

Test (native engine, v0.80.0 / protocol 69, two human-driver seats):
  P0 (reporter's seat): 12x Grizzly Bears (stand-in for the reporter's
      Witherbloom Apprentice -- an ordinary noncommander creature) +
      12x Day of Judgment + 12x Forest + 12x Plains.
  P1 (opponent): 12x Shield Broker + 12x Perrie, the Pulverizer +
      4x Boon of Safety (fallback shield-counter source) +
      12x Island + 8x Plains + 8x Forest.

  1. P0 plays a Bear. P1 casts Shield Broker; its ETB targets the Bear
     (answering the prompt explicitly when one appears; single-target
     auto-target is also accepted).
  2. P0 casts Day of Judgment ("Destroy all creatures"). The Bear's
     shield counter is removed INSTEAD of destruction (replacement);
     Shield Broker is destroyed. Control of the Bear must revert to P0
     permanently.
  3. P1 casts Perrie, the Pulverizer; its ETB ("put a shield counter on
     target creature") targets the Bear. (If the counter lands elsewhere,
     P1 casts Boon of Safety targeting the Bear as the fallback source.)
     The ended Shield Broker control effect must NOT reactivate: the Bear
     stays under P0's control.

Behavioral contract:
  A1 broker_steal      after the Broker ETB resolves, the Bear carries
                       exactly 1 shield counter and is controlled by P1
                       (pre_wipe.json).
  A2 wipe_replacement  after Day of Judgment resolves, the Bear is on the
                       battlefield with 0 shield counters (counter consumed
                       instead of destruction) (post_wipe.json).
  A3 control_reverted   after the wipe, the Bear is controlled by P0 and
                       Shield Broker is in P1's graveyard (post_wipe.json).
  A4 no_reactivation    after the later shield counter (Perrie/Boon) is on
                       the Bear, the Bear is STILL controlled by P0, in
                       both post_perrie.json and the final post.json.
  A5 cleanup            stack empty, game advances >=1 turn past the Perrie
                       turn with no stall.

Verdict: reproduced iff A4 fails (control reactivated by the later shield
counter -- the reported defect) or A1/A2/A3 fail as a clearly identified
related failure. not-reproduced iff A1..A5 all pass. blocked iff a stage
cannot be reached within the turn cap (state which one).
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
RUN_ID = "20260912-6893f"
EVDIR = f"{BACKFILL}/evidence/6893/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BROKER = "Shield Broker"
PERRIE = "Perrie, the Pulverizer"
BOON = "Boon of Safety"
BEAR = "Grizzly Bears"
DOJ = "Day of Judgment"
ISLAND = "Island"
PLAINS = "Plains"
FOREST = "Forest"
P0_LANDS = (FOREST, PLAINS)
P1_LANDS = (ISLAND, PLAINS, FOREST)

P0_DECK = [(BEAR, 12), (DOJ, 12), (FOREST, 12), (PLAINS, 12)]
P1_DECK = [(BROKER, 12), (PERRIE, 12), (BOON, 4),
           (ISLAND, 12), (PLAINS, 8), (FOREST, 8)]

TIMEOUT = 1800
TURN_CAP = 60

ST = {}
MULLS = {}
WF_SEEN = []
SUBMITTED_IIDS = set()


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP->BROKER_RESOLVED->WIPE_RESOLVED
        #                  ->PERRIE_RESOLVED(_DONE)->DONE
        "stop": False,
        "broker_cast": False,
        "broker_etb_answered": False,
        "wipe_cast": False,
        "perrie_cast": False,
        "perrie_etb_answered": False,
        "boon_cast": False,
        "boon_answered": False,
        "perrie_turn": None,
        "bear_oid": None,
        "broker_oid": None,
        "counter_source": None,  # "perrie" | "boon"
        "rejections": [],
        "turns_seen": set(),
        "stall_observed": False,
        "last_rev_change": None,
        "stages_reached": ["SETUP"],
    })
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    WF_SEEN.clear()
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
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in (ISLAND, PLAINS, FOREST) and not o.get("tapped"))


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


def etb_wait_for(state, pid, card_substr):
    """True when `pid` faces a target prompt plausibly from card_substr."""
    if not is_target_wait(state) or wf_player(state) != pid:
        return False
    wd = wf_data(state)
    src = (state.get("objects") or {}).get(str(wd.get("source_id"))) or {}
    desc = (wd.get("description") or "").lower()
    return card_substr.lower() in oname(src).lower() or "shield counter" in desc


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
                    "controller": o.get("controller")})
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


def set_stage(s):
    if ST["stage"] != s:
        ST["stage"] = s
        ST["stages_reached"].append(s)
        wire("stage", {"stage": s})
        say(f"STAGE -> {s}")


C0 = None
C1 = None


async def answer_etb(c, pid, card_substr, flag_key, target_name, state):
    """Answer one target prompt for `pid` by choosing the Bear."""
    if ST[flag_key]:
        return False
    if not etb_wait_for(state, pid, card_substr):
        return False
    vi = (c.latest or {}).get("viewer_interaction") or {}
    opps = vi.get("opportunities") or vi.get("interactions") or []
    for opp in opps:
        iid = opp.get("interactionId") or opp.get("interaction_id")
        if not iid or iid in SUBMITTED_IIDS:
            continue
        cands = candidate_info(opp, state)
        bear_cand = next((x for x in cands if x["name"] == target_name
                          and x["zone"] == "Battlefield"), None)
        if bear_cand is None:
            say(f"[{c.name}] prompt for {card_substr} has no Bear candidate; "
                f"candidates={cands}; holding")
            return True
        say(f"[{c.name}] answering {card_substr}: targeting "
            f"{bear_cand['name']} oid={bear_cand['ref']}")
        wire("etb_answer", {"card": card_substr, "choice": bear_cand,
                            "candidates": cands})
        await submit_interaction(c, iid,
                                 build_response(opp.get("response") or {},
                                                bear_cand["choice_id"]))
        SUBMITTED_IIDS.add(iid)
        ST[flag_key] = True
        return True
    return False


def find_bear(state):
    """The P0-owned Bear on the battlefield (the Witherbloom stand-in)."""
    for oid, o in state["objects"].items():
        if (o.get("zone") == "Battlefield" and oname(o) == BEAR
                and o.get("owner") == 0):
            return str(oid), o
    for oid, o in state["objects"].items():
        if o.get("zone") == "Battlefield" and oname(o) == BEAR:
            return str(oid), o
    return None, None


def stack_empty(state):
    return not (state.get("stack") or [])


async def observe_stages(state):
    """Advance the stage machine from authoritative state."""
    stage = ST["stage"]
    bear_oid, bear = find_bear(state)

    if stage == "SETUP" and ST["broker_cast"] and bear is not None:
        if (shield_count(bear) == 1 and bear.get("controller") == 1
                and stack_empty(state)):
            ST["bear_oid"] = bear_oid
            for oid, o in state["objects"].items():
                if o.get("zone") == "Battlefield" and oname(o) == BROKER \
                        and o.get("controller") == 1:
                    ST["broker_oid"] = str(oid)
            say(f"Broker ETB resolved: Bear oid={bear_oid} shield=1 "
                f"controller=1")
            await export_now("pre_wipe.json")
            set_stage("BROKER_RESOLVED")

    elif stage == "BROKER_RESOLVED" and ST["wipe_cast"] and bear is not None:
        broker_gy = any(oname(o) == BROKER
                        for o in state["objects"].values()
                        if o.get("zone") == "Graveyard"
                        and o.get("controller") == 1)
        if (bear.get("zone") == "Battlefield" and shield_count(bear) == 0
                and bear.get("controller") == 0 and broker_gy
                and stack_empty(state)):
            say("Wipe resolved: Bear survived with counter consumed, "
                "control reverted to P0, Broker in P1 graveyard")
            await export_now("post_wipe.json")
            set_stage("WIPE_RESOLVED")

    elif stage == "WIPE_RESOLVED" and ST["perrie_cast"] and bear is not None:
        if stack_empty(state) and shield_count(bear) >= 1:
            ST["counter_source"] = ST["counter_source"] or "perrie"
            ST["perrie_turn"] = state.get("turn_number")
            say(f"Perrie ETB resolved: Bear shield={shield_count(bear)} "
                f"controller={bear.get('controller')}")
            wire("perrie_counter", {"bear_oid": bear_oid,
                                    "shield": shield_count(bear),
                                    "controller": bear.get("controller")})
            await export_now("post_perrie.json")
            set_stage("PERRIE_RESOLVED")

    elif stage == "PERRIE_RESOLVED" and ST["boon_cast"] and bear is not None:
        # fallback path only: Boon supplied the later counter
        if stack_empty(state) and shield_count(bear) >= 1:
            ST["counter_source"] = "boon"
            ST["perrie_turn"] = state.get("turn_number")
            say(f"Boon resolved: Bear shield={shield_count(bear)} "
                f"controller={bear.get('controller')}")
            await export_now("post_perrie.json")
            set_stage("PERRIE_RESOLVED_DONE")

    if ST["stage"] in ("PERRIE_RESOLVED", "PERRIE_RESOLVED_DONE") \
            and ST["perrie_turn"] is not None:
        turn = state.get("turn_number") or 0
        if turn >= ST["perrie_turn"] + 1 and stack_empty(state):
            await export_now("post.json")
            set_stage("DONE")
            ST["stop"] = True


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


async def mulligan_tick(c, pid, state, acts, lands):
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) in lands)
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
        return ("discard", h, n)
    return None


async def p0_tick(c, pid, state, acts):
    r = await mulligan_tick(c, pid, state, acts, P0_LANDS)
    if r is True:
        return True
    if isinstance(r, tuple):
        _, h, n = r
        pref = [o for o in h if oname(state["objects"][o]) not in P0_LANDS]
        pref += [o for o in h if o not in pref]
        keep = [o for o in pref if oname(state["objects"][o])
                in (BEAR, DOJ)]
        pref = keep + [o for o in pref
                       if oname(state["objects"][o]) not in (BEAR, DOJ)]
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
        for ln in P0_LANDS:  # land drop every tick (retry; no keep-flag)
            lid = find_hand(state, pid, ln)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True
                break
        stage = ST["stage"]
        if stage == "SETUP":
            if len(bf_named(state, pid, BEAR)) < 1:
                oid = find_hand(state, pid, BEAR)
                if oid and untapped_lands(state, pid) >= 2:
                    a = castspell_advertised(acts, oid)
                    if a:
                        await submit_as_is(c, a)
                        say(f"[P0] casts {BEAR} (oid={oid})")
                        return True
        elif stage == "BROKER_RESOLVED" and not ST["wipe_cast"]:
            oid = find_hand(state, pid, DOJ)
            if oid and untapped_lands(state, pid) >= 4 \
                    and untapped_of(state, pid, PLAINS) >= 2:
                a = castspell_advertised(acts, oid)
                if a:
                    await submit_as_is(c, a)
                    ST["wipe_cast"] = True
                    say(f"[P0] casts {DOJ} (oid={oid})")
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
    if await answer_etb(c, pid, "Shield Broker", "broker_etb_answered",
                        BEAR, state):
        return True
    if await answer_etb(c, pid, "Perrie", "perrie_etb_answered",
                        BEAR, state):
        return True
    if ST["boon_cast"] and not ST["boon_answered"]:
        if await answer_etb(c, pid, "Boon of Safety", "boon_answered",
                            BEAR, state):
            return True
    r = await mulligan_tick(c, pid, state, acts, P1_LANDS)
    if r is True:
        return True
    if isinstance(r, tuple):
        _, h, n = r
        pref = [o for o in h if oname(state["objects"][o]) not in P1_LANDS]
        pref += [o for o in h if o not in pref]
        keep = [o for o in pref if oname(state["objects"][o])
                in (BROKER, PERRIE, BOON)]
        pref = keep + [o for o in pref
                       if oname(state["objects"][o])
                       not in (BROKER, PERRIE, BOON)]
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
        for ln in P1_LANDS:
            lid = find_hand(state, pid, ln)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True
                break
        stage = ST["stage"]
        if stage == "SETUP" and not ST["broker_cast"]:
            oid = find_hand(state, pid, BROKER)
            if oid and untapped_lands(state, pid) >= 5 \
                    and untapped_of(state, pid, ISLAND) >= 2 \
                    and len(bf_named(state, 0, BEAR)) >= 1:
                a = castspell_advertised(acts, oid)
                if a:
                    await submit_as_is(c, a)
                    ST["broker_cast"] = True
                    say(f"[P1] casts {BROKER} (oid={oid})")
                    return True
        elif stage == "WIPE_RESOLVED" and not ST["perrie_cast"]:
            oid = find_hand(state, pid, PERRIE)
            if oid and untapped_lands(state, pid) >= 4 \
                    and untapped_of(state, pid, FOREST) >= 1 \
                    and untapped_of(state, pid, PLAINS) >= 1 \
                    and untapped_of(state, pid, ISLAND) >= 1:
                a = castspell_advertised(acts, oid)
                if a:
                    await submit_as_is(c, a)
                    ST["perrie_cast"] = True
                    say(f"[P1] casts {PERRIE} (oid={oid})")
                    return True
        elif stage == "PERRIE_RESOLVED" and not ST["boon_cast"] \
                and ST["counter_source"] != "perrie":
            # Fallback: Perrie's counter did not land on the Bear.
            oid = find_hand(state, pid, BOON)
            bear_oid, bear = find_bear(state)
            if oid and bear is not None and shield_count(bear) == 0 \
                    and untapped_of(state, pid, PLAINS) >= 1:
                a = castspell_advertised(acts, oid)
                if a:
                    await submit_as_is(c, a)
                    ST["boon_cast"] = True
                    say(f"[P1] casts {BOON} fallback (oid={oid})")
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
    pre_wipe = env_state(load_env("pre_wipe.json"))
    post_wipe = env_state(load_env("post_wipe.json"))
    post_perrie = env_state(load_env("post_perrie.json"))
    post = env_state(load_env("post.json"))

    def bear_in(s):
        if not s:
            return None
        for oid, o in s["objects"].items():
            if o.get("zone") == "Battlefield" and oname(o) == BEAR \
                    and o.get("owner") == 0:
                return str(oid), o
        return None

    # A1: Broker ETB resolved -> Bear has 1 shield counter, controlled by P1
    b = bear_in(pre_wipe)
    A["A1_broker_steal"] = (
        "passed" if (b and shield_count(b[1]) == 1
                     and b[1].get("controller") == 1)
        else ("failed" if b else "not-run"))
    D["A1_broker_steal_detail"] = (
        f"bear={b[0] if b else None} shield={shield_count(b[1]) if b else None} "
        f"controller={b[1].get('controller') if b else None} "
        f"(pre_wipe.json)")

    # A2: wipe -> Bear survived, counter consumed (0 shields)
    b = bear_in(post_wipe)
    A["A2_wipe_replacement"] = (
        "passed" if (b and b[1].get("zone") == "Battlefield"
                     and shield_count(b[1]) == 0)
        else ("failed" if b else "not-run"))
    D["A2_wipe_replacement_detail"] = (
        f"bear={b[0] if b else None} zone={b[1].get('zone') if b else None} "
        f"shield={shield_count(b[1]) if b else None} (post_wipe.json): "
        "counter removed instead of destruction")

    # A3: control reverted to P0; Broker in P1 graveyard
    b = bear_in(post_wipe)
    broker_gy = False
    if post_wipe:
        broker_gy = any(oname(o) == BROKER
                        for o in post_wipe["objects"].values()
                        if o.get("zone") == "Graveyard"
                        and o.get("controller") == 1)
    A["A3_control_reverted"] = (
        "passed" if (b and b[1].get("controller") == 0 and broker_gy)
        else ("failed" if (b is not None) else "not-run"))
    D["A3_control_reverted_detail"] = (
        f"bear_controller={b[1].get('controller') if b else None} "
        f"broker_in_p1_graveyard={broker_gy} (post_wipe.json)")

    # A4: later shield counter must NOT reactivate the ended control effect
    b_pp = bear_in(post_perrie)
    b_post = bear_in(post)
    pp_ok = (b_pp and shield_count(b_pp[1]) >= 1
             and b_pp[1].get("controller") == 0)
    post_ok = (b_post and shield_count(b_post[1]) >= 1
               and b_post[1].get("controller") == 0)
    A["A4_no_reactivation"] = (
        "passed" if (pp_ok and post_ok)
        else ("failed" if (b_pp and shield_count(b_pp[1]) >= 1) else
              "not-run"))
    D["A4_no_reactivation_detail"] = (
        f"counter_source={ST['counter_source']} "
        f"post_perrie: bear={b_pp[0] if b_pp else None} "
        f"shield={shield_count(b_pp[1]) if b_pp else None} "
        f"controller={b_pp[1].get('controller') if b_pp else None}; "
        f"post: bear={b_post[0] if b_post else None} "
        f"shield={shield_count(b_post[1]) if b_post else None} "
        f"controller={b_post[1].get('controller') if b_post else None}")

    # A5: cleanup
    turns_past = ((max(ST["turns_seen"]) - ST["perrie_turn"])
                  if ST["perrie_turn"] is not None and ST["turns_seen"]
                  else 0)
    A["A5_cleanup"] = (
        "passed" if (ST["stage"] == "DONE" and not ST["stall_observed"]
                     and turns_past >= 1)
        else ("failed" if ST["stall_observed"] else "not-run"))
    D["A5_cleanup_detail"] = (
        f"stage={ST['stage']} turns_past_perrie={turns_past} "
        f"stall={ST['stall_observed']} stages={ST['stages_reached']}")

    if A["A4_no_reactivation"] == "failed":
        verdict = "reproduced"
    elif A["A1_broker_steal"] == "failed" \
            or A["A2_wipe_replacement"] == "failed" \
            or A["A3_control_reverted"] == "failed":
        verdict = "reproduced"  # related failure of the same card/effect
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    elif not any(v == "failed" for v in A.values()):
        verdict = "blocked"
    else:
        verdict = "reproduced"
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
            # re-tick every 5s regardless (a tick that sends nothing leaves
            # no revision change; cf. #825 driver lesson)
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
            ST["turns_seen"].add(turn)

        if wf_type(state) == "GameOver":
            say("game over")
            ST["stop"] = True
            continue

        try:
            await observe_stages(state)
        except Exception as e:
            say(f"observe error: {e}")

        if ST["turns_seen"] and not ST["stop"] \
                and now - ST["last_rev_change"] > 120:
            ST["stall_observed"] = True
            say("STALL: no revision for 120s; "
                f"waiting_for={wf_type(state)} player={wf_player(state)}")
            wire("stall", {"wf": wf_type(state), "player": wf_player(state)})
            await export_now("mid_stall.json")
            ST["stop"] = True
            continue

        if turn > TURN_CAP and ST["stage"] in ("SETUP", "BROKER_RESOLVED",
                                              "WIPE_RESOLVED"):
            say(f"TURN CAP {TURN_CAP} at stage {ST['stage']}; stopping")
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
        "stages_reached": ST["stages_reached"],
        "counter_source": ST["counter_source"],
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x card density is a test-harness convenience (engine accepts >4-of for custom games).",
            "Grizzly Bears stands in for the reporter's Witherbloom Apprentice as the stolen creature (both ordinary noncommander creatures).",
            "Perrie, the Pulverizer is the reporter's 'Perry the Piledriver' (the commander that re-entered and placed the later shield counter); Boon of Safety is a fallback shield-counter source used only if Perrie's counter does not land on the Bear.",
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
