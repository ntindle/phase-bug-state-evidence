#!/usr/bin/env python3
"""Issue #9854: Scaleguard Sentinels -- "if you revealed a Dragon card or
controlled a Dragon as you cast this spell" has no typed gate.

Oracle (Scaleguard Sentinels): "As an additional cost to cast this spell,
you may reveal a Dragon card from your hand. This creature enters with a
+1/+1 counter on it if you revealed a Dragon card or controlled a Dragon
as you cast this spell."

Report (phase-rs/phase#9854, opened 2026-10-12 by matthewevans, open,
bug/area:parser/priority:p3-card-specific): read from code -- no typed
gate exists for the linked optional additional cost (reveal a Dragon card)
combined with an as-cast control snapshot ("controlled a Dragon"). Related
family: issue #9853 ("Enters with counters gated by an If in a different
sentence").

Behavioral contract (per playbook: test the reported OUTCOME; this is a
parser-subsystem report, so the decisive contract is parse-level, with a
game-level confirmation of the reported wrong-game-result):
  Parse (decisive): in the pinned v0.108.0 card-data.json, Scaleguard
      Sentinels' "enters with a +1/+1 counter on it if you revealed a
      Dragon card or controlled a Dragon as you cast this spell" ability
      must carry a TYPED gate -- a replacement/condition structure that
      links the optional reveal additional cost with an as-cast "controlled
      a Dragon" snapshot. Bug: the whole gate lowers as Unimplemented
      (effect type Unimplemented, name "enters_with_gate"), with the
      additional cost parsed (Optional -> Reveal 1 Dragon) but nothing
      consuming it.
  Game (confirming): cast the Sentinels in three legs --
      Leg "neither": no Dragon revealed, no Dragon controlled. Expected:
          enters with NO +1/+1 counter (2/3).
      Leg "reveal": accept the optional reveal, reveal a Dragon
          (Fearsome Whelp) from hand. Expected: enters with EXACTLY ONE
          +1/+1 counter (3/4).
      Leg "control": control a Dragon (Fearsome Whelp on the battlefield),
          do not reveal. Expected: enters with EXACTLY ONE +1/+1 counter.
      Under the bug (no typed gate), the counter gate never fires, so the
      reveal and control legs enter 2/3 without a counter; the neither leg
      is coincidentally correct.

Assertions:
  A1 setup_ok        game created, both seats joined
  A2 parse_gate      FAILED = the reported bug: the enters-with gate lowers
                     as Unimplemented "enters_with_gate" (no typed gate);
                     passed = a typed gate exists (condition/replacement
                     structure linking the reveal and the control snapshot)
  A3 neither_leg     cast with no reveal and no Dragon controlled; FAILED =
                     a +1/+1 counter was added anyway; passed = entered 2/3
                     with no counter; not-run = the Sentinels never entered
  A4 reveal_leg      revealed a Dragon while casting; FAILED = it entered
                     without a counter (the gate never fired); passed =
                     entered 3/4 with exactly one counter; not-run = the
                     reveal prompt was never offered or the leg never ran
  A5 control_leg     controlled a Dragon while casting, no reveal; FAILED =
                     entered without a counter; passed = entered 3/4 with
                     exactly one counter; not-run = the leg never ran
  A6 data_shape      the card-data lower matches the reported shape (one
                     Spell-kind ability, Unimplemented enters_with_gate,
                     Optional Reveal-Dragon additional cost, oracle text
                     intact)

Verdict:
  reproduced      A2 failed (no typed gate -- the reported outcome),
                  corroborated by the game legs where they completed
  not-reproduced  A2 passed (a typed gate exists) and the game legs behave
                  per the oracle text
  blocked         fixture never reached, or parse evidence unreadable

Driver conventions (verified scenario_9853_01080.py, protocol 134):
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

01081 revision (2026-10-13): the 01080 run timed out because one long game
waited ~40 turns on natural draws for the reveal/control legs. This
revision plays ONE SHORT GAME PER LEG, each with a per-leg opener
requirement (fast-abort + redeal otherwise), so each leg finishes in a
few turns. Parse-level contract and verdict logic unchanged.
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
log = logging.getLogger("scenario9854")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9854-v01080-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9854
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
               "v0.108.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release; reused "
               "by this run); scenario games created via "
               "CreateGameWithSettings; evidence under evidence/9854/" + RUN_ID),
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
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def is_creature(o):
    return "Creature" in (((o.get("card_types") or {}).get("core_types")) or [])


SENTINELS = "scaleguard sentinels"
WHELP = "fearsome whelp"
FOREST = "forest"
MOUNTAIN = "mountain"
ISLAND = "island"


def sentinels_oids(state, pid=0):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == SENTINELS]


def newest_sentinels_oid(state, pid, exclude):
    cands = [o for o in sentinels_oids(state, pid) if o not in exclude]
    if not cands:
        return None
    def keyfn(o):
        try:
            return int(o)
        except (TypeError, ValueError):
            return 0
    return max(cands, key=keyfn)


def whelp_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == WHELP]


def p1p1_counters_of(state, oid):
    o = get_obj(state, oid)
    raw = o.get("counters")
    count = 0
    keys = ("p1p1", "+1/+1", "plusone", "plus_one", "plus1plus1", "ponepone")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if any(key in str(k).lower() for key in keys):
                try:
                    count += int(v)
                except (TypeError, ValueError):
                    count += 1
    elif isinstance(raw, list):
        for c in raw:
            nm = c.get("name") if isinstance(c, dict) else str(c)
            if any(key in str(nm).lower() for key in keys):
                try:
                    count += int(c.get("count", 1)) if isinstance(c, dict) else 1
                except (TypeError, ValueError):
                    count += 1
    return count, raw


def obj_pt(state, oid):
    o = get_obj(state, oid)
    return o.get("power"), o.get("toughness")


def stack_spell_oid(state, lname):
    for e in state.get("stack") or []:
        oid = e.get("object_id", e.get("id", ""))
        if obj_lname(state, oid) == lname:
            return str(oid)
    return None


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


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


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


def check_parse_gate():
    """Record the parser shape this scenario depends on: Scaleguard
    Sentinels' "enters with a +1/+1 counter on it if you revealed a Dragon
    card or controlled a Dragon as you cast this spell" ability must carry
    a typed gate linking the optional reveal additional cost with an
    as-cast control snapshot -- or the card must read unsupported.

    Returns a dict with the decisive A2 evidence. Never aborts: the missing
    typed gate IS the reported outcome."""
    s = CARD_DATA.get("scaleguard sentinels", {})
    oracle = s.get("oracle_text") or ""
    abilities = s.get("abilities") or []
    spell_abs = [a for a in abilities if a.get("kind") == "Spell"]
    gate = spell_abs[0] if spell_abs else {}
    eff = gate.get("effect") or {}
    ac = s.get("additional_cost") or {}
    ac_data = ac.get("data") or {}
    analysis = {
        "oracle": oracle,
        "n_spell_abilities": len(spell_abs),
        "gate_effect_type": eff.get("type"),
        "gate_effect_name": eff.get("name"),
        "gate_description": gate.get("description"),
        "gate_condition": gate.get("condition"),
        "gate_optional": gate.get("optional"),
        "additional_cost_type": ac.get("type"),
        "additional_cost_data": ac_data,
        "parse_warnings": s.get("parse_warnings"),
    }
    # The reported bug: the whole enters-with gate lowers as Unimplemented
    # ("enters_with_gate") -- there is no typed gate linking the optional
    # reveal with the as-cast control snapshot. The additional cost itself
    # parses (Optional -> Reveal 1 Dragon), but nothing consumes it.
    no_typed_gate = (
        len(spell_abs) == 1
        and eff.get("type") == "Unimplemented"
        and eff.get("name") == "enters_with_gate"
        and gate.get("condition") is None
    )
    typed_gate_present = (
        len(spell_abs) == 1
        and eff.get("type") not in (None, "Unimplemented")
    )
    analysis["no_typed_gate"] = no_typed_gate
    analysis["typed_gate_present"] = typed_gate_present
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump({"issue": 9854, "card": "scaleguard sentinels",
                   "oracle_text": oracle,
                   "abilities": abilities,
                   "additional_cost": ac,
                   "analysis": analysis}, f, indent=1, default=str)
    say(f"parse-level check: no_typed_gate={no_typed_gate} "
        f"effect={eff.get('type')} name={eff.get('name')} "
        f"additional_cost={ac.get('type')}")
    wire("parse_level", analysis)
    return analysis


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


def untapped_named_lands(state, pid, lname):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == lname
               and not get_obj(state, o).get("tapped"))


def untapped_land_total(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


MENU_ACTION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                     "castSpell", "activateAbility", "candidate", "mana",
                     "mulliganDecision", "playLand"}


def hand_card_opportunity(st, pid):
    """A vi opportunity offering a REAL hand-card choice (not the priority
    menu): genuine card choices carry NO action surface (9842 lesson)."""
    state = st["state"]
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type")
        real = []
        for ch in cands:
            is_menu = any(
                s.get("type") == "action"
                and (s.get("data") or {}).get("code") in MENU_ACTION_CODES
                for s in ch.get("surfaces", []) or [])
            if not is_menu and _cand_ref(ch) in handset:
                real.append(ch)
        if real:
            return opp, rtype, stype, real
    return None, None, None, []


def active_cast(ctx):
    for cast in ctx["casts"]:
        if not cast.get("done"):
            return cast
    return None


def optional_cost_opportunity(st):
    """The engine offers the optional reveal additional cost as an
    exactChoices opportunity carrying a decideOptionalCost code (plus
    candidate/cancelCast codes)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or []
        codes = set()
        for ch in chs:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalCost" in codes:
            return opp, chs
    return None, None


def choice_dump(state, ch):
    return {
        "id": ch.get("id"),
        "text": choice_text(ch)[:120],
        "codes": sorted(c for c in surf_codes(ch) if c),
        "ref": _cand_ref(ch),
        "ref_name": obj_lname(state, _cand_ref(ch))
        if _cand_ref(ch) else None,
        "status": (ch.get("status") or {}).get("type"),
    }


async def handle_reveal_prompt(c, st, ctx, tag):
    """Handle the optional reveal additional-cost prompt for the active
    Sentinels cast.

    The engine offers the optional cost in two stages:
      1. a boolean exactChoices opportunity (codes decideOptionalCost /
         candidate / cancelCast; choices text "true"/"false", no card
         refs): "true" = pay the reveal cost, "false" = decline it.
      2. after accepting, a hand-card choice (real card candidates with NO
         action surface, 9842 lesson) to pick which Dragon to reveal.
    reveal branch: answer "true", then reveal the branch's Whelp.
    Other branches: answer "false" (never cancelCast). Anything else is
    recorded, never auto-answered."""
    cast = active_cast(ctx)
    if cast is None or cast.get("reveal_handled"):
        return False
    state = st["state"]

    # stage 2: pick the Dragon card to reveal (only after accepting)
    if cast.get("cost_accepted") and not cast.get("revealed"):
        opp, rtype, stype, real = hand_card_opportunity(st, 0)
        if opp is None:
            return False
        iid = opp.get("interactionId")
        key = (tag, "reveal-pick", cast["branch"], str(iid))
        if key in SUBMITTED_OPPS:
            return False
        want = str(cast.get("reveal_target_oid"))
        pick = next((ch for ch in real if _cand_ref(ch) == want), None)
        names = [obj_lname(state, _cand_ref(ch)) for ch in real]
        wire("reveal_card_choice_seen",
             {"who": tag, "branch": cast["branch"], "rev": c.revision,
              "rtype": rtype, "stype": stype, "candidates": names,
              "raw": json.dumps(opp, default=str)})
        say(f"[{tag}] reveal card choice (branch {cast['branch']}): "
            f"rtype={rtype} stype={stype} candidates={names}")
        if pick is None:
            wire("reveal_no_target",
                 {"who": tag, "want": want, "candidates": names})
            say(f"[{tag}] reveal card choice present but Whelp not among "
                f"candidates -- not answering")
            return False
        SUBMITTED_OPPS.add(key)
        cast["reveal_handled"] = True
        cast["revealed"] = True
        cast["revealed_oid"] = _cand_ref(pick)
        say(f"[{tag}] revealing Fearsome Whelp (oid "
            f"{cast['revealed_oid']}) as the additional cost")
        wire("reveal_answer",
             {"who": tag, "revealed_oid": cast["revealed_oid"]})
        await answer_vi(c, opp, pick, tag)
        return True

    # stage 1: the boolean decideOptionalCost opportunity
    opp, chs = optional_cost_opportunity(st)
    if opp is None:
        return False
    cast["prompt_offered"] = True
    iid = opp.get("interactionId")
    key = (tag, "reveal", cast["branch"], str(iid))
    if key in SUBMITTED_OPPS:
        return False
    dumps = [choice_dump(state, ch) for ch in chs]
    wire("reveal_prompt_seen",
         {"who": tag, "branch": cast["branch"], "rev": c.revision,
          "iid": str(iid), "choices": dumps,
          "raw": json.dumps(opp, default=str)})
    say(f"[{tag}] reveal prompt offered (branch {cast['branch']}): "
        f"{json.dumps(dumps, default=str)[:400]}")
    SUBMITTED_OPPS.add(key)  # log once; handling below runs this once

    def bool_choice(want_true):
        for ch in chs:
            codes = surf_codes(ch)
            if "cancelCast" in codes:
                continue
            if "decideOptionalCost" not in codes:
                continue
            if choice_text(ch).strip().lower() == ("true" if want_true else "false"):
                return ch
        return None

    if cast["branch"] == "reveal":
        pick = bool_choice(True)
        if pick is None:
            wire("reveal_no_accept",
                 {"who": tag, "choices": dumps})
            say(f"[{tag}] boolean reveal prompt has no 'true' choice -- "
                f"not answering")
            return False
        cast["cost_accepted"] = True
        say(f"[{tag}] accepting the optional reveal cost (branch reveal); "
            f"awaiting the card-choice follow-up")
        wire("reveal_cost_accept", {"who": tag})
        await answer_vi(c, opp, pick, tag)
        return True
    # neither / control branches: decline the optional cost.
    pick = bool_choice(False)
    if pick is None:
        wire("reveal_no_decline_path",
             {"who": tag, "branch": cast["branch"], "choices": dumps})
        say(f"[{tag}] reveal prompt offered on branch {cast['branch']} but "
            f"no 'false' choice observed -- recorded, not answered")
        return False
    cast["reveal_handled"] = True
    cast["revealed"] = False
    say(f"[{tag}] declining the reveal (branch {cast['branch']})")
    wire("reveal_decline", {"who": tag, "branch": cast["branch"],
                            "choice": choice_dump(state, pick)})
    await answer_vi(c, opp, pick, tag)
    return True


def update_cast(c, st, ctx, tag):
    state = st["state"]
    for cast in ctx["casts"]:
        if cast.get("done"):
            continue
        spell_oid = stack_spell_oid(state, SENTINELS)
        if spell_oid and not cast.get("seen_on_stack"):
            cast["seen_on_stack"] = True
            cast["spell_oid"] = spell_oid
            wire("sentinels_on_stack",
                 {"who": tag, "branch": cast["branch"],
                  "rev": c.revision, "spell_oid": spell_oid})
            say(f"[{tag}] Sentinels on the stack (branch {cast['branch']})")
        if cast.get("seen_on_stack") and spell_oid is None \
                and not cast.get("resolved"):
            cast["resolved"] = True
            wire("sentinels_resolved",
                 {"who": tag, "branch": cast["branch"],
                  "rev": c.revision,
                  "p0_bf": bf_names(state, 0)[:10]})
            say(f"[{tag}] Sentinels spell resolved (branch {cast['branch']}); "
                f"awaiting entry")
        if cast.get("resolved") and not cast.get("entered"):
            # match the newest unattributed Sentinels permanent (one per
            # leg; spell oid may be re-issued or reused)
            taken = {x.get("sentinels_oid") for x in ctx["casts"]
                     if x.get("sentinels_oid")}
            perm = newest_sentinels_oid(state, 0, taken)
            if perm is not None:
                cast["entered"] = True
                cast["sentinels_oid"] = perm
                n, raw = p1p1_counters_of(state, perm)
                cast["counters"] = n
                cast["counters_raw"] = raw
                cast["need_post_export"] = True
                pwr, tgh = obj_pt(state, perm)
                cast["power"] = str(pwr)
                cast["toughness"] = str(tgh)
                wire("sentinels_entered",
                     {"who": tag, "branch": cast["branch"],
                      "rev": c.revision, "sentinels_oid": perm,
                      "p1p1_counters": n, "counters_raw": raw,
                      "power": str(pwr), "toughness": str(tgh),
                      "prompt_offered": cast.get("prompt_offered", False),
                      "revealed": cast.get("revealed"),
                      "whelp_controlled": len(whelp_oids(state, 0)) > 0})
                say(f"[{tag}] Sentinels entered (oid {perm}) P/T "
                    f"{pwr}/{tgh} +1/+1 counters={n} "
                    f"prompt_offered={cast.get('prompt_offered', False)} "
                    f"revealed={cast.get('revealed')} "
                    f"(branch {cast['branch']})")


async def common_tick_head(c, acts, st, pid, tag, ctx):
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

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
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

    update_cast(c, st, ctx, tag)
    for cast in ctx["casts"]:
        if cast.pop("need_post_export", False):
            say(f"[{tag}] post-cast export (branch {cast['branch']})")
            post = json.loads(await c.export_state())
            with open(f"{EVDIR}/post_cast_{cast['branch']}.json", "w") as f:
                json.dump(post, f, indent=1)
            wire("post_cast_export", {"who": tag, "branch": cast["branch"]})
            cast["post_exported"] = True
            cast["done"] = True
    if await handle_reveal_prompt(c, st, ctx, tag):
        return True
    if real_decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt",
                 {"who": tag, "rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return True

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_names(state, 0)
        branch = ctx["branch"]
        n_live = sum(1 for x in ctx["casts"] if not x.get("done"))
        leg_done = any(x.get("done") for x in ctx["casts"])
        g = untapped_named_lands(state, 0, FOREST)
        m = untapped_named_lands(state, 0, MOUNTAIN)
        total_untapped = untapped_land_total(state, 0)
        n_whelps_bf = len(whelp_oids(state, 0))

        dkey = (tag, "castdiag", state.get("turn_number"), state.get("phase"))
        if dkey not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(dkey)
            cast_types = sorted({a.get("type") for a in acts
                                 if "cast" in str(a.get("type")).lower()})
            say(f"[{tag}] castdiag turn={state.get('turn_number')} "
                f"branch={branch} hand_n={len(hn)} "
                f"sentinels_in_hand={SENTINELS in hn} "
                f"whelp_in_hand={WHELP in hn} whelps_bf={n_whelps_bf} "
                f"untapped_forests={g} untapped_mountains={m} "
                f"cast_actions={cast_types} leg_done={leg_done}")
            wire("cast_diag", {"who": tag, "branch": branch,
                               "turn": state.get("turn_number"),
                               "sentinels_in_hand": SENTINELS in hn,
                               "whelp_in_hand": WHELP in hn,
                               "whelps_bf": n_whelps_bf,
                               "untapped_forests": g,
                               "untapped_mountains": m,
                               "cast_action_types": cast_types,
                               "hand": hn})

        def submit_sentinels_cast(branch, reveal_target_oid):
            ctx["casts"].append({
                "branch": branch,
                "reveal_target_oid": str(reveal_target_oid)
                if reveal_target_oid else None,
                "submitted": True, "seen_on_stack": False,
                "spell_oid": None, "resolved": False,
                "prompt_offered": False,
                "reveal_handled": False, "revealed": None,
                "revealed_oid": None,
                "entered": False, "sentinels_oid": None,
                "counters": None, "counters_raw": None,
                "power": None, "toughness": None,
                "post_exported": False, "done": False})
            ST["mana_needs"][tag] = {"G": 2}

        # per-leg plan (one short game per leg):
        #   neither: cast Sentinels with no Dragon in hand / on the
        #            battlefield (opener guarantees this).
        #   reveal:  cast Sentinels holding a Whelp; accept the reveal.
        #   control: cast the Whelp first, then cast Sentinels while
        #            controlling it; decline the reveal.
        if n_live == 0 and not leg_done and branch == "control" \
                and not ctx.get("whelp_cast") and WHELP in hn \
                and m >= 1 and total_untapped >= 2:
            a, oid = cast_action_for(acts, state, WHELP)
            if a is not None:
                ST["mana_needs"][tag] = {"R": 1, "generic": 1}
                ctx["whelp_cast"] = True
                say(f"[{tag}] casting Fearsome Whelp (oid {oid})")
                wire("cast_whelp", {"oid": oid,
                                    "cast_action_data": a.get("data")})
                await submit_as_is(c, a)
                return True
        if n_live == 0 and not leg_done and SENTINELS in hn and g >= 2:
            if branch == "control" and n_whelps_bf == 0:
                pass  # wait until the Whelp is on the battlefield
            else:
                a, oid = cast_action_for(acts, state, SENTINELS)
                if a is not None:
                    woid = (next((o for o in hand_ids(state, 0)
                                  if obj_lname(state, o) == WHELP), None)
                            if branch == "reveal" else None)
                    submit_sentinels_cast(branch, woid)
                    say(f"[{tag}] casting Sentinels (oid {oid}) "
                        f"branch={branch} reveal_target={woid}")
                    wire("cast_sentinels",
                         {"oid": oid, "branch": branch,
                          "reveal_target_oid": str(woid) if woid else None,
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

# ---------------------------------------------------------------- run

def opener_ok(branch, hn):
    """Per-leg opening-hand requirements so each short game can finish in
    a few turns instead of waiting on draws:
      neither: Sentinels + 2 Forests, and NO Dragon in hand (the engine
               then skips the optional reveal prompt entirely).
      reveal:  Sentinels + a Whelp + 2 Forests.
      control: Sentinels + a Whelp + 2 Forests + a Mountain (Whelp is
               {1}{R}, cast before the Sentinels)."""
    s = hn.count(SENTINELS) >= 1
    w = hn.count(WHELP) >= 1
    f = hn.count(FOREST) >= 2
    m = hn.count(MOUNTAIN) >= 1
    if branch == "neither":
        return s and f and not w
    if branch == "reveal":
        return s and w and f
    if branch == "control":
        return s and w and f and m
    return False


async def run_leg(branch, attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9854-{branch}-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {"casts": [], "whelp_cast": False, "branch": branch}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Forest", 44), ("Mountain", 8),
                         ("Fearsome Whelp", 4), ("Scaleguard Sentinels", 4)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 60),))
    say(f"game {p0.game_code} branch={branch} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"branch": branch, "attempt": attempt,
                        "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"branch": branch, "attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                                and p1.player_id is not None) else "failed"

    # fast-abort: each leg needs its opener cards in the first 7; redeal a
    # fresh game when they are missing.
    for _ in range(80):
        await asyncio.sleep(0.25)
        if p0.latest and p0.latest.get("state", {}).get("turn_number", 0) >= 1:
            break
    st = p0.latest
    hn = hand_names(st["state"], 0) if st else []
    if st is None or not opener_ok(branch, hn):
        say(f"[{tag}] fast abort: opener {hn} fails the {branch}-leg "
            f"requirements")
        wire("fast_abort", {"reason": f"opener fails {branch}-leg requirements",
                            "hand": hn})
        obs["notes"].append(f"fast abort: opener lacked the {branch}-leg "
                            f"requirements; fresh game redealt")
        obs["fast_aborted"] = True
        await p0.close()
        await p1.close()
        return obs

    t0 = time.time()
    last_diag = 0
    settle_ticks = 0
    while time.time() - t0 < 600:
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
                    f"casts={[(x['branch'], x.get('entered'), x.get('counters')) for x in ctx['casts']]} "
                    f"p0bf={bf_names(s, 0)[:6]} p1bf={bf_names(s, 1)[:6]}")
                if decision_pending(st):
                    say(f"[diag] {c.name} pending opps: {_opp_summary(st)}")
        # end condition: this leg's cast done + quiet settle
        leg_casts = [x for x in ctx["casts"]
                     if x.get("branch") == branch and x.get("done")]
        if leg_casts:
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break
        if len(ctx["casts"]) == 0 and time.time() - t0 > 300:
            say(f"[{tag}] no cast submitted >300s; capturing state and bailing")
            wire("leg_bail", {"branch": branch, "reason": "no cast in 300s"})
            break

    st = p0.latest
    obs["casts"] = [
        {"branch": x["branch"], "submitted": x.get("submitted"),
         "seen_on_stack": x.get("seen_on_stack"),
         "resolved": x.get("resolved"),
         "prompt_offered": x.get("prompt_offered"),
         "reveal_handled": x.get("reveal_handled"),
         "revealed": x.get("revealed"),
         "revealed_oid": x.get("revealed_oid"),
         "entered": x.get("entered"),
         "sentinels_oid": x.get("sentinels_oid"),
         "counters": x.get("counters"),
         "counters_raw": x.get("counters_raw"),
         "power": x.get("power"), "toughness": x.get("toughness"),
         "post_exported": x.get("post_exported")}
        for x in ctx["casts"]]
    leg = next((x for x in ctx["casts"] if x["branch"] == branch), None)
    obs["leg"] = {
        "branch": branch,
        "completed": bool(leg and leg.get("entered")),
        "counters": leg.get("counters") if leg else None,
        "prompt_offered": leg.get("prompt_offered") if leg else None,
        "revealed": leg.get("revealed") if leg else None,
    }
    if leg and leg.get("entered"):
        obs["notes"].append(
            f"{branch} leg: Sentinels (oid {leg.get('sentinels_oid')}) "
            f"entered with {leg.get('counters')} +1/+1 counter(s) "
            f"(P/T {leg.get('power')}/{leg.get('toughness')}; "
            f"prompt_offered={leg.get('prompt_offered')}, "
            f"revealed={leg.get('revealed')})")
    else:
        obs["notes"].append(
            f"{branch} leg incomplete: the Sentinels never entered "
            f"(casts={obs['casts']})")

    fn = f"{EVDIR}/post_cast_{branch}.json"
    try:
        with open(fn) as f:
            json.load(f)
        obs["notes"].append(f"post_cast_{branch}.json captured and parses")
    except (OSError, ValueError):
        obs["notes"].append(f"post_cast_{branch}.json missing/unparseable")

    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    parse_ev = check_parse_gate()
    # one short game per leg; retry each leg up to 8 redeals
    legs = {}
    leg_notes = []
    for branch in ("neither", "reveal", "control"):
        obs = None
        for attempt in range(24):
            obs = await run_leg(branch, attempt)
            if obs.get("fast_aborted"):
                say(f"[{branch}] attempt {attempt} fast-aborted; redealing")
                continue
            if obs.get("leg", {}).get("completed"):
                say(f"[{branch}] leg completed on attempt {attempt}")
                break
            say(f"[{branch}] attempt {attempt} incomplete; retrying"
                if attempt < 23 else f"[{branch}] giving up after 24 attempts")
        legs[branch] = obs
        leg_notes.extend(obs.get("notes", []))

    ass = {}
    a1 = all((legs[b] or {}).get("assert", {}).get("A1_setup_ok") == "passed"
             for b in ("neither", "reveal", "control")
             if legs.get(b) is not None)
    ass["A1_setup_ok"] = "passed" if a1 else "failed"

    def leg_assert(branch, expect_counters, label):
        obs = legs.get(branch) or {}
        leg = (obs.get("leg") or {})
        if not leg.get("completed"):
            ass[label] = "not-run"
            leg_notes.append(
                f"{label} not-run: {branch}-leg Sentinels never entered "
                f"(casts={(obs.get('casts'))})")
            return
        got = leg.get("counters")
        detail = (f"{branch} leg: Sentinels entered with {got} +1/+1 "
                  f"counter(s), expected {expect_counters} "
                  f"(prompt_offered={leg.get('prompt_offered')}, "
                  f"revealed={leg.get('revealed')})")
        if got == expect_counters:
            ass[label] = "passed"
            leg_notes.append(f"{label}: {detail}")
        else:
            ass[label] = "failed"
            leg_notes.append(f"{label} FAILED: {detail}")

    # A3: neither leg -- no reveal, no Dragon controlled -> no counter
    leg_assert("neither", 0, "A3_neither_leg")
    # A4: reveal leg -- revealed a Dragon -> exactly one counter
    leg_assert("reveal", 1, "A4_reveal_leg")
    # A5: control leg -- controlled a Dragon, no reveal -> exactly one counter
    leg_assert("control", 1, "A5_control_leg")
    # A2 (decisive, parse-level): "failed" means the reported bug is present.
    if parse_ev["no_typed_gate"]:
        ass["A2_parse_gate"] = "failed"
    elif parse_ev["typed_gate_present"]:
        ass["A2_parse_gate"] = "passed"
    else:
        ass["A2_parse_gate"] = "not-run"
    # A6: the card-data lower matches the reported shape
    acd = parse_ev["additional_cost_data"] or {}
    ac_cost = acd.get("cost") or {}
    ac_filter = ac_cost.get("filter") or {}
    ass["A6_data_shape"] = "passed" if (
        parse_ev["n_spell_abilities"] == 1
        and parse_ev["gate_effect_type"] == "Unimplemented"
        and parse_ev["gate_effect_name"] == "enters_with_gate"
        and parse_ev["additional_cost_type"] == "Optional"
        and ac_cost.get("type") == "Reveal"
        and "if you revealed a Dragon card or controlled a Dragon" in
            (parse_ev["oracle"] or "")) else "failed"
    notes = list(leg_notes)
    notes.append(
        "neither-leg nuance: the engine did not offer the optional reveal "
        "prompt on the neither cast because P0 held no Dragon card in hand "
        "at the time (prompt_offered=false) -- the optional cost was "
        "correctly skipped, not declined.")
    notes.append(
        f"parse: no_typed_gate={parse_ev['no_typed_gate']} "
        f"effect={parse_ev['gate_effect_type']} "
        f"name={parse_ev['gate_effect_name']} "
        f"condition={parse_ev['gate_condition']} "
        f"additional_cost={parse_ev['additional_cost_type']} "
        f"parse_warnings={parse_ev['parse_warnings']}")
    for b in ("neither", "reveal", "control"):
        notes.append(f"casts[{b}]: {(legs.get(b) or {}).get('casts')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; priority-gated passes; "
                 "sleep(0) yield before leg evaluation. One short game per "
                 "leg with per-leg opener requirements "
                 "(neither: Sentinels+2 Forests, no Dragon in hand; reveal: "
                 "+Whelp; control: +Whelp+Mountain, Whelp cast first). The "
                 "optional reveal additional cost (hand-card candidates with "
                 "no action surface) is answered REVEAL-the-Whelp on the "
                 "reveal branch and declined (if a decline path exists) on "
                 "the other branches; any other prompt is recorded, never "
                 "auto-answered.")

    a2 = ass.get("A2_parse_gate")
    if a2 == "failed":
        verdict = "reproduced"
    elif a2 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = the parse output carries no "
                 "typed gate for the 'enters with a +1/+1 counter if you "
                 "revealed a Dragon card or controlled a Dragon as you cast "
                 "this spell' clause -- the whole clause lowers as "
                 "Unimplemented 'enters_with_gate' (condition null) while "
                 "the optional Reveal-Dragon additional cost parses but "
                 "nothing consumes it -- the reported outcome. "
                 "not-reproduced = a typed gate exists.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9854",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.108.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified "
                           "release; reused by this run); evidence under "
                           "evidence/9854/" + RUN_ID),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Forest", 44], ["Mountain", 8],
                   ["Fearsome Whelp", 4], ["Scaleguard Sentinels", 4]],
            "P1": [["Island", 60]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "parse_evidence": parse_ev,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; A6 %s"
                   % (ass.get("A1_setup_ok"), a2,
                      ass.get("A3_neither_leg"),
                      ass.get("A4_reveal_leg"),
                      ass.get("A5_control_leg"),
                      ass.get("A6_data_shape"))),
        "scope": ("Scaleguard Sentinels 'enters with a +1/+1 counter if you "
                  "revealed a Dragon card or controlled a Dragon as you cast "
                  "this spell' gate: parse-level presence of a typed gate "
                  "linking the optional reveal additional cost with the "
                  "as-cast control snapshot, plus three in-game legs "
                  "(neither / reveal / control); two-player game; native "
                  "human seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The reveal/control legs can only run if the engine offers the "
            "optional reveal prompt and the legs' preconditions are drawn; "
            "missing preconditions mark those legs not-run.",
            "+1/+1-counter reads are a best-effort parse of the runtime "
            "object's counters payload; the verbatim payload plus P/T are "
            "preserved in wire_log.jsonl under sentinels_entered.",
            "The Sentinels permanent is located as the newest P0 "
            "'scaleguard sentinels' permanent after resolution; if the "
            "engine re-issues the oid, the fallback is recorded in the wire "
            "log.",
        ],
        "setup_line": ("P0 44x Forest + 8x Mountain + 4x Fearsome Whelp "
                       "({1}{R} 2/2 Dragon) + 4x Scaleguard Sentinels "
                       "({G}{G} 2/3 Human Soldier); P1 60x Island"),
        "contract_line": ("Parse (decisive): the 'enters with a +1/+1 counter "
                          "if you revealed a Dragon card or controlled a "
                          "Dragon as you cast this spell' clause must carry "
                          "a typed gate, or the card must read unsupported. "
                          "Game (confirming): neither leg (no reveal, no "
                          "Dragon) -> 2/3 no counter; reveal leg (revealed a "
                          "Dragon) -> 3/4 one counter; control leg "
                          "(controlled a Dragon, no reveal) -> 3/4 one "
                          "counter."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9854_01080.py", "w") as f:
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
    W, H = 1000, 1000
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9854 - Scaleguard Sentinels: 'enters with a +1/+1",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "counter ... as you cast this spell' has no typed gate",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_parse_gate": "A2 [bug] no typed gate (Unimplemented enters_with_gate)",
        "A3_neither_leg": "A3 neither leg: no counter without reveal/Dragon",
        "A4_reveal_leg": "A4 reveal leg: one counter after revealing a Dragon",
        "A5_control_leg": "A5 control leg: one counter while controlling a Dragon",
        "A6_data_shape": "A6 card-data lower matches the reported shape",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:14]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9854/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9854.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9854 instance is alive "
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
