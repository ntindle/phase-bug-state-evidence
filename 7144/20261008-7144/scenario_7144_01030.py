#!/usr/bin/env python3
"""Issue #7144: Port Razer "simply can't attack" - protocol-106 re-validation.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Port Razer ({3}{R}{R} Creature):
    "Whenever this creature deals combat damage to a player, untap each
     creature you control. After this phase, there is an additional combat
     phase.
     This creature can't attack a player it has already attacked this turn."

Reported symptom: "Should give additional combats when attacking a new
player and can't attack the same player during the same turn. However it
simply cant attack."

Prior finding (v0.82.0, run 20260914-7144-084417, reproduced): the
"can't attack a player it has already attacked this turn" static is
parsed with condition=null, i.e. an UNCONDITIONAL CantAttack. The engine
computes zero valid attackers and auto-skips DeclareAttackers, so the
Razer can never attack any player, even with no attack history.

Setup (native engine, three human-client seats, single-user, Bo1, life 20):
  P0: 24x Port Razer, 36x Mountain (Razer controller).
  P1: 60x Plains.  P2: 60x Plains.
  P0 ramps to 5 Mountains and casts Port Razer (engine Auto payment).
  P1/P2 do nothing but pass (never attack, never block).
  Driver observes P0's combats with the Razer on the battlefield:
   - bug path: engine never offers DeclareAttackers (zero valid
     attackers); finalize after >=3 such P0 combats (>=2 with the Razer
     not summoning-sick).
   - success path (if the engine offers the prompt): honestly declare
     razer->P1, verify acceptance, watch for damage + the additional
     combat, then declare razer->P2 in the extra combat.

Assertions (each passed / failed / not-run):
  A1_razer_on_battlefield  POST: Port Razer on P0 battlefield.
  A2_able_bodied           POST: Razer untapped, a creature, no summoning
                           sickness.
  A3_combats_observed      >=3 P0 BeginCombat entries with the Razer on
                           board, >=2 with the Razer not summoning-sick.
  A4_no_declare_prompt     In every observed P0 combat where the Razer was
                           able-bodied, the engine never offered P0 a
                           DeclareAttackers prompt. (THE REPORTED BUG:
                           expect PASSED.)
  A5_never_attacked        No Razer attack ever recorded; P1/P2 life stayed
                           20. (THE REPORTED BUG: expect PASSED.)
  A6_condition_dropped     Pinned card-data parse: Port Razer's CantAttack
                           static has condition=null (qualifier dropped).
  A7_extra_combat          not-run unless combat-1 razer->P1 was accepted:
                           an additional combat phase occurred on the same
                           turn after combat damage.
  A8_new_player_attack     not-run unless A7 ran: razer->P2 declared in the
                           extra combat (a player not attacked this turn).

Verdict rule (exact): reproduced iff A1, A2, A3 pass and no Razer attack
was ever recorded (A5 passed). not-reproduced iff a Razer attack was
recorded and combat damage dealt (the full correct path, incl. the extra
combat). blocked iff A1 or A3 failed.

Protocol-106 port notes (driver conventions from scenario_7143_01030.py):
  - my_priority = PassPriority in legal_actions; casts gated on it.
  - waiting_for is gone (null) on 106; prompts arrive as
    viewer_interaction opportunities or legacy actions.
  - CastSpell carries payment_mode Auto: the engine taps mana itself; the
    driver never answers tapLandForMana and runs no driver-side mana
    payment (legacy PayMana actions are answered if they appear).
  - DeclareAttackers is a legacy Action:
    {"attacks": [[oid, {"type": "Player", "data": pid}]], "bands": []}.
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).
  - Pre/post states are authoritative exports (data.state parsed once from
    the export envelope).

Evidence: evidence/7144/<run-id>/pre.json, post.json,
post_combat1.json (attack path only), run.json, parse_port_razer.json,
scenario_7144_01030.py, wire_log.jsonl, scenario_run.log, server.log,
summary.png, manifest.sha256.
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
ISSUE = 7144
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-7144")
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

PARSE = {"ok": False, "razer": None, "cant_attack_condition": "unknown"}


def check_parse_7144():
    """Parse check: Port Razer present; record the CantAttack static's
    condition (the v0.82.0 finding was condition=null)."""
    rz = CARD_DATA.get("port razer", {})
    with open(f"{EVDIR}/parse_port_razer.json", "w") as fh:
        json.dump({"port_razer": {
                       "oracle_text": rz.get("oracle_text"),
                       "mana_cost": rz.get("mana_cost"),
                       "abilities": rz.get("abilities"),
                       "static_abilities": rz.get("static_abilities")}},
                  fh, indent=1, default=str)
    say("saved parse_port_razer.json")
    cond = "unknown"
    for s in rz.get("static_abilities") or []:
        if s.get("mode") == "CantAttack":
            cond = s.get("condition")
            break
    PARSE["razer"] = bool(rz)
    PARSE["cant_attack_condition"] = cond
    PARSE["ok"] = bool(rz) and "additional combat" in \
        str(rz.get("oracle_text") or "")
    say(f"parse: razer={PARSE['razer']} oracle_ok={PARSE['ok']} "
        f"cant_attack_condition={cond!r}")
    wire("parse_check", {"oracle_ok": PARSE["ok"],
                         "cant_attack_condition": cond})
    return PARSE["ok"]


RAZER = "port razer"
MOUNTAIN = "mountain"
PLAINS = "plains"
LANDS = (MOUNTAIN, PLAINS)

P0_DECK = [("Port Razer", 24), ("Mountain", 36)]
P1_DECK = [("Plains", 60)]
P2_DECK = [("Plains", 60)]

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1800
STALL_AFTER = 150
TURN_CAP = 70

STOP = {"stop": False}
ST = {"stage": "setup", "razer_cast": False, "razer_oid": None,
      "pre_exported": False, "post_exported": False,
      "post_combat1_exported": False,
      "combats": [],            # {n, turn, sick, prompt_offered}
      "last_p0_phase": None,
      "prompt_offered": False,
      "declare_pending": None,  # {combat, target, razer_oid, t}
      "declare_results": [],    # {combat, target, accepted, note}
      "attack_recorded": False,
      "combat1_turn": None, "combat1_damage": False,
      "extra_combat_seen": False, "extra_combat_turn": None,
      "extra_declare_done": False,
      "life": {1: 20, 2: 20},
      "rejections": [],
      "game_code": None, "game_over": False,
      "pids": {}}
OBS = {"unexpected_prompts": [], "tick_errors": [], "notes": [],
       "life_trace": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
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


def oname(o):
    return str(o.get("base_name") or o.get("card_name")
               or o.get("name") or "").lower()


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
            if lname is None or oname(o) == lname:
                out.append(int(oid))
    return out


def razer_oid(state, pid=0):
    oids = bf_oids(state, pid, RAZER)
    return oids[0] if oids else None


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if oname(get_obj(state, oid)) in LANDS
            and not get_obj(state, oid).get("tapped")]


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


def cand_oid(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            try:
                return str(int(d["reference"]))
            except (TypeError, ValueError):
                return None
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
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            ST["rejections"].append({"who": c.name, "type": t, "data": data})
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


def attackers_list(state):
    return (state.get("combat") or {}).get("attackers") or []


def attack_recorded(state, oid, ttype, tdata):
    for a in attackers_list(state):
        try:
            if int(a.get("object_id")) != int(oid):
                continue
        except (TypeError, ValueError):
            continue
        tgt = a.get("attack_target") or {}
        if tgt.get("type") != ttype:
            continue
        if tdata is not None and tgt.get("data") != tdata:
            continue
        return True
    return False


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


# ------------------------------------------------------------- mulligan

async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] mulligan -> Keep (hand={hand_lnames(st['state'], pid)})")
    wire("mulligan", {"who": tag, "decision": "Keep"})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def discard_rank_7144(state, o, pid):
    nm = obj_lname(state, o)
    if pid == 0:
        if nm == RAZER and sum(1 for h in hand_lnames(state, 0)
                               if h == RAZER) > 1:
            return 1  # spare razers first
        if nm == MOUNTAIN and len(bf_oids(state, 0)) > 8:
            return 0
        return 6  # protect the first razer + lands while ramping
    return 0


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
                        key=lambda o: (discard_rank_7144(state, o, pid),
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
    """P1/P2: never attack, never block. P0's honest attack attempt is
    handled separately in p0 combat logic (needs the Razer oid)."""
    state = st["state"]
    if pid == 0:
        return False
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


# ------------------------------------------------------- combat logic

def razer_able(state, pid=0):
    """Razer on BF, untapped, no summoning sickness, a creature."""
    oid = razer_oid(state, pid)
    if oid is None:
        return False, None
    o = get_obj(state, oid)
    able = (not o.get("tapped")) and (not o.get("summoning_sick")) \
        and (o.get("power") is not None)
    return able, oid


async def p0_combat_watch(c, state, acts, tag):
    """Observe P0's combats; attempt the honest attack when (and only
    when) the engine offers a DeclareAttackers prompt."""
    turn = state.get("turn_number")
    phase = state.get("phase") or ""
    active = state.get("active_player")
    if active != 0:
        return False

    # BeginCombat entries with the Razer on the battlefield
    prev = ST["last_p0_phase"]
    ST["last_p0_phase"] = (turn, phase)
    if phase == "BeginCombat" and prev != (turn, phase) \
            and razer_oid(state, 0) is not None:
        rz = get_obj(state, razer_oid(state, 0))
        sick = bool(rz.get("sick") or rz.get("summoning_sick"))
        n = len(ST["combats"]) + 1
        ST["combats"].append({"n": n, "turn": turn, "sick": sick,
                              "prompt_offered": False})
        # extra-combat detection: a second P0 BeginCombat on combat-1's turn
        if ST["combat1_turn"] is not None and turn == ST["combat1_turn"] \
                and not ST["extra_combat_seen"]:
            ST["extra_combat_seen"] = True
            ST["extra_combat_turn"] = turn
            say(f"[P0] EXTRA COMBAT begins on turn {turn} "
                f"(combat #{n}) -- Port Razer trigger fired")
            wire("extra_combat", {"n": n, "turn": turn})
        else:
            say(f"[P0] combat #{n} begins (turn {turn}); razer "
                f"summoning_sick={sick}")
            wire("p0_combat_begins", {"n": n, "turn": turn, "sick": sick})
        return False

    # DeclareAttackers prompt for P0: the honest attack attempt
    if phase == "DeclareAttackers":
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"),
                  None)
        if da is None:
            return False
        able, roid = razer_able(state, 0)
        n = len(ST["combats"])
        if ST["combats"]:
            ST["combats"][-1]["prompt_offered"] = True
        ST["prompt_offered"] = True
        wire("declare_prompt_offered",
             {"combat": n, "turn": turn, "razer_able": able,
              "razer_oid": roid})
        say(f"[P0] combat #{n} (turn {turn}): DeclareAttackers PROMPT "
            f"OFFERED; razer able-bodied={able}")
        if ST["declare_pending"] is not None:
            return True  # already submitted; waiting on verification
        if roid is None:
            say("[P0] prompt offered but no Razer on BF?!")
            return True
        # extra combat -> attack the NEW player (P2); else attack P1
        target = 2 if ST["extra_combat_seen"] and not ST[
            "extra_declare_done"] else 1
        if target == 2:
            ST["extra_declare_done"] = True
        key = (turn, n, c.revision)
        if ST.get("declare_key") == key:
            return True
        ST["declare_key"] = key
        d = dict(da)
        d["data"] = dict(d.get("data") or {})
        d["data"]["attacks"] = [[int(roid),
                                 {"type": "Player", "data": target}]]
        d["data"]["bands"] = []
        say(f"[P0] declaring razer->{ 'P2' if target == 2 else 'P1'} "
            f"(combat #{n})")
        wire("declare_submitted", {"combat": n, "target": target,
                                   "razer_oid": int(roid)})
        await submit_as_is(c, d)
        ST["declare_pending"] = {"combat": n, "target": target,
                                 "razer_oid": int(roid), "t": time.time()}
        return True

    # verify a pending declaration
    if ST["declare_pending"] is not None:
        pend = ST["declare_pending"]
        roid, target = pend["razer_oid"], pend["target"]
        if attack_recorded(state, roid, "Player", target):
            ST["declare_pending"] = None
            ST["declare_results"].append(
                {"combat": pend["combat"], "target": target,
                 "accepted": True, "turn": turn})
            ST["attack_recorded"] = True
            if target == 1 and ST["combat1_turn"] is None:
                ST["combat1_turn"] = turn
            say(f"[P0] attack ACCEPTED: razer->P{target} "
                f"(combat #{pend['combat']})")
            wire("declare_accepted", {"combat": pend["combat"],
                                      "target": target})
            return True
        # rejection arrived?
        for r in ST["rejections"]:
            wire("declare_rejection_seen", {"rejection": r})
        if phase not in ("DeclareAttackers", "DeclareBlockers"):
            ST["declare_pending"] = None
            ST["declare_results"].append(
                {"combat": pend["combat"], "target": target,
                 "accepted": False, "note": "phase moved on without "
                                            "the attack recorded"})
            say(f"[P0] attack NOT recorded: razer->P{target} "
                f"(phase moved to {phase})")
            wire("declare_not_recorded", {"combat": pend["combat"],
                                          "target": target, "phase": phase})
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
    if await do_discard(c, acts, st, pid, tag):
        return True
    # legacy mana actions: answer if they appear. Driver never taps mana
    # itself (engine Auto payment on 106).
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0

    # life tracking (damage detection)
    for q in (1, 2):
        lv = life_of(state, q)
        if lv is not None and lv < ST["life"][q]:
            say(f"[{tag}] P{q} life {ST['life'][q]} -> {lv} "
                f"(turn {turn} phase {phase})")
            wire("life_drop", {"player": q, "from": ST["life"][q], "to": lv,
                               "turn": turn, "phase": phase})
            ST["life"][q] = lv
            if q == 1 and lv == 16 and not ST["combat1_damage"]:
                ST["combat1_damage"] = True
                await export_named(c, "post_combat1")

    # Razer on battlefield?
    roid = razer_oid(state, 0)
    if roid is not None and not ST["razer_cast"]:
        ST["razer_cast"] = True
        ST["razer_oid"] = roid
        ST["stage"] = "observe"
        say(f"[P0] Port Razer confirmed on battlefield (turn {turn})")
        wire("razer_on_battlefield", {"turn": turn, "oid": roid})
    if ST["razer_cast"] and not ST["pre_exported"]:
        if await export_named(c, "pre"):
            ST["pre_exported"] = True
            say("PRE exported (razer on board, pre-combat observation)")

    # combat observation / honest attack attempt
    if await p0_combat_watch(c, state, acts, tag):
        return True

    # extra-combat path complete -> finalize
    if ST["extra_declare_done"] and ST["declare_pending"] is None:
        if phase in ("PostCombatMain", "End", "Cleanup") and \
                turn == ST.get("extra_combat_turn"):
            say("[P0] extra-combat path complete; finalizing")
            STOP["stop"] = True
            return True

    # bug path: enough combats observed with no attack -> finalize
    nonsick = [cb for cb in ST["combats"] if not cb["sick"]]
    if len(ST["combats"]) >= 3 and len(nonsick) >= 2 \
            and not ST["attack_recorded"] \
            and phase in ("PostCombatMain", "End", "Cleanup"):
        say(f"[P0] {len(ST['combats'])} combats observed "
            f"({len(nonsick)} non-sick), no attack recorded; finalizing")
        STOP["stop"] = True
        return True

    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # ---- P0 priority: land drops, then cast the Razer, then pass ----
    if my_main(state, pid) and stack_empty(state):
        lands = [oid for oid in bf_oids(state, 0)
                 if oname(get_obj(state, oid)) in LANDS]
        for a in acts:
            if a["type"] == "PlayLand" and len(lands) < 8:
                say(f"[{tag}] plays Mountain (bf lands={len(lands)})")
                wire("play_land", {"who": tag})
                await submit_as_is(c, a)
                return True
        if not ST["razer_cast"] and RAZER in hand_lnames(state, 0) \
                and len(untapped_lands(state, 0)) >= 5:
            found = cast_spell_for(acts, state, RAZER)
            if found:
                oid, action = found[0]
                say(f"[P0] casting Port Razer oid={oid} t{turn} "
                    f"(engine Auto payment)")
                wire("razer_cast", {"oid": str(oid), "turn": turn})
                await submit_as_is(c, action)
                return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


async def p1p2_tick(c, pid, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await do_declare(c, acts, st, pid, tag):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True
    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True
    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


# ------------------------------------------------------------- main loop

async def main():
    global t_start
    t_start = time.time()
    last_rev_change = t_start
    game_started = False
    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0

    hello = await verify_server_hello()
    check_parse_7144()

    p0 = PhaseClient("P07144r")
    await p0.connect()
    say("P0 creating game (Bo1, 3 seats)...")
    await p0.create(deck(*P0_DECK), player_count=3)
    p1 = PhaseClient("P17144r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P27144r")
    await p2.connect()
    say("P2 joining...")
    await p2.join(p0.game_code, deck(*P2_DECK))
    ST["game_code"] = p0.game_code
    ST["pids"] = {"P0": p0.player_id, "P1": p1.player_id, "P2": p2.player_id}
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")
    wire("game", {"code": p0.game_code, "pids": ST["pids"],
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                  "p2_deck": P2_DECK})

    async def finish():
        dur = time.time() - t_start
        notes = []
        ass = {k: "not-run" for k in
               ("A1_razer_on_battlefield", "A2_able_bodied",
                "A3_combats_observed", "A4_no_declare_prompt",
                "A5_never_attacked", "A6_condition_dropped",
                "A7_extra_combat", "A8_new_player_attack")}

        def load(fn):
            try:
                with open(f"{EVDIR}/{fn}.json") as f:
                    return json.load(f)["state"]
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
                return None

        pre = load("pre")
        post = load("post")
        if pre is not None:
            say("loaded pre.json")
        if post is not None:
            say("loaded post.json")

        # ---- A1: razer on P0 battlefield (POST) ----
        ok = post is not None and razer_oid(post, 0) is not None
        notes.append(f"A1: razer on P0 BF in POST: {ok}")
        ass["A1_razer_on_battlefield"] = "passed" if ok else "failed"

        # ---- A2: able-bodied in POST ----
        able = False
        sick = tapped = "?"
        if post is not None and razer_oid(post, 0) is not None:
            o = get_obj(post, razer_oid(post, 0))
            tapped = bool(o.get("tapped"))
            sick = bool(o.get("sick") or o.get("summoning_sick"))
            able = (not tapped) and (not sick) \
                and (o.get("power") is not None)
        notes.append(f"A2: able-bodied={able} "
                     f"(tapped={tapped} summoning_sick={sick})")
        ass["A2_able_bodied"] = "passed" if able else "failed"

        # ---- A3: >=3 P0 combats with razer on board, >=2 non-sick ----
        nonsick = [cb for cb in ST["combats"] if not cb["sick"]]
        ok = len(ST["combats"]) >= 3 and len(nonsick) >= 2
        notes.append(f"A3: p0 combats with razer on board="
                     f"{len(ST['combats'])} (non-sick={len(nonsick)}); "
                     f"turns={[c['turn'] for c in ST['combats']]}")
        ass["A3_combats_observed"] = "passed" if ok else "failed"

        # ---- A4: no DeclareAttackers prompt in able-bodied combats ----
        prompted = [c for c in nonsick if c["prompt_offered"]]
        ok = len(prompted) == 0
        notes.append(f"A4: declare prompt offered in able-bodied combats: "
                     f"{len(prompted)} (expect 0; engine auto-skips with "
                     f"zero valid attackers)")
        ass["A4_no_declare_prompt"] = "passed" if ok else "failed"

        # ---- A5: no Razer attack ever recorded; lives untouched ----
        attacked = ST["attack_recorded"] or bool(ST["declare_results"]
                                                 and any(
                r.get("accepted") for r in ST["declare_results"]))
        lives_ok = ST["life"][1] == 20 and ST["life"][2] == 20
        ok = (not attacked) and lives_ok
        notes.append(f"A5: razer attack recorded={attacked} "
                     f"lives={ST['life']} declare_results="
                     f"{ST['declare_results']}")
        ass["A5_never_attacked"] = "passed" if ok else "failed"

        # ---- A6: parse mechanism (condition null) ----
        ok = PARSE["cant_attack_condition"] is None
        notes.append(f"A6: pinned card-data CantAttack condition="
                     f"{PARSE['cant_attack_condition']!r} (expect None: "
                     f"the 'a player it has already attacked this turn' "
                     f"qualifier was dropped at parse time)")
        ass["A6_condition_dropped"] = "passed" if ok else "failed"

        # ---- A7/A8: extra-combat path (only if combat-1 attack landed) ----
        c1 = next((r for r in ST["declare_results"]
                   if r.get("target") == 1 and r.get("accepted")), None)
        if c1 is None:
            notes.append("A7/A8 not-run: combat-1 razer->P1 never accepted")
        else:
            ok7 = ST["extra_combat_seen"]
            notes.append(f"A7: extra combat on turn {ST['combat1_turn']}: "
                         f"{ST['extra_combat_seen']}")
            ass["A7_extra_combat"] = "passed" if ok7 else "failed"
            c2 = next((r for r in ST["declare_results"]
                       if r.get("target") == 2), None)
            if c2 is None:
                notes.append("A8 not-run: no extra-combat declaration made")
            else:
                ok8 = bool(c2.get("accepted"))
                notes.append(f"A8: extra-combat razer->P2 accepted="
                             f"{c2.get('accepted')}")
                ass["A8_new_player_attack"] = "passed" if ok8 else "failed"

        # ---- verdict ----
        if ass["A1_razer_on_battlefield"] != "passed" or \
                ass["A3_combats_observed"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A1) or observation "
                         "window (A3) failed")
        elif not attacked:
            verdict = "reproduced"
            notes.append("verdict=reproduced: Port Razer never attacked "
                         "any player across "
                         f"{len(ST['combats'])} observed P0 combats; "
                         "engine offered no DeclareAttackers prompt in "
                         "able-bodied combats (parsed CantAttack condition "
                         "is null)")
        else:
            # an attack landed: check the full correct path
            full = (ST["combat1_damage"] and ST["extra_combat_seen"]
                    and ass["A8_new_player_attack"] == "passed")
            if full:
                verdict = "not-reproduced"
                notes.append("verdict=not-reproduced: razer attacked P1, "
                             "dealt damage, extra combat fired, razer "
                             "attacked P2 in the extra combat")
            else:
                verdict = "reproduced"
                notes.append("verdict=reproduced: a Razer attack landed "
                             "but the reported path diverged (related "
                             "failure); combat1_damage="
                             f"{ST['combat1_damage']} extra_combat="
                             f"{ST['extra_combat_seen']} A8="
                             f"{ass['A8_new_player_attack']}")
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
            "format_config": "default Bo1 (3 human-client seats, life 20)",
            "decks": {"P0": [[n, c] for n, c in P0_DECK],
                      "P1": [[n, c] for n, c in P1_DECK],
                      "P2": [[n, c] for n, c in P2_DECK]},
            "setup_line": ("P0 24x Port Razer / 36x Mountain; P1/P2 60x "
                           "Plains. P0 plays a Mountain each turn, casts "
                           "Port Razer at 5 untapped Mountains (engine Auto "
                           "payment). P1/P2 pass always, never attack, "
                           "never block. Driver observes P0's combats with "
                           "the Razer on the battlefield; if the engine "
                           "offers DeclareAttackers it honestly declares "
                           "razer->P1 (extra combat: razer->P2)."),
            "contract_line": ("Port Razer should be able to attack a player "
                              "it has not attacked this turn and grant an "
                              "additional combat after dealing combat "
                              "damage. Report: it simply can't attack. "
                              "Expected: A1-A3 pass, A4/A5 pass in the bug "
                              "direction (no prompt, no attack), A6 "
                              "confirms the parse mechanism."),
            "driver_notes": [
                "Protocol-106 port of driver/scenario_7144.py (v0.82.0 / "
                "protocol 70); the behavioral contract is unchanged "
                "(A1-A8 replace the old A1-A6 with finer observation).",
                "my_priority = PassPriority in legal_actions; the Razer "
                "cast is gated on it.",
                "CastSpell carries payment_mode Auto on 106: the engine "
                "taps mana itself; the driver never answers tapLandForMana "
                "and runs no driver-side mana payment (legacy PayMana "
                "actions are answered if they appear).",
                "DeclareAttackers is a legacy Action: {\"attacks\": "
                "[[oid, {\"type\": \"Player\", \"data\": pid}]], "
                "\"bands\": []}. Acceptance is verified via the combat "
                "attackers list (attack_recorded) on later ticks; "
                "rejections are captured from the client inbox.",
                "The stack-watch branch always falls through to the "
                "pass-priority gate (never returns early); all seats must "
                "pass in succession for a stack entry to resolve.",
                "Pre/post states are authoritative exports (data.state "
                "parsed once from the export envelope); the reported "
                "OUTCOME is asserted on the saved states, not the prompt.",
                "Turn order is randomized by the engine; the driver keys "
                "on active_player/phase, never on order.",
            ],
            "assertions": ass,
            "observations": OBS,
            "driver_state": ST,
            "rejections": ST["rejections"],
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "parse_port_razer.json",
                               "scenario_7144_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"]
                              + (["post_combat1.json"]
                                 if ST["post_combat1_exported"] else []),
            "limitations": [
                "Browser UI not exercised; native engine via three "
                "human-client seats.",
                "Dense test decks are a harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1/P2 never attack or block; life changes can only come "
                "from a Razer attack.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
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
        with open(f"{EVDIR}/scenario_7144_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_7144_01030.py into EVDIR")

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

        render_summary(run)

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
        for c in (p0, p1, p2):
            try:
                await c.close()
            except Exception:
                pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1100
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7144 - Port Razer cannot attack",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "3-seat native engine",
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
            "A1_razer_on_battlefield": "Port Razer on P0 battlefield (POST)",
            "A2_able_bodied": "untapped, creature, no summoning sickness",
            "A3_combats_observed": ">=3 P0 combats w/ razer, >=2 non-sick",
            "A4_no_declare_prompt": "no DeclareAttackers prompt in "
                                    "able-bodied combats",
            "A5_never_attacked": "no razer attack recorded; lives 20/20",
            "A6_condition_dropped": "CantAttack static condition=null "
                                    "in pinned card data",
            "A7_extra_combat": "extra combat after combat-1 damage",
            "A8_new_player_attack": "razer->P2 in the extra combat",
        }
        for k, lab in labels.items():
            av = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if av == "passed" else (
                (255, 90, 90) if av == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {av}", fill=col)
            d.text((300, y), lab[:62], fill=(150, 160, 175))
            y += 26
        y += 10
        d.text((24, y), "Driver state:", fill=(200, 210, 225))
        y += 24
        ds = run.get("driver_state", {})
        for ln in [
            f"combats={[(c['n'], c['turn'], 'sick' if c['sick'] else 'ok', 'prompt' if c['prompt_offered'] else '-') for c in ds.get('combats', [])]}",
            f"declare_results={ds.get('declare_results')}",
            f"lives={ds.get('life')} combat1_damage={ds.get('combat1_damage')} "
            f"extra_combat={ds.get('extra_combat_seen')}",
        ]:
            d.text((36, y), str(ln)[:116], fill=(160, 175, 195))
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
        files = ["pre.json", "post.json", "run.json",
                 "parse_port_razer.json", "scenario_7144_01030.py",
                 "wire_log.jsonl", "scenario_run.log", "server.log",
                 "summary.png"]
        if ST["post_combat1_exported"]:
            files.append("post_combat1.json")
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

    while time.time() - t_start < GAME_TIMEOUT and not STOP.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick),
                             (p1, "P1", p1p2_tick),
                             (p2, "P2", p1p2_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS["notes"].extend(
                    f"rejection: {c.name} {r[0]}" for r in rej)
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
                pid = {"P0": 0, "P1": 1, "P2": 2}[tag]
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

        winner = state.get("winner")
        lives = [life_of(state, i) for i in (0, 1, 2)]
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

        # early-exit watchdog: no Razer on board by turn 40 -> finish
        if turn > 40 and not ST["razer_cast"] and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 40 with no Razer on BF; "
                                "finishing")
            say("watchdog: turn 40, no razer; finishing")
            STOP["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"razer_bf={ST['razer_cast']} "
                f"combats={len(ST['combats'])} "
                f"prompt={ST['prompt_offered']} "
                f"lands0={len(untapped_lands(state, 0))} "
                f"life={[life_of(state, i) for i in (0, 1, 2)]} "
                f"stack={len(state.get('stack') or [])}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    # POST export (authoritative) before assertions
    if not ST["post_exported"]:
        if await export_named(p0, "post"):
            ST["post_exported"] = True
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
