#!/usr/bin/env python3
"""Issue #6868: Jared Carthalion -3 doesn't add counters to his own Kavu.

Oracle: "[-3]: Choose up to two target creatures. For each of them, put a
number of +1/+1 counters on it equal to the number of colors it is."
Card data marks the counter child Unimplemented (classifier:
unsupported_aspect); the report says targets are accepted but no counters
are put on the all-colors Kavu (5 colors -> 5 counters expected).

Plan (two human seats, native engine, v0.80.0 / protocol 69):
  SETUP  - land drops; P1 casts Grizzly Bears; P0 casts Jared Carthalion
           (gated on 5 distinct untapped colors).
  PLUS1  - P0 activates Jared +1 (ability_index 0) -> loyalty 6, a 3/3
           all-colors Kavu token is created.
  MINUS3 - next P0 turn: export pre_activate.json, activate -3
           (ability_index 1), answer the up-to-two TargetSelection with
           Kavu (5 colors) + P1 Bears (1 color). Wait for resolution;
           export post_activate.json + post.json.

Behavioral contract:
  A1 setup_ok       pre_activate.json: Jared BF, Kavu token BF (all 5
                    colors), P1 Bears BF
  A2 minus3_offered wire evidence: ActivateAbility ability_index 1
                    advertised on Jared
  A3 target_resolve  target prompt answered (Kavu+Bears), no rejection;
                    Jared loyalty 6 -> 3, stack empty post-resolution
  A4 kavu_counters   post: Kavu carries 5 +1/+1 counters (3/3 -> 8/8)
  A5 bears_counters  post: P1 Bears carries 1 +1/+1 counter (2/2 -> 3/3)
  A6 cleanup         game continues; no stuck prompt

Verdict = reproduced iff A3 passes (targets accepted, ability resolves)
but A4/A5 fail (counters missing); not-reproduced iff A4 and A5 pass.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6868b"
EVID_ISSUE = "6868"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

JARED = "Jared Carthalion"
KAVU = "Kavu"
BEAR = "Grizzly Bears"
PLAINS, ISLAND, SWAMP, MOUNTAIN, FOREST = (
    "Plains", "Island", "Swamp", "Mountain", "Forest")
LANDS = (PLAINS, ISLAND, SWAMP, MOUNTAIN, FOREST)

ST = {"stage": "SETUP", "stop": False, "retry": False}
ACT = {}          # loyalty activations: {"plus1": {...}, "minus3": {...}}
MULLS = {"P0": 0, "P1": 0}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
TGT = {"minus3": None}   # target prompt answer record
PLUS1_TURN = {"turn": None}
OBS = {}          # observations: prompt candidates, submitted ids, dumps


def reset_globals():
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False})
    ACT.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.update({"iid": None})
    TGT.clear()
    TGT.update({"minus3": None})
    PLUS1_TURN.clear()
    PLUS1_TURN.update({"turn": None})
    OBS.clear()


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


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def distinct_untapped_colors(state, pid):
    return {name for name in LANDS if untapped_of(state, pid, name) >= 1}


def loyalty_of(o):
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


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
                    "controller": o.get("controller")})
    return out


def jared_oids(state):
    return [str(oid) for oid, o in bf(state, 0) if oname(o) == JARED]


def kavu_oid(state):
    for oid, o in state["objects"].items():
        if (oname(o) == KAVU and o.get("zone") == "Battlefield"
                and o.get("controller") == 0):
            return str(oid)
    return None


def p1_bear_oid(state):
    for oid, o in state["objects"].items():
        if (oname(o) == BEAR and o.get("zone") == "Battlefield"
                and o.get("controller") == 1):
            return str(oid)
    return None


def activate_options(acts, state, want_index):
    """Find Jared's ActivateAbility options on his object (ability_index)."""
    srcs = set(jared_oids(state))
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src in srcs:
            out.append((d.get("ability_index"), a))
    if want_index is not None:
        return [a for i, a in out if i == want_index]
    return [a for _, a in out]


async def answer_minus3_prompt(c, state, acts, st):
    """Answer P0's -3 TargetSelection sequentially: the engine opens one
    prompt per target (spec max=1). Target Kavu first, then P1 Bears."""
    if TGT.get("minus3"):
        return False
    vi = get_vi(st)
    if not vi:
        return False
    wf = (state.get("waiting_for") or {})
    if wf.get("type") != "TargetSelection":
        return False
    if (wf.get("data") or {}).get("player") != 0:
        return False
    want_refs = [r for r in (kavu_oid(state), p1_bear_oid(state)) if r]
    if len(want_refs) < 2:
        return False
    answered = OBS.setdefault("minus3_answered", [])
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
        zones = {x["zone"] for x in cands}
        if "Battlefield" not in zones:
            continue
        present = {x["ref"] for x in cands}
        nxt = next((r for r in want_refs
                    if r in present and r not in answered), None)
        if not nxt:
            continue
        if ("minus3_prompt", len(chs)) not in SHAPES:
            SHAPES.add(("minus3_prompt", len(chs)))
            wire("minus3_target_prompt",
                 {"rtype": rtype, "candidates": cands, "opportunity": opp})
            say(f"[P0] -3 target prompt candidates: "
                f"{[(x['name'], x['ref'], x['controller']) for x in cands]}")
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        want = next(x for x in cands if x["ref"] == nxt)
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] -3: unexpected prompt shape {rtype}/{spec_type}")
            continue
        OBS.setdefault("target_submissions", []).append(
            {"choice_id": want["choice_id"], "ref": want["ref"],
             "name": want["name"]})
        await send_interaction(c, {"interactionId": iid, "response": resp_out})
        SUBMITTED.add(iid)
        answered.append(nxt)
        say(f"[P0] -3: targeted {want['name']} (oid={want['ref']}) "
            f"[{len(answered)}/2]")
        if len(answered) >= 2:
            TGT["minus3"] = {"refs": want_refs, "at": time.time()}
        return True
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    for a in acts:
        if a["type"] == "MulliganDecision":
            lands = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
            n_lands = sum(1 for n in lands if n in LANDS)
            has_jared = JARED in lands if is_p0 else True
            keep_ok = n_lands >= 2 and (has_jared or MULLS[c.name] >= 1)
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
            if is_p0:
                keep = {JARED, BEAR}
                pref = [o for o in h
                        if oname(state["objects"][o]) in LANDS]
                pref += [o for o in h if o not in pref
                         and oname(state["objects"][o]) not in keep]
                pref += [o for o in h if o not in pref]
            else:
                keep = {BEAR}
                pref = [o for o in h
                        if oname(state["objects"][o]) in LANDS]
                pref += [o for o in h if o not in pref
                         and oname(state["objects"][o]) not in keep]
                pref += [o for o in h if o not in pref]
            picks = pref[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}")
                return True
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
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
    # answer P0's -3 target prompt before anything else
    if is_p0 and ST["stage"] == "MINUS3":
        if await answer_minus3_prompt(c, state, acts, st):
            return True
    # never pass while P0 has a decision pending
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                        "ManaPayment", "ChooseXValue", "DiscardChoice") \
            and wplayer == 0:
        return False
    # P0's plan
    if is_p0 and is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if ST["stage"] == "SETUP":
            if await p0_setup_cast(c, pid, state, acts):
                return True
        elif ST["stage"] == "PLUS1":
            if await p0_plus1(c, pid, state, acts):
                return True
        elif ST["stage"] == "MINUS3":
            if await p0_minus3(c, pid, state, acts):
                return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p0_land_drop(c, pid, state, acts):
    have = {oname(o) for _, o in bf(state, pid)}
    order = [l for l in LANDS if l not in have] + [l for l in LANDS
                                                  if l in have]
    for name in order:
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def p0_setup_cast(c, pid, state, acts):
    # Bears filler (needs G + 1)
    if not any(oname(o) == BEAR for _, o in bf(state, pid)):
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, FOREST) >= 1 \
                and sum(1 for _, o in bf(state, pid)
                        if not o.get("tapped")) >= 2:
            await submit_as_is(c, a)
            say("P0 casts Grizzly Bears")
            return True
    # Jared (needs W U B R G); gate on none-on-BF (legend)
    if not jared_oids(state):
        oid = find_hand(state, pid, JARED)
        a = castspell_advertised(acts, oid)
        if a and len(distinct_untapped_colors(state, pid)) >= 5:
            await submit_as_is(c, a)
            say("P0 casts Jared Carthalion")
            return True
    return False


async def p0_plus1(c, pid, state, acts):
    if ACT.get("plus1"):
        return False
    opts = activate_options(acts, state, 0)
    if ("plus1_opts", len(opts)) not in SHAPES:
        SHAPES.add(("plus1_opts", len(opts)))
        wire("plus1_options", {"count": len(opts),
                              "all_action_types":
                              sorted({a["type"] for a in acts})})
        say(f"[P0] Jared +1 options: {len(opts)}")
    if not opts:
        return False
    ACT["plus1"] = {"at": time.time(), "turn": state.get("turn_number")}
    PLUS1_TURN["turn"] = state.get("turn_number")
    await submit_as_is(c, opts[0])
    say(f"[P0] activates Jared +1 (ability_index 0) "
        f"turn={state.get('turn_number')}")
    wire("jared_plus1", {"turn": state.get("turn_number")})
    return True


async def p0_minus3(c, pid, state, acts):
    if ACT.get("minus3") or TGT.get("minus3"):
        return False
    # one loyalty activation per permanent per turn: wait for a fresh turn
    if PLUS1_TURN.get("turn") is not None and \
            state.get("turn_number") == PLUS1_TURN["turn"]:
        return False
    if not kavu_oid(state) or not p1_bear_oid(state):
        return False
    opts = activate_options(acts, state, 1)
    if ("minus3_opts", len(opts)) not in SHAPES:
        SHAPES.add(("minus3_opts", len(opts)))
        wire("minus3_options", {"count": len(opts),
                               "all_action_types":
                               sorted({a["type"] for a in acts})})
        say(f"[P0] Jared -3 options: {len(opts)}")
    if not opts:
        return False
    OBS["minus3_offered"] = True
    await export_now("pre_activate.json")
    ACT["minus3"] = {"at": time.time(), "turn": state.get("turn_number")}
    await submit_as_is(c, opts[0])
    say(f"[P0] activates Jared -3 (ability_index 1) "
        f"turn={state.get('turn_number')}")
    wire("jared_minus3", {"turn": state.get("turn_number")})
    return True


async def p1_step(c, pid, state, acts):
    # land drop preferring Forest (Bears), then cast Bears
    if is_my_main(state, pid):
        order = [FOREST] + [l for l in LANDS if l != FOREST]
        for name in order:
            lid = find_hand(state, pid, name)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True
                break
        if not p1_bear_oid(state):
            oid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, oid)
            if a and untapped_of(state, pid, FOREST) >= 1 \
                    and sum(1 for _, o in bf(state, pid)
                            if not o.get("tapped")) >= 2:
                await submit_as_is(c, a)
                say("P1 casts Grizzly Bears")
                return True
    return False


def plus1p1_counters(o):
    """Return total +1/+1 counters on an object (best effort)."""
    total = 0
    for ck in ("counters", "counter_list", "p1p1_counters"):
        v = o.get(ck)
        if isinstance(v, (int, float)) and ck == "p1p1_counters":
            total += int(v)
        elif isinstance(v, list):
            for e in v:
                if isinstance(e, dict):
                    t = str(e.get("type", "")) + str(e.get("kind", "")) + \
                        str(e.get("name", ""))
                    if "P1P1" in t.upper().replace("1", "1") or \
                            "P1P1" in str(e):
                        total += int(e.get("count", e.get("amount", 1)))
                elif isinstance(e, str) and "P1P1" in e:
                    total += 1
    blob = json.dumps(o)
    # fallback: explicit counter entries not covered above
    return total, blob


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((JARED, 8), (BEAR, 12),
                         (PLAINS, 8), (ISLAND, 8), (SWAMP, 8),
                         (MOUNTAIN, 8), (FOREST, 8)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((BEAR, 12),
                                    (PLAINS, 12), (ISLAND, 12),
                                    (SWAMP, 12), (MOUNTAIN, 12),
                                    (FOREST, 12)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1800
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej:
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

        if (state.get("waiting_for") or {}).get("type") == "GameOver" \
                and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        # --- stage transitions
        if ST["stage"] == "SETUP" and jared_oids(state):
            ST["stage"] = "PLUS1"
            say("=== stage -> PLUS1 ===")
        if ST["stage"] == "PLUS1" and ACT.get("plus1") and kavu_oid(state):
            ST["stage"] = "MINUS3"
            say(f"=== Kavu token on BF; stage -> MINUS3 "
                f"(turn {state.get('turn_number')}) ===")
            wire("kavu_created",
                 {"oid": kavu_oid(state),
                  "obj": state["objects"].get(kavu_oid(state), {})})

        # --- resolution watch after -3 targets answered
        if TGT.get("minus3") and not ST["stop"]:
            zids = jared_oids(state)
            loy = loyalty_of(state["objects"][zids[0]]) if zids else None
            stack_empty = not any(x.get("zone") == "Stack"
                                  for x in state["objects"].values())
            if loy == 3 and stack_empty:
                # give cleanup a beat, then export
                await asyncio.sleep(1.5)
                st2 = p0.latest
                state2 = st2["state"] if st2 else state
                stack_empty2 = not any(x.get("zone") == "Stack"
                                       for x in state2["objects"].values())
                if stack_empty2:
                    ko = state2["objects"].get(kavu_oid(state2) or "", {})
                    bo = state2["objects"].get(p1_bear_oid(state2) or "", {})
                    OBS["kavu_post"] = ko
                    OBS["bear_post"] = bo
                    wire("post_resolution_objects",
                         {"kavu": ko, "bear": bo})
                    await export_now("post_activate.json")
                    try:
                        await export_now("post.json")
                    except Exception as e:
                        say(f"final post export failed: {e}")
                    ST["stop"] = True
                    say("=== DONE: -3 resolved ===")
            elif time.time() - TGT["minus3"]["at"] > 180:
                say("target answered but no resolution after 180s; "
                    "exporting post anyway")
                wire("resolution_timeout", {})
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # --- stuck watches
        if ST["stage"] == "MINUS3" and ACT.get("minus3") \
                and not TGT.get("minus3") \
                and time.time() - ACT["minus3"]["at"] > 180 \
                and not ST["stop"]:
            say("target prompt never answered after 180s; exporting post")
            wire("target_timeout", {})
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    def colors_of(o):
        c = o.get("colors") or o.get("color") or []
        if isinstance(c, str):
            return [c]
        return list(c)

    # ---- assertions
    try:
        pre = load_state("pre_activate.json")
        ko = pre["objects"].get(kavu_oid(pre) or "", {})
        A["A1_setup_ok"] = ("passed"
                            if jared_oids(pre)
                            and kavu_oid(pre)
                            and len(colors_of(ko)) == 5
                            and p1_bear_oid(pre)
                            else "failed")
        obs["notes"].append(f"A1: jared={bool(jared_oids(pre))} "
                            f"kavu={kavu_oid(pre)} colors={colors_of(ko)} "
                            f"p1bear={p1_bear_oid(pre)}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"A1 eval error: {e}")

    A["A2_minus3_offered"] = ("passed"
                              if OBS.get("minus3_offered") else "failed")
    obs["notes"].append(f"A2: minus3 offered={OBS.get('minus3_offered')}")

    try:
        tgt_ok = TGT.get("minus3") is not None
        rejs = []
        for line in open(f"{EVDIR}/wire_log.jsonl"):
            d = json.loads(line)
            if d["event"] == "rejected":
                rejs.append(d)
        post = load_state("post_activate.json")
        zids = jared_oids(post)
        loy = loyalty_of(post["objects"][zids[0]]) if zids else None
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        A["A3_target_resolve"] = ("passed"
                                  if tgt_ok and not rejs and loy == 3
                                  and stack_empty else "failed")
        obs["notes"].append(f"A3: answered={tgt_ok} rejections={len(rejs)} "
                            f"loyalty={loy} stack_empty={stack_empty}")
    except Exception as e:
        A["A3_target_resolve"] = "not-run"
        obs["notes"].append(f"A3 eval error: {e}")

    try:
        post = load_state("post_activate.json")
        ko = post["objects"].get(kavu_oid(post) or "", {})
        n, _ = plus1p1_counters(ko)
        obs["notes"].append(f"A4: kavu obj keys={sorted(ko.keys())} "
                            f"counters_field={ko.get('counters')} "
                            f"power={ko.get('power')} toughness="
                            f"{ko.get('toughness')}")
        A["A4_kavu_counters"] = ("passed" if n == 5 else "failed")
        obs["notes"].append(f"A4: kavu +1/+1 counters={n} (expected 5)")
    except Exception as e:
        A["A4_kavu_counters"] = "not-run"
        obs["notes"].append(f"A4 eval error: {e}")

    try:
        post = load_state("post_activate.json")
        bo = post["objects"].get(p1_bear_oid(post) or "", {})
        n, _ = plus1p1_counters(bo)
        obs["notes"].append(f"A5: bear obj counters_field={bo.get('counters')} "
                            f"power={bo.get('power')} toughness="
                            f"{bo.get('toughness')}")
        A["A5_bears_counters"] = ("passed" if n == 1 else "failed")
        obs["notes"].append(f"A5: P1 Bears +1/+1 counters={n} (expected 1)")
    except Exception as e:
        A["A5_bears_counters"] = "not-run"
        obs["notes"].append(f"A5 eval error: {e}")

    try:
        final = load_state("post.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in final["objects"].values())
        game_over = (final.get("waiting_for") or {}).get("type") == "GameOver"
        A["A6_cleanup"] = ("passed" if stack_empty and not game_over
                           else "failed")
    except Exception:
        A["A6_cleanup"] = "not-run"

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"targets: {TGT}")
    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN, "targets": TGT,
                   "observations": OBS}, f, indent=2)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": OBS, "wf_sequence": WF_SEEN}, f, indent=2)

    verdict = ("reproduced"
               if A.get("A3_target_resolve") == "passed"
               and A.get("A4_kavu_counters") == "failed"
               and A.get("A5_bears_counters") == "failed"
               else ("not-reproduced"
                     if A.get("A4_kavu_counters") == "passed"
                     and A.get("A5_bears_counters") == "passed"
                     else "blocked"))
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")

    await p0.close()
    await p1.close()
    return obs, True


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = ({"assert": {},
                          "notes": [f"attempt {n} crash: {e!r}]"]}), False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps({**obs.get("assert", {}),
                      "verdict": obs.get("verdict")}, indent=2))
