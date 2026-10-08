#!/usr/bin/env python3
"""Issue #7143: Torment of Hailfire x Bloodchief Ascension interaction.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Bloodchief Ascension ({B} Enchantment):
    "At the beginning of each end step, if an opponent lost 2 or more life
     this turn, you may put a quest counter on this enchantment.
     (Damage causes loss of life.)
     Whenever a card is put into an opponent's graveyard from anywhere, if
     this enchantment has three or more quest counters on it, you may have
     that player lose 2 life. If you do, you gain 2 life."
  Torment of Hailfire ({X}{B}{B} Sorcery):
    "Repeat the following process X times. Each opponent loses 3 life
     unless that player sacrifices a nonland permanent of their choice
     or discards a card."

Reported symptom: with Bloodchief Ascension at 3 quest counters, casting
Torment of Hailfire with X=10 raises NO Bloodchief triggers at all, even
though 10 cards move into the opponent's graveyard during resolution.
Expected: one may-trigger offer per card put into the opponent's graveyard.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 4x Bloodchief Ascension, 8x Bump in the Night, 4x Torment of
      Hailfire, 4x Dark Ritual, 40x Swamp.
  P1: 60x Memnite.
  P0 casts Ascension ASAP, then casts Bump in the Night targeting P1 once
  per turn until 3 quest counters (end-step may-offers accepted), then
  ramps to 12 untapped Swamps and casts Torment of Hailfire with X=10.
  P1 casts a Memnite on each of its own main phases (free creature,
  sacrifice fodder); each unless-pay rep answers sacrifice while a
  Memnite remains on P1's board, else discard, else decline.
  NOTE: the engine randomizes turn order; the driver keys everything on
  active_player/phase, never on turn order. In single-user mode seats are
  pids 0/1 in join order (P0=0, P1=1).

Assertions (each passed / failed / not-run):
  A1_setup        PRE: Ascension on P0 BF with exactly 3 quest counters,
                  >=10 Memnites on P1 BF, lives 20 / (20-3*bumps).
  A2_cast_x10     Torment cast with X=10 submitted and resolved to P0
                  graveyard.
  A3_ten_choices  10 UnlessPaymentChooseCost prompts offered to P1 (one per
                  rep).
  A4_sacrifices   all 10 answered "sacrifice"; P1 graveyard +10 Memnites,
                  P1 battlefield -10 Memnites.
  A5_triggers     Bloodchief may-trigger offers to P0 == graveyard moves
                  (10). THE REPORTED BUG: expect FAILED (0 offers).
  A6_effect       4 accepts -> P1 -8, P0 +8 (decline the rest).
  A7_cleanup      POST: stack empty, game continues.

Verdict rule (exact): reproduced iff A2 passed and (A3 or A4 or A5 or A6)
failed in the bug direction; not-reproduced iff all of A1-A7 passed;
blocked iff A1 or A2 failed.

Protocol-106 port notes (driver conventions from scenario_7079_01030.py):
  - my_priority = PassPriority in legal_actions; casts gated on it.
  - waiting_for is gone (null) on 106; every prompt arrives as a
    viewer_interaction opportunity.
  - CastSpell carries payment_mode Auto: the engine taps mana itself; the
    driver never answers tapLandForMana and runs no driver-side mana
    payment (legacy PayMana actions are answered if they appear).
  - Torment's ChooseX is answered X=10 via the schema-number shape
    (scenario_6983_01030.py shape); X=10 is CONFIRMED when the first
    unless-pay prompt arrives.
  - decideOptionalEffect exactChoices carry the may-offers: the
    end-step quest-counter offer and the "lose 2 life" trigger offer are
    discriminated by phase (end-step -> quest offer; anything else after
    the torment loop starts -> trigger offer). Decline = role "accept"
    value "false" (scenario_301_01030.py shape).
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).
  - Pre/post states are authoritative exports (data.state parsed once from
    the export envelope) via the host client only.

Evidence: evidence/7143/<run-id>/pre.json, post_torment.json, post.json,
run.json, parse_bloodchief_torment.json, scenario_7143_01030.py,
wire_log.jsonl, scenario_run.log, server.log, summary.png,
manifest.sha256, target_sel_*.json.
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
ISSUE = 7143
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7143")
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

PARSE = {"ok": False, "bloodchief": None, "torment": None}


def check_parse_7143():
    """Parse check: both cards present with expected oracle text / parses."""
    bc = CARD_DATA.get("bloodchief ascension", {})
    to = CARD_DATA.get("torment of hailfire", {})
    with open(f"{EVDIR}/parse_bloodchief_torment.json", "w") as fh:
        json.dump({"bloodchief_ascension": {
                       "oracle_text": bc.get("oracle_text"),
                       "mana_cost": bc.get("mana_cost"),
                       "abilities": bc.get("abilities"),
                       "static_abilities": bc.get("static_abilities")},
                   "torment_of_hailfire": {
                       "oracle_text": to.get("oracle_text"),
                       "mana_cost": to.get("mana_cost"),
                       "abilities": to.get("abilities"),
                       "static_abilities": to.get("static_abilities")}},
                  fh, indent=1, default=str)
    say("saved parse_bloodchief_torment.json")
    bc_or = str(bc.get("oracle_text") or "").lower()
    to_or = str(to.get("oracle_text") or "").lower()
    ok = ("quest counter" in bc_or and "lose 2 life" in bc_or
          and "sacrifices a nonland permanent" in to_or
          and "discards a card" in to_or)
    PARSE["ok"] = ok
    PARSE["bloodchief"] = bool(bc)
    PARSE["torment"] = bool(to)
    say(f"parse: bloodchief={PARSE['bloodchief']} torment={PARSE['torment']} "
        f"oracle_ok={ok}")
    wire("parse_check", {"oracle_ok": ok,
                         "bloodchief": PARSE["bloodchief"],
                         "torment": PARSE["torment"]})
    return ok


ASCENSION = "bloodchief ascension"
BUMP = "bump in the night"
TORMENT = "torment of hailfire"
RITUAL = "dark ritual"
SWAMP = "swamp"
MEMNITE = "memnite"
LANDS = (SWAMP,)

P0_DECK = [("Bloodchief Ascension", 4), ("Bump in the Night", 8),
           ("Torment of Hailfire", 4), ("Dark Ritual", 4), ("Swamp", 40)]
P1_DECK = [("Memnite", 60)]

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1800
STALL_AFTER = 150
TURN_CAP = 70

STOP = {"stop": False}
ST = {"stage": "setup", "stage_t0": 0.0, "ascension_cast": False,
      "bumps_cast": 0, "bump_in_flight": False, "bump_turn": -1,
      "quest_counters": 0, "quest_offers": 0, "quest_accepts": 0,
      "pre_exported": False, "post_torment_exported": False,
      "post_exported": False,
      "pre_p1_gy_memnites": 0, "pre_p1_bf_memnites": 0,
      "p1_life_pre_torment": None, "p0_life_pre_torment": None,
      "torment_cast": False, "torment_in_flight": False,
      "torment_resolved": False, "torment_on_stack_seen": False,
      "x_answered": False, "x_confirmed": False, "torment_x": None,
      "unless_prompts": 0, "unless_reps": [], "unless_loop_started": False,
      "unless_quiet_ticks": 0, "resolve_settle_t0": None,
      "bc_offers": 0, "bc_answered": 0, "bc_accepted": 0, "bc_log": [],
      "expected_triggers": None, "settle_t0": None,
      "land_turn": -1, "p1_mem_turn": -1, "game_code": None,
      "game_over": False, "x_shape_wired": False}
OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": [], "life_trace": [],
       "offer_scans": [], "unless_shapes": [], "optional_shapes": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
MULLS = {"P0": 0, "P1": 0}
PASSED_REV = {}
LAST_IID = {"iid": None}



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

# ------------------------------------------------------- 7143 helpers

def gy_oids(state, pid, lname=None):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("controller")) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def counters_of_kind(state, oid, kind_substr):
    o = get_obj(state, oid)
    total = 0
    ks = kind_substr.lower()
    c = o.get("counters")
    if isinstance(c, dict):
        for k, v in c.items():
            if ks in str(k).lower() and isinstance(v, (int, float)):
                total += int(v)
    elif isinstance(c, list):
        for e in c:
            if isinstance(e, dict):
                k = str(e.get("type") or e.get("kind")
                        or e.get("counter_type") or "")
                if ks in k.lower():
                    try:
                        total += int(e.get("count", e.get("n", 1)))
                    except Exception:
                        pass
            elif isinstance(e, str) and ks in e.lower():
                total += 1
    for k in ("quest_counters", "questCounters"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            total += int(v)
    return total


def cast_spell_for(acts, state, lname):
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


def torment_in_gy(state, pid=0):
    return any(gy_oids(state, pid, TORMENT))


def ascension_quest(state):
    asc = bf_oids(state, 0, ASCENSION)
    if not asc:
        return None, 0
    return asc[0], counters_of_kind(state, asc[0], "quest")


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
        keep = ((ASCENSION in hn or BUMP in hn or TORMENT in hn)
                and n_lands >= 1) or n >= 2
    else:
        keep = True  # 60x Memnite: always has the plan card
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
            if pid == 0:
                if nm == ASCENSION or nm == TORMENT:
                    return (3, str(ref))  # never bottom the key cards
                if nm == BUMP:
                    return (2, str(ref))
                if nm in LANDS:
                    return (0, str(ref))  # bottom lands first
                return (1, str(ref))
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


def discard_rank_7143(state, o, pid):
    nm = obj_lname(state, o)
    if pid == 1:
        return 0  # P1's hand is all Memnites; any discard is fine
    if nm == SWAMP:
        return 0
    if nm == RITUAL:
        return 1
    if nm == BUMP and (ST["quest_counters"] >= 3 or ST["bumps_cast"] >= 3):
        return 2
    if nm == ASCENSION and bf_oids(state, 0, ASCENSION):
        return 2
    if nm == TORMENT and (ST["torment_cast"] or
                          sum(1 for h in hand_lnames(state, 0)
                              if h == TORMENT) > 1):
        return 3
    return 6  # protect everything else


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
                        key=lambda o: (discard_rank_7143(state, o, pid),
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


# ------------------------------------------------------------- optional offers

def find_optional_opp(st):
    """decideOptionalEffect exactChoices opportunities (may abilities)."""
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                out.append(opp)
                break
    return out


def pick_accept_decline(opp, want_accept):
    chs = ((opp.get("response", {}) or {}).get("data", {}) or {}
           ).get("choices", [])
    for ch in chs:
        is_accept = None
        for sf in ch.get("surfaces", []) or []:
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if any(k in txt for k in ("accept", "yes", "pay", "cast")):
                is_accept = True
            elif any(k in txt for k in ("decline", "no", "don't", "do not")):
                is_accept = False
        if is_accept == want_accept:
            return ch
    return None


def is_end_step_phase(state):
    ph = str(state.get("phase") or "").lower()
    return "end" in ph


async def quest_counter_offer(c, st, state, tag):
    """Bloodchief's end-step 'you may put a quest counter' offer -> accept.

    Discrimination: the quest-counter offer fires at END STEP; the
    bloodchief graveyard may-offers fire during resolution (non-end-step
    phases). The opportunity itself is a decideOptionalEffect
    exactChoices in both cases."""
    for opp in find_optional_opp(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        if not is_end_step_phase(state):
            continue
        pick = pick_accept_decline(opp, True)
        if pick is None:
            continue
        if iid not in OBS["optional_shapes"]:
            OBS["optional_shapes"].append(iid)
            wire("quest_counter_offer_shape",
                 {"who": tag, "iid": iid, "phase": state.get("phase"),
                  "texts": [choice_text(ch)[:80] for ch in
                            ((opp.get("response", {}) or {}).get("data",
                             {}) or {}).get("choices", [])],
                  "opp": json.loads(json.dumps(opp, default=str))})
        ST["quest_offers"] += 1
        say(f"[{tag}] quest-counter offer #{ST['quest_offers']}: ACCEPT")
        await answer_vi(c, opp, pick, tag)
        ST["quest_accepts"] += 1
        return True
    return False


async def bloodchief_offer(c, st, state, tag):
    """Bloodchief's 'you may have that player lose 2 life' trigger offer.

    Only counted after the torment unless-pay loop started (i.e. not an
    end-step quest offer). Accept the first 4 (keep P1 alive), decline
    the rest."""
    if not ST["unless_loop_started"]:
        return False
    for opp in find_optional_opp(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        if is_end_step_phase(state):
            continue  # quest-counter offer; other handler owns it
        accept = ST["bc_accepted"] < 4 and (life_of(state, 1) or 0) > 3
        pick = pick_accept_decline(opp, accept)
        if pick is None:
            continue
        if iid not in OBS["optional_shapes"]:
            OBS["optional_shapes"].append(iid)
            wire("bloodchief_offer_shape",
                 {"who": tag, "iid": iid, "phase": state.get("phase"),
                  "turn": state.get("turn_number"),
                  "texts": [choice_text(ch)[:80] for ch in
                            ((opp.get("response", {}) or {}).get("data",
                             {}) or {}).get("choices", [])],
                  "opp": json.loads(json.dumps(opp, default=str))})
        ST["bc_offers"] += 1
        say(f"[{tag}] bloodchief trigger offer #{ST['bc_offers']}: "
            f"{'ACCEPT' if accept else 'DECLINE'}")
        wire("bc_offer", {"n": ST["bc_offers"], "accept": accept,
                          "p0_life": life_of(state, 0),
                          "p1_life": life_of(state, 1)})
        ST["bc_log"].append({"offer": ST["bc_offers"], "iid": iid,
                             "accept": accept,
                             "p0_life_before": life_of(state, 0),
                             "p1_life_before": life_of(state, 1)})
        await answer_vi(c, opp, pick, tag)
        ST["bc_answered"] += 1
        if accept:
            ST["bc_accepted"] += 1
        return True
    return False


# ------------------------------------------------------------- torment X

async def answer_x(c, st, tag):
    """ChooseXValue for Torment of Hailfire -> X = 10.

    106 shapes (per scenario_6983_01030.py): schema with spec.type
    "number", or exactChoices with a numbered choice."""
    if ST["x_answered"]:
        return False
    if not ST["torment_in_flight"]:
        return False
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        iid = opp.get("interactionId")
        blob = json.dumps(opp, default=str).lower()
        if "x" not in blob and "torment" not in blob \
                and resp.get("type") != "schema":
            continue
        if resp.get("type") == "schema" and spec.get("type") == "number":
            sub = {"interactionId": iid,
                   "response": {"type": "number", "data": {"value": 10}}}
            say(f"[{tag}] Torment X-choice -> X=10 (schema number)")
            wire("x_answer", {"iid": iid, "x": 10, "shape": "schema-number"})
            SUBMITTED_OPPS.add(iid)
            await interact_as(c, sub, tag)
            ST["x_answered"] = True
            ST["torment_x"] = 10
            return True
        if resp.get("type") == "exactChoices":
            chs = data.get("choices") or []
            pick = None
            for ch in chs:
                blob = (choice_text(ch) + " "
                        + json.dumps(ch, default=str)).lower()
                if re.search(r"\bx\s*=\s*10\b", blob) \
                        or blob.strip() == "10":
                    pick = ch
                    break
            if pick is None:
                continue
            say(f"[{tag}] Torment X-choice -> X=10 (exactChoices)")
            wire("x_answer", {"iid": iid, "x": 10, "shape": "exactChoices"})
            await answer_vi(c, opp, pick, tag)
            ST["x_answered"] = True
            return True
    return False


# ------------------------------------------------------------- bump target

def record_target_sel(state, opp, stage):
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
            "choice_id": ch.get("id"), "oid": oid, "seat": cand_seat(ch),
            "name": obj_lname(state, oid) if oid else choice_text(ch),
            "zone": o.get("zone"), "controller": o.get("controller"),
            "text": choice_text(ch)[:120]})
    rec = {"interactionId": iid, "turn": state.get("turn_number"),
           "phase": state.get("phase"), "stage": stage,
           "rtype": resp.get("type"),
           "spec_type": (data.get("spec") or {}).get("type"),
           "candidates": cand_info}
    OBS["target_selections"].append(rec)
    n = len(OBS["target_selections"])
    with open(f"{EVDIR}/target_sel_{n}.json", "w") as f:
        json.dump({"record": rec,
                   "opportunity": json.loads(json.dumps(opp, default=str))},
                  f, indent=1, default=str)
    say(f"target selection #{n} (stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']})"
                    for x in cand_info[:8]))
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "iid": iid})


async def bump_target(c, st, state, tag):
    """Answer Bump in the Night's TargetSelection with the P1 player."""
    if not ST["bump_in_flight"]:
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
        record_target_sel(state, opp, "bump-target")
        pick = next((ch for ch in chs if cand_seat(ch) == 1), None)
        if pick is None:
            pick = chs[0]
        say(f"[{tag}] Bump in the Night targets P1 (seat 1)")
        wire("bump_target_answer", {"choiceId": pick.get("id"),
                                   "seat": cand_seat(pick)})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


# ------------------------------------------------------------- unless-pay

def unless_branch_of(ch):
    """106 shape (wired 2026-10-08): exactChoices with a
    chooseUnlessCostBranch action code; value surfaces carry
    {"role": "costBranch", "value": "decline"} or
    {"role": "costBranchIndex", "value": "0"|"1"}.
    Torment's oracle order: "sacrifices a nonland permanent of their
    choice or discards a card" -> index 0 = sacrifice, index 1 = discard
    (confirmed live by the follow-up selection offering battlefield
    permanents for index 0 and hand cards for index 1)."""
    vals = {}
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") and "value" in d:
            vals[str(d["role"])] = str(d["value"])
    if vals.get("costBranch") == "decline":
        return "decline"
    idx = vals.get("costBranchIndex")
    if idx == "0":
        return "sacrifice"
    if idx == "1":
        return "discard"
    # text fallback (kept for robustness; the code shape above is primary)
    blob = (choice_text(ch) + " " + " ".join(vals.values())).lower()
    if "decline" in blob:
        return "decline"
    if "sacrif" in blob:
        return "sacrifice"
    if "discard" in blob:
        return "discard"
    if "lose" in blob or "don't pay" in blob or "do not pay" in blob:
        return "decline"
    return "unknown"


def is_unless_opp(opp):
    """An unless-pay prompt: exactChoices whose choices carry the
    chooseUnlessCostBranch action code."""
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "exactChoices":
        return False
    for ch in (resp.get("data", {}) or {}).get("choices", []):
        if "chooseUnlessCostBranch" in surf_codes(ch):
            return True
    return False


async def unless_branch(c, st, state, tag):
    """Answer Torment's unless-pay rep: sacrifice while a Memnite is on
    P1's battlefield, else discard while P1 has cards in hand, else
    decline (lose 3 life)."""
    for opp in unanswered_ops(st):
        if not is_unless_opp(opp):
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or []
        if not chs:
            continue
        iid = opp.get("interactionId")
        kinds = [unless_branch_of(ch) for ch in chs]
        if iid not in OBS["unless_shapes"]:
            OBS["unless_shapes"].append(iid)
            wire("unless_pay_branch_shape",
                 {"who": tag, "iid": iid, "rep": ST["unless_prompts"] + 1,
                  "n": len(chs), "kinds": kinds,
                  "texts": [choice_text(ch)[:60] for ch in chs],
                  "blobs": [(choice_text(ch) + " "
                             + " ".join(str((s.get('data') or {}).get('value'))
                                         for s in ch.get('surfaces', []))
                             )[:100] for ch in chs],
                  "opp": json.loads(json.dumps(opp, default=str))})
            say(f"[{tag}] unless-pay shape wired (rep "
                f"{ST['unless_prompts'] + 1}): kinds={kinds}")
        memnites = len(bf_oids(state, 1, MEMNITE))
        hand_n = len(hand_ids(state, 1))
        idx = {"sacrifice": None, "discard": None, "decline": None}
        for i, k in enumerate(kinds):
            if k in idx and idx[k] is None:
                idx[k] = i
        if idx["sacrifice"] is not None and memnites >= 1:
            pi, branch = idx["sacrifice"], "sacrifice"
        elif idx["discard"] is not None and hand_n >= 1:
            pi, branch = idx["discard"], "discard"
        elif idx["decline"] is not None:
            pi, branch = idx["decline"], "decline"
        else:
            pi, branch = 0, kinds[0] + "?fallback"
        ST["unless_prompts"] += 1
        ST["unless_loop_started"] = True
        ST["unless_quiet_ticks"] = 0
        ST["last_unless_tick"] = time.time()
        ST["unless_pending_followup"] = branch \
            if branch in ("sacrifice", "discard") else None
        ST["unless_reps"].append(
            {"rep": ST["unless_prompts"], "branch": branch, "kinds": kinds,
             "p1_bf_memnites": memnites, "p1_hand": hand_n,
             "p1_gy_memnites": len(gy_oids(state, 1, MEMNITE)),
             "p1_life": life_of(state, 1)})
        say(f"[{tag}] torment rep {ST['unless_prompts']}: branch={branch} "
            f"(kinds={kinds})")
        wire("unless_branch_answer",
             {"who": tag, "rep": ST["unless_prompts"], "branch": branch})
        await answer_vi(c, opp, chs[pi], tag)
        return True
    return False


async def cost_followup(c, st, state, tag):
    """Follow-up to a paid unless branch: choose WHICH memnite to
    sacrifice or WHICH card to discard."""
    if not ST.get("unless_pending_followup"):
        return False
    want = ST["unless_pending_followup"]
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        if "decideOptionalEffect" in codes or "passPriority" in codes:
            continue
        bf = set(str(o) for o in bf_oids(state, 1))
        hand = set(hand_ids(state, 1))
        mems = set(str(o) for o in bf_oids(state, 1, MEMNITE))
        sac_c = [ch for ch in chs if cand_oid(ch) in bf]
        dis_c = [ch for ch in chs if cand_oid(ch) in hand]
        if not sac_c and not dis_c:
            continue
        iid = opp.get("interactionId")
        if iid not in OBS["unless_shapes"]:
            OBS["unless_shapes"].append(iid)
            wire("unless_cost_followup_shape",
                 {"who": tag, "iid": iid, "want": want,
                  "n": len(chs),
                  "texts": [choice_text(ch)[:60] for ch in chs][:8],
                  "zones": [(cand_oid(ch),
                             get_obj(state, cand_oid(ch)).get("zone"))
                            for ch in chs][:8],
                  "opp": json.loads(json.dumps(opp, default=str))})
        if want == "sacrifice" and sac_c:
            mem_c = [ch for ch in sac_c if cand_oid(ch) in mems]
            pick = mem_c[0] if mem_c else sac_c[0]
            say(f"[{tag}] sacrifice follow-up: memnite "
                f"oid={cand_oid(pick)}")
            ST["unless_reps"][-1]["sacrificed_oid"] = cand_oid(pick)
        elif want == "discard" and dis_c:
            pick = dis_c[0]
            say(f"[{tag}] discard follow-up: oid={cand_oid(pick)}")
            ST["unless_reps"][-1]["discarded_oid"] = cand_oid(pick)
        else:
            continue
        wire("unless_followup_answer",
             {"who": tag, "want": want, "oid": cand_oid(pick)})
        await answer_vi(c, opp, pick, tag)
        ST["unless_pending_followup"] = None
        return True
    return False


# ------------------------------------------------------------- misc helpers

async def play_a_land(c, state, pid, acts, tag, target):
    if len(untapped_lands(state, pid)) >= target:
        return False
    cands = [a for a in acts if a.get("type") == "PlayLand"]
    if not cands:
        return False
    a = cands[0]
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
    if await answer_x(c, st, tag):
        return True
    if await bump_target(c, st, state, tag):
        return True
    if await quest_counter_offer(c, st, state, tag):
        return True
    if await bloodchief_offer(c, st, state, tag):
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
    is_p0_main = (phase in MAIN_PHASES and state.get("active_player") == 0)

    lives = (life_of(state, 0), life_of(state, 1))
    tr = OBS["life_trace"]
    if all(v is not None for v in lives) and (not tr or tr[-1][1] != lives):
        tr.append((round(time.time() - t_start, 1), lives))
        say(f"life = {lives}")
        wire("life", {"life": lives})

    # track bump resolution + live quest counters
    if ST["bump_in_flight"] and gy_oids(state, 0, BUMP):
        ST["bump_in_flight"] = False
        say(f"[P0] bump resolved (bumps_cast={ST['bumps_cast']})")
    asc_oid, quest = ascension_quest(state)
    ST["quest_counters"] = quest

    # stage transitions driven by state
    if ST["stage"] == "setup" and asc_oid is not None:
        ST["ascension_cast"] = True
        ST["stage"] = "counters"
        say("stage -> counters")
    if ST["stage"] == "counters" and quest >= 3:
        ST["stage"] = "ramp"
        say("stage -> ramp")
    if ST["stage"] == "ramp":
        # PRE is exported at the torment window (right before the cast),
        # after verifying quest==3 and >=10 P1 memnites on the board.
        if (my_priority(top_acts(st)) and stack_empty(state)
                and quest >= 3 and TORMENT in hand_lnames(state, 0)
                and len(untapped_lands(state, 0)) >= 12
                and len(bf_oids(state, 1, MEMNITE)) >= 10
                and not ST["torment_cast"]):
            found = cast_spell_for(acts, state, TORMENT)
            if not ST["pre_exported"]:
                if await export_named(c, "pre"):
                    ST["pre_exported"] = True
                    ST["p1_life_pre_torment"] = life_of(state, 1)
                    ST["p0_life_pre_torment"] = life_of(state, 0)
                    ST["pre_p1_gy_memnites"] = len(gy_oids(state, 1, MEMNITE))
                    ST["pre_p1_bf_memnites"] = len(bf_oids(state, 1, MEMNITE))
                    say(f"PRE exported: quest={quest} "
                        f"p1_bf_memnites={ST['pre_p1_bf_memnites']} "
                        f"life={ST['p0_life_pre_torment']}/"
                        f"{ST['p1_life_pre_torment']}")
                st = st_of(c) or st
                state = st["state"]
                acts = merged_actions(st)
                found = cast_spell_for(acts, state, TORMENT)
            if found:
                oid, action = found[0]
                ST["torment_cast"] = True
                ST["torment_in_flight"] = True
                ST["stage"] = "torment"
                say(f"[P0] cast Torment of Hailfire oid={oid} t{turn} "
                    f"(engine Auto payment)")
                wire("torment_cast", {"oid": str(oid), "turn": turn})
                await submit_as_is(c, action)
                return True
    if ST["stage"] == "torment":
        # X=10 is CONFIRMED only when the first unless-pay prompt
        # arrives (the cast entered resolution with X=10). If the
        # torment resolves with zero unless prompts despite the X=10
        # submission, that is recorded (A3 fails in that case).
        if ST["unless_prompts"] > 0 and not ST["x_confirmed"]:
            ST["x_confirmed"] = True
            ST["stage"] = "torment_resolve"
            say("[P0] X=10 confirmed (first unless-pay prompt arrived)")
        elif ST["torment_resolved"] and not ST["x_confirmed"] \
                and not ST.get("x_zero_prompts_noted"):
            ST["x_zero_prompts_noted"] = True
            ST["stage"] = "torment_resolve"
            say("[P0] torment resolved with ZERO unless-pay prompts "
                "despite X=10 submission")

    # --- torment loop-end: the spell reaching P0's graveyard is the
    # persistent signal; the stack zone proved unreliable on 106 (the
    # spell object left the Stack zone while the unless-pay loop was
    # still presenting prompts). Falls through to the pass-priority
    # gate; never returns early (deadlock lesson) ---
    if ST["torment_in_flight"]:
        on_stack = any(
            obj_lname(state, oid) == TORMENT
            for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Stack" and str(o.get("controller")) == "0")
        if on_stack and not ST["torment_on_stack_seen"]:
            ST["torment_on_stack_seen"] = True
            say("Torment of Hailfire seen on the stack")
            wire("torment_on_stack", {})
        if torment_in_gy(state) and not ST["torment_resolved"]:
            ST["torment_resolved"] = True
            ST["torment_in_flight"] = False
            say("Torment of Hailfire in P0 graveyard (resolution done)")
            wire("torment_resolved", {})
    if ST["stage"] == "torment_resolve":
        # loop end: torment resolved AND no unless prompt recently
        quiet = (time.time() - ST.get("last_unless_tick", 0)) > 20
        if ST["torment_resolved"] and quiet:
            if not ST["post_torment_exported"]:
                if await export_named(c, "post_torment"):
                    ST["post_torment_exported"] = True
                    n_gy = len(gy_oids(state, 1, MEMNITE))
                    ST["expected_triggers"] = \
                        n_gy - ST.get("pre_p1_gy_memnites", 0)
                    ST["pt_bc_offers_so_far"] = ST["bc_offers"]
                    say(f"post_torment exported: unless_prompts="
                        f"{ST['unless_prompts']} p1_gy_memnites={n_gy} "
                        f"expected_triggers={ST['expected_triggers']}")
            ST["stage"] = "triggers"
            ST["stage_t0"] = time.time()
            say("stage -> triggers")
    if ST["stage"] == "triggers":
        exp = ST["expected_triggers"]
        if exp is not None and exp > 0 and ST["bc_answered"] >= exp:
            ST["stage"] = "settle"
            ST["settle_t0"] = time.time()
            say(f"stage -> settle (bc_answered={ST['bc_answered']}/{exp})")
        elif exp == 0:
            ST["stage"] = "settle"
            ST["settle_t0"] = time.time()
            say("stage -> settle (no graveyard moves; nothing to trigger)")
        elif time.time() - ST["stage_t0"] > 90 and stack_empty(state):
            say(f"[P0] triggers quiet 90s: bc_answered={ST['bc_answered']} "
                f"expected={exp}; settling")
            ST["stage"] = "settle"
            ST["settle_t0"] = time.time()
    if ST["stage"] == "settle":
        if ST["settle_t0"] is None:
            ST["settle_t0"] = time.time()
        if stack_empty(state) and my_priority(top_acts(st)) \
                and time.time() - ST["settle_t0"] > 20:
            if not ST["post_exported"]:
                if await export_named(c, "post"):
                    ST["post_exported"] = True
            return True

    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # ---- P0 priority: land drops, then casts per stage, then pass ----
    if is_p0_main and stack_empty(state):
        if await play_a_land(c, state, 0, acts, tag, 99):
            return True
        hn = hand_lnames(state, 0)
        if ST["stage"] == "setup" and asc_oid is None and ASCENSION in hn:
            found = cast_spell_for(acts, state, ASCENSION)
            if found:
                oid, action = found[0]
                say(f"[P0] casting Bloodchief Ascension oid={oid} t{turn}")
                wire("ascension_cast", {"oid": str(oid), "turn": turn})
                await submit_as_is(c, action)
                return True
        if ST["stage"] == "counters" and quest < 3 \
                and not ST["bump_in_flight"] and BUMP in hn \
                and turn != ST["bump_turn"]:
            found = cast_spell_for(acts, state, BUMP)
            if found:
                oid, action = found[0]
                ST["bump_in_flight"] = True
                ST["bumps_cast"] += 1
                ST["bump_turn"] = turn
                say(f"[P0] casting Bump in the Night (#{ST['bumps_cast']}) "
                    f"oid={oid} t{turn}")
                wire("bump_cast", {"oid": str(oid), "turn": turn,
                                   "n": ST["bumps_cast"]})
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
    if await unless_branch(c, st, state, tag):
        return True
    if await cost_followup(c, st, state, tag):
        return True
    # legacy mana actions: answer if they appear. Driver never taps mana
    # itself.
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
    is_p1_main = (phase in MAIN_PHASES and state.get("active_player") == pid)

    # cast a Memnite whenever payable on own main phase (engine Auto
    # payment; Memnite is free). One per tick.
    if is_p1_main and stack_empty(state) and MEMNITE in hand_lnames(state, 1):
        found = cast_spell_for(acts, state, MEMNITE)
        if found:
            oid, action = found[0]
            say(f"[{tag}] casting Memnite oid={oid} "
                f"(bf={len(bf_oids(state, 1, MEMNITE))})")
            wire("memnite_cast", {"oid": str(oid)})
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

import re  # noqa: E402  (used by answer_x)


async def main():
    global t_start
    t_start = time.time()
    last_rev_change = t_start
    game_started = False
    ST["stage_t0"] = t_start

    hello = await verify_server_hello()
    check_parse_7143()

    p0 = PhaseClient("P07143r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P17143r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    async def finish():
        dur = time.time() - t_start
        notes = []
        ass = {k: "not-run" for k in
               ("A1_setup", "A2_cast_x10", "A3_ten_choices",
                "A4_sacrifices", "A5_triggers", "A6_effect", "A7_cleanup")}

        def load(fn):
            try:
                with open(f"{EVDIR}/{fn}.json") as f:
                    return json.load(f)["state"]
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
                return None

        pre = load("pre")
        post_torment = load("post_torment")
        post = load("post")
        if pre is not None:
            say("loaded pre.json")
        if post_torment is not None:
            say("loaded post_torment.json")
        if post is not None:
            say("loaded post.json")

        # ---- A1: setup ----
        if pre is not None:
            asc = bf_oids(pre, 0, ASCENSION)
            qc = counters_of_kind(pre, asc[0], "quest") if asc else -1
            p1mem = len(bf_oids(pre, 1, MEMNITE))
            l0, l1 = life_of(pre, 0), life_of(pre, 1)
            exp_l1 = 20 - 3 * ST["bumps_cast"]
            ok = (len(asc) == 1 and qc == 3 and p1mem >= 10
                  and l0 == 20 and l1 == exp_l1)
            notes.append(f"A1: asc_bf={len(asc)} quest={qc} "
                         f"p1_memnites_bf={p1mem} life={l0}/{l1} "
                         f"(expect 20/{exp_l1}; bumps={ST['bumps_cast']})")
        else:
            ok = False
            notes.append("A1 failed: pre.json missing")
        ass["A1_setup"] = "passed" if ok else "failed"

        # ---- A2: torment cast with X=10 submitted and resolved ----
        x_ok = ST["torment_x"] == 10 and ST["x_answered"]
        resolved = post_torment is not None and torment_in_gy(post_torment)
        ok = x_ok and resolved
        notes.append(f"A2: torment_cast={ST['torment_cast']} "
                     f"x_submitted={ST['torment_x']} "
                     f"x_answered={ST['x_answered']} "
                     f"x_confirmed={ST['x_confirmed']} "
                     f"resolved_to_gy={resolved}")
        ass["A2_cast_x10"] = "passed" if ok else ("not-run"
                                                  if not ST["torment_cast"]
                                                  else "failed")

        # ---- A3: 10 unless-pay prompts ----
        n_unless = ST["unless_prompts"]
        if ST["torment_cast"]:
            ok = n_unless == 10
            notes.append(f"A3: unless-pay prompts offered={n_unless} "
                         f"(expect 10); branches="
                         f"{[r['branch'] for r in ST['unless_reps']]}")
        else:
            notes.append("A3 not-run: torment never cast")
        ass["A3_ten_choices"] = ("passed" if ok else
                                 ("not-run" if not ST["torment_cast"]
                                  else "failed"))

        # ---- A4: 10 sacrifices moved memnites to P1 graveyard ----
        if pre is not None and post_torment is not None:
            gy_pre = len(gy_oids(pre, 1, MEMNITE))
            gy_pt = len(gy_oids(post_torment, 1, MEMNITE))
            bf_pre = len(bf_oids(pre, 1, MEMNITE))
            bf_pt = len(bf_oids(post_torment, 1, MEMNITE))
            sac_reps = sum(1 for r in ST["unless_reps"]
                           if r["branch"] == "sacrifice")
            ok = (gy_pt - gy_pre == 10 and bf_pre - bf_pt == 10
                  and sac_reps == 10)
            notes.append(f"A4: p1 gy memnites {gy_pre}->{gy_pt} "
                         f"(delta {gy_pt - gy_pre}, expect 10); "
                         f"p1 bf memnites {bf_pre}->{bf_pt} "
                         f"(delta {bf_pre - bf_pt}, expect -10); "
                         f"sacrifice reps={sac_reps}/10")
        else:
            ok = False
            notes.append("A4 not-run: pre/post_torment missing")
        ass["A4_sacrifices"] = ("passed" if ok else
                                ("not-run" if ass["A3_ten_choices"]
                                 == "not-run" else "failed"))

        # ---- A5: bloodchief trigger offers == graveyard moves ----
        exp = ST["expected_triggers"]
        if exp is not None and exp > 0:
            ok = ST["bc_offers"] == exp
            notes.append(f"A5: bloodchief may-offers={ST['bc_offers']} "
                         f"(expect {exp} = gy moves); "
                         f"answered={ST['bc_answered']} accepted="
                         f"{ST['bc_accepted']}")
        elif exp == 0:
            ok = ST["bc_offers"] == 0
            notes.append(f"A5: no graveyard moves (exp=0); "
                         f"bc_offers={ST['bc_offers']}")
        else:
            notes.append("A5 not-run: torment loop never completed")
        ass["A5_triggers"] = ("passed" if ok else
                              ("not-run" if exp is None else "failed"))

        # ---- A6: effect accounting (4 accepts -> P1 -8, P0 +8) ----
        if post is not None and ST["bc_answered"] > 0:
            l0 = life_of(post, 0)
            l1 = life_of(post, 1)
            exp_l0 = 20 + 2 * ST["bc_accepted"]
            exp_l1 = ST["p1_life_pre_torment"] - 2 * ST["bc_accepted"] \
                if ST["p1_life_pre_torment"] is not None else None
            ok = (l0 == exp_l0 and exp_l1 is not None and l1 == exp_l1
                  and ST["bc_accepted"] >= 1)
            notes.append(f"A6: accepted={ST['bc_accepted']} "
                         f"(declined={ST['bc_answered'] - ST['bc_accepted']}) "
                         f"life post={l0}/{l1} "
                         f"(expect {exp_l0}/{exp_l1})")
        elif post is not None and ST["bc_offers"] == 0 and exp == 0:
            ok = True
            notes.append("A6: vacuous (no triggers could exist)")
        else:
            ok = False
            notes.append("A6 not-run: no trigger offers answered / "
                         "post missing")
        ass["A6_effect"] = ("passed" if ok else
                            ("not-run" if ST["bc_answered"] == 0
                             and exp is None else "failed"))

        # ---- A7: cleanup ----
        if post is not None:
            stack_empty_ok = not (post.get("stack") or [])
            winner = post.get("winner")
            ok = stack_empty_ok and winner is None and not ST["game_over"]
            notes.append(f"A7: stack_empty={stack_empty_ok} winner={winner} "
                         f"game_over_flag={ST['game_over']}")
        else:
            ok = False
            notes.append("A7 failed: post.json missing")
        ass["A7_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A1_setup"] != "passed" or \
                ass["A2_cast_x10"] not in ("passed", "failed"):
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A1) or cast (A2) failed")
        elif ass["A2_cast_x10"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: torment never resolved")
        elif ass["A3_ten_choices"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: fewer than 10 unless-pay "
                         "prompts offered during X=10 Torment "
                         f"({ST['unless_prompts']} seen)")
        elif ass["A4_sacrifices"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: sacrifices chosen but "
                         "memnites did not move to graveyard as expected")
        elif ass["A5_triggers"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: cards moved to opponent's "
                         "graveyard during Torment but Bloodchief "
                         "Ascension produced "
                         f"{ST['bc_offers']} trigger offers vs "
                         f"{ST['expected_triggers']} moves")
        elif ass["A6_effect"] == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: trigger offers answered but "
                         "life totals did not change as expected")
        elif all(ass.get(k) == "passed" for k in
                 ("A1_setup", "A2_cast_x10", "A3_ten_choices",
                  "A4_sacrifices", "A5_triggers", "A6_effect",
                  "A7_cleanup")):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: 10 sacrifices each "
                         "raised a Bloodchief trigger; accepts moved "
                         "life as oracle text requires")
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
            "setup_line": ("P0 4x Bloodchief Ascension / 8x Bump in the "
                           "Night / 4x Torment of Hailfire / 4x Dark Ritual "
                           "/ 40x Swamp; P1 60x Memnite. P0 casts Ascension "
                           "ASAP, then one Bump per turn (target P1) until "
                           "3 quest counters (end-step may-offers "
                           "accepted). P0 ramps to 12 untapped Swamps, then "
                           "casts Torment X=10 (engine Auto payment; X=10 "
                           "via schema-number prompt). P1 casts a Memnite "
                           "each own main phase. Each unless-pay rep: "
                           "sacrifice a Memnite while any remain, else "
                           "discard, else decline. Bloodchief may-offers: "
                           "accept first 4 (P1 life>3), decline the rest."),
            "contract_line": ("With Bloodchief Ascension at 3 quest "
                              "counters, Torment of Hailfire X=10 should "
                              "raise one Ascension may-trigger per card put "
                              "into the opponent's graveyard (10 "
                              "sacrifices). Report: zero triggers offered. "
                              "Expected: A1-A4 pass, A5 fails in the bug "
                              "direction."),
            "driver_notes": [
                "Protocol-106 port of driver/scenario_7143.py (v0.82.0 / "
                "protocol 70); the behavioral contract, assertions A1..A7 "
                "and the verdict rule are unchanged.",
                "my_priority = PassPriority in legal_actions; casts gated "
                "on it.",
                "CastSpell carries payment_mode Auto on 106: the engine "
                "taps mana itself; the driver never answers tapLandForMana "
                "and runs no driver-side mana payment (legacy PayMana "
                "actions are answered if they appear).",
                "waiting_for is gone (null) on 106; all prompts arrive as "
                "viewer_interaction opportunities. decideOptionalEffect "
                "exactChoices with role=accept value=true/false carry the "
                "quest-counter and bloodchief may-offers; they are "
                "discriminated by phase (end-step -> quest offer, else -> "
                "bloodchief trigger offer).",
                "Torment's ChooseX is answered as X=10 via the "
                "schema-number shape (scenario_6983_01030.py shape); "
                "X=10 is confirmed when the first unless-pay prompt "
                "arrives.",
                "UnlessPaymentChooseCost arrives as exactChoices with a "
                "chooseUnlessCostBranch action code; value surfaces carry "
                "role=costBranch value=decline, or role=costBranchIndex "
                "value=0 (sacrifice) / value=1 (discard) per Torment's "
                "oracle order (\"sacrifices ... or discards\"); the "
                "follow-up selection's candidate zones (battlefield vs "
                "hand) confirm the mapping live. Paid branches have a "
                "schema-select follow-up (choose which permanent/card).",
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
            "evidence_files": ["pre.json", "post_torment.json", "post.json",
                               "run.json", "parse_bloodchief_torment.json",
                               "scenario_7143_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"]
                              + sorted(os.path.basename(p) for p in
                                       glob.glob(f"{EVDIR}/target_sel_*.json")),
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Dense test decks are a harness convenience (engine "
                "accepts >4-of for custom games); P1's 60x Memnite deck "
                "exists to supply 10 sacrifice fodder.",
                "P1 never attacks; P0 never attacks. Life changes come "
                "only from Bump in the Night, Torment unless-pay "
                "declines, and Bloodchief trigger accepts.",
                "Dark Ritual is deck filler and is never cast in this "
                "scenario; Torment's {X}{B}{B} is paid by the engine's "
                "auto-tap of 12 Swamps.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Turn order is randomized by the engine; the driver keys "
                "on active_player, not order.",
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
        with open(f"{EVDIR}/scenario_7143_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_7143_01030.py into EVDIR")

        # server.log: the pinned server on 127.0.0.1:9374 is shared;
        # excerpt lines for this game code from the owning run's log.
        gc = ST.get("game_code") or ""
        srv_src = None
        for cand in (f"{BACKFILL}/runs/{RUN_ID}/server.log",
                     f"{BACKFILL}/runs/20261008-7021/server.log"):
            if os.path.exists(cand):
                srv_src = cand
                break
        wrote = False
        if srv_src is not None:
            try:
                import re as _re
                with open(srv_src, "rb") as f:
                    raw = f.read().decode("utf-8", "replace")
                clean = _re.sub(r"\x1b\[[0-9;]*m", "", raw)
                excerpt = [ln for ln in clean.splitlines()
                           if gc and gc in ln]
                if excerpt:
                    with open(f"{EVDIR}/server.log", "w") as f:
                        f.write("\n".join(excerpt) + "\n")
                    say(f"copied {len(excerpt)} server.log lines for game "
                        f"{gc} into EVDIR")
                    wrote = True
            except Exception as e:
                notes.append(f"server.log excerpt failed: {e}")
        if not wrote:
            note = ("no per-game server.log lines available; the pinned "
                    "v0.103.0 server on 127.0.0.1:9374 was already "
                    "running (dedicated to this run); wire traffic is in "
                    "wire_log.jsonl, driver log in scenario_run.log")
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write(note + "\n")
            say("server.log: wrote note instead")

        render_summary(run, {"pre": pre, "post_torment": post_torment,
                             "post": post})

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

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1180
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7143 - Torment of Hailfire x "
               "Bloodchief Ascension", fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "X=10 torment vs 3-quest-counter Bloodchief",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 26
        labels = {
            "A1_setup": "ascension + 3 quest counters, >=10 memnites, "
                        "lives 20/11",
            "A2_cast_x10": "torment cast X=10, resolved to graveyard",
            "A3_ten_choices": "10 unless-pay prompts (one per rep)",
            "A4_sacrifices": "10 sacrifices -> 10 memnites to P1 graveyard",
            "A5_triggers": "bloodchief may-offers == graveyard moves",
            "A6_effect": "accepts move life correctly (P1 -2 / P0 +2 ea.)",
            "A7_cleanup": "game continues, stack empty",
        }
        for k, lab in labels.items():
            av = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if av == "passed" else (
                (255, 90, 90) if av == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {av}", fill=col)
            d.text((230, y), lab[:70], fill=(150, 160, 175))
            y += 26
        y += 10
        d.text((24, y), "Driver state:", fill=(200, 210, 225))
        y += 24
        ds = run.get("driver_state", {})
        for ln in [
            f"bumps_cast={ds.get('bumps_cast')} "
            f"quest: offers={ds.get('quest_offers')} "
            f"accepts={ds.get('quest_accepts')} "
            f"counters={ds.get('quest_counters')}",
            f"torment: cast={ds.get('torment_cast')} "
            f"x_confirmed={ds.get('x_confirmed')} "
            f"unless_prompts={ds.get('unless_prompts')} "
            f"branches={[r.get('branch') for r in (ds.get('unless_reps') or [])][:10]}",
            f"bloodchief: offers={ds.get('bc_offers')} "
            f"answered={ds.get('bc_answered')} "
            f"accepted={ds.get('bc_accepted')} "
            f"expected={ds.get('expected_triggers')}",
        ]:
            d.text((36, y), ln[:116], fill=(160, 175, 195))
            y += 24
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:14]:
            d.text((36, y), str(n)[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest(quiet=False):
        files = ["pre.json", "post_torment.json", "post.json", "run.json",
                 "parse_bloodchief_torment.json", "scenario_7143_01030.py",
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

        # game-over via winner or a player at <=0 life (a dead player
        # mid-resolution can leave the engine broadcasting no further
        # revisions; do not sit on it until the stall watchdog).
        winner = state.get("winner")
        lives = [life_of(state, i) for i in (0, 1)]
        if winner is not None or any(
                l is not None and l <= 0 for l in lives):
            ST["game_over"] = True
            OBS["notes"].append(f"game over: winner={winner} lives={lives}")
            say(f"GAME OVER winner={winner} lives={lives}")
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

        # early-exit watchdogs (do not let a stuck setup burn the budget)
        if turn > 30 and ST["stage"] == "setup" and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 30 with no Ascension; "
                                "finishing")
            say("watchdog: turn 30, no ascension; finishing")
            await finish()
            return
        if turn > 45 and ST["stage"] in ("counters", "ramp") \
                and not ST["torment_cast"] and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 45 with no Torment; "
                                "finishing")
            say("watchdog: turn 45, no torment; finishing")
            await finish()
            return

        if time.time() - last_diag > 60:
            last_diag = time.time()
            asc_oid, qc = ascension_quest(state)
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"stage={ST['stage']} "
                f"asc_bf={asc_oid is not None} quest={qc} "
                f"p1mem={len(bf_oids(state, 1, MEMNITE))} "
                f"lands0={len(untapped_lands(state, 0))} "
                f"life={[life_of(state, i) for i in (0, 1)]} "
                f"unless={ST['unless_prompts']} "
                f"bc={ST['bc_offers']}/{ST['bc_answered']} "
                f"torment={ST['torment_cast']}/{ST['torment_resolved']} "
                f"stack={len(state.get('stack') or [])}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
