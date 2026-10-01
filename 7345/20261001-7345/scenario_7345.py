#!/usr/bin/env python3
"""Issue #7345: Pariah's Shield - "Just took 7 damage in combat and I'm pretty
sure I'm supposed to take 0 -> It's all supposed to go to my creature instead."

Oracle (pinned v0.99.0 card data):
  pariah's shield {5} Artifact - Equipment: "All damage that would be dealt
  to you is dealt to equipped creature instead. Equip {3}"

Run: 20261001-7345 on pinned v0.99.0 (build d919616, protocol 98).

Behavioral contract (native engine, P0 + P1 human driver seats):
  Leg 1 (baseline control): P1 attacks P0 with a 7-power Nyxborn Brute
  while the Shield is NOT equipped. Expect P0 life 20 -> 13 (7 unblocked).
  Leg 2 (reported case): P0 casts pariah's shield, equips it to its memnite,
  then P1 attacks again with the kilnbeast. Expect P0 life unchanged (13)
  and the 7 damage redirected to the memnite (1/1 -> dies).

  A1 setup_ok        pre.json: P0 main, memnite on P0 BF, kilnbeast on P1 BF,
                    life 20/20, stack empty
  A2 baseline_damage post1.json: P0 life == 13 after the unblocked 7-power
                    attack (proves the test setup actually deals 7)
  A3 shield_equipped mid2.json: shield on P0 BF attached to the memnite
  A4 redirect       post2.json: P0 life still 13 AND memnite took the 7
                    (in P0 graveyard or marked damage 7)  <- REPORTED OUTCOME
  A5 attacker_intact post2.json: Brute still on P1 BF (attack happened)
  A6 cleanup        post2.json: stack empty, game proceeding; rejections noted

Verdict: reproduced iff A1+A2+A3 pass and A4 fails (P0 took combat damage
despite the equipped Shield). not-reproduced iff A1-A5 pass. blocked iff A1
fails, or A2/A3 fail (baseline damage or equip is a prerequisite for testing
the reported replacement effect).

Card-name note: this engine's card-data keys are lowercase ("pariah's
shield", "plated kilnbeast", "memnite", "plains", "mountain"); the driver
uses those exact strings.
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
RUN_ID = "20261001-7345"
ISSUE = 7345

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

SHIELD = "Pariah's Shield"
MEMNITE = "Memnite"
PLAINS = "Plains"
BEAST = "Nyxborn Brute"
MOUNTAIN = "Mountain"
# Dense threat playsets are a test-harness convenience (the engine accepts
# >4-of for custom games); they make the setup draw-reliable.
P0_DECK = [(SHIELD, 8), (MEMNITE, 8), (PLAINS, 44)]
P1_DECK = [(BEAST, 16), (MOUNTAIN, 44)]
P0_LANDS = (PLAINS,)
P1_LANDS = (MOUNTAIN,)
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
        "pre_exported": False, "post1_exported": False,
        "mid2_exported": False, "post2_exported": False,
        "rejections": [], "stop": False,
        "memnite_oid": None, "shield_oid": None, "beast_oid": None,
        "p0_seat": None, "p1_seat": None,
        "attack1_pending": False, "attack1_done": False, "attack1_turn": None,
        "attack2_pending": False, "attack2_done": False, "attack2_turn": None,
        "equip_submitted": False, "equip_targeted": False, "equip_attached": False,
        "hold_priority": False,
        "vi_wired": set(),
        "no_equip_observations": 0,
        "life_after_attack1": None,
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


def bf_oid(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def is_tapped(state, oid):
    o = state["objects"].get(str(oid), {})
    return bool(o.get("tapped"))


def life_of(state, pid):
    players = state.get("players") or []
    return (players[pid] or {}).get("life") if len(players) > pid else None


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
    n_lands = sum(1 for o in hand_oids(state, pid)
                  if oname(state["objects"][o]) in lands)
    has_key = any(oname(state["objects"][o]) in key_names
                  for o in hand_oids(state, pid))
    n_mulls = _MULL_REV.get(c.name, 0)
    keep_ok = (n_lands >= 2 and has_key) or n_mulls >= 2 or \
        (n_lands >= 3 and n_mulls >= 1)
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        _MULL_REV[c.name] = n_mulls + 1
    _MULL_REV[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice}")
    return True


async def do_bottom(c, pid, lands, key_names):
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
    keep_names = set(key_names) | set(lands)
    pref = [o for o in h if oname(state["objects"][o]) not in keep_names]
    pref += [o for o in h if o not in pref]
    picks = [int(x) for x in pref[:n]]
    _MULL_REV[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid, lands, key_names):
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
    keep_names = set(key_names) | set(lands)
    pref = [o for o in h if oname(state["objects"][o]) not in keep_names]
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


def vi_opp_referencing(st, *oids):
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        if any("castSpell" in action_codes(ch) or "passPriority" in action_codes(ch)
               for ch in chs if isinstance(ch, dict)):
            continue
        blob = json.dumps(opp, default=str)
        if all(str(o) in blob for o in oids):
            return opp, data
    return None


def submit_vi(c, opp, ch):
    iid = opp.get("interactionId") or opp.get("id")
    cid = ch.get("id") or ch.get("choiceId")
    spec = ((opp.get("response") or {}).get("data") or {}).get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else None
    if not iid or cid is None:
        return None
    if stype == "sequence":
        sub = {"interactionId": iid,
               "response": {"type": "sequence", "data": {"choiceIds": [cid]}}}
    elif stype == "select":
        sub = {"interactionId": iid,
               "response": {"type": "select", "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    return sub, iid, cid, stype


async def answer_equip_target(c, pid, state, st):
    """Answer the Equip target prompt: choose the memnite."""
    if ST.get("equip_targeted"):
        return False
    mem = ST.get("memnite_oid")
    if not mem:
        return False
    found = vi_opp_referencing(st, mem)
    if not found:
        return False
    opp, data = found
    chs = data.get("choices") or data.get("candidates") or []
    pick = None
    for chx in chs:
        if isinstance(chx, dict) and str(mem) in json.dumps(chx, default=str):
            if "castSpell" in action_codes(chx) or "passPriority" in action_codes(chx):
                continue
            pick = chx
            break
    if pick is None:
        wire("equip_target_no_memnite_choice", {})
        return False
    res = submit_vi(c, opp, pick)
    if not res:
        wire("equip_target_vi_unusable", {})
        return False
    sub, iid, cid, stype = res
    wire("equip_target_submit", {"iid": iid, "cid": cid, "stype": stype,
                                 "target": mem})
    await c.send_interaction(sub)
    ST["equip_targeted"] = True
    say(f"[P0] equip target prompt answered -> memnite {mem} (stype={stype})")
    return True


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


async def p_cast(c, pid, state, acts, name):
    if bf_oid(state, pid, name):
        return False
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
            return True
    return False


def clear_inflight(state, pid):
    cif = ST.get("cast_in_flight")
    if cif and not any(str(oid) == str(cif) for oid in hand_oids(state, pid)):
        ST["cast_in_flight"] = None
        wire("cast_left_hand", {"oid": cif})
        say(f"cast in-flight {cif} left hand")


def wire_vi_snapshot(c, pid, st, why):
    vi = get_vi(st)
    if not vi:
        return
    key = why + ":" + json.dumps(vi, default=str)[:200]
    if key in ST["vi_wired"]:
        return
    ST["vi_wired"].add(key)
    wire("p0_vi", {"why": why, "wf": wf_type(st["state"]), "vi": vi,
                   "stage": ST.get("stage")})
    say(f"[P0] vi ({why}) wf={wf_type(st['state'])} stage={ST.get('stage')}")


def shield_attached_to(state, shield_oid, mem_oid):
    """Check whether the Shield object records attachment to the memnite.
    Tries several field shapes; wires the object on first sight for diagnosis."""
    sh = state["objects"].get(str(shield_oid))
    if not sh:
        return False
    key = f"shieldobj:{shield_oid}"
    if key not in ST["vi_wired"]:
        ST["vi_wired"].add(key)
        wire("shield_object", {"oid": shield_oid, "object": sh})
        say(f"[P0] shield object wired for attachment inspection")
    blob = json.dumps(sh, default=str)
    # direct attached_to field
    for f in ("attached_to", "attachedTo", "equipped_to", "attached"):
        v = sh.get(f)
        if v is not None and str(v) == str(mem_oid):
            return True
        if isinstance(v, (list, tuple)) and str(mem_oid) in [str(x) for x in v]:
            return True
    # attachment recorded on the creature side
    mem = state["objects"].get(str(mem_oid), {})
    for f in ("equipment", "attached_equipment", "attachments", "auras_and_equipment"):
        v = mem.get(f)
        if v is not None and str(shield_oid) in json.dumps(v, default=str):
            return True
    return False


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, P0_LANDS, (SHIELD, MEMNITE)):
        return True
    if await do_bottom(c, pid, P0_LANDS, (SHIELD, MEMNITE)):
        return True
    if await do_discard(c, pid, P0_LANDS, (SHIELD, MEMNITE)):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True

    stage = ST["stage"]
    if stage == "SETUP" and is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast(c, pid, state, acts, MEMNITE):
            return True
        if await p_cast(c, pid, state, acts, SHIELD):
            return True
        if await p_land_drop(c, pid, state, acts, PLAINS):
            return True
        mem = bf_oid(state, pid, MEMNITE)
        if mem:
            ST["memnite_oid"] = mem
        beast = bf_oid(state, 1, BEAST)
        if beast:
            ST["beast_oid"] = beast
        stack_empty = len(state.get("stack") or []) == 0
        ready = (mem and beast
                 and life_of(state, 0) == 20 and life_of(state, 1) == 20
                 and stack_empty and not ST.get("pre_exported")
                 and (state.get("turn_number") or 99) <= 13
                 and not ST.get("attack1_pending") and not ST.get("attack1_done"))
        if ready:
            s = await export_now(c, "pre.json")
            if s is not None:
                ST["pre_exported"] = True
                wire("pre", {"memnite_oid": mem, "beast_oid": beast,
                             "turn": state.get("turn_number"),
                             "phase": state.get("phase")})
                say(f"[P0] pre exported (memnite {mem}, beast {beast})")
            return True
    if stage == "EQUIP" and is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        mem = ST.get("memnite_oid") or bf_oid(state, pid, MEMNITE)
        if mem:
            ST["memnite_oid"] = mem
        shield = bf_oid(state, pid, SHIELD)
        if shield:
            ST["shield_oid"] = shield
        if not shield:
            if await p_cast(c, pid, state, acts, SHIELD):
                return True
            if await p_land_drop(c, pid, state, acts, PLAINS):
                return True
        else:
            if get_vi(st) and str(wf_player(state)) == str(pid):
                if await answer_equip_target(c, pid, state, st):
                    return True
                wire_vi_snapshot(c, pid, st, "equip_unanswered")
            if not ST.get("equip_submitted"):
                aa = [a for a in acts
                      if a.get("type") == "ActivateAbility"
                      and str((a.get("data") or {}).get("source_id")) == str(shield)]
                if aa:
                    wire("equip_activate_offered", {"action": aa[0]})
                    say("[P0] EQUIP: ActivateAbility (equip) offered - submitting")
                    await submit_as_is(c, aa[0])
                    ST["equip_submitted"] = True
                    return True
                ST["no_equip_observations"] = ST.get("no_equip_observations", 0) + 1
                if ST["no_equip_observations"] >= 6:
                    wire("no_equip_offer", {
                        "flat_types": sorted(set(x.get("type") for x in acts))})
                    say("[P0] EQUIP: ActivateAbility NOT offered after 6 observations - BLOCKED")
                    ST["hold_priority"] = True
                    try:
                        await export_now(c, "post2.json")
                    finally:
                        ST["hold_priority"] = False
                    ST["post2_exported"] = True
                    ST["equip_blocked"] = True
                    ST["stop"] = True
                    return True
            if await p_land_drop(c, pid, state, acts, PLAINS):
                return True
    # P0 never attacks; declare no blockers when defending
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a.get("type") == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            say("[P0] declares no blockers")
            return True
    if await pass_priority(c, pid):
        return True
    return False


_P1_ACTS_WIRED = set()

async def p1_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, P1_LANDS, (BEAST,)):
        return True
    if await do_bottom(c, pid, P1_LANDS, (BEAST,)):
        return True
    if await do_discard(c, pid, P1_LANDS, (BEAST,)):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True
    if is_my_main(state, pid) and my_priority(state, pid):
        key = (state.get("turn_number"), c.revision)
        if key not in _P1_ACTS_WIRED:
            _P1_ACTS_WIRED.add(key)
            wire("p1_acts", {"turn": state.get("turn_number"),
                             "flat_types": sorted(set(x.get("type") for x in acts)),
                             "hand": [oname(state["objects"][o]) for o in hand_oids(state, pid)],
                             "castspell_oids": [str((x.get("data") or {}).get("object_id")) for x in acts if x.get("type") == "CastSpell"]})
        clear_inflight(state, pid)
        if await p_cast(c, pid, state, acts, BEAST):
            return True
        if await p_land_drop(c, pid, state, acts, MOUNTAIN):
            return True
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            sub = copy.deepcopy(a)
            beast = ST.get("beast_oid") or bf_oid(state, pid, BEAST)
            if beast:
                ST["beast_oid"] = beast
            want = False
            n = 0
            turn = state.get("turn_number") or 0
            if beast and not is_tapped(state, beast):
                if not ST.get("attack1_done") and not ST.get("attack1_pending") and turn >= 4:
                    want = True
                    n = 1
                elif (ST.get("attack1_done") and ST.get("mid2_exported")
                        and not ST.get("attack2_done") and not ST.get("attack2_pending")):
                    want = True
                    n = 2
            if want:
                sub["data"]["attacks"] = [[int(beast), {"type": "Player",
                                                  "data": int(ST.get("p0_seat", 0))}]]
                sub["data"]["bands"] = []
                wire("attack_declared", {"n": n, "attacker": beast, "turn": turn})
                say(f"[P1] attack{n} declared with kilnbeast {beast} (turn {turn})")
                if n == 1:
                    ST["attack1_pending"] = True
                    ST["attack1_turn"] = turn
                else:
                    ST["attack2_pending"] = True
                    ST["attack2_turn"] = turn
            else:
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    if await pass_priority(c, pid):
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
                # A rejected equip target submission: clear the optimistic
                # flag so the driver re-answers the still-pending prompt.
                if ST.get("stage") == "EQUIP":
                    ST["equip_targeted"] = False
                    wire("equip_prompt_flag_cleared", {"n_rej": len(rej)})
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
        _p0, _p1 = p0.player_id, p1.player_id
        stage = ST["stage"]
        turn = state.get("turn_number") or 0

        # ---- attack 1 (baseline) checkpoint ----
        if ST.get("attack1_pending") and not ST.get("post1_exported"):
            if turn > (ST.get("attack1_turn") or 0):
                ST["hold_priority"] = True
                try:
                    s = await export_now(p0, "post1.json")
                    if s is not None:
                        ST["post1_exported"] = True
                        ST["attack1_pending"] = False
                        ST["attack1_done"] = True
                        ps = json.loads(s)["state"]
                        ST["life_after_attack1"] = life_of(ps, _p0)
                        wire("post1", {"life_p0": ST["life_after_attack1"],
                                       "turn": turn})
                        say(f"post1: baseline attack resolved, P0 life="
                            f"{ST['life_after_attack1']}")
                        ST["stage"] = "EQUIP"
                        stage = "EQUIP"
                finally:
                    ST["hold_priority"] = False
        # ---- equip checkpoint ----
        if stage == "EQUIP" and ST.get("shield_oid") and ST.get("memnite_oid") \
                and not ST.get("mid2_exported") and not ST.get("equip_blocked"):
            if shield_attached_to(state, ST["shield_oid"], ST["memnite_oid"]):
                ST["hold_priority"] = True
                try:
                    s = await export_now(p0, "mid2.json")
                    if s is not None:
                        ST["mid2_exported"] = True
                        ST["equip_attached"] = True
                        wire("mid2", {"shield": ST["shield_oid"],
                                      "memnite": ST["memnite_oid"], "turn": turn})
                        say(f"mid2: shield equipped to memnite (turn {turn})")
                        ST["stage"] = "ATTACK2"
                        stage = "ATTACK2"
                finally:
                    ST["hold_priority"] = False
        # ---- attack 2 (reported case) checkpoint ----
        if ST.get("attack2_pending") and not ST.get("post2_exported"):
            if turn > (ST.get("attack2_turn") or 0):
                ST["hold_priority"] = True
                try:
                    s = await export_now(p0, "post2.json")
                    if s is not None:
                        ST["post2_exported"] = True
                        ST["attack2_pending"] = False
                        ST["attack2_done"] = True
                        ps = json.loads(s)["state"]
                        wire("post2", {"life_p0": life_of(ps, _p0),
                                       "turn": turn})
                        say(f"post2: equipped-shield attack resolved, P0 life="
                            f"{life_of(ps, _p0)} (turn {turn})")
                finally:
                    ST["hold_priority"] = False
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
            bf0 = [(oname(o), o.get("tapped")) for _, o in bf(state, _p0)]
            bf1 = [(oname(o), o.get("tapped")) for _, o in bf(state, _p1)]
            stack = state.get("stack") or []
            say(f"DIAG turn={turn} phase={state.get('phase')} "
                f"wf={wf_type(state)}/p{wf_player(state)} stack={len(stack)} "
                f"life={[life_of(state,0), life_of(state,1)]} "
                f"p0hand={hand0} p1hand={hand1} p0bf={bf0} p1bf={bf1} stage={stage}")
            wire("diag", {"turn": turn, "phase": state.get("phase"),
                          "wf": wf_type(state), "stack_n": len(stack),
                          "life": [life_of(state, 0), life_of(state, 1)],
                          "p0hand": hand0, "p1hand": hand1, "p0bf": bf0, "p1bf": bf1,
                          "stage": stage})

        # stall guards
        if not ST.get("pre_exported") and now - t0 > 600:
            say("SETUP stalled 600s without pre export; stopping")
            wire("setup_stalled", {})
            ST["stop"] = True
        if ST.get("stage") == "EQUIP" and not ST.get("mid2_exported") \
                and not ST.get("equip_blocked") and now - t0 > 1200:
            say("EQUIP stalled; exporting post2 and stopping")
            ST["hold_priority"] = True
            try:
                await export_now(p0, "post2.json")
            finally:
                ST["hold_priority"] = False
            ST["post2_exported"] = True
            ST["stop"] = True
        if not ST.get("post2_exported") and now - t0 > TIMEOUT - 120:
            await export_now(p0, "post2.json")
            ST["post2_exported"] = True
            ST["stop"] = True

    await p0.close()
    await p1.close()
    return dict(ST)


if __name__ == "__main__":
    st = asyncio.run(main())
    print(json.dumps({k: (v if not isinstance(v, (dict, list, set)) else str(v)[:200])
                      for k, v in st.items()}, indent=2, default=str))
