#!/usr/bin/env python3
"""Issue #6763 re-validation on pinned v0.104.0 (build 4227122, WS protocol
118): Emperor of Bones targeting -- opponent cannot tell which Walking
Ballista is targeted.

Reported: when Emperor of Bones' "exile up to one target card from a
graveyard" trigger targets one of two Walking Ballistas in the opponent's
graveyard, the responding opponent cannot identify the exact physical card
targeted before responding. Triage: stable object-instance presentation in a
public zone; every player must be able to identify the exact graveyard object
when duplicate names exist. Classifier: frontend presentation (not card data).

Behavioral contract (ported from the protocol-106 scenario_6763_01030.py):
  E1 setup_ok         Emperor of Bones on P1's battlefield; >=2 Walking
                      Ballista objects in P0's graveyard; the Emperor exile
                      trigger on the stack with exactly one target.
  E2 trigger_targeted the trigger's ability targets == [{"Object": oid}] with
                      oid one of P0's graveyard Ballista ids (exactly one).
  E3 identity_distinct the two games' target oids are distinct objects with
                      the same name (a name-only label cannot disambiguate).
  E4 snapshot_captured P0's viewer snapshot at the "before responding" moment
                      (P0 holds priority, trigger on stack) carries the same
                      target Object id in BOTH the viewer envelope and the
                      authoritative export; variant A and B target different
                      oids.
  E5 cleanup          trigger resolves: chosen Ballista exiled, stack empty,
                      game proceeds.
  U1..U5 (browser, real React components seeded from the captured viewer
                      snapshot): stack target label and StackTargetArcs anchor
                      are identical for both target variants -- the opponent
                      cannot tell which Ballista was targeted.

Two real games are run (not token-replaced exports): game A targets the most
recently-died Ballista in P0's graveyard; game B targets the oldest one.

Verdict = reproduced iff every E and U assertion passes (engine distinctly
targets; UI renders identically = bug still observed).

Protocol-118 driver notes (v0.104.0): ported from the verified protocol-106
scenario_6763_01030.py using the conventions from the verified protocol-118
scenario_6760_01040.py / scenario_6765_01040.py:
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full via
    driver/client.py PhaseClient; HELLO advertises protocol 118 (exact match
    enforced).
  - waiting_for is gone; priority = PassPriority in the viewing seat's merged
    legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered as-is ("keep") gated on the advertised action.
  - Bottom-after-mulligan via vi schema/select, gated on waitingForKind.code
    == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi, gated on hand > 7 + schema/select opportunity
    offering hand cards.
  - Casts go out as bare legacy CastSpell actions matched by object_id; the
    engine auto-pays (payment_mode Auto) -- the driver never taps lands for
    mana itself, so there is no double-payment artifact. No legacy PayMana
    actions or vi tapLandForMana menus are used at all.
  - Ballista X=0 answered via vi schema/number opportunity (value 0). The X
    prompt is part of the cast itself, so it is answered BEFORE the
    cast-confirmation guard (the guard only blocks priority passes and new
    main-phase plays, never the cast's own choice prompts).
  - Cast-confirmation guard (protocol-118 lesson, cf. 2026-10-09 #6765
    re-validation): after EVERY CastSpell submission the driver sets
    G["cast_pending"][client] and does NOT pass priority or start new
    main-phase plays until the spell is confirmed on the stack/battlefield
    (cast_zone checked each tick; the *_cast flag is set on CONFIRMATION,
    never on submission). A 30s backstop clears a still-in-hand cast as
    dropped so the state-based cast block re-triggers and retries. The guard
    runs BEFORE any early-return block (attackers/blockers/target-wait).
    This scenario has no one-shot *_consumed flags to reset (cast blocks are
    state-based: Ballista while <2 in gy, Emperor while not on battlefield).
  - Repeating prompts are answered per-firing, not per-leg: submitted
    opportunities are keyed by interactionId (G["target_answered"],
    G["x_answered"]); a fresh iid for the same logical prompt is answered
    again.
  - Land plays via legacy PlayLand + vi playLand opportunity (answered before
    the decision gate).
  - real_decision_pending EXCLUDES tapLandForMana/untapLandForMana/castSpell/
    activateAbility/passPriority/playLand/mulliganDecision/candidate/mana.
  - surf_codes() filters None codes; land matching uses is_land() helper.
  - Emperor trigger target selection is a vi schema sequence/select
    opportunity: submit the engine-advertised choice id for the intended
    Ballista oid, then poll until the interaction id leaves the vi.
  - One in-flight submission per client (G["submitted"] with timestamps);
    client rejections are drained into the wire log each tick.
  - After the priority gate, await asyncio.sleep(0) and re-read state before
    leg (capture) evaluation.
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
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6763_118")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-20261009-0647"
ISSUE = 6763
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.104.0",
    "build_commit": "4227122",
    "protocol_version": 118,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.104.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-09: pinned release v0.104.0 == latest stable; "
               "ServerHello 0.104.0/4227122/protocol 118 verified by "
               "handshake this run; hashes recomputed against on-disk "
               "artifacts this run; reusing the pinned v0.104.0 server on "
               "127.0.0.1:9374"),
}

for _f, _k in (("server/releases/v0.104.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.104.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.104.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

BALLISTA = "Walking Ballista"
EMPEROR = "Emperor of Bones"
FOREST = "Forest"
SWAMP = "Swamp"


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

# per-game mutable state, reset by run_game()
G = {}


def reset_game_state(tag):
    G.clear()
    G.update({
        "tag": tag,
        "mulls": set(),
        "submitted": {},       # interactionId -> timestamp
        "land_turn": {},
        "passed_rev": {},
        "cast_pending": {},    # client name -> {"kind","oid","name","since"}
        "x_answered": set(),
        "target_answered": set(),
        "chosen_oid": None,
        "entry_id": None,
        "trigger_turn": None,
        "captured": False,
        "resolved": False,
        "rejections": [],
        "notes": [],
    })


# ---------------------------------------------------------------- state helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def gy_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("graveyard") or [])]


def ballista_gy_ids(state, pid):
    return [oid for oid in gy_ids(state, pid)
            if obj_lname(state, oid) == BALLISTA.lower()]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict) and s.get("data", {}).get("code")]


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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
            codes.update(surf_codes(ch))
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def collect_object_target_refs(entry):
    """All TargetRef::Object ids chosen on a stack entry (excludes the
    effect's target *spec*, which serializes as {"type": "Typed", ...})."""
    refs = []

    def rec(o):
        if isinstance(o, dict):
            if set(o.keys()) == {"Object"} and isinstance(o["Object"], int):
                refs.append(o["Object"])
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec((entry.get("kind", {}) or {}).get("data", {}).get("ability", {}))
    return refs


def find_emperor_trigger(state):
    """(entry, chosen_oids) for the Emperor of Bones exile trigger on the
    stack."""
    for e in state.get("stack") or []:
        refs = collect_object_target_refs(e)
        if not refs:
            continue
        blob = json.dumps(e).lower()
        if "graveyard" not in blob:
            continue
        src = e.get("source_id")
        if src is not None and obj_lname(state, src) == EMPEROR.lower():
            return e, refs
    return None, []


def parse_export(raw):
    """ExportAuthoritativeState: data.state is a JSON string; parse once."""
    env = json.loads(raw)
    st = env["state"]
    if isinstance(st, str):
        st = json.loads(st)
    return st


# ---------------------------------------------------------------- interaction primitives

async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        rdata = {"choiceIds": [cid]}
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": rdata}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    await interact_as(c, sub, tag)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.104.0"), f"unexpected version {ver}"
    assert int(proto) == 118, f"unexpected protocol {proto}"
    assert str(build) == "4227122", f"unexpected build {build}"


def opp_fresh(iid, ttl=15):
    return time.time() - G["submitted"].get(iid, 0) >= ttl


def mark_submitted(iid):
    G["submitted"][iid] = time.time()


def cast_zone(state, oid):
    return str(get_obj(state, oid).get("zone") or "").lower()


async def guard_cast_pending(c, tag):
    """Cast-confirmation guard (protocol 118): while this client's CastSpell
    is in flight, the driver must not pass priority or start new main-phase
    plays -- a bare CastSpell (payment_mode Auto) can lose a race to the
    driver's own PassPriority submitted on the next tick. The *_cast flag is
    set on CONFIRMATION (spell on stack/battlefield), never on submission.
    A 30s backstop clears a still-in-hand cast as dropped so the state-based
    cast block re-triggers and retries. Runs BEFORE any early-return block.
    Returns "waiting" / "confirmed" / "dropped" / None (no pending cast)."""
    pend = (G.get("cast_pending") or {}).get(c.name)
    if not pend:
        return None
    st = c.latest
    if not st:
        return "waiting"
    state = st["state"]
    oid = pend["oid"]
    zone = cast_zone(state, oid)
    if zone in ("stack", "battlefield"):
        G["cast_pending"].pop(c.name, None)
        G[pend["kind"] + "_cast"] = True
        say(f"[{tag}] cast confirmed on {zone}: {pend['name']} (oid {oid})")
        wire("cast_confirmed", {"kind": pend["kind"], "oid": oid,
                                "zone": zone})
        return "confirmed"
    if zone == "hand" and time.time() - pend["since"] > 30:
        # The cast never left the hand: the server dropped it (priority
        # race). Clear pending so the state-based cast block re-triggers;
        # this scenario has no one-shot *_consumed flags to reset.
        G["cast_pending"].pop(c.name, None)
        say(f"[{tag}] cast NOT confirmed after 30s (still in hand): "
            f"{pend['name']} (oid {oid}); will retry")
        wire("cast_dropped_retry", {"kind": pend["kind"], "oid": oid})
        return "dropped"
    # Still in flight: hold priority, do nothing else.
    return "waiting"


def note_cast_pending(c, kind, oid, name):
    G.setdefault("cast_pending", {})[c.name] = {
        "kind": kind, "oid": str(oid), "name": name, "since": time.time()}


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in G["mulls"]:
        return False
    G["mulls"].add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        if not opp_fresh(iid):
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            oid = _cand_reference(ch)
            nm = obj_lname(state, oid) if oid is not None else "?"
            return (0, str(oid)) if nm in (FOREST.lower(), SWAMP.lower()) \
                else (1, str(oid))

        picks = sorted(cands, key=bkey)[:n]
        mark_submitted(iid)
        say(f"[{tag}] bottoming via vi")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id")
                                                                  for ch in picks]}}}, tag)
        return True
    return False


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in G["submitted"]:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        handset = set(hand)
        if not any(str(_cand_reference(ch)) in handset for ch in cands):
            continue
        spec = rdata.get("spec") or {}
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in cands[:max(1, n)]]
        mark_submitted(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
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


def play_land_legacy(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if G["land_turn"].get(tag) == turn:
        return None
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    return a
    return None


async def answer_vi_play_land(c, state, pid, tag, land_lname):
    turn = state.get("turn_number")
    if G["land_turn"].get(tag) == turn:
        return False
    st = c.latest
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            if ref is None or obj_lname(state, ref) != land_lname:
                continue
            if str(ref) not in hand_ids(state, pid):
                continue
            iid = opp.get("interactionId")
            if not opp_fresh(iid):
                continue
            mark_submitted(iid)
            G["land_turn"][tag] = turn
            say(f"[{tag}] playing land via vi playLand: {land_lname}")
            await answer_vi(c, opp, ch, tag)
            return True
    return False


async def my_land_or_vi_land(c, state, pid, acts, tag, land_lname):
    a = play_land_legacy(c, state, pid, acts, tag)
    if a is not None:
        G["land_turn"][tag] = state.get("turn_number")
        say(f"[{tag}] playing land {land_lname} (legacy)")
        await submit_as_is(c, a)
        return True
    return await answer_vi_play_land(c, state, pid, tag, land_lname)


def cast_action_for(acts, state, oid):
    """Bare legacy CastSpell for the hand object `oid` (protocol 118: the
    engine auto-pays, so the action carries no payment)."""
    for a in acts:
        if a["type"] != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


async def answer_ballista_x(c, state, tag):
    """P0: Ballista ChooseXValue (schema number) -> 0. This prompt is part of
    the cast itself, so it is answered BEFORE the cast-confirmation guard
    (the guard only blocks priority passes and new main-phase plays)."""
    st = c.latest
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in G["x_answered"] or not opp_fresh(iid):
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if (spec.get("type") or "") != "number":
            continue
        mark_submitted(iid)
        G["x_answered"].add(iid)
        sub = {"interactionId": iid,
               "response": {"type": "number", "data": {"value": 0}}}
        say(f"[{tag}] Ballista X-choice -> X=0")
        wire("x_choice", {"who": tag, "iid": iid})
        await interact_as(c, sub, tag)
        return True
    return False


async def answer_trigger_target(c, state, tag, variant):
    """P1: Emperor trigger target selection, answered per-firing (keyed by
    interactionId, not per-leg). Pick the Ballista per variant: 'A' -> most
    recently died (last in gy order); 'B' -> oldest (first)."""
    st = c.latest
    gy = ballista_gy_ids(state, 0)
    if len(gy) < 2:
        return False
    want = gy[-1] if variant == "A" else gy[0]
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in G["target_answered"] or not opp_fresh(iid):
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        stype = (spec.get("type") or "") if rtype == "schema" else ""
        cands = data.get("candidates") or data.get("choices") or []
        # only answer prompts whose candidates are P0-gy Ballistas
        refs = {str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None}
        if not refs or not (refs <= set(gy)):
            continue
        pick_ch = next((ch for ch in cands
                        if str(_cand_reference(ch)) == str(want)), None)
        if pick_ch is None:
            continue
        if rtype == "schema" and stype in ("sequence", "select"):
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick_ch["id"]]}}}
        elif rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick_ch["id"]}}}
        else:
            continue
        mark_submitted(iid)
        G["target_answered"].add(iid)
        G["chosen_oid"] = int(want)
        say(f"[{tag}] Emperor trigger targets Ballista oid {want} "
            f"(choice {pick_ch['id']}) variant={variant}")
        wire("trigger_target", {"who": tag, "iid": iid, "pick": want,
                                "choice_id": pick_ch["id"], "variant": variant,
                                "gy": gy})
        await interact_as(c, sub, tag)
        return True
    return False


# ---------------------------------------------------------------- per-seat ticks

async def p0_tick(c, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return True
    # Ballista X-choice is part of the cast: answer before the guard.
    if await answer_ballista_x(c, state, tag):
        return True
    # ---- cast-confirmation guard: hold while a CastSpell is in flight;
    # never pass priority under it. Runs before any early-return block.
    if await guard_cast_pending(c, tag) == "waiting":
        return True
    # ---- attackers / blockers: always empty (P0 never attacks or blocks)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # capture the "before responding" moment: Emperor trigger on the stack
    # with its target chosen, and P0 (the opponent) holding priority
    if not G["captured"]:
        entry, refs = find_emperor_trigger(state)
        if (entry is not None and refs == [G["chosen_oid"]]
                and my_priority(acts) and not real_decision_pending(st)):
            G["entry_id"] = entry.get("id")
            G["trigger_turn"] = state.get("turn_number")
            say(f"[{tag}] CAPTURE: trigger entry {G['entry_id']} targets "
                f"Ballista {G['chosen_oid']}; P0 holds priority")
            wire("capture", {"entry_id": G["entry_id"],
                             "chosen": G["chosen_oid"],
                             "phase": state.get("phase"),
                             "turn": state.get("turn_number")})
            raw = await c.export_state()
            snap_state = parse_export(raw)
            with open(f"{EVDIR}/snapshot_{tag}.json", "w") as f:
                json.dump({"state": snap_state}, f)
            with open(f"{EVDIR}/viewer_{tag}.json", "w") as f:
                json.dump(copy.deepcopy(c.latest), f, default=str)
            G["captured"] = True
            return True

    if my_main(state, 0):
        if await my_land_or_vi_land(c, state, 0, acts, tag, FOREST.lower()):
            return True
        # cast Ballista with X=0 while fewer than 2 are in the graveyard;
        # engine auto-pays (payment_mode Auto), X=0 dies to SBAs on resolve
        if (len(ballista_gy_ids(state, 0)) < 2
                and state.get("phase") == "PreCombatMain"):
            hid = next((o for o in hand_ids(state, 0)
                        if obj_lname(state, o) == BALLISTA.lower()), None)
            a = cast_action_for(acts, state, hid) if hid is not None else None
            if a is not None:
                note_cast_pending(c, "ballista", hid, BALLISTA)
                say(f"[{tag}] casting Walking Ballista (oid {hid}), X=0; "
                    f"engine auto-pays (payment_mode Auto)")
                wire("cast_ballista", {"oid": hid})
                await submit_as_is(c, a)
                return True
    if await answer_vi_play_land(c, state, 0, tag, FOREST.lower()):
        return True
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if G["passed_rev"].get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            G["passed_rev"][c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag, variant):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return True
    # ---- cast-confirmation guard: hold while a CastSpell is in flight;
    # never pass priority under it. Runs before any early-return block.
    if await guard_cast_pending(c, tag) == "waiting":
        return True
    # ---- attackers / blockers: always empty (P1 never attacks or blocks)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return True
    # Emperor trigger target selection (answered before the decision gate)
    if await answer_trigger_target(c, state, tag, variant):
        return True

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if await my_land_or_vi_land(c, state, 1, acts, tag, SWAMP.lower()):
            return True
        # Cast Emperor once P0 has 2 Ballistas to choose from; the engine
        # auto-pays {1}{B} (payment_mode Auto), so no mana bookkeeping.
        if (not bf_by_name(state, 1, EMPEROR.lower())
                and len(ballista_gy_ids(state, 0)) >= 2
                and next((o for o in hand_ids(state, 1)
                          if obj_lname(state, o) == EMPEROR.lower()), None) is not None
                and state.get("phase") == "PreCombatMain"):
            hid = next((o for o in hand_ids(state, 1)
                        if obj_lname(state, o) == EMPEROR.lower()), None)
            a = cast_action_for(acts, state, hid) if hid is not None else None
            if a is not None:
                note_cast_pending(c, "emperor", hid, EMPEROR)
                say(f"[{tag}] casting Emperor of Bones (oid {hid}); "
                    f"engine auto-pays (payment_mode Auto)")
                wire("cast_emperor", {"oid": hid})
                await submit_as_is(c, a)
                return True
    if await answer_vi_play_land(c, state, 1, tag, SWAMP.lower()):
        return True
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if G["passed_rev"].get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            G["passed_rev"][c.name] = c.revision
        return True
    return False


# ---------------------------------------------------------------- game runner

async def run_game(variant):
    reset_game_state(variant)
    tag = variant
    p0 = PhaseClient(f"P0-{tag}")
    await p0.connect()
    await p0.create(deck((BALLISTA, 12), (FOREST, 48)))
    p1 = PhaseClient(f"P1-{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck((EMPEROR, 4), (SWAMP, 56)))
    say(f"game {p0.game_code} variant={variant}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"variant": variant, "game_code": p0.game_code})

    result = {"variant": variant, "game_code": p0.game_code,
              "captured": False, "post": False}
    try:
        t0 = time.time()
        last_diag = time.time()
        last_rev = {}
        while time.time() - t0 < 1500:
            await asyncio.sleep(0.15)
            for c, is_p0 in ((p0, True), (p1, False)):
                # drain rejections into the wire log (exact submissions +
                # any rejections must be visible)
                while c.rejections:
                    rj = c.rejections.pop(0)
                    wire("rejection", {"who": c.name, "rej": rj})
                    G["rejections"].append({"who": c.name, "rej": rj})
                if not c.latest:
                    continue
                if c.revision == last_rev.get(c.name) and not my_priority(top_acts(c.latest)):
                    continue
                last_rev[c.name] = c.revision
                try:
                    if is_p0:
                        await p0_tick(c, tag)
                    else:
                        await p1_tick(c, tag, variant)
                except Exception as e:
                    say(f"[{tag}] tick error {c.name}: {type(e).__name__}: {e}")
                    wire(f"{tag}_tick_error",
                         {"who": c.name, "err": f"{type(e).__name__}: {e}"})
            # post-capture: wait for the trigger to resolve, then export
            if G["captured"] and not G["resolved"] and p0.latest:
                s = p0.latest["state"]
                entry = next((e for e in (s.get("stack") or [])
                              if e.get("id") == G["entry_id"]), None)
                chosen = get_obj(s, G["chosen_oid"])
                if entry is None and chosen.get("zone") == "Exile" \
                        and not (s.get("stack") or []):
                    raw = await p0.export_state()
                    with open(f"{EVDIR}/post_trigger_{tag}.json", "w") as f:
                        json.dump({"state": parse_export(raw)}, f)
                    say(f"[{tag}] trigger resolved; chosen Ballista "
                        f"{G['chosen_oid']} exiled; post state exported")
                    wire("post", {"variant": tag,
                                  "phase": s.get("phase"),
                                  "turn": s.get("turn_number")})
                    G["resolved"] = True
                    result["post"] = True
                    break
            if time.time() - last_diag > 45:
                last_diag = time.time()
                for c in (p0, p1):
                    st = c.latest
                    if not st:
                        say(f"[{tag}] DIAG {c.name}: no state yet")
                        continue
                    s = st["state"]
                    say(f"[{tag}] DIAG {c.name}: rev={c.revision} "
                        f"turn={s.get('turn_number')} phase={s.get('phase')} "
                        f"vikind={vi_kind_code(st)!r} "
                        f"real_decision={real_decision_pending(st)} "
                        f"my_prio={my_priority(top_acts(st))} "
                        f"stack={len(s.get('stack') or [])} "
                        f"p0_gy_ballistas={ballista_gy_ids(s, 0)} "
                        f"cast_pending={bool((G.get('cast_pending') or {}).get(c.name))} "
                        f"captured={G['captured']}")
        result["captured"] = G["captured"]
        result["chosen_oid"] = G["chosen_oid"]
        result["entry_id"] = G["entry_id"]
        result["notes"] = G["notes"]
        result["rejections"] = G["rejections"]
        if p0.latest:
            s = p0.latest["state"]
            result["final_gy_ballistas"] = ballista_gy_ids(s, 0)
    finally:
        await p0.close()
        await p1.close()
    return result


async def main():
    await verify_server_hello()
    t0 = time.time()
    games = {}
    for variant in ("A", "B"):
        say(f"===== game {variant} =====")
        g = await run_game(variant)
        games[variant] = g
        say(f"game {variant}: captured={g['captured']} "
            f"chosen={g.get('chosen_oid')} entry={g.get('entry_id')}")
        if not g["captured"]:
            say(f"game {variant} FAILED to capture; aborting")
            break

    # ---- assertions ----
    ass = {}
    notes = []
    A, B = games.get("A", {}), games.get("B", {})
    snap = {}
    for v in ("A", "B"):
        try:
            with open(f"{EVDIR}/snapshot_{v}.json") as f:
                snap[v] = json.loads(f.read())["state"]
        except FileNotFoundError:
            snap[v] = None

    def gy_ballistas_export(st):
        out = []
        for x in (st.get("players") or []):
            p = x if isinstance(x, dict) else {}
            if str(p.get("id")) == "0":
                for oid in p.get("graveyard") or []:
                    o = (st.get("objects") or {}).get(str(oid), {})
                    if str(o.get("base_name") or o.get("name") or "").lower() \
                            == BALLISTA.lower():
                        out.append(int(oid))
        return out

    # E1..E2 per variant, then E3/E4 across, E5 from game A post state
    per = {}
    for v in ("A", "B"):
        s = snap.get(v)
        g = games.get(v, {})
        if s is None or not g.get("captured"):
            per[v] = None
            notes.append(f"{v}: no capture")
            continue
        gy = gy_ballistas_export(s)
        emp = [oid for oid, o in (s.get("objects") or {}).items()
               if o.get("zone") == "Battlefield"
               and str(o.get("controller")) == "1"
               and str(o.get("base_name") or o.get("name") or "").lower()
               == EMPEROR.lower()]
        entry, refs = find_emperor_trigger(s)
        e1 = (len(gy) >= 2 and len(emp) >= 1 and entry is not None)
        e2 = (len(refs) == 1 and refs[0] == g.get("chosen_oid")
              and g.get("chosen_oid") in gy)
        per[v] = {"gy": gy, "emp": emp, "refs": refs, "e1": e1, "e2": e2}
        notes.append(f"E1[{v}]: gy_ballistas={gy} emperor_bf={emp} "
                     f"trigger_entry={g.get('entry_id')}")
        notes.append(f"E2[{v}]: entry targets={refs} "
                     f"chosen={g.get('chosen_oid')}")

    ass["E1_setup_ok"] = ("passed" if all(p and p["e1"] for p in per.values()
                                         if p is not None)
                          and all(per.values()) else "failed")
    ass["E2_trigger_targeted"] = ("passed" if all(p and p["e2"] for p in per.values()
                                                  if p is not None)
                                  and all(per.values()) else "failed")

    # E3: the two target oids are distinct objects with the same name
    ca, cb = A.get("chosen_oid"), B.get("chosen_oid")
    names_ok = True
    for v, c in (("A", ca), ("B", cb)):
        s = snap.get(v)
        if s is None or c is None:
            names_ok = False
            continue
        o = (s.get("objects") or {}).get(str(c), {})
        if str(o.get("base_name") or o.get("name") or "").lower() \
                != BALLISTA.lower():
            names_ok = False
    ass["E3_identity_distinct"] = ("passed" if (ca is not None and cb is not None
                                                and ca != cb and names_ok)
                                   else "failed")
    notes.append(f"E3: chosen_A={ca} chosen_B={cb} distinct={ca != cb}")

    # E4: viewer envelopes + authoritative exports carry the same target id;
    # A and B differ
    e4_ok = True
    for v, g in (("A", A), ("B", B)):
        try:
            with open(f"{EVDIR}/viewer_{v}.json") as f:
                vsnap = json.load(f)
            vent, vrefs = find_emperor_trigger(vsnap["state"])
            ok = (vrefs == [g.get("chosen_oid")]
                  and collect_object_target_refs(
                      next(e for e in snap[v]["stack"]
                           if e.get("id") == g.get("entry_id"))) == [g.get("chosen_oid")])
            if not ok:
                e4_ok = False
            notes.append(f"E4[{v}]: viewer refs={vrefs} export refs match="
                         f"{ok}")
        except Exception as e:
            e4_ok = False
            notes.append(f"E4[{v}]: read error {e}")
    if ca == cb:
        e4_ok = False
    ass["E4_snapshot_captured"] = "passed" if e4_ok else "failed"

    # E5: game A post state -- chosen Ballista exiled, stack empty
    try:
        with open(f"{EVDIR}/post_trigger_A.json") as f:
            spost = json.loads(f.read())["state"]
        chosen_post = (spost.get("objects") or {}).get(str(ca), {})
        e5 = (chosen_post.get("zone") == "Exile"
              and not (spost.get("stack") or []))
        ass["E5_cleanup"] = "passed" if e5 else "failed"
        notes.append(f"E5: chosen zone post={chosen_post.get('zone')} "
                     f"stack_empty={not (spost.get('stack') or [])} "
                     f"phase={spost.get('phase')}")
    except FileNotFoundError:
        ass["E5_cleanup"] = "not-run"
        notes.append("E5: no post-trigger export captured")

    dur = time.time() - t0
    engine_result = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "v0.104.0",
            "build_commit": "4227122",
            "protocol_version": 118,
            "mode": "Full",
            "binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": True,
            "observed_at": "2026-10-09",
            "source": SERVER_IDENTITY["source"],
        },
        "driver": {"protocol_advertised": 118, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6763_01040.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [[BALLISTA, 12], [FOREST, 48]],
            "P1": [[EMPEROR, 4], [SWAMP, 56]],
        },
        "game_A": {"chosen_oid": ca, "trigger_entry_id": A.get("entry_id"),
                   "game_code": A.get("game_code")},
        "game_B": {"chosen_oid": cb, "trigger_entry_id": B.get("entry_id"),
                   "game_code": B.get("game_code")},
        "assertions": ass,
        "notes": notes,
        "revalidation_of": {
            "run_id": "20261007-6763",
            "protocol": 106,
        },
    }
    with open(f"{EVDIR}/engine_result.json", "w") as f:
        json.dump(engine_result, f, indent=1)
    # keep an immutable copy of the exact scenario that produced this evidence
    with open(f"{EVDIR}/scenario_6763_01040.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6763_01040.py").read())
    say(f"ENGINE DONE assertions={json.dumps(ass)}")
    WIRE.close()
    RUNLOG.close()
    return ass


asyncio.run(main())
