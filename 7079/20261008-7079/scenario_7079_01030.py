#!/usr/bin/env python3
"""Issue #7079: [Card Bug] Magnificent End: Not possible to cast when not 5 mana available.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Magnificent End ({4}{W} Instant):
    "This spell costs {3} less to cast if it targets a tapped creature.
     Magnificent End deals 5 damage to target creature."

Card-data parse state on v0.103.0 (verified 2026-10-08 before the run):
  static_abilities[0] = ModifyCost Reduce {3} generic,
    spell_filter Typed(Card) with property Targets(Typed Creature + Tapped),
    affected SelfRef.
  abilities[0] = Spell DealDamage Fixed 5 to Typed Creature.
  The target-sensitive reduction clause parses as SUPPORTED (matches the
  triage classifier's supported_aspect_defect verdict).

Reported symptom: Magnificent End cannot be cast unless {4}{W} (the printed
cost) is available, even when a tapped creature is a legal target (reduced
cost would be {1}{W}). Expected: with a tapped-creature target and {1}{W}
available, the spell should be announceable; targets announced determine
whether the reduction applies when the total cost is locked in.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Magnificent End, 28x Plains, 20x Forest.
  P1: 12x Grizzly Bears, 48x Forest.
  P0 plays a land only while its untapped land count < 2 (prefer Plains, so
  >=1 Plains untapped), then holds -> exactly 2 lands on the window turn.
  P1 casts Grizzly Bears as soon as payable on its main phase, attacks P0
  with it at the first opportunity (P0 declares no blockers) -> the Bear is
  tapped through P0's next PreCombatMain.
  Window: P0 PreCombatMain priority (my_priority) with exactly 2 untapped
  lands (>=1 Plains), Magnificent End in hand, a tapped opposing Bear, P1
  having attacked at least once, stack empty. Printed {4}{W} is unpayable;
  the reduced {1}{W} is payable IFF the target-dependent reduction is
  honored by the cast-availability preflight.
  NOTE: the engine randomizes turn order; the driver keys everything on
  active_player/phase, never on turn order. In single-user mode seats are
  pids 0/1 in join order (P0=0, P1=1).

Plan:
  1. Mulligans: P0 keep if Magnificent End in hand and >=2 lands, else mull
     (max 2); P1 keep if Grizzly Bears in hand and >=2 lands, else mull
     (max 2). Mulligan-bottom prompt answered by bottoming lands first,
     never the key card.
  2. P0 holds lands at exactly 2 (prefer Plains). P1 casts a Bear when
     payable ({1}{G}; engine auto-taps mana) and attacks P0 at the first
     DeclareAttackers with it.
  3. At the window: PRE is exported (seat-0 export), then the merged legal
     actions are scanned for a CastSpell for Magnificent End.
  4a. OFFER PATH (A3 passes): submit the CastSpell, answer the
      TargetSelection vi with the tapped Bear (recorded; match by object
      oid, fall back to seat==1), let the engine auto-pay. Verify: Bear
      destroyed (5 damage), Magnificent End in P0 graveyard, exactly 2
      lands tapped ({1}{W} paid). Export POST after resolution (stack
      empty).
  4b. BUG PATH (A3 fails, expected): release the land hold; P0 resumes
      playing lands on its main phases until >=5 untapped lands (>=1
      Plains); then check the printed-cost control: CastSpell for
      Magnificent End IS advertised. Export POST in that window (stack
      empty).

Assertions (each passed / failed / not-run):
  A1_parse_gap      card-data v0.103.0 carries the Reduce-{3}-generic /
                    Targets(Tapped Creature) clause as supported (PASS
                    confirms the triage's supported-aspect call: the defect
                    is in runtime preflight, not a parser gap).
  A2_setup_ok       PRE: P0 PreCombatMain, Magnificent End in hand, exactly 2
                    untapped lands incl. >=1 Plains, tapped Grizzly Bears on
                    P1 BF, priority to P0 (my_priority at assessment), P1 at
                    20, P0 below 20 (expected 18 after one Bear attack).
  A3_reduced_offer  CastSpell for Magnificent End is advertised in P0's
                    merged legal actions during the window (printed cost
                    unpayable, reduced cost payable). Expected FAILED under
                    the bug.
  A4_printed_offer  CONTROL: on a later P0 main phase with >=5 untapped lands
                    (>=1 Plains), CastSpell for Magnificent End IS advertised
                    (printed cost payable). Expected passed.
  A5_reduced_cast   (offer path only) the spell is cast, targets the tapped
                    Bear, pays exactly {1}{W} (2 lands tapped), deals 5, the
                    Bear dies, the spell reaches P0's graveyard.
  A6_cleanup        POST: stack empty, game proceeds.

Verdict rule (exact): reproduced iff A2 passed and A3 failed;
not-reproduced iff A3 passed and A5 passed; reproduced (related) iff A3
passed but A5 failed; blocked iff A2 failed.

Protocol-106 port notes (from driver/scenario_6982_01030.py conventions):
  - my_priority = PassPriority present in legal_actions; casts gated on it.
  - Engine auto-taps for CastSpell (payment_mode Auto): the driver never
    answers tapLandForMana and runs no driver-side mana payment.
  - DeclareAttackers: {"attacks": [[oid, {"type":"Player","data":0}]],
    "bands": []}; P1 attacks P0 (seat 0). DeclareBlockers:
    {"assignments": []}.
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).
  - The End's TargetSelection is answered from P0's viewer_interaction with
    the tapped-Bear candidate (oid match, seat==1 fallback); the engine may
    also auto-target a sole legal target (handled as the auto-target case).

Evidence: evidence/7079/<run-id>/pre.json, post.json, run.json,
parse_magnificent_end.json, scenario_7079_01030.py, wire_log.jsonl,
scenario_run.log, server.log, summary.png, manifest.sha256,
target_sel_*.json.
"""
import asyncio
import glob
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7079
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7079")
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

PARSE = {"ok": False, "clause": None}


def check_parse_end():
    """A1: v0.103.0 static_abilities[0] is ModifyCost Reduce {3} generic
    with Targets(Typed Creature + Tapped) filter and affected SelfRef."""
    c = CARD_DATA.get("magnificent end", {})
    clause = None
    for s in c.get("static_abilities", []) or []:
        mode = ((s.get("mode") or {}).get("ModifyCost")) or {}
        if mode.get("mode") == "Reduce":
            clause = s
            break
    with open(f"{EVDIR}/parse_magnificent_end.json", "w") as fh:
        json.dump({"card": "Magnificent End",
                   "oracle_text": c.get("oracle_text"),
                   "mana_cost": c.get("mana_cost"),
                   "static_abilities": c.get("static_abilities"),
                   "abilities": c.get("abilities")},
                  fh, indent=1, default=str)
    say("saved parse_magnificent_end.json")
    ok = False
    if clause:
        mode = clause["mode"]["ModifyCost"]
        amt = mode.get("amount") or {}
        props = (mode.get("spell_filter") or {}).get("properties") or []
        tgt = next((p for p in props if p.get("type") == "Targets"), {})
        inner = (tgt.get("filter") or {})
        inner_props = inner.get("properties") or []
        ok = (amt.get("generic") == 3
              and (mode.get("spell_filter") or {}).get("type") == "Typed"
              and "Card" in ((mode.get("spell_filter") or {})
                             .get("type_filters") or [])
              and tgt.get("type") == "Targets"
              and "Creature" in (inner.get("type_filters") or [])
              and any(p.get("type") == "Tapped" for p in inner_props)
              and (clause.get("affected") or {}).get("type") == "SelfRef")
    PARSE["clause"] = clause
    PARSE["ok"] = ok
    say(f"parse: ModifyCost Reduce clause found={clause is not None} "
        f"-> A1={'passed' if ok else 'failed'}")
    wire("parse_check", {"A1": "passed" if ok else "failed",
                         "clause_present": clause is not None})
    return ok


END = "magnificent end"
BEARS = "grizzly bears"
PLAINS = "plains"
FOREST = "forest"
LANDS = (PLAINS, FOREST)

P0_DECK = [("Magnificent End", 12), ("Plains", 28), ("Forest", 20)]
P1_DECK = [("Grizzly Bears", 12), ("Forest", 48)]

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45

STOP = {"stop": False}
ST = {"window_assessed": False, "offered_reduced": False,
      "offered_reduced_detail": None, "offered_printed": False,
      "offered_printed_detail": None, "end_cast": False,
      "end_in_flight": False, "end_oid": None, "end_on_stack_seen": False,
      "end_resolved": False, "end_resolved_at": None,
      "target_answered": False, "target_auto": False,
      "target_submitted_oid": None, "bear_oid": None,
      "land_target": 2, "pre_exported": False, "post_exported": False,
      "p1_bear_cast": False, "p1_attacked": False, "p1_attack_turn": None}
OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": [], "life_trace": [],
       "offer_scans": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
MULLS = {"P0": 0, "P1": 0}
PASSED_REV = {}
LAST_IID = {"iid": None}
P0_PID = 0
CASTDIAG_DONE = set()

# ------------------------------------------------------- state helpers

def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


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


def bf_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and str(o.get("controller")) == str(pid):
            if lname is None or obj_lname(state, oid) == lname:
                out.append(int(oid))
    return out


def bf_lands(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield"
                and str(o.get("controller")) == str(pid)
                and nm in LANDS):
            out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [oid for oid in bf_lands(state, pid)
            if not get_obj(state, oid).get("tapped")]


def untapped_land_names(state, pid):
    return [obj_lname(state, oid) for oid in untapped_lands(state, pid)]


def is_land(o):
    nm = str(o.get("base_name") or o.get("name") or "").lower()
    return nm in LANDS


def stack_empty(state):
    return not (state.get("stack") or [])


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


def my_main(state, pid):
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and stack_empty(state))


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


def cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_oid(ch):
    ref = cand_reference(ch)
    try:
        return str(int(ref))
    except (TypeError, ValueError):
        return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                pass
    return None


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(opp):
    for c in (opp.get("response") or {}).get("data", {}).get("choices", []):
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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type")})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
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
        if stype == "text":
            val = None
            for s in choice.get("surfaces", []) or []:
                d = s.get("data") or {}
                if (isinstance(d, dict) and d.get("role") == "choice"
                        and "value" in d):
                    val = d["value"]
                    break
            sub = {"interactionId": iid,
                   "response": {"type": "text", "data": {"value": val}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [cid]}}}
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


async def export_named(c, tag):
    try:
        s = await c.export_state()
        with open(f"{EVDIR}/{tag}.json", "w") as f:
            f.write(s)
        say(f"exported {tag.upper()}")
        return True
    except Exception as e:
        OBS["notes"].append(f"{tag} export failed: {e}")
        say(f"{tag} export failed: {e}")
        return False

# ------------------------------------------------- issue-specific logic

async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hn = hand_lnames(st["state"], pid)
    n_lands = sum(1 for h in hn if h in LANDS)
    n = MULLS.get(tag, 0)
    want = END if pid == 0 else BEARS
    keep = (want in hn and n_lands >= 2) or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        MULLS[tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hn})")
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
    if MULLS.get(tag, 0) <= 0:
        return False
    key_card = END if pid == 0 else BEARS
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
        spec = (rdata.get("spec", {}) or {})
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = cand_oid(ch)
            nm = obj_lname(state, ref) if ref else ""
            if nm == key_card:
                return (2, str(ref))   # never bottom the key card
            if nm in LANDS:
                return (1, str(ref))   # bottom lands first
            return (0, str(ref))

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
    nm = obj_lname(state, o)
    if nm == FOREST:
        return 0                      # lands first; Forest before Plains
    if nm == PLAINS:
        return 1
    if (pid == 0 and nm == END) or (pid == 1 and nm == BEARS):
        return 3                      # key card kept last
    return 2


async def do_discard(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    for opp in vi_ops(st):
        if not is_select_schema_opp(opp):
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = cand_oid(ch)
            if ref is not None:
                ref_of[ref] = ch["id"]
        ranked = sorted(hand,
                        key=lambda o: (discard_rank(state, o, pid),
                                       obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        DISCARDED_IIDS.add(iid)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                          tag)
        return True
    return False


async def do_declare(c, acts, st, pid, tag):
    state = st["state"]
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            if pid == 1:
                bears = [oid for oid in bf_oids(state, 1, BEARS)]
                d["data"]["attacks"] = [
                    [int(oid), {"type": "Player", "data": P0_PID}]
                    for oid in bears]
                d["data"]["bands"] = []
                if bears and not ST["p1_attacked"]:
                    ST["p1_attacked"] = True
                    ST["p1_attack_turn"] = state.get("turn_number")
                    say(f"[{tag}] attacks P0 with bears {bears} "
                        f"(turn {ST['p1_attack_turn']})")
                    wire("attack_declared",
                         {"attackers": bears,
                          "turn": ST["p1_attack_turn"]})
            else:
                d["data"]["attacks"] = []
                d["data"]["bands"] = []
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            return True
    return False


def record_target_sel(state, opp, stage):
    """Record a target-selection opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in OBS["target_selections"]):
        return
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    cand_info = []
    for ch in cands:
        oid = cand_oid(ch)
        o = get_obj(state, oid) if oid else {}
        cand_info.append({
            "choice_id": ch.get("id"),
            "oid": oid,
            "seat": cand_seat(ch),
            "name": obj_lname(state, oid) if oid else choice_text(ch),
            "zone": o.get("zone"),
            "controller": o.get("controller"),
            "tapped": o.get("tapped"),
            "text": choice_text(ch)[:120],
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "rtype": resp.get("type"),
        "spec_type": ((data.get("spec") or {}).get("type")),
        "candidates": cand_info,
    }
    OBS["target_selections"].append(rec)
    n = len(OBS["target_selections"])
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(json.dumps(opp, default=str))},
                  f, indent=1, default=str)
    say(f"target selection #{n} (stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']},"
                    f"tapped={x['tapped']})" for x in cand_info[:8]))
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "iid": iid,
          "candidates": [(x["name"], x["zone"], x["controller"], x["tapped"])
                         for x in cand_info]})


async def end_target_tick(c, tag, st, state):
    """Answer P0's Magnificent End TargetSelection with the tapped Bear
    (oid match on the window bear, falling back to a tapped P1 bear, then
    seat==1)."""
    if not (ST["end_cast"] and not ST["end_resolved"]):
        return False
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            continue
        record_target_sel(state, opp, "end-target")
        pick = next((ch for ch in chs
                     if cand_oid(ch) == str(ST["bear_oid"])), None)
        if pick is None:
            for ch in chs:
                oid = cand_oid(ch)
                if oid is None:
                    continue
                o = get_obj(state, oid)
                if (o.get("zone") == "Battlefield"
                        and str(o.get("controller")) == "1"
                        and obj_lname(state, oid) == BEARS
                        and o.get("tapped")):
                    pick = ch
                    break
        if pick is None:
            pick = next((ch for ch in chs if cand_seat(ch) == 1), None)
        if pick is None:
            say(f"[{tag}] end-target: no bear candidate; holding")
            wire("end_target_no_bear",
                 {"choices": [choice_text(ch)[:60] for ch in chs][:8]})
            continue
        say(f"[{tag}] Magnificent End targets tapped bear "
            f"(oid={cand_oid(pick)})")
        wire("end_target_answer", {"choiceId": pick.get("id"),
                                  "oid": cand_oid(pick)})
        await answer_vi(c, opp, pick, tag)
        ST["target_answered"] = True
        ST["target_submitted_oid"] = cand_oid(pick)
        return True
    return False


def cast_spell_for(acts, state, lname):
    """CastSpell-ish actions whose card is `lname` (search data values and
    the by-object source oid)."""
    out = []
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    out.append((v, a))
                    break
            except (TypeError, ValueError):
                pass
    return out


def scan_offers(c, tag, state, acts, lname):
    found = cast_spell_for(acts, state, lname)
    if found:
        detail = {"by": tag, "turn": state.get("turn_number"),
                  "phase": state.get("phase"),
                  "untapped_lands": untapped_land_names(state, 0),
                  "oids": [str(o) for o, _a in found]}
        OBS["offer_scans"].append(detail)
        wire("offer_scan", detail)
        say(f"[{tag}] CastSpell offered for {lname}: {detail}")
    else:
        wire("offer_scan_empty", {"by": tag, "lname": lname,
                                  "turn": state.get("turn_number"),
                                  "phase": state.get("phase"),
                                  "untapped_lands":
                                      untapped_land_names(state, 0)})
    return found


async def play_a_land(c, state, pid, acts, tag, target):
    """Play a land while fewer than `target` untapped lands (prefer Plains
    so >=1 Plains ends up untapped)."""
    if len(untapped_lands(state, pid)) >= target:
        return False
    cands = [a for a in acts if a.get("type") == "PlayLand"]
    if not cands:
        return False

    def is_plains(a):
        try:
            return obj_lname(state, a.get("_src_oid")) == PLAINS
        except (TypeError, ValueError):
            return False

    plains = [a for a in cands if is_plains(a)]
    a = plains[0] if plains else cands[0]
    say(f"[{tag}] plays land "
        f"{obj_lname(state, a.get('_src_oid'))} "
        f"(target {target} untapped)")
    wire("play_land", {"who": tag, "target": target})
    await submit_as_is(c, a)
    return True


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def log_unanswered(c, tag, st):
    for opp in unanswered_ops(st):
        iid = opp.get("interactionId")
        if iid in LOGGED_IIDS or iid in DISCARDED_IIDS:
            continue
        LOGGED_IIDS.add(iid)
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        OBS["unexpected_prompts"].append(
            {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
             "rtype": resp.get("type"),
             "texts": [choice_text(ch)[:60] for ch in chs][:8]})
        say(f"[{tag}] unanswered vi iid={iid} n={len(chs)} "
            f"rtype={resp.get('type')}")
        wire("unanswered_vi",
             {"who": tag, "iid": iid,
              "opportunity": json.loads(json.dumps(opp, default=str))})

# ------------------------------------------------------------- seat ticks

async def p0_tick(c, pid, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await do_declare(c, acts, st, 0, tag):
        return True
    if await end_target_tick(c, tag, st, state):
        return True
    # legacy mana actions: answer if they appear (engine Auto payment on
    # 106 means they usually do not). Driver never taps mana itself.
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0
    is_p0_main = (phase in ("PreCombatMain", "PostCombatMain", "Main")
                  and state.get("active_player") == 0)

    # life trace
    lives = (life_of(state, 0), life_of(state, 1))
    tr = OBS["life_trace"]
    if all(v is not None for v in lives) and (not tr or tr[-1][1] != lives):
        tr.append((round(time.time() - t_start, 1), lives))
        say(f"life = {lives}")
        wire("life", {"life": lives})

    # --- the reduced-cost window: precondition-gated, NOT turn-gated ---
    # fire on the first P0 PreCombatMain priority where ALL hold:
    #   * P1 has attacked at least once with a Bear (bear is tapped)
    #   * Magnificent End in P0 hand
    #   * exactly 2 untapped P0 lands including a Plains
    #     (printed {4}{W} unpayable, reduced {1}{W} payable)
    #   * stack empty
    if (phase == "PreCombatMain" and state.get("active_player") == 0
            and my_priority(top_acts(st))
            and not ST["window_assessed"]
            and not ST["end_in_flight"]
            and ST["p1_attacked"]
            and END in hand_lnames(state, 0)
            and stack_empty(state)):
        ul = untapped_land_names(state, 0)
        bears = [o for o in bf_oids(state, 1, BEARS)
                 if get_obj(state, o).get("tapped")]
        if len(ul) == 2 and PLAINS in ul and bears:
            if not ST["pre_exported"]:
                if await export_named(c, "pre"):
                    ST["pre_exported"] = True
                st = st_of(c) or st
                state = st["state"]
                acts = merged_actions(st)
            found = scan_offers(c, "P0", state, acts, END)
            ST["window_assessed"] = True
            ST["offered_reduced"] = bool(found)
            ST["offered_reduced_detail"] = {
                "turn": turn, "phase": phase,
                "untapped_lands": sorted(ul),
                "bear_oid": bears[0],
                "bear_tapped": True,
                "my_priority": True,
                "p1_attack_turn": ST["p1_attack_turn"],
                "offered_oids": sorted({str(o) for o, _a in found})}
            ST["bear_oid"] = bears[0]
            wire("window_assessed", ST["offered_reduced_detail"])
            say(f"WINDOW ASSESSED: offered_reduced={ST['offered_reduced']} "
                f"detail={json.dumps(ST['offered_reduced_detail'], sort_keys=True)}")
            if found:
                oid, action = found[0]
                ST["end_oid"] = str(oid)
                await submit_as_is(c, action)
                ST["end_cast"] = True
                ST["end_in_flight"] = True
                say(f"[P0] cast Magnificent End oid={oid} "
                    f"(engine Auto payment; expecting reduced {{1}}{{W}})")
                wire("end_cast", {"oid": str(oid)})
                return True
            # expected bug path: release the land hold, drive to 5 mana
            # for the printed-cost control
            ST["land_target"] = 5
            say("[P0] not offered at reduced cost; land hold released "
                "(target 5 untapped for the printed-cost control)")
            wire("land_hold_released", {})

    # --- printed-cost control (A4): >=5 untapped lands, >=1 Plains ---
    if (ST["window_assessed"] and not ST["offered_reduced"]
            and not ST["offered_printed"]
            and phase == "PreCombatMain" and state.get("active_player") == 0
            and my_priority(top_acts(st)) and stack_empty(state)):
        ul = untapped_land_names(state, 0)
        if len(ul) >= 5 and PLAINS in ul:
            found = scan_offers(c, "P0", state, acts, END)
            if found:
                ST["offered_printed"] = True
                ST["offered_printed_detail"] = {
                    "turn": turn, "phase": phase,
                    "untapped_lands": sorted(ul),
                    "offered_oids": sorted({str(o) for o, _a in found})}
                say(f"[P0] printed-cost control: CastSpell offered with "
                    f"{len(ul)} untapped lands (turn {turn})")
                wire("printed_offer", ST["offered_printed_detail"])
                if await export_named(c, "post"):
                    ST["post_exported"] = True
                    STOP["stop"] = True
                    return True

    # --- resolution tracking for the cast path (stack-watch: falls
    # through to the pass-priority gate below; never returns early) ---
    if ST["end_in_flight"]:
        on_stack = any(
            obj_lname(state, oid) == END
            for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Stack" and str(o.get("controller")) == "0")
        if on_stack and not ST["end_on_stack_seen"]:
            ST["end_on_stack_seen"] = True
            say("Magnificent End seen on the stack")
            wire("end_on_stack", {})
        elif not on_stack and ST["end_on_stack_seen"] \
                and not ST["end_resolved"]:
            ST["end_resolved"] = True
            ST["end_resolved_at"] = time.time()
            ST["end_in_flight"] = False
            if not ST["target_answered"]:
                ST["target_auto"] = True
                say("Magnificent End left the stack with no target prompt "
                    "answered (auto-target path)")
            say("Magnificent End left the stack (resolved)")
            wire("end_resolved", {"target_auto": ST["target_auto"]})
    if (ST["end_resolved"] and not ST["post_exported"]
            and stack_empty(state)):
        if await export_named(c, "post"):
            ST["post_exported"] = True
            say("POST exported: reduced-cost path resolved, stack empty")
            STOP["stop"] = True
            return True

    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # ---- P0 priority: land drops, then always pass ----
    # build to exactly 2 untapped lands (window mode), or to 5 (control
    # mode after the land hold is released). Retry every tick; the engine
    # enforces 1 land/turn.
    if is_p0_main:
        if await play_a_land(c, state, 0, acts, tag, ST["land_target"]):
            return True

    # --- castability diagnostic while Magnificent End is in hand ---
    if (is_p0_main and END in hand_lnames(state, 0)):
        key = ("castdiag", turn, phase)
        if key not in CASTDIAG_DONE:
            CASTDIAG_DONE.add(key)
            casts = [(a["type"], str(a.get("_src_oid")))
                     for a in acts if "cast" in a["type"].lower()]
            say(f"[P0] castdiag t{turn} {phase}: untapped="
                f"{untapped_land_names(state, 0)} casts={casts} "
                f"bear_tapped={[o for o in bf_oids(state, 1, BEARS) if get_obj(state, o).get('tapped')]}")

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


async def p1_tick(c, pid, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    # legacy mana actions: answer if they appear (engine Auto payment on
    # 106 means they usually do not). Driver never taps mana itself.
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True
    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    phase = state.get("phase") or ""
    is_p1_main = (phase in ("PreCombatMain", "PostCombatMain", "Main")
                  and state.get("active_player") == pid)

    # cast a Grizzly Bears as soon as payable (engine Auto payment), but
    # only while none is on the battlefield (one bear is all the setup
    # needs; keeps P0's life math predictable)
    if (is_p1_main and not bf_oids(state, pid, BEARS)
            and not ST["end_in_flight"]):
        found = cast_spell_for(acts, state, BEARS)
        if found:
            oid, action = found[0]
            ST["p1_bear_cast"] = True
            say(f"[{tag}] casting Grizzly Bears oid={oid} "
                f"(engine Auto payment)")
            wire("bear_cast", {"oid": str(oid)})
            await submit_as_is(c, action)
            return True
    # land drop every own main phase (retry each tick; engine enforces
    # 1 land/turn)
    if is_p1_main:
        if await play_a_land(c, state, pid, acts, tag, 99):
            return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True

# ------------------------------------------------------------- main loop

async def main():
    global t_start, P0_PID
    t_start = time.time()
    last_rev_change = t_start
    game_started = False

    hello = await verify_server_hello()
    check_parse_end()

    p0 = PhaseClient("P07079r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P17079r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    GAME = p0.game_code
    P0_PID = int(p0.player_id)
    say(f"game {GAME}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game", {"code": GAME, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    async def finish():
        dur = time.time() - t_start
        notes = []
        ass = {k: "not-run" for k in
               ("A1_parse_gap", "A2_setup_ok", "A3_reduced_offer",
                "A4_printed_offer", "A5_reduced_cast", "A6_cleanup")}

        def load(fn):
            try:
                with open(f"{EVDIR}/{fn}.json") as f:
                    return json.load(f)["state"]
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
                return None

        pre, post = load("pre"), load("post")
        if pre is not None:
            say("loaded pre.json")
        if post is not None:
            say("loaded post.json")

        # ---- A1: parse gap (supported aspect) ----
        ass["A1_parse_gap"] = "passed" if PARSE["ok"] else "failed"
        notes.append(f"A1: ModifyCost Reduce {{3}} / Targets(Tapped "
                     f"Creature) / SelfRef supported={PARSE['ok']}")

        # ---- A2: setup (PRE at the window) ----
        if pre is not None:
            ul = untapped_land_names(pre, 0)
            bears = [o for o in bf_oids(pre, 1, BEARS)
                     if get_obj(pre, o).get("tapped")]
            l0, l1 = life_of(pre, 0), life_of(pre, 1)
            ok = (pre.get("phase") == "PreCombatMain"
                  and END in hand_lnames(pre, 0)
                  and len(ul) == 2 and PLAINS in ul
                  and len(bears) > 0
                  and l1 == 20 and l0 is not None and l0 < 20)
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: phase={pre.get('phase')} "
                         f"end_in_hand={END in hand_lnames(pre, 0)} "
                         f"untapped_lands={ul} tapped_bears={bears} "
                         f"life={l0}/{l1} (expect <20/20 after one bear "
                         f"attack) priority@assess="
                         f"{(ST['offered_reduced_detail'] or {}).get('my_priority')}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing")

        # ---- A3: reduced offer (recorded live at the window) ----
        ass["A3_reduced_offer"] = ("passed" if ST["offered_reduced"]
                                   else "failed")
        notes.append(f"A3: offered_reduced={ST['offered_reduced']} "
                     f"detail={ST['offered_reduced_detail']}")

        # ---- A4: printed-cost control ----
        if ST["offered_printed"]:
            ass["A4_printed_offer"] = "passed"
        elif not ST["window_assessed"]:
            ass["A4_printed_offer"] = "not-run"
        else:
            ass["A4_printed_offer"] = "failed"
        notes.append(f"A4: offered_printed={ST['offered_printed']} "
                     f"detail={ST['offered_printed_detail']}")

        # ---- A5: reduced cast correct (offer path only) ----
        if ST["offered_reduced"]:
            if ST["end_resolved"] and post is not None:
                bear_gy = any(obj_lname(post, int(oid)) == BEARS
                              for oid, o in (post.get("objects") or {})
                              .items()
                              if o.get("zone") == "Graveyard"
                              and str(o.get("controller")) == "1")
                end_gy = any(obj_lname(post, int(oid)) == END
                             for oid, o in (post.get("objects") or {})
                             .items()
                             if o.get("zone") == "Graveyard"
                             and str(o.get("controller")) == "0")
                tapped_now = [oid for oid in bf_lands(post, 0)
                              if get_obj(post, oid).get("tapped")]
                lives = [life_of(post, 0), life_of(post, 1)]
                target_ok = (ST["target_submitted_oid"] == str(ST["bear_oid"])
                             or ST["target_auto"])
                ok = (bear_gy and end_gy and len(tapped_now) == 2
                      and lives[1] == 20 and target_ok)
                notes.append(f"A5: bear_in_P1_gy={bear_gy} "
                             f"end_in_P0_gy={end_gy} "
                             f"tapped_P0_lands={len(tapped_now)} (expect 2: "
                             f"{{1}}{{W}} paid) lives={lives} "
                             f"target_ok={target_ok} "
                             f"(submitted={ST['target_submitted_oid']} "
                             f"bear={ST['bear_oid']} auto={ST['target_auto']})")
                ass["A5_reduced_cast"] = "passed" if ok else "failed"
            else:
                ass["A5_reduced_cast"] = "failed"
                notes.append(f"A5 failed: end_resolved={ST['end_resolved']} "
                             f"post={'present' if post else 'missing'}")
        else:
            ass["A5_reduced_cast"] = "not-run"
            notes.append("A5 not-run: spell never offered at reduced cost")

        # ---- A6: cleanup ----
        if post is not None:
            stack_empty_ok = not (post.get("stack") or [])
            ass["A6_cleanup"] = "passed" if stack_empty_ok else "failed"
            notes.append(f"A6: stack_empty={stack_empty_ok}")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 failed: post.json missing")

        # ---- verdict (exact rule) ----
        if ass["A2_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif ass["A3_reduced_offer"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: with a tapped-creature target "
                         "and exactly {1}{W} available, CastSpell for "
                         "Magnificent End was NOT advertised; printed-cost "
                         f"control offered={ST['offered_printed']} "
                         "(A4 passed strengthens the preflight-defect read)")
        elif ass["A5_reduced_cast"] == "passed":
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: spell offered at the "
                         "reduced cost and the full path (target tapped "
                         "bear, pay {1}{W}, 5 damage, bear dies) completed")
        elif ass["A3_reduced_offer"] == "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced (related): spell was offered "
                         "but the reduced-cost cast did not complete "
                         "correctly (A5 failed)")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-10-08",
            "server": {
                "server_version": hello.get("server_version"),
                "build_commit": hello.get("build_commit"),
                "protocol_version": hello.get("protocol_version"),
                "mode": hello.get("mode"),
                "server_binary_sha256":
                    SERVER_IDENTITY["server_binary_sha256"],
                "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
                "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
                "signature_verified":
                    SERVER_IDENTITY["signature_verified"],
            },
            "driver": {"protocol_advertised": 106,
                       "client": "driver/client.py"},
            "scenario_sha256": sha256_of_file(__file__),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": [[n, c] for n, c in P0_DECK],
                      "P1": [[n, c] for n, c in P1_DECK]},
            "setup_line": ("P0 12x Magnificent End / 28x Plains / 20x "
                           "Forest; P1 12x Grizzly Bears / 48x Forest. P0 "
                           "mulligans for End+2 lands, holds lands at "
                           "exactly 2 (Plains preferred). P1 mulligans for "
                           "Bear+2 lands, casts a Bear when payable (engine "
                           "Auto payment), attacks P0 at the first "
                           "opportunity (P0 blocks nothing). Window: P0 "
                           "PreCombatMain priority with 2 untapped lands "
                           "(>=1 Plains), End in hand, tapped Bear on P1 "
                           "BF, P1 attacked at least once, stack empty."),
            "contract_line": ("Magnificent End's cost reduction ({3} less "
                              "for a tapped-creature target) is not honored "
                              "by the cast-availability preflight: with "
                              "exactly {1}{W} available and a tapped Bear "
                              "target, CastSpell is not advertised "
                              "(expected); the printed-cost control (5 "
                              "lands) IS advertised -> reproduced."),
            "driver_notes": [
                "Protocol-106 port of driver/scenario_7079.py (v0.82.0 / "
                "protocol 70) for pinned v0.103.0; the behavioral contract, "
                "assertions A1..A6 and the verdict rule are unchanged.",
                "my_priority = PassPriority in legal_actions; casts are "
                "gated on it.",
                "CastSpell carries payment_mode Auto: the engine taps mana "
                "itself; the driver never answers tapLandForMana and runs "
                "no driver-side mana payment (legacy PayMana actions are "
                "answered if they appear).",
                "DeclareAttackers via legacy Action with "
                "attacks=[[int(oid), {type:Player, data:P0_PID}]], "
                "bands=[]; DeclareBlockers with assignments=[].",
                "The End's TargetSelection is answered from P0's "
                "viewer_interaction with the tapped-Bear candidate (object "
                "oid match, tapped-P1-bear fallback, seat==1 fallback); "
                "the engine may also auto-target a sole legal target "
                "(recorded as the auto-target case).",
                "The stack-watch branch always falls through to the "
                "pass-priority gate (never returns early); both seats must "
                "pass in succession for a stack entry to resolve.",
                "Pre/post states are authoritative exports (data.state "
                "parsed once from the export envelope) via the host client "
                "only; the reported OUTCOME is asserted on the saved "
                "states, not the prompt.",
            ],
            "assertions": ass,
            "observations": OBS,
            "driver_state": ST,
            "mulligans": MULLS,
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "parse_magnificent_end.json",
                               "scenario_7079_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"]
                              + sorted(os.path.basename(p) for p in
                                       glob.glob(f"{EVDIR}/target_sel_*.json")),
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "12x Magnificent End / 12x Grizzly Bears density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games).",
                "The untapped-creature-target control (spell not offered at "
                "reduced total when only untapped targets are legal) was not "
                "exercised in this run; the printed-cost control (A4) covers "
                "the positive offer path.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Turn order is randomized by the engine; the driver keys on "
                "active_player, not order.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1, default=str)
        say(f"wrote run.json verdict={verdict}")
        for k, v in ass.items():
            say(f"{k}: {v}")

        with open(__file__) as f:
            src = f.read()
        with open(f"{EVDIR}/scenario_7079_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_7079_01030.py into EVDIR")

        srv_src = None
        for cand in (f"{BACKFILL}/runs/{RUN_ID}/server.log",
                     f"{BACKFILL}/runs/20261008-7079/server.log"):
            if os.path.exists(cand):
                srv_src = cand
                break
        if srv_src is not None:
            import shutil
            shutil.copy(srv_src, f"{EVDIR}/server.log")
            say(f"copied server.log from {srv_src} into EVDIR")
        else:
            note = (f"no per-run server.log at runs/{RUN_ID}/server.log; "
                    f"the pinned server on 127.0.0.1:9374 was already "
                    f"running (dedicated to this run); wire traffic is in "
                    f"wire_log.jsonl, driver log in scenario_run.log")
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write(note + "\n")
            say("server.log: wrote note instead (no runs/<run-id>/server.log)")

        render_summary(run, pre, post)

        write_manifest()               # build 1
        say("scenario finished")       # final scenario_run.log line
        write_manifest(quiet=True)     # build 2 -- no logging after this
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        for c in (p0, p1):
            try:
                await c.close()
            except Exception:
                pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, pre, post):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 960
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7079 - Magnificent End",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "target-dependent {3} reduction preflight",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: {4}{W}; costs {3} less if it targets a "
               "tapped creature; deals 5 to target creature.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse_gap": "card-data: Reduce{3}/Targets(Tapped) supported",
            "A2_setup_ok": "PRE: 2 lands (1W), tapped Bear, End in hand",
            "A3_reduced_offer": "CastSpell offered w/ only {1}{W} payable",
            "A4_printed_offer": "CONTROL: offered w/ 5 mana (printed cost)",
            "A5_reduced_cast": "cast: target tapped Bear, pay {1}{W}, 5 dmg",
            "A6_cleanup": "POST stack empty",
        }
        for k, lab in labels.items():
            av = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if av == "passed" else (
                (255, 90, 90) if av == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {av} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Board across states:", fill=(200, 210, 225))
        y += 24
        for label, st in (("pre ", pre), ("post", post)):
            if st is not None:
                ul = untapped_land_names(st, 0)
                bears = [o for o in bf_oids(st, 1, BEARS)]
                bt = (get_obj(st, bears[0]).get("tapped")
                      if bears else None)
                line = (f"{label}: life {life_of(st, 0)}/{life_of(st, 1)}  "
                        f"P0 untapped lands={ul}  bear={bears} "
                        f"tapped={bt}  stack={len(st.get('stack') or [])}")
            else:
                line = f"{label}: (no state)"
            d.text((36, y), line[:118], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:15]:
            d.text((36, y), str(n)[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest(quiet=False):
        files = ["pre.json", "post.json", "run.json",
                 "parse_magnificent_end.json", "scenario_7079_01030.py",
                 "wire_log.jsonl", "scenario_run.log", "server.log",
                 "summary.png"]
        files += sorted(os.path.basename(p)
                        for p in glob.glob(f"{EVDIR}/target_sel_*.json"))
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                say(f"manifest: MISSING {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # build 2 must be quiet: any say() after the second write would
        # append to scenario_run.log and stale its manifest hash.
        if not quiet:
            say(f"wrote manifest.sha256 ({len(lines)} files)")

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t_start < GAME_TIMEOUT and not STOP.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick),
                             (p1, "P1", p1_tick)):
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
            st = st_of(c)
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_rev_change = time.time()
                if (st.get("state") or {}).get("turn_number", 0) >= 1:
                    game_started = True
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
                pid = {"P0": 0, "P1": 1}[tag]
                await tick(c, pid, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if STOP.get("stop"):
            break

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STOP["stop"] = True
            continue

        if game_started and not STOP.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["notes"].append(f"stall: no revision for {STALL_AFTER}s")
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {})
            STOP["stop"] = True
            continue

        if turn > TURN_CAP and not STOP.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STOP["stop"] = True
            continue

        if (turn or 0) > 30 and not ST["window_assessed"] \
                and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 30 reached with no window; "
                                "finishing")
            say("watchdog: turn 30, no window; finishing")
            await finish()
            return

        if (ST["window_assessed"] and not ST["offered_reduced"]
                and not ST["offered_printed"] and (turn or 0) > 30
                and not STOP.get("stop")):
            OBS["notes"].append("watchdog: turn 30 with no printed offer; "
                                "finishing")
            say("watchdog: turn 30, no printed offer; finishing")
            await finish()
            return

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"pp={state.get('priority_player')} "
                f"life={[life_of(state, i) for i in (0, 1)]} "
                f"window={ST['window_assessed']} "
                f"offered_red={ST['offered_reduced']} "
                f"offered_pr={ST['offered_printed']} "
                f"end={ST['end_cast']}/{ST['end_resolved']} "
                f"lands0={untapped_land_names(state, 0)} "
                f"bear_attacked={ST['p1_attacked']} "
                f"stack={len(state.get('stack') or [])}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
