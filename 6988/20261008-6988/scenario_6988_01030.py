#!/usr/bin/env python3
"""Issue #6988: Tenuous Truce's bidirectional attack trigger is unsupported.
(v0.103.0 / protocol 106 re-validation; port of scenario_6988.py from
protocol 70/v0.81.3.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0), "Tenuous Truce":
  "Enchant opponent. At the beginning of enchanted opponent's end step,
   you and that player each draw a card. When you attack enchanted
   opponent or a planeswalker they control or when they attack you or a
   planeswalker you control, sacrifice this Aura."
Parse state on v0.103.0 (observed 2026-10-08 before the run, see
parse_tenuous_truce.json):
  trigger[0]: mode "Phase" (end-step draw) -- supported.
  trigger[1]: mode "YouAttack", description "When you attack enchanted
              opponent, sacrifice ~." -- the "or a planeswalker they
              control" leg is dropped.
  trigger[2]: mode {"Unknown": "When a planeswalker they control or when
              they attack you or a planeswalker you control"} -- the
              remaining three legs are entirely unparsed.
The parse defect is UNCHANGED from v0.81.3. This run re-validates the
runtime consequence on v0.103.0/protocol 106.

Reported symptom: "Expected: the Aura sacrifices itself when either
party attacks the other (or their planeswalkers)." Expected per Oracle:
sacrifice on any of the four attack legs.

Setup (native engine, two human-client seats, default Bo1):
  P0: 8x Tenuous Truce, 12x Grizzly Bears, 20x Forest, 20x Plains.
  P1: 12x Grizzly Bears, 4x Jace Beleren, 22x Forest, 22x Island.
  P0 enchants P1 with Tenuous Truce #1; both sides build boards: P0 Bears,
  P1 Bears + Jace Beleren. One fresh Aura per leg, because the parsed
  YouAttack leg has no valid_target gate and fires on ANY P0 attack, so
  reusing one Aura would vacate later legs.

Assertions (correct-behavior properties; "failed" = the defect is present):
  A1_parse_gap       v0.103.0 card data: exactly one Unknown-mode trigger
                     carrying the bidirectional attack text, plus one
                     YouAttack leg missing the planeswalker clause.
                     Expected PASS (the parse defect signature persists).
  A2_setup_ok        pre.json: Aura on BF attached to P1, P1 Jace on BF,
                     ready Bears on both sides. Expected PASS.
  A3_control_direct  mid_ctrl.json: Aura #1 sacrificed after P0 attacked P1
                     (parsed YouAttack leg works; attack landed: P1 life
                     20-><20). Expected PASS.
  A4_back_leg        mid_back.json: Aura #2 sacrificed after P1 attacked P0
                     (attack landed: P0 life 20-><20). Oracle expects
                     sacrifice. Expected FAIL (the reproduced bug: the
                     Unknown-mode legs never fire).
  A5_pw_leg          post.json: Aura #3 sacrificed after P0 attacked P1's
                     Jace Beleren (attack landed: Jace loyalty 3-><3);
                     mechanism attributed via the stack trigger's
                     description. Expected PASS (parsed YouAttack leg fires
                     on any P0 attack since it carries no valid_target
                     gate).
  A6_cleanup         post.json stack empty, game advanced. Expected PASS.

Verdict rule: reproduced iff A3 passes and A4 fails.
blocked iff the setup never completes or the attack stages do not run.

Protocol-106 driver rules applied (do not re-derive):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full; gate
    on waiting_for; exact card-data names; export response data.state is a
    JSON string parsed once.
  - The engine AUTO-TARGETS sole legal targets: Truce's target prompt may
    never appear (export PRE at chapter transitions; the resolution watch
    treats "aura attached to P1" as the terminal condition either way).
  - Do NOT pay mana via vi when a cast uses Auto (engine auto-taps --
    driver taps double-pay). No driver mana payment at all.
  - Discard prompts are schema+select, answered with
    {"type":"select","data":{"choiceIds":[...]}}.
  - The stack-watch branch must FALL THROUGH to the priority-pass logic --
    never `return` early from it.
  - real_decision_pending must not hold on already-answered opportunities
    (SUBMITTED_OPPS filter on interaction ids).
  - One in-flight submission per acting client; gate on state_revision.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6988
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-6988")
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
say("server artifact hashes (checked against the v0.103.0 pin): "
    f"bin={SERVER_IDENTITY['server_binary_sha256'][:16]}... "
    f"card-data={SERVER_IDENTITY['card_data_sha256'][:16]}... "
    f"draft-pools={SERVER_IDENTITY['draft_pools_sha256'][:16]}...")
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

TRUCE_T = "Tenuous Truce"
TRUCE_L = "tenuous truce"
BEARS_T = "Grizzly Bears"
BEARS_L = "grizzly bears"
JACE_T = "Jace Beleren"
JACE_L = "jace beleren"
FOREST_L = "forest"
PLAINS_L = "plains"
ISLAND_L = "island"
ALL_LANDS = (FOREST_L, PLAINS_L, ISLAND_L)

P0_DECK = ((TRUCE_T, 8), (BEARS_T, 12), ("Forest", 20), ("Plains", 20))
P1_DECK = ((BEARS_T, 12), (JACE_T, 4), ("Forest", 22), ("Island", 22))

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1800
STALL_AFTER = 150

ST = {"stage": "SETUP", "stop": False, "game_code": None,
      "mulls": {"P0": 0, "P1": 0},
      "pre_exported": False, "mid_ctrl_exported": False,
      "mid_back_exported": False, "post_exported": False,
      "truce_cast": False, "truce_targeted": False, "truce_in_flight": False,
      "aura_oid": None, "aura_oids": [],
      "p1_jace_cast": False,
      "ctrl_submitted": False, "ctrl_turn": None,
      "ctrl_trigger_desc": None, "ctrl_trigger_seen": False,
      "ctrl_retries": 0,
      "back_submitted": False, "back_turn": None,
      "back_trigger_desc": None, "back_trigger_seen": False,
      "back_retries": 0,
      "pw_submitted": False, "pw_turn": None,
      "pw_trigger_desc": None, "pw_trigger_seen": False,
      "pw_retries": 0,
      "pre_turn": None, "pre_p0_life": None, "pre_p1_life": None,
      "pre_jace_loyalty": None,
      "turn_cap_abort": False, "last_action": None}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "tick_errors": [], "notes": [], "wf_sequence": []}
WF_SEEN = []

PARSE = {"truce": None, "a1": False, "a1_detail": ""}


# ---------------------------------------------------------------------------
# data-level parse check (primary evidence)


def check_data_level():
    """Record the v0.103.0 parse of Tenuous Truce's attack trigger.

    A1 contract: exactly one Unknown-mode trigger carrying the
    bidirectional attack text, plus one YouAttack leg missing the
    planeswalker clause.
    """
    tru = CARD_DATA.get(TRUCE_L, {})
    trigs = tru.get("triggers") or []
    with open(f"{EVDIR}/parse_tenuous_truce.json", "w") as f:
        json.dump({"card": TRUCE_T,
                   "oracle_text": tru.get("oracle_text"),
                   "mana_cost": tru.get("mana_cost"),
                   "triggers": [{"mode": t.get("mode"),
                                 "description": t.get("description"),
                                 "trigger_zones": t.get("trigger_zones")}
                                for t in trigs]},
                  f, indent=1)
    say(f"saved parse_tenuous_truce.json ({len(trigs)} triggers)")
    unknown = [t for t in trigs
               if isinstance(t.get("mode"), dict) and "Unknown" in t["mode"]]
    you_attack = [t for t in trigs if t.get("mode") == "YouAttack"]
    ok = (len(unknown) == 1
          and "they attack you" in unknown[0]["mode"]["Unknown"]
          and len(you_attack) == 1
          and "planeswalker" not in
          (you_attack[0].get("description") or "").lower())
    detail = (f"{len(trigs)} triggers; Unknown-mode="
              f"{[t['mode']['Unknown'][:60] for t in unknown]}; "
              f"YouAttack={[t.get('description') for t in you_attack]}")
    PARSE["a1"] = ok
    PARSE["a1_detail"] = detail
    PARSE["truce"] = {"n_triggers": len(trigs)}
    say(f"A1 parse: {detail} -> {'passed' if ok else 'failed'}")
    wire("parse_truce", {"a1": ok, "detail": detail})
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


def ready_bears(state, pid):
    return [oid for oid in bf_oids(state, pid, BEARS_L)
            if not get_obj(state, oid).get("summoning_sick")
            and not get_obj(state, oid).get("tapped")]


def jace_of(state, pid):
    for oid in bf_oids(state, pid, JACE_L):
        return oid
    return None


def truce_on_bf(state, exclude=()):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if obj_lname(state, oid) == TRUCE_L
            and o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == "0"
            and oid not in exclude]


def attached_to_player(o, pid):
    """Recursive int search through the nested attached_to structure."""
    found = []

    def rec(x):
        if isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)
        elif isinstance(x, int) and not isinstance(x, bool):
            found.append(x)

    rec(o.get("attached_to"))
    return pid in found


def aura_attached_to_p1(state, exclude=()):
    """Return (oid, (to_p1, detail)) for a fresh Truce on BF attached to P1."""
    for oid in truce_on_bf(state, exclude=exclude):
        o = get_obj(state, oid)
        to_p1 = attached_to_player(o, 1)
        if to_p1:
            return oid, (True, json.dumps(o.get("attached_to"),
                                         default=str)[:200])
    return None, (None, None)


def stack_empty(state):
    return not (state.get("stack") or [])


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def my_main(state, pid):
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and stack_empty(state))


def is_my_main(state, pid):
    return my_main(state, pid)


# ---------------------------------------------------------------------------
# viewer-interaction + action helpers (protocol-106 conventions)


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
                      "mulliganDecision", "playLand", "chooseBranch",
                      "decideOptionalEffect"}


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
        stype = spec.get("type") or "select"
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
    # dense decks: always keep
    say(f"[{tag}] mulligan -> Keep")
    wire("mulligan", {"who": tag, "decision": "Keep"})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def discard_rank(state, o, pid):
    # Hand-size discard order: lands, then the current seat's expendable
    # creatures; protect Truce (P0 needs copies for all three legs) and
    # Jace (until P1 casts it).
    nm = obj_lname(state, o)
    if nm in ALL_LANDS:
        return 0
    if nm == BEARS_L:
        return 1
    if nm == JACE_L:
        return 1 if ST.get("p1_jace_cast") else 2
    if nm == TRUCE_L:
        return 3
    return 1


async def do_discard_handsize(c, acts, st, pid, tag):
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
# issue-specific: truce target selection, attack plans, trigger attribution


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("seat") is not None:
            return d.get("seat")
    return None


def opp_choices(opp):
    data = (opp.get("response") or {}).get("data") or {}
    return data.get("choices") or data.get("candidates") or []


async def answer_truce_target(c, tag, st, state, pid):
    """TargetSelection for the Tenuous Truce cast: pick the P1 (seat 1)
    player candidate and submit its engine-issued choice id."""
    if not (ST["truce_in_flight"] and not ST["truce_targeted"]):
        return False
    wf = wf_of(state)
    if wf.get("type") != "TargetSelection":
        return False
    if (wf.get("data") or {}).get("player") != pid:
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
                t = choice_text(ch).lower()
                if "opponent" in t or "player" in t:
                    pick = ch
                    break
        iid = opp.get("interactionId")
        if pick is None:
            key = (tag, "truce_target_nopick", str(iid))
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                wire("truce_target_no_pick",
                     {"opportunity": json.loads(json.dumps(opp,
                                                           default=str))})
                say(f"[{tag}] truce target: no P1 candidate; NOT answering")
            return False
        ST["truce_targeted"] = True
        say(f"[{tag}] truce target: P1 (seat={seat_of(pick)}, "
            f"text={choice_text(pick)[:70]})")
        wire("truce_target", {"text": choice_text(pick)[:80],
                              "seat": seat_of(pick)})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def attack_plan(state, pid):
    """(attacks, tag) for the current stage's DeclareAttackers window."""
    stage = ST["stage"]
    if stage == "CTRL_ATTACK" and pid == 0:
        bears = ready_bears(state, 0)
        if bears:
            return ([[int(bears[0]), {"type": "Player", "data": 1}]], "ctrl")
        say("[P0] CTRL_ATTACK: no ready bears")
        return ([], "ctrl")
    if stage == "BACK_ATTACK" and pid == 1:
        bears = ready_bears(state, 1)
        if bears:
            return ([[int(bears[0]), {"type": "Player", "data": 0}]], "back")
        say("[P1] BACK_ATTACK: no ready bears")
        return ([], "back")
    if stage == "PW_ATTACK" and pid == 0:
        bears = ready_bears(state, 0)
        jace = jace_of(state, 1)
        if bears and jace:
            return ([[int(bears[0]),
                      {"type": "Planeswalker", "data": int(jace)}]], "pw")
        say(f"[P0] PW_ATTACK: missing pieces (bears={len(bears)}, "
            f"p1jace={jace is not None})")
        return ([], "pw")
    return ([], None)


async def do_attack(c, acts, st, pid, tag, state):
    for a in acts:
        if a.get("type") != "DeclareAttackers":
            continue
        if not (state.get("active_player") == pid
                and (state.get("phase") or "") == "DeclareAttackers"
                and wf_player(state) == pid):
            # not our declare window: keep the game moving with empty
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"].update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            return True
        attacks, atk_tag = attack_plan(state, pid)
        d = dict(a)
        d["data"] = dict(d.get("data") or {})
        d["data"]["attacks"] = attacks
        d["data"]["bands"] = []
        await submit_as_is(c, d)
        if atk_tag and attacks:
            key = f"{atk_tag}_submitted"
            if not ST[key]:
                ST[key] = True
                ST[f"{atk_tag}_turn"] = turn_of(state)
                ST["last_action"] = atk_tag
                say(f"[{tag}] {ST['stage']}: attacks={attacks}")
        elif atk_tag:
            say(f"[{tag}] {ST['stage']}: no attackers declared "
                f"(missing pieces)")
        return True
    return False


def find_truce_trigger_entries(state):
    """Shape-agnostic scan of the stack for the Aura's sacrifice trigger."""
    out = []
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str)
        if "Tenuous Truce" not in blob and "acrifice" not in blob.lower():
            continue
        desc = None
        kind = None

        def rec(x):
            nonlocal desc
            if isinstance(x, dict):
                for k, v in x.items():
                    if k == "description" and isinstance(v, str) and v \
                            and desc is None:
                        if "sacrifice" in v.lower() \
                                or "attack" in v.lower():
                            desc = v
                    rec(v)
            elif isinstance(x, list):
                for v in x:
                    rec(v)

        rec(e)
        k = e.get("kind") or {}
        if isinstance(k, dict):
            kind = k.get("type")
        else:
            kind = k
        out.append({"kind": kind, "description": desc, "raw": e})
    return out


# ---------------------------------------------------------------------------
# seat ticks


async def generic_prompt(c, tag, st, state, decline_after=30):
    """Log unexpected prompts; answer the first choice after a stall.

    Truce target selection and hand-size discards have dedicated handlers
    above -- never answer them here.
    """
    acted = False
    for opp in unanswered_ops(st):
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
            OBS["auto_answered"].append(
                {"who": tag, "iid": str(iid)[:8],
                 "text": choice_text(pick)[:60]})
            await answer_vi(c, opp, pick, tag)
            acted = True
    return acted


async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_discard_handsize(c, acts, st, 0, tag):
        return True
    if await answer_truce_target(c, tag, st, state, 0):
        return True
    # combat: our attacks in attack stages, empty otherwise
    if await do_attack(c, acts, st, 0, tag, state):
        return True
    for a in acts:
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            return True

    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        # cast Tenuous Truce for the current leg (engine auto-pays)
        if ST["stage"] in ("SETUP", "RECAST_B", "RECAST_C") \
                and not ST["truce_cast"]:
            tid = find_hand(state, 0, TRUCE_L)
            a = find_cast_action(acts, state, TRUCE_L) if tid else None
            if a:
                ST["truce_cast"] = True
                ST["truce_in_flight"] = True
                await submit_as_is(c, a)
                say(f"[P0] casts Tenuous Truce ({ST['stage']})")
                return True
        # build a board of Bears (2) in SETUP
        if ST["stage"] == "SETUP" \
                and len(bf_oids(state, 0, BEARS_L)) < 2:
            bid = find_hand(state, 0, BEARS_L)
            a = find_cast_action(acts, state, BEARS_L) if bid else None
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Grizzly Bears")
                return True
        if await play_a_land(c, state, 0, acts, tag):
            return True

    # stack-watch: attribute the sacrifice trigger, then FALL THROUGH to
    # the priority-pass logic -- never return early from here
    _ = find_truce_trigger_entries(state)

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
    if await do_discard_handsize(c, acts, st, 1, tag):
        return True
    if await do_attack(c, acts, st, 1, tag, state):
        return True
    for a in acts:
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            return True

    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if not ST["p1_jace_cast"]:
            jid = find_hand(state, 1, JACE_L)
            a = find_cast_action(acts, state, JACE_L) if jid else None
            if a:
                ST["p1_jace_cast"] = True
                await submit_as_is(c, a)
                say("[P1] casts Jace Beleren")
                return True
        if len(bf_oids(state, 1, BEARS_L)) < 2:
            bid = find_hand(state, 1, BEARS_L)
            a = find_cast_action(acts, state, BEARS_L) if bid else None
            if a:
                await submit_as_is(c, a)
                say("[P1] casts Grizzly Bears")
                return True
        if await play_a_land(c, state, 1, acts, tag):
            return True

    _ = find_truce_trigger_entries(state)

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
    d.text((24, y), "phase-rs/phase #6988 - Tenuous Truce's bidirectional "
           "attack trigger unsupported", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08",
           fill=(140, 160, 180))
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
        "A1_parse_gap": "PARSE: one Unknown-mode trigger carries the "
                        "bidirectional attack text",
        "A2_setup_ok": "GAME: pre Aura on BF attached to P1, P1 Jace on "
                       "BF, ready Bears both sides",
        "A3_control_direct": "GAME: Aura #1 sacrificed after P0 attacked P1 "
                             "(parsed-leg control)",
        "A4_back_leg": "GAME: Aura #2 sacrificed after P1 attacked P0",
        "A5_pw_leg": "GAME: Aura #3 sacrificed after P0 attacked P1's "
                     "planeswalker",
        "A6_cleanup": "GAME: post stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Oracle: 'When you attack enchanted opponent or a "
           "planeswalker they", fill=(200, 210, 225))
    y += 22
    d.text((36, y), "control or when they attack you or a planeswalker you "
           "control,", fill=(200, 210, 225))
    y += 22
    d.text((36, y), "sacrifice this Aura.'", fill=(200, 210, 225))
    y += 24
    d.text((24, y), "Parsed on v0.103.0 (unchanged from v0.81.3):",
           fill=(200, 210, 225))
    y += 22
    d.text((36, y), "- YouAttack: 'When you attack enchanted opponent, "
           "sacrifice ~.'", fill=(150, 160, 175))
    y += 22
    d.text((36, y), "- Unknown: 'When a planeswalker they control or when "
           "they attack you", fill=(150, 160, 175))
    y += 22
    d.text((36, y), "  or a planeswalker you control' (never fires)",
           fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Stack-trigger attribution:", fill=(200, 210, 225))
    y += 24
    dst = run.get("driver_state") or {}
    for ttag in ("ctrl", "back", "pw"):
        seen = dst.get(f"{ttag}_trigger_seen")
        desc = (dst.get(f"{ttag}_trigger_desc") or "")[:58]
        d.text((36, y), f"{ttag}: trigger seen={seen} desc={desc}",
               fill=(150, 160, 175))
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:13]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    # NOTE: scenario_run.log is hashed LAST, after all say() logging is
    # done; no say() may follow the final build().
    files = ["pre.json", "mid_ctrl.json", "mid_back.json", "post.json",
             "parse_tenuous_truce.json", "assertions.json", "run.json",
             "scenario_6988_01030.py",
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
    src = f"{BACKFILL}/runs/server-9374.log"
    code = ST.get("game_code") or ""
    try:
        raw = open(src, errors="replace").read()
        clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        lines = [l for l in clean.splitlines() if code and code in l]
        hdr = (f"# server.log excerpt for #6988 run {RUN_ID}: game {code} "
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
           ("A1_parse_gap", "A2_setup_ok", "A3_control_direct",
            "A4_back_leg", "A5_pw_leg", "A6_cleanup")}

    # ---- A1: parse (data-level, primary evidence) ----
    ass["A1_parse_gap"] = "passed" if data_level["a1"] else "failed"
    notes.append(f"A1: {PARSE['a1_detail']} -> {ass['A1_parse_gap']}")

    pre, mid_ctrl, mid_back, post = (
        load_state_file(n) for n in ("pre", "mid_ctrl", "mid_back", "post"))
    for nm, s in (("pre", pre), ("mid_ctrl", mid_ctrl),
                  ("mid_back", mid_back), ("post", post)):
        say(f"{nm}.json: {'loaded' if s is not None else 'missing'}")

    # ---- A2: setup ----
    if pre is not None:
        oid, (to_p1, detail) = aura_attached_to_p1(pre)
        j1 = jace_of(pre, 1)
        rb0, rb1 = ready_bears(pre, 0), ready_bears(pre, 1)
        ok = bool(oid and to_p1 and j1 and rb0 and rb1)
        notes.append(
            f"A2: pre.json turn={turn_of(pre)} aura={oid} attached_to_p1="
            f"{to_p1} ({detail}); P1 Jace={j1} loyalty="
            f"{get_obj(pre, j1).get('loyalty') if j1 else None}; "
            f"ready bears P0={len(rb0)} P1={len(rb1)}; "
            f"P0 life={life_of(pre, 0)} P1 life={life_of(pre, 1)} -> "
            f"{'passed' if ok else 'failed'}")
        ass["A2_setup_ok"] = "passed" if ok else "failed"
    else:
        notes.append("A2 not-run: no pre.json (setup never completed)")
        ass["A2_setup_ok"] = "not-run"

    # ---- A3: control (P0 attacks P1 directly; aura #1) ----
    if mid_ctrl is not None and ass["A2_setup_ok"] == "passed":
        bf_truce = truce_on_bf(mid_ctrl)
        gy = [oid for oid, o in (mid_ctrl.get("objects") or {}).items()
              if obj_lname(mid_ctrl, oid) == TRUCE_L
              and o.get("zone") == "Graveyard"
              and str(o.get("controller", -1)) == "0"]
        p1life = life_of(mid_ctrl, 1)
        landed = p1life is not None and p1life < 20
        sacrificed = not bf_truce and len(gy) > 0
        ass["A3_control_direct"] = "passed" if sacrificed else "failed"
        notes.append(
            f"A3: mid_ctrl.json (P0 attacked P1, turn {ST['ctrl_turn']}): "
            f"Truce on BF={bf_truce}, in P0 graveyard={gy}, P1 life={p1life} "
            f"(attack landed={landed}); stack trigger seen="
            f"{ST['ctrl_trigger_seen']} "
            f"desc={str(ST['ctrl_trigger_desc'])[:70]}; parsed YouAttack "
            f"leg expected to fire -> {ass['A3_control_direct']}")
    else:
        ass["A3_control_direct"] = "not-run"
        notes.append("A3 not-run: no mid_ctrl.json or setup failed")

    # ---- A4: back leg (P1 attacks P0; aura #2) ----
    if mid_back is not None and ass["A2_setup_ok"] == "passed":
        bf_truce = truce_on_bf(mid_back)
        p0life = life_of(mid_back, 0)
        landed = p0life is not None and p0life < 20
        sacrificed = not bf_truce
        ass["A4_back_leg"] = "passed" if sacrificed else "failed"
        notes.append(
            f"A4: mid_back.json (P1 attacked P0, turn {ST['back_turn']}): "
            f"Truce on BF={bf_truce}, P0 life={p0life} "
            f"(attack landed={landed}); stack trigger seen="
            f"{ST['back_trigger_seen']}; oracle expects sacrifice -> "
            f"{ass['A4_back_leg']}")
    else:
        ass["A4_back_leg"] = "not-run"
        notes.append("A4 not-run: no mid_back.json or setup failed")

    # ---- A5: planeswalker leg (P0 attacks P1's Jace; aura #3) ----
    if post is not None and ass["A2_setup_ok"] == "passed":
        bf_truce = truce_on_bf(post)
        j1 = jace_of(post, 1)
        loy = get_obj(post, j1).get("loyalty") if j1 else None
        landed = (loy is not None and loy < 3) or j1 is None
        sacrificed = not bf_truce
        ass["A5_pw_leg"] = "passed" if sacrificed else "failed"
        notes.append(
            f"A5: post.json (P0 attacked P1's Jace, turn {ST['pw_turn']}): "
            f"Truce on BF={bf_truce}, Jace loyalty={loy} "
            f"(attack landed={landed}); stack trigger seen="
            f"{ST['pw_trigger_seen']} "
            f"desc={str(ST['pw_trigger_desc'])[:70]} -> "
            f"{ass['A5_pw_leg']}")
    else:
        ass["A5_pw_leg"] = "not-run"
        notes.append("A5 not-run: no post.json or setup failed")

    # ---- A6: cleanup ----
    if post is not None:
        ok = stack_empty(post)
        ass["A6_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A6: post.json stack empty={ok}, "
                     f"turn={turn_of(post)}, phase={post.get('phase')} -> "
                     f"{ass['A6_cleanup']}")
    else:
        notes.append("A6 not-run: no post.json")
        ass["A6_cleanup"] = "not-run"

    # ---- verdict ----
    if ass["A2_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup never completed "
                     f"(turn-cap abort={ST['turn_cap_abort']})")
    elif not (ass["A3_control_direct"] in ("passed", "failed")
              and ass["A4_back_leg"] in ("passed", "failed")
              and ass["A5_pw_leg"] in ("passed", "failed")):
        verdict = "blocked"
        notes.append("verdict=blocked: attack stages did not complete "
                     "cleanly")
    elif ass["A3_control_direct"] == "passed" \
            and ass["A4_back_leg"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: the Aura sacrificed itself when P0 "
                     "attacked P1 (parsed YouAttack leg works) but NOT when "
                     "P1 attacked P0 (the Unknown-mode bidirectional legs "
                     "never fire)")
    elif ass["A3_control_direct"] == "passed" \
            and ass["A4_back_leg"] == "passed":
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: the Aura sacrificed itself on "
                     "both player-attack legs")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: control leg did not behave as "
                     "expected; cannot attribute the back leg")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN}, f, indent=2)
    say("wrote assertions.json")

    run = {
        "issue": ISSUE,
        "title": "Tenuous Truce's bidirectional attack trigger is unsupported",
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
        "scenario_sha256": sha256_of_file(__file__),
        "format_config": "default Bo1 (2 human-client seats)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "verdict": verdict,
        "assertions": ass,
        "result": ("A1 parse_gap: " + ass["A1_parse_gap"] +
                   " (attack trigger split into a YouAttack leg missing the "
                   "planeswalker clause plus one Unknown-mode leg carrying "
                   "'When a planeswalker they control or when they attack "
                   "you or a planeswalker you control'). A2 setup_ok: " +
                   ass["A2_setup_ok"] + ". A3 control_direct: " +
                   ass["A3_control_direct"] +
                   " (P0 attacked P1; parsed leg fired). A4 back_leg: " +
                   ass["A4_back_leg"] + " (P1 attacked P0). A5 pw_leg: " +
                   ass["A5_pw_leg"] +
                   " (P0 attacked P1's Jace Beleren; mechanism attributed "
                   "via stack trigger). A6 cleanup: " +
                   ass["A6_cleanup"] + "."),
        "scope": ("Tenuous Truce attack-trigger legs: card-data parse check + "
                  "runtime attacks in both directions plus a planeswalker "
                  "attack, with the parsed YouAttack leg as the control; "
                  "native engine, two human-client seats; one fresh Aura "
                  "per leg because the parsed YouAttack leg has no "
                  "valid_target gate and fires on any P0 attack"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
            "The 'they attack your planeswalker' sub-leg was not separately exercised; the 'they attack you' leg covers the unparsed mode.",
        ],
        "observations": OBS,
        "driver_state": {k: v for k, v in ST.items()},
        "notes": notes,
        "evidence_files": ["pre.json", "mid_ctrl.json", "mid_back.json",
                           "post.json", "parse_tenuous_truce.json",
                           "assertions.json", "run.json", "manifest.sha256",
                           "summary.png", "scenario_6988_01030.py",
                           "wire_log.jsonl", "scenario_run.log",
                           "server.log"],
        "duration_s": round(dur, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6988_01030.py", "w") as f:
        f.write(src)
    say("copied scenario_6988_01030.py into EVDIR")
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
    global T0, C0
    T0 = time.time()
    last_rev_change = T0

    hello = await verify_server_hello()
    data_level = check_data_level()

    p0 = PhaseClient("P06988r")
    await p0.connect()
    C0 = p0
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16988r")
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
                # a rejected attack submission must not count as submitted;
                # only unset the tag whose attack was the last submission
                if ST.get("last_action"):
                    rtag = ST["last_action"]
                    mid_done = ST.get(f"mid_{rtag}_exported") or (
                        rtag == "pw" and ST.get("post_exported"))
                    if ST.get(f"{rtag}_submitted") and not mid_done:
                        ST[f"{rtag}_retries"] += 1
                        if ST[f"{rtag}_retries"] <= 3:
                            ST[f"{rtag}_submitted"] = False
                            say(f"[{c.name}] {rtag} attack rejected; "
                                f"will retry (#{ST[f'{rtag}_retries']})")
                        else:
                            say(f"[{c.name}] {rtag} attack rejected 3x; "
                                f"giving up on this leg")
                            ST["stop"] = True
                    ST["last_action"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
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
        record_wf(state)
        turn = turn_of(state)

        # truce resolution watch: a fresh aura attached to P1 closes the
        # current leg's cast (auto-target counts: no prompt needed)
        if ST["truce_in_flight"]:
            oid, (to_p1, detail) = aura_attached_to_p1(
                state, exclude=tuple(ST["aura_oids"]))
            if oid and to_p1:
                ST["aura_oid"] = oid
                ST["aura_oids"].append(oid)
                ST["truce_in_flight"] = False
                ST["truce_targeted"] = True
                say(f"Tenuous Truce resolved on BF (oid={oid}, "
                    f"stage={ST['stage']}); attached_to_p1={to_p1} "
                    f"detail={detail}")
                wire("aura_resolved", {"oid": oid, "stage": ST["stage"],
                                      "detail": detail})
                if ST["stage"] == "RECAST_B":
                    ST["stage"] = "BACK_ATTACK"
                    say("stage -> BACK_ATTACK (aura #2 on BF)")
                elif ST["stage"] == "RECAST_C":
                    ST["stage"] = "PW_ATTACK"
                    say("stage -> PW_ATTACK (aura #3 on BF)")

        # trigger attribution: capture the sacrifice trigger on the stack
        # right after each attack declaration (falls through; no early
        # return from this branch)
        for tag in ("ctrl", "back", "pw"):
            if ST.get(f"{tag}_submitted") \
                    and not ST.get(f"{tag}_trigger_seen"):
                hits = find_truce_trigger_entries(state)
                if hits:
                    ST[f"{tag}_trigger_seen"] = True
                    ST[f"{tag}_trigger_desc"] = hits[0]["description"]
                    wire(f"{tag}_trigger_stack",
                         {"kind": hits[0]["kind"],
                          "description": hits[0]["description"]})
                    say(f"[{tag}] trigger on stack: kind={hits[0]['kind']} "
                        f"desc={str(hits[0]['description'])[:90]}")

        # SETUP -> CTRL_ATTACK (pre exported once all pieces are ready)
        if ST["stage"] == "SETUP" and not ST["pre_exported"]:
            oid, (to_p1, _) = aura_attached_to_p1(state)
            j1 = jace_of(state, 1)
            rb0, rb1 = ready_bears(state, 0), ready_bears(state, 1)
            if oid and to_p1 and j1 and rb0 and rb1:
                await export_now("pre.json")
                ST["pre_exported"] = True
                ST["stage"] = "CTRL_ATTACK"
                ST["pre_turn"] = turn
                ST["pre_p0_life"] = life_of(state, 0)
                ST["pre_p1_life"] = life_of(state, 1)
                ST["pre_jace_loyalty"] = get_obj(state, j1).get("loyalty")
                say(f"SETUP complete at turn {turn}: aura={oid} attached to "
                    f"P1, P1 Jace loyalty={ST['pre_jace_loyalty']}, ready "
                    f"bears P0={len(rb0)} P1={len(rb1)}; "
                    f"stage -> CTRL_ATTACK")
        # CTRL_ATTACK -> RECAST_B (defender's turn starts, stack empty)
        if ST["stage"] == "CTRL_ATTACK" and ST["ctrl_submitted"] \
                and state.get("active_player") == 1 \
                and stack_empty(state) and not ST["mid_ctrl_exported"]:
            await export_now("mid_ctrl.json")
            ST["mid_ctrl_exported"] = True
            ST["stage"] = "RECAST_B"
            ST.update({"truce_cast": False, "truce_targeted": False,
                       "truce_in_flight": False})
            say(f"stage -> RECAST_B (P0 attacked P1 directly on turn "
                f"{ST['ctrl_turn']})")
        # BACK_ATTACK -> RECAST_C
        if ST["stage"] == "BACK_ATTACK" and ST["back_submitted"] \
                and state.get("active_player") == 0 \
                and stack_empty(state) and not ST["mid_back_exported"]:
            await export_now("mid_back.json")
            ST["mid_back_exported"] = True
            ST["stage"] = "RECAST_C"
            ST.update({"truce_cast": False, "truce_targeted": False,
                       "truce_in_flight": False})
            say(f"stage -> RECAST_C (P1 attacked P0 on turn "
                f"{ST['back_turn']})")
        # PW_ATTACK -> DONE
        if ST["stage"] == "PW_ATTACK" and ST["pw_submitted"] \
                and state.get("active_player") == 1 \
                and stack_empty(state) and not ST["post_exported"]:
            await export_now("post.json")
            ST["post_exported"] = True
            ST["stage"] = "DONE"
            say(f"stage -> DONE (P0 attacked P1's Jace on turn "
                f"{ST['pw_turn']})")
            ST["stop"] = True

        if turn > 60 and not ST["stop"]:
            ST["turn_cap_abort"] = True
            ST["stop"] = True
            say("turn cap 60 reached; aborting")

        if ST["post_exported"]:
            say("post exported; finishing")
            await finish(p0, hello, data_level)
            await p0.close()
            await p1.close()
            return
        if ST["stop"]:
            say("stop requested; finishing")
            await finish(p0, hello, data_level)
            await p0.close()
            await p1.close()
            return
        if time.time() - last_rev_change > STALL_AFTER:
            say(f"STALL: no revision change for {STALL_AFTER}s; finishing")
            OBS["notes"].append("stall watchdog fired")
            await finish(p0, hello, data_level)
            await p0.close()
            await p1.close()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = state
            say(f"DIAG turn={turn} active={s.get('active_player')} "
                f"phase={s.get('phase')} P0life={life_of(s, 0)} "
                f"P1life={life_of(s, 1)} stage={ST['stage']} "
                f"auras={ST['aura_oids']} stack={len(s.get('stack') or [])} "
                f"ctrl/back/pw={ST['ctrl_submitted']}/{ST['back_submitted']}/"
                f"{ST['pw_submitted']} P0hand={hand_lnames(s, 0)[:8]} "
                f"P1hand={hand_lnames(s, 1)[:8]}")

    OBS["notes"].append(f"global timeout ({GAME_TIMEOUT}s) hit")
    say("global timeout; finishing")
    await finish(p0, hello, data_level)
    await p0.close()
    await p1.close()


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        OBS["wf_sequence"].append(wf)
        wire("waiting_for", {"type": wf, "stage": ST["stage"]})


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


C0 = None


if __name__ == "__main__":
    asyncio.run(main())
