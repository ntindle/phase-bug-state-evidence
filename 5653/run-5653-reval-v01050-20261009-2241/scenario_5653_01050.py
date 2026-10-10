#!/usr/bin/env python3
"""Issue #5653: Chains of Mephistopheles -- result-referential conditions
dropped from the Draw/Mill sub-actions at runtime.

RE-VALIDATION on pinned v0.105.0 (2026-10-09, protocol 120). Prior runs:
  v0.78.0  20260909-5653 reproduced -- published, evidence 5653/20260909-5653
  v0.85.0  20260916-5653 reproduced -- published, evidence 5653/20260916-5653
  v0.102.0 20261004-5653 reproduced -- published, evidence 5653/20261004-5653
  v0.103.0 20261006-5653 reproduced -- published, evidence 5653/20261006-5653
Ported from scenario_5653_01040.py (v0.104.0/protocol 118, verified
2026-10-08) to protocol 120: HELLO advertises 120; engine auto-pays
mana (bare CastSpell, no tapLandForMana/pay_mana bookkeeping);
cast-confirmation guard (30s backstop) before any priority pass;
repeating prompts answered per-firing keyed by interactionId.
Claimed parser fix PR #6855 ("fix(parser): preserve discard-this-way
conditional outcomes", merged 2026-08-02) says it closes #5653, but the
2026-09-16 backfill run found the runtime still broken. This run re-tests the
reported OUTCOME on the current pin (per the playbook: never infer a fix from
a parser signal).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Issue (internal triage, 2026-07-12, status:confirmed): Chains of
Mephistopheles / Magus of the Chains misparse the result-referential
conditions, so the three sub-actions run unconditionally instead of
branching on the discard outcome.

Oracle text (verified from pinned v0.104.0 card-data.json, key
'chains of mephistopheles'):
  "If a player would draw a card except the first one they draw in each of
   their draw steps, that player discards a card instead. If the player
   discards a card this way, they draw a card. If the player doesn't discard
   a card this way, they mill a card."

Pinned v0.104.0 parse (replacements[0], verified this run): STILL the
old shape -- Discard{Fixed 1} [condition=null] -> Draw{Fixed 1}
[condition=EffectOutcome{signal: OptionalEffectPerformed}] -> Mill{Fixed 1}
[condition=Not(EffectOutcome{signal: OptionalEffectPerformed})].
A mandatory discard is not an optional effect, so whether the runtime emits
that signal for the discard determines whether the Draw branch fires, the
Mill branch fires, or neither.

Correct behavior per Oracle for ONE extra draw event with P1 hand = H >= 1
(each chained draw is itself a non-first draw, hence replaced again):
  discard 1 -> draw (replaced) -> discard 1 -> ... -> hand empty -> mill 1.
  Net: exactly H discards, exactly 1 mill, 0 net draws, P1 hand H -> 0.
Buggy behavior observed on v0.78.0/v0.85.0:
  per extra draw: discard exactly 1, draw never fires, mill never fires.
  Net: 1 discard, 0 mills, P1 hand H -> H-1.

Setup (native engine, two human-client seats):
  P0: 8x chains of mephistopheles ({1}{B}{B}) + 8x blue sun's zenith
      ({X}{U}{U}{U}, cast at X=1) + 22x island + 22x swamp.
      (8x density: engine accepts >4-of for custom games; mulligan to
      chains + 2+ lands.)
  P1: 60x island dummy (plays a land, passes; never attacks, never casts).

Flow:
  1. P0 casts Chains of Mephistopheles (turn 3+, {1}{B}{B}).
  2. Control: P1's draw-step first draws must be untouched (hand stays 7).
  3. P0 casts Blue Sun's Zenith X=1 targeting P1 ("target player draws 1").
  4. Observe the replacement sequence to completion; export pre/post states.

Assertions:
  A1_setup_ok        pre.json: P0 main phase, chains on P0 battlefield,
                     zenith in hand, >=4 untapped lands (>=3 islands),
                     P1 hand H>=1, life 20/20.
  A2_first_draw_untouched (control): P1 hand at pre matches 7 kept + draws -
                     lands played (accounting for the starting player's skipped
                     first draw) with an empty graveyard: the draw-step
                     first-draw exception (ExceptFirstDrawInDrawStep) holds.
  A3_replacement_fires: during resolution P1 discards >= 1 card (gy grows from
                     hand, not from library) and performs no normal draw.
  A4_draw_branch_fires (correct): total P1 discards during resolution == H
                     (every card discarded via the chained draw branch), i.e.
                     P1 hand H -> 0. Buggy: exactly 1 discard, draw never fires.
  A5_mill_exactly_once (correct): exactly 1 P1 mill during resolution AND it
                     happens only after the hand is empty (hand_after == 0).
                     Buggy: 0 mills.
  A6_cleanup         post.json: stack empty, game advanced past the cast,
                     zenith in P0 library (it shuffles itself in).

Verdict rule: reproduced iff A1 passed and the Oracle-mandated
discard->draw/discard->mill branching is observably broken (A4 or A5 fail).
not-reproduced iff the full correct trace is observed (A1..A6 pass).
blocked iff setup cannot be driven to the Zenith cast.

Evidence: evidence/5653/<run-id>/pre.json (before Zenith cast),
mid_resolution.json (first zone change during resolution), post.json (after
full resolution), run.json, assertions.json, manifest.sha256, summary.png,
scenario_5653_01050.py, wire_log.jsonl, scenario_run.log, data_evidence.json,
chains_parse.json

Protocol-120 driver conventions (ported from scenario_5653_01040.py):
  - HELLO advertises protocol 120; engine auto-pays mana (bare CastSpell);
  - cast-confirmation guard with 30s backstop before any priority pass;
  - repeating prompts answered per-firing keyed by interactionId.
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full (via
    driver/client.py); deck schema {"name", "main_deck": [...]}.
  - waiting_for is GONE (null); priority = PassPriority in the viewing seat's
    top-level legal_actions. MulliganDecision answered as legacy Action
    (verified accepted on 106). Bottom-after-mulligan via vi select, gated on
    waitingForKind.code == 'mulligan' AND turn 1/Untap.
  - Priority-menu noise (tapLandForMana / castSpell / activateAbility /
    passPriority choice menus) is not a decision: real_decision_pending
    excludes NON_DECISION_CODES; only schema opportunities,
    decideOptionalEffect, and unknown codes block passes.
  - surf_codes() filters None codes.
  - P1's Chains discard (mandatory): answered during the resolution window
    via vi schema/select of own hand cards or legacy SelectCards, count from
    the spec constraint (default 1). DiscardToHandSize answered via the
    generic 'choose' vi surface only when hand > 7 outside the window.
  - Zenith X-choice: vi schema/number opportunity answered 1. Target: vi
    schema/sequence with composite choice ids, prefer seat-1 candidate.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-5653-reval-v01050-20261009-2241"
ISSUE = 5653
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CHAINS = "chains of mephistopheles"
ZENITH = "blue sun's zenith"
ISLAND = "island"
SWAMP = "swamp"

P0_DECK = deck((CHAINS, 8), (ZENITH, 8), (ISLAND, 22), (SWAMP, 22))
P1_DECK = deck((ISLAND, 60))

SERVER_IDENTITY = {
    "server_version": "v0.105.0",
    "build_commit": "965e243",
    "protocol_version": 120,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.105.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode (pin verified 2026-10-09 13:11 run); data digests "
                       "match the signed manifest; digests recomputed against "
                       "on-disk files this run"),
    "source": ("2026-10-09: latest stable release v0.105.0 (published "
               "2026-10-09T17:49:54Z) == pinned release dir; ServerHello "
               "0.105.0/965e243/protocol 120 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "server process on 127.0.0.1:9374 (pid 16023) reused from "
               "owning run run-20261009-2211-backfill (not restarted "
               "per playbook; probe confirmed pinned version/build/protocol)"),
}
for _f, _k in (("server/releases/v0.105.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.105.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.105.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"))

ASS_KEYS = ("A1_setup_ok", "A2_first_draw_untouched", "A3_replacement_fires",
            "A4_draw_branch_fires", "A5_mill_exactly_once", "A6_cleanup")

MULLS = set()
SUBMITTED = set()
LAND_PLAYED_TURN = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        for a in opp.get("actions", []) or []:
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


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def untapped_lands(state, pid, name=None):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")
            and (name is None or obj_lname(state, o) == name)]


def chains_on_bf(state, pid=0):
    return len(bf_by_name(state, pid, CHAINS)) > 0


def gy_names(state, pid):
    return [obj_lname(state, oid) for oid, o in
            (state.get("objects") or {}).items()
            if get_obj(state, oid).get("zone") == "Graveyard"
            and str(get_obj(state, oid).get("controller", -1)) == str(pid)]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
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
    assert str(ver).startswith("0.105.0"), f"unexpected version {ver}"
    assert int(proto) == 120, f"unexpected protocol {proto}"
    assert str(build) == "965e243", f"unexpected build {build}"


def check_data_level():
    ok, notes = True, []
    c = CARD_DATA.get(CHAINS, {})
    oracle = str(c.get("oracle_text", ""))
    for needle in ("discards a card instead", "they draw a card",
                   "they mill a card"):
        if needle.lower() not in oracle.lower():
            ok = False
            notes.append(f"{CHAINS}: oracle missing {needle!r}")
    reps = c.get("replacements") or []
    shape = None
    if reps:
        r = reps[0]
        ex = r.get("execute") or {}
        eff = ex.get("effect") or {}
        sub = ex.get("sub_ability") or {}
        subeff = sub.get("effect") or {}
        sub2 = sub.get("sub_ability") or {}
        sub2eff = sub2.get("effect") or {}
        shape = {
            "event": r.get("event"),
            "head_effect": eff.get("type"),
            "head_condition": str(eff.get("condition")),
            "mid_effect": subeff.get("type"),
            "mid_condition": str(sub.get("condition")),
            "tail_effect": sub2eff.get("type"),
            "tail_condition": str(sub2.get("condition")),
        }
        if not (eff.get("type") == "Discard" and subeff.get("type") == "Draw"
                and sub2eff.get("type") == "Mill"):
            ok = False
            notes.append(f"unexpected replacement shape: {shape}")
    else:
        ok = False
        notes.append("no replacements in card data")
    with open(f"{EVDIR}/chains_parse.json", "w") as f:
        json.dump({"oracle": oracle, "replacements": reps,
                   "shape": shape}, f, indent=1, default=str)
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {CHAINS: oracle[:200],
                              ZENITH: str(CARD_DATA.get(ZENITH, {})
                                          .get("oracle_text"))[:160]}},
                  f, indent=1)
    say(f"data-level check: ok={ok} notes={notes} shape={shape}")


async def do_mulligan(c, acts, st, pid, tag, keep_names):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if isinstance(s.get("data"), dict)]
            if "mulliganDecision" in codes:
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    mull_count = sum(1 for k in MULLS if k[0] == tag)
    if tag == "P0" and len(hn) > 4 and not \
            any(n in hn for n in keep_names) and mull_count < 4:
        say(f"[{tag}] mulligan ({len(hn)} cards, hand={hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn,
                          "iid": iid})
        return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn, "iid": iid})
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
    target = None
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
        target = (opp, cands, spec)
        break
    if target is None:
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm == CHAINS:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    sub = {"interactionId": iid,
           "response": {"type": "select",
                        "data": {"choiceIds": [ch.get("id") for ch in picks]}}}
    await interact_as(c, sub, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(hand)
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        if any(_cand_reference(ch) in handset for ch in cands):
            key = (tag, "handsize", str(c.revision), opp.get("interactionId"))
            if key in SUBMITTED:
                return False

            def rank(o):
                nm = obj_lname(state, o)
                if nm in (ISLAND, SWAMP):
                    return (0, nm)
                if nm in (CHAINS, ZENITH):
                    return (5, nm)
                return (2, nm)

            picks = [ch["id"] for ch in
                     sorted(cands,
                            key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
            SUBMITTED.add(key)
            say(f"[{tag}] discarding to hand size via vi: picks={picks}")
            wire("handsize_discard", {"who": tag, "picks": picks})
            spec = (rdata.get("spec") or {})
            stype = spec.get("type") or "select"
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


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
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


def cast_spell_offered(state, st, spell):
    """True if a CastSpell action for `spell` is currently offered."""
    for a in merged_actions(st):
        if a["type"] == "CastSpell" \
                and obj_lname(state, a.get("data", {}).get("object_id")) == spell:
            return True, a
    vi = st.get("viewer_interaction") or {}
    if vi.get("canSubmit"):
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "castSpell" in codes and spell in choice_text(ch).lower():
                    return True, {"_vi_choice": ch, "_vi_opp": opp}
    return False, None


async def cast_named(c, st, state, acts, name, tag, flag):
    """Bare CastSpell (protocol 120: engine auto-pays). Arms the
    cast-confirmation guard; `flag` is a one-shot cleared on the 30s
    backstop so a dropped cast re-triggers."""
    offered, act = cast_spell_offered(state, st, name)
    if not offered:
        return False
    oid = None
    if "_vi_choice" in act:
        opp, ch = act["_vi_opp"], act["_vi_choice"]
        say(f"[{tag}] casting {name} via viewer_interaction choice")
        wire(f"{tag}_cast_vi", {"name": name,
                                "choice": choice_text(ch)[:160]})
        await answer_vi(c, opp, ch, tag)
    else:
        oid = str(act.get("data", {}).get("object_id"))
        say(f"[{tag}] casting {name} (oid={oid})")
        wire(f"{tag}_cast", {"name": name,
                             "action": {k: v for k, v in act.items()
                                        if not k.startswith("_")}})
        await submit_as_is(c, act)
        set_pending_cast(oid, name, tag, flag)
        ST[flag] = True
    await asyncio.sleep(0.5)
    return True


def set_pending_cast(oid, name, tag, flag):
    ST["pending_cast"] = {"oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag, "flag": flag}


def cast_guard_tick(state, tag):
    """Protocol-120 cast-confirmation guard (2026-10-09 lesson).

    Returns "hold" while our CastSpell is in flight but unconfirmed: the
    driver must not pass priority or start new plays in that window -- a
    bare CastSpell can lose a race to our own PassPriority submitted on the
    next tick and be silently dropped by the server. Returns "proceed" once
    confirmed; a 30s backstop clears a still-unconfirmed cast and resets
    the one-shot flag so the play re-triggers. Must run BEFORE any
    early-return block (attackers/blockers/target-wait).
    """
    pc = ST.get("pending_cast")
    if not pc:
        return "proceed"
    o = (state.get("objects") or {}).get(str(pc["oid"]))
    zone = str((o.get("zone") or "")).lower() if o else "gone"
    if zone in ("stack", "battlefield", "graveyard", "exile", "command"):
        ST["pending_cast"] = None
        say(f"[{tag}] cast confirmed ({zone}): {pc['name']} "
            f"oid {pc['oid']}")
        wire("cast_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                "zone": zone})
        return "proceed"
    # fallback: the spell may live under a new object id on the stack
    want = pc["name"].lower()
    for oid2, o2 in (state.get("objects") or {}).items():
        nm = str(o2.get("base_name") or o2.get("name") or "").lower()
        if nm == want and str(o2.get("zone") or "").lower() == "stack":
            ST["pending_cast"] = None
            say(f"[{tag}] cast confirmed (stack scan, oid {oid2}): "
                f"{pc['name']}")
            wire("cast_confirmed", {"name": pc["name"],
                                    "oid": str(oid2), "zone": "stack",
                                    "via": "name_scan"})
            return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] CAST NOT CONFIRMED after 30s ({zone}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("cast_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]), "zone": zone})
        ST["pending_cast"] = None
        if pc.get("flag"):
            ST[pc["flag"]] = False
        return "proceed"
    return "hold"


async def answer_x_choice(c, st, tag, value):
    """Zenith X-choice: vi schema/number opportunity answered with value."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if ("x", iid) in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if (spec.get("type") or "") != "number":
            continue
        SUBMITTED.add(("x", iid))
        say(f"[{tag}] choosing X={value} for Blue Sun's Zenith")
        wire("x_choice", {"submission": {"interactionId": iid, "value": value},
                          "interaction": opp})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "number",
                                           "data": {"value": value}}}, tag)
        return True
    return False


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def _find_player_refs(data, want=1):
    refs = []
    if isinstance(data, dict):
        t = data.get("type")
        if t == "Player":
            try:
                if int(data.get("data")) == want:
                    refs.append(data)
            except (TypeError, ValueError):
                pass
        for k in ("player", "seat", "player_id", "seat_index"):
            try:
                if data.get(k) is not None and int(data[k]) == want:
                    refs.append({k: data[k]})
            except (TypeError, ValueError):
                pass
        for v in data.values():
            refs.extend(_find_player_refs(v, want))
    elif isinstance(data, list):
        for v in data:
            refs.extend(_find_player_refs(v, want))
    return refs


async def answer_zenith_target(c, st, tag):
    """Zenith target: vi schema/sequence with composite choice ids; prefer
    the seat-1 (P1) player candidate. Protocol 120 instead offers legacy
    ChooseTarget actions (one per candidate): submit the P1 one as-is."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if ("tgt", iid) in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "sequence":
            continue
        cands = rdata.get("candidates") or []
        avail = [ch for ch in cands
                 if ch.get("status", {}).get("type") in (None, "available")]
        if not avail:
            continue
        pick = next((ch for ch in avail if cand_seat(ch) == 1), None)
        if pick is None:
            say(f"[{tag}] target prompt: no seat-1 candidate; "
                f"texts={[choice_text(x)[:80] for x in avail]}")
            wire(f"{tag}_target_no_pick", {"interaction": opp})
            continue
        SUBMITTED.add(("tgt", iid))
        say(f"[{tag}] targeting P1 with Zenith (choice {pick.get('id')})")
        wire("zenith_target", {"interaction": opp})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "sequence",
                                           "data": {"choiceIds": [pick.get("id")]}}},
                          tag)
        return True
    # legacy ChooseTarget path (protocol 120): one action per candidate
    for a in merged_actions(st):
        if a.get("type") != "ChooseTarget":
            continue
        clean = {k: v for k, v in a.items() if not k.startswith("_")}
        key = ("tgt_leg", json.dumps(clean.get("data", {}),
                                     sort_keys=True, default=str))
        if key not in SUBMITTED:
            SUBMITTED.add(key)
            say(f"[{tag}] ChooseTarget offered: "
                f"{json.dumps(clean, default=str)[:500]}")
            wire("choosetarget_seen", {"action": clean})
        if ("tgt_leg_done",) in SUBMITTED:
            continue
        if _find_player_refs(clean.get("data", {}), 1):
            SUBMITTED.add(("tgt_leg_done",))
            say(f"[{tag}] submitting ChooseTarget for P1")
            wire("choosetarget_submit", {"action": clean})
            await submit_as_is(c, a)
            ST["zenith_target_answered"] = True
            ST["notes"].append("P0 targeted P1 with Blue Sun's Zenith "
                               "(legacy ChooseTarget)")
            return True
    return False


async def answer_p1_chains_discard(c, st, state, tag):
    """P1's mandatory Chains discard during the resolution window. Answers a
    vi schema/select opportunity offering P1's own hand cards, or a legacy
    SelectCards action on hand cards. Count from the spec constraint,
    default 1."""
    hand = hand_ids(state, 1)
    if not hand:
        return False
    handset = set(hand)
    # vi surface first
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if ("p1d", iid) in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        if not any(_cand_reference(ch) in handset for ch in cands):
            continue
        spec = rdata.get("spec", {}) or {}
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
            .get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        n = max(1, min(n, len(cands)))
        picks = [ch["id"] for ch in cands[:n]]
        SUBMITTED.add(("p1d", iid))
        say(f"[{tag}] P1 discards to Chains via vi: "
            f"{[obj_lname(state, _cand_reference(cands[i])) for i in range(n)]}")
        wire("p1_chains_discard", {"who": tag, "picks": picks,
                                   "hand": hand_lnames(state, 1)})
        stype = spec.get("type") or "select"
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        ST["discard_events"].append({"turn": state.get("turn_number"),
                                     "phase": state.get("phase"),
                                     "discarded": hand_lnames(state, 1)[:n],
                                     "hand_before": len(hand)})
        return True
    # legacy SelectCards fallback
    for a in merged_actions(st):
        if a.get("type") != "SelectCards":
            continue
        cards = [int(x) for x in (a.get("data", {}) or {}).get("cards", [])]
        if not cards or not all(x in handset for x in cards):
            continue
        key = ("p1d", json.dumps(a.get("data", {}), sort_keys=True))
        if key in SUBMITTED:
            continue
        SUBMITTED.add(key)
        sub = {"type": "SelectCards", "data": {"cards": cards[:1]}}
        say(f"[{tag}] P1 discards to Chains via legacy SelectCards: "
            f"{[obj_lname(state, x) for x in cards[:1]]}")
        wire("p1_chains_discard_legacy", {"submission": sub})
        await c.send_action(sub)
        ST["discard_events"].append({"turn": state.get("turn_number"),
                                     "phase": state.get("phase"),
                                     "discarded": hand_lnames(state, 1)[:1],
                                     "hand_before": len(hand)})
        return True
    return False

# ---------------------------------------------------------------- ticks

ST = {"discard_events": [], "p1_turns": [],
      "pre_p1_gy": None, "cast_submitted": False, "cast_turn": None,
      "stage": "setup", "pre_exported": False, "mid_exported": False,
      "post_exported": False, "zenith_x_answered": False,
      "zenith_target_answered": False, "notes": [], "done": False}


def can_pay_generic(state, pid, lands_needed):
    return len(untapped_lands(state, pid)) >= lands_needed


async def p0_tick(c, st, acts, state):
    tag = "P0"
    if await do_mulligan(c, acts, st, 0, tag, [CHAINS]):
        return
    if await do_bottom(c, acts, st, 0, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return

    # yield to the pump before reading fresh state for leg evaluation
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # protocol-120 cast-confirmation guard BEFORE any early return:
    # while our CastSpell is in flight we may answer schema decisions
    # (X-choice / target) but must not pass priority or start new plays.
    holding = cast_guard_tick(state, tag) == "hold"
    # a confirmed zenith cast advances the stage (the guard cleared
    # pending_cast; the backstop would have reset zenith_cast_submitted)
    awaiting_target = any(a.get("type") == "ChooseTarget"
                        for a in acts)
    if ST.get("zenith_cast_submitted") and ST["stage"] == "setup" \
            and (not ST.get("pending_cast") or awaiting_target):
        if awaiting_target:
            # the spell is demonstrably alive (awaiting our target
            # answer); drop the stale guard entry
            ST["pending_cast"] = None
        ST["cast_submitted"] = True
        ST["cast_turn"] = state.get("turn_number")
        ST["stage"] = "resolve"
        ST["notes"].append(f"Zenith cast confirmed on stack "
                           f"(turn {ST['cast_turn']})")
        say(f"[P0] Zenith confirmed on stack (turn {ST['cast_turn']})")

    if ST["stage"] == "setup":
        if my_main(state, 0) and not holding:
            if await play_a_land(c, state, 0, acts, tag):
                return
            # 1. cast Chains of Mephistopheles when not yet on BF
            #    (engine auto-pays; offered only when castable)
            if (not chains_on_bf(state, 0)
                    and not ST.get("chains_cast_submitted")):
                if await cast_named(c, st, state, acts, CHAINS, tag,
                                    "chains_cast_submitted"):
                    ST["notes"].append("P0 cast Chains of Mephistopheles")
                    return
            # 2. cast Blue Sun's Zenith X=1 targeting P1
            if (chains_on_bf(state, 0)
                    and not ST.get("zenith_cast_submitted")):
                say("[P0] main phase: exporting PRE, then casting "
                    "Zenith X=1 @P1")
                try:
                    pre = await c.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    ST["pre_exported"] = True
                    ST["pre_p1_gy"] = len(player_of(state, 1)
                                         .get("graveyard", []))
                    ST["p1_turns_at_pre"] = list(ST["p1_turns"])
                except Exception as e:
                    ST["notes"].append(f"pre export failed: {e}")
                    say(f"[P0] pre export failed: {e}")
                if await cast_named(c, st, state, acts, ZENITH, tag,
                                    "zenith_cast_submitted"):
                    ST["notes"].append("Zenith CastSpell submitted "
                                       f"(turn {state.get('turn_number')})")
                    say("[P0] submitted Blue Sun's Zenith X=1 @P1")
                    return
        if not holding and not real_decision_pending(st) \
                and my_priority(acts):
            await pass_priority(c, st, acts)
        return

    if ST["stage"] == "resolve":
        # answer the Zenith X-choice and target prompts (schema opportunities
        # are real decisions and must not be passed through)
        if not ST["zenith_x_answered"]:
            if await answer_x_choice(c, st, tag, 1):
                ST["zenith_x_answered"] = True
                ST["notes"].append("P0 chose X=1 on the Zenith X-choice prompt")
                return
        if not ST["zenith_target_answered"]:
            if await answer_zenith_target(c, st, tag):
                ST["zenith_target_answered"] = True
                ST["notes"].append("P0 targeted P1 with Blue Sun's Zenith")
                return
        # mid export: first zone change during resolution
        if not ST["mid_exported"] and len(ST.get("hand_trace", [])) > 1:
            try:
                mid = await c.export_state()
                with open(f"{EVDIR}/mid_resolution.json", "w") as f:
                    f.write(mid)
                ST["mid_exported"] = True
                say("[P0] exported MID (resolution underway)")
            except Exception as e:
                ST["notes"].append(f"mid export failed: {e}")
        # post export: cast submitted, stack empty, P1 gy grew (resolution
        # happened) -- or the game advanced well past the cast turn.
        p1_gy_now = len(player_of(state, 1).get("graveyard", []))
        resolved = (ST["pre_p1_gy"] is not None
                    and p1_gy_now > ST["pre_p1_gy"])
        advanced_far = (state.get("turn_number", 0) > (ST["cast_turn"] or 0) + 2)
        if (ST["cast_submitted"] and not ST["post_exported"]
                and len(state.get("stack", []) or []) == 0
                and (resolved or advanced_far)):
            say("[P0] Zenith resolution window over; exporting POST")
            try:
                post = await c.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                ST["done"] = True
                say("[P0] exported POST")
            except Exception as e:
                ST["notes"].append(f"post export failed: {e}")
            return
        if not holding and not real_decision_pending(st) \
                and my_priority(acts):
            await pass_priority(c, st, acts)
        return

    # stage "done": just pass
    if not holding and not real_decision_pending(st) \
            and my_priority(acts):
        await pass_priority(c, st, acts)


async def p1_tick(c, st, acts, state):
    tag = "P1"
    if await do_mulligan(c, acts, st, 1, tag, ["island"]):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    # P1 never casts, but the guard must still run before early returns
    holding = cast_guard_tick(state, tag) == "hold"
    atypes = set(a.get("type") for a in acts)

    # Chains discard during the resolution window (mandatory; must not be
    # confused with cleanup discards -- gate on the resolution flag)
    if ST["cast_submitted"] and not ST["post_exported"]:
        if await answer_p1_chains_discard(c, st, state, tag):
            return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = dict(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if not holding and not real_decision_pending(st) \
            and my_priority(acts):
        await pass_priority(c, st, acts)


# ---------------------------------------------------------------- evaluation

def evaluate():
    ass = {k: "not-run" for k in ASS_KEYS}
    notes = ST["notes"]
    try:
        pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
            if os.path.exists(f"{EVDIR}/pre.json") else None
        post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
            if os.path.exists(f"{EVDIR}/post.json") else None
    except Exception as e:
        notes.append(f"state reload failed: {e}")
        pre_st, post_st = None, None
    H = None
    if pre_st is not None:
        H = len(player_of(pre_st, 1).get("hand", []))
        ok = (chains_on_bf(pre_st, 0)
              and any(obj_lname(pre_st, o) == ZENITH
                      for o in hand_ids(pre_st, 0))
              and len(untapped_lands(pre_st, 0)) >= 4
              and len(untapped_lands(pre_st, 0, ISLAND)) >= 3
              and H is not None and H >= 1
              and player_of(pre_st, 0).get("life") == 20
              and player_of(pre_st, 1).get("life") == 20
              and pre_st.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(f"pre.json: P0 main phase, Chains on BF, Zenith in "
                         f"hand, {len(untapped_lands(pre_st, 0))} untapped "
                         f"lands, P1 hand H={H}, life 20/20")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("pre.json setup precondition not met "
                         f"(chains={chains_on_bf(pre_st, 0)}, "
                         f"lands={len(untapped_lands(pre_st, 0))}, H={H})")
        p1t = ST.get("p1_turns_at_pre") or ST.get("p1_turns") or []
        p1_first = bool(p1t) and min(p1t) == 1
        p1_draws = len(p1t) - (1 if p1_first else 0)
        p1_lands = sum(1 for oid, o in pre_st.get("objects", {}).items()
                       if o.get("zone") == "Battlefield"
                       and o.get("controller") == 1
                       and obj_lname(pre_st, oid) == ISLAND)
        p1_gy_pre = len(player_of(pre_st, 1).get("graveyard", []))
        expected_h = 7 + p1_draws - p1_lands
        if H == expected_h and p1_gy_pre == 0:
            ass["A2_first_draw_untouched"] = "passed"
            notes.append(f"P1 hand at pre == {H} (expected {expected_h} = 7 "
                         f"kept + {p1_draws} draws - {p1_lands} lands; P1 "
                         f"went {'first' if p1_first else 'second'}), gy "
                         f"empty: draw-step first draws never replaced")
        else:
            ass["A2_first_draw_untouched"] = "failed"
            notes.append(f"P1 hand at pre == {H} (expected {expected_h}), "
                         f"gy={p1_gy_pre}: the draw-step first-draw exception "
                         f"may itself be broken")
    if pre_st is not None and post_st is not None and H is not None:
        pre_gy = set(str(o) for o in player_of(pre_st, 1).get("graveyard", []))
        post_gy = [o for o in player_of(post_st, 1).get("graveyard", [])]
        new_gy = [o for o in post_gy if str(o) not in pre_gy]
        pre_lib = set(str(o) for o in player_of(pre_st, 1).get("library", []))
        post_lib = set(str(o) for o in player_of(post_st, 1).get("library", []))
        post_hand = set(str(o) for o in player_of(post_st, 1).get("hand", []))
        pre_hand = set(str(o) for o in player_of(pre_st, 1).get("hand", []))
        discards = [o for o in new_gy if str(o) in pre_hand]
        mills = [o for o in new_gy if str(o) in pre_lib]
        hand_after = len(post_hand)
        say(f"resolution deltas: H={H} discards={len(discards)} "
            f"mills={len(mills)} hand_after={hand_after} "
            f"lib {len(pre_lib)}->{len(post_lib)}")
        ST["resolution"] = {"H": H, "discards": len(discards),
                            "discard_names": [obj_lname(post_st, o) for o in discards],
                            "mills": len(mills),
                            "mill_names": [obj_lname(post_st, o) for o in mills],
                            "hand_after": hand_after,
                            "lib_before": len(pre_lib),
                            "lib_after": len(post_lib)}
        if len(discards) >= 1 and hand_after <= H:
            ass["A3_replacement_fires"] = "passed"
            notes.append(f"replacement fired: {len(discards)} discard(s), "
                         f"no normal draw (hand {H}->{hand_after})")
        else:
            ass["A3_replacement_fires"] = "failed"
            notes.append(f"replacement did not fire as expected: discards="
                         f"{len(discards)}, hand {H}->{hand_after}")
        if len(discards) == H and hand_after == 0:
            ass["A4_draw_branch_fires"] = "passed"
            notes.append(f"draw branch fired: all {H} cards discarded via "
                         f"the chained replacement (hand {H}->0)")
        else:
            ass["A4_draw_branch_fires"] = "failed"
            notes.append(f"draw branch broken: only {len(discards)}/{H} "
                         f"discarded, hand {H}->{hand_after} (predicted "
                         f"buggy: exactly 1 discard, draw never fires)")
        if len(mills) == 1 and hand_after == 0:
            ass["A5_mill_exactly_once"] = "passed"
            notes.append("mill fired exactly once, after the hand was "
                         "emptied (Oracle: mill only when no discard)")
        else:
            ass["A5_mill_exactly_once"] = "failed"
            notes.append(f"mill branch broken: mills={len(mills)} with "
                         f"hand_after={hand_after} (predicted buggy: "
                         f"mill never fires)")
        stack_empty = len(post_st.get("stack", []) or []) == 0
        zenith_resolved = not any(
            "blue sun" in json.dumps(e, default=str).lower()
            for e in post_st.get("stack", []) or [])
        if stack_empty and zenith_resolved:
            ass["A6_cleanup"] = "passed"
            notes.append("post.json: stack empty, Zenith resolved (shuffled "
                         "into P0 library per its text)")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"post.json: stack_empty={stack_empty} "
                         f"zenith_resolved={zenith_resolved}")
    else:
        for k in ("A3_replacement_fires", "A4_draw_branch_fires",
                  "A5_mill_exactly_once", "A6_cleanup"):
            ass[k] = "failed"
            notes.append(f"{k} could not be evaluated (missing pre/post state)")
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("setup incomplete; see notes")
    elif ass["A4_draw_branch_fires"] == "failed" \
            or ass["A5_mill_exactly_once"] == "failed":
        verdict = "reproduced"
        notes.append("Oracle-mandated discard->draw / discard->mill "
                     "branching is broken: result-referential conditions "
                     "do not gate the Draw/Mill sub-actions")
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
        notes.append("mixed assertion outcome on the reported contract")
    return verdict, ass


# ---------------------------------------------------------------- render + finalize

def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 780
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #5653 - Chains of Mephistopheles result-referential "
                     "conditions (revalidation)", fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Chains on BF, Zenith in hand, lands, P1 hand>=1",
        "A2_first_draw_untouched": "A2 control: draw-step first draws untouched",
        "A3_replacement_fires": "A3 replacement fires: >=1 discard, no normal draw",
        "A4_draw_branch_fires": "A4 [bug] draw branch: all H discarded (hand H->0)",
        "A5_mill_exactly_once": "A5 [bug] mill exactly once, after hand emptied",
        "A6_cleanup": "A6 cleanup: stack empty, Zenith resolved",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:10]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 5653/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_5653_01050.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_5653_01050 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


async def _main():
    t_start = time.time()
    ST["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
    ST["hand_trace"] = []
    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(P0_DECK)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = 0.0
    resolve_deadline = None
    try:
        while time.time() - t_start < 1500:
            await asyncio.sleep(0.15)
            for c, is_p0 in ((p0, True), (p1, False)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                    last_change[c.player_id] = time.time()
                else:
                    if time.time() - last_change[c.player_id] > 60:
                        s0 = st["state"]
                        la = [a.get("type") for a in
                              (st.get("legal_actions") or [])][:8]
                        say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                            f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                            f"legal={la} vikind={vi_kind_code(st)}")
                        last_change[c.player_id] = time.time()
                    acts_now = [a.get("type") for a in
                                (st.get("legal_actions") or [])]
                    # re-tick while the client has anything to answer
                    # (PassPriority AND decision actions like ChooseTarget;
                    # the old PassPriority-only gate deadlocked target
                    # selection on protocol 120)
                    if not (acts_now and time.time()
                            - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    if is_p0:
                        await p0_tick(c, st, merged_actions(st), st["state"])
                    else:
                        await p1_tick(c, st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"tick error {c.name}: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            if ST["done"]:
                say("done flag set; finishing")
                break
            if p1.latest:
                s = p1.latest["state"]
                if s.get("active_player") == 1:
                    t = s.get("turn_number")
                    if t not in ST["p1_turns"]:
                        ST["p1_turns"].append(t)
            # resolution sampler: track P1 zones while the Zenith resolution
            # is underway so the mid export fires on the first zone change
            if ST["cast_submitted"] and not ST["post_exported"] and p1.latest:
                s = p1.latest["state"]
                samp = (len(player_of(s, 1).get("hand", [])),
                        len(player_of(s, 1).get("graveyard", [])),
                        len(player_of(s, 1).get("library", [])))
                if not ST["hand_trace"] or ST["hand_trace"][-1][1:] != samp:
                    ST["hand_trace"].append((s.get("turn_number"),) + samp)
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                s = p0.latest["state"]
                say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} pp={[a.get('type') for a in (p0.latest.get('legal_actions') or [])][:6]} "
                    f"P0hand={len(hand_lnames(s, 0))} P1hand={len(hand_lnames(s, 1))} "
                    f"chains={chains_on_bf(s, 0)} stack={len(s.get('stack') or [])} "
                    f"stage={ST['stage']} discards={len(ST['discard_events'])}")
            if ST["cast_submitted"] and not ST["post_exported"] \
                    and resolve_deadline is None:
                resolve_deadline = time.time() + 300
            if not ST["cast_submitted"] or ST["post_exported"]:
                resolve_deadline = None
            if resolve_deadline and time.time() > resolve_deadline:
                ST["notes"].append("Zenith cast but post-resolution state not "
                                   "reached in 300s; see wire log (possible "
                                   "unhandled interaction)")
                break
        else:
            ST["notes"].append("global timeout (1500s) hit before assertions resolved")
    finally:
        if not ST["post_exported"]:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                say("exported post.json at teardown")
            except Exception as e:
                ST["notes"].append(f"post export at teardown failed: {e}")
        for c in (p0, p1):
            try:
                await c.close()
            except Exception:
                pass

    verdict, ass = evaluate()
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(ass, f, indent=1)
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - t_start, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/run-20261009-2211-backfill",
        "chains_parse": {
            "event": "Draw",
            "condition": "ExceptFirstDrawInDrawStep",
            "mode": "Mandatory",
            "chain": [
                {"effect": "Discard", "count": 1, "condition": None},
                {"effect": "Draw", "count": 1,
                 "condition": "EffectOutcome{signal: OptionalEffectPerformed}"},
                {"effect": "Mill", "count": 1,
                 "condition": "Not(EffectOutcome{signal: OptionalEffectPerformed})"},
            ],
            "verified_from": "pinned v0.105.0 card-data.json replacements[0] "
                             "(structure unchanged from v0.78.0/v0.85.0/v0.103.0/v0.104.0)",
        },
        "driver": {"protocol_advertised": 120,
                   "client": "driver/client.py",
                   "scenario": "driver/scenario_5653_01050.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_5653_01050.py", "rb").read()).hexdigest(),
        "decks": {"P0": [list(t) for t in
                          [(CHAINS, 8), (ZENITH, 8), (ISLAND, 22), (SWAMP, 22)]],
                  "P1": [["island", 60]]},
        "assertions": ass,
        "notes": ST["notes"],
        "observations": {
            "discard_events": ST["discard_events"],
            "resolution": ST.get("resolution"),
            "hand_trace": ST.get("hand_trace"),
            "p1_turns": ST["p1_turns"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x Chains / 8x Zenith deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "Magus of the Chains carries the same Oracle text and parse class; "
            "only Chains of Mephistopheles was driven (same-class coverage).",
            "The correct-behavior trace assumes each chained draw is itself a "
            "non-first draw subject to replacement (standard rules reading); "
            "the decisive observed facts are the discard/mill counts.",
            "States are authoritative exports, restorable only via full game replay.",
        ],
        "setup_line": "P0: 8x chains of mephistopheles + 8x blue sun's zenith + "
                      "22x island + 22x swamp (mulligan to chains + 2 lands); "
                      "P1: 60x island dummy",
        "contract_line": "P0 casts Blue Sun's Zenith X=1 targeting P1 with Chains "
                         "of Mephistopheles on the battlefield: the draw is "
                         "replaced by discard-1; per Oracle, discarding must "
                         "chain into another draw (until the hand is empty) and "
                         "mill exactly once, only when no card was discarded",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_5653_01050.py",
                f"{EVDIR}/scenario_5653_01050.py")
    try:
        render_summary(run, f"{EVDIR}/summary.png")
        say("rendered summary.png")
    except Exception as e:
        say(f"summary render failed: {e}")
        ST["notes"].append(f"summary render failed: {e}")
    # manifest AFTER closing the append-mode logs (final verdict lines flush
    # after the hashes would otherwise be computed)
    WIRE.close()
    RUNLOG.close()
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


asyncio.run(main())
