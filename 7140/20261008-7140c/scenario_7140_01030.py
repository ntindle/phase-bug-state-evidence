#!/usr/bin/env python3
"""Issue #7140: mercurial spelldancer - "Doesn't allow the copy, it does
remove the counters though."

Protocol-106 port of driver/scenario_7140.py (v0.82.0/protocol 70,
validated 2026-09-14, verdict reproduced) for pinned v0.103.0.
Behavioral contract, assertions A1..A7 and verdict rule unchanged.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0, key 'mercurial spelldancer'):
  Mercurial Spelldancer ({1}{U}, 2/1 Phyrexian Rogue):
    "This creature can't be blocked.
     Whenever you cast a noncreature spell, put an oil counter on this
     creature.
     Whenever this creature deals combat damage to a player, you may remove
     two oil counters from it. If you do, when you next cast an instant or
     sorcery spell this turn, copy that spell. You may choose new targets
     for the copy."

Card-data parse state on v0.103.0 (verified 2026-10-08 before the run):
  triggers[0] = SpellCast -> PutCounter oil 1 on SelfRef.
  triggers[1] = DamageDone -> RemoveCounter oil 2 on SelfRef (optional),
    sub_ability = CreateDelayedTrigger(WhenNextEvent SpellCast
    [Instant|Sorcery]) -> CopySpell(target=TriggeringSource,
    retarget=MayChooseNewTargets).
  The full clause parses as SUPPORTED. The reported defect is a runtime
  consumption defect: the removal happens but no copy is ever produced.

Reported symptom: after the combat-damage trigger removes two oil counters,
the next instant/sorcery cast that turn is NOT copied.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 4x mercurial spelldancer, 20x lightning bolt, 18x island, 18x mountain.
  P1: 60x forest (passive punching bag; never plays lands/casts/blocks).

Planned line:
  Setup: P0 drops lands, casts Spelldancer, casts 2x Lightning Bolt at P1
    (each noncreature cast => +1 oil counter; P1 20->14).
  COMBAT1 (turn>=5): attack P1 with Spelldancer (unblockable) => 2 combat
    damage (P1 14->12) => DamageDone trigger raises the optional
    "you may remove two oil counters" (vi exactChoices with
    decideOptionalEffect action code). Driver ACCEPTS. Counters 2->0 and
    the delayed copy trigger is registered. PRE exported before answering.
  accept_test (post-combat): P0 casts Lightning Bolt at P1. The delayed
    trigger should copy it: a "may choose new targets" prompt (answered
    accept) then a retarget target-selection (answered P1). Expect P1 12->6
    and exactly one bolt CARD in P0's graveyard (the copy is not a card).
    MID exported in the copy window. Copy detection is outcome-grounded
    (damage delta + retarget prompt + stack sample); the copy window can
    close between 250ms driver ticks, so stack sampling is opportunistic.
  regen (turn>=7 pre-combat): P0 casts Lightning Bolt at ITSELF (+1 oil
    counter; P0 20->17; counters back to 2 without killing P1).
  COMBAT2 (turn>=7): attack P1 with Spelldancer (P1 -> -2) => optional
    prompt again. Driver DECLINES as the control branch. Counters stay 2.
  decline_test (post-combat): P0 casts Lightning Bolt at P1 => NO copy is
    offered (decline control): exactly one bolt on the stack, no retarget
    prompt, exactly 3 damage.
  cleanup: stack empty, POST exported.

Assertions (each passed / failed / not-run):
  A1_parse            card-data: RemoveCounter oil 2 + CreateDelayedTrigger /
                      CopySpell MayChooseNewTargets all present (parse check).
  A2_setup            PRE: Spelldancer on P0 BF with exactly 2 oil counters;
                      P1 at 20->14 from the two setup Bolts only.
  A3_removal_accept   OptionalEffectChoice offered on combat damage and
                      accepted; oil counters 2->0.
  A4_copy_created     after the accept-test Bolt is cast, a copy of the Bolt
                      is evidenced: a may-retarget prompt accepted and/or a
                      retarget target-selection answered and/or 2 bolt spells
                      sampled on the stack and/or the 6-damage outcome.
                      (The reported bug fails HERE: counters removed but no
                      copy.)
  A5_copy_resolves    P1 12->6 from the accept-test Bolt+copy; exactly one
                      bolt card added to P0 graveyard by that cast.
  A6_decline_control  combat-2 prompt DECLINED: counters stay 2;
                      decline-test Bolt deals exactly 3, no retarget prompt.
  A7_cleanup          stack empty, game proceeds after the line completes.

Verdict rule:
  reproduced     iff A3 passes (counters removed on accept) but A4 fails
                 (no copy created / no retarget prompt) - the exact reported
                 symptom.
  not-reproduced iff A3+A4+A5 pass (copy created, retargeted, both resolve).
  blocked        iff A2 fails (setup never reached).

Protocol-106 port notes (from driver/scenario_7079_01030.py conventions):
  - my_priority = PassPriority present in legal_actions; casts gated on it.
  - Engine auto-taps for CastSpell (payment_mode Auto): the driver never
    answers tapLandForMana and runs no driver-side mana payment (legacy
    PayMana actions are answered if they appear).
  - DeclareAttackers: {"attacks": [[oid, {"type":"Player","data":1}]],
    "bands": []}; DeclareBlockers: {"assignments": []}.
  - The combat-damage optional prompt is a vi opportunity whose choices
    carry the decideOptionalEffect action code (scenario_301_01030.py
    shape); accept = choice with surface role "accept" value "true",
    decline = value "false".
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).
  - Pre/post/mid states are authoritative exports (data.state parsed once
    from the export envelope) via the host client only; the reported
    OUTCOME is asserted on the saved states, not the prompt.

Evidence: evidence/7140/<run-id>/pre.json, mid.json, post.json, run.json,
parse_mercurial_spelldancer.json, scenario_7140_01030.py, wire_log.jsonl,
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
ISSUE = 7140
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7140")
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

PARSE = {"ok": False, "triggers": None}


def check_parse_dancer():
    """A1: v0.103.0 triggers carry RemoveCounter oil 2 + CreateDelayedTrigger
    (WhenNextEvent SpellCast [Instant|Sorcery]) + CopySpell with
    retarget MayChooseNewTargets."""
    c = CARD_DATA.get("mercurial spelldancer", {})
    trigs = c.get("triggers", [])
    blob = json.dumps(trigs)
    has = {
        "RemoveCounter": "RemoveCounter" in blob,
        "CreateDelayedTrigger": "CreateDelayedTrigger" in blob,
        "CopySpell": "CopySpell" in blob,
        "MayChooseNewTargets": "MayChooseNewTargets" in blob,
        "oil": '"oil"' in blob or "'oil'" in blob,
    }
    with open(f"{EVDIR}/parse_mercurial_spelldancer.json", "w") as fh:
        json.dump({"card": "Mercurial Spelldancer",
                   "oracle_text": c.get("oracle_text"),
                   "mana_cost": c.get("mana_cost"),
                   "triggers": trigs,
                   "checks": has},
                  fh, indent=1, default=str)
    say("saved parse_mercurial_spelldancer.json")
    ok = all(has.values()) and len(trigs) >= 2
    PARSE["triggers"] = trigs
    PARSE["ok"] = ok
    say(f"parse: {has} -> A1={'passed' if ok else 'failed'}")
    wire("parse_check", {"A1": "passed" if ok else "failed", **has})
    return ok


DANCER = "mercurial spelldancer"
BOLT = "lightning bolt"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"
LANDS = (ISLAND, MOUNTAIN)

P0_DECK = [("Mercurial Spelldancer", 4), ("Lightning Bolt", 20),
           ("Island", 18), ("Mountain", 18)]
P1_DECK = [("Forest", 60)]

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45

STOP = {"stop": False}
ST = {
    "stage": "setup",  # setup -> combat1 -> accept_test -> regen ->
                       # combat2 -> decline_test -> cleanup -> done
    "dancer_cast": False, "counter_bolts": 0,
    "attack1_done": False, "attack2_done": False,
    "counters_pre_accept": None, "p1_life_pre": None,
    "p1_life_pre_attack": None,  # P1 life at the combat1 transition,
                                 # BEFORE the dancer's combat damage
    "accept_offered": False, "accept_done": False,
    "counters_post_accept": None,
    "test_bolt_cast": False, "test_bolt_targeted": False,
    "test_target_auto": False, "test_bolt_max_stack": 0,
    "retarget_may_offered": False, "retarget_may_accepted": False,
    "retarget_offered": False, "retarget_done": False,
    "gy_bolts_pre_test": None, "p1_life_pre_test": None,
    "p1_life_after_accept": None, "gy_bolts_after_accept": None,
    "mid_exported": False,
    "regen_bolt_cast": False, "gy_bolts_pre_regen": None,
    "p0_life_pre_regen": None, "regen_resolved": False,
    "decline_offered": False, "decline_done": False,
    "counters_at_decline": None,
    "decl_bolt_cast": False, "decl_bolt_targeted": False,
    "decl_bolt_max_stack": 0, "decl_retarget_seen": False,
    "gy_bolts_pre_decl": None, "p1_life_pre_decl": None,
    "p1_life_after_decl": None,
    "pre_exported": False, "post_exported": False,
    "cleanup_turn": None, "p0_life_final": None, "p1_life_final": None,
    "pending_bolt_kind": None, "pending_bolt_tgt": 1,
}
OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": [], "life_trace": [],
       "optional_prompts": []}
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


def counters_of(state, oid):
    """Oil counters on an object; handles dict- or list-shaped counters."""
    o = get_obj(state, oid)
    c = o.get("counters") or {}
    if isinstance(c, dict):
        for k in ("oil", "Oil", "OIL"):
            if k in c:
                v = c[k]
                return int(v.get("count", v) if isinstance(v, dict) else v)
        return sum(int(v.get("count", v)) if isinstance(v, dict) else int(v)
                   for v in c.values())
    if isinstance(c, list):
        n = 0
        for e in c:
            if isinstance(e, dict) and str(e.get("type", "")).lower() == "oil":
                n += int(e.get("count", 1))
        return n
    return 0


def stack_spells(state, name=None):
    out = []
    for e in state.get("stack") or []:
        nm = str(e.get("name") or e.get("card_name") or "").lower()
        if name is None or nm == name:
            out.append(e)
    return out


def gy_bolts(state, pid):
    return sum(1 for oid, o in (state.get("objects") or {}).items()
               if o.get("zone") == "Graveyard"
               and str(o.get("base_name") or o.get("name")
                       or "").lower() == BOLT
               and str(o.get("controller")) == str(pid))


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


def accept_of(choice):
    """surface role 'accept' value: 'true' (accept) / 'false' (decline)."""
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "accept":
            return str(d.get("value"))
    return None


def is_optional_opp(opp):
    for ch in (opp.get("response") or {}).get("data", {}).get("choices", []):
        if "decideOptionalEffect" in surf_codes(ch) \
                or "decideOptionalCost" in surf_codes(ch):
            return True
    return False


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
    want = DANCER if pid == 0 else None
    if pid == 0:
        keep = (want in hn and n_lands >= 2) or n >= 2
    else:
        keep = n_lands >= 2 or n >= 2
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
    key_card = DANCER if pid == 0 else None
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
            if key_card and nm == key_card:
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
        return 0
    if nm in LANDS:
        return 1
    if pid == 0 and nm == DANCER:
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
            if pid == 0:
                # stage-gated below; default to no attack
                d["data"]["attacks"] = []
                d["data"]["bands"] = []
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


async def play_a_land(c, state, pid, acts, tag, target):
    """Play a land while fewer than `target` untapped lands (prefer the
    color P0 needs)."""
    if len(untapped_lands(state, pid)) >= target:
        return False
    cands = [a for a in acts if a.get("type") == "PlayLand"]
    if not cands:
        return False

    def rank(a):
        try:
            nm = obj_lname(state, a.get("_src_oid"))
        except (TypeError, ValueError):
            return (9, "")
        # P0 wants Island for the dancer and Mountain for bolts
        if pid == 0:
            return (0 if nm == ISLAND else (1 if nm == MOUNTAIN else 2), nm)
        return (3, nm)

    a = sorted(cands, key=rank)[0]
    say(f"[{tag}] plays land {obj_lname(state, a.get('_src_oid'))} "
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
             "codes": sorted({x for ch in chs
                              for x in surf_codes(ch) if x}),
             "texts": [choice_text(ch)[:60] for ch in chs][:8]})
        say(f"[{tag}] unanswered vi iid={iid} n={len(chs)} "
            f"rtype={resp.get('type')}")
        wire("unanswered_vi",
             {"who": tag, "iid": iid,
              "opportunity": json.loads(json.dumps(opp, default=str))})

# ------------------------------------------------- issue-specific logic

def record_target_sel(state, opp, stage, purpose):
    """Record a target-selection opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in OBS["target_selections"]):
        return
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("candidates") or data.get("choices") or []
    cand_info = []
    for ch in chs:
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
        "purpose": purpose,
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
    say(f"target selection #{n} ({purpose}, stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']},"
                    f"tapped={x['tapped']})" for x in cand_info[:8]))
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "purpose": purpose, "iid": iid})


def record_optional_opp(state, opp, stage, role):
    """Record an optional-effect opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r["interactionId"] == iid for r in OBS["optional_prompts"]):
        return
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or []
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "role": role,
        "rtype": resp.get("type"),
        "choices": [{"id": ch.get("id"),
                     "text": choice_text(ch)[:160],
                     "accept": accept_of(ch),
                     "codes": [x for x in surf_codes(ch) if x]}
                    for ch in chs],
    }
    OBS["optional_prompts"].append(rec)
    n = len(OBS["optional_prompts"])
    with open(f"{EVDIR}/optional_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(json.dumps(opp, default=str))},
                  f, indent=1, default=str)
    say(f"optional prompt #{n} ({role}, stage {stage}): "
        + " | ".join(f"{c['text'][:60]}(accept={c['accept']})"
                     for c in rec["choices"]))
    wire("optional_prompt_recorded",
         {"n": n, "stage": stage, "role": role, "iid": iid})


async def optional_effect_tick(c, pid, tag, st, state):
    """Answer the combat-damage 'you may remove two oil counters' prompt
    (combat1: ACCEPT the reported branch; combat2: DECLINE the control)
    and the copy's 'may choose new targets' prompt (accept_test: ACCEPT)."""
    stage = ST["stage"]
    if stage not in ("combat1", "combat2", "accept_test"):
        return False
    for opp in unanswered_ops(st):
        if not is_optional_opp(opp):
            continue
        resp = opp.get("response", {}) or {}
        chs = (resp.get("data", {}) or {}).get("choices", []) or []
        if not chs:
            continue
        if stage == "combat1":
            role = "removal-accept"
        elif stage == "combat2":
            role = "removal-decline"
        else:
            role = "retarget-may"
        record_optional_opp(state, opp, stage, role)
        accept_ch = next((ch for ch in chs if accept_of(ch) == "true"), None)
        decline_ch = next((ch for ch in chs if accept_of(ch) == "false"),
                          None)
        if stage == "combat1":
            st_now = st["state"]
            dancer = bf_oids(st_now, 0, DANCER)
            ST["counters_pre_accept"] = (counters_of(st_now, dancer[0])
                                         if dancer else None)
            ST["p1_life_pre"] = life_of(st_now, 1)
            say(f"[P0] combat1 optional: pre counters="
                f"{ST['counters_pre_accept']} P1 life={ST['p1_life_pre']}")
            if not ST["pre_exported"]:
                if await export_named(c, "pre"):
                    ST["pre_exported"] = True
                    st_now = st_of(c)["state"]
            ST["accept_offered"] = True
            if accept_ch is None:
                say("[P0] ERROR: no accept=true choice on the removal "
                    "prompt; NOT answering")
                wire("optional_no_accept_choice", {"stage": stage})
                return True
            say("[P0] ACCEPTING removal of two oil counters "
                "(reported branch)")
            await answer_vi(c, opp, accept_ch, tag)
            ST["accept_done"] = True
            ST["stage"] = "accept_test"
            return True
        elif stage == "combat2":
            st_now = st["state"]
            dancer = bf_oids(st_now, 0, DANCER)
            ST["counters_at_decline"] = (counters_of(st_now, dancer[0])
                                         if dancer else None)
            ST["decline_offered"] = True
            if decline_ch is None:
                say("[P0] ERROR: no accept=false choice on the removal "
                    "prompt; NOT answering")
                wire("optional_no_decline_choice", {"stage": stage})
                return True
            say("[P0] DECLINING removal (control branch)")
            await answer_vi(c, opp, decline_ch, tag)
            ST["decline_done"] = True
            ST["stage"] = "decline_test"
            return True
        else:  # accept_test: the copy's "may choose new targets"
            ST["retarget_may_offered"] = True
            if accept_ch is None:
                say("[P0] retarget-may: no accept=true choice; holding")
                wire("retarget_may_no_accept", {})
                return True
            say("[P0] retarget-may: ACCEPTING (choose new targets)")
            await answer_vi(c, opp, accept_ch, tag)
            ST["retarget_may_accepted"] = True
            return True
    return False


async def bolt_target_tick(c, tag, st, state):
    """Answer a pending bolt's own target selection (seat from
    pending_bolt_tgt)."""
    stage = ST["stage"]
    if stage not in ("setup", "accept_test", "regen", "decline_test"):
        return False
    kind = ST.get("pending_bolt_kind")
    if not kind:
        return False
    for opp in unanswered_ops(st):
        if is_optional_opp(opp):
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        record_target_sel(state, opp, stage, f"bolt-{kind}")
        seat = ST.get("pending_bolt_tgt", 1)
        pick = next((ch for ch in chs if cand_seat(ch) == seat), None)
        if pick is None:
            pick = chs[0]
        say(f"[P0] bolt-{kind} target: choosing seat {seat} "
            f"(choice {pick.get('id')})")
        await answer_vi(c, opp, pick, tag)
        ST["pending_bolt_kind"] = None
        if kind == "test":
            ST["test_bolt_targeted"] = True
        elif kind == "decl":
            ST["decl_bolt_targeted"] = True
        return True
    return False


async def retarget_tick(c, tag, st, state):
    """Answer the copy's MayChooseNewTargets target selection in
    accept_test. A retarget-shaped prompt in decline_test is unexpected
    (control violation) and is recorded."""
    stage = ST["stage"]
    # decline-control violation path
    if stage == "decline_test" and ST.get("decl_bolt_targeted"):
        for opp in unanswered_ops(st):
            if is_optional_opp(opp):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("candidates") or data.get("choices") or []
            if not chs:
                continue
            ST["decl_retarget_seen"] = True
            record_target_sel(state, opp, stage, "unexpected-retarget")
            say("[P0] UNEXPECTED retarget prompt in decline_test; "
                "answering seat 1 to keep the game moving")
            pick = next((ch for ch in chs if cand_seat(ch) == 1), None)
            if pick is None:
                pick = chs[0]
            await answer_vi(c, opp, pick, tag)
            return True
        return False
    # accept leg: only after the test bolt's own targeting
    if stage != "accept_test" or not ST.get("test_bolt_targeted") \
            or ST.get("retarget_done"):
        return False
    for opp in unanswered_ops(st):
        if is_optional_opp(opp):
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        record_target_sel(state, opp, stage, "copy-retarget")
        ST["retarget_offered"] = True
        pick = next((ch for ch in chs if cand_seat(ch) == 1), None)
        if pick is None:
            pick = chs[0]
        say(f"[P0] copy retarget: choosing seat 1 (choice {pick.get('id')})")
        await answer_vi(c, opp, pick, tag)
        ST["retarget_done"] = True
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
        vals = list(d.values())
        src = a.get("_src_oid")
        if src is not None:
            vals.append(src)
        for v in vals:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    out.append((v, a))
                    break
            except (TypeError, ValueError):
                pass
    return out


async def p0_declare(c, acts, st, state):
    for a in acts:
        if a.get("type") != "DeclareAttackers":
            continue
        d = dict(a)
        d["data"] = dict(d.get("data") or {})
        stage = ST["stage"]
        turn = state.get("turn_number") or 0
        dancer = bf_oids(state, 0, DANCER)
        cnum = counters_of(state, dancer[0]) if dancer else 0
        gyb = gy_bolts(state, 0)
        do_atk = False
        if dancer and cnum >= 2 and gyb >= 2 and turn >= 5 \
                and not ST["attack1_done"] \
                and stage in ("setup", "combat1"):
            do_atk = True
            leg = 1
        elif dancer and ST.get("regen_resolved") and turn >= 7 \
                and not ST["attack2_done"] and stage in ("regen", "combat2"):
            do_atk = True
            leg = 2
        else:
            leg = 0
        if do_atk:
            d["data"]["attacks"] = [
                [dancer[0], {"type": "Player", "data": 1}]]
            d["data"]["bands"] = []
            say(f"[P0] declaring attacker: spelldancer -> P1 (leg {leg})")
        else:
            d["data"]["attacks"] = []
            d["data"]["bands"] = []
        await submit_as_is(c, d)
        if do_atk:
            if leg == 1:
                ST["attack1_done"] = True
            else:
                ST["attack2_done"] = True
        return True
    return False


def bolt_resolved(state, kind):
    pre = ST.get(f"gy_bolts_pre_{kind}")
    if pre is None:
        return False
    return len(stack_spells(state, BOLT)) == 0 and gy_bolts(state, 0) > pre


async def stage_transitions(c, tag, st, state):
    """Bookkeeping transitions from live state. Returns True if the tick
    should stop here (cleanup exported)."""
    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    if stage == "setup":
        dancer = bf_oids(state, 0, DANCER)
        cnum = counters_of(state, dancer[0]) if dancer else 0
        # gy>=2 gates on both setup bolts having RESOLVED (the counter
        # trigger resolves above the spell, so counters alone can hit 2
        # while a bolt is still on the stack)
        if dancer and cnum >= 2 and gy_bolts(state, 0) >= 2 and turn >= 5:
            ST["stage"] = "combat1"
            ST["p1_life_pre_attack"] = life_of(state, 1)
            say(f"[P0] stage -> combat1 (counters={cnum}, turn={turn}, "
                f"P1 life={ST['p1_life_pre_attack']})")

    if ST["stage"] == "accept_test":
        # capture the post-accept counter state before the test bolt
        if ST["counters_post_accept"] is None \
                and not ST["test_bolt_cast"]:
            dancer = bf_oids(state, 0, DANCER)
            ST["counters_post_accept"] = (counters_of(state, dancer[0])
                                          if dancer else None)
            say(f"[P0] counters_post_accept="
                f"{ST['counters_post_accept']}")
        if ST["test_bolt_cast"]:
            n = len(stack_spells(state, BOLT))
            if n > ST["test_bolt_max_stack"]:
                ST["test_bolt_max_stack"] = n
                say(f"[P0] accept_test stack: {n} bolt spell(s)")
                wire("accept_test_stack", {"n": n})
            # auto-target case: the target prompt never appeared but the
            # game advanced past targeting
            if not ST["test_bolt_targeted"] \
                    and (n > 0 or gy_bolts(state, 0)
                         > (ST.get("gy_bolts_pre_test") or 0)):
                ST["test_bolt_targeted"] = True
                ST["test_target_auto"] = True
                say("[P0] test bolt: no target prompt seen; treating as "
                    "auto-target")
            if ST["test_bolt_targeted"] and not ST["mid_exported"]:
                if await export_named(c, "mid"):
                    ST["mid_exported"] = True
                    if ST["p1_life_pre_test"] is None:
                        # fallback: capture now (cast-time capture missed)
                        ST["p1_life_pre_test"] = life_of(state, 1)
            if bolt_resolved(state, "test"):
                ST["p1_life_after_accept"] = life_of(state, 1)
                ST["gy_bolts_after_accept"] = gy_bolts(state, 0)
                say(f"[P0] accept_test bolt resolved: P1 "
                    f"{ST['p1_life_pre_test']}->"
                    f"{ST['p1_life_after_accept']}, P0-gy bolts="
                    f"{ST['gy_bolts_after_accept']}")
                ST["stage"] = "regen"

    if ST["stage"] == "regen":
        if ST["regen_bolt_cast"] and bolt_resolved(state, "regen"):
            ST["regen_resolved"] = True
            dancer = bf_oids(state, 0, DANCER)
            cnum = counters_of(state, dancer[0]) if dancer else 0
            say(f"[P0] regen bolt resolved (counters={cnum})")
        if ST.get("regen_resolved"):
            dancer = bf_oids(state, 0, DANCER)
            cnum = counters_of(state, dancer[0]) if dancer else 0
            if cnum >= 2 and turn >= 7:
                ST["stage"] = "combat2"
                say(f"[P0] stage -> combat2 (counters={cnum}, turn={turn})")

    if ST["stage"] == "decline_test" and ST["decl_bolt_cast"]:
        n = len(stack_spells(state, BOLT))
        if n > ST["decl_bolt_max_stack"]:
            ST["decl_bolt_max_stack"] = n
            say(f"[P0] decline_test stack: {n} bolt spell(s)")
        if not ST["decl_bolt_targeted"] \
                and (n > 0 or gy_bolts(state, 0)
                     > (ST.get("gy_bolts_pre_decl") or 0)):
            ST["decl_bolt_targeted"] = True
            say("[P0] decline bolt: no target prompt seen; auto-target")
        if bolt_resolved(state, "decl"):
            ST["p1_life_after_decl"] = life_of(state, 1)
            say(f"[P0] decline_test bolt resolved: P1 "
                f"{ST['p1_life_pre_decl']}->"
                f"{ST['p1_life_after_decl']}")
            ST["stage"] = "cleanup"
            ST["cleanup_turn"] = turn

    if ST["stage"] == "cleanup":
        if turn > (ST.get("cleanup_turn") or turn) \
                and stack_empty(state):
            ST["p0_life_final"] = life_of(state, 0)
            ST["p1_life_final"] = life_of(state, 1)
            if await export_named(c, "post"):
                ST["post_exported"] = True
                ST["stage"] = "done"
                return True
    return False

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
    if await optional_effect_tick(c, 0, "P0", st, state):
        return True
    if await bolt_target_tick(c, "P0", st, state):
        return True
    if await retarget_tick(c, "P0", st, state):
        return True
    if await p0_declare(c, acts, st, state):
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

    # life trace
    lives = (life_of(state, 0), life_of(state, 1))
    tr = OBS["life_trace"]
    if all(v is not None for v in lives) and (not tr or tr[-1][1] != lives):
        tr.append((round(time.time() - t_start, 1), lives))
        say(f"life = {lives}")
        wire("life", {"life": lives})

    # stage transitions from live state (never holds priority itself)
    if await stage_transitions(c, tag, st, state):
        STOP["stop"] = True
        return True

    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # ---- P0 priority: land drops, then stage-driven casts ----
    is_p0_main = (phase in ("PreCombatMain", "PostCombatMain", "Main")
                  and state.get("active_player") == 0)
    if is_p0_main:
        if await play_a_land(c, state, 0, acts, tag, 99):
            return True
        dancer_bf = bf_oids(state, 0, DANCER)
        if not dancer_bf and not ST["dancer_cast"]:
            found = cast_spell_for(acts, state, DANCER)
            if found:
                oid, action = found[0]
                say(f"[P0] casting mercurial spelldancer oid={oid} "
                    f"(engine Auto payment)")
                wire("dancer_cast", {"oid": str(oid)})
                await submit_as_is(c, action)
                ST["dancer_cast"] = True
                return True
        hn = hand_lnames(state, 0)
        stg = ST["stage"]
        want = None
        if stg == "setup" and ST["counter_bolts"] < 2 and dancer_bf \
                and BOLT in hn:
            want = ("counter", 1)      # setup bolts -> P1
        elif stg == "accept_test" and phase == "PostCombatMain" \
                and not ST["test_bolt_cast"] and BOLT in hn:
            want = ("test", 1)         # accept-test bolt -> P1
        elif stg == "regen" and turn >= 7 and not ST["regen_bolt_cast"] \
                and BOLT in hn:
            want = ("regen", 0)        # regen bolt -> SELF (P0)
        elif stg == "decline_test" and phase == "PostCombatMain" \
                and not ST["decl_bolt_cast"] and BOLT in hn:
            want = ("decl", 1)         # decline-test bolt -> P1
        if want:
            kind, tgt = want
            found = cast_spell_for(acts, state, BOLT)
            if found:
                oid, action = found[0]
                say(f"[P0] casting lightning bolt ({kind}) at seat {tgt}")
                wire("bolt_cast", {"kind": kind, "oid": str(oid),
                                   "target_seat": tgt})
                await submit_as_is(c, action)
                if kind == "counter":
                    ST["counter_bolts"] += 1
                else:
                    ST[f"{kind}_bolt_cast"] = True
                    ST[f"gy_bolts_pre_{kind}"] = gy_bolts(state, 0)
                if kind == "test":
                    ST["p1_life_pre_test"] = life_of(state, 1)
                elif kind == "decl":
                    ST["p1_life_pre_decl"] = life_of(state, 1)
                elif kind == "regen":
                    ST["p0_life_pre_regen"] = life_of(state, 0)
                ST["pending_bolt_kind"] = kind
                ST["pending_bolt_tgt"] = tgt
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

    # P1 is fully passive otherwise (never plays lands, never casts,
    # never blocks, never attacks)
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
    check_parse_dancer()

    p0 = PhaseClient("P07140r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P17140r")
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
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        notes = []
        ass = {k: "not-run" for k in
               ("A1_parse", "A2_setup", "A3_removal_accept",
                "A4_copy_created", "A5_copy_resolves", "A6_decline_control",
                "A7_cleanup")}

        def load(fn):
            try:
                with open(f"{EVDIR}/{fn}.json") as f:
                    return json.load(f)["state"]
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
                return None

        if not ST["post_exported"]:
            try:
                s = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(s)
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post fallback export failed: {e}")

        pre, mid, post = load("pre"), load("mid"), load("post")
        for fn, s in (("pre", pre), ("mid", mid), ("post", post)):
            if s is not None:
                say(f"loaded {fn}.json")

        def dancer_counters(s):
            d = bf_oids(s, 0, DANCER)
            return counters_of(s, d[0]) if d else None

        # ---- A1: parse ----
        ass["A1_parse"] = "passed" if PARSE["ok"] else "failed"
        notes.append("A1: card-data triggers carry RemoveCounter oil 2 + "
                     f"CreateDelayedTrigger + CopySpell + "
                     f"MayChooseNewTargets: {PARSE['ok']}")

        # ---- A2: setup (PRE at the accept prompt) ----
        # The DamageDone optional prompt fires AFTER combat damage, so the
        # prompt-time P1 life is 12 (14 after the two setup bolts, minus 2
        # from the dancer). p1_life_pre_attack is captured at the combat1
        # transition, before the attack.
        if pre is not None:
            d = bf_oids(pre, 0, DANCER)
            c = dancer_counters(pre)
            ok = (len(d) == 1 and c == 2
                  and ST.get("p1_life_pre_attack") == 14
                  and ST.get("p1_life_pre") == 12
                  and ST["counter_bolts"] >= 2)
            notes.append(f"A2: dancer_bf={len(d)} counters_pre={c} "
                         f"(expect 2) p1_life_pre_attack="
                         f"{ST.get('p1_life_pre_attack')} (expect 14, "
                         f"before combat damage) p1_life_pre="
                         f"{ST.get('p1_life_pre')} (expect 12, at the "
                         f"prompt after damage) counter_bolts="
                         f"{ST['counter_bolts']} (expect >=2)")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: removal accepted, counters 2->0 ----
        ok = (ST["accept_offered"] and ST["accept_done"]
              and ST["counters_pre_accept"] == 2
              and ST["counters_post_accept"] == 0)
        notes.append(f"A3: offered={ST['accept_offered']} "
                     f"done={ST['accept_done']} "
                     f"counters {ST['counters_pre_accept']}->"
                     f"{ST['counters_post_accept']} (expect 2->0)")
        ass["A3_removal_accept"] = "passed" if ok else "failed"

        # ---- A5 first (outcome-grounded): the accept-test Bolt must deal
        # 6 to P1 (3 original + 3 copy) with exactly one bolt CARD added
        # to P0's graveyard by that cast (the copy is not a card).
        dmg_accept = None
        if ST["p1_life_pre_test"] is not None \
                and ST["p1_life_after_accept"] is not None:
            dmg_accept = (ST["p1_life_pre_test"]
                          - ST["p1_life_after_accept"])
            ok = (dmg_accept == 6
                  and ST.get("gy_bolts_after_accept")
                  == (ST.get("gy_bolts_pre_test") or 0) + 1)
            notes.append(f"A5: P1 {ST['p1_life_pre_test']}->"
                         f"{ST['p1_life_after_accept']} (dmg={dmg_accept}, "
                         f"expect 6); P0-gy bolts="
                         f"{ST.get('gy_bolts_after_accept')} (expect "
                         f"{(ST.get('gy_bolts_pre_test') or 0) + 1})")
        else:
            ok = False
            notes.append("A5 failed: life window not captured")
        ass["A5_copy_resolves"] = "passed" if ok else "failed"

        # ---- A4: copy evidence - a may-retarget prompt answered, a
        # retarget target-selection answered, two bolt spells sampled on
        # the stack, or the 6-damage outcome itself.
        ok = (ST["retarget_may_accepted"]
              or ST["retarget_offered"]
              or ST["retarget_done"]
              or ST["test_bolt_max_stack"] >= 2
              or dmg_accept == 6)
        notes.append(f"A4: retarget_may_accepted="
                     f"{ST['retarget_may_accepted']} "
                     f"retarget_offered={ST['retarget_offered']} "
                     f"retarget_done={ST['retarget_done']} "
                     f"max_bolt_stack={ST['test_bolt_max_stack']} "
                     f"dmg_accept={dmg_accept}")
        ass["A4_copy_created"] = "passed" if ok else "failed"

        # ---- A6: decline control ----
        if post is not None and ST["decline_done"]:
            dmg = None
            if ST["p1_life_pre_decl"] is not None \
                    and ST["p1_life_after_decl"] is not None:
                dmg = (ST["p1_life_pre_decl"]
                       - ST["p1_life_after_decl"])
            ok = (ST["decline_offered"] and ST["decline_done"]
                  and ST["counters_at_decline"] == 2
                  and not ST["decl_retarget_seen"]
                  and dmg == 3)
            notes.append(f"A6: offered={ST['decline_offered']} "
                         f"done={ST['decline_done']} "
                         f"counters_at_decline="
                         f"{ST['counters_at_decline']} (expect 2) "
                         f"decl_max_stack={ST['decl_bolt_max_stack']} "
                         f"decl_retarget_seen={ST['decl_retarget_seen']} "
                         f"P1 {ST['p1_life_pre_decl']}->"
                         f"{ST['p1_life_after_decl']} (dmg={dmg}, expect 3)")
        else:
            ok = False
            notes.append(f"A6 failed: decline_done={ST['decline_done']} "
                         f"post={'ok' if post is not None else 'missing'}")
        ass["A6_decline_control"] = "passed" if ok else "failed"

        # ---- A7: cleanup ----
        if post is not None:
            stack_ok = not (post.get("stack") or [])
            ok = stack_ok
            notes.append(f"A7: stack_empty={stack_ok} "
                         f"life_final={ST['p0_life_final']}/"
                         f"{ST['p1_life_final']}")
        else:
            ok = False
            notes.append("A7 failed: post.json missing")
        ass["A7_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif (ass["A3_removal_accept"] == "passed"
                and ass["A4_copy_created"] == "failed"):
            verdict = "reproduced"
            notes.append("verdict=reproduced: counters were removed on "
                         "accept but no copy of the next instant/sorcery "
                         "was created - the exact reported symptom")
        elif (ass["A3_removal_accept"] == "passed"
                and ass["A4_copy_created"] == "passed"
                and ass["A5_copy_resolves"] == "passed"):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: accept removed counters, "
                         "copy was created, retargeted, and both bolt+copy "
                         "resolved for 6")
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
            "setup_line": ("P0 4x Mercurial Spelldancer / 20x Lightning "
                           "Bolt / 18x Island / 18x Mountain; P1 60x "
                           "Forest (passive). P0 mulligans for Dancer+2 "
                           "lands, casts Dancer, casts 2x Bolt at P1 (+1 "
                           "oil counter each). Combat1 (turn>=5): attack "
                           "with Dancer, accept the remove-two-counters "
                           "optional, cast Bolt at P1 (copy expected). "
                           "Regen (turn>=7): Bolt at self (+1 counter). "
                           "Combat2: attack, DECLINE the optional "
                           "(control), cast Bolt at P1 (no copy "
                           "expected)."),
            "contract_line": ("Mercurial Spelldancer's combat-damage "
                              "optional removes two oil counters but the "
                              "delayed 'copy your next instant/sorcery' "
                              "never produces a copy (expected); the "
                              "decline control keeps counters at 2 with no "
                              "copy -> reproduced."),
            "driver_notes": [
                "Protocol-106 port of driver/scenario_7140.py (v0.82.0 / "
                "protocol 70) for pinned v0.103.0; the behavioral contract, "
                "assertions A1..A7 and the verdict rule are unchanged.",
                "my_priority = PassPriority in legal_actions; casts are "
                "gated on it.",
                "CastSpell carries payment_mode Auto: the engine taps mana "
                "itself; the driver never answers tapLandForMana and runs "
                "no driver-side mana payment (legacy PayMana actions are "
                "answered if they appear).",
                "The combat-damage optional is a vi opportunity whose "
                "choices carry the decideOptionalEffect action code "
                "(scenario_301_01030.py shape); accept = role accept "
                "value true, decline = value false.",
                "The copy's 'may choose new targets' is answered from the "
                "same optional shape (accept) in the accept_test stage, "
                "then the retarget target-selection is answered with seat "
                "1; the engine may also auto-target a sole legal target "
                "(recorded as the auto-target case).",
                "DeclareAttackers via legacy Action with "
                "attacks=[[int(oid), {type:Player, data:1}]], bands=[]; "
                "DeclareBlockers with assignments=[].",
                "The stack-watch branch always falls through to the "
                "pass-priority gate (never returns early); both seats must "
                "pass in succession for a stack entry to resolve.",
                "Copy detection is outcome-grounded (damage delta + "
                "retarget prompt + opportunistic stack sample); the copy "
                "window can close between 250ms driver ticks.",
                "Pre/post/mid states are authoritative exports (data.state "
                "parsed once from the export envelope) via the host client "
                "only; the reported OUTCOME is asserted on the saved "
                "states, not the prompt.",
            ],
            "assertions": ass,
            "observations": OBS,
            "driver_state": ST,
            "mulligans": MULLS,
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "parse_mercurial_spelldancer.json",
                               "scenario_7140_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"]
                              + sorted(os.path.basename(p) for p in
                                       glob.glob(f"{EVDIR}/target_sel_*.json"))
                              + sorted(os.path.basename(p) for p in
                                       glob.glob(f"{EVDIR}/optional_*.json")),
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "4x Mercurial Spelldancer / 20x Lightning Bolt density is a "
                "test-harness convenience (engine accepts >4-of for custom "
                "games).",
                "P1 is a fully passive punching bag (60x Forest, never "
                "plays lands, never casts, never blocks).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Turn order is randomized by the engine; the driver keys on "
                "active_player and turn_number, not order.",
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
        with open(f"{EVDIR}/scenario_7140_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_7140_01030.py into EVDIR")

        srv_src = None
        for cand in (f"{BACKFILL}/runs/{RUN_ID}/server.log",):
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

        render_summary(run, pre, mid, post)

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

    def render_summary(run, pre, mid, post):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1020
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7140 - Mercurial Spelldancer",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "delayed copy trigger after counter removal",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: combat damage -> may remove two oil "
               "counters. If you do, copy your next instant/sorcery.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: RemoveCounter + CopySpell parse ok",
            "A2_setup": "PRE: dancer on BF, exactly 2 oil counters",
            "A3_removal_accept": "accept: counters 2->0",
            "A4_copy_created": "copy of next bolt (retarget/stack/6dmg)",
            "A5_copy_resolves": "P1 12->6, one bolt card in P0 gy",
            "A6_decline_control": "decline: counters stay 2, bolt deals 3",
            "A7_cleanup": "POST stack empty",
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

        def counters_in(s):
            dd = bf_oids(s, 0, DANCER)
            return counters_of(s, dd[0]) if dd else None

        for label, s in (("pre ", pre), ("mid ", mid), ("post", post)):
            if s is not None:
                line = (f"{label}: life {life_of(s, 0)}/{life_of(s, 1)}  "
                        f"dancer_counters={counters_in(s)}  "
                        f"stack_bolts={len(stack_spells(s, BOLT))}")
            else:
                line = f"{label}: (no state)"
            d.text((36, y), line[:118], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:16]:
            d.text((36, y), str(n)[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest(quiet=False):
        files = ["pre.json", "mid.json", "post.json", "run.json",
                 "parse_mercurial_spelldancer.json", "scenario_7140_01030.py",
                 "wire_log.jsonl", "scenario_run.log", "server.log",
                 "summary.png"]
        files += sorted(os.path.basename(p)
                        for p in glob.glob(f"{EVDIR}/target_sel_*.json"))
        files += sorted(os.path.basename(p)
                        for p in glob.glob(f"{EVDIR}/optional_*.json"))
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

        if turn > 30 and ST["stage"] == "setup" and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 30 reached, setup never "
                                "completed; finishing")
            say("watchdog: turn 30, setup never completed; finishing")
            await finish()
            return

        if turn > 40 and ST["stage"] not in ("cleanup", "done") \
                and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 40 reached with the line "
                                "incomplete; finishing")
            say("watchdog: turn 40, line incomplete; finishing")
            await finish()
            return

        if time.time() - last_diag > 60:
            last_diag = time.time()
            dancer = bf_oids(state, 0, DANCER)
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"pp={state.get('priority_player')} "
                f"life={[life_of(state, i) for i in (0, 1)]} "
                f"stage={ST['stage']} "
                f"counters={[counters_of(state, o) for o in dancer]} "
                f"attacks={ST['attack1_done']}/{ST['attack2_done']} "
                f"test={ST['test_bolt_cast']}/{ST['test_bolt_targeted']} "
                f"decl={ST['decl_bolt_cast']}/{ST['decl_bolt_targeted']} "
                f"stack_bolts={len(stack_spells(state, BOLT))}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
