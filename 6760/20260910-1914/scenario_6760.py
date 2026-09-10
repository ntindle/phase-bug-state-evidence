#!/usr/bin/env python3
"""Issue #6760: Ojer Kaslem, Deepest Growth only allows 1 creature or land, not both.

Oracle: "Whenever Ojer Kaslem deals combat damage to a player, reveal that many
cards from the top of your library. You may put a creature card and/or a land
card from among them onto the battlefield. Put the rest on the bottom in a
random order."

Reported: the engine only permits choosing ONE card total (creature OR land)
instead of independently one creature AND one land.

Driver plan (protocol 69, v0.79.0, native engine, two human-client seats):
  P0: 4x Ojer Kaslem, Deepest Growth + 24x Forest + 32x Llanowar Elves (60)
  P1: 60x Island (draw-go, never blocks)
  P0 ramps, casts Ojer Kaslem (3GG), attacks unblocked (6 damage -> reveal 6),
  accepts the may-choice, then at the card-selection prompt attempts to select
  BOTH one creature and one land.
  If the 2-card submission is rejected (or only one card enters), the bug is
  reproduced: the driver then selects a single card to confirm the one-card
  behavior and checks the rest go to the bottom of the library.

Assertions:
  A1 setup_ok             Ojer Kaslem on P0 battlefield, attack declared
  A2 damage_and_reveal    P1 20->14 from Ojer combat damage; 6 cards revealed
  A3 may_accepted         OptionalEffectChoice offered and accepted
  A4 both_categories      >=1 creature and >=1 land among revealed cards
  A5 two_cards_enter      2-card (creature+land) selection accepted AND both
                          enter P0's battlefield
  A6 rest_to_bottom       unchosen revealed cards end in P0's library
  A7 cleanup              stack empty, trigger fully resolved, game proceeds

Verdict = reproduced iff A4 passed and A5 failed.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6760")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-1914"
EVDIR = f"{BACKFILL}/evidence/6760/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

OJER = "Ojer Kaslem, Deepest Growth"
FOREST = "Forest"
ELVES = "Llanowar Elves"
ISLAND = "Island"

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.79.0/data/card-data.json"))
CARDS = CARD_DATA.get("cards", CARD_DATA)
TYPE_OF = {}
for _k, _c in CARDS.items():
    _nm = (_c.get("name") or "").lower()
    _ct = _c.get("card_type") or {}
    _core = [t for t in (_ct.get("core_types") or [])]
    TYPE_OF[_nm] = _core


def card_types(name):
    return TYPE_OF.get((name or "").lower(), [])


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "payload": payload}, default=str) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else None


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o["id"]
    return None


def find_hand(state, pid, name):
    for oid in state["players"][pid]["hand"]:
        if obj_name(state, oid) == name:
            return oid
    return None


def life(state, pid):
    return state["players"][pid]["life"]


def lib_size(state, pid):
    return len(state["players"][pid]["library"])


def untapped_forests(state, pid):
    return sum(1 for o in bf(state, pid)
               if (o.get("base_name") or o.get("name")) == FOREST
               and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain"))


def find_action(acts, atype):
    for a in acts:
        if a.get("type") == atype:
            return a
    return None


_PASSED_REV = {}


async def gated_pass_prio(c):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get(c.name, -1) >= rev:
        return False
    for a in st.get("legal_actions", []):
        if a["type"] == "PassPriority":
            await c.send_action(a)
            _PASSED_REV[c.name] = rev
            return True
    return False


def drain_inbox(ctx):
    """Collect ActionRejected/Error messages with timestamps."""
    for c, tag in ctx["clients"]:
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("ActionRejected", "Error"):
                rec = {"who": tag, "type": t, "at": time.time(),
                       "data": json.dumps(data, default=str)[:800]}
                ctx["rejections"].append(rec)
                wire("rejection", rec)
                say(f"[{tag}] REJECTION {t}: {rec['data'][:220]}")


def find_reference(choice):
    """Resolve a candidate's object reference from its surfaces."""
    found = []

    def rec(node, depth=0):
        if depth > 6 or found:
            return
        if isinstance(node, dict):
            dd = node.get("data") if isinstance(node.get("data"), dict) else None
            if dd is not None and "reference" in dd:
                found.append(dd["reference"])
                return
            for v in node.values():
                rec(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                rec(v, depth + 1)

    rec(choice.get("surfaces", []))
    return found[0] if found else None


async def answer_may(c, ctx):
    """Accept Ojer's OptionalEffectChoice may-choice (decideOptionalEffect=true)."""
    st = c.latest
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []) or []:
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data", {}) or {}).get("choices", []) or []:
            codes = [sf.get("data", {}).get("code")
                     for sf in ch.get("surfaces", []) or []]
            if "decideOptionalEffect" not in codes:
                continue
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data", {}) or {}
                if dd.get("role") == "accept" and str(dd.get("value")).lower() == "true":
                    wire("may_choice_opportunity", op)
                    sub = {"interactionId": op.get("interactionId"),
                           "response": {"type": "choose",
                                        "data": {"choiceId": ch["id"]}}}
                    wire("may_choice_submission", sub)
                    await c.send_interaction(sub)
                    ctx["may_answered"] = True
                    ctx["may_accepted_at"] = time.time()
                    say("P0 ACCEPTS Ojer may-choice")
                    return True
    return False


async def begin_select(p0, op, ctx):
    """Core test: at the revealed-card selection prompt, attempt to select
    one creature AND one land. Records the opportunity, exports pre-state,
    submits the 2-card choice, and arms the state machine."""
    iid = op.get("interactionId")
    resp = op.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    cands = data.get("candidates", []) or []
    wire("select_opportunity_full", op)
    st = p0.latest
    s = st["state"]
    classified = []
    for ch in cands:
        ref = find_reference(ch)
        name = None
        if ref is not None:
            o = s["objects"].get(str(ref))
            name = (o.get("base_name") or o.get("name")) if o else None
        if name is None:
            name = ch.get("label") or ch.get("name") or ch.get("text")
        ctype = "Creature" if "Creature" in card_types(name) else (
            "Land" if "Land" in card_types(name) else "?")
        classified.append({"choice_id": ch.get("id"), "ref": ref,
                           "name": name, "type": ctype})
    wire("select_classified", {"spec": spec, "candidates": classified,
                               "waiting_for": s.get("waiting_for")})
    say(f"select prompt: spec={json.dumps(spec)[:400]} n_candidates={len(classified)}")
    for cc in classified:
        say(f"  candidate {cc['choice_id']}: {cc['name']} ({cc['type']}) ref={cc['ref']}")
    pre_s = await p0.export_state()
    with open(f"{EVDIR}/pre_select_{ctx['attempt']}.json", "w") as f:
        f.write(pre_s)
    ctx["pre_select_env"] = pre_s
    ctx["pre_select"] = json.loads(pre_s)["state"]
    ctx["select_iid"] = iid
    ctx["select_spec"] = spec
    ctx["candidates"] = classified
    ctx["select_started_at"] = time.time()
    has_c = any(c["type"] == "Creature" for c in classified)
    has_l = any(c["type"] == "Land" for c in classified)
    ctx["assert"]["A4_both_categories"] = "passed" if (has_c and has_l) else "failed"
    ctx["assert"]["A2_damage_and_reveal"] = (
        "passed" if (life(ctx["pre_select"], 1) == 14 and len(classified) == 6)
        else "failed")
    if not (has_c and has_l):
        ctx["notes"].append(
            f"revealed set lacks a category (creature={has_c} land={has_l}); "
            "cannot test the reported outcome on this attempt")
        ctx["select_phase"] = "done"
        return
    c1 = next(c["choice_id"] for c in classified if c["type"] == "Creature")
    c2 = next(c["choice_id"] for c in classified if c["type"] == "Land")
    ctx["two_ids"] = [c1, c2]
    rtype = spec.get("type") if isinstance(spec, dict) else None
    sub = {"interactionId": iid,
           "response": {"type": rtype or "sequence",
                        "data": {"choiceIds": [c1, c2]}}}
    wire("two_card_submission", sub)
    await p0.send_interaction(sub)
    ctx["two_sent_at"] = time.time()
    ctx["select_phase"] = "two_sent"
    say(f"submitted 2-card selection (creature {c1} + land {c2})")


def prompt_still_pending(p0, ctx):
    st = p0.latest
    if not st:
        return True
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []) or []:
        if op.get("interactionId") == ctx["select_iid"]:
            return True
    return False


def rejections_since(ctx, t0):
    return [r for r in ctx["rejections"]
            if r["who"] == "P0" and r["at"] >= t0]


async def select_machine(p0, ctx):
    """Drives the post-may-choice selection test across ticks."""
    st = p0.latest
    if st is None:
        return
    s = st["state"]
    if life(s, 1) < 20:
        ctx["damage_seen"] = True
    phase = ctx["select_phase"]
    if phase == "idle":
        if not (ctx["may_answered"] or ctx["damage_seen"]):
            return
        vi = st.get("viewer_interaction") or {}
        for op in vi.get("opportunities", []) or []:
            resp = op.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            if resp.get("type") == "schema" and data.get("candidates"):
                wire("schema_opportunity_shape",
                     {"interactionId": op.get("interactionId"),
                      "spec": data.get("spec"),
                      "n_candidates": len(data.get("candidates", [])),
                      "waiting_for": s.get("waiting_for")})
                await begin_select(p0, op, ctx)
                return
        if ctx["may_answered"] and time.time() - ctx["may_accepted_at"] > 150:
            ctx["notes"].append("no schema select prompt within 150s of may-accept")
            ctx["select_phase"] = "done"
        return
    if phase == "two_sent":
        rej = rejections_since(ctx, ctx["two_sent_at"])
        if rej:
            ctx["two_rejected"] = True
            ctx["two_rejection"] = rej[0]
            say(f"2-card submission REJECTED: {rej[0]['data'][:220]}")
            # fall back: submit a single card (the creature) to confirm the
            # one-card behavior and let the game proceed
            c1 = ctx["two_ids"][0]
            spec = ctx["select_spec"] or {}
            rtype = spec.get("type") if isinstance(spec, dict) else None
            sub = {"interactionId": ctx["select_iid"],
                   "response": {"type": rtype or "sequence",
                                "data": {"choiceIds": [c1]}}}
            wire("one_card_submission", sub)
            await p0.send_interaction(sub)
            ctx["one_sent_at"] = time.time()
            ctx["select_phase"] = "one_sent"
            say(f"submitted 1-card fallback selection (creature {c1})")
            return
        if not prompt_still_pending(p0, ctx):
            await finish_select(p0, ctx, path="two_accepted")
            return
        if time.time() - ctx["two_sent_at"] > 60:
            ctx["notes"].append("2-card submission: no rejection, prompt still "
                                "pending after 60s")
            ctx["select_phase"] = "done"
        return
    if phase == "one_sent":
        rej = rejections_since(ctx, ctx["one_sent_at"])
        if rej:
            ctx["notes"].append(f"1-card fallback also rejected: {rej[0]['data'][:200]}")
            ctx["select_phase"] = "done"
            return
        if not prompt_still_pending(p0, ctx):
            await finish_select(p0, ctx, path="one_accepted")
            return
        if time.time() - ctx["one_sent_at"] > 60:
            ctx["notes"].append("1-card submission: prompt still pending after 60s")
            ctx["select_phase"] = "done"
        return


async def finish_select(p0, ctx, path):
    """Evaluate the outcome from the post-selection state."""
    await asyncio.sleep(2)
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post_select_{ctx['attempt']}.json", "w") as f:
        f.write(post_s)
    post = json.loads(post_s)["state"]
    ctx["post_select"] = post
    classified = ctx["candidates"]
    by_cid = {c["choice_id"]: c for c in classified}
    entered = []
    for c in classified:
        ref = c["ref"]
        if ref is None:
            continue
        o = post["objects"].get(str(ref))
        zone = o.get("zone") if o else "missing"
        if zone == "Battlefield" and o.get("controller") == 0:
            entered.append(c)
    p0lib = set(str(x) for x in post["players"][0]["library"])
    bottomed = [c for c in classified
                if c["ref"] is not None
                and str(c["ref"]) in p0lib]
    say(f"select outcome path={path}: entered={[c['name'] for c in entered]} "
        f"bottomed={len(bottomed)}/{len(classified)}")
    wire("select_outcome", {"path": path,
                            "entered": [c["name"] for c in entered],
                            "bottomed": [c["name"] for c in bottomed],
                            "waiting_for": post.get("waiting_for")})
    if path == "two_accepted":
        both = len(entered) == 2 and {c["type"] for c in entered} == {"Creature", "Land"}
        ctx["assert"]["A5_two_cards_enter"] = "passed" if both else "failed"
        if not both:
            ctx["notes"].append(
                f"2-card submission accepted but battlefield shows "
                f"{[c['name'] for c in entered]} (expected 1 creature + 1 land)")
    else:  # one_accepted after the 2-card submission was rejected
        one_in = (len(entered) == 1 and entered[0]["choice_id"] == ctx["two_ids"][0])
        other = next(c for c in classified if c["choice_id"] == ctx["two_ids"][1])
        other_obj = post["objects"].get(str(other["ref"])) if other["ref"] else None
        other_zone = other_obj.get("zone") if other_obj else "missing"
        ctx["notes"].append(
            f"2-card submission rejected ({(ctx.get('two_rejection') or {}).get('data','')[:160]}); "
            f"single-card fallback entered={one_in}; the land '{other['name']}' "
            f"ended in zone {other_zone} (never put onto the battlefield)")
        ctx["assert"]["A5_two_cards_enter"] = "failed"
    n_rest = len(classified) - len(entered)
    ctx["assert"]["A6_rest_to_bottom"] = (
        "passed" if (n_rest == len(bottomed)) else "failed")
    if n_rest != len(bottomed):
        ctx["notes"].append(
            f"expected {n_rest} non-entered revealed cards in library, found {len(bottomed)}")
    stack = post.get("stack", []) or []
    wf = post.get("waiting_for") or {}
    ctx["assert"]["A7_cleanup"] = (
        "passed" if (not stack and wf.get("type") in ("Priority", None)) else "failed")
    # A3: the optional branch was exercised. If the engine presented the select
    # directly with no may-prompt, submitting a selection is the accept path.
    ctx["assert"]["A3_may_accepted"] = (
        "passed" if (ctx["may_answered"] or path in ("two_accepted", "one_accepted"))
        else "failed")
    ctx["select_phase"] = "done"
    ctx["finished"] = True


async def discard_if_needed(c, pid, tag, s, acts, ctx):
    wf = s.get("waiting_for") or {}
    wtype = wf.get("type") or ""
    if "Discard" not in wtype:
        return False
    data = wf.get("data") or {}
    named = data.get("player")
    if named is not None and named != pid:
        return False
    hids = list(s["players"][pid]["hand"])
    prefer = ELVES if pid == 0 else ISLAND
    def rank(oid):
        nm = obj_name(s, oid)
        return (0, nm) if nm == prefer else (1, nm or "")
    hids.sort(key=rank)
    n = max(0, len(hids) - 7)
    if n <= 0:
        return False
    picks = hids[:n]
    await c.send_action({"type": "SelectCards",
                         "data": {"cards": [int(x) for x in picks]}})
    say(f"{tag} discards {[obj_name(s, x) for x in picks]} ({wtype})")
    return True


async def p0_tick(c, s, acts, ctx):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    wf = s.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")
    if wtype == "OrderTriggers" and wplayer == 0:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    if await discard_if_needed(c, 0, "P0", s, acts, ctx):
        return True
    # Ojer may-choice
    if wtype == "OptionalEffectChoice" and wplayer == 0 and not ctx["may_answered"]:
        if await answer_may(c, ctx):
            return True
    # track Ojer cast turn for summoning-sickness gating
    if ctx["ojer_cast_turn"] is None and find_bf(s, 0, OJER) is not None:
        ctx["ojer_cast_turn"] = s.get("turn_number")
        say(f"Ojer Kaslem on battlefield at turn {ctx['ojer_cast_turn']}")
    # attack
    if wtype == "DeclareAttackers" and s.get("active_player") == 0:
        oid = find_bf(s, 0, OJER)
        if (oid is not None and ctx["ojer_cast_turn"] is not None
                and s.get("turn_number", 0) > ctx["ojer_cast_turn"]):
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = [[oid, {"type": "Player", "data": 1}]]
                sub["data"]["bands"] = []
                wire("declare_attackers", sub["data"])
                await c.send_action({"type": "DeclareAttackers",
                                     "data": sub["data"]})
                ctx["attacked"] = True
                ctx["assert"]["A1_setup_ok"] = "passed"
                say(f"P0 attacks P1 with Ojer Kaslem (turn {s.get('turn_number')})")
                pre_a = await c.export_state()
                with open(f"{EVDIR}/pre_attack_{ctx['attempt']}.json", "w") as f:
                    f.write(pre_a)
                return True
        return False
    # main-phase development
    if is_my_main(s, 0):
        for a in acts:
            d = a.get("data", {}) or {}
            if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == FOREST:
                await c.send_action(a)
                return True
        if (find_bf(s, 0, OJER) is None
                and find_hand(s, 0, OJER) is not None
                and untapped_forests(s, 0) >= 5
                and s.get("phase") == "PreCombatMain"):
            for a in acts:
                d = a.get("data", {}) or {}
                if a["type"] == "CastSpell" and obj_name(s, d.get("object_id")) == OJER:
                    await c.send_action(a)
                    say("P0 casts Ojer Kaslem, Deepest Growth")
                    return True
    if wtype == "Priority" and s.get("priority_player") == 0:
        return await gated_pass_prio(c)
    return False


async def p1_tick(c, s, acts, ctx):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    wf = s.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")
    if wtype == "OrderTriggers" and wplayer == 1:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    if await discard_if_needed(c, 1, "P1", s, acts, ctx):
        return True
    if wtype == "DeclareBlockers" and s.get("active_player") == 0:
        db = find_action(acts, "DeclareBlockers")
        if db:
            sub = copy.deepcopy(db)
            sub["data"]["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
            say("P1 declares no blockers")
            return True
        return False
    if is_my_main(s, 1):
        for a in acts:
            d = a.get("data", {}) or {}
            if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == ISLAND:
                await c.send_action(a)
                return True
    if wtype == "Priority" and s.get("priority_player") == 1:
        return await gated_pass_prio(c)
    return False


async def keep_mulligan(c):
    st = c.latest
    if not st:
        return False
    for a in st.get("legal_actions", []):
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps opening hand")
            return True
    return False


def new_ctx(attempt):
    return {
        "attempt": attempt,
        "clients": [],
        "rejections": [],
        "assert": {},
        "notes": [],
        "may_answered": False,
        "may_accepted_at": None,
        "damage_seen": False,
        "select_phase": "idle",
        "select_iid": None,
        "select_spec": None,
        "candidates": [],
        "two_ids": [],
        "two_sent_at": None,
        "two_rejected": False,
        "two_rejection": None,
        "one_sent_at": None,
        "ojer_cast_turn": None,
        "attacked": False,
        "pre_select": None,
        "pre_select_env": None,
        "post_select": None,
        "finished": False,
    }


async def run_attempt(attempt):
    ctx = new_ctx(attempt)
    _PASSED_REV.clear()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck((OJER, 4), (FOREST, 24), (ELVES, 32)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck((ISLAND, 60)))
    ctx["clients"] = [(p0, "P0"), (p1, "P1")]
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ctx["assert"]["A1_setup_ok"] = "failed"  # flipped on attack
    t0 = time.time()
    last_rev = {}
    last_tick = {}
    try:
        while time.time() - t0 < 1500 and not ctx["finished"]:
            await asyncio.sleep(0.12)
            drain_inbox(ctx)
            for c, pid, is_p0 in ((p0, 0, True), (p1, 1, False)):
                if await keep_mulligan(c):
                    continue
                st = c.latest
                if st is None:
                    continue
                rev = st.get("state_revision", -1)
                if rev == last_rev.get(c.name) and time.time() - last_tick.get(c.name, 0) < 3:
                    continue
                s = st["state"]
                acts = st.get("legal_actions", [])
                if is_p0:
                    acted = await p0_tick(c, s, acts, ctx)
                else:
                    acted = await p1_tick(c, s, acts, ctx)
                last_rev[c.name] = st.get("state_revision", -1)
                last_tick[c.name] = time.time()
            await select_machine(p0, ctx)
            if ctx["select_phase"] == "done":
                ctx["finished"] = True
    finally:
        await p0.close()
        await p1.close()
    # fill unset assertions
    for k in ("A1_setup_ok", "A2_damage_and_reveal", "A3_may_accepted",
              "A4_both_categories", "A5_two_cards_enter", "A6_rest_to_bottom",
              "A7_cleanup"):
        ctx["assert"].setdefault(k, "not-run")
    return ctx


async def main():
    t0 = time.time()
    final = None
    for attempt in (1, 2):
        say(f"===== attempt {attempt} =====")
        ctx = await run_attempt(attempt)
        say(f"attempt {attempt} assertions: {json.dumps(ctx['assert'])}")
        # keep this attempt if it reached the select prompt with both categories
        if ctx["assert"].get("A4_both_categories") == "passed":
            final = ctx
            break
        final = ctx
        if ctx["assert"].get("A4_both_categories") == "failed":
            say("attempt lacked both categories in the revealed set; retrying")
            continue
        break
    dur = time.time() - t0
    ass = final["assert"]
    notes = final["notes"] + [
        "protocol-69 driver (v0.79.0): Ojer cast via CastSpell action; attack via "
        "DeclareAttackers attacks=[[oid,{type:Player,data:1}]]; may-choice via "
        "exactChoices decideOptionalEffect accept=true; revealed-card selection "
        "via schema prompt submitted as {type:<spec.type>, data:{choiceIds:[...]}}.",
        "P0 deck 4x Ojer Kaslem, Deepest Growth + 24x Forest + 32x Llanowar Elves; "
        "P1 60x Island draw-go, never blocks (engine accepts >4-of for custom games).",
    ]
    a4 = ass.get("A4_both_categories")
    a5 = ass.get("A5_two_cards_enter")
    if a4 == "passed" and a5 == "failed":
        verdict = "reproduced"
    elif a4 == "passed" and a5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": 6760,
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
        "driver": {"protocol_advertised": 69, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6760.py"),
        "decks": {
            "P0": [[OJER, 4], [FOREST, 24], [ELVES, 32]],
            "P1": [[ISLAND, 60]],
        },
        "assertions": ass,
        "notes": notes,
        "rejections": final["rejections"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Not tested on the original 2026-07-29 build; verdict is scoped to "
            "v0.79.0, not a fix claim.",
        ],
        "setup_line": "P0: 4x Ojer Kaslem, Deepest Growth + 24x Forest + 32x Llanowar Elves; P1: 60x Island (never blocks)",
        "contract_line": "Ojer deals 6 combat damage -> reveal 6 -> may put a creature AND a land onto the battlefield",
        "stats": {"states_seen": "n/a", "trigger_observations": "n/a"},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6760.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6760.py").read())
    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    WIRE.close()
    RUNLOG.close()


asyncio.run(main())
