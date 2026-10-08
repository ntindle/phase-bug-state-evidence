#!/usr/bin/env python3
"""Issue #6911: Entish Restoration doesn't prompt for sacrifice.

Reporter (Discord): "[[Entish Restoration]] went straight to selecting 3
lands, instead of prompting to sacrifice one first."

Oracle: "Sacrifice a land. Search your library for up to two basic land
cards, put them onto the battlefield tapped, then shuffle. If you control a
creature with power 4 or greater, instead search your library for up to
three basic land cards, put them onto the battlefield tapped, then shuffle."

Data (v0.103.0 card-data.json, verified in check_data_level): the parse is
CORRECT -- Spell ability with effect Sacrifice{count:1, target Land} and a
sub_ability gated by ConditionInstead (you control a creature with power
>= 4): SearchLibrary{up-to-3 basic lands} / else SearchLibrary{up-to-2}.
The sacrifice node is present at the data layer; the reported defect is at
resolution time (the engine offers the search without ever prompting the
sacrifice).

Protocol-106 port of driver/scenario_6911.py (v0.81.3 / protocol 70) for
pinned v0.103.0 (protocol 106). Conventions from scenario_6889_01030.py:
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [<name strings>]}.
  - waiting_for is gone (null): priority = advertised PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap; DiscardToHandSize
    via vi schema/select (the ONLY accepted submission for a select schema
    is {"type": "select", "data": {"choiceIds": [...]}}).
  - CastSpell via legacy Action; the v0.103.0 engine auto-taps reliably
    (payment_mode Auto); manual taps are a >90s fallback only (driver taps
    on top of engine auto-taps DOUBLE-PAY -- never tap for test casts).
  - real_decision_pending excludes the 106 priority-menu codes and any
    already-answered opportunity (answered selections clear from vi;
    holding on them deadlocks).
  - Resolution prompts are answered BEFORE the priority-pass gate; the
    pass gate always runs at the end of the tick (never hold priority
    while watching the stack).
  - Exports go through the host client only (P1 export is rejected).
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients; rejection-drain resync per tick.

Plan (native engine, v0.103.0 / protocol 106, two human-driver seats):
  P0: 12x Entish Restoration, 8x Baloth Gorger ({2}{G}{G} 4/4), 40x Forest.
      Plays a land each turn, casts Gorger when affordable, then casts
      Entish Restoration on a later turn.
  P1: 12x Grizzly Bears, 48x Forest. Plays a land each turn, occasionally
      casts bears, never attacks. Never interferes with P0's prompts.

Behavioral contract:
  A1 setup_ok        ER cast with a 4+ power creature (Baloth Gorger) on P0's
                     battlefield (pre.json exported at cast).
  A2 sacrifice_prompted  during resolution a choice to sacrifice one of P0's
                     own lands was advertised BEFORE any library-search
                     choice (the reported defect: search offered with no
                     sacrifice prompt first).
  A3 land_sacrificed  the land chosen at the sacrifice prompt left the
                     battlefield for the graveyard.
  A4 search_up_to_three  the search prompt advertised up to 3 basic lands
                     (4+ branch); the chosen lands entered tapped; library
                     shuffled; counts consistent.
  A5 cleanup         ER in P0 graveyard, stack empty, game proceeding.

Verdict: reproduced iff A2 fails (no sacrifice prompt, or search prompt
comes first); not-reproduced iff the reported path was exercised and every
assertion passed; blocked iff ER was never cast within the turn cap.
Never "fixed".
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6911
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261007-6911")
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
    "server_binary_sha256":
        "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc",
    "card_data_sha256":
        "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe",
    "draft_pools_sha256":
        "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd",
    "signature_verified": True,
}

for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")


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


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))


def check_data_level():
    """Record the v0.103.0 parse of Entish Restoration: the sacrifice node
    must be present (Sacrifice{1, Land} with a ConditionInstead
    sub_ability carrying the up-to-3 / up-to-2 SearchLibrary branches).
    The reported bug is at resolution time, not in the data."""
    c = CARD_DATA["entish restoration"]
    abils = c.get("abilities") or []
    sac = None
    for a in abils:
        eff = a.get("effect") or {}
        if eff.get("type") == "Sacrifice":
            sac = a
            break
    sub = (sac or {}).get("sub_ability") or {}
    cond = sub.get("condition") or {}
    search = sub.get("effect") or {}
    else_search = (sub.get("else_ability") or {}).get("effect") or {}
    ev = {
        "name": c.get("name"),
        "oracle_text": c.get("oracle_text"),
        "ability_count": len(abils),
        "sacrifice_effect": (sac or {}).get("effect"),
        "instead_condition_type": cond.get("type"),
        "search_effect": search,
        "else_search_effect": else_search,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
        json.dump({"name": c.get("name"),
                   "oracle_text": c.get("oracle_text"),
                   "abilities": abils}, f, indent=1)
    ok = (sac is not None
          and cond.get("type") == "ConditionInstead"
          and search.get("type") == "SearchLibrary"
          and else_search.get("type") == "SearchLibrary")
    say(f"data-level: sacrifice_node={'present' if sac else 'MISSING'} "
        f"instead_cond={cond.get('type')} "
        f"search_max={((search.get('count') or {}).get('max') or {})} "
        f"else_max={((else_search.get('count') or {}).get('max') or {})} "
        f"parse_ok={ok}")
    wire("data_level", {"sacrifice_present": sac is not None,
                        "instead_condition": cond.get("type"),
                        "search_type": search.get("type"),
                        "else_search_type": else_search.get("type"),
                        "parse_ok": ok})
    assert ok, "v0.103.0 card-data lost the Entish Restoration sacrifice node"


ER_T = "Entish Restoration"
ER_L = "entish restoration"
GORGER_T = "Baloth Gorger"
GORGER_L = "baloth gorger"
BEAR_T = "Grizzly Bears"
BEAR_L = "grizzly bears"
FOREST_T = "Forest"
FOREST_L = "forest"

P0_DECK = ((ER_T, 12), (GORGER_T, 8), (FOREST_T, 40))
P1_DECK = ((BEAR_T, 12), (FOREST_T, 48))

GAME_TIMEOUT = 2000
STALL_AFTER = 150
TURN_CAP = 40

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0}, "legend_answered": 0,
         "opp_shapes_logged": set()}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {"cast": None}
OBS = {"pre_exported": False, "er_cast": False, "er_cast_turn": None,
       "er_cast_oid": None, "gorger_cast": False,
       "sac_oid": None, "search_max": None, "search_chosen": [],
       "mid_sac_exported": False, "mid_search_exported": False,
       "post_exported": False, "rejections": [], "notes": [],
       "opp_seq": [], "stall_observed": False}
P0C = {"c": None}


def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def perm_oids(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def find_hand_oid(state, pid, lname):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == lname:
            return o
    return None


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


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
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")
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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_object_ref(cand):
    for s in (cand or {}).get("surfaces", []) or []:
        if s.get("type") == "object":
            return str((s.get("data") or {}).get("reference"))
    ref = _cand_reference(cand)
    return str(ref) if ref is not None else None


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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": STAGE["stage"]})
    await c.send_action(action)


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
    n_lands = sum(1 for h in hand if h == FOREST_L)
    if tag == "P0":
        choice = "Keep" if (ER_L in hand and n_lands >= 2) or n >= 2 \
            else "Mulligan"
    else:
        choice = "Keep" if (BEAR_L in hand and n_lands >= 1) or n >= 2 \
            else "Mulligan"
    if choice == "Mulligan":
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
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False
        keep = {ER_L, BEAR_L}

        def bkey(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else "?"
            if nm in keep:
                return (2, str(ref))
            if ref is not None and is_land(get_obj(state, ref)):
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


def p0_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln in (ER_L, GORGER_L):
        return 3
    if is_land(get_obj(state, o)):
        return 1
    return 0


def p1_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln == BEAR_L:
        return 2
    if is_land(get_obj(state, o)):
        return 1
    return 0


async def do_discard(c, acts, st, pid, tag, rank):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
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
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]
        ranked = sorted(hand, key=lambda o: (rank(state, o),
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


async def do_legend(c, acts, st, tag):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, a)
            STAGE["legend_answered"] += 1
            say(f"[{tag}] legend rule: keeps first (advertised action)")
            wire("legend", {"who": tag, "style": "action"})
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
            d = copy.deepcopy(a)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            d = copy.deepcopy(a)
            d.setdefault("data", {})["assignments"] = []
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
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    st = st_of(c)
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                ref = str(d.get("reference", ""))
                if ref in [str(x) for x in lands]:
                    iid = opp.get("interactionId")
                    key = (tag, "playland", str(iid), ref)
                    if key in SUBMITTED_OPPS:
                        continue
                    SUBMITTED_OPPS.add(key)
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                    wire("play_land_vi", {"who": tag, "oid": ref})
                    await answer_vi(c, opp, ch, tag)
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


async def pay_mana_vi(c, st, tag, needs):
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None,
                                                                "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        for ch, s in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in s:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used} (fallback)")
        wire("tap_land", {"who": tag, "used_for": used, "fallback": True})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def drive_mana(c, tag):
    cs = ACT.get("cast")
    st = st_of(c)
    state = st["state"]
    if cs and cs["in_flight"]:
        if any(obj_lname(state, o) == cs["lname"] and
               get_obj(state, o).get("zone") in ("Stack", "Battlefield")
               for o in (state.get("objects") or {})):
            cs["in_flight"] = False
            return False
        if time.time() - cs["submit_t"] < 90:
            return False
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) <= 0:
            return False
        if await pay_mana_vi(c, st, tag, needs):
            cs["taps"] += 1
            say(f"[{tag}] FALLBACK manual tap #{cs['taps']} for {cs['tag']} "
                f"(auto-tap did not fire in 90s)")
            wire("manual_tap", {"tag": cs["tag"], "taps": cs["taps"],
                                "needs": dict(needs), "fallback": True})
            return True
        return False
    return False


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------- issue-specific logic


def classify_resolution_op(op, state, pid):
    """Label a P0 ER-resolution opportunity: sacrifice | search | other.

    sacrifice: candidates are P0-controlled Battlefield lands.
    search:    candidates are Library-zone basic lands.
    """
    resp = op.get("response") or {}
    rtype = resp.get("type")
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    cands = data.get("candidates") or data.get("choices") or []
    refs = []
    for ch in cands:
        ref = _cand_reference(ch)
        entry = {"choice_id": ch.get("id"),
                 "oid": str(ref) if ref is not None else None,
                 "name": None, "zone": None, "controller": None}
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if s.get("type") == "object":
                entry["name"] = entry["name"] or d.get("name")
                entry["zone"] = entry["zone"] or d.get("zone")
                if d.get("controller") is not None:
                    entry["controller"] = d.get("controller")
        if entry["oid"] is not None:
            o = get_obj(state, entry["oid"])
            entry["name"] = entry["name"] or oname(o)
            entry["zone"] = entry["zone"] or o.get("zone")
            if entry["controller"] is None:
                entry["controller"] = o.get("controller")
        if entry["zone"]:
            entry["zone"] = str(entry["zone"]).lower()
        refs.append(entry)
    if not refs:
        return f"other({rtype}/{spec.get('type')}/nocands)", refs
    zones = {r["zone"] for r in refs if r["zone"]}
    names = {str(r["name"]).lower() for r in refs if r["name"]}
    ctrls = {str(r["controller"]) for r in refs
             if r["controller"] is not None}
    if zones == {"battlefield"} and names and names <= {"forest"} \
            and (not ctrls or ctrls == {str(pid)}):
        return "sacrifice", refs
    if zones == {"library"} and names and names <= {"forest"} \
            and (not ctrls or ctrls == {str(pid)}):
        return "search", refs
    return f"other({rtype}/{spec.get('type')}/{sorted(zones)})", refs


def spec_max(spec):
    d = (spec.get("data") or {})
    cd = ((d.get("constraint") or {}).get("data") or {})
    mx = cd.get("max")
    if isinstance(mx, dict):
        return mx.get("value") or mx.get("max")
    return mx


async def handle_resolution_prompts(c, state, acts, st):
    """Answer P0 ER-resolution opportunities (sacrifice, then search).
    Returns True only when a submission was made; unanswered/unknown
    prompts fall through to the priority-pass gate."""
    if STAGE["stage"] != "RESOLVE":
        return False
    for op in unanswered_ops(st):
        iid = op.get("interactionId") or op.get("id")
        label, refs = classify_resolution_op(op, state, 0)
        resp = op.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        spec_type = (spec.get("type") or "").lower()
        shape = (rtype, spec_type, label, len(refs))
        if shape not in STAGE["opp_shapes_logged"]:
            STAGE["opp_shapes_logged"].add(shape)
            wire("resolve_prompt_shape",
                 {"rtype": rtype, "spec_type": spec_type, "label": label,
                  "n_candidates": len(refs),
                  "kind": vi_kind_code(st), "opportunity": op})
            say(f"[P0] RESOLVE prompt: rtype={rtype} spec={spec_type} "
                f"label={label} n={len(refs)} kind={vi_kind_code(st)}")
        if label == "sacrifice":
            picks = [r for r in refs
                     if str(r["name"]).lower() == "forest"] or refs
            chosen = picks[:1]
            OBS["sac_oid"] = chosen[0]["oid"]
            OBS["opp_seq"].append("sacrifice")
            if not OBS["mid_sac_exported"]:
                await do_export(c, "mid_sacrifice.json")
                OBS["mid_sac_exported"] = True
        elif label == "search":
            OBS["search_max"] = spec_max(spec)
            picks = [r for r in refs
                     if str(r["name"]).lower() == "forest"][:3]
            chosen = picks
            OBS["search_chosen"] = [r["oid"] for r in chosen]
            OBS["opp_seq"].append("search")
            if not OBS["mid_search_exported"]:
                await do_export(c, "mid_search.json")
                OBS["mid_search_exported"] = True
        else:
            say(f"[P0] RESOLVE prompt: unknown label={label}, NOT answering")
            wire("resolve_prompt_unknown",
                 {"rtype": rtype, "spec_type": spec_type,
                  "opportunity": op})
            continue
        ids = [r["choice_id"] for r in chosen if r["choice_id"]]
        if not ids:
            say(f"[P0] RESOLVE prompt {label}: no choice ids, NOT answering")
            wire("resolve_prompt_no_ids", {"label": label})
            continue
        if rtype == "schema" and spec_type in ("select", "sequence"):
            sub = {"interactionId": iid,
                   "response": {"type": spec_type,
                                "data": {"choiceIds": ids}}}
        elif rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": ids[0]}}}
        else:
            say(f"[P0] RESOLVE prompt {label}: unsupported "
                f"rtype={rtype}/spec={spec_type}, NOT answering")
            wire("resolve_prompt_unsupported",
                 {"label": label, "rtype": rtype, "spec_type": spec_type,
                  "opportunity": op})
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[P0] RESOLVE answered: {label} "
            f"oids={[r['oid'] for r in chosen]}")
        wire("resolve_submit", {"label": label,
                                "chosen_oids": [r["oid"] for r in chosen],
                                "submission": sub})
        await interact_as(c, sub, "P0")
        return True
    return False


def untapped_named(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname
            and not get_obj(state, oid).get("tapped")]


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
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True
    if await drive_mana(c, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # resolution prompts first; never pass priority on a decision
    if await handle_resolution_prompts(c, state, acts, st):
        return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        if STAGE["stage"] == "SETUP" and not perm_oids(state, 0, GORGER_L):
            oid = find_hand_oid(state, 0, GORGER_L)
            if oid and len(untapped_named(state, 0, FOREST_L)) >= 4:
                for a in acts:
                    if a.get("type") == "CastSpell" and \
                            str(a.get("data", {}).get("object_id")) == oid:
                        ACT["cast"] = {"tag": f"gorger-{oid}",
                                       "lname": GORGER_L, "in_flight": True,
                                       "taps": 0, "submit_t": time.time()}
                        MANA_NEEDS[tag] = {"G": 2, "generic": 2}
                        OBS["gorger_cast"] = True
                        say(f"[P0] casts {GORGER_T} ({oid})")
                        wire("cast_submit", {"tag": "gorger", "oid": oid})
                        await submit_as_is(c, a)
                        return True
        if STAGE["stage"] == "SETUP" and OBS["gorger_cast"] \
                and not OBS["er_cast"] and perm_oids(state, 0, GORGER_L):
            oid = find_hand_oid(state, 0, ER_L)
            if oid and len(untapped_named(state, 0, FOREST_L)) >= 3:
                for a in acts:
                    if a.get("type") == "CastSpell" and \
                            str(a.get("data", {}).get("object_id")) == oid:
                        await do_export(c, "pre.json")
                        OBS["pre_exported"] = True
                        OBS["er_cast"] = True
                        OBS["er_cast_oid"] = oid
                        OBS["er_cast_turn"] = state.get("turn_number")
                        ACT["cast"] = {"tag": f"er-{oid}", "lname": ER_L,
                                       "in_flight": True, "taps": 0,
                                       "submit_t": time.time()}
                        MANA_NEEDS[tag] = {"G": 1, "generic": 2}
                        STAGE["stage"] = "RESOLVE"
                        say(f"[P0] casts {ER_T} ({oid}) "
                            f"turn={OBS['er_cast_turn']}")
                        wire("cast_submit",
                             {"tag": "er", "oid": oid,
                              "turn": OBS["er_cast_turn"]})
                        await submit_as_is(c, a)
                        return True

    if real_decision_pending(st):
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
    if await do_discard(c, acts, st, 1, tag, p1_discard_rank):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True
    if await drive_mana(c, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 1, acts, tag):
            return True
        if STAGE["stage"] == "SETUP" \
                and len(perm_oids(state, 1, BEAR_L)) < 3:
            oid = find_hand_oid(state, 1, BEAR_L)
            if oid and untapped_named(state, 1, FOREST_L):
                for a in acts:
                    if a.get("type") == "CastSpell" and \
                            str(a.get("data", {}).get("object_id")) == oid:
                        say(f"[P1] casts {BEAR_T} ({oid})")
                        wire("p1_cast_bear", {"oid": oid})
                        await submit_as_is(c, a)
                        return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


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


# ------------------------------------------------------------- main loop


async def main():
    t0 = time.time()
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}
    last_rev_change = t0
    game_started = False

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06911")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16911")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    P0C["c"] = p0
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
    while time.time() - t0 < GAME_TIMEOUT and not STAGE.get("stop"):
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
                if (st.get("state") or {}).get("turn_number", 0) >= 1:
                    game_started = True
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # with no revision change (missed-broadcast resilience).
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STAGE["stop"] = True
            continue

        # --- post-export gate: ER resolved (in P0 graveyard), stack empty,
        # at least one resolution prompt was answered -> capture post.json
        if STAGE["stage"] == "RESOLVE" and OBS["opp_seq"] \
                and not OBS["post_exported"]:
            er_oid = OBS.get("er_cast_oid")
            er_gy = False
            if er_oid is not None:
                eo = get_obj(state, er_oid)
                er_gy = eo.get("zone") == "Graveyard" \
                    and str(eo.get("controller")) == "0"
            if er_gy and stack_empty(state):
                await do_export(p0, "post.json")
                OBS["post_exported"] = True
                STAGE["stage"] = "DONE"
                STAGE["stop"] = True
                say("post.json exported; stopping")
                continue

        if game_started and not STAGE.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["stall_observed"] = True
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {"stage": STAGE["stage"],
                           "opp_seq": OBS["opp_seq"]})
            try:
                await do_export(p0, "mid_stall.json")
            except Exception as e:
                say(f"mid_stall export failed: {e}")
            STAGE["stop"] = True
            continue

        if turn > TURN_CAP and not STAGE.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            STAGE["stop"] = True
            continue

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"],
                      "opp_seq": OBS["opp_seq"]})

    # ------------------------------------------------------- assertions
    A, D = {}, {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    mid_sac_s = load("mid_sacrifice.json")
    mid_search_s = load("mid_search.json")
    post_s = load("post.json")

    # A1: ER cast with 4+ creature on BF
    a1 = OBS["pre_exported"] and pre_s is not None \
        and bool(perm_oids(pre_s, 0, GORGER_L))
    A["A1_setup_ok"] = "passed" if a1 else "failed"
    D["A1_setup_ok"] = (f"pre_exported={OBS['pre_exported']} "
                        f"gorger_on_bf_at_cast="
                        f"{bool(pre_s and perm_oids(pre_s, 0, GORGER_L))} "
                        f"er_cast_turn={OBS['er_cast_turn']}")

    # A2: sacrifice prompt before any search prompt
    seq = OBS["opp_seq"]
    sac_idx = next((i for i, l in enumerate(seq) if l == "sacrifice"), None)
    search_idx = next((i for i, l in enumerate(seq) if l == "search"), None)
    if sac_idx is not None and (search_idx is None or sac_idx < search_idx):
        A["A2_sacrifice_prompted"] = "passed"
    elif search_idx is not None and sac_idx is None:
        A["A2_sacrifice_prompted"] = "failed"
    elif sac_idx is None and search_idx is None:
        A["A2_sacrifice_prompted"] = "not-run"
    else:
        A["A2_sacrifice_prompted"] = "failed"
    D["A2_sacrifice_prompted"] = (f"prompt_sequence={seq} "
                                  f"sac_idx={sac_idx} search_idx={search_idx}")

    # A3: sacrificed land BF->Graveyard (or, in the bug case, proof that no
    # land left the battlefield at all)
    if OBS["sac_oid"] is not None and mid_sac_s is not None \
            and post_s is not None:
        before = (mid_sac_s.get("objects", {})
                  .get(str(OBS["sac_oid"]), {}))
        after = (post_s.get("objects", {}).get(str(OBS["sac_oid"]), {}))
        ok = (before.get("zone") == "Battlefield"
              and after.get("zone") == "Graveyard")
        A["A3_land_sacrificed"] = "passed" if ok else "failed"
        D["A3_land_sacrificed"] = (f"oid={OBS['sac_oid']} {oname(before)} "
                                   f"{before.get('zone')}"
                                   f"->{after.get('zone')}")
    else:
        lost = []
        if pre_s is not None and post_s is not None:
            pre_bf = {oid for oid, o in
                      (pre_s.get("objects") or {}).items()
                      if str(o.get("base_name") or o.get("name") or "")
                      .lower() == "forest"
                      and o.get("zone") == "Battlefield"
                      and str(o.get("controller")) == "0"}
            post_bf = {oid for oid, o in
                       (post_s.get("objects") or {}).items()
                       if str(o.get("base_name") or o.get("name") or "")
                       .lower() == "forest"
                       and o.get("zone") == "Battlefield"
                       and str(o.get("controller")) == "0"}
            lost = sorted(pre_bf - post_bf)
        A["A3_land_sacrificed"] = "not-run"
        D["A3_land_sacrificed"] = (f"sac_oid={OBS['sac_oid']}; "
                                   f"no sacrifice prompt answered; "
                                   f"p0_forests_left_bf_pre_to_post={lost}")

    # A4: search offered up to 3, lands entered tapped, counts consistent
    if OBS["search_max"] is not None and mid_search_s is not None \
            and post_s is not None:
        n_chosen = len(OBS["search_chosen"])
        entered = []
        for oid in OBS["search_chosen"]:
            o = (post_s.get("objects") or {}).get(str(oid), {})
            entered.append(o.get("zone") == "Battlefield"
                           and o.get("tapped") is True
                           and str(o.get("base_name") or o.get("name") or "")
                           .lower() == "forest")
        pre_bf = sum(1 for oid in bf_oids(pre_s, 0)
                     if obj_lname(pre_s, oid) == "forest") \
            if pre_s is not None else None
        post_bf = sum(1 for oid in bf_oids(post_s, 0)
                      if obj_lname(post_s, oid) == "forest")
        expected = (pre_bf + n_chosen - (1 if OBS["sac_oid"] else 0)) \
            if pre_bf is not None else None
        ok = (OBS["search_max"] == 3 and n_chosen == 3 and all(entered)
              and (expected is None or post_bf == expected))
        A["A4_search_up_to_three"] = "passed" if ok else "failed"
        D["A4_search_up_to_three"] = (f"search_max={OBS['search_max']} "
                                      f"chosen={n_chosen} "
                                      f"entered_tapped={entered} "
                                      f"bf_forests pre={pre_bf} post={post_bf} "
                                      f"expected_post={expected}")
    else:
        A["A4_search_up_to_three"] = "not-run" \
            if OBS["search_max"] is None else "failed"
        D["A4_search_up_to_three"] = (f"search_max={OBS['search_max']} "
                                      f"chosen={OBS['search_chosen']}")

    # A5: cleanup
    if post_s is not None:
        er_gy = any(str(o.get("base_name") or o.get("name") or "")
                    .lower() == "entish restoration"
                    and o.get("zone") == "Graveyard"
                    and str(o.get("controller")) == "0"
                    for o in (post_s.get("objects") or {}).values())
        stack_clear = stack_empty(post_s)
        ok = er_gy and stack_clear
        A["A5_cleanup"] = "passed" if ok else "failed"
        D["A5_cleanup"] = (f"er_in_gy={er_gy} stack_empty={stack_clear} "
                           f"turn={post_s.get('turn_number')}")
    else:
        A["A5_cleanup"] = "not-run"
        D["A5_cleanup"] = "no post.json"

    if not OBS["er_cast"]:
        verdict = "blocked"
    elif A["A2_sacrifice_prompted"] == "failed":
        verdict = "reproduced"
    elif all(A[k] == "passed" for k in
             ("A1_setup_ok", "A2_sacrifice_prompted", "A3_land_sacrificed",
              "A4_search_up_to_three", "A5_cleanup")):
        verdict = "not-reproduced"
    elif any(A[k] == "failed" for k in A):
        verdict = "reproduced"
    else:
        verdict = "blocked"

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations":
                   {k: v for k, v in OBS.items() if k != "rejections"},
                   "rejections": OBS.get("rejections", []),
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)

    date_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0))
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "date": date_iso,
        "game_code": STAGE.get("game_code"),
        "server": {
            "server_version": SERVER_IDENTITY["server_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "server_binary_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/"
                "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/data/draft-pools.json"),
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "mode": "Full",
        },
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [[ER_T, 12], [GORGER_T, 8], [FOREST_T, 40]],
            "P1": [[BEAR_T, 12], [FOREST_T, 48]],
        },
        "setup_line": ("P0 12x Entish Restoration / 8x Baloth Gorger "
                       "(4/4) / 40x Forest; P1 12x Grizzly Bears / 48x "
                       "Forest, passive"),
        "contract_line": ("ER resolution must first prompt the caster to "
                          "sacrifice a land, then offer the up-to-3 basic "
                          "land search (4+ power creature controlled)"),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6911.py (v0.81.3 / "
            "protocol 70) for pinned v0.103.0; behavioral contract "
            "A1..A5 and verdict logic unchanged.",
            "waiting_for is gone (null); priority = top-level "
            "PassPriority; all decisions via viewer_interaction; "
            "MulliganDecision via legacy Action; bottom via vi "
            "schema/select gated on waitingForKind.code=='mulligan'; "
            "DiscardToHandSize via vi schema/select (the ONLY accepted "
            "shape for a select schema).",
            "CastSpell via legacy Action; the v0.103.0 engine auto-taps "
            "reliably; manual taps are a >90s fallback only (driver "
            "taps on top of engine auto-taps double-pay).",
            "Resolution prompts (sacrifice/search) are classified from "
            "candidate object surfaces: sacrifice = all P0-controlled "
            "Battlefield lands; search = all Library-zone basic lands. "
            "Answered before the priority-pass gate; the pass gate "
            "always runs at the end of the tick.",
            "Data-level: v0.103.0 parses Entish Restoration correctly "
            "(Sacrifice{1, Land} with a ConditionInstead sub_ability "
            "carrying SearchLibrary up-to-3 / else up-to-2) -- the "
            "defect is at resolution time, not in the data.",
            "Pre/post states are authoritative exports (data.state parsed "
            "once from the export envelope) via the host client only; "
            "the reported OUTCOME is asserted on the saved states, not "
            "the prompt.",
        ],
        "assertions": A,
        "assertion_details": D,
        "prompt_sequence": OBS["opp_seq"],
        "sac_oid": OBS["sac_oid"],
        "search_max": OBS["search_max"],
        "search_chosen": OBS["search_chosen"],
        "verdict": verdict,
        "evidence_comment_id": 5651198580,
        "limitations": [
            "Browser UI not exercised; native engine via two human-driver "
            "seats.",
            "12x/8x deck density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The up-to-2 branch (no 4+ creature) was not exercised; both "
            "branches share the sacrifice node.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
        ],
        "mulligans": STAGE["mulls"],
        "rejections": OBS["rejections"],
        "stall_observed": OBS["stall_observed"],
        "notes": OBS.get("notes", []),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"wrote run.json verdict={verdict}")

    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6911_01030.py", "w") as f:
        f.write(src)

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    say("scenario finished")


if __name__ == "__main__":
    asyncio.run(main())
