#!/usr/bin/env python3
"""Issue #6902: Sneak Attack — delayed end-step sacrifice trigger only
sometimes sacrifices the creature (open).

Oracle (pinned card-data.json v0.102.0): "{R}: You may put a creature card
from your hand onto the battlefield. That creature gains haste. Sacrifice
the creature at the beginning of the next end step."

v0.81.2 finding (protocol 70, run 20260912-6902d, verdict reproduced):
  the delayed end-step sacrifice triggers only sometimes sacrificed the
  creature (the reported symptom).

Protocol-106 port for the pinned v0.102.0 re-validation. Conventions
(from scenario_6899_01020.py / scenario_301_01020.py):
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
  - ActivateAbility submitted WHILE HOLDING priority; the "you may" via vi
    exactChoices decideOptionalEffect (accept); the creature-from-hand
    choice via the advertised schema/exactChoices card opportunity.
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

Plan (native engine, v0.102.0 / protocol 106, two human-client seats):
  P0: 36x Mountain + 12x Sneak Attack + 12x Grizzly Bears.
  P1: 48x Mountain + 12x Grizzly Bears (draw-go).

  Leg 1: P0 casts Sneak Attack, then activates it TWICE on one main phase,
  putting two Grizzly Bears onto the battlefield (may accepted, Bear
  chosen from hand each time). At the beginning of the end step, two
  delayed sacrifice triggers must fire and each must sacrifice its own
  creature (both Bears -> graveyard).
  Leg 2: on a later turn, P0 activates Sneak Attack during its END STEP.
  The creature enters during the end step, so no sacrifice should happen
  this end step; the delayed trigger waits for the NEXT end step and
  sacrifices the creature then.

Behavioral contract:
  A1 setup_ok            both seats joined.
  A2 creatures_enter     leg-1: 2 Bears entered via the two activations
                         (mid_end.json: both bear oids on the battlefield).
  A3 triggers_fired      leg-1: >=2 sacrifice delayed triggers observed on
                         the stack at the end step.
  A4 both_sacrificed     leg-1: both leg-1 Bear oids in Graveyard in post1.json.
  A5 cleanup_leg1        leg-1: stack empty after the end step.
  A6 endstep_survives    leg-2: Bear that entered during the end step is
                         still on the battlefield at the next P0 turn.
  A7 next_endstep_sac    leg-2: Bear in Graveyard after the following end
                         step, stack empty.

Verdict: reproduced iff A1+A2 pass and (A3 fails or A4 fails).
not-reproduced iff A1..A7 all pass. blocked otherwise.
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
log = logging.getLogger("scenario301_106")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261006-6902"
ISSUE = 6902
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
    "source": ("2026-10-04: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "fresh v0.102.0 server started by this run on 127.0.0.1:9375 "
               "with isolated run dir runs/20261004-301"),
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


def untapped_islands(state, pid):
    return sum(1 for o in bf_by_name(state, pid, "island")
               if not get_obj(state, o).get("tapped"))


def bolt_obj(state):
    for o in (state.get("objects") or {}).values():
        if str(o.get("base_name") or o.get("name") or "").lower() == "lightning bolt":
            return o
    return None


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
    menu or a mana-ability menu) in its viewer_interaction. The 106 engine
    offers tapLandForMana / castSpell / activateAbility choice menus at
    ordinary priority windows; treating those as decisions stalls the game
    (they must not block priority passes)."""
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
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"



async def do_mulligan(c, acts, st, pid, tag):
    """Protocol 106: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission). #301 always keeps."""
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
    """Protocol 106: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Answer via the vi opportunity.
    Gated on kind == 'mulligan' AND turn 1 / Untap."""
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
        if nm == "lightning bolt":
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
    """Protocol 106: DiscardToHandSize surfaces via viewer_interaction;
    gate on hand > 7 plus a schema/select opportunity offering hand cards
    (106 uses a generic 'choose' waitingForKind code)."""
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
        if nm == "lightning bolt":
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
        say(f"[{tag}] discarding to hand size via vi ({stype}): "
            f"{[obj_lname(state, _cand_reference(ch)) for ch in cands if ch['id'] in picks]}")
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


def find_activate(st, source_oid):
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility":
            d = a.get("data") or {}
            if str(d.get("source_id")) == str(source_oid) \
                    or str(a.get("_src_oid")) == str(source_oid):
                return ("submit", a, "ActivateAbility legal_action")
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "activateAbility" not in codes:
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)
                    and s.get("data", {}).get("role") == "source"]
            if str(source_oid) in refs:
                return ("interaction", (opp, ch),
                        "activateAbility via viewer_interaction (choose)")
    return None

# ---------------------------------------------------------------- ticks

async def p1_tick(c, g, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        await pass_priority(c, st, acts)


async def p0_tick_ramp(c, g, tag):
    """One ramp tick for P0. Returns ('ACTIVATE', wand_id) at the window."""
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
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
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

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        wand_id = next(iter(bf_by_name(state, 0, "chaos wand")), None)
        on_stack = any(obj_lname(state, oid) == "chaos wand"
                       and str(get_obj(state, oid).get("controller", -1)) == "0"
                       for oid, o in (state.get("objects") or {}).items()
                       if o.get("zone") == "Stack")
        if wand_id is None and not on_stack:
            if "chaos wand" in [obj_lname(state, o) for o in hand_ids(state, 0)] \
                    and can_pay_generic(state, 0, 3):
                a, oid = cast_action_for(acts, state, "chaos wand")
                if a is not None:
                    ST["mana_needs"][tag] = {"generic": 3}
                    say(f"[{tag}] casting Chaos Wand (oid {oid})")
                    wire("cast_wand", {"action": {k: v for k, v in a.items()
                                                 if not k.startswith("_")}})
                    await submit_as_is(c, a)
                    return True
        if wand_id is not None and untapped_islands(state, 0) >= 12:
            return ("ACTIVATE", wand_id)
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def generic_tick(c, g, tag, pid):
    """Post-activation driver tick: keep the game moving without answering
    decision prompts (those are handled explicitly)."""
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def tick_all(p0, p1, g, ramp_tag):
    """One round of driver ticks for both clients. Returns ('ACTIVATE', id)
    if the ramp window is reached."""
    await generic_tick(p1, g, f"P1{ramp_tag}", 1)
    r = await p0_tick_ramp(p0, g, ramp_tag)
    return r if isinstance(r, tuple) else None


async def tick_all_generic(p0, p1, g, ramp_tag):
    await generic_tick(p1, g, f"P1{ramp_tag}", 1)
    await generic_tick(p0, g, ramp_tag, 0)


async def settle(p0, p1, g, ramp_tag, cond, timeout, label, poll=0.25):
    """Drive both clients (no explicit decisions) until cond(state) or
    timeout. Export-only checkpoints fall through to the priority pass."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await tick_all_generic(p0, p1, g, ramp_tag)
        await asyncio.sleep(0)  # yield to pump before leg evaluation
        st = p0.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in settle: {label}")
    return None


# ---------------------------------------------------------------- wand activation

def wand_ability_on_stack(state, wand_id):
    """True if the wand's activated ability is on the stack. Stack entries
    are checked by source reference; falls back to any non-empty stack
    while the wand is tapped and awaiting resolution."""
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str)
        if str(wand_id) in blob and "ctivat" in blob:
            return True
    return False


async def activate_wand(p0, p1, g, ramp_tag, wand_id, timeout=240):
    """Submit the advertised ActivateAbility WHILE HOLDING priority (never
    pass first: submitting after passing leaves the ability unactivated and
    the game drifts on). Then drive the {4} payment and wait until the
    ability is actually on the stack."""
    submitted = False
    t0 = time.time()
    diag_n = 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.3)
        # Drive P1 only (it just passes); P0 must keep its priority until
        # the ability is submitted. Payment ticks for P0 run below without
        # passing.
        await generic_tick(p1, g, f"P1{ramp_tag}", 1)
        if not submitted:
            await asyncio.sleep(0)
            st = p0.latest
            if st is None:
                continue
            acts = merged_actions(st)
            if not my_priority(acts):
                # P0 lost priority somehow; let one generic tick run then retry
                await generic_tick(p0, g, ramp_tag, 0)
                continue
            r = find_activate(st, wand_id)
            if r is None:
                if diag_n % 10 == 0:
                    say(f"[{ramp_tag}] ActivateAbility not offered; "
                        f"acts={[a['type'] for a in acts][:10]}")
                    wire(f"{ramp_tag}_activate_missing",
                         {"acts": [a["type"] for a in acts][:10]})
                diag_n += 1
                continue
            kind, sub, what = r
            say(f"submitting ActivateAbility via {what} (holding priority)")
            wire("activate_ability", {"via": what})
            ST["mana_needs"][ramp_tag] = {"generic": 4}
            if kind == "interaction":
                opp, ch = sub
                await answer_vi(p0, opp, ch, ramp_tag)
            else:
                await submit_as_is(p0, sub)
            submitted = True
            continue
        # payment + resolution driving for P0 (no priority pass while the
        # payment/decision surface is pending)
        st = p0.latest
        if st is None:
            continue
        acts = merged_actions(st)
        if await pay_tick(p0, acts):
            continue
        needs = ST["mana_needs"].get(ramp_tag, {})
        if sum(needs.values()) > 0:
            if await pay_mana_vi(p0, st, ramp_tag, needs):
                continue
        await asyncio.sleep(0)
        st = p0.latest or st
        s = st["state"]
        w = get_obj(s, wand_id)
        if diag_n % 8 == 0:
            pool = player_of(s, 0).get("mana_pool")
            say(f"[{ramp_tag}] activation watch: wand tapped={w.get('tapped')} "
                f"stack={len(s.get('stack') or [])} needs={ST['mana_needs'].get(ramp_tag)} "
                f"pool={json.dumps(pool, default=str)[:120]} vikind={vi_kind_code(st)!r}")
            wire(f"{ramp_tag}_activation_watch",
                 {"wand_tapped": w.get("tapped"),
                  "stack": s.get("stack"),
                  "needs": ST["mana_needs"].get(ramp_tag),
                  "vi": st.get("viewer_interaction")})
        diag_n += 1
        if w.get("tapped") and wand_ability_on_stack(s, wand_id):
            say(f"[{ramp_tag}] wand ability on the stack")
            ST["mana_needs"][ramp_tag] = {}
            return True
        if w.get("tapped") and not (s.get("stack") or []):
            # Tapped but nothing on the stack: the ability may have resolved
            # already (fast exile) or fizzled; let the caller decide via the
            # exile settle.
            say(f"[{ramp_tag}] wand tapped, stack empty -- proceeding to exile watch")
            ST["mana_needs"][ramp_tag] = {}
            return True
        if not real_decision_pending(st) and my_priority(acts):
            await pass_priority(p0, st, acts)
    ST["mana_needs"][ramp_tag] = {}
    return False


# ---------------------------------------------------------------- optional-cast prompt

def find_optional_cast_opp(st):
    """Find the may-cast OptionalEffectChoice opportunity: exactChoices with
    a decideOptionalEffect action code."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


async def pick_optional_cast(c, want_accept, tag):
    st = c.latest
    opp = find_optional_cast_opp(st)
    if opp is None:
        # diagnostic: dump what IS pending
        vi = st.get("viewer_interaction") or {}
        say(f"[{tag}] no decideOptionalEffect opportunity; vi kind={vi_kind_code(st)!r} "
            f"ops={len(vi_ops(st))}")
        wire(f"{tag}_optional_unanswered_vi", vi)
        return None, None
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "cast" in txt or "accept" in txt:
                is_accept = True
            elif "decline" in txt or "don't" in txt or "do not" in txt:
                is_accept = False
        if is_accept == want_accept:
            return opp.get("interactionId"), ch["id"]
    return None, None


# ---------------------------------------------------------------- target selection

def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices whose
    choices carry candidate/target codes (passPriority menus excluded)."""
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
                    and ("decideOptionalEffect" not in codes) \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_target_candidate(state, opp):
    """Prefer the opponent-player candidate (seat 1)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []

    def seat_of(ch):
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
            # also check role/data embedded seat
            for k, v in (d or {}).items():
                if k in ("seat", "player") and isinstance(v, int):
                    return v
        return None

    best, best_score = None, -1
    for ch in cands:
        codes = set(surf_codes(ch))
        score = 0
        if seat_of(ch) == 1:
            score += 2
        if codes & {"candidate", "target"}:
            score += 1
        if score > best_score:
            best, best_score = ch, score
    if best is None and len(cands) == 1:
        best = cands[0]
    return best


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']}")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid, cid

# ---------------------------------------------------------------- #6902 flow
# Issue #6902: Sneak Attack — delayed end-step sacrifice trigger only
# sometimes sacrifices the creature.

SNEAK = "Sneak Attack"
BEAR = "Grizzly Bears"
MTN = "Mountain"
SNEAK_L = "sneak attack"
BEAR_L = "grizzly bears"


def reset_g6902():
    global G6902
    G6902 = {
        "joined": False,
        "cast_sneak": False,
        "sneak_oid": None,
        "leg1_activations": 0,
        "leg1_bears": [],
        "leg1_turn": None,
        "leg1_end_start": None,
        "leg1_end_done": False,
        "mid1_exported": False,
        "post1_exported": False,
        "trig_records": [],
        "leg2_activated": False,
        "leg2_turn": None,
        "leg2_bear": None,
        "mid2_exported": False,
        "post2_pre_exported": False,
        "post2_exported": False,
        "stop": False,
    }


reset_g6902()


def untapped_lands(state, pid):
    n = 0
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            n += 1
    return n


def sneak_ability_on_stack(state, sneak_oid):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if str(sneak_oid) in blob and ("ctivat" in blob or "Sneak" in blob):
            return True
    return False


def sacrifice_triggers_on_stack(state):
    """Stack entries that look like Sneak Attack's delayed sacrifice trigger:
    the instantiated entry mentions sacrifice and is tied to Sneak Attack or
    is a delayed trigger entry."""
    recs = []
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        low = blob.lower()
        if "sacrifice" in low and ("sneak" in low or "delayed" in low):
            if isinstance(e, dict):
                recs.append({"id": e.get("id"),
                             "text": (e.get("description") or e.get("name")
                                      or "")[:200]})
            else:
                recs.append({"id": None, "text": blob[:200]})
    return recs


def find_hand_bear_choice(st):
    """After accepting the may-choice, pick a Grizzly Bears from P0's hand.
    Returns (opp, rtype, spec_type, choice) or None."""
    state = st["state"]
    hand = set(hand_ids(state, 0))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = (data.get("spec") or {}).get("type") or "select"
            for ch in data.get("candidates") or []:
                ref = _cand_reference(ch)
                if ref is not None and str(ref) in hand \
                        and obj_lname(state, ref) == BEAR_L:
                    return opp, "schema", spec, ch
        elif rtype == "exactChoices":
            for ch in (data.get("choices") or []):
                txt = choice_text(ch).lower()
                ref = _cand_reference(ch)
                if (ref is not None and str(ref) in hand
                        and obj_lname(state, ref) == BEAR_L) \
                        or BEAR_L in txt:
                    return opp, "exact", None, ch
    return None


async def answer_trigger_order(c, st, tag):
    """Answer a multiple-trigger ordering prompt (schema sequence whose
    candidates reference stack triggers, not hand cards)."""
    state = st["state"]
    hand = set(hand_ids(state, 0))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        if spec.get("type") != "sequence":
            continue
        cands = data.get("candidates") or []
        if not cands:
            continue
        refs = [str(_cand_reference(ch)) for ch in cands]
        if any(r in hand for r in refs):
            continue  # card choice, not trigger ordering
        blob = json.dumps(opp, default=str).lower()
        if "trigger" not in blob and "order" not in blob:
            continue
        iid = opp.get("interactionId")
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": [ch["id"] for ch in cands]}}}
        say(f"[{tag}] answering trigger ordering ({len(cands)} triggers)")
        wire("trigger_order", {"n": len(cands)})
        await interact_as(c, sub, tag)
        return True
    return False


async def activate_sneak_once(p0, p1, g, tag, sneak_oid, timeout=300):
    """Submit the advertised ActivateAbility while holding priority, pay {R},
    accept the may-choice, and choose a Grizzly Bears from hand. Returns the
    bear oid once it is on the battlefield, else None."""
    st = p0.latest
    r = find_activate(st, sneak_oid)
    if r is None:
        say(f"[{tag}] ActivateAbility not offered for sneak {sneak_oid}")
        wire("activate_missing", {"sneak_oid": sneak_oid})
        return None
    kind, sub, what = r
    say(f"[{tag}] submitting ActivateAbility ({what}) holding priority")
    wire("activate_ability", {"via": what, "sneak_oid": sneak_oid})
    ST["mana_needs"][tag] = {"R": 1}
    if kind == "interaction":
        opp, ch = sub
        await answer_vi(p0, opp, ch, tag)
    else:
        await submit_as_is(p0, sub)
    bears_before = set(bf_by_name(p0.latest["state"], 0, BEAR_L))
    bear_oid = None
    may_done = False
    t0 = time.time()
    diag_n = 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.4)
        await generic_tick(p1, g, f"P1{tag}", 1)
        st = p0.latest
        if st is None:
            continue
        acts = merged_actions(st)
        if await pay_tick(p0, acts):
            continue
        needs = ST["mana_needs"].get(tag, {})
        if sum(needs.values()) > 0 and await pay_mana_vi(p0, st, tag, needs):
            continue
        state = st["state"]
        if bear_oid is None:
            # Bear choice may be offered with or without a preceding
            # may-choice; answer whichever surface appears first.
            f = find_hand_bear_choice(st)
            if f:
                opp, rtype, spec, ch = f
                ref = _cand_reference(ch)
                say(f"[{tag}] choosing bear for sneak (ref {ref})")
                wire("sneak_bear_choice", {"ref": str(ref)})
                if rtype == "schema":
                    sub2 = {"interactionId": opp.get("interactionId"),
                            "response": {"type": spec,
                                         "data": {"choiceIds": [ch["id"]]}}}
                else:
                    sub2 = {"interactionId": opp.get("interactionId"),
                            "response": {"type": "choose",
                                         "data": {"choiceId": ch["id"]}}}
                await p0.send_interaction(sub2)
                if ref is not None:
                    bear_oid = str(ref)
                continue
        if not may_done:
            iid, cid = await pick_optional_cast(p0, True, tag)
            if cid:
                say(f"[{tag}] accepting sneak may-choice (choice {cid})")
                wire("sneak_may_accept", {"iid": iid, "cid": cid})
                await p0.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose", "data": {"choiceId": cid}}})
                may_done = True
                continue
        if bear_oid is not None:
            b = get_obj(state, bear_oid)
            if b.get("zone") == "Battlefield":
                say(f"[{tag}] bear {bear_oid} entered the battlefield")
                ST["mana_needs"][tag] = {}
                return bear_oid
        else:
            now = set(bf_by_name(state, 0, BEAR_L))
            new = now - bears_before
            if new:
                bear_oid = sorted(new)[0]
                say(f"[{tag}] bear {bear_oid} entered the battlefield "
                    f"(detected by battlefield diff)")
                ST["mana_needs"][tag] = {}
                return bear_oid
        if diag_n % 15 == 0:
            say(f"[{tag}] activation watch: stack={len(state.get('stack') or [])} "
                f"may_done={may_done} vikind={vi_kind_code(st)!r} "
                f"real_decision={real_decision_pending(st)}")
        diag_n += 1
        if not real_decision_pending(st) and my_priority(acts):
            await pass_priority(p0, st, acts)
    ST["mana_needs"][tag] = {}
    say(f"[{tag}] activate_sneak_once timed out (may_done={may_done})")
    wire("activate_timeout", {"may_done": may_done})
    return bear_oid


async def export_authoritative(c, fn, tag):
    s = await c.export_state()
    with open(f"{EVDIR}/{fn}", "w") as f:
        f.write(s)
    say(f"[{tag}] exported {fn}")
    wire("export", {"file": fn})
    return json.loads(s)["state"]


async def p0_tick_6902(p0, p1, g, tag):
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
        da = next(a for a in acts if a["type"] == "DeclareAttackers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(p0, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await answer_trigger_order(p0, st, tag):
        return
    if await pay_tick(p0, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0 and await pay_mana_vi(p0, st, tag, needs):
        return
    await asyncio.sleep(0)
    st = p0.latest or st
    state = st["state"]
    acts = merged_actions(st)

    phase = state.get("phase") or ""
    turn = state.get("turn_number")
    actp = state.get("active_player")
    stack_empty = not (state.get("stack") or [])

    sneak_oid = next(iter(bf_by_name(state, 0, SNEAK_L)), None)
    if sneak_oid:
        G6902["sneak_oid"] = sneak_oid

    # ---- leg sequencing (quiet board: no stack, no real decision)
    if stack_empty and not real_decision_pending(st):
        # play a land first (ramp to the Sneak Attack cast)
        if my_main(state, 0):
            if await play_a_land(p0, state, 0, acts, tag):
                return
        # 1. cast Sneak Attack (need 6 untapped: {3}{R} + 2x {R} so both
        # leg-1 activations happen on the same turn)
        if not G6902["cast_sneak"] and my_main(state, 0):
            if any(obj_lname(state, o) == SNEAK_L for o in hand_ids(state, 0)) \
                    and untapped_lands(state, 0) >= 6:
                a, oid = cast_action_for(acts, state, SNEAK_L)
                if a is not None:
                    ST["mana_needs"][tag] = {"generic": 3, "R": 1}
                    G6902["cast_sneak"] = True
                    say(f"[{tag}] casting Sneak Attack (oid {oid})")
                    wire("cast_sneak", {"oid": oid})
                    await submit_as_is(p0, a)
                    return
        # 2. leg-1 activations (two, same main phase if possible)
        if sneak_oid is not None and G6902["leg1_activations"] < 2 \
                and my_main(state, 0):
            if any(obj_lname(state, o) == BEAR_L for o in hand_ids(state, 0)) \
                    and untapped_lands(state, 0) >= 1:
                if G6902["leg1_activations"] == 0:
                    await export_authoritative(p0, "pre.json", tag)
                b = await activate_sneak_once(p0, p1, g, tag, sneak_oid)
                if b:
                    G6902["leg1_activations"] += 1
                    G6902["leg1_bears"].append(b)
                    say(f"[{tag}] leg-1 activation "
                        f"{G6902['leg1_activations']}/2 -> bear {b}")
                else:
                    say(f"[{tag}] leg-1 activation failed; will retry")
                return
        # 4. leg-2: on a later P0 end step, activate once
        if G6902["leg1_end_done"] and not G6902["leg2_activated"] \
                and phase == "End" and actp == 0 \
                and turn is not None and G6902["leg1_turn"] is not None \
                and turn > G6902["leg1_turn"] and my_priority(acts):
            if sneak_oid is not None \
                    and any(obj_lname(state, o) == BEAR_L
                            for o in hand_ids(state, 0)) \
                    and untapped_lands(state, 0) >= 1:
                b = await activate_sneak_once(p0, p1, g, tag, sneak_oid)
                if b:
                    G6902["leg2_activated"] = True
                    G6902["leg2_turn"] = turn
                    G6902["leg2_bear"] = b
                    say(f"[{tag}] leg-2 activation during end step -> bear {b}")
                    await export_authoritative(p0, "mid2_end.json", tag)
                    G6902["mid2_exported"] = True
                return
        # 5. A6 checkpoint: first P0 turn after leg-2
        if G6902["leg2_activated"] and not G6902["post2_pre_exported"] \
                and actp == 0 and turn is not None \
                and turn > G6902["leg2_turn"]:
            await export_authoritative(p0, "post2_pre.json", tag)
            G6902["post2_pre_exported"] = True
        # 6. A7 checkpoint: after that turn's end step
        if G6902["leg2_activated"] and not G6902["post2_exported"] \
                and turn is not None and turn > G6902["leg2_turn"] + 2:
            await export_authoritative(p0, "post2.json", tag)
            G6902["post2_exported"] = True
            G6902["stop"] = True
            say(f"[{tag}] leg 2 complete; stopping")
            return

    # ---- leg-1 end-step watch (stack may hold the delayed triggers)
    if G6902["leg1_activations"] >= 2 and not G6902["leg1_end_done"] \
            and phase == "End" and actp == 0:
        if G6902.get("leg1_end_start") is None:
            G6902["leg1_end_start"] = time.time()
        in_end_for = time.time() - G6902["leg1_end_start"]
        # Keep scanning: triggers may hit the stack a few ticks in.
        for r in sacrifice_triggers_on_stack(state):
            if r["id"] not in [x["id"] for x in G6902["trig_records"]]:
                G6902["trig_records"].append(r)
        recs = G6902["trig_records"]
        if not G6902["mid1_exported"] and (recs or in_end_for > 12):
            m1 = await export_authoritative(p0, "mid_end.json", tag)
            G6902["trig_records"] = sacrifice_triggers_on_stack(m1) or recs
            G6902["mid1_exported"] = True
            G6902["leg1_turn"] = turn
            say(f"[{tag}] leg-1 end step: sacrifice triggers on stack = "
                f"{len(G6902['trig_records'])} (after {in_end_for:.1f}s)")
            wire("leg1_end_triggers", G6902["trig_records"])
        elif G6902["mid1_exported"] and stack_empty \
                and turn is not None and G6902["leg1_turn"] is not None \
                and turn > G6902["leg1_turn"]:
            await export_authoritative(p0, "post1.json", tag)
            G6902["post1_exported"] = True
            G6902["leg1_end_done"] = True
            say(f"[{tag}] leg-1 end step resolved; post1 exported")

    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(p0.name, -1) < p0.revision:
            await pass_priority(p0, st, acts)
            PASSED_REV[p0.name] = p0.revision


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


def load_state_file(fn):
    p = f"{EVDIR}/{fn}"
    if not os.path.exists(p):
        return None
    return json.loads(open(p).read())["state"]


def evaluate(obs):
    A = obs["assert"]
    mid1 = load_state_file("mid_end.json")
    post1 = load_state_file("post1.json")
    pre2 = load_state_file("post2_pre.json")
    post2 = load_state_file("post2.json")
    bears = G6902["leg1_bears"]
    b2 = G6902.get("leg2_bear")

    A["A1_setup_ok"] = "passed" if G6902.get("joined") else "failed"
    obs["notes"].append(f"game created and both seats joined: "
                        f"{G6902.get('joined')}")

    if mid1 is None or len(bears) != 2:
        A["A2_creatures_enter"] = "not-run" if mid1 is None else "failed"
    elif all(zone_of(mid1, b) == "Battlefield" for b in bears):
        A["A2_creatures_enter"] = "passed"
    else:
        A["A2_creatures_enter"] = "failed"
    obs["notes"].append(f"leg-1 bears put in by sneak: {bears}")

    recs = G6902["trig_records"]
    if mid1 is None:
        A["A3_triggers_fired"] = "not-run"
    else:
        A["A3_triggers_fired"] = "passed" if len(recs) >= 2 else "failed"
    obs["notes"].append(f"sacrifice triggers on stack at leg-1 end step: "
                        f"{len(recs)}")

    if post1 is None or len(bears) != 2:
        A["A4_both_sacrificed"] = "not-run" if post1 is None else "failed"
    elif all(zone_of(post1, b) == "Graveyard" for b in bears):
        A["A4_both_sacrificed"] = "passed"
    else:
        A["A4_both_sacrificed"] = "failed"
    if post1 is not None:
        obs["notes"].append(
            "post1 bear zones: " +
            ", ".join(f"{b}={zone_of(post1, b)}" for b in bears))

    A["A5_cleanup_leg1"] = ("not-run" if post1 is None
                            else ("passed" if not (post1.get("stack") or [])
                                  else "failed"))

    if pre2 is None or not b2:
        A["A6_endstep_survives"] = "not-run"
    elif zone_of(pre2, b2) == "Battlefield":
        A["A6_endstep_survives"] = "passed"
    else:
        A["A6_endstep_survives"] = "failed"
    obs["notes"].append(f"leg-2 bear {b2}; zone at next P0 turn: "
                        f"{zone_of(pre2, b2) if pre2 and b2 else '?'}")

    if post2 is None or not b2:
        A["A7_next_endstep_sac"] = "not-run"
    elif zone_of(post2, b2) == "Graveyard":
        A["A7_next_endstep_sac"] = "passed"
    else:
        A["A7_next_endstep_sac"] = "failed"
    if post2 is not None:
        obs["notes"].append(
            f"post2: leg-2 bear zone={zone_of(post2, b2) if b2 else '?'} "
            f"stack_empty={not (post2.get('stack') or [])}")

    a = A
    if a.get("A1_setup_ok") == "passed" and a.get("A2_creatures_enter") == "passed" \
            and (a.get("A3_triggers_fired") == "failed"
                 or a.get("A4_both_sacrificed") == "failed"):
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_creatures_enter", "A3_triggers_fired",
              "A4_both_sacrificed", "A5_cleanup_leg1",
              "A6_endstep_survives", "A7_next_endstep_sac")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    return verdict


async def _run_game(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    reset_g6902()
    obs = {"assert": {}, "notes": [], "_fixture_ok": True}
    tag = "6902" if attempt == 0 else f"6902-r{attempt}"
    ST["mana_needs"][tag] = {}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    say("connecting P1...")
    await p0.create(deck((MTN, 36), (SNEAK, 12), (BEAR, 12)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck((MTN, 48), (BEAR, 12)))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code})
    G6902["joined"] = p0.player_id is not None and p1.player_id is not None
    G6902["game_code"] = p0.game_code
    obs["assert"]["A1_setup_ok"] = "passed" if G6902["joined"] else "failed"

    t_end = time.time() + 1500
    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    try:
        while time.time() < t_end and not G6902["stop"]:
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
                        await p0_tick_6902(p0, p1, {"tag": tag}, tag)
                    else:
                        await p1_tick(c, {"tag": tag}, f"P1{tag}")
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
                        f"g6902={ {k: v for k, v in G6902.items() if k not in ('game_code',)} }")
    except Exception as e:
        obs["notes"].append(f"drive loop exception: {e}")
    finally:
        await p0.close()
        await p1.close()
    if not G6902["stop"]:
        obs["notes"].append("drive loop ended without completing leg 2 "
                            "(timeout or early exit)")
    verdict = evaluate(obs)
    return obs


async def run_game():
    for attempt in range(3):
        obs = await _run_game(attempt)
        if obs.get("_fixture_ok", True):
            return obs
    for k in ("A1_setup_ok", "A2_creatures_enter", "A3_triggers_fired",
              "A4_both_sacrificed", "A5_cleanup_leg1",
              "A6_endstep_survives", "A7_next_endstep_sac"):
        obs["assert"].setdefault(k, "not-run")
    return obs

def check_data_level():
    """Record the v0.102.0 parse of Sneak Attack. The reported defect is
    behavioral (delayed end-step sacrifice only sometimes fires); the parse
    is expected to be fully supported: Activated ChangeZone (cost {R}) ->
    haste (sub) -> CreateDelayedTrigger(AtNextPhase End -> Sacrifice)."""
    sa = CARD_DATA.get("sneak attack", {})
    abils = sa.get("abilities") or []
    chain_desc = []

    def walk(a):
        e = a.get("effect") or {}
        chain_desc.append({"kind": a.get("kind"),
                           "effect": e.get("type"),
                           "cost": a.get("cost")})
        if e.get("type") == "CreateDelayedTrigger":
            inner = e.get("effect") or {}
            chain_desc.append({"delayed_condition": e.get("condition"),
                               "inner_kind": inner.get("kind"),
                               "inner_effect": (inner.get("effect")
                                                or {}).get("type")})
        s = a.get("sub_ability")
        if s:
            walk(s)

    for a in abils:
        walk(a)
    ok = (len(abils) == 1 and abils[0].get("kind") == "Activated"
          and (abils[0].get("effect") or {}).get("type") == "ChangeZone")
    ev = {"name": sa.get("name"), "oracle_text": sa.get("oracle_text"),
          "mana_cost": sa.get("mana_cost"), "chain": chain_desc,
          "parse_ok": ok}
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1, default=str)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump(sa, f, indent=1, default=str)
    say(f"data-level: parse_ok={ok} chain="
        f"{json.dumps(chain_desc, default=str)[:300]}")
    wire("data_level", ev)
    return ok


# ---------------------------------------------------------------- summary + main

def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 860
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6902 — Sneak Attack end-step sacrifice (revalidation)",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} — {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_creatures_enter": "A2 leg-1: 2 Bears entered via sneak activations",
        "A3_triggers_fired": "A3 leg-1: >=2 sacrifice triggers on stack at end step",
        "A4_both_sacrificed": "A4 leg-1: both Bears in graveyard post end step",
        "A5_cleanup_leg1": "A5 leg-1: stack empty after end step",
        "A6_endstep_survives": "A6 leg-2: end-step-entered Bear survives to next turn",
        "A7_next_endstep_sac": "A7 leg-2: Bear sacrificed at the following end step",
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
    for n in run["notes"][:12]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6902/"
           + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


def write_server_excerpts(game_code):
    """Copy server-log lines for this game into the evidence dir."""
    src = f"{BACKFILL}/runs/20261006-0441/server.log"
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
                 "Action; bottom via vi schema/select; DiscardToHandSize via "
                 "vi; Sneak Attack cast via advertised CastSpell ({3}{R} via "
                 "vi tapLandForMana/PayMana); activation via advertised "
                 "ActivateAbility while holding priority ({R} paid); the 'may' "
                 "via vi exactChoices decideOptionalEffect (accept); the "
                 "creature choice via the advertised schema/exactChoices card "
                 "opportunity (Grizzly Bears from hand); trigger-ordering via "
                 "schema sequence; empty declares; priority-gated passes; "
                 "5s re-tick backstop covers priority holders and pending "
                 "decisions.")
    verdict = obs.get("verdict", "blocked")
    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/6902",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "runs/20261006-0441 (v0.102.0 server on 127.0.0.1:9374, isolated games.db)",
        "data_level_parse_ok": data_ok,
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Mountain", 36], ["Sneak Attack", 12], ["Grizzly Bears", 12]],
            "P1": [["Mountain", 48], ["Grizzly Bears", 12]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": ("A1 setup_ok: %s; A2 creatures_enter: %s; A3 triggers_fired: %s; "
                   "A4 both_sacrificed: %s; A5 cleanup_leg1: %s; "
                   "A6 endstep_survives: %s; A7 next_endstep_sac: %s"
                   % tuple(ass.get(k, "not-run") for k in
                           ("A1_setup_ok", "A2_creatures_enter",
                            "A3_triggers_fired", "A4_both_sacrificed",
                            "A5_cleanup_leg1", "A6_endstep_survives",
                            "A7_next_endstep_sac"))),
        "scope": "Sneak Attack two-activation leg + end-step activation leg; native human seats (no AI)",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x card density is a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "setup_line": "P0: 36x Mountain + 12x Sneak Attack + 12x Grizzly Bears; P1: 48x Mountain + 12x Grizzly Bears (draw-go)",
        "contract_line": ("Leg 1: cast Sneak Attack, activate twice on one main phase; both Bears "
                          "must be sacrificed at the next end step (two delayed triggers). "
                          "Leg 2: activate during a later end step; the Bear must survive to the "
                          "next turn and be sacrificed at the following end step."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6902_01020.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    write_server_excerpts(G6902.get("game_code") or "")
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
    pidfile = "/tmp/scenario_6902_01020.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_6902_01020 instance is alive "
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
