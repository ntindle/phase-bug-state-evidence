#!/usr/bin/env python3
"""Issue #6865 revalidation on v0.98.0 (protocol 94): Call Forth the Tempest.

Reported (Discord): "[[call forth the tempest]] doesn't deal damage based on
cc of cards played before."

Oracle: "Cascade, cascade. Call Forth the Tempest deals damage to each
creature your opponents control equal to the total mana value of other
spells you've cast this turn."

The 2026-09-11 v0.80.0 run (20260911-6865c, protocol 69, evidence commit
270202b9584d3082925c353965f54c50871fcc3d) found NOT-REPRODUCED: Bolt (MV1)
+ Hammer (MV2) + Tempest cast on the same turn, Bear destroyed, Spider took
exactly 3. The pinned v0.98.0 card data still parses the damage clause as
DamageAll(amount=Ref(PropertyAggregate(Sum, ManaValue,
TurnJournal(SpellsCast, Controller, Typed[Card, OtherThanTriggerObject])))),
so this run re-tests resolution on the new build per the staleness rule.

Plan (two human seats, native engine, v0.98.0 / protocol 94):
  SETUP  - land drops; P1 casts Grizzly Bears (2/2) + Giant Spider (2/4);
           P0 holds Lightning Bolt (MV1), Volcanic Hammer (MV2),
           Call Forth the Tempest (MV8: {5}{R}{R}{R}).
  PROOF  - first P0 PreCombatMain with >=11 untapped Mountains and all three
           spells in hand: export pre.json; cast Bolt, Hammer, Tempest on
           the same turn (targets answered as prompts appear, mana via
           pay_tick). Cascade triggers: decline any may-cast.
  RESOLVE- stack resolves top-down: cascades, then Tempest (DamageAll = 3
           to each P1 creature), then Hammer (3 to P1), then Bolt (3 to P1).
           Export post.json once all three are in P0's graveyard.

Behavioral contract:
  A1 setup_ok      pre.json: P0 PreCombatMain, Bolt+Hammer+Tempest in hand,
                   >=11 untapped Mountains; P1 has Bear+Spider on BF; 20/20
  A2 precast_ok    Bolt + Hammer resolved to P1 (life 20->14), both in P0 gy,
                   no other P0 casts
  A3 journal_ok    P0's spells_cast_this_turn == Bolt+Hammer+Tempest on the
                   proof turn (MVs 1, 2, 8)
  A4 bear_dies     the pre-proof Bear is in P1's graveyard post-resolution
  A5 spider_3      the pre-proof Spider is on BF with exactly 3 damage_marked
                   (3 = 1+2; Tempest's own MV8 excluded as "other spells")
  A6 cleanup       Tempest in P0 gy, stack empty, game proceeds; P1 life 14

Verdict = not-reproduced iff A1..A6 all pass; reproduced iff Tempest resolves
(in gy, stack empty) and A4/A5 fail; blocked otherwise.

Protocol-94 driver (v0.98.0, per scenario_6861_0980): HELLO advertises
protocol 94 (server enforces exact match); MulliganDecision as
{"choice":{"type":"Keep"}} gated on the seat's pending Declare;
BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]} (int
oids); DeclareAttackers/Blockers via relations-schema interaction (no legacy
actions on protocol 94); CastSpell/ActivateAbility via advertised actions;
PayMana* via pay_tick; TargetSelection answered via viewer_interaction
schema/exactChoices; PassPriority only when the seat genuinely holds Priority;
per-client stale watchdog.
"""
import asyncio
import copy
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
log = logging.getLogger("scenario6865")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-6865"
EVDIR = f"{BACKFILL}/evidence/6865/{EVID_RUN_ID}"
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

BOLT = "Lightning Bolt"            # MV1, 3 dmg any target
HAMMER = "Volcanic Hammer"         # MV2, 3 dmg any target
TEMPEST = "Call Forth the Tempest"  # MV8 ({5}{R}{R}{R})
BEAR = "Grizzly Bears"             # 2/2
SPIDER = "Giant Spider"            # 2/4
MOUNTAIN = "Mountain"
FOREST = "Forest"
LANDS = (MOUNTAIN, FOREST)

ST = {"stage": "SETUP", "step": 0, "stop": False,
      "proof_turn": None, "proof_enter": None,
      "declines": 0, "pre_exported": False, "mid_exported": False,
      "bolt_oid": None, "hammer_oid": None, "tempest_oid": None,
      "bolt_sent": False, "hammer_sent": False, "tempest_sent": False,
      "bear_oid": None, "spider_oid": None}
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
PHASES = []
CASTLOG = []  # (turn, name) for P0 casts
_WATCH = {}
_DISCARD_REV = {}


# ------------------------------------------------------------------ helpers

def oname(o):
    return o.get("base_name") or o.get("card_name") or o.get("name") or ""


def owner_of(o, pid):
    return o.get("owner") == pid or o.get("controller") == pid


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


def zone_oids(state, pid, zone, name=None):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == zone and owner_of(o, pid):
            if name is None or oname(o) == name:
                out.append(str(oid))
    return out


def untapped_mountains(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == MOUNTAIN and not o.get("tapped"))


def n_untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in LANDS and not o.get("tapped"))


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


def damage_of(o):
    for k in ("damage", "damage_marked", "marked_damage", "damageMarked"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


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


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "stage": ST["stage"],
                             "step": ST["step"]})


def record_phase(state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if not PHASES or PHASES[-1] != key:
        PHASES.append(key)
        wire("phase", {"turn": key[0], "active": key[1], "phase": key[2],
                       "stage": ST["stage"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"], "step": ST["step"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"], "step": ST["step"]})
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
            return op.get("interactionId") or op.get("id")
    return None


async def answer_declare(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    iid = find_relations_op(c)
    if not iid:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": []}}}
    wire("declare_empty", {"who": tag, "wf": wf.get("type"),
                           "submission": sub})
    say(f"[{tag}] declares empty ({wf.get('type')})")
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


def p0_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm in (BOLT, HAMMER, TEMPEST):
            return 2        # key spells: keep
        if nm in LANDS:
            return 1        # lands next
        return 0            # everything else bottomed/discarded first
    return rank


def p1_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm in (BEAR, SPIDER):
            return 2
        if nm in LANDS:
            return 1
        return 0
    return rank


# ------------------------------------------------------- 6865 interaction handlers

def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
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


async def answer_burn_target(c, state, st):
    """Answer Bolt/Hammer TargetSelection: target P1 (seat 1)."""
    wf = wf_of(state)
    if wf.get("type") != "TargetSelection" or str(wf_player(state)) != "0":
        return False
    for opp in vi_ops(c):
        iid = opp.get("interactionId") or opp.get("id")
        if not iid or iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        want = next((x for x in cands if x["seat"] == 1), None)
        if not want:
            continue
        if ("burn_tgt", iid) not in SHAPES:
            SHAPES.add(("burn_tgt", iid))
            wire("burn_target_prompt",
                 {"rtype": rtype,
                  "spec": (data.get("spec") or {}).get("type"),
                  "candidates": cands, "opportunity": opp})
            say(f"[P0] burn target prompt: "
                f"{[(x['name'], x['seat']) for x in cands]} -> P1")
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] burn: unexpected prompt shape {rtype}/{spec_type}")
            continue
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        say("[P0] burn targets P1 (seat 1)")
        return True
    return False


async def decline_cascade(c, state, st):
    """Decline cascade's may-cast (OptionalEffectChoice exactChoices)."""
    wf = wf_of(state)
    if wf.get("type") != "OptionalEffectChoice" \
            or str(wf_player(state)) != "0":
        return False
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
        cands = candidate_info(opp, state)
        if ("cascade", iid) not in SHAPES:
            SHAPES.add(("cascade", iid))
            wire("cascade_may_choice",
                 {"candidates": cands, "opportunity": opp})
            say(f"[P0] cascade may-cast prompt: "
                f"{[(x['choice_id'], x['value'], x['text']) for x in cands]}")
        want = next((x for x in cands
                     if str(x["value"]).lower() == "false"), None)
        if not want:
            want = next((x for x in cands
                         if "decline" in x["text"].lower()
                         or x["text"].lower().startswith("no")), None)
        if not want:
            say("[P0] cascade: no decline choice identifiable; NOT answering")
            wire("cascade_no_decline_found", {"candidates": cands})
            return False
        await send_interaction(c, {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId":
                                                         want["choice_id"]}}})
        SUBMITTED.add(iid)
        ST["declines"] += 1
        say(f"[P0] cascade may-cast DECLINED (#{ST['declines']})")
        return True
    return False


# ------------------------------------------------------- scenario plan

def proof_ready(state, pid):
    return (untapped_mountains(state, pid) >= 11
            and find_hand(state, pid, BOLT)
            and find_hand(state, pid, HAMMER)
            and find_hand(state, pid, TEMPEST))


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, MOUNTAIN)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


async def p0_cast_named(c, pid, state, acts, name):
    oid = find_hand(state, pid, name)
    a = castspell_advertised(acts, oid)
    if not a:
        return False
    await submit_as_is(c, a)
    CASTLOG.append((state.get("turn_number"), name))
    say(f"[P0] casts {name} (oid {oid})")
    return oid


def spell_in_gy(state, name):
    return any(oname(o) == name and o.get("zone") == "Graveyard"
               and o.get("owner") == 0
               for o in state["objects"].values())


def tempest_zone(state):
    return [(str(oid), o.get("zone")) for oid, o in state["objects"].items()
            if oname(o) == TEMPEST and o.get("owner") == 0]


async def proof_step(c, pid, state, acts):
    """Cast Bolt -> Hammer -> Tempest on the same turn, each resolving
    before the next is cast."""
    if not ST["bolt_sent"]:
        oid = await p0_cast_named(c, pid, state, acts, BOLT)
        if oid:
            ST["bolt_oid"] = str(oid)
            ST["bolt_sent"] = True
            return True
        return False
    if not spell_in_gy(state, BOLT):
        return False  # let the Bolt resolve
    if not ST["hammer_sent"]:
        oid = await p0_cast_named(c, pid, state, acts, HAMMER)
        if oid:
            ST["hammer_oid"] = str(oid)
            ST["hammer_sent"] = True
            return True
        return False
    if not spell_in_gy(state, HAMMER):
        return False  # let the Hammer resolve
    if not ST["tempest_sent"]:
        oid = await p0_cast_named(c, pid, state, acts, TEMPEST)
        if oid:
            ST["tempest_oid"] = str(oid)
            ST["tempest_sent"] = True
            return True
        return False
    return False


async def p1_step(c, pid, state, acts):
    for name in LANDS:
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    if my_main(state, pid):
        n_bear = sum(1 for _, o in bf(state, pid) if oname(o) == BEAR)
        n_spider = sum(1 for _, o in bf(state, pid) if oname(o) == SPIDER)
        for want, have, need in ((BEAR, n_bear, 2), (SPIDER, n_spider, 1)):
            if have >= need:
                continue
            oid = find_hand(state, pid, want)
            a = castspell_advertised(acts, oid)
            if a and n_untapped_lands(state, pid) >= (2 if want == BEAR
                                                     else 4):
                await submit_as_is(c, a)
                say(f"[P1] casts {want}")
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
    if await answer_declare(c, pid, tag):
        return True
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, copy.deepcopy(a))
            say(f"{c.name} answers ChooseLegend (keep first)")
            wire("legend_answered", {"player": pid})
            return True
    # P0 answers burn targets + cascade declines before anything else
    if is_p0:
        if await answer_burn_target(c, state, st):
            return True
        if await decline_cascade(c, state, st):
            return True
    # never pass while this seat has a decision pending
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    if wt0 in ("OptionalCostChoice", "TargetSelection", "ManaPayment",
               "ChooseXValue", "DiscardChoice", "ChooseLegend") \
            and str(wplayer) == str(pid):
        return False
    # P0's plan
    if is_p0 and my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if ST["stage"] == "PROOF":
            if await proof_step(c, pid, state, acts):
                return True
        # else: fall through to PassPriority below
    if not is_p0:
        if await p1_step(c, pid, state, acts):
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
    t0 = time.time()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((MOUNTAIN, 36), (BOLT, 10), (HAMMER, 10),
                         (TEMPEST, 4)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((FOREST, 40), (BEAR, 12),
                                    (SPIDER, 12)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
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

        if (wf_of(state).get("type") == "GameOver") and not ST["stop"]:
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")
            ST["stop"] = True
            ST["retry"] = True

        # --- stage transitions (driven by observed state, not submissions)
        if ST["stage"] == "SETUP":
            if (my_main(state, 0) and proof_ready(state, 0)
                    and any(oname(o) == BEAR for _, o in bf(state, 1))
                    and any(oname(o) == SPIDER for _, o in bf(state, 1))):
                for oid, o in bf(state, 1):
                    if oname(o) == BEAR and not ST["bear_oid"]:
                        ST["bear_oid"] = str(oid)
                    if oname(o) == SPIDER and not ST["spider_oid"]:
                        ST["spider_oid"] = str(oid)
                await export_now("pre.json")
                ST["pre_exported"] = True
                ST["proof_turn"] = state.get("turn_number")
                ST["stage"] = "PROOF"
                say(f"=== stage -> PROOF (turn {ST['proof_turn']}) "
                    f"bear={ST['bear_oid']} spider={ST['spider_oid']} ===")
        elif ST["stage"] == "PROOF":
            zones = tempest_zone(state)
            if (ST["tempest_sent"]
                    and any(z == "Stack" for _, z in zones)
                    and not ST["mid_exported"]):
                ST["tempest_oid"] = next(oid for oid, z in zones
                                         if z == "Stack")
                await export_now("mid_tempest_on_stack.json")
                ST["mid_exported"] = True
                ST["proof_enter"] = now
                say(f"=== Tempest on stack (oid {ST['tempest_oid']}); "
                    f"mid exported ===")
            gy0 = [oname(o) for oid, o in state["objects"].items()
                   if o.get("zone") == "Graveyard" and o.get("owner") == 0]
            if (ST["tempest_sent"] and TEMPEST in gy0 and BOLT in gy0
                    and HAMMER in gy0 and not state.get("stack")):
                await asyncio.sleep(1.5)
                await export_now("post.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("=== all three resolved; post.json exported; DONE ===")
            elif (ST["tempest_sent"] and ST["proof_enter"]
                    and now - ST["proof_enter"] > 480):
                await export_now("mid_stall.json")
                obs["notes"].append(
                    "PROOF watchdog: 480s after Tempest reached the stack "
                    f"with no resolution; zones={tempest_zone(state)} "
                    f"declines={ST['declines']}")
                say("=== PROOF watchdog fired; stopping ===")
                ST["stage"] = "STALLED"
                ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"P0 casts: {CASTLOG}")
    obs["notes"].append(f"cascade declines: {ST['declines']}")
    obs["notes"].append(f"stage at end: {ST['stage']}")
    obs["phases"] = PHASES
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "phases": PHASES,
                   "casts": CASTLOG, "declines": ST["declines"],
                   "stage": ST["stage"],
                   "oids": {"bear": ST["bear_oid"],
                            "spider": ST["spider_oid"],
                            "tempest": ST["tempest_oid"],
                            "bolt": ST["bolt_oid"],
                            "hammer": ST["hammer_oid"]}}, f, indent=2)
    for n in obs["notes"]:
        say(f"NOTE: {n}")

    await p0.close()
    await p1.close()
    return obs, True


def reset_st():
    ST.clear()
    ST.update({"stage": "SETUP", "step": 0, "stop": False,
               "proof_turn": None, "proof_enter": None,
               "declines": 0, "pre_exported": False,
               "mid_exported": False,
               "bolt_oid": None, "hammer_oid": None,
               "tempest_oid": None,
               "bolt_sent": False, "hammer_sent": False,
               "tempest_sent": False,
               "bear_oid": None, "spider_oid": None})
    MULLS.clear()
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    PHASES.clear()
    CASTLOG.clear()
    _DISCARD_REV.clear()
    _WATCH.clear()


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 3):
        reset_st()
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
    evaluate(obs)


# ------------------------------------------------------- evaluate

def load_state(path):
    return json.load(open(f"{EVDIR}/{path}"))["state"]


def evaluate(obs):
    A = obs["assert"]

    def journal_spells(state):
        j = state.get("spells_cast_this_turn_by_player") or {}
        out = []
        for e in j.get("0", []) or []:
            if isinstance(e, dict):
                out.append((e.get("name"), e.get("mana_value")))
        return out

    try:
        pre = load_state("pre.json")
        pre_ok = (
            pre.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")
            and pre.get("active_player") == 0
            and untapped_mountains(pre, 0) >= 11
            and find_hand(pre, 0, BOLT) is not None
            and find_hand(pre, 0, HAMMER) is not None
            and find_hand(pre, 0, TEMPEST) is not None
            and any(oname(o) == BEAR for _, o in bf(pre, 1))
            and any(oname(o) == SPIDER for _, o in bf(pre, 1))
            and life_of(pre, 0) == 20 and life_of(pre, 1) == 20
        )
        A["A1_setup_ok"] = "passed" if pre_ok else "failed"
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"A1 eval error: {e}")

    try:
        post = load_state("post.json")
        js = journal_spells(post)
        bolt = next((j for j in js if j[0] == BOLT), None)
        hammer = next((j for j in js if j[0] == HAMMER), None)
        tempest = next((j for j in js if j[0] == TEMPEST), None)
        A["A2_precast_ok"] = ("passed"
                              if bolt and hammer and bolt[1] == 1
                              and hammer[1] == 2
                              and spell_in_gy(post, BOLT)
                              and spell_in_gy(post, HAMMER)
                              and life_of(post, 1) == 14
                              else "failed")
        A["A3_journal_ok"] = ("passed"
                              if (len(js) == 3 and bolt and hammer
                                  and tempest and bolt[1] == 1
                                  and hammer[1] == 2 and tempest[1] == 8)
                              else "failed")
        A["A4_bear_dies"] = ("passed"
                             if (ST.get("bear_oid")
                                 and post["objects"].get(ST["bear_oid"], {})
                                 .get("zone") == "Graveyard")
                             else "failed")
        sp = post["objects"].get(ST.get("spider_oid") or "", {})
        A["A5_spider_3"] = ("passed"
                            if (sp and sp.get("zone") == "Battlefield"
                                and damage_of(sp) == 3)
                            else "failed")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        A["A6_cleanup"] = ("passed"
                           if (spell_in_gy(post, TEMPEST) and stack_empty
                               and life_of(post, 1) == 14)
                           else "failed")
    except Exception as e:
        for k in ("A2_precast_ok", "A3_journal_ok", "A4_bear_dies",
                  "A5_spider_3", "A6_cleanup"):
            A[k] = "not-run"
        obs["notes"].append(f"post eval error: {e}")

    if ST["stage"] != "DONE":
        for k in list(A):
            if A[k] not in ("passed", "failed"):
                A[k] = "not-run"
        verdict = "blocked"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_precast_ok", "A3_journal_ok",
              "A4_bear_dies", "A5_spider_3", "A6_cleanup")):
        verdict = "not-reproduced"
    elif (spell_in_gy_safe("post.json", TEMPEST)
          and (A.get("A4_bear_dies") == "failed"
               or A.get("A5_spider_3") == "failed")):
        verdict = "reproduced"
    else:
        verdict = "blocked"

    try:
        post = load_state("post.json")
        js = journal_spells(post)
    except Exception:
        js = []
    obs["notes"].append(f"proof turn: {ST['proof_turn']}; "
                        f"declines: {ST['declines']}")
    obs["notes"].append(f"journal spells: {js}")
    obs["notes"].append("protocol-94 driver (v0.98.0): mulligan "
                        "{choice:{type:Keep}} gated on pending Declare; "
                        "bottom/discard-to-hand-size via single "
                        "SelectCards(data.cards); "
                        "DeclareAttackers/Blockers via relations-schema "
                        "interaction; CastSpell via advertised actions; "
                        "PayMana* via pay_tick; TargetSelection via "
                        "viewer_interaction schema/exactChoices; "
                        "priority-gated passes; stale-client watchdog.")

    run = {
        "issue": 6865,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(time.time())),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(
            f"{BACKFILL}/driver/scenario_6865_0980.py"),
        "decks": {
            "P0": [[MOUNTAIN, 36], [BOLT, 10], [HAMMER, 10],
                   [TEMPEST, 4]],
            "P1": [[FOREST, 40], [BEAR, 12], [SPIDER, 12]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "wf_sequence": WF_SEEN,
        "casts": CASTLOG,
        "oids": {"bear": ST["bear_oid"], "spider": ST["spider_oid"],
                 "bolt": ST["bolt_oid"], "hammer": ST["hammer_oid"],
                 "tempest": ST["tempest_oid"]},
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_6865_0980.py).",
        ],
        "setup_line": "P0: 36x Mountain, 10x Lightning Bolt, 10x Volcanic Hammer, 4x Call Forth the Tempest; "
                      "P1: 40x Forest, 12x Grizzly Bears, 12x Giant Spider",
        "contract_line": ("Cast Bolt (MV1), Hammer (MV2), then Tempest on the "
                          "same turn; expect 3 damage to each opponent "
                          "creature (Bear destroyed, Spider survives with "
                          "exactly 3 marked), excluding Tempest's own MV8."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN, "casts": CASTLOG,
                   "oids": run["oids"]}, f, indent=2)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_6865_0980.py",
                f"{EVDIR}/scenario_6865_0980.py")
    render_summary(run, f"{EVDIR}/summary.png")
    WIRE.close()
    RUNLOG.close()
    write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(A)}", flush=True)


def spell_in_gy_safe(path, name):
    try:
        return spell_in_gy(load_state(path), name)
    except Exception:
        return False


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6865 - Call Forth the Tempest (revalidation)",
           fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} "
                    f"({si['build_commit']}) protocol "
                    f"{si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), "Assertions (from saved states):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: P0 main priority, 11+ untapped Mountains, "
                       "Bolt+Hammer+Tempest in hand; P1 Bear+Spider on BF",
        "A2_precast_ok": "A2 Bolt+Hammer resolved to P1 (20->14), both in "
                         "P0 gy",
        "A3_journal_ok": "A3 turn journal: Bolt(MV1)+Hammer(MV2)+Tempest(MV8) "
                         "on the proof turn",
        "A4_bear_dies": "A4 pre-proof Bear in P1's graveyard",
        "A5_spider_3": "A5 pre-proof Spider on BF with exactly 3 "
                       "damage_marked",
        "A6_cleanup": "A6 Tempest in P0 gy, stack empty, P1 at 14",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:7]:
        d.text((40, y), n[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6865/"
                         + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


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


if __name__ == "__main__":
    asyncio.run(main())
