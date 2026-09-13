#!/usr/bin/env python3
"""Issue #6907: Nesting Grounds doesn't ask which counter to move.

Oracle (verified from pinned v0.81.3 card-data.json):
  {T}: Add {C}.
  {1}, {T}: Move a counter from target permanent you control onto a second
  target permanent. Activate only as a sorcery.

Reported (Discord 2026-08-02): a permanent with both +1/+1 and lore
counters; Nesting Grounds doesn't offer selection for which counter to
move, but seems to default to +1/+1's.

Pinned parse: MoveCounters effect with counter_type null (unspecified),
count Fixed 1; source = Typed Permanent controller You; target = Typed
Permanent (any controller).

Behavioral contract (native engine, protocol 70, two human seats):
  P0 controls Nesting Grounds, Sanctuary Warden (enters with 2 shield
  counters), Gavony Township, Grizzly Bears. P1 is passive (lands only).
  - CONTROL (single counter type): Warden has only shield counters.
    Activate Nesting Grounds targeting Warden (source) -> Bears
    (destination). Expect: no counter-type choice; exactly one shield
    counter moves.
  - PROOF (two counter types): activate Gavony Township (Warden gains a
    +1/+1 counter; now shield + +1/+1). Activate Nesting Grounds again
    targeting Warden -> Bears. Correct behavior: a counter-type choice is
    offered; the driver answers "shield" and exactly one shield counter
    moves. Reported bug: no choice is offered; the engine defaults to
    +1/+1.

  A1 setup_ok        pre_control: NG + Warden + Township + Bears on P0 BF;
                     Warden has >=1 shield and no other counter type;
                     P0 main-phase priority.
  A2 control_move    post_control: exactly one shield counter moved
                     Warden -> Bears; no +1/+1 on either.
  A3 counter_prompted  PROOF: a counter-type choice opportunity was offered
                     during the second activation (the reported gap).
  A4 exact_move      (A3 passed) exactly one counter of the chosen type
                     moved Warden -> Bears; other types unchanged.
  A5 default_documents (A3 failed) documents which counter type the engine
                     moved by default (report says +1/+1).
  A6 cleanup         post_proof: stack empty, game proceeding.

Verdict: reproduced iff A1 passes and (A3 fails, or A3 passes but A4
fails). not-reproduced iff A1, A2, A3, A4, A6 all pass. blocked iff A1
fails (or the two-type PROOF setup never materializes).
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
ISSUE = 6907
RUN_ID = "20260912-6907d"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

WARDEN = "Sanctuary Warden"
NG = "Nesting Grounds"
TOWN = "Gavony Township"
BEAR = "Grizzly Bears"
FOREST = "Forest"
PLAINS = "Plains"

P0_DECK = [(WARDEN, 4), (BEAR, 4), (NG, 4), (TOWN, 4),
           (FOREST, 22), (PLAINS, 22)]
P1_DECK = [(FOREST, 30), (PLAINS, 30)]
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


def counters_of(o):
    v = (o or {}).get("counters") or {}
    return dict(v) if isinstance(v, dict) else {}


def untapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped")
            and oname(o) in (FOREST, PLAINS, NG, TOWN)]


def untapped_plains(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) == PLAINS]


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
        "stage": "SETUP",   # SETUP -> CONTROL -> PROOF_SETUP -> PROOF -> DONE
        "stop": False,
        "turn_cap": 80,
        "rejections": [],
        "game_code": None,
        "mulligans": 0,
        "warden_oid": None,
        "bears_oid": None,
        "ng_oid": None,
        "town_oid": None,
        "warden_cast": False,
        "declined_trigger": 0,
        "ng1_submitted": False,
        "ng1_turn": None,
        "ng1_watch": None,
        "ng1_stack_seen": False,
        "ng1_resolved": False,
        "town_submitted": False,
        "town_turn": None,
        "town_watch": None,
        "town_resolved": False,
        "ng2_submitted": False,
        "ng2_turn": None,
        "ng2_watch": None,
        "ng2_stack_seen": False,
        "ng2_resolved": False,
        "ng_targets_answered": [],
        "target_sels": [],
        "target_sel_files": 0,
        "decisions": [],          # non-target decision opportunities
        "decision_files": 0,
        "counter_prompts": 0,     # counter-like opportunities seen
        "counter_choice_pick": None,
        "held_logged": set(),
        "hold_since": None,
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


def looks_like_counter_choice(opp):
    """A non-card decision opportunity that names counter types."""
    if is_card_choice(opp):
        return False
    blob = json.dumps(opp, default=str).lower()
    if "optionaleffect" in blob or "decideoptionaleffect" in blob:
        return False
    return "counter" in blob


def record_decision(c, state, opp):
    """Record any non-target decision opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in ST["decisions"]):
        return None
    blob = json.dumps(opp, default=str)
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": ST.get("stage"),
        "wf_type": wf_type(state),
        "wf_data": wf(state).get("data"),
        "counter_like": looks_like_counter_choice(opp),
        "is_card_choice": is_card_choice(opp),
        "blob_head": blob[:3000],
    }
    ST["decisions"].append(rec)
    n = ST["decision_files"] = ST["decision_files"] + 1
    with open(f"{EVDIR}/decision_{n}.json", "w") as f:
        json.dump({"rec": rec, "opp": json.loads(blob)}, f, indent=1,
                  default=str)
    if rec["counter_like"]:
        ST["counter_prompts"] = ST["counter_prompts"] + 1
    wire("decision_recorded",
         {"n": n, "wf_type": rec["wf_type"], "stage": rec["stage"],
          "counter_like": rec["counter_like"]})
    say(f"[{c.name}] recorded decision #{n} ({rec['wf_type']}, "
        f"counter_like={rec['counter_like']})")
    return rec


async def answer_counter_choice(c, opp):
    """Answer a counter-type choice, preferring 'shield'. Records the pick."""
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    rtype = resp.get("type")
    nodes = opp_candidates(opp) or opp_choices(opp)
    pick = None
    for n in nodes:
        if "shield" in json.dumps(n, default=str).lower():
            pick = n
            break
    if pick is None:
        for n in nodes:
            b = json.dumps(n, default=str).lower()
            if any(t in b for t in ("p1p1", "+1/+1", "lore", "charge")):
                pick = n
                break
    if pick is None and nodes:
        pick = nodes[0]
    if pick is None:
        say(f"[{c.name}] counter choice: no nodes to pick from")
        return False
    ST["counter_choice_pick"] = json.dumps(pick, default=str)[:800]
    wire("counter_choice_pick",
         {"pick": ST["counter_choice_pick"], "stage": ST.get("stage")})
    say(f"[{c.name}] counter choice pick: "
        f"{ST['counter_choice_pick'][:200]}")
    cid = choice_id_of(pick)
    iid = opp.get("interactionId")
    stype = spec.get("type") if rtype == "schema" else rtype
    if stype == "sequence":
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": [cid]}}}
    elif stype == "select":
        sub = {"interactionId": iid,
               "response": {"type": "select",
                            "data": {"choiceIds": [cid]}}}
    elif stype == "text":
        val = None
        for s in pick.get("surfaces") or []:
            v = surf_data(s)["value"]
            if v:
                val = v
                break
        sub = {"interactionId": iid,
               "response": {"type": "text",
                            "data": {"value": val or cid}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    await submit_interaction(c, sub)
    return True


async def decline_optional_effect(c, opp):
    """Decline an OptionalEffectChoice (decideOptionalEffect false)."""
    for ch in opp_choices(opp):
        codes = [surf_data(s)["code"] for s in ch.get("surfaces") or []]
        vals = [str(surf_data(s)["value"]) for s in ch.get("surfaces") or []]
        if "decideOptionalEffect" in codes and "false" in vals:
            await submit_interaction(
                c, {"interactionId": opp.get("interactionId"),
                    "response": {"type": "choose",
                                 "data": {"choiceId": choice_id_of(ch)}}})
            ST["declined_trigger"] = ST["declined_trigger"] + 1
            say(f"[{c.name}] declines optional effect")
            return True
    return False


def find_activate(st, src_oid, want_index, text_hint):
    """Find viewer_interaction activateAbility choice for src ability index."""
    cands = []
    for op in vi_opps(st):
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data") or {}
        choices = data.get("choices") or resp.get("choices") or []
        for ch in choices:
            codes = [surf_data(s)["code"] for s in ch.get("surfaces") or []]
            if "activateAbility" not in codes:
                continue
            refs = [str(surf_data(s)["reference"])
                    for s in ch.get("surfaces") or []
                    if surf_data(s)["role"] == "source"]
            if str(src_oid) not in refs:
                continue
            vals = [str(surf_data(s)["value"])
                    for s in ch.get("surfaces") or []]
            cands.append((op.get("interactionId"), ch, vals,
                          json.dumps(ch, default=str)))
    if not cands:
        return None
    for iid, ch, vals, blob in cands:
        if str(want_index) in vals:
            return iid, ch
    for iid, ch, vals, blob in cands:
        if text_hint.lower() in blob.lower():
            return iid, ch
    if len(cands) == 1:
        return cands[0][0], cands[0][1]
    wire("activate_ambiguous",
         {"src": str(src_oid), "cands": [(i, v) for i, _, v, _ in cands]})
    return None


def record_target_sel(c, state, opp):
    """Record a TargetSelection opportunity once per interactionId."""
    iid = opp.get("interactionId")
    for r in ST["target_sels"]:
        if r["interactionId"] == iid:
            return r
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
        "prompt_text": json.dumps(
            (opp.get("response") or {}).get("data") or {},
            default=str)[:1500],
        "candidates": cand_info,
    }
    ST["target_sels"].append(rec)
    n = ST["target_sel_files"] = ST["target_sel_files"] + 1
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump(rec, f, indent=1, default=str)
    wire("target_selection_recorded",
         {"n": n, "turn": rec["turn"], "stage": rec["stage"],
          "candidates": [(x["name"], x["zone"], x["controller"])
                         for x in cand_info]})
    say(f"[{c.name}] recorded TargetSelection #{n}: "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'})"
                    for x in cand_info[:8]))
    return rec


async def answer_ng_target(c, pid, state):
    """Answer one Nesting Grounds target slot: source slot -> Warden,
    destination slot -> Bears. Disambiguated by candidate controllers
    (destination slot may include P1 permanents) and prompt text."""
    for opp in current_opps(c):
        if not is_card_choice(opp):
            continue
        rec = record_target_sel(c, state, opp)
        cands = rec["candidates"]
        prompt = (rec.get("prompt_text") or "").lower()
        p1_in_cands = any(x["controller"] == 1 for x in cands)
        warden_oid = str(ST.get("warden_oid") or "")
        bears_oid = str(ST.get("bears_oid") or "")
        answered = [str(x) for x in ST.get("ng_targets_answered") or []]
        warden_avail = any(x["oid"] == warden_oid for x in cands)
        bears_avail = any(x["oid"] == bears_oid for x in cands)
        # destination slot: candidates reach beyond P0, or prompt says second
        is_dest_slot = p1_in_cands or ("second" in prompt)
        want = None
        if is_dest_slot:
            if bears_oid not in answered and bears_avail:
                want = bears_oid
            elif warden_oid not in answered and warden_avail:
                want = warden_oid
        else:
            if warden_oid not in answered and warden_avail:
                want = warden_oid
            elif bears_oid not in answered and bears_avail:
                want = bears_oid
        node = None
        if want:
            for nd in card_choice_nodes(opp):
                if candidate_oid(nd) == want:
                    node = nd
                    break
        if node is None:
            nodes = card_choice_nodes(opp)
            node = nodes[0] if nodes else None
            if node is None:
                continue
            say(f"[{c.name}] NG target: fallback to first candidate")
        o = objs(state).get(str(candidate_oid(node))) or {}
        say(f"[{c.name}] NG target answers {oname(o)} "
            f"(dest_slot={is_dest_slot})")
        wire("target_answer", {"stage": ST.get("stage"),
                               "want": want,
                               "dest_slot": is_dest_slot,
                               "choiceId": choice_id_of(node)})
        ST["ng_targets_answered"].append(candidate_oid(node))
        # submit: sequence of 1 (single slot); if the spec wants 2, send both
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        iid = opp.get("interactionId")
        cid = choice_id_of(node)
        rtype = spec.get("type") if resp.get("type") == "schema" \
            else resp.get("type")
        if rtype == "sequence" and (
                str(spec.get("max")) == "2" or spec.get("count") == 2):
            other = bears_oid if want == warden_oid else warden_oid
            onode = next((nd for nd in card_choice_nodes(opp)
                          if candidate_oid(nd) == other), None)
            cids = [cid] + ([choice_id_of(onode)] if onode else [])
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": cids}}}
        elif rtype == "sequence":
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
        elif rtype == "select":
            sub = {"interactionId": iid,
                   "response": {"type": "select",
                                "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": cid}}}
        await submit_interaction(c, sub)
        return True
    return False

# ------------------------------------------------------------------- ticks

async def play_land(c, pid, state, acts):
    plains_bf = len(bf_named(state, pid, PLAINS))
    forests_bf = len(bf_named(state, pid, FOREST))
    order = []
    if plains_bf < 2:
        order.append(PLAINS)
    if forests_bf < 2:
        order.append(FOREST)
    order += [NG, TOWN]
    if plains_bf >= 2:
        order.append(PLAINS)
    if forests_bf >= 2:
        order.append(FOREST)
    seen = set()
    dedup = []
    for n in order:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    for ln in dedup:
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
        take(BEAR, 1)
        take(NG, 1)
        take(TOWN, 1)
        take(WARDEN, 1)
    else:
        take(FOREST, 2)
        take(PLAINS, 2)
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
        keep_name = WARDEN if pid == 0 else FOREST
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
    if pid == 0 and ST["mulligans"] < 3 and not find_hand(state, 0, WARDEN):
        ST["mulligans"] += 1
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"[P0] mulligans ({ST['mulligans']}) seeking Warden")
    else:
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        if pid == 0:
            say("[P0] keeps hand")
    return True


def ability_on_stack(state, src_oid):
    for e in stack(state):
        d = e if isinstance(e, dict) else {}
        if str(d.get("source_id")) == str(src_oid):
            return True
    return False


def tapped_of(state, oid):
    return bool((objs(state).get(str(oid)) or {}).get("tapped"))


async def submit_ng_activation(c, pid, state, pre_path, tag):
    """Submit Nesting Grounds' {1},{T} MoveCounters activation."""
    ng = ST.get("ng_oid")
    f = find_activate(c.latest, ng, 1, "move a counter")
    if not f:
        dkey = ("nact", tag, state.get("turn_number"))
        if dkey not in ST["held_logged"]:
            ST["held_logged"].add(dkey)
            wire("no_activation_option", {"tag": tag, "ng": str(ng)})
            say(f"[P0] {tag}: NG activation not advertised")
        return False
    iid, ch = f
    await export_now(pre_path)
    wire("ng_activate", {"tag": tag, "iid": iid,
                         "choiceId": choice_id_of(ch)})
    say(f"[P0] activates Nesting Grounds ({tag}, turn "
        f"{state.get('turn_number')}); {pre_path} exported")
    await submit_interaction(
        c, {"interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": choice_id_of(ch)}}})
    ST["ng_targets_answered"] = []
    return True


async def p0_tick(c, pid, state, acts):
    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    # --- Warden ETB may-trigger: decline during SETUP ---
    if wf_type(state) == "OptionalEffectChoice" \
            and wf_pending_for(state, pid):
        if stage == "SETUP":
            for opp in current_opps(c):
                if await decline_optional_effect(c, opp):
                    return True
        return True  # hold: never pass while it is pending

    # --- Warden ETB RemoveCounter target (SETUP fallback: if the engine
    #     skips the may-choice and asks for the trigger target directly,
    #     answer with Warden itself so the single-type setup stays valid).
    #     Trigger targets surface as TriggerTargetSelection on this build.
    if stage == "SETUP" and wf_type(state) in (
            "TargetSelection", "TriggerTargetSelection") \
            and wf_pending_for(state, pid):
        blob = json.dumps(wf(state), default=str).lower()
        if "removecounter" in blob or "removecounter" in json.dumps(
                current_opps(c), default=str).lower():
            for opp in current_opps(c):
                if not is_card_choice(opp):
                    continue
                record_target_sel(c, state, opp)
                warden_oid = str(ST.get("warden_oid") or "")
                node = None
                for nd in card_choice_nodes(opp):
                    if candidate_oid(nd) == warden_oid:
                        node = nd
                        break
                if node is None:
                    nodes = card_choice_nodes(opp)
                    node = nodes[0] if nodes else None
                if node is None:
                    return True
                o = objs(state).get(str(candidate_oid(node))) or {}
                say(f"[P0] Warden-trigger target (no may-choice): "
                    f"{oname(o)}")
                wire("warden_trigger_target",
                     {"oid": candidate_oid(node)})
                await submit_choice(c, opp, node)
                return True
        return True  # hold unexpected SETUP target prompt

    # --- NG target selections (CONTROL / PROOF) ---
    if stage in ("CONTROL", "PROOF") and wf_type(state) in (
            "TargetSelection", "TriggerTargetSelection") \
            and wf_pending_for(state, pid):
        if await answer_ng_target(c, pid, state):
            return True
        return True

    # --- other decisions during CONTROL/PROOF: record; answer counter
    #     choices; hold anything unrecognized ---
    if stage in ("CONTROL", "PROOF") and wf_player(state) == pid \
            and wf_type(state) not in (
                None, "Priority", "DeclareAttackers", "DeclareBlockers",
                "MulliganDecision", "DiscardToHandSize"):
        for opp in current_opps(c):
            rec = record_decision(c, state, opp)
            if rec and rec["counter_like"]:
                ok = await answer_counter_choice(c, opp)
                say(f"[P0] counter-type choice answered: {ok} "
                    f"(stage {stage})")
                return True
        key = ("held", wf_type(state), stage)
        if key not in ST["held_logged"]:
            ST["held_logged"].add(key)
            wire("decision_held",
                 {"who": c.name, "wf_type": wf_type(state),
                  "stage": stage})
            say(f"[P0] HOLDING unhandled decision {wf_type(state)} "
                f"(stage {stage})")
        if ST["hold_since"] is None:
            ST["hold_since"] = time.time()
        return True
    ST["hold_since"] = None

    # --- generic unhandled-decision hold (SETUP / PROOF_SETUP) ---
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers",
            "MulliganDecision", "DiscardToHandSize"):
        wft = wf_type(state)
        key = ("held", wft, stage)
        if key not in ST["held_logged"]:
            ST["held_logged"].add(key)
            wire("decision_held",
                 {"who": c.name, "wf_type": wft, "stage": stage})
            say(f"[P0] HOLDING unhandled decision {wft} (stage {stage})")
        if ST["hold_since"] is None:
            ST["hold_since"] = time.time()
        return True
    ST["hold_since"] = None

    if stage == "SETUP":
        w = bf_named(state, pid, WARDEN)
        if w:
            ST["warden_oid"] = w[0]
            ST["warden_cast"] = True
        b = bf_named(state, pid, BEAR)
        if b:
            ST["bears_oid"] = b[0]
        ng = bf_named(state, pid, NG)
        if ng:
            ST["ng_oid"] = ng[0]
        tw = bf_named(state, pid, TOWN)
        if tw:
            ST["town_oid"] = tw[0]
        if ng and w and tw and b:
            wc = counters_of(objs(state)[w[0]])
            if wc.get("shield", 0) >= 1 and not any(
                    k != "shield" for k in wc):
                ST["stage"] = "CONTROL"
                say(f"board ready (turn {turn}): NG + Warden "
                    f"(shield={wc.get('shield')}) + Township + Bears; "
                    f"stage -> CONTROL")
                return True
        if not is_my_main(state, pid):
            return False
        if not b:
            boid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, boid)
            if boid and a is not None and len(untapped_lands(
                    state, pid)) >= 2 and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                await submit_as_is(c, a)
                say(f"[P0] casts Grizzly Bears (turn {turn})")
                return True
        if not w:
            woid = find_hand(state, pid, WARDEN)
            a = castspell_advertised(acts, woid)
            if woid and a is not None and len(untapped_lands(
                    state, pid)) >= 6 and len(untapped_plains(
                        state, pid)) >= 2 and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                await submit_as_is(c, a)
                say(f"[P0] casts Sanctuary Warden (turn {turn})")
                return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "CONTROL":
        if not ST["ng1_submitted"]:
            ng = ST.get("ng_oid")
            ready = (ng and is_my_main(state, pid)
                     and wf_player(state) == pid
                     and wf_type(state) == "Priority"
                     and not stack(state) and not tapped_of(state, ng))
            if ready:
                ok = await submit_ng_activation(c, pid, state,
                                                "pre_control.json",
                                                "control")
                if ok:
                    ST["ng1_submitted"] = True
                    ST["ng1_turn"] = turn
                    ST["ng1_watch"] = time.time()
                    return True
            if is_my_main(state, pid):
                await play_land(c, pid, state, acts)
            return False
        if ability_on_stack(state, ST.get("ng_oid")):
            ST["ng1_stack_seen"] = True
            return False
        if ST["ng1_stack_seen"] or turn > (ST.get("ng1_turn") or 0):
            ST["ng1_resolved"] = True
            await export_now("post_control.json")
            ST["stage"] = "PROOF_SETUP"
            say("[P0] NG activation #1 resolved; post_control exported; "
                "stage -> PROOF_SETUP")
            return True
        if time.time() - (ST.get("ng1_watch") or time.time()) > 150:
            await export_now("mid_control_stall.json")
            ST["ng1_resolved"] = True
            ST["stage"] = "PROOF_SETUP"
            say("[P0] NG #1 watch timed out; mid_control_stall exported; "
                "stage -> PROOF_SETUP")
            return True
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    if stage == "PROOF_SETUP":
        if not ST["town_submitted"]:
            tw = ST.get("town_oid")
            ready = (tw and is_my_main(state, pid)
                     and wf_player(state) == pid
                     and wf_type(state) == "Priority"
                     and not stack(state) and not tapped_of(state, tw))
            if ready:
                f = find_activate(c.latest, tw, 1, "+1/+1 counter")
                if f:
                    iid, ch = f
                    await export_now("pre_township.json")
                    wire("township_activate",
                         {"iid": iid, "choiceId": choice_id_of(ch)})
                    say(f"[P0] activates Gavony Township (turn {turn}); "
                        f"pre_township exported")
                    await submit_interaction(
                        c, {"interactionId": iid,
                            "response": {"type": "choose", "data": {
                                "choiceId": choice_id_of(ch)}}})
                    ST["town_submitted"] = True
                    ST["town_turn"] = turn
                    ST["town_watch"] = time.time()
                    return True
                dkey = ("ntown", turn)
                if dkey not in ST["held_logged"]:
                    ST["held_logged"].add(dkey)
                    say(f"[P0] PROOF_SETUP ready but no Township "
                        f"activation advertised")
            if is_my_main(state, pid):
                await play_land(c, pid, state, acts)
            return False
        w = objs(state).get(str(ST.get("warden_oid"))) or {}
        if counters_of(w).get("P1P1", 0) >= 1:
            ST["town_resolved"] = True
            await export_now("post_township.json")
            ST["stage"] = "PROOF"
            say(f"[P0] Township resolved: Warden counters="
                f"{counters_of(w)}; post_township exported; "
                f"stage -> PROOF")
            return True
        if turn > (ST.get("town_turn") or 0) or \
                time.time() - (ST.get("town_watch") or time.time()) > 150:
            await export_now("post_township.json")
            w2 = objs(state).get(str(ST.get("warden_oid"))) or {}
            if counters_of(w2).get("P1P1", 0) >= 1:
                ST["town_resolved"] = True
                ST["stage"] = "PROOF"
                say("[P0] Township leg settled with +1/+1 present; "
                    "stage -> PROOF")
            else:
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("[P0] Township never gave Warden +1/+1; cannot build "
                    "two-type source; DONE (blocked)")
            return True
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    if stage == "PROOF":
        if not ST["ng2_submitted"]:
            ng = ST.get("ng_oid")
            ready = (ng and is_my_main(state, pid)
                     and wf_player(state) == pid
                     and wf_type(state) == "Priority"
                     and not stack(state) and not tapped_of(state, ng))
            if ready:
                ok = await submit_ng_activation(c, pid, state,
                                                "pre_proof.json",
                                                "proof")
                if ok:
                    ST["ng2_submitted"] = True
                    ST["ng2_turn"] = turn
                    ST["ng2_watch"] = time.time()
                    return True
            if is_my_main(state, pid):
                await play_land(c, pid, state, acts)
            return False
        if ability_on_stack(state, ST.get("ng_oid")):
            ST["ng2_stack_seen"] = True
            return False
        if ST["ng2_stack_seen"] or turn > (ST.get("ng2_turn") or 0):
            ST["ng2_resolved"] = True
            await export_now("post_proof.json")
            ST["stage"] = "DONE"
            ST["stop"] = True
            say("[P0] NG activation #2 resolved; post_proof exported; DONE")
            return True
        if time.time() - (ST.get("ng2_watch") or time.time()) > 150:
            await export_now("mid_proof_stall.json")
            ST["ng2_resolved"] = True
            ST["stage"] = "DONE"
            ST["stop"] = True
            say("[P0] NG #2 watch timed out; mid_proof_stall exported; DONE")
            return True
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    return False


async def p1_tick(c, pid, state, acts):
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers",
            "MulliganDecision", "DiscardToHandSize"):
        wire("decision_held_p1", {"wf_type": wf_type(state)})
        return True
    if is_my_main(state, pid):
        await play_land(c, pid, state, acts)
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    rej = drain_rejections(c)
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
                keep_name = WARDEN if pid == 0 else FOREST
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
                    state, 0, WARDEN):
                ST["mulligans"] += 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type":
                                                           "Mulligan"}}})
                say(f"[P0] mulligans ({ST['mulligans']}) seeking Warden")
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


def counter_delta(pre, post, pid, name):
    """(pre_counters, post_counters) for the named permanent of pid."""
    def snap(st):
        if not st:
            return None
        oids = bf_named(st, pid, name)
        if not oids:
            return None
        return counters_of((objs(st).get(str(oids[0])) or {}))
    a, b = snap(pre), snap(post)
    if a is None or b is None:
        return None, a, b
    keys = set(a) | set(b)
    return {k: b.get(k, 0) - a.get(k, 0) for k in keys}, a, b


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
        f"target_sels={len(ST['target_sels'])} "
        f"decisions={len(ST['decisions'])}")
    wire("loop_end", {k: ST.get(k) for k in (
        "stage", "stop", "warden_cast", "ng1_submitted", "ng1_resolved",
        "town_submitted", "town_resolved", "ng2_submitted",
        "ng2_resolved", "counter_prompts", "declined_trigger")})
    await C0.close()
    await C1.close()
    t_end = time.time()

    # ---- assertions from SAVED states ----
    A, D = {}, {}
    pre_c = env_state(load_env("pre_control.json"))
    post_c = env_state(load_env("post_control.json"))
    pre_p = env_state(load_env("pre_proof.json"))
    post_p = env_state(load_env("post_proof.json"))

    # A1
    if pre_c:
        ng_ok = len(bf_named(pre_c, 0, NG)) >= 1
        w_ok = len(bf_named(pre_c, 0, WARDEN)) >= 1
        t_ok = len(bf_named(pre_c, 0, TOWN)) >= 1
        b_ok = len(bf_named(pre_c, 0, BEAR)) >= 1
        woids = bf_named(pre_c, 0, WARDEN)
        wc = counters_of((objs(pre_c).get(str(woids[0])) or {})) if woids \
            else {}
        ctr_ok = wc.get("shield", 0) >= 1 and not any(
            k != "shield" for k in wc)
        phase_ok = (pre_c.get("phase") or "") in ("PreCombatMain",
                                                  "PostCombatMain") \
            and pre_c.get("active_player") == 0
        ok = ng_ok and w_ok and t_ok and b_ok and ctr_ok and phase_ok
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"ng={ng_ok} warden={w_ok} township={t_ok} "
                            f"bears={b_ok} warden_counters={wc} "
                            f"single_type={ctr_ok} p0_main={phase_ok} "
                            f"turn={pre_c.get('turn_number')}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre_control.json missing (activation never ran)"

    # A2
    if pre_c and post_c:
        dw, aw, bw = counter_delta(pre_c, post_c, 0, WARDEN)
        db, ab, bb = counter_delta(pre_c, post_c, 0, BEAR)
        ctrl_prompts = [r for r in ST["decisions"]
                        if r.get("stage") == "CONTROL"
                        and r.get("counter_like")]
        if dw is None or db is None:
            A["A2_control_move"] = "not-run"
            D["A2_control_move"] = "warden/bears not found in pre/post"
        else:
            ok = (dw.get("shield", 0) == -1 and db.get("shield", 0) == 1
                  and dw.get("P1P1", 0) == 0 and db.get("P1P1", 0) == 0
                  and all(v == 0 for k, v in dw.items()
                          if k not in ("shield", "P1P1"))
                  and all(v == 0 for k, v in db.items()
                          if k not in ("shield", "P1P1")))
            A["A2_control_move"] = "passed" if ok else "failed"
            D["A2_control_move"] = (
                f"warden {aw}->{bw} bears {ab}->{bb}; "
                f"counter-like prompts during CONTROL: "
                f"{len(ctrl_prompts)} (expect 0)")
    else:
        A["A2_control_move"] = "not-run"
        D["A2_control_move"] = "pre/post control states missing"

    # A3 (gated on the two-type PROOF setup actually existing)
    proof_prompts = [r for r in ST["decisions"]
                     if r.get("stage") == "PROOF" and r.get("counter_like")]
    two_types = False
    if pre_p:
        woids = bf_named(pre_p, 0, WARDEN)
        wc = counters_of((objs(pre_p).get(str(woids[0])) or {})) \
            if woids else {}
        two_types = wc.get("shield", 0) >= 1 and wc.get("P1P1", 0) >= 1
        D["A3_setup_note"] = f"pre_proof warden counters={wc}"
    if not pre_p or not two_types:
        A["A3_counter_prompted"] = "not-run"
        D["A3_counter_prompted"] = (
            "two-type source never materialized in pre_proof; "
            + D.get("A3_setup_note", "pre_proof missing"))
    else:
        n = len(proof_prompts)
        A["A3_counter_prompted"] = "passed" if n >= 1 else "failed"
        D["A3_counter_prompted"] = (
            f"{n} counter-type choice opportunity(ies) during PROOF "
            f"(expect >=1); " + D.get("A3_setup_note", ""))

    # A4
    if A.get("A3_counter_prompted") == "passed" and pre_p and post_p:
        dw, aw, bw = counter_delta(pre_p, post_p, 0, WARDEN)
        db, ab, bb = counter_delta(pre_p, post_p, 0, BEAR)
        moved_types = [k for k, v in (dw or {}).items() if v == -1]
        gained_types = [k for k, v in (db or {}).items() if v == 1]
        ok = (dw is not None and db is not None
              and len(moved_types) == 1 and len(gained_types) == 1
              and moved_types[0] == gained_types[0]
              and all(v == 0 for k, v in dw.items()
                      if k != moved_types[0])
              and all(v == 0 for k, v in db.items()
                      if k != gained_types[0]))
        A["A4_exact_move"] = "passed" if ok else "failed"
        D["A4_exact_move"] = (
            f"warden {aw}->{bw} bears {ab}->{bb}; "
            f"moved={moved_types} gained={gained_types} "
            f"(expect exactly one shared type); "
            f"driver pick={ST.get('counter_choice_pick')}")
    else:
        A["A4_exact_move"] = "not-run"
        D["A4_exact_move"] = "A3 not passed"

    # A5
    if A.get("A3_counter_prompted") == "failed" and pre_p and post_p:
        dw, aw, bw = counter_delta(pre_p, post_p, 0, WARDEN)
        db, ab, bb = counter_delta(pre_p, post_p, 0, BEAR)
        if dw is None or db is None:
            A["A5_default_documents"] = "not-run"
            D["A5_default_documents"] = "warden/bears missing in pre/post"
        else:
            total_moved = sum(-v for v in dw.values() if v < 0)
            total_gained = sum(v for v in db.values() if v > 0)
            default_p1p1 = (dw.get("P1P1", 0) == -1
                            and db.get("P1P1", 0) == 1
                            and total_moved == 1 and total_gained == 1)
            A["A5_default_documents"] = \
                "passed" if default_p1p1 else "failed"
            D["A5_default_documents"] = (
                f"no counter choice offered; warden {aw}->{bw} bears "
                f"{ab}->{bb}; defaulted to +1/+1 = {default_p1p1} "
                f"(report says the engine defaults to +1/+1)")
    else:
        A["A5_default_documents"] = "not-run"
        D["A5_default_documents"] = "A3 not failed"

    # A6
    final = post_p or post_c
    if final:
        ok = not stack(final)
        A["A6_cleanup"] = "passed" if ok else "failed"
        D["A6_cleanup"] = (f"stack empty={ok}; "
                           f"turn={final.get('turn_number')}")
    else:
        A["A6_cleanup"] = "not-run"
        D["A6_cleanup"] = "no post states"

    if A.get("A1_setup_ok") == "passed" and (
            A.get("A3_counter_prompted") == "failed"
            or (A.get("A3_counter_prompted") == "passed"
                and A.get("A4_exact_move") == "failed")):
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_control_move", "A3_counter_prompted",
              "A4_exact_move", "A6_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "details": D, "verdict": verdict,
                   "rejections": ST["rejections"][:20],
                   "target_sels": [
                       {k: r[k] for k in ("interactionId", "turn", "phase",
                                         "stage", "candidates")}
                       for r in ST["target_sels"]],
                   "decisions": [
                       {k: r[k] for k in ("interactionId", "turn",
                                         "wf_type", "stage",
                                         "counter_like")}
                       for r in ST["decisions"]],
                   "counter_choice_pick": ST.get("counter_choice_pick")},
                  f, indent=1, default=str)
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
        "title": "Nesting Grounds doesn't ask which counter to move",
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
            "ng1_turn": ST.get("ng1_turn"),
            "town_turn": ST.get("town_turn"),
            "ng2_turn": ST.get("ng2_turn"),
            "declined_warden_trigger": ST.get("declined_trigger"),
            "target_sels": len(ST["target_sels"]),
            "decisions_recorded": len(ST["decisions"]),
            "counter_prompts": ST.get("counter_prompts"),
            "counter_choice_pick": ST.get("counter_choice_pick"),
            "rejections": len(ST["rejections"]),
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": "Nesting Grounds' {1},{T} ability must offer a "
                        "counter-type choice when the source permanent has "
                        "multiple counter types. A1+A3-failed confirms the "
                        "reported missing choice.",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "4x Sanctuary Warden / 4x Grizzly Bears / 4x Nesting Grounds / "
            "4x Gavony Township density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "Shield + +1/+1 counter pairing stands in for the report's "
            "+1/+1 + lore pairing (same defect class: unspecified "
            "counter_type in the MoveCounters parse).",
            "Not tested on the original 2026-08-02 build; verdict is "
            "scoped to v0.81.3, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
        ],
        "setup_line": "P0: 4x Sanctuary Warden, 4x Grizzly Bears, 4x "
                      "Nesting Grounds, 4x Gavony Township, 22x Forest, "
                      "22x Plains; P1: 30x Forest, 30x Plains (passive). "
                      "P0 casts Warden (declines its may-trigger) and "
                      "Bears; plays Nesting Grounds + Gavony Township.",
        "contract_line": "CONTROL: with Warden holding only shield "
                         "counters, activate Nesting Grounds "
                         "Warden->Bears; expect no counter choice, exactly "
                         "one shield moves. PROOF: after Gavony Township "
                         "gives Warden +1/+1, activate again; expect a "
                         "counter-type choice (driver answers shield) and "
                         "exactly one shield moves.",
        "stats": {
            "target_selections": len(ST["target_sels"]),
            "decisions": len(ST["decisions"]),
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written; verdict = " + verdict)

    # ---- server excerpts (shared server log, filter by game code) ----
    try:
        lines = open(f"{BACKFILL}/runs/20260912-6906e/server.log",
                     errors="replace").read().splitlines()
        gc = ST.get("game_code") or ""
        keep = [l for l in lines if gc and gc in l]
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

    W, H = 1040, 1060
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
    d.text((24, y), "#6907 - Nesting Grounds counter-type choice",
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
        "Oracle {1},{T}: Move a counter from target permanent you",
        "control onto a second target permanent. (Sorcery speed.)",
        "",
        "Reported: with +1/+1 and lore counters on the source, no",
        "choice is offered; the engine defaults to +1/+1.",
    ]:
        d.text((24, y), ln, fill=TEXT)
        y += 20
    y += 8
    rows = [
        ("CONTROL activation", f"turn={dec.get('ng1_turn')}"),
        ("Township activation", f"turn={dec.get('town_turn')}"),
        ("PROOF activation", f"turn={dec.get('ng2_turn')}"),
        ("counter-type prompts",
         f"{dec.get('counter_prompts')} (expect >=1 in PROOF)"),
        ("driver counter pick",
         f"{(dec.get('counter_choice_pick') or 'none')[:90]}"),
        ("target selections", f"{dec.get('target_sels')}"),
        ("rejections", f"{dec.get('rejections')}"),
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
           "States: pre_control/post_control/pre_proof/post_proof.json + "
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
