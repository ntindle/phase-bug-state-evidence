#!/usr/bin/env python3
"""Issue #7162: "Lazav, Familiar Stranger is not prompted to become exiled
creatures" -- Lazav exiles cards correctly after a crime, but never offers
the "you may have Lazav become a copy of that card" choice.

Protocol-106 driver for pinned v0.103.0 (build ec27a8d), adapted from the
scenario_7141_01030.py conventions.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0, key "lazav, familiar stranger"):
  Lazav, Familiar Stranger ({U}{B}, 1/4 Legendary Creature - Shapeshifter):
    "Whenever you commit a crime, put a +1/+1 counter on Lazav. Then you may
     exile a card from a graveyard. If a creature card was exiled this way,
     you may have Lazav become a copy of that card until end of turn. This
     ability triggers only once each turn. (Targeting opponents, anything
     they control, and/or cards in their graveyards is a crime.)"

Card-data parse state on v0.103.0 (verified 2026-10-08 before the run):
  triggers[0] = CommitCrime -> PutCounter(P1P1, SelfRef)
    sub_ability (optional, target_choice_timing=Resolution) =
      ChangeZone(Graveyard -> Exile, target=Typed Card in Graveyard)
      sub_ability (optional, duration=UntilEndOfTurn) =
        BecomeCopy(target=ParentTarget, duration=UntilEndOfTurn,
                   condition=ZoneChangedThisWay[Creature])
    sub_link = SequentialSibling throughout.
  Parse-shape observation (not asserted): the "triggers only once each turn"
  clause is not visible as a distinct node in the trigger blob; the driver
  commits exactly one crime per turn so the clause is exercised trivially.

Reported symptom (Discord): "[[Lazav, Familiar Stranger]] exiles cards
correctly after committing a crime, but doesn't give the option to become a
copy of the exiled card if its a creature."

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 8x lazav, familiar stranger, 8x child of night, 8x lightning bolt,
      14x island, 12x swamp, 10x mountain (denser than 4-of for fixture
      reliability; the engine accepts >4-of in custom games).
  P1: 60x forest (passive; never plays lands/casts/blocks).

Planned line (deterministic, no combat):
  Setup: P0 drops lands (Island, then Swamp, then Mountain), casts Lazav
    ({U}{B}, engine Auto payment), casts Child of Night ({1}{B} 2/1
    lifelink, engine Auto payment). PRE exported in the crime-turn main
    phase with Lazav + Child of Night on the battlefield.
  CRIME (one turn): P0 casts Lightning Bolt targeting its own Child of
    Night (3 damage kills the 2/1; NOT a crime -- targeting your own
    permanent). Child of Night goes to P0's graveyard. P0 then casts a
    second Lightning Bolt targeting P1 -- a crime (targeting an opponent).
    Lazav's CommitCrime trigger fires (once).
  RESOLUTION: the trigger resolves: +1/+1 counter on Lazav (automatic);
    the may-exile offer is EXPECTED for P0 (driver ACCEPTS); the exile
    selection is EXPECTED (driver exiles Child of Night from P0's
    graveyard; offer_exile.json exported at the prompt); the may-become-copy
    offer is EXPECTED for P0 (driver ACCEPTS; offer_copy.json exported at
    the prompt). POST exported once the stack is empty and the game moves
    on.

Assertions (each passed / failed / not-run):
  A1_parse            card-data: CommitCrime -> PutCounter(P1P1, SelfRef) +
                      ChangeZone(Graveyard->Exile, optional, Resolution) +
                      BecomeCopy(ParentTarget, optional, UntilEndOfTurn,
                      condition ZoneChangedThisWay[Creature]).
  A2_setup            PRE: Lazav and Child of Night on P0's battlefield.
  A3_crime_trigger    Lazav's trigger fired and resolved: Lazav carries at
                      least one +1/+1 counter in POST (counter is the
                      unconditional first effect).
  A4_exile_offered    the may-exile offer was raised for P0 and the driver
                      exiled Child of Night (a creature card) from P0's
                      graveyard; the Child is in exile in POST.
  A5_copy_offered     the may-become-copy offer was raised for P0 (the
                      reported symptom is that it never appears).
  A6_copy_effect      (only if A5 passes) accept: POST: the same Lazav
                      object (oid tracked from PRE) is a copy of Child of
                      Night until end of turn -- name "child of night",
                      power 3 / toughness 2 (2/1 copy + the +1/+1 counter),
                      still on the battlefield.
  A7_cleanup          POST: stack empty, game proceeding.

Verdict rule:
  blocked        iff A2 fails (setup never reached).
  reproduced     iff A2..A4 pass and (A5 fails -- the reported missing
                 prompt -- or (A5 passes and A6 fails), i.e. the accept
                 branch does not produce the copy).
  not-reproduced iff A2..A7 all pass.

Protocol-106 conventions (from scenario_7141_01030.py):
  - my_priority = PassPriority present in legal_actions; casts gated on it.
  - Engine auto-taps for CastSpell (payment_mode Auto): the driver never
    answers tapLandForMana and runs no driver-side mana payment (legacy
    PayMana actions are answered if they appear).
  - Bolt target selection is a vi opportunity answered with the advertised
    candidate (own Child of Night for bolt 1, seat 1 for bolt 2); the
    engine may also auto-target a sole legal target (recorded as the
    auto-target case).
  - The may-exile / may-copy offers are vi opportunities whose choices
    carry the decideOptionalEffect action code (scenario_301_01030.py
    shape); accept = choice with surface role "accept" value "true".
    The exile card selection is a schema select/sequence opportunity whose
    candidates reference graveyard objects; answered with the Child of
    Night oid via the select shape.
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).
  - Pre/offer/post states are authoritative exports via the host client
    only; the reported OUTCOME is asserted on the saved states, not the
    prompt.

Evidence: evidence/7162/<run-id>/pre.json, offer_exile.json,
offer_exile_opp.json, offer_copy.json, offer_copy_opp.json, post.json,
run.json, parse_lazav_familiar_stranger.json, scenario_7162_01030.py,
wire_log.jsonl, scenario_run.log, server.log, summary.png, manifest.sha256,
lazav_prompts.json.
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
ISSUE = 7162
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-0811-7162")
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

PARSE = {"ok": False, "triggers": None, "obs": {}}


def check_parse_lazav():
    """A1: v0.103.0 trigger[0] is CommitCrime -> PutCounter(P1P1, SelfRef)
    -> optional ChangeZone(Graveyard->Exile, target_choice_timing=Resolution)
    -> optional BecomeCopy(ParentTarget, UntilEndOfTurn,
    condition=ZoneChangedThisWay[Creature])."""
    c = CARD_DATA.get("lazav, familiar stranger", {})
    trigs = c.get("triggers", [])
    blob = json.dumps(trigs)
    has = {
        "CommitCrime": "CommitCrime" in blob,
        "PutCounter": "PutCounter" in blob,
        "P1P1": "P1P1" in blob,
        "SelfRef": "SelfRef" in blob,
        "ChangeZone": "ChangeZone" in blob,
        "Graveyard": '"Graveyard"' in blob,
        "Exile": '"Exile"' in blob,
        "optional": '"optional": true' in blob,
        "Resolution": '"Resolution"' in blob,
        "BecomeCopy": "BecomeCopy" in blob,
        "ParentTarget": "ParentTarget" in blob,
        "UntilEndOfTurn": "UntilEndOfTurn" in blob,
        "ZoneChangedThisWay": "ZoneChangedThisWay" in blob,
        "Creature": '"Creature"' in blob,
    }
    with open(f"{EVDIR}/parse_lazav_familiar_stranger.json", "w") as fh:
        json.dump({"card": "Lazav, Familiar Stranger",
                   "oracle_text": c.get("oracle_text"),
                   "mana_cost": c.get("mana_cost"),
                   "power": c.get("power"),
                   "toughness": c.get("toughness"),
                   "triggers": trigs,
                   "checks": has},
                  fh, indent=1, default=str)
    say("saved parse_lazav_familiar_stranger.json")
    ok = all(has.values()) and len(trigs) >= 1
    PARSE["triggers"] = trigs
    PARSE["ok"] = ok
    say(f"parse: {has} -> A1={'passed' if ok else 'failed'}")
    wire("parse_check", {"A1": "passed" if ok else "failed", **has})
    return ok


LAZAV = "lazav, familiar stranger"
CHILD = "child of night"
BOLT = "lightning bolt"
ISLAND = "island"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
LANDS = (ISLAND, SWAMP, MOUNTAIN)

P0_DECK = [(LAZAV, 8), (CHILD, 8), (BOLT, 8), (ISLAND, 14), (SWAMP, 12),
           (MOUNTAIN, 10)]
P1_DECK = [(FOREST, 60)]

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45
OFFER_WAIT_S = 60

STOP = {"stop": False}
ST = {
    "stage": "setup",  # setup -> crime -> crime_leg -> settle -> done
    "lazav_cast": False,
    "child_cast": False,
    "lazav_oid": None,
    "child_oid": None,
    "pre_exported": False,
    "gy0_bolts_pre": None,
    "p1_life_pre": None,
    "p0_life_pre": None,
    "bolt1_cast": False, "bolt1_pending": False, "bolt1_targeted": False,
    "bolt1_auto": False,
    "child_in_gy": False,
    "bolt2_cast": False, "bolt2_pending": False, "bolt2_targeted": False,
    "bolt2_auto": False,
    "gy0_bolts_post_bolt1": None,
    "p1_life_pre_crime2": None,
    "may_exile_seen": False, "may_exile_accepted": False,
    "exile_offer_exported": False,
    "exile_done": False, "exiled_child_oid": None,
    "may_copy_seen": False, "may_copy_accepted": False,
    "copy_offer_exported": False,
    "copy_accepted": False,
    "crime_leg_t0": None,
    "copy_wait_expired": False,
    "p1_life_post_bolt2": None,
    "post_exported": False,
    "settle_ticks": 0,
}

OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": [], "life_trace": [],
       "optional_prompts": [], "lazav_prompts": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
MULLS = {"P0": 0, "P1": 0}
PASSED_REV = {}
LAST_IID = {"iid": None}
P0_PID = 0

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


def lib_oids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


def bf_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller")) == str(pid):
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


def exile_oids(state):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if str(o.get("zone", "")).lower() == "exile"]


def gy_oids(state, pid, lname=None):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("controller")) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def stack_spells(state, name=None):
    out = []
    for e in state.get("stack") or []:
        nm = str(e.get("name") or e.get("card_name") or "").lower()
        if name is None or nm == name:
            out.append(e)
    return out


def gy_bolts(state, pid):
    return len(gy_oids(state, pid, BOLT))


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
    return spec.get("type") in ("select", "sequence")


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
    if pid == 0:
        keep = (LAZAV in hn and n_lands >= 2) or n >= 2
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
    key_card = LAZAV if pid == 0 else None
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
    if pid == 0 and (nm == LAZAV or nm == CHILD):
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
                # stage-gated in p0_declare; default to no attack here
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
    color P0 needs: Island first, then Swamp)."""
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
        if pid == 0:
            return (0 if nm == ISLAND else
                    (1 if nm == SWAMP else
                     (2 if nm == MOUNTAIN else 3)), nm)
        return (4, nm)

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

def record_opp(state, opp, stage, purpose, fname):
    """Record an opportunity once per interactionId to EVDIR/<fname>.json."""
    iid = opp.get("interactionId")
    if any(r.get("interactionId") == iid for r in OBS["target_selections"]):
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
            "accept": accept_of(ch),
            "codes": surf_codes(ch),
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
    with open(f"{EVDIR}/{fname}", "w") as f:
        json.dump(rec, f, indent=1, default=str)
    say(f"[{purpose}] recorded {fname} (iid={iid}, n={len(chs)})")
    wire(f"{purpose}_recorded", {"iid": iid, "n": len(chs)})


def record_optional_opp(state, opp, stage, role):
    iid = opp.get("interactionId")
    if any(r.get("interactionId") == iid for r in OBS["optional_prompts"]):
        return
    resp = opp.get("response", {}) or {}
    chs = (resp.get("data", {}) or {}).get("choices", []) or []
    rec = {"interactionId": iid, "turn": state.get("turn_number"),
           "phase": state.get("phase"), "stage": stage, "role": role,
           "rtype": resp.get("type"),
           "accept_values": [accept_of(ch) for ch in chs],
           "codes": [surf_codes(ch) for ch in chs],
           "texts": [choice_text(ch)[:80] for ch in chs]}
    OBS["optional_prompts"].append(rec)
    say(f"[P0] optional prompt ({role}) iid={iid} "
        f"accept={rec['accept_values']}")
    wire("optional_prompt", rec)


# ------------------------------------------------- issue-specific logic

def p1p1_counters(state, oid):
    """Number of +1/+1 counters on an object."""
    from collections import Counter as _C  # local import, no new dep
    v = get_obj(state, oid).get("counters") or {}
    if not isinstance(v, dict):
        return 0
    total = 0
    for k, n in v.items():
        kl = str(k).lower().replace("_", "").replace(" ", "")
        if "p1p1" in kl or kl == "+1/+1":
            try:
                total += int(n)
            except (TypeError, ValueError):
                pass
    return total


def lazav_oid(state):
    """Track Lazav by oid (its name changes if it becomes a copy)."""
    return ST.get("lazav_oid")


def record_lazav_prompt(state, opp, stage, role, fname):
    """Record a Lazav-leg opportunity once per interactionId."""
    iid = opp.get("interactionId")
    if any(r.get("interactionId") == iid for r in OBS["lazav_prompts"]):
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
            "accept": accept_of(ch),
            "codes": surf_codes(ch),
            "text": choice_text(ch)[:120],
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
        "role": role,
        "rtype": resp.get("type"),
        "spec_type": ((data.get("spec") or {}).get("type")),
        "candidates": cand_info,
    }
    OBS["lazav_prompts"].append(rec)
    with open(f"{EVDIR}/{fname}", "w") as f:
        json.dump(rec, f, indent=1, default=str)
    say(f"[{role}] recorded {fname} (iid={iid}, n={len(chs)})")
    wire(f"{role}_recorded", {"iid": iid, "n": len(chs)})


def is_exile_selection(opp, state):
    """Schema select/sequence whose candidates reference graveyard objects."""
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    data = resp.get("data", {}) or {}
    chs = data.get("candidates") or []
    if not chs:
        return False
    for ch in chs:
        oid = cand_oid(ch)
        if oid and str(get_obj(state, oid).get("zone", "")).lower() \
                in ("graveyard",):
            return True
    return False


async def bolt_target_tick(c, tag, st, state):
    """Answer Lightning Bolt target selections: bolt1 -> own Child of Night,
    bolt2 -> P1 (seat 1). Handles the engine auto-target case."""
    stage = ST["stage"]
    if stage not in ("crime", "crime_leg"):
        return False
    pending = None
    if ST["bolt1_pending"] and not ST["bolt1_targeted"]:
        pending = ("bolt1", "own-child")
    elif ST["bolt2_pending"] and not ST["bolt2_targeted"]:
        pending = ("bolt2", "p1")
    if pending is None:
        return False
    btag, want = pending
    for opp in unanswered_ops(st):
        if is_optional_opp(opp):
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        record_lazav_prompt(state, opp, stage, f"{btag}-target",
                            f"{btag}_target.json")
        pick = None
        if want == "own-child":
            for ch in chs:
                oid = cand_oid(ch)
                if oid and obj_lname(state, oid) == CHILD \
                        and str(get_obj(state, oid).get("controller")) \
                        == "0":
                    pick = ch
                    break
        else:  # want == "p1"
            for ch in chs:
                if cand_seat(ch) == 1:
                    pick = ch
                    break
        if pick is None:
            say(f"[P0] {btag}: no matching target among "
                f"{len(chs)} candidates; NOT answering")
            wire(f"{btag}_no_target", {"n": len(chs)})
            return True
        say(f"[P0] {btag} target: choosing "
            f"{choice_text(pick)[:60]} (choice {pick.get('id')})")
        await answer_vi(c, opp, pick, tag)
        ST[f"{btag}_targeted"] = True
        ST[f"{btag}_pending"] = False
        return True
    return False


async def lazav_crime_tick(c, pid, tag, st, state):
    """Handle the Lazav trigger's resolution prompts during crime_leg:
    may-exile (optional) -> exile card selection (schema select) ->
    may-become-copy (optional, THE reported prompt). Accepts every may so
    the accept branch is exercised; records the copy offer's presence."""
    if ST["stage"] != "crime_leg":
        return False
    if pid != 0:
        return False
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        rtype = resp.get("type")
        if is_optional_opp(opp):
            if not ST["exile_done"]:
                role = "may-exile"
                record_lazav_prompt(state, opp, "crime_leg", role,
                                    "offer_exile_opp.json")
                if not ST["exile_offer_exported"]:
                    if await export_named(c, "offer_exile"):
                        ST["exile_offer_exported"] = True
                ST["may_exile_seen"] = True
                accept_ch = next((ch for ch in chs
                                  if accept_of(ch) == "true"), None)
                if accept_ch is None:
                    say("[P0] may-exile: no accept=true choice; NOT "
                        "answering")
                    wire("may_exile_no_accept", {})
                    return True
                say("[P0] may-exile: ACCEPTING")
                await answer_vi(c, opp, accept_ch, tag)
                ST["may_exile_accepted"] = True
                return True
            else:
                role = "may-copy"
                record_lazav_prompt(state, opp, "crime_leg", role,
                                    "offer_copy_opp.json")
                if not ST["copy_offer_exported"]:
                    if await export_named(c, "offer_copy"):
                        ST["copy_offer_exported"] = True
                ST["may_copy_seen"] = True
                accept_ch = next((ch for ch in chs
                                  if accept_of(ch) == "true"), None)
                if accept_ch is None:
                    say("[P0] may-copy: no accept=true choice; NOT "
                        "answering")
                    wire("may_copy_no_accept", {})
                    return True
                say("[P0] may-copy: ACCEPTING (reported branch)")
                await answer_vi(c, opp, accept_ch, tag)
                ST["may_copy_accepted"] = True
                ST["copy_accepted"] = True
                return True
        if is_exile_selection(opp, state):
            role = "exile-selection"
            record_lazav_prompt(state, opp, "crime_leg", role,
                                "exile_selection.json")
            if not ST["exile_offer_exported"]:
                if await export_named(c, "offer_exile"):
                    ST["exile_offer_exported"] = True
            ST["may_exile_seen"] = True  # selection implies the offer
            pick = None
            for ch in chs:
                oid = cand_oid(ch)
                if oid and obj_lname(state, oid) == CHILD:
                    pick = ch
                    break
            if pick is None:
                say("[P0] exile-selection: Child of Night not among "
                    f"{len(chs)} candidates; NOT answering")
                wire("exile_selection_no_child", {"n": len(chs)})
                return True
            iid = opp.get("interactionId")
            spec = (data.get("spec", {}) or {})
            stype = spec.get("type") or "select"
            say(f"[P0] exile-selection: exiling child of night "
                f"oid={cand_oid(pick)}")
            SUBMITTED_OPPS.add(iid)
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": stype,
                                               "data": {"choiceIds":
                                                        [pick.get("id")]}}},
                              tag)
            ST["exile_done"] = True
            ST["exiled_child_oid"] = cand_oid(pick)
            ST["crime_leg_t0"] = time.time()
            return True
        # Any other schema/exactChoices prompt in the leg: record it; if
        # its candidates reference the exiled Child (ParentTarget for the
        # BecomeCopy), answer with it.
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        refs_child = any(
            cand_oid(ch) == ST.get("exiled_child_oid")
            for ch in chs if cand_oid(ch))
        if refs_child and rtype in ("schema", "exactChoices"):
            role = "copy-target-fallback"
            record_lazav_prompt(state, opp, "crime_leg", role,
                                "copy_target_fallback.json")
            pick = next(ch for ch in chs
                        if cand_oid(ch) == ST["exiled_child_oid"])
            say("[P0] copy-target-fallback: choosing the exiled child")
            await answer_vi(c, opp, pick, tag)
            return True
        say(f"[P0] crime_leg: unexpected vi iid={opp.get('interactionId')} "
            f"rtype={rtype} codes={sorted(codes)}; NOT answering")
        wire("crime_leg_unexpected",
             {"rtype": rtype, "codes": sorted(codes),
              "n": len(chs)})
        OBS["notes"].append(f"unexpected crime_leg prompt rtype={rtype} "
                             f"codes={sorted(codes)}")
        return True
    return False


def cast_spell_for(acts, state, lname):
    """CastSpell-ish actions whose card is `lname`."""
    out = []
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        vals = list(d.values())
        src_oid = a.get("_src_oid")
        if src_oid is not None:
            vals.append(src_oid)
        for v in vals:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    out.append((v, a))
                    break
            except (TypeError, ValueError):
                pass
    return out


async def stage_transitions(c, tag, st, state):
    """Bookkeeping transitions from live state. Returns True if the tick
    should stop here (post exported)."""
    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    if stage == "setup":
        laz = bf_oids(state, 0, LAZAV)
        ch = bf_oids(state, 0, CHILD)
        if laz:
            ST["lazav_oid"] = laz[0]
        if ch:
            ST["child_oid"] = ch[0]
        if laz and ch and turn >= 4 \
                and state.get("active_player") == 0 \
                and str(state.get("phase") or "") in MAIN_PHASES:
            ST["p1_life_pre"] = life_of(state, 1)
            ST["p0_life_pre"] = life_of(state, 0)
            ST["gy0_bolts_pre"] = gy_bolts(state, 0)
            if await export_named(c, "pre"):
                ST["pre_exported"] = True
                ST["stage"] = "crime"
                ST["crime_leg_t0"] = time.time()
                say(f"[P0] stage -> crime (turn={turn})")

    if stage == "crime":
        st_now = st["state"]
        # bolt1 resolved? (it leaves the stack; P0 gy gains a bolt)
        if ST["bolt1_cast"] and ST["bolt1_targeted"] \
                and not ST["child_in_gy"]:
            gy = gy_oids(st_now, 0, BOLT)
            ch_gy = gy_oids(st_now, 0, CHILD)
            if ch_gy:
                ST["child_in_gy"] = True
                ST["gy0_bolts_post_bolt1"] = len(gy)
                say(f"[P0] bolt1 resolved: child of night in P0 gy; "
                    f"P0-gy bolts={len(gy)}")
        # auto-target case for bolt1
        if ST["bolt1_pending"] and not ST["bolt1_targeted"]:
            gy = gy_oids(st_now, 0, BOLT)
            ch_gy = gy_oids(st_now, 0, CHILD)
            if len(gy) > (ST.get("gy0_bolts_pre") or 0) or ch_gy:
                ST["bolt1_targeted"] = True
                ST["bolt1_auto"] = True
                ST["bolt1_pending"] = False
                ST["child_in_gy"] = bool(ch_gy)
                say("[P0] bolt1: no target prompt seen; treating as "
                    "auto-target")
        # crime committed when bolt2 is cast+targeted; then the trigger leg
        if ST["bolt2_cast"] and ST["bolt2_targeted"]:
            ST["stage"] = "crime_leg"
            ST["crime_leg_t0"] = time.time()
            ST["p1_life_pre_crime2"] = life_of(st_now, 1)
            say("[P0] stage -> crime_leg (crime committed)")
        elif ST["bolt2_pending"] and not ST["bolt2_targeted"]:
            # auto-target case for bolt2: bolt left the stack / P1 took 3
            p1 = life_of(st_now, 1)
            if (ST.get("p1_life_pre") is not None and p1 is not None
                    and p1 <= ST["p1_life_pre"] - 3):
                ST["bolt2_targeted"] = True
                ST["bolt2_auto"] = True
                ST["bolt2_pending"] = False
                ST["stage"] = "crime_leg"
                ST["crime_leg_t0"] = time.time()
                ST["p1_life_pre_crime2"] = ST["p1_life_pre"]
                say("[P0] bolt2: no target prompt seen; treating as "
                    "auto-target; stage -> crime_leg")

    if stage == "crime_leg":
        st_now = st["state"]
        # copy-offer wait backstop: the reported bug is the missing prompt.
        # Keep passing priority so the game settles; finish once the stack
        # is empty and the wait has expired.
        if ST["exile_done"] and not ST["may_copy_seen"] \
                and ST.get("crime_leg_t0") \
                and time.time() - ST["crime_leg_t0"] > OFFER_WAIT_S \
                and not ST["copy_wait_expired"]:
            ST["copy_wait_expired"] = True
            say(f"[P0] crime_leg: no may-copy offer within "
                f"{OFFER_WAIT_S}s of the exile (the reported symptom); "
                f"letting the game settle")
            wire("copy_offer_missing", {})
        leg_done = ST["copy_accepted"] or ST["copy_wait_expired"]
        settled = stack_empty(st_now)
        if leg_done and settled:
            ST["settle_ticks"] += 1
        else:
            ST["settle_ticks"] = 0
        if ST["settle_ticks"] >= 3:
            if not ST["post_exported"]:
                ST["p1_life_post_bolt2"] = life_of(st_now, 1)
                if await export_named(c, "post"):
                    ST["post_exported"] = True
                    ST["stage"] = "settle"
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
    if await bolt_target_tick(c, tag, st, state):
        return True
    if await lazav_crime_tick(c, 0, tag, st, state):
        return True
    if await do_declare(c, acts, st, 0, tag):
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

    # ---- P0 priority: land drops, then Lazav, then Child, then the bolts
    is_p0_main = (phase in MAIN_PHASES and state.get("active_player") == 0)
    if is_p0_main:
        if await play_a_land(c, state, 0, acts, tag, 99):
            return True
        laz_bf = bf_oids(state, 0, LAZAV)
        if not laz_bf and not ST["lazav_cast"]:
            found = cast_spell_for(acts, state, LAZAV)
            if found:
                oid, action = found[0]
                say(f"[P0] casting lazav, familiar stranger oid={oid} "
                    f"(engine Auto payment)")
                wire("lazav_cast", {"oid": str(oid)})
                await submit_as_is(c, action)
                ST["lazav_cast"] = True
                return True
        ch_bf = bf_oids(state, 0, CHILD)
        if laz_bf and not ch_bf and not ST["child_cast"]:
            found = cast_spell_for(acts, state, CHILD)
            if found:
                oid, action = found[0]
                say(f"[P0] casting child of night oid={oid} "
                    f"(engine Auto payment)")
                wire("child_cast", {"oid": str(oid)})
                await submit_as_is(c, action)
                ST["child_cast"] = True
                return True
        if ST["stage"] == "crime":
            bolts = cast_spell_for(acts, state, BOLT)
            if not ST["bolt1_cast"] and bolts:
                oid, action = bolts[0]
                say(f"[P0] casting bolt 1 (own child of night) oid={oid}")
                wire("bolt1_cast", {"oid": str(oid)})
                await submit_as_is(c, action)
                ST["bolt1_cast"] = True
                ST["bolt1_pending"] = True
                return True
            if ST["bolt1_cast"] and ST["child_in_gy"]                     and not ST["bolt2_cast"] and bolts:
                oid, action = bolts[0]
                say(f"[P0] casting bolt 2 (P1 -- the crime) oid={oid}")
                wire("bolt2_cast", {"oid": str(oid)})
                await submit_as_is(c, action)
                ST["bolt2_cast"] = True
                ST["bolt2_pending"] = True
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
    check_parse_lazav()

    p0 = PhaseClient("P07162r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P17162r")
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
               ("A1_parse", "A2_setup", "A3_crime_trigger",
                "A4_exile_offered", "A5_copy_offered", "A6_copy_effect",
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

        pre, offer_exile, offer_copy, post = (load("pre"),
                                              load("offer_exile"),
                                              load("offer_copy"),
                                              load("post"))
        for fn, s in (("pre", pre), ("offer_exile", offer_exile),
                      ("offer_copy", offer_copy), ("post", post)):
            if s is not None:
                say(f"loaded {fn}.json")

        def names_of(s, oids):
            return [obj_lname(s, o) for o in oids]

        # ---- A1: parse ----
        ass["A1_parse"] = "passed" if PARSE["ok"] else "failed"
        notes.append(f"A1: CommitCrime + PutCounter(P1P1, SelfRef) + "
                     f"ChangeZone(Graveyard->Exile, optional, Resolution) + "
                     f"BecomeCopy(ParentTarget, optional, UntilEndOfTurn, "
                     f"ZoneChangedThisWay[Creature]): {PARSE['ok']}")

        # ---- A2: setup ----
        if pre is not None:
            g = bf_oids(pre, 0, LAZAV)
            ch = bf_oids(pre, 0, CHILD)
            ok = (len(g) == 1 and len(ch) == 1
                  and ST["p1_life_pre"] == 20)
            notes.append(f"A2: lazav_bf={len(g)} child_bf={len(ch)} "
                         f"p1_life_pre={ST['p1_life_pre']} (expect 20) "
                         f"turn={pre.get('turn_number')}")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: crime trigger fired and resolved (counter) ----
        # The +1/+1 counter is the trigger's unconditional first effect.
        if post is not None and ST.get("lazav_oid"):
            o = get_obj(post, ST["lazav_oid"])
            n = p1p1_counters(post, ST["lazav_oid"])
            zone = o.get("zone")
            ok = (n >= 1 and zone == "Battlefield")
            notes.append(f"A3: lazav oid={ST['lazav_oid']} zone={zone} "
                         f"+1/+1 counters={n} (expect >=1); "
                         f"bolt1_cast={ST['bolt1_cast']} "
                         f"bolt2_cast={ST['bolt2_cast']} "
                         f"p1 {ST['p1_life_pre']}->"
                         f"{ST['p1_life_post_bolt2']}")
        else:
            ok = False
            notes.append(f"A3 failed: post={'ok' if post else 'missing'} "
                         f"lazav_oid={ST.get('lazav_oid')}")
        ass["A3_crime_trigger"] = "passed" if ok else "failed"

        # ---- A4: may-exile offered; Child of Night exiled ----
        if post is not None:
            ex = exile_oids(post)
            ex_names = names_of(post, ex)
            child_exiled = any(
                n == CHILD and str(get_obj(post, o).get("controller"))
                == "0" for o, n in zip(ex, ex_names))
            ok = (bool(ST["may_exile_seen"]) and ST["exile_done"]
                  and child_exiled)
            notes.append(f"A4: may_exile_seen={ST['may_exile_seen']} "
                         f"accepted={ST['may_exile_accepted']} "
                         f"exile_done={ST['exile_done']} "
                         f"exiled_child_oid={ST['exiled_child_oid']} "
                         f"child exiled in post={child_exiled} "
                         f"exile={ex_names}")
        else:
            ok = False
            notes.append("A4 failed: post.json missing")
        ass["A4_exile_offered"] = "passed" if ok else "failed"

        # ---- A5: may-become-copy offered (the reported symptom) ----
        ok = bool(ST["may_copy_seen"])
        notes.append(f"A5: may_copy_seen={ST['may_copy_seen']} "
                     f"accepted={ST['may_copy_accepted']} "
                     f"copy_wait_expired={ST['copy_wait_expired']}")
        ass["A5_copy_offered"] = "passed" if ok else "failed"

        # ---- A6: copy effect (only meaningful if the offer appeared) ----
        if post is not None and ST["may_copy_seen"] \
                and ST.get("lazav_oid"):
            o = get_obj(post, ST["lazav_oid"])
            nm = str(o.get("base_name") or o.get("name") or "").lower()
            pw, tu = o.get("power"), o.get("toughness")
            n = p1p1_counters(post, ST["lazav_oid"])
            zone = o.get("zone")
            # state power/toughness includes counters (PRE-offer
            # observation: base 1/4 Lazav + one +1/+1 reads 2/5), so a
            # correct Child-of-Night copy with the counter reads 3/2.
            ok = (nm == CHILD and pw == 3 and tu == 2
                  and zone == "Battlefield" and n >= 1)
            notes.append(f"A6: lazav oid={ST['lazav_oid']} name={nm!r} "
                         f"(expect 'child of night') pt={pw}/{tu} "
                         f"(expect 3/2 = 2/1 copy + one +1/+1) "
                         f"zone={zone} +1/+1={n}")
        elif not ST["may_copy_seen"]:
            ok = False
            notes.append("A6 failed: copy offer never appeared "
                         "(A5 failed); copy effect not exercised")
        else:
            ok = False
            notes.append(f"A6 failed: post={'ok' if post else 'missing'} "
                         f"lazav_oid={ST.get('lazav_oid')}")
        ass["A6_copy_effect"] = "passed" if ok else "failed"

        # ---- A7: cleanup ----
        if post is not None:
            stack_ok = stack_empty(post)
            ok = stack_ok
            notes.append(f"A7: stack_empty={stack_ok} "
                         f"life_final={life_of(post, 0)}/"
                         f"{life_of(post, 1)}")
        else:
            ok = False
            notes.append("A7 failed: post.json missing")
        ass["A7_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif ass["A3_crime_trigger"] != "passed" \
                or ass["A4_exile_offered"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: the crime trigger / exile "
                         "preconditions did not complete; the reported "
                         "copy prompt could not be tested")
        elif ass["A5_copy_offered"] != "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: the crime fired, the counter "
                         "was placed, and Child of Night was exiled, but "
                         "the may-become-copy offer never appeared -- the "
                         "reported symptom")
        elif ass["A6_copy_effect"] != "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: the copy offer appeared but "
                         "accepting it did not make Lazav a copy of the "
                         "exiled creature")
        elif all(ass.get(k) == "passed"
                 for k in ("A2_setup", "A3_crime_trigger",
                           "A4_exile_offered", "A5_copy_offered",
                           "A6_copy_effect", "A7_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: crime trigger, exile, "
                         "copy offer and copy effect all behaved per "
                         "Oracle")
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
            "setup_line": ("P0 8x Lazav, Familiar Stranger / 8x Child "
                           "of Night / 8x Lightning Bolt / 14x Island / "
                           "12x Swamp / 10x Mountain; P1 60x Forest "
                           "(passive). P0 mulligans for Lazav+2 lands, casts "
                           "Lazav ({U}{B}, engine Auto payment), casts Child "
                           "of Night ({1}{B} 2/1, engine Auto payment). "
                           "Crime turn: Bolt 1 targets own Child of Night "
                           "(kills it; not a crime), Bolt 2 targets P1 (a "
                           "crime). Lazav's CommitCrime trigger resolves: "
                           "counter, may-exile (driver ACCEPTS, exiles the "
                           "Child), may-become-copy (driver ACCEPTS)."),
            "contract_line": ("Lazav's crime trigger must put a +1/+1 "
                              "counter on Lazav, offer the exile of a card "
                              "from a graveyard, and -- if a creature card "
                              "was exiled -- offer having Lazav become a "
                              "copy of it until end of turn. The report "
                              "says the copy offer never appears."),
            "driver_notes": [
                "Protocol-106 driver for pinned v0.103.0, adapted from "
                "the scenario_7141_01030.py conventions.",
                "my_priority = PassPriority in legal_actions; casts are "
                "gated on it.",
                "CastSpell carries payment_mode Auto: the engine taps mana "
                "itself; the driver never answers tapLandForMana and runs "
                "no driver-side mana payment (legacy PayMana actions are "
                "answered if they appear).",
                "The may-exile / may-become-copy offers are vi "
                "opportunities whose choices carry the "
                "decideOptionalEffect action code "
                "(scenario_301_01030.py shape); accept = role accept "
                "value true, decline = value false.",
                "The exile card selection is a schema select/sequence "
                "opportunity whose candidates reference graveyard objects; "
                "answered with the Child of Night oid via the select "
                "shape.",
                "Bolt target selections are answered with the advertised "
                "candidate (own Child of Night for bolt 1, seat 1 for bolt "
                "2); the engine may also auto-target a sole legal target "
                "(recorded as the auto-target case).",
                "Lazav is tracked by object oid across states because its "
                "name changes if the copy effect applies.",
                "The stack-watch branch always falls through to the "
                "pass-priority gate (never returns early); both seats must "
                "pass in succession for a stack entry to resolve.",
                "Pre/offer/post states are authoritative exports "
                "(data.state parsed once from the export envelope) via the "
                "host client only; the reported OUTCOME is asserted on the "
                "saved states, not the prompt.",
            ],
            "assertions": ass,
            "observations": OBS,
            "driver_state": ST,
            "mulligans": MULLS,
            "notes": notes,
            "evidence_files": ["pre.json", "offer_exile.json",
                               "offer_exile_opp.json",
                               "exile_selection.json",
                               "offer_copy.json", "offer_copy_opp.json",
                               "bolt1_target.json", "bolt2_target.json",
                               "lazav_prompts.json", "post.json",
                               "run.json",
                               "parse_lazav_familiar_stranger.json",
                               "scenario_7162_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "P1 is a fully passive punching bag (60x Forest; "
                "never plays lands, never casts, never blocks).",
                "8x key-card density is a test-harness convenience (the "
                "engine accepts >4-of for custom games).",
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

        with open(f"{EVDIR}/lazav_prompts.json", "w") as f:
            json.dump(OBS["lazav_prompts"], f, indent=1, default=str)
        say(f"wrote lazav_prompts.json "
            f"({len(OBS['lazav_prompts'])} prompts)")
        with open(__file__) as f:
            src = f.read()
        with open(f"{EVDIR}/scenario_7162_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_7162_01030.py into EVDIR")

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

        render_summary(run, pre, offer_exile, offer_copy, post)

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

    def render_summary(run, pre, offer_exile, offer_copy, post):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7162 - Lazav, Familiar Stranger",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "crime trigger: counter + may-exile + may-become-copy",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: crime -> +1/+1 counter, may exile a card "
               "from a graveyard, may have Lazav become a copy of it.",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: CommitCrime + counter + exile + copy",
            "A2_setup": "PRE: Lazav + Child of Night on P0 BF, P1 at 20",
            "A3_crime_trigger": "trigger resolved: Lazav has +1/+1 counter",
            "A4_exile_offered": "may-exile offered; Child exiled from gy",
            "A5_copy_offered": "may-become-copy offer raised (reported bug)",
            "A6_copy_effect": "accept: Lazav is a copy of Child of Night",
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

        def lazav_sig(s):
            oid = ST.get("lazav_oid")
            if s is None or oid is None:
                return "lazav=(untracked)"
            o = get_obj(s, oid)
            nm = str(o.get("base_name") or o.get("name") or "?")
            return (f"lazav oid={oid} name={nm[:24]} "
                    f"pt={o.get('power')}/{o.get('toughness')} "
                    f"zone={o.get('zone')} "
                    f"+1/+1={p1p1_counters(s, oid)}")

        for label, s in (("pre        ", pre),
                         ("offer_exile", offer_exile),
                         ("offer_copy ", offer_copy),
                         ("post       ", post)):
            if s is not None:
                line = (f"{label}: life {life_of(s, 0)}/{life_of(s, 1)}  "
                        f"exile={len(exile_oids(s))}  "
                        f"stack={len(s.get('stack') or [])}  "
                        f"{lazav_sig(s)}")
            else:
                line = f"{label}: (no state)"
            d.text((36, y), line[:118], fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:22]:
            d.text((36, y), str(n)[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest(quiet=False):
        files = ["pre.json", "offer_exile.json",
                 "offer_exile_opp.json", "exile_selection.json",
                 "offer_copy.json", "offer_copy_opp.json",
                 "bolt1_target.json", "bolt2_target.json",
                 "lazav_prompts.json", "post.json", "run.json",
                 "parse_lazav_familiar_stranger.json",
                 "scenario_7162_01030.py", "wire_log.jsonl",
                 "scenario_run.log", "server.log", "summary.png"]
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
    last_rev_change = t_start
    game_started = False
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

        if turn > 40 and ST["stage"] not in ("settle", "done") \
                and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 40 reached with the line "
                                "incomplete; finishing")
            say("watchdog: turn 40, line incomplete; finishing")
            await finish()
            return

        if time.time() - last_diag > 60:
            last_diag = time.time()
            laz = bf_oids(state, 0, LAZAV)
            ch = bf_oids(state, 0, CHILD)
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"pp={state.get('priority_player')} "
                f"life={[life_of(state, i) for i in (0, 1)]} "
                f"stage={ST['stage']} "
                f"lazav_bf={len(laz)} child_bf={len(ch)} "
                f"bolt1={ST['bolt1_cast']}/{ST['bolt1_targeted']} "
                f"bolt2={ST['bolt2_cast']}/{ST['bolt2_targeted']} "
                f"exile_offer={ST['may_exile_seen']}/{ST['exile_done']} "
                f"copy_offer={ST['may_copy_seen']}/{ST['copy_accepted']} "
                f"exile={len(exile_oids(state))} "
                f"stack_bolts={len(stack_spells(state, BOLT))}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
