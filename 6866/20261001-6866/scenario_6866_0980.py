#!/usr/bin/env python3
"""Issue #6866 (revalidation on v0.98.0 / protocol 94): Delina, Wild Mage
doesn't reroll on 15-20.

Oracle (pinned v0.98.0 card-data): "Whenever Delina attacks, choose target
creature you control, then roll a d20. 1-14 | Create a tapped and attacking
token that's a copy of that creature, except it's not legendary and it has
'At end of combat, exile this token.' 15-20 | Create one of those tokens.
You may roll again."

Pinned data: the 1-14 branch is fully implemented (CopyTokenOf with
RemoveSupertype(Legendary) + granted end-of-combat exile trigger). The 15-20
branch parses as effect=Unimplemented("create") with an optional
sub_ability=Unimplemented("roll again"). The triage acceptance criteria: a
15-20 creates the token and offers an optional reroll; each further 15-20
may repeat; 1-14 ends the sequence.

The engine surfaces no die-roll result to the client (no RollResult
waiting_for), so the roll branch is identified by its effect: 1-14 creates
exactly one token; 15-20 (Unimplemented) creates none.

The 2026-09-11 v0.80.0 run (20260911-6866b, protocol 69) found REPRODUCED:
low turn created exactly 1 token; high turn (trigger fired, 0 tokens) offered
no "roll again" prompt (only an empty OptionalEffectChoice, answered true,
which resolved nothing); the low-turn token was also NOT exiled by
PostCombatMain (A7 failed there).

Plan (two human seats, native engine, v0.98.0 / protocol 94):
  SETUP  - P0: lands, Grizzly Bears, Delina, Wild Mage on BF. P1: passive
           Forests + Llanowar Elves that chump-block Delina each turn
           (Delina 3/2 vs Elf 1/1: elf dies, Delina lives, P1 takes 0).
  ATTACK - P0 attacks with Delina alone every turn (relations-schema
           DeclareAttackers); the trigger's TargetSelection (2 legal
           targets: Bear + Delina) is answered with the Bear. Per attack
           turn record: target answered (turn), new P0 token oids after
           trigger resolution (delta 1 => 1-14, delta 0 => 15-20
           signature), waiting_for types seen that turn. P1 blocks every
           attacker with a separate untapped Elf (relations-schema
           DeclareBlockers).
  STOP   - once >=1 low turn (delta==1, control) and >=1 high turn
           (target answered, delta==0) are recorded; watchdog 14 attacks.

Behavioral contract:
  A1 setup_ok       pre.json: P0 PreCombatMain, Delina on BF (Bear on BF
                    preferred for the 2-target prompt path), life 20/20
  A2 trigger_fires  >=1 TargetSelection answered (target = Grizzly Bears)
  A3 low_branch     >=1 turn with exactly 1 new token: tapped, attacking,
                    is_token, copy of Bear, non-legendary, carries the
                    granted end-of-combat exile ability
  A4 high_observed  >=1 turn where the trigger fired and the initial roll
                    resolved with 0 new tokens (15-20 signature; 1-14 provably
                    creates a token per A3)
  A5 reroll_offered on a high turn the engine offered the optional
                    "you may roll again" prompt (OptionalEffectChoice)
  A6 reroll_works  accepting the reroll produced a further roll resolution
                    (a new token from a 1-14 re-roll, or a repeated prompt
                    from another 15-20)
  A7 exile_ok       on low turns the token is exiled by PostCombatMain
                    (acceptance criterion; recorded as observed)

Verdict = reproduced iff A1..A4 pass and the reroll does not actually happen
(A5 fails: never offered -- the original report; or A5 passes but A6 fails:
offered yet accepting resolves nothing). not-reproduced iff A1..A6 all pass.
Blocked if no Delina attack turn completes.

Protocol-94 driver (v0.98.0, per scenario_6865_0980): HELLO advertises
protocol 94 (server enforces exact match); MulliganDecision as
{"choice":{"type":"Keep"}} gated on the seat's pending Declare;
BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]} (int
oids); DeclareAttackers/Blockers via relations-schema interaction (no legacy
actions on protocol 94); CastSpell/ActivateAbility via advertised actions;
PayMana* via pay_tick; TargetSelection answered via viewer_interaction
schema/exactChoices; optional-effect accept/decline from decideOptionalEffect
surfaces role/value; PassPriority only when the seat genuinely holds Priority;
per-client stale watchdog; stale-interaction handling via rejection drain.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-6866"
EVID_ISSUE = "6866"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
TMPD = f"/tmp/ev6866_{RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(TMPD, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


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


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.98.0",
    "build_commit": "61e8550",
    "protocol_version": 94,
    "server_binary_sha256": "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
    "card_data_sha256": "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
    "draft_pools_sha256": "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
    "signature_verified": True,
}

for _f, _k in (("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.98.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

DELINA = "Delina, Wild Mage"
BEAR = "Grizzly Bears"
ELF = "Llanowar Elves"
MTN = "Mountain"
FOREST = "Forest"
LANDS = (MTN, FOREST)

P0_DECK = [(DELINA, 8), (BEAR, 12), (MTN, 20), (FOREST, 20)]
P1_DECK = [(FOREST, 30), (ELF, 30)]

ST = {}
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
PHASES = []
CASTLOG = []
C0 = None
_WATCH = {}
_DISCARD_REV = {}


def reset_globals():
    global C0
    ST.clear()
    ST.update({
        "stage": "SETUP", "stop": False, "retry": False,
        "attack_turns": [], "tokens_before": {}, "deltas": {},
        "target_answers": [], "low_turn": None, "high_turn": None,
        "wf_by_turn": {}, "stack_kinds_by_turn": {},
        "exile_check": {}, "pre_exported": False,
        "reroll_prompts": [], "reroll_answers": [], "tokens_at_reroll": {},
        "other_may_prompts": [],
        "mid_low_exported": False, "post_low_exported": False,
        "mid_high_exported": False, "post_high_exported": False,
        "opp_logged": False, "game_code": None,
    })
    MULLS.clear()
    SHAPES.clear(); WF_SEEN.clear(); SUBMITTED.clear()
    LAST_SUBMIT.clear(); LAST_SUBMIT.update({"iid": None})
    PHASES.clear(); CASTLOG.clear()
    _DISCARD_REV.clear(); _WATCH.clear()
    C0 = None


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


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def p0_token_oids(state):
    return {str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == 0
            and o.get("is_token")}


def active_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain",
                                       "Main"))


def life_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


def combat_attacker_oids(state):
    out = set()
    for a in ((state.get("combat") or {}).get("attackers") or []):
        out.add(str(a.get("object_id")))
    return out


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


def record_wf(state):
    wf = wf_of(state).get("type")
    turn = state.get("turn_number")
    if wf:
        ST["wf_by_turn"].setdefault(turn, [])
        if not ST["wf_by_turn"][turn] or ST["wf_by_turn"][turn][-1][0] != wf:
            ST["wf_by_turn"][turn].append(
                (wf, wf_player(state)))
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": wf_of(state).get("data"),
                             "stage": ST["stage"]})


def record_phase(state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if not PHASES or PHASES[-1] != key:
        PHASES.append(key)
        wire("phase", {"turn": key[0], "active": key[1], "phase": key[2],
                       "stage": ST["stage"]})
    turn = state.get("turn_number")
    kinds = []
    for entry in state.get("stack") or []:
        k = entry.get("kind") or {}
        kinds.append(k.get("type") if isinstance(k, dict) else str(k))
    if kinds:
        ST["stack_kinds_by_turn"].setdefault(turn, set()).update(kinds)


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
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return found


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 45:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} "
                    f"wf={json.dumps(wf_of(s), default=str)[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 45s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


async def export_now(path, dest_dir=EVDIR):
    s = await C0.export_state()
    with open(f"{dest_dir}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s

# ------------------------------------------------------- protocol-94 ticks

async def do_mulligan(c, pid, tag, keep_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if MULLS.get((tag, "kept")):
        return False
    mull_count = MULLS.get((tag, "mulls"), 0)
    choice = "Keep" if (keep_fn(state) or mull_count >= 2) else "Mulligan"
    say(f"[{tag}] mulligan -> {choice} (prior mulligans: {mull_count})")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    if choice == "Mulligan":
        MULLS[(tag, "mulls")] = mull_count + 1
    else:
        MULLS[(tag, "kept")] = True
    wire("mulligan", {"who": tag, "decision": choice})
    return True


async def do_bottom(c, pid, tag, rank_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    if (tag, "bottomed") in MULLS:
        return False
    n = (pend.get("phase") or {}).get("count") or 1
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=rank_fn(state))[:n]]
    say(f"[{tag}] bottoming {n}: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


async def do_discard(c, pid, tag, rank_fn):
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
                                    key=rank_fn(state))[:n]]
    say(f"[{tag}] discarding to hand size: "
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


def relations_shape(op):
    data = (op.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}
    return edges, cands


def cand_object_ref(cand):
    for s in (cand or {}).get("surfaces", []) or []:
        if s.get("type") == "object":
            return str((s.get("data") or {}).get("reference"))
    return None


def cand_seat(cand):
    for s in (cand or {}).get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            return d["seat"]
    return None


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = seat = None
        val = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            if "reference" in d:
                ref = str(d["reference"])
            if "seat" in d:
                seat = d["seat"]
            if "value" in d:
                val = d["value"]
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "value": val, "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": str(ch.get("text") or ch.get("label") or "")[:80]})
    return out


def optional_effect_choices(opp):
    """Parse exactChoices for decideOptionalEffect: [(choice_id, role, value,
    text)]. The value surface carries role 'accept' with value 'true' for the
    affirmative choice (observed on the v0.80.0 run)."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        role = val = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            if s.get("type") == "value" and d.get("role"):
                role = d.get("role")
                val = d.get("value")
            elif "value" in d and val is None:
                val = d["value"]
        out.append({"choice_id": ch.get("id"), "role": role, "value": val,
                    "text": str(ch.get("text") or ch.get("label") or "")[:80]})
    return out


async def answer_declare_attackers_empty(c, pid, tag):
    """Declare empty attackers via the relations-schema interaction (used by
    P1, who never attacks; also the P0 fallback when Delina can't attack)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DeclareAttackers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    if not iid or iid in SUBMITTED:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": []}}}
    wire("declare_attackers_empty", {"who": tag, "submission": sub})
    say(f"[{tag}] declares empty attackers")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    return True


async def answer_declare_blockers_empty(c, pid, tag):
    """Declare empty blockers via the relations-schema interaction (P0 is
    never attacked, but the handler keeps the game moving if it happens)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DeclareBlockers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    if not iid or iid in SUBMITTED:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": []}}}
    wire("declare_blockers_empty", {"who": tag, "submission": sub})
    say(f"[{tag}] declares empty blockers")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    return True


async def answer_declare_attackers_p0(c, pid):
    """P0 declares attackers via the relations-schema interaction: Delina
    alone (edge whose source candidate is Delina, target preferring P1's
    seat), or empty if she cannot attack."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DeclareAttackers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    if not iid or iid in SUBMITTED:
        return False
    edges, cands = relations_shape(op)
    if ("attack_edges",) not in SHAPES:
        SHAPES.add(("attack_edges",))
        wire("declare_attackers_shape",
             {"n_edges": len(edges),
              "edges": edges[:6],
              "candidates": {k: candidate_info(
                  {"response": {"data": {"candidates": [v]}}}, state)
                  for k, v in list(cands.items())[:8]}})
    rels = []
    for e in edges:
        src = e.get("sourceId")
        tids = e.get("targetIds") or []
        cand = cands.get(src, {})
        ref = cand_object_ref(cand)
        o = state["objects"].get(ref, {}) if ref else {}
        if oname(o) != DELINA:
            continue
        if o.get("tapped") or o.get("summoning_sick") or o.get("has_summoning_sickness"):
            continue
        want_tid = None
        for tid in tids:
            if cand_seat(cands.get(tid, {})) == 1:
                want_tid = tid
                break
        if want_tid is None and tids:
            want_tid = tids[0]
        if want_tid:
            rels.append({"sourceId": src, "targetId": want_tid,
                         "group": None})
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": rels}}}
    wire("declare_attackers", {"who": "P0", "n_rels": len(rels),
                               "submission": sub})
    say(f"[P0] declares attackers: {len(rels)} relations "
        f"(turn {state.get('turn_number')})")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    return True


async def answer_declare_blockers_p1(c, pid):
    """P1 blocks every advertised attacker with a separate untapped Elf
    (greedy 1:1, Delina first)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DeclareBlockers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    if not iid or iid in SUBMITTED:
        return False
    edges, cands = relations_shape(op)
    if ("block_edges",) not in SHAPES:
        SHAPES.add(("block_edges",))
        wire("declare_blockers_shape",
             {"n_edges": len(edges), "edges": edges[:8]})

    def cand_name(cid):
        ref = cand_object_ref(cands.get(cid, {}))
        o = state["objects"].get(ref, {}) if ref else {}
        return oname(o), ref, o

    # distinct advertised attacker choice-ids, Delina first
    attackers = []
    seen = set()
    for e in edges:
        for tid in e.get("targetIds") or []:
            if tid not in seen:
                seen.add(tid)
                attackers.append(tid)
    attackers.sort(key=lambda tid: 0 if cand_name(tid)[0] == DELINA else 1)
    used = set()
    rels = []
    for tid in attackers:
        for e in edges:
            src = e.get("sourceId")
            if (e.get("targetIds") or []) and tid not in (e.get("targetIds") or []):
                continue
            nm, ref, o = cand_name(src)
            if nm != ELF or not ref or ref in used:
                continue
            if o.get("tapped"):
                continue
            rels.append({"sourceId": src, "targetId": tid, "group": None})
            used.add(ref)
            break
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": rels}}}
    wire("declare_blockers", {"who": "P1", "n_rels": len(rels),
                              "attackers": len(attackers),
                              "submission": sub})
    say(f"[P1] declares blockers: {len(rels)} relations for "
        f"{len(attackers)} attackers")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    return True


async def answer_delina_target(c, state, st):
    """Answer Delina's attack-trigger target prompt: target our Bear
    (fallback: Delina herself). On protocol 94 the prompt surfaces via
    viewer_interaction as a schema (sequence) or exactChoices response."""
    wf = wf_of(state)
    if wf.get("type") not in ("TargetSelection", "TriggerTargetSelection"):
        return False
    if str(wf_player(state)) != "0":
        return False
    for opp in vi_ops(c):
        iid = opp.get("interactionId") or opp.get("id")
        if not iid or iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        want = next((x for x in cands if x["name"] == BEAR), None)
        if not want:
            want = next((x for x in cands if x["name"] == DELINA), None)
        if not want:
            continue
        if not ST["opp_logged"]:
            ST["opp_logged"] = True
            wire("delina_target_prompt",
                 {"candidates": cands, "opportunity": opp,
                  "wf_type": wf.get("type")})
        rtype = resp.get("type")
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] target: unexpected prompt shape {rtype}/{spec_type}")
            wire("delina_target_unexpected_shape",
                 {"rtype": rtype, "spec_type": spec_type,
                  "candidates": cands})
            continue
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        ST["target_answers"].append((state.get("turn_number"), want["name"]))
        say(f"[P0] Delina trigger targets {want['name']} "
            f"(turn {state.get('turn_number')})")
        return True
    return False


async def answer_delina_reroll(c, state, st):
    """Handle Delina's 'you may roll again' OptionalEffectChoice (15-20
    branch). Log the full opportunity once, then ACCEPT to test whether the
    reroll actually resolves. Cap answers per turn to avoid an infinite
    chain.

    Any other OptionalEffectChoice for player 0 (e.g. an unimplemented-effect
    continue prompt) is answered with the affirmative choice and logged as
    such, but NOT counted as a reroll prompt for A5: the opportunity text is
    checked for a roll mention."""
    wf = wf_of(state)
    if wf.get("type") != "OptionalEffectChoice":
        return False
    if str(wf_player(state)) != "0":
        return False
    turn = state.get("turn_number")
    for opp in vi_ops(c):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if not iid or iid in SUBMITTED:
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = optional_effect_choices(opp)
        all_text = " ".join(
            [x["text"] for x in cands] + [
                str((opp.get("data") or {}).get("prompt") or ""),
                str((opp.get("data") or {}).get("description") or ""),
                str((wf.get("data") or {}).get("description") or ""),
            ]).lower()
        is_reroll = "roll" in all_text
        if ("reroll", iid) not in SHAPES:
            SHAPES.add(("reroll", iid))
            wire("delina_reroll_prompt",
                 {"turn": turn, "is_reroll": is_reroll,
                  "prompt_text": all_text[:300],
                  "candidates": cands, "opportunity": opp,
                  "wf_data": wf.get("data")})
            say(f"[P0] OptionalEffectChoice (turn {turn}, "
                f"is_reroll={is_reroll}): "
                f"{[(x['choice_id'], x['role'], x['value'], x['text']) for x in cands]}")
            if is_reroll:
                ST["reroll_prompts"].append((turn, iid))
                if turn not in ST["tokens_at_reroll"]:
                    ST["tokens_at_reroll"][turn] = sorted(p0_token_oids(state))
            else:
                ST["other_may_prompts"].append((turn, iid))
        n_answers = sum(1 for t, _, _ in ST["reroll_answers"] if t == turn)
        if n_answers >= 6:
            say(f"[P0] reroll answer cap reached on turn {turn}; not answering")
            wire("reroll_cap", {"turn": turn})
            return False
        # accept = the choice whose value-surface role is 'accept' with value
        # 'true' (decideOptionalEffect convention); fall back to value/text
        want = next((x for x in cands
                     if x["role"] == "accept"
                     and str(x["value"]).lower() == "true"), None)
        if not want:
            want = next((x for x in cands
                         if str(x["value"]).lower() == "true"), None)
        if not want:
            want = next((x for x in cands
                         if any(k in x["text"].lower()
                                for k in ("yes", "roll again", "roll",
                                          "continue"))), None)
        if not want:
            say("[P0] OptionalEffectChoice: no accept choice identifiable; "
                "NOT answering")
            wire("reroll_no_accept_found", {"candidates": cands,
                                            "is_reroll": is_reroll})
            ST["stop"] = True
            ST["stage"] = "UNHANDLED_PROMPT"
            return False
        await send_interaction(c, {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId":
                                                         want["choice_id"]}}})
        SUBMITTED.add(iid)
        ST["reroll_answers"].append((turn, want["text"], want["value"]))
        say(f"[P0] OptionalEffectChoice ACCEPTED (turn {turn}, "
            f"is_reroll={is_reroll}, choice {want['choice_id']})")
        return True
    return False


def p0_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm in (DELINA, BEAR):
            return 2        # key creatures: keep
        if nm in LANDS:
            return 1        # lands next
        return 0            # everything else bottomed/discarded first
    return rank


def p1_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == ELF:
            return 2
        if nm in LANDS:
            return 1
        return 0
    return rank


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


async def p0_land_drop(c, pid, state, acts):
    for ln in (MTN, FOREST):
        lid = find_hand(state, pid, ln)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def p0_develop(c, pid, state, acts):
    """Main-phase development: land drop, then Bear, then Delina."""
    if await p0_land_drop(c, pid, state, acts):
        return True
    if not any(oname(o) == BEAR for _, o in bf(state, pid)):
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            CASTLOG.append((state.get("turn_number"), BEAR))
            say("[P0] casts Bear")
            return True
    if not any(oname(o) == DELINA for _, o in bf(state, pid)):
        oid = find_hand(state, pid, DELINA)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            CASTLOG.append((state.get("turn_number"), DELINA))
            say("[P0] casts Delina")
            return True
    return False


async def p1_develop(c, pid, state, acts):
    lid = find_hand(state, pid, FOREST)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    if my_main(state, pid):
        n_elf = sum(1 for _, o in bf(state, pid) if oname(o) == ELF)
        if n_elf < 4:
            oid = find_hand(state, pid, ELF)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say("[P1] casts Elf")
                return True
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    tag = "P0" if is_p0 else "P1"
    keep_fn = (lambda s: sum(1 for o in hand_oids(s, pid)
                             if oname(s["objects"][o]) in LANDS) >= 2) \
        if is_p0 else \
        (lambda s: sum(1 for o in hand_oids(s, pid)
                       if oname(s["objects"][o]) in LANDS) >= 1)
    if await do_mulligan(c, pid, tag, keep_fn):
        return True
    if await do_bottom(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await do_discard(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await pay_tick(c):
        return True
    if is_p0:
        if await answer_declare_attackers_p0(c, pid):
            return True
        if await answer_declare_blockers_empty(c, pid, "P0"):
            return True
    else:
        if await answer_declare_attackers_empty(c, pid, "P1"):
            return True
        if await answer_declare_blockers_p1(c, pid):
            return True
    if is_p0:
        if await answer_delina_target(c, state, st):
            return True
        if await answer_delina_reroll(c, state, st):
            return True
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, copy.deepcopy(a))
            say(f"{c.name} answers ChooseLegend (keep first)")
            wire("legend_answered", {"player": pid})
            return True
    # never pass while this seat has a decision pending
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    if wt0 in ("OptionalCostChoice", "TargetSelection",
               "TriggerTargetSelection", "OptionalEffectChoice",
               "ManaPayment", "ChooseXValue", "DiscardChoice",
               "ChooseLegend") and str(wplayer) == str(pid):
        return False
    if is_p0 and my_main(state, pid):
        if await p0_develop(c, pid, state, acts):
            return True
    if not is_p0:
        if await p1_develop(c, pid, state, acts):
            return True
    # default: pass priority only when genuinely holding it
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False

# ------------------------------------------------------- main loop

async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    ST["game_code"] = p0.game_code
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            if not c.latest:
                continue
            watch(c)
            rej = drain_rejections(c)
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            if rej:
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
        state = st["state"]
        record_wf(state)
        record_phase(state)

        if wf_of(state).get("type") == "GameOver" and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        turn = state.get("turn_number")
        phase = state.get("phase")
        active = state.get("active_player")

        # --- stage transitions
        if ST["stage"] == "SETUP":
            delina_bf = any(oname(o) == DELINA for _, o in bf(state, 0))
            bear_bf = any(oname(o) == BEAR for _, o in bf(state, 0))
            if delina_bf and ST.get("delina_turn") is None:
                ST["delina_turn"] = turn
                say(f"Delina on BF at turn {turn}")
            # need Delina + (ideally) Bear for the 2-target prompt path;
            # fall back to Delina-alone after 6 turns so a slow Bear draw
            # can't stall setup forever
            if active_main(state, 0) and delina_bf and (
                    bear_bf or (turn - (ST.get("delina_turn") or turn)) >= 6):
                await export_now("pre.json")
                ST["pre_exported"] = True
                ST["stage"] = "ATTACK"
                say(f"=== stage -> ATTACK (bear_bf={bear_bf}) ===")
        elif ST["stage"] == "ATTACK":
            # per-turn pre-attack snapshot (kept in /tmp; the interesting
            # turns are copied into the evidence dir at the end)
            if phase == "PreCombatMain" and active == 0 \
                    and turn not in ST["attack_turns"] \
                    and any(oname(o) == DELINA for _, o in bf(state, 0)):
                ST["attack_turns"].append(turn)
                await export_now(f"pre_attack_{turn}.json", dest_dir=TMPD)
                say(f"--- attack turn {turn} "
                    f"(#{len(ST['attack_turns'])}) ---")
            # token baseline at DeclareAttackers (backfill the attack-turn
            # record if PreCombatMain was missed between polls)
            if phase == "DeclareAttackers" and active == 0 \
                    and any(oname(o) == DELINA for _, o in bf(state, 0)):
                if turn not in ST["attack_turns"]:
                    ST["attack_turns"].append(turn)
                    say(f"--- attack turn {turn} "
                        f"(#{len(ST['attack_turns'])}, backfilled) ---")
                if turn not in ST["tokens_before"]:
                    ST["tokens_before"][turn] = p0_token_oids(state)
            # token delta once the trigger resolved (stack empty after the
            # target was answered), or at DeclareBlockers/CombatDamage.
            # The reroll prompt (if any) snapshots tokens BEFORE the accept,
            # so initial_delta distinguishes the first roll's branch even
            # when a later accepted reroll creates a token.
            if turn in ST["attack_turns"] and turn not in ST["deltas"] \
                    and turn in ST["tokens_before"]:
                answered = any(t == turn for t, _ in ST["target_answers"])
                stack_empty = not state.get("stack")
                if (answered and stack_empty and phase == "DeclareAttackers") \
                        or phase in ("DeclareBlockers", "CombatDamage"):
                    new = p0_token_oids(state) - ST["tokens_before"][turn]
                    ST["deltas"][turn] = sorted(new)
                    say(f"turn {turn}: token delta = {len(new)} "
                        f"(new oids {sorted(new)})")
            # branch classification per attack turn
            prompt_turns = {t for t, _ in ST["reroll_prompts"]}
            trig = any("rigger" in str(k)
                       for k in ST["stack_kinds_by_turn"].get(turn, set()))
            if turn in ST["attack_turns"] and trig:
                if turn in ST["tokens_at_reroll"] \
                        and turn in ST["tokens_before"]:
                    initial = len(set(ST["tokens_at_reroll"][turn])
                                  - ST["tokens_before"][turn])
                elif turn in ST["deltas"]:
                    initial = len(ST["deltas"][turn])
                else:
                    initial = None
                if initial == 1 and ST["low_turn"] is None:
                    ST["low_turn"] = turn
                    say(f"turn {turn}: classified LOW (initial_delta=1)")
                if initial == 0 and ST["high_turn"] is None:
                    ST["high_turn"] = turn
                    say(f"turn {turn}: classified HIGH (initial_delta=0, "
                        f"prompt={'yes' if turn in prompt_turns else 'no'})")
            # exports on the first low / high turns
            if ST["low_turn"] is not None and not ST["mid_low_exported"] \
                    and turn == ST["low_turn"] \
                    and phase in ("DeclareBlockers", "CombatDamage"):
                await export_now("mid_low.json")
                ST["mid_low_exported"] = True
                say(f"=== mid_low exported (turn {turn}, phase={phase}) ===")
            if ST["low_turn"] is not None and not ST["post_low_exported"] \
                    and turn == ST["low_turn"] and phase == "PostCombatMain" \
                    and active == 0 and not state.get("stack"):
                await export_now("post_low.json")
                ST["post_low_exported"] = True
                low_oids = set(ST["deltas"].get(turn, []))
                still = low_oids & p0_token_oids(state)
                ST["exile_check"][turn] = len(still) == 0
                say(f"turn {turn}: low-turn token exiled by PostCombatMain: "
                    f"{len(still) == 0}")
            if ST["high_turn"] is not None and not ST["mid_high_exported"] \
                    and turn == ST["high_turn"] and (
                        wf_of(state).get("type") == "OptionalEffectChoice"
                        or phase in ("DeclareBlockers", "CombatDamage")):
                await export_now("mid_high.json")
                ST["mid_high_exported"] = True
                say(f"=== mid_high exported (turn {turn}, "
                    f"wf={wf_of(state).get('type')}, phase={phase}) ===")
            if ST["high_turn"] is not None and not ST["post_high_exported"] \
                    and turn == ST["high_turn"] and phase == "PostCombatMain" \
                    and active == 0 and not state.get("stack"):
                await export_now("post_high.json")
                ST["post_high_exported"] = True
            # done once we have both branches + closing exports
            if ST["low_turn"] is not None and ST["high_turn"] is not None \
                    and ST["post_low_exported"] and ST["post_high_exported"]:
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("=== both branches observed; DONE ===")
            elif len(ST["attack_turns"]) >= 14 and not ST["stop"]:
                await export_now("mid_watchdog.json")
                obs["notes"].append(
                    f"watchdog: 14 attack turns without both branches "
                    f"(low={ST['low_turn']} high={ST['high_turn']} "
                    f"deltas={ {t: len(v) for t, v in ST['deltas'].items()} })")
                say("=== watchdog fired; stopping ===")
                ST["stage"] = "WATCHDOG"
                ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    # final state export
    try:
        await export_now("post.json")
    except Exception as e:
        obs["notes"].append(f"post.json export failed: {e!r}")

    # copy the per-turn pre-attack states for the decisive turns
    for tag, t in (("low", ST["low_turn"]), ("high", ST["high_turn"])):
        if t is not None:
            src = f"{TMPD}/pre_attack_{t}.json"
            if os.path.exists(src):
                shutil.copy(src, f"{EVDIR}/pre_{tag}.json")
                say(f"copied pre_{tag}.json (turn {t})")

    obs["notes"].append(f"attack turns: {ST['attack_turns']}")
    obs["notes"].append(f"target answers: {ST['target_answers']}")
    obs["notes"].append(
        f"token deltas: { {t: len(v) for t, v in ST['deltas'].items()} }")
    obs["notes"].append(f"low_turn={ST['low_turn']} high_turn={ST['high_turn']}")
    obs["notes"].append(f"reroll prompts: {ST['reroll_prompts']}")
    obs["notes"].append(f"reroll answers: {ST['reroll_answers']}")
    obs["notes"].append(f"other may-prompts: {ST['other_may_prompts']}")
    obs["notes"].append(
        f"tokens_at_reroll: { {t: len(v) for t, v in ST['tokens_at_reroll'].items()} }")
    obs["notes"].append(f"exile_check={ST['exile_check']}")
    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"P0 casts: {CASTLOG}")
    obs["notes"].append(f"stage at end: {ST['stage']}")
    serializable = {str(k): sorted(v) if isinstance(v, set) else v
                    for k, v in ST["stack_kinds_by_turn"].items()}
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"],
                   "phases": PHASES,
                   "casts": CASTLOG,
                   "attack_turns": ST["attack_turns"],
                   "target_answers": ST["target_answers"],
                   "deltas": {str(t): v for t, v in ST["deltas"].items()},
                   "low_turn": ST["low_turn"],
                   "high_turn": ST["high_turn"],
                   "reroll_prompts": ST["reroll_prompts"],
                   "reroll_answers": ST["reroll_answers"],
                   "other_may_prompts": ST["other_may_prompts"],
                   "tokens_at_reroll": {str(t): v
                                        for t, v in
                                        ST["tokens_at_reroll"].items()},
                   "tokens_before": {str(t): sorted(v)
                                     for t, v in ST["tokens_before"].items()},
                   "exile_check": ST["exile_check"],
                   "wf_by_turn": {str(k): v
                                  for k, v in ST["wf_by_turn"].items()},
                   "stack_kinds_by_turn": serializable,
                   "stage": ST["stage"]}, f, indent=2)
    for n in obs["notes"]:
        say(f"NOTE: {n}")

    await p0.close()
    await p1.close()
    return obs, True


# ------------------------------------------------------- evaluate

def load_env(path):
    with open(f"{EVDIR}/{path}") as f:
        return json.load(f)


def assertions_from_evidence(obs):
    """Compute A1..A7 from the saved evidence files."""
    A = {}
    notes = []

    def env(name):
        return load_env(name)

    # A1: setup (Delina on BF; Bear preferred for the 2-target path)
    try:
        s = env("pre.json")["state"]
        bf0 = [v for v in s["objects"].values()
               if v.get("zone") == "Battlefield" and v.get("controller") == 0]
        delina_bf = any(oname(v) == DELINA for v in bf0)
        bear_bf = any(oname(v) == BEAR for v in bf0)
        life = [(p.get("id"), p.get("life")) for p in s.get("players", [])]
        ok = (s.get("phase") == "PreCombatMain"
              and s.get("active_player") == 0 and delina_bf
              and all(l == 20 for _, l in life))
        A["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(
            f"A1: phase={s.get('phase')} active={s.get('active_player')} "
            f"delina_bf={delina_bf} bear_bf={bear_bf} life={life}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        notes.append(f"A1: not-run ({e})")

    obse = load_env("observations.json")
    answers = obse.get("target_answers", [])
    deltas = {int(k): v for k, v in obse.get("deltas", {}).items()}

    # A2: trigger fires
    A["A2_trigger_fires"] = "passed" if any(
        n == BEAR for _, n in answers) else "failed"
    notes.append(f"A2: target answers={answers}")

    # A3: low branch token
    low_turn = obse.get("low_turn")
    try:
        s = env("mid_low.json")["state"]
        new_oids = deltas.get(low_turn, [])
        toks = [s["objects"][o] for o in new_oids if o in s["objects"]]
        attackers = set()
        for a in ((s.get("combat") or {}).get("attackers") or []):
            attackers.add(str(a.get("object_id")))
        checks = []
        detail = {}
        if len(toks) == 1:
            t = toks[0]
            sups = (t.get("card_types") or {}).get("supertypes") or []
            detail = {"oid": new_oids[0], "name": oname(t),
                      "tapped": t.get("tapped"),
                      "attacking": new_oids[0] in attackers,
                      "is_token": t.get("is_token"),
                      "supertypes": sups, "zone": t.get("zone")}
            blob = json.dumps(t)
            checks = [
                oname(t) == BEAR,
                bool(t.get("tapped")),
                new_oids[0] in attackers,
                bool(t.get("is_token")),
                "Legendary" not in sups,
                "exile" in blob.lower(),
            ]
        ok = len(toks) == 1 and all(checks)
        A["A3_low_branch_token"] = "passed" if ok else "failed"
        notes.append(f"A3: low_turn={low_turn} new_oids={new_oids} "
                     f"token={detail} checks={checks}")
    except Exception as e:
        A["A3_low_branch_token"] = "not-run"
        notes.append(f"A3: not-run ({e})")

    # A4: high branch observed (trigger fired, first roll resolved 15-20:
    # no token from the initial resolution)
    high_turn = obse.get("high_turn")
    trig_high = any("rigger" in str(k) for k in
                    obse.get("stack_kinds_by_turn", {}).get(str(high_turn),
                                                            []))
    A["A4_high_branch_observed"] = "passed" if high_turn is not None \
        and trig_high else "failed"
    notes.append(f"A4: high_turn={high_turn} trigger_seen={trig_high} "
                 f"stack_kinds={obse.get('stack_kinds_by_turn', {}).get(str(high_turn))} "
                 f"tokens_at_reroll={obse.get('tokens_at_reroll', {})} "
                 f"final_deltas={ {t: len(v) for t, v in deltas.items()} }")

    # A5: reroll offered on the high turn
    prompts = obse.get("reroll_prompts", [])
    prompts_high = [p for p in prompts if p[0] == high_turn]
    A["A5_reroll_offered"] = "passed" if prompts_high else "failed"
    notes.append(f"A5: reroll prompts on high turn {high_turn}: "
                 f"{prompts_high} (all prompts: {prompts}; "
                 f"other may-prompts: {obse.get('other_may_prompts', [])})")

    # A6: accepting the reroll actually re-rolls. Evidence of a further roll
    # resolution after the accept: a new token appears (1-14 re-roll), or a
    # further reroll prompt appears (another 15-20).
    answers_r = obse.get("reroll_answers", [])
    answers_high = [a for a in answers_r if a[0] == high_turn]
    final_delta = len(deltas.get(high_turn, [])) if high_turn else 0
    works = (final_delta > 0) or (len(prompts_high) >= 2)
    if A["A5_reroll_offered"] == "failed":
        A["A6_reroll_works"] = "not-run"
    else:
        A["A6_reroll_works"] = "passed" if works else "failed"
    notes.append(f"A6: reroll answers on high turn: {answers_high}; "
                 f"final token delta={final_delta}; "
                 f"prompts on turn={len(prompts_high)}")

    # A7: end-of-combat exile on low turns (acceptance criterion)
    try:
        exile_check = obse.get("exile_check", {})
        vals = [v for k, v in exile_check.items()]
        A["A7_exile_ok"] = "passed" if vals and all(vals) else "failed"
        notes.append(f"A7: exile_check={exile_check} (token gone by "
                     f"PostCombatMain on low turns)")
    except Exception as e:
        A["A7_exile_ok"] = "not-run"
        notes.append(f"A7: not-run ({e})")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": notes}, f, indent=2)
    say("assertions: " + json.dumps(A))
    for n in notes:
        say("ANOTE: " + n)
    return A, notes


def compute_verdict(A):
    a1_4 = all(A.get(k) == "passed" for k in
               ("A1_setup_ok", "A2_trigger_fires", "A3_low_branch_token",
                "A4_high_branch_observed"))
    if not A.get("A2_trigger_fires") == "passed":
        return "blocked"
    if a1_4 and (A.get("A5_reroll_offered") == "failed"
                 or (A.get("A5_reroll_offered") == "passed"
                     and A.get("A6_reroll_works") == "failed")):
        return "reproduced"
    if all(A.get(k) == "passed" for k in
           ("A1_setup_ok", "A2_trigger_fires", "A3_low_branch_token",
            "A4_high_branch_observed", "A5_reroll_offered",
            "A6_reroll_works")):
        return "not-reproduced"
    return "blocked"


# ------------------------------------------------------- finalize

def render_summary(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 18
    d.text((24, y),
           "Issue #6866 - Delina, Wild Mage doesn't reroll on 15-20 "
           "(revalidation)", fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           f"server v{si['validated_version']} ({si['build_commit']}) "
           f"protocol {si['protocol_version']} - run {run['run_id']} - "
           f"{run['date']}", fill=(140, 160, 180))
    y += 26
    vcol = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
            "blocked": (230, 200, 90)}[run["verdict"]]
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 32
    # state panels derived from saved states
    try:
        obse = load_env("observations.json")
        deltas = {int(k): len(v)
                  for k, v in obse.get("deltas", {}).items()}
    except Exception:
        obse, deltas = {}, {}
    d.text((24, y), "Observed (from saved states):", fill=(200, 210, 225))
    y += 22
    for line in [
        f"attack turns: {obse.get('attack_turns', [])}",
        f"token deltas per turn: {deltas}",
        f"low_turn={obse.get('low_turn')} high_turn={obse.get('high_turn')}",
        f"reroll prompts seen: {obse.get('reroll_prompts', [])}",
        f"reroll answers: {[(t, v) for t, _, v in obse.get('reroll_answers', [])]}",
        f"low-turn exile by PostCombatMain: {obse.get('exile_check', {})}",
    ]:
        d.text((40, y), line[:120], fill=(150, 165, 185))
        y += 20
    y += 6
    d.text((24, y), "Assertions (A1..A7 from saved states):",
           fill=(200, 210, 225))
    y += 22
    labels = {
        "A1_setup_ok": "A1 setup: P0 PreCombatMain, Delina on BF, 20/20",
        "A2_trigger_fires": "A2 Delina trigger target answered (Bear)",
        "A3_low_branch_token": "A3 1-14: 1 token, tapped+attacking, Bear "
                               "copy, non-legendary, exile granted",
        "A4_high_branch_observed": "A4 15-20: trigger fired, 0 new tokens",
        "A5_reroll_offered": "A5 'you may roll again' prompt offered",
        "A6_reroll_works": "A6 accepting the reroll re-rolls",
        "A7_exile_ok": "A7 low-turn token exiled by PostCombatMain",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed"
                                             else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col)
        y += 20
    y += 6
    d.text((24, y), "Limitations: native engine via two human-client seats; "
                    "die-roll value not exposed to the client;",
           fill=(120, 130, 150))
    y += 20
    d.text((24, y), "15-20 identified by effect signature (trigger fired, 0 "
                    "tokens). States restorable only via full game replay.",
           fill=(120, 130, 150))
    d.text((24, H - 28), "Evidence: ntindle/phase-bug-state-evidence 6866/"
                         + run["run_id"] + " (manifest.sha256)",
           fill=(120, 130, 150))
    out = f"{EVDIR}/summary.png"
    img.save(out)
    say(f"wrote {out}")


def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    done = False
    for n in range(1, 3):
        reset_globals()
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = {"assert": {},
                         "notes": [f"attempt {n} crash: {e!r}"]}, False
        if done:
            break
        say(f"attempt {n} did not complete; starting a new game")
    if not done:
        obs["notes"].append("all attempts exhausted without completing")
        with open(f"{EVDIR}/observations.json", "w") as f:
            json.dump({"notes": obs["notes"], "stage": ST.get("stage")}, f,
                      indent=2)

    A, anotes = assertions_from_evidence(obs)
    obs["notes"].extend(anotes)
    verdict = compute_verdict(A)
    say(f"VERDICT: {verdict}")

    obse = {}
    try:
        obse = load_env("observations.json")
    except Exception:
        pass
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    run = {
        "issue": 6866,
        "run_id": RUN_ID,
        "date": "2026-10-01",
        "started_at": now_iso,
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario": "scenario_6866_0980.py",
        "scenario_sha256": sha256_of_file(
            f"{BACKFILL}/driver/scenario_6866_0980.py"),
        "decks": {"P0": [[n, c] for n, c in P0_DECK],
                  "P1": [[n, c] for n, c in P1_DECK]},
        "observed": {
            "attack_turns": obse.get("attack_turns", []),
            "target_answers": obse.get("target_answers", []),
            "token_deltas": {t: len(v)
                             for t, v in obse.get("deltas", {}).items()},
            "low_turn": obse.get("low_turn"),
            "high_turn": obse.get("high_turn"),
            "reroll_prompts": obse.get("reroll_prompts", []),
            "reroll_answers": obse.get("reroll_answers", []),
            "exile_check": obse.get("exile_check", {}),
        },
        "assertions": A,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "Die-roll value not exposed to the client; 15-20 identified by effect signature (trigger fired, 0 tokens).",
            "States are authoritative exports, restorable only via full game replay (scenario_6866_0980.py).",
        ],
        "notes": obs["notes"],
        "wf_sequence": WF_SEEN,
        "casts": CASTLOG,
        "contract": ("Delina attacks -> target Bear -> roll d20: 1-14 must "
                     "create a tapped+attacking non-legendary Bear token with "
                     "end-of-combat exile; 15-20 must create the token and "
                     "offer 'you may roll again'."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(f"{BACKFILL}/driver/scenario_6866_0980.py",
                f"{EVDIR}/scenario_6866_0980.py")
    render_summary(run)
    WIRE.close()
    RUNLOG.close()
    write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(A)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
