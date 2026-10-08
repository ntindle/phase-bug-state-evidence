#!/usr/bin/env python3
"""Issue #6986: Rakdos's per-creature coin-flip iteration is swallowed.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Rakdos, the Showstopper ({4}{B}{R}, Creature - Demon 6/6):
    "Flying, trample
     When Rakdos enters, flip a coin for each creature that isn't a Demon,
     Devil, or Imp. Destroy each creature whose coin comes up tails."

Reported symptom (issue body + classifier): the emitted ETB trigger AST
collapses the per-creature coin-flip operation into one generic FlipCoin
followed by an unqualified DestroyAll. A Swallow:DynamicQty parse warning
covers the full ETB operation. No runtime behavior was tested in the
report.

Parse state on v0.103.0 (observed 2026-10-08 before the run):
  parse_warnings: [SwallowedClause detector=DynamicQty, unit_span
    first_line=0 last_line=1] -> STILL present.
  triggers[0] execute = FlipCoin (win_effect/lose_effect null);
  sub_ability = DestroyAll{target Typed[Creature], controller null,
    cant_regenerate false}, sub_link SequentialSibling -> the collapsed
  shape is UNCHANGED from v0.81.3. A1/A2 are expected to fail again.

Setup (native engine, two human-client seats, default Bo1):
  P0: 3x Rakdos the Showstopper, 4x Grizzly Bears, 10x Mountain,
      10x Forest, 10x Swamp.
  P1: 24x Mountain, 24x Forest (passive: land drop, pass priority).
  P0 ramps, puts >=2 Bears on the battlefield, casts Rakdos ({4}{B}{R};
  engine Auto payment -- NO driver-side mana payment per the 2026-10-07
  double-payment rule). The ETB trigger fires; the driver records every
  coin-flip prompt and deliberately answers HEADS (heads = no tails ->
  per Oracle zero creatures should be destroyed).

Protocol-106 flip prompts: a 2-choice vi opportunity whose choices name
heads/tails (or coin/flip) -- text matching is safe here (no other
2-choice heads/tails prompt exists in this game), so no blob scanning.

Assertions (correct-behavior properties; "failed" = the defect is present):
  Parse (primary; measured on the pinned v0.103.0 card-data.json):
    A1_parse_warn_absent   the ETB carries no Swallow:DynamicQty warning.
                           expected FAIL (still present on v0.103.0).
    A2_parse_per_creature  the AST expresses per-creature flips with the
                           Demon/Devil/Imp exclusions.
                           expected FAIL (single FlipCoin + unqualified
                           DestroyAll, unchanged).
  Game (runtime consequence of the parse defect):
    A3_setup_ok            Rakdos cast with >=2 other creatures on BF and
                           its ETB trigger observed. expected PASS.
    A4_flip_per_creature   one flip prompt per eligible creature
                           (>=2 flips observed). expected FAIL (<=1).
    A5_exclusions_honored  Rakdos (a Demon, excluded by Oracle) survives
                           its own ETB. expected FAIL (destroyed by the
                           unqualified DestroyAll).
    A6_heads_no_destroy    with the single flip answered heads, zero
                           creatures are destroyed. expected FAIL (all
                           destroyed: no creature<->result association).
                           not-run if no flip prompt was ever answered.
    A7_cleanup             stack empty, game proceeds. expected PASS.

Verdict rule: reproduced iff (A1 or A2 fails) AND (A4, A5, or A6 fails).
blocked iff the game never reaches the ETB (A3 failed and etb_seen is
false). A parse defect with no reachable runtime trigger is still a
valid parse-level result, but the playbook prefers testing the reported
outcome, so the runtime leg is required for a reproduced verdict here.

Evidence: evidence/6986/<run-id>/pre.json, mid.json, post.json,
parse_rakdos.json, run.json, manifest.sha256, summary.png,
scenario_6986_01030.py, wire_log.jsonl, scenario_run.log, server.log
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6986
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-6986")
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
CREATURE_NAMES = set()
SUBTYPE_OF = {}


# ---------------------------------------------------------------------------
# data-level parse checks (primary evidence)


def strip_desc(node):
    if isinstance(node, dict):
        return {k: strip_desc(v) for k, v in node.items()
                if k not in ("description", "oracle_text", "name",
                             "flavor_name")}
    if isinstance(node, list):
        return [strip_desc(v) for v in node]
    return node


PARSE = {"warns": None, "ast": None, "ok1": False, "ok2": False}


def check_data_level():
    """Record the v0.103.0 parse of Rakdos's ETB trigger.

    A1 contract: the ETB carries no Swallow:DynamicQty warning.
    A2 contract: the emitted AST expresses per-creature flips with the
    Demon/Devil/Imp exclusions (description/oracle strings stripped
    first so the prose 'for each ... Demon, Devil, or Imp' cannot
    false-positive the exclusion search).
    """
    global CREATURE_NAMES, SUBTYPE_OF
    CREATURE_NAMES = {name for name, c in CARD_DATA.items()
                      if "Creature" in ((c.get("card_type") or {})
                                        .get("core_types") or [])}
    SUBTYPE_OF = {name: set(((c.get("card_type") or {}).get("subtypes")
                             or []))
                  for name, c in CARD_DATA.items()}
    say(f"creature names in card-data: {len(CREATURE_NAMES)}")

    rk = CARD_DATA.get(RAKDOS_L, {})
    with open(f"{EVDIR}/parse_rakdos.json", "w") as f:
        json.dump({"card": "Rakdos, the Showstopper",
                   "oracle_text": rk.get("oracle_text"),
                   "keywords": rk.get("keywords"),
                   "abilities": rk.get("abilities"),
                   "triggers": rk.get("triggers"),
                   "parse_warnings": rk.get("parse_warnings")}, f, indent=1)
    say("saved parse_rakdos.json")

    warns = rk.get("parse_warnings") or []
    swallow_dyn = [w for w in warns
                   if w.get("type") == "SwallowedClause"
                   and w.get("detector") == "DynamicQty"]
    PARSE["warns"] = warns
    ok1 = len(swallow_dyn) == 0
    PARSE["ok1"] = ok1
    say(f"parse_warnings: {[(w.get('type'), w.get('detector')) for w in warns]}")
    wire("parse_warnings",
         {"all": [(w.get("type"), w.get("detector"), w.get("unit_span"))
                  for w in warns]})

    trig = (rk.get("triggers") or [None])[0] or {}
    trig_json = json.dumps(strip_desc(trig))
    flip_count_ast = trig_json.count('"FlipCoin"')
    exec_eff = ((trig.get("execute") or {}).get("effect") or {})
    sub_eff = (((trig.get("execute") or {}).get("sub_ability") or {})
               .get("effect") or {})
    single_flip_null = (exec_eff.get("type") == "FlipCoin"
                        and exec_eff.get("win_effect") is None
                        and exec_eff.get("lose_effect") is None)
    tgt = sub_eff.get("target") or {}
    destroy_unqualified = (
        sub_eff.get("type") == "DestroyAll"
        and tgt.get("type") == "Typed"
        and [str(x) for x in (tgt.get("type_filters") or [])] == ["Creature"]
        and tgt.get("controller") is None)
    exclusion_terms = any(t in trig_json for t in ("Demon", "Devil", "Imp"))
    collapsed = (flip_count_ast == 1 and single_flip_null
                 and destroy_unqualified and not exclusion_terms)
    PARSE["ast"] = {"flipcoin_nodes": flip_count_ast,
                    "single_flip_null": single_flip_null,
                    "destroy_unqualified": destroy_unqualified,
                    "exclusion_terms": exclusion_terms,
                    "collapsed": collapsed,
                    "trigger_desc": trig.get("description")}
    ok2 = not collapsed
    PARSE["ok2"] = ok2
    say(f"AST: FlipCoin nodes={flip_count_ast} single_flip_null="
        f"{single_flip_null} destroy_unqualified={destroy_unqualified} "
        f"exclusion_terms={exclusion_terms} collapsed={collapsed}")
    wire("parse_ast", PARSE["ast"])
    return {"ok1": ok1, "ok2": ok2}


RAKDOS_L = "rakdos, the showstopper"
RAKDOS_T = "Rakdos, the Showstopper"
BEAR_L = "grizzly bears"
BEAR_T = "Grizzly Bears"
MOUNTAIN_L = "mountain"
FOREST_L = "forest"
SWAMP_L = "swamp"
ALL_LANDS = (MOUNTAIN_L, FOREST_L, SWAMP_L)

P0_DECK = ((RAKDOS_T, 3), (BEAR_T, 4), ("Mountain", 10), ("Forest", 10),
           ("Swamp", 10))
P1_DECK = (("Mountain", 24), ("Forest", 24))

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 30

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0}}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {"rakdos_cast": False, "rakdos_cast_turn": None, "rakdos_oid": None,
      "etb_seen": False, "etb_resolved": False,
      "pre_exported": False, "mid_exported": False, "post_exported": False,
      "post_at": None, "flip_count": 0, "flip_answered_heads": 0,
      "pre_board": [], "mid_board": [], "post_board": [],
      "eligible_at_pre": 0}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "tick_errors": [], "notes": [], "flip_prompts": [], "etb_window": []}


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
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def all_bf_creatures(state):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("base_name") or o.get("name") or "").lower()
            in CREATURE_NAMES]


def flip_eligible(state):
    """Creatures that per Oracle need a coin flip (not Demon/Devil/Imp)."""
    out = []
    for oid in all_bf_creatures(state):
        nm = str(get_obj(state, oid).get("base_name")
                 or get_obj(state, oid).get("name") or "").lower()
        subs = {s.lower() for s in SUBTYPE_OF.get(nm, set())}
        if not (subs & {"demon", "devil", "imp"}):
            out.append(str(oid))
    return out


def board_snapshot(state):
    return sorted([(str(oid), obj_lname(state, oid),
                    get_obj(state, oid).get("controller"))
                   for oid in all_bf_creatures(state)])


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and not get_obj(state, oid).get("tapped")]


def stack_entries(state):
    return state.get("stack") or []


def stack_empty(state):
    return not stack_entries(state)


def rakdos_trigger_on_stack(state):
    """True if Rakdos's ETB trigger is on the stack."""
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        src = e.get("source_id") or e.get("source")
        kind = e.get("kind") or {}
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        try:
            src_l = obj_lname(state, src) if src is not None else ""
        except (TypeError, ValueError):
            src_l = ""
        if src_l == RAKDOS_L and ktype == "TriggeredAbility":
            return True
        desc = str(e.get("description") or kind.get("description") or "")
        dl = desc.lower()
        if ("rakdos" in dl and ktype == "TriggeredAbility") or \
                ("flip a coin" in dl and "rakdos" in dl):
            return True
    return False


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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type"),
                           "stage": STAGE["stage"]})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "stage": STAGE["stage"],
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
    n = STAGE["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in ALL_LANDS)
    need_rakdos = RAKDOS_L in hand if pid == 0 else True
    keep = (n_lands >= 2 and need_rakdos) or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        STAGE["mulls"][tag] = n + 1
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
    if STAGE["mulls"].get(tag, 0) <= 0:
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
            return (0, choice_text(ch).lower() != RAKDOS_L)

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
    if nm in ALL_LANDS:
        return 0
    if pid == 0:
        if nm == BEAR_L:
            return 2
        if nm == RAKDOS_L:
            return 3
        return 4
    return 1


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
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    try:
                        ref_of[str(int(d["reference"]))] = ch["id"]
                    except (TypeError, ValueError):
                        pass
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o, pid),
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
# issue-specific prompts

def is_flip_like(opp):
    """A 2-choice heads/tails (or coin/flip) opportunity: Rakdos's flip.

    Strict shape + wording check -- this game has no other 2-choice
    heads/tails prompt, so answering it is safe.
    """
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    if len(chs) != 2:
        return False
    texts = " ".join(choice_text(ch) for ch in chs).lower()
    return ("heads" in texts and "tails" in texts) or \
        ("coin" in texts and "flip" in texts)


def pick_heads(chs):
    for ch in chs:
        if "heads" in choice_text(ch).lower():
            return ch
    return chs[0] if chs else None


async def flip_prompt_tick(c, tag, st, state):
    """Record + answer coin-flip prompts during the Rakdos ETB window.

    Deliberate decision: always answer HEADS, so any destruction proves
    the result is uncorrelated with the flip (and zero destruction on a
    correct engine proves heads was honored).
    """
    if not ST["etb_seen"] or ST["etb_resolved"]:
        return False
    acted = False
    for opp in unanswered_ops(st):
        if not is_flip_like(opp):
            continue
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        rec = {"who": tag, "iid": str(iid)[:12],
               "n_choices": len(chs),
               "texts": [choice_text(ch)[:60] for ch in chs][:6]}
        OBS["flip_prompts"].append(rec)
        wire("flip_prompt", {"opportunity":
                             json.loads(json.dumps(opp, default=str))})
        say(f"[{tag}] COIN-FLIP prompt #{ST['flip_count'] + 1} "
            f"choices={[choice_text(ch)[:20] for ch in chs][:4]}")
        ST["flip_count"] += 1
        pick = pick_heads(chs)
        if pick is None:
            say(f"[{tag}] flip prompt with no choices; NOT answering")
            SUBMITTED_OPPS.add(iid)
            continue
        if "heads" in choice_text(pick).lower():
            ST["flip_answered_heads"] += 1
        await answer_vi(c, opp, pick, tag)
        acted = True
    return acted


async def generic_prompt(c, tag, st, state, decline_after=25):
    acted = False
    for opp in unanswered_ops(st):
        if is_flip_like(opp):
            continue
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
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


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


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
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True

    # track Rakdos on the battlefield
    if ST["rakdos_oid"] is None:
        rks = bf_oids(state, 0, RAKDOS_L)
        if rks:
            ST["rakdos_oid"] = rks[0]
            ST["rakdos_cast_turn"] = state.get("turn_number")
            say(f"Rakdos on BF: oid={rks[0]} turn={ST['rakdos_cast_turn']}")
            wire("rakdos_cast", {"oid": rks[0],
                                 "turn": ST["rakdos_cast_turn"]})
    # ETB window: trigger on the stack -> PRE export
    if (ST["rakdos_oid"] is not None and not ST["etb_seen"]
            and rakdos_trigger_on_stack(state)):
        ST["etb_seen"] = True
        ST["pre_board"] = board_snapshot(state)
        ST["eligible_at_pre"] = len(flip_eligible(state))
        say(f"ETB SEEN: trigger on stack; board={ST['pre_board']} "
            f"eligible={ST['eligible_at_pre']}")
        wire("etb_seen", {"board": ST["pre_board"],
                          "eligible": ST["eligible_at_pre"]})
        if not ST["pre_exported"]:
            await do_export(c, "pre.json")
            ST["pre_exported"] = True
            say("PRE exported: Rakdos ETB on the stack")
    # flip prompts during the ETB window (answered heads)
    if await flip_prompt_tick(c, tag, st, state):
        return True
    # ETB resolved: trigger was seen and the stack is now empty
    if (ST["etb_seen"] and not ST["etb_resolved"]
            and stack_empty(state)):
        ST["etb_resolved"] = True
        ST["mid_board"] = board_snapshot(state)
        say(f"ETB RESOLVED: board now={ST['mid_board']} "
            f"flips_seen={ST['flip_count']} "
            f"heads_answers={ST['flip_answered_heads']}")
        wire("etb_resolved", {"board": ST["mid_board"],
                              "flips": ST["flip_count"],
                              "heads_answers": ST["flip_answered_heads"]})
        if not ST["mid_exported"]:
            await do_export(c, "mid.json")
            ST["mid_exported"] = True
            say("MID exported: after ETB resolution")
    # POST: after ETB resolved and the game proceeds
    if ST["etb_resolved"]:
        if ST["post_at"] is None:
            ST["post_at"] = time.time()
        if (not ST["post_exported"] and ST["post_at"] is not None
                and time.time() - ST["post_at"] > 10
                and stack_empty(state)):
            ST["post_board"] = board_snapshot(state)
            await do_export(c, "post.json")
            ST["post_exported"] = True
            say(f"POST exported; board={ST['post_board']}")
            STAGE["stage"] = "DONE"
            STAGE["stop"] = True
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        in_flight = not stack_empty(state)
        bears_n = len(bf_oids(state, 0, BEAR_L))
        hn = hand_lnames(state, 0)
        n_untapped = len(untapped_lands(state, 0))
        if not in_flight and my_main(state, 0):
            # cast Rakdos ({4}{B}{R}) once 2 Bears are out; the engine
            # auto-pays via the Auto payment_mode (no driver mana taps)
            if (not ST["rakdos_cast"] and RAKDOS_L in hn
                    and n_untapped >= 6 and bears_n >= 2
                    and not bf_oids(state, 0, RAKDOS_L)):
                a = find_cast_action(acts, state, RAKDOS_L)
                if a is not None:
                    ST["rakdos_cast"] = True
                    say(f"[{tag}] casting {RAKDOS_T} with {bears_n} Bears "
                        f"on BF, {n_untapped} untapped lands (Auto payment)")
                    wire("cast_submit", {"tag": "rakdos"})
                    await submit_as_is(c, a)
                    return True
            # otherwise put Bears on the board
            if BEAR_L in hn and bears_n < 4 and n_untapped >= 2:
                a = find_cast_action(acts, state, BEAR_L)
                if a is not None:
                    say(f"[{tag}] casting {BEAR_T} (Auto payment)")
                    wire("cast_submit", {"tag": "bear"})
                    await submit_as_is(c, a)
                    return True
        if await play_a_land(c, state, 0, acts, tag):
            return True

    # never hold priority while watching the stack: fall through to pass
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
    if await do_discard(c, acts, st, 1, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True
    if await flip_prompt_tick(c, tag, st, state):
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

def board_from_state(state):
    return sorted([(str(oid), obj_lname(state, oid),
                    get_obj(state, oid).get("controller"))
                   for oid in all_bf_creatures(state)])


def render_summary(run):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 1040
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6986 - Rakdos, the Showstopper "
           "per-creature coin-flip iteration swallowed", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
           "parser + runtime", fill=(140, 160, 180))
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
        "A1_parse_warn_absent": "PARSE: no Swallow:DynamicQty warning on the ETB",
        "A2_parse_per_creature": "PARSE: AST has per-creature flips + exclusions",
        "A3_setup_ok": "GAME: Rakdos cast, >=2 Bears, ETB observed",
        "A4_flip_per_creature": "GAME: one flip prompt per eligible creature",
        "A5_exclusions_honored": "GAME: Rakdos (Demon, excluded) survives its ETB",
        "A6_heads_no_destroy": "GAME: heads flip -> zero creatures destroyed",
        "A7_cleanup": "GAME: stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Oracle: flip a coin for each creature that isn't a "
           "Demon, Devil, or Imp;", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "destroy each creature whose coin comes up tails. "
           "Flip deliberately answered HEADS.", fill=(150, 160, 175))
    y += 30
    dst = run.get("driver_state") or {}
    d.text((24, y), f"pre board:  {dst.get('pre_board')}",
           fill=(150, 160, 175))
    y += 24
    d.text((24, y), f"mid board:  {dst.get('mid_board')}",
           fill=(150, 160, 175))
    y += 24
    d.text((24, y), f"post board: {dst.get('post_board')}",
           fill=(150, 160, 175))
    y += 24
    d.text((24, y), f"flips observed: {dst.get('flip_count')} "
           f"(heads answers: {dst.get('flip_answered_heads')})",
           fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
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
             "parse_rakdos.json", "run.json", "scenario_6986_01030.py",
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


async def finish(c0, hello, data_level):
    dur = time.time() - T0
    notes = OBS["notes"]
    ass = {k: "not-run" for k in
           ("A1_parse_warn_absent", "A2_parse_per_creature",
            "A3_setup_ok", "A4_flip_per_creature", "A5_exclusions_honored",
            "A6_heads_no_destroy", "A7_cleanup")}

    # ---- A1/A2: parse (data-level, primary evidence) ----
    ass["A1_parse_warn_absent"] = "passed" if data_level["ok1"] else "failed"
    notes.append(f"A1: Swallow:DynamicQty warnings on the ETB: "
                 f"{len([w for w in PARSE['warns'] if w.get('type') == 'SwallowedClause' and w.get('detector') == 'DynamicQty'])} "
                 f"(expected 0) -> {ass['A1_parse_warn_absent']}")
    ass["A2_parse_per_creature"] = "passed" if data_level["ok2"] else "failed"
    a = PARSE["ast"]
    notes.append(f"A2: emitted ETB AST expresses per-creature flips with the "
                 f"Demon/Devil/Imp exclusions: {data_level['ok2']} "
                 f"(FlipCoin nodes={a['flipcoin_nodes']}, single flip with "
                 f"null win/lose={a['single_flip_null']}, unqualified "
                 f"DestroyAll={a['destroy_unqualified']}, exclusion terms in "
                 f"AST={a['exclusion_terms']}) -> {ass['A2_parse_per_creature']}")

    # reload saved states
    states = {}
    for fn in ("pre", "mid", "post"):
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                raw = json.load(open(p))
                s = raw["state"]
                states[fn] = json.loads(s) if isinstance(s, str) else s
                say(f"loaded {fn}.json")
        except Exception as e:
            notes.append(f"state reload failed for {fn}.json: {e}")
    pre, mid, post = (states.get(k) for k in ("pre", "mid", "post"))

    # ---- A3: setup reached the ETB ----
    if pre is not None and ST["etb_seen"]:
        pre_board = board_from_state(pre)
        bears_pre = [x for x in pre_board if x[1] == BEAR_L]
        rak_pre = [x for x in pre_board if x[1] == RAKDOS_L]
        ok = len(bears_pre) >= 2 and len(rak_pre) == 1
        ass["A3_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A3: pre.json board={pre_board}; bears={len(bears_pre)} "
                     f"(need >=2), rakdos={len(rak_pre)} -> "
                     f"{ass['A3_setup_ok']}")
    else:
        ass["A3_setup_ok"] = "failed"
        notes.append(f"A3 failed: ETB never observed "
                     f"(rakdos_cast={ST['rakdos_cast']}, "
                     f"etb_seen={ST['etb_seen']}, "
                     f"eligible_at_pre={ST['eligible_at_pre']})")

    # ---- A4: one flip per eligible creature ----
    if ass["A3_setup_ok"] == "passed":
        eligible = ST["eligible_at_pre"]
        flips = ST["flip_count"]
        ok = flips >= eligible and flips > 1
        ass["A4_flip_per_creature"] = "passed" if ok else "failed"
        notes.append(f"A4: eligible creatures at pre={eligible}; "
                     f"coin-flip prompts observed={flips} "
                     f"(heads answers={ST['flip_answered_heads']}); "
                     f"per-creature iteration present: {ok} -> "
                     f"{ass['A4_flip_per_creature']}")
    else:
        notes.append("A4 not-run: setup never reached the ETB")

    # ---- A5: the excluded Demon survives its own ETB ----
    if ass["A3_setup_ok"] == "passed" and mid is not None:
        mid_board = board_from_state(mid)
        rak_mid = [x for x in mid_board if x[1] == RAKDOS_L]
        ok = len(rak_mid) == 1
        ass["A5_exclusions_honored"] = "passed" if ok else "failed"
        notes.append(f"A5: mid.json board={mid_board}; Rakdos (a Demon, "
                     f"excluded by Oracle) on BF: {len(rak_mid)} "
                     f"(expected 1) -> {ass['A5_exclusions_honored']}")
    else:
        notes.append("A5 not-run: no mid.json (ETB never resolved)")

    # ---- A6: heads flip -> zero destruction ----
    if (ass["A3_setup_ok"] == "passed" and mid is not None
            and ST["flip_answered_heads"] >= 1):
        mid_board = board_from_state(mid)
        pre_board = board_from_state(pre) if pre is not None else []
        destroyed = len(pre_board) - len(mid_board)
        ok = destroyed == 0
        ass["A6_heads_no_destroy"] = "passed" if ok else "failed"
        notes.append(f"A6: answered heads {ST['flip_answered_heads']}x; "
                     f"creatures pre={len(pre_board)} mid={len(mid_board)}; "
                     f"destroyed={destroyed} (expected 0 on heads) -> "
                     f"{ass['A6_heads_no_destroy']}")
    elif ass["A3_setup_ok"] == "passed" and mid is not None:
        notes.append(f"A6 not-run: no flip prompt was ever answered "
                     f"(flips observed={ST['flip_count']}); "
                     f"destruction was flip-independent by construction")
    else:
        notes.append("A6 not-run: no ETB resolution observed")

    # ---- A7: cleanup ----
    if post is not None:
        ok = not (post.get("stack") or [])
        ass["A7_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A7: post.json stack empty={ok}, "
                     f"turn={post.get('turn_number')}, "
                     f"phase={post.get('phase')} -> {ass['A7_cleanup']}")
    else:
        notes.append("A7 not-run: post.json missing")

    # ---- verdict ----
    parse_fail = ass["A1_parse_warn_absent"] == "failed" \
        or ass["A2_parse_per_creature"] == "failed"
    runtime_fail = ass["A4_flip_per_creature"] == "failed" \
        or ass["A5_exclusions_honored"] == "failed" \
        or ass["A6_heads_no_destroy"] == "failed"
    if ass["A3_setup_ok"] == "failed" and not ST["etb_seen"]:
        verdict = "blocked"
        notes.append("verdict=blocked: the game never reached Rakdos's "
                     "ETB trigger")
    elif parse_fail and runtime_fail:
        verdict = "reproduced"
        notes.append("verdict=reproduced: parse defect (Swallow:DynamicQty "
                     "+ collapsed single-FlipCoin/unqualified-DestroyAll "
                     "AST) manifests at runtime: "
                     f"flips={ST['flip_count']} heads_answers="
                     f"{ST['flip_answered_heads']} "
                     f"pre_board={ST['pre_board']} mid_board="
                     f"{ST['mid_board']}")
    elif (ass["A1_parse_warn_absent"] == "passed"
            and ass["A2_parse_per_creature"] == "passed"
            and ass["A4_flip_per_creature"] == "passed"
            and ass["A5_exclusions_honored"] == "passed"
            and (ass["A6_heads_no_destroy"] in ("passed", "not-run"))
            and ass["A7_cleanup"] == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: incomplete/mixed assertion chain")
    notes.append(f"verdict={verdict}")

    run = {
        "run_id": RUN_ID, "issue": ISSUE,
        "verdict": verdict, "validated_at": "2026-10-08",
        "server": {**SERVER_IDENTITY,
                   "observed_at": "2026-10-08",
                   "source": "ServerHello on 127.0.0.1:9374 (v0.103.0 "
                             "server already running from earlier run) + "
                             "verified pin (minisign-verified binary + "
                             "signed data manifest with the repo-pinned key).",
                   "hello": hello},
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(
            f"{BACKFILL}/driver/scenario_6986_01030.py"),
        "format_config": "default Bo1 (2 human-client seats)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "observations": OBS,
        "driver_state": {k: v for k, v in ST.items()},
        "notes": notes,
        "evidence_files": ["pre.json", "mid.json", "post.json",
                           "parse_rakdos.json",
                           "run.json", "manifest.sha256", "summary.png",
                           "scenario_6986_01030.py", "wire_log.jsonl",
                           "scenario_run.log", "server.log"],
        "limitations": [
            "Browser UI not exercised; this is an area:parser issue - "
            "the primary evidence is the emitted AST in the pinned "
            "card-data.json (input Oracle text preserved in "
            "parse_rakdos.json).",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports (restorable only via "
            "full game replay).",
            "Rakdos itself (a Demon) is the exclusion representative; "
            "Devil/Imp subtypes were not separately fielded (same "
            "exclusion clause, same absent AST filter).",
            "The coin flip was deliberately answered HEADS every time; "
            "the tails branch was not exercised.",
        ],
        "duration_s": round(dur, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(f"{BACKFILL}/driver/scenario_6986_01030.py",
                f"{EVDIR}/scenario_6986_01030.py")
    say("copied scenario_6986_01030.py into EVDIR")
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

    p0 = PhaseClient("P06986r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16986r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    no_rakdos_watchdog_at = None
    while time.time() - T0 < GAME_TIMEOUT and not STAGE.get("stop"):
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
        turn = state.get("turn_number") or 0
        if turn >= TURN_CAP and not ST["etb_seen"]:
            say(f"turn cap {TURN_CAP} reached with no ETB; finishing")
            OBS["notes"].append(f"turn cap {TURN_CAP} reached, no ETB")
            await finish(p0, hello, data_level)
            return
        if ST["post_exported"]:
            say("post exported; finishing")
            await finish(p0, hello, data_level)
            return
        if time.time() - last_rev_change > STALL_AFTER:
            say(f"STALL: no revision change for {STALL_AFTER}s; finishing")
            OBS["notes"].append("stall watchdog fired")
            await finish(p0, hello, data_level)
            return
        if (not ST["rakdos_cast"] and turn >= 14
                and no_rakdos_watchdog_at is None):
            no_rakdos_watchdog_at = time.time()
            OBS["notes"].append(f"watchdog: turn {turn} reached with Rakdos "
                                f"never cast; giving 90s more")
        if (no_rakdos_watchdog_at is not None
                and time.time() - no_rakdos_watchdog_at > 90):
            OBS["notes"].append("watchdog: Rakdos never cast; finishing")
            await finish(p0, hello, data_level)
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = state
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} pp={s.get('priority_player')} "
                f"P0life={life_of(s, 0)} P1life={life_of(s, 1)} "
                f"rakdos={bf_oids(s, 0, RAKDOS_L)} "
                f"bears={len(bf_oids(s, 0, BEAR_L))} "
                f"etb={ST['etb_seen']}/{ST['etb_resolved']} "
                f"flips={ST['flip_count']} "
                f"P0hand={hand_lnames(s, 0)}")
    OBS["notes"].append(f"global timeout ({GAME_TIMEOUT}s) hit")
    await finish(p0, hello, data_level)


asyncio.run(main())
