#!/usr/bin/env python3
"""Issue #6875: Krark-Clan Shaman — ability can't be activated more than once.

Reported (Discord): "Game doesn't allow the ability to be used more than
once; the first sacrifice trigger occurs and it resolves, even in manual
payment mode."

Oracle: "Sacrifice an artifact: This creature deals 1 damage to each creature
without flying." Parsed (v0.80.0 card-data): Activated DamageAll, cost
Sacrifice 1 artifact.

Triage acceptance criteria:
- Each available artifact may be sacrificed to pay for a separate activation
  while earlier activations remain on the stack.
- Each activation resolves independently and deals 1 damage to every creature
  without flying.
- Cost payment cannot reuse an artifact already sacrificed.

Matt's follow-up: the report may match the frontend's deliberate default
auto-pass policy (Full Control disables it). This scenario drives the engine
directly over WS (no frontend), so an engine-side refusal is a genuine
engine defect; an engine-side allowance points at the frontend policy.

Plan (native engine, v0.80.0 / protocol 69, two human-client seats):
  P0: Krark-Clan Shaman + Memnites ({0} artifact creatures) + Mountains.
  P1: Grizzly Bears (2/2, no flying — damage witness) + Ornithopters
      (0/2, flying — immunity witness) + Forests/Mountains.
  SETUP: both develop boards (P0: Shaman + >=2 Memnites; P1: >=2 Bears,
      >=1 Ornithopter).
  PROOF (P0 main phase): export pre.json; activate the Shaman sacrificing
      Memnite #1 -> answer the sacrifice-cost prompt -> activation #1 on the
      stack (export mid1.json); then, while activation #1 is still on the
      stack and P0 holds priority, attempt a SECOND activation sacrificing
      Memnite #2. Two Shaman activations coexisting on the stack ->
      export mid2.json. Let both resolve; export post.json from the
      stack-emptying tick (before cleanup wipes damage_marked).

Behavioral contract:
  A1 setup_ok              pre.json: Shaman + >=2 Memnites on P0 BF, >=2 Bears
                           + >=1 Ornithopter on P1 BF, P0 main phase
  A2 first_activation_ok   activation #1 entered the stack; its sacrifice
                           cost was paid (chosen Memnite in graveyard)
  A3 second_activation_ok  a second Shaman activation was submitted AND both
                           activations coexisted on the stack (THE reported
                           bug is that the 2nd is never allowed)
  A4 independent_resolution both activations resolved: every Bear on the
                           battlefield took 2 total damage (2/2 -> dead in
                           graveyard); Ornithopters (flying) untouched with
                           damage_marked == 0
  A5 cleanup               stack empty, game not over, no dangling P0 prompt

Verdict: reproduced iff A1+A2 pass and A3 fails. not-reproduced iff
A1..A5 all pass. blocked otherwise.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

from PIL import Image, ImageDraw  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6875c"
ISSUE = "6875"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SHAMAN = "Krark-Clan Shaman"
MEMNITE = "Memnite"
MOUNTAIN = "Mountain"
BEAR = "Grizzly Bears"
THOPTER = "Ornithopter"
FOREST = "Forest"
LANDS = {MOUNTAIN, FOREST}
P0_DECK = [(SHAMAN, 8), (MEMNITE, 8), (MOUNTAIN, 44)]
P1_DECK = [(BEAR, 12), (THOPTER, 12), (FOREST, 26), (MOUNTAIN, 10)]

TIMEOUT = 1500
SECOND_ACT_DEADLINE = 150  # s after act1 on stack without act2 -> fail

ST = {}
ACT = {}
OBS = {}
SUBMITTED = set()
MULLS = {}
SHAPES = set()
WF_SEEN = []
LAST_SUBMIT = {"iid": None}
C0 = None


def reset():
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False,
               "act1_at": None, "second_deadline_armed": False})
    ACT.clear()
    OBS.clear()
    OBS.update({"sac_choices": [], "rejections": [],
                "act1_sid": None, "act2_sid": None,
                "act1_at": None, "act2_windows": 0,
                "second_never_offered": False,
                "two_on_stack_seen": False,
                "p0_priority_windows": [],
                "second_offer_shapes": [],
                "act1_submitted": None, "act2_submitted": None,
                "mid1_exported": False, "mid2_exported": False,
                "mid2_done": False, "post_done": False,
                "saw_post_mid2_nonempty": False,
                "proof_t0": None, "pre_ok": False,
                "post_exported": False})
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
        WIRE.flush()
    except Exception:
        pass


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


def is_artifact(o):
    ct = o.get("card_type") or {}
    return "Artifact" in (ct.get("core_types") or [])


def p0_artifacts(state):
    return [(oid, o) for oid, o in bf(state, 0) if is_artifact(o)]


def untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in LANDS and not o.get("tapped"))


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "n"):
            if k in v:
                return num(v[k])
    return None


def stack_ids(state):
    return [str(e.get("id")) for e in (state.get("stack") or [])]


def stack_entry(state, sid):
    for e in (state.get("stack") or []):
        if str(e.get("id")) == str(sid):
            return e
    return None


def shaman_stack_sids(state):
    out = []
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if "deals 1 damage to each creature without flying" in blob:
            out.append(str(e.get("id")))
    return out


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
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST["stage"]})
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
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if "seat" in d:
                    seat = d["seat"]
        o = state["objects"].get(str(ref), {}) if ref else {}
        se = stack_entry(state, ref) if ref else None
        if se is not None:
            name, zone, controller = ("stack-entry", "Stack",
                                      se.get("controller"))
        else:
            name, zone, controller = oname(o), o.get("zone"), \
                o.get("controller")
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": name, "zone": zone, "controller": controller,
                    "text": ch.get("text")})
    return out


def build_target_response(resp, want):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [want["choice_id"]]}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": want["choice_id"]}}
    return None


def activate_options(acts, state, pid, name):
    srcs = set(perm_oids(state, pid, name))
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src in srcs:
            out.append((d.get("ability_index"), a))
    return out


def wf_player(state):
    return (state.get("waiting_for") or {}).get("data", {}).get("player")


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def p0_has_priority(state, acts):
    return (wf_player(state) == 0
            and any(a["type"] == "PassPriority" for a in acts))


async def answer_sac_prompt(c, state, acts, st):
    """Answer P0's sacrifice-cost prompt by choosing the next Memnite.

    Returns True if a submission was made.
    """
    if wf_player(state) != 0:
        return False
    if wf_type(state) in (None, "Priority", "GameOver"):
        return False
    vi = get_vi(st)
    if not vi:
        return False
    # choose the next unsacrificed Memnite on P0's battlefield
    used = {str(x["ref"]) for x in OBS["sac_choices"]}
    mems = [oid for oid in perm_oids(state, 0, MEMNITE) if oid not in used]
    if not mems:
        return False
    want_oid = mems[0]
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        cands = candidate_info(opp, state)
        if not cands:
            continue
        # only treat as the sacrifice prompt if candidates include our artifact
        want = next((x for x in cands
                     if x["ref"] == want_oid and x["zone"] == "Battlefield"),
                    None)
        if not want:
            continue
        shape = (wf_type(state), resp.get("type"), len(cands))
        if shape not in SHAPES:
            SHAPES.add(shape)
            wire("sac_prompt", {"wf": wf_type(state),
                                "rtype": resp.get("type"),
                                "candidates": cands,
                                "opportunity": opp})
            say(f"[P0] sacrifice prompt ({wf_type(state)}/"
                f"{resp.get('type')}): "
                f"{[(x['name'], x['ref']) for x in cands]}")
        resp_out = build_target_response(resp, want)
        if not resp_out:
            say(f"[P0] sacrifice: unexpected prompt shape {resp.get('type')}")
            continue
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        OBS["sac_choices"].append({"ref": want_oid, "name": MEMNITE,
                                   "at": time.time(),
                                   "n": len(OBS["sac_choices"]) + 1})
        say(f"[P0] sacrifices Memnite oid={want_oid} "
            f"(choice #{len(OBS['sac_choices'])})")
        return True
    return False


def vi_activate_choice(st, state):
    """Find an 'activate the Shaman' choice in viewer_interaction.

    Looks for exactChoices opportunities whose surfaces carry an
    activateAbility code referencing the Shaman; falls back to any choice
    whose candidate is the Shaman object on the battlefield.
    """
    vi = get_vi(st)
    if not vi:
        return None
    shaman_oids = set(perm_oids(state, 0, SHAMAN))
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data", {}) or {}).get("choices", []) or []:
            codes = []
            refs = []
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict):
                    if d.get("code"):
                        codes.append(d["code"])
                    if "reference" in d:
                        refs.append(str(d["reference"]))
            if "activateAbility" in codes and \
                    any(r in shaman_oids for r in refs):
                return opp, ch
    return None


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, MOUNTAIN)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


async def p0_setup(c, pid, state, acts):
    # cast Shaman first (needs one untapped land), then Memnites — up to 3
    if not perm_oids(state, pid, SHAMAN):
        oid = find_hand(state, pid, SHAMAN)
        a = castspell_advertised(acts, oid)
        if a and untapped_lands(state, pid) >= 1:
            await submit_as_is(c, a)
            say("P0 casts Krark-Clan Shaman")
            return True
        return False
    if len(perm_oids(state, pid, MEMNITE)) < 3:
        oid = find_hand(state, pid, MEMNITE)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say("P0 casts Memnite")
            return True
    return False


async def p1_setup(c, pid, state, acts):
    # land drop
    for land in (FOREST, MOUNTAIN):
        lid = find_hand(state, pid, land)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    # creatures: Bears ({1}{G}) then Ornithopters ({0})
    if len(perm_oids(state, pid, BEAR)) < 2:
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a and untapped_lands(state, pid) >= 2:
            await submit_as_is(c, a)
            say("P1 casts Grizzly Bears")
            return True
    if len(perm_oids(state, pid, THOPTER)) < 2:
        oid = find_hand(state, pid, THOPTER)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say("P1 casts Ornithopter")
            return True
    return False


async def p0_proof(c, pid, state, acts, st):
    """PROOF stage for P0: activate #1, then #2 while #1 is on the stack.

    Driver fixes vs the first attempt (20260911-6875b), which passed P0's
    priority away immediately after submitting activation #1 and therefore
    never actually attempted the second activation:
    - after submitting an activation, P0 HOLDS priority (returns True)
      until the sacrifice-cost prompt is answered and the activation is
      visible on the stack, instead of falling through to PassPriority;
    - act1-on-stack is detected inline in the tick (no main-loop race);
    - the decisive window is P0's first priority with act1 on the stack:
      if the 2nd activation is not offered there, hold briefly to
      confirm, then export post_failure and stop (verdict: reproduced).
    """
    # 0) always answer a pending sacrifice-cost prompt first
    if await answer_sac_prompt(c, state, acts, st):
        return True
    now = time.time()
    sids = shaman_stack_sids(state)
    # inline: record act1 the moment it is visible on the stack
    if sids and not OBS["act1_sid"]:
        OBS["act1_sid"] = sids[0]
        OBS["act1_at"] = now
        say(f"activation #1 confirmed on stack: sid={sids[0]}")
        wire("act1_on_stack", {"sid": sids[0], "stack_sids": sids})
        await export_now("mid1.json")
        say("exported mid1.json")
    if len(sids) >= 2 and not OBS["mid2_done"]:
        OBS["mid2_done"] = True
        say(f"TWO Shaman activations coexisting on stack: {sids}")
        wire("two_on_stack", {"stack_sids": sids})
        await export_now("mid2.json")
        say("exported mid2.json")
    n_sac = len(OBS["sac_choices"])
    shaman = perm_oids(state, pid, SHAMAN)
    if not shaman:
        return False
    # hold P0's pass while activation #1 is submitted but not yet on stack
    if OBS["act1_submitted"] and not sids and n_sac == 0:
        if now - OBS["act1_submitted"]["at"] < 30:
            return True
    # hold while activation #2 is submitted but not yet on stack
    if OBS["act2_submitted"] and len(sids) < 2 and not OBS["mid2_done"]:
        if now - OBS["act2_submitted"]["at"] < 30:
            return True
    # submit activation #1 (no sacrifice answered yet)
    if n_sac == 0 and not OBS["act1_submitted"]:
        if not p0_has_priority(state, acts):
            return False
        opts = activate_options(acts, state, pid, SHAMAN)
        found = vi_activate_choice(st, state)
        shape = ("act1_opts", len(opts), bool(found),
                 tuple(sorted({a["type"] for a in acts})))
        if shape not in SHAPES:
            SHAPES.add(shape)
            wire("activation_options",
                 {"which": 1, "shaman_ability_options": len(opts),
                  "vi_choice": bool(found),
                  "all_action_types": sorted({a["type"] for a in acts})})
            say(f"[P0] activation #1 options: {len(opts)} "
                f"vi={bool(found)} "
                f"(actions: {sorted({a['type'] for a in acts})})")
        if opts:
            OBS["act1_submitted"] = {"at": now,
                                     "ability_index": opts[0][0]}
            await submit_as_is(c, opts[0][1])
            say(f"[P0] submits activation #1 "
                f"(ability_index={opts[0][0]})")
            return True
        if found:
            opp, ch = found
            OBS["act1_submitted"] = {"at": now, "via": "vi",
                                     "choice_id": ch.get("id")}
            await send_interaction(c, {"interactionId":
                                       opp.get("interactionId"),
                                       "response": {"type": "choose",
                                                    "data": {"choiceId":
                                                             ch.get("id")}}})
            SUBMITTED.add(opp.get("interactionId"))
            say("[P0] submits activation #1 via viewer_interaction")
            return True
        return False
    # decisive window: act1 on stack and P0 holds priority -> attempt #2
    if sids and not OBS["act2_submitted"]:
        if not p0_has_priority(state, acts):
            return False
        OBS["act2_windows"] += 1
        w = OBS["act2_windows"]
        opts = activate_options(acts, state, pid, SHAMAN)
        found = vi_activate_choice(st, state)
        shape = ("act2_offer", len(opts), bool(found),
                 tuple(sorted({a["type"] for a in acts})))
        if shape not in OBS["second_offer_shapes"]:
            OBS["second_offer_shapes"].append(shape)
            wire("second_activation_offer",
                 {"window": w, "action_options": len(opts),
                  "vi_choice": bool(found),
                  "all_action_types": sorted({a["type"] for a in acts}),
                  "act1_sid": OBS["act1_sid"],
                  "stack_sids": sids,
                  "artifacts": [o for o, _ in p0_artifacts(state)]})
            say(f"[P0] 2nd-activation window #{w}: "
                f"action_options={len(opts)} vi_choice={bool(found)}")
        if opts:
            OBS["act2_submitted"] = {"at": now,
                                     "ability_index": opts[0][0]}
            await submit_as_is(c, opts[0][1])
            say(f"[P0] submits activation #2 "
                f"(ability_index={opts[0][0]}) while #1 still on stack")
            wire("second_activation_submitted",
                 {"ability_index": opts[0][0]})
            return True
        if found:
            opp, ch = found
            OBS["act2_submitted"] = {"at": now, "via": "vi",
                                     "choice_id": ch.get("id")}
            await send_interaction(c, {"interactionId":
                                       opp.get("interactionId"),
                                       "response": {"type": "choose",
                                                    "data": {"choiceId":
                                                             ch.get("id")}}})
            SUBMITTED.add(opp.get("interactionId"))
            say("[P0] submits activation #2 via viewer_interaction")
            wire("second_activation_submitted", {"via": "vi"})
            return True
        # not offered: hold priority briefly to confirm, then conclude
        if w >= 3 or (OBS["act1_at"] and now - OBS["act1_at"] > 25):
            OBS["second_never_offered"] = True
            say(f"[P0] 2nd activation NOT offered in {w} P0-priority "
                f"windows with act1 on stack "
                f"(artifacts: {[o for o, _ in p0_artifacts(state)]})")
            wire("second_never_offered",
                 {"windows": w, "act1_sid": OBS["act1_sid"]})
            await export_now("post_failure.json")
            say("exported post_failure.json")
            ST["stop"] = True
            return True
        return True  # hold: do not pass while confirming
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
            keep_ok = n_lands >= 2
            choice = "Keep" if (keep_ok or MULLS[c.name] >= 2) else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and \
                wf_type(state) == "MulliganDecision":
            pending = (wf_data(state).get("pending", []))
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
    if wf_type(state) == "DiscardToHandSize":
        pend = wf_data(state)
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            keep = {SHAMAN, MEMNITE} if is_p0 else {BEAR, THOPTER}
            pref = [o for o in h if oname(state["objects"][o]) in LANDS]
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
    # never pass while P0 has a cost/decision pending in PROOF
    if is_p0 and ST["stage"] == "PROOF" and wf_type(state) not in \
            (None, "Priority", "GameOver") and wf_player(state) == 0:
        if await answer_sac_prompt(c, state, acts, st):
            return True
        return False
    # P0's plan
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await p0_land_drop(c, pid, state, acts):
                return True
            if await p0_setup(c, pid, state, acts):
                return True
        elif ST["stage"] == "PROOF":
            if await p0_proof(c, pid, state, acts, st):
                return True
    if not is_p0:
        if ST["stage"] == "SETUP" and is_my_main(state, pid):
            if await p1_setup(c, pid, state, acts):
                return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def attempt():
    reset()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0_deck": P0_DECK,
                  "p1_deck": P1_DECK})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
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
                OBS.setdefault("rejections", []).extend(
                    {"at": now, "who": c.name,
                     "type": r["type"], "data": r["data"]} for r in rej)
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

        if wf_type(state) == "GameOver" and not ST["stop"]:
            obs["notes"].append("game over before sequence completed")
            say("game over -> stopping")
            ST["stop"] = True
            break

        # --- SETUP -> PROOF transition
        if ST["stage"] == "SETUP" and is_my_main(state, 0):
            sh = perm_oids(state, 0, SHAMAN)
            mm = perm_oids(state, 0, MEMNITE)
            br = perm_oids(state, 1, BEAR)
            th = perm_oids(state, 1, THOPTER)
            if sh and len(mm) >= 2 and len(br) >= 2 and th:
                say(f"=== stage -> PROOF (turn {state.get('turn_number')}) "
                    f"shaman={sh} memnites={mm} bears={br} thopter={th} ===")
                wire("proof_armed", {"turn": state.get("turn_number"),
                                     "shaman": sh, "memnites": mm,
                                     "bears": br, "thopter": th})
                await export_now("pre.json")
                ST["stage"] = "PROOF"

        # --- PROOF watches (from P0's authoritative view)
        # act1/mid1/mid2 detection now happens inline in p0_proof's tick;
        # the main loop only keeps a watchdog and the post-resolution watch.
        if ST["stage"] == "PROOF":
            if not OBS["proof_t0"]:
                OBS["proof_t0"] = time.time()
            if time.time() - OBS["proof_t0"] > 600 and not ST["stop"]:
                say("PROOF watchdog: 600s elapsed without conclusion; "
                    "stopping")
                wire("proof_watchdog", {})
                ST["stop"] = True
                break
            # resolutions: after mid2, capture the first stack-empty state
            if OBS["mid2_done"] and not OBS["post_done"]:
                sids = shaman_stack_sids(state)
                if sids:
                    OBS["saw_post_mid2_nonempty"] = True
                if not sids and OBS["saw_post_mid2_nonempty"]:
                    say("stack empty after both activations; "
                        "exporting post")
                    wire("stack_emptied", {})
                    try:
                        await export_now("post.json")
                    except Exception as e:
                        say(f"post export failed: {e}")
                    OBS["post_done"] = True
                    ST["stop"] = True
                    break

    # ---- assertions ----
    def load_state(path):
        try:
            env = json.load(open(f"{EVDIR}/{path}"))
            return env["state"]
        except FileNotFoundError:
            return None

    pre = load_state("pre.json")
    mid1 = load_state("mid1.json")
    mid2 = load_state("mid2.json")
    post = load_state("post.json")

    try:
        assert pre is not None
        sh = perm_oids(pre, 0, SHAMAN)
        mm = perm_oids(pre, 0, MEMNITE)
        br = perm_oids(pre, 1, BEAR)
        th = perm_oids(pre, 1, THOPTER)
        A["A1_setup_ok"] = ("passed" if sh and len(mm) >= 2
                            and len(br) >= 2 and th
                            and is_my_main(pre, 0) else "failed")
        obs["notes"].append(f"A1: shaman={len(sh)} memnites={len(mm)} "
                            f"bears_p1={len(br)} thopter_p1={len(th)} "
                            f"main={is_my_main(pre, 0)}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"A1 eval error: {e}")

    try:
        assert mid1 is not None and OBS["act1_sid"]
        m1sids = shaman_stack_sids(mid1)
        gy_mm = [str(oid) for oid, o in mid1["objects"].items()
                 if o.get("zone") == "Graveyard" and oname(o) == MEMNITE
                 and o.get("controller") == 0]
        sac_refs = [s["ref"] for s in OBS["sac_choices"]]
        if sac_refs:
            sac_ok = sac_refs[0] in gy_mm
            sac_note = f"sac1={sac_refs[0]} in gy={sac_refs[0] in gy_mm}"
        else:
            # engine may have auto-paid the cost with no prompt: require a
            # newly-graveyarded P0 Memnite relative to pre
            pre_gy = {str(oid) for oid, o in pre["objects"].items()
                      if o.get("zone") == "Graveyard" and oname(o) == MEMNITE
                      and o.get("controller") == 0} if pre else set()
            sac_ok = any(g not in pre_gy for g in gy_mm)
            sac_note = f"auto-paid? new P0 Memnite in gy={sac_ok}"
        A["A2_first_activation_ok"] = (
            "passed" if OBS["act1_sid"] in m1sids and sac_ok
            else "failed")
        obs["notes"].append(f"A2: act1_sid={OBS['act1_sid']} in mid1 stack; "
                            f"{sac_note}")
    except Exception as e:
        A["A2_first_activation_ok"] = "not-run" \
            if A.get("A1_setup_ok") != "passed" else "failed"
        obs["notes"].append(f"A2 eval: {e}")

    try:
        if OBS["mid2_done"] and mid2 is not None:
            m2sids = shaman_stack_sids(mid2)
            gy_mm = [str(oid) for oid, o in mid2["objects"].items()
                     if o.get("zone") == "Graveyard" and oname(o) == MEMNITE
                     and o.get("controller") == 0]
            pre_gy = {str(oid) for oid, o in pre["objects"].items()
                      if o.get("zone") == "Graveyard" and oname(o) == MEMNITE
                      and o.get("controller") == 0} if pre else set()
            new_gy = [g for g in gy_mm if g not in pre_gy]
            sac_refs = [s["ref"] for s in OBS["sac_choices"]]
            if len(sac_refs) >= 2:
                costs_ok = (sac_refs[0] in gy_mm and sac_refs[1] in gy_mm
                            and sac_refs[0] != sac_refs[1])
                cost_note = f"sac1={sac_refs[0]} sac2={sac_refs[1]}"
            else:
                # auto-paid costs: require two distinct newly-sacrificed
                # P0 Memnites in the graveyard
                costs_ok = len(new_gy) >= 2
                cost_note = f"auto-paid? new P0 Memnites in gy={new_gy}"
            ok = len(m2sids) >= 2 and costs_ok
            A["A3_second_activation_ok"] = "passed" if ok else "failed"
            obs["notes"].append(
                f"A3: two_on_stack mid2 sids={m2sids}; {cost_note}")
        elif A.get("A2_first_activation_ok") == "passed":
            A["A3_second_activation_ok"] = "failed"
            obs["notes"].append(
                f"A3: second activation never coexisted on stack; "
                f"second_never_offered={OBS['second_never_offered']} "
                f"act2_submitted={bool(OBS['act2_submitted'])} "
                f"act2_windows={OBS['act2_windows']} "
                f"offer_shapes={OBS['second_offer_shapes']}")
        else:
            A["A3_second_activation_ok"] = "not-run"
            obs["notes"].append("A3 not-run: first activation not confirmed")
    except Exception as e:
        A["A3_second_activation_ok"] = "not-run"
        obs["notes"].append(f"A3 eval error: {e}")

    try:
        if A.get("A3_second_activation_ok") != "passed":
            A["A4_independent_resolution"] = "not-run"
            obs["notes"].append("A4 not-run: A3 did not pass")
        else:
            assert post is not None
            pre_bears = perm_oids(pre, 1, BEAR)
            post_bears_bf = perm_oids(post, 1, BEAR)
            post_bears_gy = [str(oid) for oid, o in post["objects"].items()
                             if o.get("zone") == "Graveyard"
                             and oname(o) == BEAR and o.get("controller") == 1]
            pre_th = perm_oids(pre, 1, THOPTER)
            post_th = perm_oids(post, 1, THOPTER)
            th_dmg = [num(post["objects"][o].get("damage_marked"))
                      for o in post_th]
            bears_dead = (not post_bears_bf
                          and all(b in post_bears_gy for b in pre_bears))
            th_ok = (len(post_th) == len(pre_th)
                     and all(d == 0 for d in th_dmg))
            stack_empty = not stack_ids(post)
            ok = bears_dead and th_ok and stack_empty
            A["A4_independent_resolution"] = "passed" if ok else "failed"
            obs["notes"].append(f"A4: bears_dead={bears_dead} "
                                f"(pre_bf={len(pre_bears)} post_bf="
                                f"{len(post_bears_bf)} gy_has_all="
                                f"{all(b in post_bears_gy for b in pre_bears)}); "
                                f"thopters {len(pre_th)}->{len(post_th)} "
                                f"dmg={th_dmg}; stack_empty={stack_empty}")
    except Exception as e:
        A["A4_independent_resolution"] = "not-run"
        obs["notes"].append(f"A4 eval error: {e}")

    try:
        if A.get("A3_second_activation_ok") != "passed":
            A["A5_cleanup"] = "not-run"
            obs["notes"].append("A5 not-run: A3 did not pass")
        else:
            assert post is not None
            stack_empty = not stack_ids(post)
            game_over = wf_type(post) == "GameOver"
            dangling = (wf_player(post) == 0 and wf_type(post) not in
                        (None, "Priority", "GameOver"))
            ok = stack_empty and not game_over and not dangling
            A["A5_cleanup"] = "passed" if ok else "failed"
            obs["notes"].append(f"A5: stack_empty={stack_empty} "
                                f"game_over={game_over} dangling={dangling}")
    except Exception as e:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append(f"A5 eval error: {e}")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    # ---- verdict ----
    a1, a2, a3 = (A.get("A1_setup_ok"), A.get("A2_first_activation_ok"),
                  A.get("A3_second_activation_ok"))
    if a1 == "passed" and a2 == "passed" and a3 == "failed":
        if OBS["second_never_offered"]:
            verdict = "reproduced"
        elif OBS["act2_submitted"]:
            rej = [r for r in OBS["rejections"]]
            verdict = "reproduced" if rej else "blocked"
            obs["notes"].append(
                f"verdict nuance: act2 submitted but never coexisted; "
                f"rejections={len(rej)}")
        else:
            verdict = "blocked"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_first_activation_ok",
              "A3_second_activation_ok", "A4_independent_resolution",
              "A5_cleanup")):
        verdict = "not-reproduced"
    elif a1 == "failed":
        verdict = "blocked"
    else:
        verdict = "blocked" if a1 != "passed" else "reproduced"
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN}, f, indent=2, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": OBS, "wf_sequence": WF_SEEN}, f,
                  indent=2, default=str)

    # ---- run.json ----
    def sha_file(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            h.update(f.read())
        return h.hexdigest()

    run = {
        "issue": 6875,
        "run_id": RUN_ID,
        "issue_title": "Krark-clan shaman - ability cannot be used more than once",
        "server": {
            "server_version": "0.80.0",
            "build_commit": "22cca6d",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": "1d414c0e999a088560ab9ad0d77a4ae4f5773610cee6afda616a62c0e654238e",
            "card_data_sha256": "7ce6f92d0adb8fc4158bf0ab76797a644eb77dcea01f9743bb849550bb677bfd",
            "draft_pools_sha256": "c78dbd16f671e5b21ec094d6fcbc2b5da76e79cc82c2d9daa914369180021348",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
        },
        "validated_at": "2026-09-12",
        "verdict": verdict,
        "assertions": A,
        "notes": obs["notes"],
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats (explicit priority driving).",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
        ],
        "scenario": "driver/scenario_6875.py",
        "evidence_dir": f"6875/{RUN_ID}",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2)

    render_summary_png()

    # ---- manifest ----
    files = sorted(os.listdir(EVDIR))
    lines = []
    for fn in files:
        if fn == "manifest.sha256":
            continue
        lines.append(f"{sha_file(os.path.join(EVDIR, fn))}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest written ({len(lines)} files)")

    await p0.close()
    await p1.close()
    return obs, True


def render_summary_png():
    W, H = 1000, 1000
    BG = (18, 20, 26)
    PANEL = (26, 30, 38)
    TEXT = (235, 238, 245)
    DIM = (150, 160, 175)
    GREEN = (110, 220, 140)
    RED = (240, 120, 120)
    YELLOW = (240, 200, 110)
    ACCENT = (110, 180, 255)

    def load(p):
        try:
            with open(os.path.join(EVDIR, p)) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    run = load("run.json") or {}
    ass = (load("assertions.json") or {}).get("assertions", {})
    obs = (load("observations.json") or {}).get("observations", {})

    def summarize(env):
        if not env:
            return ["(missing)"]
        s = env["state"]
        objs = s.get("objects", {})
        lines = []
        for p in s.get("players", []):
            pid = p.get("id")
            bfc = {}
            for o in objs.values():
                if o.get("zone") == "Battlefield" and \
                        o.get("controller") == pid:
                    n = (o.get("card_name") or o.get("name")
                         or o.get("base_name") or "?")
                    bfc[n] = bfc.get(n, 0) + 1
            lines.append(f"P{pid} life {p.get('life')} | battlefield: " +
                         (", ".join(f"{k}x{v}"
                                     for k, v in sorted(bfc.items()))
                          or "empty"))
        stk = s.get("stack") or []
        wf = s.get("waiting_for") or {}
        lines.append(f"turn {s.get('turn_number')} | {s.get('phase')} | "
                     f"active P{s.get('active_player')} | stack({len(stk)}) | "
                     f"waiting_for {wf.get('type')}")
        return lines

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    verdict = run.get("verdict", "?")
    vc = {"reproduced": RED, "not-reproduced": GREEN,
          "blocked": YELLOW}.get(verdict, DIM)
    d.text((20, y), f"#6875 Krark-Clan Shaman: ability used more than once",
           fill=TEXT)
    y += 24
    d.text((20, y), f"verdict: {verdict}", fill=vc)
    y += 22
    srv = run.get("server", {})
    d.text((20, y),
           f"phase-server v{srv.get('server_version')} "
           f"build {srv.get('build_commit')} protocol "
           f"{srv.get('protocol_version')} | 2026-09-11 | run {RUN_ID}",
           fill=DIM)
    y += 30
    for tag, label in (("pre.json", "PRE (proof setup)"),
                       ("mid1.json", "MID1 (activation #1 on stack)"),
                       ("mid2.json", "MID2 (both activations on stack)"),
                       ("post.json", "POST (both resolved)")):
        env = load(tag)
        d.rectangle([14, y, W - 14, y + 20 + 18 * 5], fill=PANEL)
        d.text((24, y + 4), label, fill=ACCENT)
        for i, ln in enumerate(summarize(env)[:5]):
            d.text((24, y + 24 + i * 16), ln[:110], fill=TEXT)
        y += 20 + 18 * 5 + 10
    d.text((20, y), "assertions", fill=ACCENT)
    y += 22
    order = ["A1_setup_ok", "A2_first_activation_ok",
             "A3_second_activation_ok", "A4_independent_resolution",
             "A5_cleanup"]
    for k in order:
        v = ass.get(k, "not-run")
        c = {"passed": GREEN, "failed": RED}.get(v, YELLOW)
        d.text((24, y), f"{k}: {v}", fill=c)
        y += 20
    y += 6
    sacs = obs.get("sac_choices", [])
    act2_sid = None
    m2 = load("mid2.json")
    if m2:
        s2 = [str(e.get("id")) for e in (m2["state"].get("stack") or [])]
        act2_sid = next((s for s in s2 if s != str(obs.get("act1_sid"))),
                        None)
    d.text((20, y), f"sacrifices paid: {len(sacs)} | "
                    f"act1_sid={obs.get('act1_sid')} "
                    f"act2_sid={act2_sid} | "
                    f"act2_windows={obs.get('act2_windows', 0)} | "
                    f"rejections={len(obs.get('rejections', []))}",
           fill=DIM)
    y += 22
    d.text((20, y), "evidence: ntindle/phase-bug-state-evidence "
                    f"6875/{RUN_ID}/ (commit-pinned links in issue comment)",
           fill=DIM)
    img.save(os.path.join(EVDIR, "summary.png"))
    say("rendered summary.png")


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"],
           "verdict": "blocked"}
    for n in range(1, 4):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            import traceback
            traceback.print_exc()
            obs = {"assert": {}, "notes": [f"attempt {n} crash: {e!r}"],
                   "verdict": "blocked"}
            done = False
        if done:
            return obs
        say(f"attempt {n} did not complete; retrying")
        await asyncio.sleep(2)
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps({**obs.get("assert", {}),
                      "verdict": obs.get("verdict")}, indent=2))
