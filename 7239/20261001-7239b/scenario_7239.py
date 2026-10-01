#!/usr/bin/env python3
"""Issue #7239 on v0.98.0 (protocol 94): Hot Pursuit triggers with no players
eliminated and does not untap gained creatures.

Reported (GitHub, 4-player Commander): with no players eliminated, at the
beginning of the Hot Pursuit controller's combat the ability still resolved:
gained control of the goaded/suspected creatures (they gained haste) but
tapped creatures did not untap. Expected: with <2 players eliminated the
intervening condition is false, so nothing should happen.

Oracle: "When this enchantment enters, suspect target creature an opponent
controls. As long as this enchantment remains on the battlefield, that
creature is also goaded. At the beginning of combat on your turn, if two or
more players have lost the game, gain control of all goaded and/or suspected
creatures until end of turn. Untap them. They gain haste until end of turn."

Pinned v0.98.0 card-data parse of the BeginCombat trigger (defective):
  - condition: null (the "if two or more players have lost the game" clause
    is dropped) -> fires with 0 players eliminated
  - GainControlAll with target type_filters: [] controller: null (filter: any)
    -> takes control of ALL permanents, not just goaded/suspected creatures
  - SetTapState Untap with target SelfRef -> untaps Hot Pursuit itself,
    not the gained creatures
  - haste via GenericEffect UntilEndOfTurn on ParentTarget (works)

Plan (four human seats, native engine, 20-life free-for-all):
  P0: 8x Hot Pursuit, 52x Mountain. P1: 8x Grizzly Bears, 52x Forest.
  P2/P3: 60x Forest (draw-go).
  SETUP   - land drops; P1 casts Grizzly Bears (T2); P0 holds Hot Pursuit.
  MANEUVER- P1 attacks P0 with Bears on P1's T3 (taps it). P0 holds.
  READY   - P0 main phase with tapped P1 Bears on bf, all players alive:
            export pre.json.
  CASTING - P0 casts Hot Pursuit; ETB suspects Bears; wait for resolution.
  OBSERVE - pass to P0's BeginCombat; the trigger should NOT fire (0
            eliminated). Record any control change / untap / haste, export
            post.json after the resolution settles, stop.

Behavioral contract:
  A1 setup_ok        pre.json: Hot Pursuit castable state reached with Bears
                     on P1's bf tapped, all 4 players not eliminated
  A2 no_spurious_control_change
                     no battlefield permanent changes controller pre->post
                     (FAILED = bug: trigger fired with 0 players eliminated)
  A3 scope_correct    (only if fired) gained set == suspected/goaded only
  A4 untap_correct    (only if fired) tapped gained creatures untap
  A5 haste_granted    (only if fired) gained creatures gain haste

Verdict = reproduced iff A2 fails; not-reproduced iff A1 and A2 pass;
blocked otherwise.

Protocol-94 driver (v0.98.0): HELLO advertises protocol 94 (server enforces
exact match); MulliganDecision as {"choice":{"type":"Keep"}} gated on the
seat's pending Declare; BottomCards/DiscardToHandSize via single SelectCards
{"cards":[...]} (int oids); DeclareAttackers/Blockers via relations-schema
interaction; CastSpell via advertised actions; PayMana* via pay_tick;
TargetSelection via choose/sequence per response kind; PassPriority only when
the seat genuinely holds Priority; per-client stale watchdog.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario7239")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-7239b"
EVDIR = f"{BACKFILL}/evidence/7239/{EVID_RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)

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

HP = "Hot Pursuit"
BEARS = "Grizzly Bears"
MOUNTAIN = "Mountain"
FOREST = "Forest"

ST = {"stage": "SETUP", "stop": False, "p0_mains": 0, "p0_main_sig": None,
      "cast_turn": None, "hp_resolved": False,
      "fired": None, "fired_at": 0, "quiet_t0": None,
      "etb_answered": False, "suspected_seen": None}
CAST = {}
MULLS = {}
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
_WATCH = {}
BASE_CTRL = {}   # oid -> controller at pre.json

DECISION_TYPES = ("OptionalCostChoice", "TargetSelection", "ManaPayment",
                  "ChooseXValue", "DiscardChoice", "OptionalEffectChoice")


# ------------------------------------------------------------------ helpers

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


def bf_of(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_oid(state, pid, name):
    for oid, o in bf_of(state, pid):
        if oname(o) == name:
            return oid
    return None


def untapped_count(state, pid, name):
    return sum(1 for _, o in bf_of(state, pid)
               if oname(o) == name and not o.get("tapped"))


def my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


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
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "stage": ST["stage"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action, "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub, "stage": ST["stage"]})
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
            view = (f"turn={s.get('turn')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} wf={json.dumps(wf_of(s), default=str)[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 45s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def stack_empty(state):
    return not any(o.get("zone") == "Stack" for o in state["objects"].values())


def all_alive(state):
    pls = state.get("players", []) or []
    return all(not p.get("is_eliminated") for p in pls) and len(pls) == 4


def bears_obj(state):
    """The Grizzly Bears object regardless of controller (there is only one)."""
    for oid, o in state["objects"].items():
        if oname(o) == BEARS and o.get("zone") == "Battlefield":
            return oid, o
    return None, None


def is_suspected(o):
    # structural status field (NOT a substring search: the card's own rules
    # text and lki snapshots contain the word "suspect")
    return o.get("is_suspected") is True


def has_haste(o):
    # granted keyword list (NOT a substring search: the card's own rules text
    # contains "haste")
    return "Haste" in (o.get("keywords") or [])


# ------------------------------------------------------- shared p94 ticks

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
    await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": choice}}})
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
    picks = [int(x) for x in sorted(hand_oids(state, pid), key=rank_fn(state))[:n]]
    say(f"[{tag}] bottoming {n}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


_DISCARD_REV = {}


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
    picks = [int(x) for x in sorted(hand_oids(state, pid), key=rank_fn(state))[:n]]
    say(f"[{tag}] discarding to hand size: {n}")
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
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return op
    return None


async def answer_declare_empty(c, pid, tag):
    """Declare no attackers / no blockers via the relations schema."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": []}}}
    wire("declare_empty", {"who": tag, "wf": wf.get("type")})
    say(f"[{tag}] declares empty ({wf.get('type')})")
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


async def answer_declare_attack(c, pid, tag, attacker_name, target_seat):
    """Declare attackers: each advertised attacker of attacker_name attacks
    the target_seat player (single relation per source)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") != "DeclareAttackers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    data = (op.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}

    def cand_obj_name(cid):
        ch = cands.get(cid, {})
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "object":
                ref = str((s.get("data") or {}).get("reference"))
                return oname(state["objects"].get(ref, {}))
        return None

    def cand_seat(cid):
        ch = cands.get(cid, {})
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if dd.get("seat") is not None:
                return str(dd.get("seat"))
        return None

    seen_src = set()
    rels = []
    for e in edges:
        src = e.get("sourceId")
        tids = e.get("targetIds") or []
        if not src or not tids or src in seen_src:
            continue
        if cand_obj_name(src) != attacker_name:
            continue
        pick = next((t for t in tids if cand_seat(t) == str(target_seat)), tids[0])
        rels.append({"sourceId": src, "targetId": pick, "group": None})
        seen_src.add(src)
    iid = op.get("interactionId") or op.get("id")
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": rels}}}
    wire("declare_attack", {"who": tag, "n_edges": len(edges), "n_rels": len(rels)})
    say(f"[{tag}] declares attack: {len(rels)} relations ({len(edges)} edges)")
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


async def answer_suspect_target(c, pid, tag):
    """Answer P0's Hot Pursuit ETB Suspect TargetSelection with the Bears."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") != "TargetSelection":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    vi = c.latest.get("viewer_interaction") or {}
    wire("suspect_target_opportunity", vi)
    for op in vi.get("opportunities", []) or []:
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = data.get("candidates", []) or data.get("choices", []) or []
        pick = None
        for ch in cands:
            for s in ch.get("surfaces", []) or []:
                if s.get("type") == "object":
                    ref = str((s.get("data") or {}).get("reference"))
                    if oname(state["objects"].get(ref, {})) == BEARS:
                        pick = ch
                        break
            if pick:
                break
        if pick is None and len(cands) == 1:
            pick = cands[0]
        if pick is None:
            continue
        iid = op.get("interactionId") or op.get("id")
        if resp.get("type") == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose", "data": {"choiceId": pick["id"]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence", "data": {"choiceIds": [pick["id"]]}}}
        wire("suspect_target_submit", {"pick_id": pick.get("id"), "kind": sub["response"]["type"]})
        say(f"[{tag}] ETB suspect target -> {pick.get('id')}")
        await send_interaction(c, sub)
        SUBMITTED.add(iid)
        ST["etb_answered"] = True
        return True
    say(f"[{tag}] TargetSelection: no Bears candidate found")
    return False


def p0_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == HP:
            return 3
        if nm == MOUNTAIN:
            return 2
        return 0
    return rank


def p1_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == BEARS:
            return 3
        if nm == FOREST:
            return 2
        return 0
    return rank


def flat_rank(state):
    def rank(oid):
        return 0
    return rank


# ------------------------------------------------------- per-seat ticks

def hp_on_bf(state, pid):
    return bf_oid(state, pid, HP) is not None


def bears_tapped_p1(state):
    oid, o = bears_obj(state)
    return bool(oid and o.get("controller") == 1 and o.get("tapped"))


async def cast_hp_step(c, pid, state, acts):
    """P0 casts Hot Pursuit once Bears is tapped on P1's side (or fallback)."""
    if CAST.get("done"):
        return False
    if not CAST.get("in_flight"):
        if not (bears_tapped_p1(state) or ST["p0_mains"] >= 6):
            return False
        hid = find_hand(state, pid, HP)
        if not hid:
            say("[P0] Hot Pursuit not in hand; holding")
            return False
        if untapped_count(state, pid, MOUNTAIN) < 2:
            return False
        a = castspell_advertised(acts, hid)
        CAST.update({"oid": str(hid), "in_flight": True, "offered": bool(a),
                     "stack_seen": False, "bf_seen": False,
                     "rev_at_submit": c.revision, "wall_at_submit": time.time()})
        say(f"[P0] casting Hot Pursuit oid={hid} offered={bool(a)}")
        wire("hp_cast_attempt", {"oid": hid, "offered": bool(a)})
        if a:
            await submit_as_is(c, a)
        else:
            obj = state["objects"].get(str(hid), {})
            cid = obj.get("card_id", int(hid))
            raw = {"type": "CastSpell",
                   "data": {"object_id": int(hid), "card_id": int(cid),
                            "targets": [], "payment_mode": {"type": "Auto"}}}
            await submit_as_is(c, raw)
        return True
    # in flight: track stack -> battlefield
    oid = CAST["oid"]
    o = state["objects"].get(str(oid), {})
    if o.get("zone") == "Stack" and not CAST["stack_seen"]:
        CAST["stack_seen"] = True
        wire("hp_stack_seen", {"oid": oid})
        say("[P0] Hot Pursuit on stack")
    if o.get("zone") == "Battlefield" and o.get("controller") == pid \
            and not CAST["bf_seen"]:
        CAST["bf_seen"] = True
        ST["cast_turn"] = state.get("turn")
        wire("hp_bf_seen", {"oid": oid, "turn": ST["cast_turn"]})
        say(f"[P0] Hot Pursuit on battlefield (turn {ST['cast_turn']})")
    if CAST["bf_seen"]:
        CAST["in_flight"] = False
        CAST["done"] = True
        return True
    if time.time() - CAST["wall_at_submit"] > 120 and not CAST["stack_seen"]:
        say("[P0] Hot Pursuit cast silent-fail; giving up")
        CAST["in_flight"] = False
        CAST["done"] = True
        CAST["silent_fail"] = True
        return True
    return False


async def tick(c, pid, role):
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], merged_actions(st)
    tag = role
    # mulligans
    if role == "P0":
        if await do_mulligan(c, pid, tag, lambda s: sum(
                1 for o in hand_oids(s, pid)
                if oname(s["objects"][o]) == MOUNTAIN) >= 2):
            return True
    elif role == "P1":
        if await do_mulligan(c, pid, tag, lambda s: sum(
                1 for o in hand_oids(s, pid)
                if oname(s["objects"][o]) == FOREST) >= 2):
            return True
    else:
        if await do_mulligan(c, pid, tag, lambda s: True):
            return True
    rank = p0_rank if role == "P0" else (p1_rank if role == "P1" else flat_rank)
    if await do_bottom(c, pid, tag, rank):
        return True
    if await do_discard(c, pid, tag, rank):
        return True
    if await pay_tick(c):
        return True
    # declares
    if role == "P1" and ST["stage"] == "MANEUVER":
        if await answer_declare_attack(c, pid, tag, BEARS, 0):
            return True
    if await answer_declare_empty(c, pid, tag):
        return True
    # P0 ETB suspect targeting
    if role == "P0" and ST["stage"] == "CASTING":
        if await answer_suspect_target(c, pid, tag):
            return True
    # role main-phase actions
    if my_main(state, pid):
        if role == "P0":
            # state["turn"] is None on this build; count mains by phase signature
            sig = (state.get("active_player"), state.get("phase"))
            if sig != ST["p0_main_sig"]:
                ST["p0_main_sig"] = sig
                ST["p0_mains"] += 1
            # land drop
            lid = find_hand(state, pid, MOUNTAIN)
            for a in acts:
                if a["type"] == "PlayLand" and lid and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            if ST["stage"] == "CASTING":
                if await cast_hp_step(c, pid, state, acts):
                    return True
        elif role == "P1":
            lid = find_hand(state, pid, FOREST)
            for a in acts:
                if a["type"] == "PlayLand" and lid and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            # cast Bears on T2+
            if not bf_oid(state, pid, BEARS) and ST["stage"] == "SETUP":
                eid = find_hand(state, pid, BEARS)
                a = castspell_advertised(acts, eid) if eid else None
                if eid and a and untapped_count(state, pid, FOREST) >= 2:
                    await submit_as_is(c, a)
                    say(f"[P1] casts {BEARS}")
                    return True
        else:
            lid = find_hand(state, pid, FOREST)
            for a in acts:
                if a["type"] == "PlayLand" and lid and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    # never pass while this seat has a real decision pending
    wt0 = wf_of(state).get("type")
    if wt0 in DECISION_TYPES and str(wf_player(state)) == str(pid):
        return False
    # default: pass priority only when genuinely holding it
    if wt0 == "Priority" and str(wf_player(state)) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False


def controller_snapshot(state):
    return {oid: o.get("controller") for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield"}


def detect_control_change(state):
    """Compare live battlefield controllers to the pre.json baseline."""
    changed = []
    for oid, ctrl in controller_snapshot(state).items():
        if oid in BASE_CTRL and BASE_CTRL[oid] != ctrl:
            o = state["objects"][oid]
            changed.append({"oid": oid, "name": oname(o),
                            "was": BASE_CTRL[oid], "now": ctrl,
                            "tapped": bool(o.get("tapped"))})
    return changed


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    clients = []
    try:
        p0 = PhaseClient("P0"); await p0.connect(); clients.append(p0)
        say("P0 creating 4-player game...")
        await p0.create(deck((HP, 8), (MOUNTAIN, 52)), player_count=4)
        decks = {"P0": [[HP, 8], [MOUNTAIN, 52]]}
        for i, (nm, dk) in enumerate(
                (("P1", deck((BEARS, 8), (FOREST, 52))),
                 ("P2", deck((FOREST, 60))),
                 ("P3", deck((FOREST, 60))),), start=1):
            c = PhaseClient(nm); await c.connect(); clients.append(c)
            say(f"{nm} joining...")
            await c.join(p0.game_code, dk)
            decks[nm] = dk
        seats = [(c, c.player_id, c.name) for c in clients]
        say(f"game {p0.game_code}; seats {[(n, p) for _, p, n in seats]}")
        wire("game", {"code": p0.game_code,
                      "seats": {n: p for _, p, n in seats}})

        global C0
        C0 = p0

        last_rev = {}
        force_tick = {}
        last_tick_wall = {}
        TIMEOUT = 2400
        while time.time() - t0 < TIMEOUT and not ST["stop"]:
            await asyncio.sleep(0.25)
            now = time.time()
            for c, pid, role in seats:
                watch(c)
                rej = drain_rejections(c)
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
                    if await tick(c, pid, role):
                        last_rev[c.name] = c.revision
                except Exception as e:
                    say(f"tick error {c.name}: {e}")
            st = p0.latest
            if not st:
                continue
            record_wf(st["state"])
            state = st["state"]

            # ---- stage transitions ----
            if ST["stage"] == "SETUP":
                boid0, bo0 = bears_obj(state)
                if boid0 and bo0.get("controller") == 1 and stack_empty(state):
                    say("=== stage -> MANEUVER (Bears on P1's battlefield) ===")
                    wire("stage_maneuver", {})
                    ST["stage"] = "MANEUVER"
                    continue
            if ST["stage"] == "MANEUVER" and bears_tapped_p1(state):
                say("=== stage -> READY (Bears tapped on P1's side) ===")
                wire("stage_ready", {})
                ST["stage"] = "READY"
                continue
            if ST["stage"] == "READY" and my_main(state, 0):
                await export_now("pre.json")
                pre_env = json.loads(open(f"{EVDIR}/pre.json").read())
                BASE_CTRL.update(controller_snapshot(pre_env["state"]))
                wire("baseline_controllers", {"n": len(BASE_CTRL)})
                say(f"=== stage -> CASTING (pre.json exported, {len(BASE_CTRL)} bf permanents) ===")
                ST["stage"] = "CASTING"
                continue
            if ST["stage"] == "CASTING" and CAST.get("done") and not CAST.get("silent_fail"):
                if hp_on_bf(state, 0) and stack_empty(state) and not (
                        wf_of(state).get("type") == "TargetSelection"
                        and str(wf_player(state)) == "0"):
                    if ST["quiet_t0"] is None:
                        ST["quiet_t0"] = now
                    if now - ST["quiet_t0"] > 5:
                        boid1, bo1 = bears_obj(state)
                        ST["suspected_seen"] = bool(boid1 and is_suspected(bo1))
                        ST["hp_resolved"] = True
                        say(f"=== stage -> OBSERVE (cast settled; bears suspected={ST['suspected_seen']}) ===")
                        wire("stage_observe", {"suspected": ST["suspected_seen"]})
                        ST["stage"] = "OBSERVE"
                        ST["quiet_t0"] = None
                        continue
                else:
                    ST["quiet_t0"] = None
            if ST["stage"] == "CASTING" and CAST.get("silent_fail"):
                say("CASTING silent-fail; exporting post and stopping")
                await export_now("post.json")
                ST["stop"] = True
                break
            if ST["stage"] == "OBSERVE":
                # log any stack objects at P0's BeginCombat (the trigger itself)
                if state.get("active_player") == 0 and state.get("phase") == "BeginCombat":
                    for oid, o in state["objects"].items():
                        if o.get("zone") == "Stack":
                            wire("stack_at_begin_combat",
                                 {"oid": oid, "name": oname(o),
                                  "desc": str(o.get("description") or "")[:200]})
                changed = detect_control_change(state)
                if changed and ST["fired"] is None:
                    ST["fired"] = {"turn": state.get("turn"),
                                   "phase": state.get("phase"),
                                   "active": state.get("active_player"),
                                   "changed": changed}
                    ST["fired_at"] = now
                    say(f"!!! CONTROL CHANGE DETECTED: {json.dumps(changed)} "
                        f"(turn={state.get('turn')} phase={state.get('phase')})")
                    wire("control_change", ST["fired"])
                if ST["fired"] is not None:
                    # export post.json as soon as the trigger has resolved
                    # (stack empty); do NOT let the turn advance — the control
                    # change and haste are until-end-of-turn.
                    if stack_empty(state):
                        await export_now("post.json")
                        say("=== post.json exported right after trigger resolution; stop ===")
                        ST["stop"] = True
                        break
                    if now - ST["fired_at"] > 20:
                        say("WARNING: stack never emptied after control change; "
                            "exporting post anyway")
                        await export_now("post.json")
                        ST["stop"] = True
                        break
                else:
                    # no firing: stop once P0's combat is fully behind us
                    if state.get("active_player") == 0 and state.get("phase") in (
                            "PostCombatMain", "EndOfTurn", "Cleanup") \
                            and ST.get("hp_resolved"):
                        await export_now("post.json")
                        say("=== P0 combat passed with no control change; stop ===")
                        ST["stop"] = True
                        break
            # safety: if we never got going, don't spin forever pre-READY
            if ST["stage"] in ("SETUP", "MANEUVER") and now - t0 > 1200:
                say(f"stalled in {ST['stage']}; exporting post and stopping")
                await export_now("post.json")
                ST["stop"] = True
                break

        # ------------------------------------------------------- evaluate
        def load_state(path):
            env = json.load(open(f"{EVDIR}/{path}"))
            return env["state"]

        try:
            pre = load_state("pre.json")
            pre_ok = True
        except Exception as e:
            pre = None
            pre_ok = False
            obs["notes"].append(f"pre.json missing: {e}")

        if pre_ok:
            boid, bo = bears_obj(pre)
            bears_ok = bool(boid and bo.get("controller") == 1 and bo.get("tapped"))
            alive_ok = all_alive(pre)
            A["A1_setup_ok"] = "passed" if bears_ok and alive_ok else "failed"
            # NOTE: pre.json is exported BEFORE P0 casts Hot Pursuit (READY gate),
            # so HP-on-bf is not expected in pre; the cast happens in CASTING.
            obs["notes"].append(
                f"pre: bears controller={bo.get('controller') if boid else None} "
                f"tapped={bo.get('tapped') if boid else None} "
                f"suspected={is_suspected(bo) if boid else None} "
                f"all_alive={alive_ok}")
            # re-check: HP is cast AFTER pre.json; setup requires the cast to
            # have completed (post.json) for the observation to be meaningful.
            try:
                post0 = load_state("post.json")
                hp_cast_ok = hp_on_bf(post0, 0)
            except Exception:
                hp_cast_ok = False
            if not hp_cast_ok:
                A["A1_setup_ok"] = "failed"
                obs["notes"].append("Hot Pursuit never reached P0's battlefield")
        else:
            A["A1_setup_ok"] = "not-run"

        fired = ST["fired"]
        changed_names = sorted({c["name"] for c in (fired["changed"] if fired else [])})
        if pre_ok and fired:
            A["A2_no_spurious_control_change"] = "failed"
            obs["notes"].append(
                f"BUG: BeginCombat trigger fired with 0 players eliminated "
                f"(turn={fired['turn']} phase={fired['phase']} active={fired['active']}); "
                f"control changed: {changed_names}")
        elif pre_ok and not fired and A["A1_setup_ok"] == "passed":
            A["A2_no_spurious_control_change"] = "passed"
            obs["notes"].append("no control change between pre.json and post.json")
        else:
            A["A2_no_spurious_control_change"] = "not-run"

        # A3/A4/A5: only meaningful if the trigger fired
        if fired:
            try:
                post = load_state("post.json")
            except Exception:
                post = None
            # A3: scope — only suspected/goaded creatures should be gained
            post_bears_susp = None
            if post is not None:
                for oid, o in post["objects"].items():
                    if (o.get("base_name") or "") == BEARS and o.get("zone") == "Battlefield":
                        post_bears_susp = is_suspected(o)
                        break
            non_bears_gained = [c for c in fired["changed"] if c["name"] != BEARS]
            if not non_bears_gained and post_bears_susp:
                A["A3_scope_correct"] = "passed"
            elif not non_bears_gained:
                A["A3_scope_correct"] = "passed"
                obs["notes"].append("only Bears changed control (suspect marker status uncertain)")
            else:
                A["A3_scope_correct"] = "failed"
            obs["notes"].append(f"A3: gained permanents={changed_names} "
                                f"(bears is_suspected in post={post_bears_susp})")
            # A4: untap — tapped gained creatures should untap
            if post is not None:
                tapped_gained = [c for c in fired["changed"] if c["tapped"]]
                if not tapped_gained:
                    A["A4_untap_correct"] = "not-run"
                    obs["notes"].append("A4: no tapped permanent changed control; untap clause untestable")
                else:
                    still_tapped = []
                    for c in tapped_gained:
                        o = post["objects"].get(c["oid"], {})
                        if o.get("tapped"):
                            still_tapped.append(c["name"])
                    A["A4_untap_correct"] = "failed" if still_tapped else "passed"
                    obs["notes"].append(f"A4: tapped gained={ [c['name'] for c in tapped_gained] }; "
                                        f"still tapped in post={still_tapped}")
                # A5: haste — granted "until end of turn" to the gained creatures;
                # post.json is exported immediately after resolution, same turn.
                no_haste = []
                for c in fired["changed"]:
                    o = post["objects"].get(c["oid"], {})
                    core = ((o.get("card_types") or {}).get("core_types")) or []
                    if "Creature" in core and not has_haste(o):
                        no_haste.append(oname(o) or c["oid"])
                A["A5_haste_granted"] = "failed" if no_haste else "passed"
                bkw = None
                for oid, o in post["objects"].items():
                    if (o.get("base_name") or "") == BEARS and o.get("zone") == "Battlefield":
                        bkw = o.get("keywords")
                        break
                obs["notes"].append(f"A5: gained creatures missing Haste keyword in post={no_haste}; "
                                    f"bears keywords={bkw}")
            else:
                A["A4_untap_correct"] = "not-run"
                A["A5_haste_granted"] = "not-run"
        else:
            A["A3_scope_correct"] = "not-run"
            A["A4_untap_correct"] = "not-run"
            A["A5_haste_granted"] = "not-run"

        if A["A2_no_spurious_control_change"] == "failed":
            verdict = "reproduced"
        elif A["A1_setup_ok"] == "passed" and A["A2_no_spurious_control_change"] == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"

        obs["notes"].append(f"WF sequence: {WF_SEEN}")
        obs["notes"].append(f"stages reached: {ST['stage']}; fired={bool(fired)}; "
                            f"p0_mains={ST['p0_mains']}")
        obs["notes"].append("protocol-94 driver (v0.98.0): 4 human seats; "
                            "MulliganDecision as {choice:{type:Keep}}; SelectCards for "
                            "bottom/discard; relations-schema DeclareAttackers/Blockers; "
                            "CastSpell via advertised actions; PayMana* via pay_tick; "
                            "ETB Suspect TargetSelection via choose/sequence; "
                            "priority-gated passes; stale-client watchdog.")

        run = {
            "issue": 7239,
            "run_id": EVID_RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
            "duration_s": round(time.time() - t0, 1),
            "server_identity": SERVER_IDENTITY,
            "server_note": "shared process on 127.0.0.1:9374 started by run 20261001-7219 (not restarted); identity re-verified by hash at scenario import",
            "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
            "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_7239.py"),
            "decks": decks,
            "assertions": A,
            "notes": obs["notes"],
            "wf_sequence": WF_SEEN,
            "trigger_firing": fired,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via four human-client seats.",
                "20-life four-player free-for-all, not Commander (40 life); the reported defect (missing 2+-eliminated condition) is format-independent.",
                "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
                "States are authoritative exports, restorable only via full game replay (scenario_7239.py).",
            ],
            "setup_line": "P0: 8x Hot Pursuit, 52x Mountain; P1: 8x Grizzly Bears, 52x Forest; P2/P3: 60x Forest",
            "contract_line": ("With 0 players eliminated and a tapped suspected Bears on P1's battlefield, "
                              "P0's BeginCombat trigger must not change any permanent's controller."),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": A, "notes": obs["notes"],
                       "wf_sequence": WF_SEEN, "trigger_firing": fired}, f, indent=2)
        with open(f"{EVDIR}/scenario_7239.py", "w") as f:
            f.write(open(f"{BACKFILL}/driver/scenario_7239.py").read())
        await render_summary(run, f"{EVDIR}/summary.png")
        WIRE.close()
        RUNLOG.close()
        await write_manifest()
        print(f"DONE verdict={verdict} assertions={json.dumps(A)}", flush=True)
        return verdict, A
    finally:
        for c in clients:
            try:
                await c.close()
            except Exception:
                pass


async def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 780
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 24
    d.text((24, y), "phase-rs/phase #7239 — Hot Pursuit", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "trigger fires with 0 players eliminated; tapped gained creatures stay tapped",
           fill=(150, 160, 180))
    y += 30
    v = run["verdict"]
    vc = (255, 120, 120) if v == "reproduced" else ((120, 255, 150) if v == "not-reproduced" else (255, 220, 120))
    d.text((24, y), f"verdict: {v}", fill=vc)
    y += 30
    for k in ("A1_setup_ok", "A2_no_spurious_control_change", "A3_scope_correct",
              "A4_untap_correct", "A5_haste_granted"):
        val = run["assertions"].get(k, "not-run")
        col = {"passed": (120, 255, 150), "failed": (255, 120, 120)}.get(val, (200, 200, 200))
        d.text((24, y), f"{k}: {val}", fill=col)
        y += 22
    y += 8
    tf = run.get("trigger_firing")
    if tf:
        d.text((24, y), f"fired at turn={tf['turn']} phase={tf['phase']} active={tf['active']}",
               fill=(255, 200, 120))
        y += 22
        names = sorted({c["name"] for c in tf["changed"]})
        d.text((24, y), f"control changed ({len(tf['changed'])}): {', '.join(names[:12])}",
               fill=(255, 200, 120))
        y += 26
    for n in run["notes"][:6]:
        d.text((24, y), str(n)[:118], fill=(140, 150, 170))
        y += 20
    d.text((24, H - 52), f"server {si['validated_version']} build {si['build_commit']} "
                         f"proto {si['protocol_version']} | run {run['run_id']}", fill=(120, 130, 150))
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7239/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
