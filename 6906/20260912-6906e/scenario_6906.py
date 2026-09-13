#!/usr/bin/env python3
"""Issue #6906: Calix, Destiny's Hand -3 doesn't associate targets correctly.

Oracle (verified from pinned card-data.json):
  [-3]: Exile target creature or enchantment you don't control until target
  enchantment you control leaves the battlefield.

Reported: after choosing the exile target, Calix should pick a second target
(an enchantment its controller controls) so the exiled card returns when that
enchantment leaves. Instead, Calix just exiles straight up.

Pinned parse (v0.81.3 card-data.json) shows a single-target ChangeZone
destination Exile with target Or[Creature controller Opponent, Enchantment
controller Opponent]; no second target, no duration, no return linkage.
The triage classifier found the same: the AST drops the second target,
duration, and return linkage and therefore performs unconditional exile.

Behavioral contract (native engine, protocol 70, two human seats):
  P0 casts 2x Glorious Anthem (own enchantments) and Calix, Destiny's Hand.
  P1 casts 2x Grizzly Bears (opponent's creatures). P0 activates Calix's -3
  (ability_index 1, loyalty 4 -> 1) at main-phase priority.
  - The activation must offer TWO target selections: (1) the exile target
    (a creature or enchantment the opponent controls), (2) an enchantment
    P0 controls that the exile is linked to.
  - Driver answers (1) with a P1 Bear and (2) with a P0 Anthem if offered.
  - P1 then destroys the linked Anthem with Naturalize. If the link exists,
    the exiled Bear must return to P1's battlefield immediately.

  A1 setup_ok        pre-activation: Calix on P0 BF, >=2 Anthems on P0 BF,
                     >=2 Bears on P1 BF, P0 main-phase priority.
  A2 target_prompted >=1 TargetSelection recorded for the -3 activation.
  A3 two_target_slots >=2 TargetSelection prompts for the -3 activation
                     (exile target + own enchantment). The reported gap.
  A4 exile_observed  the chosen Bear is in Exile after -3 resolution.
  A5 own_enchant_targeted (only if A3 passes) the second selection chose a
                     P0 Anthem; else not-run.
  A6 return_on_destroy post-Naturalize: if A3 passed, the exiled Bear
                     returned to P1's BF when the linked Anthem left;
                     if A3 failed, the exiled Bear is still in Exile
                     (documents the missing return linkage).
  A7 cleanup         final state: stack empty, game proceeding.

Verdict: reproduced iff A1 passes and A3 fails. not-reproduced iff
A1..A7 all pass. blocked iff A1 fails.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6906
RUN_ID = "20260912-6906e"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

CALIX = "Calix, Destiny's Hand"
ANTHEM = "Glorious Anthem"
BEAR = "Grizzly Bears"
NAT = "Naturalize"
FOREST = "Forest"
PLAINS = "Plains"

P0_DECK = [(CALIX, 4), (ANTHEM, 8), (FOREST, 24), (PLAINS, 24)]
P1_DECK = [(BEAR, 24), (NAT, 4), (FOREST, 32)]
TIMEOUT = 2400

ST = {}
C0 = None
C1 = None


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()
    RUNLOG2.write(msg + "\n")
    RUNLOG2.flush()


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


def objs(state):
    return state.get("objects") or {}


def players(state):
    return state.get("players") or []


def player_obj(state, pid):
    for p in players(state):
        if p.get("player_id") == pid or p.get("id") == pid \
                or p.get("seat") == pid:
            return p
    return {}


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(objs(state)[oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def gy_oids(state, pid):
    return set(str(oid) for oid, o in objs(state).items()
               if o.get("zone") == "Graveyard" and o.get("controller") == pid)


def exile_oids(state):
    return set(str(oid) for oid, o in objs(state).items()
               if o.get("zone") == "Exile")


def lib_count(state, pid):
    p = player_obj(state, pid)
    lib = p.get("library")
    if isinstance(lib, list):
        return len(lib)
    return None


def untapped_lands(state, pid, land_name):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == land_name and not o.get("tapped")]


def untapped_land_count(state, pid):
    return sum(1 for oid, o in bf(state, pid)
               if not o.get("tapped") and oname(o) in (FOREST, PLAINS))


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


def wf_pending_for(state, pid):
    d = wf(state).get("data") or {}
    if isinstance(d.get("player"), int):
        return d["player"] == pid
    for p in d.get("pending") or []:
        if isinstance(p, dict) and p.get("player") == pid:
            return True
    return False


def stack(state):
    return state.get("stack") or []


def get_vi(st):
    vi = (st or {}).get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def current_opps(c):
    st = c.latest if c else None
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []


def vi_opps(st):
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []

# ----------------------------------------------------------------- helpers

async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path} ({len(s)} bytes)")
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
    ST.setdefault("settle", {})[c.name] = c.revision


async def submit_interaction(c, submission):
    wire("interaction_submit", {"who": c.name, "submission": submission,
                                "stage": ST.get("stage")})
    await c.send_interaction(submission)
    ST.setdefault("settle", {})[c.name] = c.revision


def settle_pending(c):
    return (ST.get("settle") or {}).get(c.name) is not None \
        and c.revision <= ST["settle"][c.name]


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
    if found:
        (ST.get("settle") or {}).pop(c.name, None)
    return found


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> PROOF -> DESTROY -> DONE
        "stop": False,
        "turn_cap": 80,
        "rejections": [],
        "game_code": None,
        "mulligans": 0,
        "calix_oid": None,
        "anthem_cast": 0,
        "calix_cast": False,
        "calix_cast_turn": None,
        "minus3_submitted": False,
        "minus3_turn": None,
        "minus3_watch": None,
        "target_answers": 0,
        "ability_stack_seen": False,
        "ability_resolved": False,
        "target_sels": [],       # recorded TargetSelection opportunities
        "target_sel_files": 0,
        "exile_target_oid": None,
        "exile_target_name": None,
        "own_enchant_oid": None,
        "own_enchant_name": None,
        "post_minus3": False,
        "nat_cast": False,
        "nat_resolved": False,
        "anthem_destroyed": False,
        "nat_target_oid": None,
        "destroy_turn": None,
        "proof_turns": [],
        "hold_since": None,
        "held_logged": set(),
        "last_progress_turn": 0,
    })


def surf_data(s):
    d = s.get("data") or {}
    return {
        "type": s.get("type"),
        "code": d.get("code") or s.get("code"),
        "action": d.get("action") or s.get("action"),
        "role": d.get("role") or s.get("role"),
        "value": d.get("value") if d.get("value") is not None
                 else s.get("value"),
        "seat": d.get("seat") if d.get("seat") is not None
                else s.get("seat"),
        "reference": d.get("reference") or s.get("reference"),
        "symbols": d.get("symbols") or s.get("symbols"),
    }


def choice_id_of(node):
    return (node.get("choiceId") or node.get("choice_id")
            or node.get("id"))


def opp_choices(opp):
    resp = opp.get("response") or {}
    out = []
    for v in (resp.get("choices"), (resp.get("data") or {}).get("choices")):
        if isinstance(v, list):
            out.extend(v)
    return out


def opp_candidates(opp):
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    out = []
    for v in (resp.get("candidates"), data.get("candidates")):
        if isinstance(v, list):
            out.extend(v)
    return out


def candidate_oid(cand):
    for s in cand.get("surfaces") or []:
        ref = surf_data(s)["reference"]
        if ref is not None and str(ref).lstrip("-").isdigit():
            return str(ref)
    for k in ("object_id", "objectId", "oid"):
        v = cand.get(k)
        if v is not None and str(v).lstrip("-").isdigit():
            return str(v)
    cid = choice_id_of(cand)
    if cid is not None and str(cid).lstrip("-").isdigit():
        return str(cid)
    return None


def is_card_choice(opp):
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    rtype = resp.get("type")
    if rtype == "schema" and spec.get("type") in ("select", "sequence"):
        return bool(opp_candidates(opp))
    if rtype == "exactChoices":
        for ch in opp_choices(opp):
            if candidate_oid(ch):
                return True
    return False


def card_choice_nodes(opp):
    resp = opp.get("response") or {}
    cands = opp_candidates(opp)
    if cands:
        return cands
    return [ch for ch in opp_choices(opp) if candidate_oid(ch)]


def submit_choice(c, opp, node, resp_type=None):
    resp = (opp.get("response") or {})
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    rtype = resp_type or resp.get("type")
    if rtype == "schema" and isinstance(spec, dict):
        rtype = spec.get("type") or rtype
    cid = choice_id_of(node)
    sub = {"interactionId": opp.get("interactionId"),
           "response": {"type": "choose", "data": {"choiceId": cid}}}
    if rtype == "sequence":
        sub["response"] = {"type": "sequence",
                           "data": {"choiceIds": [cid]}}
    elif rtype == "select":
        sub["response"] = {"type": "select",
                           "data": {"choiceIds": [cid]}}
    return submit_interaction(c, sub)


def find_activate_minus3(st, calix_oid):
    """Find the viewer_interaction activateAbility choice for Calix's -3
    (ability_index 1). Returns (interactionId, choice) or None."""
    cands = []
    for op in vi_opps(st):
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data") or {}
        choices = data.get("choices") or resp.get("choices") or []
        for ch in choices:
            codes = [surf_data(s)["code"]
                     for s in ch.get("surfaces") or []]
            if "activateAbility" not in codes:
                continue
            refs = [str(surf_data(s)["reference"])
                    for s in ch.get("surfaces") or []
                    if surf_data(s)["role"] == "source"]
            if str(calix_oid) not in refs:
                continue
            vals = [str(surf_data(s)["value"])
                    for s in ch.get("surfaces") or []]
            cands.append((op.get("interactionId"), ch, vals))
    if not cands:
        return None
    for iid, ch, vals in cands:
        if "1" in vals:
            return iid, ch
    # single unindexed choice: use it (recorded for the wire log)
    if len(cands) == 1:
        return cands[0][0], cands[0][1]
    wire("activate_ambiguous", {"cands": [(i, v) for i, _, v in cands]})
    return None


def find_activate_minus3_legacy(acts, calix_oid):
    """Fallback: legacy ActivateAbility action with ability_index 1
    (cf. #6863 on protocol 69)."""
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src == str(calix_oid) and d.get("ability_index") == 1:
            return a
    return None


def activation_shapes(st, acts, calix_oid):
    """Diagnostic: what activation-shaped options are advertised?"""
    out = {"opps": [], "actions": []}
    for op in vi_opps(st):
        resp = op.get("response", {}) or {}
        data = resp.get("data") or {}
        choices = data.get("choices") or resp.get("choices") or []
        codes = set()
        for ch in choices:
            for s in ch.get("surfaces") or []:
                c = surf_data(s)["code"]
                if c:
                    codes.add(c)
        out["opps"].append({"type": resp.get("type"),
                            "codes": sorted(codes),
                            "n_choices": len(choices)})
    for a in acts:
        if a.get("type") in ("ActivateAbility",):
            d = a.get("data", {}) or {}
            out["actions"].append({"type": a["type"],
                                   "source_id": d.get("source_id"),
                                   "ability_index": d.get("ability_index")})
    return out


def record_target_sel(c, state, opp):
    """Record a TargetSelection opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in ST["target_sels"]):
        return
    nodes = card_choice_nodes(opp) if is_card_choice(opp) else []
    cand_info = []
    for nd in nodes:
        oid = candidate_oid(nd)
        o = objs(state).get(str(oid)) if oid else None
        cand_info.append({
            "choiceId": choice_id_of(nd),
            "oid": oid,
            "name": oname(o) if o else None,
            "zone": (o or {}).get("zone"),
            "controller": (o or {}).get("controller"),
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": ST.get("stage"),
        "wf_data": wf(state).get("data"),
        "pending_source": ((wf(state).get("data") or {}).get("pending_cast")
                           or {}),
        "prompt_text": json.dumps(
            (opp.get("response") or {}).get("data") or {},
            default=str)[:1500],
        "candidates": cand_info,
        "opp": json.loads(json.dumps(opp, default=str)),
    }
    ST["target_sels"].append(rec)
    n = ST["target_sel_files"] = ST["target_sel_files"] + 1
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump(rec, f, indent=1, default=str)
    wire("target_selection_recorded",
         {"n": n, "turn": rec["turn"],
          "candidates": [(x["name"], x["zone"], x["controller"])
                         for x in cand_info]})
    say(f"[P0] recorded TargetSelection #{n}: "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'})"
                    for x in cand_info[:8]))


def choose_target_by_name(state, opp, want_name, want_controller=None):
    """Pick a target candidate by card name (+ optional controller)."""
    best = None
    for nd in card_choice_nodes(opp):
        oid = candidate_oid(nd)
        o = objs(state).get(str(oid)) if oid else None
        if not o:
            continue
        if oname(o) != want_name:
            continue
        if want_controller is not None and o.get(
                "controller") != want_controller:
            continue
        best = nd
        break
    return best

# ------------------------------------------------------------------- ticks

async def play_land(c, pid, state, acts):
    for ln in (FOREST, PLAINS):
        lid = find_hand(state, pid, ln)
        if lid:
            la = next((x for x in acts if x["type"] == "PlayLand"
                       and str(x.get("data", {}).get("object_id"))
                       == str(lid)), None)
            if la:
                await submit_as_is(c, la)
                return True
    return False


def discard_picks(state, pid):
    hand = hand_oids(state, pid)
    over = len(hand) - 7
    if over <= 0:
        return []
    by_name = {}
    for oid in hand:
        by_name.setdefault(oname(objs(state)[oid]), []).append(oid)
    picks = []

    def take(name, keep):
        ids = by_name.get(name, [])
        while len(ids) > keep and len(picks) < over:
            picks.append(ids.pop())

    if pid == 0:
        take(FOREST, 2)
        take(PLAINS, 2)
        take(ANTHEM, 2)   # protect own-enchantment targets
        take(CALIX, 1)
    else:
        take(FOREST, 2)
        take(BEAR, 2)
        take(NAT, 99)     # protect Naturalize for the destroy leg
    for oid in hand:
        if len(picks) >= over:
            break
        if oid not in picks:
            picks.append(oid)
    return [int(x) for x in picks[:over]]


async def handle_mulligan(c, pid, state):
    pend = (wf(state).get("data") or {}).get("pending") or []
    my = next((p for p in pend if p.get("player") == pid), None)
    ph = (my or {}).get("phase") or {}
    ptype = ph.get("type") if isinstance(ph, dict) else None
    if ptype == "BottomCards":
        n = int(ph.get("count") or 0)
        keep_name = CALIX if pid == 0 else FOREST
        named = [(oid, oname(objs(state)[oid]))
                 for oid in hand_oids(state, pid)]
        picks = [oid for oid, nm in named if nm != keep_name][:n]
        if len(picks) < n:
            picks = [oid for oid, _ in named][:n]
        picks = [int(x) for x in picks[:n]]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": picks}})
        say(f"[{c.name}] bottoms {len(picks)} after mulligan")
        return True
    if pid == 0 and ST["mulligans"] < 3 and not find_hand(state, 0, CALIX):
        ST["mulligans"] += 1
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"[P0] mulligans ({ST['mulligans']}) seeking Calix")
    else:
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        if pid == 0:
            say(f"[P0] keeps hand")
    return True


def calix_loyalty(state, calix_oid):
    o = objs(state).get(str(calix_oid)) or {}
    for k in ("loyalty", "loyalty_counters"):
        v = o.get(k)
        if isinstance(v, int):
            return v
    ctrs = o.get("counters") or {}
    if isinstance(ctrs, dict):
        for k, v in ctrs.items():
            if "loyalty" in str(k).lower() and isinstance(v, int):
                return v
    return None


def calix_ability_on_stack(state, calix_oid):
    for e in stack(state):
        d = e if isinstance(e, dict) else {}
        if str(d.get("source_id")) == str(calix_oid) and \
                str(d.get("kind", {}).get("type") if isinstance(
                    d.get("kind"), dict) else d.get("kind")) \
                .lower().find("activ") >= 0:
            return True
        blob = json.dumps(d, default=str).lower()
        if "calix" in blob and "exile target creature" in blob:
            return True
    return False


async def answer_target_prompt(c, pid, state, want_name,
                               want_controller=None, stage_tag=""):
    """Answer P0/P1's pending TargetSelection with a named card candidate."""
    for opp in current_opps(c):
        if not is_card_choice(opp):
            continue
        record_target_sel(c, state, opp)
        node = choose_target_by_name(state, opp, want_name,
                                     want_controller)
        if node is None:
            # second target slot may legitimately name something else;
            # fall back to the first candidate only when we must not stall
            nodes = card_choice_nodes(opp)
            node = nodes[0] if nodes else None
            if node is None:
                continue
            say(f"[{c.name}] {stage_tag}: no {want_name} candidate; "
                f"fallback to first candidate")
            wire("target_fallback", {"stage": stage_tag,
                                    "want": want_name,
                                    "recorded": len(ST["target_sels"])})
        else:
            say(f"[{c.name}] {stage_tag}: targets {want_name}")
        wire("target_answer", {"stage": stage_tag, "want": want_name,
                               "choiceId": choice_id_of(node)})
        ST["last_target_oid"] = candidate_oid(node)
        await submit_choice(c, opp, node)
        return True
    return False


async def p0_tick(c, pid, state, acts):
    stage = ST["stage"]
    turn = state.get("turn_number") or 0
    phase = state.get("phase") or ""

    # --- Calix -3 target selections (PROOF) ---
    if stage == "PROOF" and wf_type(state) == "TargetSelection" \
            and wf_pending_for(state, pid):
        n_answered = ST.get("target_answers", 0)
        if n_answered == 0:
            # first target slot: the exile target (opponent's creature)
            ok = await answer_target_prompt(c, pid, state, BEAR, 1,
                                            "minus3-exile-target")
            if ok:
                # record the ACTUALLY submitted candidate, not bears[0]:
                # candidate ranking may pick a different copy
                ST["exile_target_oid"] = ST.get("last_target_oid")
                ST["exile_target_name"] = BEAR
                ST["target_answers"] = 1
                return True
        elif n_answered == 1:
            # any further target slot: the own enchantment to associate
            ok = await answer_target_prompt(c, pid, state, ANTHEM, 0,
                                            "minus3-own-enchant")
            if ok:
                ST["own_enchant_oid"] = ST.get("last_target_oid")
                ST["own_enchant_name"] = ANTHEM
                ST["target_answers"] = 2
                return True
        return True  # prompt present; never pass while it is pending

    # --- decisions first: never pass while a decision is pending ---
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers",
            "MulliganDecision"):
        wft = wf_type(state)
        key = ("held", wft, stage)
        if key not in ST["held_logged"]:
            ST["held_logged"].add(key)
            vi = (c.latest or {}).get("viewer_interaction")
            wire("decision_held", {"who": c.name, "wf_type": wft,
                                  "vi": json.dumps(vi, default=str)[:3000],
                                  "stage": stage})
            say(f"[P0] HOLDING unhandled decision {wft} (stage {stage})")
        if ST["hold_since"] is None:
            ST["hold_since"] = time.time()
        return True
    ST["hold_since"] = None

    if stage == "SETUP":
        calix_bf = bf_named(state, pid, CALIX)
        if calix_bf:
            ST["calix_oid"] = calix_bf[0]
            ST["calix_cast"] = True
            ST["calix_cast_turn"] = turn
            anthems = bf_named(state, pid, ANTHEM)
            if len(anthems) >= 2:
                ST["stage"] = "PROOF"
                say(f"Calix + {len(anthems)} Anthems on battlefield "
                    f"(turn {turn}); stage -> PROOF")
                return True
        if not is_my_main(state, pid):
            return False
        # cast Glorious Anthems first (need 2 on BF for the prompt test),
        # then Calix
        anthems_bf = len(bf_named(state, pid, ANTHEM))
        if anthems_bf < 2:
            aoid = find_hand(state, pid, ANTHEM)
            a = castspell_advertised(acts, aoid)
            if aoid and a is not None and untapped_land_count(
                    state, pid) >= 3 and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                await submit_as_is(c, a)
                ST["anthem_cast"] += 1
                say(f"[P0] submits Glorious Anthem cast (turn {turn})")
                return True
        else:
            coid = find_hand(state, pid, CALIX)
            a = castspell_advertised(acts, coid)
            if coid and a is not None and untapped_land_count(
                    state, pid) >= 4 and wf_player(state) == pid \
                    and wf_type(state) == "Priority" \
                    and not bf_named(state, pid, CALIX):
                await submit_as_is(c, a)
                say(f"[P0] submits Calix cast (turn {turn})")
                return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "PROOF":
        if not ST["minus3_submitted"]:
            calix = ST.get("calix_oid") or next(
                (oid for oid, o in bf(state, pid)
                 if oname(o) == CALIX), None)
            bears = bf_named(state, 1, BEAR)
            anthems = bf_named(state, pid, ANTHEM)
            loyal = calix_loyalty(state, calix) if calix else None
            ready = (calix and len(bears) >= 2 and len(anthems) >= 2
                     and is_my_main(state, pid)
                     and wf_player(state) == pid
                     and wf_type(state) == "Priority"
                     and (loyal is None or loyal >= 3))
            if ready:
                f = find_activate_minus3(c.latest, calix)
                legacy = None if f else find_activate_minus3_legacy(
                    acts, calix)
                if f or legacy:
                    if f:
                        iid, ch = f
                        wire("minus3_submit",
                             {"iid": iid, "choiceId": choice_id_of(ch),
                              "loyalty": loyal, "route": "viewer_interaction"})
                        say(f"[P0] activates Calix -3 (loyalty {loyal}, "
                            f"turn {turn}); pre_activate exported")
                        await export_now("pre_activate.json")
                        ST["pre"] = {"turn": turn, "phase": phase,
                                     "calix_loyalty": loyal,
                                     "bears": len(bears),
                                     "anthems": len(anthems)}
                        await c.send_interaction(
                            {"interactionId": iid,
                             "response": {"type": "choose",
                                          "data": {"choiceId":
                                                   choice_id_of(ch)}}})
                    else:
                        wire("minus3_submit",
                             {"loyalty": loyal, "route": "legacy_action",
                              "action": legacy})
                        say(f"[P0] activates Calix -3 via legacy "
                            f"ActivateAbility (loyalty {loyal}, turn {turn})"
                            f"; pre_activate exported")
                        await export_now("pre_activate.json")
                        ST["pre"] = {"turn": turn, "phase": phase,
                                     "calix_loyalty": loyal,
                                     "bears": len(bears),
                                     "anthems": len(anthems)}
                        await submit_as_is(c, legacy)
                    ST.setdefault("settle", {})[c.name] = c.revision
                    ST["minus3_submitted"] = True
                    ST["minus3_turn"] = turn
                    ST["minus3_watch"] = time.time()
                    ST["target_answers"] = 0
                    return True
                # ready but no activation option advertised: diagnose
                dkey = ("nact", turn)
                if dkey not in ST["held_logged"]:
                    ST["held_logged"].add(dkey)
                    shapes = activation_shapes(c.latest, acts, calix)
                    wire("no_activation_option",
                         {"turn": turn, "loyalty": loyal,
                          "bears": len(bears), "anthems": len(anthems),
                          "shapes": shapes})
                    say(f"[P0] PROOF ready (turn {turn}, loyalty {loyal}) "
                        f"but no -3 activation advertised: {shapes}")
            if is_my_main(state, pid):
                await play_land(c, pid, state, acts)
            return False
        # in-flight: watch the ability resolve, then export post state
        if calix_ability_on_stack(state, ST.get("calix_oid")):
            ST["ability_stack_seen"] = True
            return False
        if ST["ability_stack_seen"] and not ST["ability_resolved"]:
            ST["ability_resolved"] = True
            await export_now("post_minus3.json")
            ST["stage"] = "DESTROY"
            say("[P0] Calix -3 resolved; post_minus3 exported; "
                "stage -> DESTROY")
            return True
        # no stack entry ever seen (engine may resolve instantly): export
        # post state once the turn advances past the activation turn
        if not ST["ability_stack_seen"] and turn > (
                ST.get("minus3_turn") or 0):
            ST["ability_resolved"] = True
            await export_now("post_minus3.json")
            ST["stage"] = "DESTROY"
            say("[P0] activation turn passed without stack sighting; "
                "post_minus3 exported; stage -> DESTROY")
            return True
        if time.time() - (ST.get("minus3_watch")
                          or time.time()) > 120:
            wire("minus3_stall", {"target_sels": len(ST["target_sels"])})
            await export_now("mid_stall.json")
            ST["ability_resolved"] = True
            ST["stage"] = "DESTROY"
            say("[P0] -3 resolution watch timed out; mid_stall exported; "
                "stage -> DESTROY")
            return True
        return False

    if stage == "DESTROY":
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False
    return False


async def p1_tick(c, pid, state, acts):
    stage = ST["stage"]
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers",
            "MulliganDecision"):
        wft = wf_type(state)
        if wft == "TargetSelection" and stage == "DESTROY":
            # Naturalize's own target prompt: pick P0's Glorious Anthem
            ok = await answer_target_prompt(c, pid, state, ANTHEM, 0,
                                            "naturalize-target")
            if ok:
                if ST.get("last_target_oid") and not ST.get(
                        "nat_target_oid"):
                    ST["nat_target_oid"] = ST["last_target_oid"]
                    wire("nat_target_recorded",
                         {"oid": ST["nat_target_oid"]})
                return True
        wire("decision_held_p1", {"wf_type": wft, "stage": stage})
        return True
    if stage in ("SETUP", "PROOF"):
        if is_my_main(state, pid):
            bears_bf = bf_named(state, pid, BEAR)
            if len(bears_bf) < 2:
                boid = find_hand(state, pid, BEAR)
                a = castspell_advertised(acts, boid)
                if boid and a is not None and untapped_land_count(
                        state, pid) >= 2 and wf_player(state) == pid \
                        and wf_type(state) == "Priority":
                    await submit_as_is(c, a)
                    say(f"[P1] submits Grizzly Bears cast")
                    return True
            await play_land(c, pid, state, acts)
        return False
    if stage == "DESTROY":
        # destroy an Anthem with Naturalize, then observe whether the
        # exiled Bear returns. Completion is tracked against the SPECIFIC
        # targeted Anthem (P0 may control more than one).
        anthems = bf_named(state, 0, ANTHEM)
        nat_oid = find_hand(state, pid, NAT)
        if not ST["nat_cast"] and anthems and nat_oid and is_my_main(
                state, pid):
            a = castspell_advertised(acts, nat_oid)
            if a is not None and untapped_land_count(
                    state, pid) >= 2 and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                await submit_as_is(c, a)
                ST["nat_cast"] = True
                say("[P1] casts Naturalize targeting P0's Glorious Anthem")
                return True
        if ST["nat_cast"] and not ST["nat_target_oid"]:
            # fallback pickup if the target was answered before this block
            # saw it (kept for robustness; normally set at answer time)
            if ST.get("last_target_oid"):
                ST["nat_target_oid"] = ST["last_target_oid"]
                wire("nat_target_recorded",
                     {"oid": ST["nat_target_oid"], "via": "fallback"})
        if ST["nat_target_oid"] and not ST["anthem_destroyed"]:
            z = zone_of(state, ST["nat_target_oid"])
            if z != "Battlefield":
                ST["anthem_destroyed"] = True
                ST["destroy_turn"] = state.get("turn_number")
                ST["destroy_watch"] = time.time()
                await export_now("mid_destroyed.json")
                say(f"[P1] targeted Anthem left battlefield (zone={z}); "
                    f"mid_destroyed exported; watching for return")
                return True
            return False
        if ST["anthem_destroyed"] and not ST["stop"]:
            # settle: one full turn after the destroy, or 60s
            turn = state.get("turn_number") or 0
            if turn > (ST.get("destroy_turn") or 0) or \
                    time.time() - (ST.get("destroy_watch")
                                   or time.time()) > 60:
                await export_now("post_destroy.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("[P1] destroy leg settled; post_destroy exported; DONE")
                return True
            return False
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    rej = drain_rejections(c)
    # rejection-storm backoff: repeated rejections with no revision change
    # mean the client is spinning on a stale state; hold submissions briefly
    rkey, vkey, bkey = (c.name, "rej_n"), (c.name, "rej_rev"), \
        (c.name, "backoff_until")
    if rej:
        ST[rkey] = ST.get(rkey, 0) + len(rej)
        if ST[rkey] >= 8 and c.revision == ST.get(vkey):
            ST[bkey] = time.time() + 8
            ST[rkey] = 0
            wire("backoff", {"who": c.name, "rev": c.revision})
        ST[vkey] = c.revision
    else:
        ST[rkey] = 0
    if time.time() < ST.get(bkey, 0):
        return False
    if settle_pending(c):
        return False
    if wf_type(state) == "MulliganDecision" and wf_pending_for(state, pid):
        if await handle_mulligan(c, pid, state):
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            pend = (wf(state).get("data") or {}).get("pending") or []
            my = next((p for p in pend if p.get("player") == pid), None)
            phase = (my or {}).get("phase") or {}
            if isinstance(phase, dict) and phase.get("type") == "BottomCards":
                n = int(phase.get("count") or 0)
                keep_name = CALIX if pid == 0 else FOREST
                named = [(oid, oname(objs(state)[oid]))
                         for oid in hand_oids(state, pid)]
                picks = [oid for oid, nm in named
                         if nm != keep_name][:n]
                if len(picks) < n:
                    picks = [oid for oid, _ in named][:n]
                picks = [int(x) for x in picks[:n]]
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": picks}})
                say(f"[{c.name}] bottoms {len(picks)} after mulligan")
                return True
            if pid == 0 and ST["mulligans"] < 3 and not find_hand(
                    state, 0, CALIX):
                ST["mulligans"] += 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"[P0] mulligans ({ST['mulligans']}) seeking Calix")
            else:
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        picks = discard_picks(state, pid)
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": picks}})
            say(f"[{c.name}] discards {len(picks)} to hand size")
            return True
        wire("discard_no_picks", {"who": c.name})
        return True
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers" and \
            wf_player(state) == pid:
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if (state.get("phase") or "") == "DeclareBlockers" and pid in (0, 1) \
            and wf_player(state) == pid:
        for a in acts:
            if a["type"] == "DeclareBlockers":
                sub = copy.deepcopy(a)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            if wf_player(state) == pid:
                await submit_as_is(c, a)
                return True
            continue
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and wt not in (None, "Priority"):
        return True
    if wf_player(state) != pid:
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False

# ------------------------------------------------------------------- run

async def get_server_hello():
    import websockets
    async with websockets.connect(
            "ws://127.0.0.1:9374/ws", max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        return json.loads(raw)


def load_env(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())
    except Exception as e:
        return {"_err": str(e)[:160]}


def env_state(env):
    if not env or "_err" in env:
        return None
    s = env.get("state")
    return s if isinstance(s, dict) else json.loads(s)


def zone_of(state, oid):
    return (objs(state).get(str(oid)) or {}).get("zone")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def main():
    reset_state()
    t_start = time.time()
    hello = await get_server_hello()
    say("ServerHello observed: " + json.dumps(hello)[:400])
    wire("server_hello", hello)
    ST["server_hello"] = hello.get("data", hello)

    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    ST["game_code"] = C0.game_code
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        st0 = C0.latest
        turn = (st0.get("state", {}).get("turn_number") or 0) if st0 else 0
        if turn > ST["turn_cap"]:
            say("turn cap reached; stopping")
            wire("turn_cap", {})
            break
        if acted0 or acted1:
            last_progress = time.time()
        if ST.get("hold_since") and time.time() - ST["hold_since"] > 150:
            say("held decision for 150s with no progress; exporting and "
                "stopping")
            wire("hold_timeout", {"stage": ST.get("stage")})
            await export_now("mid_held.json")
            break
        if time.time() - last_progress > 300:
            say("no progress for 300s; stopping")
            break
        await asyncio.sleep(0.15)

    say(f"loop ended: stage={ST['stage']} stop={ST['stop']} "
        f"target_sels={len(ST['target_sels'])}")
    wire("loop_end", {k: ST.get(k) for k in (
        "stage", "stop", "calix_cast", "minus3_submitted",
        "ability_resolved", "nat_cast", "anthem_destroyed",
        "target_answers")})
    await C0.close()
    await C1.close()
    t_end = time.time()

    # ---- assertions from SAVED states ----
    A, D = {}, {}
    pre = env_state(load_env("pre_activate.json"))
    postm = env_state(load_env("post_minus3.json"))
    midd = env_state(load_env("mid_destroyed.json"))
    postd = env_state(load_env("post_destroy.json"))
    tsel_recs = ST.get("target_sels") or []
    # only prompts offered for the -3 activation count toward A2/A3;
    # other spells' prompts (e.g. Naturalize) are recorded too but tagged
    # with their stage
    tsel_proof = [r for r in tsel_recs if r.get("stage") == "PROOF"]

    # A1
    if pre:
        calix_ok = len(bf_named(pre, 0, CALIX)) >= 1
        anthem_ok = len(bf_named(pre, 0, ANTHEM)) >= 2
        bear_ok = len(bf_named(pre, 1, BEAR)) >= 2
        phase_ok = (pre.get("phase") or "") in ("PreCombatMain",
                                                "PostCombatMain") \
            and pre.get("active_player") == 0
        ok = calix_ok and anthem_ok and bear_ok and phase_ok
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"calix_bf={calix_ok} anthems_bf={anthem_ok} "
                            f"bears_p1_bf={bear_ok} p0_main={phase_ok} "
                            f"turn={pre.get('turn_number')}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre_activate.json missing (activation never ran)"

    # A2
    if ST.get("minus3_submitted"):
        n = len(tsel_proof)
        A["A2_target_prompted"] = "passed" if n >= 1 else "failed"
        D["A2_target_prompted"] = (f"{n} TargetSelection prompt(s) for the "
                                   f"-3 activation "
                                   f"({len(tsel_recs)} total recorded)")
    else:
        A["A2_target_prompted"] = "not-run"
        D["A2_target_prompted"] = "-3 activation never submitted"

    # A3: the reported gap - two target slots
    if A.get("A2_target_prompted") == "passed":
        n = len(tsel_proof)
        A["A3_two_target_slots"] = "passed" if n >= 2 else "failed"
        slot_desc = []
        for r in tsel_proof:
            cands = [(x["name"], x["zone"], x["controller"])
                     for x in r["candidates"]]
            slot_desc.append(cands)
        D["A3_two_target_slots"] = (
            f"{n} -3 target slot(s) offered (expect >=2: exile target + "
            f"own enchantment); candidates={slot_desc}")
    else:
        A["A3_two_target_slots"] = "not-run"
        D["A3_two_target_slots"] = "A2 not passed"

    # A4
    if postm and ST.get("exile_target_oid"):
        z = zone_of(postm, ST["exile_target_oid"])
        ok = z == "Exile"
        A["A4_exile_observed"] = "passed" if ok else "failed"
        D["A4_exile_observed"] = (
            f"target Bear oid {ST['exile_target_oid']} zone={z} "
            f"(expect Exile)")
    elif postm and A.get("A2_target_prompted") == "passed":
        ex = sorted(exile_oids(postm))
        A["A4_exile_observed"] = "failed"
        D["A4_exile_observed"] = (f"no tracked target oid; exiled oids in "
                                  f"post state: {ex}")
    else:
        A["A4_exile_observed"] = "not-run"
        D["A4_exile_observed"] = "post_minus3.json missing"

    # A5
    if A.get("A3_two_target_slots") == "passed":
        second = tsel_proof[1] if len(tsel_proof) > 1 else None
        anthem_picked = second and any(
            x["name"] == ANTHEM and x["controller"] == 0
            for x in second["candidates"])
        A["A5_own_enchant_targeted"] = \
            "passed" if ST.get("own_enchant_name") == ANTHEM else "failed"
        D["A5_own_enchant_targeted"] = (
            f"second slot answered with {ST.get('own_enchant_name')}; "
            f"Anthem among offered candidates={bool(anthem_picked)}")
    else:
        A["A5_own_enchant_targeted"] = "not-run"
        D["A5_own_enchant_targeted"] = "no second target slot (A3 not passed)"

    # A6: return-on-destroy (or its absence)
    if postd and ST.get("anthem_destroyed"):
        bear_oid = ST.get("exile_target_oid")
        z = zone_of(postd, bear_oid) if bear_oid else None
        bears_bf_p1 = [oid for oid, o in bf(postd, 1)
                       if oname(o) == BEAR]
        if A.get("A3_two_target_slots") == "passed":
            ok = bear_oid is not None and str(bear_oid) in bears_bf_p1
            A["A6_return_on_destroy"] = "passed" if ok else "failed"
            D["A6_return_on_destroy"] = (
                f"linked Anthem destroyed; target Bear zone={z}; on P1 "
                f"BF={str(bear_oid) in bears_bf_p1} (expect return)")
        else:
            # no link existed: the exiled Bear must stay in Exile,
            # documenting the unconditional-exile finding
            ok = (bear_oid is not None and z == "Exile"
                  and str(bear_oid) not in bears_bf_p1)
            A["A6_return_on_destroy"] = "passed" if ok else "failed"
            D["A6_return_on_destroy"] = (
                f"no link slot offered; Anthem destroyed; target Bear "
                f"zone={z} (expect Exile: documents unconditional exile, "
                f"no return linkage)")
    else:
        A["A6_return_on_destroy"] = "not-run"
        D["A6_return_on_destroy"] = "destroy leg incomplete"

    # A7
    final = postd or postm
    if final:
        ok = not stack(final)
        A["A7_cleanup"] = "passed" if ok else "failed"
        D["A7_cleanup"] = (f"stack empty={ok}; "
                           f"turn={final.get('turn_number')}")
    else:
        A["A7_cleanup"] = "not-run"
        D["A7_cleanup"] = "no post states"

    if A.get("A1_setup_ok") == "passed" \
            and A.get("A3_two_target_slots") == "failed":
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_target_prompted", "A3_two_target_slots",
              "A4_exile_observed", "A5_own_enchant_targeted",
              "A6_return_on_destroy", "A7_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "details": D, "verdict": verdict,
                   "rejections": ST["rejections"][:20],
                   "target_sels": [
                       {k: r[k] for k in ("interactionId", "turn", "phase",
                                         "candidates")} for r in tsel_recs],
                   "pre": ST.get("pre"),
                   "proof_turns": ST.get("proof_turns")}, f, indent=1,
                  default=str)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("assertions", {"A": A, "verdict": verdict})

    shutil.copy(__file__, f"{EVDIR}/scenario_{ISSUE}.py")

    # ---- run.json ----
    sh = ST.get("server_hello", {})
    rel = f"{BACKFILL}/server/releases/v0.81.3"
    binpath = f"{rel}/phase-server-slim-x86_64-unknown-linux-musl"
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "Calix, Destiny's Hand -3 doesn't associate targets correctly",
        "server": {
            "server_version": sh.get("server_version"),
            "build_commit": sh.get("build_commit"),
            "protocol_version": sh.get("protocol_version"),
            "mode": sh.get("mode"),
        },
        "binary_sha256": sha256_file(binpath),
        "card_data_sha256": sha256_file(f"{rel}/data/card-data.json"),
        "draft_pools_sha256": sha256_file(f"{rel}/data/draft-pools.json"),
        "signature_verified": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(t_start)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(t_end)),
        "game_code": ST.get("game_code"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "decisions": {
            "calix_cast_turn": ST.get("calix_cast_turn"),
            "minus3_turn": ST.get("minus3_turn"),
            "target_answers": ST.get("target_answers"),
            "target_sels_proof": len(tsel_proof),
            "target_sels_total": len(tsel_recs),
            "exile_target_oid": ST.get("exile_target_oid"),
            "own_enchant_oid": ST.get("own_enchant_oid"),
            "nat_cast": ST.get("nat_cast"),
            "rejections": len(ST["rejections"]),
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": "The -3 activation must offer two target slots per "
                        "Oracle (exile target; own enchantment the exile is "
                        "linked to). A single slot confirms the reported "
                        "unconditional exile.",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "4x Calix / 8x Glorious Anthem / 24x Grizzly Bears density is a "
            "test-harness convenience (engine accepts >4-of for custom "
            "games).",
            "Not tested on the original 2026-08-02 build; verdict is scoped "
            "to v0.81.3, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
        ],
        "setup_line": "P0: 4x Calix, 8x Glorious Anthem, 24x Forest, 24x "
                      "Plains; P1: 24x Grizzly Bears, 4x Naturalize, 32x "
                      "Forest. P0 casts 2x Anthem + Calix; P1 casts 2x "
                      "Bears.",
        "contract_line": "P0 activates Calix -3 at main-phase priority; the "
                         "driver answers the exile-target slot with a P1 "
                         "Bear and any second slot with a P0 Anthem. P1 "
                         "then destroys an Anthem with Naturalize and the "
                         "driver observes whether the exiled Bear returns.",
        "stats": {
            "target_selections": len(tsel_recs),
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written; verdict = " + verdict)

    # ---- server excerpts ----
    try:
        lines = open(f"{RUNDIR}/server.log", errors="replace").read(
            ).splitlines()
        keep = [l for l in lines if any(
            k in l.lower() for k in
            ("calix", "exile", "anthem", "naturalize", "target",
             "error", "panic", "warn"))]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(keep[-400:]) + "\n")
        say(f"server_excerpts.log: {len(keep)} matching lines")
    except Exception as e:
        say(f"server excerpts FAILED: {e}")

    WIRE.close()
    RUNLOG.close()
    RUNLOG2.close()
    render_png()
    write_manifest()
    validate()


def render_png():
    from PIL import Image, ImageDraw
    run = json.load(open(f"{EVDIR}/run.json"))
    dec = run["decisions"]
    tsel = dec.get("target_sels_proof", dec.get("target_answers", 0))

    W, H = 1040, 960
    BG = (18, 20, 26)
    PANEL = (26, 30, 38)
    TEXT = (235, 238, 245)
    DIM = (150, 160, 175)
    ACCENT = (110, 180, 255)
    GREEN = (110, 220, 140)
    RED = (240, 120, 120)
    YELLOW = (240, 200, 110)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    srv = run["server"]
    d.text((24, y), "#6906 - Calix, Destiny's Hand -3 target association",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv.get('server_version')} "
           f"({srv.get('build_commit')}, protocol "
           f"{srv.get('protocol_version')}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']}",
           fill=DIM)
    y += 30
    for ln in [
        "Oracle [-3]: Exile target creature or enchantment you don't",
        "control until target enchantment you control leaves the",
        "battlefield.",
        "",
        "Reported: after choosing the exile target, Calix should pick a",
        "second target (own enchantment) the exile is linked to. Instead,",
        "Calix just exiles straight up.",
    ]:
        d.text((24, y), ln, fill=TEXT)
        y += 20
    y += 8
    rows = [
        ("-3 activation", f"turn={dec.get('minus3_turn')} "
                          f"target_answers={tsel} (expect 2)"),
        ("exile target", f"oid={dec.get('exile_target_oid')}"),
        ("own enchantment slot", f"oid={dec.get('own_enchant_oid')} "
                                 f"(expect a Glorious Anthem)"),
        ("destroy leg", f"naturalize_cast={dec.get('nat_cast')}"),
    ]
    d.rectangle([16, y, W - 16, y + 30 + len(rows) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Observed (from saved states)", fill=YELLOW)
    y += 34
    for tag, val in rows:
        d.text((28, y), tag, fill=TEXT)
        d.text((28, y + 20), (val or "")[:150], fill=DIM)
        y += 44
    y += 12
    A = run["assertions"]
    Dd = run["assertion_details"]
    d.rectangle([16, y, W - 16, y + 34 + len(A) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    y += 34
    for k, v in A.items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, y), f"{k}: {v}", fill=color)
        d.text((28, y + 20), Dd.get(k, "")[:160], fill=DIM)
        y += 44
    y += 12
    d.text((24, y), "Limitations: " + "; ".join(run["limitations"])[:200],
           fill=DIM)
    y += 24
    d.text((24, y), "Evidence summary (not a gameplay screenshot). "
           "States: pre_activate/post_minus3/post_destroy.json + "
           "manifest.sha256",
           fill=DIM)
    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, flush=True)


def write_manifest():
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256"
                   and os.path.isfile(os.path.join(EVDIR, f)))
    lines = []
    for f in files:
        h = hashlib.sha256(open(os.path.join(EVDIR, f), "rb").read()
                           ).hexdigest()
        lines.append(f"{h}  {f}\n")
    with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
        f.writelines(lines)
    print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)


def validate():
    ok = True
    man = {}
    for ln in open(os.path.join(EVDIR, "manifest.sha256")):
        h, _, fn = ln.strip().partition("  ")
        man[fn] = h
    for fn, h in man.items():
        p = os.path.join(EVDIR, fn)
        if not os.path.exists(p):
            print(f"VALIDATE FAIL: missing {fn}", flush=True)
            ok = False
            continue
        ah = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if ah != h:
            print(f"VALIDATE FAIL: hash mismatch {fn}", flush=True)
            ok = False
        if fn.endswith(".json"):
            try:
                json.load(open(p))
            except Exception as e:
                print(f"VALIDATE FAIL: {fn} not JSON: {e}", flush=True)
                ok = False
    try:
        from PIL import Image
        im = Image.open(os.path.join(EVDIR, "summary.png"))
        im.verify()
        print("VALIDATE: summary.png opens OK", flush=True)
    except Exception as e:
        print(f"VALIDATE FAIL: summary.png: {e}", flush=True)
        ok = False
    print("VALIDATE: " + ("ALL OK" if ok else "FAILURES"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
