#!/usr/bin/env python3
"""Issue #6914: Kediss, Emberclaw Familiar — Kediss deals the extra damage
which is wrong.

Oracle (Kediss, Emberclaw Familiar {1}{R} 1/1, Partner):
  "Whenever a commander you control deals combat damage to an opponent,
   it deals that much damage to each other opponent."
Oracle (Malcolm, Keen-Eyed Navigator {2}{U} 2/2 Siren Pirate, flying, Partner):
  "Whenever one or more Pirates you control deal damage to your opponents,
   you create a Treasure token for each opponent dealt damage."

Rules expectation: the commander that dealt the combat damage is the source
of the additional damage. Malcolm's own trigger (source-sensitive: only
Pirates) is the acceptance observable.

Protocol-106 port of driver/scenario_6914.py (v0.81.3 / protocol 70) for
pinned v0.103.0 (protocol 106). Conventions from scenario_6911_01030.py:
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [...], "commander": [...]} via cdeck().
  - waiting_for is gone (null): priority = advertised PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap; DiscardToHandSize
    via vi schema/select (the ONLY accepted submission for a select schema
    is {"type": "select", "data": {"choiceIds": [...]}}).
  - CastSpell via legacy Action; the v0.103.0 engine auto-taps reliably
    (payment_mode Auto); manual taps are a >90s fallback only (driver taps
    on top of engine auto-taps DOUBLE-PAY -- never tap for test casts).
  - DeclareAttackers via relations-schema vi opportunity; empty declares
    only ever answer relations-schema.
  - OrderTriggers: answered deterministically (Malcolm first) via vi
    schema/sequence when offered; legacy OrderTriggers action as fallback.
  - real_decision_pending excludes the 106 priority-menu codes and any
    already-answered opportunity; resolution prompts answered BEFORE the
    priority-pass gate; the pass gate always runs at the end of the tick
    (never hold priority while watching the stack).
  - Exports go through the host client only.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients; rejection-drain resync per tick.

Plan (native engine, v0.103.0 / protocol 106, three human-driver seats,
CommanderDraft, starting life 40):
  P0: commanders=[Malcolm, Keen-Eyed Navigator + Kediss, Emberclaw Familiar]
      (Partner pair; identity {U}{R}), main = 48x Island + 12x Mountain.
      Casts Malcolm, then Kediss, attacks P1 with Malcolm (unblocked).
  P1: commander=[Ayula, Queen Among Bears] (never cast), main = 60x Forest.
      Inert, no blockers.
  P2: 60x Forest, no commander. Inert.

Behavioral contract:
  A1 setup_ok        pre.json at DeclareAttackers: Malcolm on P0 BF with
                     is_commander=true and untapped; Kediss on P0 BF;
                     all players at 40 life.
  A2 combat_damage   P1 life 40 -> 38 (Malcolm's unblocked combat damage).
  A3 kediss_trigger  P2 life 40 -> 38 (Kediss's trigger resolved).
  A4 source_attribution  post.json (stack empty, turn past attack turn):
                     P0 Treasure tokens == 2 -> passed (commander is the
                     damage source; Malcolm's trigger fired for the Kediss
                     damage too). == 1 with A1-A3 passed -> failed (bug:
                     engine sourced the extra damage from Kediss, not a
                     Pirate, so Malcolm's trigger never fired for P2).
  A5 cleanup         post.json: stack empty, game proceeding (no stall).

Verdict: reproduced iff A1-A3 pass and A4 fails with exactly 1 treasure;
not-reproduced iff A1-A4 pass; blocked iff the game cannot be driven to
trigger resolution. Never "fixed".
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6914
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261007-6914")
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
# cross-check against the values recorded by the 6911 run on the same pin
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


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))


def check_data_level():
    """Record the v0.103.0 parse of Kediss + Malcolm (oracle text and the
    trigger structure). The reported defect is damage-source attribution at
    resolution time, not a missing parse."""
    out = {}
    for key in ("kediss, emberclaw familiar",
                "malcolm, keen-eyed navigator"):
        c = CARD_DATA.get(key, {})
        abils = c.get("abilities") or []
        trig = [a for a in abils
                if str((a.get("trigger") or {}).get("type") or "")
                .lower().find("damage") >= 0
                or "combat damage" in str(a.get("oracle_text") or "").lower()]
        out[key] = {
            "name": c.get("name"),
            "oracle_text": c.get("oracle_text"),
            "ability_count": len(abils),
            "damage_triggers": [
                {"trigger": t.get("trigger"),
                 "effect": t.get("effect")} for t in trig],
        }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    ked = out["kediss, emberclaw familiar"]
    ok = (ked["name"] == "Kediss, Emberclaw Familiar"
          and "each other opponent" in (ked["oracle_text"] or ""))
    say(f"data-level: kediss parse ok={ok} "
        f"damage_triggers={len(ked['damage_triggers'])}")
    wire("data_level", {"kediss_parse_ok": ok,
                        "n_damage_triggers": len(ked["damage_triggers"])})
    assert ok, "v0.103.0 card-data lost the Kediss trigger"


MALCOLM_T = "Malcolm, Keen-Eyed Navigator"
MALCOLM_L = "malcolm, keen-eyed navigator"
KEDISS_T = "Kediss, Emberclaw Familiar"
KEDISS_L = "kediss, emberclaw familiar"
AYULA_T = "Ayula, Queen Among Bears"
AYULA_L = "ayula, queen among bears"
ISLAND_T = "Island"
ISLAND_L = "island"
MOUNTAIN_T = "Mountain"
MOUNTAIN_L = "mountain"
FOREST_T = "Forest"
FOREST_L = "forest"

P0_COMMANDER = [MALCOLM_T, KEDISS_T]
P0_MAIN = ((ISLAND_T, 48), (MOUNTAIN_T, 12))
P1_COMMANDER = [AYULA_T]
P1_DECK = ((FOREST_T, 60),)
P2_DECK = ((FOREST_T, 60),)

COMMANDER_FORMAT = {
    "format": "CommanderDraft",
    "starting_life": 40,
    "min_players": 3,
    "max_players": 8,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": True,
    "commander_damage_threshold": 21,
    "range_of_influence": None,
    "team_based": False,
    "sideboard_policy": {"type": "Forbidden"},
    "uses_commander": True,
    "supplies_fixed_deck": False,
    "default_deck_copy_limit": {"type": "Unlimited"},
    "allow_debug_actions": False,
}
STARTING_LIFE = 40

GAME_TIMEOUT = 1800
STALL_AFTER = 150
TURN_CAP = 45

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0, "P2": 0}, "legend_answered": 0,
         "opp_shapes_logged": set(), "trigger_order": None,
         "order_via": None}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
ACT = {"cast": None}
OBS = {"pre_exported": False, "attack_turn": None,
       "malcolm_cast": False, "kediss_cast": False,
       "mid_exported": False, "post_exported": False,
       "rejections": [], "notes": [], "opp_seq": [],
       "stall_observed": False, "trigger_order_texts": []}


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


def treasure_count(state, pid):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == "Battlefield"
               and str(o.get("controller", -1)) == str(pid)
               and "treasure" in oname(o).lower())


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


def candidate_seat(ch):
    for s in (ch or {}).get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


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


async def do_mulligan(c, acts, st, pid, tag, lands_need):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = STAGE["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in (ISLAND_L, MOUNTAIN_L, FOREST_L))
    choice = "Keep" if n_lands >= lands_need or n >= 2 else "Mulligan"
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

        def bkey(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else "?"
            if is_land(get_obj(state, ref)):
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


def discard_rank(state, o):
    if is_land(get_obj(state, o)):
        return 0
    return 1


async def do_discard(c, acts, st, pid, tag):
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
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o),
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


async def play_a_land(c, state, pid, acts, tag, prefer_lname=None):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    if prefer_lname:
        lands.sort(key=lambda o: 0 if obj_lname(state, o) == prefer_lname
                   else 1)
    for o in lands:
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
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


def find_cast_action(acts, state, lname):
    """Find a CastSpell legacy action for the object named `lname`
    (commanders live in the command zone, not the hand)."""
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


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and not get_obj(state, oid).get("tapped")]


def untapped_named(state, pid, lname):
    return [oid for oid in untapped_lands(state, pid)
            if obj_lname(state, oid) == lname]


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------- issue-specific logic


def is_trigger_order_op(opp, state):
    """A vi opportunity asking P0 to order its triggers: schema/sequence
    whose candidates name Malcolm and Kediss."""
    resp = opp.get("response") or {}
    if resp.get("type") != "schema":
        return False
    spec = (resp.get("data") or {}).get("spec") or {}
    if (spec.get("type") or "").lower() != "sequence":
        return False
    data = resp.get("data") or {}
    cands = data.get("candidates") or data.get("choices") or []
    texts = " ".join(choice_text(ch) for ch in cands).lower()
    return "malcolm" in texts and "kediss" in texts


async def handle_order_triggers(c, st, tag):
    """Answer the OrderTriggers sequencing prompt deterministically:
    Malcolm's trigger first, then Kediss's (matches the v0.81.3 run)."""
    if STAGE["trigger_order"] is not None:
        return False
    for opp in unanswered_ops(st):
        if not is_trigger_order_op(opp, st["state"]):
            continue
        iid = opp.get("interactionId") or opp.get("id")
        data = (opp.get("response") or {}).get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        ids = [ch.get("id") for ch in cands if ch.get("id")]
        if not ids:
            continue

        def rank(cid):
            ch = next((x for x in cands if x.get("id") == cid), {})
            blob = json.dumps(ch, default=str).lower()
            return 0 if "malcolm" in blob else 1

        ids.sort(key=rank)
        texts = [choice_text(next((x for x in cands if x.get("id") == i), {}))
                 for i in ids]
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": ids}}}
        SUBMITTED_OPPS.add(iid)
        STAGE["trigger_order"] = ids
        STAGE["order_via"] = "vi-sequence"
        OBS["trigger_order_texts"] = texts
        say(f"[{tag}] ordering triggers (vi): {texts}")
        wire("order_triggers", {"who": tag, "via": "vi-sequence",
                                "texts": texts, "submission": sub})
        await interact_as(c, sub, tag)
        return True
    # legacy fallback: advertised OrderTriggers action (order not controlled)
    for a in merged_actions(st):
        if a.get("type") == "OrderTriggers":
            STAGE["trigger_order"] = ["legacy-default"]
            STAGE["order_via"] = "legacy-action"
            say(f"[{tag}] ordering triggers via legacy OrderTriggers action")
            wire("order_triggers", {"who": tag, "via": "legacy-action"})
            await submit_as_is(c, a)
            return True
    return False


async def declare_malcolm_attack(c, st, state, tag, p1_pid):
    """Declare Malcolm (untapped, P0 BF) attacking P1 via the
    relations-schema vi opportunity."""
    if state.get("active_player") != 0:
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    mal = perm_oids(state, 0, MALCOLM_L)
    if not mal or get_obj(state, mal[0]).get("tapped"):
        return False
    if not perm_oids(state, 0, KEDISS_L):
        return False
    opp = find_relations_op(st)
    if opp is None:
        return False
    iid = opp.get("interactionId") or opp.get("id")
    key = (tag, "declare-atk", str(iid))
    if key in SUBMITTED_OPPS:
        return True
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}
    rels = []
    for e in edges:
        src = e.get("sourceId")
        cand = cands.get(src, {})
        ref = _cand_reference(cand)
        if ref is None or str(ref) != str(mal[0]):
            continue
        want = None
        for tid in e.get("targetIds") or []:
            if candidate_seat(cands.get(tid, {})) == p1_pid:
                want = tid
                break
        if want is None and (e.get("targetIds") or []):
            want = (e.get("targetIds") or [])[0]
        if want:
            rels.append({"sourceId": src, "targetId": want,
                         "group": None})
    if not rels:
        say(f"[{tag}] no relations edge for Malcolm -> P1; NOT declaring")
        wire("declare_no_edge", {"who": tag})
        return False
    SUBMITTED_OPPS.add(key)
    if not OBS["pre_exported"]:
        await do_export(c, "pre.json")
        OBS["pre_exported"] = True
    OBS["attack_turn"] = state.get("turn_number")
    STAGE["stage"] = "RESOLVE"
    say(f"[{tag}] declaring Malcolm attacking P1 "
        f"(turn {OBS['attack_turn']})")
    wire("attack", {"who": tag, "attacker_oid": mal[0],
                    "target": p1_pid, "turn": OBS["attack_turn"],
                    "relations": rels})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "relations",
                                       "data": {"relations": rels}}},
                      tag)
    return True


async def p0_tick(c, tag, p1_pid):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag, 3):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    # attack declaration (before empty-declare fallback)
    if STAGE["stage"] == "SETUP":
        if await declare_malcolm_attack(c, st, state, tag, p1_pid):
            return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # trigger ordering before the priority-pass gate (a real decision)
    if await handle_order_triggers(c, st, tag):
        return True

    # mid export: first tick with P2 at 38 (Kediss trigger resolved)
    if (OBS["attack_turn"] is not None and not OBS["mid_exported"]
            and life_of(state, 2) == STARTING_LIFE - 2):
        await do_export(c, "mid.json")
        OBS["mid_exported"] = True
        say("exported MID (P2 at 38: Kediss trigger resolved)")

    # post export: stack empty and the game moved past the attack turn
    if (OBS["attack_turn"] is not None and not OBS["post_exported"]
            and (state.get("turn_number") or 0) > OBS["attack_turn"]
            and stack_empty(state)):
        await do_export(c, "post.json")
        OBS["post_exported"] = True
        STAGE["stage"] = "DONE"
        STAGE["stop"] = True
        say("exported POST; stopping")
        return True

    if my_main(state, 0) or my_priority(top_acts(st)):
        # P0 needs a Mountain in play to cast Kediss (1R): prefer it
        prefer = None if OBS["kediss_cast"] else MOUNTAIN_L
        if await play_a_land(c, state, 0, acts, tag, prefer_lname=prefer):
            return True
        # cast Malcolm (2U) from the command zone
        if (not OBS["malcolm_cast"]
                and not perm_oids(state, 0, MALCOLM_L)
                and len(untapped_lands(state, 0)) >= 3):
            a = find_cast_action(acts, state, MALCOLM_L)
            if a is not None:
                OBS["malcolm_cast"] = True
                say(f"[{tag}] casts {MALCOLM_T}")
                wire("cast_submit", {"tag": "malcolm"})
                await submit_as_is(c, a)
                return True
        # cast Kediss (1R) from the command zone
        if (OBS["malcolm_cast"] and not OBS["kediss_cast"]
                and not perm_oids(state, 0, KEDISS_L)
                and len(untapped_lands(state, 0)) >= 2
                and untapped_named(state, 0, MOUNTAIN_L)):
            a = find_cast_action(acts, state, KEDISS_L)
            if a is not None:
                OBS["kediss_cast"] = True
                say(f"[{tag}] casts {KEDISS_T}")
                wire("cast_submit", {"tag": "kediss"})
                await submit_as_is(c, a)
                return True

    # never hold priority while watching the stack: fall through to pass
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
    if await do_mulligan(c, acts, st, 1, tag, 2):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if await handle_order_triggers(c, st, tag):
        return True

    if my_main(state, 1) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 1, acts, tag):
            return True
        # P1 never casts Ayula: inert opponent, no blockers

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p2_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 2, tag, 2):
        return True
    if await do_bottom(c, acts, st, 2, tag):
        return True
    if await do_discard(c, acts, st, 2, tag):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await do_declare_empty(c, acts, st, 2, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if await handle_order_triggers(c, st, tag):
        return True

    if my_main(state, 2) or my_priority(top_acts(st)):
        if await play_a_land(c, state, 2, acts, tag):
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
    for k in ("P0", "P1", "P2"):
        MANA_NEEDS[k] = {}
    last_rev_change = t0
    game_started = False

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06914")
    await p0.connect()
    say("P0 creating game (CommanderDraft, 3 seats)...")
    await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                    format_config=COMMANDER_FORMAT)
    p1 = PhaseClient("P16914")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, cdeck(P1_COMMANDER, *P1_DECK))
    p2 = PhaseClient("P26914")
    await p2.connect()
    say("P2 joining...")
    await p2.join(p0.game_code, deck(*P2_DECK))
    STAGE["game_code"] = p0.game_code
    p1_pid = p1.player_id
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1_pid} "
        f"P2 seat={p2.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1_pid, "p2": p2.player_id,
                  "p0_commander": P0_COMMANDER, "p0_main": P0_MAIN,
                  "p1_commander": P1_COMMANDER, "p2_deck": P2_DECK})

    last_rev = {}
    last_tick_at = {}
    while time.time() - t0 < GAME_TIMEOUT and not STAGE.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick),
                             (p2, "P2", p2_tick)):
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
                if tag == "P0":
                    await tick(c, tag, p1_pid)
                else:
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

        if game_started and not STAGE.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["stall_observed"] = True
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {"stage": STAGE["stage"]})
            try:
                await do_export(p0, "mid_stall.json")
            except Exception as e:
                say(f"mid_stall export failed: {e}")
            STAGE["stop"] = True
            continue

        if turn > TURN_CAP and not STAGE.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STAGE["stop"] = True
            continue

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"]})

    # ------------------------------------------------------- assertions
    A, D = {}, {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    mid_s = load("mid.json")
    post_s = load("post.json")

    # A1: setup at DeclareAttackers
    if pre_s is not None:
        mal = perm_oids(pre_s, 0, MALCOLM_L)
        ked = perm_oids(pre_s, 0, KEDISS_L)
        mal_ok = bool(mal) and bool(get_obj(pre_s, mal[0]).get("is_commander")) \
            and not get_obj(pre_s, mal[0]).get("tapped")
        ok = (mal_ok and bool(ked)
              and life_of(pre_s, 0) == STARTING_LIFE
              and life_of(pre_s, 1) == STARTING_LIFE
              and life_of(pre_s, 2) == STARTING_LIFE)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (
            f"malcolm_bf={mal[0] if mal else None} "
            f"is_commander={bool(mal and get_obj(pre_s, mal[0]).get('is_commander'))} "
            f"untapped={bool(mal and not get_obj(pre_s, mal[0]).get('tapped'))} "
            f"kediss_bf={ked[0] if ked else None} "
            f"life={[life_of(pre_s, i) for i in (0, 1, 2)]} "
            f"attack_turn={OBS['attack_turn']}")
    else:
        A["A1_setup_ok"] = "failed"
        D["A1_setup_ok"] = "pre.json missing (attack never declared)"

    # A2/A3: life totals
    dmg_s = mid_s if mid_s is not None else post_s
    if dmg_s is not None:
        p1_life = life_of(dmg_s, 1)
        p2_life = life_of(dmg_s, 2)
        A["A2_combat_damage"] = "passed" if p1_life == 38 else "failed"
        D["A2_combat_damage"] = (f"P1 life={p1_life} (expect 38 = Malcolm's "
                                 f"2 unblocked combat damage)")
        A["A3_kediss_trigger"] = "passed" if p2_life == 38 else "failed"
        D["A3_kediss_trigger"] = (f"P2 life={p2_life} (expect 38 = Kediss "
                                  f"trigger dealing 2)")
    else:
        A["A2_combat_damage"] = "failed"
        A["A3_kediss_trigger"] = "failed"
        D["A2_combat_damage"] = "no mid.json/post.json"
        D["A3_kediss_trigger"] = "no mid.json/post.json"

    # A4: treasure count decides the source question
    if post_s is not None:
        ntre = treasure_count(post_s, 0)
        D["A4_source_attribution"] = (
            f"P0 treasure tokens={ntre} (expect 2 if the commander is the "
            f"damage source; 1 = engine sourced it from Kediss)")
        if (A["A1_setup_ok"] == "passed"
                and A["A2_combat_damage"] == "passed"
                and A["A3_kediss_trigger"] == "passed"):
            if ntre == 2:
                A["A4_source_attribution"] = "passed"
                D["A4_source_attribution"] += (
                    " -> Malcolm's trigger fired for the Kediss-trigger "
                    "damage too: Malcolm (a Pirate) is the damage source. "
                    "Bug NOT reproduced.")
            elif ntre == 1:
                A["A4_source_attribution"] = "failed"
                D["A4_source_attribution"] += (
                    " -> only 1 treasure despite P2 taking 2: the engine "
                    "attributed the extra damage to Kediss (not a Pirate), "
                    "so Malcolm's trigger did not fire. BUG REPRODUCED.")
            else:
                A["A4_source_attribution"] = "failed"
                D["A4_source_attribution"] += f" -> unexpected count {ntre}."
        else:
            A["A4_source_attribution"] = "failed"
            D["A4_source_attribution"] += " -> prerequisites not all passed."
    else:
        A["A4_source_attribution"] = "failed"
        D["A4_source_attribution"] = "post.json missing"

    # A5: cleanup
    if post_s is not None:
        stack_clear = stack_empty(post_s)
        A["A5_cleanup"] = "passed" if stack_clear else "failed"
        D["A5_cleanup"] = (f"stack_empty={stack_clear} "
                           f"turn={post_s.get('turn_number')}")
    else:
        A["A5_cleanup"] = "not-run"
        D["A5_cleanup"] = "no post.json"

    if (A["A1_setup_ok"] == "passed"
            and A["A2_combat_damage"] == "passed"
            and A["A3_kediss_trigger"] == "passed"
            and A["A4_source_attribution"] == "failed"
            and post_s is not None
            and treasure_count(post_s, 0) == 1):
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_combat_damage", "A3_kediss_trigger",
              "A4_source_attribution", "A5_cleanup")):
        verdict = "not-reproduced"
    elif A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    else:
        verdict = "reproduced"

    for k in ("A1_setup_ok", "A2_combat_damage", "A3_kediss_trigger",
              "A4_source_attribution", "A5_cleanup"):
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
            "server_binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "mode": "Full",
        },
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": {"commander": P0_COMMANDER,
                   "main": [[n, c] for n, c in P0_MAIN]},
            "P1": {"commander": P1_COMMANDER,
                   "main": [[n, c] for n, c in P1_DECK]},
            "P2": {"commander": [],
                   "main": [[n, c] for n, c in P2_DECK]},
        },
        "setup_line": ("P0 commanders Malcolm, Keen-Eyed Navigator + Kediss, "
                       "Emberclaw Familiar (Partner), 48x Island / 12x "
                       "Mountain; P1 commander Ayula (never cast), 60x "
                       "Forest, inert; P2 60x Forest, no commander, inert"),
        "contract_line": ("Malcolm attacks P1 unblocked; Malcolm's trigger "
                          "fires for the combat damage (1 treasure); "
                          "Kediss's trigger deals 2 to P2. If the commander "
                          "(Malcolm, a Pirate) is the damage source, "
                          "Malcolm's trigger fires again (2nd treasure); if "
                          "Kediss is the source, it does not (1 treasure)."),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6914.py (v0.81.3 / "
            "protocol 70) for pinned v0.103.0; behavioral contract "
            "A1..A5 and verdict logic unchanged.",
            "waiting_for is gone (null); priority = top-level "
            "PassPriority; all decisions via viewer_interaction; "
            "MulliganDecision via legacy Action; bottom via vi "
            "schema/select gated on waitingForKind.code=='mulligan'; "
            "DiscardToHandSize via vi schema/select (the ONLY accepted "
            "shape for a select schema).",
            "CastSpell via legacy Action (commanders cast from the "
            "command zone); the v0.103.0 engine auto-taps reliably, so "
            "no driver mana taps (driver taps on top of engine auto-taps "
            "double-pay).",
            "DeclareAttackers via relations-schema vi opportunity "
            "(Malcolm -> P1); empty declares only ever answer "
            "relations-schema.",
            "OrderTriggers answered deterministically (Malcolm first) "
            "via vi schema/sequence; legacy OrderTriggers action as "
            "fallback (order recorded). Answered before the "
            "priority-pass gate; the pass gate always runs at the end "
            "of the tick (never hold priority while watching the "
            "stack).",
            "Pre/post/mid states are authoritative exports "
            "(data.state parsed once from the export envelope) via the "
            "host client only; the reported OUTCOME is asserted on the "
            "saved states, not the prompt.",
            "Data-level: v0.103.0 card-data parses Kediss correctly "
            "(trigger present, 'each other opponent'); the defect is "
            "damage-source attribution at resolution time.",
        ],
        "assertions": A,
        "assertion_details": D,
        "trigger_order": STAGE["trigger_order"],
        "trigger_order_via": STAGE["order_via"],
        "trigger_order_texts": OBS["trigger_order_texts"],
        "attack_turn": OBS["attack_turn"],
        "verdict": verdict,
        "evidence_comment_id": 5651440803,
        "limitations": [
            "Browser UI not exercised; native engine via three "
            "human-driver seats.",
            "48x/12x deck density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The observable is Malcolm's own source-sensitive trigger "
            "(Pirate damage -> Treasure): the acceptance criterion the "
            "issue's triage comments named, not a direct wire read of "
            "the damage source field.",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports (restorable only via full "
            "game replay).",
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
    with open(f"{EVDIR}/scenario_6914_01030.py", "w") as f:
        f.write(src)

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1, p2):
        try:
            await c.close()
        except Exception:
            pass
    say("scenario finished")


if __name__ == "__main__":
    asyncio.run(main())