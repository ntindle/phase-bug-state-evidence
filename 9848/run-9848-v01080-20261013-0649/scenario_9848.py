#!/usr/bin/env python3
"""Issue #9848: Reservoir Kraken -- spurious stack-time target selection for
"any opponent may tap an untapped creature they control".

Oracle: "Trample, ward {2}
At the beginning of each combat, if this creature is untapped, any opponent
may tap an untapped creature they control. If they do, tap this creature and
create a 1/1 blue Fish creature token with "This token can't be blocked.""

Report (phase-rs/phase#9848, opened 2026-10-12 by matthewevans, open,
bug/area:engine): the intervening-if trigger raises a TriggerTargetSelection
when it goes on the stack although the ability has no target. Expected: no
target selection; opponents are asked at resolution. Actual (reporter's
probe): a target-selection prompt is raised at stack time; the end result is
correct once answered. Where: trigger target slot enumeration
(collect_target_slots_inner) for opponent-scoped optional clauses.
CR 603.4 (intervening-if checked when the trigger would go on the stack),
CR 115.1 (targets are declared as part of putting the ability on the
stack -- this ability has none).

Behavioral contract (per playbook: test the reported OUTCOME):
  P0 ramps to 4 mana and casts Reservoir Kraken; P1 fields an Aegis Turtle.
  On a BeginCombat step with the Kraken untapped, the trigger fires.
  Expected (correct): NO target-selection prompt at stack time; at
      resolution the opponent (P1) is asked whether to tap an untapped
      creature they control; if they tap one, the Kraken taps and a 1/1
      blue Fish token is created.
  Bug (per the issue): a TriggerTargetSelection prompt is raised at stack
      time (before any priority pass on the trigger).

Assertions:
  A1 setup_ok        game created, both seats joined
  A2 fixture_ok      Kraken on P0 BF untapped when the trigger window opens;
                     P1 controls >=1 untapped creature then
  A3 pre_trigger_export  pre_trigger.json captured and parses
  A4 no_spurious_stacktime_target  no target-selection prompt observed while
                     the Kraken trigger is on the stack pre-resolution
                     (FAILED = the reported bug)
  A5 opponent_asked_at_resolution  P1 offered the may-tap choice during
                     resolution (decideOptionalEffect or creature choice)
  A6 resolution_effect  post-resolution: P1's chosen Turtle tapped, Kraken
                     tapped, 1/1 blue Fish token on P0's battlefield
  A7 data_shape      card-data trigger lower matches the reported shape
                     (Phase/BeginCombat, intervening-if Not(SourceIsTapped),
                     first effect optional SetTapState on an untapped
                     Creature controlled by "You")

Verdict:
  reproduced      A2 passed and A4 failed (spurious stack-time prompt seen)
  not-reproduced  A2 passed and A4 passed and A5 passed
  blocked         fixture never reached, or A5 not evaluable

Driver conventions (verified scenario_9847.py, protocol 134):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (viewer_interaction)
  - exact card-data names; export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; vi candidate references are strings
    (normalize with str() before oid comparisons)
  - real_decision_pending filters None surface codes (9845 lesson)
  - genuine card-choice candidates have NO action surface (9842 lesson)
"""
import asyncio
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
log = logging.getLogger("scenario9848")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9848-v01080-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9848
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.108.0",
    "build_commit": "b8fa1d3",
    "protocol_version": 134,
    "mode": "Full",
    "server_binary_sha256": "83a3d30919ea7d56b98054048566d105116523abb2b88e5e4478c40ecb00c1f7",
    "card_data_sha256": "2f65a5829b509ad69a32110ff9a1361363e7f97f24c41c6a53d4ae6850e8e813",
    "draft_pools_sha256": "8c9945e3c7d511ebb1ca9ff37bee5b1f58144c1c4b5d6bed52f81d6d050c7297",
    "signature_verified": True,
    "signature_note": ("v0.108.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.108.0 (published "
               "2026-10-12T21:10:34Z) == pinned release dir; ServerHello "
               "0.108.0/b8fa1d3/protocol 134 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.108.0 server on 127.0.0.1:9374 started by this run; "
               "scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9848/" + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


for _f, _k in (("server/releases/v0.108.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.108.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.108.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against the ledger-pinned v0.108.0 digests",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.108.0/data/card-data.json"))


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

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


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


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_names(state, pid):
    return [obj_lname(state, o) for o in bf_oids(state, pid)]


def is_land(o):
    # NOTE: runtime state objects use "card_types" (plural); card-data.json
    # uses "card_type" (singular). This helper reads runtime state.
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def is_creature(o):
    return "Creature" in (((o.get("card_types") or {}).get("core_types")) or [])


def kraken_oid(state):
    for o in bf_oids(state, 0):
        if obj_lname(state, o) == "reservoir kraken":
            return o
    return None


def turtle_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == "aegis turtle"]


def fish_token_oids(state):
    return [o for o in bf_oids(state, 0)
            if "fish" in obj_lname(state, o)]


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def untapped_creature_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_creature(get_obj(state, o)) and not get_obj(state, o).get("tapped")]


def stack_raw(state):
    return [{"name": obj_lname(state, e.get("object_id", e.get("id", ""))),
             "kind": (e.get("kind") or {}).get("type") if isinstance(e.get("kind"), dict) else e.get("kind"),
             "controller": e.get("controller"),
             "raw_keys": sorted(e.keys())}
            for e in state.get("stack") or []]


def kraken_trigger_on_stack(state):
    """True when the Kraken's BeginCombat trigger is on the stack.

    Primary signal: a stack entry referencing the Kraken object (or a
    trigger-kind entry naming the kraken). Fallback: ANY stack entry while
    phase is BeginCombat with an untapped Kraken on P0's battlefield --
    this fixture has no other triggers, so any stack entry then is the
    Kraken trigger. Returns (found, entry_or_None, method)."""
    kr = kraken_oid(state)
    entries = state.get("stack") or []
    for e in entries:
        for key in ("object_id", "id", "source_id", "source"):
            v = e.get(key)
            try:
                if v is not None and kr is not None and int(v) == int(kr):
                    return True, e, "kraken-oid"
            except (TypeError, ValueError):
                pass
        kind = e.get("kind")
        ks = json.dumps(kind, default=str).lower() if kind else ""
        nm = obj_lname(state, e.get("object_id", e.get("id", "")))
        if "trigger" in ks and "kraken" in nm:
            return True, e, "trigger-kind+kraken-name"
    if entries and state.get("phase") == "BeginCombat" and kr is not None \
            and not get_obj(state, kr).get("tapped"):
        return True, entries[-1], "fallback:any-stack-entry-in-BeginCombat"
    return False, None, None


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


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
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    """Verbatim from the verified scenario_301_01080.py (None codes filtered)."""
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
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices whose
    choices carry candidate/target codes (passPriority menus and
    decideOptionalEffect excluded)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def find_decide_optional_opp(st):
    """exactChoices opportunity carrying a decideOptionalEffect action code
    (the may-tap choice at resolution)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


def _cand_ref(ch):
    """Card oid reference for a candidate: top-level or nested in a
    surface's data (protocol-134 discard/select prompts nest it)."""
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def candidate_owner_seat(state, ch):
    """Controller seat of a permanent candidate (best effort)."""
    ref = _cand_ref(ch)
    if ref is None:
        return None
    o = get_obj(state, ref)
    try:
        return int(o.get("controller"))
    except (TypeError, ValueError):
        return None


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
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.108.0"), f"unexpected version {ver}"
    assert int(proto) == 134, f"unexpected protocol {proto}"
    assert str(build) == "b8fa1d3", f"unexpected build {build}"


def check_data_level():
    """Record the parser shapes this scenario depends on: Reservoir Kraken's
    trigger must be the reported shape -- Phase/BeginCombat, intervening-if
    Not(SourceIsTapped), first effect optional SetTapState targeting an
    untapped Creature controlled by "You"."""
    k = CARD_DATA.get("reservoir kraken", {})
    trigs = k.get("triggers") or []
    notes = []
    ok = True
    t = trigs[0] if trigs else {}
    if t.get("mode") != "Phase" or t.get("phase") != "BeginCombat":
        ok = False
        notes.append(f"trigger is not Phase/BeginCombat: mode={t.get('mode')} phase={t.get('phase')}")
    cond = t.get("condition") or {}
    if not (cond.get("type") == "Not"
            and (cond.get("condition") or {}).get("type") == "SourceIsTapped"):
        ok = False
        notes.append(f"intervening-if is not Not(SourceIsTapped): {json.dumps(cond)[:200]}")
    e = (t.get("execute") or {})
    eff = e.get("effect") or {}
    tgt = eff.get("target") or {}
    tf = tgt.get("type_filters") or []
    untapped_prop = any(isinstance(p, dict) and p.get("type") == "Untapped"
                        for p in (tgt.get("properties") or []))
    if not (eff.get("type") == "SetTapState" and (eff.get("state") or {}).get("type") == "Tap"):
        ok = False
        notes.append(f"first effect is not SetTapState/Tap: {json.dumps(eff)[:200]}")
    if not (tgt.get("type") == "Typed" and "Creature" in tf
            and tgt.get("controller") == "You" and untapped_prop):
        ok = False
        notes.append(f"first-effect target is not Typed Creature{{controller You, Untapped}}: "
                     f"{json.dumps(tgt)[:250]}")
    if e.get("optional") is not True:
        ok = False
        notes.append(f"first effect is not optional: optional={e.get('optional')!r}")
    # the follow-ups: tap self, then create the Fish token
    sub1 = (e.get("sub_ability") or {})
    e1 = (sub1.get("effect") or {})
    if not (e1.get("type") == "SetTapState"
            and (e1.get("target") or {}).get("type") == "SelfRef"):
        ok = False
        notes.append(f"second effect is not SetTapState SelfRef: {json.dumps(e1)[:200]}")
    sub2 = (sub1.get("sub_ability") or {})
    e2 = (sub2.get("effect") or {})
    if not (e2.get("type") == "Token" and e2.get("name") == "Fish"):
        ok = False
        notes.append(f"third effect is not Token Fish: {json.dumps(e2)[:200]}")
    mc = k.get("mana_cost") or {}
    if not (mc.get("generic") == 2 and mc.get("shards") == ["Blue", "Blue"]):
        ok = False
        notes.append(f"mana cost is not {{2}}{{U}}{{U}}: {json.dumps(mc)[:150]}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle_text": k.get("oracle_text"),
                   "trigger": t}, f, indent=1, default=str)
    say(f"data-level check: ok={ok} notes={notes}")
    wire("data_level", {"ok": ok, "notes": notes})
    assert ok, f"data-level check failed: {notes}"


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_names(state, pid)
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    if len(hand_ids(state, pid)) <= 7:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if _cand_ref(ch) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "handsize", iid)
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        picks = cands[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:40] for x in picks]}")
        wire("handsize_discard", {"who": tag, "iid": iid})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
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


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def pick_optional_accept(opp):
    """Pick the accept choice of a decideOptionalEffect opportunity."""
    chs = (opp.get("response", {}).get("data", {}) or {}).get("choices", [])
    for ch in chs:
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "tap an untapped" in txt or "tap target" in txt:
                is_accept = "don't" not in txt and "do not" not in txt
            elif txt.strip() in ("yes", "accept"):
                is_accept = True
            elif "decline" in txt or "don't" in txt or "do not" in txt \
                    or txt.strip() == "no":
                is_accept = False
        if is_accept:
            return ch
    return None


# ---------------------------------------------------------------- trigger window

def update_window(c, st, ctx, tag):
    """Track the Kraken trigger's stack lifetime. Opens the window when the
    trigger is first observed on the stack; marks resolving when it leaves."""
    if ctx.get("window_done"):
        return
    state = st["state"]
    on_stack, entry, method = kraken_trigger_on_stack(state)
    if on_stack and not ctx.get("window_open"):
        kr = kraken_oid(state)
        ctx["window_open"] = True
        ctx["window_open_turn"] = state.get("turn_number")
        ctx["window_open_phase"] = state.get("phase")
        ctx["window_open_active"] = state.get("active_player")
        ctx["window_kraken_oid"] = kr
        ctx["window_kraken_tapped"] = bool(get_obj(state, kr).get("tapped")) if kr else None
        ctx["window_p1_untapped_creatures"] = untapped_creature_oids(state, 1)
        ctx["need_pre_export"] = True
        wire("window_open", {
            "who": tag, "rev": c.revision,
            "turn": state.get("turn_number"), "phase": state.get("phase"),
            "active_player": state.get("active_player"),
            "match_method": method,
            "stack": stack_raw(state),
            "kraken_oid": kr, "kraken_tapped": ctx["window_kraken_tapped"],
            "p1_untapped_creatures": ctx["window_p1_untapped_creatures"],
            "p1_bf": bf_names(state, 1),
        })
        say(f"[{tag}] WINDOW OPEN: kraken trigger on stack "
            f"(turn={state.get('turn_number')} phase={state.get('phase')} "
            f"active={state.get('active_player')} method={method}); "
            f"kraken_tapped={ctx['window_kraken_tapped']} "
            f"p1_untapped_creatures={len(ctx['window_p1_untapped_creatures'])}")
    if ctx.get("window_open") and not on_stack and not ctx.get("resolving"):
        ctx["resolving"] = True
        wire("trigger_left_stack", {"who": tag, "rev": c.revision,
                                    "turn": state.get("turn_number"),
                                    "phase": state.get("phase"),
                                    "stack": stack_raw(state)})
        say(f"[{tag}] trigger left the stack -- resolution underway")


async def record_and_answer_spurious(c, opp, rtype, stype, ctx, tag, seat):
    """A target-selection prompt observed while the Kraken trigger is still
    on the stack (pre-resolution): this is the reported bug. Record its full
    shape, then answer it (first candidate) so the game can proceed -- the
    issue notes the end result is correct once answered."""
    state = c.latest["state"]
    iid = opp.get("interactionId")
    key = (tag, "spurious", seat, str(iid))
    if key in SUBMITTED_OPPS:
        return False
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    recs = []
    for ch in cands:
        ref = _cand_ref(ch)
        o = get_obj(state, ref) if ref else {}
        recs.append({"text": choice_text(ch)[:80],
                     "ref": ref,
                     "ref_name": obj_lname(state, ref) if ref else None,
                     "ref_controller": o.get("controller"),
                     "ref_zone": o.get("zone"),
                     "owner_seat": candidate_owner_seat(state, ch)})
    rec = {"seat": seat, "who": tag, "rtype": rtype, "stype": stype,
           "iid": str(iid)[:12], "n_candidates": len(recs),
           "candidates": recs}
    ctx.setdefault("spurious_prompts", []).append(rec)
    wire("spurious_target_prompt", rec)
    wire("spurious_target_prompt_full", {"iid": str(iid)[:12], "opp": opp})
    say(f"[{tag}] SPURIOUS stack-time target prompt (seat {seat}): "
        f"{rtype}/{stype}, {len(recs)} candidates: "
        f"{[(r['ref_name'], r['ref_controller']) for r in recs[:6]]}")
    SUBMITTED_OPPS.add(key)
    pick = cands[0]
    n_rej_before = len(c.rejections)
    await answer_vi(c, opp, pick, tag)
    await asyncio.sleep(0.5)
    if len(c.rejections) > n_rej_before:
        wire("spurious_answer_rejected",
             {"who": tag, "rejection": c.rejections[-1]})
        say(f"[{tag}] WARNING: spurious-prompt answer rejected: "
            f"{json.dumps(c.rejections[-1])[:200]}")
    return True


async def answer_may_tap(c, opp, ctx, tag, seat):
    """P1's at-resolution may-tap choice (decideOptionalEffect): accept so
    the full tap chain is exercised."""
    iid = opp.get("interactionId")
    key = (tag, "may-tap", str(iid))
    if key in SUBMITTED_OPPS:
        return False
    chs = (opp.get("response", {}).get("data", {}) or {}).get("choices", [])
    wire("may_tap_prompt", {
        "who": tag, "seat": seat, "iid": str(iid)[:12],
        "choices": [{"text": choice_text(ch)[:100],
                     "codes": sorted(cc for cc in surf_codes(ch) if cc),
                     "surfaces": ch.get("surfaces")}
                    for ch in chs]})
    pick = pick_optional_accept(opp)
    if pick is None:
        say(f"[{tag}] WARNING: may-tap prompt with no identifiable accept "
            f"choice; leaving pending")
        wire("may_tap_no_accept", {"who": tag, "iid": str(iid)[:12]})
        return False
    SUBMITTED_OPPS.add(key)
    ctx["resolution_choice_seen"] = True
    ctx["resolution_phase"] = True
    say(f"[{tag}] accepting may-tap (seat {seat})")
    wire("may_tap_accept", {"who": tag, "choice": choice_text(pick)[:80]})
    await answer_vi(c, opp, pick, tag)
    return True


async def answer_resolution_creature(c, opp, ctx, tag, seat):
    """P1's at-resolution creature choice: tap one of P1's untapped
    creatures (prefer the Aegis Turtle)."""
    state = c.latest["state"]
    iid = opp.get("interactionId")
    key = (tag, "resolve-creature", str(iid))
    if key in SUBMITTED_OPPS:
        return False
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    pick, pick_ref = None, None
    for ch in cands:
        ref = _cand_ref(ch)
        if ref and obj_lname(state, ref) == "aegis turtle":
            o = get_obj(state, ref)
            if str(o.get("controller")) == "1" and not o.get("tapped"):
                pick, pick_ref = ch, ref
                break
    if pick is None:
        for ch in cands:
            ref = _cand_ref(ch)
            if ref:
                o = get_obj(state, ref)
                if str(o.get("controller")) == "1" \
                        and is_creature(o) and not o.get("tapped"):
                    pick, pick_ref = ch, ref
                    break
    if pick is None and cands:
        pick, pick_ref = cands[0], _cand_ref(cands[0])
    if pick is None:
        say(f"[{tag}] WARNING: resolution creature prompt with no "
            f"candidates; leaving pending")
        return False
    SUBMITTED_OPPS.add(key)
    ctx["resolution_choice_seen"] = True
    ctx["resolution_phase"] = True
    ctx["tapped_creature_oid"] = pick_ref
    say(f"[{tag}] P1 tapping {obj_lname(state, pick_ref) if pick_ref else '?'} "
        f"(oid {pick_ref}) for the Kraken trigger")
    wire("resolution_creature_pick",
         {"who": tag, "ref": pick_ref,
          "name": obj_lname(state, pick_ref) if pick_ref else None})
    await answer_vi(c, opp, pick, tag)
    return True


async def handle_window_prompt(c, st, ctx, tag, seat):
    """Classify and answer prompts during the Kraken trigger window.

    - decideOptionalEffect -> the at-resolution may-tap choice (legitimate).
    - target-shaped while the trigger is still on the stack -> SPURIOUS
      (the reported bug); record and answer to unblock.
    - target-shaped after the trigger left the stack (or after the may was
      accepted) -> legitimate resolution creature choice."""
    if not ctx.get("window_open") or ctx.get("window_done"):
        return False
    opp = find_decide_optional_opp(st)
    if opp is not None:
        return await answer_may_tap(c, opp, ctx, tag, seat)
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    if ctx.get("resolving") or ctx.get("resolution_phase"):
        return await answer_resolution_creature(c, opp, ctx, tag, seat)
    return await record_and_answer_spurious(c, opp, rtype, stype, ctx, tag, seat)


# ---------------------------------------------------------------- legs

KRAKEN = "reservoir kraken"
TURTLE = "aegis turtle"


async def common_tick_head(c, acts, st, pid, tag, ctx):
    """Shared head for both legs. Returns 'acted' (True/False) or None to
    continue with seat-specific logic."""
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    return None


async def p1_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, 1, tag, ctx)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    update_window(c, st, ctx, tag)
    if await handle_window_prompt(c, st, ctx, tag, 1):
        return

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
        if TURTLE in hand_names(state, 1) \
                and untapped_land_count(state, 1) >= 1 \
                and len(turtle_oids(state, 1)) < 2:
            a, oid = cast_action_for(acts, state, TURTLE)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1}
                say(f"[{tag}] casting {TURTLE} (oid {oid})")
                wire("cast_turtle_p1", {"oid": oid})
                await submit_as_is(c, a)
                return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, 0, tag, ctx)
    if r is not None:
        return True
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    update_window(c, st, ctx, tag)
    if ctx.pop("need_pre_export", False):
        say(f"[{tag}] pre-trigger export")
        pre = json.loads(await c.export_state())
        with open(f"{EVDIR}/pre_trigger.json", "w") as f:
            json.dump(pre, f, indent=1)
        wire("pre_trigger_export", {"who": tag})
        ctx["pre_exported"] = True
    if await handle_window_prompt(c, st, ctx, tag, 0):
        return True

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_names(state, 0)
        kr = kraken_oid(state)
        if kr is None and KRAKEN in hn and untapped_land_count(state, 0) >= 4:
            a, oid = cast_action_for(acts, state, KRAKEN)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 2, "generic": 2}
                say(f"[{tag}] casting Reservoir Kraken (oid {oid})")
                wire("cast_kraken", {"oid": oid})
                await submit_as_is(c, a)
                return True
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


def _opp_summary(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        out.append({"iid": str(opp.get("interactionId"))[:8],
                    "rtype": resp.get("type"),
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c)})
    return out


# ---------------------------------------------------------------- run

async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9848-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {"window_open": False, "window_done": False, "resolving": False,
           "resolution_phase": False, "resolution_choice_seen": False,
           "spurious_prompts": [], "pre_exported": False,
           "need_pre_export": False, "tapped_creature_oid": None,
           "kraken_cast": False}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Island", 56), ("Reservoir Kraken", 4)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 52), ("Aegis Turtle", 8)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                                and p1.player_id is not None) else "failed"

    t0 = time.time()
    it = 0
    last_diag = 0
    post_settle_ticks = 0
    while time.time() - t0 < 900:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx)
        await p0_tick(p0, tag, ctx)
        await asyncio.sleep(0)
        it += 1
        if time.time() - last_diag > 20:
            last_diag = time.time()
            for c, pidx in ((p0, 0), (p1, 1)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"decision_pending={decision_pending(st)} "
                    f"my_priority={my_priority(acts)} "
                    f"window_open={ctx.get('window_open')} "
                    f"resolving={ctx.get('resolving')} "
                    f"spurious={len(ctx.get('spurious_prompts', []))} "
                    f"p0bf={bf_names(s, 0)[:6]} p1bf={bf_names(s, 1)[:6]}")
                if decision_pending(st):
                    say(f"[diag] {c.name} pending opps: {_opp_summary(st)}")
        # after the trigger resolved and the resolution choice was handled,
        # let priority settle, then finish
        if ctx.get("resolving"):
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                post_settle_ticks += 1
            else:
                post_settle_ticks = 0
            if post_settle_ticks >= 8 or \
                    (time.time() - t0 > 120 and ctx.get("resolution_choice_seen")):
                break
        if ctx.get("window_open") and not ctx.get("resolving") \
                and time.time() - t0 > 600:
            # trigger stuck on the stack far too long; capture and bail
            say(f"[{tag}] window stuck open >600s; capturing state and bailing")
            wire("window_stuck", {"stack": stack_raw(p0.latest["state"])})
            break

    st = p0.latest
    state = st["state"] if st else {}

    # ---- assertions
    kr_open = ctx.get("window_kraken_oid")
    p1c = ctx.get("window_p1_untapped_creatures") or []
    obs["assert"]["A2_fixture_ok"] = "passed" if (
        ctx.get("window_open") and kr_open is not None
        and ctx.get("window_kraken_tapped") is False and len(p1c) >= 1
    ) else "failed"
    if not ctx.get("window_open"):
        obs["notes"].append(
            f"trigger window never opened: turn={state.get('turn_number')} "
            f"phase={state.get('phase')} p0bf={bf_names(state, 0)} "
            f"p1bf={bf_names(state, 1)} p0hand={hand_names(state, 0)}")
    try:
        with open(f"{EVDIR}/pre_trigger.json") as f:
            json.load(f)
        obs["assert"]["A3_pre_trigger_export"] = "passed"
    except (OSError, ValueError):
        obs["assert"]["A3_pre_trigger_export"] = (
            "failed" if ctx.get("window_open") else "not-run")

    spurious = ctx.get("spurious_prompts", [])
    obs["spurious_prompts"] = [
        {k: v for k, v in r.items()} for r in spurious]
    obs["assert"]["A4_no_spurious_stacktime_target"] = (
        "failed" if spurious else
        ("passed" if ctx.get("window_open") else "not-run"))
    obs["assert"]["A5_opponent_asked_at_resolution"] = (
        "passed" if ctx.get("resolution_choice_seen") else
        ("not-run" if not ctx.get("resolving") else "failed"))

    # ---- post export + A6
    post = None
    if ctx.get("resolving"):
        for _ in range(4):
            await asyncio.sleep(0.5)
            await p1_tick(p1, f"P1{tag}", ctx)
            await p0_tick(p0, tag, ctx)
        say(f"[{tag}] post-resolution export")
        post = json.loads(await p0.export_state())
        with open(f"{EVDIR}/post_resolution.json", "w") as f:
            json.dump(post, f, indent=1)
        wire("post_resolution_export", {"who": tag})
    else:
        obs["notes"].append("no post export: trigger never resolved")
    obs["assert"]["A6_resolution_effect"] = "not-run"
    if post is not None:
        pstate = post.get("state", post)
        kr = kraken_oid(pstate)
        kr_tapped = bool(get_obj(pstate, kr).get("tapped")) if kr else None
        fish = fish_token_oids(pstate)
        toid = ctx.get("tapped_creature_oid")
        tobj = get_obj(pstate, toid) if toid else {}
        turtle_tapped = bool(tobj.get("tapped")) if toid else None
        obs["post_state"] = {
            "kraken_present": kr is not None,
            "kraken_tapped": kr_tapped,
            "fish_tokens": len(fish),
            "tapped_creature_oid": toid,
            "tapped_creature_name": obj_lname(pstate, toid) if toid else None,
            "tapped_creature_tapped": turtle_tapped,
            "p0_bf": bf_names(pstate, 0)[:10],
            "p1_bf": bf_names(pstate, 1)[:10],
        }
        say(f"[{tag}] post: kraken_tapped={kr_tapped} fish={len(fish)} "
            f"turtle_tapped={turtle_tapped}")
        wire("post_counts", obs["post_state"])
        if ctx.get("resolution_choice_seen") and toid:
            obs["assert"]["A6_resolution_effect"] = (
                "passed" if (kr_tapped and len(fish) >= 1 and turtle_tapped)
                else "failed")
        else:
            obs["notes"].append(
                "A6 not-run: resolution choice not completed "
                f"(seen={ctx.get('resolution_choice_seen')} picked={toid})")

    ctx["window_done"] = True
    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    check_data_level()
    obs = None
    for attempt in range(3):
        obs = await run_attempt(attempt)
        a2 = obs["assert"].get("A2_fixture_ok")
        if a2 == "passed":
            break
        say(f"[attempt {attempt}] incomplete (A2={a2}); retrying"
            if attempt < 2 else f"[attempt {attempt}] incomplete; giving up")

    ass = dict(obs["assert"])
    ass["A7_data_shape"] = "passed"  # check_data_level() asserts before any run
    notes = list(obs["notes"])
    notes.append(f"spurious stack-time prompts observed: {len(obs.get('spurious_prompts', []))}")
    for r in obs.get("spurious_prompts", [])[:3]:
        notes.append(
            f"spurious prompt seat={r['seat']} {r['rtype']}/{r['stype']}: "
            f"{[(c['ref_name'], c['ref_controller']) for c in r['candidates'][:6]]}")
    notes.append(f"opponent asked at resolution: {obs['assert'].get('A5_opponent_asked_at_resolution')}")
    notes.append(f"post state: {obs.get('post_state')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; priority-gated passes; "
                 "sleep(0) yield before leg evaluation. Trigger window opens "
                 "when the Kraken BeginCombat trigger is observed on the "
                 "stack; a target-shaped prompt while it is on the stack "
                 "(pre-resolution) is classified spurious; prompts after it "
                 "leaves the stack are legitimate resolution choices.")

    a2 = ass.get("A2_fixture_ok")
    a4 = ass.get("A4_no_spurious_stacktime_target")
    a5 = ass.get("A5_opponent_asked_at_resolution")
    if a2 == "passed" and a4 == "failed":
        verdict = "reproduced"
    elif a2 == "passed" and a4 == "passed" and a5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = fixture complete and a "
                 "target-selection prompt was observed while the Kraken "
                 "trigger was on the stack pre-resolution. not-reproduced = "
                 "fixture complete, no such prompt, and the opponent was "
                 "asked at resolution.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9848",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("runs/backfill-20261013-114139 (v0.108.0 server on "
                           "127.0.0.1:9374 started by this run; "
                           "evidence under evidence/9848/" + RUN_ID + ")"),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Island", 56], ["Reservoir Kraken", 4]],
            "P1": [["Island", 52], ["Aegis Turtle", 8]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; A6 %s; A7 %s; "
                   "spurious_prompts=%d post=%s"
                   % (ass.get("A1_setup_ok"), a2,
                      ass.get("A3_pre_trigger_export"), a4, a5,
                      ass.get("A6_resolution_effect"), ass.get("A7_data_shape"),
                      len(obs.get("spurious_prompts", [])),
                      obs.get("post_state"))),
        "scope": ("Reservoir Kraken BeginCombat intervening-if trigger: "
                  "whether a target-selection prompt is raised at stack time; "
                  "two-player game; native human seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The spurious prompt is answered by the driver (first candidate) "
            "to let the game proceed, per the issue's note that the end "
            "result is correct once answered; the full prompt shape is "
            "preserved in the wire log before answering.",
            "Trigger-on-stack detection falls back to 'any stack entry during "
            "BeginCombat with an untapped Kraken out' -- this fixture has no "
            "other triggers; the matched stack entry is wire-logged.",
        ],
        "setup_line": ("P0 56x Island + 4x Reservoir Kraken ({2}{U}{U} 6/6 "
                       "trample ward {2}); P1 52x Island + 8x Aegis Turtle "
                       "({U} 0/5)"),
        "contract_line": ("Cast Reservoir Kraken, reach a BeginCombat step "
                          "with it untapped. Expected (correct): no "
                          "target-selection prompt at stack time; at "
                          "resolution the opponent is asked whether to tap "
                          "an untapped creature they control. Bug: a "
                          "TriggerTargetSelection prompt is raised at stack "
                          "time."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9848.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read()) + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9848 - Reservoir Kraken: spurious stack-time target selection",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_fixture_ok": "A2 Kraken untapped on P0 BF; P1 has untapped creature",
        "A3_pre_trigger_export": "A3 pre-trigger state captured",
        "A4_no_spurious_stacktime_target": "A4 [bug] NO target prompt while trigger on stack",
        "A5_opponent_asked_at_resolution": "A5 opponent (P1) asked the may-tap at resolution",
        "A6_resolution_effect": "A6 turtle+Kraken tapped; Fish token created",
        "A7_data_shape": "A7 card-data trigger lower matches reported shape",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:15]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9848/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9848.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9848 instance is alive "
                             f"(pid {old}); refusing")
        except (ProcessLookupError, ValueError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        if os.path.exists(pidfile):
            os.remove(pidfile)


if __name__ == "__main__":
    asyncio.run(main())
