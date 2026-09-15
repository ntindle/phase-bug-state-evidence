#!/usr/bin/env python3
"""Issue #7168: Ivy, Gleeful Spellthief -- copy doesn't target Ivy.

Oracle: "Flying. Whenever a player casts a spell that targets only a single
creature other than Ivy, you may copy that spell. The copy targets Ivy.
(A copy of an Aura spell becomes a token.)"

Reported (Discord): the spell is copied correctly, but the copy does NOT
target Ivy.

Parse (pinned v0.83.0 card-data.json): the SpellCast trigger emits
  CopySpell{target: TriggeringSource, retarget: KeepOriginalTargets}
and "The copy targets ~" is an Unimplemented sub-ability
(unrecognized_clause_head). The retarget-to-Ivy child is unsupported, so the
copy keeps the original target.

Plan (native engine, v0.83.0 / protocol 70, two human driver seats):
  P0: 4x ivy, gleeful spellthief / 8x grizzly bears / 24x forest / 24x island.
      Casts Ivy ({G}{U}), then exactly one Bear ({1}{G}).
  P1: 20x lightning bolt / 40x mountain.
      Casts one Bolt targeting P0's Bear once Ivy is on the BF.
  P0 answers Ivy's optional-copy decision with accept=true.
  Observe the copy on the stack (controller/targets), watch for any
  CopyRetarget prompt, then observe resolution. Ivy is 2/1: she dies iff
  the copy targets her.

Behavioral contract:
  A1 setup_ok          Ivy + one Bear on P0 BF; P1 cast Bolt at the Bear.
  A2 trigger_offered   Ivy's optional-copy decision raised for P0.
  A3 copy_created      after accept, a second Bolt spell (controller 0,
                       the copy) observed on the stack.
  A4 copy_targets_ivy  the copy's targets include Ivy's oid. (expect FAILED)
  A5 no_retarget_prompt no CopyRetarget/TargetSelection prompt was raised
                       for the copy.
  A6 outcome           post: Ivy alive on BF and the targeted Bear dead --
                       the copy did not hit Ivy.
  A7 cleanup           stack empty, game advances, post.json exported.

Verdict: reproduced iff A1-A3 pass and (A4 fails or A5 fails).
         not-reproduced iff A1-A5 pass.
         blocked iff A1 or A2 fails.

Driver notes:
  - Target candidates live under response.data.candidates with numeric-string
    references; the ACTUALLY submitted candidate oid is recorded (cf. #6906,
    #7021 ref_key normalization).
  - OptionalEffectChoice "you may" prompts carry empty-text choices with
    value surfaces (role=accept, value true/false); accept via accept=true
    (cf. #6987).
  - After a client casts, it gets priority first: gate main-phase casting on
    in-flight state but always fall through to PassPriority (cf. #6862).
  - The Aura-copy clause ("a copy of an Aura spell becomes a token") is part
    of the same unsupported retarget child; no Aura leg is run -- recorded as
    not-run with the parse evidence.
"""
import asyncio
import copy as _copy
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260915-7168d"
ISSUE = 7168
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

IVY = "ivy, gleeful spellthief"
BEAR = "grizzly bears"
BOLT = "lightning bolt"
FOREST = "forest"
ISLAND = "island"
MOUNTAIN = "mountain"

P0_DECK = [(IVY, 4), (BEAR, 8), (FOREST, 24), (ISLAND, 24)]
P1_DECK = [(BOLT, 20), (MOUNTAIN, 40)]

TIMEOUT = 1500
TURN_CAP = 40

SERVER_IDENTITY = {
    "validated_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "server_binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
}

ST = {}
SUBMITTED = set()
MULLS = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> BOLT_CAST -> COPY_DECISION -> COPY_WINDOW
                           #   -> RESOLVED -> DONE
        "stop": False,
        "ivy_oid": None,
        "bear_oid": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "bolt_cast": False,
        "bolt_in_flight": False,
        "bolt_target_chosen": False,
        "bolt_target_oid": None,
        "copy_decision_seen": False,
        "copy_accepted": False,
        "copy_retarget_seen": False,
        "saw_two_bolts": False,
        "copy_entry": None,      # raw stack entry of the copy (controller 0)
        "orig_entry": None,      # raw stack entry of the original (ctrl 1)
        "copy_stack_id": None,   # stack id of the detected copy
        "seen_stack_ids": set(), # stack ids observed since Bolt cast
        "trigger_seen": False,
        "bolt_cast_turn": None,
        "quiescent_ticks": 0,
    })


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


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


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def life_of(state, pid):
    return player_of(state, pid).get("life")


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped")
                and (name is None or nm == name)):
            out.append(int(oid))
    return out


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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def stack_entries(state):
    return state.get("stack") or []


def bolt_spells(state):
    return [e for e in stack_entries(state)
            if BOLT in str(e.get("name") or "").lower()]


def trigger_entries(state):
    out = []
    for e in stack_entries(state):
        kind = e.get("kind") or {}
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        if ktype == "TriggeredAbility":
            out.append(e)
    return out


def ints_in(obj, acc=None):
    if acc is None:
        acc = set()
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, int):
        acc.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            ints_in(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            ints_in(v, acc)
    return acc


def candidate_ref_oid(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


def ref_key(ref):
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
        return None
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


def accept_choice(chs):
    for ch in chs:
        for sf in ch.get("surfaces", []) or []:
            d = sf.get("data") or {}
            if (sf.get("type") == "value" and d.get("role") == "accept"
                    and str(d.get("value")).lower() == "true"):
                return ch
    return None


async def do_mulligan(c, pid, tag):
    st = c.latest
    a = find_action(merged_actions(st), "MulliganDecision")
    if not a:
        return False
    names = hand_lnames(st["state"], pid)
    nlands = sum(1 for n in names if n in (FOREST, ISLAND, MOUNTAIN))
    keep = nlands >= 2
    say(f"[{tag}] mulligan: {nlands} lands -> {'keep' if keep else 'redo'}")
    sub = _copy.deepcopy(a)
    sub["data"]["decision"] = "keep" if keep else "mulligan"
    await submit_as_is(c, sub)
    MULLS[tag] = True
    wire("mulligan", {"who": tag, "decision": sub["data"]["decision"],
                      "n_lands": nlands})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    state = st["state"]
    a = find_action(merged_actions(st), "SelectCards")
    if not a:
        return False
    n = ((wf_of(state).get("data") or {}).get("phase") or {}).get("count", 1)
    h = hand_ids(state, pid)
    picks = h[:n]
    sub = _copy.deepcopy(a)
    sub["data"]["cardIds"] = [int(x) for x in picks]
    say(f"[{tag}] bottoming {n}: {[lname(state, x) for x in picks]}")
    await submit_as_is(c, sub)
    return True


async def discard_tick(c, pid, tag, acts, st, state, protect):
    if wf_of(state).get("type") != "DiscardChoice":
        return False
    if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
        return False
    a = find_action(acts, "DiscardChoice")
    if not a:
        return False
    n = ((wf_of(state).get("data") or {}).get("count")) or 1
    prio = sorted(hand_ids(state, pid),
                  key=lambda o: (0 if lname(state, o) in
                                 (FOREST, ISLAND, MOUNTAIN)
                                 else (1 if lname(state, o) not in protect
                                       else 2)))
    sub = _copy.deepcopy(a)
    sub["data"]["cardIds"] = [int(x) for x in prio[:n]]
    say(f"[{tag}] discarding {[lname(state, x) for x in prio[:n]]}")
    await submit_as_is(c, sub)
    return True


async def discard_handsize_tick(c, pid, tag, st, state, protect):
    wf = wf_of(state)
    if wf.get("type") != "DiscardToHandSize":
        return False
    data = wf.get("data") or {}
    if str(data.get("player")) != str(pid):
        return False
    n = data.get("count") or 1
    vi = get_vi(st)
    if not vi:
        return False
    hand = hand_ids(state, pid)
    prio = sorted(
        hand, key=lambda o: (0 if lname(state, o) in
                             (FOREST, ISLAND, MOUNTAIN)
                             else (1 if lname(state, o) not in protect
                                   else 2)))
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        ref2cid = {}
        for ch in cands:
            r = ref_key(candidate_ref_oid(ch))
            if r is not None:
                ref2cid[r] = ch.get("id")
        picks = [ref2cid[str(x)] for x in prio if str(x) in ref2cid][:n]
        if len(picks) < n:
            picks = [ch.get("id") for ch in cands[:n]]
        SUBMITTED.add(key)
        spec = (rdata.get("spec") or {}).get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": spec, "data": {"choiceIds": picks}}}
        say(f"[{tag}] discarding {n} to hand size")
        wire("interaction_submission",
             {"who": tag, "submission": sub, "kind": "DiscardToHandSize"})
        await c.send_interaction(sub)
        return True
    return False


async def combat_tick(c, pid, tag, acts, st, state):
    wtype = wf_of(state).get("type") or ""
    if wtype == "DeclareAttackers" and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            sub = _copy.deepcopy(da)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
        return True
    if wtype == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            sub = _copy.deepcopy(da)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
        return True
    return False


async def pay_tick(acts, c, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def cast_named(c, acts, state, name, tag):
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        d = a.get("data") or {}
        oid = (d.get("object_id") or d.get("source_id") or a.get("_src_oid"))
        if oid is not None and lname(state, int(oid)) == name:
            say(f"[{tag}] casting {name} (oid={oid})")
            wire("cast", {"who": tag, "name": name, "oid": int(oid)})
            await submit_as_is(c, a)
            return True
    return False


async def play_land_pref(c, acts, state, pref, tag):
    plays = [a for a in acts if a["type"] == "PlayLand"]
    if not plays:
        return False
    for name in pref:
        for a in plays:
            d = a.get("data") or {}
            oid = d.get("object_id") or a.get("_src_oid")
            if oid is not None and lname(state, int(oid)) == name:
                say(f"[{tag}] playing land {name}")
                wire("play_land", {"who": tag, "name": name,
                                   "oid": int(oid)})
                await submit_as_is(c, a)
                return True
    await submit_as_is(c, plays[0])
    return True


async def answer_bolt_target(c, st, state, tag):
    """Answer P1's Bolt TargetSelection: choose P0's Bear."""
    want = ST.get("bear_oid")
    if want is None:
        bears = bf_ids(state, 0, BEAR)
        if not bears:
            return False
        want = bears[0]
        ST["bear_oid"] = want
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        key = (tag, "bolt_target", str(iid))
        if key in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        if not choices:
            continue
        if not ST.get("cands_dumped"):
            ST["cands_dumped"] = True
            wire("target_candidates",
                 {"iid": str(iid)[:12], "n": len(choices),
                  "sample": [{"ref": str(candidate_ref_oid(ch))[:40],
                              "ref_key": ref_key(candidate_ref_oid(ch))}
                             for ch in choices[:16]]})
        pick = None
        for ch in choices:
            if ref_key(candidate_ref_oid(ch)) == str(want):
                pick = ch
                break
        if pick is None:
            wire("target_no_match",
                 {"iid": str(iid)[:12], "want": want,
                  "n_choices": len(choices)})
            continue
        SUBMITTED.add(key)
        ST["bolt_target_oid"] = want
        ST["bolt_target_chosen"] = True
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick["id"]}}}
        else:
            stype = (data.get("spec", {}) or {}).get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick["id"]]}}}
        say(f"[{tag}] Bolt target: Bear oid={want} (choice {pick['id']})")
        wire("target_answer", {"iid": str(iid)[:12],
                               "candidate_oid": want,
                               "choice_id": pick.get("id"), "sub": sub})
        await c.send_interaction(sub)
        return True
    return False


async def ivy_copy_decision_tick(c, st, state, tag):
    """Answer Ivy's 'you may copy' OptionalEffectChoice with accept=true."""
    vi = get_vi(st)
    if not vi:
        return False
    wf = wf_of(state)
    wtype = wf.get("type") or ""
    # Only consider actual decision prompts, not routine priority menus.
    if wtype not in ("OptionalEffectChoice", "OptionalCostChoice",
                     "MayChoice", "Choice"):
        return False
    wf_blob = json.dumps(wf, default=str).lower()
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        key = (tag, "ivy_copy", str(iid))
        if key in SUBMITTED:
            continue
        blob = (json.dumps(opp, default=str) + " " + wtype + " "
                + wf_blob).lower()
        if "copy" not in blob:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        acc = accept_choice(chs)
        if acc is None:
            wire("ivy_copy_no_accept",
                 {"wf": wtype,
                  "opp": json.loads(json.dumps(opp, default=str))})
            say(f"[{tag}] Ivy copy prompt WITHOUT accept choice "
                f"(wf={wtype}); leaving unanswered")
            SUBMITTED.add(key)
            ST["copy_decision_seen"] = True
            return False
        SUBMITTED.add(key)
        ST["copy_decision_seen"] = True
        ST["copy_accepted"] = True
        say(f"[{tag}] Ivy copy decision: ACCEPT (wf={wtype})")
        wire("ivy_copy_accept", {"wf": wtype,
                                "iid": str(iid)[:12]})
        await answer_vi(c, opp, acc, tag)
        return True
    return False


async def copy_retarget_tick(c, st, state, tag):
    """If the engine raises CopyRetarget for the copy, record it and choose
    Ivy (the correct target) so the flow completes; A5 records the prompt."""
    if wf_of(state).get("type") != "CopyRetarget":
        return False
    vi = get_vi(st)
    if not vi:
        return False
    ST["copy_retarget_seen"] = True
    say(f"[{tag}] CopyRetarget prompt observed (unexpected per acceptance "
        f"criteria)")
    wire("copy_retarget_seen",
         {"wf": json.loads(json.dumps(wf_of(state), default=str))})
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        key = (tag, "copy_retarget", str(iid))
        if key in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        wire("copy_retarget_candidates",
             {"iid": str(iid)[:12], "n": len(choices),
              "sample": [{"ref": str(candidate_ref_oid(ch))[:40],
                          "ref_key": ref_key(candidate_ref_oid(ch))}
                         for ch in choices[:16]]})
        ivy = ST.get("ivy_oid")
        pick = None
        if ivy is not None:
            for ch in choices:
                if ref_key(candidate_ref_oid(ch)) == str(ivy):
                    pick = ch
                    break
        if pick is None and choices:
            pick = choices[0]
        if pick is None:
            continue
        SUBMITTED.add(key)
        say(f"[{tag}] CopyRetarget: choosing "
            f"ref={ref_key(candidate_ref_oid(pick))} "
            f"(ivy={ivy})")
        await answer_vi(c, opp, pick, tag)
        return True
    return False

# ------------------------------------------------------- per-seat ticks ---
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if find_action(acts, "MulliganDecision") and "P0" not in MULLS:
            await do_mulligan(c, 0, "P0")
            return
        if find_action(acts, "SelectCards"):
            await do_bottom(c, 0, "P0")
            return
    if await pay_tick(acts, c, "P0"):
        return
    if await combat_tick(c, 0, "P0", acts, st, state):
        return
    if await discard_handsize_tick(c, 0, "P0", st, state, (IVY, BEAR)):
        return
    # Ivy's copy decision owns copy-looking prompts first.
    if await ivy_copy_decision_tick(c, st, state, "P0"):
        return
    if await copy_retarget_tick(c, st, state, "P0"):
        return
    if not my_priority(state, 0):
        return
    if await discard_tick(c, 0, "P0", acts, st, state, (IVY, BEAR)):
        return
    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 0 \
            and not ST["bolt_cast"]:
        # Land drop first: Forest, then Island.
        await play_land_pref(c, acts, state, [FOREST, ISLAND], "P0")
        ivy_bf = bf_id(state, 0, IVY)
        if ivy_bf is None:
            if await cast_named(c, acts, state, IVY, "P0"):
                return
        elif not bf_ids(state, 0, BEAR):
            if await cast_named(c, acts, state, BEAR, "P0"):
                return
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return


async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if find_action(acts, "MulliganDecision") and "P1" not in MULLS:
            await do_mulligan(c, 1, "P1")
            return
        if find_action(acts, "SelectCards"):
            await do_bottom(c, 1, "P1")
            return
    if await pay_tick(acts, c, "P1"):
        return
    if await combat_tick(c, 1, "P1", acts, st, state):
        return
    if await discard_handsize_tick(c, 1, "P1", st, state, (BOLT,)):
        return
    # Bolt target selection owns the TargetSelection prompt.
    if wtype == "TargetSelection" and ST["bolt_cast"] \
            and not ST["bolt_target_chosen"]:
        if await answer_bolt_target(c, st, state, "P1"):
            return
    if await copy_retarget_tick(c, st, state, "P1"):
        return
    if not my_priority(state, 1):
        return
    if await discard_tick(c, 1, "P1", acts, st, state, (BOLT,)):
        return
    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 1:
        await play_land_pref(c, acts, state, [MOUNTAIN], "P1")
        # Cast the test Bolt only once the pre-export is done and the
        # fixture is ready; gate main-phase casting on in-flight casts but
        # always fall through to PassPriority (cf. #6862).
        if (ST["pre_exported"] and not ST["bolt_cast"]
                and not ST["bolt_in_flight"]
                and bf_id(state, 0, IVY) is not None
                and bf_ids(state, 0, BEAR)
                and BOLT in hand_lnames(state, 1)
                and untapped_lands(state, 1, MOUNTAIN)):
            if await cast_named(c, acts, state, BOLT, "P1"):
                ST["bolt_cast"] = True
                ST["bolt_in_flight"] = True
                ST["bolt_cast_turn"] = state.get("turn_number")
                ST["stage"] = "BOLT_CAST"
                say("[P1] test Bolt cast; awaiting target selection")
                return
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return


# --------------------------------------------------------------- evaluate -
def load_st(fn):
    p = f"{EVDIR}/{fn}"
    if os.path.exists(p):
        raw = open(p).read()
        return json.loads(raw)["state"]
    return None


def evaluate():
    a = {}
    pre_st = load_st("pre.json")
    mid_st = load_st("mid.json")
    post_st = load_st("post.json")

    ivy_oid = ST.get("ivy_oid")
    bear_oid = ST.get("bolt_target_oid") or ST.get("bear_oid")

    # A1: fixture ready and Bolt cast at the Bear.
    a["A1_setup_ok"] = ("passed" if (
        ST["bolt_cast"] and ST["bolt_target_chosen"]
        and (ST.get("bolt_target_oid") is not None)
        and ivy_oid is not None) else "failed")

    # A2: Ivy's optional-copy decision was raised for P0.
    a["A2_trigger_offered"] = ("passed" if ST["copy_decision_seen"]
                               else "failed")

    # A3: a second Bolt spell (controller 0 = the copy) observed on stack.
    a["A3_copy_created"] = ("passed" if ST["saw_two_bolts"] else "failed")

    # A4: the copy's targets include Ivy.
    copy_targets_ivy = False
    ce = ST.get("copy_entry")
    if ce is not None and ivy_oid is not None:
        tgt_ints = ints_in(ce.get("targets"))
        copy_targets_ivy = ivy_oid in tgt_ints
        say(f"A4: copy entry targets ints include ivy({ivy_oid})="
            f"{copy_targets_ivy}; bear({bear_oid})={bear_oid in tgt_ints}")
    a["A4_copy_targets_ivy"] = ("passed" if copy_targets_ivy else "failed")

    # A5: no CopyRetarget prompt for the copy.
    a["A5_no_retarget_prompt"] = ("passed" if not ST["copy_retarget_seen"]
                                 else "failed")

    # A6: outcome -- Ivy alive on BF, targeted Bear dead.
    if post_st is not None and bear_oid is not None and ivy_oid is not None:
        ivy_alive = get_obj(post_st, ivy_oid).get("zone") == "Battlefield"
        bear_zone = get_obj(post_st, bear_oid).get("zone")
        bear_dead = bear_zone != "Battlefield"
        a["A6_outcome"] = ("passed" if (ivy_alive and bear_dead) else "failed")
        say(f"A6: post ivy_alive={ivy_alive} bear_zone={bear_zone}")
    else:
        a["A6_outcome"] = "not-run"

    # A7: cleanup -- stack empty and post exported.
    if post_st is not None:
        a["A7_cleanup"] = ("passed" if not (post_st.get("stack") or [])
                           else "failed")
    else:
        a["A7_cleanup"] = "not-run"

    if a["A1_setup_ok"] == "failed" or a["A2_trigger_offered"] == "failed":
        verdict = "blocked"
    elif (a["A3_copy_created"] == "passed"
            and (a["A4_copy_targets_ivy"] == "failed"
                 or a["A5_no_retarget_prompt"] == "failed")):
        verdict = "reproduced"
    elif all(v == "passed" for v in a.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return a, verdict


# ----------------------------------------------------------------- render -
def render_summary(run):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 980
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7168 - Ivy, Gleeful Spellthief: "
           "copy does not target Ivy", fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           f"server v{SERVER_IDENTITY['validated_version']} "
           f"({SERVER_IDENTITY['build_commit']}) protocol 70 - 2026-09-15",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else (120, 220, 120))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire observations):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "Ivy+Bear on P0 BF; P1 Bolt cast at the Bear",
        "A2_trigger_offered": "Ivy optional-copy decision raised for P0",
        "A3_copy_created": "second Bolt spell (controller 0 copy) on stack",
        "A4_copy_targets_ivy": "copy's targets include Ivy  [expect FAIL]",
        "A5_no_retarget_prompt": "no CopyRetarget prompt for the copy",
        "A6_outcome": "post: Ivy alive, targeted Bear dead",
        "A7_cleanup": "stack empty, post.json exported",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12
    d.text((24, y), "Parse evidence (v0.83.0 card-data.json):",
           fill=(200, 210, 225))
    y += 24
    for line in [
        "CopySpell{target: TriggeringSource, retarget: KeepOriginalTargets}",
        "'The copy targets ~' -> Unimplemented(unrecognized_clause_head)",
        "=> copy keeps the original target; retarget-to-Ivy unsupported",
    ]:
        d.text((36, y), line[:112], fill=(170, 180, 195))
        y += 22
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    try:
        subprocess.run(["cp", os.path.abspath(__file__),
                        f"{EVDIR}/scenario_7168.py"], check=False)
        say("copied scenario_7168.py into EVDIR")
    except Exception as e:
        say(f"scenario copy failed: {e}")
    files = ["pre.json", "mid.json", "post.json", "run.json",
             "scenario_7168.py", "wire_log.jsonl", "scenario_run.log",
             "summary.png", "parse_ivy.json"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    # NOTE: manifest must be the last file touched; do NOT say()/wire()
    # after this point, or the log hashes will invalidate. The caller
    # is responsible for closing logs before invoking this.


async def finish(a, verdict, notes):
    # parse evidence: the Ivy trigger AST from the pinned card-data
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.83.0/data/"
                            f"card-data.json"))
        ivy = cd.get("ivy, gleeful spellthief", {})
        with open(f"{EVDIR}/parse_ivy.json", "w") as f:
            json.dump({"name": ivy.get("name"),
                       "oracle_text": ivy.get("oracle_text"),
                       "triggers": ivy.get("triggers")}, f, indent=2)
    except Exception as e:
        notes.append(f"parse_ivy.json failed: {e}")
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "verdict": verdict,
        "assertions": a,
        "server_identity": SERVER_IDENTITY,
        "fixture": {
            "ivy_oid": ST.get("ivy_oid"),
            "bear_oid": ST.get("bear_oid"),
            "bolt_target_oid": ST.get("bolt_target_oid"),
            "copy_decision_seen": ST["copy_decision_seen"],
            "copy_accepted": ST["copy_accepted"],
            "copy_retarget_seen": ST["copy_retarget_seen"],
            "saw_two_bolts": ST["saw_two_bolts"],
            "copy_entry": ST.get("copy_entry"),
            "orig_entry": ST.get("orig_entry"),
        },
        "notes": notes,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2)
    say(f"wrote run.json verdict={verdict}")
    render_summary(run)
    wire("run_complete", {"verdict": verdict, "assertions": a})
    # NOTE: write_manifest() is called by main() after logs are closed.


# ------------------------------------------------------------------- main -
async def main():
    reset()
    notes = []
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    for c in (p0, p1):
        await c.connect()
    say("server identity pinned: v0.83.0 (b7a59d4) protocol 70, mode Full")

    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    async def export_named(name):
        data = await p0.export_state()
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(data)
        say(f"exported {name}.json")
        wire(f"{name}_exported", {})
        return True

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < TIMEOUT:
        await asyncio.sleep(0.15)
        if ST["stop"]:
            break
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(tag))
            stale = time.time() - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = rev
            last_tick_at[tag] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"], c)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})

        # ---- stage transitions from P1's view (P1's hand is only visible
        #      to P1; P0 sees 'hidden card') ----
        p1state = p1.latest["state"] if p1.latest else None
        if p0.latest:
            state = p0.latest["state"]
            if ST["ivy_oid"] is None:
                iv = bf_id(state, 0, IVY)
                if iv is not None:
                    ST["ivy_oid"] = iv
                    say(f"Ivy on BF oid={iv}")
                    wire("ivy_bf", {"oid": iv})
            if ST["bear_oid"] is None:
                b = bf_id(state, 0, BEAR)
                if b is not None:
                    ST["bear_oid"] = b
                    say(f"Bear on BF oid={b}")
                    wire("bear_bf", {"oid": b})

            # pre.json: fixture ready, P1 to act in main phase.
            # NOTE: P1-hand visibility requires P1's own view.
            if (p1state is not None and not ST["pre_exported"]
                    and ST["ivy_oid"] is not None
                    and ST["bear_oid"] is not None
                    and BOLT in hand_lnames(p1state, 1)
                    and untapped_lands(p1state, 1, MOUNTAIN)
                    and p1state.get("phase") in ("PreCombatMain",
                                                 "PostCombatMain")
                    and p1state.get("active_player") == 1
                    and p1state.get("priority_player") == 1
                    and wf_of(p1state).get("type") == "Priority"):
                await export_named("pre")
                ST["pre_exported"] = True
                notes.append(
                    f"pre: turn {p1state.get('turn_number')} "
                    f"ivy_oid={ST['ivy_oid']} bear_oid={ST['bear_oid']} "
                    f"P1 hand has Bolt, "
                    f"{len(untapped_lands(p1state, 1, MOUNTAIN))} "
                    f"untapped Mountain(s)")

            # Track the Ivy trigger on the stack (corroborates A2).
            if ST["bolt_cast"] and not ST["trigger_seen"]:
                for e in trigger_entries(state):
                    blob = json.dumps(e, default=str).lower()
                    if "ivy" in blob or "copy" in blob:
                        ST["trigger_seen"] = True
                        say(f"Ivy trigger on stack: "
                            f"{str(e.get('name'))[:60]}")
                        wire("ivy_trigger_stack",
                             json.loads(json.dumps(e, default=str)))
                        break

            # Copy window: detect the copy by NEW stack-entry ID after the
            # copy was accepted (robust to the copy not carrying the
            # original's name).
            if ST["bolt_cast"]:
                cur_ids = {e.get("id") for e in stack_entries(state)
                           if e.get("id") is not None}
                if not ST["copy_accepted"]:
                    # Baseline: remember everything on the stack up to and
                    # including the acceptance, so only post-acceptance
                    # entries count as new.
                    ST["seen_stack_ids"] |= cur_ids
                elif not ST["saw_two_bolts"]:
                    new_ids = cur_ids - ST["seen_stack_ids"]
                    ST["seen_stack_ids"] |= cur_ids
                    for e in stack_entries(state):
                        if e.get("id") not in new_ids:
                            continue
                        kind = e.get("kind") or {}
                        ktype = (kind.get("type")
                                 if isinstance(kind, dict) else kind)
                        if ktype in ("Spell", "CastSpell", "SpellCopy"):
                            ST["saw_two_bolts"] = True
                            ST["copy_stack_id"] = e.get("id")
                            snap = {"id": e.get("id"),
                                    "name": e.get("name"),
                                    "controller": e.get("controller"),
                                    "targets": e.get("targets"),
                                    "kind": e.get("kind")}
                            ST["copy_entry"] = snap
                            say(f"COPY DETECTED: new stack entry "
                                f"id={e.get('id')} kind={ktype} "
                                f"targets={json.dumps(e.get('targets'), default=str)[:200]}")
                            wire("copy_detected",
                                 {"entry": json.loads(json.dumps(
                                     snap, default=str))})
                            break
            # Fallback: two name-matched Bolt spells on the stack.
            bolts = bolt_spells(state)
            if len(bolts) >= 2 and not ST["saw_two_bolts"]:
                ST["saw_two_bolts"] = True
                for e in bolts:
                    ctrl = e.get("controller")
                    snap = {"id": e.get("id"), "name": e.get("name"),
                            "controller": ctrl, "targets": e.get("targets"),
                            "kind": e.get("kind")}
                    if ctrl == 0:
                        ST["copy_entry"] = snap
                    elif ctrl == 1:
                        ST["orig_entry"] = snap
                say(f"COPY WINDOW: {len(bolts)} bolt spells on stack; "
                    f"copy_entry={json.dumps(ST['copy_entry'], default=str)[:200]}")
                wire("copy_window",
                     {"n": len(bolts), "copy": ST["copy_entry"],
                      "orig": ST["orig_entry"]})
            if ST["saw_two_bolts"] and not ST["mid_exported"]:
                await export_named("mid")
                ST["mid_exported"] = True
                ST["stage"] = "COPY_WINDOW"
                notes.append(
                    f"mid: copy targets raw={json.dumps((ST.get('copy_entry') or {}).get('targets'), default=str)[:160]}")

            # The Bolt is no longer in flight once its target was chosen
            # (it sits on the stack awaiting priority).
            if ST["bolt_cast"] and ST["bolt_in_flight"] \
                    and ST["bolt_target_chosen"]:
                ST["bolt_in_flight"] = False

            # Resolution: no Bolt spells on the stack after the copy window.
            if ST["saw_two_bolts"] and not bolts:
                ST["quiescent_ticks"] += 1
            else:
                ST["quiescent_ticks"] = 0
            if (ST["saw_two_bolts"] and ST["quiescent_ticks"] >= 3
                    and not ST["post_exported"]):
                await export_named("post")
                ST["post_exported"] = True
                ST["stage"] = "RESOLVED"
                post = load_st("post.json")
                ivy_alive = get_obj(post, ST["ivy_oid"]).get("zone") == \
                    "Battlefield"
                bear_zone = get_obj(
                    post, ST["bolt_target_oid"]).get("zone")
                notes.append(
                    f"post: turn {post.get('turn_number')} "
                    f"ivy_alive={ivy_alive} "
                    f"targeted_bear_zone={bear_zone} "
                    f"life={life_of(post, 0)}/{life_of(post, 1)}")
                say(f"RESOLVED: ivy_alive={ivy_alive} "
                    f"bear_zone={bear_zone}")
                ST["stop"] = True
                break

            # Fallback: copy accepted but window missed; resolve via outcome.
            if (ST["copy_accepted"] and not ST["saw_two_bolts"]
                    and not bolts and ST["bolt_target_chosen"]
                    and not ST["post_exported"]):
                ST["quiescent_ticks"] += 1
            if (ST["quiescent_ticks"] >= 10 and not ST["post_exported"]
                    and ST["copy_accepted"]):
                notes.append("fallback post export: copy window never "
                             "sampled; outcome-only")
                await export_named("post")
                ST["post_exported"] = True
                ST["stage"] = "RESOLVED"
                ST["stop"] = True
                break

        if p0.latest and (p0.latest["state"].get("turn_number") or 0) > TURN_CAP \
                and ST["stage"] == "SETUP":
            notes.append(f"TURN_CAP {TURN_CAP} hit in SETUP; finishing")
            say("TURN_CAP: finishing")
            break
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} stage={ST['stage']} "
                f"ivy={ST['ivy_oid']} bear={ST['bear_oid']} "
                f"bolt_cast={ST['bolt_cast']} copy_acc={ST['copy_accepted']} "
                f"two_bolts={ST['saw_two_bolts']} "
                f"P0hand={hand_lnames(s, 0)[:5]} P1hand={hand_lnames(s, 1)[:5]}")
        if time.time() - t0 > TIMEOUT - 5:
            notes.append("TIMEOUT hit; finishing")
            break

    a, verdict = evaluate()
    say(f"FINAL verdict={verdict} assertions={a}")
    notes.append(f"final stage={ST['stage']} "
                 f"copy_decision_seen={ST['copy_decision_seen']} "
                 f"copy_retarget_seen={ST['copy_retarget_seen']}")
    if not ST["post_exported"]:
        try:
            await export_named("post")
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")
    # Recompute assertions that depend on post.json possibly exported late.
    a, verdict = evaluate()
    await finish(a, verdict, notes)
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    RUNLOG.close()
    WIRE.close()
    # Manifest must hash the final closed logs; write it last.
    write_manifest()
    # Stdout only (logs are closed); do not use say() here.
    print(f"DONE issue={ISSUE} run={RUN_ID} verdict={verdict}", flush=True)
    return verdict


if __name__ == "__main__":
    print(asyncio.run(main()))
