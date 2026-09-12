#!/usr/bin/env python3
"""Issue #6895: Exquisite Blood - when the ability triggers, life is deducted.

Reported (Discord, status:confirmed, area:engine): "When ability triggered
life is deducted." Expected: life gain equal to opponent life loss. Actual:
life lost equal to opponent life loss.

Oracle: "Whenever an opponent loses life, you gain that much life."
The pinned v0.80.0 card data parses correctly (LifeLost trigger, Opponent
target, GainLife of EventContextAmount) -- the classifier flagged the AST
as correct and the runtime as applying the opposite life operation.

Behavioral contract (native engine v0.81.0 / protocol 70, two human seats):
  P0 casts Exquisite Blood ({4}{B}); P0 then Lightning Bolts P1 for 3.
  P1 20->17 fires the trigger; on resolution P0 must gain 3 (20->23).
  A1 setup_ok        EB on P0 BF, Bolt in hand pre-cast, life 20/20
  A2 trigger_fires   EB TriggeredAbility observed on stack after P1 lost life
  A3 life_gain       post: P0 20->23 (gain); bug: 20->17 (deduction)
  A4 no_extra        P1 life only reflects the 3 Bolt damage; stack empty,
                     game proceeds

Run 20260912-6895f: retry of 6895b with the interaction-envelope fix
(scenario send_interaction now calls c.send_interaction, not
c.send_action), on the newly pinned v0.81.0 (protocol 70).

Verdict: reproduced iff A2 passes and P0 life was DEDUCTED on resolution.
not-reproduced iff A2 passes and P0 gained. blocked iff setup never ran.
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
RUN_ID = "20260912-6895f"
EVDIR = f"{BACKFILL}/evidence/6895/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

EB = "Exquisite Blood"
BOLT = "Lightning Bolt"
SWAMP = "Swamp"
MOUNTAIN = "Mountain"
P0_DECK = [(EB, 12), (BOLT, 12), (SWAMP, 18), (MOUNTAIN, 18)]
P1_DECK = [("Forest", 60)]
TIMEOUT = 1200

ST = {}
SUBMITTED_IIDS = set()


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",       # SETUP -> BOLT -> RESOLVE -> DONE
        "stop": False,
        "turn_cap": 30,
        "server_hello": None,
        "eb_cast": False,
        "eb_resolved": False,
        "bolt_cast": False,
        "pre_exported": False,
        "trigger_seen": False,
        "trigger_exported": False,
        "p0_life_at_trigger": None,
        "p1_life_at_trigger": None,
        "post_exported": False,
        "rejections": [],
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


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_player(state):
    return ((state.get("waiting_for") or {}).get("data") or {}).get("player")


def eb_trigger_on_stack(state):
    """EB's trigger on the stack. Stack entries carry no ability text
    (cf. run d's stack_bolt.json), so match kind only: Exquisite Blood is
    the sole triggered-ability source in either deck, making any
    TriggeredAbility entry during RESOLVE unambiguously its trigger."""
    for e in (state.get("stack") or []):
        kind = e.get("kind")
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        if ktype == "TriggeredAbility":
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
        o = state["objects"].get(str(ref), {}) if ref else {}
        name, zone, controller = oname(o), o.get("zone"), o.get("controller")
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": name, "zone": zone, "controller": controller,
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
    # NOTE (2026-09-12, run 6895b post-mortem): the client method sends the
    # top-level {"type":"Interaction","data":{"submission":...}} envelope.
    # Routing through c.send_action nests it under {"type":"Action",...} and
    # the server rejects it as an unknown GameAction variant.
    await c.send_interaction(sub)


TARGET_LOGGED = set()


async def answer_target_selection(c, state, want_seat):
    """If a TargetSelection prompt is pending for us, answer it by picking
    the candidate whose surfaces[].data.seat == want_seat."""
    if wf_type(state) != "TargetSelection" or wf_player(state) != 0:
        return False
    for opp in current_opps():
        iid = opp.get("interactionId")
        if iid in SUBMITTED_IIDS:
            continue
        resp = opp.get("response") or {}
        cands = candidate_info(opp, state)
        want = next((x for x in cands if x["seat"] == want_seat), None)
        if iid not in TARGET_LOGGED:
            TARGET_LOGGED.add(iid)
            wire("target_prompt", {"rtype": resp.get("type"),
                                   "candidates": cands,
                                   "stage": ST.get("stage")})
            say(f"[P0] TargetSelection prompt: rtype={resp.get('type')} "
                f"cands={[(x['seat'], x['name']) for x in cands]}")
        if not want:
            wire("target_no_candidate", {"iid": iid, "want_seat": want_seat,
                                         "cands": len(cands),
                                         "stage": ST.get("stage")})
            continue
        sub = build_target_response(resp, want)
        if not sub:
            wire("target_unknown_schema", {"iid": iid,
                                           "rtype": resp.get("type"),
                                           "stage": ST.get("stage")})
            return True  # hold; do not pass priority blindly
        await send_interaction(c, {"interactionId": iid, "response": sub})
        SUBMITTED_IIDS.add(iid)
        say(f"[P0] answers TargetSelection with seat {want_seat} "
            f"(cid {want['choice_id']})")
        return True
    wire("target_held", {"who": c.name, "stage": ST.get("stage")})
    say("[P0] TargetSelection pending; holding priority")
    return True


C0 = None
C1 = None


async def p0_tick(c, pid, state, acts):
    # never pass priority while we owe a target answer
    if await answer_target_selection(c, state, 1):
        return True
    if not is_my_main(state, pid):
        return False
    lands = bf_named(state, pid, SWAMP) + bf_named(state, pid, MOUNTAIN)
    swamps = bf_named(state, pid, SWAMP)
    untapped = [oid for oid, o in bf(state, pid)
                if oname(o) in (SWAMP, MOUNTAIN) and not o.get("tapped")]
    # SETUP: cast Exquisite Blood when 5 lands (incl. 1 swamp) are ready
    if ST["stage"] == "SETUP" and not ST["eb_cast"]:
        if len(lands) >= 5 and len(swamps) >= 1:
            oid = find_hand(state, pid, EB)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                ST["eb_cast"] = True
                say("[P0] casts Exquisite Blood")
                return True
        # play a land each turn
        lid = find_hand(state, pid, SWAMP) or find_hand(state, pid, MOUNTAIN)
        a = next((x for x in acts if x["type"] == "PlayLand"
                  and str(x.get("data", {}).get("object_id")) == lid), None) \
            if lid else None
        if a:
            await submit_as_is(c, a)
            return True
        return False
    # wait for EB to resolve onto the battlefield
    if ST["stage"] == "SETUP" and ST["eb_cast"]:
        if bf_named(state, pid, EB):
            ST["eb_resolved"] = True
            ST["stage"] = "BOLT"
            say("[P0] Exquisite Blood resolved; stage -> BOLT")
        return False
    # BOLT: cast Lightning Bolt at P1 (needs 1 untapped mountain)
    if ST["stage"] == "BOLT" and not ST["bolt_cast"]:
        if len(bf_named(state, pid, EB)) and any(
                oname(state["objects"][oid]) == MOUNTAIN
                for oid in untapped):
            oid = find_hand(state, pid, BOLT)
            a = castspell_advertised(acts, oid)
            if a:
                if not ST["pre_exported"]:
                    await export_now("pre.json")
                    ST["pre_exported"] = True
                await submit_as_is(c, a)
                ST["bolt_cast"] = True
                ST["stage"] = "RESOLVE"
                say("[P0] casts Lightning Bolt at P1")
                return True
        lid = find_hand(state, pid, SWAMP) or find_hand(state, pid, MOUNTAIN)
        a = next((x for x in acts if x["type"] == "PlayLand"
                  and str(x.get("data", {}).get("object_id")) == lid), None) \
            if lid else None
        if a:
            await submit_as_is(c, a)
            return True
    return False


async def p1_tick(c, pid, state, acts):
    if not is_my_main(state, pid):
        return False
    lid = find_hand(state, pid, "Forest")
    a = next((x for x in acts if x["type"] == "PlayLand"
              and str(x.get("data", {}).get("object_id")) == lid), None) \
        if lid else None
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
    # RESOLVE: freeze each stack object in turn so nothing resolves
    # between ticks unobserved. First the Bolt is held on the stack, then
    # the EB trigger gets its own hold below once seen.
    if ST["stage"] == "RESOLVE" and (state.get("stack") or []) \
            and not ST["trigger_seen"]:
        if pid == 0 and not ST.get("bolt_stack_seen"):
            ST["bolt_stack_seen"] = True
            wire("stack_bolt", {"stack": stack_summary(state)})
            await export_now("stack_bolt.json")
            ST["stack_hold_until"] = time.time() + 3.0
            say("Bolt on stack; holding priority 3s")
        if time.time() < ST.get("stack_hold_until", 0):
            if wf_player(state) == pid and wf_type(state) == "Priority":
                return True
    # trigger observation: record the trigger on the stack + lives
    if pid == 0 and ST["stage"] == "RESOLVE" and not ST["trigger_seen"]:
        if eb_trigger_on_stack(state):
            ST["trigger_seen"] = True
            ST["p0_life_at_trigger"] = life(state, 0)
            ST["p1_life_at_trigger"] = life(state, 1)
            wire("eb_trigger", {"p0_life": ST["p0_life_at_trigger"],
                                "p1_life": ST["p1_life_at_trigger"],
                                "stage": ST["stage"]})
            say(f"EB trigger on stack: P0 life={ST['p0_life_at_trigger']} "
                f"P1 life={ST['p1_life_at_trigger']}")
            await export_now("mid_trigger.json")
            ST["trigger_exported"] = True
            # freeze the trigger on the stack: hold priority for a few
            # seconds so the export above is not the only record and the
            # resolution is observed deliberately, not raced.
            ST["trigger_hold_until"] = time.time() + 5.0
    # hold priority while the EB trigger sits on the stack (both seats)
    if ST["stage"] == "RESOLVE" and ST["trigger_seen"] \
            and not ST.get("trigger_released"):
        if time.time() < ST.get("trigger_hold_until", 0):
            if wf_player(state) == pid and wf_type(state) == "Priority":
                return True
        else:
            ST["trigger_released"] = True
            say("trigger hold released; letting it resolve")
    # post condition: trigger resolved, stack empty
    if pid == 0 and ST["stage"] == "RESOLVE" and ST["trigger_seen"] \
            and not (state.get("stack") or []):
        if not ST["post_exported"]:
            await export_now("post.json")
            ST["post_exported"] = True
            ST["stage"] = "DONE"
            ST["stop"] = True
            say("post.json exported; stopping")
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
    for a in acts:
        if a["type"] == "PassPriority":
            # never pass on a stale state during RESOLVE: the pump may have
            # delivered a newer state (e.g. the trigger hitting the stack)
            # while this tick was running. Re-tick on the fresh state.
            if ST["stage"] == "RESOLVE" and c.latest is not st:
                return True
            await submit_as_is(c, a)
            return True
    return False


async def main():
    reset()
    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    hello = C0.hello if hasattr(C0, "hello") else None
    say("connected P0")
    wire("hello", {"hello": str(hello)[:300]})
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
            if ST["stage"] == "SETUP" and not ST["eb_cast"] and turn > 15:
                say("no EB cast by turn 15; stopping")
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
    mid = load_env("mid_trigger.json")
    pre_s, post_s, mid_s = env_state(pre), env_state(post), env_state(mid)

    # A1: setup
    eb_bf = len(bf_named(pre_s, 0, EB)) > 0 if pre_s else False
    bolt_hand = find_hand(pre_s, 0, BOLT) is not None if pre_s else False
    l0 = life(pre_s, 0) if pre_s else None
    l1 = life(pre_s, 1) if pre_s else None
    A["A1_setup_ok"] = "passed" if (eb_bf and bolt_hand and l0 == 20
                                    and l1 == 20) else "failed"
    D["A1_setup_ok_detail"] = (f"eb_on_p0_bf={eb_bf} bolt_in_hand={bolt_hand} "
                               f"p0_life={l0} p1_life={l1}")

    # A2: trigger fired
    A["A2_trigger_fires"] = "passed" if ST["trigger_seen"] else "failed"
    D["A2_trigger_fires_detail"] = (
        f"trigger_seen={ST['trigger_seen']} "
        f"life_at_trigger=({ST['p0_life_at_trigger']},"
        f"{ST['p1_life_at_trigger']})")

    # A3: life gain outcome -- the heart of the issue
    p0_post = life(post_s, 0) if post_s else None
    p1_post = life(post_s, 1) if post_s else None
    if ST["trigger_seen"] and p0_post is not None and p1_post is not None \
            and l0 is not None:
        if p0_post == l0 + 3:
            A["A3_life_gain"] = "passed"
            D["A3_life_gain_detail"] = (f"P0 {l0}->{p0_post}: gained 3, "
                                       f"matches Oracle text")
        elif p0_post == l0 - 3:
            A["A3_life_gain"] = "failed"
            D["A3_life_gain_detail"] = (f"BUG: P0 {l0}->{p0_post}: LOST 3 "
                                       f"when the trigger resolved")
        else:
            A["A3_life_gain"] = "failed"
            D["A3_life_gain_detail"] = (f"unexpected P0 life {l0}->{p0_post}")
    else:
        A["A3_life_gain"] = "not-run" if not ST["trigger_seen"] else "failed"
        D["A3_life_gain_detail"] = (f"p0_post={p0_post} p1_post={p1_post}")

    # A4: no extra life movement
    if p1_post is not None and l1 is not None:
        p1_ok = p1_post == l1 - 3
        stack_empty = not (post_s.get("stack") or []) if post_s else False
        A["A4_no_extra"] = "passed" if (p1_ok and stack_empty) else "failed"
        D["A4_no_extra_detail"] = (f"P1 {l1}->{p1_post} (bolt only: {p1_ok}) "
                                   f"stack_empty={stack_empty}")
    else:
        A["A4_no_extra"] = "not-run"
        D["A4_no_extra_detail"] = f"p1_post={p1_post}"

    if A["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif A["A2_trigger_fires"] == "passed" and A["A3_life_gain"] == "failed":
        verdict = "reproduced"
    elif A["A3_life_gain"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    assertions = {"assertions": A, "details": D, "verdict": verdict,
                  "life_at_trigger": (ST["p0_life_at_trigger"],
                                      ST["p1_life_at_trigger"]),
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
