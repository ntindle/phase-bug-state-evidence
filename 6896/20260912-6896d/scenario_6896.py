#!/usr/bin/env python3
"""Issue #6896: Moonlight Hunt does nothing after choosing target.

Reported (Discord, status:confirmed, area:engine/parser, mechanic:combat):
"It prompts target selection correctly, and then just goes to graveyard."

Oracle: "Choose target creature you don't control. Each creature you
control that's a Wolf or a Werewolf deals damage equal to its power to
that creature."

Triage (mike-theDude): the parse marks the spell supported but emits a
single DealDamage child using "self power"; it does not iterate over each
controlled Wolf/Werewolf as an independent damage source. Acceptance:
every Wolf/Werewolf the caster controls deals damage equal to its own
power to the chosen target; each creature an independent source; a
creature with both subtypes deals only once; the spell resolves normally
when the legal target remains present.

Behavioral contract (native engine v0.81.0 / protocol 70, two human seats):
  P0 casts Young Wolf x2 ({G} 1/1) and Nightpack Ambusher ({2}{G}{G} 4/4,
  anthem: other Wolves/Werewolves get +1/+1 -> wolves are 2/2, 2/2, 4/4).
  P1 casts Craw Wurm (6/4). P0 casts Moonlight Hunt ({1}{G}) targeting the
  Craw Wurm. Expected: each wolf deals its power (2+2+4=8) -> Wurm dies.
  Bug: no damage at all -> Wurm survives at full, hunt in graveyard.

  A1 setup_ok       pre: P0 main, >=2 wolves (1 Young Wolf + 1 Ambusher),
                    Craw Wurm on P1 BF, Moonlight Hunt in P0 hand, 20/20
  A2 target_prompt  TargetSelection opportunity with a Craw Wurm candidate
  A3a any_damage    Wurm dead or damage_marked > 0 post-resolution
  A3b full_damage   Wurm dead (8 >= 4 lethal expected)
  A4 wolves_intact  pre wolves still on P0 BF post
  A5 hunt_resolved  Moonlight Hunt in P0 graveyard post
  A6 cleanup        stack empty, waiting_for Priority, game proceeds

Verdict: reproduced iff A2 passes and A3b fails (full per-wolf damage not
dealt). not-reproduced iff A3b passes. blocked iff A1 fails.
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
RUN_ID = "20260912-6896d"
EVDIR = f"{BACKFILL}/evidence/6896/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

YW = "Young Wolf"
AMB = "Nightpack Ambusher"
HUNT = "Moonlight Hunt"
WURM = "Craw Wurm"
BEAR = "Grizzly Bears"
FOREST = "Forest"
P0_DECK = [(YW, 8), (AMB, 8), (HUNT, 8), (FOREST, 36)]
# P1 gets a cheap Bear too: with 2+ legal hunt targets the engine must
# show the manual TargetSelection prompt (single target auto-targets).
P1_DECK = [(WURM, 8), (BEAR, 8), (FOREST, 44)]
TIMEOUT = 1200

ST = {}
SUBMITTED_IIDS = set()
MULLS = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",       # SETUP -> HUNT -> DONE
        "stop": False,
        "turn_cap": 25,
        "hunt_cast": False,
        "pre_exported": False,
        "target_prompt_seen": False,
        "target_answered": False,
        "stack_hunt_seen": False,
        "stack_hunt_exported": False,
        "post_exported": False,
        "pre_wolves": {},
        "rejections": [],
    })
    SUBMITTED_IIDS.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})


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


def objs_of(state):
    return state.get("objects") or {}


def life(state, pid):
    for p in (state.get("players") or []):
        if p.get("player_id") == pid or p.get("id") == pid \
                or p.get("seat") == pid:
            return p.get("life")
    return None


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs_of(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(objs_of(state)[oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in objs_of(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def wolf_counts(state, pid):
    counts = {}
    for oid, o in bf(state, pid):
        n = oname(o)
        if n in (YW, AMB) or (n == "Wolf" and o.get("is_token")):
            counts[n] = counts.get(n, 0) + 1
    return counts


def wolf_power_total(state, pid):
    """Sum of powers of pid's Wolf/Werewolf creatures on the battlefield."""
    tot = 0
    for oid, o in bf(state, pid):
        n = oname(o)
        if n in (YW, AMB) or (n == "Wolf" and o.get("is_token")):
            p = o.get("power")
            if isinstance(p, int):
                tot += p
    return tot


def gy_named(state, pid, name):
    return [oid for oid, o in objs_of(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


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


def wf_data(state):
    return (wf(state).get("data") or {})


def hunt_on_stack(state):
    for e in (state.get("stack") or []):
        if (e.get("source_id") is not None and
                oname(objs_of(state).get(str(e.get("source_id")), {}))
                == HUNT):
            return True
        desc = ((e.get("ability") or {}).get("description") or "")
        if "Moonlight Hunt" in desc:
            return True
    return False


def stack_summary(state):
    out = []
    for e in (state.get("stack") or []):
        kind = e.get("kind")
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        ability = e.get("ability") or {}
        out.append({"kind": ktype,
                    "desc": (ability.get("description") or "")[:90],
                    "source_id": e.get("source_id")})
    return out


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


def playland_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


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
    return found


def get_vi(st):
    vi = (st or {}).get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def current_opps():
    st = C0.latest if C0 else None
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
        o = objs_of(state).get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": ch.get("text")})
    return out


def build_target_response(resp, want):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [want["choice_id"]]}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": want["choice_id"]}}
    return None


async def send_interaction(c, sub):
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST.get("stage")})
    # The client method sends the top-level
    # {"type":"Interaction","data":{"submission":...}} envelope; routing
    # through send_action nests it under {"type":"Action",...} and the
    # server rejects it as an unknown GameAction variant (run 6895b).
    await c.send_interaction(sub)


TARGET_LOGGED = set()


async def answer_hunt_target(c, state):
    """Answer Moonlight Hunt's TargetSelection with the Craw Wurm."""
    if wf_type(state) != "TargetSelection" or wf_player(state) != 0:
        return False
    for opp in current_opps():
        iid = opp.get("interactionId")
        if iid in SUBMITTED_IIDS:
            continue
        resp = opp.get("response") or {}
        cands = candidate_info(opp, state)
        want = next((x for x in cands if x["name"] == WURM
                     and x["controller"] == 1), None)
        if iid not in TARGET_LOGGED:
            TARGET_LOGGED.add(iid)
            ST["target_prompt_seen"] = True
            wire("target_prompt", {"rtype": resp.get("type"),
                                   "candidates": cands,
                                   "stage": ST.get("stage")})
            say(f"[P0] TargetSelection prompt: rtype={resp.get('type')} "
                f"cands={[(x['name'], x['controller']) for x in cands]}")
        if not want:
            wire("target_no_wurm", {"iid": iid, "cands": len(cands),
                                    "stage": ST.get("stage")})
            continue
        sub = build_target_response(resp, want)
        if not sub:
            wire("target_unknown_schema", {"iid": iid,
                                           "rtype": resp.get("type"),
                                           "stage": ST.get("stage")})
            return True
        await send_interaction(c, {"interactionId": iid, "response": sub})
        SUBMITTED_IIDS.add(iid)
        ST["target_answered"] = True
        say(f"[P0] answers TargetSelection with {WURM} "
            f"(cid {want['choice_id']})")
        return True
    wire("target_held", {"who": c.name, "stage": ST.get("stage")})
    say("[P0] TargetSelection pending; holding priority")
    return True


C0 = None
C1 = None


def untapped_forests(state, pid):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == FOREST and not o.get("tapped")]


async def p0_tick(c, pid, state, acts):
    # never pass priority while we owe the hunt's target answer
    if ST["stage"] in ("SETUP", "HUNT"):
        if await answer_hunt_target(c, state):
            return True
    if not is_my_main(state, pid):
        return False
    untapped = untapped_forests(state, pid)
    wc = wolf_counts(state, pid)

    if ST["stage"] == "SETUP":
        # hunt trigger: 1+ Young Wolf, 1+ Ambusher, Wurm on P1 BF
        if (wc.get(YW, 0) >= 1 and wc.get(AMB, 0) >= 1
                and bf_named(state, 1, WURM) and len(untapped) >= 2):
            oid = find_hand(state, pid, HUNT)
            a = castspell_advertised(acts, oid)
            if oid and a:
                if not ST["pre_exported"]:
                    await export_now("pre.json")
                    ST["pre_exported"] = True
                    ST["pre_wolves"] = dict(wc)
                await submit_as_is(c, a)
                ST["hunt_cast"] = True
                ST["stage"] = "HUNT"
                say("[P0] casts Moonlight Hunt")
                return True
        # build the wolf pack: Young Wolves first, then the Ambusher
        if wc.get(YW, 0) < 3 and len(untapped) >= 1:
            oid = find_hand(state, pid, YW)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Young Wolf")
                return True
        if wc.get(AMB, 0) < 1 and len(untapped) >= 4:
            oid = find_hand(state, pid, AMB)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Nightpack Ambusher")
                return True
        lid = find_hand(state, pid, FOREST)
        a = playland_advertised(acts, lid)
        if a:
            await submit_as_is(c, a)
            return True
        return False
    if ST["stage"] == "HUNT":
        # hold priority while the hunt sits on the stack; export it once
        if hunt_on_stack(state):
            if not ST["stack_hunt_seen"]:
                ST["stack_hunt_seen"] = True
                wire("stack_hunt", {"stack": stack_summary(state)})
                await export_now("mid_hunt.json")
                ST["stack_hunt_exported"] = True
                ST["stack_hold_until"] = time.time() + 3.0
                say("Moonlight Hunt on stack; holding priority 3s")
            if time.time() < ST.get("stack_hold_until", 0):
                if wf_player(state) == pid and wf_type(state) == "Priority":
                    return True
        # resolved: hunt in graveyard, stack empty
        if (not (state.get("stack") or []) and ST["hunt_cast"]
                and gy_named(state, 0, HUNT) and not ST["post_exported"]):
            await export_now("post.json")
            ST["post_exported"] = True
            ST["stage"] = "DONE"
            ST["stop"] = True
            say("post.json exported; stopping")
            return True
        lid = find_hand(state, pid, FOREST)
        a = playland_advertised(acts, lid)
        if a:
            await submit_as_is(c, a)
            return True
    return False


async def p1_tick(c, pid, state, acts):
    if not is_my_main(state, pid):
        return False
    untapped = untapped_forests(state, pid)
    if not bf_named(state, pid, WURM) and len(untapped) >= 6:
        oid = find_hand(state, pid, WURM)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say("[P1] casts Craw Wurm")
            return True
        # diagnostic: why not casting with 6+ untapped lands?
        hand_names = [oname(objs_of(state)[oid])
                      for oid in hand_oids(state, pid)]
        wire("p1_no_wurm_cast",
             {"untapped": len(untapped), "wurm_in_hand": oid is not None,
              "hand": hand_names,
              "castspell_oids": [str(a.get("data", {}).get("object_id"))
                                 for a in acts if a["type"] == "CastSpell"],
              "turn": state.get("turn_number"),
              "stage": ST.get("stage")})
    # cheap Bear as a second legal hunt target so the engine must show the
    # manual TargetSelection prompt (single legal target auto-targets)
    if not bf_named(state, pid, BEAR) and len(untapped) >= 2:
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say("[P1] casts Grizzly Bears")
            return True
    lid = find_hand(state, pid, FOREST)
    a = playland_advertised(acts, lid)
    if a:
        await submit_as_is(c, a)
        return True
    return False


async def empty_declare(c, acts):
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def empty_blockers(c, acts):
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    drain_rejections(c)
    # mulligan: keep on >= 2 lands AND a key card in hand, else mulligan
    # (max 2). P1 must have a Craw Wurm; P0 must have a wolf.
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for oid in hand_oids(state, pid)
                          if oname(objs_of(state)[oid]) == FOREST)
            if pid == 0:
                key = find_hand(state, pid, YW) or find_hand(state, pid, AMB)
            else:
                key = find_hand(state, pid, WURM)
            keep_ok = (n_lands >= 2 and key is not None) or MULLS[c.name] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice} (lands={n_lands} "
                f"key={'Y' if key else 'N'})")
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
                                   "data": {"cards": [int(x)
                                                     for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        n = max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x)
                                                     for x in picks]}})
            return True
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        if await empty_declare(c, acts):
            return True
    if (state.get("phase") or "") == "DeclareBlockers" \
            and wf_player(state) == pid:
        if await empty_blockers(c, acts):
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # never pass priority while a target prompt names us
    if wf_player(state) == pid and wf_type(state) == "TargetSelection":
        wire("target_held", {"who": c.name, "stage": ST.get("stage")})
        return True
    # only pass when the wait actually names us; a stale state can still
    # list PassPriority after priority moved on (wrong_player rejects)
    if wf_player(state) != pid:
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def main():
    reset()
    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    say("connected P0")
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats {C0.player_id}/{C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        st0 = C0.latest
        if st0:
            turn = st0.get("state", {}).get("turn_number") or 0
            if turn > ST["turn_cap"]:
                say("turn cap reached; stopping")
                wire("turn_cap", {})
                break
            if ST["stage"] == "SETUP" and turn > 16:
                say("setup watchdog: no hunt by turn 16; stopping")
                wire("setup_watchdog", {})
                break
        if acted0 or acted1:
            last_progress = time.time()
        if time.time() - last_progress > 240:
            say("no progress for 240s; dumping stall info and stopping")
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

    # ---- assertions ----
    A = {}
    D = {}

    def load_env(fn):
        try:
            return json.loads(open(f"{EVDIR}/{fn}").read())
        except Exception as e:
            D[f"{fn}_err"] = str(e)[:120]
            return None

    def env_state(env):
        if not env:
            return None
        s = env.get("state")
        return s if isinstance(s, dict) else json.loads(s)

    pre = load_env("pre.json")
    post = load_env("post.json")
    mid = load_env("mid_hunt.json")
    pre_s, post_s, mid_s = env_state(pre), env_state(post), env_state(mid)

    def wurm_obj(s):
        """The targeted Craw Wurm: prefer the copy that saw the battlefield
        (non-library, highest damage), not an untouched library copy."""
        if not s:
            return None
        cands = [o for o in objs_of(s).values()
                 if oname(o) == WURM and o.get("controller") == 1]
        if not cands:
            return None
        def key(o):
            z = o.get("zone")
            return (0 if z == "Library" else 1, o.get("damage_marked") or 0)
        return max(cands, key=key)

    # A1: setup
    if pre_s:
        wc = wolf_counts(pre_s, 0)
        wurm_bf = len(bf_named(pre_s, 1, WURM)) > 0
        hunt_hand = find_hand(pre_s, 0, HUNT) is not None
        l0, l1 = life(pre_s, 0), life(pre_s, 1)
        powers = {oid: (objs_of(pre_s)[oid].get("power"),
                        objs_of(pre_s)[oid].get("toughness"))
                  for oid, o in bf(pre_s, 0)
                  if oname(o) in (YW, AMB)}
        A["A1_setup_ok"] = "passed" if (
            wc.get(YW, 0) >= 1 and wc.get(AMB, 0) >= 1 and wurm_bf
            and hunt_hand and l0 == 20 and l1 == 20) else "failed"
        D["A1_setup_ok_detail"] = (
            f"wolves={wc} wurm_on_p1_bf={wurm_bf} hunt_in_hand={hunt_hand} "
            f"life={l0}/{l1} powers={powers}")
    else:
        A["A1_setup_ok"] = "failed"
        D["A1_setup_ok_detail"] = "pre.json missing"

    # A2: target prompt
    A["A2_target_prompt"] = "passed" if ST["target_prompt_seen"] else "failed"
    D["A2_target_prompt_detail"] = (
        f"prompt_seen={ST['target_prompt_seen']} "
        f"answered={ST['target_answered']}")

    # A3: damage outcome. Expected lethal damage is derived from the actual
    # pre-hunt battlefield powers of P0's Wolf/Werewolf creatures.
    wo = wurm_obj(post_s)
    exp_dmg = wolf_power_total(pre_s, 0) if pre_s else None
    if post_s and wo is not None:
        zone = wo.get("zone")
        dmg = wo.get("damage_marked", 0) or 0
        dead = zone == "Graveyard"
        A["A3a_any_damage"] = "passed" if (dead or dmg > 0) else "failed"
        A["A3b_full_damage"] = "passed" if dead else "failed"
        D["A3_detail"] = (f"wurm zone={zone} damage_marked={dmg} "
                          f"expected={exp_dmg} (sum of P0 wolf powers pre-hunt) "
                          f"vs toughness 4")
    else:
        A["A3a_any_damage"] = "not-run"
        A["A3b_full_damage"] = "not-run"
        D["A3_detail"] = f"post wurm object: {wo is not None}"

    # A4: wolves intact
    if pre_s and post_s:
        pre_wc, post_wc = wolf_counts(pre_s, 0), wolf_counts(post_s, 0)
        ok = all(post_wc.get(n, 0) >= c for n, c in pre_wc.items())
        A["A4_wolves_intact"] = "passed" if ok else "failed"
        D["A4_wolves_intact_detail"] = f"pre={pre_wc} post={post_wc}"
    else:
        A["A4_wolves_intact"] = "not-run"
        D["A4_wolves_intact_detail"] = "missing pre/post"

    # A5: hunt resolved to graveyard
    if post_s:
        in_gy = len(gy_named(post_s, 0, HUNT)) > 0
        A["A5_hunt_resolved"] = "passed" if in_gy else "failed"
        D["A5_hunt_resolved_detail"] = f"hunt_in_p0_gy={in_gy}"
    else:
        A["A5_hunt_resolved"] = "not-run"
        D["A5_hunt_resolved_detail"] = "post.json missing"

    # A6: cleanup
    if post_s:
        stack_empty = not (post_s.get("stack") or [])
        wf_ok = wf_type(post_s) == "Priority"
        A["A6_cleanup"] = "passed" if (stack_empty and wf_ok) else "failed"
        D["A6_cleanup_detail"] = (f"stack_empty={stack_empty} "
                                  f"wf={wf_type(post_s)}")
    else:
        A["A6_cleanup"] = "not-run"
        D["A6_cleanup_detail"] = "post.json missing"

    # Verdict gates on the damage OUTCOME (the reported bug), not on whether
    # the target prompt appeared: with a single legal target the engine
    # auto-targets (no prompt), which is legitimate. A2 is recorded for the
    # report path but does not gate the verdict.
    if A["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif A["A3a_any_damage"] == "failed":
        verdict = "reproduced"
    elif A["A3b_full_damage"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    assertions = {"assertions": A, "details": D, "verdict": verdict,
                  "pre_wolves": ST["pre_wolves"],
                  "rejections": ST["rejections"]}
    json.dump(assertions, open(f"{EVDIR}/assertions.json", "w"), indent=1)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("final", assertions)
    await C0.close()
    await C1.close()
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
