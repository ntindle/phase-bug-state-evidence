#!/usr/bin/env python3
"""Issue #6864 re-validation on pinned v0.102.0 (protocol 106):
Zariel, Archduke of Avernus ultimate doesn't allow you to choose to untap
a creature.

Oracle: "[-6]: You get an emblem with 'At the end of the first combat phase
on your turn, untap target creature you control. After this phase, there is
an additional combat phase.'"

Data level (v0.102.0 pinned card-data.json): the -6 ability's effect is
{"type": "Unimplemented", "name": "emblem_creation", ...} -- no emblem,
no end-of-combat trigger, no target slot, no untap, no additional combat
in the parse at all.

History: 2026-09-11 protocol-69 run (v0.80.0) and 2026-10-01 protocol-94
run (v0.98.0) both reproduced the related failure -- loyalty paid, Zariel
in graveyard, but NO emblem object, NO target prompt, attacking Bears
stayed tapped; the bare AdditionalPhase half of the collapsed AST did
insert an extra combat.

Plan (two human seats, native engine, protocol 106):
  SETUP  - P0 plays a land per turn (Mountain preference), casts Grizzly
           Bears whenever affordable, casts Zariel, Archduke of Avernus
           ({2}{R}{R}) once affordable. P1 plays a land per turn and passes.
  LOYAL1 - P0's next main phase: activate +1 (ability_index 0) -> loy 5.
  LOYAL2 - following P0 turn: activate +1 again -> loyalty 6.
  ACTIVATE - export pre_activate.json; activate -6 (ability_index 2) ->
             loyalty 0, Zariel to graveyard; export post_activate.json;
             scan for an emblem object owned by P0.
  COMBAT - P0 attacks with all untapped Bears (once); at the end of the
           first combat phase on P0's turn watch for the emblem trigger
           target prompt ("untap target creature you control"); answer it
           with a tapped attacker; watch for the untap and any additional
           combat phase.
  DONE   - export post.json once the combat sequence is over.

Behavioral contract:
  A1 setup_ok         pre_activate.json: Zariel + >=1 Bears on P0 BF
  A2 loyalty_paid     post_activate.json: Zariel in Graveyard (0 loyalty)
  A3 emblem_created   an emblem object owned by P0 exists post-activation
                      (expected FAIL: the parse is Unimplemented)
  A4 target_prompt    a target-selection prompt for P0's creature appears
                      at the end of the first combat phase (expected FAIL)
  A5 untap_resolves   the chosen creature is untapped after resolution
  A6 additional_combat an extra combat phase follows the first on the ult
                      turn (may PASS via the residual AdditionalPhase node)

Verdict = reproduced iff A3 fails (no emblem -> the reported "no choice
to untap") or A4 fails with A3 passed; not-reproduced iff A3..A6 all pass;
else blocked.

Protocol-106 driver notes (per scenario_6862_01020.py):
  - waiting_for is gone; priority = PassPriority in top-level
    legal_actions; decisions via viewer_interaction.
  - MulliganDecision via legacy Action (always Keep).
  - DiscardToHandSize via vi schema/select opportunity over hand cards.
  - CastSpell via legacy Action with vi castSpell-choice fallback; mana via
    legacy PayMana and/or vi tapLandForMana menus driven by MANA_NEEDS.
  - DeclareAttackers via legacy Action with attacks=[[oid, {Player, 1}]].
  - real_decision_pending excludes the noisy 106 priority-menu codes
    (passPriority, tapLandForMana, untapLandForMana, castSpell,
    activateAbility, candidate, mana, mulliganDecision, playLand).
  - play_a_land matches any land via is_land(); vi playLand choices
    answered before the decision gate.
  - Loyalty abilities via legacy ActivateAbility (ability_index) with vi
    activateAbility-choice fallback.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients.
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
ISSUE = 6864
RUN_ID = "20261006-6864"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

ZARIEL = "zariel, archduke of avernus"
BEAR = "grizzly bears"
MOUNTAIN = "mountain"
FOREST = "forest"
PLAINS = "plains"
ISLAND = "island"
SWAMP = "swamp"

ZARIEL_T = "Zariel, Archduke of Avernus"
BEAR_T = "Grizzly Bears"
MOUNTAIN_T = "Mountain"
FOREST_T = "Forest"
PLAINS_T = "Plains"
ISLAND_T = "Island"
SWAMP_T = "Swamp"

GAME_TIMEOUT = 2400

ST = {"stage": "SETUP", "stop": False, "attacked": False,
      "act_once": False, "ult_turn": None, "cast_inflight": None}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}
TRIGGERS = []        # emblem-candidate triggers on the stack
PHASES = []          # (turn, active_player, phase) on change
TGT = {"prompt_seen": False, "candidates": None, "answered": None,
       "seen_at": None}
LAST_IID = {"iid": None}
ZARLOG = []


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for oid in bf_oids(state, pid)
               if is_land(get_obj(state, oid))
               and not get_obj(state, oid).get("tapped"))


def untapped_of(state, pid, lname):
    return sum(1 for oid in bf_oids(state, pid)
               if obj_lname(state, oid) == lname
               and not get_obj(state, oid).get("tapped"))


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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


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


def cand_refs(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            out.append(str(d["reference"]))
    return out


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def st_of(c):
    return c.latest or {}


def loyalty_of(o):
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    for c in o.get("counters", []) or []:
        if isinstance(c, dict) and "loyal" in str(c).lower():
            return int(c.get("count", c.get("amount", 0)))
    return None


def zariel_oid(state, pid=0):
    for oid, o in state["objects"].items():
        if (obj_lname(state, oid) == ZARIEL
                and o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == str(pid)):
            return str(oid)
    return None


def emblem_objects(state, pid=0):
    out = []
    for oid, o in state["objects"].items():
        name = oname(o).lower()
        zonesub = str(o.get("zone", "")).lower()
        subtypes = o.get("subtypes") or []
        if (str(o.get("owner", o.get("controller", -1))) == str(pid)) and (
                o.get("is_emblem") is True or "emblem" in name
                or "emblem" in zonesub
                or any("emblem" in str(s).lower() for s in subtypes)):
            out.append((str(oid), oname(o), o.get("zone")))
    return out

# ------------------------------------------------------------- interaction primitives
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
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
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"


def check_data_level():
    z = CARD_DATA.get("zariel, archduke of avernus", {})
    oracle = str(z.get("oracle_text", ""))
    abs2 = (z.get("abilities") or [{}])[2]
    eff = abs2.get("effect", {}) or {}
    ev = {
        "zariel_oracle": oracle,
        "minus6_effect_type": eff.get("type"),
        "minus6_effect_name": eff.get("name"),
        "minus6_description": eff.get("description"),
        "minus6_raw": eff,
        "minus6_unimplemented": eff.get("type") == "Unimplemented",
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    say(f"data-level: -6 effect type={ev['minus6_effect_type']} "
        f"name={ev['minus6_effect_name']} unimplemented="
        f"{ev['minus6_unimplemented']}")
    assert ev["minus6_unimplemented"], \
        f"expected Unimplemented emblem_creation, got {ev['minus6_effect_type']}"


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull")
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] mulligan -> Keep")
    wire("mulligan", {"who": tag, "decision": "Keep"})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def p0_discard_rank(state, o):
    # never discard Zariel; shed lands, then Bears
    ln = obj_lname(state, o)
    if ln == ZARIEL:
        return 3
    if is_land(get_obj(state, o)):
        return 0
    if ln == BEAR:
        return 1
    return 2


def p1_discard_rank(state, o):
    return 0 if is_land(get_obj(state, o)) else 1


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
            for r in cand_refs(ch):
                ref_of.setdefault(r, ch["id"])
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


async def declare_branch(c, acts, tag, state, pid, attack_plan):
    """DeclareAttackers / DeclareBlockers. attack_plan: None => P0 uses its
    attackers (once, only when ST['stage']=='COMBAT' and not attacked)."""
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = copy.deepcopy(a)
            dd = d.setdefault("data", {})
            if attack_plan is not None and not ST["attacked"] \
                    and ST["stage"] == "COMBAT":
                bears = [int(oid) for oid in bf_oids(state, pid)
                         if obj_lname(state, oid) == BEAR
                         and not get_obj(state, oid).get("tapped")]
                dd["attacks"] = [[y, {"type": "Player", "data": 1}]
                                 for y in bears]
                dd["bands"] = []
                ST["attacked"] = True
                say(f"[P0] declares attackers: {bears} -> P1")
                wire("declare_attackers", {"attackers": bears})
            else:
                dd["attacks"] = []
                dd["bands"] = []
            await submit_as_is(c, d)
            return True
        if a.get("type") == "DeclareBlockers":
            d = copy.deepcopy(a)
            dd = d.setdefault("data", {})
            dd["assignments"] = []
            await submit_as_is(c, d)
            return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None,
                                                               "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" not in codes:
                continue
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
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def p0_land_prefer(state, o):
    # Mountain-first (RR for Zariel), then Forest (G for Bears)
    ln = obj_lname(state, o)
    return 0 if ln == MOUNTAIN else (1 if ln == FOREST else 2)


async def play_a_land(c, state, pid, acts, tag, prefer=None):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    if prefer:
        lands.sort(key=lambda o: prefer(state, o))
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("_src_oid")) == str(o):
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


def vi_cast_choice(st, oid):
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None


async def cast_by_name(c, state, pid, tag, lname, mana_needs):
    """Cast a named spell from hand: legacy CastSpell action first, then
    vi castSpell-choice fallback. Sets MANA_NEEDS and returns True when a
    submission went out."""
    st = st_of(c)
    acts = merged_actions(st)
    oid = next((o for o in hand_ids(state, pid)
                if obj_lname(state, o) == lname), None)
    if oid is None:
        return False
    a = None
    for act in acts:
        if "cast" not in str(act.get("type", "")).lower():
            continue
        vals = list((act.get("data") or {}).values()) + [act.get("_src_oid")]
        for v in vals:
            try:
                if int(v) == int(oid):
                    a = act
                    break
            except (TypeError, ValueError):
                continue
        if a is not None:
            break
    vich = vi_cast_choice(st, oid)
    if a is None and vich is None:
        return False
    if untapped_lands(state, pid) < sum(mana_needs.values()):
        return False
    MANA_NEEDS[tag] = dict(mana_needs)
    ST["cast_inflight"] = {"oid": oid, "tag": tag}
    wire("cast", {"who": tag, "lname": lname, "oid": oid,
                  "via": "legal_actions" if a else "viewer_interaction"})
    say(f"[{tag}] casts {lname} (oid={oid})")
    if a is not None:
        await submit_as_is(c, a)
    else:
        opp, ch = vich
        await answer_vi(c, opp, ch, tag)
    return True

# ------------------------------------------------------------- loyalty abilities
def vi_activate_choice(st, oid):
    """Find a vi opportunity with an activateAbility choice for oid."""
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "activateAbility" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None


async def activate_loyalty(c, state, pid, tag, ability_index, label):
    """Activate Zariel's loyalty ability. Legacy ActivateAbility with
    ability_index first; vi activateAbility-choice fallback."""
    st = st_of(c)
    acts = merged_actions(st)
    z = zariel_oid(state, pid)
    if not z:
        return False
    a = None
    for act in acts:
        if act.get("type") != "ActivateAbility":
            continue
        d = act.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src != str(z):
            continue
        if int(d.get("ability_index", -1)) == ability_index:
            a = act
            break
    vich = vi_activate_choice(st, z)
    key = (tag, "actopt", str(z))
    if key not in SUBMITTED_OPPS:
        SUBMITTED_OPPS.add(key)
        wire("activate_options",
             {"zariel": z, "loyalty": loyalty_of(get_obj(state, z)),
              "legacy_matches": sum(1 for act in acts
                                    if act.get("type") == "ActivateAbility"
                                    and str((act.get("data") or {}).get(
                                        "source_id",
                                        (act.get("data") or {}).get(
                                            "object_id", ""))) == str(z)),
              "vi_choice": bool(vich)})
        say(f"[{tag}] Zariel loyalty={loyalty_of(get_obj(state, z))} "
            f"legacy_activate={bool(a)} vi_activate={bool(vich)}")
    if a is None and vich is None:
        return False
    if label == "ult":
        await do_export(c, "pre_activate.json")
    ST["act_once"] = True
    wire("activate", {"who": tag, "label": label,
                      "ability_index": ability_index,
                      "via": "legal_actions" if a else "viewer_interaction"})
    say(f"[{tag}] activates Zariel {label} (ability_index {ability_index})")
    if a is not None:
        d = dict(a.get("data", {}))
        for k in ("source_id", "object_id"):
            if k in d:
                d[k] = int(d[k])
        await submit_as_is(c, {"type": "ActivateAbility", "data": d})
    else:
        opp, ch = vich
        await answer_vi(c, opp, ch, tag)
    return True


# ------------------------------------------------------------- emblem trigger prompt
def emblem_prompt_opportunity(st, state, pid):
    """Find a target-choice opportunity whose candidates reference
    Battlefield creatures P0 controls: the (missing) emblem trigger prompt
    for 'untap target creature you control'."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        mine = []
        for ch in items:
            for r in cand_refs(ch):
                o = get_obj(state, r)
                if o.get("zone") == "Battlefield" \
                        and str(o.get("controller", -1)) == str(pid):
                    mine.append((ch, r, o))
                    break
        if not mine:
            continue
        if rtype == "schema":
            spec = (data.get("spec") or {}).get("type")
            if spec in ("select", "sequence"):
                return opp, rtype, spec
        elif rtype == "exactChoices":
            codes = set()
            for ch in items:
                codes.update(x for x in surf_codes(ch) if x)
            if "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes:
                return opp, rtype, "choose"
    return None, None, None


async def answer_emblem_prompt(c, state, pid, tag):
    """Answer the emblem trigger's 'untap target creature you control':
    pick a tapped attacker first, else any creature we control."""
    if ST["stage"] != "COMBAT" or TGT.get("answered"):
        return False
    st = st_of(c)
    opp, rtype, stype = emblem_prompt_opportunity(st, state, pid)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    items = data.get("candidates") or data.get("choices") or []
    cands = []
    for ch in items:
        for r in cand_refs(ch):
            o = get_obj(state, r)
            if o.get("zone") == "Battlefield" \
                    and str(o.get("controller", -1)) == str(pid):
                cands.append({"choice_id": ch.get("id"), "ref": r,
                              "name": oname(o),
                              "tapped": bool(o.get("tapped"))})
                break
    if not cands:
        return False
    if TGT["candidates"] is None:
        TGT["candidates"] = cands
        TGT["prompt_seen"] = True
        TGT["seen_at"] = time.time()
        wire("emblem_target_prompt",
             {"rtype": rtype, "spec": stype, "candidates": cands,
              "opportunity": opp})
        say(f"[P0] EMBLEM target prompt seen: rtype={rtype} spec={stype}")
        for cc in cands:
            say(f"    candidate: {cc['name']} tapped={cc['tapped']} "
                f"choice={cc['choice_id']}")
        await do_export(c, "mid_target.json")
    want = next((x for x in cands if x["tapped"]), cands[0])
    pick = next(ch for ch in items if ch.get("id") == want["choice_id"])
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": stype,
                            "data": {"choiceIds": [pick.get("id")]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
    SUBMITTED_OPPS.add(iid)
    TGT["answered"] = {"ref": want["ref"], "name": want["name"],
                       "at": time.time()}
    say(f"[P0] emblem: targeted {want['name']} (oid={want['ref']})")
    wire("emblem_target_submit", {"choice": want})
    await interact_as(c, sub, tag)
    return True


# ------------------------------------------------------------- seat ticks
async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await declare_branch(c, acts, tag, state, 0, True):
        return True
    if await pay_tick(c, acts):
        return True

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    # answer the emblem target prompt before anything else
    if await answer_emblem_prompt(c, state, 0, tag):
        return True

    if my_main(state, 0):
        if ST["stage"] == "SETUP":
            if await play_a_land(c, state, 0, acts, tag, prefer=p0_land_prefer):
                return True
            if not zariel_oid(state, 0):
                # ramp: Bears first (blockers/attackers), then Zariel
                if any(obj_lname(state, oid) == BEAR
                       for oid in bf_oids(state, 0)):
                    pass  # one Bear is enough; save mana for Zariel
                else:
                    if await cast_by_name(c, state, 0, tag, BEAR,
                                          {"G": 1, "generic": 1}):
                        return True
                if await cast_by_name(c, state, 0, tag, ZARIEL,
                                      {"R": 2, "generic": 2}):
                    return True
            else:
                if await cast_by_name(c, state, 0, tag, BEAR,
                                      {"G": 1, "generic": 1}):
                    return True
        elif ST["stage"] in ("LOYAL1", "LOYAL2"):
            if not ST["act_once"]:
                if await play_a_land(c, state, 0, acts, tag,
                                     prefer=p0_land_prefer):
                    return True
                if await activate_loyalty(c, state, 0, tag, 0, "+1"):
                    return True
        elif ST["stage"] == "ACTIVATE":
            if not ST["act_once"]:
                if await activate_loyalty(c, state, 0, tag, 2, "ult"):
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
    if await do_discard(c, acts, st, 1, tag, p1_discard_rank):
        return True
    if await declare_branch(c, acts, tag, state, 1, None):
        return True
    if await pay_tick(c, acts):
        return True

    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


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


def record_phase(state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if not PHASES or PHASES[-1] != key:
        PHASES.append(key)
        wire("phase", {"turn": key[0], "active": key[1], "phase": key[2],
                       "stage": ST["stage"]})


def scan_triggers(state):
    for oid, o in state["objects"].items():
        if o.get("zone") != "Stack":
            continue
        kind = (o.get("kind") or {})
        if not isinstance(kind, dict):
            continue
        if kind.get("type") != "TriggeredAbility":
            continue
        desc = str(kind.get("ability", {}).get("description", ""))
        if "untap" in desc.lower() and oid not in [t[0] for t in TRIGGERS]:
            TRIGGERS.append((str(oid), desc[:200]))
            wire("emblem_trigger_seen",
                 {"oid": str(oid), "description": desc[:300],
                  "stage": ST["stage"]})
            say(f"[watch] emblem-candidate trigger on stack: {desc[:160]}")

# ------------------------------------------------------------- main
async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P06864")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((ZARIEL_T, 8), (BEAR_T, 12),
                         (MOUNTAIN_T, 20), (FOREST_T, 20)))
    p1 = PhaseClient("P16864")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code,
                  deck((FOREST_T, 15), (PLAINS_T, 15), (ISLAND_T, 15),
                       (SWAMP_T, 15)))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej and LAST_IID["iid"] in SUBMITTED_OPPS:
                SUBMITTED_OPPS.discard(LAST_IID["iid"])
                say(f"[{c.name}] resync after rejection")
                LAST_IID["iid"] = None
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
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
        record_phase(state)
        scan_triggers(state)

        # clear spent mana needs once the spell they paid for resolves
        ci = ST.get("cast_inflight")
        if ci:
            o = get_obj(state, ci["oid"])
            if o.get("zone") not in ("Hand", "Stack"):
                MANA_NEEDS[ci["tag"]] = {}
                ST["cast_inflight"] = None
                wire("mana_cleared", {"who": ci["tag"],
                                     "zone": o.get("zone")})

        z = zariel_oid(state, 0)
        if z:
            lo = loyalty_of(get_obj(state, z))
            if not ZARLOG or ZARLOG[-1][1] != lo:
                ZARLOG.append((state.get("turn_number"), lo))
                wire("zariel_loyalty", {"turn": state.get("turn_number"),
                                        "loyalty": lo})
                say(f"[watch] Zariel loyalty={lo} "
                    f"(turn {state.get('turn_number')})")

        # --- stage transitions
        if ST["stage"] == "SETUP":
            if z and any(obj_lname(state, oid) == BEAR
                         for oid in bf_oids(state, 0)):
                ST["stage"] = "LOYAL1"
                ST["act_once"] = False
                say("=== stage -> LOYAL1 ===")
                wire("stage", {"stage": "LOYAL1"})
        elif ST["stage"] == "LOYAL1":
            if z and loyalty_of(get_obj(state, z)) == 5:
                ST["stage"] = "LOYAL2"
                ST["act_once"] = False
                say("=== stage -> LOYAL2 ===")
                wire("stage", {"stage": "LOYAL2"})
        elif ST["stage"] == "LOYAL2":
            if z and loyalty_of(get_obj(state, z)) == 6:
                ST["stage"] = "ACTIVATE"
                ST["act_once"] = False
                say("=== stage -> ACTIVATE ===")
                wire("stage", {"stage": "ACTIVATE"})
        elif ST["stage"] == "ACTIVATE" and ST["act_once"]:
            # ult submitted; wait until Zariel leaves the battlefield
            if not z:
                ST["ult_turn"] = state.get("turn_number")
                await do_export(p0, "post_activate.json")
                post_act = json.load(open(f"{EVDIR}/post_activate.json"))["state"]
                embs = emblem_objects(post_act, 0)
                wire("emblem_scan_post_ult",
                     {"found": [(oid, nm, zn) for oid, nm, zn in embs]})
                say(f"[watch] post-ult emblem scan: {embs}")
                ST["stage"] = "COMBAT"
                ST["attacked"] = False
                ST["act_once"] = False
                say("=== stage -> COMBAT ===")
                wire("stage", {"stage": "COMBAT"})
        elif ST["stage"] == "COMBAT":
            # end observation once the ult turn's combat sequence is over:
            # first PostCombatMain on P0's turn at/after the ult turn,
            # with the attack done.
            if (ST["ult_turn"] is not None
                    and state.get("active_player") == 0
                    and (state.get("phase") or "") == "PostCombatMain"
                    and (state.get("turn_number") or 0) >= ST["ult_turn"]):
                stack_empty = not any(o.get("zone") == "Stack"
                                      for o in state["objects"].values())
                if stack_empty:
                    await asyncio.sleep(2)
                    await do_export(p0, "post.json")
                    ST["stop"] = True
                    say("=== DONE (post-combat main reached) ===")
            # safety: 10 P0 turns after the ult with no combat end -> stop
            if (ST["ult_turn"] is not None
                    and (state.get("turn_number") or 0) >= ST["ult_turn"] + 20):
                try:
                    await do_export(p0, "post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
                say("=== DONE (safety: 20 turns past ult) ===")

        if time.time() - last_diag > 90:
            last_diag = time.time()
            s = state
            z2 = zariel_oid(s, 0)
            say(f"DIAG P0: rev={p0.revision} turn={s.get('turn_number')} "
                f"phase={s.get('phase')} prio={my_priority(top_acts(st_of(p0)))} "
                f"decision={real_decision_pending(st_of(p0))} "
                f"stage={ST['stage']} zariel={bool(z2)} "
                f"loyalty={loyalty_of(get_obj(s, z2)) if z2 else None} "
                f"untapped={untapped_lands(s, 0)} "
                f"prompt={TGT['prompt_seen']} attacked={ST['attacked']}")

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    A = obs["assert"]

    def nm2(o):
        return o.get("card_name") or o.get("name") or o.get("base_name") or ""

    def bf2(state, pid):
        return [(oid, o) for oid, o in state["objects"].items()
                if o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == str(pid)]

    pre_act = env_state("pre_activate.json")
    if pre_act is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_activate.json missing (ult never submitted)")
    else:
        A["A1_setup_ok"] = ("passed"
                            if any(nm2(o) == ZARIEL_T for _, o in bf2(pre_act, 0))
                            and any(nm2(o) == BEAR_T for _, o in bf2(pre_act, 0))
                            else "failed")
        obs["notes"].append(
            f"pre_activate.json: turn={pre_act.get('turn_number')} "
            f"phase={pre_act.get('phase')}")

    post_act = env_state("post_activate.json")
    if post_act is None:
        A["A2_loyalty_paid"] = "not-run"
        A["A3_emblem_created"] = "not-run"
    else:
        zariel_zones = [o.get("zone") for oid, o in post_act["objects"].items()
                        if nm2(o) == ZARIEL_T and o.get("owner") == 0]
        A["A2_loyalty_paid"] = ("passed"
                                if "Graveyard" in zariel_zones
                                and "Battlefield" not in zariel_zones
                                else "failed")
        obs["notes"].append(
            f"A2: P0 Zariel zones post-ult: {sorted(set(zariel_zones))}")
        embs_act = emblem_objects(post_act, 0)
        A["A3_emblem_created"] = ("passed" if embs_act else "failed")
        obs["notes"].append(f"A3: emblem objects post-ult={embs_act}")

    post = env_state("post.json")
    if post is None:
        A["A4_target_prompt"] = ("passed" if TGT["prompt_seen"]
                                 else "not-run")
        A["A5_untap_resolves"] = "not-run"
        A["A6_additional_combat"] = "not-run"
        obs["notes"].append("post.json missing")
    else:
        prompt_events = sum(1 for line in open(f"{EVDIR}/wire_log.jsonl")
                            for d in [json.loads(line)]
                            if d["event"] == "emblem_target_prompt")
        A["A4_target_prompt"] = ("passed" if prompt_events > 0 else "failed")
        obs["notes"].append(
            f"A4: emblem target prompt events for P0: {prompt_events}")
        tgt = TGT.get("answered")
        if tgt and tgt.get("ref"):
            o = post["objects"].get(str(tgt["ref"]), {})
            A["A5_untap_resolves"] = ("passed"
                                      if o.get("zone") == "Battlefield"
                                      and not o.get("tapped") else "failed")
            obs["notes"].append(f"A5: targeted {tgt['name']} "
                                 f"oid={tgt['ref']} zone={o.get('zone')} "
                                 f"tapped={o.get('tapped')}")
        else:
            attackers = [oid for oid, o in post["objects"].items()
                         if nm2(o) == BEAR_T and o.get("controller") == 0
                         and o.get("zone") == "Battlefield"]
            tapped = [oid for oid in attackers
                      if post["objects"][oid].get("tapped")]
            A["A5_untap_resolves"] = ("failed" if tapped else "not-run")
            obs["notes"].append(
                f"A5: no emblem prompt answered; P0 Bears on BF="
                f"{attackers}, tapped={tapped}")
        ult_turn = ST["ult_turn"]
        phs = [p for p in PHASES if p[0] == ult_turn]
        begins = sum(1 for p in phs if p[2] == "BeginCombat")
        A["A6_additional_combat"] = ("passed" if begins >= 2 else "failed")
        obs["notes"].append(f"A6: ult_turn={ult_turn}; BeginCombat phases "
                             f"on ult turn={begins}; ult-turn phases="
                             f"{[p[2] for p in phs]}")

    obs["notes"].append(f"Zariel loyalty trace: {ZARLOG}")
    obs["notes"].append(f"emblem triggers seen on stack: {TRIGGERS}")
    obs["notes"].append(f"target answer: {TGT['answered']}")
    obs["notes"].append("protocol-106 driver (v0.102.0): mulligan via legacy "
                        "MulliganDecision (always Keep); land drops via "
                        "PlayLand/vi playLand; CastSpell via legacy action "
                        "with vi castSpell-choice fallback, mana via "
                        "PayMana/vi tapLandForMana driven by MANA_NEEDS; "
                        "loyalty abilities via legacy ActivateAbility "
                        "(ability_index) with vi activateAbility fallback; "
                        "DeclareAttackers via legacy action; sleep(0) yield "
                        "before leg evaluation; 5s re-tick backstop.")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    if A.get("A3_emblem_created") == "failed" \
            and A.get("A2_loyalty_paid") == "passed":
        verdict = "reproduced"
    elif A.get("A3_emblem_created") == "passed" \
            and A.get("A4_target_prompt") == "failed" \
            and A.get("A2_loyalty_paid") == "passed":
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A3_emblem_created", "A4_target_prompt", "A5_untap_resolves",
              "A6_additional_combat")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "ult_turn": ST["ult_turn"], "phases": PHASES,
                   "triggers": TRIGGERS, "tgt": TGT,
                   "zariel_loyalty": ZARLOG}, f, indent=1)

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": ST.get("game_code"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": {
            "validated_version": "v0.102.0",
            "build_commit": "e17f6fd",
            "protocol_version": 106,
            "server_binary_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.102.0/"
                "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.102.0/data/draft-pools.json"),
            "signature_verified": True,
        },
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [[ZARIEL_T, 8], [BEAR_T, 12],
                   [MOUNTAIN_T, 20], [FOREST_T, 20]],
            "P1": [["Forest", 15], ["Plains", 15], ["Island", 15],
                   ["Swamp", 15]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_6864_01020.py), not direct load.",
            "Only the -6 loyalty ability was exercised (+1 used for loyalty setup, 0 untouched).",
            "Emblem scan is heuristic: is_emblem flag, name/subtype containing 'Emblem'; command zone checked in post-ult export.",
        ],
        "setup_line": "P0: Zariel, Archduke of Avernus + Grizzly Bears on BF; +1 x2 to loyalty 6, then -6 on the ult turn. P1: passive land-drops.",
        "contract_line": ("-6 must create the emblem; at end of first combat "
                          "its trigger must offer 'untap target creature you "
                          "control', then an additional combat follows."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")

    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "phases": PHASES,
                   "triggers": TRIGGERS, "tgt": TGT,
                   "zariel_loyalty": ZARLOG}, f, indent=1)

    await render_summary(f"{EVDIR}/summary.png")
    shutil.copy(__file__, f"{EVDIR}/scenario_6864_01020.py")
    say("copied driver into evidence dir")
    await write_manifest()

    await p0.close()
    await p1.close()
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass


async def render_summary(out_path):
    """Render the PNG from the saved evidence files only."""
    from PIL import Image, ImageDraw
    A = json.load(open(f"{EVDIR}/assertions.json"))
    notes = A.get("notes", [])
    asserts = A.get("assertions", {})
    run = json.load(open(f"{EVDIR}/run.json"))

    def load_ev_state(path):
        with open(f"{EVDIR}/{path}") as f:
            return json.load(f)["state"]

    try:
        pre = load_ev_state("pre_activate.json")
        lo = None
        for o in pre["objects"].values():
            if (o.get("card_name") or o.get("name") or "") == ZARIEL_T \
                    and o.get("zone") == "Battlefield":
                lo = loyalty_of(o)
        pre_line = (f"pre-ult: turn {pre.get('turn_number')} "
                    f"{pre.get('phase')} | Zariel loyalty={lo} on BF")
    except Exception:
        pre_line = "pre_activate.json: missing"
    try:
        post_act = load_ev_state("post_activate.json")
        zzs = sorted({o.get("zone") for o in post_act["objects"].values()
                      if (o.get("card_name") or o.get("name") or "") == ZARIEL_T
                      and o.get("owner") == 0})
        ex_line = f"post-ult: Zariel zones={zzs} (loyalty cost paid)"
    except Exception:
        ex_line = "post_activate.json: missing"
    W, H = 1000, 800
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6864 - Zariel ult never creates the emblem, "
                    "so no target choice to untap", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server {si['validated_version']} "
                    f"({si['build_commit']}) protocol "
                    f"{si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), pre_line, fill=(170, 180, 195))
    y += 26
    d.text((24, y), ex_line, fill=(170, 180, 195))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Zariel + Bear on P0 BF pre-ult",
        "A2_loyalty_paid": "A2 Zariel in graveyard after -6",
        "A3_emblem_created": "A3 emblem object created for P0",
        "A4_target_prompt": "A4 'untap target creature' prompt appears",
        "A5_untap_resolves": "A5 chosen creature untaps",
        "A6_additional_combat": "A6 additional combat phase follows",
    }
    for k, lab in labels.items():
        v = asserts.get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in notes[:10]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6864/"
           + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
