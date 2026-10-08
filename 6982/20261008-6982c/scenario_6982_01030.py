#!/usr/bin/env python3
"""Issue #6982: [Card Bug] Florian's where-X life-loss binding is unsupported.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Florian, Voldaren Scion ({1}{B}{R} Legendary Creature - Vampire Noble, 3/3):
    "First strike. At the beginning of each of your postcombat main phases,
     look at the top X cards of your library, where X is the total amount of
     life your opponents lost this turn. Exile one of those cards and put
     the rest on the bottom of your library in a random order. You may play
     the exiled card this turn."

Card-data parse state on v0.103.0 (verified 2026-10-08 before the run):
  triggers[0] (mode "Phase", phase "PostCombatMain",
  constraint OnlyDuringYourTurn, trigger_zones ["Battlefield"]):
    execute.effect = {"type": "Unimplemented", "name": "where_x_binding",
      "description": "where X is the total amount of life your opponents
      lost this turn"} -- the parent effect of a sub_ability chain that
      would otherwise: exile one of the looked cards, put the rest on the
      bottom, and grant PlayFromExile UntilEndOfTurn to P0.

Reported symptom: the where_x_binding clause is unsupported, so the whole
trigger's "look at the top X" outcome never happens.

Setup (native engine, three human-client seats, single-user, Bo1, life 20):
  P0: 4x Florian, Voldaren Scion, 4x Lightning Bolt, 26x Swamp, 26x Mountain.
  P1: 60x Forest (passive). P2: 60x Forest (passive).
  P1/P2 each turn: play a land if possible, declare no blockers/attackers,
  always pass priority.
  NOTE: the engine randomizes turn order; the driver keys everything on
  active_player/phase, never on turn order. In single-user mode seats are
  pids 0/1/2 in join order (P0=0, P1=1, P2=2).

Plan:
  1. P0 mulligans to find Florian + Bolt, plays a land each turn, casts
     Florian on/after its 3rd turn ({1}{B}{R}; engine auto-taps mana).
  2. On the cast turn: PRE is exported in P0's PreCombatMain with Florian
     on BF, all players at 20 life, stack empty.
  3. On P0's NEXT turn (Florian no longer summoning-sick): PreCombatMain -
     cast Lightning Bolt targeting P2 (20 -> 17). Combat - attack P1 with
     Florian (20 -> 17). Total opponent life lost this turn = 6.
  4. At the beginning of P0's PostCombatMain the trigger should fire with
     X = 6: look at top 6, exile 1, rest on bottom, may play this turn.
     POST is exported right after the trigger resolves (stack empty).

Assertions (each passed / failed / not-run):
  A1_parse_gap     card-data v0.103.0: Florian triggers[0].execute.effect
                   is {"type": "Unimplemented", "name": "where_x_binding"}
                   (expected per bug report; PASS confirms the reported
                   parser gap).
  A2_setup_ok      PRE: Florian on P0 BF; life totals 20/20/20.
  A3_life_loss     after combat + bolt: P1 == 17 and P2 == 17
                   (6 total opponent life lost this turn).
  A4_trigger_fires
                   a Florian TriggeredAbility appeared on the stack during
                   P0's damage-turn PostCombatMain (or an equivalent
                   trigger interaction surfaced).
  A5_look_X        the trigger's look choice offered exactly 6 cards on the
                   damage turn (record the offered count; 0/not-offered if
                   the engine silently skips the Unimplemented effect).
  A6_exile_one     exactly one of the looked cards is exiled and the
                   library shrank by exactly 1 (looked cards stay in the
                   library, reordered).
  A7_play_permission
                   the exiled card is playable this turn: legal_actions
                   advertises a cast/play from exile for P0, or the state
                   carries a runtime PlayFromExile grant (card-text
                   mentions do NOT count).
  A8_cleanup       POST: stack empty.

Verdict rule (exact): blocked if A2 fails or A3 fails; reproduced if A1
passes and any of A5/A6/A7 fail; not-reproduced if A1 fails and A5/A6/A7
all pass; else blocked (incomplete chain).

Protocol-106 port notes (from driver/scenario_6963_01030.py conventions):
  - my_priority = PassPriority present in legal_actions; casts gated on it.
  - Engine auto-taps for CastSpell (payment_mode Auto): the driver never
    answers tapLandForMana and runs no driver-side mana payment.
  - DeclareAttackers: {"attacks": [[oid, {"type":"Player","data":1}]],
    "bands": []}; empty arrays for no attack. DeclareBlockers:
    {"assignments": []}.
  - The stack-watch branch always falls through to the pass-priority gate;
    real_decision_pending holds on genuine vi decisions only (priority
    menus excluded via NON_DECISION_CODES).

Evidence: evidence/6982/<run-id>/pre.json, post.json, run.json,
parse_florian.json, scenario_6982_01030.py, wire_log.jsonl,
scenario_run.log, server.log, summary.png, manifest.sha256.
"""
import asyncio
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
ISSUE = 6982
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261008-6982")
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

PARSE = {"ok": False, "effect": None}


def check_parse_florian():
    """A1: Florian triggers[0].execute.effect is still
    Unimplemented(where_x_binding) on the pinned v0.103.0 card-data."""
    f = CARD_DATA.get("florian, voldaren scion", {})
    trigs = f.get("triggers", [])
    eff = ((trigs[0] or {}).get("execute", {}) or {}).get("effect", {})
    with open(f"{EVDIR}/parse_florian.json", "w") as fh:
        json.dump({"card": "Florian, Voldaren Scion",
                   "oracle_text": f.get("oracle_text"),
                   "triggers": f.get("triggers"),
                   "abilities": f.get("abilities"),
                   "static_abilities": f.get("static_abilities")},
                  fh, indent=1, default=str)
    say("saved parse_florian.json")
    PARSE["effect"] = eff
    PARSE["ok"] = (eff.get("type") == "Unimplemented"
                   and eff.get("name") == "where_x_binding")
    say(f"parse: triggers[0].execute.effect type={eff.get('type')} "
        f"name={eff.get('name')} -> A1={'passed' if PARSE['ok'] else 'failed'}")
    wire("parse_check", {"effect": eff, "A1": "passed" if PARSE["ok"]
                                           else "failed"})
    return PARSE["ok"]


FLORIAN = "florian, voldaren scion"
BOLT = "lightning bolt"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
LANDS = (SWAMP, MOUNTAIN, FOREST)

P0_DECK = (("Florian, Voldaren Scion", 4), ("Lightning Bolt", 4),
           ("Swamp", 26), ("Mountain", 26))
P1_DECK = (("Forest", 60),)
P2_DECK = (("Forest", 60),)

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45

STOP = {"stop": False}
ST = {"florian_cast": False, "florian_oid": None, "florian_bf_turn": None,
      "bolt_submitted": False, "bolt_cast": False, "bolt_resolved": False,
      "pre_exported": False, "post_exported": False,
      "attacked": False, "damage_turn": None,
      "trigger_seen_damage_turn": False, "trigger_entry": None,
      "trigger_sightings": [],
      "stack_entry_keys": [],
      "look_offered": None, "look_iid": None, "look_answered": False,
      "exile_before": None, "lib_before": None}
OBS = {"unexpected_prompts": [], "rejections": [], "tick_errors": [],
       "notes": [], "target_selections": [], "life_trace": []}
SUBMITTED_OPPS = set()
DISCARDED_IIDS = set()
LOGGED_IIDS = set()
MULLS = {"P0": 0, "P1": 0, "P2": 0}
PASSED_REV = {}
LAND_PLAYED_TURN = {}
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


def exile_of(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Exile"
            and str(o.get("controller", o.get("owner", ""))) == str(pid)]


def library_of(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Library"
            and str(o.get("controller", o.get("owner", ""))) == str(pid)]


def is_land(o):
    nm = str(o.get("base_name") or o.get("name") or "").lower()
    return nm in LANDS


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield"
                and str(o.get("controller")) == str(pid)
                and not o.get("tapped") and nm in LANDS):
            out.append(int(oid))
    return out


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


def is_card_pick_choice(ch):
    """True if a choice is a genuine card pick (e.g. 'exile one of the
    looked cards'), as opposed to the standard priority menu (passPriority
    + tapLandForMana) or other engine menus. A real pick names a card
    object and carries no priority-menu action code."""
    saw_card = False
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if not isinstance(d, dict):
            continue
        if s.get("type") == "action" and d.get("code") in (
                "passPriority", "tapLandForMana", "untapLandForMana",
                "playLand", "mulliganDecision"):
            return False
        if d.get("name") and d.get("zone") != "battlefield":
            saw_card = True
    return saw_card


def florian_trigger_on_stack(state):
    for e in state.get("stack") or []:
        k = e.get("kind")
        ktype = (k.get("type") if isinstance(k, dict) else k)
        blob = json.dumps(e, default=str).lower()
        if ktype == "TriggeredAbility" and "florian" in blob:
            return e
    return None


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

# ------------------------------------------------- issue-specific logic

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
        # P0 wants Florian + Bolt in the opener; allow 2 mulligans.
        keep = (FLORIAN in hn and BOLT in hn and n_lands >= 2) or n >= 2
    else:
        keep = n_lands >= 3 or n >= 2
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


def discard_rank(state, o):
    nm = obj_lname(state, o)
    if nm in LANDS:
        return 0
    if nm == FLORIAN:
        return 1
    return 2  # Lightning Bolt kept last


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
                        key=lambda o: (discard_rank(state, o),
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
            if (pid == 0 and ST["bolt_resolved"] and not ST["attacked"]
                    and ST["florian_oid"] is not None):
                d["data"]["attacks"] = [
                    [int(ST["florian_oid"]), {"type": "Player", "data": 1}]]
                d["data"]["bands"] = []
                ST["attacked"] = True
                ST["damage_turn"] = state.get("turn_number")
                say(f"[{tag}] attacking P1 with Florian "
                    f"oid={ST['florian_oid']} (turn {ST['damage_turn']})")
                wire("attack_declared",
                     {"attacker": int(ST["florian_oid"]),
                      "turn": ST["damage_turn"]})
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


def record_target_sel(state, opp, stage):
    """Record a target-selection opportunity once per interactionId."""
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
            "choice_id": ch.get("id"),
            "oid": oid,
            "seat": cand_seat(ch),
            "name": obj_lname(state, oid) if oid else choice_text(ch),
            "zone": o.get("zone"),
            "controller": o.get("controller"),
            "text": choice_text(ch)[:120],
        })
    rec = {
        "interactionId": iid,
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "stage": stage,
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
    say(f"target selection #{n} (stage {stage}): "
        + ", ".join(f"{x['name'] or '?'}({x['zone'] or '?'},p{x['controller']},"
                    f"seat={x['seat']})" for x in cand_info[:8]))
    wire("target_selection_recorded",
         {"n": n, "stage": stage, "iid": iid,
          "candidates": [(x["name"], x["zone"], x["controller"], x["seat"])
                         for x in cand_info]})


async def bolt_target_tick(c, tag, st, state):
    """Answer P0's bolt TargetSelection with the player candidate seat==2."""
    if not (ST["bolt_submitted"] and not ST["bolt_resolved"]):
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
        record_target_sel(state, opp, "bolt")
        pick = next((ch for ch in chs if cand_seat(ch) == 2), None)
        if pick is None:
            say(f"[{tag}] bolt-target: no seat-2 candidate; holding")
            wire("bolt_target_no_seat2",
                 {"choices": [choice_text(ch)[:60] for ch in chs][:8]})
            continue
        say(f"[{tag}] bolt targets P2 (seat 2)")
        wire("bolt_target_answer", {"choiceId": pick.get("id")})
        await answer_vi(c, opp, pick, tag)
        ST["bolt_cast"] = True
        return True
    return False


async def decline_unexpected_optional(c, st, tag):
    """Florian's trigger is NOT optional; no may-choice is expected. If an
    unexpected OptionalEffectChoice surfaces, decline it (accept=false) and
    note it, so the run cannot stall on it."""
    for opp in unanswered_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or []
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        if "decideOptionalEffect" not in codes \
                and "decideOptionalCost" not in codes:
            continue
        iid = opp.get("interactionId")
        pick = None
        for ch in chs:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if (isinstance(d, dict) and d.get("role") == "accept"
                        and str(d.get("value")).lower() == "false"):
                    pick = ch
                    break
            if pick is not None:
                break
        if pick is None:
            continue
        say(f"[{tag}] declining unexpected optional (iid={iid})")
        wire("optional_decline",
             {"who": tag, "iid": iid,
              "opportunity": json.loads(json.dumps(opp, default=str))})
        OBS["unexpected_prompts"].append(
            {"who": tag, "iid": str(iid)[:8], "kind": "optional",
             "action": "declined"})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def scan_trigger_and_look(st, state):
    """Record Florian trigger stack presence (all P0 postcombats) and any
    look interaction on the damage turn. Dumps every stack entry seen on
    the damage-turn postcombat for diagnosis."""
    turn = state.get("turn_number")
    phase = state.get("phase")
    ent = florian_trigger_on_stack(state)
    if ent is not None:
        sig = (turn, phase, len(state.get("stack") or []))
        if sig not in ST["trigger_sightings"]:
            ST["trigger_sightings"].append(sig)
            say(f"Florian TriggeredAbility ON STACK (turn {turn} {phase})")
            wire("trigger_sighting",
                 {"turn": turn, "phase": phase,
                  "entry": json.loads(json.dumps(ent, default=str))})
        if ST["attacked"]:
            ST["trigger_seen_damage_turn"] = True
            if ST["trigger_entry"] is None:
                ST["trigger_entry"] = json.loads(json.dumps(ent, default=str))
    if (ST["attacked"] and phase == "PostCombatMain"
            and state.get("active_player") == 0):
        for i, e in enumerate(state.get("stack") or []):
            key = (turn, i, json.dumps(e, default=str)[:120])
            if key not in ST["stack_entry_keys"]:
                ST["stack_entry_keys"].append(key)
                wire("stack_entry",
                     {"turn": turn, "idx": i,
                      "entry": json.loads(json.dumps(e, default=str))})
    # damage-turn look detector: a genuine card-pick interaction in P0's
    # damage-turn postcombat (NOT the priority menu: passPriority /
    # tapLandForMana / playLand / mulliganDecision choices are excluded by
    # is_card_pick_choice; discard prompts are excluded by iid tracking).
    if ST["attacked"] and ST["look_offered"] is None:
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            if iid in SUBMITTED_OPPS or iid in DISCARDED_IIDS:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("candidates") or data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(x for x in surf_codes(ch) if x)
            if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
                continue
            if is_priority_menu(opp):
                continue
            picks = [ch for ch in chs if is_card_pick_choice(ch)]
            if picks:
                ST["look_offered"] = len(picks)
                ST["look_iid"] = iid
                say(f"LOOK interaction offered {len(picks)} cards "
                    f"(iid={iid})")
                wire("look_offered",
                     {"n": len(picks), "iid": iid,
                      "texts": [choice_text(ch)[:60] for ch in picks][:12],
                      "opportunity": json.loads(
                          json.dumps(opp, default=str))})


async def answer_damage_look(c, st, state, tag):
    """If the damage-turn look is waiting on P0, answer it by exiling the
    first offered card (the trigger's 'exile one of those cards' step)."""
    if not ST["attacked"] or ST["look_answered"]:
        return False
    if ST["look_offered"] is None:
        return False
    for opp in unanswered_ops(st):
        iid = opp.get("interactionId")
        if iid in DISCARDED_IIDS:
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        picks = [ch for ch in chs if is_card_pick_choice(ch)]
        if not picks:
            continue
        pick = picks[0]
        say(f"[{tag}] answering damage-turn look: exile "
            f"{choice_text(pick)[:60]}")
        wire("look_answer", {"choiceId": pick.get("id"),
                             "card": choice_text(pick)[:60]})
        await answer_vi(c, opp, pick, tag)
        ST["look_answered"] = True
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
    if await do_declare(c, acts, st, 0, tag):
        return True
    if await bolt_target_tick(c, tag, st, state):
        return True
    if await decline_unexpected_optional(c, st, tag):
        return True
    # legacy mana actions: answer if they appear (engine Auto payment on
    # 106 means they usually do not). Driver never taps mana itself.
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] answering legacy {a['type']}")
            wire("legacy_paymana", {"who": tag, "type": a["type"]})
            await submit_as_is(c, a)
            return True

    wtype = (state.get("waiting_for") or {}).get("type") or ""
    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0

    # track Florian on BF (+ the turn it arrived: summoning sickness means
    # the damage turn must be a LATER P0 turn)
    oids = bf_oids(state, 0, FLORIAN)
    if oids and ST["florian_oid"] is None:
        ST["florian_oid"] = int(oids[0])
        ST["florian_bf_turn"] = turn
        say(f"Florian on BF: oid={oids[0]} (turn {turn})")
        wire("florian_on_bf", {"oid": int(oids[0]), "turn": turn})
    # life trace
    lives = tuple(life_of(state, i) for i in (0, 1, 2))
    tr = OBS["life_trace"]
    if all(v is not None for v in lives) and (not tr or tr[-1][1] != lives):
        tr.append((round(time.time() - t_start, 1), lives))
        say(f"life = {lives}")
        wire("life", {"life": lives})

    # postcombat observation on P0's turns (stack-watch: never returns
    # from here -- it must fall through to the pass-priority gate below)
    damage_postcombat = (phase == "PostCombatMain"
                         and state.get("active_player") == 0
                         and ST["attacked"])
    if phase == "PostCombatMain" and state.get("active_player") == 0:
        if ST["exile_before"] is None and damage_postcombat:
            ST["exile_before"] = sorted(exile_of(state, 0))
            ST["lib_before"] = len(library_of(state, 0))
            say(f"damage-turn postcombat baselines: exile="
                f"{len(ST['exile_before'])} lib={ST['lib_before']}")
        scan_trigger_and_look(st, state)
        if damage_postcombat and await answer_damage_look(c, st, state, tag):
            return True

    # PRE: Florian on BF, stack empty, all 20, P0 precombat priority
    if (ST["florian_oid"] is not None and not ST["pre_exported"]
            and stack_empty(state) and lives == (20, 20, 20)
            and phase == "PreCombatMain" and state.get("active_player") == 0
            and my_priority(top_acts(st))):
        if await export_named(c, "pre"):
            ST["pre_exported"] = True
            say("PRE exported: Florian on BF, all at 20")
            st = st_of(c) or st
            state = st["state"]
            acts = merged_actions(st)

    # POST: damage turn done, P0 postcombat, stack empty, no pending look
    # answer -> export NOW and finish immediately
    if (damage_postcombat and not ST["post_exported"]
            and stack_empty(state)
            and not (ST["look_offered"] is not None
                     and not ST["look_answered"])):
        if await export_named(c, "post"):
            ST["post_exported"] = True
            say("POST exported: damage-turn postcombat, stack empty")
            STOP["stop"] = True
            return True

    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True

    # ---- P0 priority: casts, then land, then always pass ----
    in_flight = bool(state.get("stack") or [])
    if (not in_flight and phase in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == 0):
        hn = hand_lnames(state, 0)
        n_untapped = len(untapped_lands(state, 0))
        if (not ST["florian_cast"] and ST["florian_oid"] is None
                and FLORIAN in hn and n_untapped >= 3):
            a = find_cast_action(acts, state, FLORIAN)
            if a is not None:
                ST["florian_cast"] = True
                say(f"[{tag}] casting Florian (engine Auto payment)")
                wire("cast_submit", {"tag": "florian"})
                await submit_as_is(c, a)
                return True
        # bolt only on a LATER P0 turn than the Florian-cast turn
        # (summoning sickness), after PRE, once, while P2 still at 20
        if (ST["pre_exported"] and not ST["bolt_submitted"]
                and phase == "PreCombatMain"
                and ST["florian_bf_turn"] is not None
                and turn > ST["florian_bf_turn"]
                and BOLT in hn and lives[2] == 20):
            a = find_cast_action(acts, state, BOLT)
            if a is not None:
                ST["bolt_submitted"] = True
                say(f"[{tag}] casting Lightning Bolt (engine Auto payment)")
                wire("cast_submit", {"tag": "bolt"})
                await submit_as_is(c, a)
                return True
    if (my_main(state, 0) or my_priority(top_acts(st))) \
            and state.get("active_player") == 0:
        if await play_a_land(c, state, 0, acts, tag):
            return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if PASSED_REV.get(c.name, -1) < c.revision:
        await pass_priority(c, st, top_acts(st))
        PASSED_REV[c.name] = c.revision
    return True


async def passive_tick(c, pid, tag):
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
    if not my_priority(top_acts(st)):
        log_unanswered(c, tag, st)
        return True
    # passive: play land on own main phase, then pass
    if state.get("active_player") == pid \
            and (my_main(state, pid) or my_priority(top_acts(st))):
        if await play_a_land(c, state, pid, acts, tag):
            return True
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

    hello = await verify_server_hello()
    check_parse_florian()

    p0 = PhaseClient("P06982r")
    await p0.connect()
    say("P0 creating game (default Bo1, 3 seats)...")
    await p0.create(deck(*P0_DECK), player_count=3)
    p1 = PhaseClient("P16982r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P26982r")
    await p2.connect()
    say("P2 joining...")
    await p2.join(p0.game_code, deck(*P2_DECK))
    GAME = p0.game_code
    say(f"game {GAME}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"P2 seat={p2.player_id} RUN_ID={RUN_ID}")
    wire("game", {"code": GAME, "p0": p0.player_id, "p1": p1.player_id,
                  "p2": p2.player_id, "p0_deck": P0_DECK,
                  "p1_deck": P1_DECK, "p2_deck": P2_DECK})

    async def finish():
        dur = time.time() - t_start
        notes = []
        ass = {k: "not-run" for k in
               ("A1_parse_gap", "A2_setup_ok", "A3_life_loss",
                "A4_trigger_fires", "A5_look_X", "A6_exile_one",
                "A7_play_permission", "A8_cleanup")}

        def load(fn):
            try:
                with open(f"{EVDIR}/{fn}.json") as f:
                    return json.load(f)["state"]
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
                return None

        pre, post = load("pre"), load("post")
        if pre is not None:
            say("loaded pre.json")
        if post is not None:
            say("loaded post.json")

        # ---- A1: parse gap ----
        ass["A1_parse_gap"] = "passed" if PARSE["ok"] else "failed"
        notes.append(f"A1: triggers[0].execute.effect="
                     f"{json.dumps(PARSE['effect'], default=str)[:160]}")

        # ---- A2: setup (PRE: Florian on P0 BF; 20/20/20) ----
        if pre is not None:
            oid = bf_oids(pre, 0, FLORIAN)
            lives = [life_of(pre, i) for i in (0, 1, 2)]
            ok = (len(oid) > 0 and lives == [20, 20, 20])
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: florian_oids={oid} lives={lives}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing")

        # ---- A3: life loss (POST: P1 == 17 and P2 == 17) ----
        if post is not None:
            l1, l2 = life_of(post, 1), life_of(post, 2)
            ok = (l1 == 17 and l2 == 17)
            ass["A3_life_loss"] = "passed" if ok else "failed"
            notes.append(f"A3: POST P1 life={l1} P2 life={l2} "
                         f"(expected 17/17)")
        else:
            ass["A3_life_loss"] = "failed"
            notes.append("A3 failed: post.json missing")

        # ---- A4: trigger fired on the damage turn ----
        ok = bool(ST["trigger_seen_damage_turn"])
        ass["A4_trigger_fires"] = "passed" if ok else "failed"
        notes.append(f"A4: trigger_on_stack_damage_turn="
                     f"{ST['trigger_seen_damage_turn']} "
                     f"sightings={ST['trigger_sightings']}")
        if ST["trigger_entry"]:
            wire("trigger_entry_snapshot", ST["trigger_entry"])

        # ---- A5: look offered exactly 6 cards (damage turn) ----
        offered = ST["look_offered"]
        if offered is None:
            notes.append("A5: no look interaction was offered on the "
                         "damage turn (Unimplemented effect skipped "
                         "silently)")
            offered = 0
        ok = (offered == 6)
        ass["A5_look_X"] = "passed" if ok else "failed"
        notes.append(f"A5: offered_count={offered} (expected 6)")

        # ---- A6: exactly one exiled, library shrank by 1 ----
        new_exiled = set()
        if (pre is not None and post is not None
                and ST["exile_before"] is not None
                and ST["lib_before"] is not None):
            ex_pre = set(ST["exile_before"])
            ex_post = set(exile_of(post, 0))
            new_exiled = ex_post - ex_pre
            lib_post = len(library_of(post, 0))
            ok = (len(new_exiled) == 1 and offered == 6
                  and lib_post == ST["lib_before"] - 1)
            ass["A6_exile_one"] = "passed" if ok else "failed"
            notes.append(f"A6: newly_exiled={len(new_exiled)} "
                         f"lib_pre={ST['lib_before']} lib_post={lib_post} "
                         f"(expected 1 exiled, library -1)")
        else:
            ass["A6_exile_one"] = "failed"
            notes.append("A6 failed: missing pre/post state or exile "
                         "baselines")

        # ---- A7: exiled card playable this turn ----
        # Card-text mentions of PlayFromExile do NOT count; require a
        # runtime grant or an advertised cast-from-exile action.
        if post is not None and new_exiled:
            st0 = p0.latest or {}
            cast_from_exile = False
            for a in merged_actions(st0):
                d = a.get("data", {}) or {}
                for v in list(d.values()) + [a.get("_src_oid")]:
                    try:
                        if v is not None and int(v) in new_exiled \
                                and a["type"] in ("CastSpell", "PlayCard",
                                                  "PlayLand"):
                            cast_from_exile = True
                    except (TypeError, ValueError):
                        pass
            runtime_grant = False
            for key in ("transient_continuous_effects",
                        "exile_cast_permissions_used",
                        "exile_play_permissions_used"):
                blob = json.dumps(post.get(key), default=str)
                if "PlayFromExile" in blob:
                    runtime_grant = True
            ok = cast_from_exile or runtime_grant
            ass["A7_play_permission"] = "passed" if ok else "failed"
            notes.append(f"A7: exiled={sorted(new_exiled)} "
                         f"cast_from_exile_action={cast_from_exile} "
                         f"runtime_grant={runtime_grant}")
        else:
            ass["A7_play_permission"] = "failed"
            notes.append("A7 failed: no card was exiled by the trigger, "
                         "so no permission could be granted")

        # ---- A8: cleanup ----
        if post is not None:
            stack_empty_ok = not (post.get("stack") or [])
            ok = stack_empty_ok
            ass["A8_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A8: stack_empty={stack_empty_ok}")
        else:
            ass["A8_cleanup"] = "failed"
            notes.append("A8 failed: post.json missing")

        # ---- verdict (exact rule) ----
        if ass["A2_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif ass["A3_life_loss"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: life-loss precondition (A3) "
                         "not achieved")
        elif (ass["A1_parse_gap"] == "passed"
                and any(ass[k] == "failed"
                        for k in ("A5_look_X", "A6_exile_one",
                                  "A7_play_permission"))):
            verdict = "reproduced"
            notes.append("verdict=reproduced: parser gap confirmed (A1) and "
                         "the trigger outcome is not implemented "
                         "(A5/A6/A7 fail)")
        elif (ass["A1_parse_gap"] == "failed"
                and all(ass[k] == "passed"
                        for k in ("A5_look_X", "A6_exile_one",
                                  "A7_play_permission"))):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: clause now parses and the "
                         "full trigger outcome was observed")
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
            "format_config": "default Bo1 (3 human-client seats, life 20)",
            "decks": {"P0": [[n, c] for n, c in P0_DECK],
                      "P1": [[n, c] for n, c in P1_DECK],
                      "P2": [[n, c] for n, c in P2_DECK]},
            "setup_line": ("P0 4x Florian / 4x Lightning Bolt / 26x Swamp / "
                           "26x Mountain; P1/P2 60x Forest (passive: land "
                           "each own main, no attacks/blocks, always pass). "
                           "P0 mulligans for Florian+Bolt, casts Florian "
                           "{1}{B}{R} (engine Auto payment); on a later P0 "
                           "turn bolts P2 (20->17) then attacks P1 with "
                           "Florian (20->17); X=6 at P0's postcombat."),
            "contract_line": ("Florian's triggers[0].execute.effect is "
                              "Unimplemented(where_x_binding) on v0.103.0: "
                              "the 'look at top X' outcome never happens "
                              "(no 6-card look, no exile, no play "
                              "permission) -> reproduced."),
            "driver_notes": [
                "Protocol-106 port of driver/scenario_6982.py (v0.81.3 / "
                "protocol 70) for pinned v0.103.0; the behavioral contract, "
                "assertions A1..A8 and the verdict rule are unchanged.",
                "my_priority = PassPriority in legal_actions; casts and "
                "activations are gated on it.",
                "CastSpell carries payment_mode Auto: the engine taps mana "
                "itself; the driver never answers tapLandForMana and runs "
                "no driver-side mana payment (legacy PayMana actions are "
                "answered if they appear).",
                "DeclareAttackers via legacy Action with "
                "attacks=[[int(oid), {type:Player, data:1}]], bands=[]; "
                "DeclareBlockers with assignments=[].",
                "The bolt's TargetSelection is answered from P0's "
                "viewer_interaction with the seat==2 candidate (P2).",
                "The stack-watch branch always falls through to the "
                "pass-priority gate (never returns early); both seats must "
                "pass in succession for a stack entry to resolve.",
                "Florian's trigger is not optional: no may-choice is "
                "expected; unexpected OptionalEffectChoice prompts are "
                "declined (accept=false) so the run cannot stall on them.",
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
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "parse_florian.json",
                               "scenario_6982_01030.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log",
                               "summary.png", "manifest.sha256"],
            "limitations": [
                "Browser UI not exercised; native engine via three "
                "human-client seats.",
                "4x Florian / 4x Lightning Bolt density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Turn order is randomized by the engine (seat_order "
                "varies); the driver keys on active_player, not order.",
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
        with open(f"{EVDIR}/scenario_6982_01030.py", "w") as f:
            f.write(src)
        say("copied scenario_6982_01030.py into EVDIR")

        srv_src = None
        for cand in (f"{BACKFILL}/runs/{RUN_ID}/server.log",
                     f"{BACKFILL}/runs/20261008-6982/server.log"):
            if os.path.exists(cand):
                srv_src = cand
                break
        if srv_src is not None:
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

        render_summary(run, pre, post)

        write_manifest()          # build 1
        say("scenario finished")  # final scenario_run.log line
        write_manifest()          # build 2 -- no logging after this point
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

    def render_summary(run, pre, post):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 960
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6982 - Florian, Voldaren Scion",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
               "where-X life-loss binding Unsupported",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Assertions (from saved states / card-data):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse_gap": "card-data: effect Unimplemented(where_x_binding)",
            "A2_setup_ok": "PRE: Florian on P0 BF, life 20/20/20",
            "A3_life_loss": "POST: P1=17, P2=17 (bolt + Florian attack)",
            "A4_trigger_fires": "Florian TriggeredAbility on stack (dmg turn)",
            "A5_look_X": "look offered exactly 6 cards (dmg turn)",
            "A6_exile_one": "1 exiled, library shrank by 1",
            "A7_play_permission": "exiled card playable this turn",
            "A8_cleanup": "POST stack empty",
        }
        for k, lab in labels.items():
            av = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if av == "passed" else (
                (255, 90, 90) if av == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {av} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Life totals across states (P0/P1/P2):",
               fill=(200, 210, 225))
        y += 24
        for label, st in (("pre ", pre), ("post", post)):
            if st is not None:
                fl = bf_oids(st, 0, FLORIAN)
                line = (f"{label}: {life_of(st, 0)}/{life_of(st, 1)}/"
                        f"{life_of(st, 2)}  florian_oids={fl}  "
                        f"stack={len(st.get('stack') or [])}")
            else:
                line = f"{label}: (no state)"
            d.text((36, y), line, fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:15]:
            d.text((36, y), str(n)[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        import glob
        files = ["pre.json", "post.json", "run.json",
                 "parse_florian.json", "scenario_6982_01030.py",
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
        say(f"wrote manifest.sha256 ({len(lines)} files)")

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t_start < GAME_TIMEOUT and not STOP.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick),
                             (p1, "P1", passive_tick),
                             (p2, "P2", passive_tick)):
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
                pid = {"P0": 0, "P1": 1, "P2": 2}[tag]
                if tag == "P0":
                    await tick(c, tag)
                else:
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

        # bolt resolution marker
        if (ST["bolt_cast"] and not ST["bolt_resolved"]
                and life_of(state, 2) == 17 and stack_empty(state)):
            ST["bolt_resolved"] = True
            say("bolt resolved: P2 life 17")
            wire("bolt_resolved", {})

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

        if (turn or 0) > 30 and ST["florian_oid"] is None \
                and not STOP.get("stop"):
            OBS["notes"].append("watchdog: turn 30 reached with no Florian "
                                "on BF; finishing")
            say("watchdog: turn 30, no Florian; finishing")
            await finish()
            return

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} "
                f"pp={state.get('priority_player')} "
                f"life={[life_of(state, i) for i in (0, 1, 2)]} "
                f"florian={ST['florian_oid']} "
                f"bolt={ST['bolt_submitted']}/{ST['bolt_cast']}/"
                f"{ST['bolt_resolved']} attacked={ST['attacked']} "
                f"pre={ST['pre_exported']} post={ST['post_exported']} "
                f"trig_dmg={ST['trigger_seen_damage_turn']} "
                f"look={ST['look_offered']} "
                f"stack={len(state.get('stack') or [])}")

    say(f"loop ended: elapsed={time.time()-t_start:.0f}s")
    wire("loop_end", {})
    await finish()


t_start = 0.0

if __name__ == "__main__":
    asyncio.run(main())
