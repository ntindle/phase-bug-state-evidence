#!/usr/bin/env python3
"""Issue #6870: Lithoform Engine {2},{T} won't let you copy a planeswalker
loyalty ability.

Oracle (Lithoform Engine ability 0): "{2}, {T}: Copy target activated or
triggered ability you control. You may choose new targets for the copy."
Card data parses this faithfully (target StackAbility controller You,
retarget MayChooseNewTargets); triage points at runtime target enumeration
excluding loyalty abilities.

Plan (two human seats, native engine, v0.80.0 / protocol 69):
  SETUP - land drops; P0 casts Lithoform Engine, then Chandra Nalaar.
  PROOF - on a fresh P0 main phase (both permanents settled a full turn):
          export pre_activate.json; activate Chandra +1 (ability_index 0)
          targeting P1; with the loyalty ability on the stack, activate
          Lithoform Engine {2},{T} (ability_index 0).
  COPY  - the Engine ability resolves -> copy of the loyalty ability is
          created; answer the may-choose-new-targets prompt (choose P1);
          watch both the copy and the original resolve.

Behavioral contract:
  A1 setup_ok       pre_activate.json: Engine untapped on P0 BF, Chandra
                    on P0 BF, P0 main phase
  A2 loyalty_on_stack loyalty ability confirmed on the stack after the +1
                    (new P0-controlled stack object, DealDamage/1 signature)
  A3 engine_targets_loyalty Engine activation accepted AND the loyalty
                    ability was a legal target (prompt candidate ref, or
                    auto-target with target ref == loyalty sid). Fails if
                    the activation is rejected, the ability is not offered,
                    or the target prompt omits the loyalty ability.
  A4 copy_damage     post: P1 life dropped by exactly 2 (original 1 +
                    copy 1); Chandra loyalty 6 -> 7
  A5 cleanup         stack empty in post.json, game not over

Verdict = reproduced iff A1+A2 pass and (A3 fails, or A3 passes but A4
fails = targeting worked yet the copy never dealt damage, a related
failure of the same copy ability); not-reproduced iff A1..A4 pass.
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
RUN_ID = "20260911-6870d"
EVID_ISSUE = "6870"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ENGINE = "Lithoform Engine"
CHANDRA = "Chandra Nalaar"
MOUNTAIN = "Mountain"
LANDS = (MOUNTAIN,)

ST = {"stage": "SETUP", "stop": False, "retry": False}
ACT = {}          # {"plus1": {...}, "plus1_targeted": bool, "engine": {...},
                  #  "engine_targeted": {...}}
MULLS = {"P0": 0, "P1": 0}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
OBS = {}          # observations
IDS = {"loyalty": None, "engine_ability": None}
SEEN_TURN = {}    # name -> turn_number first seen on P0 BF
STACK_BEFORE_PLUS1 = {"ids": None}
ENGINE_OFFER_WATCH = {"since": None}
TARGET_BUG_WATCH = {"since": None}


def reset_globals():
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False})
    ACT.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.update({"iid": None})
    OBS.clear()
    IDS.update({"loyalty": None, "engine_ability": None})
    SEEN_TURN.clear()
    STACK_BEFORE_PLUS1.update({"ids": None})
    ENGINE_OFFER_WATCH.update({"since": None})
    TARGET_BUG_WATCH.update({"since": None})


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


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def loyalty_of(o):
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    return None


def stack_ids(state):
    # stack entries live in state["stack"] (NOT as objects with
    # zone=="Stack"); each entry has id/controller/source_id/kind.
    return [str(e.get("id")) for e in (state.get("stack") or [])]


def stack_entry(state, sid):
    for e in (state.get("stack") or []):
        if str(e.get("id")) == str(sid):
            return e
    return None


def stack_entry_name(e):
    kind = e.get("kind") or {}
    data = kind.get("data") or {}
    ab = data.get("ability") or {}
    desc = ab.get("description") or ""
    return f"stack:{kind.get('type')}:{desc[:70]}"


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
            wire("rejected", {"who": c.name, "type": t, "data": data})
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
    """Resolve opportunity candidates to (choiceId, ref_oid, seat, name,
    zone). Player candidates carry seat instead of a reference."""
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
            name, zone, controller = (stack_entry_name(se), "Stack",
                                      se.get("controller"))
        else:
            name, zone, controller = oname(o), o.get("zone"), \
                o.get("controller")
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": name, "zone": zone,
                    "controller": controller,
                    "text": ch.get("text")})
    return out


def perm_oids(state, pid, name):
    return [str(oid) for oid, o in bf(state, pid) if oname(o) == name]


def activate_options(acts, state, pid, name, want_index):
    """ActivateAbility options for `name` on `pid`'s battlefield."""
    srcs = set(perm_oids(state, pid, name))
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src in srcs:
            out.append((d.get("ability_index"), a))
    if want_index is not None:
        return [a for i, a in out if i == want_index]
    return [a for _, a in out]


def wf_player(state):
    return (state.get("waiting_for") or {}).get("data", {}).get("player")


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def is_target_wait(state):
    return wf_type(state) in ("TargetSelection", "TriggerTargetSelection")


async def answer_plus1_target(c, state, acts, st):
    """Chandra +1 target prompt: choose P1 (seat 1)."""
    if not ACT.get("plus1") or ACT.get("plus1_targeted"):
        return False
    if not is_target_wait(state) or wf_player(state) != 0:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        # the +1 targets players/planeswalkers, not stack objects
        if any(x["zone"] == "Stack" for x in cands):
            continue
        want = next((x for x in cands if x["seat"] == 1), None)
        if not want:
            continue
        if ("plus1_prompt", len(chs)) not in SHAPES:
            SHAPES.add(("plus1_prompt", len(chs)))
            wire("plus1_target_prompt",
                 {"rtype": resp.get("type"), "candidates": cands,
                  "opportunity": opp})
            say(f"[P0] +1 target prompt: "
                f"{[(x['seat'], x['name']) for x in cands]}")
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if resp.get("type") == "schema" and spec_type in ("sequence",
                                                         "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif resp.get("type") == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] +1: unexpected prompt shape {resp.get('type')}/"
                f"{spec_type}")
            continue
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        ACT["plus1_targeted"] = {"at": time.time()}
        say("[P0] +1 targets P1")
        return True
    return False


async def answer_engine_target(c, state, acts, st):
    """Lithoform Engine copy-ability target prompt: the loyalty ability on
    the stack (IDS['loyalty']) must be among the candidates. If the prompt
    appears without it, record the bug signature and leave the prompt
    pending (a failure state may intentionally wait for a decision)."""
    if not ACT.get("engine") or ACT.get("engine_targeted"):
        return False
    if not is_target_wait(state) or wf_player(state) != 0:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        if not any(x["zone"] == "Stack" for x in cands):
            continue
        if ("engine_prompt", len(chs)) not in SHAPES:
            SHAPES.add(("engine_prompt", len(chs)))
            wire("engine_target_prompt",
                 {"rtype": resp.get("type"), "candidates": cands,
                  "loyalty_sid": IDS["loyalty"], "opportunity": opp})
            say(f"[P0] engine target prompt candidates: "
                f"{[(x['name'], x['ref'], x['controller']) for x in cands]} "
                f"loyalty_sid={IDS['loyalty']}")
        want = next((x for x in cands if x["ref"] == IDS["loyalty"]), None)
        if not want:
            OBS["engine_target_bug"] = {
                "candidates": cands, "loyalty_sid": IDS["loyalty"],
                "at": time.time()}
            wire("engine_target_bug", OBS["engine_target_bug"])
            say("[P0] BUG SIGNATURE: engine target prompt omits the "
                f"loyalty ability (sid={IDS['loyalty']}); leaving pending")
            if TARGET_BUG_WATCH["since"] is None:
                TARGET_BUG_WATCH["since"] = time.time()
            return False
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if resp.get("type") == "schema" and spec_type in ("sequence",
                                                         "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif resp.get("type") == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] engine: unexpected prompt shape {resp.get('type')}/"
                f"{spec_type}")
            continue
        OBS.setdefault("target_submissions", []).append(
            {"choice_id": want["choice_id"], "ref": want["ref"]})
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        ACT["engine_targeted"] = {"at": time.time(), "auto": False,
                                  "ref": want["ref"]}
        say(f"[P0] engine targets loyalty ability (sid={want['ref']})")
        return True
    return False


async def answer_copy_retarget(c, state, acts, st):
    """In COPY stage: the copy's may-choose-new-targets prompt. Choose P1.
    Handles TargetSelection for players/planeswalkers and exactChoices
    OptionalEffectChoice prompts mentioning targets."""
    if ST["stage"] != "COPY":
        return False
    if wf_player(state) != 0:
        return False
    wft = wf_type(state)
    if wft not in ("TargetSelection", "TriggerTargetSelection",
                   "CopyRetarget"):
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        prompt_text = json.dumps(opp.get("prompt") or opp.get("title") or "")
        if wft in ("TargetSelection", "TriggerTargetSelection",
                   "CopyRetarget"):
            # retarget candidates are players/planeswalkers, never stack
            if any(x["zone"] == "Stack" for x in cands):
                continue
            want = next((x for x in cands if x["seat"] == 1), None)
            if not want:
                continue
            if ("retarget_prompt", len(chs)) not in SHAPES:
                SHAPES.add(("retarget_prompt", len(chs)))
                wire("copy_retarget_prompt",
                     {"rtype": rtype, "candidates": cands,
                      "opportunity": opp})
                say(f"[P0] copy retarget prompt: "
                    f"{[(x['seat'], x['name']) for x in cands]}")
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            if rtype == "schema" and spec_type in ("sequence", "select"):
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": [want["choice_id"]]}}
            elif rtype == "exactChoices":
                resp_out = {"type": "choose",
                            "data": {"choiceId": want["choice_id"]}}
            else:
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            SUBMITTED.add(iid)
            OBS["retarget_answered"] = {"seat": 1, "at": time.time()}
            say("[P0] copy keeps/chooses P1 as new target")
            return True
        if rtype == "exactChoices" and "target" in prompt_text.lower():
            # boolean may-choice about choosing new targets
            pick = None
            for x in cands:
                t = json.dumps(x).lower()
                if "true" in t or "yes" in t or "choose" in t:
                    pick = x
                    break
            pick = pick or cands[0]
            if ("retarget_may", len(chs)) not in SHAPES:
                SHAPES.add(("retarget_may", len(chs)))
                wire("copy_retarget_may",
                     {"candidates": cands, "opportunity": opp,
                      "pick": pick["choice_id"]})
                say(f"[P0] copy may-retarget choice: {prompt_text[:120]}")
            await send_interaction(
                c, {"interactionId": iid,
                    "response": {"type": "choose",
                                 "data": {"choiceId": pick["choice_id"]}}})
            SUBMITTED.add(iid)
            OBS["retarget_may_answered"] = {"pick": pick["choice_id"],
                                           "at": time.time()}
            return True
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
            if is_p0:
                has_piece = ENGINE in lands or CHANDRA in lands
                keep_ok = n_lands >= 2 and (has_piece or MULLS[c.name] >= 2)
            else:
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
            keep = {ENGINE, CHANDRA} if is_p0 else set()
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
    # engine-advertised mana payments: submit as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # answer prompts before anything else
    if is_p0:
        if await answer_plus1_target(c, state, acts, st):
            return True
        if await answer_engine_target(c, state, acts, st):
            return True
        if await answer_copy_retarget(c, state, acts, st):
            return True
    # never pass while P0 has a decision pending
    if is_p0 and wf_type(state) in ("OptionalCostChoice", "TargetSelection",
                                    "TriggerTargetSelection", "CopyRetarget",
                                    "ManaPayment",
                                    "ChooseXValue", "DiscardChoice",
                                    "OrderTriggers") \
            and wf_player(state) == 0:
        return False
    # P0's plan
    if is_p0 and is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if ST["stage"] == "SETUP":
            if await p0_setup_cast(c, pid, state, acts):
                return True
        elif ST["stage"] == "PROOF":
            if await p0_proof(c, pid, state, acts):
                return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    # Hold priority while the loyalty ability is in flight: P0 must not
    # pass it away before the Engine activation, or the ability resolves
    # and the proof window closes.
    hold = (is_p0 and ACT.get("plus1_targeted") and not ACT.get("engine")
            and not OBS.get("engine_target_bug"))
    for a in acts:
        if a["type"] == "PassPriority":
            if hold:
                wire("priority_held", {"stage": ST["stage"]})
                return False
            await submit_as_is(c, a)
            return True
    return False


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, MOUNTAIN)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


async def p0_setup_cast(c, pid, state, acts):
    # Engine first (4), then Chandra (5)
    if not perm_oids(state, pid, ENGINE):
        oid = find_hand(state, pid, ENGINE)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, MOUNTAIN) >= 4:
            await submit_as_is(c, a)
            say("P0 casts Lithoform Engine")
            return True
    if not perm_oids(state, pid, CHANDRA):
        oid = find_hand(state, pid, CHANDRA)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, MOUNTAIN) >= 5:
            await submit_as_is(c, a)
            say("P0 casts Chandra Nalaar")
            return True
    return False


async def p0_proof(c, pid, state, acts):
    turn = state.get("turn_number")
    # both permanents must have settled a full turn before the proof
    first_turns = [SEEN_TURN.get(n) for n in (ENGINE, CHANDRA)]
    if any(t is None for t in first_turns) or turn <= max(first_turns):
        return False
    # step 1: Chandra +1 (ability_index 0), targets chosen via prompt
    if not ACT.get("plus1"):
        opts = activate_options(acts, state, pid, CHANDRA, 0)
        if ("plus1_opts", len(opts)) not in SHAPES:
            SHAPES.add(("plus1_opts", len(opts)))
            wire("plus1_options", {"count": len(opts), "all_action_types":
                 sorted({a["type"] for a in acts})})
            say(f"[P0] Chandra +1 options: {len(opts)}")
        if not opts:
            return False
        STACK_BEFORE_PLUS1["ids"] = set(stack_ids(state))
        ACT["plus1"] = {"at": time.time(), "turn": turn}
        await submit_as_is(c, opts[0])
        say(f"[P0] activates Chandra +1 (ability_index 0) turn={turn}")
        return True
    # step 2: with the loyalty ability on the stack, activate Engine {2},{T}
    if ACT.get("plus1_targeted") and not ACT.get("engine") \
            and IDS.get("loyalty") and IDS["loyalty"] in stack_ids(state):
        opts = activate_options(acts, state, pid, ENGINE, 0)
        if ("engine_opts", len(opts)) not in SHAPES:
            SHAPES.add(("engine_opts", len(opts)))
            wire("engine_options", {"count": len(opts), "all_action_types":
                 sorted({a["type"] for a in acts})})
            say(f"[P0] Engine copy-ability options: {len(opts)}")
        if not opts:
            # ability not offered while its only legal target sits on the
            # stack: record; the stuck watch below converts this to a
            # captured failure
            if ENGINE_OFFER_WATCH["since"] is None:
                ENGINE_OFFER_WATCH["since"] = time.time()
                wire("engine_not_offered",
                     {"loyalty_sid": IDS["loyalty"],
                      "all_action_types":
                      sorted({a["type"] for a in acts})})
                say("[P0] Engine {2},{T} NOT advertised while loyalty "
                    "ability on stack")
            return False
        ENGINE_OFFER_WATCH["since"] = None
        await export_now("pre_activate.json")
        OBS["life_before"] = life_of(state, 1)
        ACT["engine"] = {"at": time.time(), "turn": turn}
        await submit_as_is(c, opts[0])
        say(f"[P0] activates Engine copy ability (ability_index 0) "
            f"turn={turn}")
        wire("engine_activated", {"turn": turn,
                                  "loyalty_sid": IDS["loyalty"]})
        return True
    return False


async def p1_step(c, pid, state, acts):
    if is_my_main(state, pid):
        lid = find_hand(state, pid, MOUNTAIN)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    return False


def stack_blob(state, sid):
    return json.dumps(stack_entry(state, sid) or {}, default=str)


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((ENGINE, 8), (CHANDRA, 8), (MOUNTAIN, 44)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((MOUNTAIN, 60)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    mid_exported = False
    TIMEOUT = 1800
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
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        # --- stage transitions
        if ST["stage"] == "SETUP":
            for n in (ENGINE, CHANDRA):
                oids = perm_oids(state, 0, n)
                if oids and n not in SEEN_TURN:
                    SEEN_TURN[n] = state.get("turn_number")
                    say(f"{n} on BF turn {SEEN_TURN[n]}")
            if ENGINE in SEEN_TURN and CHANDRA in SEEN_TURN:
                ST["stage"] = "PROOF"
                say("=== stage -> PROOF ===")

        # --- identify the loyalty ability on the stack
        if ST["stage"] == "PROOF" and ACT.get("plus1_targeted") \
                and not IDS["loyalty"]:
            before = STACK_BEFORE_PLUS1["ids"] or set()
            ch_oids = set(perm_oids(state, 0, CHANDRA))
            new_ids = [sid for sid in stack_ids(state) if sid not in before]
            p0_new = [sid for sid in new_ids
                      if str((stack_entry(state, sid) or {})
                             .get("controller")) == "0"]
            if p0_new:
                # prefer the entry matching the +1 signature
                def score(sid):
                    e = stack_entry(state, sid) or {}
                    kind = (e.get("kind") or {}).get("type", "")
                    ab = ((e.get("kind") or {}).get("data") or {}) \
                        .get("ability") or {}
                    desc = ab.get("description") or ""
                    s = 0
                    if kind == "ActivatedAbility":
                        s += 2
                    if "deals 1 damage" in desc:
                        s += 2
                    if str(ab.get("source_id")) in ch_oids:
                        s += 2
                    return s
                p0_new.sort(key=score, reverse=True)
                IDS["loyalty"] = p0_new[0]
                wire("loyalty_on_stack",
                     {"sid": IDS["loyalty"],
                      "score": score(IDS["loyalty"]),
                      "candidates": p0_new,
                      "entry": stack_entry(state, IDS["loyalty"])})
                say(f"loyalty ability on stack: sid={IDS['loyalty']}")
                await export_now("mid_loyalty.json")
                mid_exported = True

        # --- identify the Engine ability on the stack + auto-target
        if ACT.get("engine") and not ACT.get("engine_targeted") \
                and not IDS["engine_ability"]:
            eng_oids = set(perm_oids(state, 0, ENGINE))
            new_ids = [sid for sid in stack_ids(state)
                       if sid != IDS["loyalty"]]
            p0_new = [sid for sid in new_ids
                      if str((stack_entry(state, sid) or {})
                             .get("controller")) == "0"]
            if p0_new:
                def escore(sid):
                    e = stack_entry(state, sid) or {}
                    ab = ((e.get("kind") or {}).get("data") or {}) \
                        .get("ability") or {}
                    desc = ab.get("description") or ""
                    s = 0
                    if "Copy target activated or triggered" in desc:
                        s += 3
                    if str(ab.get("source_id")) in eng_oids:
                        s += 2
                    return s
                p0_new.sort(key=escore, reverse=True)
                IDS["engine_ability"] = p0_new[0]
                wire("engine_ability_on_stack",
                     {"sid": IDS["engine_ability"],
                      "entry": stack_entry(state, IDS["engine_ability"])})
                say(f"engine ability on stack: sid={IDS['engine_ability']}")
                # auto-target check: does it reference the loyalty ability?
                blob = stack_blob(state, IDS["engine_ability"])
                if IDS["loyalty"] and str(IDS["loyalty"]) in blob:
                    ACT["engine_targeted"] = {"at": time.time(),
                                              "auto": True,
                                              "ref": IDS["loyalty"]}
                    say("[P0] engine auto-targeted the loyalty ability "
                        "(no prompt)")
                    wire("engine_autotarget",
                         {"engine_sid": IDS["engine_ability"],
                          "loyalty_sid": IDS["loyalty"]})

        # --- COPY stage: engine ability resolved
        if ACT.get("engine_targeted") and ST["stage"] == "PROOF":
            if IDS["engine_ability"] and \
                    IDS["engine_ability"] not in stack_ids(state):
                ST["stage"] = "COPY"
                say("=== stage -> COPY (engine ability resolved) ===")
                copies = [e for e in (state.get("stack") or [])
                          if str(e.get("id")) != IDS["loyalty"]]
                wire("copy_stage", {"at": time.time(),
                                    "stack": state.get("stack"),
                                    "waiting_for": state.get("waiting_for")})
                OBS["copy_seen"] = [e.get("id") for e in copies]

        # --- DONE: stack fully empty after targeting
        if ST["stage"] == "COPY" and ACT.get("engine_targeted") \
                and not ST["stop"]:
            if not stack_ids(state):
                await asyncio.sleep(2)
                st2 = p0.latest
                state2 = st2["state"] if st2 else state
                if not stack_ids(state2):
                    OBS["life_after"] = life_of(state2, 1)
                    ch = perm_oids(state2, 0, CHANDRA)
                    OBS["loyalty_after"] = loyalty_of(
                        state2["objects"][ch[0]]) if ch else None
                    say(f"DONE: stack empty; P1 life={OBS['life_after']} "
                        f"chandra loyalty={OBS['loyalty_after']}")
                    await export_now("post_activate.json")
                    try:
                        await export_now("post.json")
                    except Exception as e:
                        say(f"final post export failed: {e}")
                    ST["stop"] = True
            elif time.time() - ACT["engine_targeted"]["at"] > 180:
                say("resolution timeout after 180s; exporting post anyway")
                wire("resolution_timeout", {})
                try:
                    await export_now("post_activate.json")
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # --- stuck watches
        if ST["stage"] == "PROOF" and not ST["stop"]:
            # loyalty ability never materialized on the stack
            pt = (ACT.get("plus1_targeted") or {}).get("at")
            if pt and not IDS["loyalty"] and time.time() - pt > 120:
                say("plus1 targeted but no loyalty ability on stack after "
                    "120s; exporting post")
                wire("loyalty_missing_timeout", {})
                OBS["loyalty_missing"] = True
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            # engine never offered while its only legal target is on stack
            since = ENGINE_OFFER_WATCH["since"]
            if since and time.time() - since > 90:
                say("engine copy ability never offered after 90s; "
                    "exporting post")
                wire("engine_offer_timeout", {})
                OBS["engine_never_offered"] = True
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            # target prompt omits the loyalty ability: failure state may
            # intentionally still wait for the decision
            bsince = TARGET_BUG_WATCH["since"]
            if bsince and time.time() - bsince > 120:
                say("target prompt still omits loyalty ability after 120s; "
                    "exporting post (failure state)")
                wire("target_bug_timeout", {})
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            # engine activation rejected outright
            if ACT.get("engine") and not ACT.get("engine_targeted") \
                    and not IDS["engine_ability"]:
                rej_lines = []
                try:
                    for line in open(f"{EVDIR}/wire_log.jsonl"):
                        d = json.loads(line)
                        if d["event"] == "rejected" and d["t"] >= \
                                ACT["engine"]["at"]:
                            rej_lines.append(d)
                except Exception:
                    pass
                if rej_lines and time.time() - ACT["engine"]["at"] > 20:
                    say("engine activation rejected; exporting post")
                    wire("engine_rejected_stop", {"count": len(rej_lines)})
                    OBS["engine_rejected"] = True
                    try:
                        await export_now("post.json")
                    except Exception as e:
                        say(f"post export failed: {e}")
                    ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    # ---- assertions
    try:
        pre = load_state("pre_activate.json")
        eng = perm_oids(pre, 0, ENGINE)
        ch = perm_oids(pre, 0, CHANDRA)
        eng_untapped = eng and not pre["objects"][eng[0]].get("tapped")
        A["A1_setup_ok"] = ("passed"
                            if eng_untapped and ch
                            and is_my_main(pre, 0) else "failed")
        obs["notes"].append(f"A1: engine_untapped={bool(eng_untapped)} "
                            f"chandra={bool(ch)} main={is_my_main(pre, 0)}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"A1 eval error: {e}")

    A["A2_loyalty_on_stack"] = ("passed" if IDS["loyalty"] else "failed")
    obs["notes"].append(f"A2: loyalty_sid={IDS['loyalty']}")

    A["A3_engine_targets_loyalty"] = (
        "passed" if ACT.get("engine_targeted") else "failed")
    obs["notes"].append(
        f"A3: engine_targeted={ACT.get('engine_targeted')} "
        f"target_bug={bool(OBS.get('engine_target_bug'))} "
        f"never_offered={bool(OBS.get('engine_never_offered'))} "
        f"rejected={bool(OBS.get('engine_rejected'))}")

    try:
        post = load_state("post_activate.json")
        life_b, life_a = OBS.get("life_before"), OBS.get("life_after")
        ch = perm_oids(post, 0, CHANDRA)
        loy = loyalty_of(post["objects"][ch[0]]) if ch else None
        dmg = ((life_b - life_a)
               if life_b is not None and life_a is not None else None)
        A["A4_copy_damage"] = ("passed"
                               if dmg == 2 and loy == 7 else "failed")
        obs["notes"].append(f"A4: life {life_b}->{life_a} (dmg={dmg}, "
                            f"expected 2); chandra loyalty={loy} "
                            f"(expected 7)")
    except Exception as e:
        A["A4_copy_damage"] = "not-run"
        obs["notes"].append(f"A4 eval error: {e}")

    try:
        final = load_state("post.json")
        stack_empty = not stack_ids(final)
        game_over = wf_type(final) == "GameOver"
        A["A5_cleanup"] = ("passed" if stack_empty and not game_over
                           else "failed")
        obs["notes"].append(f"A5: stack_empty={stack_empty} "
                            f"game_over={game_over}")
    except Exception as e:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append(f"A5 eval error: {e}")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "observations": {k: v for k, v in OBS.items()
                                    if k not in ("life_before",)},
                   "ids": IDS, "act": ACT}, f, indent=2)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": OBS, "wf_sequence": WF_SEEN,
                   "ids": IDS}, f, indent=2)

    a3 = A.get("A3_engine_targets_loyalty")
    a4 = A.get("A4_copy_damage")
    if A.get("A1_setup_ok") == "passed" and \
            A.get("A2_loyalty_on_stack") == "passed":
        if a3 == "passed" and a4 == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")

    await p0.close()
    await p1.close()
    return obs, True


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = ({"assert": {},
                          "notes": [f"attempt {n} crash: {e!r}]"]}), False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps({**obs.get("assert", {}),
                      "verdict": obs.get("verdict")}, indent=2))
