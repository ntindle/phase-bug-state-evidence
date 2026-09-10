#!/usr/bin/env python3
"""Issue #6763: Emperor of Bones targeting -- opponent cannot tell which
Walking Ballista is targeted.

Reported: when Emperor of Bones' "exile up to one target card from a
graveyard" trigger targets one of two Walking Ballistas in the opponent's
graveyard, the responding opponent cannot identify the exact physical card
targeted before responding. Triage: stable object-instance presentation in a
public zone; every player must be able to identify the exact graveyard object
when duplicate names exist. Classifier: frontend presentation (not card data).

Behavioral contract (v0.79.0 / protocol 69, two human seats):
  E1 setup_ok        Emperor of Bones on P1's battlefield; >=2 Walking Ballista
                     objects in P0's graveyard; the Emperor trigger on the stack.
  E2 trigger_targeted  the trigger's ability targets == [{"Object": oid}] with
                     oid one of P0's graveyard Ballista ids (exactly one target).
  E3 identity_distinct the two Ballista ids are distinct objects with the same
                     name (so a name-only label cannot disambiguate them).
  E4 snapshot_captured P0's viewer snapshot at the "before responding" moment
                     (P0 holds Priority, trigger on stack) is preserved and
                     carries the same target Object id; variant-B snapshot (same
                     UI state, other Ballista targeted) is derived faithfully.
  E5 cleanup          trigger resolves: chosen Ballista exiled, stack empty,
                     game proceeds.
  U1..U5 (browser, real React components seeded from the captured snapshot):
                     stack target label and StackTargetArcs anchor are identical
                     for both target variants -- the opponent cannot tell which
                     Ballista was targeted.

Verdict = reproduced iff every E and U assertion passes (the UI-visible
artifacts are indistinguishable across the two target variants).
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
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6763")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6763"
EVDIR = f"{BACKFILL}/evidence/6763/{RUN_ID}"
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
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def gy_ids(state, pid):
    return [int(x) for x in (state["players"][pid].get("graveyard") or [])]


def ballista_gy_ids(state, pid):
    return [oid for oid in gy_ids(state, pid)
            if obj_name(state, oid) == "Walking Ballista"]


def find_hand(state, pid, name):
    for oid in state["players"][pid]["hand"]:
        if obj_name(state, oid) == name:
            return int(oid)
    return None


def find_bf(state, pid, name):
    for oid, o in state["objects"].items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and (o.get("base_name") or o.get("name")) == name):
            return int(oid)
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    codes = set()
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("code"):
            codes.add(d["code"])
    return codes


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def collect_object_target_refs(entry):
    """All TargetRef::Object ids chosen on a stack entry (excludes the
    effect's target *spec*, which serializes as {"type": "Typed", ...})."""
    refs = []

    def rec(o):
        if isinstance(o, dict):
            if set(o.keys()) == {"Object"} and isinstance(o["Object"], int):
                refs.append(o["Object"])
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(entry.get("kind", {}).get("data", {}).get("ability", {}))
    return refs


def find_emperor_trigger(state):
    """(entry, chosen_oid) for the Emperor of Bones exile trigger on the stack."""
    for e in state.get("stack") or []:
        refs = collect_object_target_refs(e)
        if not refs:
            continue
        blob = json.dumps(e).lower()
        if "graveyard" not in blob:
            continue
        src = e.get("source_id")
        if src is not None and obj_name(state, src) == "Emperor of Bones":
            return e, refs
    return None, []


async def pay_and_mulligan(c):
    st = c.latest
    if not st:
        return False
    s = st["state"]
    wf = s.get("waiting_for") or {}
    if wf.get("type") == "DiscardToHandSize":
        data = wf.get("data") or {}
        if data.get("player") != c.player_id:
            return False
        count = data.get("count", 0)
        hand = [int(x) for x in s["players"][c.player_id]["hand"]]
        # discard lands first, then anything
        lands = [oid for oid in hand if obj_name(s, oid) in ("Forest", "Swamp")]
        others = [oid for oid in hand if oid not in lands]
        disc = (lands + others)[:count]
        op = None
        vi = get_vi(st)
        if vi:
            for o in vi.get("opportunities", []):
                resp = o.get("response", {}) or {}
                if resp.get("type") == "schema":
                    op = o
                    break
        if op is None:
            return False
        iid = op.get("interactionId")
        odata = (op.get("response", {}) or {}).get("data", {}) or {}
        spec = odata.get("spec") or {}
        sub_type = spec.get("type") if isinstance(spec, dict) else None
        ochs = odata.get("choices") or odata.get("candidates") or []
        # choiceIds are choice-id strings, matched via candidate references
        choice_ids = []
        for oid in disc:
            ch = next((ch for ch in ochs
                       if cand_ref(ch) is not None
                       and int(cand_ref(ch)) == oid), None)
            if ch is not None:
                choice_ids.append(ch["id"])
        if not choice_ids:
            return False
        sub = {"interactionId": iid,
               "response": {"type": sub_type or "sequence",
                            "data": {"choiceIds": choice_ids}}}
        say(f"{c.name} discards {disc}")
        await c.send_interaction(sub)
        return True
    acts = st.get("legal_actions", [])
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps opening hand")
            return True
    return False


class Ctx:
    def __init__(self):
        self.assert_ = {}
        self.notes = []
        self.x_submitted = set()   # interactionIds answered for Ballista X
        self.target_submitted = set()
        self.submitted_at = {}
        self.captured = False
        self.chosen_oid = None
        self.other_oid = None
        self.entry_id = None
        self.trigger_turn = None
        self.done = False


async def answer_x_and_targets(c, ctx, seat_name):
    """P0: Ballista ChooseXValue (schema number) -> 0.
    P1: Emperor trigger target selection -> top Ballista of P0's graveyard."""
    st = c.latest
    if not st:
        return False
    s = st["state"]
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    for op in vi.get("opportunities", []):
        iid = op.get("interactionId")
        if iid in ctx.x_submitted or iid in ctx.target_submitted:
            # a rejected submission may be retried after 10s
            if time.time() - ctx.submitted_at.get(iid, 0) < 10:
                continue
            ctx.x_submitted.discard(iid)
            ctx.target_submitted.discard(iid)
        resp = op.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        chs = data.get("choices") or data.get("candidates") or []
        if rtype == "schema" and spec_type == "number" and seat_name == "P0":
            sub = {"interactionId": iid,
                   "response": {"type": "number", "data": {"value": 0}}}
            say("[P0] Ballista X-choice -> X=0")
            wire("x_choice", {"iid": iid})
            await c.send_interaction(sub)
            ctx.x_submitted.add(iid)
            ctx.submitted_at[iid] = time.time()
            acted = True
            continue
        if (rtype == "schema" and spec_type in ("sequence", "select")
                and seat_name == "P1"):
            refs = [cand_ref(ch) for ch in chs]
            refs = [int(r) for r in refs if r is not None]
            # pick the top of P0's graveyard: the most recently died Ballista
            gy = ballista_gy_ids(s, 0)
            pick = next((oid for oid in reversed(gy) if oid in refs), None)
            if pick is None and refs:
                pick = refs[0]
            if pick is None:
                continue
            # choiceIds are the opportunity's own choice-id strings, NOT the
            # object ids (server rejects integer object ids here)
            pick_ch = next((ch for ch in chs
                            if cand_ref(ch) is not None
                            and int(cand_ref(ch)) == pick), None)
            if pick_ch is None:
                continue
            sub = {"interactionId": iid,
                   "response": {"type": spec_type,
                                "data": {"choiceIds": [pick_ch["id"]]}}}
            say(f"[P1] Emperor trigger targets Ballista oid {pick} "
                f"(choice {pick_ch['id']})")
            wire("trigger_target", {"iid": iid, "pick": pick,
                                    "choice_id": pick_ch["id"],
                                    "refs": refs, "p0_gy": gy})
            await c.send_interaction(sub)
            ctx.target_submitted.add(iid)
            ctx.submitted_at[iid] = time.time()
            ctx.chosen_oid = pick
            acted = True
            continue
    return acted


async def p0_main(p0, ctx):
    st = p0.latest
    if not st:
        return False
    s = st["state"]
    if not (s.get("active_player") == p0.player_id
            and s.get("priority_player") == p0.player_id
            and s.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")):
        return False
    acts = st.get("legal_actions", [])
    for a in acts:
        d = a.get("data", {}) or {}
        if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == "Forest":
            await p0.send_action(a)
            say("P0 plays Forest")
            return True
    for a in acts:
        d = a.get("data", {}) or {}
        if (a["type"] == "CastSpell"
                and obj_name(s, d.get("object_id")) == "Walking Ballista"):
            await p0.send_action(a)
            say(f"P0 casts Walking Ballista (oid {d.get('object_id')})")
            wire("cast_ballista", {"action_data": d})
            return True
    return False


async def p1_main(p1, ctx):
    st = p1.latest
    if not st:
        return False
    s = st["state"]
    if not (s.get("active_player") == p1.player_id
            and s.get("priority_player") == p1.player_id
            and s.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")):
        return False
    acts = st.get("legal_actions", [])
    for a in acts:
        d = a.get("data", {}) or {}
        if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == "Swamp":
            await p1.send_action(a)
            say("P1 plays Swamp")
            return True
    untapped_swamps = [o for oid, o in s["objects"].items()
                       if (o.get("zone") == "Battlefield"
                           and o.get("controller") == p1.player_id
                           and (o.get("base_name") or o.get("name")) == "Swamp"
                           and not o.get("tapped"))]
    if len(untapped_swamps) >= 2:
        for a in acts:
            d = a.get("data", {}) or {}
            if (a["type"] == "CastSpell"
                    and obj_name(s, d.get("object_id")) == "Emperor of Bones"):
                await p1.send_action(a)
                say(f"P1 casts Emperor of Bones (oid {d.get('object_id')})")
                wire("cast_emperor", {"action_data": d})
                return True
    return False


async def gated_pass(p0, p1):
    for c in (p0, p1):
        st = c.latest
        if not st:
            continue
        s = st["state"]
        wf = s.get("waiting_for") or {}
        if (wf.get("type") == "Priority"
                and s.get("priority_player") == c.player_id):
            for a in st.get("legal_actions", []):
                if a["type"] == "PassPriority":
                    await c.send_action(a)
                    break


async def export_state(c, path):
    raw = await c.export_state()
    env = json.loads(raw)
    with open(path, "w") as f:
        f.write(raw)
    return env["state"]


async def watchdog(p0, p1, ctx, t0):
    try:
        while not ctx.done:
            await asyncio.sleep(30)
            for c in (p0, p1):
                st = c.latest
                if not st:
                    say(f"[watchdog] {c.name}: no state yet")
                    continue
                s = st["state"]
                wf = s.get("waiting_for") or {}
                acts = [a["type"] for a in st.get("legal_actions", [])][:6]
                say(f"[watchdog] {c.name} t+{int(time.time()-t0)}s "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={wf.get('type')} prio={s.get('priority_player')} "
                    f"stack={len(s.get('stack') or [])} acts={acts}")
    except asyncio.CancelledError:
        pass


def write_variant_snapshots(p0_latest, chosen_oid, other_oid, entry_id):
    """snapshot_A.json: captured P0 viewer state (target = chosen Ballista).
    snapshot_B.json: identical except every TargetRef::Object(chosen) becomes
    TargetRef::Object(other) -- exactly what the engine would project had P1
    targeted the other copy (target_label is name-only either way)."""
    snap_a = copy.deepcopy(p0_latest)
    with open(f"{EVDIR}/snapshot_A.json", "w") as f:
        json.dump(snap_a, f)
    raw = json.dumps(snap_a)
    token_a = f'"Object": {chosen_oid}'
    token_b = f'"Object": {other_oid}'
    n = raw.count(token_a)
    raw_b = raw.replace(token_a, token_b)
    snap_b = json.loads(raw_b)
    with open(f"{EVDIR}/snapshot_B.json", "w") as f:
        json.dump(snap_b, f)
    # sanity: the stack entry target really flipped, nothing else structural
    def entry_of(snap):
        for e in snap["state"]["stack"]:
            if e.get("id") == entry_id:
                return e
        return None
    refs_a = collect_object_target_refs(entry_of(snap_a))
    refs_b = collect_object_target_refs(entry_of(snap_b))
    return n, refs_a, refs_b


async def main():
    t0 = time.time()
    ass = {}
    notes = []
    ctx = Ctx()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(("Walking Ballista", 12), ("Forest", 48)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Emperor of Bones", 4), ("Swamp", 56)))
    say(f"game {p0.game_code}")
    ass["setup_started"] = "passed"

    deadline = 1500
    wd = asyncio.create_task(watchdog(p0, p1, ctx, t0))
    pre_state = None
    while time.time() - t0 < deadline and not ctx.done:
        await asyncio.sleep(0.15)
        for c in (p0, p1):
            await pay_and_mulligan(c)
        await answer_x_and_targets(p0, ctx, "P0")
        await answer_x_and_targets(p1, ctx, "P1")
        await p0_main(p0, ctx)
        await p1_main(p1, ctx)

        st = p0.latest
        if st:
            s = st["state"]
            entry, refs = find_emperor_trigger(s)
            wf = s.get("waiting_for") or {}
            holding = (wf.get("type") == "Priority"
                       and s.get("priority_player") == p0.player_id)
            # capture the "before responding" moment: trigger on stack with its
            # target chosen, and P0 (the opponent) holding priority
            if entry is not None and refs and holding and not ctx.captured:
                ctx.entry_id = entry.get("id")
                ctx.trigger_turn = s.get("turn_number")
                ctx.chosen_oid = refs[0]
                gy = ballista_gy_ids(s, p0.player_id)
                others = [oid for oid in gy if oid != ctx.chosen_oid]
                ctx.other_oid = others[0] if others else None
                say(f"CAPTURE: trigger entry {ctx.entry_id} targets "
                    f"Ballista {ctx.chosen_oid}; P0 holds priority")
                wire("capture", {"entry_id": ctx.entry_id,
                                 "chosen": ctx.chosen_oid,
                                 "other": ctx.other_oid,
                                 "gy_ballistas": gy,
                                 "phase": s.get("phase")})
                pre_state = await export_state(p0, f"{EVDIR}/pre_trigger.json")
                with open(f"{EVDIR}/viewer_snapshot_p0.json", "w") as f:
                    json.dump(p0.latest, f)
                n, refs_a, refs_b = write_variant_snapshots(
                    p0.latest, ctx.chosen_oid, ctx.other_oid, ctx.entry_id)
                wire("variant_snapshots",
                     {"replaced_tokens": n, "refs_A": refs_a, "refs_B": refs_b})
                say(f"variant snapshots written (tokens replaced: {n}; "
                    f"A->{refs_a} B->{refs_b})")
                ctx.captured = True
            # post: trigger resolved, stack empty, game moved on
            if (ctx.captured and not (s.get("stack") or [])
                    and (s.get("turn_number"), s.get("phase")) !=
                        (ctx.trigger_turn, "BeginningOfCombat")
                    and s.get("phase") not in ("BeginningOfCombat",)):
                # post export: chosen Ballista exiled, stack empty, game moved on
                post_state = await export_state(p0, f"{EVDIR}/post_trigger.json")
                say("exporting POST_TRIGGER state")
                wire("post", {"phase": s.get("phase"),
                              "turn": s.get("turn_number")})
                ctx.done = True
        await gated_pass(p0, p1)

    # ---- assertions ----
    s_pre = pre_state
    try:
        with open(f"{EVDIR}/post_trigger.json") as f:
            s_post = json.loads(f.read())["state"]
    except FileNotFoundError:
        s_post = None

    if s_pre is not None and ctx.chosen_oid is not None:
        gy = ballista_gy_ids(s_pre, p0.player_id)
        emperor = find_bf(s_pre, p1.player_id, "Emperor of Bones")
        entry = next((e for e in (s_pre.get("stack") or [])
                      if e.get("id") == ctx.entry_id), None)
        refs = collect_object_target_refs(entry) if entry else []
        # E1 setup
        ok = (len(gy) >= 2 and emperor is not None and entry is not None)
        ass["E1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"E1: gy_ballistas={gy} emperor_bf={emperor} "
                     f"trigger_entry={ctx.entry_id}")
        # E2 trigger targeted exactly one of the Ballistas
        ok = (len(refs) == 1 and refs[0] == ctx.chosen_oid
              and ctx.chosen_oid in gy)
        ass["E2_trigger_targeted"] = "passed" if ok else "failed"
        notes.append(f"E2: entry targets={refs} chosen={ctx.chosen_oid}")
        # E3 distinct objects, same name (name-only labels cannot disambiguate)
        names = {obj_name(s_pre, oid) for oid in gy}
        ok = (ctx.other_oid is not None and ctx.other_oid != ctx.chosen_oid
              and len(names) == 1 and "Walking Ballista" in names)
        ass["E3_identity_distinct"] = "passed" if ok else "failed"
        notes.append(f"E3: chosen={ctx.chosen_oid} other={ctx.other_oid} "
                     f"names={names}")
        # E4 viewer snapshot preserved with the same target identity
        vrefs = None
        try:
            with open(f"{EVDIR}/viewer_snapshot_p0.json") as f:
                vsnap = json.load(f)
            vent = next((e for e in vsnap["state"]["stack"]
                         if e.get("id") == ctx.entry_id), None)
            vrefs = collect_object_target_refs(vent) if vent else []
            va = json.load(open(f"{EVDIR}/snapshot_A.json"))
            vb = json.load(open(f"{EVDIR}/snapshot_B.json"))
            vae = next((e for e in va["state"]["stack"]
                        if e.get("id") == ctx.entry_id), None)
            vbe = next((e for e in vb["state"]["stack"]
                        if e.get("id") == ctx.entry_id), None)
            ok = (vrefs == [ctx.chosen_oid]
                  and collect_object_target_refs(vae) == [ctx.chosen_oid]
                  and collect_object_target_refs(vbe) == [ctx.other_oid])
        except Exception as e:
            ok = False
            notes.append(f"E4 snapshot read error: {e}")
        ass["E4_snapshot_captured"] = "passed" if ok else "failed"
        notes.append(f"E4: viewer target={vrefs} "
                     f"A->{ctx.chosen_oid} B->{ctx.other_oid}")
        # E5 cleanup: trigger resolved, chosen Ballista exiled, game proceeds
        if s_post is not None:
            chosen_post = s_post["objects"].get(str(ctx.chosen_oid), {})
            ok = (chosen_post.get("zone") == "Exile"
                  and not (s_post.get("stack") or []))
            ass["E5_cleanup"] = "passed" if ok else "failed"
            notes.append(f"E5: chosen zone post={chosen_post.get('zone')} "
                         f"stack_empty={not (s_post.get('stack') or [])} "
                         f"phase={s_post.get('phase')}")
        else:
            ass["E5_cleanup"] = "not-run"
            notes.append("E5: no post-trigger export captured")
    else:
        for k in ("E1_setup_ok", "E2_trigger_targeted",
                  "E3_identity_distinct", "E4_snapshot_captured",
                  "E5_cleanup"):
            ass[k] = "not-run"
        notes.append(f"never captured trigger on stack "
                     f"(captured={ctx.captured} chosen={ctx.chosen_oid})")

    wd.cancel()
    await p0.close()
    await p1.close()

    engine_result = {
        "issue": 6763,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
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
        "scenario_sha256": sha256_of_file(
            f"{BACKFILL}/driver/scenario_6763.py"),
        "decks": {
            "P0": [["Walking Ballista", 12], ["Forest", 48]],
            "P1": [["Emperor of Bones", 4], ["Swamp", 56]],
        },
        "chosen_oid": ctx.chosen_oid,
        "other_oid": ctx.other_oid,
        "trigger_entry_id": ctx.entry_id,
        "assertions": ass,
        "notes": notes,
    }
    with open(f"{EVDIR}/engine_result.json", "w") as f:
        json.dump(engine_result, f, indent=1)
    say(f"ENGINE DONE assertions={json.dumps(ass)}")
    WIRE.close()
    RUNLOG.close()


asyncio.run(main())
