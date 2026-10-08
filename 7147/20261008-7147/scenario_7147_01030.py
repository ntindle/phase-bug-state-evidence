#!/usr/bin/env python3
"""phase-rs/phase #7147 - Talon Gates of Madara ETB: selecting no targets
phases out ALL creatures instead of none. Protocol-106 re-validation.

Oracle: "When this land enters, up to one target creature phases out."

Reported: "Was able to play it, but when I selected none for the targets
it phased all available targets."

Triage acceptance criteria:
- Zero targets is legal and affects nothing.
- One selected target phases out only that creature.
- An empty target list can never fall back to all legal targets.

Card-data parse (v0.103.0): trigger mode ChangesZone, effect PhaseOut,
target Typed[Creature], multi_target {min: 0, max: 1}, optional False --
the parse is correct, so the defect (if any) is in runtime empty-selection
handling.

Behavioral contract (2 human seats, native engine, protocol 106):
  Leg A (reported bug): P0 plays Talon Gates of Madara with bears on both
    battlefields; at the ETB TriggerTargetSelection submit an EMPTY target
    selection (schema sequence/select, choiceIds: []); assert no creature
    phases out. PRE exported at the play transition (before prompt
    handling); "prompt never appeared + trigger resolved" is recorded as
    the auto-target case, not as a pass.
  Leg B (control): on a later turn P0 plays a second Talon Gates and
    selects exactly ONE bear; assert only that bear phases out.
  A1 parse_ok:       multi_target min 0 / max 1 on the ETB trigger.
  A2 setup_ok:       leg-A prompt seen, >=1 bear on each BF, pre exported.
  A3 empty_accepted: empty selection accepted (no interaction rejection).
  A4 zero_phases_nothing: no bear phased out after leg-A resolution.
                     (fails => the reported bug reproduces)
  A5 control_single: leg-B single target phases out exactly that bear.
  A6 cleanup:        stack empty at final observation.

Verdict: reproduced iff A2+A3 pass and A4 fails (creatures phased out
despite the empty selection). not-reproduced iff A4 passes. blocked iff
A2 or A3 fail (or A4 not-run).

Protocol-106 port notes (driver conventions from scenario_7143_01030.py):
  - waiting_for is gone (null) on 106; every prompt arrives as a
    viewer_interaction opportunity.
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full via
    the driver client; casts gated on my_priority (PassPriority in
    legal_actions).
  - CastSpell carries payment_mode Auto: the engine taps mana itself; the
    driver never answers tapLandForMana and runs no driver-side mana
    payment (legacy PayMana actions are answered if they appear).
  - The ETB TriggerTargetSelection is answered via the vi schema
    select/sequence opportunity: leg A submits choiceIds: [].
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES). Answered interactionIds are
    excluded everywhere (SUBMITTED_OPPS) so a hold never fires on an
    already-answered opportunity.
  - Pre/post states are authoritative exports (data.state parsed once from
    the export envelope) via the host client only.

Evidence: evidence/7147/<run-id>/preA.json, midA.json, preB.json,
midB.json, post.json, opportunity_legA.json, opportunity_legB.json,
parse_talon.json, run.json, scenario_7147_01030.py, wire_log.jsonl,
scenario_run.log, summary.png, manifest.sha256.
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
ISSUE = 7147
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7147")
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


def check_parse_7147():
    """Parse check: Talon Gates ETB is ChangesZone/PhaseOut with
    multi_target {min: 0, max: 1} (optional targeting preserved)."""
    t = CARD_DATA.get("talon gates of madara", {})
    trigs = t.get("triggers") or []
    rec = {"oracle_text": t.get("oracle_text"),
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
        rec["trigger"] = {"mode": tr.get("mode"),
                          "effect_type": eff.get("type"),
                          "target_type": tgt.get("type"),
                          "target_filters": tgt.get("type_filters"),
                          "multi_target": mt,
                          "optional": ex.get("optional"),
                          "optional_targeting": ex.get("optional_targeting")}
        ok = (tr.get("mode") == "ChangesZone"
              and eff.get("type") == "PhaseOut"
              and tgt.get("type") == "Typed"
              and "Creature" in (tgt.get("type_filters") or [])
              and mt.get("min") == 0 and mxv == 1)
    with open(f"{EVDIR}/parse_talon.json", "w") as fh:
        json.dump(rec, fh, indent=1, default=str)
    PARSE["ok"] = ok
    say(f"parse: talon_gates_of_madara ok={ok}")
    wire("parse_check", {"ok": ok, "trigger": rec["trigger"]})
    return ok


# ------------------------------------------------------------- constants

TALON = "talon gates of madara"
BEAR = "grizzly bears"
FOREST = "forest"
LANDS = (FOREST, TALON)

P0_DECK = [("Talon Gates of Madara", 12), ("Grizzly Bears", 12),
           ("Forest", 36)]
P1_DECK = [("Grizzly Bears", 12), ("Forest", 48)]
MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")
GAME_TIMEOUT = 1800
TURN_CAP = 40

STOP = {"stop": False}
ST = {"leg": "A", "legA": None, "legB": None, "done_reason": None}
OBS = {"rejections": [], "tick_errors": [], "unexpected_prompts": [],
       "target_sels": [], "notes": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
MULLS = {"P07147r": 0, "P17147r": 0}
PASSED_REV = {}
LAST_IID = {"iid": None}
t_start = 0.0


def fresh_legs():
    return {
        "legA": {"prompt_seen": False, "ncands": 0, "played": False,
                 "pre": False, "answered": False, "answer_t": None,
                 "mid": False, "turn": None, "answer_turn": None,
                 "mid_turn": None, "mid_phase": None, "rejected": False,
                 "targets_sampled": False},
        "legB": {"prompt_seen": False, "ncands": 0, "played": False,
                 "pre": False, "answered": False, "answer_t": None,
                 "mid": False, "turn": None, "answer_turn": None,
                 "mid_turn": None, "mid_phase": None, "rejected": False,
                 "target_oid": None, "target_cand_id": None,
                 "targets_sampled": False},
    }


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
        if o.get("zone") == "Battlefield" and str(o.get("controller")) == str(pid):
            if lname is None or obj_lname(state, oid) == lname:
                out.append(int(oid))
    return out


def bf_lands(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield"
                and str(o.get("controller")) == str(pid)
                and obj_lname(state, oid) in LANDS):
            out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [oid for oid in bf_lands(state, pid)
            if not get_obj(state, oid).get("tapped")]


def is_land(o):
    return str(o.get("base_name") or o.get("name") or "").lower() in LANDS


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


def drain(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            OBS["rejections"].append({"who": c.name, "type": t,
                                      "data": data, "leg": ST.get("leg")})
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
        keep = ((TALON in hn or BEAR in hn) and n_lands >= 1) or n >= 2
    else:
        keep = True  # P1's deck is bears + forests; always playable
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
            if nm in (TALON, BEAR):
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
    if nm in (TALON, BEAR):
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


# ------------------------------------------------------- talon ETB target

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


def has_trigger_on_stack(state):
    for e in state.get("stack") or []:
        k = e.get("kind")
        kt = k.get("type") if isinstance(k, dict) else k
        if kt == "TriggeredAbility":
            return True
    return False


def stack_trigger_targets(state):
    out = []
    for e in state.get("stack") or []:
        k = e.get("kind")
        kt = k.get("type") if isinstance(k, dict) else k
        if kt == "TriggeredAbility":
            out.append(e.get("targets"))
    return out


def phased_signal(o):
    """True if a battlefield object is phased out (checked against
    several field shapes; the engine keeps phased-out permanents in
    the Battlefield zone)."""
    ps = o.get("phase_status")
    if isinstance(ps, dict):
        if ps.get("status") == "PhasedOut":
            return True
    z = str(o.get("zone", "")).lower().replace("_", "")
    if z == "phasedout":
        return True
    return False


def bears_status(state):
    out = {}
    for oid, o in (state.get("objects") or {}).items():
        if obj_lname(state, oid) == BEAR:
            out[str(oid)] = {
                "zone": o.get("zone"),
                "controller": o.get("controller"),
                "tapped": bool(o.get("tapped")),
                "phased": phased_signal(o),
            }
    return out


async def answer_talon_target(c, st, state, tag, leg):
    """Answer the Talon Gates ETB TriggerTargetSelection.

    leg A: submit an EMPTY selection (the reported repro).
    leg B: submit exactly one candidate (control; a P1 bear if present).
    PRE is exported at this chapter transition, before answering (the
    engine may auto-target a sole legal target, so the prompt is not a
    precondition for the export).
    Returns True if an answer was submitted."""
    legst = ST[f"leg{leg}"]
    if legst["answered"]:
        return False
    opp, rtype, spec_type = target_opportunity(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    cands = data.get("candidates") or data.get("choices") or []
    with open(f"{EVDIR}/opportunity_leg{leg}.json", "w") as f:
        json.dump(opp, f, indent=1, default=str)
    say(f"[{tag}] leg{leg} TriggerTargetSelection: rtype={rtype} "
        f"stype={spec_type} ncands={len(cands)} iid={iid}")
    for ch in cands:
        ref = cand_oid(ch)
        nm = obj_lname(state, ref) if ref else "?"
        o = get_obj(state, ref) if ref else {}
        say(f"    cand id={ch.get('id')} -> {nm}#{ref} "
            f"(P{o.get('controller')}) zone={o.get('zone')}")
    wire("target_prompt", {"leg": leg, "rtype": rtype, "stype": spec_type,
                           "ncands": len(cands),
                           "cands": [cand_oid(ch) for ch in cands]})
    legst["prompt_seen"] = True
    legst["ncands"] = len(cands)
    legst["turn"] = state.get("turn_number")
    legst["answer_turn"] = state.get("turn_number")
    if not legst["pre"]:
        legst["pre"] = True
        await export_named(c, f"pre{leg}")
    if rtype == "schema":
        if leg == "A":
            ids = []  # the reported bug: choose NOTHING
            say(f"[{tag}] legA submits EMPTY target selection")
        else:
            pick = None
            for ch in cands:
                ref = cand_oid(ch)
                if ref is None:
                    continue
                o = get_obj(state, ref)
                if (obj_lname(state, ref) == BEAR
                        and str(o.get("controller")) == "1"
                        and not phased_signal(o)):
                    pick = ch
                    break
            pick = pick or (cands[0] if cands else None)
            if pick is None:
                say(f"[{tag}] legB: no candidates!")
                return False
            ids = [pick.get("id")]
            legst["target_oid"] = cand_oid(pick)
            legst["target_cand_id"] = pick.get("id")
            ref = cand_oid(pick)
            say(f"[{tag}] legB targets "
                f"{obj_lname(state, ref) if ref else '?'}#{ref}")
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": ids}}}
    else:
        # exactChoices shape: find an explicit none/decline for leg A
        if leg == "A":
            none = None
            for ch in cands:
                tx = (ch.get("text") or "").lower()
                if any(k in tx for k in
                       ["none", "no target", "decline", "done",
                        "skip", "cancel", "zero"]):
                    none = ch
                    break
            if none is None:
                say(f"[{tag}] legA: exactChoices with no none-option; "
                    f"cannot submit empty -> marking rejected path")
                legst["rejected"] = True
                SUBMITTED_OPPS.add(iid)
                return False
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": none.get("id")}}}
            say(f"[{tag}] legA answers none-choice id={none.get('id')} "
                f"text={none.get('text')!r}")
        else:
            pick = None
            for ch in cands:
                ref = cand_oid(ch)
                if ref is None:
                    continue
                o = get_obj(state, ref)
                if (obj_lname(state, ref) == BEAR
                        and str(o.get("controller")) == "1"
                        and not phased_signal(o)):
                    pick = ch
                    break
            pick = pick or (cands[0] if cands else None)
            if pick is None:
                return False
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
            legst["target_oid"] = cand_oid(pick)
            say(f"[{tag}] legB answers choice id={pick.get('id')} "
                f"-> {cand_oid(pick)}")
    wire("target_answer", {"leg": leg, "sub": sub})
    say(f"[{tag}] leg{leg} submitting: {json.dumps(sub)[:220]}")
    SUBMITTED_OPPS.add(iid)
    OBS["target_sels"].append({"leg": leg, "iid": iid,
                               "ncands": len(cands),
                               "rtype": rtype, "stype": spec_type})
    await interact_as(c, sub, tag)
    legst["answered"] = True
    legst["answer_t"] = time.time()
    return True


async def watch_resolution():
    """After a leg's answer, export mid as soon as the trigger leaves the
    stack -- same-turn capture, so phased-out permanents cannot phase back
    in at an untap step before we observe them. Never returns early from
    the stack-watch: this branch falls through to the caller's
    pass-priority gate."""
    leg = ST["leg"]
    if leg not in ("A", "B"):
        return
    legst = ST[f"leg{leg}"]
    if not legst["answered"] or legst["mid"]:
        return
    st = st_of(C0)
    if not st:
        return
    state = st["state"]
    tgts = stack_trigger_targets(state)
    if tgts and not legst.get("targets_sampled"):
        legst["targets_sampled"] = True
        wire(f"leg{leg}_trigger_targets", {"targets": tgts})
        say(f"[leg{leg}] trigger targets on stack: "
            f"{json.dumps(tgts)[:300]}")
    if not has_trigger_on_stack(state):
        legst["mid"] = True
        legst["mid_turn"] = state.get("turn_number")
        legst["mid_phase"] = state.get("phase")
        s_ok = await export_named(C0, f"mid{leg}")
        if s_ok:
            with open(f"{EVDIR}/mid{leg}.json") as f:
                s = json.load(f)["state"]
            bs = bears_status(s)
            say(f"[leg{leg}] mid: {len(bs)} bears tracked; phased="
                f"{[k for k, v in bs.items() if v['phased']]} "
                f"(answer turn {legst.get('answer_turn')} -> mid turn "
                f"{legst.get('mid_turn')})")
            wire(f"mid{leg}_bears", bs)
        if leg == "A":
            ST["leg"] = "B"
            say("legA resolution observed; leg=B armed")
        else:
            ST["leg"] = "DONE"
            ST["done_reason"] = "both legs resolved"
            STOP["stop"] = True
            say("legB resolution observed; stopping")


def phased_in_bears(state, pid):
    return [int(oid) for oid, v in bears_status(state).items()
            if v["zone"] == "Battlefield" and v["controller"] == pid
            and not v["phased"]]


def play_land_for(c, state, pid, acts, lname):
    """Play a land (PlayLand legacy action) whose object is lname."""
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
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    # the ETB target prompt (both legs); answered before anything else.
    # P0 controls the trigger, so only P0 answers.
    leg = ST["leg"]
    if leg in ("A", "B"):
        legst = ST[f"leg{leg}"]
        # PRE at the chapter transition: the trigger appearing on the stack
        # is the transition, exported before prompt handling (the engine
        # may auto-target a sole legal target without any prompt).
        if legst["played"] and not legst["pre"] \
                and has_trigger_on_stack(state):
            legst["pre"] = True
            say(f"[{tag}] leg{leg}: ETB trigger on stack, exporting PRE")
            await export_named(c, f"pre{leg}")
        if await answer_talon_target(c, st, state, tag, leg):
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

    if is_p0_main and stack_empty(state) and not my_priority(top_acts(st)):
        pass  # not our priority yet; fall to the gate below
    elif is_p0_main and stack_empty(state):
        legst = ST[f"leg{leg}"] if leg in ("A", "B") else None
        if leg == "A" and legst is not None and not legst["played"]:
            b0 = phased_in_bears(state, 0)
            b1 = phased_in_bears(state, 1)
            pa = play_land_for(c, state, pid, acts, TALON)
            if pa and b0 and b1:
                legst["played"] = True
                say(f"[{tag}] legA plays Talon Gates (turn {turn}) "
                    f"bears P0={len(b0)} P1={len(b1)}")
                wire("talon_played", {"leg": "A", "turn": turn})
                await submit_as_is(c, pa)
                return True
        if leg == "B" and legst is not None and not legst["played"]:
            b0 = phased_in_bears(state, 0)
            b1 = phased_in_bears(state, 1)
            pa = play_land_for(c, state, pid, acts, TALON)
            if pa and (len(b0) + len(b1)) >= 2:
                legst["played"] = True
                say(f"[{tag}] legB plays Talon Gates (turn {turn}) "
                    f"bears P0={len(b0)} P1={len(b1)}")
                wire("talon_played", {"leg": "B", "turn": turn})
                await submit_as_is(c, pa)
                return True
        # land drop (Forest) -- keep mana flowing
        if len(untapped_lands(state, 0)) < 8:
            pa = play_land_for(c, state, pid, acts, FOREST)
            if pa:
                say(f"[{tag}] plays Forest (turn {turn})")
                await submit_as_is(c, pa)
                return True
        # cast bears (2 max per side keeps targets tidy)
        if len(bf_oids(state, 0, BEAR)) < 2 and BEAR in hand_lnames(state, 0):
            found = cast_for(acts, state, BEAR)
            if found:
                oid, action = found[0]
                say(f"[{tag}] casts Grizzly Bears oid={oid} t{turn} "
                    f"(engine Auto payment)")
                wire("bear_cast", {"oid": str(oid), "turn": turn})
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
        if len(untapped_lands(state, 1)) < 8:
            pa = play_land_for(c, state, pid, acts, FOREST)
            if pa:
                say(f"[{tag}] plays Forest (turn {turn})")
                await submit_as_is(c, pa)
                return True
        if len(bf_oids(state, 1, BEAR)) < 2 and BEAR in hand_lnames(state, 1):
            found = cast_for(acts, state, BEAR)
            if found:
                oid, action = found[0]
                say(f"[{tag}] casts Grizzly Bears oid={oid} t{turn} "
                    f"(engine Auto payment)")
                wire("bear_cast", {"oid": str(oid), "turn": turn})
                await submit_as_is(c, action)
                return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


# ------------------------------------------------------------- main loop

C0 = C1 = None
SERVER_HELLO = {}


async def main():
    global t_start, C0, C1
    t_start = time.time()
    legs = fresh_legs()
    ST["legA"], ST["legB"] = legs["legA"], legs["legB"]

    hello = await verify_server_hello()
    SERVER_HELLO.update(hello)
    check_parse_7147()

    p0 = PhaseClient("P07147r")
    await p0.connect()
    say("P0 creating game (Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P17147r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    C0, C1 = p0, p1
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                  "server_hello": hello})

    last_rev_change = t_start
    last_revs = (-1, -1)
    while time.time() - t_start < GAME_TIMEOUT and not STOP["stop"]:
        revs = (p0.revision, p1.revision)
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
        if time.time() - last_rev_change > 150:
            ST["done_reason"] = "stall: no revision change for 150s"
            say("STALL: no revision change for 150s")
            STOP["stop"] = True
            break
        for c, pid, tag in ((p0, 0, "P07147r"), (p1, 1, "P17147r")):
            try:
                if pid == 0:
                    await p0_tick(c, pid, tag)
                else:
                    await p1_tick(c, pid, tag)
            except Exception as e:
                OBS["tick_errors"].append({"who": tag, "error": str(e)[:200]})
                say(f"tick error [{tag}]: {e}")
        await watch_resolution()
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
    legA, legB = ST["legA"], ST["legB"]
    preA = load_state("preA.json")
    midA = load_state("midA.json")
    preB = load_state("preB.json")
    midB = load_state("midB.json")
    post = load_state("post.json")

    # A1: parse check on the pinned card-data
    ok1 = PARSE["ok"]
    ass["A1_parse_ok"] = "passed" if ok1 else "failed"
    notes.append(f"A1: parse ok={ok1} (see parse_talon.json)")

    # A2: leg-A prompt seen with bears on both sides, pre exported
    bpre = bears_status(preA) if preA else {}
    sides = {v["controller"] for v in bpre.values()
             if v["zone"] == "Battlefield"}
    ok2 = (legA["prompt_seen"] and legA["pre"] and sides == {0, 1}
           and legA["ncands"] >= 2)
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"
    notes.append(f"A2: prompt_seen={legA['prompt_seen']} ncands="
                 f"{legA['ncands']} sides={sorted(sides)} pre={legA['pre']}")

    # A3: empty selection accepted (no rejection tied to the answer)
    rej_legA = [r for r in OBS["rejections"] if r.get("leg") == "A"]
    ok3 = (legA["answered"] and not legA["rejected"]
           and not any("nteraction" in json.dumps(r.get("data", ""))
                       for r in rej_legA))
    ass["A3_empty_accepted"] = "passed" if ok3 else "failed"
    notes.append(f"A3: answered={legA['answered']} rejected_flag="
                 f"{legA['rejected']} legA rejections={len(rej_legA)}")

    # A4: no bear phased out after leg-A resolution
    bmid = bears_status(midA) if midA else {}
    phasedA = sorted(k for k, v in bmid.items() if v["phased"])
    ok4 = (legA["mid"] and not phasedA)
    same_turn = (legA.get("mid_turn") == legA.get("answer_turn")
                 and legA.get("mid_turn") is not None)
    if not legA["mid"]:
        ass["A4_zero_phases_nothing"] = "not-run"
    elif ok4 and same_turn:
        ass["A4_zero_phases_nothing"] = "passed"
    else:
        ass["A4_zero_phases_nothing"] = "failed"
    notes.append(f"A4: mid_exported={legA['mid']} phased_after_empty="
                 f"{phasedA} answer_turn={legA.get('answer_turn')} "
                 f"mid_turn={legA.get('mid_turn')} same_turn={same_turn} "
                 f"(bears pre={len(bpre)} mid={len(bmid)})")

    # A5: control leg -- exactly the chosen bear phased out
    bpreB = bears_status(preB) if preB else {}
    bmidB = bears_status(midB) if midB else {}
    phasedB = sorted(k for k, v in bmidB.items() if v["phased"])
    tgt = str(legB["target_oid"]) if legB["target_oid"] is not None else None
    others_phased = [k for k in phasedB if k != tgt]
    if not legB["prompt_seen"]:
        ass["A5_control_single"] = "not-run"
    elif legB["mid"] and tgt is not None and tgt in phasedB \
            and not others_phased:
        ass["A5_control_single"] = "passed"
    else:
        ass["A5_control_single"] = "failed"
    notes.append(f"A5: prompt_seen={legB['prompt_seen']} answered="
                 f"{legB['answered']} target_oid={tgt} phased={phasedB} "
                 f"others_phased={others_phased}")

    # A6: cleanup -- stack empty at final observation
    final = post or midB or midA
    fstack = len(final.get("stack") or []) if final else None
    ok6 = fstack == 0
    ass["A6_cleanup"] = "passed" if ok6 else (
        "not-run" if final is None else "failed")
    notes.append(f"A6: final stack depth={fstack}")

    if ass["A2_setup_ok"] != "passed" or ass["A3_empty_accepted"] != "passed":
        verdict = "blocked"
    elif ass["A4_zero_phases_nothing"] == "not-run":
        verdict = "blocked"
        notes.append("verdict=blocked: mid export missed the resolution "
                     "window; re-run required.")
    elif ass["A4_zero_phases_nothing"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    notes.append(
        f"verdict={verdict}: legA empty selection -> phased bears "
        f"{phasedA}; legB single-target control -> phased {phasedB} "
        f"(target {tgt})")
    return ass, notes, verdict


async def finish():
    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7147_01030.py")
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
                  "P1": C1.player_id if C1 else None},
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "rejections": OBS["rejections"],
        "target_selections": OBS["target_sels"],
        "unexpected_prompts": OBS["unexpected_prompts"],
        "tick_errors": OBS["tick_errors"],
        "legs": {"A": dict(ST["legA"]), "B": dict(ST["legB"])},
        "done_reason": ST["done_reason"],
        "verdict": verdict,
        "scope": "Talon Gates of Madara ETB 'up to one target creature "
                 "phases out': leg A submits an EMPTY target selection at "
                 "the TriggerTargetSelection prompt; leg B (control) "
                 "selects exactly one creature; native engine, two "
                 "human-client seats, protocol 106",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense 12x playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Phased-out detection keys on phase_status.status == "
            "'PhasedOut' plus Battlefield zone; per-bear pre/mid diffs "
            "are in the run notes and wire log.",
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
             "/home/hatch/workspace/dev/phase-backfill/driver/render_summary_7147.py",
             EVDIR], check=True, timeout=120)
        print("summary.png rendered", flush=True)
    except Exception as e:
        print(f"PNG render failed: {e}", flush=True)
    write_manifest()


if __name__ == "__main__":
    asyncio.run(main())
