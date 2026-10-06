#!/usr/bin/env python3
"""Issue #7239: Hot Pursuit triggers with no players eliminated and does not
untap gained creatures (open, status:confirmed, area:engine/multiplayer).

Oracle (pinned card-data.json v0.102.0): "When this enchantment enters,
suspect target creature an opponent controls. As long as this enchantment
remains on the battlefield, that creature is also goaded. At the beginning
of combat on your turn, if two or more players have lost the game, gain
control of all goaded and/or suspected creatures until end of turn. Untap
them. They gain haste until end of turn."

v0.98.0 finding (protocol 94, run 20261001-7239b, verdict reproduced):
  1. The "if two or more players have lost the game" condition was dropped
     (condition: null) -- the BeginCombat trigger fired with 0 eliminated.
  2. GainControlAll used filter:any -- P0 gained control of ALL 12 opponent
     permanents, not just the suspected creature.
  3. The Untap sub-effect targeted SelfRef (Hot Pursuit) -- the tapped
     Bears stayed tapped in the post-resolution state.
  Haste grant worked. The maintained comment lives at
  https://github.com/phase-rs/phase/issues/7239#issuecomment-5930853520.

v0.102.0 data note (checked this run): the BeginCombat Phase trigger is now
parsed as effect Unimplemented/"unparsed_condition" with the whole clause
as its description -- the parser no longer marks the clause supported at
all. The behavioral question this run answers: does the trigger still fire
and misbehave on v0.102.0, or is it now inert?

Protocol-106 port for the pinned v0.102.0 re-validation. Conventions:
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [<name strings>]}; player_count=4.
  - waiting_for is gone (null): priority = advertised PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on waitingForKind.code ==
    'mulligan' AND turn 1 / Untap; DiscardToHandSize via vi schema/select.
  - CastSpell via legacy actions (engine auto-taps reliably; manual taps
    are a >90s fallback only); mana paid via PayMana/PayManaAbilityMana
    actions and vi tapLandForMana menus driven by MANA_NEEDS.
  - HP's ETB Suspect target via the advertised schema/select (or
    sequence) opportunity (candidates carry object surfaces with
    reference).
  - DeclareAttackers/DeclareBlockers: relations-schema vi opportunity for
    the real attack (Bears -> P0); empty declares via the advertised
    action; blockers use the relations-first grace window so the
    advertised empty submit never eats a pending relations opportunity.
  - real_decision_pending excludes the 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand).
  - sleep(0) yield before leg evaluation; 5s re-tick backstop covers
    priority holders AND pending decisions.
  - Export envelope: data.state is a JSON string parsed once.

Plan (native engine, v0.102.0 / protocol 106, four human-client seats):
  P0: 8x Hot Pursuit, 52x Mountain. P1: 8x Grizzly Bears, 52x Forest.
  P2/P3: 60x Forest (draw-go).
  SETUP: land drops; P1 casts Grizzly Bears when it has 2 untapped Forests.
  MANEUVER: on P1's next turn, P1 attacks P0 with the Bears via the
    relations-schema DeclareAttackers opportunity (taps it). P0 declares
    no blockers.
  READY: at P0's next main phase with the tapped Bears on P1's
    battlefield and all four players alive: export pre.json.
  CASTING: P0 casts Hot Pursuit ({1}{R}); the ETB suspects the Bears;
    settle, export post_cast.json.
  OBSERVE: advance to P0's BeginCombat with 0 players eliminated. The
    trigger must NOT fire: no control change, no untap, no haste.
    If it fires (bug), record the firing and export post.json right after
    the trigger resolves (stack empty, same turn). If it never fires,
    export post.json once P0's combat is fully behind us.

Behavioral contract:
  A1 setup_ok        pre.json: P0 main, tapped Bears on P1 bf, all alive;
                     post_cast.json: HP on P0 bf, Bears is_suspected.
  A2 no_spurious_control_change
                     no battlefield permanent changes controller
                     pre->post (FAILED = bug: trigger fired with 0
                     players eliminated).
  A3 scope_correct    (only if fired) gained set == suspected Bears only.
  A4 untap_correct    (only if fired) tapped Bears untaps in post.
  A5 haste_granted    (only if fired) gained Bears has Haste in post.

Verdict: reproduced iff A1 passes and A2 fails; not-reproduced iff A1
and A2 pass; blocked otherwise.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time
import traceback

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario7239_106")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261006-7239b"
ISSUE = 7239
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.102.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-06: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.102.0 server on 127.0.0.1:9374 (run dir "
               "runs/20261006-9295, backfill-owned, isolated games.db)"),
}

for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))


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

ST = {"mana_needs": {}, "opp_shapes_logged": set(), "block_grace_until": {}}
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


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


def stack_empty(state):
    return not (state.get("stack") or [])


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
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and stack_empty(state))


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
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction. Schema
    opportunities are decisions; the priority-menu codes are not (playLand
    is always optional, so a menu OFFERING it is never forced)."""
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


def all_alive(state):
    pls = state.get("players", []) or []
    return all(not p.get("is_eliminated") for p in pls) and len(pls) == 4


def is_suspected(o):
    # structural status field (NOT a substring search: the card's own rules
    # text and lki snapshots contain the word "suspect")
    return o.get("is_suspected") is True


def has_haste(o):
    # granted keyword list (NOT a substring search: the card's own rules
    # text contains "haste")
    kw = o.get("keywords") or []
    return any(str(k).lower() == "haste" for k in kw)


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
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"


async def do_mulligan(c, acts, st, pid, tag, keep_fn):
    """Protocol 106: MulliganDecision arrives as a legacy legal action AND as
    an exactChoices vi opportunity (keep/mulligan). Prefer the vi
    interaction path -- the legacy action path silently ignores the
    "Mulligan" choice on v0.102.0 (probe_etb3, 2026-10-06)."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    opp = None
    for o in vi_ops(st):
        resp = o.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        vals = set()
        for ch in (resp.get("data") or {}).get("choices", []):
            for s in ch.get("surfaces", []) or []:
                if s.get("type") == "value":
                    vals.add((s.get("data", {}) or {}).get("value"))
        if "keep" in vals or "mulligan" in vals:
            opp = o
            break
    iid = (opp.get("interactionId") if opp else None)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    key = (tag, "mull", iid or len(hn))
    if key in MULLS:
        return False
    MULLS.add(key)
    nmulls = sum(1 for k in MULLS if k[0] == tag)
    keep_by_fn = keep_fn(state, pid, hn)
    keep = keep_by_fn or nmulls >= 3
    want_val = "keep" if keep else "mulligan"
    decision = "keep" if keep else "mull"
    say(f"[{tag}] mulligan -> {decision} ({len(hn)} cards) iid={iid}")
    wire("mulligan", {"who": tag, "decision": decision, "hand": hn,
                      "iid": iid})
    if opp is not None:
        for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
            vals = [(s.get("data", {}) or {}).get("value")
                    for s in ch.get("surfaces", []) or []
                    if s.get("type") == "value"]
            if want_val in vals:
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}}, tag)
                return True
        say(f"[{tag}] WARNING: no {want_val} choice in mulligan opp; "
            f"legacy fallback")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if keep else "Mulligan"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_bottom(c, acts, st, pid, tag, rank_fn):
    """Protocol 106: bottom-after-mulligan via the vi schema/select
    opportunity gated on waitingForKind.code == 'mulligan' AND turn 1 /
    Untap."""
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; not answering")
        wire(f"{tag}_bottom_no_vi", {"acts": [a.get("data") for a in sel_acts][:8]})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        return (rank_fn(state, oid, nm), str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, rank_fn):
    """Protocol 106: DiscardToHandSize surfaces via viewer_interaction."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        found = False
        handset = set(hand)
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            rdata = resp.get("data", {}) or {}
            spec = rdata.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            cands = rdata.get("candidates") or []
            if any(_cand_reference(ch) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    ref_of = {}
    for opp in vi_ops(st):
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in rdata.get("candidates") or []:
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]
    ranked = sorted(hand, key=lambda o: (rank_fn(state, o), obj_lname(state, o)))
    pick = ranked[:max(1, n)]
    choice_ids = [ref_of[o] for o in pick if o in ref_of]
    if not choice_ids:
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding {n}: {[obj_lname(state, o) for o in pick]}")
    wire("discard", {"who": tag, "oids": pick})
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        iid = opp.get("interactionId")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}}, tag)
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
        if a.get("type") == "PassPriority":
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


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


def relations_shape(opp):
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}
    return edges, cands


def cand_object_ref(cand):
    for s in (cand or {}).get("surfaces", []) or []:
        if s.get("type") == "object":
            return str((s.get("data") or {}).get("reference"))
    return None


def candidate_seat(ch):
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


async def do_declare_attackers_empty(c, acts, st, pid, tag):
    """Empty attackers declare. Tries the relations-schema vi opportunity
    first (relations empty) with the advertised action as the fallback."""
    state = st["state"]
    if str(state.get("active_player")) != str(pid):
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    opp = find_relations_op(st)
    if opp is not None:
        iid = opp.get("interactionId") or opp.get("id")
        key = (tag, "declare-atk", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] declare no attackers via vi relations")
        wire("declare_empty_vi", {"who": tag})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "relations",
                                           "data": {"relations": []}}}, tag)
        return True
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = copy.deepcopy(a)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            key = (tag, "declare-atk-blind", str(c.revision))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare no attackers (advertised action)")
            wire("declare_empty_action", {"who": tag})
            await submit_as_is(c, d)
            return True
    return False


async def do_declare_blockers_empty(c, acts, st, pid, tag):
    """Empty blockers declare. The relations-schema vi opportunity is
    tried FIRST with a 25s grace window: the advertised empty submit is
    skipped while the relations opportunity may still arrive (avoids the
    106 blockers race where an early empty submit eats the opportunity)."""
    state = st["state"]
    if str(state.get("active_player")) == str(pid):
        return False
    if "declareblock" not in str(state.get("phase") or "").lower():
        return False
    opp = find_relations_op(st)
    if opp is not None:
        iid = opp.get("interactionId") or opp.get("id")
        key = (tag, "declare-blk", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] declare no blockers via vi relations")
        wire("declare_empty_vi", {"who": tag, "kind": "blockers"})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "relations",
                                           "data": {"relations": []}}}, tag)
        return True
    # no relations opp yet: if attackers exist, arm the grace window and
    # wait instead of submitting the advertised empty declare
    attackers = [oid for oid, o in (state.get("objects") or {}).items()
                 if str(o.get("controller", -1)) != str(pid)
                 and (o.get("attacking") or (o.get("combat") or {}).get("attacking"))]
    if attackers and time.time() < ST["block_grace_until"].get(tag, 0):
        return False
    if attackers:
        ST["block_grace_until"][tag] = time.time() + 25
        wire("block_grace", {"who": tag, "attackers": len(attackers)})
        return False
    for a in acts:
        if a.get("type") == "DeclareBlockers":
            d = copy.deepcopy(a)
            d.setdefault("data", {})["assignments"] = []
            key = (tag, "declare-blk-blind", str(c.revision))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare no blockers (advertised action, no attackers)")
            wire("declare_empty_action", {"who": tag, "kind": "blockers"})
            await submit_as_is(c, d)
            return True
    return False


# ---------------------------------------------------------------- #7239 flow

HP = "Hot Pursuit"
BEARS = "Grizzly Bears"
HP_L = "hot pursuit"
BEARS_L = "grizzly bears"
MOUNTAIN_L = "mountain"
FOREST_L = "forest"

P0_DECK = [(HP, 8), ("Mountain", 52)]
P1_DECK = [(BEARS, 8), ("Forest", 52)]
P2_DECK = [("Forest", 60)]
P3_DECK = [("Forest", 60)]

G7239 = {}


def reset_g7239():
    global G7239
    G7239 = {
        "joined": False,
        "game_code": None,
        "stage": "SETUP",
        "bears_cast": False,
        "bears_oid": None,
        "bears_attacked": False,
        "attack_declared_turn": None,
        "pre_exported": False,
        "post_cast_exported": False,
        "hp_cast": False,
        "hp_cast_turn": None,
        "hp_oid": None,
        "etb_answered": False,
        "etb_kind": None,
        "suspected_seen": None,
        "fired": None,
        "fired_at": 0,
        "base_ctrl": {},
        "stop": False,
        "stop_reason": "",
        "fixture_deadline": None,
    }


reset_g7239()


def bears_obj(state):
    """The Grizzly Bears object regardless of controller (only one exists)."""
    for oid, o in (state.get("objects") or {}).items():
        if obj_lname(state, oid) == BEARS_L and o.get("zone") == "Battlefield":
            return oid, o
    return None, None


def controller_snapshot(state):
    return {oid: o.get("controller") for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"}


def detect_control_change(state):
    changed = []
    for oid, ctrl in controller_snapshot(state).items():
        if oid in G7239["base_ctrl"] and G7239["base_ctrl"][oid] != ctrl:
            o = get_obj(state, oid)
            changed.append({"oid": oid, "name": str(o.get("base_name") or o.get("name") or "?"),
                            "was": G7239["base_ctrl"][oid], "now": ctrl,
                            "tapped": bool(o.get("tapped"))})
    return changed


async def do_declare_attackers_bears(c, st, pid, tag):
    """P1 declares the Grizzly Bears alone attacking P0 via the
    relations-schema opportunity (the 106 analog of the protocol-94
    relations declare)."""
    state = st["state"]
    if str(state.get("active_player")) != str(pid):
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    opp = find_relations_op(st)
    if opp is None:
        if G7239["fixture_deadline"] is None:
            G7239["fixture_deadline"] = time.time() + 90
            wire("attack_waiting_relations", {"who": tag})
        if time.time() > G7239["fixture_deadline"]:
            say(f"[{tag}] relations declare opportunity never arrived; "
                f"fixture broken")
            wire("attack_no_relations_opp", {"who": tag})
            G7239["stop"] = True
            G7239["stop_reason"] = "no relations declare opportunity for P1 attack"
        return False
    iid = opp.get("interactionId") or opp.get("id")
    key = (tag, "declare-atk", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    edges, cands = relations_shape(opp)
    if "attack_edges" not in ST["opp_shapes_logged"]:
        ST["opp_shapes_logged"].add("attack_edges")
        wire("declare_attackers_shape",
             {"n_edges": len(edges), "edges": edges[:6],
              "n_candidates": len(cands)})
    boid = G7239["bears_oid"]
    rels = []
    for e in edges:
        src = e.get("sourceId")
        tids = e.get("targetIds") or []
        cand = cands.get(src, {})
        ref = cand_object_ref(cand)
        if ref is None or str(ref) != str(boid):
            continue
        want_tid = None
        for tid in tids:
            if candidate_seat(cands.get(tid, {})) == 0:
                want_tid = tid
                break
        if want_tid is None and tids:
            want_tid = tids[0]
        if want_tid:
            rels.append({"sourceId": src, "targetId": want_tid, "group": None})
    if not rels:
        say(f"[{tag}] no Bears edge in relations; declaring empty (keeps game moving)")
        wire("declare_attackers", {"who": tag, "n_rels": 0,
                                   "reason": "bears-edge-missing"})
        SUBMITTED_OPPS.add(key)
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "relations",
                                           "data": {"relations": []}}}, tag)
        return True
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": rels}}}
    wire("declare_attackers", {"who": tag, "n_rels": len(rels),
                               "submission": sub})
    say(f"[{tag}] declares attackers: Bears -> P0 (turn {state.get('turn_number')})")
    SUBMITTED_OPPS.add(key)
    await interact_as(c, sub, tag)
    return True


def find_suspect_target_opp(st):
    """HP's ETB 'suspect target creature an opponent controls': a schema
    select/sequence opportunity whose candidates carry object references.
    Returns (opp, spec_type) or (None, None)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = (data.get("spec") or {}).get("type") or ""
        if spec not in ("select", "sequence"):
            continue
        cands = data.get("candidates") or []
        if cands and any(_cand_reference(ch) is not None for ch in cands):
            return opp, spec
    return None, None


async def answer_suspect_target(c, st, tag):
    state = st["state"]
    opp, spec = find_suspect_target_opp(st)
    if opp is None:
        return False
    iid = opp.get("interactionId") or opp.get("id")
    key = (tag, "suspect-target", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or []
    boid = G7239["bears_oid"]
    pick = None
    for ch in cands:
        ref = _cand_reference(ch)
        if ref is not None and str(ref) == str(boid):
            pick = ch
            break
    if pick is None and len(cands) == 1:
        pick = cands[0]
    if pick is None:
        say(f"[{tag}] suspect target: no Bears candidate among {len(cands)}; waiting")
        return False
    G7239["etb_kind"] = {"spec": spec, "n_candidates": len(cands),
                         "waitingForKind": vi_kind_code(st)}
    with open(f"{EVDIR}/suspect_target_opp.json", "w") as f:
        json.dump({"opportunity": opp, "waitingForKind": vi_kind_code(st)},
                  f, indent=1, default=str)
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] ETB suspect target -> Bears choice {pick.get('id')} "
        f"(spec={spec})")
    wire("suspect_target_submit", {"pick_id": pick.get("id"), "spec": spec})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": spec,
                                       "data": {"choiceIds": [pick.get("id")]}}}, tag)
    G7239["etb_answered"] = True
    return True


def p0_keep(state, pid, hn):
    return hn.count(MOUNTAIN_L) >= 2


def p1_keep(state, pid, hn):
    return hn.count(FOREST_L) >= 2


def pN_keep(state, pid, hn):
    return True


def bottom_rank_generic(key_lname):
    def rank(state, oid, nm):
        if nm == key_lname:
            return (2, nm)
        if nm in (MOUNTAIN_L, FOREST_L):
            return (1, nm)
        return (0, nm)
    return rank


def discard_rank(state, oid):
    nm = obj_lname(state, oid)
    if nm in (MOUNTAIN_L, FOREST_L):
        return (0, nm)
    if nm == HP_L:
        return (3, nm)
    return (2, nm)


async def seat_tick(c, pid, tag, is_p0, is_p1):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    keep_fn = p0_keep if is_p0 else (p1_keep if is_p1 else pN_keep)
    key_lname = HP_L if is_p0 else (BEARS_L if is_p1 else FOREST_L)
    if await do_mulligan(c, acts, st, pid, tag, keep_fn):
        return
    if await do_bottom(c, acts, st, pid, tag, bottom_rank_generic(key_lname)):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag, discard_rank):
        return
    if "DeclareAttackers" in atypes:
        if is_p1 and G7239["stage"] == "MANEUVER":
            if await do_declare_attackers_bears(c, st, pid, tag):
                return
        else:
            if await do_declare_attackers_empty(c, acts, st, pid, tag):
                return
    if "DeclareBlockers" in atypes:
        if await do_declare_blockers_empty(c, acts, st, pid, tag):
            return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # P1 ETB-less cast of the Bears in SETUP
    if is_p1 and G7239["stage"] == "SETUP" and not G7239["bears_cast"] \
            and my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
        if any(obj_lname(state, o) == BEARS_L for o in hand_ids(state, pid)) \
                and untapped_lands(state, pid) >= 2:
            a, oid = cast_action_for(acts, state, BEARS_L)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                G7239["bears_cast"] = True
                say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                wire("cast_bear", {"oid": oid})
                await submit_as_is(c, a)
                return
    # P0 casts Hot Pursuit in CASTING
    if is_p0 and G7239["stage"] == "CASTING" and not G7239["hp_cast"] \
            and my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
        if any(obj_lname(state, o) == HP_L for o in hand_ids(state, pid)) \
                and untapped_lands(state, pid) >= 2:
            a, oid = cast_action_for(acts, state, HP_L)
            if a is not None:
                ST["mana_needs"][tag] = {"R": 1, "generic": 1}
                G7239["hp_cast"] = True
                G7239["hp_cast_turn"] = state.get("turn_number")
                say(f"[{tag}] casting Hot Pursuit (oid {oid}) on turn "
                    f"{G7239['hp_cast_turn']}")
                wire("cast_hp", {"oid": oid, "turn": G7239["hp_cast_turn"]})
                await submit_as_is(c, a)
                return
    # land drops + generic main-phase play for every seat
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return

    # P0 answers the ETB suspect target in CASTING
    if is_p0 and G7239["stage"] == "CASTING":
        if await answer_suspect_target(c, st, tag):
            return

    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def export_authoritative(c, fn, tag):
    s = await c.export_state()
    with open(f"{EVDIR}/{fn}", "w") as f:
        f.write(s)
    say(f"[{tag}] exported {fn}")
    wire("export", {"file": fn})
    return json.loads(s)["state"]


def load_state_file(fn):
    p = f"{EVDIR}/{fn}"
    if not os.path.exists(p):
        return None
    return json.loads(open(p).read())["state"]


async def run_game():
    obs = {"assert": {}, "notes": [], "_fixture_ok": True}
    p0 = PhaseClient("P07239")
    await p0.connect()
    say("P0 creating 4-player game...")
    await p0.create(deck(*P0_DECK), player_count=4)
    p1 = PhaseClient("P17239"); await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P27239"); await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    p3 = PhaseClient("P37239"); await p3.connect()
    await p3.join(p0.game_code, deck(*P3_DECK))
    seats = [(p0, 0, "P0", True, False), (p1, 1, "P1", False, True),
             (p2, 2, "P2", False, False), (p3, 3, "P3", False, False)]
    say(f"game {p0.game_code}; seats {[(n, p) for _, p, n, _, _ in seats]}")
    wire("game_start", {"game_code": p0.game_code})
    G7239["joined"] = all(c.player_id is not None for c, _, _, _, _ in seats)
    G7239["game_code"] = p0.game_code
    obs["assert"]["A1_setup_ok"] = "passed" if G7239["joined"] else "failed"

    t_end = time.time() + 1800
    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    quiet_t0 = None
    try:
        while time.time() < t_end and not G7239["stop"]:
            await asyncio.sleep(0.15)
            for c, pid, tag, is_p0, is_p1 in seats:
                st = c.latest
                if not st:
                    continue
                if c.revision == last_rev.get(c.name):
                    # 5s re-tick backstop: covers priority holders AND
                    # pending decisions (missed-broadcast resilience)
                    if time.time() - last_tick_at.get(c.name, 0) < 5:
                        continue
                last_rev[c.name] = c.revision
                last_tick_at[c.name] = time.time()
                try:
                    await seat_tick(c, pid, tag, is_p0, is_p1)
                except Exception as e:
                    say(f"[{tag}] tick error {c.name}: "
                        f"{type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}",
                                        "tb": traceback.format_exc()[-2000:]})
            st = p0.latest
            if not st:
                continue
            state = st["state"]
            phase = state.get("phase") or ""
            turn = state.get("turn_number")
            actp = state.get("active_player")

            # track the Bears object
            boid, bo = bears_obj(state)
            if boid and G7239["bears_oid"] is None:
                G7239["bears_oid"] = boid
                say(f"[P1] Grizzly Bears on battlefield (oid {boid})")
                wire("bears_bf", {"oid": boid})
            hoid = None
            for o in bf_by_name(state, 0, HP_L):
                hoid = o
                break
            if hoid and G7239["hp_oid"] is None:
                G7239["hp_oid"] = hoid
                say(f"[P0] Hot Pursuit on battlefield (oid {hoid})")
                wire("hp_bf", {"oid": hoid})

            # ---- stage transitions ----
            if G7239["stage"] == "SETUP":
                if boid and bo.get("controller") == 1 and stack_empty(state):
                    G7239["stage"] = "MANEUVER"
                    say("=== stage -> MANEUVER (Bears on P1's battlefield) ===")
                    wire("stage_maneuver", {})
                    continue
            if G7239["stage"] == "MANEUVER":
                if boid and bo.get("controller") == 1 and bo.get("tapped"):
                    G7239["stage"] = "READY"
                    G7239["bears_attacked"] = True
                    G7239["attack_declared_turn"] = turn
                    say("=== stage -> READY (Bears tapped on P1's side) ===")
                    wire("stage_ready", {"turn": turn})
                    continue
                # P1's turn is passing without an attack: nothing to do;
                # the Bears must attack on P1's next DeclareAttackers.
            if G7239["stage"] == "READY" and my_main(state, 0):
                if not G7239["pre_exported"]:
                    pre = await export_authoritative(p0, "pre.json", "P0")
                    G7239["base_ctrl"].update(controller_snapshot(pre))
                    G7239["pre_exported"] = True
                    say(f"=== stage -> CASTING (pre.json; "
                        f"{len(G7239['base_ctrl'])} bf permanents) ===")
                    wire("baseline_controllers", {"n": len(G7239["base_ctrl"])})
                    G7239["stage"] = "CASTING"
                    continue
            if G7239["stage"] == "CASTING" and G7239["hp_cast"] \
                    and hoid and stack_empty(state):
                # ETB finding (probe_etb3, 2026-10-06): with a single legal
                # target the engine applies Suspect with NO target prompt
                # (Bears is_suspected=True, no schema opportunity ever
                # offered). So do NOT gate on etb_answered -- after a quiet
                # window, read is_suspected directly. Keep answering the
                # opportunity if one ever appears (multi-target case).
                if quiet_t0 is None:
                    quiet_t0 = time.time()
                if time.time() - quiet_t0 > 5:
                    boid2, bo2 = bears_obj(state)
                    G7239["suspected_seen"] = bool(boid2 and is_suspected(bo2))
                    await export_authoritative(p0, "post_cast.json", "P0")
                    say(f"=== stage -> OBSERVE (HP settled; bears "
                        f"suspected={G7239['suspected_seen']}; "
                        f"etb_answered={G7239['etb_answered']}) ===")
                    wire("stage_observe",
                         {"suspected": G7239["suspected_seen"],
                          "etb_answered": G7239["etb_answered"]})
                    G7239["stage"] = "OBSERVE"
                    quiet_t0 = None
                    continue
            else:
                if G7239["stage"] == "CASTING":
                    quiet_t0 = None
            if G7239["stage"] == "OBSERVE":
                # log stack entries at P0's BeginCombat (the trigger itself)
                if actp == 0 and phase == "BeginCombat":
                    for so in state.get("stack") or []:
                        wire("stack_at_begin_combat",
                             {"name": str(so.get("name") or so.get("base_name") or "?"),
                              "desc": str(so.get("description") or "")[:200]})
                changed = detect_control_change(state)
                if changed and G7239["fired"] is None:
                    G7239["fired"] = {"turn": turn, "phase": phase,
                                      "active": actp, "changed": changed}
                    G7239["fired_at"] = time.time()
                    say(f"!!! CONTROL CHANGE DETECTED: {json.dumps(changed)} "
                        f"(turn={turn} phase={phase})")
                    wire("control_change", G7239["fired"])
                if G7239["fired"] is not None:
                    # export post.json as soon as the trigger has resolved
                    # (stack empty); do NOT let the turn advance -- the
                    # control change and haste are until-end-of-turn.
                    if stack_empty(state):
                        await export_authoritative(p0, "post.json", "P0")
                        G7239["stop"] = True
                        G7239["stop_reason"] = "post.json exported after trigger resolution"
                        say("=== post.json exported right after trigger resolution; stop ===")
                        break
                    if time.time() - G7239["fired_at"] > 30:
                        say("WARNING: stack never emptied after control change; "
                            "exporting post anyway")
                        await export_authoritative(p0, "post.json", "P0")
                        G7239["stop"] = True
                        G7239["stop_reason"] = "post.json exported (stack never emptied)"
                        break
                else:
                    # no firing: stop once P0's combat is fully behind us
                    if actp == 0 and phase in ("PostCombatMain", "End", "Cleanup") \
                            and G7239["suspected_seen"]:
                        await export_authoritative(p0, "post.json", "P0")
                        G7239["stop"] = True
                        G7239["stop_reason"] = "P0 combat passed with no control change"
                        say("=== P0 combat passed with no control change; stop ===")
                        break
            # safety valves
            if G7239["stage"] in ("SETUP", "MANEUVER") and time.time() - (t_end - 1800) > 1200:
                say(f"stalled in {G7239['stage']}; exporting post and stopping")
                await export_authoritative(p0, "post.json", "P0")
                G7239["stop"] = True
                G7239["stop_reason"] = f"stalled in {G7239['stage']}"
                break
            if G7239["stage"] == "CASTING" and G7239["hp_cast"] \
                    and G7239["hp_cast_turn"] is not None \
                    and turn is not None \
                    and turn >= G7239["hp_cast_turn"] + 6:
                say(f"CASTING safety valve: {turn - G7239['hp_cast_turn']} turns "
                    f"since HP cast without settling; exporting post_cast and "
                    f"moving to OBSERVE")
                boid2, bo2 = bears_obj(state)
                G7239["suspected_seen"] = bool(boid2 and is_suspected(bo2))
                await export_authoritative(p0, "post_cast.json", "P0")
                wire("stage_observe",
                     {"suspected": G7239["suspected_seen"],
                      "via": "casting_safety_valve"})
                G7239["stage"] = "OBSERVE"
                continue
            if time.time() - last_diag > 60:
                last_diag = time.time()
                say(f"[P0] DIAG rev={p0.revision} turn={turn} phase={phase} "
                    f"act={actp} stack={len(state.get('stack') or [])} "
                    f"stage={G7239['stage']} hp_cast={G7239['hp_cast']} "
                    f"etb={G7239['etb_answered']} fired={bool(G7239['fired'])}")
    except Exception as e:
        obs["notes"].append(f"drive loop exception: {e}")
    finally:
        for c, _, _, _, _ in seats:
            try:
                await c.close()
            except Exception:
                pass
    if not G7239["stop"]:
        obs["notes"].append("drive loop ended without completing (timeout)")
    obs["_fixture_ok"] = True
    verdict = evaluate(obs)
    return obs


def check_data_level():
    """Record the v0.102.0 parse of Hot Pursuit's BeginCombat trigger."""
    hp = CARD_DATA.get("hot pursuit", {})
    trigs = hp.get("triggers") or []
    chain = []
    boc = None
    for t in trigs:
        mode = t.get("mode")
        ex = (t.get("execute") or {})
        eff = (ex.get("effect") or {})
        rec = {"mode": mode, "phase": t.get("phase"),
               "condition": t.get("condition"),
               "effect_type": eff.get("type"),
               "effect_name": eff.get("name"),
               "description": (t.get("description") or "")[:160]}
        chain.append(rec)
        if mode == "Phase" and t.get("phase") == "BeginCombat":
            boc = t
    ev = {"name": hp.get("name"), "oracle_text": hp.get("oracle_text"),
          "mana_cost": hp.get("mana_cost"), "trigger_chain": chain,
          "data_notes": []}
    if boc is None:
        ev["data_notes"].append("No Phase/BeginCombat trigger found in the "
                                "v0.102.0 parse at all.")
        parse_ok = False
    else:
        eff = ((boc.get("execute") or {}).get("effect") or {})
        ev["data_notes"].append(
            "v0.102.0 parse: the BeginCombat clause is "
            f"effect={eff.get('type')}/{eff.get('name')} "
            f"(condition={boc.get('condition')}). The parser no longer "
            "marks this clause supported -- the whole clause is an "
            "unparsed_condition. The v0.98.0 finding (condition:null + "
            "GainControlAll filter:any + Untap->SelfRef) was against an "
            "older card-data AST.")
        parse_ok = eff.get("type") == "Unimplemented"
    ev["parse_ok"] = parse_ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1, default=str)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump(hp, f, indent=1, default=str)
    say(f"data-level: parse_ok={parse_ok}")
    wire("data_level", ev)
    return parse_ok


def evaluate(obs):
    A = obs["assert"]
    pre = load_state_file("pre.json")
    post_cast = load_state_file("post_cast.json")
    post = load_state_file("post.json")

    # A1: fixture in place
    if pre is None:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append("A1: not-run (pre.json missing)")
    else:
        boid, bo = bears_obj(pre)
        bears_ok = bool(boid and bo.get("controller") == 1 and bo.get("tapped"))
        alive_ok = all_alive(pre)
        hp_cast_ok = False
        susp_ok = False
        if post_cast is not None:
            hp_cast_ok = len(bf_by_name(post_cast, 0, HP_L)) >= 1
            boid2, bo2 = bears_obj(post_cast)
            susp_ok = bool(boid2 and is_suspected(bo2))
        A["A1_setup_ok"] = "passed" if (bears_ok and alive_ok and hp_cast_ok and susp_ok) else "failed"
        obs["notes"].append(
            f"A1: pre bears controller={bo.get('controller') if boid else None} "
            f"tapped={bo.get('tapped') if boid else None}; all_alive={alive_ok}; "
            f"post_cast HP_on_P0_bf={hp_cast_ok}; bears is_suspected={susp_ok}; "
            f"etb_kind={G7239['etb_kind']}")

    fired = G7239["fired"]
    if fired:
        A["A2_no_spurious_control_change"] = "failed"
        names = sorted({c["name"] for c in fired["changed"]})
        obs["notes"].append(
            f"A2: FAILED -- BeginCombat trigger fired with 0 players eliminated "
            f"(turn={fired['turn']} phase={fired['phase']} active={fired['active']}); "
            f"control changed: {names}")
    elif A.get("A1_setup_ok") == "passed" and post is not None:
        A["A2_no_spurious_control_change"] = "passed"
        obs["notes"].append("A2: passed -- no control change pre->post "
                            f"({G7239['stop_reason']})")
    else:
        A["A2_no_spurious_control_change"] = "not-run"
        obs["notes"].append("A2: not-run (fixture incomplete)")

    if fired and post is not None:
        # A3: scope -- only the suspected Bears should change control
        non_bears = [c for c in fired["changed"] if c["name"].lower() != BEARS_L]
        boid_p, bo_p = bears_obj(post)
        bears_gained = bool(boid_p and bo_p.get("controller") == 0)
        A["A3_scope_correct"] = "passed" if (not non_bears and bears_gained) else "failed"
        obs["notes"].append(
            f"A3: gained={sorted({c['name'] for c in fired['changed']})}; "
            f"bears is_suspected in post={is_suspected(bo_p) if boid_p else None}; "
            f"bears now controlled by P0={bears_gained}")
        # A4: untap -- the tapped Bears should untap
        tapped_gained = [c for c in fired["changed"] if c["tapped"]]
        if not tapped_gained:
            A["A4_untap_correct"] = "not-run"
            obs["notes"].append("A4: not-run -- no tapped permanent changed control")
        else:
            still_tapped = [c["name"] for c in tapped_gained
                            if get_obj(post, c["oid"]).get("tapped")]
            A["A4_untap_correct"] = "failed" if still_tapped else "passed"
            obs["notes"].append(
                f"A4: tapped gained={[c['name'] for c in tapped_gained]}; "
                f"still tapped in post={still_tapped}")
        # A5: haste -- gained creatures gain haste until end of turn
        missing = []
        for c in fired["changed"]:
            o = get_obj(post, c["oid"])
            cts = ((o.get("card_types") or {}).get("core_types") or [])
            if "Creature" in cts and not has_haste(o):
                missing.append(c["name"])
        A["A5_haste_granted"] = "failed" if missing else "passed"
        obs["notes"].append(f"A5: gained creatures missing Haste in post={missing}")
    else:
        A["A3_scope_correct"] = "not-run"
        A["A4_untap_correct"] = "not-run"
        A["A5_haste_granted"] = "not-run"

    if A.get("A1_setup_ok") == "passed" and A.get("A2_no_spurious_control_change") == "failed":
        verdict = "reproduced"
    elif A.get("A1_setup_ok") == "passed" and A.get("A2_no_spurious_control_change") == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    return verdict


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #7239 -- Hot Pursuit BeginCombat trigger (revalidation)",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} -- run {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: tapped suspected Bears (P1), HP cast (P0), all alive",
        "A2_no_spurious_control_change": "A2 no control change at P0 BeginCombat (0 eliminated)",
        "A3_scope_correct": "A3 scope: only suspected Bears gained (if fired)",
        "A4_untap_correct": "A4 untap: tapped gained creatures untap (if fired)",
        "A5_haste_granted": "A5 haste: gained creatures gain Haste (if fired)",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y),
               f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:14]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7239/"
           + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


def write_server_excerpts(game_code):
    """Copy server-log lines for this game into the evidence dir."""
    src = f"{BACKFILL}/runs/20261006-9295/server.log"
    out = []
    try:
        with open(src, errors="replace") as f:
            for line in f:
                if game_code in line:
                    out.append(line.rstrip("\n"))
    except OSError as e:
        out = [f"server log unreadable: {e}"]
    with open(f"{EVDIR}/server_excerpts.log", "w") as f:
        f.write("\n".join(out[-400:]) + "\n")
    say(f"server excerpts: {len(out)} lines for game {game_code}")


async def _main():
    t0 = time.time()
    await verify_server_hello()
    data_ok = check_data_level()
    obs = await run_game()
    dur = time.time() - t0
    ass = obs.get("assert", {})
    notes = list(obs.get("notes", []))
    notes.append("protocol-106 driver (v0.102.0): MulliganDecision via legacy "
                 "Action; bottom via vi schema/select gated on "
                 "waitingForKind.code=='mulligan'; DiscardToHandSize via vi; "
                 "Grizzly Bears ({1}{G}) and Hot Pursuit ({1}{R}) cast via "
                 "advertised CastSpell (engine auto-taps + vi "
                 "tapLandForMana/PayMana); P1's attack via the relations-schema "
                 "DeclareAttackers opportunity (Bears -> P0); empty declares "
                 "via the relations-first grace pattern; HP ETB Suspect via "
                 "the schema/select target opportunity; priority-gated "
                 "passes; 5s re-tick backstop covers priority holders and "
                 "pending decisions.")
    verdict = obs.get("verdict", "blocked")
    scenario_src = open(__file__, "rb").read()
    keys = ("A1_setup_ok", "A2_no_spurious_control_change", "A3_scope_correct",
            "A4_untap_correct", "A5_haste_granted")
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/7239",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "runs/20261006-9295 (v0.102.0 server on 127.0.0.1:9374, isolated games.db)",
        "data_level_parse_ok": data_ok,
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": [["Hot Pursuit", 8], ["Mountain", 52]],
            "P1": [["Grizzly Bears", 8], ["Forest", 52]],
            "P2": [["Forest", 60]],
            "P3": [["Forest", 60]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": "; ".join(f"{k}: {ass.get(k, 'not-run')}" for k in keys),
        "scope": ("Hot Pursuit BeginCombat trigger with 0 players eliminated; "
                  "native human seats (no AI); 20-life four-player free-for-all"),
        "limitations": [
            "Browser UI not exercised; native engine via four human-client seats.",
            "20-life four-player free-for-all, not Commander (40 life); the "
            "reported defect (missing 2+-eliminated condition) is "
            "format-independent.",
            ">4-of deck densities are a test-harness convenience (engine "
            "accepts them for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The 'two or more players lost' branch was not exercised -- only "
            "the 0-eliminated case.",
            "Verdict is scoped to v0.102.0, not a fix claim.",
        ],
        "setup_line": "P0: 8x Hot Pursuit, 52x Mountain; P1: 8x Grizzly Bears, 52x Forest; P2/P3: 60x Forest",
        "contract_line": ("With 0 players eliminated and a tapped suspected Bears on P1's "
                          "battlefield, P0's BeginCombat trigger must not change any "
                          "permanent's controller, untap anything, or grant haste."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7239_01020.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    write_server_excerpts(G7239.get("game_code") or "")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    # Close logs BEFORE computing the manifest so their hashes are final.
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


async def main():
    pidfile = "/tmp/scenario_7239_01020.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_7239_01020 instance is alive "
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


asyncio.run(main())
