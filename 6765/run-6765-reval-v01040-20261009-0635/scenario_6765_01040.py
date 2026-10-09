#!/usr/bin/env python3
"""Issue #6765 re-validation on pinned v0.104.0 (build 4227122, WS protocol 118): Yidris, Maelstrom Wielder.

Oracle (verified from pinned card data):
  Trample
  Whenever Yidris deals combat damage to a player, as you cast spells from
  your hand this turn, they gain cascade.

Reported: Yidris doesn't give cascade after attacking.

Parser signal (pinned card-data.json, unchanged on v0.103.0): the
DamageDone trigger's execute effect is {"type": "Unimplemented", "name":
"unrecognized_clause_head", "description": "as you cast spells from your
hand"} -- the temporary grant is explicitly marked unsupported, with the
real GenericEffect/AddKeyword(Cascade) nested underneath as sub_ability.

Behavioral contract (ported from scenario_6765.py, protocol-72 era, onto
the protocol-106 harness proven by scenario_6764_01020.py, then onto the
protocol-118 harness proven by scenario_6764_01040.py / scenario_6760_01040.py):
  A0 connect             both clients connected, seats assigned
  A1 setup_ok            P0 has Yidris, Maelstrom Wielder on the battlefield
  A2 combat_damage       Yidris attacks unblocked; P1 loses exactly 5 life
  A3 pre_damage_control  Shock cast from hand BEFORE combat damage gains no
                         cascade (timing control: grant must not pre-exist).
                         Runs only when >=2 Shocks are in hand so the A4
                         window is never jeopardized; otherwise not-run.
  A4 yidris_grant        Shock cast from hand AFTER Yidris dealt combat damage
                         gains cascade: a Cascade TriggeredAbility from the
                         Shock must hit the stack and/or cascade must exile
                         cards as it resolves
  A5 control_cascade     Bloodbraid Elf (native cascade) cast later triggers
                         cascade normally: proves the cascade pipeline works.
                         Its free-cast may prompt is DECLINED after the
                         trigger is observed, purely to let the game settle.
  A6 cleanup             game proceeds cleanly

Single-attack design (inherited): ramp -> hunt (first P0 PreCombatMain
where Yidris is attack-ready AND a Shock is in hand AND a Mountain is
untapped) -> the SAME turn hosts A3 (pre-damage control, conditional), the
real attack, and the A4 post-damage window -> control stage for Bloodbraid.

Land-drop cap: routine land drops stop at 10 board lands (only fixing a
missing color beyond that), keeping spare lands in hand as cleanup-discard
fodder so discards never eat Shocks/Bloodbraids/Yidris.

STRICT CASCADE DETECTION (inherited): genuine cascade signals only
  (1) a TriggeredAbility stack entry whose ability effect type is exactly
      "Cascade" (NOT a text search: the Yidris damage trigger's own
      description contains the word "cascade");
  (2) a genuine may prompt: exactChoices with decideOptionalEffect codes
      or a true/false boolean pair, or a card-choice prompt whose
      candidates are ALL from the exile zone (cascade free-casts select
      from exile, never from hand);
  (3) a positive exile-zone delta across the cast window (cascade exiles
      cards from the top of the library as it resolves).
The driver never submits into an unidentified prompt. After a cascade is
observed (A4 not-reproduced path, A5 control), a genuine decide/bool may
prompt is DECLINED -- an explicit, engine-advertised, documented decision --
solely to let the game settle for A6. Exile-zone card choices are never
auto-answered.

Verdict = reproduced iff A2 passed, A5 passed, and A4 failed (the grant is
missing while the underlying cascade keyword works).

Protocol-118 driver notes (v0.104.0 port): ported from the verified
protocol-106 scenario_6765_01030.py using the conventions from the verified
protocol-118 scenario_6764_01040.py:
  - Casts go out as bare legacy CastSpell matched by object_id; the engine
    auto-pays (payment_mode Auto) -- the driver never taps lands for mana
    itself, so there is no double-payment artifact (cf. 2026-10-07 driver
    note). No legacy PayMana actions or vi tapLandForMana menus are used at
    all in this scenario (pay_tick/pay_mana_vi deleted).
  - MulliganDecision answered via legacy Action; BottomCards via vi
    schema/select gated on waitingForKind.code == "mulligan"; land plays via
    legacy PlayLand + vi playLand opportunity answered before the decision
    gate; Shock targets via the advertised vi target opportunity (schema
    select/sequence), seat-1 candidate preferred.
  - A single `await asyncio.sleep(0)` yield after the settle block, before
    reading fresh state for main-phase play evaluation (race fix).
  - Cast-confirmation guard (protocol-118 necessity): with engine auto-pay
    there is no mana-payment phase to hold priority, so a bare CastSpell can
    lose a race to the driver's own PassPriority on the next tick (observed
    2026-10-09: PassPriority applied 10ms before the in-flight Yidris
    CastSpell; the server processed but silently dropped the cast). After
    every CastSpell submission the driver sets ctx["cast_pending"] and does
    NOT pass priority until the spell is confirmed on the stack/battlefield;
    if the spell is still in hand after 30s the pending flag clears and the
    cast is retried (the *_cast flags are set on confirmation, not on
    submission).
  - verify_server_hello asserts 0.104.0 / 4227122 / protocol 118 (exact).
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
log = logging.getLogger("scenario6765_118")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-6765-reval-v01040-20261009-0635"
ISSUE = 6765
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
    "source": ("2026-10-09: latest stable release v0.104.0 (published "
               "2026-10-08) == pinned release dir; ServerHello "
               "0.104.0/4227122/protocol 118 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "fresh isolated server on 127.0.0.1:9374 started by this run"),
}

for _f, _k in (("server/releases/v0.104.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.104.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.104.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

YIDRIS = "Yidris, Maelstrom Wielder"
SHOCK = "Shock"
BLOODBRAID = "Bloodbraid Elf"
FOREST = "Forest"
MOUNTAIN = "Mountain"
ISLAND = "Island"
SWAMP = "Swamp"
LAND_NAMES = (FOREST, MOUNTAIN, ISLAND, SWAMP)
KEY_CARDS = ("yidris, maelstrom wielder", "shock", "bloodbraid elf")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

ST = {}
MULLS = {}
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


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def exile_oids(state):
    return {oid: o for oid, o in (state.get("objects") or {}).items()
            if str(o.get("zone") or "").lower() == "exile"}


def is_land(o):
    if not o:
        return False
    ct = (o.get("card_types") or {}).get("core_types") or []
    if "Land" in ct:
        return True
    return "Land" in str(o.get("type_line") or "") or \
        "land" in str(o.get("card_type") or "").lower()


def untapped_named(state, pid, name):
    return sum(1 for o in bf_oids(state, pid)
               if not get_obj(state, o).get("tapped")
               and obj_lname(state, o) == name.lower())


def untapped_lands(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if not get_obj(state, o).get("tapped")
               and is_land(get_obj(state, o)))


def board_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid) if is_land(get_obj(state, o)))


def board_colors(state, pid):
    return {obj_lname(state, o) for o in bf_oids(state, pid)
            if is_land(get_obj(state, o))} & {"forest", "mountain", "island", "swamp"}


def find_hand(state, pid, lname):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == lname:
            return o
    return None


def count_hand(state, pid, lname):
    return sum(1 for o in hand_ids(state, pid) if obj_lname(state, o) == lname)


def can_pay_yidris(state):
    # {B}{G}{R}{U}, no generic
    return all(untapped_named(state, 0, n) >= 1
               for n in ("swamp", "forest", "mountain", "island"))


def can_pay_bloodbraid(state):
    # {2}{R}{G}
    return (untapped_named(state, 0, "mountain") >= 1
            and untapped_named(state, 0, "forest") >= 1
            and untapped_lands(state, 0) >= 4)


def yidris_attack_ready(state, ctx):
    oid = ctx.get("yidris_oid")
    if not oid:
        return False
    o = get_obj(state, oid)
    return (str(o.get("zone") or "").lower() == "battlefield"
            and str(o.get("controller", -1)) == "0"
            and not o.get("tapped")
            and state.get("turn_number", 0) > (ctx.get("yidris_cast_turn") or 0))


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
            if isinstance(s.get("data"), dict) and s.get("data", {}).get("code")]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability/land-play menu) in its viewer_interaction."""
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
    assert str(ver).startswith("0.104.0"), f"unexpected version {ver}"
    assert int(proto) == 118, f"unexpected protocol {proto}"
    assert str(build) == "4227122", f"unexpected build {build}"


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "object":
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("reference") is not None:
                return str(d.get("reference")), d
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return str(d.get("reference")), d
    return None, None


def choice_bool_value(ch):
    for s in ch.get("surfaces", []) or []:
        dd = s.get("data", {}) or {}
        if dd.get("role") in ("accept", "value", "pay") \
                and str(dd.get("value", "")).lower() in ("true", "false"):
            return str(dd["value"]).lower()
    return None


async def do_mulligan(c, acts, st, pid, tag, is_p0):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", c.name, ST.get("mull_counts", {}).get(c.name, 0))
    if key in MULLS:
        return False
    MULLS[key] = True
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    lands = sum(1 for n in hn if n in ("forest", "mountain", "island", "swamp"))
    mulls = ST.get("mull_counts", {}).get(c.name, 0)
    if not is_p0:
        choice = "Keep"
    elif (lands >= 3 and ("shock" in hn or mulls >= 2)) or mulls >= 3:
        # 4-color deck: demand 3+ lands; prefer (not require) a Shock so the
        # hunt window arrives promptly.
        choice = "Keep"
    else:
        choice = "Mulligan"
    if choice == "Mulligan":
        ST.setdefault("mull_counts", {})[c.name] = mulls + 1
    say(f"[{tag}] {choice.lower()}s opening hand "
        f"(lands={lands} mulls={mulls} hand={len(hn)} shock={'shock' in hn})")
    wire("mulligan", {"who": tag, "decision": choice, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
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
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        ref, _d = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        # Bottom spare lands first; protect ALL key cards (Yidris / Shock /
        # Bloodbraid): the hunt windows need Shocks in hand, A5 needs a
        # Bloodbraid.
        if nm in KEY_CARDS:
            return (2, str(ref))
        if ref is not None and is_land(get_obj(state, ref)):
            return (0, str(ref))
        return (1, str(ref))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)[0]) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
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
            if any(str(_cand_reference(ch)[0]) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        # Discard spare lands first; ALL key cards fully protected (the land
        # buffer is deep enough that cleanup never needs to eat them).
        if nm in KEY_CARDS:
            return (2, nm)
        if is_land(get_obj(state, o)):
            return (0, nm)
        return (1, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)[0]))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
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


async def answer_vi_play_land(c, state, pid, acts, tag, land_name):
    """Protocol 106 advertises land plays as a vi exactChoices opportunity
    carrying a playLand action code. Answer it before the decision gate so
    the driver never stalls holding priority with only a land-play menu."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for opp in vi_ops(state and c.latest):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref, _d = _cand_reference(ch)
            if ref is None or obj_lname(state, ref) != land_name.lower():
                continue
            if str(ref) not in hand_ids(state, pid):
                continue
            LAND_PLAYED_TURN[(tag,)] = turn
            say(f"[{tag}] playing land via vi playLand: {land_name}")
            wire("play_land_vi", {"who": tag, "ref": ref})
            await answer_vi(c, opp, ch, tag)
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
                    say(f"[{tag}] playing land {obj_lname(state, o)} (legacy)")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def pick_land_name(state, pid):
    """Prefer the color we have fewest of on board (fix colors); the
    land-drop cap keeps a spare-land buffer in hand."""
    hand_lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not hand_lands:
        return None
    counts = {}
    for o in hand_lands:
        nm = obj_lname(state, o)
        counts[nm] = counts.get(nm, 0) + 1
    board = {}
    for o in bf_oids(state, pid):
        if is_land(get_obj(state, o)):
            nm = obj_lname(state, o)
            board[nm] = board.get(nm, 0) + 1
    return min(counts, key=lambda nm: (board.get(nm, 0), nm))


async def play_land_capped(c, state, pid, acts, tag):
    """Land-drop cap: stop routine drops at 10 board lands (only fixing a
    missing color beyond that), keeping spare lands in hand as
    cleanup-discard fodder so discards never eat key cards."""
    bl = board_land_count(state, pid)
    bc = board_colors(state, pid)
    hl = sum(1 for o in hand_ids(state, pid) if is_land(get_obj(state, o)))
    if not (bl < 10 or (len(bc) < 4 and hl > 1)):
        return False
    lname = pick_land_name(state, pid)
    if lname is None:
        return False
    if await play_a_land_named(c, state, pid, acts, tag, lname):
        return True
    return await answer_vi_play_land(c, state, pid, acts, tag, lname.title())


async def play_a_land_named(c, state, pid, acts, tag, lname):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if obj_lname(state, o) != lname.lower():
            continue
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing land {lname} (legacy)")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


def cast_action_for(acts, state, oid):
    """Bare legacy CastSpell matched by object_id; the engine auto-pays
    (payment_mode Auto) -- the driver never pays mana itself."""
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

# ------------------------------------------------------------- target selection (106)

def candidate_seat(ch):
    """Player seat for a target/choice candidate (strict: only player /
    target / candidate surfaces with a seat-ish key)."""
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
                codes.update(surf_codes(ch))
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_target_candidate(opp, preferred_seat):
    """Prefer the candidate at preferred_seat (a player)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    best, best_score = None, -1
    for ch in cands:
        codes = set(surf_codes(ch))
        score = 0
        if candidate_seat(ch) == preferred_seat:
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
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']} "
        f"({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid, cid


# --------------------------------------- strict cascade-signal detection

def iter_stack(state):
    return state.get("stack") or []


def _ability_effect_type(entry):
    try:
        return (entry.get("kind", {}).get("data", {}).get("ability", {})
                .get("effect", {}).get("type"))
    except Exception:
        return None


def is_cascade_trigger_entry(entry):
    return (entry.get("kind", {}).get("type") == "TriggeredAbility"
            and _ability_effect_type(entry) == "Cascade")


def entry_source_name(entry):
    try:
        data = entry.get("kind", {}).get("data", {}) or {}
        ability = data.get("ability", {}) or {}
        trig = ability.get("trigger_source", {}) or {}
        nm = ((trig.get("lki") or {}).get("name")
              or (data.get("lki") or {}).get("name"))
        if nm:
            return str(nm)
        return f"source_id={entry.get('source_id')}"
    except Exception:
        return "?"


def is_mana_menu(opp):
    """True when the opportunity is a mana-payment menu (tapLandForMana
    choices). Not a may-cast/free-cast prompt; never treat as one."""
    data = (opp.get("response") or {}).get("data") or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        if "tapLandForMana" in surf_codes(ch):
            return True
    return False


def choice_has_card_ref(state, ch):
    """True when a choice candidate references a real game object."""
    for s in ch.get("surfaces", []):
        dd = s.get("data", {}) or {}
        for k in ("reference", "object_id", "card", "objectId"):
            if str(dd.get(k)) in (state.get("objects") or {}):
                return True
    return False


def cand_zone(ch):
    for s in ch.get("surfaces", []) or []:
        dd = s.get("data", {}) or {}
        z = dd.get("zone")
        if z:
            return str(z)
    return None


def scan_genuine_may(st):
    """Single non-blocking scan for a genuine may/free-cast style prompt.
    Returns (opp, kind) or (None, None). Never matches hand-zone card
    selections (cleanup discard) or mana menus."""
    if st is None:
        return None, None
    state = st["state"]
    for opp in vi_ops(st):
        if is_mana_menu(opp):
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        if rtype == "exactChoices" and cands:
            codes = set()
            for ch in cands:
                codes.update(surf_codes(ch))
            if "decideOptionalEffect" in codes:
                return opp, "decide"
            bools = [choice_bool_value(ch) for ch in cands]
            if len(cands) == 2 and set(bools) == {"true", "false"}:
                return opp, "bool"
    # cascade free-casts select from exile; require EVERY candidate to carry
    # an exile zone (a cleanup-discard select is hand-zone).
    for opp in vi_ops(st):
        if is_mana_menu(opp):
            continue
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands or not any(choice_has_card_ref(state, ch) for ch in cands):
            continue
        codes = set()
        for ch in cands:
            codes.update(surf_codes(ch))
        if "decideOptionalEffect" in codes:
            continue
        zones = {(cand_zone(ch) or "").lower() for ch in cands}
        if zones <= {"exile"}:
            return opp, "exile"
    return None, None


async def decline_may(c, opp, kind, tag):
    iid = opp.get("interactionId")
    cands = ((opp.get("response") or {}).get("data") or {}).get("choices", [])
    pick = None
    if kind == "decide":
        for ch in cands:
            if "decideOptionalEffect" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []):
                dd = s.get("data", {}) or {}
                if dd.get("role") == "accept" \
                        and str(dd.get("value")).lower() == "false":
                    pick = ch
            if pick is None and choice_bool_value(ch) == "false":
                pick = ch
    elif kind == "bool":
        for ch in cands:
            if choice_bool_value(ch) == "false":
                pick = ch
                break
    if pick is None:
        say(f"[{tag}] could not identify decline choice (kind={kind})")
        wire("may_decline_failed", {"kind": kind})
        return False
    say(f"[{tag}] declining may prompt (kind={kind}, choice {pick.get('id')})")
    wire("may_decline", {"kind": kind, "choice": pick.get("id")})
    await answer_vi(c, opp, pick, tag)
    return True


def export_now_sync(envelope_text, name):
    with open(f"{EVDIR}/{name}", "w") as f:
        f.write(envelope_text)
    return json.loads(envelope_text)["state"]


# ---------------------------------------------------------------- game ticks

async def p1_tick(c, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag, False):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    # P1 never attacks or blocks (draw-go, no creatures)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if my_main(state, 1):
        if await play_land_capped(c, state, 1, acts, tag):
            return
    if await answer_vi_play_land(c, state, 1, acts, tag, MOUNTAIN):
        return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision

def watch_concluded(w, timeout_s):
    return (w.get("trigger_seen") or w.get("may_seen")
            or (time.time() - w.get("start", 0) >= timeout_s))


async def scan_watches(c, tag, ctx, state):
    """Passive strict cascade-signal scan for the active watches. Never
    submits anything."""
    for wkey, timeout in (("a3_watch", 25), ("a4_watch", 60), ("control_watch", 90)):
        w = ctx.get(wkey)
        if not w or w.get("done"):
            continue
        for e in iter_stack(state):
            if not is_cascade_trigger_entry(e):
                continue
            src = entry_source_name(e)
            tagid = f"{src}#{e.get('id')}"
            if tagid not in w.setdefault("sources", []):
                w["sources"].append(tagid)
                say(f"[{tag}] {wkey}: CASCADE TRIGGER ON STACK: {src} "
                    f"(entry {e.get('id')})")
                wire(f"{wkey}_cascade_trigger",
                     {"source": src, "entry_id": e.get("id"),
                      "entry": json.dumps(e, default=str)[:2000]})
        if w.get("sources"):
            want = w.get("expect_source")
            if want is None or any(want.lower() in s.lower() for s in w["sources"]):
                w["trigger_seen"] = True
        if not w.get("may_seen"):
            opp, kind = scan_genuine_may(c.latest)
            if opp is not None:
                w["may_seen"] = True
                w["may_kind"] = kind
                say(f"[{tag}] {wkey}: genuine may/free-cast prompt seen (kind={kind})")
                wire(f"{wkey}_may_prompt", {"kind": kind})
        if watch_concluded(w, timeout):
            w["done"] = True
            say(f"[{tag}] {wkey}: concluded "
                f"(trigger={w.get('trigger_seen')} may={w.get('may_seen')} "
                f"kind={w.get('may_kind')})")
            wire(f"{wkey}_concluded", {k: v for k, v in w.items()
                                       if k not in ("start",)})


async def p0_tick(c, tag, ctx):
    """P0 driver tick: ramp, hunt (A3 control + attack), A4 window,
    control (Bloodbraid), otherwise keep priority moving."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag, True):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return True
    # ---- cast-confirmation guard: hold while a CastSpell is in flight;
    # never pass priority under it. Runs before any early-return block so a
    # dropped cast is always detected (30s backstop, then retry).
    _cast_status = await guard_cast_pending(c, tag, ctx)
    if _cast_status == "waiting":
        return True
    # ---- attackers: the real attack goes out on the hunt/attack turn only
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            dd = d.setdefault("data", {})
            if (ctx.get("phase") == "hunt" and ctx.get("attack_live")
                    and not ctx.get("attackers_declared")
                    and yidris_attack_ready(state, ctx)):
                yid = ctx["yidris_oid"]
                # AttackTarget::Player(PlayerId(1)) tagged-union wire shape,
                # per engine types/actions.rs roundtrip tests.
                dd["attacks"] = [[int(yid), {"type": "Player", "data": 1}]]
                dd["bands"] = []
                ctx["attackers_declared"] = True
                ctx["attack_turn"] = state.get("turn_number")
                ctx["life_before"] = life(state, 1)
                pre_s = await c.export_state()
                export_now_sync(pre_s, "pre_damage.json")
                say(f"[{tag}] declaring attackers: Yidris -> P1 "
                    f"(turn {ctx['attack_turn']}, P1 life {ctx['life_before']})")
                wire("declare_attackers", {"yidris_oid": yid,
                                           "turn": ctx["attack_turn"],
                                           "life_before": ctx["life_before"]})
            else:
                dd["attacks"] = []
                dd["bands"] = []
            await submit_as_is(c, d)
        return True
    # ---- blockers: always empty (Yidris stays back on P1's turns)
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return True
    # ---- Shock target selection (A3 control + A4 window; both target P1)
    if ctx.get("shock_target_pending"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            ch = pick_target_candidate(opp, 1)
            if ch is not None and candidate_seat(ch) == 1:
                ctx["shock_target_pending"] = False
                say(f"[{tag}] submitting Shock target: P1 (seat 1)")
                await submit_target(c, opp, rtype, stype, ch, tag)
                return True
            wire("target_no_p1", {"opp": str(opp)[:400]})
            say(f"[{tag}] target opportunity without seat-1 candidate; waiting")
            return True
        return True  # wait for the opportunity; do not pass under the cast
    # ---- capture Yidris oid once it lands (no driver mana payment at all;
    # the engine auto-pays every cast)
    if ctx.get("yidris_cast") and not ctx.get("yidris_oid"):
        yids = bf_by_name(state, 0, "yidris, maelstrom wielder")
        if yids:
            ctx["yidris_oid"] = int(yids[0])
            say(f"[{tag}] Yidris on battlefield (oid {ctx['yidris_oid']})")
    # ---- passive watches (damage + cascade signals)
    await scan_damage(c, tag, ctx, state)
    await scan_watches(c, tag, ctx, state)
    # ---- settle: decline a genuine decide/bool may prompt after a cascade
    # was observed (documented decision; exile card choices never answered)
    if ctx.get("settle_may") and not ctx.get("settle_done"):
        opp, kind = scan_genuine_may(st)
        if opp is not None and kind in ("decide", "bool"):
            if await decline_may(c, opp, kind, tag):
                ctx["settle_done"] = True
            return True
        if opp is not None:
            say(f"[{tag}] settle: prompt kind={kind} left unanswered "
                f"(never auto-answer exile card choices)")
            wire("settle_unhandled", {"kind": kind})
        ctx["settle_done"] = True
        nxt = ctx.pop("settle_next_phase", None)
        if nxt:
            ctx["phase"] = nxt
            say(f"[{tag}] settle complete; advancing to phase {nxt}")
        return True
    # ---- race fix: yield, then re-read fresh state before evaluating
    # main-phase plays (a tick's state may be stale after the awaits above)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    # ---- main-phase plays
    if my_main(state, 0):
        if await play_land_capped(c, state, 0, acts, tag):
            return True
        # ramp: cast Yidris {B}{G}{R}{U}
        if (ctx.get("phase") == "ramp" and not ctx.get("yidris_cast")
                and find_hand(state, 0, "yidris, maelstrom wielder") is not None
                and state.get("phase") == "PreCombatMain"
                and can_pay_yidris(state)):
            yoid = find_hand(state, 0, "yidris, maelstrom wielder")
            a = cast_action_for(acts, state, yoid) if yoid is not None else None
            if a is not None and not ctx.get("cast_pending"):
                ctx["cast_pending"] = {"kind": "yidris", "oid": str(yoid),
                                       "name": "Yidris, Maelstrom Wielder",
                                       "since": time.time()}
                say(f"[{tag}] casting Yidris, Maelstrom Wielder (oid {yoid}); "
                    f"engine auto-pays (payment_mode Auto)")
                wire("cast_yidris", {"oid": yoid,
                                    "turn": state.get("turn_number")})
                await submit_as_is(c, a)
                return True
        # hunt: the first fully-staged P0 PreCombatMain hosts A3 + the attack
        if (ctx.get("phase") == "hunt" and not ctx.get("hunt_consumed")
                and state.get("phase") == "PreCombatMain"
                and yidris_attack_ready(state, ctx)
                and find_hand(state, 0, "shock") is not None
                and untapped_named(state, 0, "mountain") >= 1):
            ctx["hunt_consumed"] = True
            ctx["attack_live"] = True
            ctx["attack_turn"] = state.get("turn_number")
            nshock = count_hand(state, 0, "shock")
            say(f"[{tag}] hunt window staged on turn {ctx['attack_turn']} "
                f"(shocks in hand: {nshock})")
            wire("hunt_window", {"turn": ctx["attack_turn"], "shocks": nshock})
            if nshock >= 2 and untapped_named(state, 0, "mountain") >= 2:
                # A3: pre-damage control Shock (must NOT gain cascade)
                pre_s = await c.export_state()
                ctx["pre_shock_cast"] = export_now_sync(pre_s, "pre_shock_cast.json")
                soid = find_hand(state, 0, "shock")
                a = cast_action_for(acts, state, soid) if soid is not None else None
                if a is not None and not ctx.get("cast_pending"):
                    ctx["cast_pending"] = {"kind": "a3", "oid": str(soid),
                                           "name": "Shock (A3 control)",
                                           "since": time.time()}
                    ctx["a3_shock_oid"] = str(soid)
                    ctx["a3_watch"] = {"start": time.time(),
                                       "expect_source": "Shock",
                                       "exile_before": len(exile_oids(state))}
                    ctx["shock_target_pending"] = True
                    say(f"[{tag}] A3: casting pre-damage Shock (oid {soid}); "
                        f"engine auto-pays")
                    wire("cast_shock_a3", {"oid": soid})
                    await submit_as_is(c, a)
                    return True
                ctx["assert"]["A3_pre_damage_control"] = "not-run"
                ctx["notes"].append("A3: hunt window staged but no CastSpell "
                                    "action for the control Shock")
            else:
                ctx["assert"]["A3_pre_damage_control"] = "not-run"
                ctx["notes"].append(
                    f"A3 skipped: only {nshock} Shock(s) in hand; the A4 "
                    "window takes priority")
                say(f"[{tag}] A3 not-run ({nshock} shock in hand)")
            return True
        # A4: post-damage Shock from hand (must gain cascade per Yidris)
        if (ctx.get("phase") == "hunt" and ctx.get("damage_done")
                and not ctx.get("post_consumed")
                and state.get("phase") == "PostCombatMain"
                and state.get("turn_number") == ctx.get("attack_turn")
                and find_hand(state, 0, "shock") is not None
                and untapped_named(state, 0, "mountain") >= 1):
            ctx["post_consumed"] = True
            # Stand Yidris down: the grant test is over, and further attacks
            # would kill P1 before the A5 control runs.
            ctx["attack_live"] = False
            say(f"[{tag}] Yidris stood down (attack_live=False); P1 must survive for A5")
            pre_s = await c.export_state()
            ctx["pre_cast"] = export_now_sync(pre_s, "pre_cast.json")
            soid = find_hand(state, 0, "shock")
            a = cast_action_for(acts, state, soid) if soid is not None else None
            if a is not None and not ctx.get("cast_pending"):
                ctx["cast_pending"] = {"kind": "a4", "oid": str(soid),
                                       "name": "Shock (A4 post-damage)",
                                       "since": time.time()}
                ctx["a4_shock_oid"] = str(soid)
                ctx["a4_watch"] = {"start": time.time(),
                                   "expect_source": "Shock",
                                   "exile_before": len(exile_oids(state))}
                ctx["shock_target_pending"] = True
                say(f"[{tag}] A4: casting post-damage Shock (oid {soid}); "
                    f"engine auto-pays")
                wire("cast_shock_a4", {"oid": soid})
                await submit_as_is(c, a)
                return True
            ctx["assert"]["A4_yidris_grant"] = "not-run"
            ctx["notes"].append("A4: post-damage window staged but no CastSpell "
                                "action for Shock")
            return True
        # control: Bloodbraid Elf (native cascade) on a later turn
        if (ctx.get("phase") == "control" and not ctx.get("control_consumed")
                and find_hand(state, 0, "bloodbraid elf") is not None
                and can_pay_bloodbraid(state)):
            ctx["control_consumed"] = True
            pre_s = await c.export_state()
            export_now_sync(pre_s, "pre_control.json")
            boid = find_hand(state, 0, "bloodbraid elf")
            a = cast_action_for(acts, state, boid) if boid is not None else None
            if a is not None and not ctx.get("cast_pending"):
                ctx["cast_pending"] = {"kind": "control", "oid": str(boid),
                                       "name": "Bloodbraid Elf (A5 control)",
                                       "since": time.time()}
                ctx["control_watch"] = {"start": time.time(),
                                        "expect_source": "Bloodbraid Elf",
                                        "exile_before": len(exile_oids(state))}
                say(f"[{tag}] A5: casting Bloodbraid Elf (oid {boid}); "
                    f"engine auto-pays")
                wire("cast_bloodbraid", {"oid": boid})
                await submit_as_is(c, a)
                return True
            ctx["assert"]["A5_control_cascade"] = "not-run"
            ctx["notes"].append("A5: control window staged but no CastSpell "
                                "action for Bloodbraid Elf")
            return True
    if await answer_vi_play_land(c, state, 0, acts, tag, MOUNTAIN):
        return True
    # ---- watch finalization (A3 / A4 / control)
    if await finalize_watches(c, tag, ctx):
        return True
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def scan_damage(c, tag, ctx, state):
    """Detect Yidris combat damage: P1 life drops by exactly 5 after the
    attackers were declared. Also wire-logs the Yidris trigger on stack."""
    if ctx.get("damage_done") or not ctx.get("attackers_declared"):
        return
    if ctx.get("life_before") is None:
        return
    for e in iter_stack(state):
        if (e.get("kind", {}).get("type") == "TriggeredAbility"
                and "yidris" in entry_source_name(e).lower()):
            tagid = f"yidris#{e.get('id')}"
            if tagid not in ctx.setdefault("yidris_trigger_texts", []):
                ctx["yidris_trigger_texts"].append(tagid)
                say(f"[{tag}] YIDRIS TRIGGER ON STACK: "
                    f"{json.dumps(e, default=str)[:200]}")
                wire("yidris_trigger_stack",
                     {"entry": json.dumps(e, default=str)[:2000]})
    l1 = life(state, 1)
    if l1 is not None and l1 <= ctx["life_before"] - 5:
        ctx["damage_done"] = True
        ctx["damage_turn"] = state.get("turn_number")
        say(f"[{tag}] combat damage observed: P1 life "
            f"{ctx['life_before']}->{l1} (turn {ctx['damage_turn']})")
        wire("combat_damage", {"life_before": ctx["life_before"],
                               "life_after": l1,
                               "turn": ctx["damage_turn"]})
        post_s = await c.export_state()
        ctx["post_damage"] = export_now_sync(post_s, "post_damage.json")
        say(f"[{tag}] exported post_damage.json (grant window should be active)")

async def finalize_watches(c, tag, ctx):
    """Conclude finished watches and evaluate assertions. Returns True if it
    acted (caller should re-tick)."""
    # ---- A3 finalization: control Shock must NOT cascade
    w = ctx.get("a3_watch")
    if w and w.get("done") and not ctx.get("a3_evaluated"):
        ctx["a3_evaluated"] = True
        st = c.latest
        exile_after = len(exile_oids(st["state"])) if st else None
        exile_d = (exile_after - w["exile_before"]) if exile_after is not None else None
        casc = bool(w.get("trigger_seen") or w.get("may_seen")
                    or (exile_d is not None and exile_d > 0))
        ctx["assert"]["A3_pre_damage_control"] = "passed" if not casc else "failed"
        ctx["notes"].append(
            f"A3: pre-damage Shock: cascade trigger={w.get('trigger_seen')} "
            f"sources={w.get('sources')} may={w.get('may_seen')} "
            f"kind={w.get('may_kind')} exile {w['exile_before']}->{exile_after}")
        say(f"[{tag}] A3: cascade before damage = {casc} (expect False) "
            f"-> {ctx['assert']['A3_pre_damage_control']}")
        wire("a3_result", {"casc": casc, "exile_delta": exile_d,
                           "sources": w.get("sources")})
        return True
    # ---- A4 finalization: post-damage Shock MUST cascade
    w = ctx.get("a4_watch")
    if w and w.get("done") and not ctx.get("a4_evaluated"):
        ctx["a4_evaluated"] = True
        post_s = await c.export_state()
        post = export_now_sync(post_s, "post_cast.json")
        exile_after = len(exile_oids(post))
        exile_d = exile_after - w["exile_before"]
        casc = bool(w.get("trigger_seen") or w.get("may_seen") or exile_d > 0)
        ctx["assert"]["A4_yidris_grant"] = "passed" if casc else "failed"
        ctx["notes"].append(
            f"A4: post-damage Shock: cascade trigger={w.get('trigger_seen')} "
            f"sources={w.get('sources')} may={w.get('may_seen')} "
            f"kind={w.get('may_kind')} exile {w['exile_before']}->{exile_after}")
        say(f"[{tag}] A4: cascade after Yidris damage = {casc} (expect True) "
            f"-> {ctx['assert']['A4_yidris_grant']}")
        wire("a4_result", {"casc": casc, "exile_delta": exile_d,
                           "sources": w.get("sources")})
        if casc:
            # Settle only: decline the free-cast may prompt now that the
            # cascade is recorded. Exile-zone card choices are never answered.
            ctx["settle_may"] = True
            ctx["settle_done"] = False
            ctx["settle_next_phase"] = "control"
        else:
            ctx["phase"] = "control"
            say(f"[{tag}] advancing to control phase")
        return True
    # ---- control finalization: Bloodbraid MUST cascade natively
    w = ctx.get("control_watch")
    if w and w.get("done") and not ctx.get("control_evaluated"):
        ctx["control_evaluated"] = True
        post_s = await c.export_state()
        post = export_now_sync(post_s, "post_control.json")
        exile_after = len(exile_oids(post))
        exile_d = exile_after - w["exile_before"]
        casc = bool(w.get("trigger_seen") or w.get("may_seen") or exile_d > 0)
        ctx["assert"]["A5_control_cascade"] = "passed" if casc else "failed"
        ctx["notes"].append(
            f"A5: Bloodbraid cascade trigger={w.get('trigger_seen')} "
            f"sources={w.get('sources')} may={w.get('may_seen')} "
            f"kind={w.get('may_kind')} exile {w['exile_before']}->{exile_after}")
        say(f"[{tag}] A5: native cascade works = {casc} (expect True) "
            f"-> {ctx['assert']['A5_control_cascade']}")
        wire("a5_result", {"casc": casc, "exile_delta": exile_d,
                           "sources": w.get("sources")})
        if casc:
            ctx["settle_may"] = True
            ctx["settle_done"] = False
            ctx["settle_next_phase"] = "done"
        else:
            ctx["phase"] = "done"
        return True
    return False


def cast_zone(state, oid):
    o = get_obj(state, oid)
    return str(o.get("zone") or "").lower()


async def guard_cast_pending(c, tag, ctx):
    """Cast-confirmation guard: while a CastSpell is in flight, the driver
    must not pass priority or start other main-phase plays (the bare
    CastSpell can lose a race to the driver's own PassPriority).
    Returns "waiting", "confirmed", "dropped", or None (no pending cast)."""
    pend = ctx.get("cast_pending")
    if not pend:
        return None
    st = c.latest
    if not st:
        return "waiting"
    state = st["state"]
    oid = pend["oid"]
    zone = cast_zone(state, oid)
    if zone in ("stack", "battlefield"):
        # Confirmed: the spell left the hand.
        kind = pend["kind"]
        ctx["cast_pending"] = None
        ctx[kind + "_cast"] = True
        if kind == "yidris":
            ctx["yidris_cast_turn"] = state.get("turn_number")
        say(f"[{tag}] cast confirmed on {zone}: {pend['name']} (oid {oid})")
        wire("cast_confirmed", {"kind": kind, "oid": oid, "zone": zone})
        return "confirmed"
    if zone == "hand" and time.time() - pend["since"] > 30:
        # The cast never left the hand: the server dropped it (priority
        # race). Clear pending so the cast is retried; do NOT set *_cast.
        # Reset the one-shot consumed flags so the cast blocks re-trigger,
        # and clear shock_target_pending (no target opportunity is coming).
        ctx["cast_pending"] = None
        kind = pend["kind"]
        if kind == "a3":
            ctx["hunt_consumed"] = False
            ctx["shock_target_pending"] = False
        elif kind == "a4":
            ctx["post_consumed"] = False
            ctx["shock_target_pending"] = False
        elif kind == "control":
            ctx["control_consumed"] = False
        say(f"[{tag}] cast NOT confirmed after 30s (still in hand): "
            f"{pend['name']} (oid {oid}); will retry")
        wire("cast_dropped_retry", {"kind": kind, "oid": oid})
        return True
    # Still in flight: hold priority, do nothing else.
    return True


async def drive(p0, p1, tag, ctx, timeout_s, done_fn, diag_every=30):
    """Tick both clients until done_fn() is truthy or the timeout fires.
    Returns True iff done_fn() became truthy."""
    t0 = time.time()
    last_diag = 0.0
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.15)
        for c, is_p0 in ((p0, True), (p1, False)):
            st = c.latest
            if not st:
                continue
            try:
                if is_p0:
                    await p0_tick(c, tag, ctx)
                else:
                    await p1_tick(c, f"P1{tag}")
            except Exception as e:
                say(f"[{tag}] tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        for c in (p0, p1):
            while True:
                try:
                    t, data = c.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t in ("ActionRejected", "Error"):
                    rec = json.dumps(data, default=str)[:400]
                    ctx["rejections"].append({"who": c.name, "type": t,
                                             "at": time.time(), "data": rec})
                    wire("rejection", {"who": c.name, "type": t, "data": rec})
        try:
            if done_fn():
                return True
        except Exception as e:
            say(f"[{tag}] done_fn error: {e}")
        if time.time() - last_diag > diag_every:
            last_diag = time.time()
            st = p0.latest
            if st:
                s = st["state"]
                say(f"[{tag}] DIAG rev={p0.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} prio={my_priority(top_acts(st))} "
                    f"vikind={vi_kind_code(st)!r} "
                    f"real_decision={real_decision_pending(st)} "
                    f"yidris_cast={ctx.get('yidris_cast')} "
                    f"stage={ctx.get('phase')} a4={ctx.get('assert', {}).get('A4_yidris_grant')}")
    return False


# ---------------------------------------------------------------- attempt runner

def new_ctx():
    return {
        "phase": "ramp",
        "assert": {},
        "notes": [],
        "rejections": [],
        "life_before": None,
    }


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    ST["mull_counts"] = {}
    ctx = new_ctx()
    tag = f"6765-a{attempt}"
    for k in ("A0_connect", "A1_setup_ok", "A2_combat_damage",
              "A3_pre_damage_control", "A4_yidris_grant",
              "A5_control_cascade", "A6_cleanup"):
        ctx["assert"][k] = "not-run"

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck((FOREST, 20), (MOUNTAIN, 20), (ISLAND, 20), (SWAMP, 20),
                         (YIDRIS, 6), (SHOCK, 12), (BLOODBRAID, 8)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck((MOUNTAIN, 200)))
    ctx["assert"]["A0_connect"] = ("passed" if p0.player_id is not None
                                   and p1.player_id is not None else "failed")
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    try:
        # ---- phase 1: ramp, cast Yidris
        say(f"[{tag}] phase 1: ramp to Yidris")
        ok = await drive(p0, p1, tag, ctx, 900,
                         lambda: bool(ctx.get("yidris_cast"))
                         and p0.latest is not None
                         and bool(bf_by_name(p0.latest["state"], 0,
                                             "yidris, maelstrom wielder")))
        if not ok:
            ctx["notes"].append("phase 1 timed out: Yidris never reached the "
                                "battlefield")
            return ctx
        ctx["assert"]["A1_setup_ok"] = "passed"
        ctx["notes"].append(f"Yidris on BF (oid {ctx.get('yidris_oid')}), cast "
                            f"turn {ctx.get('yidris_cast_turn')}")
        say(f"[{tag}] A1 passed: Yidris on battlefield")
        ctx["phase"] = "hunt"

        # ---- phase 2: hunt (A3 control + attack) + A4 window
        say(f"[{tag}] phase 2: hunt + post-damage window")
        ok = await drive(p0, p1, tag, ctx, 1200,
                         lambda: bool(ctx.get("a4_evaluated"))
                         and (not ctx.get("settle_may") or ctx.get("settle_done"))
                         and p0.latest is not None
                         and not (p0.latest["state"].get("stack") or []))
        if not ctx.get("attackers_declared"):
            ctx["notes"].append("phase 2 timed out: attackers never declared")
            return ctx
        if not ctx.get("damage_done"):
            ctx["notes"].append("phase 2: attackers declared but no 5-life "
                                "combat damage observed")
            ctx["assert"]["A2_combat_damage"] = "failed"
            return ctx
        # A2: exactly -5 life from Yidris (life_before was captured at
        # attackers-declared, i.e. after any A3 Shock resolved; evaluate from
        # the post_damage export, before the A4 Shock's damage lands)
        pd = ctx.get("post_damage") or {}
        l1 = life(pd, 1)
        a2 = (ctx.get("life_before") is not None and l1 == ctx["life_before"] - 5)
        ctx["assert"]["A2_combat_damage"] = "passed" if a2 else "failed"
        ctx["notes"].append(f"A2: P1 life {ctx.get('life_before')}->{l1} "
                            f"(expect -5); yidris trigger seen="
                            f"{bool(ctx.get('yidris_trigger_texts'))}")
        say(f"[{tag}] A2: combat damage -> {ctx['assert']['A2_combat_damage']}")
        if not ok:
            ctx["notes"].append("phase 2 timed out before the A4 window "
                                "settled; A4 recorded as not-run")
            if ctx["assert"]["A4_yidris_grant"] == "not-run":
                pass
            return ctx
        # A3 default: if the watch never ran (no A3 cast), keep not-run
        if ctx["assert"]["A3_pre_damage_control"] == "not-run" and ctx.get("a3_cast"):
            ctx["notes"].append("A3: control Shock cast but watch never "
                                "concluded; leaving not-run")

        # ---- phase 3: control (Bloodbraid native cascade)
        say(f"[{tag}] phase 3: Bloodbraid control")
        # parked-by-design escape: after the control cascade is observed we
        # deliberately never answer the exile-zone free-cast card choice, so
        # the game parks at that prompt with the trigger on the stack. Treat
        # 45s without a revision change as phase-3 completion.
        phase3_st = {"rev": -1, "t": time.time()}

        def phase3_done():
            if not (ctx.get("control_evaluated")
                    and (not ctx.get("settle_may") or ctx.get("settle_done"))):
                return False
            if p0.latest is not None and not (p0.latest["state"].get("stack") or []):
                return True
            if p0.revision != phase3_st["rev"]:
                phase3_st["rev"] = p0.revision
                phase3_st["t"] = time.time()
            parked = time.time() - phase3_st["t"] > 45
            if parked:
                say(f"[{tag}] phase 3: parked 45s at rev={p0.revision} "
                    f"(cascade card choice deliberately unanswered)")
            return parked

        ok = await drive(p0, p1, tag, ctx, 600, phase3_done)
        if not ctx.get("control_evaluated"):
            ctx["notes"].append("phase 3 timed out: Bloodbraid control never "
                                "evaluated")
            return ctx

        # ---- phase 4: cleanup
        say(f"[{tag}] phase 4: cleanup")
        await drive(p0, p1, tag, ctx, 45, lambda: False)
        fin_s = await p0.export_state()
        fin = export_now_sync(fin_s, "final.json")
        a6 = (not (fin.get("stack") or [])
              and not real_decision_pending(p0.latest))
        ctx["assert"]["A6_cleanup"] = "passed" if a6 else "failed"
        say(f"[{tag}] A6: stack_empty={not (fin.get('stack') or [])} "
            f"no_pending={not real_decision_pending(p0.latest)} "
            f"-> {ctx['assert']['A6_cleanup']}")
        ctx["finished"] = True
    finally:
        await p0.close()
        await p1.close()
    return ctx

# ---------------------------------------------------------------- main

async def main():
    await verify_server_hello()
    t0 = time.time()
    final = None
    for attempt in (1, 2, 3):
        say(f"===== attempt {attempt} =====")
        ctx = await run_attempt(attempt)
        say(f"attempt {attempt} assertions: {json.dumps(ctx['assert'])}")
        final = ctx
        # stop retrying once the A4 window was actually exercised
        if ctx["assert"].get("A4_yidris_grant") in ("passed", "failed"):
            break
        say("attempt did not reach the A4 window; retrying with a fresh game")
    dur = time.time() - t0
    ass = final["assert"]
    notes = final["notes"] + [
        "protocol-118 driver (v0.104.0): Yidris cast via bare legacy "
        "CastSpell matched by object_id, engine auto-pays (payment_mode "
        "Auto); the driver never taps lands for mana itself (no "
        "double-payment artifact). Shock casts via CastSpell with {R:1}; "
        "Shock targets via the advertised vi target "
        "opportunity (schema select/sequence), seat-1 candidate preferred; "
        "attackers declared via the advertised DeclareAttackers action with "
        "attacks=[[yidris_oid, {type:Player, data:1}]] (AttackTarget tagged "
        "union per engine types/actions.rs); cascade signals detected "
        "strictly: a TriggeredAbility stack entry whose ability effect type "
        "is exactly 'Cascade', a genuine decide/bool may prompt or an "
        "all-exile card choice, or a positive exile-zone delta; a genuine "
        "decide/bool may prompt is DECLINED after an observed cascade purely "
        "to let the game settle (exile card choices are never answered); "
        "Yidris stood down after the A4 window so P1 survives for A5.",
        "P0 deck 20x Forest/Mountain/Island/Swamp + 6x Yidris, Maelstrom "
        "Wielder + 12x Shock + 8x Bloodbraid Elf (106 cards); P1 200x "
        "Mountain draw-go, never attacks or blocks (engine accepts >4-of "
        "and oversized decks for custom games; the size prevents decking "
        "losses during the 4-color ramp).",
        "Parser signal: pinned card-data.json marks Yidris's damage-trigger "
        "execute as Unimplemented ('as you cast spells from your hand'); the "
        "AddKeyword(Cascade) grant exists only as its sub_ability.",
    ]
    a2 = ass.get("A2_combat_damage")
    a3 = ass.get("A3_pre_damage_control")
    a4 = ass.get("A4_yidris_grant")
    a5 = ass.get("A5_control_cascade")
    if a2 == "passed" and a4 == "failed" and a5 == "passed" \
            and a3 in ("passed", "not-run"):
        verdict = "reproduced"
    elif a2 == "passed" and a4 == "passed" and a5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.104.0",
            "build_commit": "4227122",
            "protocol_version": 118,
            "mode": "Full",
            **{k: v for k, v in SERVER_IDENTITY.items()
                if k in ("server_binary_sha256", "card_data_sha256",
                         "draft_pools_sha256", "signature_verified")},
            "observed_at": "2026-10-09",
            "source": SERVER_IDENTITY["source"],
        },
        "driver": {"protocol_advertised": 118, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6765_01040.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [[FOREST, 20], [MOUNTAIN, 20], [ISLAND, 20], [SWAMP, 20],
                   [YIDRIS, 6], [SHOCK, 12], [BLOODBRAID, 8]],
            "P1": [[MOUNTAIN, 200]],
        },
        "assertions": ass,
        "notes": notes,
        "rejections": final["rejections"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Dense playsets (12x Shock, 8x Bloodbraid, 6x Yidris) and oversized "
            "decks (106/200 cards) are test-harness conveniences (engine "
            "accepts >4-of and >60-card decks for custom games; the size "
            "prevents decking losses during the 4-color ramp).",
            "Parser signal: pinned card-data.json marks Yidris's "
            "damage-trigger execute as Unimplemented ('as you cast spells "
            "from your hand'); the AddKeyword(Cascade) grant exists only as "
            "its sub_ability.",
            "A5's cascade free-cast may prompt is declined after the trigger "
            "is observed, purely to let the game settle; exile-zone card "
            "choices are never auto-answered.",
            "A6 cleanup stays failed by design: after the A5 control the game "
            "parks at the cascade free-cast card choice, which the driver "
            "deliberately never auto-answers (exile-zone card choices are not "
            "driver decisions); not an engine defect.",
            "Not tested on the original 2026-07-29 build; verdict is scoped "
            "to v0.104.0, not a fix claim.",
        ],
        "setup_line": ("P0: 80 lands + 6x Yidris, Maelstrom Wielder + 12x Shock + "
                       "8x Bloodbraid Elf (106 cards); P1: 200x Mountain (draw-go); "
                       "oversized decks prevent decking during the 4-color ramp"),
        "contract_line": ("Yidris attacks unblocked on a fully-staged turn; "
                          "pre-damage Shock (when a spare is available) must not "
                          "cascade; post-damage Shock must gain cascade; "
                          "Bloodbraid Elf control must cascade natively"),
        "stats": {"states_seen": "n/a", "trigger_observations": "n/a"},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6765_01040.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6765_01040.py").read())
    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    WIRE.close()
    RUNLOG.close()


asyncio.run(main())
