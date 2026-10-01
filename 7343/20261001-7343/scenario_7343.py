#!/usr/bin/env python3
"""Issue #7343: Fatal Grudge was not castable from hand even with basic
lands of its colors readily available.

Oracle (pinned v0.99.0 card data): Fatal Grudge {B}{R} sorcery.
"As an additional cost to cast this spell, sacrifice a nonland permanent.
Each opponent chooses a permanent they control that shares a card type
with the sacrificed permanent and sacrifices it. Draw a card."
Parser: fully_parsed (Required Sacrifice nonland permanent; per-opponent
TargetOnly + Draw 1 SequentialSibling).

Run: 20261001-7343 on pinned v0.99.0 (build d919616, protocol 98).

Behavioral contract (native engine, P0 + P1 human driver seats):
  A1 setup_ok       pre.json: P0 PreCombatMain, Fatal Grudge in hand,
                    P0 Memnite on BF, P1 Memnite on BF, >=1 untapped
                    Swamp + >=1 untapped Mountain for P0
  A2 cast_offered   engine advertises CastSpell for the Grudge (the gate
                    offers it despite the Required additional cost)
  A3 cost_paid      additional cost paid: a P0 Memnite in graveyard;
                    Swamp + Mountain tapped for {B}{R}
  A4 opponent_sac  P1's Memnite in graveyard (chose + sacrificed)
  A5 draw           P0 library count pre -> post decreased by exactly 1
  A6 cleanup        Grudge in P0 graveyard, stack empty, game proceeding

Verdict: reproduced iff A1 passes and A2 fails (cast never offered - the
reported "not castable"), or the additional-cost / opponent-choice flow
stalls or silently no-ops (A3/A4 fail). not-reproduced iff A1-A6 pass.
"""
import asyncio
import copy
import json
import os
import sys
import time
import hashlib

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-7343"

SERVER_IDENTITY = {
    "validated_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
}


def _sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = _sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

EVDIR = f"{BACKFILL}/evidence/7343/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

GRUDGE = "Fatal Grudge"
MEMNITE = "Memnite"
SWAMP = "Swamp"
MOUNTAIN = "Mountain"
P0_DECK = [(GRUDGE, 4), (MEMNITE, 8), (SWAMP, 24), (MOUNTAIN, 24)]
P1_DECK = [(MEMNITE, 12), (MOUNTAIN, 48)]
LANDS = (SWAMP, MOUNTAIN)
TIMEOUT = 1500

ST = {}
_MULL_REV = {}
_PASSED_REV = {}
_DISCARD_REV = {}
WF_SEEN = []


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "rejections": [], "stop": False,
        "grudge_oid": None, "memnite_oid": None, "p1_memnite_oid": None,
        "cast_offered": False, "cast_submitted": False,
        "sac_paid": False, "opp_choice_done": False,
        "library_pre": None, "library_post": None,
        "hold_priority": False,
        "vi_wired_cast": set(),
    })
    _MULL_REV.clear(); _PASSED_REV.clear(); _DISCARD_REV.clear(); WF_SEEN.clear()


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
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
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


def bf_oid(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def zone_count(state, pid, zone):
    return sum(1 for o in state["objects"].values()
               if o.get("zone") == zone and o.get("controller") == pid)


def lib_count(state, pid):
    return sum(1 for o in state["objects"].values()
               if o.get("zone") == "Library" and o.get("controller") == pid)


def gy_names(state, pid):
    return [oname(o) for o in state["objects"].values()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def pending_for(state, pid):
    for p in wf_data(state).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def my_priority(state, pid):
    return (wf_type(state) == "Priority"
            and str(wf_player(state)) == str(pid))


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action, "stage": ST.get("stage")})
    await c.send_action(action)


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
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


async def export_now(c, path):
    try:
        s = await c.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


async def do_mulligan(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "MulliganDecision":
        return False
    if _MULL_REV.get((c.name, c.revision)):
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    n_lands = sum(1 for o in hand_oids(state, pid)
                  if oname(state["objects"][o]) in LANDS)
    n_mulls = _MULL_REV.get(c.name, 0)
    keep_ok = n_lands >= 2 or n_mulls >= 2
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        _MULL_REV[c.name] = n_mulls + 1
    _MULL_REV[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice}")
    return True


async def do_bottom(c, pid):
    st = c.latest
    if not st:
        return False
    if _MULL_REV.get((c.name, "bottom", c.revision)):
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    ph = pend.get("phase") or {}
    if ph.get("type") != "BottomCards":
        return False
    n = int(ph.get("count", 1) or 1)
    h = hand_oids(state, pid)
    pref = [o for o in h if oname(state["objects"][o]) not in LANDS
            and oname(state["objects"][o]) not in (GRUDGE, MEMNITE)]
    pref += [o for o in h if o not in pref]
    picks = [int(x) for x in pref[:n]]
    _MULL_REV[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_type(state) != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_oids(state, pid)) - 7
    if n <= 0:
        return False
    h = hand_oids(state, pid)
    # keep Grudge + lands; discard Memnites first
    pref = [o for o in h if oname(state["objects"][o]) == MEMNITE]
    pref += [o for o in h if o not in pref
             and oname(state["objects"][o]) not in LANDS
             and oname(state["objects"][o]) != GRUDGE]
    pref += [o for o in h if o not in pref]
    picks = [int(x) for x in pref[:n]]
    if not picks:
        return False
    _DISCARD_REV[(c.name, rev)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} discards {len(picks)}")
    return True


async def pass_priority(c, pid):
    st = c.latest
    if not st:
        return False
    if ST.get("hold_priority"):
        return False
    if not my_priority(st["state"], pid):
        return False
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get((c.name, rev)):
        return False
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "PassPriority":
            _PASSED_REV[(c.name, rev)] = True
            await submit_as_is(c, a)
            return True
    return False


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_data(state), "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} stage={ST.get('stage')}")


def vi_ops(st):
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def vi_choice_for_oid(st, oid):
    """Find a viewer_interaction opportunity whose candidates reference oid.
    Returns (opportunity, choice) or None. Skips castSpell/priority menus."""
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        for ch in chs:
            if not isinstance(ch, dict):
                continue
            codes = action_codes(ch)
            if "castSpell" in codes or "passPriority" in codes:
                continue
            blob = json.dumps(ch, default=str)
            if str(oid) in blob:
                return opp, ch
    return None


def vi_cast_choice(st, oid):
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in action_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None


async def handle_castoffer(c, pid, state):
    """Answer the engine's CastOffer confirmation (7313 pattern)."""
    st = c.latest
    if wf_type(state) != "CastOffer":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    data = wf_data(state)
    kind = data.get("kind") or {}
    oid = kind.get("object_id")
    vcc = vi_cast_choice(st, oid)
    if vcc:
        opp, ch = vcc
        iid = opp.get("interactionId") or opp.get("id")
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": ch["id"]}}}
        wire("castoffer_submit", {"oid": oid, "choice_id": ch.get("id")})
        await c.send_interaction(sub)
        say(f"[{c.name}] answered CastOffer for oid={oid}")
        return True
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if len(chs) == 1 and isinstance(chs[0], dict) and chs[0].get("id"):
            iid = opp.get("interactionId") or opp.get("id")
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": chs[0]["id"]}}}
            wire("castoffer_submit_single", {"oid": oid,
                                            "choice_id": chs[0]["id"]})
            await c.send_interaction(sub)
            say(f"[{c.name}] answered CastOffer (single choice) for oid={oid}")
            return True
    return False


async def answer_sacrifice_prompt(c, pid, state, st):
    """Answer P0's additional-cost sacrifice prompt with the Memnite."""
    if ST.get("sac_paid"):
        return False
    moid = ST.get("memnite_oid")
    if not moid:
        return False
    # legacy action route: an advertised Sacrifice* action for our Memnite
    for a in st.get("legal_actions", []) or []:
        blob = json.dumps(a, default=str)
        if "acrifice" in (a.get("type") or "") and str(moid) in blob:
            wire("sacrifice_action", {"action": a})
            await submit_as_is(c, a)
            ST["sac_paid"] = True
            say(f"[P0] sacrifice via advertised {a.get('type')} (memnite {moid})")
            return True
    vcc = vi_choice_for_oid(st, moid)
    if not vcc:
        return False
    opp, ch = vcc
    iid = opp.get("interactionId") or opp.get("id")
    cid = ch.get("id") or ch.get("choiceId")
    spec = ((opp.get("response") or {}).get("data") or {}).get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else None
    if not iid or cid is None:
        wire("sac_vi_unusable", {"iid": iid, "cid": cid, "stype": stype})
        return False
    if stype == "sequence":
        sub = {"interactionId": iid,
               "response": {"type": "sequence", "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    wire("sacrifice_submit", {"iid": iid, "cid": cid, "stype": stype, "sub": sub})
    await c.send_interaction(sub)
    ST["sac_paid"] = True
    say(f"[P0] sacrificed Memnite {moid} (stype={stype})")
    return True


async def answer_opp_choice(c, pid, state, st):
    """Answer P1's 'choose a permanent sharing a card type' with P1's Memnite."""
    if ST.get("opp_choice_done"):
        return False
    moid = ST.get("p1_memnite_oid")
    if not moid:
        # fall back to any P1 Memnite on the battlefield
        moid = bf_oid(state, pid, MEMNITE)
        if moid:
            ST["p1_memnite_oid"] = moid
        else:
            return False
    for a in st.get("legal_actions", []) or []:
        blob = json.dumps(a, default=str)
        atype = (a.get("type") or "").lower()
        if ("choose" in atype or "sacrifice" in atype) and str(moid) in blob:
            wire("oppchoice_action", {"action": a})
            await submit_as_is(c, a)
            ST["opp_choice_done"] = True
            say(f"[P1] opp choice via advertised {a.get('type')} (memnite {moid})")
            return True
    vcc = vi_choice_for_oid(st, moid)
    if not vcc:
        return False
    opp, ch = vcc
    iid = opp.get("interactionId") or opp.get("id")
    cid = ch.get("id") or ch.get("choiceId")
    spec = ((opp.get("response") or {}).get("data") or {}).get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else None
    if not iid or cid is None:
        wire("oppchoice_vi_unusable", {"iid": iid, "cid": cid, "stype": stype})
        return False
    if stype == "sequence":
        sub = {"interactionId": iid,
               "response": {"type": "sequence", "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    wire("oppchoice_submit", {"iid": iid, "cid": cid, "stype": stype, "sub": sub})
    await c.send_interaction(sub)
    ST["opp_choice_done"] = True
    say(f"[P1] chose+sacrificed Memnite {moid} (stype={stype})")
    return True


async def p_land_drop(c, pid, state, acts, land_pref):
    if state.get("land_played_this_turn"):
        return False
    for name in land_pref:
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == str(lid):
                    await submit_as_is(c, a)
                    say(f"[{c.name}] land drop {name}")
                    return True
    return False


async def p_cast_memnite(c, pid, state, acts):
    if bf_oid(state, pid, MEMNITE):
        return False
    oid = find_hand(state, pid, MEMNITE)
    if not oid:
        return False
    if ST.get("cast_in_flight"):
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            await submit_as_is(c, a)
            say(f"[{c.name}] cast Memnite (oid={oid})")
            ST["cast_in_flight"] = str(oid)
            return True
    obj = state["objects"].get(str(oid), {})
    cid = obj.get("card_id", int(oid))
    await submit_as_is(c, {"type": "CastSpell",
                           "data": {"object_id": int(oid), "card_id": int(cid),
                                    "targets": [], "payment_mode": {"type": "Auto"}}})
    say(f"[{c.name}] cast Memnite raw (oid={oid})")
    ST["cast_in_flight"] = str(oid)
    return True


def clear_inflight(state, pid):
    cif = ST.get("cast_in_flight")
    if cif and not any(str(oid) == str(cif) for oid in hand_oids(state, pid)):
        ST["cast_in_flight"] = None
        wire("cast_left_hand", {"oid": cif})
        say(f"cast in-flight {cif} left hand")


def castspell_advertised(acts, oid):
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            return a
    return None


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    if await handle_castoffer(c, pid, state):
        return True
    vi = get_vi(st)
    if vi and str(wf_player(state)) == str(pid):
        if ST.get("stage") in ("CAST", "RESOLVING"):
            if await answer_sacrifice_prompt(c, pid, state, st):
                return True
        # observe any vi during the cast flow
        key = json.dumps(vi, default=str)[:160]
        if key not in ST["vi_wired_cast"]:
            ST["vi_wired_cast"].add(key)
            wire("cast_vi", {"wf": wf_type(state), "vi": vi, "stage": ST.get("stage")})
            say(f"[P0] cast vi wf={wf_type(state)} stage={ST.get('stage')}")

    if ST["stage"] == "SETUP" and is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        moid = bf_oid(state, pid, MEMNITE)
        if not moid:
            if await p_cast_memnite(c, pid, state, acts):
                return True
        # land drops until we have Swamp + Mountain
        if untapped_of(state, pid, SWAMP) < 1 or untapped_of(state, pid, MOUNTAIN) < 1:
            if await p_land_drop(c, pid, state, acts, (SWAMP, MOUNTAIN)):
                return True
        else:
            if await p_land_drop(c, pid, state, acts, (SWAMP, MOUNTAIN)):
                return True
        # conditions to cast: Grudge in hand, Memnite on BF, P1 Memnite on BF,
        # Swamp+Mountain untapped
        goid = find_hand(state, pid, GRUDGE)
        moid = bf_oid(state, pid, MEMNITE)
        p1m = bf_oid(state, 1, MEMNITE)
        if moid and goid and p1m \
                and untapped_of(state, pid, SWAMP) >= 1 \
                and untapped_of(state, pid, MOUNTAIN) >= 1:
            ST["memnite_oid"] = moid
            ST["p1_memnite_oid"] = p1m
            if not ST["pre_exported"]:
                s = await export_now(c, "pre.json")
                if s is not None:
                    ST["pre_exported"] = True
                    ST["grudge_oid"] = goid
                    ps = json.loads(s)["state"]
                    ST["library_pre"] = lib_count(ps, pid)
                    wire("pre", {"grudge_oid": goid, "memnite_oid": moid,
                                 "p1_memnite_oid": p1m,
                                 "library_pre": ST["library_pre"],
                                 "turn": state.get("turn_number"),
                                 "phase": state.get("phase")})
            a = castspell_advertised(acts, goid)
            if a:
                ST["cast_offered"] = True
                wire("castspell_advertised", {"action": a})
                say(f"[P0] Fatal Grudge offered (oid={goid}) - gate OK")
                await submit_as_is(c, a)
                ST["cast_submitted"] = True
                ST["stage"] = "CAST"
                ST["cast_at"] = time.time()
                return True
            else:
                wire("no_castspell_offer", {
                    "flat_types": sorted(set(x.get("type") for x in acts)),
                    "grudge_oid": goid,
                })
                say(f"[P0] Fatal Grudge NOT offered (oid={goid}) - BUG CANDIDATE")
                ST["no_offer_observations"] = ST.get("no_offer_observations", 0) + 1
                if ST["no_offer_observations"] >= 4 and not ST.get("post_exported"):
                    # 4 main-phase observations with no offer: capture and stop
                    ST["hold_priority"] = True
                    try:
                        await export_now(c, "post.json")
                    finally:
                        ST["hold_priority"] = False
                    ST["post_exported"] = True
                    ST["stop"] = True
                    return True
    if state.get("active_player") == pid and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if await pass_priority(c, pid):
        return True
    return False


async def p1_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    vi = get_vi(st)
    if vi and str(wf_player(state)) == str(pid):
        if ST.get("stage") in ("CAST", "RESOLVING"):
            if await answer_opp_choice(c, pid, state, st):
                return True
        key = "p1:" + json.dumps(vi, default=str)[:160]
        if key not in ST["vi_wired_cast"]:
            ST["vi_wired_cast"].add(key)
            wire("opp_vi", {"wf": wf_type(state), "vi": vi, "stage": ST.get("stage")})
            say(f"[P1] opp vi wf={wf_type(state)} stage={ST.get('stage')}")
    if is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast_memnite(c, pid, state, acts):
            return True
        if await p_land_drop(c, pid, state, acts, (MOUNTAIN,)):
            return True
    if state.get("active_player") == pid and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if await pass_priority(c, pid):
        return True
    return False


def grudge_on_stack(state):
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str)
        if GRUDGE.lower().replace(" ", "") in blob.lower().replace(" ", "") \
                or str(ST.get("grudge_oid")) in blob:
            return True
    return False


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_created", {"code": p0.game_code, "p0_seat": p0.player_id,
                          "p1_seat": p1.player_id,
                          "p0_deck": P0_DECK, "p1_deck": P1_DECK})
    clients = [(p0, p0.player_id, p0_tick), (p1, p1.player_id, p1_tick)]
    last_rev = {}
    last_wall = 0

    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, _ in clients:
            rej = drain_rejections(c)
            if rej:
                ST["rejections"].extend(
                    {"at": now, "who": c.name, "type": r["type"], "data": r["data"]}
                    for r in rej)
        if now - last_wall >= 3:
            last_wall = now
            for c, pid, tickf in clients:
                try:
                    await tickf(c, pid)
                except Exception as e:
                    say(f"tick error [{c.name}]: {e}")
            for c, _, _ in clients:
                last_rev[c.name] = c.revision
        else:
            for c, pid, tickf in clients:
                if c.revision != last_rev.get(c.name, -1):
                    try:
                        await tickf(c, pid)
                    except Exception as e:
                        say(f"tick error [{c.name}]: {e}")
                    last_rev[c.name] = c.revision

        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # ---- CAST flow checkpoints ----
        if ST.get("stage") == "CAST":
            stack = state.get("stack") or []
            sac_done = (MEMNITE in gy_names(state, p0.player_id)
                        and ST.get("memnite_oid") is not None
                        and not any(str(oid) == str(ST["memnite_oid"])
                                    for oid, o in state["objects"].items()
                                    if o.get("zone") == "Battlefield"))
            if sac_done and not ST.get("mid_exported"):
                s = await export_now(p0, "mid_cast.json")
                if s is not None:
                    ST["mid_exported"] = True
                    wire("mid_cast", {"stack_n": len(stack), "wf": wf_type(state)})
                    say(f"mid_cast: sacrifice paid, stack={len(stack)} wf={wf_type(state)}")
                    ST["stage"] = "RESOLVING"
            if not stack and ST.get("sac_paid") and not ST.get("post_exported"):
                # resolution completed (stack empty after the cast)
                ST["hold_priority"] = True
                try:
                    s = await export_now(p0, "post.json")
                    if s is not None:
                        ST["post_exported"] = True
                        ps = json.loads(s)["state"]
                        ST["library_post"] = lib_count(ps, p0.player_id)
                        say(f"post: stack empty after cast, library_post={ST['library_post']}")
                finally:
                    ST["hold_priority"] = False
                ST["stop"] = True
            # stall guard: cast submitted, 150s, still pending
            if not ST.get("post_exported") and now - ST.get("cast_at", now) > 150:
                say("cast stalled 150s; exporting post and stopping")
                wire("cast_stalled", {"wf": wf_type(state),
                                      "rejections": ST["rejections"][-3:]})
                ST["hold_priority"] = True
                try:
                    await export_now(p0, "post.json")
                finally:
                    ST["hold_priority"] = False
                ST["post_exported"] = True
                ST["stop"] = True

        if ST.get("stage") == "RESOLVING":
            stack = state.get("stack") or []
            if not stack and not ST.get("post_exported"):
                ST["hold_priority"] = True
                try:
                    s = await export_now(p0, "post.json")
                    if s is not None:
                        ST["post_exported"] = True
                        ps = json.loads(s)["state"]
                        ST["library_post"] = lib_count(ps, p0.player_id)
                        say(f"post: resolved, library_post={ST['library_post']}")
                finally:
                    ST["hold_priority"] = False
                ST["stop"] = True
            if not ST.get("post_exported") and now - ST.get("cast_at", now) > 240:
                say("resolving stalled 240s; exporting post and stopping")
                ST["hold_priority"] = True
                try:
                    await export_now(p0, "post.json")
                finally:
                    ST["hold_priority"] = False
                ST["post_exported"] = True
                ST["stop"] = True

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            say("game over")
            continue

        if now - ST.get("last_diag", 0) > 30:
            ST["last_diag"] = now
            _p0, _p1 = p0.player_id, p1.player_id
            hand0 = [oname(state["objects"][o]) for o in hand_oids(state, _p0)]
            bf0 = [oname(o) for _, o in bf(state, _p0)]
            bf1 = [oname(o) for _, o in bf(state, _p1)]
            stack = state.get("stack") or []
            say(f"DIAG turn={state.get('turn_number')} phase={state.get('phase')} "
                f"wf={wf_type(state)}/p{wf_player(state)} stack={len(stack)} "
                f"p0hand={len(hand0)} p0mem={sum(1 for n in bf0 if n==MEMNITE)} "
                f"p0grudge_hand={sum(1 for n in hand0 if n==GRUDGE)} "
                f"p1mem={sum(1 for n in bf1 if n==MEMNITE)} "
                f"untap_sw={untapped_of(state,_p0,SWAMP)} "
                f"untap_mo={untapped_of(state,_p0,MOUNTAIN)} "
                f"stage={ST.get('stage')} sac={ST.get('sac_paid')} "
                f"opp={ST.get('opp_choice_done')}")
            wire("diag", {"turn": state.get("turn_number"), "phase": state.get("phase"),
                          "wf": wf_type(state), "stack_n": len(stack),
                          "p0hand": hand0, "p0bf": bf0, "p1bf": bf1})

        if not ST.get("post_exported") and now - t0 > TIMEOUT - 120:
            await export_now(p0, "post.json")
            ST["post_exported"] = True
            ST["stop"] = True

    await p0.close()
    await p1.close()
    return dict(ST)


if __name__ == "__main__":
    st = asyncio.run(main())
    print(json.dumps({k: (v if not isinstance(v, (dict, list)) else str(v)[:200])
                      for k, v in st.items()}, indent=2, default=str))
