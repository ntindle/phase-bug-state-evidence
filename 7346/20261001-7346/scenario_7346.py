#!/usr/bin/env python3
"""Issue #7346: Pain for All - "The aura entry did not deal damage to my
target (player)."

Oracle (pinned v0.99.0 card data):
  pain for all {2}{R} Enchantment - Aura: "Enchant creature you control
  When this Aura enters, enchanted creature deals damage equal to its power
  to any other target.
  Whenever enchanted creature is dealt damage, it deals that much damage to
  each opponent."

Parsed ETB trigger: ChangesZone -> DealDamage, amount Ref(Power(Anaphoric
"enchanted creature")), target Typed{Another}. The triage suspects `another`
is applied as an object-only filter, so a chosen PLAYER is not a legal
subject at resolution and the damage silently does nothing.

Run: 20261001-7346 on pinned v0.99.0 (build d919616, protocol 98).

Behavioral contract (native engine, P0 + P1 human driver seats):
  Leg 1 (reported case): P0 enchants its own Memnite (1/1) with Pain for
  All. ETB trigger: driver targets P1 (the player). Expect P1 life 20 -> 19.
  Leg 2 (control): P0 enchants a second Memnite with a second Pain for All.
  ETB trigger: driver targets P1's Memnite (a creature). Expect the
  creature to take 1 (1/1 dies -> P1 graveyard), P1 life unchanged.

  A1 setup_ok        pre.json: P0 main, priority, 2 memnites on P0 BF,
                    memnite on P1 BF, life 20/20, stack empty
  A2 aura1_attached  pain1 on P0 BF with attached_to == memA
  A3 player_offered   leg-1 ETB target prompt advertised P1 (player, seat 1)
                    as a candidate  <- KEY DIAGNOSTIC (triage: targeting bug
                    vs resolution bug)
  A4 damage_player    post1.json: P1 life == 19  <- REPORTED OUTCOME
  A5 creature_control post2.json: P1's memnite took the 1 (in P1 graveyard),
                    P1 life still 19, creature target was offered
  A6 cleanup         stack empty, game proceeding; rejections noted

Verdict: reproduced iff A1+A2 pass and (A3 fails or A4 fails).
not-reproduced iff A1-A5 pass. blocked iff A1 or A2 fails.

Card-name note: this engine's state reports Title-Case card names
("Pain for All", "Memnite", "Mountain"); the driver matches those.
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
RUN_ID = "20261001-7346"
ISSUE = 7346

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

PAIN = "Pain for All"
MEMNITE = "Memnite"
MOUNTAIN = "Mountain"
# Dense playsets are a test-harness convenience (the engine accepts
# >4-of for custom games); they make the setup draw-reliable.
P0_DECK = [(PAIN, 8), (MEMNITE, 16), (MOUNTAIN, 36)]
P1_DECK = [(MEMNITE, 8), (MOUNTAIN, 52)]
P0_LANDS = (MOUNTAIN,)
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
        "pre_exported": False, "mid1_exported": False, "post1_exported": False,
        "mid2_exported": False, "post2_exported": False,
        "rejections": [], "stop": False,
        "memA_oid": None, "memB_oid": None, "p1mem_oid": None,
        "pain1_oid": None, "pain2_oid": None,
        "p0_seat": None, "p1_seat": None,
        "hold_priority": False,
        "vi_wired": set(),
        "cast_in_flight": None,
        "etb1_answer_turn": None, "etb2_answer_turn": None,
        "etb1_stack_seen": False, "etb2_stack_seen": False,
        "aura1_cast": False, "aura2_cast": False,
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


def bf_oid(state, pid, name, exclude=()):
    for oid, o in bf(state, pid):
        if oname(o) == name and str(oid) not in {str(x) for x in exclude}:
            return oid
    return None


def bf_oids(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def is_tapped(state, oid):
    o = state["objects"].get(str(oid), {})
    return bool(o.get("tapped"))


def life_of(state, pid):
    players = state.get("players") or []
    return (players[pid] or {}).get("life") if len(players) > pid else None


def untapped_lands(state, pid, land_name):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == land_name and not o.get("tapped")]


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


async def do_mulligan(c, pid, lands, key_names, need_counts=None):
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
    needs_met = all(sum(1 for n in hand_names if n == nm) >= cnt
                    for nm, cnt in (need_counts or {}).items())
    n_mulls = _MULL_REV.get(c.name, 0)
    keep_ok = (n_lands >= 2 and has_key and needs_met) or n_mulls >= 2 or \
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
    """Order hand oids for discard/bottom: first_names first, then anything,
    then last_names last (protected)."""
    h = hand_oids(state, pid)
    def rank(o):
        n = oname(state["objects"][o])
        if n in first_names:
            return 0
        if n in last_names:
            return 2
        return 1
    return sorted(h, key=rank)


async def do_bottom(c, pid, lands, key_names, protect=()):
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
    # bottom lands first, protect the named threats (Memnite) for last
    picks = [int(x) for x in _discard_order(state, pid, lands, protect)[:n]]
    _MULL_REV[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid, lands, key_names, protect=()):
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


def _is_menu_opp(opp):
    data = (opp.get("response") or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    if not chs:
        return True
    return any("castSpell" in action_codes(ch) or "passPriority" in action_codes(ch)
               for ch in chs if isinstance(ch, dict))


def match_player1(ch):
    """A choice that is the player P1 (seat 1)."""
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "player" and (s.get("data") or {}).get("seat") == 1:
            return True
    blob = json.dumps(ch, default=str)
    return (('"seat": 1' in blob or '"seat":1' in blob)
            and 'player' in blob.lower())


def match_oid(want):
    def f(ch):
        return str(want) in json.dumps(ch, default=str)
    return f


async def answer_target(c, pid, st, match_fn, tag):
    """Answer the first non-menu target opportunity in this stage whose
    choice matches match_fn. Wires the FULL prompt (all candidates) once."""
    if ST.get(tag + "_answered"):
        return False
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs or _is_menu_opp(opp):
            continue
        key = tag + "_prompt"
        if key not in ST["vi_wired"]:
            ST["vi_wired"].add(key)
            wire(tag + "_prompt_seen", {"opp": opp, "n": len(chs),
                                        "stage": ST.get("stage")})
            say(f"[{tag}] target prompt seen: {len(chs)} candidates")
        for ch in chs:
            if isinstance(ch, dict) and match_fn(ch):
                res = submit_vi(c, opp, ch)
                if not res:
                    wire(tag + "_vi_unusable", {"choice": ch})
                    continue
                sub, iid, cid, stype = res
                wire(tag + "_submit", {"iid": iid, "cid": cid, "stype": stype,
                                      "choice": ch})
                await c.send_interaction(sub)
                ST[tag + "_answered"] = True
                ST[tag + "_answer_turn"] = st["state"].get("turn_number")
                say(f"[{tag}] answered target (stype={stype}, cid={cid})")
                return True
    return False


def aura_attached_to(state, pain_oid, mem_oid):
    p = state["objects"].get(str(pain_oid), {})
    att = p.get("attached_to") or {}
    return str(att.get("data")) == str(mem_oid)


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


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, P0_LANDS, (PAIN, MEMNITE),
                         need_counts={MEMNITE: 1}):
        return True
    if await do_bottom(c, pid, P0_LANDS, (PAIN, MEMNITE), protect=(MEMNITE,)):
        return True
    if await do_discard(c, pid, P0_LANDS, (PAIN, MEMNITE), protect=(MEMNITE,)):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True

    stage = ST["stage"]

    if stage == "SETUP" and is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast(c, pid, state, acts, MEMNITE):
            return True
        if await p_land_drop(c, pid, state, acts, MOUNTAIN):
            return True
        mems = bf_oids(state, pid, MEMNITE)
        if len(mems) >= 1 and not ST.get("memA_oid"):
            ST["memA_oid"] = mems[0]
        if len(mems) >= 2 and not ST.get("memB_oid"):
            ST["memB_oid"] = mems[1]
        p1mem = bf_oid(state, 1, MEMNITE)
        if p1mem:
            ST["p1mem_oid"] = p1mem
        stack_empty = len(state.get("stack") or []) == 0
        ready = (len(mems) >= 2 and p1mem
                 and len(untapped_lands(state, pid, MOUNTAIN)) >= 3
                 and find_hand(state, pid, PAIN)
                 and life_of(state, 0) == 20 and life_of(state, 1) == 20
                 and stack_empty and not ST.get("pre_exported")
                 and (state.get("turn_number") or 99) <= 60)
        if ready:
            s = await export_now(c, "pre.json")
            if s is not None:
                ST["pre_exported"] = True
                wire("pre", {"memA": ST["memA_oid"], "memB": ST["memB_oid"],
                             "p1mem": ST["p1mem_oid"],
                             "turn": state.get("turn_number"),
                             "phase": state.get("phase")})
                say(f"[P0] pre exported (memA {ST['memA_oid']}, "
                    f"memB {ST['memB_oid']}, p1mem {ST['p1mem_oid']})")
                ST["stage"] = "AURA1"
            return True

    if stage == "AURA1":
        memA = ST.get("memA_oid")
        # answer the aura's enchant-target prompt first (cast-time target);
        # the prompt can arrive outside a Priority wait, so don't gate it.
        if ST.get("aura1_cast") and memA:
            if await answer_target(c, pid, st, match_oid(memA), "ench1"):
                return True
        if is_my_main(state, pid) and my_priority(state, pid):
            clear_inflight(state, pid)
            if not ST.get("aura1_cast"):
                if len(untapped_lands(state, pid, MOUNTAIN)) >= 3:
                    if await p_cast(c, pid, state, acts, PAIN, tag="aura1_cast"):
                        return True
            if await p_land_drop(c, pid, state, acts, MOUNTAIN):
                return True

    if stage == "TRIGGER1":
        # answer the ETB trigger's "any other target" prompt with P1
        if not ST.get("etb1_answered"):
            if await answer_target(c, pid, st, match_player1, "etb1"):
                return True

    if stage == "AURA2":
        memB = ST.get("memB_oid")
        if ST.get("aura2_cast") and memB:
            if await answer_target(c, pid, st, match_oid(memB), "ench2"):
                return True
        if is_my_main(state, pid) and my_priority(state, pid):
            clear_inflight(state, pid)
            if not ST.get("aura2_cast"):
                if len(untapped_lands(state, pid, MOUNTAIN)) >= 3:
                    if await p_cast(c, pid, state, acts, PAIN, tag="aura2_cast"):
                        return True
            if await p_land_drop(c, pid, state, acts, MOUNTAIN):
                return True

    if stage == "TRIGGER2":
        p1mem = ST.get("p1mem_oid")
        if p1mem and not ST.get("etb2_answered"):
            if await answer_target(c, pid, st, match_oid(p1mem), "etb2"):
                return True

    # never attack; never block
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


async def p1_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid, P1_LANDS, (MEMNITE,)):
        return True
    if await do_bottom(c, pid, P1_LANDS, (MEMNITE,), protect=(MEMNITE,)):
        return True
    if await do_discard(c, pid, P1_LANDS, (MEMNITE,), protect=(MEMNITE,)):
        return True
    if await handle_mana_payment(c, pid, state, st):
        return True
    if is_my_main(state, pid) and my_priority(state, pid):
        clear_inflight(state, pid)
        if await p_cast(c, pid, state, acts, MEMNITE):
            return True
        if await p_land_drop(c, pid, state, acts, MOUNTAIN):
            return True
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
            return True
    if await pass_priority(c, pid):
        return True
    return False


async def main():
    reset()
    t0 = time.time()
    os.environ["PHASE_WS_URL"] = "ws://127.0.0.1:9376/ws"
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

    def pain_oid(state, n):
        pains = [oid for oid, o in bf(state, 0)
                 if oname(o) == PAIN]
        pains.sort(key=int)
        return pains[n] if len(pains) > n else None

    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, _ in clients:
            rej = drain_rejections(c)
            if rej:
                ST["rejections"].extend(
                    {"at": now, "who": c.name, "type": r["type"], "data": r["data"]}
                    for r in rej)
                # A rejected cast/target submission: clear optimistic flags so
                # the driver retries instead of stalling on in-flight state.
                if ST.get("cast_in_flight"):
                    wire("cast_inflight_cleared_on_reject",
                         {"oid": ST["cast_in_flight"]})
                    ST["cast_in_flight"] = None
                    for _t in ("aura1_cast", "aura2_cast"):
                        if ST.get(_t):
                            ST[_t] = None
                for tag in ("ench1", "etb1", "ench2", "etb2"):
                    if ST.get(tag + "_answered"):
                        ST[tag + "_answered"] = False
                        wire(tag + "_flag_cleared_on_reject", {})
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
        stack = state.get("stack") or []

        # AURA1 -> TRIGGER1: first Pain for All on P0 BF attached to memA
        if stage == "AURA1":
            p1o = pain_oid(state, 0)
            if p1o and aura_attached_to(state, p1o, ST.get("memA_oid")):
                ST["pain1_oid"] = p1o
                wire("pain1_attached", {"pain": p1o, "memA": ST["memA_oid"],
                                        "turn": turn})
                say(f"pain1 {p1o} attached to memA {ST['memA_oid']}")
                ST["stage"] = "TRIGGER1"
                stage = "TRIGGER1"
        # TRIGGER1: answer prompt, capture mid1, then post1 after resolution
        if stage == "TRIGGER1":
            if stack and not ST.get("etb1_stack_seen"):
                ST["etb1_stack_seen"] = True
                wire("etb1_stack", {"stack": stack, "turn": turn})
            if ST.get("etb1_answered") and not ST.get("mid1_exported"):
                s = await export_now(p0, "mid1.json")
                if s is not None:
                    ST["mid1_exported"] = True
                    wire("mid1", {"turn": turn, "stack_n": len(stack),
                                  "life": [life_of(state, 0), life_of(state, 1)]})
            if ST.get("etb1_answered") and not ST.get("post1_exported") \
                    and len(stack) == 0 and turn > (ST.get("etb1_answer_turn") or 0):
                s = await export_now(p0, "post1.json")
                if s is not None:
                    ST["post1_exported"] = True
                    ps = json.loads(s)["state"]
                    wire("post1", {"life_p1": life_of(ps, 1), "turn": turn})
                    say(f"post1: P1 life={life_of(ps, 1)} (turn {turn})")
                    ST["stage"] = "AURA2"
                    stage = "AURA2"
            if ST.get("etb1_answered") and not ST.get("post1_exported") \
                    and now - t0 > 900 and len(stack) == 0:
                # watchdog: answer given long ago, stack empty, turn stuck
                s = await export_now(p0, "post1.json")
                if s is not None:
                    ST["post1_exported"] = True
                    ST["stage"] = "AURA2"
                    stage = "AURA2"
                    wire("post1_watchdog", {"turn": turn})
        # AURA2 -> TRIGGER2: second Pain for All attached to memB
        if stage == "AURA2":
            p2o = pain_oid(state, 1)
            if p2o and aura_attached_to(state, p2o, ST.get("memB_oid")):
                ST["pain2_oid"] = p2o
                wire("pain2_attached", {"pain": p2o, "memB": ST["memB_oid"],
                                        "turn": turn})
                say(f"pain2 {p2o} attached to memB {ST['memB_oid']}")
                ST["stage"] = "TRIGGER2"
                stage = "TRIGGER2"
        # TRIGGER2: answer with P1's creature, capture mid2/post2
        if stage == "TRIGGER2":
            if stack and not ST.get("etb2_stack_seen"):
                ST["etb2_stack_seen"] = True
                wire("etb2_stack", {"stack": stack, "turn": turn})
            if ST.get("etb2_answered") and not ST.get("mid2_exported"):
                s = await export_now(p0, "mid2.json")
                if s is not None:
                    ST["mid2_exported"] = True
                    wire("mid2", {"turn": turn, "stack_n": len(stack)})
            if ST.get("etb2_answered") and not ST.get("post2_exported") \
                    and len(stack) == 0 and turn > (ST.get("etb2_answer_turn") or 0):
                s = await export_now(p0, "post2.json")
                if s is not None:
                    ST["post2_exported"] = True
                    wire("post2", {"turn": turn})
                    say(f"post2 exported (turn {turn})")
                ST["stage"] = "DONE"
                ST["stop"] = True
            if ST.get("etb2_answered") and not ST.get("post2_exported") \
                    and now - t0 > 1200 and len(stack) == 0:
                s = await export_now(p0, "post2.json")
                if s is not None:
                    ST["post2_exported"] = True
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
            say(f"DIAG turn={turn} phase={state.get('phase')} "
                f"wf={wf_type(state)}/p{wf_player(state)} stack={len(stack)} "
                f"life={[life_of(state,0), life_of(state,1)]} "
                f"p0hand={hand0} p1hand={hand1} p0bf={bf0} p1bf={bf1} stage={stage}")
            wire("diag", {"turn": turn, "phase": state.get("phase"),
                          "wf": wf_type(state), "stack_n": len(stack),
                          "life": [life_of(state, 0), life_of(state, 1)],
                          "p0hand": hand0, "p1hand": hand1, "p0bf": bf0, "p1bf": bf1,
                          "stage": stage})

        if not ST.get("pre_exported") and now - t0 > 600:
            say("SETUP stalled 600s without pre export; stopping")
            wire("setup_stalled", {})
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
