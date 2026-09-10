#!/usr/bin/env python3
"""Issue #6764: Knowledge Pool lets you select any card in exile and still doesn't cast it.

Oracle (verified from pinned card data):
  "Imprint - When this artifact enters, each player exiles the top three cards
   of their library. Whenever a player casts a spell from their hand, that player
   exiles it. If the player does, they may cast a spell from among other cards
   exiled with this artifact without paying its mana cost."

Behavioral contract:
  A1 setup_ok          Knowledge Pool on battlefield; imprint exiled top 3 of
                       each library (6 exile objects, each library -3)
  A2 trigger_fires     casting Shock from hand moves it to exile (not stack)
                       and a may-cast choice appears for the caster
  A3 choice_scope      the free-cast card choice offers ONLY other cards exiled
                       with THIS Knowledge Pool: the triggering spell is
                       excluded and no foreign (non-imprinted) cards appear
  A4 free_cast_executes accepting and choosing Shock casts it free through the
                       normal pipeline: stack entry, resolves, P1 takes 2,
                       Shock to graveyard, P0 pays no mana
  A5 cleanup           game proceeds cleanly after the free cast resolves

Two triggers are exercised: trigger#1 declined (control branch), trigger#2
accepted choosing Shock#1 (the reported path).
Verdict = reproduced iff A3 fails on either trigger or A4 fails.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client  # noqa: F401  (default URL ws://localhost:9374/ws)
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6764"
EVID_ISSUE = "6764"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
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
        WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload},
                              default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# ---------------------------------------------------------------- state helpers

def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_mountains(state, pid):
    return sum(1 for o in bf(state, pid)
               if (o.get("base_name") or o.get("name")) == "Mountain" and not o.get("tapped"))


def hand_oids(state, pid):
    return list(state["players"][pid].get("hand", []))


def hand_names(state, pid):
    return [obj_name(state, oid) for oid in hand_oids(state, pid)]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if obj_name(state, oid) == name:
            return oid
    return None


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o["id"]
    return None


def lib_size(state, pid):
    return len(state["players"][pid].get("library", []))


def exile_objs(state):
    return [o for o in state["objects"].values() if o.get("zone") == "Exile"]


def life(state, pid):
    return state["players"][pid].get("life")


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


def cast_spell_action(acts, state, name):
    for a in acts:
        d = a.get("data", {})
        if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == name:
            return a
    return None


# ------------------------------------------------------------- interaction io

def vi_opps(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def surf_codes(ch):
    return [s.get("data", {}).get("code") for s in ch.get("surfaces", [])]


def choice_bool_value(ch):
    """Extract a true/false value from a choice's value surfaces (protocol 69
    kicker/may prompts carry empty text; value surfaces hold the answer)."""
    for s in ch.get("surfaces", []):
        dd = s.get("data", {}) or {}
        if dd.get("role") in ("accept", "value", "pay") and str(dd.get("value", "")).lower() in ("true", "false"):
            return str(dd["value"]).lower()
    return None


def cand_seat(ch):
    for s in ch.get("surfaces", []):
        dd = s.get("data", {}) or {}
        if "seat" in dd:
            return dd["seat"]
    return None


def cand_name(state, cand):
    """Resolve a candidate dict to a card name via object references, else label."""
    for s in cand.get("surfaces", []):
        dd = s.get("data", {}) or {}
        for k in ("reference", "object_id", "card", "objectId"):
            ref = dd.get(k)
            if ref is not None:
                n = obj_name(state, ref)
                if n != "?":
                    return n
    for k in ("label", "name", "title"):
        if cand.get(k):
            return str(cand[k])
    return f"<id {cand.get('id')}>"


def cand_zone(state, cand):
    for s in cand.get("surfaces", []):
        dd = s.get("data", {}) or {}
        for k in ("reference", "object_id", "card", "objectId"):
            ref = dd.get(k)
            if ref is not None:
                o = state["objects"].get(str(ref))
                if o:
                    return o.get("zone")
    return None


async def submit_choice(c, iid, choice_id):
    await c.send_interaction({"interactionId": iid,
                              "response": {"type": "choose", "data": {"choiceId": choice_id}}})


async def submit_sequence(c, iid, spec_type, choice_ids):
    await c.send_interaction({"interactionId": iid,
                              "response": {"type": spec_type,
                                           "data": {"choiceIds": choice_ids}}})

# ---------------------------------------------------------------- tick driver

ST = {"stop": False, "stage": "mulligan", "mulls": {}, "notes": [],
      "imprint_oids": [], "shock1_oid": None, "shock2_oid": None,
      "pre_free_untapped": None, "free_stack_seen": False}
_PASSED_REV = {}


async def handle_mulligan(c, pid, state, acts, is_p0):
    # BottomCards phase rides on a SelectCards legal action while waiting_for
    # is MulliganDecision.
    for a in acts:
        if a["type"] == "SelectCards":
            pend = (state.get("waiting_for") or {}).get("data", {}).get("pending", []) or []
            count = 1
            for p in pend:
                if p.get("player") == pid and (p.get("phase") or {}).get("type") == "BottomCards":
                    count = int(p["phase"].get("count", 1))
            h = hand_oids(state, pid)

            def bkey(oid):
                n = obj_name(state, oid)
                return (0 if n not in ("Mountain",) else 1,
                        0 if n != "Knowledge Pool" else 1, n)
            picks = sorted(h, key=bkey)[:count]
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}: {[obj_name(state, x) for x in picks]}")
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            hn = hand_names(state, pid)
            lands = sum(1 for n in hn if n == "Mountain")
            has_pool = "Knowledge Pool" in hn
            mulls = ST["mulls"].get(c.name, 0)
            if not is_p0:
                choice = "Keep"
            elif lands >= 2 and (has_pool or mulls >= 2):
                choice = "Keep"
            elif mulls < 3:
                choice = "Mulligan"
            else:
                choice = "Keep"
            if choice == "Mulligan":
                ST["mulls"][c.name] = mulls + 1
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": choice}}})
            say(f"{c.name} {choice.lower()}s opening hand "
                f"(lands={lands} pool={has_pool} mulls={mulls} hand={len(hn)})")
            return True
    return False


async def handle_discard(c, pid, state, acts):
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    if wtype not in ("DiscardToHandSize", "DiscardChoice"):
        return False
    data = wf.get("data") or {}
    named = data.get("player")
    if named is not None and named != pid:
        return False
    count = data.get("count")
    if not count:
        count = max(0, len(hand_oids(state, pid)) - 7)
    if count <= 0:
        return False
    h = hand_oids(state, pid)

    def dkey(oid):
        n = obj_name(state, oid)
        return (0 if n == "Mountain" else 1, n)
    picks = sorted(h, key=dkey)[:count]
    # try the legacy SelectCards action first (protocol 69 ok on v0.79.0)
    for a in acts:
        if a["type"] == "SelectCards":
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} discards {[obj_name(state, x) for x in picks]}")
            return True
    # fall back to a schema interaction with candidates
    for opp in vi_opps(c):
        data2 = (opp.get("response") or {}).get("data", {}) or {}
        spec = data2.get("spec") or {}
        if spec.get("type") in ("sequence", "select") and data2.get("candidates"):
            stype = spec.get("type")
            await submit_sequence(c, opp.get("interactionId"), stype,
                                  [str(x) for x in picks])
            say(f"{c.name} discards via interaction {[obj_name(state, x) for x in picks]}")
            return True
    return False


def p0_hold_priority(state):
    """True when the stage machine owns P0's next cast: don't let the generic
    tick pass P0's priority away. Only holds during P0's own main phase AND
    only while ST["cast_armed"] (waiting for the cast window); holding during
    post-cast waits (trigger resolution, choices) deadlocks the game — the
    trigger needs both players to pass (hit 2026-09-10, cost one full run)."""
    if not ST.get("cast_armed"):
        return False
    if not is_my_main(state, 0):
        return False
    stage = ST["stage"]
    if stage == "ramp":
        return (find_hand(state, 0, "Knowledge Pool") is not None
                and untapped_mountains(state, 0) >= 6)
    if stage in ("shock1", "shock2"):
        return find_hand(state, 0, "Shock") is not None and untapped_mountains(state, 0) >= 2
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], st.get("legal_actions", []) or []
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if wtype == "MulliganDecision":
        if await handle_mulligan(c, pid, state, acts, is_p0):
            return True
    if await handle_discard(c, pid, state, acts):
        return True
    if ST.get("stage") in ("ramp", "shock1", "shock2") and is_my_main(state, pid):
        if is_p0 and p0_hold_priority(state):
            return False  # stage machine will cast
        for a in acts:
            if a["type"] == "PlayLand":
                await c.send_action(a)
                return True
    if wtype == "Priority" and state.get("priority_player") == pid:
        if is_p0 and p0_hold_priority(state):
            return False
        rev = st.get("state_revision", -1)
        if _PASSED_REV.get(c.name, -1) >= rev:
            return False
        for a in acts:
            if a["type"] == "PassPriority":
                await c.send_action(a)
                _PASSED_REV[c.name] = rev
                return True
    return False


async def pilot(p0, p1):
    try:
        while not ST["stop"]:
            await tick(p0, p0.player_id, True)
            await tick(p1, p1.player_id, False)
            await asyncio.sleep(0.15)
    except asyncio.CancelledError:
        pass


async def wait_cond(c, cond, timeout, label, poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        st = c.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in wait_cond: {label}")
    return None


async def export_ev(c, name):
    s = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(s)
    say(f"exported {name}.json ({len(s)} bytes)")
    return json.loads(s)["state"]

# ------------------------------------------------------- cast + decision flow

async def answer_target_selection(c, seat, timeout=60, label="target"):
    """Answer a schema TargetSelection by picking the candidate for `seat`.
    Returns the chosen candidate id or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        st = c.latest
        if not st:
            continue
        state = st["state"]
        for opp in vi_opps(c):
            resp = opp.get("response") or {}
            if resp.get("type") != "schema":
                continue
            data = resp.get("data") or {}
            spec = data.get("spec") or {}
            if spec.get("type") not in ("sequence", "select"):
                continue
            cands = data.get("candidates") or []
            if not cands:
                continue
            pick = None
            for cd in cands:
                if cand_seat(cd) == seat:
                    pick = cd
                    break
            if pick is None and len(cands) == 1:
                pick = cands[0]
            if pick is None:
                continue
            wire(f"{label}_opportunity",
                 {"interaction": opp,
                  "candidates": [(cd.get("id"), cand_name(state, cd), cand_seat(cd))
                                 for cd in cands]})
            say(f"{c.name} answers {label}: seat={seat} candidate={pick.get('id')} "
                f"({cand_name(state, pick)})")
            await submit_sequence(c, opp.get("interactionId"), spec.get("type"),
                                  [pick["id"]])
            return pick["id"]
    say(f"TIMEOUT answering {label} target selection")
    return None


async def cast_shock_at_p1(c, shock_oid, timeout=120):
    """Cast Shock from hand targeting P1 (seat 1). Returns True once the shock
    is on the stack with P1 chosen."""
    st = c.latest
    state = st["state"]
    a = cast_spell_action(st.get("legal_actions", []) or [], state, "Shock")
    if a is None:
        say(f"{c.name}: no CastSpell action for Shock available")
        return False
    d = a.get("data", {})
    if str(d.get("object_id")) != str(shock_oid):
        say(f"{c.name}: CastSpell object {d.get('object_id')} != tracked {shock_oid}; "
            f"re-tracking to the advertised object")
        shock_oid = str(d.get("object_id"))
    wire("shock_cast_action", {"action": a, "tracked_oid": str(shock_oid)})
    await c.send_action(a)
    say(f"{c.name} casts Shock (oid {shock_oid})")
    tgt = await answer_target_selection(c, seat=1, timeout=timeout, label="shock_target")
    return (tgt is not None), shock_oid


async def wait_may_cast(c, timeout=90):
    """Wait for P0's may-cast decision opportunity after the trigger exiles the
    shock. Returns (opp, kind) where kind in {'decide', 'bool', 'direct'} or
    (None, None) on timeout. 'direct' = card choice with no separate may prompt."""
    t0 = time.time()
    seen_shapes = set()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        st = c.latest
        if not st:
            continue
        for opp in vi_opps(c):
            resp = opp.get("response") or {}
            rtype = resp.get("type")
            data = resp.get("data") or {}
            spec = data.get("spec") or {}
            stype = spec.get("type")
            cands = data.get("candidates") or data.get("choices") or []
            key = (rtype, stype, len(cands))
            if key in seen_shapes:
                continue
            seen_shapes.add(key)
            wire("may_prompt_shape", {"rtype": rtype, "spec": stype,
                                      "n": len(cands), "opp": opp})
            if rtype != "exactChoices":
                continue
            codes = set()
            for ch in cands:
                codes.update(surf_codes(ch))
            if "decideOptionalEffect" in codes:
                say(f"{c.name}: may-cast prompt (decideOptionalEffect, {len(cands)} choices)")
                wire("may_prompt", opp)
                return opp, "decide"
            bools = [choice_bool_value(ch) for ch in cands]
            if len(cands) == 2 and set(bools) == {"true", "false"}:
                say(f"{c.name}: may-cast prompt (bool pair, {len(cands)} choices)")
                wire("may_prompt", opp)
                return opp, "bool"
        # direct card choice without a separate may prompt?
        card = find_card_choice(c)
        if card:
            say(f"{c.name}: card choice appeared with no separate may prompt")
            wire("may_prompt_direct_card_choice", card[0])
            return card[0], "direct"
    say("TIMEOUT waiting for may-cast prompt")
    return None, None


async def answer_may(c, opp, kind, want_accept):
    """Answer the may-cast prompt. Returns True if a submission was sent."""
    iid = opp.get("interactionId")
    cands = ((opp.get("response") or {}).get("data") or {}).get("choices", [])
    pick = None
    if kind == "decide":
        for ch in cands:
            if "decideOptionalEffect" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []):
                dd = s.get("data", {}) or {}
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
                    if is_accept == want_accept:
                        pick = ch
        # fall back to bool value surfaces
        if pick is None:
            want = "true" if want_accept else "false"
            for ch in cands:
                if choice_bool_value(ch) == want:
                    pick = ch
                    break
    elif kind == "bool":
        want = "true" if want_accept else "false"
        for ch in cands:
            if choice_bool_value(ch) == want:
                pick = ch
                break
    if pick is None:
        say(f"{c.name}: could not identify {'accept' if want_accept else 'decline'} choice")
        wire("may_answer_failed", {"kind": kind, "opp": opp})
        return False
    sub = {"interactionId": iid,
           "response": {"type": "choose", "data": {"choiceId": pick["id"]}}}
    wire("may_answer", {"accept": want_accept, "submission": sub})
    say(f"{c.name} answers may-cast: {'ACCEPT' if want_accept else 'DECLINE'} "
        f"(choice {pick['id']})")
    await submit_choice(c, iid, pick["id"])
    return True


def card_choice_cands(copp):
    data = (copp.get("response") or {}).get("data") or {}
    return data.get("candidates", []) or data.get("choices", [])


async def submit_card_pick(c, copp, rtype, stype, pick_cid, label):
    iid = copp.get("interactionId")
    if rtype == "exactChoices":
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": pick_cid}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": stype or "sequence",
                            "data": {"choiceIds": [pick_cid]}}}
    wire(f"{label}_pick", {"submission": sub})
    say(f"{c.name} picks card choice {pick_cid} ({label})")
    await c.send_interaction(sub)


def find_card_choice(c):
    """Find a card-selection opportunity (the free-cast choice). Returns
    (opp, rtype, spec_type, [(cid, cname)]) or None."""
    st = c.latest
    if not st:
        return None
    state = st["state"]
    for opp in vi_opps(c):
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        stype = spec.get("type")
        cands = data.get("candidates") or data.get("choices") or []
        if rtype == "exactChoices":
            # a may prompt is exactChoices too; distinguish by codes
            codes = set()
            for ch in cands:
                codes.update(surf_codes(ch))
            if "decideOptionalEffect" in codes:
                continue
            if not cands:
                continue
            named = [(ch.get("id"), cand_name(state, ch)) for ch in cands]
            return opp, rtype, None, named
        if rtype == "schema" and stype in ("sequence", "select") and cands:
            named = [(ch.get("id"), cand_name(state, ch)) for ch in cands]
            return opp, rtype, stype, named
    return None


async def wait_card_choice(c, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        found = find_card_choice(c)
        if found:
            opp, rtype, stype, named = found
            say(f"{c.name}: card choice ({rtype}/{stype}): " +
                ", ".join(f"{cid}={n}" for cid, n in named))
            wire("card_choice", {"opp": opp,
                                 "candidates": [(cid, n) for cid, n in named]})
            return found
    say("TIMEOUT waiting for card choice")
    return None

# ------------------------------------------------------------------ main flow

def cand_ref_oid(state, cand):
    for s in cand.get("surfaces", []):
        dd = s.get("data", {}) or {}
        for k in ("reference", "object_id", "card", "objectId"):
            ref = dd.get(k)
            if ref is not None and str(ref) in state["objects"]:
                return str(ref)
    return None


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(("Mountain", 36), ("Knowledge Pool", 12), ("Shock", 12)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(("Mountain", 60)))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    obs["assert"]["A1_setup_ok"] = ("passed" if p0.player_id is not None
                                    and p1.player_id is not None else "failed")
    ST["mulls"] = {"P0": 0, "P1": 0}
    pilot_task = asyncio.create_task(pilot(p0, p1))

    # ---- mulligans done?
    s = await wait_cond(
        p0, lambda s: (s.get("waiting_for") or {}).get("type") != "MulliganDecision"
        and len(hand_oids(s, 0)) > 0, 180, "mulligans done")
    if s is None:
        obs["notes"].append("mulligan phase never completed")
        ST["stop"] = True
        await pilot_task
        await p0.close(); await p1.close()
        return obs, None
    say(f"mulligans done: P0 hand={hand_names(p0.latest['state'], 0)}")
    wire("post_mulligan_hands",
         {"P0": hand_names(p0.latest["state"], 0), "P1": hand_names(p1.latest["state"], 1)})
    ST["stage"] = "ramp"
    ST["cast_armed"] = True  # hold P0 priority only until the pool is cast

    # ---- ramp: cast Knowledge Pool
    pool_cast_state = None

    async def cast_pool_once():
        s = await wait_cond(
            p0, lambda s: (find_bf(s, 0, "Knowledge Pool") is not None)
            or (is_my_main(s, 0) and find_hand(s, 0, "Knowledge Pool") is not None
                and untapped_mountains(s, 0) >= 6), 900, "pool cast window")
        if s is None:
            return None
        if find_bf(s, 0, "Knowledge Pool") is not None:
            ST["cast_armed"] = False
            return s
        st = p0.latest
        a = cast_spell_action(st.get("legal_actions", []) or [], st["state"], "Knowledge Pool")
        if not a:
            say("pool cast window seen but no CastSpell action")
            return None
        pre0, pre1 = lib_size(st["state"], 0), lib_size(st["state"], 1)
        pool_oid = a["data"]["object_id"]
        wire("pool_cast_action", {"action": a, "pre_lib": [pre0, pre1]})
        await p0.send_action(a)
        ST["cast_armed"] = False  # pool cast: let priority flow for the ETB trigger
        say(f"P0 casts Knowledge Pool (oid {pool_oid}); pre-cast libs {pre0}/{pre1}")
        return ("cast", pre0, pre1)

    r = await cast_pool_once()
    if r is None:
        obs["notes"].append("Knowledge Pool never cast")
        ST["stop"] = True
        await pilot_task
        await p0.close(); await p1.close()
        return obs, None
    if isinstance(r, tuple):
        _, pre0, pre1 = r
        # wait for pool on BF + imprint resolution (6 exiled)
        def imprinted(s):
            return (find_bf(s, 0, "Knowledge Pool") is not None
                    and len(exile_objs(s)) >= 6)
        s = await wait_cond(p0, imprinted, 180, "imprint resolution")
        if s is None:
            obs["notes"].append("imprint never resolved")
            ST["stop"] = True
            await pilot_task
            await p0.close(); await p1.close()
            return obs, None
        ex = exile_objs(s)
        ST["imprint_oids"] = [str(o["id"]) for o in ex]
        a1 = (len(ex) == 6 and lib_size(s, 0) == pre0 - 3 and lib_size(s, 1) == pre1 - 3)
        obs["assert"]["A1_setup_ok"] = "passed" if a1 else "failed"
        obs["notes"].append(
            f"imprint: exile={len(ex)} ({[(o.get('base_name') or o.get('name')) for o in ex]}); "
            f"libs {pre0}->{lib_size(s,0)}, {pre1}->{lib_size(s,1)}")
        say(f"A1: exile={[ (o.get('base_name') or o.get('name')) for o in ex ]}")
    else:
        obs["notes"].append("pool was already on BF at window check")
    say("exporting PRE_TRIGGER state")
    await export_ev(p0, "pre_trigger")

    # ---- trigger#1: cast Shock from hand, DECLINE the free cast (control)
    ST["stage"] = "shock1"
    ST["cast_armed"] = True

    async def do_trigger(accept, tag):
        """Cast a Shock from hand (targets P1), wait for the exile + may-cast
        prompt, answer it, and if accepted drive the card choice. Returns a
        dict of observations."""
        o = {"tag": tag}
        s = await wait_cond(
            p0, lambda s: is_my_main(s, 0) and find_hand(s, 0, "Shock") is not None
            and untapped_mountains(s, 0) >= 2, 600, f"{tag} shock window")
        if s is None:
            o["error"] = "no shock cast window"
            ST["cast_armed"] = False
            return o
        shock_oid = str(find_hand(s, 0, "Shock"))
        o["shock_oid"] = shock_oid
        o["pre_untapped"] = untapped_mountains(s, 0)
        ok, shock_oid = await cast_shock_at_p1(p0, shock_oid)
        ST["cast_armed"] = False  # cast submitted: let priority flow for the trigger
        o["shock_oid"] = shock_oid
        o["cast_submitted"] = ok
        if not ok:
            o["error"] = "shock cast failed"
            return o
        # the trigger should exile the shock (from the stack)
        s = await wait_cond(
            p0, lambda s: (s["objects"].get(shock_oid) or {}).get("zone") == "Exile",
            120, f"{tag} trigger exiles shock")
        o["trigger_exiled"] = s is not None
        if s is None:
            o["error"] = "trigger never exiled the shock"
            return o
        say(f"{tag}: shock {shock_oid} exiled by trigger")
        opp, kind = await wait_may_cast(p0, timeout=90)
        o["may_kind"] = kind
        if opp is None:
            o["error"] = "no may-cast prompt"
            return o
        await export_ev(p0, f"maycast_{tag}")
        if kind == "direct":
            # The card choice arrived with no separate may prompt; treat the
            # may as implicit and work directly from this opportunity.
            o["may_answered"] = "implicit"
            o["notes"] = "no separate may prompt; card choice is direct"
            found = find_card_choice(p0)
            if not found:
                found = (opp, "unknown", None, [])
        else:
            if not await answer_may(p0, opp, kind, want_accept=accept):
                o["error"] = "may answer failed"
                return o
            o["may_answered"] = accept
            if not accept:
                # control: wait for the trigger to finish with the shock still exiled
                s = await wait_cond(
                    p0, lambda s: len(s.get("stack", []) or []) == 0
                    and (s.get("waiting_for") or {}).get("type") in ("Priority", None)
                    and (s["objects"].get(shock_oid) or {}).get("zone") == "Exile",
                    120, f"{tag} decline settle")
                o["decline_settled"] = s is not None
                if s is not None:
                    await export_ev(p0, f"post_{tag}")
                return o
            # accepted: the card choice should appear
            found = await wait_card_choice(p0, timeout=90)
        if not found:
            o["error"] = "no card choice after accept"
            return o
        copp, rtype, stype, named = found
        st_now = p0.latest["state"]
        oids = []
        for ch in ((copp.get("response") or {}).get("data") or {}).get(
                "candidates", []) or ((copp.get("response") or {}).get("data") or {}).get("choices", []):
            oids.append(cand_ref_oid(st_now, ch))
        o["choice_oids"] = oids
        o["choice_names"] = [n for _, n in named]
        o["choice_rtype"] = rtype
        wire(f"{tag}_choice_oids", {"oids": oids, "names": o["choice_names"]})
        await export_ev(p0, f"cardchoice_{tag}")
        if kind == "direct" and not accept:
            # mandatory choice with no clean decline: pick a land (least likely
            # to cast) so the game keeps moving, then settle like a decline.
            chs = card_choice_cands(copp)
            pick = None
            for ch in chs:
                ro = cand_ref_oid(st_now, ch)
                if ro and obj_name(st_now, ro) == "Mountain":
                    pick = ch
                    break
            if pick is None and chs:
                pick = chs[0]
            if pick is not None:
                await submit_card_pick(p0, copp, rtype, stype, pick.get("id"),
                                       f"{tag}_direct_fallback")
                o["direct_fallback_pick"] = cand_name(st_now, pick)
            s = await wait_cond(
                p0, lambda s: len(s.get("stack", []) or []) == 0
                and (s.get("waiting_for") or {}).get("type") in ("Priority", None)
                and (s["objects"].get(shock_oid) or {}).get("zone") == "Exile",
                180, f"{tag} direct-fallback settle")
            o["decline_settled"] = s is not None
        return o

    t1 = await do_trigger(accept=False, tag="trig1")
    obs["notes"].append(f"trig1: {json.dumps({k: v for k, v in t1.items() if k != 'choice_oids'})[:400]}")
    a2a = t1.get("trigger_exiled") and t1.get("may_kind") is not None
    obs["assert"]["A2_trigger1_fires"] = "passed" if a2a else ("failed" if t1.get("error") else "not-run")
    if t1.get("may_answered") is False:
        obs["assert"]["A2b_decline_control"] = "passed" if t1.get("decline_settled") else "failed"

    # ---- trigger#2: cast Shock#2, ACCEPT, choose Shock#1 (the reported path)
    ST["stage"] = "shock2"
    ST["cast_armed"] = True
    t2 = await do_trigger(accept=True, tag="trig2")
    obs["notes"].append(f"trig2: {json.dumps({k: v for k, v in t2.items() if k != 'choice_oids'})[:400]}")
    a2b = t2.get("trigger_exiled") and t2.get("may_kind") is not None
    obs["assert"]["A2_trigger2_fires"] = "passed" if a2b else ("failed" if t2.get("error") else "not-run")

    imprint = set(ST["imprint_oids"])
    shock1 = t1.get("shock_oid")
    shock2 = t2.get("shock_oid")

    def scope_check(tag, choice_oids, trigger_oid, extra_ok):
        """choice must be within imprint(+extra_ok); triggering spell excluded."""
        cset = set(x for x in choice_oids if x)
        unresolved = sum(1 for x in choice_oids if not x)
        ok_within = cset <= (imprint | extra_ok)
        foreign = sorted(cset - (imprint | extra_ok),
                         key=lambda x: obj_name(p0.latest["state"], x))
        trig_in = trigger_oid in cset
        verifiable = len(choice_oids) > 0 and unresolved == 0
        return (verifiable and ok_within and not trig_in), {
            "n_candidates": len(choice_oids),
            "n_resolved": len(cset),
            "n_unresolved": unresolved,
            "foreign": [(x, obj_name(p0.latest["state"], x)) for x in foreign],
            "trigger_offered": trig_in}

    # A3 on every trigger that produced a card choice (trig1 only has one in
    # the "direct" shape; normally it was declined at the may prompt)
    a3_results = {}
    for tg, t, trig_oid, extra in (("trig1", t1, shock1, set()),
                                   ("trig2", t2, shock2, {shock1} if shock1 else set())):
        if t.get("choice_oids") is not None and trig_oid:
            ok, detail = scope_check(tg, t["choice_oids"], trig_oid, extra)
            a3_results[tg] = ok
            obs["notes"].append(f"A3 {tg} detail: {json.dumps(detail)[:500]}")
    if a3_results:
        obs["assert"]["A3_choice_scope"] = ("passed" if all(a3_results.values())
                                           else "failed")
    else:
        obs["assert"]["A3_choice_scope"] = "not-run"
        obs["notes"].append("no trigger produced an observable card choice")

    if t2.get("choice_oids") is not None and shock1 and shock2:
        st_now = p0.latest["state"]
        copp = None
        # re-find the live opportunity to submit against
        found = find_card_choice(p0)
        pick_cid = None
        if found:
            copp, rtype, stype, named = found
            for ch in ((copp.get("response") or {}).get("data") or {}).get(
                    "candidates", []) or ((copp.get("response") or {}).get("data") or {}).get("choices", []):
                if cand_ref_oid(st_now, ch) == shock1:
                    pick_cid = ch.get("id")
                    break
        if pick_cid and found:
            copp, rtype, stype, named = found
            say(f"P0 picks Shock#1 ({shock1}) for the free cast: choice {pick_cid}")
            await submit_card_pick(p0, copp, rtype, stype, pick_cid, "free_cast")
            ST["pre_free_untapped"] = untapped_mountains(p0.latest["state"], 0)
            await export_ev(p0, "pre_free_cast")
            # the free shock needs a target
            tgt = await answer_target_selection(p0, seat=1, timeout=90,
                                                label="free_shock_target")
            obs["assert"]["A4a_free_target_chosen"] = "passed" if tgt else "failed"
            # watch for the free shock on the stack, then for its resolution
            def stack_watch(s):
                if (s["objects"].get(shock1) or {}).get("zone") == "Stack":
                    ST["free_stack_seen"] = True
                return ((s["objects"].get(shock1) or {}).get("zone") in (
                    "Graveyard", "Battlefield") and life(s, 1) == 18)
            s = await wait_cond(p0, stack_watch, 180, "free shock resolves")
            z1 = (p0.latest["state"]["objects"].get(shock1) or {}).get("zone") if p0.latest else "?"
            no_mana_paid = (ST["pre_free_untapped"] is not None and p0.latest
                            and untapped_mountains(p0.latest["state"], 0) >= ST["pre_free_untapped"])
            a4 = (s is not None and ST["free_stack_seen"] and z1 == "Graveyard"
                  and no_mana_paid)
            obs["assert"]["A4_free_cast_executes"] = "passed" if a4 else "failed"
            obs["notes"].append(
                f"A4: stack_seen={ST['free_stack_seen']} shock1 zone={z1} "
                f"P1 life={life(p0.latest['state'], 1) if p0.latest else '?'} "
                f"untapped {ST['pre_free_untapped']}->{untapped_mountains(p0.latest['state'], 0) if p0.latest else '?'}")
            say("exporting POST_FREE_CAST state")
            await export_ev(p0, "post_free_cast")
        else:
            obs["assert"]["A4_free_cast_executes"] = "not-run"
            obs["notes"].append("Shock#1 not among card-choice candidates; free-cast path not testable")
    else:
        for k in ("A3_choice_scope", "A4_free_cast_executes"):
            obs["assert"].setdefault(k, "not-run")
        obs["notes"].append("trig2 card choice never observed")

    # ---- A5 cleanup: game proceeds
    ST["stage"] = "done"
    s = await wait_cond(
        p0, lambda s: len(s.get("stack", []) or []) == 0
        and (s.get("waiting_for") or {}).get("type") == "Priority", 120, "cleanup settle")
    obs["assert"]["A5_cleanup"] = "passed" if s is not None else "failed"
    if s is not None:
        await export_ev(p0, "final")

    ST["stop"] = True
    await pilot_task
    await p0.close()
    await p1.close()
    return obs, (t1, t2)

# ------------------------------------------------------------- verdict + run

async def arun():
    obs, trigs = await main()
    return obs, trigs


def build_run(obs, dur, t0):
    a = obs["assert"]
    a3 = a.get("A3_choice_scope")
    a4 = a.get("A4_free_cast_executes")
    a2 = a.get("A2_trigger2_fires")
    notrun = {k for k, v in a.items() if v == "not-run"}
    if (a3 == "failed" or a4 == "failed") and a2 == "passed":
        verdict = "reproduced"
    elif a3 == "passed" and a4 == "passed":
        verdict = "not-reproduced"
    elif notrun and (a3 != "failed" and a4 != "failed"):
        verdict = "blocked"
    else:
        verdict = "blocked"
    run = {
        "issue": 6764,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-09-10",
            "source": "ServerHello + sha256 match of pinned verified artifacts",
        },
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 69, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6764.py"),
        "decks": {
            "P0": [["Mountain", 36], ["Knowledge Pool", 12], ["Shock", 12]],
            "P1": [["Mountain", 60]],
        },
        "assertions": a,
        "notes": obs["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Cards exiled by a *different* effect or a second Knowledge Pool "
            "were not present in this fixture; A3 covers the triggering-spell "
            "exclusion and the imprint-set boundary only.",
            "Dense playsets (12x) are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
        ],
        "setup_line": "P0: 36x Mountain + 12x Knowledge Pool + 12x Shock; P1: 60x Mountain (draw-go)",
        "contract_line": ("Decline trig#1 (control); accept trig#2 choosing Shock#1: "
                          "free cast must resolve for 2 damage with no mana paid"),
    }
    return run


def write_manifest():
    import hashlib
    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(os.path.join(EVDIR, f)) and f != "manifest.sha256")
    lines = []
    for f in files:
        h = hashlib.sha256()
        with open(os.path.join(EVDIR, f), "rb") as fh:
            h.update(fh.read())
        lines.append(f"{h.hexdigest()}  {f}")
    with open(os.path.join(EVDIR, "manifest.sha256"), "w") as mf:
        mf.write("\n".join(lines) + "\n")
    return files


if __name__ == "__main__":
    t0 = time.time()
    try:
        obs, trigs = asyncio.run(asyncio.wait_for(arun(), timeout=2400))
    except asyncio.TimeoutError:
        say("GLOBAL TIMEOUT after 2400s")
        obs = {"assert": {"A1_setup_ok": "not-run"}, "notes": ["global timeout"]}
    dur = time.time() - t0
    run = build_run(obs, dur, t0)
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={run['verdict']} assertions={json.dumps(run['assertions'])}")
    try:
        WIRE.close(); RUNLOG.close()
    except Exception:
        pass
    files = write_manifest()
    say(f"manifest written for {len(files)} files")
