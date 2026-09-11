#!/usr/bin/env python3
"""Issue #6865: Call Forth the Tempest - "deals damage to each creature your
opponents control equal to the total mana value of other spells you've cast
this turn" doesn't deal damage based on the MV of cards played before.

Oracle (pinned v0.80.0 card-data): the damage clause now PARSES as
DamageAll(amount=Ref(PropertyAggregate(Sum, ManaValue,
TurnJournal(SpellsCast, Controller, Typed[Card, OtherThanTriggerObject]))))
target Typed(Creature, controller=Opponent). The Aug-2026 classifier called it
unsupported_aspect; the data/parser gap appears closed, so this run tests
whether the engine actually resolves the damage.

Plan (two human seats, native engine, v0.80.0 / protocol 69):
  SETUP  - land drops; P1 casts Grizzly Bears (2/2) + Giant Spider (2/4);
           P0 holds Lightning Bolt (MV1), Volcanic Hammer (MV2),
           Call Forth the Tempest (MV8 per pinned data: {5}{R}{R}{R}).
  PROOF  - first P0 PreCombatMain with >=11 untapped Mountains and all three
           spells in hand: export pre.json; cast Bolt, Hammer, Tempest
           BACK-TO-BACK on the same turn (no priority pass between casts;
           target prompts answered as they appear). All three must be
           submitted on the proof turn so the turn journal holds MV 1+2.
  CASCADE- two cascade triggers go on the stack above Tempest: decline each
           may-cast if offered (none was offered on the 6865b probe run;
           triggers resolved as no-ops).
  RESOLVE- stack resolves top-down: cascades, then Tempest (DamageAll = 3
           to each P1 creature), then Hammer (3 to P1), then Bolt (3 to P1).
           Export post.json once all three are in P0's graveyard.

Behavioral contract:
  A1 setup_ok      pre.json: P0 PreCombatMain, Bolt+Hammer+Tempest in hand,
                   >=11 untapped Mountains; P1 has Bear+Spider on BF; life 20/20
  A2 precast_ok    Bolt + Hammer resolved to P1 (life 20->14), both in P0 gy,
                   no other P0 casts
  A3 journal_ok    P0's spells_cast_this_turn_by_player == 3 on the proof
                   turn; exactly Bolt+Hammer+Tempest submitted
  A4 bear_dies     the pre-proof Bear is in P1's graveyard post-resolution
  A5 spider_3      the pre-proof Spider is on BF with exactly 3 damage_marked
                   (3 = 1+2; Tempest's own MV8 excluded as "other spells")
  A6 cleanup       Tempest in P0 gy, stack empty, game proceeds; P1 life 14

Verdict = not-reproduced iff A1..A6 all pass; reproduced iff Tempest resolves
(A6 partial: Tempest in gy, stack empty) and A4/A5 fail; blocked if the run
cannot reach Tempest resolution.
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
RUN_ID = "20260911-6865c"
EVID_ISSUE = "6865"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BOLT = "Lightning Bolt"          # MV1, 3 dmg any target
HAMMER = "Volcanic Hammer"       # MV2, 3 dmg any target
TEMPEST = "Call Forth the Tempest"  # MV8 per pinned data
BEAR = "Grizzly Bears"           # 2/2
SPIDER = "Giant Spider"          # 2/4
MOUNTAIN = "Mountain"
FOREST = "Forest"
LANDS = (MOUNTAIN, FOREST)

ST = {"stage": "SETUP", "stop": False, "retry": False}


def reset_globals():
    global C0
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False,
               "proof_turn": None, "proof_enter": None,
               "declines": 0, "pre_exported": False, "mid_exported": False,
               "bolt_oid": None, "hammer_oid": None, "tempest_oid": None,
               "bolt_sent": False, "hammer_sent": False,
               "tempest_sent": False,
               "bear_oid": None, "spider_oid": None})
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    PHASES.clear()
    CASTLOG.clear()
    C0 = None
MULLS = {"P0": 0, "P1": 0}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
PHASES = []
CASTLOG = []  # (turn, name, target_desc) for P0 casts


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


def untapped_mountains(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == MOUNTAIN and not o.get("tapped"))


def n_untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in LANDS and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def life_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


def damage_of(o):
    for k in ("damage", "damage_marked", "marked_damage", "damageMarked"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data"),
                             "stage": ST["stage"]})


def record_phase(state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if not PHASES or PHASES[-1] != key:
        PHASES.append(key)
        wire("phase", {"turn": key[0], "active": key[1], "phase": key[2],
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
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = seat = None
        val = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            if "reference" in d:
                ref = str(d["reference"])
            if "seat" in d:
                seat = d["seat"]
            if "value" in d:
                val = d["value"]
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "value": val, "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": str(ch.get("text") or ch.get("label") or "")[:80]})
    return out


async def answer_burn_target(c, state, st):
    """Answer Bolt/Hammer TargetSelection: target P1 (player, seat 1)."""
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
        want = next((x for x in cands if x["seat"] == 1), None)
        if not want:
            continue
        if ("burn_tgt", iid) not in SHAPES:
            SHAPES.add(("burn_tgt", iid))
            wire("burn_target_prompt",
                 {"rtype": rtype, "candidates": cands,
                  "opportunity": opp})
            say(f"[P0] burn target prompt: "
                f"{[(x['name'], x['seat']) for x in cands]} -> P1")
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] burn: unexpected prompt shape {rtype}/{spec_type}")
            continue
        await send_interaction(c, {"interactionId": iid, "response": resp_out})
        SUBMITTED.add(iid)
        say(f"[P0] burn targets P1 (seat 1)")
        return True
    return False


async def decline_cascade(c, state, st):
    """Decline cascade's may-cast (OptionalEffectChoice decideOptionalEffect).
    Pick the 'false' value choice; never accept."""
    vi = get_vi(st)
    if not vi:
        return False
    wf = (state.get("waiting_for") or {})
    if wf.get("type") != "OptionalEffectChoice":
        return False
    if (wf.get("data") or {}).get("player") != 0:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        if ("cascade", iid) not in SHAPES:
            SHAPES.add(("cascade", iid))
            wire("cascade_may_choice",
                 {"candidates": cands, "opportunity": opp})
            say(f"[P0] cascade may-cast prompt: "
                f"{[(x['choice_id'], x['value'], x['text']) for x in cands]}")
        # pick the decline ('false') choice via value surface
        want = next((x for x in cands
                     if str(x["value"]).lower() == "false"), None)
        if not want:
            want = next((x for x in cands
                         if "decline" in x["text"].lower()
                         or x["text"].lower().startswith("no")), None)
        if not want:
            say("[P0] cascade: no decline choice identifiable; NOT answering")
            wire("cascade_no_decline_found", {"candidates": cands})
            return False
        await send_interaction(c, {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId":
                                                         want["choice_id"]}}})
        SUBMITTED.add(iid)
        ST["declines"] += 1
        say(f"[P0] cascade may-cast DECLINED (#{ST['declines']})")
        return True
    return False


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, MOUNTAIN)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


def proof_ready(state, pid):
    return (untapped_mountains(state, pid) >= 11
            and find_hand(state, pid, BOLT)
            and find_hand(state, pid, HAMMER)
            and find_hand(state, pid, TEMPEST))


async def p0_cast_named(c, pid, state, acts, name):
    oid = find_hand(state, pid, name)
    a = castspell_advertised(acts, oid)
    if not a:
        return False
    await submit_as_is(c, a)
    CASTLOG.append((state.get("turn_number"), name))
    say(f"[P0] casts {name} (oid {oid})")
    return True


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            lands = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
            n_lands = sum(1 for n in lands if n in LANDS)
            keep_ok = n_lands >= 2
            choice = "Keep" if (keep_ok or MULLS[c.name] >= 2) else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
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
    wt0 = (state.get("waiting_for") or {}).get("type")
    if wt0 == "DiscardToHandSize":
        pend = (state.get("waiting_for") or {}).get("data") or {}
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            # P0 protects 1 Tempest, 2 Bolt, 2 Hammer; P1 protects creatures
            keep = {TEMPEST: 1, BOLT: 2, HAMMER: 2} if is_p0 else \
                {BEAR: 2, SPIDER: 2}
            seen = {}
            ranked = []
            for o in h:
                nm = oname(state["objects"][o])
                seen[nm] = seen.get(nm, 0) + 1
                if seen[nm] <= keep.get(nm, 0):
                    ranked.append((0, o))       # protected
                elif nm in LANDS:
                    ranked.append((2, o))       # lands last
                else:
                    ranked.append((1, o))
            ranked.sort(key=lambda x: x[0], reverse=True)
            picks = [o for _, o in ranked[:n]]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}: "
                    f"{[oname(state['objects'][o]) for o in picks]}")
                return True
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "DeclareBlockers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "ChooseLegend" and is_p0:
            await submit_as_is(c, a)
            say("P0 ChooseLegend: submitted as-is")
            return True
    # P0 answers burn targets + cascade declines before anything else
    if is_p0:
        if await answer_burn_target(c, state, st):
            return True
        if await decline_cascade(c, state, st):
            return True
    # never pass while this seat has a decision pending
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if wt0 in ("OptionalCostChoice", "TargetSelection",
               "OptionalEffectChoice", "ManaPayment", "ChooseXValue",
               "DiscardChoice", "ChooseLegend") and wplayer == pid:
        return False
    # P0's plan: act, else fall through to PassPriority below.
    # In PROOF, cast Bolt/Hammer/Tempest back-to-back on the SAME turn:
    # each tick submits at most one cast, answers pending target prompts
    # first (see above), and never passes priority while a cast is
    # unsubmitted, so the turn journal holds all three.
    if is_p0 and is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if ST["stage"] == "PROOF":
            if not ST["bolt_sent"]:
                if await p0_cast_named(c, pid, state, acts, BOLT):
                    ST["bolt_sent"] = True
                    return True
            elif not ST["hammer_sent"]:
                if await p0_cast_named(c, pid, state, acts, HAMMER):
                    ST["hammer_sent"] = True
                    return True
            elif not ST["tempest_sent"]:
                if await p0_cast_named(c, pid, state, acts, TEMPEST):
                    ST["tempest_sent"] = True
                    return True
        # else: fall through to PassPriority below
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p1_step(c, pid, state, acts):
    # land drop (any land)
    for name in LANDS:
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    # opportunistic creature casts in main phases
    if is_my_main(state, pid):
        n_bear = sum(1 for _, o in bf(state, pid) if oname(o) == BEAR)
        n_spider = sum(1 for _, o in bf(state, pid) if oname(o) == SPIDER)
        for want, have, need in ((BEAR, n_bear, 2), (SPIDER, n_spider, 1)):
            if have >= need:
                continue
            oid = find_hand(state, pid, want)
            a = castspell_advertised(acts, oid)
            if a and n_untapped_lands(state, pid) >= (
                    2 if want == BEAR else 4):
                await submit_as_is(c, a)
                say(f"[P1] casts {want}")
                return True
    return False


def tempest_zone(state):
    zones = [(str(oid), o.get("zone")) for oid, o in state["objects"].items()
             if oname(o) == TEMPEST and o.get("owner") == 0]
    return zones


def p0_spell_oids_in_gy(state, name):
    return [str(oid) for oid, o in state["objects"].items()
            if oname(o) == name and o.get("zone") == "Graveyard"
            and o.get("owner") == 0]


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((MOUNTAIN, 36), (BOLT, 10), (HAMMER, 10),
                         (TEMPEST, 4)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((FOREST, 40), (BEAR, 12),
                                    (SPIDER, 12)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1800
    state_keys_logged = False
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                LAST_SUBMIT["iid"] = None
            if rej:
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
        record_phase(st["state"])
        state = st["state"]
        if not state_keys_logged:
            state_keys_logged = True
            wire("state_top_keys", {"keys": sorted(state.keys())})
            say(f"state top-level keys: {sorted(state.keys())}")

        if (state.get("waiting_for") or {}).get("type") == "GameOver" \
                and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        # --- stage transitions (driven by observed state, not submissions)
        if ST["stage"] == "SETUP":
            if (is_my_main(state, 0) and proof_ready(state, 0)
                    and any(oname(o) == BEAR for _, o in bf(state, 1))
                    and any(oname(o) == SPIDER for _, o in bf(state, 1))):
                # record the exact pre-proof creature oids
                for oid, o in bf(state, 1):
                    if oname(o) == BEAR and not ST["bear_oid"]:
                        ST["bear_oid"] = str(oid)
                    if oname(o) == SPIDER and not ST["spider_oid"]:
                        ST["spider_oid"] = str(oid)
                await export_now("pre.json")
                ST["pre_exported"] = True
                ST["proof_turn"] = state.get("turn_number")
                ST["stage"] = "PROOF"
                say(f"=== stage -> PROOF (turn {ST['proof_turn']}) "
                    f"bear={ST['bear_oid']} spider={ST['spider_oid']} ===")
        elif ST["stage"] == "PROOF":
            # all three casts must be SUBMITTED on the proof turn; resolution
            # order is top-down (cascades, Tempest, Hammer, Bolt). DONE once
            # every submitted spell has resolved.
            zones = tempest_zone(state)
            if (ST["tempest_sent"]
                    and any(z == "Stack" for _, z in zones)
                    and not ST.get("mid_exported")):
                ST["tempest_oid"] = next(oid for oid, z in zones
                                         if z == "Stack")
                await export_now("mid_tempest_on_stack.json")
                ST["mid_exported"] = True
                ST["proof_enter"] = now
                say(f"=== Tempest on stack (oid {ST['tempest_oid']}); "
                    f"mid exported ===")
            gy0 = [oname(o) for oid, o in state["objects"].items()
                   if o.get("zone") == "Graveyard" and o.get("owner") == 0]
            if (ST["tempest_sent"] and TEMPEST in gy0 and BOLT in gy0
                    and HAMMER in gy0 and not state.get("stack")):
                await asyncio.sleep(1.5)
                await export_now("post.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("=== all three resolved; post.json exported; DONE ===")
            elif (ST["tempest_sent"]
                    and now - (ST.get("proof_enter") or now) > 240):
                await export_now("mid_stall.json")
                obs["notes"].append(
                    "PROOF watchdog: 240s after Tempest reached the stack "
                    f"with no resolution; zones={tempest_zone(state)} "
                    f"declines={ST['declines']}")
                say("=== PROOF watchdog fired; stopping ===")
                ST["stage"] = "STALLED"
                ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"P0 casts: {CASTLOG}")
    obs["notes"].append(f"cascade declines: {ST['declines']}")
    obs["notes"].append(f"stage at end: {ST['stage']}")
    obs["phases"] = PHASES
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "phases": PHASES,
                   "casts": CASTLOG, "declines": ST["declines"],
                   "stage": ST["stage"],
                   "oids": {"bear": ST["bear_oid"],
                            "spider": ST["spider_oid"],
                            "tempest": ST["tempest_oid"]}}, f, indent=2)
    for n in obs["notes"]:
        say(f"NOTE: {n}")

    await p0.close()
    await p1.close()
    return obs, True


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 5):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = {"assert": {},
                         "notes": [f"attempt {n} crash: {e!r}"]}, False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs.get("assert", {}), indent=2))
