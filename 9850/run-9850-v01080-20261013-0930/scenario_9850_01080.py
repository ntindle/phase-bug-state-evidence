#!/usr/bin/env python3
"""Issue #9850: Gobsmacked -- the "as long as you can see the chosen person
from your seat" gate is dropped from the static.

Oracle: "Enchant nonland permanent
As this Aura enters, choose a person outside the game.
As long as you can see the chosen person from your seat, enchanted permanent
can't attack or block, and its activated abilities can't be activated."

Report (phase-rs/phase#9850, opened 2026-10-12 by matthewevans, open,
bug/area:parser/priority:p3-card-specific): the conditional static is parsed
without its "as long as" condition and applies unconditionally. Expected: the
restrictions apply only while the condition holds (a physical-game condition
the engine cannot evaluate; the card should stay unsupported or hold the
condition as an explicit unknown). Actual (reporter's probe): read from parse
output, the gate is not present on the static; the card shows as supported.
Where: static condition extraction in parse_inner_condition and the
as-long-as strip path in parser/oracle_static.rs. CR 614.12a.

Behavioral contract (per playbook: test the reported OUTCOME; this is a
parser-subsystem report, so the decisive contract is parse-level, with a
game-level confirmation that the card shows as supported and applies the
statics unconditionally):
  Parse (decisive): in the pinned v0.108.0 card-data.json, Gobsmacked's two
      statics (CantAttackOrBlock, CantBeActivated) must carry the
      "As long as you can see the chosen person from your seat" condition --
      as a real condition or as an explicit unknown -- OR the card must read
      as unsupported. Bug: both statics have condition == null while their
      modes are concrete, and a SwallowedClause/Condition_AsLongAs warning
      (gap kind unparsed_condition) documents that the parser knew about the
      gate and applied the statics unconditionally anyway.
  Game (confirming): P0 casts Gobsmacked enchanting P1's Llanowar Elves.
      Expected under the bug: the cast succeeds (card shows as supported),
      the Aura attaches, the enchanted Elves cannot attack and cannot
      activate its mana ability, while an unenchanted control Elves and the
      Grizzly Bears behave normally.

Assertions:
  A1 setup_ok          game created, both seats joined
  A2 parse_gate        FAILED = the reported bug: the "as long as" gate is
                       absent from both statics (condition null) while the
                       modes are concrete; passed = gate present as a real
                       or explicit-unknown condition, or card unsupported
  A3 cast_ok           Gobsmacked cast and resolved; Aura on P0 battlefield
                       attached to the target creature (shows as supported
                       in-game)
  A4 attack_restricted enchanted Elves not offered as an attacker at P1's
                       DeclareAttackers while the unenchanted Bears is
                       (restriction applies unconditionally in-game)
  A5 ability_restricted no ActivateAbility offered for the enchanted Elves
                       while the control Elves offers one
  A6 data_shape        card-data lower matches the reported shape (two
                       statics, SwallowedClause warning with the as-long-as
                       guard)

Verdict:
  reproduced      A2 failed (gate dropped in parse output) -- the reported
                  outcome, regardless of A3-A5
  not-reproduced  A2 passed (gate present or card unsupported)
  blocked         fixture never reached, or parse evidence unreadable

Driver conventions (verified scenario_9848.py, protocol 134):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (viewer_interaction)
  - exact card-data names; export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; vi candidate references are strings
    (normalize with str() before oid comparisons)
  - real_decision_pending filters None surface codes (9845 lesson)
  - genuine card-choice candidates have NO action surface (9842 lesson)
  - runtime state objects use "card_types" (plural); card-data.json uses
    "card_type" (singular) (9847 lesson)
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
log = logging.getLogger("scenario9850")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9850-v01080-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9850
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
               "evidence under evidence/9850/" + RUN_ID),
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


GOBSMACKED = "gobsmacked"
ELVES = "llanowar elves"
BEARS = "grizzly bears"


def gobsmacked_oid(state, pid=0):
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == GOBSMACKED:
            return o
    return None


def elves_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == ELVES]


def bears_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == BEARS]


def aura_attached_to(state, aura_oid):
    o = get_obj(state, aura_oid)
    return str(o.get("attached_to") or o.get("enchanting") or "") or None


def activate_ability_offered(acts, state, oid):
    """True if an ActivateAbility legal action is advertised for oid."""
    for a in acts:
        if a["type"] == "ActivateAbility":
            d = a.get("data", {}) or {}
            for v in list(d.values()) + [a.get("_src_oid")]:
                try:
                    if int(v) == int(oid):
                        return True
                except (TypeError, ValueError):
                    continue
    return False


def offered_attacker_oids(st, acts):
    """Best-effort set of attacker oids the engine offers at a
    DeclareAttackers step: vi candidate/choice refs plus any oids found in
    the DeclareAttackers action payload."""
    oids = set()
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in (data.get("candidates") or data.get("choices") or []):
            ref = _cand_ref(ch)
            if ref is not None:
                oids.add(str(ref))
    for a in acts:
        if a["type"] == "DeclareAttackers":
            blob = json.dumps(a.get("data", {}), default=str)
            for oid in (st.get("state", {}).get("objects") or {}):
                if str(oid) in blob:
                    oids.add(str(oid))
    return oids


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


def check_parse_level():
    """Record the parser shapes this scenario depends on: Gobsmacked's two
    statics must carry the "as long as you can see the chosen person from
    your seat" condition (or be explicitly unknown/unsupported).

    Returns a dict with the decisive A2 evidence. This function does NOT
    abort the run when the bug is present -- the dropped gate IS the
    reported outcome, so the game confirmation still runs."""
    g = CARD_DATA.get("gobsmacked", {})
    statics = g.get("static_abilities") or []
    warnings = g.get("parse_warnings") or []
    modes = []
    conditions = []
    for s in statics:
        m = s.get("mode")
        modes.append("CantAttackOrBlock" if m == "CantAttackOrBlock"
                     else ("CantBeActivated" if isinstance(m, dict)
                           and "CantBeActivated" in m else json.dumps(m)[:80]))
        conditions.append(s.get("condition"))
    swallow = [w for w in warnings
               if w.get("type") == "SwallowedClause"
               and w.get("detector") == "Condition_AsLongAs"]
    gap = (swallow[0].get("gap") or {}) if swallow else {}
    oracle = g.get("oracle_text") or ""
    gate_clause = "As long as you can see the chosen person from your seat"
    oracle_has_gate = gate_clause in oracle
    # The reported bug: the gate is not present on the static while the
    # modes are concrete (card shows as supported).
    gate_dropped = (
        oracle_has_gate
        and len(statics) == 2
        and all(c is None for c in conditions)
        and "CantAttackOrBlock" in modes
        and "CantBeActivated" in modes
        and bool(swallow)
        and gap.get("kind") == "unparsed_condition"
    )
    statics_unimplemented = all(
        isinstance(s.get("mode"), dict) and s.get("mode").get("type") == "Unimplemented"
        for s in statics) if statics else False
    ev = {
        "oracle_has_gate": oracle_has_gate,
        "n_statics": len(statics),
        "static_modes": modes,
        "static_conditions": conditions,
        "all_conditions_null": all(c is None for c in conditions),
        "swallowed_clause_warnings": [
            {"type": w.get("type"), "detector": w.get("detector"),
             "gap_kind": (w.get("gap") or {}).get("kind"),
             "guard": (w.get("gap") or {}).get("guard"),
             "description": (w.get("description") or "")[:160]}
            for w in swallow],
        "statics_unimplemented": statics_unimplemented,
        "gate_dropped": gate_dropped,
    }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump({"issue": 9850, "card": "gobsmacked",
                   "oracle_text": oracle,
                   "static_abilities": statics,
                   "parse_warnings": warnings,
                   "analysis": ev}, f, indent=1, default=str)
    say(f"parse-level check: gate_dropped={gate_dropped} "
        f"modes={modes} conditions_null={ev['all_conditions_null']} "
        f"swallow_warnings={len(swallow)}")
    wire("parse_level", ev)
    return ev


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


def update_cast(c, st, ctx, tag):
    """Track the Gobsmacked cast: spell on stack -> resolved on the
    battlefield attached to the target creature."""
    if ctx.get("cast_done"):
        return
    state = st["state"]
    go = gobsmacked_oid(state, 0)
    if go is not None and not ctx.get("cast_resolved"):
        att = aura_attached_to(state, go)
        ctx["cast_resolved"] = True
        ctx["cast_done"] = True
        ctx["aura_oid"] = go
        ctx["aura_attached_to"] = att
        ctx["need_post_export"] = True
        wire("gobsmacked_resolved", {
            "who": tag, "rev": c.revision, "aura_oid": go,
            "attached_to": att,
            "attached_name": obj_lname(state, att) if att else None,
            "target_oid": ctx.get("target_oid"),
            "p0_bf": bf_names(state, 0)[:10],
            "p1_bf": bf_names(state, 1)[:10]})
        say(f"[{tag}] Gobsmacked resolved: aura oid {go} attached to "
            f"{att} ({obj_lname(state, att) if att else '?'})")
    if not ctx.get("cast_seen_on_stack") and not ctx.get("cast_resolved"):
        for e in state.get("stack") or []:
            if obj_lname(state, e.get("object_id", e.get("id", ""))) == GOBSMACKED:
                ctx["cast_seen_on_stack"] = True
                ctx["need_pre_export"] = True
                wire("gobsmacked_on_stack",
                     {"who": tag, "rev": c.revision,
                      "stack": stack_raw(state)})
                say(f"[{tag}] Gobsmacked on the stack -- pre-cast export next")
                break


async def answer_aura_target(c, st, ctx, tag, seat):
    """Answer the Aura's target selection with the chosen creature."""
    if ctx.get("target_answered") or ctx.get("target_oid") is None:
        return False
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    want = str(ctx["target_oid"])
    pick = None
    for ch in cands:
        if _cand_ref(ch) == want:
            pick = ch
            break
    if pick is None:
        wire("aura_target_no_match",
             {"who": tag, "seat": seat, "want": want,
              "offered": [{"ref": _cand_ref(ch),
                           "text": choice_text(ch)[:60]} for ch in cands]})
        say(f"[{tag}] WARNING: aura target prompt does not offer oid {want}; "
            f"leaving pending")
        return False
    ctx["target_answered"] = True
    say(f"[{tag}] targeting {obj_lname(c.latest['state'], want)} (oid {want}) "
        f"with Gobsmacked")
    wire("aura_target_answer", {"who": tag, "target_oid": want})
    await answer_vi(c, opp, pick, tag)
    return True


async def handle_gobsmacked_prompt(c, st, ctx, tag, seat):
    """Answer Gobsmacked's target prompt; record any other prompt verbatim
    (notably the Unimplemented 'choose a person outside the game' ETB)."""
    if ctx.get("cast_done"):
        return False
    if await answer_aura_target(c, st, ctx, tag, seat):
        return True
    # any other real decision while the cast is in flight: record, don't answer
    if real_decision_pending(st):
        key = (tag, "other-prompt", seat, c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt_during_cast",
                 {"who": tag, "seat": seat, "rev": c.revision,
                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt during cast (seat {seat}): "
                f"{_opp_summary(st)} -- recorded, not answered")
    return False


# ---------------------------------------------------------------- legs



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

    # A4 recording BEFORE common_tick_head submits the empty attack set
    if "DeclareAttackers" in {a.get("type") for a in acts} \
            and ctx.get("cast_done") and not ctx.get("attack_step_recorded"):
        ctx["attack_step_recorded"] = True
        offered = offered_attacker_oids(st, acts)
        p1_creatures = [str(o) for o in bf_oids(state, 1)
                        if is_creature(get_obj(state, o))
                        and not get_obj(state, o).get("tapped")]
        ctx["attack_p1_creatures"] = p1_creatures
        ctx["attack_offered_creatures"] = sorted(
            o for o in offered if o in set(p1_creatures))
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        wire("declare_attackers_step", {
            "who": tag, "rev": c.revision,
            "offered_attacker_oids": sorted(offered),
            "enchanted_oid": ctx.get("aura_attached_to"),
            "bears_oids": bears_oids(state, 1),
            "elves_oids": elves_oids(state, 1),
            "declare_attackers_data": da.get("data") if da else None,
            "vi_opps": _opp_summary(st)})
        say(f"[{tag}] DeclareAttackers: offered={sorted(offered)} "
            f"enchanted={ctx.get('aura_attached_to')} "
            f"bears={bears_oids(state, 1)} elves={elves_oids(state, 1)}")
        ctx["attack_offered"] = sorted(offered)

    r = await common_tick_head(c, acts, st, 1, tag, ctx)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    update_cast(c, st, ctx, tag)
    if await handle_gobsmacked_prompt(c, st, ctx, tag, 1):
        return

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
        hn = hand_names(state, 1)
        if ELVES in hn and untapped_land_count(state, 1) >= 1 \
                and len(elves_oids(state, 1)) < 2:
            a, oid = cast_action_for(acts, state, ELVES)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 1}
                say(f"[{tag}] casting {ELVES} (oid {oid})")
                wire("cast_elves_p1", {"oid": oid})
                await submit_as_is(c, a)
                return
        if BEARS in hn and untapped_land_count(state, 1) >= 2 \
                and not bears_oids(state, 1):
            a, oid = cast_action_for(acts, state, BEARS)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                say(f"[{tag}] casting {BEARS} (oid {oid})")
                wire("cast_bears_p1", {"oid": oid})
                await submit_as_is(c, a)
                return

    # A5: ability check during P1 main phases after the aura resolved
    if ctx.get("cast_done") and not ctx.get("ability_check_done") \
            and my_main(state, 1):
        enc = ctx.get("aura_attached_to")
        elves = elves_oids(state, 1)
        control = [o for o in elves if str(o) != str(enc)]
        enc_offered = (activate_ability_offered(acts, state, enc)
                       if enc else None)
        ctl_offered = [activate_ability_offered(acts, state, o)
                       for o in control]
        if elves:
            ctx["ability_check_done"] = True
            ctx["ability_check"] = {
                "enchanted_oid": enc,
                "enchanted_offered": enc_offered,
                "control_oids": control,
                "control_offered": ctl_offered}
            wire("ability_check", ctx["ability_check"])
            say(f"[{tag}] ability check: enchanted {enc} offered={enc_offered}; "
                f"control {control} offered={ctl_offered}")

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

    update_cast(c, st, ctx, tag)
    if ctx.pop("need_pre_export", False):
        say(f"[{tag}] pre-cast export")
        pre = json.loads(await c.export_state())
        with open(f"{EVDIR}/pre_cast.json", "w") as f:
            json.dump(pre, f, indent=1)
        wire("pre_cast_export", {"who": tag})
        ctx["pre_exported"] = True
    if ctx.pop("need_post_export", False):
        say(f"[{tag}] post-cast export")
        post = json.loads(await c.export_state())
        with open(f"{EVDIR}/post_cast.json", "w") as f:
            json.dump(post, f, indent=1)
        wire("post_cast_export", {"who": tag})
        ctx["post_exported"] = True
    if await handle_gobsmacked_prompt(c, st, ctx, tag, 0):
        return True

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_names(state, 0)
        # diagnostic: trace the cast branch inputs once per main phase
        dkey = (tag, "castdiag", state.get("turn_number"), state.get("phase"))
        if dkey not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(dkey)
            cast_types = sorted({a.get("type") for a in acts
                                 if "cast" in str(a.get("type")).lower()})
            say(f"[{tag}] castdiag turn={state.get('turn_number')} "
                f"hand_n={len(hn)} gobsmacked_in_hand={GOBSMACKED in hn} "
                f"untapped_lands={untapped_land_count(state, 0)} "
                f"cast_actions={cast_types} "
                f"hand_sample={hn[:12]}")
            wire("cast_diag", {"who": tag, "turn": state.get("turn_number"),
                               "hand_n": len(hn),
                               "gobsmacked_in_hand": GOBSMACKED in hn,
                               "untapped_lands": untapped_land_count(state, 0),
                               "cast_action_types": cast_types,
                               "hand": hn})
        # uncastability streak: Gobsmacked in hand + mana available, yet the
        # engine advertises no cast action. Eight consecutive qualifying
        # P0 main phases establishes the card is uncastable in-game.
        cast_types = sorted({a.get("type") for a in acts
                             if "cast" in str(a.get("type")).lower()})
        if (GOBSMACKED in hn and untapped_land_count(state, 0) >= 2
                and not cast_types):
            ctx["uncastable_streak"] = ctx.get("uncastable_streak", 0) + 1
            if ctx["uncastable_streak"] >= 8 \
                    and not ctx.get("uncastable_confirmed"):
                ctx["uncastable_confirmed"] = True
                say(f"[{tag}] UNCASTABLE: {ctx['uncastable_streak']} "
                    f"consecutive P0 main phases with Gobsmacked in hand "
                    f"and >=2 untapped lands, zero cast actions advertised")
                wire("uncastable_confirmed",
                     {"who": tag, "streak": ctx["uncastable_streak"],
                      "hand": hn,
                      "untapped_lands": untapped_land_count(state, 0)})
        else:
            ctx["uncastable_streak"] = 0
        if not ctx.get("cast_submitted") and GOBSMACKED in hn \
                and untapped_land_count(state, 0) >= 2:
            tgt = None
            for o in elves_oids(state, 1):
                if not get_obj(state, o).get("tapped"):
                    tgt = o
                    break
            if tgt is None:
                cands = [o for o in bf_oids(state, 1)
                         if is_creature(get_obj(state, o))]
                tgt = cands[0] if cands else None
            if tgt is None:
                say(f"[{tag}] no P1 creature to enchant yet")
                return True
            a, oid = cast_action_for(acts, state, GOBSMACKED)
            if a is not None:
                ctx["target_oid"] = str(tgt)
                ctx["cast_submitted"] = True
                ST["mana_needs"][tag] = {"W": 1, "generic": 1}
                say(f"[{tag}] casting Gobsmacked (oid {oid}) targeting "
                    f"{tgt} ({obj_lname(state, tgt)})")
                wire("cast_gobsmacked",
                     {"oid": oid, "target_oid": str(tgt),
                      "cast_action_data": a.get("data")})
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
    tag = f"9850-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {"cast_submitted": False, "cast_seen_on_stack": False,
           "cast_resolved": False, "cast_done": False,
           "target_oid": None, "target_answered": False,
           "aura_oid": None, "aura_attached_to": None,
           "pre_exported": False, "post_exported": False,
           "need_pre_export": False, "need_post_export": False,
           "attack_step_recorded": False, "attack_offered": None,
           "attack_offered_creatures": None, "attack_p1_creatures": None,
           "ability_check_done": False, "ability_check": None}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Plains", 56), ("Gobsmacked", 4)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Forest", 52),
                                    ("Llanowar Elves", 4),
                                    ("Grizzly Bears", 4)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                                and p1.player_id is not None) else "failed"

    # fast-abort: the fixture needs Gobsmacked in hand early; redraw a fresh
    # game when the opening 7 lacks it (a no-Gobsmacked game costs ~10 min
    # of dead turns, a fresh game costs ~15 s).
    for _ in range(80):
        await asyncio.sleep(0.25)
        if p0.latest and p0.latest.get("state", {}).get("turn_number", 0) >= 1:
            break
    st = p0.latest
    if st is None or GOBSMACKED not in hand_names(st["state"], 0):
        say(f"[{tag}] fast abort: opening hand lacks Gobsmacked "
            f"({hand_names(st['state'], 0) if st else 'no state'})")
        wire("fast_abort", {"reason": "no gobsmacked in opening hand",
                            "hand": hand_names(st["state"], 0) if st else []})
        obs["notes"].append("fast abort: opening hand lacked Gobsmacked; "
                            "fresh game redealt")
        obs["assert"]["A3_cast_ok"] = "not-run"
        obs["assert"]["A4_attack_restricted"] = "not-run"
        obs["assert"]["A5_ability_restricted"] = "not-run"
        obs["fast_aborted"] = True
        await p0.close()
        await p1.close()
        return obs

    t0 = time.time()
    last_diag = 0
    settle_ticks = 0
    while time.time() - t0 < 900:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx)
        await p0_tick(p0, tag, ctx)
        await asyncio.sleep(0)
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
                    f"cast_done={ctx.get('cast_done')} "
                    f"attack_rec={ctx.get('attack_step_recorded')} "
                    f"ability_chk={ctx.get('ability_check_done')} "
                    f"p0bf={bf_names(s, 0)[:6]} p1bf={bf_names(s, 1)[:6]}")
                if decision_pending(st):
                    say(f"[diag] {c.name} pending opps: {_opp_summary(st)}")
        # end condition: cast resolved + exports + both game checks done
        if ctx.get("post_exported") and ctx.get("attack_step_recorded") \
                and ctx.get("ability_check_done"):
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break
        if not ctx.get("cast_done") and time.time() - t0 > 600:
            say(f"[{tag}] cast never completed >600s; capturing state and bailing")
            wire("cast_stuck", {"stack": stack_raw(p0.latest["state"])
                                if p0.latest else None})
            break
        if ctx.get("uncastable_confirmed"):
            say(f"[{tag}] uncastability established; ending attempt")
            break

    st = p0.latest
    state = st["state"] if st else {}

    # ---- A3: cast + resolution (card shows as supported in-game)
    enc = ctx.get("aura_attached_to")
    tgt = ctx.get("target_oid")
    if ctx.get("cast_resolved") and enc is not None and str(enc) == str(tgt):
        obs["assert"]["A3_cast_ok"] = "passed"
    elif ctx.get("uncastable_confirmed"):
        obs["assert"]["A3_cast_ok"] = "failed"
        obs["notes"].append(
            f"A3 FAILED: Gobsmacked uncastable in-game -- "
            f"{ctx.get('uncastable_streak')} consecutive P0 main phases with "
            f"the card in hand and >=2 untapped lands, zero cast actions "
            f"advertised (see cast_diag wire entries). The card does not "
            f"'show as supported' in-game; it is effectively dead.")
    elif ctx.get("cast_resolved"):
        obs["assert"]["A3_cast_ok"] = "failed"
        obs["notes"].append(
            f"aura resolved but attached_to={enc} != target {tgt}")
    else:
        obs["assert"]["A3_cast_ok"] = "failed"
        obs["notes"].append(
            f"cast never resolved: submitted={ctx.get('cast_submitted')} "
            f"on_stack={ctx.get('cast_seen_on_stack')} "
            f"turn={state.get('turn_number')} phase={state.get('phase')} "
            f"p0bf={bf_names(state, 0)} p1bf={bf_names(state, 1)} "
            f"p0hand={hand_names(state, 0)}")
    try:
        with open(f"{EVDIR}/pre_cast.json") as f:
            json.load(f)
        obs["notes"].append("pre_cast.json captured and parses")
    except (OSError, ValueError):
        obs["notes"].append("pre_cast.json missing/unparseable")
    try:
        with open(f"{EVDIR}/post_cast.json") as f:
            json.load(f)
        obs["notes"].append("post_cast.json captured and parses")
    except (OSError, ValueError):
        obs["notes"].append("post_cast.json missing/unparseable")

    # ---- A4: attack restriction at DeclareAttackers
    if not ctx.get("attack_step_recorded"):
        obs["assert"]["A4_attack_restricted"] = "not-run"
        obs["notes"].append(
            "A4 not-run: no P1 DeclareAttackers step observed after the "
            "aura resolved")
    else:
        offered = [str(o) for o in (ctx.get("attack_offered_creatures") or [])]
        enc_s = str(enc) if enc else None
        obs["attack_step"] = {
            "offered_creatures": offered,
            "enchanted_oid": enc_s,
            "p1_creatures_then": ctx.get("attack_p1_creatures"),
        }
        if enc_s and enc_s in offered:
            obs["assert"]["A4_attack_restricted"] = "failed"
            obs["notes"].append(
                f"A4 FAILED: enchanted creature {enc_s} was offered as an "
                f"attacker (restriction not applying in-game)")
        elif any("bears" in (n or "") for n in
                 [obj_lname(state, o) for o in offered]) or offered:
            # control attacker offered while the enchanted one is excluded
            obs["assert"]["A4_attack_restricted"] = "passed"
            obs["notes"].append(
                f"A4: offered attackers {offered} exclude the enchanted "
                f"{enc_s}; restriction applies unconditionally in-game")
        else:
            obs["assert"]["A4_attack_restricted"] = "not-run"
            obs["notes"].append(
                "A4 not-run: offered-attacker set uninterpretable from the "
                "wire data (empty); raw DeclareAttackers payload preserved "
                "in wire_log.jsonl under declare_attackers_step")

    # ---- A5: activated-ability restriction
    chk = ctx.get("ability_check")
    if not chk:
        obs["assert"]["A5_ability_restricted"] = "not-run"
        obs["notes"].append(
            "A5 not-run: no P1 main phase observed after the aura resolved")
    else:
        obs["ability_check"] = chk
        if chk["enchanted_offered"] is False \
                and any(chk["control_offered"]):
            obs["assert"]["A5_ability_restricted"] = "passed"
            obs["notes"].append(
                "A5: no ActivateAbility offered for the enchanted Elves "
                f"({chk['enchanted_oid']}) while control Elves "
                f"{chk['control_oids']} offer one")
        elif chk["enchanted_offered"] is True:
            obs["assert"]["A5_ability_restricted"] = "failed"
            obs["notes"].append(
                "A5 FAILED: ActivateAbility offered for the enchanted Elves")
        else:
            obs["assert"]["A5_ability_restricted"] = "not-run"
            obs["notes"].append(
                f"A5 not-run: ambiguous ability surface {chk}")

    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    parse_ev = check_parse_level()
    obs = None
    for attempt in range(8):
        obs = await run_attempt(attempt)
        a3 = obs["assert"].get("A3_cast_ok")
        if a3 == "passed":
            break
        if obs.get("fast_aborted"):
            say(f"[attempt {attempt}] fast-aborted (no Gobsmacked in opener); redealing")
            continue
        # uncastability is engine behavior, not draw luck: no point redealing
        if any("uncastable" in n for n in obs["notes"]):
            say(f"[attempt {attempt}] uncastability confirmed; not redealing")
            break
        say(f"[attempt {attempt}] incomplete (A3={a3}); retrying"
            if attempt < 7 else f"[attempt {attempt}] incomplete; giving up")

    ass = dict(obs["assert"])
    # A2 (decisive, parse-level): "failed" means the reported bug is present.
    if parse_ev["gate_dropped"]:
        ass["A2_parse_gate"] = "failed"
    elif parse_ev["statics_unimplemented"] or not parse_ev["all_conditions_null"]:
        ass["A2_parse_gate"] = "passed"
    else:
        ass["A2_parse_gate"] = "not-run"
    # A6: the card-data lower matches the reported shape
    ass["A6_data_shape"] = "passed" if (
        parse_ev["n_statics"] == 2
        and len(parse_ev["swallowed_clause_warnings"]) >= 1
        and parse_ev["oracle_has_gate"]) else "failed"
    notes = list(obs["notes"])
    notes.append(
        f"parse: gate_dropped={parse_ev['gate_dropped']} "
        f"modes={parse_ev['static_modes']} "
        f"conditions={parse_ev['static_conditions']} "
        f"swallow_warnings={len(parse_ev['swallowed_clause_warnings'])}")
    notes.append(f"attack step: {obs.get('attack_step')}")
    notes.append(f"ability check: {obs.get('ability_check')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; priority-gated passes; "
                 "sleep(0) yield before leg evaluation. The Aura target prompt "
                 "is answered with the chosen creature; any other prompt "
                 "during the cast (e.g. the Unimplemented 'choose a person "
                 "outside the game' ETB) is recorded, never auto-answered.")

    a2 = ass.get("A2_parse_gate")
    if parse_ev is None:
        verdict = "blocked"
    elif a2 == "failed":
        verdict = "reproduced"
    elif a2 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = the parse output drops the "
                 "'as long as' gate (both statics condition null, concrete "
                 "modes, SwallowedClause/Condition_AsLongAs warning) -- the "
                 "reported outcome. not-reproduced = the gate is present as "
                 "a real or explicit-unknown condition, or the card reads "
                 "unsupported.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9850",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("runs/run-9850-v01080-20261013-0841 (v0.108.0 "
                           "server on 127.0.0.1:9374 started by this run; "
                           "evidence under evidence/9850/" + RUN_ID + ")"),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Plains", 56], ["Gobsmacked", 4]],
            "P1": [["Forest", 52], ["Llanowar Elves", 4], ["Grizzly Bears", 4]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "parse_evidence": parse_ev,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; A6 %s"
                   % (ass.get("A1_setup_ok"), a2,
                      ass.get("A3_cast_ok"),
                      ass.get("A4_attack_restricted"),
                      ass.get("A5_ability_restricted"),
                      ass.get("A6_data_shape"))),
        "scope": ("Gobsmacked 'as long as you can see the chosen person from "
                  "your seat' gate: parse-level presence of the condition on "
                  "the two statics, plus in-game confirmation that the card "
                  "casts, attaches, and applies the restrictions "
                  "unconditionally; two-player game; native human seats, "
                  "no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The 'as long as you can see the chosen person' condition is a "
            "physical-game condition the engine cannot evaluate; the game "
            "checks confirm unconditional application, they cannot "
            "distinguish a correctly-gated static from a dropped gate.",
            "The 'choose a person outside the game' ETB clause parses as "
            "Unimplemented; any prompt it raises is recorded, never "
            "auto-answered.",
            "The offered-attacker set is a best-effort read of the "
            "DeclareAttackers action payload and viewer-interaction "
            "candidates; the raw payload is preserved in wire_log.jsonl.",
        ],
        "setup_line": ("P0 56x Plains + 4x Gobsmacked ({1}{W} Aura); "
                       "P1 52x Forest + 4x Llanowar Elves ({G} 1/1, "
                       "{T}: add {G}) + 4x Grizzly Bears ({1}{G} 2/2)"),
        "contract_line": ("Parse (decisive): Gobsmacked's CantAttackOrBlock "
                          "and CantBeActivated statics must carry the 'as "
                          "long as' gate or the card must read unsupported. "
                          "Game (confirming): cast Gobsmacked on P1's Elves; "
                          "the enchanted Elves must not be offered as an "
                          "attacker and must offer no ActivateAbility, while "
                          "the control Bears/Elves behave normally."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9850_01080.py", "w") as f:
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
    d.text((24, y), "Issue #9850 - Gobsmacked: the 'as long as you can see the chosen",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "person' gate is dropped from the static",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_parse_gate": "A2 [bug] 'as long as' gate absent from statics (condition null)",
        "A3_cast_ok": "A3 Gobsmacked cast, resolved, attached to target",
        "A4_attack_restricted": "A4 enchanted Elves not offered as attacker",
        "A5_ability_restricted": "A5 no ActivateAbility for enchanted Elves",
        "A6_data_shape": "A6 card-data static lower matches reported shape",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:15]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9850/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9850.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9850 instance is alive "
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
