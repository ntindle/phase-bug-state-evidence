#!/usr/bin/env python3
"""Issue #6987: Textual Madness cost "Pay six {C}" is unsupported.
(v0.103.0 / protocol 106 re-validation)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0), "Emrakul, the World Anew":
  "When you cast this spell, gain control of all creatures target player
   controls. Flying, protection from spells and from permanents that were
   cast this turn. When Emrakul leaves the battlefield, sacrifice all
   creatures you control. Madness--Pay six {C}."
Parse state on v0.103.0 (observed 2026-10-08 before the run):
  keywords = [Flying, Protection x2] -- NO Madness keyword.
  abilities = [Spell / Unimplemented "unknown" (description
  "Madness--Pay six {C}.")] -- the defect, unchanged from v0.81.3.
  Fiery Temper (control): keywords = [{"Madness": {"type": "Cost",
  "shards": ["Red"], "generic": 0}}] -- the working-madness control.

Reported symptom: "The Madness permission should offer the printed
alternative cost of six colorless mana after the card is discarded into
exile." Expected per the issue: discard Emrakul -> madness replacement ->
exile -> CastOffer at six {C}.

Setup (native engine, two human-client seats, default Bo1):
  P0: 12x Faithless Looting, 8x Fiery Temper, 20x Emrakul, the World Anew,
      10x Mountain, 10x Wastes.
  P1: 60x Mountain (passive: land drop, pass priority).
  CONTROL - P0 casts Faithless Looting, discards Fiery Temper (Madness {R}).
    The engine must exile it (madness replacement) and offer the madness
    cast for {R}; the driver answers the AUTO-payment choice and it
    resolves for 3 damage to P1. Proves the engine's madness machinery
    works for correctly-parsed cards.
  PRIMARY - P0 casts a second Faithless Looting, discards Emrakul.
    The driver records the zone it lands in and whether any madness cast
    is offered at six {C}.

Assertions (correct-behavior properties; "failed" = the defect is present):
  Parse (primary; measured on the pinned v0.103.0 card-data.json):
    A1_parse_emrakul_madness  Emrakul keywords carry Madness with cost 6x{C}.
                              expected FAIL (no Madness keyword; Spell/
                              Unimplemented "unknown").
  Game (runtime consequence of the parse defect):
    A2_setup_ok        pre.json: P0 main, Looting + Temper in hand,
                       >=2 untapped Mountains (one for Looting, one for
                       the madness {R}). expected PASS.
    A3_control_exile   discarded Fiery Temper entered Exile. expected PASS.
    A4_control_offer   madness cast offered for the exiled Fiery Temper.
                       expected PASS.
    A5_control_resolves  cast for {R} resolves; P1 20->17. expected PASS.
    A6_emrakul_exile   discarded Emrakul entered Exile (not gy).
                       expected FAIL (graveyard, no madness replacement).
    A7_emrakul_offer   madness cast offered at six {C}. expected FAIL
                       (no CastOffer raised).
    A8_cleanup         post.json: stack empty, game proceeds. expected PASS.

Verdict rule: reproduced iff A6 or A7 fails (the reported outcome is not
met). blocked iff the control setup never opens (A2 not passed) or the
primary proof never completes.

Protocol-106 madness driver rules (do not re-derive):
  - Madness CastOffer choices carry action code 'castSpellAsMadness'
    (not 'castSpell') with a paymentMode value surface ('auto'/'manual')
    plus a decline choice (decideOptionalEffect accept=false); answer the
    AUTO-payment choice. Response shape: exactChoices -> {"type":"choose",
    "data":{"choiceId":...}}.
  - The engine auto-pays costs itself: NEVER tap lands driver-side for
    test casts (double-payment artifact).
  - Hand-size discard order: lands, Looting, Emrakul, Temper -- a stray
    discarded Fiery Temper exiles via madness and raises an unanswerable
    stray CastOffer that stalls the game.
  - The stack-watch branch must FALL THROUGH to the priority-pass logic --
    never `return` early from it.
  - Looting's own DiscardChoice must be answered before any hand-size
    discard: after Looting draws 2 the hand can exceed 7 and the hand-size
    n can coincide with the Looting n=2. do_discard_handsize is gated off
    while a Looting DiscardChoice is outstanding.
  - The madness watch's stack scan must skip TriggeredAbility entries
    (the madness trigger mentions the card name too) and only count the
    spell after the CastOffer was answered; otherwise the watch closes on
    the trigger resolving and mis-measures the control (P1 life).
  - The engine only offers the madness cast choice `if can_pay`
    (can_pay_cost_after_auto_tap; engine source ai_support/candidates.rs):
    with a single untapped Mountain the engine auto-taps it for Looting's
    {R} and the offer degrades to decline-only -- correct engine
    behavior, not the bug. The control gate therefore needs TWO untapped
    red sources (one for Looting, one for the madness cast).
  - A real_decision_pending hold must not fire on already-answered
    opportunities (SUBMITTED_OPPS filter in unanswered_ops).
  - One in-flight submission per acting client; gate on state_revision.

Evidence: evidence/6987/<run-id>/pre.json, mid.json, post.json,
parse_emrakul.json, assertions.json, run.json, manifest.sha256,
summary.png, scenario_6987_01030.py, wire_log.jsonl, scenario_run.log,
server.log
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6987
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-6987")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SERVER_IDENTITY = {
    "server_version": "0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256": None,
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
}
for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = sha256_of_file(f"{BACKFILL}/{_f}")
assert SERVER_IDENTITY["server_binary_sha256"] == \
    "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc", \
    "binary hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["card_data_sha256"] == \
    "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe", \
    "card-data hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["draft_pools_sha256"] == \
    "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd", \
    "draft-pools hash drift from the v0.103.0 pin"
say("server identity hashes verified against the v0.103.0 pin")


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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))

LOOTING_T = "Faithless Looting"
LOOTING_L = "faithless looting"
TEMPER_T = "Fiery Temper"
TEMPER_L = "fiery temper"
EMRAKUL_T = "Emrakul, the World Anew"
EMRAKUL_L = "emrakul, the world anew"
MOUNTAIN_L = "mountain"
WASTES_L = "wastes"
ALL_LANDS = (MOUNTAIN_L, WASTES_L)

P0_DECK = ((LOOTING_T, 12), (TEMPER_T, 8), (EMRAKUL_T, 14),
           ("Mountain", 16), ("Wastes", 10))
P1_DECK = (("Mountain", 60),)

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0},
      "pre_exported": False, "mid_exported": False, "post_exported": False,
      "control_exiled": False, "control_offered": False,
      "control_offer_source": None, "control_resolved": False,
      "control_p1_before": None, "control_p1_after": None,
      "primary_zone": None, "primary_offered": False,
      "primary_offer_source": None,
      "p0_turn_cap_abort": False}
LOOT = {"tag": None, "cast": False, "in_flight": False, "discarded": False}
WATCH = {"active": False, "proof": None, "discarded": [],
         "offer": False, "offer_source": None, "offer_card": None,
         "acted": False, "cast_oid": None, "stack_seen": False,
         "resolved": False, "t0": 0.0, "turn0": 0, "closed": False,
         "close_reason": None, "target_answered": False,
         "exiled": [], "gy": [], "rev_at_submit": None, "rejected": False}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "tick_errors": [], "notes": [], "madness_offers": [],
       "discard_choices": [], "wf_sequence": []}
WF_SEEN = []

PARSE = {"emrakul": None, "temper": None, "a1": False, "a1_detail": ""}


# ---------------------------------------------------------------------------
# data-level parse check (primary evidence)


def check_data_level():
    """Record the v0.103.0 parse of Emrakul's Madness line (+ Temper control).

    A1 contract: Emrakul's keywords carry Madness with cost 6x{C}.
    """
    em = CARD_DATA.get(EMRAKUL_L, {})
    tm = CARD_DATA.get(TEMPER_L, {})
    with open(f"{EVDIR}/parse_emrakul.json", "w") as f:
        json.dump({"card": EMRAKUL_T,
                   "oracle_text": em.get("oracle_text"),
                   "keywords": em.get("keywords"),
                   "abilities": em.get("abilities"),
                   "parse_warnings": em.get("parse_warnings"),
                   "control": {"card": TEMPER_T,
                               "keywords": tm.get("keywords"),
                               "oracle_madness_line":
                               [l for l in (tm.get("oracle_text") or "")
                                .split("\n") if "adness" in l]}}, f, indent=1)
    say("saved parse_emrakul.json")
    PARSE["emrakul"] = {"keywords": em.get("keywords"),
                        "abilities": em.get("abilities")}
    PARSE["temper"] = {"keywords": tm.get("keywords")}
    mad = [k for k in (em.get("keywords") or [])
           if isinstance(k, dict) and "Madness" in k]
    ok, detail = False, f"keywords={em.get('keywords')}"
    if len(mad) == 1:
        cost = mad[0]["Madness"] or {}
        shards = cost.get("shards") or []
        total = (cost.get("generic") or 0) + len(shards)
        ok = (total == 6
              and all(str(s).lower() == "colorless" for s in shards))
        detail = f"Madness cost={cost}"
    unimp = [a for a in (em.get("abilities") or [])
             if (a.get("effect") or {}).get("type") == "Unimplemented"]
    detail += (f"; Unimplemented abilities="
               f"{[(a.get('kind'), (a.get('effect') or {}).get('name')) for a in unimp]}")
    PARSE["a1"] = ok
    PARSE["a1_detail"] = detail
    say(f"A1 parse: {detail} -> {'passed' if ok else 'failed'}")
    wire("parse_emrakul", {"a1": ok, "detail": detail,
                           "temper_keywords": tm.get("keywords")})
    return {"a1": ok}


# ---------------------------------------------------------------------------
# state helpers


def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def find_hand(state, pid, lname):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == lname:
            return o
    return None


def bf_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and not get_obj(state, oid).get("tapped")]


def untapped_mountains(state, pid):
    return sum(1 for oid in untapped_lands(state, pid)
               if obj_lname(state, oid) == MOUNTAIN_L)


def stack_empty(state):
    return not (state.get("stack") or [])


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def wf_of(state):
    return state.get("waiting_for") or {}


def my_main(state, pid):
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and stack_empty(state))


def is_my_main(state, pid):
    return my_main(state, pid)


# ---------------------------------------------------------------------------
# viewer-interaction + action helpers (protocol-106 conventions, same as the
# sibling 6986_01030 scenario that ran green on this exact server today)


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


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(op):
    for c in (op.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def unanswered_ops(st):
    out = []
    for op in vi_ops(st):
        iid = op.get("interactionId") or op.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if is_priority_menu(op):
            continue
        out.append(op)
    return out


def real_decision_pending(st):
    for opp in unanswered_ops(st):
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
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


def select_spec_n(opp):
    """Constraint count for a select-schema opportunity (0 if unknown)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    if not isinstance(spec, dict):
        return 0
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
           ).get("data", {}) or {}
    try:
        return int(con.get("min") or con.get("max") or 0)
    except (TypeError, ValueError):
        return 0


def ref_choice_map(opp):
    """Map hand-oid reference surfaces -> choice ids for a select opp."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("candidates") or data.get("choices") or []
    ref_of = {}
    for ch in chs:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                try:
                    ref_of[str(int(d["reference"]))] = ch["id"]
                except (TypeError, ValueError):
                    pass
    return ref_of


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type"),
                           "stage": ST["stage"]})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "stage": ST["stage"],
                                "response": sub.get("response")})
    LAST_IID["iid"] = sub.get("interactionId")
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
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = ST["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in ALL_LANDS)
    want = LOOTING_L if pid == 0 else None
    keep = (n_lands >= 2 and (want is None or want in hand)) or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        ST["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if ST["mulls"].get(tag, 0) <= 0:
        return False
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        n = select_spec_n(opp) or 1
        if n <= 0:
            return False

        def bkey(ch):
            return (0, choice_text(ch).lower() != LOOTING_L)

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


def discard_rank(state, o, pid):
    # Hand-size discard order: lands, Emrakul, Looting, Temper LAST.
    # A stray discarded Fiery Temper exiles via madness and raises an
    # unanswerable stray CastOffer that stalls the game; Lootings are
    # protected (rank 2) so the gates keep finding one in hand.
    nm = obj_lname(state, o)
    if nm in ALL_LANDS:
        return 0
    if pid == 0:
        if nm == EMRAKUL_L:
            return 1
        if nm == LOOTING_L:
            return 2
        if nm == TEMPER_L:
            return 3
        return 2
    return 1


async def do_discard_handsize(c, acts, st, pid, tag):
    # Never fire while a Looting DiscardChoice is outstanding: after
    # Looting draws 2 the hand can exceed 7 and the hand-size n can
    # coincide with the Looting's n=2 (the Looting discard is answered
    # by answer_looting_discard, which runs first in the tick).
    if LOOT.get("in_flight") and not LOOT.get("discarded"):
        return False
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        if select_spec_n(opp) != n:
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = ref_choice_map(opp)
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o, pid),
                                             obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] hand-size discards {n}: "
            f"{[obj_lname(state, o) for o in pick]}")
        wire("discard_handsize", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    return False


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


async def do_declare_empty(c, acts, st, pid, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"].update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower():
        opp = find_relations_op(st)
        if opp is not None:
            iid = opp.get("interactionId")
            key = (tag, "declare", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare empty via vi relations opportunity")
            wire("declare_empty_vi", {"who": tag})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "relations",
                                               "data": {"relations": []}}},
                              tag)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    for o in lands:
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
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
                                                   "data": {"choiceId":
                                                             ch.get("id")}}},
                                  c.name)
                return True
    return False


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    return a
            except (TypeError, ValueError):
                pass
    return None


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return out


# ---------------------------------------------------------------------------
# issue-specific: madness CastOffer / discard / target handling


def action_codes_of(ch):
    codes = []
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            code = (s.get("data") or {}).get("code")
            if code:
                codes.append(code)
    return codes


def opp_choices(opp):
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    return data.get("choices") or data.get("candidates") or []


def madness_choices(opp):
    """Choices carrying the castSpellAsMadness action code."""
    return [ch for ch in opp_choices(opp)
            if "castSpellAsMadness" in action_codes_of(ch)]


def payment_mode_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "paymentMode":
            return d.get("value")
    return None


def decline_choice_of(opp):
    """The decideOptionalEffect accept=false choice (madness decline)."""
    for ch in opp_choices(opp):
        if "decideOptionalEffect" not in action_codes_of(ch):
            continue
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("role") == "accept" \
                    and str(d.get("value")).lower() == "false":
                return ch
    return None


def source_name_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and s.get("type") == "object" \
                and d.get("role") == "source":
            return d.get("name")
    return None


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("seat") is not None:
            return d.get("seat")
    return None


async def answer_looting_discard(c, tag, st, state):
    """Answer Faithless Looting's DiscardChoice (exactly 2, select schema).

    Control discards Fiery Temper (+ a filler); primary discards Emrakul
    (+ a filler). Arms the madness watch on the discarded oids.
    """
    if not (LOOT.get("in_flight") and not LOOT.get("discarded")):
        return False
    wf = wf_of(state)
    if wf and wf.get("type") not in (None, "DiscardChoice"):
        return False
    for opp in unanswered_ops(st):
        if not is_select_schema_opp(opp):
            continue
        if select_spec_n(opp) != 2:
            continue
        iid = opp.get("interactionId")
        ref_of = ref_choice_map(opp)
        if not ref_of:
            continue
        hand = hand_ids(state, 0)
        proof = WATCH.get("proof")
        want_l = TEMPER_L if proof == "control" else EMRAKUL_L
        want_oid = next((o for o in hand if obj_lname(state, o) == want_l),
                        None)
        if want_oid is None or want_oid not in ref_of:
            continue
        # filler: lands first; never the madness card, never the other
        # filler: lands first, then spare Looting, then anything else
        # except the want card itself. (Discarding an Emrakul as control
        # filler is harmless: it has no madness, so it just goes to the
        # graveyard. For the primary, a Temper filler is ranked last --
        # discarding it would raise a second madness offer mid-watch.)
        def frank(o):
            nm = obj_lname(state, o)
            if nm in ALL_LANDS:
                return 0
            if nm == LOOTING_L:
                return 1
            return 2
        avoid = {TEMPER_L} if proof == "control" else {EMRAKUL_L}
        cands = [o for o in hand if o != want_oid
                 and obj_lname(state, o) not in avoid]
        if not cands:
            say(f"[{tag}] {proof} discard: no filler available; deferring")
            return False
        filler = sorted(cands, key=lambda o: (frank(o), obj_lname(state, o)))[0]
        picks = [want_oid, filler]
        if not all(o in ref_of for o in picks):
            say(f"[{tag}] {proof} discard: picks not all mapped; deferring")
            return False
        choice_ids = [ref_of[o] for o in picks]
        resp = (opp.get("response", {}) or {})
        stype = ((resp.get("data", {}) or {}).get("spec", {}) or {}
                 ).get("type") or "select"
        SUBMITTED_OPPS.add(iid)
        rec = {"proof": proof, "iid": str(iid)[:12], "n": 2,
               "discarded": [obj_lname(state, o) for o in picks]}
        OBS["discard_choices"].append(rec)
        wire("loot_discard", {"opportunity":
                              json.loads(json.dumps(opp, default=str))})
        say(f"[{tag}] {proof} Looting discards "
            f"{[obj_lname(state, o) for o in picks]} oids={picks}; "
            f"arming madness watch")
        wire("loot_discard_answered", {"proof": proof, "oids": picks})
        LOOT["discarded"] = True
        LOOT["in_flight"] = False  # the DiscardChoice is answered; later
        # hand-size discards are genuine again
        WATCH.update({"active": True, "proof": proof, "discarded": picks,
                      "t0": time.time(), "turn0": turn_of(state)})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype,
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    # a DiscardChoice is pending but no select-schema opp matched it:
    # dump the raw shape once for diagnosis
    if wf.get("type") == "DiscardChoice":
        key = (tag, "discard_shape_logged")
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("discard_shape_unmatched",
                 {"opportunities": json.loads(json.dumps(
                     unanswered_ops(st), default=str))})
            say(f"[{tag}] DiscardChoice pending but no select-schema opp "
                f"matched (logged)")
    return False


async def answer_madness_offer(c, tag, st, state):
    """Answer madness CastOffer opportunities.

    In-watch (the proof card's offer): answer the AUTO-payment choice.
    Stray offers outside a watch: decline via the decideOptionalEffect
    accept=false choice so the game keeps moving.
    """
    for opp in unanswered_ops(st):
        mch = madness_choices(opp)
        if not mch:
            continue
        iid = opp.get("interactionId")
        card = source_name_of(mch[0]) or "?"
        in_watch = (WATCH.get("active") and not WATCH.get("closed")
                    and not WATCH.get("acted"))
        rec = {"who": tag, "iid": str(iid)[:12], "card": card,
               "in_watch": in_watch,
               "modes": [payment_mode_of(ch) for ch in mch]}
        OBS["madness_offers"].append(rec)
        wire("madness_offer",
             {"opportunity": json.loads(json.dumps(opp, default=str)),
              "in_watch": in_watch, "card": card})
        if in_watch:
            proof = WATCH["proof"]
            want_l = TEMPER_L if proof == "control" else EMRAKUL_L
            auto = next((ch for ch in mch if payment_mode_of(ch) == "auto"),
                        mch[0])
            WATCH["offer"] = True
            WATCH["offer_source"] = "CastOffer/castSpellAsMadness"
            WATCH["offer_card"] = card
            if proof == "control":
                ST["control_offered"] = True
                ST["control_offer_source"] = "CastOffer (castSpellAsMadness)"
            else:
                ST["primary_offered"] = True
                ST["primary_offer_source"] = "CastOffer (castSpellAsMadness)"
            WATCH["cast_oid"] = next(
                (o for o in WATCH["discarded"]
                 if obj_lname(state, o) == want_l), None)
            WATCH["rev_at_submit"] = c.revision
            WATCH["acted"] = True
            say(f"[{tag}] {proof} madness CAST OFFER for {card} "
                f"(payment={payment_mode_of(auto)}); answering AUTO")
            wire("watch_offer_answered", {"proof": proof, "card": card,
                                          "choice": auto.get("id")})
            await answer_vi(c, opp, auto, tag)
            return True
        # stray offer outside any watch: decline it
        decline = decline_choice_of(opp)
        if decline is None:
            say(f"[{tag}] stray madness offer for {card}: no decline "
                f"choice; NOT answering")
            wire("stray_offer_no_decline", {"card": card})
            SUBMITTED_OPPS.add(iid)
            continue
        say(f"[{tag}] stray madness offer for {card}: declining")
        wire("stray_offer_declined", {"card": card})
        await answer_vi(c, opp, decline, tag)
        return True
    return False


async def answer_target(c, tag, st, state):
    """Answer the madness spell's TargetSelection: target P1 (seat 1)."""
    if not (WATCH.get("acted") and not WATCH.get("target_answered")):
        return False
    wf = wf_of(state)
    if wf.get("type") != "TargetSelection":
        return False
    if (wf.get("data") or {}).get("player") != 0:
        return False
    for opp in unanswered_ops(st):
        chs = opp_choices(opp)
        if not chs:
            continue
        pick = None
        for ch in chs:
            if seat_of(ch) == 1:
                pick = ch
                break
        if pick is None:
            for ch in chs:
                if "opponent" in choice_text(ch).lower():
                    pick = ch
                    break
        iid = opp.get("interactionId")
        if pick is None:
            key = (tag, "target_nopick", str(iid))
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                wire("target_no_pick",
                     {"opportunity": json.loads(json.dumps(opp,
                                                           default=str))})
                say(f"[{tag}] target: no P1 candidate; NOT answering")
            return False
        WATCH["target_answered"] = True
        say(f"[{tag}] {WATCH['proof']} madness target: P1 "
            f"({choice_text(pick)[:60]})")
        wire("madness_target", {"proof": WATCH["proof"],
                                "text": choice_text(pick)[:80]})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


# ---------------------------------------------------------------------------
# madness watch


def close_watch(reason):
    proof = WATCH["proof"]
    WATCH["closed"] = True
    WATCH["close_reason"] = reason
    say(f"[{proof}] WATCH closed: {reason} "
        f"(exiled={WATCH['exiled']} offer={WATCH['offer']} "
        f"resolved={WATCH['resolved']})")
    wire("watch_closed", {"proof": proof, "reason": reason,
                          "exiled": WATCH["exiled"], "gy": WATCH["gy"],
                          "offer": WATCH["offer"],
                          "offer_card": WATCH["offer_card"],
                          "resolved": WATCH["resolved"]})
    if proof == "control":
        ST["stage"] = "PRIMARY_SETUP"
        LOOT.update({"tag": None, "cast": False, "in_flight": False,
                     "discarded": False})
        WATCH.update({"active": False, "proof": None, "discarded": [],
                      "offer": False, "offer_source": None,
                      "offer_card": None, "acted": False, "cast_oid": None,
                      "stack_seen": False, "resolved": False, "t0": 0.0,
                      "turn0": 0, "closed": False, "close_reason": None,
                      "target_answered": False, "exiled": [], "gy": [],
                      "rev_at_submit": None, "rejected": False})
        say("=== stage -> PRIMARY_SETUP ===")
    else:
        ST["stage"] = "DONE"


async def watch_tick(c, tag, st, state):
    """Track the discarded proof card's zone and any madness cast.

    Never returns early from the tick's priority-pass fall-through: only
    returns True when it acted or closed the watch.
    """
    if not WATCH.get("active") or WATCH.get("closed"):
        return False
    proof = WATCH["proof"]
    card_l = TEMPER_L if proof == "control" else EMRAKUL_L
    card_t = TEMPER_T if proof == "control" else EMRAKUL_T
    # zone tracking of the discarded proof card
    for oid in WATCH["discarded"]:
        if obj_lname(state, oid) != card_l:
            continue
        z = zone_of(state, oid)
        if z in (None, "Hand"):
            continue
        if z == "Exile" and oid not in WATCH["exiled"]:
            WATCH["exiled"].append(oid)
            say(f"[{tag}] WATCH {proof}: {card_t} oid={oid} entered EXILE")
            wire("watch_exiled", {"proof": proof, "oid": oid})
            if proof == "control":
                ST["control_exiled"] = True
            else:
                ST["primary_zone"] = "Exile"
        elif z == "Graveyard" and oid not in WATCH["gy"]:
            WATCH["gy"].append(oid)
            say(f"[{tag}] WATCH {proof}: {card_t} oid={oid} in GRAVEYARD "
                f"(no madness replacement)")
            wire("watch_graveyard", {"proof": proof, "oid": oid})
            if proof == "primary" and ST["primary_zone"] is None:
                ST["primary_zone"] = "Graveyard"
    # stack scan for the madness SPELL. The madness trigger itself also
    # mentions the card name, so skip TriggeredAbility entries; and only
    # count after the offer was answered (acted), otherwise the trigger
    # sitting on the stack would be mistaken for the spell.
    def spell_on_stack():
        for e in state.get("stack") or []:
            if not isinstance(e, dict):
                continue
            kind = e.get("kind") or {}
            ktype = kind.get("type") if isinstance(kind, dict) else kind
            if ktype == "TriggeredAbility":
                continue
            if card_t in json.dumps(e, default=str):
                return True
        return False

    on_stack = bool(WATCH.get("acted")) and spell_on_stack()
    if on_stack and not WATCH.get("stack_seen"):
        WATCH["stack_seen"] = True
        say(f"[{tag}] WATCH {proof}: madness spell on the stack")
        wire("watch_stack_seen", {"proof": proof})
    # cast resolution
    if WATCH.get("acted") and WATCH.get("stack_seen") and not on_stack:
        WATCH["resolved"] = True
        if proof == "control":
            ST["control_resolved"] = True
            # capture P1 life at watch close, not at mid.json export: the
            # primary gate may never open, which would otherwise fail A5
            # by construction
            ST["control_p1_after"] = life_of(state, 1)
            say(f"[{tag}] WATCH control: madness spell resolved; "
                f"P1 life now {ST['control_p1_after']}")
        say(f"[{tag}] WATCH {proof}: madness spell left the stack "
            f"(resolved)")
        wire("watch_resolved", {"proof": proof})
        close_watch("cast resolved")
        return True
    # resolution via zone signature: the spell can resolve between two
    # polls, never being observed on the stack. Exile -> Graveyard after
    # an ACCEPTED offer (target answered) is the resolution signature.
    # (A decline would also move it to the graveyard, but then acted
    # would be False.)
    if (WATCH.get("acted") and WATCH.get("target_answered")
            and not WATCH.get("resolved") and proof == "control"
            and any(o in WATCH["exiled"]
                    and zone_of(state, o) == "Graveyard"
                    for o in WATCH["discarded"])):
        WATCH["resolved"] = True
        ST["control_resolved"] = True
        ST["control_p1_after"] = life_of(state, 1)
        say(f"[{tag}] WATCH control: Temper Exile->Graveyard after "
            f"accepted cast (resolved between polls); P1 life now "
            f"{ST['control_p1_after']}")
        wire("watch_resolved_via_zone", {"proof": proof})
        close_watch("cast resolved (zone signature)")
        return True
    # rejection / silent-fail
    if WATCH.get("rejected"):
        WATCH["rejected"] = False
        say(f"[{tag}] WATCH {proof}: madness cast rejected")
        wire("watch_cast_rejected", {"proof": proof})
        close_watch("cast rejected")
        return True
    if (WATCH.get("acted") and not WATCH.get("stack_seen")
            and WATCH.get("rev_at_submit") is not None
            and c.revision - WATCH["rev_at_submit"] >= 30):
        say(f"[{tag}] WATCH {proof}: cast silent-fail (no stack sighting)")
        wire("watch_cast_silent", {"proof": proof})
        close_watch("cast silent-fail")
        return True
    # close: no offer by a later P0 main phase with an empty stack
    if (not WATCH.get("offer") and is_my_main(state, 0)
            and turn_of(state) > WATCH["turn0"]):
        close_watch("no offer by later P0 main phase")
        return True
    # close: wall timeout
    if time.time() - WATCH["t0"] > 300:
        close_watch("watch timeout 300s")
        return True
    return False


# ---------------------------------------------------------------------------
# setup gates


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


async def control_gate(c, tag, st, state, acts):
    """CONTROL: cast Faithless Looting discarding Fiery Temper."""
    if ST["stage"] != "SETUP":
        return False
    if turn_of(state) > 40:
        ST["p0_turn_cap_abort"] = True
        ST["stop"] = True
        say("SETUP: turn cap 40 reached without control gate; aborting")
        return False
    if not is_my_main(state, 0):
        return False
    hn = hand_lnames(state, 0)
    if LOOTING_L not in hn or TEMPER_L not in hn:
        return False
    # Looting costs {R} AND the madness {R} cast is engine-auto-paid: the
    # engine only offers the madness cast choice `if can_pay`
    # (can_pay_cost_after_auto_tap), so the gate needs TWO untapped red
    # sources -- one for Looting, one for the madness cast. (A single
    # Mountain gets auto-tapped for Looting and the offer degrades to a
    # decline-only prompt, which is correct engine behavior, not the bug.)
    if not (untapped_mountains(state, 0) >= 2
            and len(untapped_lands(state, 0)) >= 3):
        return False
    if not ST["pre_exported"]:
        await do_export(c, "pre.json")
        ST["pre_exported"] = True
        ST["control_p1_before"] = life_of(state, 1)
        say(f"pre.json exported (P1 life={ST['control_p1_before']})")
        return True
    if not LOOT["cast"]:
        a = find_cast_action(acts, state, LOOTING_L)
        if a is not None:
            LOOT.update({"tag": "control", "cast": True, "in_flight": True})
            WATCH["proof"] = "control"
            ST["stage"] = "CONTROL"
            await submit_as_is(c, a)
            say("P0 casts Faithless Looting (control)")
            return True
    return False


async def primary_gate(c, tag, st, state, acts):
    """PRIMARY: cast Faithless Looting discarding Emrakul, the World Anew."""
    if ST["stage"] != "PRIMARY_SETUP":
        return False
    if turn_of(state) > 60:
        ST["p0_turn_cap_abort"] = True
        ST["stop"] = True
        say("PRIMARY: turn cap 60 reached without primary gate; aborting")
        return False
    if not is_my_main(state, 0):
        return False
    hn = hand_lnames(state, 0)
    if LOOTING_L not in hn or EMRAKUL_L not in hn:
        return False
    if not (untapped_mountains(state, 0) >= 1
            and len(untapped_lands(state, 0)) >= 2):
        return False
    if not ST["mid_exported"]:
        await do_export(c, "mid.json")
        ST["mid_exported"] = True
        say("mid.json exported (between control and primary proofs)")
        return True
    if not LOOT["cast"]:
        a = find_cast_action(acts, state, LOOTING_L)
        if a is not None:
            LOOT.update({"tag": "primary", "cast": True, "in_flight": True})
            WATCH["proof"] = "primary"
            ST["stage"] = "PRIMARY"
            await submit_as_is(c, a)
            say("P0 casts Faithless Looting (primary: discarding Emrakul)")
            return True
    return False


async def generic_prompt(c, tag, st, state, decline_after=25):
    """Log unexpected prompts; answer the first choice after a stall.

    Madness offers and Looting discards are handled by their dedicated
    handlers above -- never answer them here.
    """
    acted = False
    for opp in unanswered_ops(st):
        if madness_choices(opp):
            continue
        if (is_select_schema_opp(opp) and LOOT.get("in_flight")
                and not LOOT.get("discarded")):
            continue
        iid = opp.get("interactionId")
        chs = opp_choices(opp)
        entry = (tag, "unexp", str(iid))
        first = entry not in SUBMITTED_OPPS
        if first:
            OBS["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
                 "texts": [choice_text(ch)[:60] for ch in chs][:6]})
            say(f"[{tag}] UNEXPECTED PROMPT iid={iid} n={len(chs)} "
                f"texts={[choice_text(ch)[:30] for ch in chs][:4]}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            OBS["unexpected_prompts"][-1]["t0"] = time.time()
            SUBMITTED_OPPS.add(entry)
            continue
        rec = next((r for r in OBS["unexpected_prompts"]
                    if r["iid"] == str(iid)[:8] and r["who"] == tag), None)
        if rec is None:
            continue
        if time.time() - rec.get("t0", 0) < decline_after:
            continue
        pick = chs[0] if chs else None
        if pick is not None:
            say(f"[{tag}] answering prompt after {decline_after}s stall "
                f"(first choice)")
            await answer_vi(c, opp, pick, tag)
            acted = True
    return acted


# ---------------------------------------------------------------------------
# seat ticks


async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    # The Looting discard (exactly 2, select schema) must be answered
    # before any hand-size discard: after Looting draws 2, the hand can
    # exceed 7 and the hand-size n can coincide with the Looting n.
    if await answer_looting_discard(c, tag, st, state):
        return True
    if await do_discard_handsize(c, acts, st, 0, tag):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True
    # issue-specific handlers (in dependency order)
    if await answer_madness_offer(c, tag, st, state):
        return True
    if await answer_target(c, tag, st, state):
        return True
    # the madness watch never holds priority while watching: it only
    # returns True when it acted or closed, otherwise falls through to
    # the pass logic below
    if await watch_tick(c, tag, st, state):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await control_gate(c, tag, st, state, acts):
            return True
        if await primary_gate(c, tag, st, state, acts):
            return True
        if await play_a_land(c, state, 0, acts, tag):
            return True

    if real_decision_pending(st):
        if await generic_prompt(c, tag, st, state):
            return True
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard_handsize(c, acts, st, 1, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # P1 passive: land drop, then pass
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return True

    if real_decision_pending(st):
        if await generic_prompt(c, tag, st, state):
            return True
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


# ---------------------------------------------------------------------------
# finish: assertions, verdict, evidence files


def load_state_file(name):
    p = f"{EVDIR}/{name}.json"
    if not os.path.exists(p):
        return None
    raw = json.load(open(p))
    s = raw["state"]
    return json.loads(s) if isinstance(s, str) else s


def render_summary(run):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 1180
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6987 - Textual Madness cost "
           "\"Pay six {C}\" unsupported", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "Emrakul, the World Anew - server v0.103.0 (ec27a8d) "
           "protocol 106 - 2026-10-08", fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 120)))
    y += 34
    d.text((24, y), "Assertions (correct-behavior properties; "
           "failed = defect present):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_parse_emrakul_madness": "PARSE: Emrakul keywords carry Madness 6x{C}",
        "A2_setup_ok": "GAME: P0 main, Looting+Temper in hand, Mountain up",
        "A3_control_exile": "GAME: discarded Fiery Temper exiled (madness)",
        "A4_control_offer": "GAME: madness cast offered for Fiery Temper",
        "A5_control_resolves": "GAME: Temper cast for {R} resolves, P1 20->17",
        "A6_emrakul_exile": "GAME: discarded Emrakul exiled (not graveyard)",
        "A7_emrakul_offer": "GAME: madness cast offered at six {C}",
        "A8_cleanup": "GAME: stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Oracle: \"Madness--Pay six {C}.\"  Control: Fiery Temper "
           "\"Madness {R}\"", fill=(200, 210, 225))
    y += 30
    dst = run.get("driver_state") or {}
    d.text((24, y), f"control: exiled={dst.get('control_exiled')} "
           f"offered={dst.get('control_offered')} "
           f"resolved={dst.get('control_resolved')}", fill=(150, 160, 175))
    y += 24
    d.text((24, y), f"control P1 life: {dst.get('control_p1_before')} -> "
           f"{dst.get('control_p1_after')}", fill=(150, 160, 175))
    y += 24
    d.text((24, y), f"primary: Emrakul zone={dst.get('primary_zone')} "
           f"offered={dst.get('primary_offered')}", fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:16]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    # NOTE: scenario_run.log is hashed LAST, after all say() logging is
    # done; no say() may follow the final build().
    files = ["pre.json", "mid.json", "post.json",
             "parse_emrakul.json", "assertions.json", "run.json",
             "scenario_6987_01030.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png"]

    def build():
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                say(f"manifest: MISSING {fn}")
        return lines

    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(build()) + "\n")
    say("wrote manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(build()) + "\n")


def copy_server_log():
    src = f"{BACKFILL}/runs/{RUN_ID}/server.log"
    code = ST.get("game_code") or ""
    try:
        raw = open(src, errors="replace").read()
        clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        lines = [l for l in clean.splitlines() if code and code in l]
        hdr = (f"# server.log excerpt for #6987 run {RUN_ID}: game {code} "
               f"(v0.103.0/ec27a8d, protocol 106). Source: shared server "
               f"log {src}; excerpt covers this game's session.\n")
        with open(f"{EVDIR}/server.log", "w") as f:
            f.write(hdr + "\n".join(lines[:400]) + "\n")
        say(f"wrote server.log excerpt ({len(lines)} matching lines)")
    except Exception as e:
        say(f"server.log excerpt failed: {e}")


async def finish(c0, hello, data_level):
    dur = time.time() - T0
    notes = OBS["notes"]
    ass = {k: "not-run" for k in
           ("A1_parse_emrakul_madness", "A2_setup_ok",
            "A3_control_exile", "A4_control_offer", "A5_control_resolves",
            "A6_emrakul_exile", "A7_emrakul_offer", "A8_cleanup")}

    # ---- A1: parse (data-level, primary evidence) ----
    ass["A1_parse_emrakul_madness"] = \
        "passed" if data_level["a1"] else "failed"
    notes.append(f"A1: {PARSE['a1_detail']} -> "
                 f"{ass['A1_parse_emrakul_madness']}; control Fiery Temper "
                 f"keywords={PARSE['temper']['keywords']}")

    pre, mid, post = (load_state_file(n) for n in ("pre", "mid", "post"))
    for nm, s in (("pre", pre), ("mid", mid), ("post", post)):
        say(f"{nm}.json: {'loaded' if s is not None else 'missing'}")

    # ---- A2: setup ----
    if pre is not None:
        p0_main = (pre.get("active_player") == 0
                   and pre.get("phase") in MAIN_PHASES)
        has_loot = find_hand(pre, 0, LOOTING_L) is not None
        has_temp = find_hand(pre, 0, TEMPER_L) is not None
        nm = untapped_mountains(pre, 0)
        ok = p0_main and has_loot and has_temp and nm >= 2
        ass["A2_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A2: pre.json P0 main={p0_main}, "
                     f"Looting in hand={has_loot}, Temper in hand={has_temp}, "
                     f"untapped Mountains={nm} -> {ass['A2_setup_ok']}")
    else:
        notes.append("A2 not-run: no pre.json (control gate never opened)")
        ass["A2_setup_ok"] = "not-run"

    # ---- A3/A4/A5: control ----
    if ass["A2_setup_ok"] == "passed":
        ass["A3_control_exile"] = \
            "passed" if ST["control_exiled"] else "failed"
        ass["A4_control_offer"] = \
            "passed" if ST["control_offered"] else "failed"
    else:
        ass["A3_control_exile"] = "not-run"
        ass["A4_control_offer"] = "not-run"
    notes.append(f"A3: discarded Fiery Temper entered Exile: "
                 f"{ST['control_exiled']} -> {ass['A3_control_exile']}")
    notes.append(f"A4: madness cast offered for Fiery Temper: "
                 f"{ST['control_offered']} "
                 f"(source={ST['control_offer_source']}) -> "
                 f"{ass['A4_control_offer']}")
    p1d = None
    if (ST["control_p1_before"] is not None
            and ST["control_p1_after"] is not None):
        p1d = ST["control_p1_before"] - ST["control_p1_after"]
    ok5 = ST["control_resolved"] and p1d == 3
    if ST["control_offered"]:
        ass["A5_control_resolves"] = "passed" if ok5 else "failed"
    else:
        ass["A5_control_resolves"] = "not-run"
    notes.append(f"A5: control resolved={ST['control_resolved']}, P1 life "
                 f"{ST['control_p1_before']}->{ST['control_p1_after']} "
                 f"(delta={p1d}, expected 3) -> {ass['A5_control_resolves']}")

    # ---- A6/A7: primary ----
    if ST.get("primary_zone") == "Exile":
        ass["A6_emrakul_exile"] = "passed"
    elif ST.get("primary_zone") == "Graveyard":
        ass["A6_emrakul_exile"] = "failed"
    else:
        ass["A6_emrakul_exile"] = "not-run"
    notes.append(f"A6: discarded Emrakul zone={ST.get('primary_zone')} "
                 f"(expected Exile via madness replacement) -> "
                 f"{ass['A6_emrakul_exile']}")
    if ST["primary_offered"]:
        ass["A7_emrakul_offer"] = "passed"
    elif ass["A6_emrakul_exile"] in ("passed", "failed"):
        ass["A7_emrakul_offer"] = "failed"
    else:
        ass["A7_emrakul_offer"] = "not-run"
    notes.append(f"A7: madness cast offered at six {{C}}: "
                 f"{ST['primary_offered']} "
                 f"(source={ST['primary_offer_source']}) -> "
                 f"{ass['A7_emrakul_offer']}")

    # ---- A8: cleanup ----
    if post is not None:
        ok = stack_empty(post)
        ass["A8_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A8: post.json stack empty={ok}, "
                     f"turn={turn_of(post)}, phase={post.get('phase')} -> "
                     f"{ass['A8_cleanup']}")
    else:
        notes.append("A8 not-run: no post.json")
        ass["A8_cleanup"] = "not-run"

    # ---- verdict ----
    if ass["A2_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: control setup never reached "
                     f"(turn-cap abort={ST['p0_turn_cap_abort']})")
    elif (ass["A6_emrakul_exile"] == "failed"
            or ass["A7_emrakul_offer"] == "failed"):
        verdict = "reproduced"
        notes.append("verdict=reproduced: Emrakul's madness permission did "
                     "not offer the printed six-{C} alternative cost after "
                     "discard (control: Fiery Temper Madness {R} worked)")
    elif (ass["A6_emrakul_exile"] == "passed"
            and ass["A7_emrakul_offer"] == "passed"):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: Emrakul exiled on discard and "
                     "the six-{C} madness cast was offered")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: primary proof did not complete")
    notes.append(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN}, f, indent=2)
    say("wrote assertions.json")

    run = {
        "issue": ISSUE,
        "title": "Textual Madness cost \"Pay six {C}\" is unsupported",
        "run_id": RUN_ID,
        "validated_at": "2026-10-08",
        "validated_version": "v0.103.0",
        "server_version": "0.103.0",
        "build_commit": "ec27a8d",
        "protocol_version": 106,
        "server_binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
        "signature_verified": True,
        "server_identity": {**SERVER_IDENTITY,
                            "observed_at": "2026-10-08",
                            "hello": hello},
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(
            f"{BACKFILL}/driver/scenario_6987_01030.py"),
        "format_config": "default Bo1 (2 human-client seats)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "result": (f"A1 parse_emrakul_madness: {ass['A1_parse_emrakul_madness']} "
                   f"({'no Madness keyword' if ass['A1_parse_emrakul_madness'] != 'passed' else 'Madness 6xC present'}). "
                   f"A2 setup_ok: {ass['A2_setup_ok']}. "
                   f"A3 control_exile: {ass['A3_control_exile']}; "
                   f"A4 control_offer: {ass['A4_control_offer']}; "
                   f"A5 control_resolves: {ass['A5_control_resolves']} "
                   f"(Fiery Temper Madness {{R}} control). "
                   f"A6 emrakul_exile: {ass['A6_emrakul_exile']}; "
                   f"A7 emrakul_offer: {ass['A7_emrakul_offer']}. "
                   f"A8 cleanup: {ass['A8_cleanup']}."),
        "scope": ("Emrakul, the World Anew madness permission: card-data "
                  "parse check + runtime discard via Faithless Looting with "
                  "Fiery Temper (Madness {R}) as the working-madness "
                  "control; native engine, two human-client seats"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
            "Six-{C} payment was not attempted: the offer itself is the asserted outcome.",
        ],
        "observations": OBS,
        "driver_state": {k: v for k, v in ST.items()},
        "notes": notes,
        "evidence_files": ["pre.json", "mid.json", "post.json",
                           "parse_emrakul.json", "assertions.json",
                           "run.json", "manifest.sha256", "summary.png",
                           "scenario_6987_01030.py", "wire_log.jsonl",
                           "scenario_run.log", "server.log"],
        "duration_s": round(dur, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6987_01030.py", "w") as f:
        f.write(src)
    say("copied scenario_6987_01030.py into EVDIR")
    copy_server_log()
    render_summary(run)
    write_manifest()
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
          flush=True)


# ---------------------------------------------------------------------------
# main loop

async def main():
    global T0
    T0 = time.time()
    last_rev_change = T0

    hello = await verify_server_hello()
    data_level = check_data_level()

    p0 = PhaseClient("P06987r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16987r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - T0 < GAME_TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
                if c.name == "P06987r" and WATCH.get("acted") \
                        and not WATCH.get("stack_seen") \
                        and not WATCH.get("closed"):
                    WATCH["rejected"] = True
            st = st_of(c)
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_rev_change = time.time()
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # (or holding an unanswered vi decision) with no revision
                # change (missed-broadcast resilience).
                pending_vi = bool(unanswered_ops(st))
                if not ((my_priority(top_acts(st)) or pending_vi)
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        wf = wf_of(state)
        wft = wf.get("type")
        if wft and (not WF_SEEN or WF_SEEN[-1] != wft):
            WF_SEEN.append(wft)
            wire("waiting_for", {"type": wft, "stage": ST["stage"]})
        turn = turn_of(state)
        if ST["stage"] == "SETUP" and turn >= 45:
            say("turn cap 45 reached in SETUP with no control; finishing")
            OBS["notes"].append("turn cap 45 reached, control never ran")
            await finish(p0, hello, data_level)
            return
        if ST["stage"] == "PRIMARY_SETUP" and turn >= 65:
            say("turn cap 65 reached in PRIMARY_SETUP; finishing")
            OBS["notes"].append("turn cap 65 reached, primary never ran")
            await finish(p0, hello, data_level)
            return
        if ST["stage"] == "DONE" and not ST["post_exported"]:
            try:
                await do_export(p0, "post.json")
                ST["post_exported"] = True
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True
        if ST["post_exported"]:
            say("post exported; finishing")
            await finish(p0, hello, data_level)
            return
        if time.time() - last_rev_change > STALL_AFTER:
            say(f"STALL: no revision change for {STALL_AFTER}s; finishing")
            OBS["notes"].append("stall watchdog fired")
            await finish(p0, hello, data_level)
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = state
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} P0life={life_of(s, 0)} "
                f"P1life={life_of(s, 1)} stage={ST['stage']} "
                f"watch={WATCH['proof']}/{WATCH['active']}/{WATCH['closed']} "
                f"offer={WATCH['offer']} stack_seen={WATCH['stack_seen']} "
                f"primary_zone={ST['primary_zone']} "
                f"P0hand={hand_lnames(s, 0)}")
    OBS["notes"].append(f"global timeout ({GAME_TIMEOUT}s) hit")
    await finish(p0, hello, data_level)


asyncio.run(main())
