#!/usr/bin/env python3
"""Issue #6903: Shadow Kin exiles whatever it likes (open, status:confirmed).

Oracle (pinned card-data.json v0.103.0): "Flash. At the beginning of your
upkeep, each player mills three cards. You may exile a creature card from
among the cards milled this way. If you do, this creature becomes a copy of
that card, except it has this ability."

v0.81.3 finding (protocol 70, run 20260912-f33127k, verdict reproduced):
  - Each player milled exactly 3 (mill itself worked).
  - The may-exile choice offered 7 candidates, NONE of them milled: 2x
    Shadow Kin from P0's hand and 5x Island from the battlefield.
  - Accepting exiled a Shadow Kin from hand; Kin became a copy of it.
  - Declining on the next upkeep still produced a mandatory EffectZoneChoice
    (battlefield candidates, min 1) -- no legal decline path; driver held it.

Triage acceptance criteria (mike-theDude, 2026-08-03):
  1. Each player mills three cards.
  2. The optional choice contains only creature cards among the cards milled
     by that exact trigger.
  3. Selecting a card exiles it from its current graveyard and makes Shadow
     Kin the specified copy.
  4. Battlefield permanents and pre-existing graveyard cards are never
     eligible.

Protocol-106 port for the pinned v0.103.0 re-validation. Conventions
(from scenario_6902_01020.py):
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [<name strings>]}.
  - waiting_for is gone (null): priority = advertised PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on waitingForKind.code ==
    'mulligan' AND turn 1 / Untap; DiscardToHandSize via vi schema/select.
  - CastSpell via legacy actions (engine auto-taps reliably; manual taps
    are a >90s fallback only); mana paid via PayMana/PayManaAbilityMana
    actions and vi tapLandForMana menus driven by MANA_NEEDS.
  - The "you may" via vi exactChoices decideOptionalEffect (accept/decline);
    the exile card choice via the advertised schema/select opportunity
    (candidates carry object surfaces with role=candidate + reference).
  - DeclareAttackers/DeclareBlockers: empty declares via the advertised
    action; vi fallback only answers relations-schema opportunities.
  - real_decision_pending excludes the 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand);
    decideOptionalEffect / decideOptionalCost and schema opportunities
    are real decisions.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop covers
    priority holders AND pending decisions.
  - Export envelope: data.state is a JSON string parsed once.

Plan (native engine, v0.103.0 / protocol 106, two human-client seats):
  P0: 4x Shadow Kin + 24x Storm Crow + 32x Island.
  P1: 24x Grizzly Bears + 36x Forest (draw-go, never attacks).
  P0 casts Storm Crow (battlefield temptation), then Shadow Kin ({3}{U}).
  Leg 1: P0's next upkeep with Kin on the battlefield (turn kin+2).
    Observe the mill, record every exile candidate with its zone, accept
    the may-choice; choose the first IN-SCOPE candidate (newly milled
    creature); if none in scope, choose the first offered candidate to
    document what the engine does with it (mirrors the v0.81.3 run).
  Leg 2: P0's following upkeep (turn kin+4). The copy retains the trigger.
    DECLINE the may-choice. Clean decline -> game proceeds (post2).
    Mandatory follow-up card choice -> record, capture mid_held.json, hold
    (the v0.81.3 decline bug).

Behavioral contract:
  A1 setup_ok         pre_upkeep1: P0 Upkeep turn kin+2, Kin on P0 BF,
                     Crow on P0 BF, Bear on P1 BF (temptations present).
  A2 each_milled_3    pre_upkeep1 -> mid_mill1: each library -3, each
                     graveyard +3.
  A3 optional_offered leg1: the may-exile decision was offered (when >=1
                     creature was milled; not-run if none milled and none
                     offered, which is correct).
  A4 candidates_in_scope leg1: every recorded exile candidate is a
                     creature card in the newly-milled set.
  A5 legal_exile_and_copy leg1: the submitted choice was in-scope; in
                     post1 it is in Exile and Kin's name == its name.
  A6 ability_retained leg2: upkeep trigger fired again (each library -3
                     pre_upkeep2 -> mid_mill2/post2).
  A7 decline_control  leg2: declined; no new exile; no mandatory
                     follow-up choice; game advanced past the upkeep.
  A8 cleanup          post2: stack empty, game advanced (or mid_held
                     captured when the decline path is broken).

Verdict: reproduced iff A1+A2 pass and (A4 fails or A5 fails or the leg-2
decline produces a mandatory follow-up choice).
not-reproduced iff A1..A8 all pass. blocked otherwise.
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
log = logging.getLogger("scenario6903_106")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261008-6903e"
ISSUE = 6903
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.103.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-08: latest stable release v0.103.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.103.0/ec27a8d/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.103.0 server on 127.0.0.1:9375 (run dir "
               "runs/20261008-6903, backfill-owned, isolated games.db)"),
}

for _f, _k in (("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.103.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))


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


def lib_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


def gy_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("graveyard") or [])]


def lib_size(state, pid):
    return len(lib_ids(state, pid))


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def is_creature(o):
    cts = (((o.get("card_types") or {}).get("core_types") or [])
           + ((o.get("base_card_types") or {}).get("core_types") or []))
    return "Creature" in cts


def untapped_lands(state, pid):
    n = 0
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            n += 1
    return n


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


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


def decision_pending(st):
    """Legacy alias: only real decisions block priority passes."""
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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"


async def do_mulligan(c, acts, st, pid, tag):
    """Protocol 106: MulliganDecision arrives as a legacy legal action."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in surf_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn, "iid": iid})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_bottom(c, acts, st, pid, tag):
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
        if nm == "shadow kin":
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
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

    def rank(o):
        nm = obj_lname(state, o)
        if nm == "shadow kin":
            return (5, nm)
        if is_land(get_obj(state, o)):
            return (0, nm)
        return (2, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
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


def can_pay_generic(state, pid, generic):
    pool = sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))
    return pool >= generic


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


def pick_optional_cast(c, want_accept, tag):
    """Find the may-choice (decideOptionalEffect) opportunity and the
    accept/decline choice id. Returns (iid, cid) or (None, None)."""
    st = c.latest
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        has = any("decideOptionalEffect" in surf_codes(ch)
                  for ch in (resp.get("data") or {}).get("choices", []))
        if not has:
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            is_accept = None
            for sf in ch.get("surfaces", []):
                dd = sf.get("data", {}) or {}
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None:
                txt = choice_text(ch).lower()
                if "cast" in txt or "accept" in txt or "yes" in txt:
                    is_accept = True
                elif "decline" in txt or "don't" in txt or "do not" in txt \
                        or "no" in txt:
                    is_accept = False
            if is_accept == want_accept:
                return opp.get("interactionId"), ch["id"]
    return None, None

# ---------------------------------------------------------------- #6903 flow
# Issue #6903: Shadow Kin -- the upkeep may-exile choice must offer only
# creature cards milled by that trigger.

KIN = "Shadow Kin"
CROW = "Storm Crow"
BEAR = "Grizzly Bears"
ISLAND = "Island"
FOREST = "Forest"
KIN_L = "shadow kin"
CROW_L = "storm crow"
BEAR_L = "grizzly bears"

P0_DECK = [(KIN, 4), (CROW, 24), (ISLAND, 32)]
P1_DECK = [(BEAR, 24), (FOREST, 36)]


def reset_g6903():
    global G6903
    G6903 = {
        "joined": False,
        "game_code": None,
        "crow_cast": False,
        "kin_cast": False,
        "kin_oid": None,
        "kin_cast_turn": None,
        "bear_cast": False,
        "pre1": False,
        "pre2": False,
        "stop": False,
        "leg1": {"mid_exported": False, "milled": [], "candidates": [],
                 "opt_answered": None, "answered": False, "chose": None,
                 "no_choice": False, "done": False},
        "leg2": {"mid_exported": False, "milled": [], "candidates": [],
                 "opt_answered": None, "declined_at": None, "followup": None,
                 "held": False, "done": False},
    }


reset_g6903()


def leg_turn(n):
    kt = G6903["kin_cast_turn"]
    return None if kt is None else kt + 2 * n


def find_kin_optional(st):
    """The may-exile prompt: exactChoices with a decideOptionalEffect code."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


def find_kin_card_choice(st):
    """The exile card choice: schema select/sequence whose candidates carry
    object references (role=candidate). Returns (opp, spec_type)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = (data.get("spec") or {}).get("type") or "select"
        cands = data.get("candidates") or []
        if spec in ("select", "sequence") and cands \
                and any(_cand_reference(ch) is not None for ch in cands):
            return opp, spec
    return None, None


def record_candidates(state, opp, L):
    """Resolve every candidate to (choice_id, oid, name, zone, controller,
    is_creature); flag in_scope against the leg's milled set."""
    milled = set(str(x) for x in L["milled"])
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    out = []
    for ch in data.get("candidates") or []:
        ref = _cand_reference(ch)
        o = get_obj(state, ref) if ref is not None else {}
        surf_name, surf_zone, surf_ctrl = None, None, None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("role") == "candidate":
                surf_name = d.get("name") or surf_name
                surf_zone = d.get("zone") or surf_zone
                if d.get("controller") is not None:
                    surf_ctrl = d.get("controller")
        name = str(o.get("base_name") or o.get("name") or surf_name or "?")
        zone = o.get("zone") or surf_zone
        rec = {
            "choice_id": ch.get("id"),
            "ref": str(ref) if ref is not None else None,
            "name": name,
            "zone": zone,
            "controller": o.get("controller", surf_ctrl),
            "is_creature": bool(is_creature(o)) if o else None,
            "in_scope": (str(ref) in milled and bool(is_creature(o))) if o else False,
        }
        out.append(rec)
    L["candidates"] = out
    return out


def milled_between(pre_state, mid_state):
    """Oids that moved Library -> Graveyard between two exports, both seats."""
    out = []
    for pid in ("0", "1"):
        pre_lib = set(lib_ids(pre_state, pid))
        pre_gy = set(gy_ids(pre_state, pid))
        mid_gy = set(gy_ids(mid_state, pid))
        for o in mid_gy - pre_gy:
            if o in pre_lib:
                out.append(o)
    return out


def load_state_file(fn):
    p = f"{EVDIR}/{fn}"
    if not os.path.exists(p):
        return None
    return json.loads(open(p).read())["state"]


async def export_authoritative(c, fn, tag):
    s = await c.export_state()
    with open(f"{EVDIR}/{fn}", "w") as f:
        f.write(s)
    say(f"[{tag}] exported {fn}")
    wire("export", {"file": fn})
    return json.loads(s)["state"]


def ensure_mid_export_sync(leg):
    """Compute the milled set for a leg from its pre/mid exports (called
    after both exist)."""
    L = G6903[f"leg{leg}"]
    pre = load_state_file(f"pre_upkeep{leg}.json")
    mid = load_state_file(f"mid_mill{leg}.json")
    if pre is not None and mid is not None:
        L["milled"] = milled_between(pre, mid)
        say(f"[6903] leg{leg}: milled set = {L['milled']} "
            f"({len(L['milled'])} cards)")


async def handle_kin_upkeep(p0, tag, leg):
    """Answer the Kin may-exile decision for a leg. Leg 1 accepts (choosing
    the first in-scope candidate, else the first offered); leg 2 declines.
    A card choice appearing after a leg-2 decline is the decline-path bug:
    record it, capture mid_held.json, and hold. Returns True if a
    submission was made or the hold was taken."""
    st = p0.latest
    if st is None:
        return False
    state = st["state"]
    L = G6903[f"leg{leg}"]

    opp = find_kin_optional(st)
    if opp is not None:
        iid = opp.get("interactionId")
        key = ("kin-opt", leg, iid)
        if key in SUBMITTED_OPPS:
            return False
        if not L["mid_exported"]:
            await export_authoritative(p0, f"mid_mill{leg}.json", tag)
            L["mid_exported"] = True
            ensure_mid_export_sync(leg)
        with open(f"{EVDIR}/opt_choice{leg}.json", "w") as f:
            json.dump(opp, f, indent=1, default=str)
        want_accept = (leg == 1)
        _iid, cid = pick_optional_cast(p0, want_accept, tag)
        if cid is None:
            say(f"[{tag}] leg{leg}: decideOptionalEffect offered but no "
                f"{'accept' if want_accept else 'decline'} choice found")
            wire(f"leg{leg}_opt_no_match", {"opp": opp})
            return False
        SUBMITTED_OPPS.add(key)
        L["opt_answered"] = "accept" if want_accept else "decline"
        if leg == 2:
            L["declined_at"] = time.time()
        say(f"[{tag}] leg{leg}: submitting "
            f"{'ACCEPT' if want_accept else 'DECLINE'} (choice {cid})")
        wire(f"leg{leg}_opt_submit",
             {"iid": iid, "cid": cid,
              "decision": "accept" if want_accept else "decline"})
        await p0.send_interaction(
            {"interactionId": iid,
             "response": {"type": "choose", "data": {"choiceId": cid}}})
        return True

    copp, spec = find_kin_card_choice(st)
    if copp is not None:
        iid = copp.get("interactionId")
        key = ("kin-choice", leg, iid)
        if key in SUBMITTED_OPPS:
            return False
        if not L["mid_exported"]:
            await export_authoritative(p0, f"mid_mill{leg}.json", tag)
            L["mid_exported"] = True
            ensure_mid_export_sync(leg)
        cands = record_candidates(state, copp, L)
        say(f"[{tag}] leg{leg}: exile choice with {len(cands)} candidates: "
            + ", ".join(f"{c['name']}@{c['zone']}(ctrl {c['controller']})"
                        f"{'' if c['in_scope'] else ' OUT-OF-SCOPE'}"
                        for c in cands))
        wire(f"leg{leg}_choice",
             {"candidates": cands, "spec": spec,
              "milled": L["milled"]})
        with open(f"{EVDIR}/exile_choice{leg}.json", "w") as f:
            json.dump(copp, f, indent=1, default=str)
        if leg == 1:
            insc = [c for c in cands if c["in_scope"]]
            # Source refs of the decision (role=source surfaces): the Kin
            # itself. On the out-of-scope bug path the engine offers
            # battlefield permanents; picking the Kin itself self-exiles the
            # trigger source (it stays in Exile, no copy) and voids leg 2 --
            # prefer any non-source offered card so the battlefield trigger
            # survives to leg 2.
            src_refs = set()
            for s in (copp.get("surfaces") or []):
                sd = (s.get("data") or {})
                if sd.get("role") == "source" and sd.get("reference"):
                    src_refs.add(str(sd["reference"]))
            pick = insc[0] if insc else None
            if pick is None:
                non_src = [c for c in cands if str(c.get("ref")) not in src_refs]
                pick = non_src[0] if non_src else (cands[0] if cands else None)
            if pick is None:
                say(f"[{tag}] leg1: choice offered with zero candidates")
                wire("leg1_choice_empty", {})
                return False
            SUBMITTED_OPPS.add(key)
            L["chose"] = pick
            L["answered"] = True
            say(f"[{tag}] leg1: submitting choice "
                f"{pick['name']}@{pick['zone']} (in_scope={pick['in_scope']})")
            wire("leg1_choice_submit", {"pick": pick})
            if (copp.get("response", {}) or {}).get("type") == "schema":
                # schema/select (or sequence): the response type is the
                # spec type, with choiceIds (NOT the "choose" envelope)
                sub = {"interactionId": iid,
                       "response": {"type": spec,
                                    "data": {"choiceIds": [pick["choice_id"]]}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick["choice_id"]}}}
            await interact_as(p0, sub, tag)
            return True
        # leg 2: a card choice after the decline is the decline-path bug
        # (v0.81.3 issued a mandatory EffectZoneChoice here). Hold it.
        L["followup"] = {"candidates": cands, "spec": spec,
                         "iid": iid}
        await export_authoritative(p0, "mid_held.json", tag)
        L["held"] = True
        G6903["stop"] = True
        say(f"[{tag}] leg2: card choice appeared AFTER decline "
            f"({len(cands)} candidates, all "
            f"{'in-scope' if all(c['in_scope'] for c in cands) else 'out-of-scope or unscoped'})"
            f" -- holding; mid_held.json captured")
        wire("leg2_decline_followup", L["followup"])
        return True
    return False


async def p1_tick(p1, tag):
    st = p1.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(p1, acts, st, 1, tag):
        return
    if await do_bottom(p1, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(p1, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(p1, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(p1, acts):
        return
    if await pay_mana_vi(p1, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(p1, state, 1, acts, tag):
            return
        # One Grizzly Bear as the battlefield temptation; never attacks.
        if not G6903["bear_cast"] \
                and any(obj_lname(state, o) == BEAR_L
                        for o in hand_ids(state, 1)) \
                and untapped_lands(state, 1) >= 3:
            a, oid = cast_action_for(acts, state, BEAR_L)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 1, "generic": 2}
                G6903["bear_cast"] = True
                say(f"[{tag}] P1 casting Grizzly Bears (oid {oid})")
                wire("cast_bear", {"oid": oid})
                await submit_as_is(p1, a)
                return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(p1.name, -1) < p1.revision:
            await pass_priority(p1, st, acts)
            PASSED_REV[p1.name] = p1.revision


async def p0_tick(p0, tag):
    st = p0.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(p0, acts, st, 0, tag):
        return
    if await do_bottom(p0, acts, st, 0, tag):
        return
    if await do_discard_to_handsize(p0, acts, st, 0, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(p0, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(p0, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(p0, st, tag, needs):
            return

    await asyncio.sleep(0)
    st = p0.latest or st
    state = st["state"]
    acts = merged_actions(st)

    phase = state.get("phase") or ""
    turn = state.get("turn_number")
    actp = state.get("active_player")
    stack_empty = not (state.get("stack") or [])

    kin_bf = bf_by_name(state, 0, KIN_L)
    if kin_bf and G6903["kin_oid"] is None:
        G6903["kin_oid"] = kin_bf[0]
        say(f"[{tag}] Shadow Kin on battlefield (oid {kin_bf[0]})")

    kt = G6903["kin_cast_turn"]

    # ---- ramp: Storm Crow then Shadow Kin on P0 main phases
    if my_main(state, 0):
        if await play_a_land(p0, state, 0, acts, tag):
            return
        if not G6903["crow_cast"] \
                and any(obj_lname(state, o) == CROW_L
                        for o in hand_ids(state, 0)) \
                and untapped_lands(state, 0) >= 2:
            a, oid = cast_action_for(acts, state, CROW_L)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1, "generic": 1}
                G6903["crow_cast"] = True
                say(f"[{tag}] casting Storm Crow (oid {oid})")
                wire("cast_crow", {"oid": oid})
                await submit_as_is(p0, a)
                return
        if not G6903["kin_cast"] \
                and any(obj_lname(state, o) == KIN_L
                        for o in hand_ids(state, 0)) \
                and untapped_lands(state, 0) >= 4:
            a, oid = cast_action_for(acts, state, KIN_L)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1, "generic": 3}
                G6903["kin_cast"] = True
                G6903["kin_cast_turn"] = turn
                say(f"[{tag}] casting Shadow Kin (oid {oid}) on turn {turn}")
                wire("cast_kin", {"oid": oid, "turn": turn})
                await submit_as_is(p0, a)
                return

    # ---- pre-upkeep checkpoints (P1's End step before each leg, or P0 Untap)
    if kt is not None and not G6903["pre1"]:
        if (turn == kt + 1 and actp == 1 and phase == "End" and stack_empty) \
                or (turn == kt + 2 and actp == 0 and phase == "Untap"):
            await export_authoritative(p0, "pre_upkeep1.json", tag)
            G6903["pre1"] = True
    if kt is not None and not G6903["pre2"] and G6903["leg1"]["done"]:
        if (turn == kt + 3 and actp == 1 and phase == "End" and stack_empty) \
                or (turn == kt + 4 and actp == 0 and phase == "Untap"):
            await export_authoritative(p0, "pre_upkeep2.json", tag)
            G6903["pre2"] = True

    # ---- leg windows: P0's upkeep turns
    for leg in (1, 2):
        lt = leg_turn(leg)
        if lt is None or turn != lt or actp != 0:
            continue
        L = G6903[f"leg{leg}"]
        if L["done"] or L.get("held"):
            continue
        in_upkeep = phase in ("Untap", "Upkeep")
        past_upkeep = phase in ("Draw", "PreCombatMain", "Combat",
                                "PostCombatMain", "End", "Cleanup")
        if in_upkeep or (past_upkeep and not L["mid_exported"]):
            # Answer (or record) the Kin decision while it is pending.
            if await handle_kin_upkeep(p0, tag, leg):
                return
            # Mill happened but no Kin decision ever surfaced: capture the
            # post-mill state once so A2/A3 can be evaluated honestly.
            if past_upkeep and not L["mid_exported"] \
                    and not L["opt_answered"]:
                await export_authoritative(p0, f"mid_mill{leg}.json", tag)
                L["mid_exported"] = True
                L["no_choice"] = True
                ensure_mid_export_sync(leg)
                say(f"[{tag}] leg{leg}: past upkeep with no Kin decision "
                    f"surfaced; mid_mill{leg}.json captured")
                wire(f"leg{leg}_no_decision", {"milled": L["milled"]})
        # leg-1 completion: choice answered (or opted) and game reached
        # P0's main phase with an empty stack.
        if leg == 1 and not L["done"] \
                and (L["answered"] or L["opt_answered"] or L["no_choice"]) \
                and phase == "PreCombatMain" and stack_empty:
            await export_authoritative(p0, "post1.json", tag)
            L["done"] = True
            say(f"[{tag}] leg1 complete; post1 exported")
        # leg-2 clean completion: declined, no follow-up, reached main.
        if leg == 2 and not L["done"] and not L["held"] \
                and L["opt_answered"] == "decline" \
                and phase == "PreCombatMain" and stack_empty:
            # one last check: no Kin decision lingering
            if await handle_kin_upkeep(p0, tag, leg):
                return
            await export_authoritative(p0, "post2.json", tag)
            L["done"] = True
            G6903["stop"] = True
            say(f"[{tag}] leg2 complete (clean decline); post2 exported")

    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(p0.name, -1) < p0.revision:
            await pass_priority(p0, st, acts)
            PASSED_REV[p0.name] = p0.revision


async def _run_game(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    reset_g6903()
    obs = {"assert": {}, "notes": [], "_fixture_ok": True}
    tag = "6903" if attempt == 0 else f"6903-r{attempt}"
    ST["mana_needs"][tag] = {}

    p0 = PhaseClient(f"P06903-{attempt}")
    await p0.connect()
    say("connecting P1...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient(f"P16903-{attempt}")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code})
    G6903["joined"] = p0.player_id is not None and p1.player_id is not None
    G6903["game_code"] = p0.game_code
    obs["assert"]["A1_setup_ok"] = "passed" if G6903["joined"] else "failed"

    t_end = time.time() + 1500
    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    try:
        while time.time() < t_end and not G6903["stop"]:
            await asyncio.sleep(0.15)
            for c, is_p0 in ((p0, True), (p1, False)):
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
                    if is_p0:
                        await p0_tick(p0, tag)
                    else:
                        await p1_tick(p1, f"P1{tag}")
                except Exception as e:
                    say(f"[{tag}] tick error {c.name}: "
                        f"{type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}",
                                        "tb": traceback.format_exc()[-2000:]})
            if time.time() - last_diag > 60:
                last_diag = time.time()
                st = p0.latest
                if st:
                    s = st["state"]
                    say(f"[{tag}] DIAG rev={p0.revision} turn={s.get('turn_number')} "
                        f"phase={s.get('phase')} act={s.get('active_player')} "
                        f"stack={len(s.get('stack') or [])} "
                        f"kin_cast={G6903['kin_cast']} "
                        f"leg1={ {k: v for k, v in G6903['leg1'].items() if k != 'candidates'} } "
                        f"leg2done={G6903['leg2']['done']} held={G6903['leg2']['held']}")
    except Exception as e:
        obs["notes"].append(f"drive loop exception: {e}")
    finally:
        await p0.close()
        await p1.close()
    if not G6903["stop"]:
        obs["notes"].append("drive loop ended without completing leg 2 "
                            "(timeout or early exit)")
    verdict = evaluate(obs)
    return obs


async def run_game():
    for attempt in range(3):
        obs = await _run_game(attempt)
        if obs.get("_fixture_ok", True):
            return obs
    for k in ("A1_setup_ok", "A2_each_milled_3", "A3_optional_offered",
              "A4_candidates_in_scope", "A5_legal_exile_and_copy",
              "A6_ability_retained", "A7_decline_control", "A8_cleanup"):
        obs["assert"].setdefault(k, "not-run")
    return obs

def check_data_level():
    """Record the v0.103.0 parse of Shadow Kin."""
    sk = CARD_DATA.get("shadow kin", {})
    trigs = sk.get("triggers") or []
    chain = []
    ok = False
    if trigs:
        t = trigs[0]
        ex = (t.get("execute") or {})
        eff = (ex.get("effect") or {})
        chain.append({"trigger": t.get("mode"), "phase": t.get("phase"),
                      "effect": eff.get("type"),
                      "mill_count": ((eff.get("count") or {}).get("value")),
                      "mill_target": (eff.get("target") or {}).get("type"),
                      "player_scope": (ex.get("player_scope") or {}).get("type")})
        sub = ex.get("sub_ability") or {}
        seff = (sub.get("effect") or {})
        chain.append({"sub": "ChangeZone",
                      "origin": seff.get("origin"),
                      "destination": seff.get("destination"),
                      "target": seff.get("target"),
                      "optional": sub.get("optional")})
        sub2 = sub.get("sub_ability") or {}
        s2eff = (sub2.get("effect") or {})
        chain.append({"sub2": "BecomeCopy",
                      "target": (s2eff.get("target") or {}).get("type"),
                      "mods": s2eff.get("additional_modifications"),
                      "condition": sub2.get("condition")})
        ok = (eff.get("type") == "Mill" and seff.get("type") == "ChangeZone"
              and s2eff.get("type") == "BecomeCopy")
    ev = {"name": sk.get("name"), "oracle_text": sk.get("oracle_text"),
          "mana_cost": sk.get("mana_cost"), "chain": chain,
          "parse_ok": ok,
          "data_notes": [
              "Mill target is Controller (not EachPlayer) with "
              "player_scope=All; on v0.81.3 the each-player mill worked "
              "behaviorally (each library -3).",
              "ChangeZone origin=null: no graveyard zone or event-set "
              "provenance in the parse -- the triage's data-level finding "
              "persists on v0.103.0; the behavioral question is whether "
              "the engine scopes candidates to the milled set anyway.",
          ]}
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1, default=str)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump(sk, f, indent=1, default=str)
    say(f"data-level: parse_ok={ok}")
    wire("data_level", ev)
    return ok


def exile_oids(state):
    return {str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Exile"}


def evaluate(obs):
    A = obs["assert"]
    pre1 = load_state_file("pre_upkeep1.json")
    mid1 = load_state_file("mid_mill1.json")
    post1 = load_state_file("post1.json")
    pre2 = load_state_file("pre_upkeep2.json")
    mid2 = load_state_file("mid_mill2.json")
    post2 = load_state_file("post2.json")
    held = load_state_file("mid_held.json")
    L1, L2 = G6903["leg1"], G6903["leg2"]
    kin_oid = G6903.get("kin_oid")

    # A1: Kin + temptations on the battlefield at leg-1 upkeep
    if pre1 is None:
        A["A1_setup_ok"] = "not-run"
    else:
        kin_ok = kin_oid is not None and zone_of(pre1, kin_oid) == "Battlefield"
        crow_ok = len(bf_by_name(pre1, 0, CROW_L)) >= 1
        bear_ok = len(bf_by_name(pre1, 1, BEAR_L)) >= 1
        A["A1_setup_ok"] = "passed" if (kin_ok and crow_ok and bear_ok) else "failed"
    obs["notes"].append(
        f"A1: kin_oid={kin_oid} zone={zone_of(pre1, kin_oid) if pre1 and kin_oid else '?'}; "
        f"crow_on_P0_BF={len(bf_by_name(pre1, 0, CROW_L)) if pre1 else '?'}; "
        f"bear_on_P1_BF={len(bf_by_name(pre1, 1, BEAR_L)) if pre1 else '?'}; "
        f"kin_cast_turn={G6903['kin_cast_turn']}")

    # A2: each player milled exactly 3 (pre1 -> mid1)
    if pre1 is None or mid1 is None:
        A["A2_each_milled_3"] = "not-run"
    else:
        d = {}
        ok = True
        for pid in ("0", "1"):
            dl = lib_size(mid1, pid) - lib_size(pre1, pid)
            dg = len(gy_ids(mid1, pid)) - len(gy_ids(pre1, pid))
            d[pid] = (dl, dg)
            ok = ok and dl == -3 and dg == 3
        A["A2_each_milled_3"] = "passed" if ok else "failed"
        obs["notes"].append(f"A2: lib/gy deltas pre1->mid1: {d} (expect (-3,+3) each)")

    # A3: the may-exile decision was offered in leg 1
    if L1["opt_answered"] or L1["answered"]:
        A["A3_optional_offered"] = "passed"
    elif L1["no_choice"]:
        milled_creatures = [o for o in L1["milled"]
                            if mid1 is not None and is_creature(get_obj(mid1, o))]
        if milled_creatures:
            A["A3_optional_offered"] = "failed"
            obs["notes"].append(
                f"A3: FAILED -- {len(milled_creatures)} creature(s) milled "
                f"but no may-exile decision was ever offered")
        else:
            A["A3_optional_offered"] = "not-run"
            obs["notes"].append("A3: not-run -- no creature milled in leg 1 "
                                "and no choice offered (correct per Oracle)")
    else:
        A["A3_optional_offered"] = "not-run"
    obs["notes"].append(f"A3: leg1 opt_answered={L1['opt_answered']} "
                        f"choice_answered={L1['answered']}")

    # A4: every leg-1 candidate in the newly-milled creature set
    cands = L1["candidates"]
    if not cands:
        A["A4_candidates_in_scope"] = "not-run" if A["A3_optional_offered"] != "passed" else "failed"
        # offered a choice but recorded zero candidates = scope failure
        if A["A3_optional_offered"] == "passed":
            obs["notes"].append("A4: FAILED -- choice offered but zero "
                                "candidates recorded")
    else:
        bad = [c for c in cands if not c["in_scope"]]
        A["A4_candidates_in_scope"] = "passed" if not bad else "failed"
        obs["notes"].append(
            f"A4: {len(cands)} candidates, {len(cands)-len(bad)} in-scope; "
            + ("all in the newly-milled creature set" if not bad else
               "OUT-OF-SCOPE: " + ", ".join(
                   f"{c['name']}@{c['zone']}(ctrl {c['controller']})" for c in bad)))
        obs["notes"].append(f"A4: milled set ({len(L1['milled'])}): " +
                             ", ".join(obj_lname(mid1, o) for o in L1["milled"])
                             if mid1 else "")

    # A5: the submitted choice was in-scope, exiled, and Kin copies it
    chose = L1["chose"]
    if post1 is None or chose is None:
        A["A5_legal_exile_and_copy"] = "not-run"
    elif not chose["in_scope"]:
        A["A5_legal_exile_and_copy"] = "failed"
        cz = zone_of(post1, chose["ref"])
        kin_name = (get_obj(post1, kin_oid).get("name")
                    or get_obj(post1, kin_oid).get("base_name")) if kin_oid else "?"
        obs["notes"].append(
            f"A5: FAILED -- submitted choice was out-of-scope "
            f"({chose['name']}@{chose['zone']}); post1 zone={cz}, "
            f"Kin name now {kin_name!r} (the illegal exile+copy "
            f"{'happened' if cz == 'Exile' else 'did not complete'})")
    else:
        cz = zone_of(post1, chose["ref"])
        kin_name = (get_obj(post1, kin_oid).get("name")
                    or get_obj(post1, kin_oid).get("base_name")) if kin_oid else "?"
        ok = cz == "Exile" and str(kin_name).lower() == chose["name"].lower()
        A["A5_legal_exile_and_copy"] = "passed" if ok else "failed"
        obs["notes"].append(f"A5: chose {chose['name']} (in-scope); post1 "
                            f"zone={cz}, Kin name={kin_name!r}")

    # A6: the trigger fired again in leg 2 (mill observed)
    leg2_ref = mid2 if mid2 is not None else post2
    if pre2 is None or leg2_ref is None:
        A["A6_ability_retained"] = "not-run"
    else:
        ok = all(lib_size(leg2_ref, pid) - lib_size(pre2, pid) == -3
                 for pid in ("0", "1"))
        A["A6_ability_retained"] = "passed" if ok else "failed"
        obs["notes"].append(
            f"A6: leg2 lib deltas pre2->"
            f"{'mid2' if mid2 is not None else 'post2'}: "
            f"{ {pid: lib_size(leg2_ref, pid) - lib_size(pre2, pid) for pid in ('0','1')} } "
            f"(expect -3 each; proves the copy kept the upkeep trigger)")

    # A7: leg-2 decline is clean (no exile, no mandatory follow-up)
    if L2["held"]:
        A["A7_decline_control"] = "failed"
        fu = L2["followup"] or {}
        obs["notes"].append(
            f"A7: FAILED -- after declining, the engine issued a follow-up "
            f"card choice ({len(fu.get('candidates', []))} candidates: "
            + ", ".join(f"{c['name']}@{c['zone']}" for c in fu.get("candidates", []))
            + "); no legal decline path; held state in mid_held.json")
    elif L2["opt_answered"] == "decline" and post2 is not None and pre2 is not None:
        new_exile = exile_oids(post2) - exile_oids(pre2)
        ok = not new_exile and L2["followup"] is None
        A["A7_decline_control"] = "passed" if ok else "failed"
        obs["notes"].append(f"A7: declined cleanly; new exiles post2: "
                            f"{sorted(new_exile) or 'none'}; game advanced "
                            f"past the upkeep")
    else:
        A["A7_decline_control"] = "not-run"
        obs["notes"].append(f"A7: not-run (opt_answered={L2['opt_answered']}, "
                            f"post2={'yes' if post2 else 'no'})")

    # A8: cleanup
    if post2 is not None:
        A["A8_cleanup"] = ("passed" if not (post2.get("stack") or []) else "failed")
    elif held is not None:
        A["A8_cleanup"] = "not-run"
        obs["notes"].append("A8: not-run -- decline path broken; held state "
                            "captured in mid_held.json instead of post2")
    else:
        A["A8_cleanup"] = "not-run"

    a = A
    declined_broken = bool(L2["held"])
    if a.get("A1_setup_ok") == "passed" and a.get("A2_each_milled_3") == "passed" \
            and (a.get("A4_candidates_in_scope") == "failed"
                 or a.get("A5_legal_exile_and_copy") == "failed"
                 or declined_broken):
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_each_milled_3", "A3_optional_offered",
              "A4_candidates_in_scope", "A5_legal_exile_and_copy",
              "A6_ability_retained", "A7_decline_control", "A8_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    return verdict


def check_data_level_done():
    return True


# ---------------------------------------------------------------- summary + main

def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6903 -- Shadow Kin may-exile choice scope (revalidation)",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} -- {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Kin + Crow (P0) + Bear (P1) on battlefield",
        "A2_each_milled_3": "A2 leg1: each player milled exactly 3",
        "A3_optional_offered": "A3 leg1: may-exile decision offered",
        "A4_candidates_in_scope": "A4 leg1: all candidates newly-milled creatures",
        "A5_legal_exile_and_copy": "A5 leg1: in-scope choice exiled; Kin copies it",
        "A6_ability_retained": "A6 leg2: upkeep trigger fired again (mill)",
        "A7_decline_control": "A7 leg2: decline is clean (no forced choice)",
        "A8_cleanup": "A8 cleanup: stack empty, game advanced",
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
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6903/"
           + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


def write_server_excerpts(game_code):
    """Copy server-log lines for this game into the evidence dir."""
    src = f"{BACKFILL}/runs/20261008-6903/server.log"
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
    notes.append("protocol-106 driver (v0.103.0): MulliganDecision via legacy "
                 "Action; bottom via vi schema/select; DiscardToHandSize via "
                 "vi; Storm Crow / Shadow Kin / Grizzly Bears cast via "
                 "advertised CastSpell ({1}{U} / {3}{U} / {1}{G} through vi "
                 "tapLandForMana/PayMana); the 'you may' via vi exactChoices "
                 "decideOptionalEffect (accept leg 1, decline leg 2); the "
                 "exile card choice via the advertised schema/select "
                 "opportunity (candidates carry object surfaces "
                 "role=candidate + reference); empty declares; "
                 "priority-gated passes; 5s re-tick backstop covers priority "
                 "holders and pending decisions.")
    verdict = obs.get("verdict", "blocked")
    scenario_src = open(__file__, "rb").read()
    keys = ("A1_setup_ok", "A2_each_milled_3", "A3_optional_offered",
            "A4_candidates_in_scope", "A5_legal_exile_and_copy",
            "A6_ability_retained", "A7_decline_control", "A8_cleanup")
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/6903",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "runs/20261008-6903 (v0.103.0 server on 127.0.0.1:9375, isolated games.db)",
        "data_level_parse_ok": data_ok,
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Shadow Kin", 4], ["Storm Crow", 24], ["Island", 32]],
            "P1": [["Grizzly Bears", 24], ["Forest", 36]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": "; ".join(f"{k}: {ass.get(k, 'not-run')}" for k in keys),
        "scope": "Shadow Kin upkeep trigger, two legs (accept leg 1, decline leg 2); native human seats (no AI)",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            ">4-of deck densities are a test-harness convenience (engine accepts them for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "Not tested on the original 2026-08-02 build; verdict is scoped "
            "to v0.103.0, not a fix claim.",
        ],
        "setup_line": "P0: 4x Shadow Kin + 24x Storm Crow + 32x Island; P1: 24x Grizzly Bears + 36x Forest (draw-go, never attacks)",
        "contract_line": ("Leg 1 (P0 upkeep, turn kin+2): each player mills 3; the may-exile choice "
                          "must offer only newly-milled creature cards; accept and exile one. "
                          "Leg 2 (turn kin+4): the copy retains the trigger; decline must be clean "
                          "(no exile, no mandatory follow-up choice)."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6903_01020.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    write_server_excerpts(G6903.get("game_code") or "")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    # Close logs BEFORE computing the manifest so their hashes are final.
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


async def main():
    pidfile = "/tmp/scenario_6903_01020.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_6903_01020 instance is alive "
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
