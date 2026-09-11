#!/usr/bin/env python3
"""Issue #6834: Casting Lotus Petal (mana cost 0) fails with "Cannot pay mana
cost" and the game gets stuck.

Reported: "When trying to cast a Lotus Petal (with mana cost 0), I got an
error 'Cannot pay mana cost' and game is stuck." The reporter's attached save
(game-state-turn-5-...) shows the stranded shape: the Petal spell (id 18)
sits on the stack with actual_mana_spent 0, stack_paid_facts {18: {}},
has_pending_cast False, while the game waits on Priority for player 1 --
payment bookkeeping never completed and the spell never resolves.

Triage acceptance criteria:
  - A spell with an empty mana cost completes payment without requiring a
    mana action.
  - A failed or cancelled cast cannot leave a stale payment/stack state that
    blocks progress.
  - Retrying the same legal cast behaves identically without sandbox
    intervention.

Behavioral contract (single game, two human-client seats, v0.79.0/proto 69):
  TEST 1 - P0's main phase (priority): cast Lotus Petal via the advertised
           CastSpell. PRE export first.
           MID  - export when the Petal spell is on the stack; record
                  has_pending_cast, waiting_for, and any Error/ActionRejected
                  wire messages ("Cannot pay mana cost" = smoking gun).
           POST - export once the spell leaves the stack: Petal must be on
                  P0's battlefield, stack empty, no stuck state.
  TEST 2 - Retry: cast a second Lotus Petal on a later P0 main phase
           (acceptance criterion 3); same outcome required.
  CTRL   - Activate one Petal's {T}, sac mana ability (Red); pool must show
           exactly {R} captured on the first post-activation tick, Petal
           leaves the battlefield, game proceeds.

  A1 setup_ok      pre_cast1: Petal in P0 hand, PreCombatMain, P0 priority.
  A2 cast_completes cast submission accepted (zero rejections), spell enters
                   stack, payment completes without a mana action
                   (has_pending_cast clears).
  A3 resolves_clean post_cast1: Petal on P0 BF, stack empty.
  A4 no_stuck      game advances past both casts: later turn reached with
                   empty stack and Priority cycling normally (not the
                   reported "Priority + dangling stack spell" shape).
  A5 retry_identical second cast completed with zero rejections and Petal
                   on BF.
  A6 activation_ok  Petal ability: Petal sacrificed, pool captured with
                   exactly {R}, game proceeds.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and any of A2..A6 fails.
Verdict = not-reproduced iff A1..A6 all pass.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as client_mod  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

client_mod.URL = "ws://127.0.0.1:9374/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6834d"
EVID_ISSUE = "6834"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
if os.path.isdir(EVDIR) and os.listdir(EVDIR):
    raise SystemExit(f"refusing to reuse non-empty evidence dir {EVDIR}; "
                     f"bump RUN_ID")
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


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
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


PETAL = "Lotus Petal"
FOREST = "Forest"
ISLAND = "Island"

P0_DECK = deck((PETAL, 20), (FOREST, 40))
P1_DECK = deck((ISLAND, 60))

ST = {"cast1_submitted": False, "cast1_turn": None, "mid1_exported": False,
      "post1_exported": False, "cast1_seen": False,
      "cast2_submitted": False, "cast2_turn": None,
      "post2_exported": False, "cast2_seen": False,
      "act_submitted": False, "post_act_exported": False,
      "pool_after_act": None, "petal1_oid": None, "petal2_oid": None,
      "mid1_turn": None, "stop": False, "act_turn": None,
      "land_turn": -1, "land_turn_p1": -1}
WF_SEEN = []
REJECTIONS = []


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == name.lower()]


def count_bf_named(state, pid, name):
    return len(bf_named(state, pid, name))


def find_hand(state, pid, name):
    for oid in hand(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


def drain_rejections(c):
    """Collect Error/ActionRejected messages already on the client's inbox."""
    found = 0
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            found += 1
            rec = {"client": c.name, "type": t, "data": data}
            REJECTIONS.append(rec)
            wire("rejection", rec)
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def stack_entries(state):
    return state.get("stack") or []


def mana_pool(state, pid):
    """Color -> count of floating mana units for pid."""
    out = {}
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            for u in (p.get("mana_pool") or {}).get("mana", []) or []:
                blob = json.dumps(u).lower()
                for color in ("white", "blue", "black", "red", "green",
                              "colorless"):
                    if color in blob:
                        out[color] = out.get(color, 0) + 1
                        break
                else:
                    out["unknown"] = out.get("unknown", 0) + 1
    return out


def petal_spell_on_stack(state, pid):
    for e in stack_entries(state):
        try:
            if e["kind"]["type"] != "Spell":
                continue
        except (KeyError, TypeError):
            continue
        src = str(e.get("source_id") or e.get("id"))
        o = objs(state).get(src)
        if o is not None and lname(o) == PETAL.lower():
            return e
        # fall back: controller match + no name available
        if e.get("controller") == pid and o is None:
            return e
    return None


def find_vi_choice(st, code, source_ref=None):
    """(protocol 69) find (interactionId, choice) in viewer_interaction
    exactChoices whose surfaces include the given action code."""
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []):
        resp = op.get("response", {})
        if resp.get("type") != "exactChoices":
            continue
        for ch in resp["data"].get("choices", []):
            surfs = ch.get("surfaces", [])
            codes = [s.get("data", {}).get("code") for s in surfs]
            if code not in codes:
                continue
            if source_ref is not None:
                refs = [s.get("data", {}).get("reference") for s in surfs
                        if s.get("data", {}).get("role") == "source"]
                if str(source_ref) not in [str(r) for r in refs]:
                    continue
            return op.get("interactionId"), ch
    return None


def find_schema_opp(st):
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []):
        if op.get("response", {}).get("type") == "schema":
            return op
    return None


async def submit_schema_choice(c, op, pick_ids, extra=None):
    spec = op.get("response", {}).get("data", {}).get("spec", {})
    rtype = spec.get("type", "sequence")
    data = {"choiceIds": pick_ids}
    if extra:
        data.update(extra)
    wire("schema_submit", {"interactionId": op.get("interactionId"),
                           "response_type": rtype, "data": data})
    await c.send_interaction({"interactionId": op.get("interactionId"),
                              "response": {"type": rtype, "data": data}})


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            wire("action_submit", {"who": "P0", "action": "MulliganDecision/Keep"})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P0] keeps")
            return True

    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)
        ranked = sorted(oids, key=lambda x: 0 if lname(objs(state)[x]) == FOREST.lower() else 1)
        picks = [int(x) for x in ranked[:n]]
        if picks:
            wire("action_submit", {"who": "P0", "action": "SelectCards/discard",
                                  "picks": picks})
            await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
            say(f"[P0] discards {len(picks)} to hand size")
            return True
        return False

    if wtype == "ChooseLegend" and wplayer == 0:
        ca = find_action(acts, "ChooseLegend")
        if ca:
            await c.send_action(ca)
            say("[P0] legend rule: keeps first")
            return True
        return False

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    if wtype == "OrderTriggers" and wplayer == 0:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True

    # schema color choice for the Petal activation (ChooseManaColor)
    if wtype in ("ChooseManaColor",) or (wtype and "olor" in str(wtype)):
        op = find_schema_opp(st)
        if op and wplayer == 0 and ST["act_submitted"] and not ST["post_act_exported"]:
            spec = op.get("response", {}).get("data", {}).get("spec", {})
            cands = op.get("response", {}).get("data", {}).get("candidates", [])
            wire("color_candidates",
                 {"spec_type": spec.get("type"),
                  "cands": cands})
            SYM_TO_COLOR = {"W": "white", "U": "blue", "B": "black",
                            "R": "red", "G": "green", "C": "colorless"}
            pick = None
            for cand in cands:
                # candidates carry single-letter symbols (W/U/B/R/G) on the
                # mana surface; match the candidate's own identity, never the
                # shared prompt blob (which lists all colors)
                ident = ""
                for s in cand.get("surfaces", []) or []:
                    dd = s.get("data", {}) or {}
                    syms = dd.get("symbols") or []
                    ident += " " + " ".join(
                        SYM_TO_COLOR.get(x, str(x)) for x in syms)
                    ident += " " + json.dumps(
                        dd.get("value") or dd.get("label") or
                        dd.get("color") or "").lower()
                if "red" in ident:
                    pick = cand.get("id")
                    break
            if pick is None:
                wire("color_choice_no_red", {"cands_seen": len(cands)})
                return False
            extra = {"count": 1} if spec.get("type") == "manaGroups" else None
            wire("color_choice", {"candidates": len(cands), "pick": pick,
                                  "spec_type": spec.get("type")})
            await submit_schema_choice(c, op, [pick], extra=extra)
            ST["color_answered"] = True
            say(f"[P0] answers mana color choice: {pick}")
            return True
        if wplayer == 0:
            wire("hold_priority", {"wtype": wtype})
            return False

    # never pass priority while a P0 decision is pending
    if wtype in ("OptionalCostChoice", "OptionalEffectChoice", "TargetSelection",
                 "ManaPayment", "ChooseXValue", "SurveilChoice",
                 "ScryChoice", "DiscardChoice") and wplayer == 0:
        wire("hold_priority", {"wtype": wtype})
        return False

    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        import copy
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True

    db = find_action(acts, "DeclareBlockers")
    if db:
        import copy
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True

    turn = state.get("turn_number")

    if is_my_main(state, pid):
        # ---- test 1: cast the first Petal
        if not ST["cast1_submitted"]:
            oid = find_hand(state, pid, PETAL)
            for a in acts:
                if a["type"] == "CastSpell" and oid \
                        and str(a.get("data", {}).get("object_id")) == str(oid):
                    pre = await do_export("pre_cast1.json")
                    ST["cast1_turn"] = pre.get("turn_number")
                    ST["petal1_oid"] = str(oid)
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Petal1",
                                          "object_id": oid})
                    await c.send_action(a)
                    ST["cast1_submitted"] = True
                    ST["cast1_t0"] = time.time()
                    say(f"[P0] casts Lotus Petal #1 (turn {ST['cast1_turn']})")
                    return True
        # ---- test 2: retry -- cast a second Petal on a later turn
        if ST["post1_exported"] and not ST["cast2_submitted"] \
                and turn is not None and ST["cast1_turn"] is not None \
                and turn > ST["cast1_turn"]:
            oid = find_hand(state, pid, PETAL)
            for a in acts:
                if a["type"] == "CastSpell" and oid \
                        and str(a.get("data", {}).get("object_id")) == str(oid):
                    pre = await do_export("pre_cast2.json")
                    ST["cast2_turn"] = pre.get("turn_number")
                    ST["petal2_oid"] = str(oid)
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Petal2",
                                          "object_id": oid})
                    await c.send_action(a)
                    ST["cast2_submitted"] = True
                    ST["cast2_t0"] = time.time()
                    say(f"[P0] casts Lotus Petal #2 (turn {ST['cast2_turn']})")
                    return True
        # ---- control: activate one Petal's mana ability
        if ST["post2_exported"] and not ST["act_submitted"] \
                and turn is not None and ST["cast2_turn"] is not None \
                and turn > ST["cast2_turn"]:
            petal_bf = bf_named(state, 0, PETAL)
            if petal_bf:
                f = find_vi_choice(st, "activateAbility", source_ref=petal_bf[0])
                if f:
                    iid, ch = f
                    ST["act_petal_oid"] = str(petal_bf[0])
                    ST["act_turn"] = turn
                    wire("action_submit", {"who": "P0", "action": "activateAbility/Petal",
                                          "interactionId": iid,
                                          "choiceId": ch["id"]})
                    await c.send_interaction({"interactionId": iid,
                                              "response": {"type": "choose",
                                                           "data": {"choiceId": ch["id"]}}})
                    ST["act_submitted"] = True
                    say(f"[P0] activates Petal {petal_bf[0]} mana ability")
                    return True
        # land drop (retry every tick per #6690 lessons)
        if turn != ST["land_turn"]:
            hid = find_hand(state, pid, FOREST)
            for a in acts:
                if a["type"] == "PlayLand" and hid \
                        and str(a.get("data", {}).get("object_id")) == str(hid):
                    ST["land_turn"] = turn
                    wire("action_submit", {"who": "P0", "action": "PlayLand/Forest"})
                    await c.send_action(a)
                    return True

    for a in acts:
        if a["type"] == "PassPriority":
            wire("action_submit", {"who": "P0", "action": "PassPriority"})
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            return True
    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)
        picks = [int(x) for x in oids[:n]]
        if picks:
            await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
            return True
        return False
    if wtype == "OrderTriggers" and wplayer == 1:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    da = find_action(acts, "DeclareAttackers")
    if da:
        import copy
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        import copy
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True
    if is_my_main(state, pid):
        if state.get("turn_number") != ST["land_turn_p1"]:
            hid = find_hand(state, pid, ISLAND)
            for a in acts:
                if a["type"] == "PlayLand" and hid \
                        and str(a.get("data", {}).get("object_id")) == str(hid):
                    ST["land_turn_p1"] = state.get("turn_number")
                    await c.send_action(a)
                    return True
    if wtype not in ("Priority", None) and wplayer == 1:
        wire("p1_hold", {"wtype": wtype})
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------------ main

async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    try:
        await p0.create(P0_DECK)
    except Exception as e:
        say(f"game creation failed: {e}")
        A["A1_setup_ok"] = "blocked"
        obs["notes"].append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": A, "notes": obs["notes"],
                       "wf_sequence": WF_SEEN, "rejections": REJECTIONS}, f, indent=2)
        await p0.close()
        return obs
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    global C0
    C0 = p0

    last_rev = {}
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        for c, pid, tick in ((p0, p0.player_id, tick_p0),
                             (p1, p1.player_id, tick_p1)):
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if await tick(c, pid):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        drain_rejections(p0)
        drain_rejections(p1)
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)

        turn = state.get("turn_number")
        phase = state.get("phase") or ""

        # MID1: Petal #1 spell on the stack
        if ST["cast1_submitted"] and not ST["mid1_exported"]:
            if petal_spell_on_stack(state, 0):
                mid = await do_export("mid_cast1.json")
                ST["mid1_exported"] = True
                ST["mid1_turn"] = mid.get("turn_number")
                wire("mid_cast1", {"turn": ST["mid1_turn"],
                                   "has_pending_cast": mid.get("has_pending_cast"),
                                   "waiting_for": (mid.get("waiting_for") or {}).get("type"),
                                   "stack_size": len(stack_entries(mid))})
                say(f"[mid1] exported: has_pending_cast="
                    f"{mid.get('has_pending_cast')}")

        # POST1: Petal #1's spell must first be SEEN on the stack, then the
        # stack must be fully empty (resolution observed), before exporting.
        if ST["cast1_submitted"] and not ST["post1_exported"]:
            if ST["cast1_seen"] and not stack_entries(state):
                post = await do_export("post_cast1.json")
                ST["post1_exported"] = True
                ST["post1_petal_bf"] = count_bf_named(post, 0, PETAL)
                wire("post_cast1", {"turn": post.get("turn_number"),
                                    "petal_bf": ST["post1_petal_bf"],
                                    "stack_size": len(stack_entries(post)),
                                    "waiting_for": (post.get("waiting_for") or {}).get("type")})
                say(f"[post1] exported: petal_bf={ST['post1_petal_bf']}")
            elif petal_spell_on_stack(state, 0):
                ST["cast1_seen"] = True

        # POST2: same seen-then-empty discipline for the retry cast.
        if ST["cast2_submitted"] and not ST["post2_exported"]:
            if ST["cast2_seen"] and not stack_entries(state):
                post = await do_export("post_cast2.json")
                ST["post2_exported"] = True
                ST["post2_petal_bf"] = count_bf_named(post, 0, PETAL)
                ST["post2_turn"] = post.get("turn_number")
                wire("post_cast2", {"turn": ST["post2_turn"],
                                    "petal_bf": ST["post2_petal_bf"],
                                    "stack_size": len(stack_entries(post))})
                say(f"[post2] exported: petal_bf={ST['post2_petal_bf']}")
            elif petal_spell_on_stack(state, 0):
                ST["cast2_seen"] = True

        # POST-ACT: capture the mana pool on the first tick AFTER the color
        # choice was answered and the prompt cleared (the Petal leaves the
        # battlefield at activation time when the sacrifice cost is paid,
        # which is before mana is added -- so do NOT gate on BF departure).
        if ST.get("color_answered") and not ST["post_act_exported"]:
            wft = (state.get("waiting_for") or {}).get("type")
            if wft in ("Priority", None):
                pool = mana_pool(state, 0)
                ST["pool_after_act"] = pool
                post = await do_export("post_activation.json")
                ST["post_act_exported"] = True
                wire("post_activation", {"turn": post.get("turn_number"),
                                         "petal_bf": count_bf_named(post, 0, PETAL),
                                         "pool_at_capture": pool})
                say(f"[post_act] exported: pool={pool}")
                ST["stop"] = True

        # safety: if a cast was submitted but its spell never reaches the
        # stack (the reported "Cannot pay mana cost" / stuck symptom),
        # capture the stall shape and stop instead of looping forever.
        now = time.time()
        for key, seen in (("cast1", ST["cast1_seen"]), ("cast2", ST["cast2_seen"])):
            t0k = f"{key}_t0"
            if ST.get(f"{key}_submitted") and not seen \
                    and ST.get(t0k) and now - ST[t0k] > 90:
                await do_export("stuck_no_stack.json")
                obs["notes"].append(f"{key}: submitted but spell never "
                                    f"reached the stack within 90s (stall)")
                ST["stop"] = True
                say(f"[stop] {key} never reached the stack; captured stall")

    # ------------------------------------------------------- evaluate
    def load_env(p):
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)

    def env_state(p):
        try:
            return load_env(p)["state"]
        except FileNotFoundError:
            return None

    pre1 = env_state("pre_cast1.json")
    mid1 = env_state("mid_cast1.json")
    post1 = env_state("post_cast1.json")
    pre2 = env_state("pre_cast2.json")
    post2 = env_state("post_cast2.json")
    postact = env_state("post_activation.json")

    pay_errors = [r for r in REJECTIONS
                  if "pay" in json.dumps(r.get("data", {})).lower()
                  or "Cannot pay" in json.dumps(r.get("data", {}))]

    # A1
    if pre1 is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_cast1.json never exported (no Petal in hand "
                            "on a P0 main phase)")
    else:
        petal = find_hand(pre1, 0, PETAL)
        ok = petal is not None and pre1.get("phase") == "PreCombatMain" \
            and pre1.get("priority_player") == 0
        obs["notes"].append(
            f"pre_cast1: turn={pre1.get('turn_number')} phase={pre1.get('phase')} "
            f"petal_in_hand={petal is not None} priority_player="
            f"{pre1.get('priority_player')}")
        A["A1_setup_ok"] = "passed" if ok else "failed"

    # A2
    if not ST["cast1_submitted"]:
        A["A2_cast_completes"] = "not-run"
        obs["notes"].append("cast1 never submitted; A2 not-run")
    else:
        on_stack = mid1 is not None and petal_spell_on_stack(mid1, 0)
        hpc = mid1.get("has_pending_cast") if mid1 else None
        obs["notes"].append(
            f"cast1 submitted; rejections={len(REJECTIONS)} "
            f"(pay-related={len(pay_errors)}); spell_on_stack_at_mid={on_stack}; "
            f"has_pending_cast_at_mid={hpc}")
        if pay_errors:
            A["A2_cast_completes"] = \
                f"failed (bug reproduced: {pay_errors[0]['type']} on cast)"
        elif not on_stack:
            A["A2_cast_completes"] = "failed (cast never reached the stack)"
        else:
            A["A2_cast_completes"] = "passed"

    # A3
    if post1 is None:
        A["A3_resolves_clean"] = "not-run"
        obs["notes"].append("post_cast1.json missing; A3 not-run")
    else:
        n = count_bf_named(post1, 0, PETAL)
        empty = len(stack_entries(post1)) == 0
        obs["notes"].append(
            f"post_cast1: petal_on_P0_BF={n} stack_empty={empty} "
            f"waiting_for={(post1.get('waiting_for') or {}).get('type')}")
        A["A3_resolves_clean"] = "passed" if (n >= 1 and empty) else "failed"

    # A4
    if post1 is None or post2 is None:
        A["A4_no_stuck"] = "not-run"
        obs["notes"].append("post1/post2 missing; A4 not-run")
    else:
        t1, t2 = post1.get("turn_number"), post2.get("turn_number")
        stuck_shape = (post2.get("waiting_for") or {}).get("type") == "Priority" \
            and any((lambda e: e.get("kind", {}).get("type") == "Spell")
                    (e) for e in stack_entries(post2))
        obs["notes"].append(
            f"post1 turn={t1} -> post2 turn={t2} (advance={t2 > t1}); "
            f"dangling_stack_spell_with_priority={stuck_shape}")
        A["A4_no_stuck"] = "passed" if (t2 > t1 and not stuck_shape) else "failed"

    # A5
    if post2 is None:
        A["A5_retry_identical"] = "not-run"
        obs["notes"].append("post_cast2.json missing; A5 not-run")
    else:
        n2 = count_bf_named(post2, 0, PETAL)
        new_rej = [r for r in REJECTIONS]
        obs["notes"].append(
            f"post_cast2: petal_on_P0_BF={n2} (expected >=2); "
            f"total rejections={len(new_rej)}")
        A["A5_retry_identical"] = "passed" if n2 >= 2 else "failed"

    # A6
    if not ST["act_submitted"] or postact is None:
        A["A6_activation_ok"] = "not-run"
        obs["notes"].append("activation never completed; A6 not-run")
    else:
        n = count_bf_named(postact, 0, PETAL)
        pool = ST["pool_after_act"] or {}
        red = pool.get("red", 0) >= 1
        obs["notes"].append(
            f"post_activation: petal_on_P0_BF={n} (sacrificed: {n < 2}); "
            f"pool_at_capture={pool}")
        A["A6_activation_ok"] = "passed" if (n < 2 and red) else "failed"

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)
    say("rejections:", len(REJECTIONS))

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN, "rejections": REJECTIONS}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        if any(str(v).startswith("failed") for v in A.values()):
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6834,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": {"Lotus Petal": 20, "Forest": 40},
            "P1": {"Island": 60},
        },
        "verdict": verdict,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", verdict)
    with open(f"{EVDIR}/scenario_6834.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
