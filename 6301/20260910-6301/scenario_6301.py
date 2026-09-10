#!/usr/bin/env python3
"""Issue #6301: [Card Bug] Force of Will: Can't be castet.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github, confirmed, priority:p0-softlock; labels: area:engine,
area:frontend, mechanic:costs): "As a response to a Fatal Push I try to cast
a Force of Will. UI hangs with `Casting...`". Expected: cast Force of Will by
paying 1 life and exiling a blue card from hand.

Oracle text (pinned card-data.json, key "force of will"):
  You may pay 1 life and exile a blue card from your hand rather than pay
  this spell's mana cost.
  Counter target spell.

Triage finding (issue comment): the failure is in the casting flow, not the
card's representation; a stall points at a decision the casting flow opens
but never resolves - most likely the choice of which blue card to exile.

Setup (native engine, two human-client seats, v0.78.0, protocol 68):
  P0: 12x Lightning Bolt, 48x Mountain. Turn 1: Mountain, cast Lightning
      Bolt targeting P1 (Bolt is the opponent spell to respond to).
  P1: 12x Force of Will, 12x Opt (blue card to exile), 36x Island.
      Responds to Bolt with Force of Will for the alternative cost.

Expected (per card text):
  E1: Bolt goes on the stack; P1 gets priority with FoW + Opt in hand.
  E2: engine offers the FoW cast; the alternative cost (pay 1 life + exile a
      blue card from hand) can be chosen.
  E3: the blue-card exile choice is presented and answerable (choose Opt).
  E4: FoW goes on the stack targeting Bolt, resolves, counters Bolt:
      Bolt never deals damage (P1 life 19, not 16), FoW and Bolt end in
      graveyards, the exiled Opt is in exile.
  E5: the game proceeds with no stuck decision.

Assertions:
  A1_setup_ok        pre-cast: Bolt on stack, P1 priority, FoW + Opt in
                     P1's hand, life 20/20
  A2_cast_offered    CastSpell for Force of Will advertised to P1 while
                     Bolt is on the stack
  A3_exile_answered  exile-from-hand choice presented with Opt as a
                     candidate and answered with a single Opt
  A4_fow_resolves    post: FoW + Bolt in graveyards, P1 life == 19,
                     exactly one Opt exiled, no Bolt damage
  A5_no_stall        no pending FoW-related decision; game proceeds

Verdict rule: reproduced iff the FoW cast stalls (a decision opened but
never resolvable, or the cast never completes engine-side) or the
alternative-cost path fails. not-reproduced iff A1..A5 all pass (the
reported UI hang is then a frontend-only symptom; browser UI is not
exercised). blocked iff setup/cast flow cannot be driven to completion.

Evidence: evidence/6301/<run-id>/pre.json (Bolt on stack, P1 priority),
post.json (after FoW resolves / stall state), run.json,
manifest.sha256, summary.png, scenario_6301.py, wire_log.jsonl,
scenario_run.log, server_excerpt.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6301"
EVDIR = f"{BACKFILL}/evidence/6301/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

FOW = "force of will"  # card-data.json key (exact)
OPT = "opt"
BOLT = "lightning bolt"
ISLAND = "island"
MOUNTAIN = "mountain"

BASICS = {MOUNTAIN: "Red", ISLAND: "Blue"}

P0_DECK = [(BOLT, 12), (MOUNTAIN, 48)]
P1_DECK = [(FOW, 12), (OPT, 12), (ISLAND, 36)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-10",
    "source": "ServerHello + sha256 match of pinned verified artifacts; "
              "server started fresh this run (runs/20260910-6301) on "
              "127.0.0.1:9374",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def lname(state, oid):
    return obj_name(state.get("objects", {}).get(str(oid), {}))


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_oids(state, pid):
    return [str(o) for o in player_of(state, pid).get("hand", [])]


def untapped_lands(state, pid):
    return [o for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and obj_name(o) in BASICS]


def stack_entries(state):
    return state.get("stack") or []


def stack_names(state):
    out = []
    for e in stack_entries(state):
        ref = (e.get("source_id") or e.get("source") or e.get("spell")
               or e.get("object_id"))
        nm = lname(state, ref) if ref else None
        out.append((e.get("id"), str(nm or "?").lower()))
    return out


def spell_on_stack(state, name):
    if any(nm == name for _sid, nm in stack_names(state)):
        return True
    # fallback: object-zone view
    return any(obj_name(o) == name and o.get("zone") == "Stack"
               for o in state.get("objects", {}).values())


def bolt_on_stack(state):
    return spell_on_stack(state, BOLT)


def bolt_stack_id(state):
    for sid, nm in stack_names(state):
        if nm == BOLT:
            return sid
    return None


def fow_cast_recorded(state):
    """FoW seen on the stack or in a graveyard (i.e., the cast completed)."""
    for _sid, nm in stack_names(state):
        if nm == FOW:
            return "stack"
    for p in state.get("players", []):
        for o in p.get("graveyard", []):
            if lname(state, o) == FOW:
                return "graveyard"
    return None


def exiled_opts(state, pid=None):
    return [str(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Exile" and obj_name(o) == OPT
            and (pid is None or o.get("owner") == pid or o.get("controller") == pid)]


def in_gy(state, pid, name):
    return any(lname(state, o) == name
               for o in player_of(state, pid).get("graveyard", []))


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype, name=None, state=None):
    for a in acts:
        if a["type"] != atype:
            continue
        if name is None:
            return a
        d = a.get("data", {})
        oid = d.get("object_id") or a.get("_src_oid")
        if state is not None and lname(state, oid) == name:
            return a
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    return str(t)


def surf_names(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict):
            if d.get("name"):
                out.append(str(d["name"]).lower())
            if d.get("seat") is not None:
                out.append(f"player-seat:{d['seat']}")
    return out


def cand_ref_name(state, ch):
    """Resolve a target candidate's referenced object name (never guess from
    first-on-battlefield). Returns (ref_oid, name)."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("reference") is not None:
            roid = str(d["reference"])
            return roid, lname(state, roid)
    return None, ""


def cand_zone(state, ch):
    """Zone of a candidate: prefer the surface-advertised zone, else the
    referenced object's zone. Card-in-hand candidates (zone hand) are NOT
    target prompts even when they name a spell card."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("zone"):
            return str(d["zone"])
    roid, _ = cand_ref_name(state, ch)
    if roid:
        return get_obj(state, roid).get("zone")
    return None


def action_codes(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            out.append(((s.get("data") or {}).get("code")) or "")
    return out


def pay_value(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "value":
            d = s.get("data") or {}
            if d.get("role") == "pay":
                return str(d.get("value")).lower() == "true"
    return None


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_cast_offered", "A3_exile_answered",
            "A4_fow_resolves", "A5_no_stall")}
    obs = {"alt_cost_choice_seen": False, "exile_choice_seen": False,
           "exile_choices": [], "fow_target_choice": None,
           "bolt_targeted_p1": False, "cast_advertised_shapes": [],
           "interaction_shapes": [], "waiting_for_tail": None}
    pre_exported = False
    post_exported = False
    bolt_cast = False
    bolt_cast_pending = False
    fow_cast_attempted = False
    fow_on_stack_seen = False
    kept = {}
    submitted_interactions = set()
    shapes_logged = set()
    settle_empty = 0
    stall_note_written = False
    # mulligan state machines (hand-sig guard prevents deciding twice on the
    # same pre-mulligan hand when a tick re-runs on a stale revision)
    mull = {"P0": {"decided_sig": None, "decided_at": 0.0, "count": 0},
            "P1": {"decided_sig": None, "decided_at": 0.0, "count": 0}}
    p1_ready = False
    setup_invalid = False

    def hand_sig(state, who):
        return tuple(sorted(hand_oids(state, who)))

    def mulligan_decide(c, state, who, keep_fn, max_mulls=5):
        """Decide keep/mulligan on a fresh hand only. Returns the action to
        submit, or None if this hand was already decided (wait for new state).
        """
        m = mull[c.name]
        sig = hand_sig(state, who)
        if (sig == m["decided_sig"]
                and time.time() - m["decided_at"] < 30):
            return None  # already decided on this hand; wait for new state
        hn = hand_names(state, who)
        m["decided_sig"], m["decided_at"] = sig, time.time()
        if keep_fn(hn, m["count"]) or m["count"] >= max_mulls:
            kept[c.name] = True
            say(f"{c.name} keeps "
                f"(fow={FOW in hn}, opt={OPT in hn}, bolt={BOLT in hn}, "
                f"lands={sum(1 for n in hn if n in BASICS)}, "
                f"mulls={m['count']})")
            return {"type": "MulliganDecision",
                    "data": {"choice": {"type": "Keep"}}}
        m["count"] += 1
        say(f"{c.name} mulligans #{m['count']}")
        return {"type": "MulliganDecision",
                "data": {"choice": {"type": "Mulligan"}}}

    def verify_p1_hand(state):
        """After P1's mulligan completes, confirm FoW + Opt are in hand."""
        nonlocal p1_ready, setup_invalid
        if p1_ready or setup_invalid:
            return
        hn = hand_names(state, 1)
        if FOW in hn and OPT in hn:
            p1_ready = True
            say(f"P1 hand verified: FoW + Opt present "
                f"({len(hn)} cards)")
        else:
            setup_invalid = True
            notes.append(f"P1 final hand lacks FoW/Opt "
                         f"(hand={[n for n in hn][:8]}) -- setup invalid")
            say("P1 hand verification FAILED; setup invalid")

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    async def scan_interactions(st, who):
        """Handle target selection and cost/card choices. Priority menus
        (action-code surfaces) are never touched. Only schema-type prompts
        are treated as target prompts."""
        nonlocal bolt_cast_pending, fow_cast_attempted
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        c = p0 if who == "P0" else p1
        state = st["state"]
        hand = set(hand_names(state, 1)) if who == "P1" else set()
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            chs = data.get("choices") or data.get("candidates") or []
            texts = [choice_text(ch) for ch in chs]
            blob = " // ".join(texts)
            codes = set()
            for ch in chs:
                codes.update(action_codes(ch))
            key = (who, rtype, blob[:100], tuple(sorted(codes))[:3])
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{who}] interaction rtype={rtype} n={len(chs)} "
                    f"codes={sorted(codes)[:4]} choices=[{blob[:220]}]")
                wire("interaction_shape", {"who": who, "rtype": rtype,
                                           "interaction": opp})
                obs["interaction_shapes"].append(
                    f"{who}/{rtype}/{sorted(codes)[:3]}/{blob[:120]}")
            if iid in submitted_interactions:
                continue
            if rtype == "exactChoices":
                sub_type, data_key, multi = "choose", "choiceId", False
            else:
                spec = data.get("spec") or {}
                spec_type = spec.get("type") if isinstance(spec, dict) else None
                sub_type = spec_type or "sequence"
                data_key, multi = "choiceIds", True

            def submit(picks):
                payload = {data_key: ([p["id"] for p in picks] if multi
                                      else picks[0]["id"])}
                return {"interactionId": iid,
                        "response": {"type": sub_type, "data": payload}}

            # --- optional/alternative cost decision (e.g. FoW's
            # "pay 1 life and exile a blue card"): pick the choice whose
            # value surface says role=pay, value=true ---
            if "decideOptionalCost" in codes:
                if who == "P1":
                    pick = next((ch for ch in chs
                                 if "decideOptionalCost" in action_codes(ch)
                                 and pay_value(ch) is True), None)
                    if pick is None:
                        notes.append(f"decideOptionalCost prompt without a "
                                     f"pay=true choice (iid={iid})")
                        wire("optcost_no_pay",
                             {"iid": iid, "interaction": opp})
                        continue
                    say(f"[P1] alternative cost -> PAY "
                        f"(decideOptionalCost pay=true)")
                    wire("optcost_pay", {"submission": submit([pick])})
                    obs["alt_cost_choice_seen"] = True
                    await c.send_interaction(submit([pick]))
                    submitted_interactions.add(iid)
                    acted = True
                continue
            # priority menus: never touch
            if codes:
                continue
            # --- cost choice: pick the alternative cost ---
            low = blob.lower()
            if ("exile" in low and "life" in low) or "rather than pay" in low:
                pick = next((ch for ch in chs
                             if "exile" in choice_text(ch).lower()
                             and "life" in choice_text(ch).lower()), None)
                if who == "P1" and pick is not None:
                    obs["alt_cost_choice_seen"] = True
                    say(f"[P1] cost choice -> alternative cost "
                        f"({choice_text(pick)[:80]!r})")
                    wire("alt_cost_choice",
                         {"submission": submit([pick]), "blob": blob[:300]})
                    await c.send_interaction(submit([pick]))
                    submitted_interactions.add(iid)
                    acted = True
                continue
            # --- classify candidates ---
            names = {choice_text(ch).lower() for ch in chs} | \
                {n for ch in chs for n in surf_names(ch)}
            is_target_prompt = (
                any(n.startswith("player-seat:")
                    for ch in chs for n in surf_names(ch))
                or any((cand_zone(state, ch) or "").lower() == "stack"
                       for ch in chs))
            is_card_choice = bool(names & hand) and not is_target_prompt
            # --- card choice (exile a blue card): pick exactly one Opt ---
            if is_card_choice and who == "P1":
                obs["exile_choice_seen"] = True
                opts = [ch for ch in chs
                        if choice_text(ch).lower() == OPT
                        or OPT in surf_names(ch)]
                obs["exile_choices"] = sorted(names)[:12]
                if not opts:
                    notes.append(f"exile choice offered but no Opt candidate "
                                 f"(candidates={sorted(names)[:12]})")
                    wire("exile_no_opt", {"interaction": opp})
                    continue
                pick = opts[0]
                say(f"[P1] exile choice -> Opt (of {len(opts)} opt candidates)")
                wire("exile_choice", {"submission": submit([pick]),
                                      "available": sorted(names)[:12]})
                obs["exile_answered_opt"] = True
                await c.send_interaction(submit([pick]))
                submitted_interactions.add(iid)
                acted = True
                continue
            # --- target selection: only schema-type prompts ---
            if rtype != "schema":
                continue
            if who == "P0" and bolt_cast_pending:
                # Bolt's target: choose P1 (player-seat:1)
                pick = next((ch for ch in chs
                             if "player-seat:1" in surf_names(ch)), None)
                if pick is None:
                    notes.append("bolt target prompt without P1 candidate")
                    wire("bolt_no_p1", {"interaction": opp})
                    continue
                say("[P0] bolt target -> P1")
                wire("bolt_target", {"submission": submit([pick])})
                await c.send_interaction(submit([pick]))
                submitted_interactions.add(iid)
                bolt_cast_pending = False
                obs["bolt_targeted_p1"] = True
                acted = True
                continue
            if who == "P1":
                # FoW's target: the Bolt spell on the stack
                pick = next((ch for ch in chs
                             if cand_ref_name(state, ch)[1] == BOLT), None)
                if pick is None:
                    pick = next((ch for ch in chs
                                 if (cand_zone(state, ch) or "").lower()
                                 == "stack"), None)
                if pick is None:
                    pick = next((ch for ch in chs
                                 if BOLT in choice_text(ch).lower()), None)
                if pick is None:
                    notes.append(f"FoW target prompt without Bolt candidate "
                                 f"(candidates={sorted(names)[:12]})")
                    wire("fow_no_bolt", {"interaction": opp})
                    continue
                say("[P1] FoW target -> Lightning Bolt on stack")
                wire("fow_target", {"submission": submit([pick])})
                await c.send_interaction(submit([pick]))
                submitted_interactions.add(iid)
                obs["fow_target_choice"] = "lightning bolt"
                acted = True
                continue
        return acted

    def evaluate():
        pre_st = load_env(f"{EVDIR}/pre.json")
        post_st = load_env(f"{EVDIR}/post.json")
        # A1: pre-cast setup
        if pre_st is not None:
            ok = (bolt_on_stack(pre_st)
                  and pre_st.get("priority_player") == 1
                  and FOW in hand_names(pre_st, 1)
                  and OPT in hand_names(pre_st, 1)
                  and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"pre.json: bolt_on_stack={bolt_on_stack(pre_st)}, "
                         f"pp={pre_st.get('priority_player')}, "
                         f"P1hand_fow={FOW in hand_names(pre_st,1)}, "
                         f"P1hand_opt={OPT in hand_names(pre_st,1)}, "
                         f"life={life_of(pre_st,0)}/{life_of(pre_st,1)}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("pre.json missing (Bolt never reached the stack "
                         "with P1 priority)")
        # A2: cast offered
        if obs["cast_advertised_shapes"]:
            ass["A2_cast_offered"] = "passed"
            notes.append(f"CastSpell(FoW) advertised to P1 with Bolt on "
                         f"stack ({len(obs['cast_advertised_shapes'])} sighting(s))")
        else:
            ass["A2_cast_offered"] = "failed"
            notes.append("CastSpell for Force of Will never advertised to P1 "
                         "while Bolt was on the stack -- matches the reported "
                         "'can't be cast' symptom")
        # A3: exile choice answered
        if obs.get("exile_answered_opt"):
            ass["A3_exile_answered"] = "passed"
            notes.append("exile-from-hand choice presented; exactly one Opt chosen")
        elif obs["exile_choice_seen"]:
            ass["A3_exile_answered"] = "failed"
            notes.append("exile choice presented but not answerable with an Opt")
        else:
            ass["A3_exile_answered"] = "failed"
            notes.append("no exile-from-hand choice was presented for the "
                         "alternative cost")
        # A4: FoW resolves and counters Bolt
        if post_st is not None and pre_st is not None:
            fow_gy = in_gy(post_st, 1, FOW)
            bolt_gy = in_gy(post_st, 0, BOLT)
            p1_life = life_of(post_st, 1)
            exiled = exiled_opts(post_st, 1)
            ok = (fow_gy and bolt_gy and p1_life == 19 and len(exiled) == 1
                  and life_of(post_st, 0) == 20)
            if ok:
                ass["A4_fow_resolves"] = "passed"
                notes.append(f"post.json: FoW and Bolt in graveyards, P1 life "
                             f"19 (paid 1, no Bolt damage), 1 Opt exiled, "
                             f"P0 life 20 -- Bolt countered as printed")
            else:
                ass["A4_fow_resolves"] = "failed"
                notes.append(f"post.json: fow_gy={fow_gy} bolt_gy={bolt_gy} "
                             f"P1life={p1_life} P0life={life_of(post_st,0)} "
                             f"exiled_opts={len(exiled)} -- "
                             f"counter/alternative-cost outcome wrong")
        else:
            ass["A4_fow_resolves"] = "failed"
            notes.append("A4 unevaluable (missing pre/post state)")
        # A5: no stuck decision
        if post_st is not None:
            wf = (post_st.get("waiting_for") or {}).get("type")
            stack_empty = len(post_st.get("stack") or []) == 0
            obs["waiting_for_tail"] = wf
            if stack_empty and life_of(post_st, 1) == 19:
                ass["A5_no_stall"] = "passed"
                notes.append(f"post.json: stack empty, waiting_for={wf}; "
                             f"game proceeded past the FoW response")
            else:
                ass["A5_no_stall"] = "failed"
                notes.append(f"post.json: stack_empty={stack_empty} "
                             f"waiting_for={wf} -- decision may be stuck")
        else:
            ass["A5_no_stall"] = "failed"
            notes.append("A5 unevaluable (missing post state)")
        # verdict
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif (ass["A2_cast_offered"] == "passed"
              and ass["A3_exile_answered"] == "passed"
              and ass["A4_fow_resolves"] == "passed"
              and ass["A5_no_stall"] == "passed"):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("FoW alternative-cost cast failed to complete "
                         "engine-side -- matches the reported 'can't be cast' stall")
        return verdict

    def load_env(path):
        try:
            with open(path) as f:
                return json.loads(f.read())["state"]
        except Exception:
            return None

    async def finish():
        nonlocal post_exported, stall_note_written
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        # server log excerpt
        try:
            with open(f"{BACKFILL}/runs/{RUN_ID}/server.log") as f:
                lines = f.readlines()
            tail = [l for l in lines
                    if "error" in l.lower() or "panic" in l.lower()
                    or "warn" in l.lower()][-40:]
            with open(f"{EVDIR}/server_excerpt.log", "w") as f:
                f.writelines(tail)
        except Exception as e:
            notes.append(f"server excerpt failed: {e}")
        verdict = evaluate()
        run = {
            "issue": 6301,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9374,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6301.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x copies per card is a test-harness convenience (engine "
                "accepts >4-of for custom games); exercised behavior is the "
                "shipped card text.",
                "The reported symptom is a UI hang ('Casting...'); this run "
                "tests the engine casting flow only. A passing engine path "
                "locates the bug in the frontend; a failing engine path "
                "reproduces it in the engine.",
                "Lightning Bolt stands in for the reported Fatal Push (any "
                "opponent spell to respond to); the bug is in FoW's cost flow, "
                "not the responded-to spell.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x lightning bolt + 48x mountain; P1: 12x force "
                          "of will + 12x opt + 36x island. Turn 1: P0 plays a "
                          "Mountain and casts Lightning Bolt targeting P1; P1 "
                          "responds with Force of Will for its alternative cost.",
            "contract_line": "P1 casts Force of Will for 1 life + exiling one "
                             "Opt: expect alt-cost choice, answerable exile "
                             "choice, FoW on the stack targeting Bolt, Bolt "
                             "countered (P1 at 19 life, one Opt exiled, FoW "
                             "and Bolt in graveyards), game proceeds.",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            f.write(json.dumps(run, indent=1))
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    async def bottom_cards(c, state, who, keep_names):
        pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
        count = 1
        for p in pending:
            if p.get("player") == who:
                ph = p.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    count = int(ph.get("count", 1))
        hand = hand_oids(state, who)

        def key(oid):
            nm = lname(state, oid)
            if nm in BASICS:
                return (0, nm)
            if nm not in keep_names:
                return (1, nm)
            return (3, nm)
        picks = sorted(hand, key=key)[:count]
        kept[f"{c.name}_bottomed"] = True
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{c.name} bottoms {count}: {[lname(state, x) for x in picks]}")

    async def p0_tick(st, acts, state):
        nonlocal bolt_cast, bolt_cast_pending
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            sub = mulligan_decide(
                p0, state, 0,
                lambda hn, mc: BOLT in hn and sum(1 for n in hn if n in BASICS) >= 1)
            if sub:
                await submit_as_is(p0, sub)
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                await bottom_cards(p0, state, 0, {BOLT})
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
                return
        if await scan_interactions(st, "P0"):
            return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        phase = state.get("phase")
        main_phase = phase in ("PreCombatMain", "PostCombatMain")
        hn = hand_names(state, 0)
        lands_played = player_of(state, 0).get("lands_played_this_turn", 0)
        if not p1_ready:
            # P1's hand not verified yet (mulligans in flight): just pass.
            for a in acts:
                if a["type"] == "PassPriority":
                    await submit_as_is(p0, a)
                    return
            return
        if not bolt_cast and main_phase:
            ca = find_action(acts, "CastSpell", BOLT, state)
            if ca and untapped_lands(state, 0):
                if lands_played == 0:
                    for a in acts:
                        if a["type"] == "PlayLand":
                            await submit_as_is(p0, a)
                            say("P0 plays Mountain")
                            return
                say("P0 casts Lightning Bolt targeting P1")
                wire("cast_bolt", ca)
                await submit_as_is(p0, ca)
                bolt_cast_pending = True
                bolt_cast = True
                return
        if not bolt_cast:
            for a in acts:
                if a["type"] == "PlayLand" and lands_played == 0:
                    await submit_as_is(p0, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        nonlocal fow_cast_attempted, fow_on_stack_seen, pre_exported, \
            post_exported, settle_empty
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            sub = mulligan_decide(
                p1, state, 1,
                lambda hn, mc: FOW in hn and OPT in hn)
            if sub:
                await submit_as_is(p1, sub)
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P1_bottomed"):
                await bottom_cards(p1, state, 1, {FOW, OPT})
                verify_p1_hand(state)  # bottoming only removes; verify now
                return
        # mulligan phase over (kept with no bottom prompt, or bottomed):
        # verify the final hand before P0 casts Bolt
        if kept.get("P1") and not kept.get("P1_bottomed"):
            verify_p1_hand(state)
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        # SelectCards outside mulligan (e.g. exile-a-blue-card as a legal
        # action rather than an interaction): choose a single Opt.
        if wtype == "SelectCards" and kept.get("P1_bottomed"):
            opts = [oid for oid in hand_oids(state, 1)
                    if lname(state, oid) == OPT]
            if opts:
                say(f"[P1] SelectCards -> exiling one Opt ({opts[0]})")
                wire("selectcards_exile", {"cards": [int(opts[0])]})
                obs["exile_choice_seen"] = True
                obs["exile_answered_opt"] = True
                await submit_as_is(p1, {"type": "SelectCards",
                                        "data": {"cards": [int(opts[0])]}})
                return
            notes.append("SelectCards pending for P1 but no Opt in hand")
            wire("selectcards_no_opt",
                 {"hand": hand_names(state, 1),
                  "waiting_for": state.get("waiting_for")})
            return
        if await scan_interactions(st, "P1"):
            return
        # pre export: Bolt on stack, P1 priority, FoW not yet attempted.
        # Exports only work for PlayerId(0) in single-user mode, so p0
        # exports even though this is P1's tick. Never let a failed export
        # block the cast attempt below.
        if (bolt_on_stack(state) and not pre_exported
                and not fow_cast_attempted
                and state.get("priority_player") == 1):
            say("PRE: Bolt on stack, P1 priority; exporting pre.json")
            try:
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                notes.append("pre.json exported (Bolt on stack, P1 priority)")
            except Exception as e:
                notes.append(f"pre export failed: {e}")
            pre_exported = True
            return  # next tick attempts the cast with fresh actions
        # post export: FoW cast recorded and stack settled
        if fow_cast_attempted and not post_exported:
            if len(stack_entries(state)) == 0:
                settle_empty += 1
            else:
                settle_empty = 0
            if fow_cast_recorded(state):
                fow_on_stack_seen = True
            if settle_empty >= 2:
                say(f"POST: stack settled; exporting post.json "
                    f"(fow_seen={fow_on_stack_seen})")
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    notes.append("post.json exported (stack settled)")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
                post_exported = True
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        # ---- P1 priority: respond to Bolt with Force of Will ----
        if bolt_on_stack(state) and not fow_cast_attempted:
            ca = find_action(acts, "CastSpell", FOW, state)
            if ca:
                say(f"P1 casts Force of Will in response to Bolt")
                wire("cast_fow", ca)
                obs["cast_advertised_shapes"].append(
                    json.dumps(ca.get("data", {}), default=str)[:200])
                await submit_as_is(p1, ca)
                fow_cast_attempted = True
                return
            # No CastSpell yet: the full action list may not have arrived.
            # Do NOT pass priority here; wait for fresh actions. The stall
            # watchdog bounds this wait (a genuine never-offered cast is
            # the reported bug, not a reason to let Bolt resolve).
            codes = sorted({a["type"] for a in acts})
            if not obs.get("no_fow_logged"):
                obs["no_fow_logged"] = True
                say(f"[P1] Bolt on stack but no CastSpell(FoW) advertised; "
                    f"actions={codes} (holding priority)")
                wire("no_fow_castspell",
                     {"actions": codes,
                      "hand": hand_names(state, 1),
                      "waiting_for": state.get("waiting_for")})
            return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stall_deadline = None
    while time.time() - t0 < 1200:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if setup_invalid:
            say("setup invalid (P1 hand lacks FoW+Opt); finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)[:6]} "
                f"P1hand={hand_names(s, 1)[:6]} "
                f"life={life_of(s, 0)}/{life_of(s, 1)} "
                f"stack={stack_names(s)} bolt_cast={bolt_cast} "
                f"fow_attempted={fow_cast_attempted} fow_seen={fow_on_stack_seen} "
                f"pre={pre_exported} post={post_exported}")
        # stall detection: FoW cast attempted but never recorded, or
        # Bolt on stack with P1 priority but no cast path for 300s
        p0_state = p0.latest["state"] if p0.latest else None
        # early exit: Bolt resolved without FoW ever being cast -> the cast
        # was never possible engine-side; capture post and finish.
        if (pre_exported and not fow_cast_attempted and p0_state is not None
                and not bolt_on_stack(p0_state)
                and len(stack_entries(p0_state)) == 0
                and not post_exported):
            notes.append("Bolt resolved without Force of Will being cast; "
                         "the alternative-cost cast was never available")
            say("bolt resolved unanswered; finishing")
            await finish()
            return
        if (((fow_cast_attempted and not fow_on_stack_seen)
                or (p0_state is not None and bolt_on_stack(p0_state)
                    and pre_exported and not fow_cast_attempted))
                and stall_deadline is None):
            stall_deadline = time.time() + 300
            say("stall watchdog armed (300s)")
        if stall_deadline and time.time() > stall_deadline and not stall_note_written:
            s = p0.latest["state"]
            notes.append(f"STALL: 300s without FoW completing; "
                         f"waiting_for={(s.get('waiting_for') or {}).get('type')}, "
                         f"stack={stack_names(s)}, "
                         f"fow_cast_attempted={fow_cast_attempted}, "
                         f"fow_recorded={fow_cast_recorded(s)}")
            stall_note_written = True
            say("stall watchdog fired; capturing post and finishing")
            await finish()
            return
    notes.append("global timeout (1200s) hit before assertions resolved")
    await finish()


asyncio.run(main())
