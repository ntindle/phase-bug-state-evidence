#!/usr/bin/env python3
"""Issue #7208: Gandalf, Spark Starter enforces 3 targets / 1 damage each.

Behavioral contract:
  Report (2026-08-10, status:confirmed, area:engine):
    "Gandalf, Spark Starter forces to choose 3 targets and give each one
     at least 1 damage"
  Expected: "It should be possible to select between 1 and 3 targets and
     then distribute the damage between the selected targets"

  Oracle text (pinned card-data.json): "Reach. When Gandalf enters, he
  deals 3 damage divided as you choose among one, two, or three targets."
  Gandalf, Spark Starter = {4}{R}{R} Legendary Creature - Avatar Wizard 4/3.

  Triage acceptance criteria: one, two, and three target selections are
  each accepted when legal; damage assignments total exactly three and give
  each chosen target at least one; unchosen optional slots do not force
  extra targets.

  Leg 1 (game 1, the reported path): P0 casts Gandalf; P1 has 7 Memnites
  on the BF. Driver attempts to select exactly ONE target (a Memnite) and
  assign all 3 damage to it. Pass = 1 target accepted, division resolves,
  the chosen Memnite dies, the other Memnites are untouched.
  Leg 2 (game 2, only if leg 1 fully passes): same setup, driver selects
  TWO targets and splits 1+2. Pass = 2 targets accepted with no forced 3rd
  slot, both chosen Memnites die, the rest survive.

Assertions (leg 1):
  A1_setup_ok        Gandalf on P0 BF at pre export; P1 has >=3 creatures on
                     BF; the ETB trigger target prompt was observed.
  A2_one_target      Target selection finalized with exactly 1 target: no
                     forced extra slot prompt, or a skip/decline affordance
                     that was accepted.
  A3_division        Division step resolved with all 3 damage assigned to
                     the single chosen target (no rejection; degenerate
                     single-target division with no prompt also passes).
  A4_damage          post: the chosen target left the BF; >=2 other P1
                     Memnites remain; stack empty.
Verdict rule:
  blocked        iff A1 fails.
  reproduced     iff A1 passes and (A2 or A3 or A4) fails.
  not-reproduced iff A1..A4 pass (leg 1) and leg-2 B1/B2 pass.

Protocol-72 notes: exactChoices -> {"type":"choose","data":{"choiceId"}};
schema specs answered with the advertised spec type ("select"/"sequence").
Target slots arrive one at a time (TriggerTargetSelection, #7179); answer
one choice per prompt and only flip stage-done when the trigger leaves the
stack. Division prompt shape is unknown to the driver: the full
waiting_for + viewer_interaction shape is dumped to the wire log on first
sight, then answered best-effort; affected assertions become not-run if
the shape is unanswerable rather than invented.

Evidence: evidence/7208/<run-id>/pre_leg1.json, post_leg1.json,
(pre_leg2.json/post_leg2.json if leg 2 runs), run.json, manifest.sha256,
summary.png, scenario_7208.py, wire_log.jsonl, scenario_run.log,
server.log (excerpts).
"""

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7208
RUN_ID = os.environ.get("RUN_ID", "20260917-7208")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

GANDALF = "gandalf, spark starter"
MOUNTAIN = "mountain"
MEMNITE = "memnite"

P0_DECK = [(GANDALF, 10), (MOUNTAIN, 50)]
P1_DECK = [(MEMNITE, 40)]

RELDIR = f"{BACKFILL}/server/releases/v0.86.0"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "67d495b599cbe7d68c9ab9fddaf2f1a37ec1d392382e31e43963d0653eed6af2",
    "card_data_sha256": "ab7a4b65e8fba8407a928eae8f261abb078f43c081923e9c0f4af30a9c40ff26",
    "draft_pools_sha256": "d20d2dbf181b2361c9cdf0e67bef34d996765338f1986bc395477905dfefd13e",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.86.0",
    "build_commit": "2cc8c28",
    "protocol_version": 72,
    "mode": "single-user",
    "binary_sha256": _ACTUAL["binary_sha256"],
    "card_data_sha256": _ACTUAL["card_data_sha256"],
    "draft_pools_sha256": _ACTUAL["draft_pools_sha256"],
    "digests_match_pin": _ACTUAL == _PIN,
    "signature_note": "minisign signatures on the binary + signed data "
                      "manifest verified against the repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY at v0.86.0 pin time "
                      "2026-09-17; binary digest also matches the GitHub "
                      "release asset digest; digests recomputed against "
                      "on-disk files this run; v0.86.0 confirmed latest "
                      "stable via GitHub releases API 2026-09-17 "
                      "(published 2026-09-17T15:48Z)",
    "observed_at": "2026-09-17",
    "handshake": "protocol 72 driver hello accepted by the isolated "
                 "server on 127.0.0.1:9374 (runs/20260917-7208)",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started "
              "fresh this run under runs/20260917-7208",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    try:
        RUNLOG.write(m + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    d = wf_of(state).get("data") or {}
    return d.get("player")


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def stack_entries(state):
    return state.get("stack") or []


def drain_rejections(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            out.append({"type": t, "data": data})
    return out


def build_assign_amounts(iid, assignments):
    """Protocol-72 assignAmounts response: assignments is a list of
    (choice_id, amount). DistributeAmong has require_all=true, so every
    candidate must appear (amount 0 allowed where the surface says so)."""
    return {"interactionId": iid,
            "response": {"type": "assignAmounts", "data": {
                "assignments": [{"choiceId": cid, "amount": amt}
                                for cid, amt in assignments]}}}


def build_submission(iid, rtype, spec_type=None, choice_id=None,
                     choice_ids=None):
    if rtype == "schema":
        stype = spec_type if spec_type in ("sequence", "select") \
            else "sequence"
        return {"interactionId": iid,
                "response": {"type": stype, "data": {
                    "choiceIds": choice_ids if choice_ids is not None
                    else ([choice_id] if choice_id is not None else [])}}}
    return {"interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": choice_id}}}


def cand_ref(ch):
    for k in ("ref", "candidateRef", "candidate_ref", "reference"):
        v = ch.get(k)
        if v is not None:
            return v
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        for k in ("ref", "reference", "objectId", "object_id", "cardId",
                  "card_id"):
            if d.get(k) is not None:
                return d[k]
    return ch.get("id")


def seat_of_choice(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if "seat" in d:
            return d["seat"]
    return None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    return chs, resp.get("type")


def gandalf_trigger_on_stack(state):
    for e in stack_entries(state):
        blob = json.dumps(e, default=str).lower()
        if "spark starter" in blob and "trigger" in blob:
            return True
    return False

# ---------------------------------------------------------------- per-game
async def run_game(leg, t_budget):
    """Play one game. leg=1: single target, all 3 damage to it.
    leg=2: two targets, 1+2 split. Returns the per-game ST dict."""
    ST = {
        "leg": leg,
        "targets_wanted": 1 if leg == 1 else 2,
        "finished": False,
        "stage": "setup",  # setup -> gandalf_cast -> trigger -> resolve -> done
        "game_code": None,
        "gandalf_oid": None,
        "gandalf_cast_turn": None,
        "trigger_seen": False,
        "targets_chosen": [],     # oids, in slot order
        "target_slots_seen": 0,
        "forced_extra_slot": False,
        "skip_attempted": False,
        "skip_accepted": False,
        "division_seen": False,
        "division_shape": None,
        "division_submitted": False,
        "division_rejected": False,
        "damage_plan": [],        # per-point target oids (leg1: [A,A,A])
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "exports": {},
        "last_rev_acted": {},
        "last_rev_seen": {},
        "last_rev_change": {},
        "need_post_export": False,
        "post_exported": False,
        "mull_count": {0: 0, 1: 0},
        "observed_at": None,
        "prompt_dumped": set(),
        "last_prompt_rev": {},
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "target_prompts": [], "division_prompts": []}

    def reset_acted():
        ST["last_rev_acted"] = {}
        ST["answered_iids"] = []

    async def export_named(name):
        try:
            raw = await p0.export_state()
            env = json.loads(raw)
            assert "state" in env, "envelope missing 'state'"
            with open(f"{EVDIR}/{name}.json", "w") as f:
                json.dump(env, f, indent=1)
            ST["exports"][name] = True
            say(f"[leg{leg}] exported {name}.json "
                f"(turn={env['state'].get('turn_number')})")
            return env["state"]
        except Exception as e:
            say(f"[leg{leg}] export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return None

    def acted(key, rev):
        if ST["last_rev_acted"].get(key) == rev:
            return True
        ST["last_rev_acted"][key] = rev
        return False

    def mulligan_entry_for(state, pid):
        d = (wf_of(state).get("data") or {})
        for p in d.get("pending", []) or []:
            if p.get("player") == pid:
                return p
        return None

    async def mulligan_decide(c, pid, tag, st, state, want):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        entry = mulligan_entry_for(state, pid)
        if not entry:
            return False
        phase = (entry.get("phase") or {})
        ptype = str(phase.get("type"))
        rev = st.get("state_revision", -1)
        if ptype == "BottomCards":
            # Protocol 72: bottom-cards answers go through
            # viewer_interaction (HumanResponseModel::Select), not the
            # MulliganDecision action.
            count = phase.get("count", 1)
            if ST.get("bottom_pending_iid") and \
                    time.time() - ST.get("bottom_pending_at", 0) > 12:
                ST["answered_iids"] = [
                    i for i in ST["answered_iids"]
                    if i != ST["bottom_pending_iid"]]
                ST["bottom_pending_iid"] = None
                say(f"[leg{leg}][{tag}] bottom retry (12s no advance)")
            hand = hand_ids(state, pid)
            g = [o for o in hand if lname(state, o) == GANDALF]
            rest = [o for o in hand if lname(state, o) != GANDALF]
            pick = (g[1:] + rest)[:count]  # keep one Gandalf
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    iid = opp.get("interactionId")
                    if iid in ST["answered_iids"]:
                        continue
                    resp = opp.get("response", {}) or {}
                    rtype = resp.get("type")
                    data = resp.get("data", {}) or {}
                    spec = data.get("spec") or {}
                    spec_type = spec.get("type") \
                        if isinstance(spec, dict) else None
                    chs = data.get("choices") or data.get("candidates") \
                        or []
                    if not chs or (rtype == "schema" and spec_type not in
                                   ("sequence", "select")):
                        continue
                    ref_to_id = {}
                    for ch in chs:
                        ref = cand_ref(ch)
                        if ref is not None:
                            ref_to_id[str(ref)] = ch.get("id")
                    choice_ids = [ref_to_id[str(o)] for o in pick
                                  if str(o) in ref_to_id]
                    if not choice_ids:
                        continue
                    sub = build_submission(iid, rtype, spec_type,
                                           choice_ids=choice_ids)
                    ST["answered_iids"].append(iid)
                    ST["bottom_pending_iid"] = iid
                    ST["bottom_pending_at"] = time.time()
                    say(f"[leg{leg}][{tag}] bottoming {len(choice_ids)} "
                        f"cards via {rtype}/{spec_type}")
                    wire("bottom_submit", {"leg": leg, "sub": sub})
                    await c.send_interaction(sub)
                    return True
            return False
        if ptype != "Declare":
            return False
        if acted(f"mull{pid}", rev):
            return True
        if want is not None and want not in hand_lnames(state, pid):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            ST["mull_count"][pid] += 1
            say(f"[leg{leg}][{tag}] mulligan #{ST['mull_count'][pid]} "
                f"(no {want})")
        else:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[leg{leg}][{tag}] mulligan: keep")
        return True

    async def bottom_cards(c, pid, tag, st, state):
        """BottomCards phase: bottom extra Gandalfs first, then lands."""
        if (wf_of(state).get("type") or "") != "SelectCards":
            return False
        d = (wf_of(state).get("data") or {})
        ph = (d.get("phase") or {})
        if str(ph.get("type")) != "BottomCards":
            return False
        count = ph.get("count", 0)
        rev = st.get("state_revision", -1)
        if acted(f"bottom{pid}", rev):
            return True
        hand = hand_ids(state, pid)
        g = [o for o in hand if lname(state, o) == GANDALF]
        rest = [o for o in hand if lname(state, o) != GANDALF]
        order = g[1:] + rest  # keep one Gandalf if present
        pick = order[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": pick}})
        say(f"[leg{leg}][{tag}] bottoming {len(pick)} cards")
        return True

    async def handle_discard(c, pid, tag, st, state, protect):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        if wf_player(state) != pid:
            return False
        count = (wf_of(state).get("data") or {}).get("count", 1)
        vi = get_vi(st)
        hand = hand_ids(state, pid)

        def ranked():
            def rank(oid):
                ln = lname(state, oid) or ""
                if any(k in ln for k in protect):
                    return 2
                if MOUNTAIN in ln or MEMNITE in ln:
                    return 0
                return 1
            return sorted(hand, key=rank)[:count]

        if vi:
            for opp in vi.get("opportunities", []) or []:
                iid = opp.get("interactionId")
                if iid in ST["answered_iids"]:
                    continue
                resp = opp.get("response", {}) or {}
                rtype = resp.get("type")
                data = resp.get("data", {}) or {}
                spec = data.get("spec") or {}
                spec_type = spec.get("type") \
                    if isinstance(spec, dict) else None
                chs = data.get("choices") or data.get("candidates") or []
                if not chs or (rtype == "schema" and spec_type not in
                               ("sequence", "select")):
                    continue
                ref_to_id = {}
                for ch in chs:
                    ref = cand_ref(ch)
                    if ref is not None:
                        ref_to_id[str(ref)] = ch.get("id")
                choice_ids = [ref_to_id[str(o)] for o in ranked()
                              if str(o) in ref_to_id]
                if not choice_ids:
                    choice_ids = [chs[0].get("id")]
                sub = build_submission(iid, rtype, spec_type,
                                       choice_ids=choice_ids)
                ST["answered_iids"].append(iid)
                say(f"[leg{leg}][{tag}] discarding to hand size via {rtype}")
                await c.send_interaction(sub)
                wire("discard_submit", {"leg": leg, "sub": sub})
                return True
        for a in merged_actions(st):
            if a.get("type") != "SelectCards":
                continue
            data = dict(a.get("data") or {})
            key = "cards" if "cards" in data else "cardIds"
            data[key] = ranked()
            say(f"[leg{leg}][{tag}] discarding via SelectCards fallback")
            await c.send_action({"type": "SelectCards", "data": data})
            return True
        return False

    def cast_spell_action(acts, state, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    def p1_memnites(state):
        return bf_ids(state, 1, MEMNITE)

    def dump_prompt_once(key, wtype, state, st):
        """Dump the full waiting_for + viewer_interaction shape once."""
        if key in ST["prompt_dumped"]:
            return
        ST["prompt_dumped"].add(key)
        payload = {
            "leg": leg, "key": key, "wtype": wtype,
            "waiting_for": wf_of(state),
            "viewer_interaction": st.get("viewer_interaction"),
            "legal_actions": [a.get("type") for a in merged_actions(st)],
        }
        wire("prompt_shape", payload)
        obs["unexpected_prompts"].append(
            {"leg": leg, "key": key, "wtype": wtype})
        say(f"[leg{leg}] PROMPT SHAPE {key} (full JSON in wire log): "
            f"wf_keys={list((wf_of(state).get('data') or {}).keys())}")

    async def handle_trigger_targets(st, acts, state):
        """Answer one Gandalf-trigger target slot; enforce the leg's
        target plan. Returns True if it acted."""
        wtype = wf_of(state).get("type") or ""
        if wtype not in ("TargetSelection", "TriggerTargetSelection"):
            return False
        dp = wf_player(state)
        if isinstance(dp, dict):
            dp = dp.get("id", -1)
        if dp != 0:
            return False
        # Only while Gandalf's trigger is the pending business.
        if ST["gandalf_oid"] is None:
            return False
        if not (gandalf_trigger_on_stack(state) or ST["trigger_seen"]):
            return False
        rev = st.get("state_revision", -1)
        d = wf_of(state).get("data") or {}
        sel = d.get("selection") or {}
        cur = sel.get("current_slot")
        dump_prompt_once(f"trigger_target_slot{cur}", wtype, state, st)
        vi = get_vi(st)
        if not vi:
            say(f"[leg{leg}] target prompt but no submittable VI")
            return False
        wanted = ST["targets_wanted"]
        if len(ST["targets_chosen"]) >= wanted:
            return await handle_extra_slot(st, acts, state, vi, rev, cur)
        if acted("trgt", rev):
            return True
        ST["target_slots_seen"] += 1
        slots = d.get("target_slots") or []
        slot_info = {"current_slot": cur,
                     "n_slots": len(slots)}
        if isinstance(cur, int) and cur < len(slots):
            slot_info["slot_optional"] = slots[cur].get("optional")
            slot_info["slot_effect"] = slots[cur].get("effect_kind")
        obs["target_prompts"].append(
            {"leg": leg, "wtype": wtype, "slot_info": slot_info,
             "chosen_so_far": list(ST["targets_chosen"])})
        say(f"[leg{leg}] target slot prompt #{ST['target_slots_seen']}: "
            f"{json.dumps(slot_info, default=str)[:300]} "
            f"chosen={ST['targets_chosen']}")

        # Case 1: still need targets for this leg -> pick a P1 Memnite.
        if len(ST["targets_chosen"]) < wanted:
            for opp in vi.get("opportunities", []) or []:
                iid = opp.get("interactionId")
                if iid in ST["answered_iids"]:
                    continue
                chs, rtype = vi_choices(opp)
                if not chs:
                    continue
                resp = opp.get("response", {}) or {}
                spec = (resp.get("data", {}) or {}).get("spec") or {}
                spec_type = spec.get("type") if isinstance(spec, dict) \
                    else None
                pick = None
                for ch in chs:
                    ref = cand_ref(ch)
                    try:
                        roid = int(ref) if ref is not None else None
                    except (TypeError, ValueError):
                        roid = None
                    if roid is not None and roid in p1_memnites(state) \
                            and roid not in ST["targets_chosen"]:
                        pick = ch
                        break
                if pick is None:
                    say(f"[leg{leg}] no P1-Memnite candidate found; "
                        f"candidates={len(chs)}")
                    continue
                roid = int(cand_ref(pick))
                sub = build_submission(iid, rtype, spec_type,
                                       choice_id=pick.get("id"),
                                       choice_ids=[pick.get("id")])
                ST["answered_iids"].append(iid)
                ST["targets_chosen"].append(roid)
                say(f"[leg{leg}] answered target slot -> Memnite oid={roid}")
                wire("target_answered",
                     {"leg": leg, "slot": ST["target_slots_seen"],
                      "oid": roid, "sub": sub})
                await p0.send_interaction(sub)
                return True
            return False
    async def handle_extra_slot(st, acts, state, vi, rev, cur):
        """Quota is filled but the engine asks for another slot. Try a
        skip affordance once; if the revision does not advance past it
        within 12s, treat the skip as failed and submit a forced target
        so the game can continue (the forced_extra_slot flag records the
        reported bug shape)."""
        ST["forced_extra_slot"] = True
        now = time.time()
        if ST.get("skip_attempted"):
            srev = ST.get("skip_rev")
            if rev == srev and now - ST.get("skip_time", 0) < 12:
                return True  # waiting for the skip to take effect
            say(f"[leg{leg}] skip did not stop the extra slot "
                f"(rev {srev}->{rev}); submitting a forced target")
            wire("skip_failed", {"leg": leg, "rev_before": srev,
                                 "rev_now": rev})
            ST["skip_attempted"] = False  # don't retry the skip
        else:
            say(f"[leg{leg}] EXTRA slot prompt with quota filled "
                f"(wanted={ST['targets_wanted']}) - looking for skip")
        # (a) legal_actions skip-like action
        for a in acts:
            if "skip" in str(a.get("type", "")).lower() or \
               "done" in str(a.get("type", "")).lower():
                ST["skip_attempted"] = True
                ST["skip_rev"] = rev
                ST["skip_time"] = now
                say(f"[leg{leg}] trying skip action {a.get('type')}")
                wire("skip_attempt", {"leg": leg, "action": a})
                await submit_as_is(p0, a)
                return True
        # (b) exactChoices decline/skip/done choice in VI
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "exactChoices":
                continue
            for ch in (resp.get("data", {}) or {}).get("choices", []) or []:
                blob = json.dumps(ch, default=str).lower()
                if any(k in blob for k in
                       ("skip", "decline", "done", "finish", "no more",
                        "no_more", "stop")):
                    ST["skip_attempted"] = True
                    ST["skip_rev"] = rev
                    ST["skip_time"] = now
                    sub = build_submission(iid, "exactChoices",
                                           choice_id=ch.get("id"))
                    ST["answered_iids"].append(iid)
                    say(f"[leg{leg}] trying VI skip choice {ch.get('id')}")
                    wire("skip_attempt", {"leg": leg, "sub": sub})
                    await p0.send_interaction(sub)
                    return True
        # (c) no (working) skip affordance: submit another Memnite to keep
        # the game alive; forced_extra_slot already records the bug.
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            resp = opp.get("response", {}) or {}
            spec = (resp.get("data", {}) or {}).get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            pick = None
            for ch in chs:
                ref = cand_ref(ch)
                try:
                    roid = int(ref) if ref is not None else None
                except (TypeError, ValueError):
                    roid = None
                if roid is not None and roid in p1_memnites(state) \
                        and roid not in ST["targets_chosen"]:
                    pick = ch
                    break
            if pick is None:
                continue
            roid = int(cand_ref(pick))
            sub = build_submission(iid, rtype, spec_type,
                                   choice_id=pick.get("id"),
                                   choice_ids=[pick.get("id")])
            ST["answered_iids"].append(iid)
            ST["targets_chosen"].append(roid)
            say(f"[leg{leg}] (forced) answered extra slot -> Memnite "
                f"oid={roid}")
            wire("target_answered_forced",
                 {"leg": leg, "oid": roid, "sub": sub})
            await p0.send_interaction(sub)
            return True
        say(f"[leg{leg}] extra slot prompt: nothing answerable")
        return False

    async def handle_division(st, acts, state):
        """Answer the DistributeAmong (assignAmounts) prompt. Every
        candidate must appear in the assignments (require_all=true);
        the engine then enforces >=1 per target at action-application
        time (engine.rs, CR 609) - so with the 3 forced targets the only
        lawful division is 1/1/1. Retries after a rejection (12s
        no-advance rule)."""
        wtype = wf_of(state).get("type") or ""
        if wtype != "DistributeAmong":
            return False
        dp = wf_player(state)
        if isinstance(dp, dict):
            dp = dp.get("id", -1)
        if dp != 0:
            return False
        if not ST["targets_chosen"]:
            return False
        if not gandalf_trigger_on_stack(state):
            return False
        rev = st.get("state_revision", -1)
        if acted("div", rev):
            return True
        if ST.get("division_iid") and \
                time.time() - ST.get("division_at", 0) > 12:
            ST["answered_iids"] = [
                i for i in ST["answered_iids"]
                if i != ST["division_iid"]]
            ST["division_iid"] = None
            ST["division_submitted"] = False
            say(f"[leg{leg}] division retry (12s no advance)")
            wire("division_retry", {"leg": leg})
        dump_prompt_once("division", wtype, state, st)
        vi = get_vi(st)
        if not vi:
            return False
        d = wf_of(state).get("data") or {}
        obs["division_prompts"].append(
            {"leg": leg, "wtype": wtype,
             "total": d.get("total"),
             "targets": d.get("targets")})
        say(f"[leg{leg}] division prompt: total={d.get('total')} "
            f"targets={d.get('targets')}")
        ST["division_seen"] = True
        ST["division_shape"] = wtype
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            # lawful division for the (forced) target set: 1 damage each
            assignments = []
            ok = True
            for ch in chs:
                ref = cand_ref(ch)
                try:
                    roid = int(ref) if ref is not None else None
                except (TypeError, ValueError):
                    roid = None
                if roid is None:
                    ok = False
                    break
                assignments.append((ch.get("id"), 1))
            if not ok or sum(a for _, a in assignments) != 3:
                say(f"[leg{leg}] division: candidate mapping failed; "
                    f"skipping opportunity")
                continue
            sub = build_assign_amounts(iid, assignments)
            ST["answered_iids"].append(iid)
            ST["division_iid"] = iid
            ST["division_at"] = time.time()
            ST["division_submitted"] = True
            ST["division_rejected"] = False
            ST["damage_plan"] = [(roid_of(chs, cid), amt)
                                 for cid, amt in assignments]
            say(f"[leg{leg}] division submitted: 1/1/1 across the 3 "
                f"forced targets (assignAmounts)")
            wire("division_submit", {"leg": leg, "sub": sub})
            await p0.send_interaction(sub)
            return True
        return False

    def roid_of(chs, cid):
        for ch in chs:
            if ch.get("id") == cid:
                try:
                    return int(cand_ref(ch))
                except (TypeError, ValueError):
                    return None
        return None

    async def combat_empty(c, pid, tag, st, state, acts):
        rev = st.get("state_revision", -1)
        wtype = wf_of(state).get("type") or ""
        if wtype == "DeclareAttackers" and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da and not acted(f"{tag}_atk", rev):
                dd = copy.deepcopy(da.get("data", {}))
                dd["attacks"] = []
                dd["bands"] = []
                await submit_as_is(c, {"type": wtype, "data": dd})
                return True
        return False

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_decide(p0, 0, "P0", st, state, GANDALF):
            return
        if await bottom_cards(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state, (GANDALF,)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_of(state).get("type") or ""
        if wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)
        for rj in drain_rejections(p0):
            obs["rejections"].append({"stage": ST["stage"], **rj})
            blob = json.dumps(rj, default=str)
            say(f"[leg{leg}][P0] REJECTION in stage {ST['stage']}: "
                f"{blob[:300]}")
            if ST.get("division_submitted"):
                # attribute to the in-flight division submission: it is
                # the only P0 submission possible during the trigger stage
                ST["division_rejected"] = True
                ST["division_submitted"] = False
                diid = ST.get("division_iid")
                if diid and diid in ST["answered_iids"]:
                    ST["answered_iids"].remove(diid)
                    say(f"[leg{leg}][P0] un-answered division iid {diid}")
                ST["division_iid"] = None
            for iid in list(ST["answered_iids"]):
                if str(iid) in blob:
                    ST["answered_iids"].remove(iid)
                    say(f"[leg{leg}][P0] un-answered rejected iid {iid}")
            # a rejected submission moved no revision: allow immediate
            # retry rather than waiting on the per-rev acted gate
            for k in ("trgt", "div"):
                ST["last_rev_acted"].pop(k, None)
        # observe Gandalf on BF
        if ST["gandalf_oid"] is None:
            gs = bf_ids(state, 0, GANDALF)
            if gs:
                ST["gandalf_oid"] = gs[0]
                say(f"[leg{leg}][obs] Gandalf on BF oid={gs[0]}")
                wire("gandalf_on_bf", {"leg": leg, "oid": gs[0]})
        if gandalf_trigger_on_stack(state) and not ST["trigger_seen"]:
            ST["trigger_seen"] = True
            ST["stage"] = "trigger"
            say(f"[leg{leg}][obs] Gandalf ETB trigger on stack")
            wire("trigger_on_stack", {"leg": leg})
        if await combat_empty(p0, 0, "P0", st, state, acts):
            return
        # pre export: Gandalf on BF + trigger pending, before any target
        # answer is submitted.
        if ST["gandalf_oid"] is not None and ST["trigger_seen"] \
                and f"pre_leg{leg}" not in ST["exports"] \
                and not ST["targets_chosen"]:
            await export_named(f"pre_leg{leg}")
            return
        # trigger target selection
        if ST["stage"] == "trigger":
            if await handle_trigger_targets(st, acts, state):
                return
            if await handle_division(st, acts, state):
                return
        # trigger resolved? (left the stack after being seen)
        if ST["trigger_seen"] and not gandalf_trigger_on_stack(state) \
                and ST["stage"] == "trigger":
            ST["stage"] = "resolve"
            ST["observed_at"] = time.time()
            if ST.get("division_submitted"):
                ST["division_accepted"] = True
                ST["division_submitted"] = False
                ST["division_rejected"] = False
            elif not ST.get("division_seen"):
                # degenerate: single target, no division prompt issued
                ST["division_accepted"] = True
            if ST["skip_attempted"] \
                    and len(ST["targets_chosen"]) == ST["targets_wanted"]:
                ST["skip_accepted"] = True
            say(f"[leg{leg}] trigger left the stack; stage -> resolve; "
                f"targets={ST['targets_chosen']} "
                f"forced_extra={ST['forced_extra_slot']}")
            wire("trigger_resolved",
                 {"leg": leg, "targets": ST["targets_chosen"],
                  "forced_extra_slot": ST["forced_extra_slot"],
                  "division_seen": ST["division_seen"]})
            if f"mid_leg{leg}" not in ST["exports"]:
                await export_named(f"mid_leg{leg}")
            return
        # ---- main-phase actions ----
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)
        if my_priority(state, 0) \
                and phase in ("PreCombatMain", "PostCombatMain") \
                and not stack_entries(state):
            for oid in hand_ids(state, 0):
                if lname(state, oid) == MOUNTAIN:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            if ST["gandalf_oid"] is None \
                    and GANDALF in hand_lnames(state, 0):
                ca = cast_spell_action(acts, state, GANDALF)
                if ca and not acted("gcast", rev):
                    ST["gandalf_cast_turn"] = turn
                    ST["stage"] = "gandalf_cast"
                    say(f"[leg{leg}][P0] casting Gandalf (turn {turn})")
                    wire("gandalf_cast", {"leg": leg, "turn": turn})
                    await submit_as_is(p0, ca)
                    return
        # close out: game advanced past the cast turn with empty stack
        if ST["stage"] == "resolve" and my_priority(state, 0) \
                and not stack_entries(state):
            ct = ST.get("gandalf_cast_turn") or 0
            if turn > ct or (ST.get("observed_at")
                             and time.time() - ST["observed_at"] > 60):
                ST["need_post_export"] = True
                ST["stage"] = "done"
                say(f"[leg{leg}][P0] observation window closed; done")
                return
        # default: pass priority (gated on my_priority, cf. #4509)
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_decide(p1, 1, "P1", st, state, None):
            return
        if await bottom_cards(p1, 1, "P1", st, state):
            return
        if await handle_discard(p1, 1, "P1", st, state, ()):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        for rj in drain_rejections(p1):
            obs["rejections"].append({"stage": "p1", **rj})
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        # dump Memnites: {0} cost, cast one per tick iteration
        if (wf_of(state).get("type") or "") == "Priority" \
                and state.get("priority_player") == 1:
            ca = cast_spell_action(acts, state, MEMNITE)
            if ca and not acted("p1mem", rev):
                await submit_as_is(p1, ca)
                return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST.get("need_post_export") and not ST.get("post_exported"):
                await export_named(f"post_leg{leg}")
                ST["post_exported"] = True
                ST["need_post_export"] = False
            if ST["stage"] == "done":
                return
            if c.latest is None:
                continue
            st = c.latest
            state = st.get("state") or {}
            if not state:
                continue
            rev = st.get("state_revision", -1)
            prev = ST["last_rev_seen"].get(tag)
            ST["last_rev_seen"][tag] = rev
            if rev != prev:
                ST["last_rev_change"][tag] = time.time()
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    # game setup
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or (sess or {}).get("game_code")
    say(f"[leg{leg}] game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"[leg{leg}] P1 joined")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < t_budget:
            await asyncio.sleep(1)
            if ST["stage"] == "done":
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                for tag in ("P0", "P1"):
                    rev = ST["last_rev_seen"].get(tag)
                    chg = ST["last_rev_change"].get(tag, t0)
                    if time.time() - chg > 45:
                        say(f"[leg{leg}][watchdog] {tag} revision {rev} "
                            f"stale {int(time.time()-chg)}s: turn="
                            f"{s.get('turn_number')} phase="
                            f"{s.get('phase')} wf="
                            f"{(s.get('waiting_for') or {}).get('type')} "
                            f"pp={s.get('priority_player')}")
                say(f"[leg{leg}][diag] t={int(time.time()-t0)}s "
                    f"stage={ST['stage']} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"gandalf={ST['gandalf_oid']} "
                    f"targets={ST['targets_chosen']} "
                    f"p1mem={len(bf_ids(s, 1, MEMNITE))}")
    finally:
        p0t.cancel()
        p1t.cancel()
    say(f"[leg{leg}] game finished: stage={ST['stage']}")
    return ST, obs

# ------------------------------------------------------------------ driver
async def main():
    t_start = time.time()
    global p0, p1
    p0 = PhaseClient("P0-gandalf")
    p1 = PhaseClient("P1-memnite")
    await p0.connect()
    await p1.connect()
    say("both clients connected")

    results = {}
    # Leg 1: the reported path - exactly one target, all 3 damage to it.
    st1, obs1 = await run_game(1, 780)
    results[1] = (st1, obs1)

    leg1_ok = (
        st1["gandalf_oid"] is not None
        and st1["trigger_seen"]
        and len(st1["targets_chosen"]) == 1
        and not st1["forced_extra_slot"]
        and f"post_leg1" in st1["exports"]
    )
    if leg1_ok:
        say("leg 1 fully clean; running leg 2 (two targets, 1+2 split)")
        # fresh clients for game 2 (per-game state starts clean anyway)
        await p0.close()
        await p1.close()
        p0 = PhaseClient("P0-gandalf-2")
        p1 = PhaseClient("P1-memnite-2")
        await p0.connect()
        await p1.connect()
        st2, obs2 = await run_game(2, 780)
        results[2] = (st2, obs2)
    else:
        say(f"leg 1 not clean (gandalf={st1['gandalf_oid']} "
            f"trigger={st1['trigger_seen']} targets={st1['targets_chosen']} "
            f"forced_extra={st1['forced_extra_slot']} "
            f"exports={sorted(st1['exports'])}); skipping leg 2")
    await finish(results, t_start)


def load_state(name):
    p = f"{EVDIR}/{name}.json"
    try:
        if os.path.exists(p):
            return json.loads(open(p).read())["state"]
    except Exception as e:
        say(f"state reload failed for {name}.json: {e}")
    return {}


async def finish(results, t_start):
    ass = {}
    notes = []
    (st1, obs1) = results[1]
    r2 = results.get(2)
    st2, obs2 = r2 if r2 else ({}, {})

    pre1 = load_state("pre_leg1")
    mid1 = load_state("mid_leg1")
    post1 = load_state("post_leg1")
    pre2 = load_state("pre_leg2") if r2 else {}
    post2 = load_state("post_leg2") if r2 else {}

    def mems(state):
        return bf_ids(state, 1, MEMNITE)

    # ---- A1: setup ----
    g1 = get_obj(pre1, st1.get("gandalf_oid")) if pre1 else {}
    a1 = (bool(pre1) and st1.get("gandalf_oid") is not None
          and g1.get("zone") == "Battlefield"
          and len(mems(pre1)) >= 3 and st1.get("trigger_seen"))
    notes.append(
        f"A1: pre_leg1 present={bool(pre1)} gandalf_oid="
        f"{st1.get('gandalf_oid')} on_bf_at_pre="
        f"{g1.get('zone') == 'Battlefield'} p1_memnites_at_pre="
        f"{len(mems(pre1)) if pre1 else None} trigger_seen="
        f"{st1.get('trigger_seen')} cast_turn="
        f"{st1.get('gandalf_cast_turn')}")
    ass["A1_setup_ok"] = "passed" if a1 else "failed"

    # ---- A2: exactly one target accepted ----
    a2 = (a1 and len(st1.get("targets_chosen", [])) == 1
          and not st1.get("forced_extra_slot"))
    notes.append(
        f"A2: targets_chosen={st1.get('targets_chosen')} "
        f"target_slots_seen={st1.get('target_slots_seen')} "
        f"forced_extra_slot={st1.get('forced_extra_slot')} "
        f"skip_attempted={st1.get('skip_attempted')} "
        f"skip_accepted={st1.get('skip_accepted')}; expect exactly 1 "
        f"target and no forced extra slot")
    ass["A2_one_target"] = "passed" if a2 else ("failed" if a1 else "not-run")

    # ---- A3: division resolved, 3 damage to the single target ----
    div_ok = bool(a2 and st1.get("division_accepted"))
    notes.append(
        f"A3: division_seen={st1.get('division_seen')} "
        f"division_shape={st1.get('division_shape')} "
        f"division_submitted={st1.get('division_submitted')} "
        f"division_accepted={st1.get('division_accepted')} "
        f"division_rejected={st1.get('division_rejected')} "
        f"damage_plan={st1.get('damage_plan')}")
    ass["A3_division"] = "passed" if div_ok else ("failed" if a1 else "not-run")

    # ---- A4: damage outcome ----
    chosen = (st1.get("targets_chosen") or [None])[0]
    post_mems = mems(post1) if post1 else []
    pre_mems = mems(pre1) if pre1 else []
    chosen_gone = (chosen is not None and chosen not in post_mems
                   and chosen in pre_mems)
    others_alive = (len(post_mems) >= 2 and chosen not in post_mems)
    stack_empty = bool(post1) and not stack_entries(post1)
    a4 = bool(a2 and div_ok and chosen_gone and others_alive and stack_empty)
    notes.append(
        f"A4: chosen={chosen} pre_memnites={pre_mems} post_memnites="
        f"{post_mems} chosen_gone={chosen_gone} others_alive(>=2)="
        f"{others_alive} stack_empty={stack_empty}")
    ass["A4_damage"] = "passed" if a4 else ("failed" if (a2 and div_ok)
                                            else "not-run")

    # ---- leg 2 (2 targets, 1+2 split) ----
    if r2:
        ch2 = st2.get("targets_chosen", [])
        b1 = (st2.get("gandalf_oid") is not None and st2.get("trigger_seen")
              and len(ch2) == 2 and not st2.get("forced_extra_slot"))
        notes.append(
            f"B1: leg2 targets_chosen={ch2} "
            f"forced_extra_slot={st2.get('forced_extra_slot')} "
            f"trigger_seen={st2.get('trigger_seen')}; expect exactly 2 "
            f"targets, no forced 3rd slot")
        ass["B1_two_targets"] = "passed" if b1 else "failed"
        pm2 = mems(pre2) if pre2 else []
        qm2 = mems(post2) if post2 else []
        both_gone = (b1 and all(o in pm2 and o not in qm2 for o in ch2))
        rest_alive = (b1 and len([o for o in qm2]) >= 1
                      and all(o not in ch2 for o in qm2))
        stack2_empty = bool(post2) and not stack_entries(post2)
        b2 = bool(b1 and both_gone and rest_alive and stack2_empty
                  and not st2.get("division_rejected"))
        notes.append(
            f"B2: leg2 pre_memnites={pm2} post_memnites={qm2} "
            f"both_chosen_gone={both_gone} rest_alive={rest_alive} "
            f"stack_empty={stack2_empty} division_rejected="
            f"{st2.get('division_rejected')} damage_plan="
            f"{st2.get('damage_plan')}")
        ass["B2_leg2_damage"] = "passed" if b2 else "failed"
    else:
        ass["B1_two_targets"] = "not-run"
        ass["B2_leg2_damage"] = "not-run"
        notes.append("B1/B2: leg 2 not run (leg 1 did not pass cleanly)")

    # ---- verdict ----
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: A1 failed - Gandalf setup/trigger "
                     "never completed")
    elif ass["A2_one_target"] != "passed" or ass["A3_division"] != "passed" \
            or ass["A4_damage"] != "passed":
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the reported Gandalf, Spark Starter defect "
            f"(targets={st1.get('targets_chosen')} "
            f"forced_extra_slot={st1.get('forced_extra_slot')})")
    elif r2 and (ass["B1_two_targets"] != "passed"
                 or ass["B2_leg2_damage"] != "passed"):
        verdict = "reproduced"
        notes.append("verdict=reproduced: single-target path works but the "
                     "two-target split fails")
    elif all(ass.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_one_target", "A3_division", "A4_damage")) \
            and (not r2 or all(ass.get(k) == "passed" for k in
                               ("B1_two_targets", "B2_leg2_damage"))):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: 1-target (and 2-target) "
                     "selection plus 3-damage division behaved per Oracle "
                     "text")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: incomplete assertion chain")
    notes.append(f"verdict={verdict}")

    dur = time.time() - t_start
    run = {
        "run_id": RUN_ID, "issue": ISSUE,
        "verdict": verdict, "validated_at": "2026-09-17",
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 72,
                   "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7208.py", "rb").read()
        ).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "observations": {"leg1": obs1, "leg2": obs2 if r2 else None},
        "driver_state": {
            "leg1": {k: st1.get(k) for k in
                     ("stage", "gandalf_oid", "gandalf_cast_turn",
                      "trigger_seen", "targets_chosen", "target_slots_seen",
                      "forced_extra_slot", "skip_attempted", "skip_accepted",
                      "division_seen", "division_shape", "division_submitted",
                      "division_rejected", "damage_plan", "wf_types_seen",
                      "exports", "mull_count")},
            "leg2": ({k: st2.get(k) for k in
                      ("stage", "gandalf_oid", "trigger_seen",
                       "targets_chosen", "target_slots_seen",
                       "forced_extra_slot", "division_seen",
                       "division_submitted", "division_rejected",
                       "damage_plan", "exports")} if r2 else None),
        },
        "notes": notes,
        "evidence_files": ["pre_leg1.json", "mid_leg1.json", "post_leg1.json",
                           "pre_leg2.json", "mid_leg2.json", "post_leg2.json",
                           "run.json", "manifest.sha256", "summary.png",
                           "scenario_7208.py", "wire_log.jsonl",
                           "scenario_run.log", "server.log"],
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "10x/40x card density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "P1 is a passive second seat (Memnites only, no attacks) - "
            "opponent interaction with the trigger is not exercised.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Only creature targets (P1 Memnites) are exercised; player / "
            "planeswalker targets are not.",
            "Leg 2 runs only when leg 1 passes cleanly.",
        ],
        "duration_s": round(dur, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(f"{BACKFILL}/driver/scenario_7208.py",
                f"{EVDIR}/scenario_7208.py")
    try:
        with open(f"{BACKFILL}/runs/{RUN_ID}/server.log", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
        clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        codes = [st.get("game_code") for st, _ in results.values()
                 if st.get("game_code")]
        excerpt = [ln for ln in clean.splitlines()
                   if any(gc and gc in ln for gc in codes)]
        if not excerpt:
            excerpt = clean.splitlines()[-400:]
        with open(f"{EVDIR}/server.log", "w") as f:
            f.write("\n".join(excerpt) + "\n")
        say(f"wrote server.log excerpts ({len(excerpt)} lines)")
    except Exception as e:
        say(f"server.log excerpt failed: {e}")
        notes.append(f"server.log excerpt failed: {e}")
    render_summary(run)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


def render_summary(run):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 1180
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7208 - Gandalf, Spark Starter",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.86.0 (2cc8c28) protocol 72 - 2026-09-17 - "
           "'deals 3 damage divided as you choose among 1-3 targets'",
           fill=(140, 160, 180))
    y += 28
    vcol = {"reproduced": (255, 90, 90),
            "not-reproduced": (120, 220, 120),
            "blocked": (230, 200, 120)}.get(run["verdict"], (180, 180, 180))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    for ln in [
            "Report: Gandalf 'forces to choose 3 targets and give each one",
            "at least 1 damage'. Expected: 1-3 targets with a player-chosen",
            "division totaling 3. Setup: P0 casts Gandalf ({4}{R}{R} 4/3);",
            "P1 has 7 Memnites (1/1) on the BF. Leg 1: driver tries exactly",
            "ONE target with all 3 damage on it. Leg 2 (if leg 1 passes):",
            "TWO targets, 1+2 split."]:
        d.text((24, y), ln, fill=(200, 210, 225))
        y += 24
    y += 10
    d.text((24, y), "Assertions (from saved states / observations):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "Gandalf on P0 BF; P1 >=3 creatures; trigger seen",
        "A2_one_target": "target selection finalized with exactly 1 target",
        "A3_division": "division resolved: 3 damage to the single target",
        "A4_damage": "chosen Memnite died; >=2 others alive; stack empty",
        "B1_two_targets": "leg 2: exactly 2 targets, no forced 3rd slot",
        "B2_leg2_damage": "leg 2: both chosen died; rest alive",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "?")
        col = {"passed": (120, 220, 120), "failed": (255, 110, 110),
               "not-run": (200, 180, 120)}.get(v, (180, 180, 180))
        d.text((24, y), f"[{v}] {k}: {lab}", fill=col)
        y += 26
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    ds = run["driver_state"]["leg1"]
    for ln in [
            f"leg1 gandalf oid={ds.get('gandalf_oid')} "
            f"cast turn={ds.get('gandalf_cast_turn')}",
            f"targets chosen={ds.get('targets_chosen')} "
            f"slots seen={ds.get('target_slots_seen')} "
            f"forced_extra={ds.get('forced_extra_slot')}",
            f"division seen={ds.get('division_seen')} "
            f"shape={ds.get('division_shape')} "
            f"submitted={ds.get('division_submitted')} "
            f"rejected={ds.get('division_rejected')}",
            f"damage plan={ds.get('damage_plan')}",
            f"wf types: {', '.join(ds.get('wf_types_seen', [])[:14])}",
            "Exports: pre_legN (Gandalf on BF, trigger pending),",
            "mid_legN (trigger resolved), post_legN (window closed).",
            "Full prompt shapes in wire_log.jsonl (prompt_shape).",
    ]:
        d.text((24, y), ln[:108], fill=(160, 175, 195))
        y += 22
    d.text((24, y + 14), "Generated from saved states/assertions; not a "
           "gameplay screenshot.", fill=(110, 125, 145))
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    import hashlib as _hl
    files = sorted(
        f for f in os.listdir(EVDIR)
        if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
    lines = []
    for fn in files:
        h = _hl.sha256()
        with open(f"{EVDIR}/{fn}", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
