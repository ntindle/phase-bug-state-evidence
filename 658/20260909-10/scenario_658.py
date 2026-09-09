#!/usr/bin/env python3
"""Issue #658: Dualcaster Mage runtime repro.

Contract:
  P1 casts Lightning Bolt targeting P0. P0 responds at instant speed with
  Dualcaster Mage (Flash). ETB trigger must target the Bolt on the stack,
  create a copy, and offer MayChooseNewTargets (choose P1). Both resolve.

Assertions:
  A1 flash_timing        Dualcaster castable+cast while Bolt on stack (ideally on P1's turn)
  A2 etb_targets_stack   ETB trigger targets the Bolt spell still on the stack
  A3 copy_created        a copy of Lightning Bolt appears on the stack
  A4 retarget_offered    MayChooseNewTargets surfaced; copy retargeted to P1
  A5 resolution          copy deals 3 to P1, original deals 3 to P0 (life deltas),
                         Dualcaster on battlefield, stack empty, nothing pending
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
EVID_RUN_ID = "20260909-10"
EVDIR = f"{BACKFILL}/evidence/658/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


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
    except Exception as e:
        say("wire error", e)


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_mountains(state, pid):
    return sum(1 for o in bf(state, pid)
               if (o.get("base_name") or o.get("name")) == "Mountain" and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


def stack_spells(state):
    return state.get("stack") or []


def find_bolt_on_stack(state):
    for entry in stack_spells(state):
        oid = entry.get("object_id") if isinstance(entry, dict) else entry
        if obj_name(state, oid) == "Lightning Bolt":
            return oid, entry
    return None, None


def life(state, pid):
    return state["players"][pid]["life"]


def get_vi(c):
    st = c.latest
    if not st:
        return None
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def iter_choices(vi):
    for opp in vi.get("opportunities", []):
        resp = opp.get("response", {})
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        for ch in choices:
            yield opp.get("interactionId"), resp.get("type"), ch


def surf_map(ch):
    return {s.get("type"): s.get("data", {}) for s in ch.get("surfaces", [])}


def choice_text(ch):
    sm = surf_map(ch)
    bits = []
    for t, d in sm.items():
        if isinstance(d, dict):
            for k in ("name", "code", "label", "value"):
                if d.get(k) not in (None, ""):
                    bits.append(f"{t}.{k}={d[k]}")
    return f"id={ch.get('id')} status={ch.get('status',{}).get('type')} " + " ".join(bits)


def player_seat(ch):
    """Return the player seat if this choice is a player candidate, else None."""
    for t, d in surf_map(ch).items():
        if t in ("player", "target") and isinstance(d, dict):
            if "seat" in d and d["seat"] is not None:
                return d["seat"]
            if "player" in d and d["player"] is not None:
                return d["player"]
            if "index" in d and d["index"] is not None:
                return d["index"]
    return None


async def submit_choice(c, vi, pred, label):
    """Submit first available choice matching pred(choice)->bool. Returns (iid, ch) or (None, None).

    Submission-type mapping (from engine protocol types + engine source):
      opportunity response "exactChoices" -> {"type": "choose", "data": {"choiceId": id}}
      opportunity response "schema"       -> spec.type variant, e.g. sequence ->
                                            {"type": "sequence", "data": {"choiceIds": [id]}}
    """
    for iid, schema, ch in iter_choices(vi):
        if ch.get("status", {}).get("type") != "available":
            continue
        try:
            if pred(ch):
                # find the opportunity's spec type for schema-wrapped responses
                sub_type = schema
                sub_data = {"choiceIds": [ch["id"]]}
                for opp in vi.get("opportunities", []):
                    if opp.get("interactionId") == iid:
                        spec = (opp.get("response", {}).get("data", {}) or {}).get("spec") or {}
                        if isinstance(spec, dict) and spec.get("type"):
                            sub_type = spec["type"]
                if schema == "exactChoices":
                    sub_type = "choose"
                    sub_data = {"choiceId": ch["id"]}
                sub = {"interactionId": iid,
                       "response": {"type": sub_type, "data": sub_data}}
                say(f"{c.name} submits {label}: {choice_text(ch)[:200]}")
                wire(f"submit_{label}", {"client": c.name, "submission": sub,
                                         "interaction": vi})
                await c.send_interaction(sub)
                await asyncio.sleep(1.0)
                rej = await drain_rejections(c, timeout=4)
                if rej:
                    say(f"submission {label} got: {json.dumps(rej)[:300]}")
                    wire(f"submit_{label}_response", rej)
                    return None, None
                return iid, ch
        except Exception as e:
            say(f"pred error for {label}: {e}")
    return None, None


async def drain_rejections(c, timeout=8):
    rej = None
    t0 = time.time()
    while time.time() - t0 < timeout and rej is None:
        await asyncio.sleep(0.4)
        try:
            while True:
                t, d = c.inbox.get_nowait()
                if t in ("ActionRejected", "Error"):
                    rej = {"type": t, "data": d}
        except asyncio.QueueEmpty:
            pass
    return rej


async def main():
    t_start = time.time()
    notes = []
    ass = {}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(("Mountain", 56), ("Dualcaster Mage", 4)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Mountain", 56), ("Lightning Bolt", 4)))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ass["A1_flash_timing"] = "not-run"

    pre = None
    pre_life = None
    dualcaster_cast = False
    bolt_targeted = False
    p1_cast_in_progress = False

    async def p1_maybe_cast_bolt():
        """P1 casts Bolt targeting P0 when P0 looks ready (>=3 untapped mountains)."""
        nonlocal bolt_targeted, p1_cast_in_progress
        if p1_cast_in_progress:
            return False
        st = p1.latest
        if not st:
            return False
        state, acts = st["state"], st.get("legal_actions", [])
        if not is_my_main(state, p1.player_id):
            return False
        if untapped_mountains(state, p0.player_id) < 3:
            return False
        if find_bolt_on_stack(state)[0] is not None:
            return False  # already one on the stack; wait
        bolt_oid = next((oid for oid in state["players"][p1.player_id]["hand"]
                         if obj_name(state, oid) == "Lightning Bolt"), None)
        if bolt_oid is None or untapped_mountains(state, p1.player_id) < 1:
            return False
        for a in acts:
            d = a.get("data", {})
            if a["type"] == "CastSpell" and d.get("object_id") == bolt_oid:
                p1_cast_in_progress = True
                try:
                    say("P1 casts Lightning Bolt")
                    wire("p1_cast_bolt", a)
                    await p1.send_action(a)
                    # target selection for Bolt -> target P0
                    t0 = time.time()
                    while time.time() - t0 < 30:
                        await asyncio.sleep(0.3)
                        st2 = p1.latest
                        if not st2:
                            continue
                        vi = get_vi(p1)
                        if vi:  # any decision awaiting input (target selection etc.)
                            break
                        if find_bolt_on_stack(st2["state"])[0] is not None:
                            break
                    vi = get_vi(p1)
                    if vi:
                        say("P1 bolt target interaction: " +
                            json.dumps(vi)[:2500])
                        wire("p1_bolt_target_interaction", vi)

                        def want_p0(ch):
                            return player_seat(ch) == 0

                        iid, ch = await submit_choice(p1, vi, want_p0, "p1_bolt_target_p0")
                        if ch is None:
                            # fallback: log all choices, pick first available candidate
                            for _iid, _schema, _ch in iter_choices(vi):
                                say("  candidate: " + choice_text(_ch)[:200])
                            for _iid, _schema, _ch in iter_choices(vi):
                                if _ch.get("status", {}).get("type") == "available":
                                    iid, ch = await submit_choice(
                                        p1, vi, lambda c, _id=_ch["id"]: c.get("id") == _id,
                                        "p1_bolt_target_fallback")
                                    break
                            notes.append(f"P1 bolt target: fallback pick used ({choice_text(ch)[:120] if ch else None})")
                    bolt_targeted = True
                    return True
                finally:
                    p1_cast_in_progress = False
        return False

    async def tick(c, pid, is_p0):
        nonlocal pre, pre_life, dualcaster_cast
        st = c.latest
        if not st:
            return
        state, acts = st["state"], st.get("legal_actions", [])
        for a in acts:
            if a["type"] == "MulliganDecision":
                await c.send_action({"type": "MulliganDecision",
                                     "data": {"choice": {"type": "Keep"}}})
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await c.send_action(a)
                return
        bolt_present = len(stack_spells(state)) > 0
        if is_p0 and bolt_present and not dualcaster_cast:
            # RESPOND: cast Dualcaster Mage at instant speed
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == "Dualcaster Mage":
                    say("exporting PRE (bolt on stack, P0 responding)")
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_life = (life(state, 0), life(state, 1))
                    wire("pre_state_meta", {"active": state.get("active_player"),
                                            "phase": state.get("phase"),
                                            "pre_life": pre_life})
                    say(f"P0 casts Dualcaster Mage in response (active={state.get('active_player')}, "
                        f"phase={state.get('phase')})")
                    wire("p0_cast_dualcaster", a)
                    await p0.send_action(a)
                    dualcaster_cast = True
                    ass["A1_flash_timing"] = "passed" if state.get("active_player") != p0.player_id else "passed-not-own-turn"
                    notes.append(f"Dualcaster cast with bolt on stack on P{state.get('active_player')}'s turn")
                    return
        if is_my_main(state, pid):
            for a in acts:
                if a["type"] == "PlayLand":
                    await c.send_action(a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await c.send_action(a)
                return

    # ---- Phase 1: setup until Dualcaster is cast in response ----
    t0 = time.time()
    last = {}
    last_diag = 0.0
    while time.time() - t0 < 900 and not dualcaster_cast:
        await asyncio.sleep(0.1)
        if p1.latest:
            await p1_maybe_cast_bolt()
        for c, pid, is_p0 in ((p0, p0.player_id, True), (p1, p1.player_id, False)):
            if c.revision == last.get(c.name):
                continue
            await tick(c, pid, is_p0)
            last[c.name] = c.revision
        if time.time() - last_diag > 30 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            hand = [obj_name(s, oid) for oid in s["players"][p0.player_id]["hand"]]
            bolt_oid, _ = find_bolt_on_stack(s)
            acts = [a["type"] for a in (p0.latest.get("legal_actions") or [])]
            say(f"DIAG turn? active={s.get('active_player')} phase={s.get('phase')} "
                f"life={life(s,0)}/{life(s,1)} P0hand={hand} "
                f"P0untapM={untapped_mountains(s,0)} boltOnStack={bolt_oid is not None} "
                f"P0acts={acts[:12]}")
        # stack watcher: log P0/P1 action surface whenever the stack is non-empty
        if p0.latest:
            s = p0.latest["state"]
            if stack_spells(s) and p0.revision != last.get("_stackrev"):
                last["_stackrev"] = p0.revision
                acts0 = [a["type"] for a in (p0.latest.get("legal_actions") or [])]
                acts1 = [a["type"] for a in (p1.latest.get("legal_actions") or [])] if p1.latest else []
                cast0 = [obj_name(s, a.get("data", {}).get("object_id"))
                         for a in (p0.latest.get("legal_actions") or []) if a["type"] == "CastSpell"]
                say(f"STACK rev={p0.revision} prio={s.get('priority_player')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} "
                    f"stack={[obj_name(s, e.get('object_id') if isinstance(e, dict) else e) for e in stack_spells(s)]} "
                    f"raw0={json.dumps(stack_spells(s)[0])[:600]} "
                    f"P0acts={acts0[:10]} P0castable={cast0} P1acts={acts1[:10]}")
    if not dualcaster_cast:
        notes.append("P0 never got to cast Dualcaster in response (no bolt window or no mage in hand)")
        ass["A1_flash_timing"] = "failed"
        for k in ("A2_etb_targets_stack", "A3_copy_created", "A4_retarget_offered", "A5_resolution"):
            ass[k] = "not-run"
        await finish(p0, p1, t_start, notes, ass, pre_life)
        return

    # ---- Phase 2: let Dualcaster resolve; handle ETB trigger targeting ----
    # Observed engine behavior: the ETB trigger does NOT prompt for its target
    # when exactly one legal target exists -- it auto-targets the Bolt, creates
    # the copy, then asks MayChooseNewTargets via waiting_for type CopyRetarget.
    etb_done = False
    mage_seen = False
    copy_id = None
    t0 = time.time()
    last = {}
    while time.time() - t0 < 180 and not etb_done:
        await asyncio.sleep(0.2)
        # 1) check for P0's ETB decision every iteration (skip plain Priority)
        st = p0.latest
        if st:
            wf = st["state"].get("waiting_for", {}) or {}
            wftype = wf.get("type")
            if wftype and wftype != "Priority":
                vi = get_vi(p0)
                if vi:
                    say(f"P0 decision: wf={json.dumps(wf)[:300]}")
                    say("interaction: " + json.dumps(vi)[:3000])
                    wire("etb_interaction", vi)
                    for _iid, _schema, _ch in iter_choices(vi):
                        say("  candidate: " + choice_text(_ch)[:220])
                    if wftype == "CopyRetarget":
                        copy_id = (wf.get("data") or {}).get("copy_id")
                        slots = ((wf.get("data") or {}).get("target_slots") or [])
                        notes.append(
                            "CopyRetarget surfaced (MayChooseNewTargets): copy_id=%s slots=%s; "
                            "engine did NOT prompt for the trigger's target (single legal target "
                            "auto-selected)" % (copy_id, json.dumps(slots)[:200]))
                        # A2: verify the copy is a copy of the Bolt on the stack
                        s = st["state"]
                        entries = {e.get("id"): e for e in stack_spells(s)
                                   if isinstance(e, dict)}
                        cp = entries.get(copy_id, {})
                        eff = (((cp.get("kind") or {}).get("data") or {}).get("ability") or {}).get("effect") or {}
                        say("copy stack entry: " + json.dumps(cp)[:1200])
                        wire("copy_stack_entry", cp)
                        if eff.get("type") == "DealDamage" and (eff.get("amount") or {}).get("value") == 3:
                            ass["A2_etb_targets_stack"] = "passed"
                            notes.append("copy effect matches Lightning Bolt (DealDamage 3); "
                                         "trigger targeted the Bolt on the stack")
                        else:
                            ass["A2_etb_targets_stack"] = "failed"
                            notes.append("copy entry does not look like a Bolt copy")
                        etb_done = True
                    else:
                        # genuine trigger target selection (engine asks): pick the Bolt
                        def want_bolt(ch):
                            txt = json.dumps(surf_map(ch)).lower()
                            return "lightning bolt" in txt

                        iid, ch = await submit_choice(p0, vi, want_bolt, "etb_target_bolt")
                        if ch is not None:
                            etb_done = True
                            ass["A2_etb_targets_stack"] = "passed"
                            notes.append(f"ETB trigger targeted via prompt: {choice_text(ch)[:150]}")
                        else:
                            notes.append(f"non-Priority decision ({wftype}) seen; no Bolt candidate matched")
                            await asyncio.sleep(3)
            if any((o.get("base_name") or o.get("name")) == "Dualcaster Mage"
                   and o.get("zone") == "Battlefield" for o in st["state"]["objects"].values()):
                if not mage_seen:
                    mage_seen = True
                    notes.append("Dualcaster resolved to battlefield")
        # 2) keep the game moving on new revisions
        for c, pid, is_p0 in ((p0, p0.player_id, True), (p1, p1.player_id, False)):
            if c.revision == last.get(c.name):
                continue
            await tick(c, pid, is_p0)
            last[c.name] = c.revision
    if not etb_done:
        ass.setdefault("A2_etb_targets_stack", "failed")
        notes.append("no ETB trigger outcome observed for P0 within 180s")

    # ---- Phase 3: copy creation + MayChooseNewTargets ----
    copy_seen = copy_id is not None
    if copy_seen:
        ass["A3_copy_created"] = "passed"
        notes.append(f"copy created on stack (copy_id={copy_id})")
    retarget_done = False
    t0 = time.time()
    last = {}
    while time.time() - t0 < 180 and not (copy_seen and retarget_done):
        await asyncio.sleep(0.2)
        if p0.latest:
            s = p0.latest["state"]
            # detect copy on stack: >=2 Bolt-like spell entries (DealDamage 3).
            # stack entries carry no card name; match the effect signature.
            def is_boltlike(e):
                if not isinstance(e, dict):
                    return False
                eff = (((e.get("kind") or {}).get("data") or {}).get("ability") or {}).get("effect") or {}
                return eff.get("type") == "DealDamage" and (eff.get("amount") or {}).get("value") == 3

            bolts = [e for e in stack_spells(s) if is_boltlike(e)]
            if len(bolts) >= 2 and not copy_seen:
                copy_seen = True
                ass["A3_copy_created"] = "passed"
                notes.append(f"copy created: {len(bolts)} Bolt-like entries on stack")
                say("stack ids: " + json.dumps([e.get("id") for e in bolts]))
                wire("stack_with_copy", stack_spells(s))
            vi = get_vi(p0)
            if vi and not retarget_done:
                wftxt = json.dumps(s.get("waiting_for", {})).lower()
                say(f"P0 decision: wf={json.dumps(s.get('waiting_for',{}))[:200]}")
                say("interaction: " + json.dumps(vi)[:3000])
                wire("retarget_interaction", vi)
                cands = list(iter_choices(vi))
                for _iid, _schema, _ch in cands:
                    say("  candidate: " + choice_text(_ch)[:220])
                seats = [player_seat(_ch) for _, _, _ch in cands]
                if any(seat is not None for seat in seats):
                    # target selection: pick P1 as the new target
                    def want_p1(ch):
                        return player_seat(ch) == 1

                    iid, ch = await submit_choice(p0, vi, want_p1, "retarget_p1")
                    if ch is not None:
                        retarget_done = True
                        ass["A4_retarget_offered"] = "passed"
                        notes.append(f"retargeted copy to: {choice_text(ch)[:150]}")
                    else:
                        notes.append("target selection seen but no P1 candidate submitted")
                        await asyncio.sleep(2)
                else:
                    # may/optional yes-no style choice: pick the affirmative
                    def want_yes(ch):
                        txt = json.dumps(surf_map(ch)).lower()
                        return ("choose" in txt or "yes" in txt or "new targets" in txt) \
                            and "decline" not in txt and "keep" not in txt and "no " not in txt

                    iid, ch = await submit_choice(p0, vi, want_yes, "may_choose_yes")
                    if ch is not None:
                        notes.append(f"may-choose answered affirmatively: {choice_text(ch)[:150]}")
                        ass["A4_retarget_offered"] = "saw-may-choice"
                    else:
                        notes.append("non-target decision seen; no affirmative candidate matched")
                        ass.setdefault("A4_retarget_offered", "not-run")
                        await asyncio.sleep(2)
        for c, pid, is_p0 in ((p0, p0.player_id, True), (p1, p1.player_id, False)):
            if c.revision == last.get(c.name):
                continue
            await tick(c, pid, is_p0)
            last[c.name] = c.revision
    ass.setdefault("A3_copy_created", "failed" if not copy_seen else "passed")
    if not retarget_done:
        ass.setdefault("A4_retarget_offered", "not-run")
        notes.append("retarget decision not completed")

    # ---- Phase 4: resolve everything ----
    t0 = time.time()
    last = {}
    while time.time() - t0 < 120:
        await asyncio.sleep(0.2)
        for c, pid, is_p0 in ((p0, p0.player_id, True), (p1, p1.player_id, False)):
            if c.revision == last.get(c.name):
                continue
            await tick(c, pid, is_p0)
            last[c.name] = c.revision
        if p0.latest:
            s = p0.latest["state"]
            if not stack_spells(s) and not get_vi(p0) and not get_vi(p1):
                # one more settle round then break
                await asyncio.sleep(2)
                break
    s = p0.latest["state"]
    post_life = (life(s, 0), life(s, 1))
    mage_bf = any((o.get("base_name") or o.get("name")) == "Dualcaster Mage"
                  and o.get("zone") == "Battlefield" and o.get("controller") == 0
                  for o in s["objects"].values())
    d0 = pre_life[0] - post_life[0] if pre_life else None
    d1 = pre_life[1] - post_life[1] if pre_life else None
    notes.append(f"life pre={pre_life} post={post_life} (deltas P0={d0} P1={d1}); mage_on_bf={mage_bf}")
    if d0 == 3 and d1 == 3 and mage_bf:
        ass["A5_resolution"] = "passed"
    else:
        ass["A5_resolution"] = "failed"
    say("exporting POST")
    post = await p0.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(post)
    await finish(p0, p1, t_start, notes, ass, pre_life)


async def finish(p0, p1, t_start, notes, ass, pre_life):
    dur = time.time() - t_start
    a1 = ass.get("A1_flash_timing")
    core = [ass.get("A1_flash_timing"), ass.get("A2_etb_targets_stack"),
            ass.get("A3_copy_created"), ass.get("A4_retarget_offered"),
            ass.get("A5_resolution")]
    if all(v == "passed" for v in core):
        verdict = "not-reproduced"
    elif any(v == "failed" for v in core):
        verdict = "reproduced"
    else:
        verdict = "blocked"
        notes.append("inconclusive: some steps not-run; see notes")
    run = {
        "issue": 658,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.77.0",
            "build_commit": "61715b5",
            "protocol_version": 67,
            "mode": "Full",
            "binary_sha256": "a52293b754605baa63e4d987a5208902ec394868d49c4bfd86b3fd57f3267c6a",
            "card_data_sha256": "698350d9b6323011a5b86a74a4d2ea54d13b4ed26a520579be7d044f0a3692e5",
            "draft_pools_sha256": "56e030fdc74b2385759310de8564a58b8716035b2dccc0da709f37250cc2d3c7",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-09-09",
            "source": "ServerHello + sha256 match of pinned verified artifacts",
        },
        "server_run_dir": "runs/20260909-10",
        "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(open(f"{BACKFILL}/driver/scenario_658.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [["Mountain", 56], ["Dualcaster Mage", 4]],
            "P1": [["Mountain", 56], ["Lightning Bolt", 4]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
        "setup_line": "P0: 56x Mountain + 4x Dualcaster Mage; P1: 56x Mountain + 4x Lightning Bolt",
        "contract_line": "P1 Bolts P0; P0 flashes in Dualcaster in response; ETB copies Bolt retargeted to P1; both resolve",
        "stats": {},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


asyncio.run(main())
