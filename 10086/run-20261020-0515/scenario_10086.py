#!/usr/bin/env python3
"""Issue #10086: "Later subtype grants can survive loss of Creature on a
devotion god".

v0.114.0 (protocol 153, build ac67cbb) first validation. Two human driver
seats, native engine.

Printed case (from the report):
- Heliod, Sun-Crowned: "As long as your devotion to white is less than five,
  Heliod isn't a creature." Intrinsically Legendary Enchantment Creature - God.
- Olivia Voldaren: "{1}{R}: Olivia Voldaren deals 1 damage to another target
  creature. That creature becomes a Vampire in addition to its other types.
  Put a +1/+1 counter on Olivia Voldaren."
- Amoeboid Changeling: "{T}: Target creature gains all creature types until
  end of turn."

Plan:
  P0 ramps white permanents until devotion >= 5 with Heliod on the
  battlefield (Heliod is then a creature).
  Leg 1 (REPORTED): Olivia's ping resolves targeting Heliod -> Heliod becomes
    a Vampire. Then Olivia pings a 1-toughness white creature (Elite Vanguard)
    -> it dies -> devotion < 5. Per CR 205.3d Heliod must be solely an
    Enchantment afterward; the Vampire grant must not leave creature subtypes.
  Leg 2 (sibling): devotion restored to 5 with another Vanguard; Amoeboid's
    "gains all creature types" resolves targeting Heliod; Olivia pings the
    restored Vanguard in the SAME turn -> devotion < 5. Same expectation.

BEHAVIORAL CONTRACT (written before observing results)
  A1_setup_ok        pre_olivia.json: Heliod on battlefield, "Creature" in
                     core_types, P0 devotion >= 5.
  A2_olivia_applied  pre_olivia.json: "Vampire" in Heliod subtypes.
  A3_devotion_dropped post_olivia.json: P0 devotion <= 4.
  A4_no_stray_olivia REPORTED: post_olivia.json: no "Creature" in core_types
                     and no "Vampire" in subtypes. failed == bug reproduced.
  A5_amoeboid_applied pre_amoeboid.json: "Creature" in core_types,
                     devotion >= 5, all-types grant visible.
  A6_no_stray_amoeboid REPORTED sibling: post_amoeboid.json: no "Creature" in
                     core_types and no creature subtypes. failed == bug.

Verdict: reproduced iff A1, A2, A3 pass and (A4 fails or A6 fails).
         not-reproduced iff A1, A2, A3, A5 pass and A4, A6 pass.
         blocked iff A1/A2 fail (setup never reached) or A3 not-run.

Data note: the pinned v0.114.0 card-data.json lists Heliod, Sun-Crowned at
{2}{W} (one White shard), not the printed {2}{W}{W}. Devotion is computed
from the engine's data; the driver ramps white permanents until devotion
>= 5 rather than assuming a pip count.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-10086-v01140-20261020-0441")
ISSUE = 10086
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

URL = "ws://127.0.0.1:9374/ws"

WIRE = None
RUNLOG = None

HELIOD = "heliod, sun-crowned"
OLIVIA = "olivia voldaren"
AMOEBOID = "amoeboid changeling"
VANGUARD = "elite vanguard"
PLAINS = "plains"
MOUNTAIN = "mountain"
SWAMP = "swamp"
ISLAND = "island"

P0_DECK = deck((HELIOD, 4), (VANGUARD, 16), (PLAINS, 40))
P1_DECK = deck((OLIVIA, 4), (AMOEBOID, 4), (MOUNTAIN, 22), (SWAMP, 14),
               (ISLAND, 16))

SERVER_IDENTITY = {
    "server_version": "v0.114.0",
    "build_commit": "ac67cbb",
    "protocol_version": 153,
    "mode": "Full",
    "binary_sha256": "025c26c78762d8fc0bed47a7774749465c3e40751df73310cd7dee6ed5d506ef",
    "card_data_sha256": "7fe886cc031d9bd6532317572f84f65da66941ebdf6a43d337b625fbc369548e",
    "draft_pools_sha256": "4a0b44a9af0881f924b4120a43fe04947dd673aa867e796917b696d7ea26ba59",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.114.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828) at pin time 2026-10-19; "
                      "binary digest matches the GitHub asset digest; "
                      "card-data + draft-pools digests match the signed "
                      "manifest; digests re-verified on disk this run",
    "observed_at": "2026-10-20",
    "source": "ServerHello asserted by handshake this run; digests "
              "recomputed against on-disk pinned release artifacts",
}

TIMEOUT = 1500
TURN_CAP = 45

ST = {}
MULLS = set()
MULL_COUNTS = {}
SUBMITTED_OPPS = set()
LAND_PLAYED = {}


def reset():
    ST.update({
        "stage": "setup",
        "stop": False,
        "notes": [],
        "mana_needs": {},
        "pending": None,   # activation in flight
        "vanguard_mark": None,
        "leg1a_t0": 0,
        "last_submit": 0.0,
        "rejections": [],
    })


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if RUNLOG is not None:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def stack_entries(state):
    return state.get("stack") or []


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def untapped_lands(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o))
            and not get_obj(state, o).get("tapped")]


def core_types_of(state, oid):
    return list(((get_obj(state, oid).get("card_types") or {})
                 .get("core_types")) or [])


def subtypes_of(state, oid):
    return list(((get_obj(state, oid).get("card_types") or {})
                  .get("subtypes")) or [])


def white_pips(o):
    mc = o.get("mana_cost") or {}
    return sum(1 for s in (mc.get("shards") or []) if s == "White")


def devotion(state, pid, color="White"):
    return sum(white_pips(get_obj(state, o)) for o in bf_oids(state, pid))


def heliod_oid(state):
    ids = bf_by_name(state, 0, HELIOD)
    return ids[0] if ids else None


def vanguard_oids(state):
    return bf_by_name(state, 0, VANGUARD)


def olivia_oid(state):
    ids = bf_by_name(state, 1, OLIVIA)
    return ids[0] if ids else None


def amoeboid_oid(state):
    ids = bf_by_name(state, 1, AMOEBOID)
    return ids[0] if ids else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg = {"type": a["type"], "data": copy.deepcopy(a["data"])}
    wire("action_submit", {"who": c.name, "action": msg})
    ST["last_submit"] = time.time()
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    ST["last_submit"] = time.time()
    await c.send_interaction(sub)


def choice_text(ch):
    bits = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        for k in ("label", "text", "description", "name"):
            if d.get(k):
                bits.append(str(d[k]))
                break
    return " | ".join(bits) or json.dumps(ch, default=str)[:120]


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
    say(f"[{tag}] vi answer iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_answer", {"who": tag, "iid": iid, "cid": cid,
                                "text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_land_named(c, state, pid, acts, tag, lname):
    turn = state.get("turn_number")
    if LAND_PLAYED.get(tag) == turn:
        return False
    for a in acts:
        if a["type"] == "PlayLand" \
                and obj_lname(state, a.get("_src_oid")) == lname:
            LAND_PLAYED[tag] = turn
            say(f"[{tag}] playing land {lname}")
            wire("play_land", {"who": tag, "lname": lname})
            await submit_as_is(c, a)
            return True
    return False


async def do_mulligan(c, acts, st, pid, tag, keep_fn):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    hn = hand_lnames(st["state"], pid)
    mulls = MULL_COUNTS.get(tag, 0)
    decision = "keep" if (keep_fn(hn) or mulls >= 4) else "mulligan"
    if decision == "mulligan":
        MULL_COUNTS[tag] = mulls + 1
    say(f"[{tag}] {decision} {len(hn)}: {hn} (mulls={mulls})")
    wire("mulligan", {"who": tag, "decision": decision, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if decision == "keep"
                                             else "Mulligan"}}})
    return True


def _cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def _cand_lname(state, ch):
    r = _cand_ref(ch)
    try:
        return obj_lname(state, int(r))
    except (TypeError, ValueError):
        return "?"


async def do_bottom(c, acts, st, pid, tag, keep_names):
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in (rdata.get("candidates") or [])
                 if str(_cand_ref(ch)) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", iid)
        if key in SUBMITTED_OPPS:
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {})
               or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            nm = _cand_lname(state, ch)
            if nm in keep_names:
                return (2, nm)
            return (0, nm)

        picks = sorted(cands, key=bkey)[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoming {[_cand_lname(state, x) for x in picks]}")
        wire("bottom", {"who": tag, "iid": iid,
                        "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


async def do_discard(c, acts, st, pid, tag, keep_names):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)

    def rank(ch):
        nm = _cand_lname(state, ch)
        if nm in keep_names:
            return (5, nm)
        return (1, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in (rdata.get("candidates") or [])
                 if str(_cand_ref(ch)) in handset]
        if not cands:
            continue
        picks = sorted(cands, key=rank)[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding {[_cand_lname(state, x) for x in picks]}")
        wire("discard", {"who": tag, "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


async def export_named(c, tag):
    s = await c.export_state()
    with open(f"{EVDIR}/{tag}.json", "w") as f:
        f.write(s)
    say(f"exported {tag}.json")
    wire("export", {"tag": tag})


def load_env(tag):
    p = f"{EVDIR}/{tag}.json"
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    st = d.get("state") if isinstance(d.get("state"), dict) else d
    return st


async def drain(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            return
        if t in ("Error", "ActionRejected"):
            ST.setdefault("rejections", []).append(
                {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
            wire("rejection", {"who": c.name, "type": t, "data": data})


# ------------------------------------------------------- ability activation

def describe_action(a):
    d = a.get("data") or {}
    bits = []
    for k in ("description", "ability_description", "ability_index",
              "source_id"):
        if d.get(k) is not None:
            bits.append(f"{k}={d[k]}")
    return " ".join(bits)


def find_activate(st, source_oid, want_text=None):
    """Find an ActivateAbility for source_oid, preferring one whose
    description contains want_text (to disambiguate multi-ability cards)."""
    cands = []
    for a in merged_actions(st):
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data") or {}
        if str(d.get("source_id")) == str(source_oid) \
                or str(a.get("_src_oid")) == str(source_oid):
            cands.append(("submit", a, describe_action(a)))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "activateAbility" not in codes:
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)]
            if str(source_oid) in refs:
                cands.append(("interaction", (opp, ch), choice_text(ch)))
    if not cands:
        return None
    if want_text:
        for c in cands:
            if want_text.lower() in c[2].lower():
                return c
        say(f"[warn] no ActivateAbility matching {want_text!r}; "
            f"have: {[c[2][:80] for c in cands]}")
        wire("activate_no_match", {"want": want_text,
                                   "have": [c[2][:120] for c in cands]})
    return cands[0]


def target_opportunity(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_target_candidate(state, opp, want_oid):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        if (ch.get("status", {}) or {}).get("type", "available") != "available":
            continue
        r = _cand_ref(ch)
        try:
            if r is not None and str(int(r)) == str(want_oid):
                return ch
        except (TypeError, ValueError):
            continue
    return None


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting target choice id={cid} "
        f"({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "iid": iid, "cid": cid,
                               "text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid


def cast_action_for(acts, state, pid, name):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and obj_lname(state, v) == name \
                        and str(get_obj(state, v).get("controller")) == str(pid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


def ability_on_stack(state, source_oid):
    for e in stack_entries(state):
        blob = json.dumps(e, default=str)
        if str(source_oid) in blob:
            return True
    return False


async def tick_seat(c, tag, pid):
    """One driver tick for a seat. Returns True if an action was taken."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    keep = {HELIOD, VANGUARD, PLAINS} if pid == 0 else \
        {OLIVIA, AMOEBOID, MOUNTAIN, SWAMP, ISLAND}
    if await do_mulligan(c, acts, st, pid, tag,
                         lambda hn: keep_mulligan(hn, pid)):
        return True
    if await do_bottom(c, acts, st, pid, tag, keep):
        return True
    if await do_discard(c, acts, st, pid, tag, keep):
        return True
    if "DeclareAttackers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareAttackers"))
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareBlockers"))
        d.setdefault("data", {}).update({"assignments": []})
        await submit_as_is(c, d)
        return True
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    stage = ST["stage"]
    # Do not answer real decisions or pass while an activation is in flight
    # for this seat; the phase coroutine owns those ticks.
    if ST["pending"] is not None and ST["pending"].get("tag") == tag:
        return False
    if real_decision_pending(st):
        return False
    if my_main(state, pid):
        if pid == 0:
            if await p0_main(c, state, acts, tag, stage):
                return True
        else:
            if await p1_main(c, state, acts, tag, stage):
                return True
    if not my_priority(acts):
        return False
    return await pass_priority(c, st, acts)


def keep_mulligan(hn, pid):
    lands = sum(1 for n in hn if n in (PLAINS, MOUNTAIN, SWAMP, ISLAND))
    if pid == 0:
        return HELIOD in hn and lands >= 2
    return OLIVIA in hn and lands >= 2


async def p0_main(c, state, acts, tag, stage):
    # land
    if await play_land_named(c, state, 0, acts, tag, PLAINS):
        return True
    if stage not in ("setup", "restore"):
        return False
    hand = hand_lnames(state, 0)
    untapped = len(untapped_lands(state, 0))
    if stage == "setup":
        # cast Heliod when affordable
        if heliod_oid(state) is None and HELIOD in hand and untapped >= 3:
            a = cast_action_for(acts, state, 0, HELIOD)
            if a is not None:
                say(f"[{tag}] casting Heliod")
                wire("cast", {"who": tag, "what": HELIOD})
                await submit_as_is(c, a)
                return True
        # ramp Vanguards until devotion >= 5
        if devotion(state, 0) < 5 and VANGUARD in hand and untapped >= 1 \
                and len(vanguard_oids(state)) < 6:
            a = cast_action_for(acts, state, 0, VANGUARD)
            if a is not None:
                say(f"[{tag}] casting Vanguard "
                    f"(devotion={devotion(state, 0)})")
                wire("cast", {"who": tag, "what": VANGUARD})
                await submit_as_is(c, a)
                return True
    elif stage == "restore":
        if devotion(state, 0) < 5 and VANGUARD in hand and untapped >= 1:
            a = cast_action_for(acts, state, 0, VANGUARD)
            if a is not None:
                say(f"[{tag}] casting restore Vanguard")
                wire("cast", {"who": tag, "what": VANGUARD})
                await submit_as_is(c, a)
                return True
    return False


async def p1_main(c, state, acts, tag, stage):
    # land preference: B/R sources for Olivia, then U for Amoeboid
    olivia = olivia_oid(state)
    amoeboid = amoeboid_oid(state)
    if olivia is None:
        prefs = [SWAMP, MOUNTAIN, ISLAND]
    elif amoeboid is None:
        prefs = [ISLAND, MOUNTAIN, SWAMP]
    else:
        prefs = [MOUNTAIN, ISLAND, SWAMP]
    for p in prefs:
        if await play_land_named(c, state, 1, acts, tag, p):
            return True
    if stage not in ("setup", "restore"):
        return False
    untapped = untapped_lands(state, 1)
    names = [obj_lname(state, o) for o in untapped]
    if olivia is None and OLIVIA in hand_lnames(state, 1) \
            and len(untapped) >= 4 and SWAMP in names and MOUNTAIN in names:
        a = cast_action_for(acts, state, 1, OLIVIA)
        if a is not None:
            say(f"[{tag}] casting Olivia")
            wire("cast", {"who": tag, "what": OLIVIA})
            await submit_as_is(c, a)
            return True
    if amoeboid is None and AMOEBOID in hand_lnames(state, 1) \
            and len(untapped) >= 2 and ISLAND in names:
        a = cast_action_for(acts, state, 1, AMOEBOID)
        if a is not None:
            say(f"[{tag}] casting Amoeboid")
            wire("cast", {"who": tag, "what": AMOEBOID})
            await submit_as_is(c, a)
            return True
    return False


# ------------------------------------------------------- activation runners

async def p1_routine(p1, st, acts, tag):
    """Answer P1's routine decisions (cleanup discard, combat declarations)
    while a phase runner owns P1's priority. Returns True if handled."""
    state = st["state"]
    atypes = set(a.get("type") for a in acts)
    if await do_discard(p1, acts, st, 1, tag,
                        {OLIVIA, AMOEBOID, MOUNTAIN, SWAMP, ISLAND}):
        return True
    if "DeclareAttackers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareAttackers"))
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(p1, d)
        return True
    if "DeclareBlockers" in atypes:
        d = copy.deepcopy(find_action(acts, "DeclareBlockers"))
        d.setdefault("data", {}).update({"assignments": []})
        await submit_as_is(p1, d)
        return True
    return False


def diag_state(tag, st):
    state = st["state"]
    wf = state.get("waiting_for") or {}
    say(f"[{tag}] diag: turn={state.get('turn_number')} "
        f"phase={state.get('phase')} active={state.get('active_player')} "
        f"waiting_for={str(wf.get('type'))[:40]} vi_ops={len(vi_ops(st))} "
        f"stack={len(stack_entries(state))}")
    wire(f"{tag}_diag", {"turn": state.get("turn_number"),
                         "phase": state.get("phase"),
                         "active": state.get("active_player"),
                         "waiting_for": str(wf.get("type"))[:60],
                         "vi_ops": len(vi_ops(st))})


async def run_olivia_ping(p0, p1, target_oid, timeout=240):
    """Activate Olivia's {1}{R} ping targeting target_oid; drive payment and
    target selection; return True once the ability has resolved."""
    tag = "P1"
    ST["pending"] = {"tag": tag, "kind": "olivia", "target": str(target_oid),
                     "submitted": False, "targeted": False, "stack_seen": False,
                     "t0": time.time()}
    try:
        t0 = time.time()
        diag = 0
        while time.time() - t0 < timeout:
            await asyncio.sleep(0.25)
            await tick_seat(p0, "P0", 0)
            p = ST["pending"]
            st = p1.latest
            if not st:
                continue
            state = st["state"]
            acts = merged_actions(st)
            if await p1_routine(p1, st, acts, tag):
                continue
            oid = olivia_oid(state)
            if oid is None:
                say("[P1] Olivia left the battlefield; aborting ping")
                wire("olivia_gone", {})
                return False
            if not p["submitted"]:
                if not my_priority(acts):
                    if diag % 40 == 0:
                        diag_state("P1", st)
                    diag += 1
                    continue
                if not my_main(state, 1):
                    # not our window: keep the game moving toward P1's turn
                    if not real_decision_pending(st):
                        await pass_priority(p1, st, acts)
                    elif diag % 8 == 0:
                        say(f"[P1] waiting for main phase "
                            f"(phase={state.get('phase')} turn={state.get('turn_number')}) "
                            f"decision pending; holding")
                    diag += 1
                    continue
                if get_obj(state, oid).get("tapped"):
                    continue
                r = find_activate(st, oid, "deals 1 damage")
                if r is None:
                    if diag % 8 == 0:
                        say(f"[P1] ActivateAbility not offered; "
                            f"acts={[a['type'] for a in acts][:8]}")
                    diag += 1
                    continue
                kind, sub, what = r
                say(f"[P1] activating Olivia ping -> "
                    f"{obj_lname(state, p['target'])} ({p['target']})")
                wire("olivia_activate", {"via": what[:120],
                                        "target": p["target"]})
                ST["mana_needs"][tag] = {"R": 1, "generic": 1}
                if kind == "interaction":
                    opp, ch = sub
                    await answer_vi(p1, opp, ch, tag)
                else:
                    await submit_as_is(p1, sub)
                p["submitted"] = True
                continue
            if await pay_tick(p1, acts):
                continue
            needs = ST["mana_needs"].get(tag, {})
            if sum(needs.values()) > 0:
                if await pay_mana_vi(p1, st, tag, needs):
                    continue
            if not p["targeted"]:
                opp, rtype, stype = target_opportunity(st)
                if opp is not None:
                    ch = pick_target_candidate(state, opp, p["target"])
                    if ch is None:
                        say(f"[P1] target opp without matching candidate "
                            f"for {p['target']}; holding")
                        wire("target_no_match", {"want": p["target"]})
                    else:
                        await submit_target(p1, opp, rtype, stype, ch, tag)
                        p["targeted"] = True
                        continue
            await asyncio.sleep(0)
            st2 = p1.latest or st
            s2 = st2["state"]
            o2 = olivia_oid(s2)
            if p["targeted"] and o2 is not None:
                if ability_on_stack(s2, o2):
                    p["stack_seen"] = True
                elif p["stack_seen"] and not stack_entries(s2):
                    say("[P1] Olivia ping resolved")
                    wire("olivia_resolved", {})
                    ST["mana_needs"][tag] = {}
                    return True
            a2 = merged_actions(st2)
            if my_priority(a2) and not real_decision_pending(st2):
                await pass_priority(p1, st2, a2)
        say("[P1] Olivia ping timed out")
        wire("olivia_timeout", {})
        return False
    finally:
        ST["pending"] = None
        ST["mana_needs"]["P1"] = {}


async def run_amoeboid_gain(p0, p1, target_oid, timeout=240):
    """Activate Amoeboid's {T} 'gains all creature types' targeting
    target_oid; return True once resolved."""
    tag = "P1"
    ST["pending"] = {"tag": tag, "kind": "amoeboid", "target": str(target_oid),
                     "submitted": False, "targeted": False, "stack_seen": False,
                     "t0": time.time()}
    try:
        t0 = time.time()
        diag = 0
        while time.time() - t0 < timeout:
            await asyncio.sleep(0.25)
            await tick_seat(p0, "P0", 0)
            p = ST["pending"]
            st = p1.latest
            if not st:
                continue
            state = st["state"]
            acts = merged_actions(st)
            if await p1_routine(p1, st, acts, tag):
                continue
            oid = amoeboid_oid(state)
            if oid is None:
                say("[P1] Amoeboid left the battlefield; aborting")
                return False
            if not p["submitted"]:
                if not my_priority(acts):
                    if diag % 40 == 0:
                        diag_state("P1", st)
                    diag += 1
                    continue
                if not my_main(state, 1):
                    # not our window: keep the game moving toward P1's turn
                    if not real_decision_pending(st):
                        await pass_priority(p1, st, acts)
                    elif diag % 8 == 0:
                        say(f"[P1] waiting for main phase for amoeboid "
                            f"(phase={state.get('phase')}) decision pending; holding")
                    diag += 1
                    continue
                o = get_obj(state, oid)
                if o.get("tapped") or o.get("summoning_sick"):
                    if diag % 8 == 0:
                        say(f"[P1] amoeboid not ready: tapped={o.get('tapped')} "
                            f"sick={o.get('summoning_sick')}")
                    diag += 1
                    continue
                r = find_activate(st, oid, "gains all creature types")
                if r is None:
                    if diag % 8 == 0:
                        say(f"[P1] amoeboid ActivateAbility not offered; "
                            f"acts={[a['type'] for a in acts][:8]}")
                    diag += 1
                    continue
                kind, sub, what = r
                say(f"[P1] activating Amoeboid all-types -> "
                    f"{obj_lname(state, p['target'])} ({p['target']})")
                wire("amoeboid_activate", {"via": what[:120],
                                           "target": p["target"]})
                if kind == "interaction":
                    opp, ch = sub
                    await answer_vi(p1, opp, ch, tag)
                else:
                    await submit_as_is(p1, sub)
                p["submitted"] = True
                continue
            if not p["targeted"]:
                opp, rtype, stype = target_opportunity(st)
                if opp is not None:
                    ch = pick_target_candidate(state, opp, p["target"])
                    if ch is None:
                        say("[P1] amoeboid target opp without match; holding")
                    else:
                        await submit_target(p1, opp, rtype, stype, ch, tag)
                        p["targeted"] = True
                        continue
            await asyncio.sleep(0)
            st2 = p1.latest or st
            s2 = st2["state"]
            o2 = amoeboid_oid(s2)
            if p["targeted"] and o2 is not None:
                if ability_on_stack(s2, o2):
                    p["stack_seen"] = True
                elif p["stack_seen"] and not stack_entries(s2):
                    say("[P1] Amoeboid all-types resolved")
                    wire("amoeboid_resolved", {})
                    return True
            a2 = merged_actions(st2)
            if my_priority(a2) and not real_decision_pending(st2):
                await pass_priority(p1, st2, a2)
        say("[P1] Amoeboid gain timed out")
        return False
    finally:
        ST["pending"] = None


async def drive_until(p0, p1, cond, timeout, label, poll=0.25):
    t0 = time.time()
    last_rev = {}
    last_tick_at = {}
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        if ST["stop"]:
            return None
        for c, ttag, pid in ((p0, "P0", 0), (p1, "P1", 1)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                holds = my_priority(merged_actions(st))
                if not (holds and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await drain(c)
                await tick_seat(c, ttag, pid)
            except Exception as e:
                say(f"[{ttag}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": ttag,
                                    "err": f"{type(e).__name__}: {e}"})
        st0 = p0.latest
        if st0 and cond(st0["state"]):
            return st0["state"]
        if (st0["state"].get("turn_number") or 0) > TURN_CAP:
            say(f"turn cap {TURN_CAP} in {label}; ending")
            wire("turn_cap", {"label": label})
            ST["stop"] = True
            return None
    say(f"TIMEOUT in drive_until: {label}")
    wire("drive_timeout", {"label": label})
    return None


def board_ready_leg1(state):
    h = heliod_oid(state)
    if h is None:
        return False
    if "Creature" not in core_types_of(state, h):
        return False
    if devotion(state, 0) < 5:
        return False
    if olivia_oid(state) is None:
        return False
    if amoeboid_oid(state) is None:
        return False
    return True


def board_ready_restore(state):
    if devotion(state, 0) < 5:
        return False
    h = heliod_oid(state)
    if h is None or "Creature" not in core_types_of(state, h):
        return False
    a = amoeboid_oid(state)
    if a is None:
        return False
    o = get_obj(state, a)
    if o.get("tapped") or o.get("summoning_sick"):
        return False
    if olivia_oid(state) is None:
        return False
    return True


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = (json.loads(raw).get("data") or {})
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.114.0"), f"unexpected version {ver}"
    assert int(proto) == 153, f"unexpected protocol {proto}"
    assert str(build) == "ac67cbb", f"unexpected build {build}"


def check_card_data():
    ok, notes = True, []
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.114.0/data/"
                        "card-data.json"))

    def need(name, phrase):
        nonlocal ok
        c = cd.get(name, {})
        ot = str(c.get("oracle_text") or "")
        if phrase not in ot:
            ok = False
            notes.append(f"{name}: oracle mismatch; missing {phrase!r}")
        else:
            notes.append(f"{name}: oracle confirmed ({phrase[:48]}...)")

    need(HELIOD, "As long as your devotion to white is less than five, "
                 "Heliod isn't a creature.")
    need(OLIVIA, "That creature becomes a Vampire in addition to its other "
                 "types.")
    need(AMOEBOID, "Target creature gains all creature types until end of "
                   "turn.")
    hc = cd.get(HELIOD, {}).get("mana_cost") or {}
    notes.append(f"heliod card-data cost: {hc} "
                 f"(printed {{2}}{{W}}{{W}}; data drives devotion)")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "heliod_oracle": str(cd.get(HELIOD, {}).get("oracle_text")),
                   "olivia_oracle": str(cd.get(OLIVIA, {}).get("oracle_text")),
                   "amoeboid_oracle": str(cd.get(AMOEBOID, {}).get("oracle_text"))},
                  f, indent=1)
    say(f"card-data check: ok={ok}")
    wire("card_data_check", {"ok": ok, "notes": notes})
    return ok


# ------------------------------------------------------------- evaluation

def heliod_report(state):
    h = heliod_oid(state)
    if h is None:
        return {"present": False}
    return {"present": True,
            "core_types": core_types_of(state, h),
            "subtypes": subtypes_of(state, h),
            "zone": get_obj(state, h).get("zone"),
            "devotion_p0": devotion(state, 0)}


def evaluate():
    keys = ("A1_setup_ok", "A2_olivia_applied", "A3_devotion_dropped",
            "A4_no_stray_olivia", "A5_amoeboid_applied", "A6_no_stray_amoeboid")
    a = {k: "not-run" for k in keys}
    notes = ST["notes"]
    pre = load_env("pre_olivia")
    post = load_env("post_olivia")
    prea = load_env("pre_amoeboid")
    posta = load_env("post_amoeboid")

    if pre is None:
        notes.append("A1/A2 not-run: pre_olivia.json missing")
        return a, "blocked", notes
    hr = heliod_report(pre)
    notes.append(f"pre_olivia: heliod={hr}")
    if hr.get("present") and "Creature" in hr["core_types"] \
            and hr["devotion_p0"] >= 5:
        a["A1_setup_ok"] = "passed"
    else:
        a["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: setup precondition not met")
        return a, "blocked", notes
    if "Vampire" in hr["subtypes"]:
        a["A2_olivia_applied"] = "passed"
    else:
        a["A2_olivia_applied"] = "failed"
        notes.append("A2 FAILED: Olivia's Vampire grant not visible")
        return a, "blocked", notes

    if post is None:
        notes.append("A3/A4 not-run: post_olivia.json missing")
        return a, "blocked", notes
    hrp = heliod_report(post)
    notes.append(f"post_olivia: heliod={hrp}")
    if hrp.get("present") and hrp["devotion_p0"] <= 4:
        a["A3_devotion_dropped"] = "passed"
    else:
        a["A3_devotion_dropped"] = "failed"
        notes.append("A3 FAILED: devotion did not drop below 5")
    stray = [t for t in ("Creature",) if t in hrp.get("core_types", [])] \
        + (["Vampire"] if "Vampire" in hrp.get("subtypes", []) else [])
    if hrp.get("present") and hrp.get("core_types") == ["Enchantment"] \
            and hrp.get("subtypes") == []:
        a["A4_no_stray_olivia"] = "passed"
        notes.append("A4 passed: Heliod solely Enchantment, no subtypes")
    else:
        a["A4_no_stray_olivia"] = "failed"
        notes.append(f"A4 FAILED (REPORTED BUG): stray type/subtype "
                     f"remains: core={hrp.get('core_types')} "
                     f"subtypes={hrp.get('subtypes')}")

    if prea is None:
        notes.append("A5/A6 not-run: pre_amoeboid.json missing")
    else:
        hra = heliod_report(prea)
        notes.append(f"pre_amoeboid: heliod={hra}")
        if hra.get("present") and "Creature" in hra["core_types"] \
                and hra["devotion_p0"] >= 5 and len(hra["subtypes"]) >= 10:
            a["A5_amoeboid_applied"] = "passed"
        else:
            a["A5_amoeboid_applied"] = "failed"
            notes.append("A5 FAILED: all-types grant not visible")
    if posta is None:
        notes.append("A6 not-run: post_amoeboid.json missing")
    else:
        hrpa = heliod_report(posta)
        notes.append(f"post_amoeboid: heliod={hrpa}")
        if hrpa.get("present") and hrpa.get("core_types") == ["Enchantment"] \
                and hrpa.get("subtypes") == []:
            a["A6_no_stray_amoeboid"] = "passed"
            notes.append("A6 passed: Heliod solely Enchantment, no subtypes")
        else:
            a["A6_no_stray_amoeboid"] = "failed"
            notes.append(f"A6 FAILED (REPORTED SIBLING BUG): stray "
                         f"type/subtype remains: core={hrpa.get('core_types')} "
                         f"subtypes={hrpa.get('subtypes')}")

    if a["A1_setup_ok"] == "passed" and a["A2_olivia_applied"] == "passed" \
            and a["A3_devotion_dropped"] == "passed":
        if a["A4_no_stray_olivia"] == "failed" \
                or a["A6_no_stray_amoeboid"] == "failed":
            verdict = "reproduced"
        elif a["A4_no_stray_olivia"] == "passed" \
                and (a["A6_no_stray_amoeboid"] == "passed"
                     or a["A5_amoeboid_applied"] != "passed"):
            verdict = "not-reproduced" if a["A6_no_stray_amoeboid"] == "passed" \
                else "blocked"
            if a["A6_no_stray_amoeboid"] != "passed":
                notes.append("leg 2 (amoeboid) did not complete; verdict "
                             "based on leg 1 only -> blocked")
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"
    return a, verdict, notes


def render_summary(run, out_path):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 980
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #10086 - subtype grants surviving loss "
           "of Creature (devotion god)", fill=(235, 240, 250))
    y += 28
    d.text((24, y), f"server v0.114.0 (ac67cbb) protocol 153 - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    vcol = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
            "blocked": (255, 200, 90)}[run["verdict"]]
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from saved authoritative states):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "pre_olivia: Heliod is a creature, devotion >= 5",
        "A2_olivia_applied": "pre_olivia: Olivia's Vampire grant on Heliod",
        "A3_devotion_dropped": "post_olivia: devotion <= 4 (Vanguard pinged)",
        "A4_no_stray_olivia": "REPORTED: post_olivia Heliod solely "
            "Enchantment, no subtypes (fail == bug)",
        "A5_amoeboid_applied": "pre_amoeboid: all-types grant on Heliod",
        "A6_no_stray_amoeboid": "REPORTED sibling: post_amoeboid Heliod "
            "solely Enchantment, no subtypes (fail == bug)",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v}", fill=col)
        d.text((300, y), lab[:72], fill=(150, 160, 175))
        y += 24
    y += 12
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((36, y), str(n)[:118], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(out_path)
    say(f"wrote {out_path}")


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------- main

async def main():
    RUNLOG_F = open(f"{EVDIR}/scenario_run.log", "w")
    global RUNLOG, WIRE
    RUNLOG = RUNLOG_F
    WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    reset()
    MULLS.clear()
    MULL_COUNTS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED.clear()
    t0 = time.time()
    await verify_server_hello()
    if not check_card_data():
        say("card-data check FAILED; aborting")
        ST["stop"] = True

    p0 = PhaseClient("P0-10086")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1-10086")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    try:
        # ---- setup: ramp until Heliod is a creature at devotion >= 5,
        #      Olivia + Amoeboid on P1's battlefield
        ST["stage"] = "setup"
        s = await drive_until(p0, p1, board_ready_leg1, 700, "setup")
        if s is None:
            say("setup failed; blocking")
            ST["stop"] = True
        else:
            h = heliod_oid(s)
            say(f"setup done: devotion={devotion(s, 0)} "
                f"heliod types={core_types_of(s, h)} "
                f"subtypes={subtypes_of(s, h)}")
            wire("setup_done", {"devotion": devotion(s, 0),
                                "heliod_types": core_types_of(s, h),
                                "heliod_subtypes": subtypes_of(s, h)})

        # ---- leg 1a: Olivia pings Heliod
        if not ST["stop"]:
            ST["stage"] = "leg1a"
            s = p0.latest["state"] if p0.latest else None
            h = heliod_oid(s) if s else None
            if h is None:
                say("leg1a: no Heliod; blocking")
                ST["stop"] = True
            elif not await run_olivia_ping(p0, p1, h):
                say("leg1a failed; blocking")
                ST["stop"] = True
            else:
                await export_named(p0, "pre_olivia")

        # ---- leg 1b: Olivia pings a Vanguard -> devotion < 5
        if not ST["stop"]:
            ST["stage"] = "leg1b"
            s = p0.latest["state"]
            vs = vanguard_oids(s)
            n_before = len(vs)
            say(f"leg1b: {n_before} vanguards on battlefield; "
                f"pinging {vs[0]}")
            if not await run_olivia_ping(p0, p1, vs[0]):
                say("leg1b failed; blocking")
                ST["stop"] = True
            else:
                s2 = await drive_until(
                    p0, p1,
                    lambda st: len(vanguard_oids(st)) < n_before, 120,
                    "vanguard-death-watch")
                if s2 is None:
                    say("leg1b: vanguard death not observed; blocking")
                    ST["stop"] = True
                else:
                    await export_named(p0, "post_olivia")

        # ---- restore: P0 casts another Vanguard -> devotion >= 5
        if not ST["stop"]:
            ST["stage"] = "restore"
            s = await drive_until(p0, p1, board_ready_restore, 600,
                                  "restore")
            if s is None:
                say("restore incomplete; leg 2 skipped")
                ST["notes"].append("restore timed out; amoeboid leg skipped")
            else:
                say(f"restore done: devotion={devotion(s, 0)}")
                wire("restore_done", {"devotion": devotion(s, 0)})

        # ---- leg 2a: Amoeboid all-types -> Heliod (same turn as 2b)
        if not ST["stop"] and load_env("post_olivia") is not None \
                and board_ready_restore(p0.latest["state"]):
            ST["stage"] = "leg2a"
            s = p0.latest["state"]
            h = heliod_oid(s)
            turn0 = s.get("turn_number")
            if not await run_amoeboid_gain(p0, p1, h):
                say("leg2a failed; skipping leg2b")
                ST["notes"].append("amoeboid activation failed; leg2b skipped")
            else:
                await export_named(p0, "pre_amoeboid")
                # ---- leg 2b: Olivia pings the restored Vanguard, same turn
                ST["stage"] = "leg2b"
                s = p0.latest["state"]
                if s.get("turn_number") != turn0:
                    note = (f"leg2b turn advanced ({turn0} -> "
                            f"{s.get('turn_number')}); all-types effect may "
                            f"have expired")
                    say(note)
                    ST["notes"].append(note)
                vs = vanguard_oids(s)
                n_before = len(vs)
                say(f"leg2b: pinging restored vanguard {vs[0]} "
                    f"(turn {s.get('turn_number')})")
                if not await run_olivia_ping(p0, p1, vs[0]):
                    say("leg2b failed")
                    ST["notes"].append("leg2b olivia ping failed")
                else:
                    s2 = await drive_until(
                        p0, p1,
                        lambda st: len(vanguard_oids(st)) < n_before, 120,
                        "vanguard-death-watch-2")
                    if s2 is None:
                        say("leg2b: vanguard death not observed")
                        ST["notes"].append("leg2b: vanguard death not observed")
                    else:
                        await export_named(p0, "post_amoeboid")
    finally:
        pass

    assertions, verdict, notes = evaluate()
    run = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server_identity": SERVER_IDENTITY,
        "verdict": verdict,
        "assertions": assertions,
        "notes": notes,
        "rejections": ST.get("rejections", []),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "scope": "Olivia Voldaren ping grant + Amoeboid Changeling all-types "
                 "grant on Heliod, Sun-Crowned across a devotion 5->4 drop; "
                 "native engine, two human driver seats",
        "limitations": [
            "Pinned card data lists Heliod, Sun-Crowned at {2}{W} (1 white "
            "pip), not the printed {2}{W}{W}; devotion computed from engine "
            "data, ramped to >= 5 empirically.",
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "States are authoritative exports, restorable only via full "
            "game replay (no standalone state-import path).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": assertions, "verdict": verdict,
                   "notes": notes}, f, indent=1, default=str)
    with open(__file__, "rb") as f:
        scen_sha = hashlib.sha256(f.read()).hexdigest()
    with open(f"{EVDIR}/scenario_10086.py", "w") as f:
        f.write(open(__file__).read())
    run["scenario_sha256"] = scen_sha
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    render_summary(run, f"{EVDIR}/summary.png")

    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(os.path.join(EVDIR, f))
                   and f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            f.write(f"{sha256_of_file(os.path.join(EVDIR, fn))}  {fn}\n")
    for fn in files:
        p = os.path.join(EVDIR, fn)
        if fn.endswith(".json"):
            json.load(open(p))
        if fn.endswith(".jsonl"):
            for line in open(p):
                json.loads(line)
    try:
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        say("PNG readable: summary.png")
    except ImportError:
        say("PIL missing; PNG check skipped")
    say(f"done: verdict={verdict} assertions={assertions}")
    wire("done", {"verdict": verdict, "assertions": assertions})
    RUNLOG_F.close()
    WIRE.close()


if __name__ == "__main__":
    asyncio.run(main())
