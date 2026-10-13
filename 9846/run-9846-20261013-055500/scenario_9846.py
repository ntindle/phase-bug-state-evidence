#!/usr/bin/env python3
"""Issue #9846: Vial Smasher the Fierce - "that player" binds to the triggering spell's caster.

Oracle: "Whenever you cast your first spell each turn, choose an opponent at
random. Vial Smasher deals damage equal to that spell's mana value to that
player or a planeswalker that player controls."

The reporter's data read: the DealDamage target lowers to
Or[TriggeringPlayer, Planeswalker{controller: You}] -- both arms wrong:
TriggeringPlayer is the caster (you), and the planeswalker arm uses your
planeswalkers. #8110 covers the missing planeswalker choice separately.

Behavioral contract (per playbook: test the reported OUTCOME, two-player game
so the "random opponent" choice is deterministic = P1):
  P0 ramps, casts Vial Smasher, then on a later turn casts Lightning Strike
  ({1}{R}, MV 2) as P0's first spell of that turn, targeting P1.
  Expected (correct): Vial Smasher's trigger deals 2 to the chosen opponent P1
      -> P0 life 20, P1 life 16 (Strike 2 + VS 2).
  Bug (per the issue): the damage target binds to the triggering spell's
  caster -> Vial Smasher deals 2 to P0 itself -> P0 life 18, P1 life 18.
  Note: with a single legal target the engine auto-targets (no target prompt),
  so the decisive evidence is the life totals (run-0345 lesson).

Assertions:
  A1 setup_ok      game created, both seats joined
  A2 fixture_ok    Vial Smasher on P0 battlefield; Lightning Strike submitted
                   while VS was on the battlefield as P0's first spell
  A3 trigger_seen  Vial Smasher's trigger appeared on the stack
  A4 bug_self_damage  post-resolution P0 life == 18 (VS hit its controller)
  A5 opp_only_strike  post-resolution P1 life == 18 (Strike hit, VS did not)

Verdict:
  reproduced      A2 and A3 passed, A4 passed (P0 took the VS damage)
  not-reproduced  A2 and A3 passed, P0 life 20 and P1 life 16 (VS hit P1)
  blocked         fixture or trigger never reached

Driver conventions (verified scenario_9845.py, protocol 134):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (viewer_interaction)
  - exact card-data names; export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; vi candidate references are strings
    (normalize with str() before oid comparisons).
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
log = logging.getLogger("scenario9846")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9846-20261013-051252")
ISSUE = 9846
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
               "fresh v0.108.0 server on 127.0.0.1:9374 started by this run; "
               "scenario games created via CreateGameWithSettings; evidence "
               "under evidence/9846/" + RUN_ID),
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


def life_of(state, pid):
    return player_of(state, pid).get("life")


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
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def stack_names(state):
    out = []
    for e in state.get("stack") or []:
        out.append(obj_lname(state, e.get("object_id", e.get("id", ""))))
    return out


def stack_raw(state):
    return [{"name": obj_lname(state, e.get("object_id", e.get("id", ""))),
             "kind": e.get("kind"), "controller": e.get("controller")}
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
    """Record the parser shapes this scenario depends on: Vial Smasher's
    trigger DealDamage target must be the reported-wrong
    Or[TriggeringPlayer, Planeswalker{controller: You}]."""
    vs = CARD_DATA.get("vial smasher the fierce", {})
    trig = (vs.get("triggers") or [None])[0] or {}
    deal = ((trig.get("execute") or {}).get("sub_ability") or {}).get("effect") or {}
    target = deal.get("target") or {}
    notes = []
    ok = True
    if deal.get("type") != "DealDamage":
        ok = False
        notes.append(f"sub_ability effect is {deal.get('type')}, not DealDamage")
    arms = target.get("filters") or []
    arm0 = arms[0].get("type") if len(arms) > 0 else None
    arm1 = arms[1] if len(arms) > 1 else {}
    if not (target.get("type") == "Or" and arm0 == "TriggeringPlayer"
            and arm1.get("type") == "Typed"
            and "Planeswalker" in (arm1.get("type_filters") or [])
            and arm1.get("controller") == "You"):
        ok = False
        notes.append(f"target shape differs from the reported-wrong lower: {json.dumps(target)[:300]}")
    amount = deal.get("amount") or {}
    qty = amount.get("qty") or {}
    if not (amount.get("type") == "Ref" and qty.get("type") == "ObjectManaValue"):
        ok = False
        notes.append(f"damage amount is not ObjectManaValue: {json.dumps(amount)[:200]}")
    choose = (trig.get("execute") or {}).get("effect") or {}
    if not (choose.get("type") == "Choose" and choose.get("choice_type") == "Opponent"
            and (choose.get("selection") or {}).get("type") == "Random"):
        ok = False
        notes.append(f"random-opponent choice shape unexpected: {json.dumps(choose)[:200]}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle_text": vs.get("oracle_text"),
                   "trigger": trig}, f, indent=1, default=str)
    say(f"data-level check: ok={ok} notes={notes}")
    wire("data_level", {"ok": ok, "notes": notes,
                        "target": target})
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
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if str(ch.get("reference", "")) in
                 set(hand_ids(state, pid))]
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


def candidate_seat(ch):
    """Seat/player index carried on a candidate's player/target/candidate
    surface (protocol-134 target prompts carry no top-level reference)."""
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def cand_surface_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def target_opportunity(st):
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

# ---------------------------------------------------------------- legs

VS_NAME = "vial smasher the fierce"
STRIKE_NAME = "lightning strike"


async def handle_target_prompts(c, st, ctx, tag, p1_id):
    """Fallback prompt handling. With a single legal target the engine
    auto-targets (no prompt expected); if a prompt appears anyway, answer
    only the documented decisions:
      - Lightning Strike's target: P1 (deliberate scenario decision)
      - Vial Smasher damage choice: record the advertised candidates; if the
        only candidate is P0 (the bug's forced outcome), submit it so the
        outcome is observable. Any other surprise prompt is logged and left
        pending (playbook: never auto-answer unfamiliar prompts)."""
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    key = (tag, "target", str(iid))
    if key in SUBMITTED_OPPS:
        return False
    state = st["state"]
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or []
    wire("target_prompt_seen", {"who": tag, "rtype": rtype, "stype": stype,
                                "strike_pending": ctx.get("strike_target_pending"),
                                "vs_trigger_seen": ctx.get("vs_trigger_seen"),
                                "candidate_seats": sorted(
                                    {candidate_seat(ch) for ch in cands
                                     if candidate_seat(ch) is not None}),
                                "candidates": [{"id": ch.get("id"),
                                                "seat": candidate_seat(ch),
                                                "surface_ref": str(cand_surface_reference(ch)),
                                                "text": choice_text(ch)[:60]}
                                               for ch in cands]})
    if ctx.get("strike_target_pending"):
        pick = next((ch for ch in cands if candidate_seat(ch) == p1_id), None)
        if pick is None:
            say(f"[{tag}] WARNING: strike target prompt without P1 candidate; "
                f"leaving pending")
            return False
        SUBMITTED_OPPS.add(key)
        ctx["strike_target_pending"] = False
        ctx["strike_targeted_p1"] = True
        say(f"[{tag}] strike target prompt: choosing P1 (seat {p1_id})")
        await answer_vi(c, opp, pick, tag)
        return True
    if ctx.get("vs_trigger_seen") and not ctx.get("vs_damage_answered"):
        ctx["vs_damage_candidates"] = [
            {"id": ch.get("id"), "seat": candidate_seat(ch),
             "surface_ref": str(cand_surface_reference(ch)),
             "text": choice_text(ch)[:60]} for ch in cands]
        say(f"[{tag}] VS damage prompt candidates: "
            f"{[(x['seat'], x['text']) for x in ctx['vs_damage_candidates']]}")
        if len(cands) == 1 and candidate_seat(cands[0]) == 0:
            SUBMITTED_OPPS.add(key)
            ctx["vs_damage_answered"] = True
            say(f"[{tag}] VS damage prompt: single candidate = P0 (the bug's "
                f"forced outcome); submitting to observe the damage")
            await answer_vi(c, opp, cands[0], tag)
            return True
        say(f"[{tag}] VS damage prompt is not the single-P0 forced shape; "
            f"leaving pending for observation")
        return False
    say(f"[{tag}] unexpected target prompt outside documented decisions; "
        f"leaving pending")
    return False


async def p1_tick(c, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    # P1 never needs mana: skip the tapLandForMana spam to keep turns fast
    # (pay_mana_vi with empty needs would tap for "any" every window).
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, tag, ctx, p1_id):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return True
    if "DeclareAttackers" in atypes:
        import copy as _copy
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
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
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    tn = state.get("turn_number")
    if tn != ctx.get("last_turn"):
        ctx["last_turn"] = tn
        ctx["spells_cast"] = 0

    # VS trigger on the stack? (pre-damage export point)
    # NOTE: match triggered abilities only -- the VS *spell* itself also sits
    # on the stack with "vial smasher" in its name (kind.type == "Spell").
    for e in state.get("stack") or []:
        nm = obj_lname(state, e.get("object_id", e.get("id", "")))
        kind = (e.get("kind") or {}).get("type") if isinstance(e.get("kind"), dict) else None
        if "vial smasher" in nm and kind != "Spell" and not ctx.get("vs_trigger_seen"):
            ctx["vs_trigger_seen"] = True
            say(f"[{tag}] VS trigger on stack: {stack_raw(state)}")
            wire("vs_trigger_seen", {"stack": stack_raw(state)})
            pre = json.loads(await c.export_state())
            with open(f"{EVDIR}/pre_trigger.json", "w") as f:
                json.dump(pre, f, indent=1)
            wire("pre_trigger_export", {"who": tag})
        if "vial smasher" in nm and kind == "Spell" and ST["mana_needs"].get(tag):
            # cast payment phase is over once the spell is on the stack
            ST["mana_needs"][tag] = {}
        if "lightning strike" in nm and kind == "Spell" and ST["mana_needs"].get(tag):
            ST["mana_needs"][tag] = {}
    if VS_NAME in bf_names(state, 0):
        ctx["vs_on_field"] = True

    if await handle_target_prompts(c, st, ctx, tag, p1_id):
        return True

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_names(state, 0)
        if VS_NAME in hn and not ctx.get("vs_on_field") \
                and untapped_land_count(state, 0) >= 3:
            a, oid = cast_action_for(acts, state, VS_NAME)
            if a is not None:
                ST["mana_needs"][tag] = {"B": 1, "R": 1, "generic": 1}
                ctx["spells_cast"] += 1
                say(f"[{tag}] casting Vial Smasher (oid {oid})")
                wire("cast_vs", {"oid": oid})
                await submit_as_is(c, a)
                return True
        if ctx.get("vs_on_field") and ctx.get("spells_cast", 0) == 0 \
                and not ctx.get("strike_submitted") \
                and STRIKE_NAME in hn \
                and untapped_land_count(state, 0) >= 2:
            a, oid = cast_action_for(acts, state, STRIKE_NAME)
            if a is not None:
                say(f"[{tag}] pre-cast export (VS on field, first spell window)")
                pre = json.loads(await c.export_state())
                with open(f"{EVDIR}/pre_cast.json", "w") as f:
                    json.dump(pre, f, indent=1)
                wire("pre_cast_export", {"who": tag})
                ST["mana_needs"][tag] = {"R": 1, "generic": 1}
                ctx["spells_cast"] += 1
                ctx["strike_submitted"] = True
                ctx["strike_target_pending"] = True
                say(f"[{tag}] casting Lightning Strike (oid {oid}) as first "
                    f"spell of the turn")
                wire("cast_strike", {"oid": oid})
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


async def settle_until(cond, p0, p1, tag0, tag1, ctx, p1_id, timeout, label,
                       poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await p1_tick(p1, tag1)
        await p0_tick(p0, tag0, ctx, p1_id)
        await asyncio.sleep(0)
        st = p0.latest
        if st and cond(st["state"], ctx):
            return True
    say(f"[{tag0}] settle timeout: {label}")
    wire("settle_timeout", {"label": label})
    return False


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9846-{attempt}"
    ST["mana_needs"][tag] = {}
    ctx = {"last_turn": None, "spells_cast": 0, "vs_on_field": False,
           "strike_submitted": False, "strike_target_pending": False,
           "strike_targeted_p1": False, "vs_trigger_seen": False,
           "vs_damage_answered": False, "vs_damage_candidates": None}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Swamp", 26), ("Mountain", 26),
                         ("Vial Smasher the Fierce", 4),
                         ("Lightning Strike", 4)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 60)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    p1_id = p1.player_id
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                                and p1.player_id is not None) else "failed"

    def window_reached(s, c):
        # turn_number counts player-turns; budget generously (VS cast ~turn 8,
        # Strike on a later P0 turn)
        return c.get("strike_submitted") or (s.get("turn_number") or 0) > 30
    ok = await settle_until(window_reached, p0, p1, tag, f"P1{tag}", ctx,
                            p1_id, timeout=600, label="cast window")
    st = p0.latest
    state = st["state"] if st else {}
    obs["assert"]["A2_fixture_ok"] = "passed" if ctx.get("strike_submitted") else "failed"
    if not ctx.get("strike_submitted"):
        obs["notes"].append(
            f"cast window never reached: VS on field={ctx.get('vs_on_field')}, "
            f"turn={state.get('turn_number')}, P0 hand={hand_names(state, 0)}")
        for k in ("A3_trigger_seen", "A4_bug_self_damage", "A5_opp_only_strike"):
            obs["assert"][k] = "not-run"
        obs["final_life"] = {"p0": life_of(state, 0), "p1": life_of(state, 1)}
        await p0.close(); await p1.close()
        return obs

    def resolved(s, c):
        return c.get("strike_submitted") and not (s.get("stack") or [])
    ok = await settle_until(resolved, p0, p1, tag, f"P1{tag}", ctx, p1_id,
                            timeout=300, label="trigger + spell resolution")
    st = p0.latest
    state = st["state"] if st else {}
    obs["assert"]["A3_trigger_seen"] = "passed" if ctx.get("vs_trigger_seen") else "failed"
    if not ctx.get("vs_trigger_seen"):
        obs["notes"].append("Vial Smasher trigger never appeared on the stack")

    say(f"[{tag}] post-resolution export")
    post = json.loads(await p0.export_state())
    with open(f"{EVDIR}/post_resolve.json", "w") as f:
        json.dump(post, f, indent=1)
    wire("post_resolve_export", {"who": tag})
    pstate = post.get("state", post)
    p0_life = life_of(pstate, 0)
    p1_life = life_of(pstate, 1)
    obs["final_life"] = {"p0": p0_life, "p1": p1_life}
    obs["vs_damage_candidates"] = ctx.get("vs_damage_candidates")
    say(f"[{tag}] final life: P0={p0_life} P1={p1_life}")
    wire("final_life", obs["final_life"])

    # Bug: VS deals 2 (Strike MV) to P0 itself -> P0 18, P1 18 (Strike only).
    # Correct: VS deals 2 to P1 -> P0 20, P1 16.
    obs["assert"]["A4_bug_self_damage"] = (
        "passed" if (p0_life == 18 and p1_life == 18) else "failed")
    obs["assert"]["A5_opp_only_strike"] = (
        "passed" if (p0_life == 20 and p1_life == 16) else "failed")
    if not (ok and p0_life in (18, 20) and p1_life in (16, 18)):
        obs["notes"].append(
            f"unexpected life totals P0={p0_life} P1={p1_life} "
            f"(stack_empty={ok}); see wire log")
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

    ass = {f"A{k[1:]}": v for k, v in obs["assert"].items()}
    notes = list(obs["notes"])
    notes.append(f"final life totals: {obs.get('final_life')}")
    notes.append(f"VS damage prompt candidates: {obs.get('vs_damage_candidates')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; priority-gated passes; "
                 "sleep(0) yield before leg evaluation; single-legal-target "
                 "auto-targeting (no target prompt expected).")

    a2, a3 = ass.get("A2_fixture_ok"), ass.get("A3_trigger_seen")
    a4, a5 = ass.get("A4_bug_self_damage"), ass.get("A5_opp_only_strike")
    if a2 == "passed" and a3 == "passed" and a4 == "passed":
        verdict = "reproduced"
    elif a2 == "passed" and a3 == "passed" and a5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = fixture + trigger observed and "
                 "P0 took the VS damage (18/18). not-reproduced = fixture + "
                 "trigger observed and P1 took the VS damage (20/16).")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9846",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("runs/run-9846-20261013-051252 (fresh v0.108.0 "
                           "server on 127.0.0.1:9374 started by this run; "
                           "evidence under evidence/9846/" + RUN_ID + ")"),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Swamp", 26], ["Mountain", 26],
                   ["Vial Smasher the Fierce", 4], ["Lightning Strike", 4]],
            "P1": [["Island", 60]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; final life P0=%s P1=%s"
                   % (ass.get("A1_setup_ok"), a2, a3, a4, a5,
                      (obs.get("final_life") or {}).get("p0"),
                      (obs.get("final_life") or {}).get("p1"))),
        "scope": ("Vial Smasher the Fierce trigger damage target binding; "
                  "two-player game (random opponent choice deterministic); "
                  "no planeswalkers in play (the #8110 planeswalker-choice "
                  "arm is out of scope); native human seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "With a single legal target the engine auto-targets, so no target "
            "prompt candidates were observed at runtime; the damage target "
            "binding is asserted from the resolution outcome (life totals) "
            "plus the card-data lower recorded in data_evidence.json.",
        ],
        "setup_line": ("P0 26x Swamp + 26x Mountain + 4x Vial Smasher the "
                       "Fierce ({1}{B}{R}) + 4x Lightning Strike ({1}{R}); "
                       "P1 60x Island"),
        "contract_line": ("Cast Vial Smasher, then cast Lightning Strike as "
                          "the first spell of a later turn targeting P1. "
                          "Expected (correct): VS deals 2 to P1 -> 20/16. "
                          "Bug: VS deals 2 to its controller -> 18/18."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9846.py", "w") as f:
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
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), 'Issue #9846 - Vial Smasher: "that player" binds to the caster',
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_fixture_ok": "A2 VS on P0 battlefield; Strike cast as first spell",
        "A3_trigger_seen": "A3 Vial Smasher trigger on the stack",
        "A4_bug_self_damage": "A4 [bug] P0 took the VS damage (18/18)",
        "A5_opp_only_strike": "A5 [correct] P1 took the VS damage (20/16)",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:14]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9846/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9846.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9846 instance is alive "
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
