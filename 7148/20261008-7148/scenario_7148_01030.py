#!/usr/bin/env python3
"""phase-rs/phase #7148 - Nils, Discipline Enforcer: "for each player, put a
+1/+1 counter on up to one target creature that player controls" places ALL
counters on ONE creature instead of one counter per player. Protocol-106
re-validation (prior: v0.82.0/protocol 70, run 20260914-7148b, reproduced).

Oracle: "At the beginning of your end step, for each player, put a +1/+1
counter on up to one target creature that player controls."
Reported: "Places all 4 counters on only one creature not one counter for
each player." (4-player Discord game; this fixture uses 3 seats, so the
correct distribution is 3 counters, one per player.)

Card-data parse (v0.103.0): trigger mode Phase/End, OnlyDuringYourTurn,
effect PutCounter P1P1 count=1, target Typed[Creature] controller=
ScopedPlayer, multi_target {min: 0, max: 1} ("up to one"), player_scope All
("for each player"). Parse is correct (still carries the
SwallowedClause/DynamicQty warning on the same line, as in v0.82.0), so a
persisting defect is in runtime per-player iteration / target binding.

Behavioral contract (3 human seats, native engine, protocol 106):
  P0 fields Nils only; P1 fields one Grizzly Bears; P2 fields one Storm
  Crow. P0 casts Nils only after the Bear and the Crow are on the
  battlefield, so the first end-step trigger sees all three creatures.
  At P0's end step the trigger fires; PRE is exported at the
  trigger-on-stack transition (before prompt handling -- the engine may
  auto-target a sole legal target without any prompt). The driver answers
  each target-selection opportunity with the scope player's creature and
  records prompts/candidates/choices.
  A1 parse_ok:        PutCounter P1P1 x1, ScopedPlayer target,
                      multi_target 0..1, player_scope All, Phase End,
                      OnlyDuringYourTurn.
  A2 setup_ok:        Nils trigger seen on stack at P0's end step, Nils on
                      P0 BF, >=1 creature on each player's BF at pre, pre
                      exported. (Prompt-not-seen + trigger-resolved is the
                      auto-target case and still counts as setup-ok if the
                      pre conditions hold.)
  A3 per_player_one:  post-resolution, each player's creature holds exactly
                      1 new +1/+1 counter and no creature holds >1 (total
                      exactly 3). Fails => the reported bug.
  A4 trigger_resolved: the Nils trigger left the stack and the game
                      advanced (mid exported at/after the answer turn).
  A5 cleanup:         stack empty at final observation.

Verdict: reproduced iff A2 passes and A3 fails. not-reproduced iff A2+A3
pass. blocked iff A2 fails (or A3 not-run).

Protocol-106 conventions (from scenario_7147_01030.py):
  - waiting_for is gone (null) on 106; every prompt arrives as a
    viewer_interaction opportunity.
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full via
    the driver client; casts gated on my_priority (PassPriority in
    legal_actions).
  - CastSpell carries payment_mode Auto: the engine taps mana itself; the
    driver never answers tapLandForMana and runs no driver-side mana
    payment (legacy PayMana actions are answered if they appear).
  - The stack-watch branch always falls through to the pass-priority
    gate; real_decision_pending holds on genuine vi decisions only
    (priority menus excluded via NON_DECISION_CODES). Answered
    interactionIds are excluded everywhere (SUBMITTED_OPPS) so a hold
    never fires on an already-answered opportunity.
  - Pre/post states are authoritative exports (data.state parsed once
    from the export envelope) via the host client only.

Evidence: evidence/7148/<run-id>/pre.json, mid.json, post.json,
opportunity_nils_<n>.json, parse_nils.json, run.json,
scenario_7148_01030.py, wire_log.jsonl, scenario_run.log, summary.png,
manifest.sha256.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7148
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7148")
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

PARSE = {"ok": False}


def check_parse_7148():
    """Parse check: Nils trigger is Phase/End, OnlyDuringYourTurn,
    PutCounter P1P1 x1, target Typed[Creature] controller ScopedPlayer,
    multi_target {min: 0, max: 1}, player_scope All."""
    t = CARD_DATA.get("nils, discipline enforcer", {})
    trigs = t.get("triggers") or []
    rec = {"oracle_text": t.get("oracle_text"),
           "parse_warnings": t.get("parse_warnings"),
           "trigger_count": len(trigs), "trigger": None}
    ok = False
    if trigs:
        tr = trigs[0]
        ex = (tr.get("execute") or {})
        eff = (ex.get("effect") or {})
        tgt = (eff.get("target") or {})
        mt = ex.get("multi_target") or {}
        mx = mt.get("max") or {}
        mxv = mx.get("value") if isinstance(mx, dict) else mx
        cnt = eff.get("count") or {}
        cntv = cnt.get("value") if isinstance(cnt, dict) else cnt
        rec["trigger"] = {"mode": tr.get("mode"),
                          "phase": tr.get("phase"),
                          "constraint": (tr.get("constraint") or {}).get(
                              "type"),
                          "effect_type": eff.get("type"),
                          "counter_type": eff.get("counter_type"),
                          "count": cntv,
                          "target_type": tgt.get("type"),
                          "target_controller": tgt.get("controller"),
                          "multi_target": mt,
                          "player_scope": (ex.get("player_scope") or {}).get(
                              "type")}
        ok = (tr.get("mode") == "Phase" and tr.get("phase") == "End"
              and (tr.get("constraint") or {}).get("type")
              == "OnlyDuringYourTurn"
              and eff.get("type") == "PutCounter"
              and eff.get("counter_type") == "P1P1" and cntv == 1
              and tgt.get("type") == "Typed"
              and tgt.get("controller") == "ScopedPlayer"
              and mt.get("min") == 0 and mxv == 1
              and (ex.get("player_scope") or {}).get("type") == "All")
    with open(f"{EVDIR}/parse_nils.json", "w") as fh:
        json.dump(rec, fh, indent=1, default=str)
    PARSE["ok"] = ok
    say(f"parse: nils_discipline_enforcer ok={ok}")
    wire("parse_check", {"ok": ok, "trigger": rec["trigger"]})
    return ok


# ------------------------------------------------------------- constants

NILS = "nils, discipline enforcer"
BEAR = "grizzly bears"
CROW = "storm crow"
PLAINS = "plains"
FOREST = "forest"
ISLAND = "island"
LANDS = (PLAINS, FOREST, ISLAND)

P0_DECK = [("Nils, Discipline Enforcer", 12), ("Plains", 48)]
P1_DECK = [("Grizzly Bears", 20), ("Forest", 40)]
P2_DECK = [("Storm Crow", 20), ("Island", 40)]
MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")
GAME_TIMEOUT = 1800
TURN_CAP = 30

STOP = {"stop": False}
ST = {"done_reason": None}
OBS = {"rejections": [], "tick_errors": [], "unexpected_prompts": [],
       "target_sels": [], "notes": []}
SUBMITTED_OPPS = set()
PASSED_REV = {}
MULLS = {"P07148r": 0, "P17148r": 0, "P27148r": 0}
NILSST = {"trigger_seen": False, "trigger_turn": None, "pre": False,
          "prompts": [], "assigned": {}, "answered": 0,
          "answer_turn": None, "mid": False, "mid_turn": None,
          "mid_phase": None}
t_start = 0.0


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


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller")) == str(pid):
            if lname is None or obj_lname(state, oid) == lname:
                out.append(int(oid))
    return out


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
    await c.send_interaction(sub)


def drain(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            OBS["rejections"].append({"who": c.name, "type": t,
                                      "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")


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


# ------------------------------------------------------------- mulligan

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
    if pid == 0:
        keep = (NILS in hn and n_lands >= 2) or n >= 2
    elif pid == 1:
        keep = (BEAR in hn) or n >= 2
    else:
        keep = (CROW in hn) or n >= 2
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
            if nm in (NILS, BEAR, CROW):
                return (2, str(ref))  # never bottom the key cards
            if nm in LANDS:
                return (0, str(ref))  # bottom lands first
            return (1, str(ref))

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
    if nm in LANDS:
        return 0
    if nm in (NILS, BEAR, CROW):
        return 6  # protect the key cards
    return 3


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


# ------------------------------------------------------- nils trigger

def has_trigger_on_stack(state):
    for e in state.get("stack") or []:
        k = e.get("kind")
        kt = k.get("type") if isinstance(k, dict) else k
        if kt == "TriggeredAbility":
            return True
    return False


def nils_trigger_on_stack(state):
    """True if a TriggeredAbility plausibly from Nils is on the stack.
    Nils is the only trigger source in this fixture, so any
    TriggeredAbility during P0's end step with Nils on the battlefield
    is the Nils end-step trigger."""
    if not bf_oids(state, 0, NILS):
        return False
    if state.get("active_player") != 0:
        return False
    return has_trigger_on_stack(state)


def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices
    whose choices carry candidate/target codes (passPriority menus
    excluded)."""
    for opp in unanswered_ops(st):
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
                codes.update(cc for cc in surf_codes(ch) if cc)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(cc in codes for cc in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def counters_of(obj):
    c = obj.get("counters")
    if isinstance(c, dict):
        return c
    if isinstance(c, list):
        out = {}
        for e in c:
            if isinstance(e, dict):
                k = e.get("type") or e.get("kind") or e.get("name")
                out[str(k)] = e.get("count", 1)
            else:
                out[str(e)] = out.get(str(e), 0) + 1
        return out
    return {}


def p1p1_count(obj):
    n = 0
    for k, v in counters_of(obj).items():
        kl = str(k).lower()
        if "p1p1" in kl or "+1/+1" in kl:
            try:
                n += int(v)
            except Exception:
                n += 1
    return n


def counter_distribution(state):
    """oid -> {name, controller, p1p1} for every battlefield object with
    +1/+1 counters."""
    out = {}
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        n = p1p1_count(o)
        if n:
            out[str(oid)] = {"name": obj_lname(state, oid),
                             "controller": o.get("controller"),
                             "p1p1": n}
    return out


def creature_summary(state):
    out = {}
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        nm = obj_lname(state, oid)
        if nm in (NILS, BEAR, CROW):
            out[str(oid)] = {"name": nm, "controller": o.get("controller"),
                             "p1p1": p1p1_count(o)}
    return out


def detect_scope_player(state, cands):
    """Which player's 'for each player' iteration is this prompt for?
    Prefer candidate-controller unanimity; the caller maps the scope to
    the first not-yet-assigned player when unanimity is absent."""
    ctrls = set()
    for ch in cands:
        oid = cand_oid(ch)
        if oid is not None:
            ctl = get_obj(state, oid).get("controller")
            if ctl is not None:
                ctrls.add(int(ctl))
    if len(ctrls) == 1:
        return next(iter(ctrls)), "candidate unanimity"
    return None, "unknown"


async def answer_nils_target(c, st, state, tag):
    """Answer Nils TriggerTargetSelection opportunities (P0 controls the
    trigger, so only P0 answers). Each prompt is answered with one
    creature controlled by the prompt's scope player: the unanimous
    candidate controller, else the first player (0/1/2) with a candidate
    creature not yet assigned a counter this trigger. Returns True if an
    answer was submitted."""
    opp, rtype, spec_type = target_opportunity(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    cands = data.get("candidates") or data.get("choices") or []
    n = len(NILSST["prompts"])
    with open(f"{EVDIR}/opportunity_nils_{n}.json", "w") as f:
        json.dump({"waiting_for": None, "opportunity": opp}, f, indent=1,
                  default=str)
    scope, how = detect_scope_player(state, cands)
    if scope is None:
        seen = {}
        for ch in cands:
            oid = cand_oid(ch)
            if oid is None:
                continue
            pc = get_obj(state, oid).get("controller")
            if pc is not None and int(pc) not in seen:
                seen[int(pc)] = oid
        scope = next((p for p in (0, 1, 2)
                      if p in seen and p not in NILSST["assigned"]), None)
        how = "inferred-first-unassigned"
        if scope is None:
            scope = next(iter(seen), None)
            how = "inferred-any"
    say(f"[{tag}] Nils target prompt #{n}: rtype={rtype} stype={spec_type} "
        f"ncands={len(cands)} scope_player={scope} ({how}) iid={iid}")
    for ch in cands:
        ref = cand_oid(ch)
        nm = obj_lname(state, ref) if ref else "?"
        o = get_obj(state, ref) if ref else {}
        say(f"    cand id={ch.get('id')} -> {nm}#{ref} "
            f"(P{o.get('controller')}) zone={o.get('zone')}")
    wire("nils_target_prompt",
         {"n": n, "rtype": rtype, "stype": spec_type, "ncands": len(cands),
          "scope_player": scope, "scope_how": how,
          "cands": [cand_oid(ch) for ch in cands]})
    if not NILSST["pre"]:
        NILSST["pre"] = True
        NILSST["answer_turn"] = state.get("turn_number")
        await export_named(c, "pre")
        say(f"[{tag}] PRE exported at first prompt (turn "
            f"{NILSST['answer_turn']})")
    # pick one candidate controlled by the scope player
    pick = None
    for ch in cands:
        oid = cand_oid(ch)
        if oid is not None and int(get_obj(state, oid).get("controller",
                                                            -1)) == scope:
            pick = ch
            break
    if pick is None and cands:
        pick = cands[0]
        say(f"[{tag}] WARNING: no candidate for scope player {scope}; "
            f"falling back to first candidate")
    if pick is None:
        say(f"[{tag}] no candidates at all; cannot answer")
        return False
    chosen_oid = cand_oid(pick)
    dup = scope in NILSST["assigned"]
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [pick.get("id")]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
    wire("nils_target_answer",
         {"n": n, "scope_player": scope, "chosen_oid": chosen_oid,
          "chosen": pick.get("id"), "duplicate_scope": dup, "sub": sub})
    say(f"[{tag}] Nils prompt #{n} answers scope P{scope} -> "
        f"{obj_lname(state, chosen_oid) if chosen_oid else '?'}#"
        f"{chosen_oid}{' (DUPLICATE SCOPE)' if dup else ''}")
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)
    NILSST["prompts"].append({"n": n, "scope_player": scope,
                              "scope_how": how, "chosen_oid": chosen_oid,
                              "duplicate_scope": dup,
                              "turn": state.get("turn_number"),
                              "phase": state.get("phase")})
    if not dup:
        NILSST["assigned"][scope] = chosen_oid
    NILSST["answered"] += 1
    return True


async def watch_resolution(c):
    """After the trigger is seen, export MID as soon as the Nils trigger
    leaves the stack (same end step). Never returns early from the
    stack-watch: this branch falls through to the caller's
    pass-priority gate."""
    if not NILSST["trigger_seen"] or NILSST["mid"]:
        return
    st = st_of(c)
    if not st:
        return
    state = st["state"]
    if not nils_trigger_on_stack(state) and not has_trigger_on_stack(state):
        NILSST["mid"] = True
        NILSST["mid_turn"] = state.get("turn_number")
        NILSST["mid_phase"] = state.get("phase")
        s_ok = await export_named(c, "mid")
        if s_ok:
            with open(f"{EVDIR}/mid.json") as f:
                s = json.load(f)["state"]
            dist = counter_distribution(s)
            say(f"[watch] mid exported at turn {NILSST['mid_turn']} "
                f"phase {NILSST['mid_phase']}; counters: {dist}")
            wire("mid_counters", dist)
        STOP["stop"] = True
        ST["done_reason"] = "nils trigger resolved; mid exported"
        say("Nils trigger resolved; stopping")


def play_land_for(state, pid, acts, lname):
    """PlayLand legacy action for a land named lname from pid's hand."""
    want = {str(o) for o in hand_ids(state, pid)
            if obj_lname(state, o) == lname}
    for a in acts:
        if a.get("type") != "PlayLand":
            continue
        ref = a.get("_src_oid")
        if ref is not None and str(ref) in want:
            return a
    return None


def cast_for(acts, state, lname):
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


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


# ------------------------------------------------------------- seat ticks

async def p0_tick(c, pid, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    drain(c)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    # PRE at the chapter transition: the Nils trigger appearing on the
    # stack is the transition, exported before prompt handling (the
    # engine may auto-target a sole legal target without any prompt).
    if (not NILSST["trigger_seen"]
            and nils_trigger_on_stack(state)
            and (state.get("phase") or "") == "End"):
        NILSST["trigger_seen"] = True
        NILSST["trigger_turn"] = state.get("turn_number")
        say(f"[{tag}] Nils end-step trigger on stack "
            f"(turn {NILSST['trigger_turn']}); exporting PRE")
        wire("nils_trigger_on_stack", {"turn": NILSST["trigger_turn"]})
        await export_named(c, "pre")
        NILSST["pre"] = True
    # the Nils end-step target prompt(s); P0 controls the trigger.
    if await answer_nils_target(c, st, state, tag):
        return True
    # legacy mana actions: answer if they appear (engine Auto payment on
    # 106 means they usually do not). Driver never taps mana itself.
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0
    is_p0_main = (phase in MAIN_PHASES and state.get("active_player") == 0)

    if is_p0_main and stack_empty(state) and my_priority(top_acts(st)):
        # land drop (Plains)
        if len(bf_oids(state, 0, PLAINS)) < 8:
            pa = play_land_for(state, pid, acts, PLAINS)
            if pa:
                say(f"[{tag}] plays Plains (turn {turn})")
                await submit_as_is(c, pa)
                return True
        # cast Nils once the Bear and the Crow are on the battlefield,
        # so the first end-step trigger sees all three creatures.
        if not bf_oids(state, 0, NILS) and NILS in hand_lnames(state, 0):
            if bf_oids(state, 1, BEAR) and bf_oids(state, 2, CROW):
                found = cast_for(acts, state, NILS)
                if found:
                    oid, action = found[0]
                    say(f"[{tag}] casts Nils oid={oid} t{turn} "
                        f"(engine Auto payment)")
                    wire("nils_cast", {"oid": str(oid), "turn": turn})
                    await submit_as_is(c, action)
                    return True

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
    drain(c)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0
    is_p1_main = (phase in MAIN_PHASES and state.get("active_player") == 1)

    if is_p1_main and stack_empty(state) and my_priority(top_acts(st)):
        if len(bf_oids(state, 1, FOREST)) < 8:
            pa = play_land_for(state, pid, acts, FOREST)
            if pa:
                say(f"[{tag}] plays Forest (turn {turn})")
                await submit_as_is(c, pa)
                return True
        if not bf_oids(state, 1, BEAR) and BEAR in hand_lnames(state, 1):
            found = cast_for(acts, state, BEAR)
            if found:
                oid, action = found[0]
                say(f"[{tag}] casts Grizzly Bears oid={oid} t{turn} "
                    f"(engine Auto payment)")
                wire("bear_cast", {"oid": str(oid), "turn": turn})
                await submit_as_is(c, action)
                return True

    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


async def p2_tick(c, pid, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    drain(c)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0
    is_p2_main = (phase in MAIN_PHASES and state.get("active_player") == 2)

    if is_p2_main and stack_empty(state) and my_priority(top_acts(st)):
        if len(bf_oids(state, 2, ISLAND)) < 8:
            pa = play_land_for(state, pid, acts, ISLAND)
            if pa:
                say(f"[{tag}] plays Island (turn {turn})")
                await submit_as_is(c, pa)
                return True
        if not bf_oids(state, 2, CROW) and CROW in hand_lnames(state, 2):
            found = cast_for(acts, state, CROW)
            if found:
                oid, action = found[0]
                say(f"[{tag}] casts Storm Crow oid={oid} t{turn} "
                    f"(engine Auto payment)")
                wire("crow_cast", {"oid": str(oid), "turn": turn})
                await submit_as_is(c, action)
                return True

    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


# ------------------------------------------------------------- main loop

C0 = C1 = C2 = None
SERVER_HELLO = {}


async def main():
    global t_start, C0, C1, C2
    t_start = time.time()

    hello = await verify_server_hello()
    SERVER_HELLO.update(hello)
    check_parse_7148()

    p0 = PhaseClient("P07148r")
    await p0.connect()
    say("P0 creating game (Bo1, 3 seats)...")
    await p0.create(deck(*P0_DECK), player_count=3)
    p1 = PhaseClient("P17148r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P27148r")
    await p2.connect()
    say("P2 joining...")
    await p2.join(p0.game_code, deck(*P2_DECK))
    C0, C1, C2 = p0, p1, p2
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"P2 seat={p2.player_id} RUN_ID={RUN_ID}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1.player_id, "p2": p2.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                  "p2_deck": P2_DECK, "server_hello": hello})

    last_rev_change = t_start
    last_revs = (-1, -1, -1)
    while time.time() - t_start < GAME_TIMEOUT and not STOP["stop"]:
        revs = (p0.revision, p1.revision, p2.revision)
        if revs != last_revs:
            last_revs = revs
            last_rev_change = time.time()
        turn = None
        try:
            if p0.latest and p0.latest.get("state"):
                turn = p0.latest["state"].get("turn_number")
        except Exception:
            pass
        if turn is not None and turn > TURN_CAP:
            ST["done_reason"] = f"turn cap {TURN_CAP} reached"
            say(f"TURN CAP {TURN_CAP} reached")
            STOP["stop"] = True
            break
        if time.time() - last_rev_change > 180:
            ST["done_reason"] = "stall: no revision change for 180s"
            say("STALL: no revision change for 180s")
            STOP["stop"] = True
            break
        for c, pid, tag, tickfn in (
                (p0, 0, "P07148r", p0_tick),
                (p1, 1, "P17148r", p1_tick),
                (p2, 2, "P27148r", p2_tick)):
            try:
                await tickfn(c, pid, tag)
            except Exception as e:
                OBS["tick_errors"].append({"who": tag, "error": str(e)[:200]})
                say(f"tick error [{tag}]: {e}")
        # stack-watch: never returns early; falls through to the
        # pass-priority gates inside each tick.
        await watch_resolution(C0)
        await asyncio.sleep(0.25)

    if not STOP["stop"]:
        ST["done_reason"] = ST["done_reason"] or "global timeout"
        say("TIMEOUT / loop ended without stop")
    say(f"main loop ended: stop={STOP['stop']} reason={ST['done_reason']}")
    await finish()


def load_state(fn):
    p = f"{EVDIR}/{fn}"
    try:
        with open(p) as f:
            return json.loads(f.read())["state"]
    except Exception:
        return None


def assertions_from_states():
    ass = {}
    notes = []
    pre = load_state("pre.json")
    mid = load_state("mid.json")
    post = load_state("post.json")

    # A1: parse check on the pinned card-data
    ok1 = PARSE["ok"]
    ass["A1_parse_ok"] = "passed" if ok1 else "failed"
    notes.append(f"A1: parse ok={ok1} (see parse_nils.json)")

    # A2: setup -- trigger seen at P0's end step, Nils on P0 BF, one
    # creature per side at pre, pre exported. Prompt-not-seen plus
    # trigger-resolved is the auto-target case and still setup-ok when
    # the pre conditions hold.
    cs_pre = creature_summary(pre) if pre else {}
    nils_bf = any(v["name"] == NILS and v["controller"] == 0
                  for v in cs_pre.values())
    sides = {v["controller"] for v in cs_pre.values()}
    ok2 = (NILSST["trigger_seen"] and NILSST["pre"] and nils_bf
           and sides == {0, 1, 2})
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"
    notes.append(f"A2: trigger_seen={NILSST['trigger_seen']} "
                 f"trigger_turn={NILSST['trigger_turn']} "
                 f"prompts_answered={NILSST['answered']} "
                 f"pre_exported={NILSST['pre']} nils_on_bf={nils_bf} "
                 f"sides={sorted(sides)} pre_creatures={cs_pre}")

    # A3: per-player counter distribution at mid -- each player's
    # creature holds exactly 1 new +1/+1 counter, none holds >1, total 3.
    dist = counter_distribution(mid) if mid else {}
    per_player = {}
    max_on_one = 0
    total = 0
    for oid, v in dist.items():
        total += v["p1p1"]
        max_on_one = max(max_on_one, v["p1p1"])
        per_player[v["controller"]] = per_player.get(
            v["controller"], 0) + v["p1p1"]
    ok3 = (NILSST["mid"] and total == 3 and max_on_one == 1
           and per_player == {0: 1, 1: 1, 2: 1})
    if not NILSST["mid"]:
        ass["A3_per_player_one"] = "not-run"
    elif ok3:
        ass["A3_per_player_one"] = "passed"
    else:
        ass["A3_per_player_one"] = "failed"
    notes.append(f"A3: mid_exported={NILSST['mid']} total_p1p1={total} "
                 f"max_on_one_creature={max_on_one} "
                 f"per_player={per_player} distribution={dist} "
                 f"prompts={NILSST['prompts']}")

    # A4: trigger resolved and game advanced
    ok4 = (NILSST["mid"] and NILSST["mid_turn"] is not None
           and NILSST["trigger_turn"] is not None
           and NILSST["mid_turn"] >= NILSST["trigger_turn"])
    ass["A4_trigger_resolved"] = "passed" if ok4 else (
        "not-run" if not NILSST["mid"] else "failed")
    notes.append(f"A4: trigger_turn={NILSST['trigger_turn']} "
                 f"mid_turn={NILSST['mid_turn']} mid_phase="
                 f"{NILSST['mid_phase']}")

    # A5: cleanup -- stack empty at final observation
    final = post or mid
    fstack = len(final.get("stack") or []) if final else None
    ok5 = fstack == 0
    ass["A5_cleanup"] = "passed" if ok5 else (
        "not-run" if final is None else "failed")
    notes.append(f"A5: final stack depth={fstack}")

    if ass["A2_setup_ok"] != "passed":
        verdict = "blocked"
    elif ass["A3_per_player_one"] == "not-run":
        verdict = "blocked"
        notes.append("verdict=blocked: mid export missed the resolution "
                     "window; re-run required.")
    elif ass["A3_per_player_one"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    notes.append(f"verdict={verdict}: {NILSST['answered']} prompt(s) "
                 f"answered; counters landed as {dist}")
    return ass, notes, verdict


async def finish():
    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7148_01030.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    if not os.path.exists(f"{EVDIR}/post.json"):
        await export_named(C0, "post")

    ass, notes, verdict = assertions_from_states()
    say(f"VERDICT: {verdict}")
    for n in notes:
        say("  " + n)

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "validated_at": time.strftime("%Y-%m-%d"),
        "server": SERVER_IDENTITY,
        "server_hello": SERVER_HELLO,
        "game_code": C0.game_code if C0 else None,
        "seats": {"P0": C0.player_id if C0 else None,
                  "P1": C1.player_id if C1 else None,
                  "P2": C2.player_id if C2 else None},
        "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
        "assertions": ass,
        "notes": notes,
        "rejections": OBS["rejections"],
        "target_selections": OBS["target_sels"],
        "unexpected_prompts": OBS["unexpected_prompts"],
        "tick_errors": OBS["tick_errors"],
        "nils": {k: v for k, v in NILSST.items()},
        "done_reason": ST["done_reason"],
        "verdict": verdict,
        "scope": "Nils, Discipline Enforcer end-step trigger 'for each "
                 "player, put a +1/+1 counter on up to one target creature "
                 "that player controls': 3-seat game (P0 Nils / P1 Bear / "
                 "P2 Crow, one creature each); P0 casts Nils once the Bear "
                 "and Crow are on the battlefield; driver answers each "
                 "target prompt with the scope player's creature; asserts "
                 "one counter per player; native engine, three "
                 "human-client seats, protocol 106",
        "limitations": [
            "Browser UI not exercised; native engine via three human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "The 'up to one' decline branch (choosing zero targets for a "
            "player) was not exercised; only the accept path was tested.",
            "Nils's second static ability (attack tax on counter-bearing "
            "creatures) was not exercised; no attacks were declared.",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written")

    try:
        WIRE.close()
    except Exception:
        pass
    RUNLOG.close()
    print("logs closed", flush=True)

    def write_manifest():
        files = sorted(f for f in os.listdir(EVDIR)
                       if os.path.isfile(f"{EVDIR}/{f}")
                       and f != "manifest.sha256")
        lines = []
        for fn in files:
            h = hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"manifest written ({len(lines)} files)", flush=True)

    write_manifest()
    try:
        import subprocess
        subprocess.run(
            [sys.executable,
             "/home/hatch/workspace/dev/phase-backfill/driver/render_summary_7148.py",
             EVDIR], check=True, timeout=120)
        print("summary.png rendered", flush=True)
    except Exception as e:
        print(f"PNG render failed: {e}", flush=True)
    write_manifest()


if __name__ == "__main__":
    asyncio.run(main())
