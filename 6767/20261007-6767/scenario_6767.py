#!/usr/bin/env python3
"""Issue #6767: Duskana, the Rage Mother doesn't trigger the +3/+3.

Protocol-106 port (v0.103.0, 2026-10-07) of scenario_6767_01020.py.

Reported (Discord, confirmed; labels include classifier:unsupported-aspect):
  "[[duskana the rage mother]] should give +3/+3 to base p/t 2/2 but does not."

Oracle (pinned v0.103.0 card-data.json, key "duskana, the rage mother"):
  "When Duskana enters, draw a card for each creature you control with base
   power and toughness 2/2.
   Whenever a creature you control with base power and toughness 2/2 attacks,
   it gets +3/+3 until end of turn."

Parser state (v0.103.0 data, verified 2026-10-07): the ETB trigger is parsed
as ChangesZone/SelfRef with a FIXED draw count of 1 (SwallowedClause warning
on the dynamic quantity); the attack trigger's mode is still
{"Unknown": "Whenever a creature you control with base power and toughness
2/2 attacks"} with a Pump +3/+3, target TriggeringSource, UntilEndOfTurn.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, v0.103.0/protocol 106):
  P0: 4x Duskana, the Rage Mother + 12x Grizzly Bears + 16x Taiga + 14x
      Savannah + 14x Plateau (44 lands; engine accepts >4-of for custom
      games). Mulligan: always keep (dense lands).
  P1: 60x Forest dummy (plays a land, passes; never attacks/blocks).

Expected (per card text):
  RAMP: P0 plays lands, casts 2x Grizzly Bears, then Duskana (2RGW).
  ATTACK: at P0's DeclareAttackers with Duskana + an attack-ready Bear on
          the battlefield: export pre.json, attack P1 with exactly one Bear.
  OBSERVE: export mid_combat.json once combat is past DeclareAttackers;
           export post.json once the stack is empty post-combat; stop.

Assertions:
  A1 setup_ok      pre.json: Duskana on P0 BF, >=1 Bear on P0 BF, 20/20 life.
  A2 attack_declared  Bear declared as attacker (wire event + combat state).
  A3 trigger_fires    a Duskana attack trigger appears on the stack / in
                      triggers_fired_this_turn between declare and mid-combat.
  A4 pump_correct      attacking Bear is 5/5 at mid_combat (until end of turn).
  A5 cleanup           stack empty at post.json; game proceeding.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and (A3 fails or A4 fails).
Verdict = not-reproduced iff A1..A5 all pass.

The ETB draw count is recorded as an observation note only (not scored):
oracle says draw = # of 2/2s controlled; the parsed trigger draws a fixed 1.

Evidence: evidence/6767/<run-id>/pre.json, mid_combat.json, post.json,
run.json, manifest.sha256, summary.png, scenario_6767.py, wire_log.jsonl,
scenario_run.log, data_evidence.json
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

ISSUE = 6767
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261007-6767"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

DUSKANA = "duskana, the rage mother"
BEAR = "grizzly bears"
P0_DECK = [("Duskana, the Rage Mother", 4), ("Grizzly Bears", 12),
           ("Taiga", 16), ("Savannah", 14), ("Plateau", 14)]
P1_DECK = [("Forest", 60)]

ST = {"stage": "RAMP", "stop": False, "attacked": False,
      "mid_exported": False, "etb_seen": False, "etb_note": None,
      "attacker_oid": None, "attack_turn": None, "mana_needs": {},
      "declare_shape_logged": False, "pre_exported": False}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ---------------------------------------------------------------- helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def hand_ids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_creature(o):
    return "Creature" in ((o.get("card_types") or {}).get("core_types") or [])


def can_attack_now(o):
    if o.get("tapped"):
        return False
    if o.get("summoning_sick") or o.get("has_summoning_sickness"):
        kws = o.get("keywords") or []
        flat = [k if isinstance(k, str) else str(k) for k in kws]
        if not any("aste" in k for k in flat):  # Haste
            return False
    return True


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def life(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return None


def pt(obj):
    return num(obj.get("power")), num(obj.get("toughness"))


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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    for opp in vi_ops(st):
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
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def stack_desc(state):
    out = []
    for sid in state.get("stack", []) or []:
        o = get_obj(state, sid)
        out.append({"id": sid,
                    "name": o.get("base_name") or o.get("name") or "?",
                    "zone": o.get("zone"),
                    "kind": str(o.get("kind", ""))[:80]})
    return out


# ------------------------------------------------------- interaction prims

async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": {k: v for k, v in msg.items()}})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data") or {}).get("spec", {}) or {}
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


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"


def check_data_level():
    ok, notes = True, []
    c = CARD_DATA.get(DUSKANA, {})
    oracle = str(c.get("oracle_text", ""))
    if "gets +3/+3" not in oracle or "attacks" not in oracle:
        ok = False
        notes.append("duskana oracle text shape missing")
    trigs = c.get("triggers", []) or []
    modes = [str(t.get("mode")) for t in trigs]
    if not any("Unknown" in m for m in modes):
        notes.append("attack trigger no longer Unknown-mode (parser changed!)")
    if not any("ChangesZone" in m for m in modes):
        ok = False
        notes.append("ETB trigger missing")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": oracle[:400],
                   "trigger_modes": modes}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"

        def rank(o):
            nm = obj_lname(state, o)
            if is_land(get_obj(state, o)):
                return (0, nm)
            if nm == DUSKANA:
                return (5, nm)
            return (2, nm)

        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
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
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used} needs_left={needs}")
        wire("tap_land", {"who": tag, "used_for": used, "needs_left": dict(needs or {})})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
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
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def can_pay(state, pid, needs):
    pool = [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")]
    return len(pool) >= sum(needs.values())


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def build_attacks(da, attacker_oid, target_player):
    """Build the DeclareAttackers submission from the advertised action's
    data shape; log the full advertised shape the first time (106)."""
    d = copy.deepcopy(da.get("data", {}))
    if not ST["declare_shape_logged"]:
        ST["declare_shape_logged"] = True
        wire("declare_attackers_shape", {"advertised": da.get("data", {})})
        say(f"DeclareAttackers advertised data keys: {list(d.keys())} :: "
            f"{json.dumps(da.get('data', {}))[:600]}")
    if "attacks" in d:
        d["attacks"] = [[attacker_oid, {"type": "Player", "data": target_player}]]
    if "bands" in d:
        d["bands"] = []
    return d


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------------ ticks

async def p1_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        dusk = bf_by_name(state, 0, DUSKANA)
        ready = [o for o in bf_by_name(state, 0, BEAR) if can_attack_now(get_obj(state, o))]
        if (not ST["attacked"] and dusk and ready and ST["stage"] == "RAMP" and da):
            pre = await do_export(c, "pre.json")
            ST["pre_exported"] = True
            dusk_n = len([o for o in (pre.get("objects") or {}).values()
                          if o.get("zone") == "Battlefield" and o.get("controller") == 0
                          and str(o.get("base_name") or o.get("name") or "").lower() == DUSKANA])
            bears_n = len([o for o in (pre.get("objects") or {}).values()
                           if o.get("zone") == "Battlefield" and o.get("controller") == 0
                           and str(o.get("base_name") or o.get("name") or "").lower() == BEAR])
            wire("pre_export", {"duskana_bf": dusk_n, "bears_bf": bears_n,
                                "life": [life(pre, 0), life(pre, 1)]})
            sub_data = build_attacks(da, ready[0], 1)
            ST["attacker_oid"] = ready[0]
            ST["attack_turn"] = state.get("turn_number")
            await submit_as_is(c, {"type": "DeclareAttackers", "data": sub_data})
            ST["attacked"] = True
            ST["stage"] = "OBSERVE"
            say(f"[{tag}] attacks P1 with Bear oid {ready[0]} "
                f"(turn {ST['attack_turn']})")
            return
        if da:
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return
        dusk_bf = bool(bf_by_name(state, 0, DUSKANA))
        dusk_hand = [o for o in hand_ids(state, 0) if obj_lname(state, o) == DUSKANA]
        if not dusk_bf and dusk_hand and can_pay(state, 0, {"generic": 2, "R": 1, "G": 1, "W": 1}):
            a, oid = cast_action_for(acts, state, DUSKANA)
            if a is not None:
                ST["mana_needs"][tag] = {"generic": 2, "R": 1, "G": 1, "W": 1}
                say(f"[{tag}] casting Duskana, the Rage Mother (oid {oid})")
                wire("cast_duskana", {"oid": oid})
                await submit_as_is(c, a)
                return
        bears_bf = len(bf_by_name(state, 0, BEAR))
        bear_hand = [o for o in hand_ids(state, 0) if obj_lname(state, o) == BEAR]
        if bears_bf < 2 and bear_hand and can_pay(state, 0, {"generic": 1, "G": 1}):
            a, oid = cast_action_for(acts, state, BEAR)
            if a is not None:
                ST["mana_needs"][tag] = {"generic": 1, "G": 1}
                say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                wire("cast_bear", {"oid": oid})
                await submit_as_is(c, a)
                return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------------- main

async def main():
    t0 = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_attack_declared", "A3_trigger_fires",
            "A4_pump_correct", "A5_cleanup")}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    C0 = p0
    last = {}
    last_tick_at = {}
    last_stack_sig = None
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if rev == last.get(c.name) and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, st, tag)
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        st = p0.latest
        if not st:
            continue
        state = st["state"]

        # ETB observation: Duskana entering the battlefield
        if not ST["etb_seen"] and bf_by_name(state, 0, DUSKANA):
            ST["etb_seen"] = True
            bears = len(bf_by_name(state, 0, BEAR))
            hand = len(hand_ids(state, 0))
            ST["etb_note"] = (f"Duskana ETB with {bears} other 2/2 Bears on BF; "
                              f"P0 hand={hand} (oracle: draw {bears}; "
                              f"parsed trigger: fixed 1)")
            say("[note]", ST["etb_note"])
            wire("duskana_etb", {"bears_on_bf": bears, "p0_hand": hand})

        # stack watch during OBSERVE
        if ST["stage"] == "OBSERVE":
            sd = stack_desc(state)
            sig = json.dumps(sd, sort_keys=True, default=str)
            if sig != last_stack_sig:
                last_stack_sig = sig
                wire("stack", {"entries": sd,
                               "triggers_fired": state.get("triggers_fired_this_turn")})
                if sd:
                    say(f"[stack] {json.dumps(sd)[:400]}")

        # mid-combat export: first tick past DeclareAttackers in OBSERVE
        if ST["stage"] == "OBSERVE" and not ST["mid_exported"] \
                and (state.get("phase") or "") in (
                    "DeclareBlockers", "CombatDamage", "EndOfCombat",
                    "PostCombatMain"):
            mid = await do_export(C0, "mid_combat.json")
            ST["mid_exported"] = True
            atk = ST.get("attacker_oid")
            o = get_obj(mid, atk)
            wire("mid_combat", {"attacker_pt": pt(o),
                                "phase": mid.get("phase"),
                                "stack": stack_desc(mid),
                                "triggers_fired": mid.get("triggers_fired_this_turn")})
            say(f"[mid] attacker {atk} P/T={pt(o)} phase={mid.get('phase')}")

        # post export: stack empty and (post-combat main or a later turn)
        if ST["mid_exported"]:
            stack_empty = not (state.get("stack") or [])
            post_phase = (state.get("phase") or "") in ("PostCombatMain", "EndStep",
                                                         "Cleanup")
            later_turn = (state.get("turn_number") or 0) > (ST.get("attack_turn") or 0)
            if stack_empty and (post_phase or later_turn):
                await asyncio.sleep(1.0)
                post = await do_export(C0, "post.json")
                atk = ST.get("attacker_oid")
                o = get_obj(post, atk)
                wire("post", {"attacker_pt": pt(o),
                              "phase": post.get("phase"),
                              "turn": post.get("turn_number"),
                              "triggers_fired": post.get("triggers_fired_this_turn")})
                say(f"[post] phase={post.get('phase')} turn={post.get('turn_number')} "
                    f"attacker P/T={pt(o)}")
                ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre.json")
    mid = env_state("mid_combat.json")
    post = env_state("post.json")

    def triggers_mention_duskana(s):
        hits = []
        for entry in s.get("triggers_fired_this_turn") or []:
            blob = json.dumps(entry, default=str)
            if "uskana" in blob or "ttack" in blob:
                hits.append(blob[:200])
        return hits

    # A1
    if pre is None:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre.json was never exported (attack never declared)")
    else:
        def lname_of(o):
            return str(o.get("base_name") or o.get("name") or "?").lower()
        dusk = [o for o in (pre.get("objects") or {}).values()
                if o.get("zone") == "Battlefield" and o.get("controller") == 0
                and lname_of(o) == DUSKANA]
        bears = [o for o in (pre.get("objects") or {}).values()
                 if o.get("zone") == "Battlefield" and o.get("controller") == 0
                 and lname_of(o) == BEAR]
        ok = (len(dusk) >= 1 and len(bears) >= 1
              and life(pre, 0) == 20 and life(pre, 1) == 20)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"pre: duskana_bf={len(dusk)} bears_bf={len(bears)} "
                     f"life={[life(pre, 0), life(pre, 1)]}")

    # A2
    atk_declared = False
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "action_submit" and \
                d["payload"].get("action", {}).get("type") == "DeclareAttackers" and \
                d["payload"].get("action", {}).get("data", {}).get("attacks"):
            atk_declared = True
    ass["A2_attack_declared"] = "passed" if atk_declared else "failed"

    # A3
    trig_hits = []
    if mid is not None:
        trig_hits += triggers_mention_duskana(mid)
    if post is not None:
        for h in triggers_mention_duskana(post):
            if h not in trig_hits:
                trig_hits.append(h)
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "stack":
            for e in d["payload"].get("entries", []) or []:
                if "uskana" in str(e.get("name", "")):
                    trig_hits.append("stack:" + str(e.get("name")))
    ass["A3_trigger_fires"] = "passed" if trig_hits else "failed"
    notes.append(f"trigger evidence hits: {trig_hits[:3] or 'none'}")

    # A4
    atk = ST.get("attacker_oid")
    if mid is not None and atk is not None:
        p, t = pt(get_obj(mid, atk))
        ass["A4_pump_correct"] = "passed" if (p == 5 and t == 5) else "failed"
        notes.append(f"mid_combat attacker {atk} P/T={p}/{t} (expected 5/5)")
        others = [(str(oid), pt(o)) for oid, o in (mid.get("objects") or {}).items()
                  if o.get("zone") == "Battlefield" and o.get("controller") == 0
                  and str(o.get("base_name") or o.get("name") or "").lower() == BEAR
                  and str(oid) != str(atk)]
        notes.append("non-attacking bears mid_combat: " + str(others))
    else:
        ass["A4_pump_correct"] = "not-run"
        notes.append("mid_combat.json missing; A4 not-run")

    # A5
    if post is not None:
        empty = not (post.get("stack") or [])
        ass["A5_cleanup"] = "passed" if empty else "failed"
        notes.append(f"post: phase={post.get('phase')} "
                     f"turn={post.get('turn_number')} stack_empty={empty}")
    else:
        ass["A5_cleanup"] = "not-run"
        notes.append("post.json missing; A5 not-run")

    if ST.get("etb_note"):
        notes.append("ETB observation: " + ST["etb_note"])

    for k in sorted(ass):
        say(f"{k}: {ass[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes}, f, indent=2)

    verdict = ("blocked" if ass.get("A1_setup_ok") in ("failed", "not-run")
               else "reproduced" if ass.get("A3_trigger_fires") == "failed"
               or ass.get("A4_pump_correct") == "failed"
               else "not-reproduced"
               if all(v == "passed" for v in ass.values())
               else "blocked")
    say("verdict:", verdict)

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_6767.py", "w") as f:
        f.write(scenario_src.decode())
    binary_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.103.0/"
             f"phase-server-slim-x86_64-unknown-linux-musl", "rb").read()).hexdigest()
    card_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json", "rb").read()).hexdigest()
    draft_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.103.0/data/draft-pools.json", "rb").read()).hexdigest()
    run_meta = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.103.0",
            "build_commit": "ec27a8d",
            "protocol_version": 106,
            "binary_sha256": binary_sha,
            "card_data_sha256": card_sha,
            "draft_pools_sha256": draft_sha,
            "run_dir": f"runs/{RUN_ID}",
            "port": 9374,
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": dict(P0_DECK), "P1": dict(P1_DECK)},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Grizzly Bears density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "ETB draw recorded as an observation only, not scored.",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_6767_01020.py (verified 2026-10-05, run 20261005-6767).",
            "Payment via vi tapLandForMana with needs={generic:2,R:1,G:1,W:1} "
            "(Duskana) / {generic:1,G:1} (Bear); legacy PayMana/PayManaAbilityMana "
            "also honored.",
            "Land plays via legal_actions PlayLand matched by _src_oid; "
            "is_land() helper covers Taiga/Savannah/Plateau/Forest.",
            "DeclareAttackers submitted from the advertised action's data shape; "
            "the full advertised shape is wire-logged (declare_attackers_shape).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=1)

    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
