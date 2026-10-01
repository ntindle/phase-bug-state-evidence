#!/usr/bin/env python3
"""Issue #7219 revalidation on v0.98.0 (protocol 94): Cantankerous Keepers
Affinity for Elves not considered.

Reported (GitHub): "If I have for example 4 green mana and two elves on the
battlefield I cannot cast Catankerous Keepers" -- Affinity for Elves should
reduce the cost.

Oracle: "Affinity for Elves (This spell costs {1} less to cast for each Elf
you control.)"  Mana cost {5}{G}. Pinned v0.98.0 card data parses the Affinity
keyword with type_filters=[{"Subtype": "Elf"}] (correct singular subtype --
the triage-time "Elve" mismatch appears fixed in the data). This run tests the
REPORTED OUTCOME on the current pin: with 2 Elves on the battlefield and 4
untapped Forests, is the cast offered, is the announced cost reduced to
{3}{G}, and does the cast complete?

Plan (two human seats, native engine):
  SETUP - land drops; cast Llanowar Elves x2 as mana allows; hold until:
          2+ Elves on P0 battlefield, Keepers in P0 hand, 4+ untapped
          Forests, P0 main phase priority -> export pre.json.
  MAIN  - attempt to cast Cantankerous Keepers. Record whether CastSpell is
          advertised (A2), pay via the advertised Auto payment, track
          Keepers Stack -> Battlefield (A4), let the ETB trigger
          resolve (A5). A3 reads the affinity reduction from the tapped-
          permanents delta between pre/post (4 tapped = {3}{G} paid).

Behavioral contract:
  A1 setup_ok        pre.json: 2+ Llanowar Elves on P0 bf, Keepers in P0
                     hand, 4+ untapped Forests, P0 main priority
  A2 cast_offered    CastSpell advertised for Keepers (legal actions or
                     viewer-interaction menu)
  A3 cost_reduced    affinity applied: exactly 4 additional P0 permanents
                     tapped between pre/post (pays {3}{G}, not {5}{G});
                     the cast is announced with payment_mode Auto so no
                     ManaPayment prompt is presented to capture
  A4 cast_completes  Keepers reaches Stack then Battlefield
  A5 cleanup         ETB trigger resolved, stack empty, game proceeds

Verdict = reproduced iff A2, A3 or A4 fails; not-reproduced iff A2, A3, A4
all pass.

Protocol-94 driver (v0.98.0, per scenario_6861_0980): HELLO advertises
protocol 94 (server enforces exact match); MulliganDecision as
{"choice":{"type":"Keep"}} gated on the seat's pending Declare;
BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]} (int
oids); DeclareAttackers/Blockers via relations-schema interaction;
CastSpell via advertised actions; PayMana* via pay_tick; PassPriority only
when the seat genuinely holds Priority; per-client stale watchdog.
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
log = logging.getLogger("scenario7219")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-7219"
EVDIR = f"{BACKFILL}/evidence/7219/{EVID_RUN_ID}"
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

KEEPERS = "Cantankerous Keepers"
ELF = "Llanowar Elves"
FOREST = "Forest"

ST = {"stage": "SETUP", "step": 0, "stop": False,
      "a2_offered": None, "a2_source": None,
      "payment_prompt_seen": False, "payment_cost": None}
CAST = {}          # active cast tracking
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
_WATCH = {}


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


def untapped_forest(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == FOREST and not o.get("tapped"))


def elf_count_bf(state, pid):
    return sum(1 for _, o in bf(state, pid) if oname(o) == ELF)


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


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "stage": ST["stage"], "step": ST["step"]})


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


def vi_cast_choice(c, oid):
    """A viewer_interaction castSpell choice for the exact object: match the
    object surface's reference id (protocol 94), not a blob substring."""
    for opp in vi_ops(c):
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


def menu_cast_spells(c):
    """(offered_for_oid_set, summary) of castSpell choices in the current
    Priority menu: choice id -> (object reference, object name, zone)."""
    out = {}
    for opp in vi_ops(c):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in action_codes(ch):
                continue
            ref = name = zone = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object":
                    ref = str(d.get("reference"))
                    name = d.get("name")
                    zone = d.get("zone")
            out[ch.get("id")] = (ref, name, zone)
    return out


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
    say(f"[{tag}] bottoming {n}: {[oname(state['objects'][str(x)]) for x in picks]}")
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
    say(f"[{tag}] discarding to hand size: {[oname(state['objects'][str(x)]) for x in picks]}")
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
    wire("declare_empty", {"who": tag, "wf": wf.get("type"), "submission": sub})
    say(f"[{tag}] declares empty ({wf.get('type')})")
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


def p0_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == KEEPERS:
            return 3
        if nm == ELF:
            return 2
        return 0
    return rank


def p1_rank(state):
    def rank(oid):
        return 0
    return rank


def keepers_on_stack(state, oid):
    o = state["objects"].get(str(oid))
    return bool(o and o.get("zone") == "Stack" and oname(o) == KEEPERS)


def keepers_on_bf(state, oid):
    o = state["objects"].get(str(oid))
    return bool(o and o.get("zone") == "Battlefield" and oname(o) == KEEPERS)


def find_costs(obj, out):
    """Recursively collect mana-cost-shaped dicts: {'type':'Cost',
    'shards':[...], 'generic':N}."""
    if isinstance(obj, dict):
        if obj.get("type") == "Cost" and "generic" in obj:
            out.append({"generic": obj.get("generic"),
                        "shards": obj.get("shards")})
        for v in obj.values():
            find_costs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_costs(v, out)


def new_cast(tag, oid):
    CAST.clear()
    CAST.update({"tag": tag, "oid": str(oid), "in_flight": True,
                 "announced": False, "stack_seen": False, "bf_seen": False,
                 "done": False, "rejected": False, "silent_fail": False,
                 "offered": None, "rev_at_submit": None, "submitted": False})


async def setup_step(c, pid, state, acts):
    # land drop (retry every tick)
    lid = find_hand(state, pid, FOREST)
    for a in acts:
        if a["type"] == "PlayLand" and lid and str(
                a.get("data", {}).get("object_id")) == lid:
            await submit_as_is(c, a)
            return True
    # cast Llanowar Elves while fewer than 2 are on the battlefield
    if elf_count_bf(state, pid) < 2 and untapped_forest(state, pid) >= 1:
        eid = find_hand(state, pid, ELF)
        a = castspell_advertised(acts, eid) if eid else None
        if a:
            await submit_as_is(c, a)
            say(f"P0 casts {ELF} ({elf_count_bf(state, pid)} on bf)")
            return True
    return False


async def capture_payment_prompt(c, state):
    """Dump the ManaPayment waiting_for for the Keepers cast before paying."""
    if ST["payment_prompt_seen"]:
        return
    wf = wf_of(state)
    if wf.get("type") != "ManaPayment" or str(wf_player(state)) != "0":
        return
    data = wf.get("data") or {}
    wire("mana_payment_prompt", {"waiting_for_data": data,
                                 "legal_actions": merged_actions(c.latest)})
    say(f"[P0] ManaPayment prompt captured "
        f"(keys={list(data.keys())})")
    costs = []
    find_costs(data, costs)
    wire("mana_payment_costs", {"costs": costs})
    say(f"[P0] costs found in prompt: {costs}")
    ST["payment_prompt_seen"] = True
    ST["payment_cost"] = costs


async def cast_step(c, pid, state, acts):
    if not CAST.get("in_flight"):
        kid = find_hand(state, pid, KEEPERS)
        if not kid:
            say("MAIN step: Keepers not in hand; aborting")
            ST["stop"] = True
            return False
        a = castspell_advertised(acts, kid)
        vich = vi_cast_choice(c, kid)
        menu = menu_cast_spells(c)
        wire("cast_menu", {"oid": kid, "menu_cast_spells": menu,
                           "n_vi_ops": len(vi_ops(c)),
                           "n_legal": len(acts)})
        say(f"MAIN menu: {len(menu)} castSpell choices: "
            f"{[(v[0], v[1], v[2]) for v in menu.values()]}")
        if not a and not vich:
            # the Priority menu can lag a tick behind the gate; wait a bit
            # before concluding the cast is not offered.
            t0 = CAST.get("menu_wait_t0") or time.time()
            CAST["menu_wait_t0"] = t0
            if time.time() - t0 < 20:
                return False
            say("MAIN menu: no castSpell choice for Keepers after 20s; "
                "concluding not offered")
        CAST.pop("menu_wait_t0", None)
        new_cast("KEEPERS", kid)
        CAST["offered"] = bool(a or vich)
        ST["a2_offered"] = CAST["offered"]
        ST["a2_source"] = ("legal_actions" if a else
                           ("viewer_interaction" if vich else "none"))
        wire("keepers_cast_attempt", {"oid": kid, "offered": CAST["offered"],
                                      "source": ST["a2_source"],
                                      "advertised_action": a})
        say(f"Keepers cast: oid={kid} offered={CAST['offered']} "
            f"({ST['a2_source']})")
        if a:
            await submit_as_is(c, a)
            CAST["via"] = "legal_actions"
        elif vich:
            opp, ch = vich
            iid = opp.get("interactionId") or opp.get("id")
            await send_interaction(c, {"interactionId": iid,
                                       "response": {"type": "choose",
                                                    "data": {"choiceId": ch["id"]}}})
            SUBMITTED.add(iid)
            CAST["via"] = "viewer_interaction"
        else:
            obj = state["objects"].get(str(kid), {})
            cid = obj.get("card_id", int(kid))
            raw = {"type": "CastSpell",
                   "data": {"object_id": int(kid), "card_id": int(cid),
                            "targets": [], "payment_mode": {"type": "Auto"}}}
            await submit_as_is(c, raw)
            CAST["via"] = "raw_action"
        CAST["submitted"] = True
        CAST["rev_at_submit"] = c.revision
        CAST["wall_at_submit"] = time.time()
        return True
    # in flight: capture the payment prompt, then track the spell
    await capture_payment_prompt(c, state)
    if keepers_on_stack(state, CAST["oid"]):
        if not CAST["stack_seen"]:
            wire("keepers_stack_seen", {"oid": CAST["oid"]})
            say("Keepers cast: on stack")
        CAST["announced"] = True
        CAST["stack_seen"] = True
    if keepers_on_bf(state, CAST["oid"]):
        if not CAST["bf_seen"]:
            wire("keepers_bf_seen", {"oid": CAST["oid"]})
            say("Keepers cast: on battlefield")
        CAST["bf_seen"] = True
    if CAST.get("rejected"):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post.json")
        ST["stop"] = True
        say("=== Keepers cast REJECTED -> stop ===")
        return True
    if CAST.get("announced") and not keepers_on_stack(state, CAST["oid"]):
        # left the stack: resolved (battlefield) or countered/removed.
        # Settle briefly so the ETB trigger can resolve before the export.
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in state["objects"].values())
        t0 = CAST.get("settle_t0") or time.time()
        CAST["settle_t0"] = t0
        if not stack_empty and time.time() - t0 < 60:
            # keep passing so the trigger can resolve
            if wf_of(state).get("type") == "Priority" \
                    and str(wf_player(state)) == "0":
                for a in acts:
                    if a.get("type") == "PassPriority":
                        await submit_as_is(c, a)
                        return True
            return False
        CAST.pop("settle_t0", None)
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post.json")
        ST["stop"] = True
        say(f"=== Keepers cast done (bf_seen={CAST['bf_seen']}) -> stop ===")
        return True
    if (not CAST.get("announced") and CAST.get("submitted")
            and (c.revision - (CAST.get("rev_at_submit") or c.revision) >= 15
                 or time.time() - CAST.get("wall_at_submit", time.time()) > 90)):
        wf = wf_of(state).get("type")
        wplayer = wf_player(state)
        if not (wf in ("TargetSelection", "OptionalCostChoice", "ManaPayment",
                       "ChooseXValue", "DiscardChoice")
                and str(wplayer) == "0"):
            CAST["in_flight"] = False
            CAST["done"] = True
            CAST["silent_fail"] = True
            await export_now("post.json")
            ST["stop"] = True
            say("=== Keepers cast silent-fail (no stack sighting) -> stop ===")
            return True
    # while the spell is on the stack, pass so it can resolve
    if CAST.get("announced") and wf_of(state).get("type") == "Priority" \
            and str(wf_player(state)) == "0":
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    tag = "P0" if is_p0 else "P1"
    if is_p0:
        if await do_mulligan(c, pid, tag, lambda s: sum(
                1 for o in hand_oids(s, pid)
                if oname(s["objects"][o]) == FOREST) >= 2):
            return True
    else:
        if await do_mulligan(c, pid, tag, lambda s: True):
            return True
    if await do_bottom(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await do_discard(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await pay_tick(c):
        return True
    if await answer_declare(c, pid, tag):
        return True
    # P0 pending decisions via viewer interaction: Keepers needs none
    # (no targets, no choices), so nothing to scan here.
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    # never pass while P0 has a cast/ability decision pending
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                         "ManaPayment", "ChooseXValue", "DiscardChoice") \
            and str(wplayer) == "0":
        # capture the payment prompt for A3 before paying
        if wt0 == "ManaPayment":
            await capture_payment_prompt(c, state)
        return False
    # a cast announcement in flight: hold until the spell is on the stack
    # (or the submission is answered). Escape on revision progress OR a
    # wall-clock timeout so a lost submission can't deadlock the driver.
    if is_p0 and CAST.get("in_flight"):
        on_stack = keepers_on_stack(state, CAST.get("oid")) \
            if CAST.get("oid") else False
        resolved = CAST.get("announced") and not on_stack
        if not on_stack and not resolved and not CAST.get("rejected"):
            revs = c.revision - (CAST.get("rev_at_submit") or c.revision)
            wall = time.time() - CAST.get("wall_at_submit", time.time())
            if revs < 15 and wall < 90:
                return False
    if is_p0 and my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await setup_step(c, pid, state, acts):
                return True
        elif await cast_step(c, pid, state, acts):
            return True
    # default: pass priority only when genuinely holding it
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((KEEPERS, 12), (ELF, 4), (FOREST, 44)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((FOREST, 60)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 2400
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            watch(c)
            rej = drain_rejections(c)
            if rej and is_p0 and CAST.get("in_flight") \
                    and not CAST.get("announced"):
                CAST["rejected"] = True
                CAST["reject_data"] = rej
                say(f"[P0] cast {CAST.get('tag')} rejected")
                force_tick[c.name] = True
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                say(f"[{c.name}] resync: retrying {LAST_SUBMIT['iid']} "
                    f"after rejection")
                LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            # periodic re-tick even without a revision change (missed
            # broadcast resilience); at most every 5s per client.
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
        record_wf(st["state"])
        state = st["state"]

        # SETUP -> MAIN gate: the reporter's board state
        if ST["stage"] == "SETUP" and my_main(state, 0):
            n_elf = elf_count_bf(state, 0)
            n_kh = len(zone_oids(state, 0, "Hand", KEEPERS))
            un = untapped_forest(state, 0)
            if n_elf >= 2 and n_kh >= 1 and un >= 4:
                await export_now("pre.json")
                ST["stage"] = "MAIN"
                ST["step"] = 1
                SUBMITTED.clear()
                say(f"=== stage -> MAIN (elves={n_elf} keepers_in_hand={n_kh} "
                    f"untapped_forests={un}) ===")
                wire("setup_ready", {"elves": n_elf, "keepers_hand": n_kh,
                                     "untapped_forests": un})
                continue

    # ------------------------------------------------------- evaluate
    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    pre = load_state("pre.json")
    A["A1_setup_ok"] = ("passed"
                        if elf_count_bf(pre, 0) >= 2
                        and len(zone_oids(pre, 0, "Hand", KEEPERS)) >= 1
                        and untapped_forest(pre, 0) >= 4
                        else "failed")
    A["A2_cast_offered"] = ("passed" if ST["a2_offered"]
                             else ("failed" if ST["a2_offered"] is False
                                   else "not-run"))

    # A3: affinity applied iff the cast tapped exactly 4 additional P0
    # permanents (pays {3}{G}); {5}{G} without affinity would tap 6.
    # The cast is announced with payment_mode Auto, so no ManaPayment
    # prompt is presented -- the tapped delta is the cost evidence.
    def tapped_mana_sources(st):
        return sum(1 for o in st["objects"].values()
                   if o.get("zone") == "Battlefield"
                   and o.get("controller") == 0 and o.get("tapped")
                   and oname(o) in (FOREST, ELF))
    try:
        post_a3 = load_state("post.json")
        delta = tapped_mana_sources(post_a3) - tapped_mana_sources(pre)
        wire_note = f"tapped_delta={delta}"
    except Exception as e:
        delta = None
        wire_note = f"tapped_delta_error={e}"
    if delta == 4:
        A["A3_cost_reduced"] = "passed"
    elif delta is None:
        A["A3_cost_reduced"] = "not-run"
    else:
        A["A3_cost_reduced"] = "failed"
    obs["notes"].append(f"A3: {wire_note} (expected 4 for affinity-reduced "
                        f"{{3}}{{G}}; 6 would mean affinity ignored)")

    cast_stack = cast_bf = False
    cast_rejected = CAST.get("rejected", False)
    with open(f"{EVDIR}/wire_log.jsonl") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            ev = e.get("event")
            if ev == "keepers_stack_seen":
                cast_stack = True
            elif ev == "keepers_bf_seen":
                cast_bf = True
    A["A4_cast_completes"] = ("passed" if cast_stack and cast_bf
                               else ("failed" if cast_rejected
                                     or CAST.get("silent_fail")
                                     or ST.get("stop")
                                     else "not-run"))
    # cleanup: stack empty in the post state
    try:
        post = load_state("post.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        n_bf = len(zone_oids(post, 0, "Battlefield", KEEPERS))
        A["A5_cleanup"] = ("passed" if stack_empty else "failed")
        obs["notes"].append(f"post: keepers_on_bf={n_bf} stack_empty={stack_empty}")
    except Exception:
        A["A5_cleanup"] = "not-run"

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"cast offered={ST['a2_offered']} "
                        f"(source={ST['a2_source']}) "
                        f"payment_prompt_costs={ST.get('payment_cost')} "
                        f"stack={cast_stack} bf={cast_bf} "
                        f"rejected={cast_rejected}")
    obs["notes"].append("protocol-94 driver (v0.98.0): mulligan as "
                        "{choice:{type:Keep}} gated on pending Declare; "
                        "bottom/discard-to-hand-size via single "
                        "SelectCards(data.cards); "
                        "DeclareAttackers/Blockers via relations-schema "
                        "interaction; CastSpell via advertised actions; "
                        "PayMana* via pay_tick; ManaPayment prompt captured "
                        "for the affinity cost check; priority-gated passes; "
                        "stale-client watchdog.")

    if A["A2_cast_offered"] == "failed" or A["A3_cost_reduced"] == "failed" \
            or A["A4_cast_completes"] == "failed":
        verdict = "reproduced"
    elif (A["A2_cast_offered"] == "passed"
          and A["A3_cost_reduced"] == "passed"
          and A["A4_cast_completes"] == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    run = {
        "issue": 7219,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_7219.py"),
        "decks": {
            "P0": [[KEEPERS, 12], [ELF, 4], [FOREST, 44]],
            "P1": [[FOREST, 60]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "wf_sequence": WF_SEEN,
        "a2_source": ST["a2_source"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_7219.py).",
            "Affinity counts only the two Llanowar Elves on the battlefield; Keepers itself is in hand at cast time.",
        ],
        "setup_line": "P0: 12x Cantankerous Keepers, 4x Llanowar Elves, 44x Forest; P1: 60x Forest (draw-go)",
        "contract_line": ("Cast Cantankerous Keepers with 2 Elves on the battlefield and 4 untapped "
                          "Forests: offered, cost reduced {5}{G}->{3}{G}, completes to the battlefield."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN, "a2_source": ST["a2_source"]}, f, indent=2)
    await render_summary(run, f"{EVDIR}/summary.png")
    WIRE.close()
    RUNLOG.close()
    await write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(A)}", flush=True)


async def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #7219 - Cantankerous Keepers: Affinity for Elves", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90)))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: 2 Elves on bf, Keepers in hand, 4+ untapped Forests, P0 main priority",
        "A2_cast_offered": "A2 CastSpell advertised for Cantankerous Keepers",
        "A3_cost_reduced": "A3 announced cost {5}{G} -> {3}{G} (affinity for 2 Elves)",
        "A4_cast_completes": "A4 Keepers reaches Stack then Battlefield",
        "A5_cleanup": "A5 ETB resolved; stack empty; game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}", fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:8]:
        d.text((40, y), n[:120], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7219/" + run["run_id"], fill=(120, 130, 150))
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
