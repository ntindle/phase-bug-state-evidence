#!/usr/bin/env python3
"""Issue #6879: Onakke Oathkeeper taxes ALL attacks, not just attacks on
planeswalkers its controller controls.

Oracle text: "Creatures can't attack planeswalkers you control unless their
controller pays {1} for each creature they control that's attacking a
planeswalker you control."

Card-data parse (v0.80.0): static CantAttack, affected=opponent creatures,
condition=UnlessPay {1}, scaling=PerAffectedCreature, defended=Planeswalker.

Plan (native engine, v0.80.0 / protocol 69, two human-driver seats):
  P0 casts Jace, Cunning Castaway + Onakke Oathkeeper.
  P1 casts 2x Grizzly Bears, keeps Forests untapped for tax payment.
  ROUND A (control): P1 attacks P0's planeswalker with 1 bear.
      Expected per oracle text: tax IS demanded ({1} per attacker).
  ROUND B (reported bug): P1 attacks P0 (the player) with 1 bear.
      Expected: NO tax. Bug: tax demanded.

Behavioral contract:
  A1 setup_ok            P0 controls Oathkeeper + Jace; P1 has an untapped
                         bear at round A declaration time.
  A2 pw_tax_demanded     tax was demanded for the planeswalker attack
                         (control; expected True).
  A3 player_tax_demanded tax was demanded for the attack on the player
                         (expected False; True == reported bug).
  A4 attacks_resolved    round A: Jace loyalty 3 -> 1; round B: P0 life
                         20 -> 18 (both attacks actually went through).

Tax detection: any P1 cost/payment submission or payment prompt between
attack declaration and combat resolution, OR a P1 untapped-land delta
(auto-tap) across that window.

Verdict: reproduced iff A1 passes and A3 is True. not-reproduced iff A1
passes, A3 is False and A4 passes. blocked iff A1 cannot be established.
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
RUN_ID = "20260912-0711d"
EVDIR = f"{BACKFILL}/evidence/6879/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

OATH = "Onakke Oathkeeper"
JACE = "Jace, Cunning Castaway"
BEAR = "Grizzly Bears"
PLAINS = "Plains"
ISLAND = "Island"
FOREST = "Forest"
P0_DECK = [(OATH, 12), (JACE, 6), (PLAINS, 14), (ISLAND, 14)]
P1_DECK = [(BEAR, 12), (FOREST, 28)]
TIMEOUT = 1800
HELLO = None

ST = {}
WF_SEEN = []


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",          # SETUP -> ARMED -> A -> A_DONE -> B -> DONE
        "round": None,             # None | "A" | "B"
        "armed_at": None,
        "declare": {},             # per-round declare info
        "tax": {"A": {"prompt": False, "submits": 0, "land_delta": 0},
                "B": {"prompt": False, "submits": 0, "land_delta": 0}},
        "pre": {},                 # per-round pre-declare snapshots
        "attack_recorded": {"A": False, "B": False},
        "combat_done": {"A": False, "B": False},
        "stop": False,
        "rejections": [],
        "turn_cap": 40,
        "server_hello": None,
    })
    WF_SEEN.clear()


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
    # NOTE: object.card_type is null in the state view; identify lands by
    # name (lesson from run 20260912-0711b: name-based land test required)
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in ("Forest", "Plains", "Island")
               and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1][0] != wf or WF_SEEN[-1][2] != ST["stage"]):
        WF_SEEN.append((wf, wf_player(state), ST["stage"]))
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST["stage"]})
        say(f"waiting_for: {wf} player={wf_player(state)} stage={ST['stage']}")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage"), "round": ST.get("round")})
    await c.send_action(action)


async def submit_interaction(c, iid, response, why):
    wire("interaction_submit", {"who": c.name, "why": why,
                                "interactionId": iid, "response": response,
                                "stage": ST.get("stage"),
                                "round": ST.get("round")})
    await c.send_interaction({"interactionId": iid, "response": response})
    r = ST["round"]
    if r in ("A", "B") and c.name == "P1":
        ST["tax"][r]["submits"] += 1


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
                              "stage": ST.get("stage"), "round": ST.get("round")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
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


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def playland_advertised(acts, oid):
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def attackers_list(state):
    return (state.get("combat") or {}).get("attackers") or []


def attack_recorded(state, oid, target_type):
    for a in attackers_list(state):
        tgt = a.get("attack_target") or {}
        try:
            if int(a.get("object_id")) == int(oid) and \
                    tgt.get("type") == target_type:
                return True
        except (TypeError, ValueError):
            continue
    return False


def jace_loyalty(state):
    for oid, o in bf(state, 0):
        if oname(o) == JACE:
            c = o.get("counters") or {}
            # counters may be a dict or list; find loyalty
            if isinstance(c, dict):
                return c.get("loyalty", c.get("Loyalty"))
            for cc in (c if isinstance(c, list) else []):
                if str(cc.get("kind", "")).lower() == "loyalty":
                    return cc.get("count")
            return o.get("loyalty")
    return None


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


C0 = None
C1 = None


async def p0_tick(c, pid, state, acts):
    # land drop every main phase
    if is_my_main(state, pid):
        for land in (PLAINS, ISLAND):
            lid = find_hand(state, pid, land)
            a = playland_advertised(acts, lid) if lid else None
            if a:
                await submit_as_is(c, a)
                say(f"[P0] plays {land}")
                return True
        if ST["stage"] in ("SETUP",):
            # cast Jace then Oathkeeper when advertised affordable;
            # never double up (legend rule / wasted mana)
            for name in (JACE, OATH):
                if bf_named(state, pid, name):
                    continue
                oid = find_hand(state, pid, name)
                a = castspell_advertised(acts, oid)
                if a:
                    await submit_as_is(c, a)
                    say(f"[P0] casts {name}")
                    return True
    return False


async def p1_tick(c, pid, state, acts):
    if is_my_main(state, pid):
        lid = find_hand(state, pid, FOREST)
        a = playland_advertised(acts, lid) if lid else None
        if a:
            await submit_as_is(c, a)
            say(f"[P1] plays Forest")
            return True
        # cast bears only during SETUP, keep >=3 untapped forests for tax
        if ST["stage"] == "SETUP":
            bears = len(bf_named(state, pid, BEAR))
            if bears < 2 and untapped_of(state, pid, FOREST) >= 5:
                oid = find_hand(state, pid, BEAR)
                a = castspell_advertised(acts, oid)
                if a:
                    await submit_as_is(c, a)
                    say("[P1] casts Grizzly Bears")
                    return True
    return False


def check_armed(state):
    if ST["stage"] != "SETUP":
        return
    oath = bf_named(state, 0, OATH)
    jace = bf_named(state, 0, JACE)
    bears = [oid for oid, o in bf(state, 1)
             if oname(o) == BEAR and not o.get("tapped")]
    if oath and jace and bears and untapped_lands(state, 1) >= 3:
        ST["stage"] = "ARMED"
        ST["armed_at"] = state.get("turn_number")
        say(f"ARMED at turn {ST['armed_at']}: Oathkeeper+jace on P0 board, "
            f"{len(bears)} untapped bears, P1 untapped lands "
            f"{untapped_lands(state, 1)}")
        wire("armed", {"turn": ST["armed_at"]})


def dump_declare_surface(c, state, acts, rnd, jace_oid):
    vi = get_vi(c.latest) or {}
    wire(f"declare_{rnd}_vi", {"vi": vi})
    for a in acts:
        if a["type"] == "DeclareAttackers":
            wire(f"declare_{rnd}_legacy_action", {"action": a})
    say(f"[P1] declare surface dumped for round {rnd}")


async def do_declare(c, pid, state, acts, rnd):
    """Submit the round's attack. Returns True if submitted."""
    bears = [oid for oid, o in bf(state, 1)
             if oname(o) == BEAR and not o.get("tapped")]
    if not bears:
        say(f"[P1] round {rnd}: no untapped bear, waiting")
        return False
    bear = bears[0]
    if rnd == "A":
        jace_oids = bf_named(state, 0, JACE)
        if not jace_oids:
            say("[P1] round A: jace gone, cannot run control")
            return False
        target = {"type": "Planeswalker", "data": int(jace_oids[0])}
    else:
        target = {"type": "Player", "data": 0}
    for a in acts:
        if a["type"] != "DeclareAttackers":
            continue
        dump_declare_surface(c, state, acts, rnd, None)
        if not ST["pre"].get(rnd):
            ST["pre"][rnd] = {
                "untapped_lands_p1": untapped_lands(state, 1),
                "jace_loyalty": jace_loyalty(state),
                "p0_life": life(state, 0),
                "turn": state.get("turn_number"),
                "bear_oid": int(bear),
            }
            s = await export_now(f"pre_round{rnd}.json")
            wire(f"pre_round{rnd}_exported", {"ok": s is not None})
        sub = copy.deepcopy(a)
        sub["data"]["attacks"] = [[int(bear), target]]
        sub["data"]["bands"] = []
        await submit_as_is(c, sub)
        ST["declare"][rnd] = {"bear": int(bear), "target": target,
                              "turn": state.get("turn_number")}
        ST["round"] = rnd
        say(f"[P1] round {rnd}: bear {bear} attacks "
            f"{'JACE' if rnd == 'A' else 'P0 player'}")
        return True
    return False


async def empty_declare(c, acts, who):
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


def find_pay_tax_choice(vi):
    """Locate the payCombatTax accept=true choice in a CombatTaxPayment
    exactChoices opportunity. Returns (interactionId, choiceId)."""
    for opp in (vi or {}).get("opportunities", []):
        resp = opp.get("response", {})
        if resp.get("type") != "exactChoices":
            continue
        for ch in resp.get("data", {}).get("choices", []):
            surfaces = ch.get("surfaces", []) or []
            is_pay = any(
                s.get("type") == "action"
                and (s.get("data") or {}).get("code") == "payCombatTax"
                for s in surfaces)
            accept_true = any(
                s.get("type") == "value"
                and (s.get("data") or {}).get("role") == "accept"
                and str((s.get("data") or {}).get("value")).lower() == "true"
                for s in surfaces)
            if is_pay and accept_true:
                return opp.get("interactionId"), ch.get("id")
    return None, None


async def tick(c, pid):
    global C0, C1
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    record_wf(state)
    drain_rejections(c)
    # legend rule: never double-cast Jace; handle ChooseLegend defensively
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"[{c.name}] legend-choice submitted as-is: {a['type']}")
            return True
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{c.name}] keeps")
            return True
    # discard to hand size (named player)
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"[{c.name}] discards {n}")
            return True
    # never pass priority while a cost/target prompt names us
    wt, wp = wf_type(state), wf_player(state)
    # combat tax: the engine demands UnlessPay attack costs via a
    # CombatTaxPayment exactChoices opportunity (payCombatTax accept
    # true/false). We always PAY so the attacks complete; the DEMAND
    # itself is what the assertions measure. Never pass priority while
    # our tax prompt is pending.
    if wf_type(state) == "CombatTaxPayment" and wf_player(state) == pid:
        vi = get_vi(st)
        iid, cid = find_pay_tax_choice(vi)
        r = ST["round"]
        if r in ("A", "B"):
            ST["tax"][r]["prompt"] = True
        if iid and cid:
            await submit_interaction(
                c, iid, {"type": "choose", "data": {"choiceId": cid}},
                "pay_combat_tax")
            say(f"[{c.name}] round {r}: pays combat tax (choice {cid})")
        else:
            wire("tax_prompt_unhandled", {"who": c.name, "vi": vi,
                                          "round": r})
            say(f"[{c.name}] tax prompt with no pay choice; holding")
        return True
    # declare attackers
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        if pid == 0:
            if await empty_declare(c, acts, "P0"):
                return True
        else:
            if ST["stage"] == "ARMED" and ST["round"] is None:
                if await do_declare(c, pid, state, acts, "A"):
                    ST["stage"] = "A"
                    return True
            elif ST["stage"] == "A_DONE" and ST["round"] == "A":
                if await do_declare(c, pid, state, acts, "B"):
                    ST["stage"] = "B"
                    return True
            if await empty_declare(c, acts, "P1-setup"):
                return True
    # declare blockers: P0 never blocks (keeps board clean for assertions)
    if (state.get("phase") or "") == "DeclareBlockers":
        if await empty_blockers(c, acts):
            return True
    # payment prompts: submit as-is (records tax submits for P1 rounds)
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            r = ST["round"]
            if r in ("A", "B") and c.name == "P1":
                ST["tax"][r]["submits"] += 1
                say(f"[P1] round {r}: paid mana (tax submit "
                    f"#{ST['tax'][r]['submits']})")
            return True
    # vi cost/payment opportunities for P1 during a round: answer as
    # advertised only if it looks like a cost prompt; never auto-decline
    # unknown prompts (log them)
    vi = get_vi(st)
    if vi and ST["round"] in ("A", "B") and c.name == "P1":
        wire("round_vi_opportunity",
             {"round": ST["round"], "vi": vi, "wf": wf_type(state)})
    # main-phase driving
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
        check_armed(state)
    # default: pass priority
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def detect_tax(state, rnd):
    """Decide whether a tax was demanded for round rnd using the wire log
    and land deltas. Returns (demanded: bool, detail: str)."""
    t = ST["tax"][rnd]
    pre = ST["pre"].get(rnd, {})
    post_lands = untapped_lands(state, 1)
    delta = pre.get("untapped_lands_p1", post_lands) - post_lands
    t["land_delta"] = delta
    # explicit payment prompt seen? scan wire for payment-ish events in
    # the round window
    prompt = t["prompt"]
    submits = t["submits"]
    demanded = prompt or submits > 0 or delta >= 1
    detail = (f"prompt={prompt} pay_submits={submits} "
              f"land_delta={delta} (pre {pre.get('untapped_lands_p1')} -> "
              f"post {post_lands})")
    return demanded, detail


async def main():
    reset()
    global C0, C1, HELLO
    # grab ServerHello for run.json provenance
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats {C0.player_id}/{C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_hello": ST["server_hello"]})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        # round state machine on fresh states
        for c, pid in ((C0, 0), (C1, 1)):
            st = c.latest
            if not st:
                continue
            state = st.get("state")
            rnd = ST["round"]
            if rnd in ("A", "B") and not ST["attack_recorded"][rnd]:
                d = ST["declare"].get(rnd)
                if d and attack_recorded(
                        state, d["bear"],
                        "Planeswalker" if rnd == "A" else "Player"):
                    ST["attack_recorded"][rnd] = True
                    say(f"round {rnd}: attack recorded in combat "
                        f"(bear {d['bear']})")
                    wire(f"round_{rnd}_attack_recorded",
                         {"declare": d, "turn": state.get("turn_number")})
            # combat done: back in a main phase or next turn's upkeep
            if rnd in ("A", "B") and ST["attack_recorded"][rnd] \
                    and not ST["combat_done"][rnd]:
                ph = state.get("phase") or ""
                if ph in ("PostCombatMain", "End", "Cleanup") or \
                        (state.get("turn_number") or 0) > \
                        (ST["declare"][rnd]["turn"] or 0):
                    ST["combat_done"][rnd] = True
                    say(f"round {rnd}: combat done at phase {ph}")
                    await export_now(
                        f"mid_round{rnd}.json" if rnd == "A"
                        else "post_roundB.json")
                    demanded, detail = detect_tax(state, rnd)
                    ST["tax"][rnd]["demanded"] = demanded
                    ST["tax"][rnd]["detail"] = detail
                    say(f"round {rnd}: tax_demanded={demanded} [{detail}]")
                    wire(f"round_{rnd}_tax", {"demanded": demanded,
                                             "detail": detail,
                                             "tax": ST["tax"][rnd]})
                    if rnd == "A":
                        ST["stage"] = "A_DONE"
                    else:
                        ST["stage"] = "DONE"
                        ST["stop"] = True
        if acted0 or acted1:
            last_progress = time.time()
        # turn cap -> blocked
        st0 = C0.latest
        if st0 and (st0.get("state", {}).get("turn_number") or 0) > ST["turn_cap"]:
            say("turn cap reached without arming; stopping")
            wire("turn_cap", {})
            break
        # stall guard
        if time.time() - last_progress > 180:
            say("no progress for 180s; dumping waiting_for and stopping")
            for c in (C0, C1):
                st = c.latest
                if st:
                    wire("stall_state",
                         {"who": c.name,
                          "wf": (st.get("state", {}).get("waiting_for")),
                          "phase": st.get("state", {}).get("phase"),
                          "acts": [a.get("type")
                                   for a in st.get("legal_actions", [])]})
            break
        await asyncio.sleep(0.15)

    # ---- assertions ----
    A = {}
    D = {}
    stA = C0.latest.get("state") if C0.latest else {}
    # reload pre states from disk for exact pre values
    preA = ST["pre"].get("A", {})
    preB = ST["pre"].get("B", {})
    A["A1_setup_ok"] = "passed" if (
        ST["armed_at"] is not None and ST["declare"].get("A")) else "failed"
    D["A1_setup_ok_detail"] = (
        f"armed_at_turn={ST['armed_at']} declareA={ST['declare'].get('A')}")
    taxA = ST["tax"]["A"].get("demanded", False)
    A["A2_pw_tax_demanded"] = "passed" if taxA else "failed"
    D["A2_pw_tax_demanded_detail"] = ST["tax"]["A"].get("detail", "no data")
    taxB = ST["tax"]["B"].get("demanded", False)
    # A3 passes when NO tax was demanded on the player attack
    A["A3_player_tax_demanded"] = "failed" if taxB else "passed"
    D["A3_player_tax_demanded_detail"] = ST["tax"]["B"].get(
        "detail", "no data (round B never ran)")
    # A4: attacks actually resolved
    try:
        midA = json.loads(open(f"{EVDIR}/mid_roundA.json").read())
        midA = json.loads(midA["state"])["state"] \
            if isinstance(midA.get("state"), str) else midA["state"]
    except Exception as e:
        midA, D["midA_err"] = None, str(e)[:120]
    try:
        postB = json.loads(open(f"{EVDIR}/post_roundB.json").read())
        postB = json.loads(postB["state"])["state"] \
            if isinstance(postB.get("state"), str) else postB["state"]
    except Exception as e:
        postB, D["postB_err"] = None, str(e)[:120]
    loy_pre = preA.get("jace_loyalty")
    loy_mid = jace_loyalty(midA) if midA else None
    life_preB = preB.get("p0_life")
    life_post = life(postB, 0) if postB else None
    okA = (loy_pre is not None and loy_mid is not None
           and loy_mid == loy_pre - 2)
    okB = (life_preB is not None and life_post is not None
           and life_post == life_preB - 2)
    A["A4_attacks_resolved"] = "passed" if (okA and okB) else \
        ("failed" if (midA and postB) else "not-run")
    D["A4_attacks_resolved_detail"] = (
        f"jace loyalty preA={loy_pre} midA={loy_mid} (expect -2); "
        f"P0 life preB={life_preB} postB={life_post} (expect -2)")
    if A["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif taxB:
        verdict = "reproduced"
    elif A["A3_player_tax_demanded"] == "passed" and \
            A["A4_attacks_resolved"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    assertions = {"assertions": A, "details": D, "verdict": verdict,
                  "waiting_for_seq": [w[0] for w in WF_SEEN]}
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
