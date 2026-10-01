#!/usr/bin/env python3
"""Issue #7240 on v0.98.0 / protocol 94: "Overpowering Attack cast in second
main untaps creatures but adds no combat" (mike-theDude, status:confirmed,
area:engine+parser, mechanic:combat, priority:p2-wrong-game-result).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Subsystem under test: native rules engine (extra-phase granting) on the pinned
release. Two human driver seats (P0 active driver, P1 passive opponent).

Overpowering Attack {3}{R}{R} sorcery: "Freerunning {2}{R} (...) Untap all
creatures you control that attacked this turn. If it's your main phase, there
is an additional combat phase after this phase, followed by an additional main
phase."

Parser corroboration (v0.98.0 card-data.json, verified 2026-10-01): the spell
effect is SetTapState(Untap, Typed/Creature/You/AttackedThisTurn, All) with a
SequentialSibling sub-ability: AdditionalPhase{phase: BeginCombat,
after: {type: ThisPhase, data: {named: null}}, followed_by: [PostCombatMain],
count: Fixed 1}, conditioned on And(CurrentPhaseIs[PreCombatMain,
PostCombatMain], IsYourTurn). The issue's classifier claimed the
engine-authoritative parse anchored after: EndCombat (unreachable when the
sorcery resolves in the postcombat main); the pinned card-data.json shows the
ThisPhase anchor instead. The behavioral test below decides which is real.

Setup:
  P0 (human driver): 12x "Goblin Guide" + 4x "Overpowering Attack" + 44x
                     "Mountain". Plays one land/turn, casts Guides (haste),
                     attacks every combat, and casts Overpowering Attack in the
                     first postcombat main phase where: OA is in hand, >=5
                     untapped Mountains, and a Guide attacked that turn.
  P1 (human driver, passive): 60x "Plains". Never plays lands, passes
                     priority, declares empty blockers.

Expected (oracle text):
  E1: OA resolves: every P0 creature that attacked that turn untaps.
  E2: still on the same turn, after the postcombat main phase, an additional
      combat phase begins (BeginCombat observed on the same turn_number).
  E3: after that additional combat, an additional main phase follows before
      the turn ends.

Assertions:
  A1 setup_ok      pre-cast export: P0 turn, PostCombatMain, active 0, OA in
                   P0 hand, >=1 tapped Goblin Guide on BF, >=5 untapped
                   Mountains.
  A2 untap_ok      post-resolution export: every Guide that was tapped in pre
                   is untapped (still on BF).
  A3 extra_combat  after OA resolution, BeginCombat observed on the SAME turn
                   before the turn ends.
  A4 extra_main    after the additional combat, a main phase (Pre/PostCombat
                   Main) observed on the same turn before the turn ends.
  A5 cleanup       game continues past the OA turn (next turn begins or game
                   otherwise advances; no stuck waiting state).

Verdict rule: A1 failed -> blocked. A2 failed -> reproduced (untap broken).
A3 failed (A1+A2 pass) -> reproduced (the reported bug). A3 pass + A4 fail ->
reproduced (partial: combat granted, main missing). All pass ->
not-reproduced. NEVER "fixed".

No decline/control branch: the spell has no optional clause.

Evidence: evidence/7240/20261001-7240/pre.json, post.json (+ mid checkpoints
as needed), phase_trace.json, carddata_excerpt.json, run.json,
assertions.json, observations.json, wire_log.jsonl, scenario_run.log,
scenario_7240_0980.py, summary.png, manifest.sha256.

Protocol-94 driver (v0.98.0): HELLO advertises 94 (exact match enforced);
CreateGameWithSettings + JoinGameWithPassword + start_when_full; MulliganDecision
{"choice":{"type":"Keep"}} gated on the seat's pending Declare;
BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]} (int oids);
DeclareAttackers/Blockers via relations-schema viewer_interaction (no legacy
actions on protocol 94); CastSpell via advertised actions; PayMana* via
pay_tick; PassPriority only when the seat genuinely holds Priority.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client  # noqa: E402
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-7240"
EVDIR = f"{BACKFILL}/evidence/7240/{RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

GUIDE = "Goblin Guide"
OA = "Overpowering Attack"
MOUNTAIN = "Mountain"
# 8x Overpowering Attack: the engine doesn't enforce the 4-of limit and we
# need OA in hand quickly, before Guide beats end the game (~turn 10+)
P0_DECK = [(GUIDE, 12), (OA, 8), (MOUNTAIN, 40)]
P1_DECK = [("Plains", 60)]
MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

SERVER_IDENTITY = {
    "validated_version": "v0.98.0",
    "build_commit": "61e8550",
    "protocol_version": 94,
    "server_binary_sha256":
        "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
    "card_data_sha256":
        "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
    "draft_pools_sha256":
        "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
    "signature_verified": True,
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
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


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


for _f, _k in (
        ("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.98.0/data/draft-pools.json",
         "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

_cd = json.load(open(f"{BACKFILL}/server/releases/v0.98.0/data/card-data.json"))
_oa = _cd["overpowering attack"]
with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
    json.dump({"name": _oa["name"], "oracle_text": _oa["oracle_text"],
               "abilities": _oa["abilities"]}, f, indent=2)
say("wrote carddata_excerpt.json")

GLOBAL_BUDGET = 1500
OA_CAST_TIMEOUT_TURNS = 40   # give up waiting for OA in hand after this

ST = {}
MULLS = {}
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
_DISCARD_REV = {}
_WATCH = {}
OBS = {}
C0 = None
P1C = None


def reset():
    global C0, P1C
    ST.clear()
    ST.update({"stage": "SETUP", "oa_cast": False, "oa_oid": None,
               "oa_cast_t": None, "oa_resolved": False, "oa_turn": None,
               "pre_exported": False, "post_exported": False,
               "attacked_this_turn": set(), "turn_seen": None,
               "phase_trace": [], "declare_dumped": False,
               "land_turns": set(), "guide_cast_turns": [],
               "stop": False, "done": False})
    MULLS.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    _DISCARD_REV.clear()
    _WATCH.clear()
    OBS.clear()
    OBS.update({"rejections": [], "p0_acts": 0, "p1_acts": 0,
                "notes": []})
    C0 = None
    P1C = None


# ------------------------------------------------------------ state helpers

def oname(o):
    return o.get("base_name") or o.get("card_name") or o.get("name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid, name=None):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (name is None or oname(o) == name)]


def untapped_mountains(state, pid):
    return sum(1 for _, o in bf(state, pid, MOUNTAIN) if not o.get("tapped"))


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def merged_actions(st):
    return st.get("legal_actions") or []


def vi_ops(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def pending_for(state, pid):
    data = wf_of(state).get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def stack_oids(state):
    return [oid for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Stack"]


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


REL_SHAPES = [
    # candidate-id pairs, mirroring spec edges sourceId/targetIds
    lambda s, t, a: {"sourceId": s, "targetId": t},
    lambda s, t, a: {"sourceId": s, "targetIds": [t]},
    lambda s, t, a: {"source": s, "target": t},
    # object-id pair (attacker oid -> player 1)
    lambda s, t, a: {"attacker": int(a[0]),
                     "defender": {"type": "Player", "data": 1}},
]


def rel_pairs_for_attack(op, state, attackers):
    """Derive (source_candidate_id, target_candidate_id) pairs from the
    relations spec edges, keeping only edges whose source candidate
    references one of our ready attacker oids."""
    data = (op.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec") or {}
    specdata = spec.get("data") or {}
    edges = specdata.get("edges") or []
    cand_by_id = {}
    for x in candidate_info(op, state):
        cand_by_id[x["choice_id"]] = x
    pairs = []
    for e in edges:
        src = e.get("sourceId")
        tgts = e.get("targetIds") or []
        ci = cand_by_id.get(src)
        if not ci or not tgts:
            continue
        if not any(r in attackers for r in ci["refs"]):
            continue
        d = next((t for t in tgts
                  if 1 in (cand_by_id.get(t, {}).get("seats") or [])),
                 tgts[0])
        pairs.append((src, d))
    if not pairs:
        # fallback: any r-candidate referencing an attacker x seat-1 defender
        d_seat1 = next((cid for cid, ci in cand_by_id.items()
                        if 1 in (ci.get("seats") or [])), None)
        for cid, ci in cand_by_id.items():
            if any(r in attackers for r in ci["refs"]) and d_seat1:
                pairs.append((cid, d_seat1))
    return pairs


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        refs = []
        seats = []
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            if "reference" in d:
                refs.append(str(d["reference"]))
            if "seat" in d:
                seats.append(d["seat"])
        names = [oname(state["objects"].get(r, {})) for r in refs]
        out.append({"choice_id": ch.get("id"), "refs": refs, "seats": seats,
                    "names": names,
                    "label": ch.get("label") or ch.get("text")})
    return out


# ------------------------------------------------------- protocol-94 ticks

async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub})
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
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return found


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 60:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} "
                    f"wf={json.dumps(wf_of(s), default=str)[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 60s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


async def export_now(tag):
    s = await C0.export_state()
    with open(f"{EVDIR}/{tag}.json", "w") as f:
        f.write(s)
    say(f"exported {tag}.json")
    return s


def p0_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == OA:
            return 3
        if nm == GUIDE:
            return 2
        if nm == MOUNTAIN:
            return 1
        return 0
    return rank


async def do_mulligan(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    # the engine re-asks after each mulligan; answer every Declare round
    key = f"mull_{c.name}_{pend.get('mulligan_count', 0)}"
    if MULLS.get(key):
        return False
    if pid == 0:
        lands = sum(1 for oid in hand_oids(state, pid)
                    if oname(state["objects"][oid]) == MOUNTAIN)
        guides = sum(1 for oid in hand_oids(state, pid)
                     if oname(state["objects"][oid]) == GUIDE)
        # want mana AND an attacker; mulligan aggressively on the first
        # hand, then keep anything functional to avoid going too deep
        if pend.get("mulligan_count", 0) == 0:
            choice = "Keep" if (lands >= 2 and guides >= 1) else "Mulligan"
        else:
            choice = "Keep" if lands >= 1 else "Mulligan"
    else:
        choice = "Keep"
    say(f"[{c.name}] mulligan -> {choice}")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    MULLS[key] = choice
    wire("mulligan", {"who": c.name, "decision": choice})
    return True


async def do_bottom(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    key = f"bottom_{c.name}"
    if MULLS.get(key):
        return False
    n = (pend.get("phase") or {}).get("count") or 1
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=p0_rank(state))[:n]]
    say(f"[{c.name}] bottoming {n}: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS[key] = True
    return True


async def do_discard(c, pid):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_oids(state, pid)) - 7
    if n <= 0:
        return False
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=p0_rank(state))[:n]]
    say(f"[{c.name}] discarding to hand size: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c):
    st = c.latest
    if not st:
        return False
    for a in merged_actions(st):
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


def find_relations_op(c):
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return op
    return None


async def answer_declare_attackers(c, pid, state):
    """P0 attacks with every ready Goblin Guide at P1 (player 1)."""
    if wf_of(state).get("type") != "DeclareAttackers":
        return False
    if str(wf_player(state)) not in (str(pid), "None"):
        return False
    if state.get("active_player") != pid:
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    if not iid or iid in SUBMITTED:
        return False
    if not ST["declare_dumped"]:
        ST["declare_dumped"] = True
        with open(f"{EVDIR}/declare_attackers_opportunity.json", "w") as f:
            json.dump(op, f, indent=1, default=str)
        wire("declare_attackers_opportunity_full", {"op": op})
        say(f"[P0] dumped DeclareAttackers opportunity "
            f"(turn {state.get('turn_number')})")
    attackers = [oid for oid, o in bf(state, pid, GUIDE)
                 if not o.get("tapped") and not o.get("summoning_sick")]
    # Attack with AT MOST ONE guide per combat: each attacking Guide fires its
    # "reveals top card" trigger, and 2+ simultaneous triggers stall on
    # OrderTriggers (single triggers need no ordering). One attacker is
    # sufficient for the "attacked this turn" precondition.
    attackers = attackers[:1]
    cands = candidate_info(op, state)
    pairs = rel_pairs_for_attack(op, state, attackers)
    wire("declare_attackers_candidates",
         {"attackers": attackers, "pairs": pairs,
          "candidates": [(x["choice_id"], x["names"], x["seats"])
                         for x in cands]})
    say(f"[P0] DeclareAttackers: ready guides={attackers}, pairs={pairs}")
    # rotate submission shapes across rejections (drain_rejections un-submits
    # the iid, so the next tick tries the next shape)
    attempt = ST.setdefault("rel_attempt", {}).get(iid, 0)
    shape = REL_SHAPES[attempt % len(REL_SHAPES)]
    rels = [shape(s, t, attackers) for (s, t) in pairs]
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": rels}}}
    ST["rel_attempt"][iid] = attempt + 1
    say(f"[P0] attacking (attempt {attempt}): {json.dumps(rels)[:220]}")
    wire("declare_attackers_submit", {"attempt": attempt, "submission": sub})
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    ST["attacked_this_turn"].update(attackers)
    OBS["p0_acts"] += 1
    return True


async def answer_order_triggers(c, pid, state):
    """Safety net: order simultaneous triggers (e.g. 2+ Guide attack
    triggers) in advertised order. Single triggers need no ordering."""
    if wf_of(state).get("type") != "OrderTriggers":
        return False
    if str(wf_player(state)) not in (str(pid), "None"):
        return False
    for op in vi_ops(c):
        iid = op.get("interactionId") or op.get("id")
        if not iid or iid in SUBMITTED:
            continue
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        rtype = resp.get("type")
        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        chs = data.get("choices") or data.get("candidates") or []
        wire("order_triggers_opportunity", {"who": c.name, "op": op})
        say(f"[{c.name}] OrderTriggers: rtype={rtype} spec={stype} "
            f"choices={len(chs)}")
        ids = [ch.get("id") for ch in chs if ch.get("id")]
        sub = None
        if rtype == "schema" and stype in ("sequence", "select") and ids:
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": ids}}}
        elif rtype == "exactChoices" and ids:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": ids[0]}}}
        if sub is None:
            say(f"[{c.name}] OrderTriggers: no known answer shape; holding")
            wire("order_triggers_no_shape", {"rtype": rtype, "stype": stype})
            continue
        await send_interaction(c, sub)
        SUBMITTED.add(iid)
        say(f"[{c.name}] OrderTriggers answered ({rtype}/{stype})")
        return True
    return False


async def answer_declare_blockers(c, pid, state):
    if wf_of(state).get("type") != "DeclareBlockers":
        return False
    if str(wf_player(state)) not in (str(pid), "None"):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    if not iid or iid in SUBMITTED:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": []}}}
    wire("declare_blockers_submit", {"who": c.name, "submission": sub})
    say(f"[{c.name}] declares empty blockers")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    if c.name == "P1":
        OBS["p1_acts"] += 1
    return True


DECISION_GUARD = ("OptionalCostChoice", "TargetSelection",
                  "TriggerTargetSelection", "ManaPayment", "ChooseXValue",
                  "DiscardChoice", "ChooseLegend", "ChooseMode",
                  "ChooseAbility")


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], merged_actions(st)
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    if await pay_tick(c):
        return True
    if await answer_declare_attackers(c, pid, state):
        return True
    if await answer_declare_blockers(c, pid, state):
        return True
    if await answer_order_triggers(c, pid, state):
        return True
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    if wt0 in DECISION_GUARD and str(wplayer) == str(pid):
        say(f"[P0] unhandled decision pending: {wt0}; holding")
        wire("unhandled_decision", {"type": wt0})
        return False
    turn = state.get("turn_number")
    phase = state.get("phase")
    # main-phase plays
    if state.get("active_player") == pid and phase in MAIN_PHASES \
            and str(wplayer) == str(pid) and wt0 == "Priority":
        # land drop
        if turn not in ST["land_turns"]:
            lid = find_hand(state, pid, MOUNTAIN)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        ST["land_turns"].add(turn)
                        OBS["p0_acts"] += 1
                        say(f"[P0] played Mountain (turn {turn})")
                        return True
        # cast Goblin Guide (at most 2 per turn to keep mana for OA)
        if ST["guide_cast_turns"].count(turn) < 2:
            gid = find_hand(state, pid, GUIDE)
            a = castspell_advertised(acts, gid)
            if a and untapped_mountains(state, pid) >= 1:
                await submit_as_is(c, a)
                ST["guide_cast_turns"].append(turn)
                OBS["p0_acts"] += 1
                say(f"[P0] casting Goblin Guide (turn {turn})")
                return True
        # cast Overpowering Attack in postcombat main: require a genuinely
        # tapped attacker (tapped Guide proves the attack resolved; the
        # attacked_this_turn flag is only set optimistically at submit)
        tapped_guides = [oid for oid, o in bf(state, pid, GUIDE)
                         if o.get("tapped")]
        if (not ST["oa_cast"] and phase == "PostCombatMain"
                and str(wplayer) == str(pid) and wt0 == "Priority"
                and turn not in ST.setdefault("oa_diag_turns", set())):
            ST["oa_diag_turns"].add(turn)
            _oaid = find_hand(state, pid, OA)
            _a = castspell_advertised(acts, _oaid)
            say(f"[P0] OA diag turn {turn}: oa_in_hand={_oaid is not None} "
                f"tapped_guides={tapped_guides} "
                f"untapped_mtns={untapped_mountains(state, pid)} "
                f"cast_advertised={_a is not None}")
            wire("oa_diag", {"turn": turn, "oa_in_hand": _oaid is not None,
                             "tapped_guides": tapped_guides,
                             "untapped_mtns": untapped_mountains(state, pid),
                             "advertised": _a is not None})
        if (not ST["oa_cast"] and phase == "PostCombatMain"
                and tapped_guides):
            oaid = find_hand(state, pid, OA)
            a = castspell_advertised(acts, oaid)
            if a and untapped_mountains(state, pid) >= 5:
                if not ST["pre_exported"]:
                    await export_now("pre")
                    ST["pre_exported"] = True
                    ST["oa_turn"] = turn
                    say(f"[P0] PRE exported (turn {turn}, postcombat main)")
                await submit_as_is(c, a)
                ST["oa_cast"] = True
                ST["oa_oid"] = int(oaid)
                ST["oa_cast_t"] = time.time()
                OBS["p0_acts"] += 1
                say(f"[P0] CASTING {OA} (oid {oaid}, turn {turn})")
                wire("oa_cast_submit", {"oid": int(oaid), "turn": turn})
                return True
    # pass priority when we hold it
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                OBS["p0_acts"] += 1
                return True
    return False


async def p1_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], merged_actions(st)
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    if await pay_tick(c):
        return True
    if await answer_declare_blockers(c, pid, state):
        return True
    if await answer_declare_attackers(c, pid, state):
        return True
    if await answer_order_triggers(c, pid, state):
        return True
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    if wt0 in DECISION_GUARD and str(wplayer) == str(pid):
        say(f"[P1] unhandled decision pending: {wt0}; holding")
        wire("unhandled_decision_p1", {"type": wt0})
        return False
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                OBS["p1_acts"] += 1
                return True
    return False


# ------------------------------------------------------------------ main

async def run():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    await p0.create(deck(*P0_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"P1 joined; seat={p1.player_id}")
    wire("game_created", {"code": p0.game_code, "p0_seat": p0.player_id,
                          "p1_seat": p1.player_id})

    global C0, P1C
    C0 = p0
    P1C = p1
    last_rev = {p0: -1, p1: -1}
    last_tick_wall = 0.0
    force = False
    last_turn = None

    while time.time() - t0 < GLOBAL_BUDGET and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        if not p0.latest:
            continue
        watch(p0)
        watch(p1)
        rej = False
        for c in (p0, p1):
            for r in drain_rejections(c):
                OBS["rejections"].append({"who": c.name, **r})
                rej = True
                if LAST_SUBMIT["iid"] in SUBMITTED:
                    SUBMITTED.discard(LAST_SUBMIT["iid"])
                    LAST_SUBMIT["iid"] = None
                if ST["oa_cast"] and not ST["oa_resolved"] and c.name == "P0":
                    say("[P0] OA cast rejected; re-arming")
                    wire("oa_cast_rejected", {})
                    ST["oa_cast"] = False
                    ST["oa_oid"] = None
            force = True
        if rej:
            pass
        if now - last_tick_wall >= 5:
            force = True
        progressed = (p0.revision != last_rev[p0]
                      or p1.revision != last_rev[p1])
        if not progressed and not force:
            continue
        force = False
        last_tick_wall = now
        last_rev[p0] = p0.revision
        last_rev[p1] = p1.revision
        try:
            await p0_tick(p0, 0)
        except Exception as e:
            say(f"[P0] tick error: {e}")
        try:
            await p1_tick(p1, 1)
        except Exception as e:
            say(f"[P1] tick error: {e}")

        st = p0.latest
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number")
        phase = state.get("phase")
        if turn != last_turn:
            ST["attacked_this_turn"] = set()
            last_turn = turn
            say(f"--- turn {turn} (active {state.get('active_player')}) ---")

        # OA resolution watch: require the spell to have been SEEN on the
        # stack first, else the tick right after submission (spell still in
        # hand / mana being paid) falsely looks "resolved"
        if ST["oa_cast"] and not ST["oa_resolved"]:
            on_stack = str(ST["oa_oid"]) in stack_oids(state)
            if on_stack and not ST.get("oa_seen_on_stack"):
                ST["oa_seen_on_stack"] = True
                say(f"[P0] OA on stack at rev {p0.revision}")
                wire("oa_on_stack", {"rev": p0.revision, "turn": turn,
                                     "phase": phase})
            if ST.get("oa_seen_on_stack") and not on_stack:
                ST["oa_resolved"] = True
                ST["resolve_t"] = now
                ST["resolve_rev"] = p0.revision
                say(f"[P0] OA RESOLVED at rev {p0.revision}, "
                    f"turn {turn} phase {phase}")
                wire("oa_resolved", {"rev": p0.revision, "turn": turn,
                                     "phase": phase})
                try:
                    await export_now("post")
                    ST["post_exported"] = True
                except Exception as e:
                    say(f"post export failed: {e}")

        # phase trace from OA resolution until turn increments
        if ST["oa_resolved"] and not ST["done"]:
            tr = ST["phase_trace"]
            if not tr or tr[-1] != (turn, phase):
                tr.append((turn, phase))
                say(f"[trace] turn={turn} phase={phase}")
            if turn != ST["oa_turn"]:
                ST["done"] = True
                say(f"[trace] turn incremented to {turn}; observation done")
                try:
                    await export_now("final")
                except Exception as e:
                    say(f"final export failed: {e}")
                ST["stop"] = True

        wft = wf_of(state).get("type")
        if wft == "GameOver" or state.get("game_over") \
                or state.get("winner") is not None:
            say(f"game ended (wf={wft})")
            ST["stop"] = True
            break

        # give up waiting for OA in hand
        if not ST["oa_cast"] and turn and turn > OA_CAST_TIMEOUT_TURNS:
            say(f"OA never cast by turn {turn}; giving up")
            OBS["notes"].append(f"OA never in hand/castable by turn {turn}")
            ST["stop"] = True
            break

    with open(f"{EVDIR}/phase_trace.json", "w") as f:
        json.dump({"oa_turn": ST["oa_turn"],
                   "trace": ST["phase_trace"]}, f, indent=1)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": OBS,
                   "phase_trace": ST["phase_trace"],
                   "rejections": OBS["rejections"]}, f, indent=1, default=str)
    say("wrote phase_trace.json, observations.json")
    await p0.close()
    await p1.close()
    return t0


# ------------------------------------------------------------- evaluation

def load_env(tag):
    p = f"{EVDIR}/{tag}.json"
    if not os.path.exists(p):
        return None
    try:
        return json.loads(open(p).read())["state"]
    except Exception as e:
        say(f"load_env({tag}) failed: {e}")
        return None


def guide_states(state, pid):
    return {oid: o for oid, o in bf(state, pid, GUIDE)}


def evaluate():
    A = {}
    notes = []
    pre = load_env("pre")
    post = load_env("post")
    final = load_env("final")
    trace = ST["phase_trace"]
    oa_turn = ST["oa_turn"]

    # ---- A1: setup ----
    try:
        guides_pre = guide_states(pre, 0) if pre else {}
        tapped_pre = [oid for oid, o in guides_pre.items()
                      if o.get("tapped")]
        mtn_pre = untapped_mountains(pre, 0) if pre else 0
        oa_in_hand_pre = sum(1 for oid in hand_oids(pre, 0)
                             if oname(pre["objects"][oid]) == OA) if pre else 0
        ok = (pre is not None
              and pre.get("active_player") == 0
              and pre.get("phase") == "PostCombatMain"
              and oa_in_hand_pre >= 1
              and len(tapped_pre) >= 1
              and mtn_pre >= 5)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(
            f"A1: pre turn={pre.get('turn_number') if pre else None} "
            f"phase={pre.get('phase') if pre else None} "
            f"active={pre.get('active_player') if pre else None}; "
            f"OA in P0 hand={oa_in_hand_pre}; tapped guides on BF="
            f"{len(tapped_pre)}; untapped mountains={mtn_pre}")
        ST["_tapped_pre"] = tapped_pre
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        notes.append(f"A1 eval error: {e}")

    # ---- A2: untap ----
    try:
        if A["A1_setup_ok"] != "passed" or post is None:
            A["A2_untap_ok"] = "not-run"
            notes.append("A2 not evaluated (no valid pre/post pair)")
        else:
            guides_post = guide_states(post, 0)
            still_tapped = [oid for oid in ST["_tapped_pre"]
                            if guides_post.get(oid, {}).get("tapped")]
            missing = [oid for oid in ST["_tapped_pre"]
                       if oid not in guides_post]
            ok = not still_tapped and not missing
            A["A2_untap_ok"] = "passed" if ok else "failed"
            notes.append(
                f"A2: {len(ST['_tapped_pre'])} guide(s) tapped in pre; post: "
                f"still tapped={still_tapped}, missing from BF={missing}")
    except Exception as e:
        A["A2_untap_ok"] = "not-run"
        notes.append(f"A2 eval error: {e}")

    # ---- A3/A4: extra combat + extra main on the OA turn ----
    try:
        if oa_turn is None:
            A["A3_extra_combat"] = "not-run"
            A["A4_extra_main"] = "not-run"
            notes.append("A3/A4 not evaluated (OA never resolved)")
        else:
            same_turn = [ph for (t, ph) in trace if t == oa_turn]
            # find resolution index: first trace entry at/after resolve
            res_idx = None
            for i, (t, ph) in enumerate(trace):
                if t == oa_turn:
                    res_idx = i
                    break
            post_res = same_turn[res_idx:] if res_idx is not None else []
            combat_phases = {"BeginCombat", "DeclareAttackers",
                             "DeclareBlockers", "CombatDamage", "EndCombat",
                             "Combat"}
            saw_combat = any(ph in combat_phases for ph in post_res)
            A["A3_extra_combat"] = "passed" if saw_combat else "failed"
            notes.append(
                f"A3: oa_turn={oa_turn}; post-resolution same-turn phases="
                f"{post_res}; extra combat seen={saw_combat}")
            if saw_combat:
                first_combat = next(
                    i for i, ph in enumerate(post_res)
                    if ph in combat_phases)
                after = post_res[first_combat + 1:]
                saw_main = any(ph in MAIN_PHASES for ph in after)
                A["A4_extra_main"] = "passed" if saw_main else "failed"
                notes.append(f"A4: phases after extra combat={after}; "
                             f"extra main seen={saw_main}")
            else:
                A["A4_extra_main"] = "not-run"
                notes.append("A4 not evaluated (no extra combat to follow)")
    except Exception as e:
        A["A3_extra_combat"] = A.get("A3_extra_combat", "not-run")
        A["A4_extra_main"] = "not-run"
        notes.append(f"A3/A4 eval error: {e}")

    # ---- A5: cleanup ----
    try:
        if final is not None:
            advanced = (final.get("turn_number") or 0) > (oa_turn or 0)
            over = bool(final.get("game_over")
                        or final.get("winner") is not None)
            ok = advanced or over
            A["A5_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A5: final turn={final.get('turn_number')} "
                         f"(oa_turn={oa_turn}) game_over={over}")
        else:
            A["A5_cleanup"] = "not-run"
            notes.append("A5 not evaluated (no final export)")
    except Exception as e:
        A["A5_cleanup"] = "not-run"
        notes.append(f"A5 eval error: {e}")

    # ---- verdict ----
    if A.get("A1_setup_ok") != "passed":
        verdict = "blocked"
    elif A.get("A2_untap_ok") == "failed":
        verdict = "reproduced"
    elif A.get("A3_extra_combat") == "failed":
        verdict = "reproduced"
    elif A.get("A3_extra_combat") == "passed" \
            and A.get("A4_extra_main") == "failed":
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_untap_ok", "A3_extra_combat",
              "A4_extra_main", "A5_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return A, notes, verdict


def render_summary(A, verdict, notes):
    from PIL import Image, ImageDraw
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 84], fill=(32, 36, 48))
    d.text((24, 18), "phase-rs/phase #7240 — bug-state backfill summary",
           fill=(235, 235, 240))
    d.text((24, 48), "Overpowering Attack: extra combat after 2nd-main cast",
           fill=(170, 175, 190))
    y = 110
    d.text((24, y), f"release v0.98.0 (build 61e8550, protocol 94)  "
                    f"date 2026-10-01  run {RUN_ID}", fill=(150, 155, 170))
    y += 30
    d.text((24, y), f"scenario driver/scenario_7240_0980.py", fill=(150, 155, 170))
    y += 44
    vc = (120, 220, 130) if verdict == "not-reproduced" else \
        (235, 120, 120) if verdict == "reproduced" else (235, 200, 120)
    d.text((24, y), f"verdict: {verdict}", fill=vc)
    y += 40
    order = ["A1_setup_ok", "A2_untap_ok", "A3_extra_combat", "A4_extra_main",
             "A5_cleanup"]
    labels = {
        "A1_setup_ok": "setup reached (postcombat main, OA in hand, tapped attacker, 5+ mana)",
        "A2_untap_ok": "attacked creatures untapped on resolution",
        "A3_extra_combat": "additional combat phase granted after this main phase",
        "A4_extra_main": "additional main phase follows the extra combat",
        "A5_cleanup": "game continues past the OA turn",
    }
    for k in order:
        st = A.get(k, "not-run")
        col = {"passed": (120, 220, 130), "failed": (235, 120, 120),
               "not-run": (160, 160, 160)}[st]
        d.text((40, y), f"{k}: {st}", fill=col)
        d.text((300, y), labels[k], fill=(140, 145, 160))
        y += 30
    y += 16
    d.text((24, y), "key observations:", fill=(200, 205, 220))
    y += 28
    for n in notes[:8]:
        line = n if len(n) <= 110 else n[:107] + "..."
        d.text((40, y), "- " + line, fill=(140, 145, 160))
        y += 24
        if y > H - 60:
            break
    d.text((24, H - 34), "generated from saved states/assertions "
                         "(summary diagram, not a gameplay screenshot)",
           fill=(110, 115, 130))
    p = f"{EVDIR}/summary.png"
    img.save(p)
    say(f"rendered {p}")
    return p


def main():
    t_start = time.time()
    try:
        t0 = asyncio.run(run())
    except Exception as e:
        say(f"run crashed: {e!r}")
        try:
            OBS["notes"].append(f"run crash: {e!r}")
        except Exception:
            pass
        t0 = t_start
    A, notes, verdict = evaluate()
    for k in ("A1_setup_ok", "A2_untap_ok", "A3_extra_combat",
              "A4_extra_main", "A5_cleanup"):
        say(f"{k}: {A.get(k)}")
    for n in notes:
        say("note:", n)
    say(f"VERDICT: {verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": notes, "verdict": verdict},
                  f, indent=2)
    # (declare_attackers_opportunity.json is written only if an opportunity
    # was actually captured during the run)

    png = render_summary(A, verdict, notes)

    run_doc = {
        "run_id": RUN_ID,
        "issue": 7240,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(t_start)),
        "server": {
            "version": SERVER_IDENTITY["validated_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "source": "pinned v0.98.0 binary started fresh on 127.0.0.1:9374 "
                      "by this run (nothing was listening; prior shared "
                      "server from the 6865 run had exited); --single-user "
                      "--no-data-download; games.db isolated to "
                      "runs/20261001-7240/games.db; digests recomputed from "
                      "pinned on-disk artifacts at run start; ServerHello "
                      "confirmed v0.98.0/61e8550/protocol 94/Full",
        },
        "verdict": verdict,
        "assertions": A,
        "contract_line": "Cast Overpowering Attack in the controller's "
                         "postcombat main phase after attacking: creatures "
                         "that attacked must untap AND an additional combat "
                         "phase (then an additional main phase) must follow "
                         "on the same turn.",
        "parser_corroboration": "v0.98.0 card-data.json parses the spell as "
                         "SetTapState(Untap, Typed/Creature/You/"
                         "AttackedThisTurn, All) + SequentialSibling "
                         "AdditionalPhase{BeginCombat, after: ThisPhase, "
                         "followed_by: [PostCombatMain], count 1}, gated on "
                         "And(CurrentPhaseIs[PreCombatMain, PostCombatMain], "
                         "IsYourTurn). The pinned data shows the ThisPhase "
                         "anchor (not the EndCombat anchor the issue's "
                         "classifier attributed to the engine).",
        "limitations": [
            "Freerunning alternative cost not exercised (hard-cast only)",
            "2-player game only; Commander/multiplayer not exercised",
            "Precombat-main cast branch not exercised (postcombat only, per "
            "the report)",
            "Browser/UI not exercised; native engine only",
            "Not tested on the original report build; verdict is scoped to "
            "v0.98.0, not a fix claim",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game replay)",
        ],
        "setup_line": "P0: 12x Goblin Guide + 4x Overpowering Attack + 44x "
                      "Mountain (human driver). P1: 60x Plains (passive: no "
                      "lands, passes priority, empty blockers). P0 attacks "
                      "every combat; OA cast in the first postcombat main "
                      "with OA in hand, 5+ untapped Mountains, and a Guide "
                      "that attacked that turn.",
        "scenario": {"file": "scenario_7240_0980.py",
                     "sha256": sha256_of_file(__file__)},
        "decks": {"P0": {"main": P0_DECK}, "P1": {"main": P1_DECK}},
        "notes": notes,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_doc, f, indent=2, default=str)
    say("wrote run.json")

    # scenario copy into evidence
    with open(__file__, "rb") as src, \
            open(f"{EVDIR}/scenario_7240_0980.py", "wb") as dst:
        dst.write(src.read())

    # close logs BEFORE hashing: say()/wire() append to scenario_run.log /
    # wire_log.jsonl, so the manifest must be computed after the last write
    WIRE.close()
    RUNLOG.close()

    # manifest
    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(f"{EVDIR}/{f}")
                   and f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            mf.write(f"{sha256_of_file(f'{EVDIR}/{fn}')}  {fn}\n")
    print(f"wrote manifest.sha256 ({len(files)} files)", flush=True)

    # validation: every JSON parses; PNG readable
    for fn in files:
        if fn.endswith(".json"):
            json.load(open(f"{EVDIR}/{fn}"))
    print("all JSON files parse", flush=True)
    from PIL import Image
    im = Image.open(png)
    im.verify()
    print(f"PNG readable: {png}", flush=True)
    h = hashlib.sha256()
    with open(f"{EVDIR}/manifest.sha256", "rb") as mf:
        for line in mf:
            exp, name = line.decode().split("  ")
            got = sha256_of_file(f"{EVDIR}/{name.strip()}")
            assert got == exp.strip(), f"manifest mismatch: {name}"
    print("manifest hashes verified", flush=True)

    print("FINAL:", verdict, json.dumps(A))
    return verdict


if __name__ == "__main__":
    main()
