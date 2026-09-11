#!/usr/bin/env python3
"""Issue #6873: cards with "reveal a <type> card from your hand or pay {N}"
as an additional cost softlock the game (Wren's Run Vanquisher reported).

Oracle: "As an additional cost to cast this spell, reveal an Elf card from
your hand or pay {3}." Card data is fully parsed (additional_cost =
Choice[Reveal(Elf,1) | Mana{3}]), so this is a runtime/interaction failure,
not a parser gap.

Plan (two human seats, native engine, v0.80.0 / protocol 69):
  GAME A (reported branch): P0 casts Wren's Run Vanquisher with a Llanowar
          Elves in hand and chooses the REVEAL branch of the additional
          cost. Expected: the reveal choice is offered, the Elf is
          selectable as the revealed card, the cast completes, the
          Vanquisher enters the battlefield, and the game proceeds.
  GAME B (control): same setup, but P0 chooses the PAY {3} branch.
          Expected: cast completes normally.

Behavioral contract:
  A1 setup_ok            P0 main phase, Vanquisher + Elf in hand, >=3
                         untapped Forests (game A) / >=6 (game B)
  A2 cost_prompted       an additional-cost decision for P0 was offered
                         during the cast
  A3 reveal_answerable   (game A) the reveal branch was offered AND
                         answered with the Elf, zero rejections of that path
  A4 cast_completes      Vanquisher on P0 battlefield, stack empty, game
                         proceeding (no perpetual-stack stall)
  A5 control_pay         (game B) the pay-{3} branch completes, Vanquisher
                         on the battlefield
  A6 no_softlock         no 90s stall with an unanswered P0 decision

Verdict: reproduced iff A1 passes and (A2 fails with the cast stuck, or A3
fails / stall observed on the reveal branch). not-reproduced iff the reveal
branch completes (A2/A3/A4 pass) and the control completes.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6873"
EVDIR = f"{BACKFILL}/evidence/6873/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

VANQ = "Wren's Run Vanquisher"
ELF = "Llanowar Elves"
FOREST = "Forest"
LANDS = (FOREST,)

ST = {}
SUBMITTED = set()
MULLS = {}
SHAPES = set()
WF_SEEN = []
LAST_SUBMIT = {"iid": None}
C0 = None


def reset(mode):
    ST.clear()
    ST.update({
        "mode": mode, "stage": "SETUP",
        "cast_submitted_at": None, "cost_prompt_at": None,
        "reveal_offered": False, "pay_offered": False,
        "reveal_answered": False, "pay_answered": False,
        "cost_decision_waits": 0, "cost_actionable_waits": 0,
        "stall_at": None, "stall_kind": None,
        "vanq_oid": None, "on_stack": False, "resolved": False,
        "pre_exported": False, "stop": False, "retry": False,
        "rejections": [], "cost_opps_logged": 0,
    })
    SUBMITTED.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    LAST_SUBMIT.update({"iid": None})


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


def perm_oids(state, pid, name):
    return [str(oid) for oid, o in bf(state, pid) if oname(o) == name]


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


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
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "mode": ST.get("mode"),
                             "stage": ST.get("stage")})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "mode": ST.get("mode"), "stage": ST.get("stage")})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "mode": ST.get("mode"),
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
                              "mode": ST.get("mode"),
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
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
        ref, seat = None, None
        texts = []
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if "seat" in d:
                    seat = d["seat"]
            for k in ("text", "label", "value"):
                v = (s.get("data") or {}).get(k) if isinstance(
                    s.get("data"), dict) else None
                if isinstance(v, str) and v:
                    texts.append(v)
        o = state["objects"].get(str(ref), {}) if ref else {}
        blob = " ".join([ch.get("text") or "", ch.get("label") or ""] +
                        texts)
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "text": blob.strip()})
    return out


def classify_cost_prompt(opp, state):
    """Return (reveal_cands, pay_cands, other) for an additional-cost
    opportunity. Reveal candidates reference Hand-zone cards; pay
    candidates mention pay/mana or reference nothing."""
    cands = candidate_info(opp, state)
    reveal, pay, other = [], [], []
    for x in cands:
        t = (x["text"] or "").lower()
        is_reveal = ("reveal" in t or "elf" in t
                     or (x["zone"] == "Hand" and x["name"] == ELF))
        is_pay = ("pay" in t or "{3}" in t or "mana" in t)
        if is_reveal and not is_pay:
            reveal.append(x)
        elif is_pay and not is_reveal:
            pay.append(x)
        else:
            other.append(x)
    return reveal, pay, other


def build_cost_response(resp, want):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select", "text"):
        if spec_type == "text":
            return {"type": "text",
                    "data": {"value": want["choice_id"]}}
        return {"type": spec_type,
                "data": {"choiceIds": [want["choice_id"]]}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": want["choice_id"]}}
    return None


COST_CODES = {"decideOptionalCost", "cancelCast"}


def opp_action_codes(opp):
    codes = set()
    for ch in (opp.get("response") or {}).get("data", {}).get("choices", []) \
            or []:
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "action":
                code = (s.get("data") or {}).get("code")
                if code:
                    codes.add(code)
    return codes


def is_cost_opportunity(opp, wf):
    """A cost prompt iff the waiting_for is cost-typed or a choice carries
    a cost-decision action code. The Priority menu (passPriority /
    castSpell / ...) is never a cost prompt."""
    if "cost" in (wf or "").lower():
        return True
    codes = opp_action_codes(opp)
    if any("cost" in c.lower() or c in COST_CODES for c in codes):
        return True
    return False


def pick_cost_choice(opp, state, codes_by_id):
    """Choose the branch per ST mode. Never pick cancelCast (that abandons
    the cast under test). Handles both exactChoices (choices) and
    candidate-style (PayCost) prompts."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    items = data.get("choices") or data.get("candidates") or []
    reveal, pay, other = classify_cost_prompt(opp, state)
    rset = {x["choice_id"] for x in reveal}
    pset = {x["choice_id"] for x in pay}
    picks = [ch.get("id") for ch in items
             if codes_by_id.get(ch.get("id")) != "cancelCast"]
    if not picks:
        return None, "no_non_cancel_choice"
    if ST["mode"] == "reveal":
        # prefer the Elf itself among the reveal candidates, in the
        # engine's candidate order
        for x in reveal:
            if x["choice_id"] in picks and x["name"] == ELF:
                return x["choice_id"], "reveal_elf"
        for cid in picks:
            if cid in rset:
                return cid, "reveal_branch"
        for cid in picks:
            if codes_by_id.get(cid) == "decideOptionalCost":
                return cid, "decide_optional_cost"
        return picks[0], "fallback_first"
    else:
        for cid in picks:
            if cid in pset:
                return cid, "pay_branch"
        for cid in picks:
            if codes_by_id.get(cid) == "decideOptionalCost":
                return cid, "decide_optional_cost"
        return picks[0], "fallback_first"


async def handle_cost_prompt(c, state, acts, st):
    """Answer P0's pending additional-cost decision. ONLY engages while
    the Vanquisher cast is in flight (CAST_WINDOW) and the opportunity is
    cost-related (cost-typed waiting_for or a cost action-surface code).
    Never picks cancelCast."""
    if ST.get("stage") != "CAST_WINDOW":
        return False
    if wf_player(state) != 0:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    wf = wf_type(state) or ""
    made = False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        if not is_cost_opportunity(opp, wf):
            continue
        codes_by_id = {}
        for ch in chs:
            for s in ch.get("surfaces", []) or []:
                if s.get("type") == "action":
                    code = (s.get("data") or {}).get("code")
                    if code:
                        codes_by_id[ch.get("id")] = code
        shape_key = ("cost_prompt", resp.get("type"),
                     str((data.get("spec") or {}).get("type")),
                     tuple(sorted(set(codes_by_id.values()))), len(chs))
        reveal, pay, other = classify_cost_prompt(opp, state)
        if shape_key not in SHAPES:
            SHAPES.add(shape_key)
            wire("cost_prompt",
                 {"rtype": resp.get("type"),
                  "spec": data.get("spec"),
                  "action_codes": codes_by_id,
                  "reveal_cands": reveal, "pay_cands": pay,
                  "other": other, "opportunity": opp,
                  "waiting_for": state.get("waiting_for"),
                  "mode": ST["mode"]})
            say(f"[P0] cost prompt ({ST['mode']}): rtype={resp.get('type')} "
                f"codes={sorted(set(codes_by_id.values()))} "
                f"reveal={[x['choice_id'] for x in reveal]} "
                f"pay={[x['choice_id'] for x in pay]} "
                f"other={len(other)}")
        ST["cost_prompt_at"] = ST["cost_prompt_at"] or time.time()
        if reveal:
            ST["reveal_offered"] = True
        if pay:
            ST["pay_offered"] = True
        want_id, why = pick_cost_choice(opp, state, codes_by_id)
        if not want_id:
            say(f"[P0] cost prompt: {why}")
            continue
        want = {"choice_id": want_id, "ref": None}
        for x in reveal + pay + other:
            if x["choice_id"] == want_id:
                want = x
                break
        # for reveal-card selection with several hand candidates, prefer
        # the Elf explicitly
        if ST["mode"] == "reveal" and why == "reveal_branch":
            elf_cands = [x for x in reveal if x["name"] == ELF]
            if elf_cands:
                want = elf_cands[0]
                want_id = want["choice_id"]
                why += "+elf"
        resp_out = build_cost_response(resp, want)
        if not resp_out:
            say(f"[P0] cost prompt: unexpected shape {resp.get('type')}")
            continue
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        ST["cost_actionable_waits"] += 1
        if why.startswith("reveal") or why == "decide_optional_cost" \
                and ST["mode"] == "reveal":
            ST["reveal_answered"] = True
        if why.startswith("pay") or why == "decide_optional_cost" \
                and ST["mode"] == "pay":
            ST["pay_answered"] = True
        say(f"[P0] cost answer ({ST['mode']}): {why} "
            f"choice_id={want_id} ref={want.get('ref')}")
        made = True
    return made


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) in LANDS)
            keep_ok = n_lands >= 2 or MULLS[c.name] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
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
            say(f"{c.name} bottoms {count}")
            return True
    if wf_type(state) == "DiscardToHandSize":
        pend = wf_data(state)
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            keep_names = {VANQ, ELF}
            # keep one Vanquisher and (reveal mode) one Elf
            keep_oids = set()
            for name in ([VANQ, ELF] if ST.get("mode") == "reveal"
                         else [VANQ]):
                oid = find_hand(state, pid, name)
                if oid:
                    keep_oids.add(oid)
            pref = [o for o in h if oname(state["objects"][o]) in LANDS
                    and o not in keep_oids]
            pref += [o for o in h if o not in pref
                     and o not in keep_oids]
            pref += [o for o in h if o not in pref]
            picks = pref[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}")
                return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"{c.name} legend-choice submitted as-is: {a['type']}")
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
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_p0:
        # answer the additional-cost decision before anything else; never
        # pass while a cost decision is pending for P0
        if await handle_cost_prompt(c, state, acts, st):
            return True
        if ST.get("stage") == "CAST_WINDOW" and wf_player(state) == 0 \
                and "cost" in (wf_type(state) or "").lower():
            ST["cost_decision_waits"] += 1
            return False
        if is_my_main(state, pid) and ST["stage"] == "SETUP":
            if await p0_land_drop(c, pid, state, acts):
                return True
            if await p0_maybe_cast(c, pid, state, acts):
                return True
    else:
        if await p1_step(c, pid, state, acts):
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, FOREST)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


def stack_has_vanq(state):
    for e in (state.get("stack") or []):
        if VANQ in json.dumps(e, default=str):
            return True
    return False


async def p0_maybe_cast(c, pid, state, acts):
    mode = ST["mode"]
    need_mana = 3 if mode == "reveal" else 6
    if untapped_of(state, pid, FOREST) < need_mana:
        return False
    if not find_hand(state, pid, VANQ):
        return False
    if mode == "reveal" and not find_hand(state, pid, ELF):
        return False
    if ST.get("cast_submitted_at"):
        return False
    oid = find_hand(state, pid, VANQ)
    a = castspell_advertised(acts, oid)
    if not a:
        return False
    if not ST["pre_exported"]:
        await export_now(f"pre_{mode}.json")
        ST["pre_exported"] = True
        wire("pre_state", {"mode": mode,
                           "untapped_forests": untapped_of(state, pid, FOREST),
                           "hand": [oname(state["objects"][o])
                                    for o in hand_oids(state, pid)]})
    await submit_as_is(c, a)
    ST["cast_submitted_at"] = time.time()
    ST["stage"] = "CAST_WINDOW"
    say(f"[P0] casts {VANQ} ({mode} branch)")
    return True


async def p1_step(c, pid, state, acts):
    if is_my_main(state, pid):
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    return False


async def attempt(mode):
    reset(mode)
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck((VANQ, 12), (ELF, 12), (FOREST, 36)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck((FOREST, 60)))
    say(f"game {mode}: {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "mode": mode})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej:
                ST["rejections"].extend(
                    {"at": now, "who": c.name, "type": r["type"],
                     "data": r["data"]} for r in rej)
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

        if wf_type(state) == "GameOver":
            ST["retry"] = True
            ST["stop"] = True
            say("game over before sequence completed -> retry")
            continue

        if ST["stage"] == "CAST_WINDOW":
            if not ST["on_stack"] and stack_has_vanq(state):
                ST["on_stack"] = True
                say("Vanquisher on the stack")
            vanq_bf = perm_oids(state, 0, VANQ)
            if vanq_bf and not ST["resolved"]:
                ST["resolved"] = True
                ST["vanq_oid"] = vanq_bf[0]
                say(f"Vanquisher resolved on BF (oid={vanq_bf[0]})")
                await export_now(f"post_{mode}.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
            # softlock watchdog: 90s after the cast with the cast neither
            # resolved nor answered
            if ST["cast_submitted_at"] and not ST["resolved"] \
                    and now - ST["cast_submitted_at"] > 90 \
                    and not ST["stall_at"]:
                ST["stall_at"] = now
                if ST["cost_prompt_at"] is None:
                    ST["stall_kind"] = "no_cost_prompt_offered"
                elif ST["cost_actionable_waits"] == 0:
                    ST["stall_kind"] = "prompt_offered_no_actionable_submit"
                else:
                    ST["stall_kind"] = "answered_but_cast_stuck"
                say(f"STALL ({ST['stall_kind']}): 90s after cast, "
                    f"decision_waits={ST['cost_decision_waits']} "
                    f"actionable={ST['cost_actionable_waits']}")
                wire("stall", {"kind": ST["stall_kind"],
                               "wf": state.get("waiting_for"),
                               "mode": mode})
                await export_now(f"mid_stall_{mode}.json")
                ST["stop"] = True

    await p0.close()
    await p1.close()
    return not ST.get("retry")


async def main():
    results = {}
    for mode in ("reveal", "pay"):
        say(f"===== GAME {mode.upper()} =====")
        for n in range(1, 4):
            try:
                ok = await attempt(mode)
            except Exception as e:
                say(f"game {mode} attempt {n} crashed: {e!r}")
                ok = False
            if ok:
                results[mode] = dict(ST)
                break
            say(f"game {mode} attempt {n} incomplete; retrying")
        else:
            results[mode] = dict(ST)
    return results


if __name__ == "__main__":
    results = asyncio.run(main())
    print(json.dumps({m: {k: results[m][k] for k in
                          ("reveal_offered", "pay_offered",
                           "reveal_answered", "pay_answered",
                           "resolved", "stall_kind",
                           "cost_decision_waits", "cost_actionable_waits",
                           "rejections") if k in results[m]}
                      for m in results}, indent=2, default=str))
