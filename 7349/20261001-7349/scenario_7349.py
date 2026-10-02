#!/usr/bin/env python3
"""Issue #7349: Time Reaper - the Consume Anomaly effect doesn't appear to work.

Oracle (pinned v0.99.0 card data):
  Time Reaper {3}{B}{B} Creature - Alien Horror 4/4: "Flying, haste
  Consume Anomaly -- Whenever this creature deals combat damage to a player,
  put target face-up card they own in exile on the bottom of their library.
  If you do, you gain 3 life."
Parsed (v0.99.0): DamageDone trigger, valid_target Player, effect
PutAtLibraryPosition with target {"type": "Typed", "type_filters": []} --
the face-up / owned-by-damaged-player / in-exile filter is absent from the
parse (triage + classifier analysis on the issue).

Reported: "Just tried to select a permanent and it would only let me target
an opponent." I.e. the trigger's target-selection prompt offers only
player(s) as legal targets, never the face-up exiled card.

Run: 20261001-7349 on pinned v0.99.0 (build d919616, protocol 98).

Behavioral contract (native engine, P0 + P1 human driver seats):
  P1 exiles one of its own Memnites face-up with Swords to Plowshares.
  P0 casts Time Reaper, attacks P1 with it (haste), P1 blocks nothing.
  On combat damage the Consume Anomaly trigger fires and P0 must choose
  "target face-up card they own in exile".

  A1 setup_ok       setup.json: P0 main, P0 priority, Time Reaper castable
                    (>=5 untapped Swamps, Reaper in P0 hand), >=1 face-up
                    exiled card owned by P1, stack empty
  A2 target_prompt THE REPORTED PATH: after combat damage, a target-selection
                    opportunity for the trigger appears in P0's view
  A3 exiled_offered candidates include the face-up exiled Memnite (card)
  A4 no_players    candidates do NOT include any player
  A5 resolution    conditional:
                     - if A3&A4 pass: driver targets the exiled Memnite;
                       post.json must show it on the bottom of P1's library
                       and P0 at 23 life (20+3)
                     - elif A2 passed but A3/A4 failed: driver takes the only
                       legal choice (the offered player); post.json must show
                       the Memnite still in P1's exile and P0 still at 20
                       life (the "if you do" clause cannot be satisfied)
  A6 cleanup       post exported; trigger off the stack; game proceeding;
                    rejections noted

Verdict: reproduced iff A1+A2 pass and A3 fails (the reported outcome:
the exiled card cannot be targeted). not-reproduced iff A1..A5 all pass.
blocked iff A1 or A2 fails.

Driver notes (protocol 98):
  - DeclareAttackers/DeclareBlockers and target selection use the
    viewer_interaction "relations" schema:
    {"type":"relations","data":{"relations":[{"sourceId":..,"targetId":..,
    "group":null}]}}. Edges in the spec map sourceId -> targetIds; the
    offered targets are the union of edge targetIds. Empty vec = declare
    nothing. (Observed on the Swords target prompt and the attack
    declaration; mirrors scenario_6860_0980.)
  - A CastSpell for Swords with a single legal target on the battlefield
    resolved without a driver-answered prompt (engine single-candidate
    path); the driver only answers the prompt while the spell is on the
    stack, keyed by interactionId (stale opportunities are never answered).
  - This engine's state reports Title-Case card names.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-7349"
ISSUE = 7349

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

EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

REAPER = "Time Reaper"
SWORDS = "Swords to Plowshares"
MEMNITE = "Memnite"
SWAMP = "Swamp"
PLAINS = "Plains"
# Dense playsets are a test-harness convenience (the engine accepts
# >4-of for custom games); they make the setup draw-reliable.
P0_DECK = [(REAPER, 8), (SWAMP, 44)]
P1_DECK = [(MEMNITE, 16), (SWORDS, 8), (PLAINS, 36)]
TIMEOUT = 1500

ST = {}
_MULL_REV = {}
_PASSED_REV = {}
_DISCARD_REV = {}
WF_SEEN = []
STACK_SEEN = []


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",
        "setup_exported": False, "pre_exported": False, "post_exported": False,
        "rejections": [], "stop": False,
        "p0_seat": None, "p1_seat": None,
        "cast_in_flight": None,
        "reaper_cast": False, "reaper_oid": None,
        "attack_declared": False, "attack_iid": None,
        "swords_cast": False, "swords_answered_iids": set(),
        "exiled_memnite_oid": None,
        "trigger_seen": False, "trigger_opp_wired": False,
        "trigger_iid": None,
        "target_answered": False, "target_choice": None,
        "answered_iids": set(),
        "trigger_stage_since": None,
    })
    _MULL_REV.clear(); _PASSED_REV.clear(); _DISCARD_REV.clear()
    WF_SEEN.clear(); STACK_SEEN.clear()


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


def bf_oid(state, pid, name, exclude=()):
    for oid, o in bf(state, pid):
        if oname(o) == name and str(oid) not in {str(x) for x in exclude}:
            return oid
    return None


def all_creatures(state):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield"
            and "Creature" in ((o.get("card_types") or {}).get("core_types") or [])]


def life_of(state, pid):
    players = state.get("players") or []
    return (players[pid] or {}).get("life") if len(players) > pid else None


def untapped_lands(state, pid, land_name):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == land_name and not o.get("tapped")]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


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


def exiled_owned(state, pid):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == "Exile" and str(o.get("owner")) == str(pid):
            out.append((str(oid), o))
    return out


def stack_has(state, *needles):
    for se in state.get("stack") or []:
        blob = json.dumps(se, default=str)
        if all(n in blob for n in needles):
            return se
    return None


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


# ---------------- mulligan / mana / priority helpers ----------------

async def do_mulligan(c, pid, lands, key_names):
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
    hand_names = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
    n_lands = sum(1 for n in hand_names if n in lands)
    has_key = any(n in key_names for n in hand_names)
    n_mulls = _MULL_REV.get(c.name, 0)
    keep_ok = (n_lands >= 2 and has_key) or n_mulls >= 2 or \
        (n_lands >= 3 and n_mulls >= 1)
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        _MULL_REV[c.name] = n_mulls + 1
    _MULL_REV[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice} (lands={n_lands})")
    return True


def _discard_order(state, pid, first_names, last_names):
    h = hand_oids(state, pid)

    def rank(o):
        n = oname(state["objects"][o])
        if n in first_names:
            return 0
        if n in last_names:
            return 2
        return 1
    return sorted(h, key=rank)


async def do_bottom(c, pid, lands, protect=()):
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
    picks = [int(x) for x in _discard_order(state, pid, lands, protect)[:n]]
    _MULL_REV[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid, lands, protect=()):
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
    picks = [int(x) for x in _discard_order(state, pid, lands, protect)[:n]]
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


async def handle_mana_payment(c, pid, state, st):
    if wf_type(state) != "ManaPayment":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    for a in st.get("legal_actions", []) or []:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            wire("mana_pay_action", {"who": c.name, "action": a,
                                     "stage": ST.get("stage")})
            await submit_as_is(c, a)
            say(f"[{c.name}] ManaPayment: submitted {a.get('type')}")
            return True
    return False


async def p_land_drop(c, pid, state, acts, land_name):
    if state.get("land_played_this_turn"):
        return False
    lid = find_hand(state, pid, land_name)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == str(lid):
                await submit_as_is(c, a)
                say(f"[{c.name}] land drop {land_name}")
                return True
    return False


async def p_cast(c, pid, state, acts, name, tag=None):
    oid = find_hand(state, pid, name)
    if not oid:
        return False
    if ST.get("cast_in_flight"):
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            await submit_as_is(c, a)
            say(f"[{c.name}] cast {name} (oid={oid})")
            ST["cast_in_flight"] = str(oid)
            if tag:
                ST[tag] = str(oid)
            return True
    return False


def clear_inflight(state, pid):
    cif = ST.get("cast_in_flight")
    if cif and not any(str(oid) == str(cif) for oid in hand_oids(state, pid)):
        ST["cast_in_flight"] = None
        wire("cast_left_hand", {"oid": cif})
        say(f"cast in-flight {cif} left hand")


# ---------------- viewer_interaction / relations helpers ----------------

def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_data(state), "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} stage={ST.get('stage')}")


def record_stack(state):
    ids = []
    for se in state.get("stack") or []:
        blob = json.dumps(se, default=str)
        ids.append(blob[:160])
    key = "|".join(ids)
    if not STACK_SEEN or STACK_SEEN[-1] != key:
        STACK_SEEN.append(key)
        wire("stack", {"entries": ids, "stage": ST.get("stage"),
                       "turn": state.get("turn_number")})
        say(f"stack ({len(ids)}): {[s[:100] for s in ids]}")


def vi_ops(st):
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def opp_intents(opp):
    out = []
    for s in opp.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("intent"):
            out.append(d.get("intent"))
    return out


def relations_opps(st, exclude_intents=()):
    """All relations-schema opportunities whose surface intents avoid the
    excluded ones. Returns list of (opp, data, spec_data)."""
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") != "schema" or not isinstance(spec, dict):
            continue
        if spec.get("type") != "relations":
            continue
        intents = opp_intents(opp)
        if any(i in exclude_intents for i in intents):
            continue
        out.append((opp, data, spec.get("data", {}) or {}))
    return out


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def classify_candidate(ch, state):
    """Classify a target candidate: ('player', seat) | ('card', name, zone)
    | ('other', text)."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if not isinstance(d, dict):
            continue
        if s.get("type") == "player" or "seat" in d:
            return ("player", d.get("seat"))
        if s.get("type") == "object" or "reference" in d:
            ref = str(d.get("reference"))
            o = (state.get("objects") or {}).get(ref, {})
            return ("card", oname(o) or d.get("name"), o.get("zone"), ref)
    return ("other", choice_text(ch))


def offered_target_ids(spec_data):
    return {t for e in spec_data.get("edges", []) or []
            for t in e.get("targetIds", []) or []}


def target_opps(st):
    """Target-selection opportunities in this client's view: relations
    schema (excluding attack/block intents) or sequence/select schema.
    Already-answered iids are skipped. Returns list of
    (opp, data, spec_data, stype)."""
    out = []
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if not iid or iid in ST["answered_iids"]:
            continue
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        if stype == "relations":
            if any(i in ("attack", "block") for i in opp_intents(opp)):
                continue
            out.append((opp, data, spec.get("data", {}) or {}, "relations"))
        elif stype in ("sequence", "select"):
            out.append((opp, data, spec.get("data", {}) or {}, stype))
    return out


def submit_target(c, o, stype, spec_data, cid):
    """Build the submission for a chosen target candidate id."""
    iid = o.get("interactionId") or o.get("id")
    if stype == "relations":
        src = next((e.get("sourceId") for e in spec_data.get("edges", []) or []
                    if cid in (e.get("targetIds") or [])), None)
        if not src:
            return None
        return ({"interactionId": iid,
                 "response": {"type": "relations",
                              "data": {"relations": [
                                  {"sourceId": src, "targetId": cid,
                                   "group": None}]}}}, iid, cid)
    # sequence / select: choiceIds
    return ({"interactionId": iid,
             "response": {"type": stype, "data": {"choiceIds": [cid]}}},
            iid, cid)


async def answer_relations(c, opp, pairs, tag):
    iid = opp.get("interactionId") or opp.get("id")
    if not iid or iid in ST["answered_iids"]:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": [
                            {"sourceId": s, "targetId": t, "group": None}
                            for s, t in pairs]}}}
    wire(f"{tag}_submit", {"iid": iid, "pairs": pairs,
                           "submission": sub, "stage": ST.get("stage")})
    await c.send_interaction(sub)
    ST["answered_iids"].add(iid)
    say(f"[{c.name}] {tag}: submitted {len(pairs)} relation(s) (iid={iid})")
    return True


async def answer_target_opp(c, o, stype, spec_data, cid, tag):
    """Answer a target-selection opportunity with the chosen candidate id."""
    res = submit_target(c, o, stype, spec_data, cid)
    if not res:
        wire(f"{tag}_unusable", {"cid": cid, "stype": stype})
        return False
    sub, iid, _ = res
    if iid in ST["answered_iids"]:
        return False
    wire(f"{tag}_submit", {"iid": iid, "cid": cid, "stype": stype,
                           "submission": sub, "stage": ST.get("stage")})
    await c.send_interaction(sub)
    ST["answered_iids"].add(iid)
    say(f"[{c.name}] {tag}: submitted target {cid} (iid={iid}, stype={stype})")
    return True


async def handle_trigger_opp(c, pid, state, st, opps, p1):
    """Answer the Consume Anomaly trigger target prompt. Returns True if it acted."""
    o, data, spec_data, stype = opps[0]
    iid = o.get("interactionId") or o.get("id")
    chs = {ch.get("id"): ch for ch in data.get("candidates", []) or []
           if isinstance(ch, dict)}
    if stype == "relations":
        offered = offered_target_ids(spec_data)
    else:
        offered = set(chs.keys())
    if not ST.get("trigger_opp_wired"):
        ST["trigger_opp_wired"] = True
        ST["trigger_iid"] = iid
        trig = stack_has(state, "Time Reaper")
        wire("trigger_opp_seen",
             {"iid": iid, "intents": opp_intents(o), "stype": stype,
              "spec": spec_data,
              "candidates": {cid: classify_candidate(ch, state)
                             for cid, ch in chs.items()},
              "offered_target_ids": sorted(offered),
              "trigger_on_stack": bool(trig),
              "trigger_blob": json.dumps(trig, default=str)[:600] if trig else None})
        s = await export_now(c, "pre.json")
        if s is not None:
            ST["pre_exported"] = True
        say(f"[P0] TRIGGER target prompt seen (iid={iid}, stype={stype}); "
            f"pre exported; offered={sorted(offered)}")
    # classify the OFFERED targets
    ex_oid = ST.get("exiled_memnite_oid")
    card_pick = None
    player_pick = None
    summary = []
    for cid in sorted(offered):
        ch = chs.get(cid)
        if ch is None:
            summary.append({"id": cid, "kind": "unknown-target"})
            continue
        kind = classify_candidate(ch, state)
        is_exiled = ex_oid and kind[0] == "card" and kind[3] == str(ex_oid)
        summary.append({"id": cid, "kind": kind[0], "detail": kind[1:],
                        "text": choice_text(ch),
                        "is_exiled_memnite": bool(is_exiled),
                        "available": (ch.get("status") or {}).get("type")})
        if kind[0] == "card" and is_exiled and card_pick is None:
            card_pick = cid
        if kind[0] == "player" and str(kind[1]) == str(p1) \
                and player_pick is None:
            player_pick = cid
    wire("trigger_candidates", {"iid": iid, "stype": stype,
                                "candidates": summary,
                                "exiled_memnite_oid": ex_oid})
    say(f"[P0] trigger candidates: {json.dumps(summary)[:700]}")
    pick, why = None, None
    if card_pick is not None:
        pick, why = card_pick, "exiled_memnite_offered"
    elif player_pick is not None:
        pick, why = player_pick, "only_player_offered"
    if pick is None:
        wire("trigger_no_pick", {"summary": summary})
        say("[P0] trigger prompt: no usable pick; leaving pending")
        return True
    if await answer_target_opp(c, o, stype, spec_data, pick, "trigger_target"):
        ST["target_answered"] = True
        ST["target_choice"] = why
        say(f"[P0] trigger target answered: {why} (target={pick})")
    return True


# ---------------- P0 ----------------

async def p0_declare_attackers(c, pid, state, st):
    """DeclareAttackers waiting for P0: attack with the Reaper via the
    relations schema when in ATTACK stage, else declare nothing."""
    if wf_type(state) != "DeclareAttackers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    roid = ST.get("reaper_oid")
    p1 = ST["p1_seat"]
    opps = relations_opps(st, exclude_intents=())
    # prefer the attack-intent opportunity
    atk = [o for o in opps if "attack" in opp_intents(o[0])]
    opp = atk[0] if atk else (opps[0] if opps else None)
    if opp is None:
        # fall back to the legacy empty declaration (accepted on proto 98)
        for a in st.get("legal_actions", []) or []:
            if a.get("type") == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                say("[P0] declares no attackers (legacy empty)")
                return True
        return False
    o, data, spec_data = opp
    iid = o.get("interactionId") or o.get("id")
    if iid in ST["answered_iids"]:
        return False
    chs = {ch.get("id"): ch for ch in data.get("candidates", []) or []
           if isinstance(ch, dict)}
    wire("attack_opp", {"iid": iid, "intents": opp_intents(o),
                        "spec": spec_data,
                        "candidates": {cid: classify_candidate(ch, state)
                                       for cid, ch in chs.items()},
                        "stage": ST.get("stage")})
    pairs = []
    if ST.get("stage") == "ATTACK" and roid and not ST.get("attack_declared"):
        # attacker candidate whose object reference is the Reaper
        atk_cid = None
        for cid, ch in chs.items():
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and str(d.get("reference")) == str(roid) \
                        and d.get("role") == "attacker":
                    atk_cid = cid
        # defender candidate: the P1 player
        def_cid = None
        for cid, ch in chs.items():
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and d.get("role") == "attackTarget" \
                        and str(d.get("seat")) == str(p1):
                    def_cid = cid
        # validate against the advertised edges
        if atk_cid and def_cid:
            ok = any(e.get("sourceId") == atk_cid and def_cid in (e.get("targetIds") or [])
                     for e in spec_data.get("edges", []) or [])
            if ok:
                pairs = [(atk_cid, def_cid)]
                ST["attack_declared"] = True
                ST["attack_iid"] = iid
                say(f"[P0] attacking P1 with Time Reaper {roid}")
            else:
                wire("attack_edge_missing", {"atk_cid": atk_cid, "def_cid": def_cid})
    # empty pairs = declare no attackers
    await answer_relations(c, o, pairs, "declare_attackers")
    return True


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, (SWAMP,), (REAPER,)):
        return True
    if await do_bottom(c, pid, (SWAMP,), protect=(REAPER,)):
        return True
    if await do_discard(c, pid, (SWAMP,), protect=(REAPER,)):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True
    if await p0_declare_attackers(c, pid, state, st):
        return True

    stage = ST["stage"]
    p1 = ST["p1_seat"]

    if stage == "SETUP" and is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_land_drop(c, pid, state, acts, SWAMP):
            return True
        ex = exiled_owned(state, p1)
        ready = (
            len(untapped_lands(state, pid, SWAMP)) >= 5
            and find_hand(state, pid, REAPER)
            and len(ex) >= 1
            and life_of(state, 0) == 20
            and len(state.get("stack") or []) == 0
            and not ST.get("setup_exported")
            and (state.get("turn_number") or 99) <= 80
        )
        if ready:
            s = await export_now(c, "setup.json")
            if s is not None:
                ST["setup_exported"] = True
                wire("setup", {"exiled": [(oid, oname(o)) for oid, o in ex],
                               "untapped_swamps": len(untapped_lands(state, pid, SWAMP)),
                               "turn": state.get("turn_number")})
                say(f"[P0] setup exported: P1 exile has "
                    f"{[(oid, oname(o)) for oid, o in ex]}")
                ST["stage"] = "CAST"
            return True

    if stage == "CAST":
        if is_my_main(state, pid) and my_priority(state, pid):
            clear_inflight(state, pid)
            roid = bf_oid(state, pid, REAPER)
            if roid:
                ST["reaper_oid"] = str(roid)
                ST["stage"] = "ATTACK"
                say(f"[P0] Time Reaper on BF (oid {roid}); stage=ATTACK")
                return True
            if not ST.get("reaper_cast"):
                if len(untapped_lands(state, pid, SWAMP)) >= 5:
                    if await p_cast(c, pid, state, acts, REAPER, tag="reaper_oid"):
                        ST["reaper_cast"] = True
                        say("[P0] Time Reaper cast")
                        return True
            if await p_land_drop(c, pid, state, acts, SWAMP):
                return True

    if stage == "ATTACK":
        # wait until the declared attack leaves DeclareAttackers behind;
        # otherwise fall through and pass priority to reach combat
        if ST.get("attack_declared"):
            if not (wf_type(state) == "DeclareAttackers"
                    and str(wf_player(state)) == str(pid)):
                ST["stage"] = "TRIGGER"
                ST["trigger_stage_since"] = time.time()
                say("[P0] attack declared; stage=TRIGGER")
                return True

    if stage == "TRIGGER":
        if not ST.get("target_answered"):
            opps = target_opps(st)
            if not opps:
                if ST.get("trigger_stage_since") and \
                        time.time() - ST["trigger_stage_since"] > 240:
                    wire("trigger_prompt_timeout", {})
                    say("[P0] TRIGGER 240s with no target prompt; exporting pre as stuck state")
                    await export_now(c, "pre.json")
                    ST["pre_exported"] = True
                    ST["stop"] = True
                    return True
                # no prompt yet: fall through and pass priority so combat proceeds
            else:
                if await handle_trigger_opp(c, pid, state, st, opps, p1):
                    return True
        # answered (or no prompt yet): fall through to pass priority

    for a in acts:
        if a.get("type") == "DeclareBlockers":
            # legacy empty blockers (accepted on proto 98); P0 has no blocks
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            say("[P0] declares no blockers")
            return True
    if await pass_priority(c, pid):
        return True
    return False


# ---------------- P1 ----------------

async def p1_declare(c, pid, state, st):
    """P1 never attacks and never blocks: empty relations on declare waits,
    falling back to the legacy empty action."""
    wt = wf_type(state)
    if wt not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    opps = relations_opps(st)
    if opps:
        o, _, _ = opps[0]
        iid = o.get("interactionId") or o.get("id")
        if iid in ST["answered_iids"]:
            return False
        await answer_relations(c, o, [], "declare_empty_p1")
        return True
    for a in st.get("legal_actions", []) or []:
        if a.get("type") in ("DeclareAttackers", "DeclareBlockers"):
            sub = copy.deepcopy(a)
            if sub["type"] == "DeclareAttackers":
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
            else:
                sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            say(f"[P1] declares empty ({sub['type']}, legacy)")
            return True
    return False


async def p1_answer_swords(c, pid, state, st):
    """Answer the Swords to Plowshares target prompt via the relations
    schema, but only while the Swords spell is actually on the stack (never
    answer stale opportunities)."""
    if not stack_has(state, SWORDS):
        return False
    opps = [o for o in relations_opps(st, exclude_intents=("attack", "block"))
            if (o[0].get("interactionId") or o[0].get("id")) not in ST["answered_iids"]]
    if not opps:
        return False
    mem = bf_oid(state, pid, MEMNITE)
    if not mem:
        return False
    o, data, spec_data = opps[0]
    chs = {ch.get("id"): ch for ch in data.get("candidates", []) or []
           if isinstance(ch, dict)}
    offered = offered_target_ids(spec_data)
    mem_cid = None
    for cid in offered:
        ch = chs.get(cid)
        if ch is None:
            continue
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and str(d.get("reference")) == str(mem):
                mem_cid = cid
    if not mem_cid:
        return False
    src = next((e.get("sourceId") for e in spec_data.get("edges", []) or []
                if mem_cid in (e.get("targetIds") or [])), None)
    if not src:
        return False
    wire("swords_opp", {"iid": o.get("interactionId"),
                        "candidates": {cid: classify_candidate(ch, state)
                                       for cid, ch in chs.items()}})
    await answer_relations(c, o, [(src, mem_cid)], "swords_target")
    say(f"[P1] Swords target -> own Memnite {mem}")
    return True


async def p1_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, (PLAINS,), (SWORDS, MEMNITE)):
        return True
    if await do_bottom(c, pid, (PLAINS,), protect=(SWORDS, MEMNITE)):
        return True
    if await do_discard(c, pid, (PLAINS,), protect=(SWORDS, MEMNITE)):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True
    if await p1_declare(c, pid, state, st):
        return True
    if await p1_answer_swords(c, pid, state, st):
        return True

    if is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        ex_now = exiled_owned(state, pid)
        # 1) get exactly one Memnite on the board (no more: the Swords
        #    that follows needs a single legal target)
        if not ex_now and bf_oid(state, pid, MEMNITE) is None:
            if await p_cast(c, pid, state, acts, MEMNITE):
                return True
        # 2) exile our own Memnite face-up with Swords to Plowshares --
        #    only while it is the sole creature on the battlefield (the
        #    engine resolves the single-candidate target without a prompt)
        if not ex_now and not ST.get("swords_cast") \
                and not stack_has(state, SWORDS):
            mem = bf_oid(state, pid, MEMNITE)
            if mem and find_hand(state, pid, SWORDS) \
                    and len(untapped_lands(state, pid, PLAINS)) >= 1 \
                    and len(all_creatures(state)) == 1:
                if await p_cast(c, pid, state, acts, SWORDS):
                    ST["swords_cast"] = True
                    say(f"[P1] Swords to Plowshares cast targeting own Memnite {mem}")
                    return True
        if await p_land_drop(c, pid, state, acts, PLAINS):
            return True

    if await pass_priority(c, pid):
        return True
    return False


# ---------------- main ----------------

async def main():
    reset()
    t0 = time.time()
    os.environ["PHASE_WS_URL"] = "ws://127.0.0.1:9374/ws"
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    ST["p0_seat"] = p0.player_id
    ST["p1_seat"] = p1.player_id
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
                if ST.get("cast_in_flight"):
                    wire("cast_inflight_cleared_on_reject",
                         {"oid": ST["cast_in_flight"]})
                    ST["cast_in_flight"] = None
                    if ST.get("reaper_cast") and ST["stage"] == "CAST":
                        ST["reaper_cast"] = False
                        ST["reaper_oid"] = None
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
        record_stack(st["state"])
        state = st["state"]
        _p0, _p1 = p0.player_id, p1.player_id
        stage = ST["stage"]
        turn = state.get("turn_number") or 0
        stack = state.get("stack") or []

        for oid, o in exiled_owned(state, _p1):
            if oname(o) == MEMNITE and not ST.get("exiled_memnite_oid"):
                ST["exiled_memnite_oid"] = str(oid)
                wire("exiled_memnite", {"oid": str(oid), "object": o})
                say(f"[main] exiled Memnite tracked: oid={oid} "
                    f"face_down={o.get('face_down')}")

        if stage == "TRIGGER" and not ST.get("trigger_seen"):
            if stack_has(state, "Time Reaper"):
                ST["trigger_seen"] = True
                wire("trigger_on_stack", {"turn": turn})

        # post: trigger answered and trigger off the stack
        if stage == "TRIGGER" and ST.get("target_answered") \
                and not ST.get("post_exported"):
            if not stack_has(state, "Time Reaper"):
                s = await export_now(p0, "post.json")
                if s is not None:
                    ST["post_exported"] = True
                    wire("post", {"turn": turn,
                                  "life": [life_of(state, 0), life_of(state, 1)],
                                  "target_choice": ST.get("target_choice")})
                    say(f"[P0] post exported: trigger resolved "
                        f"(choice={ST.get('target_choice')}, turn {turn})")
                    ST["stage"] = "DONE"
                    ST["stop"] = True

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            say("game over")
            continue

        if now - ST.get("last_diag", 0) > 30:
            ST["last_diag"] = now
            hand0 = [oname(state["objects"][o]) for o in hand_oids(state, _p0)]
            p1st = p1.latest
            hand1 = ([oname(p1st["state"]["objects"][o]) for o in hand_oids(p1st["state"], _p1)]
                     if p1st else ["?"])
            ex = [(oid, oname(o)) for oid, o in exiled_owned(state, _p1)]
            say(f"DIAG turn={turn} phase={state.get('phase')} "
                f"wf={wf_type(state)}/p{wf_player(state)} stack={len(stack)} "
                f"life={[life_of(state,0), life_of(state,1)]} "
                f"p0hand={hand0[:6]} p1hand={hand1[:6]} exiledP1={ex} stage={stage}")
            wire("diag", {"turn": turn, "phase": state.get("phase"),
                          "wf": wf_type(state), "stack_n": len(stack),
                          "life": [life_of(state, 0), life_of(state, 1)],
                          "p0hand": hand0, "p1hand": hand1,
                          "exiledP1": ex, "stage": stage})

        if not ST.get("setup_exported") and now - t0 > 900:
            say("SETUP stalled 900s without setup export; stopping")
            wire("setup_stalled", {})
            ST["stop"] = True
        if not ST.get("post_exported") and now - t0 > TIMEOUT - 120:
            await export_now(p0, "post.json")
            ST["post_exported"] = True
            ST["stop"] = True

    await p0.close()
    await p1.close()
    return dict(ST)


if __name__ == "__main__":
    st = asyncio.run(main())
    print(json.dumps({k: (v if not isinstance(v, (dict, list, set)) else str(v)[:200])
                      for k, v in st.items()}, indent=2, default=str))
