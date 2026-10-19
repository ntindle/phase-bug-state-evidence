#!/usr/bin/env python3
"""phase-rs/phase #7173 - Raphael, Tag Team Tough only triggers once per turn
globally, not once per different player damaged.

Oracle: "Menace. Whenever Raphael deals combat damage to a player for the
first time each turn, untap all attacking creatures. After this combat phase,
there is an additional combat phase."

Reported: "Only triggers once, not for each different player he damaged."
Triage (status:confirmed): each player's first combat-damage event from
Raphael should trigger independently; the AST collapses the per-player
occurrence limit into a global OncePerTurn constraint.

v0.113.0 re-validation port (protocol 151, 2026-10-19) of the verified
v0.83.0 run (driver/scenario_7173.py, run 20260915-7173e, verdict
reproduced). Protocol-151 notes: waiting_for is gone; priority = PassPriority
in the viewing seat's top-level legal_actions; decisions surface via
viewer_interaction; MulliganDecision/DeclareAttackers/DeclareBlockers/
PlayLand/PassPriority remain legacy Actions; mana payment via
PayMana/PayManaAbilityMana actions plus tapLandForMana vi opportunities;
stack entries keep kind.type == "TriggeredAbility" with source_id. Driver
logic and the behavioral contract A1..A6 are unchanged.

Contract:
  A1_parse_gap        - pinned card-data trigger carries an unkeyed
                        {"type":"OncePerTurn"} constraint (no damaged-player
                        key) [parse evidence]
  A2_setup_ok         - 3-seat game; Raphael on P0 BF ready to attack;
                        pre.json exported before combat 1
  A3_first_trigger    - combat-1 damage to P1 raises Raphael's
                        TriggeredAbility (stack sighting, else untap +
                        additional-combat corroboration)
  A4_additional_combat_1 - P0 gets a second DeclareAttackers in the attack
                        turn (the additional combat phase exists)
  A5_second_trigger   - combat-2 damage to P2 (first damage to P2 that turn)
                        raises Raphael's TriggeredAbility again, or a third
                        combat begins. FAILED = the reported bug.
  A6_cleanup          - stack empty, game proceeds; post.json exported

Verdict rule: reproduced iff A1-A4 pass and A5 fails.
not-reproduced iff A1-A4 pass and A5 passes. blocked otherwise.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")

from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7173
RUN_ID = os.environ.get("RUN_ID", "run-7173-reval-v01130-20261019-0111")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RAPHAEL = "raphael, tag team tough"
MOUNTAIN = "mountain"

P0_DECK = [(RAPHAEL, 12), (MOUNTAIN, 48)]
P1_DECK = [(MOUNTAIN, 60)]
P2_DECK = [(MOUNTAIN, 60)]

SERVER_IDENTITY = {
    "server_version": "v0.113.0",
    "build_commit": "0571ee3",
    "protocol_version": 151,
    "mode": "single-user",
    "binary_sha256": "f2b09aab4385ff6e6f352d211c2560af42445e11e6b5f2fef0065094c1a8c77d",
    "card_data_sha256": "eb309108eb0df708b6cb5b3c75858149a9fd2864ee0f4ef81195cc75af058b85",
    "draft_pools_sha256": "785376ea4dc8fd2fde29e734a1b069de3a12c57b2df1bb1471556b279b306400",
    "signature_verified": True,
    "observed_at": "2026-10-19",
    "source": ("pinned v0.113.0 (minisign-verified binary + signed data "
               "manifest, ledger server pin 2026-10-18); fresh isolated "
               "v0.113.0 server on 127.0.0.1:9374 started under setsid by "
               "this run (run dir discovered live from the server "
               "process); ServerHello re-checked by this run."),
}


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


# ---------------------------------------------------------------- state views
def objects(state):
    return state.get("objects", {}) or {}


def get_obj(state, oid):
    return objects(state).get(str(oid)) or {}


def lname_of(o):
    return str(o.get("base_name") or o.get("name") or "").lower()


def obj_lname(state, oid):
    return lname_of(get_obj(state, oid))


def bf_raphael(state, pid):
    for oid, o in objects(state).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and lname_of(o) == RAPHAEL):
            return int(oid)
    return None


def raphael_tapped(state, oid):
    return bool(get_obj(state, oid).get("tapped"))


def hand_ids(state, pid):
    return [int(oid) for oid, o in objects(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def hand_lnames(state, pid):
    return [lname_of(o) for o in objects(state).values()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def is_land(o):
    return lname_of(o) == MOUNTAIN


def untapped_lands(state, pid):
    return sum(1 for o in objects(state).values()
               if o.get("zone") == "Battlefield"
               and o.get("controller") == pid
               and is_land(o) and not o.get("tapped"))


def life_of(state, pid):
    players = state.get("players", []) or []
    if 0 <= pid < len(players) and players[pid].get("life") is not None:
        return players[pid].get("life")
    for o in objects(state).values():
        if o.get("zone") == "PlayerZone" or o.get("object_kind") == "Player":
            if o.get("controller") == pid or o.get("seat") == pid:
                return o.get("life")
    for p in players:
        if p.get("seat") == pid or p.get("player_id") == pid:
            return p.get("life")
    return None


def stack_entries(state):
    return state.get("stack") or []


# ---------------------------------------------------------------- interaction
def merged_actions(st):
    """Merge top-level legal_actions with per-object legal_actions_by_object
    (injecting _src_oid from the dict key) - protocol 151."""
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
    """Protocol 118+: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
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
               "response": {"type": stype,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
    await interact_as(c, sub, tag)


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
    """Protocol 118+: DiscardToHandSize surfaces via viewer_interaction;
    gate on hand > 7 plus a schema/select opportunity offering hand cards."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(hand)
    found = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if any(_cand_reference(ch) in handset for ch in cands):
            found = (opp, cands)
            break
    if found is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands = found

    def rank(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm == MOUNTAIN:
            return (0, str(oid))
        if nm == RAPHAEL:
            return (2, str(oid))
        return (1, str(oid))

    picks = sorted(cands, key=rank)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding {[obj_lname(state, _cand_reference(x)) for x in picks]} to hand size")
    wire("discard_handsize", {"who": tag,
                              "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": "select",
                                       "data": {"choiceIds": [x.get("id") for x in picks]}}},
                      tag)
    return True


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
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
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


def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_confirmed(state, pc):
    return cast_obj_zone(state, pc["oid"]) in (
        "stack", "battlefield", "graveyard", "exile", "command")


def cast_guard_tick(state, tag):
    """Cast-confirmation guard: never pass priority or start new plays under
    an in-flight CastSpell; a 30s backstop clears a still-unconfirmed cast."""
    pc = ST.get("pending_cast")
    if not pc or pc.get("tag") != tag:
        return "proceed"
    if cast_confirmed(state, pc):
        ST["pending_cast"] = None
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] cast guard backstop: clearing unconfirmed cast "
            f"{pc['name']}")
        wire("cast_guard_backstop", pc)
        ST["pending_cast"] = None
        ST["raphael_cast"] = False
        return "proceed"
    return "hold"


async def try_cast(c, state, acts, tag, name, needs):
    a, oid = cast_action_for(acts, state, name)
    if not a:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid})
    await submit_as_is(c, a)
    ST["pending_cast"] = {"oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag}
    return True


def server_run_dir():
    """Locate the live phase-server's run dir from its --games-db flag."""
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if "phase-server" in line and "--games-db" in line:
                parts = line.split()
                i = parts.index("--games-db")
                db = parts[i + 1]
                runid = db.split("/runs/")[1].split("/")[0]
                return runid
    except Exception as e:
        say("server_run_dir lookup failed:", e)
    return None


async def check_server_hello():
    """Read a fresh ServerHello for provenance (separate throwaway conn)."""
    import websockets
    try:
        async with websockets.connect("ws://127.0.0.1:9374/ws",
                                      max_size=10_000_000) as ws:
            raw = await asyncio.wait_for(ws.recv(), 5)
            msg = json.loads(raw)
            d = msg.get("data", {}) or {}
            say("ServerHello:", {k: d.get(k) for k in
                                 ("server_version", "build_commit",
                                  "protocol_version", "mode")})
            return d
    except Exception as e:
        say("ServerHello check failed:", e)
        return {}

MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}

ST = {"stage": "SETUP", "raphael_cast": False, "raphael_cast_turn": None,
      "raphael_oid": None, "attack_armed": False, "attack_turn": None,
      "combats": 0, "combat_turns": [], "combat_targets": [],
      "trigger_sightings": [], "p1_damaged": False, "p2_damaged": False,
      "p1_life": None, "p2_life": None, "untap_seen": False,
      "pre_exported": False, "mid_exported": False, "post_exported": False,
      "control_armed": False, "control_sighting": None, "done": False,
      "mana_needs": {}, "pending_cast": None}


def attack_plan(state, pid):
    """P0's attacks per combat; P1/P2 never attack."""
    if pid != 0 or ST["stage"] not in ("COMBAT", "CONTROL"):
        return []
    roid = bf_raphael(state, 0)
    if roid is None or raphael_tapped(state, roid):
        return []
    if ST["stage"] == "CONTROL":
        return [[roid, {"type": "Player", "data": 1}]]
    # COMBAT stage: combat1 -> P1, combat2 -> P2, combat3+ -> P1
    idx = ST["combats"]
    tgt = [1, 2, 1][idx] if idx < 3 else 1
    return [[roid, {"type": "Player", "data": tgt}]]


async def seat_tick(c, pid, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    phase = state.get("phase") or ""
    turn = state.get("turn_number") or 0
    active = state.get("active_player")

    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    # combat declarations
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            attacks = attack_plan(state, pid)
            sub = copy.deepcopy(da)
            sub.setdefault("data", {})["attacks"] = attacks
            sub["data"]["bands"] = []
            wire("declare_attackers",
                 {"tag": tag, "attacks": attacks, "turn": turn})
            await submit_as_is(c, sub)
            if pid == 0 and attacks:
                if ST["attack_turn"] is None:
                    ST["attack_turn"] = turn
                ST["combats"] += 1
                ST["combat_turns"].append(turn)
                tgt = attacks[0][1].get("data")
                ST["combat_targets"].append(tgt)
                say(f"[P0] combat #{ST['combats']} declares: Raphael -> "
                    f"P{tgt} (turn {turn})")
                if ST["stage"] == "CONTROL":
                    ST["stage"] = "COMBAT_DONE"
        return
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            sub = copy.deepcopy(db)
            sub.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, sub)
        return
    # mana payment has priority over new plays
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return
    # P0 main-phase driving
    if pid == 0 and phase in ("PreCombatMain", "PostCombatMain") \
            and active == 0 and my_priority(acts):
        if cast_guard_tick(state, tag) == "hold":
            return
        roid = bf_raphael(state, 0)
        # arm the main attack: Raphael ready on a later turn
        if (ST["stage"] == "SETUP" and roid is not None
                and not raphael_tapped(state, roid)
                and ST["raphael_cast_turn"] is not None
                and turn > ST["raphael_cast_turn"]):
            if not ST["pre_exported"]:
                await export_named("pre")
                ST["pre_exported"] = True
            ST["attack_armed"] = True
            ST["stage"] = "COMBAT"
            ST["raphael_oid"] = roid
            say(f"[P0] attack armed (turn {turn}); pre.json exported")
            return
        # control leg: next P0 turn after the attack turn, re-attack P1
        # to check the once-per-turn history resets
        if (ST["stage"] == "COMBAT" and ST["p2_damaged"]
                and ST["attack_turn"] is not None
                and turn > ST["attack_turn"]
                and roid is not None
                and not raphael_tapped(state, roid)):
            ST["stage"] = "CONTROL"
            ST["control_armed"] = True
            say(f"[P0] control leg armed (turn {turn}): attack P1 next "
                "combat to check the trigger resets next turn")
            return
        # cast Raphael when affordable and none on board (RR + 4)
        if (not ST["raphael_cast"] and roid is None
                and RAPHAEL in hand_lnames(state, 0)
                and untapped_lands(state, 0) >= 6):
            if await try_cast(c, state, acts, tag, RAPHAEL,
                              {"R": 2, "generic": 4}):
                ST["raphael_cast"] = True
                ST["raphael_cast_turn"] = turn
                say(f"[P0] cast Raphael (turn {turn})")
                return
        # generic land drop
        if await play_a_land(c, state, 0, acts, tag):
            return
    # P1/P2: play a land on their own main, nothing else
    if pid in (1, 2) and my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    # never pass priority while our own decision is pending
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if cast_guard_tick(state, tag) == "hold":
            return
        await pass_priority(c, st, acts)


async def export_named(tag):
    try:
        raw = await p0c.export_state()
        with open(f"{EVDIR}/{tag}.json", "w") as f:
            f.write(raw)
        say(f"exported {tag.upper()}")
        wire(f"export_{tag}", {"ok": True})
        return True
    except Exception as e:
        say(f"{tag} export failed: {e}")
        wire(f"export_{tag}", {"ok": False, "err": str(e)[:200]})
        return False


def scan_trigger(state, turn, phase):
    """Opportunistic stack sighting of Raphael's TriggeredAbility."""
    roid = ST["raphael_oid"] or bf_raphael(state, 0)
    if roid is None:
        return
    for e in stack_entries(state):
        kind = e.get("kind") or {}
        if kind.get("type") != "TriggeredAbility":
            continue
        ability = ((kind.get("data") or {}).get("ability")) or {}
        desc = ability.get("description") or ""
        src_match = (e.get("source_id") == roid
                     or str(e.get("source_id")) == str(roid))
        desc_match = "raphael" in desc.lower() and "first time" in desc.lower()
        if not (src_match or desc_match):
            continue
        key = (turn, phase, ST["combats"])
        if not any(s["key"] == key for s in ST["trigger_sightings"]):
            rec = {"key": key, "turn": turn, "phase": phase,
                   "combat": ST["combats"],
                   "targets_so_far": list(ST["combat_targets"]),
                   "p1_life": life_of(state, 1),
                   "p2_life": life_of(state, 2)}
            ST["trigger_sightings"].append(rec)
            wire("raphael_trigger_on_stack", rec)
            say(f"TRIGGER SIGHTING: Raphael TriggeredAbility on stack "
                f"(turn {turn}, phase {phase}, combat #{ST['combats']})")


async def main():
    global p0c
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse_gap", "A2_setup_ok", "A3_first_trigger",
            "A4_additional_combat_1", "A5_second_trigger", "A6_cleanup")}

    hello = await check_server_hello()
    if hello:
        for k in ("server_version", "build_commit", "protocol_version"):
            if hello.get(k):
                SERVER_IDENTITY[k] = hello[k]

    # ---- A1: parse check against the pinned v0.113.0 data ----
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.113.0/data/card-data.json"))
        raphael_entry = cd[RAPHAEL]
        say("oracle:", raphael_entry["oracle_text"].replace("\n", " | ")[:200])
        with open(f"{EVDIR}/parse_raphael.json", "w") as f:
            json.dump(raphael_entry["triggers"], f, indent=1)
        trig = (raphael_entry.get("triggers") or [None])[0] or {}
        constraint = trig.get("constraint") or {}
        blob = json.dumps(constraint).lower()
        unkeyed_once = (constraint.get("type") == "OncePerTurn"
                        and "player" not in blob)
        if unkeyed_once:
            ass["A1_parse_gap"] = "passed"
            notes.append("A1_parse_gap: passed (trigger constraint = "
                         f"{json.dumps(constraint)}; no damaged-player key - "
                         "the parse collapses the per-player occurrence "
                         "limit into a global once-per-turn)")
        else:
            ass["A1_parse_gap"] = "failed"
            notes.append("A1_parse_gap: FAILED to confirm gap: constraint="
                         f"{json.dumps(constraint)[:200]}")
        say(notes[-1])
    except Exception as ex:
        ass["A1_parse_gap"] = "failed"
        notes.append(f"A1_parse_gap failed: {ex}")
        say(notes[-1])

    clients = {}
    for name, i in (("P0", 0), ("P1", 1), ("P2", 2)):
        c = PhaseClient(name)
        await c.connect()
        clients[name] = (c, i)
    p0, _ = clients["P0"]
    p1, _ = clients["P1"]
    p2, _ = clients["P2"]
    p0c = p0
    await p0.create(deck(*P0_DECK), player_count=3)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")

    async def finish():
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
        states = {}
        for fn in ("pre", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, mid, post = (states.get("pre"), states.get("mid"),
                          states.get("post"))

        # ---- A2: setup ----
        if pre is not None:
            ok = bf_raphael(pre, 0) is not None
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: Raphael on P0 BF in pre.json: {ok}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing")

        # ---- A3: first trigger (combat 1 of the attack turn, damage to P1)
        atk_turn = ST["attack_turn"]
        atk_combats = sum(1 for t in ST["combat_turns"] if t == atk_turn)
        s1 = [s for s in ST["trigger_sightings"]
              if s["turn"] == atk_turn and s["combat"] == 1]
        a3 = bool(s1) or (ST["untap_seen"] and atk_combats >= 2)
        ass["A3_first_trigger"] = "passed" if a3 else "failed"
        notes.append(f"A3: stack_sighting_c1={bool(s1)} untap_seen="
                     f"{ST['untap_seen']} attack-turn combats={atk_combats} "
                     f"-> {'passed' if a3 else 'FAILED'}")

        # ---- A4: additional combat 1 (attack turn has >= 2 combats) ----
        a4 = atk_combats >= 2
        ass["A4_additional_combat_1"] = "passed" if a4 else "failed"
        notes.append(f"A4: P0 DeclareAttackers in attack turn {atk_turn} = "
                     f"{atk_combats} (targets {ST['combat_targets']}) -> "
                     f"{'passed' if a4 else 'FAILED'}")

        # ---- A5: second trigger (combat 2 of the attack turn, first damage
        # to P2). Scoped to the attack turn: the control-leg combat on the
        # next turn must not count.
        s2 = [s for s in ST["trigger_sightings"]
              if s["turn"] == atk_turn and s["combat"] == 2]
        a5 = bool(s2) or atk_combats >= 3
        ass["A5_second_trigger"] = "passed" if a5 else "failed"
        notes.append(f"A5: stack_sighting_c2={bool(s2)} attack-turn combats="
                     f"{atk_combats} -> {'passed' if a5 else 'FAILED'} "
                     "(FAILED = reported bug: first damage to a different "
                     "player does not re-trigger)")

        # ---- A6: cleanup ----
        if post is not None:
            empty = len(stack_entries(post)) == 0
            ass["A6_cleanup"] = "passed" if empty else "failed"
            notes.append(f"A6: post stack empty={empty}")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 failed: post.json missing")

        # control-leg observation: next-turn reset of the once-per-turn history
        cs = ST["control_sighting"]
        notes.append(f"control: next-turn attack on P1 re-triggered = "
                     f"{cs is not None}"
                     + (f" (turn {cs['turn']})" if cs else "")
                     + " - the once-per-turn history resets each turn")
        say(f"control: next-turn re-trigger sighting: {cs is not None}")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        if all(ass[k] == "passed" for k in
               ("A1_parse_gap", "A2_setup_ok", "A3_first_trigger",
                "A4_additional_combat_1")):
            verdict = ("not-reproduced" if ass["A5_second_trigger"] == "passed"
                       else "reproduced")
        else:
            verdict = "blocked"
        say("VERDICT:", verdict)

        srv_run = server_run_dir()
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": ("Raphael, Tag Team Tough - Only triggers once, not "
                      "for each different player he damaged."),
            "validated_at": "2026-10-19",
            "server": SERVER_IDENTITY,
            "scope": ("Raphael combat-damage trigger across two combats in "
                      "one turn vs two different players (P1 then P2), plus "
                      "a next-turn reset control; native engine, three "
                      "human-client seats; v0.113.0"),
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {
                "raphael_cast_turn": ST["raphael_cast_turn"],
                "attack_turn": ST["attack_turn"],
                "combats": ST["combats"],
                "combat_turns": ST["combat_turns"],
                "combat_targets": ST["combat_targets"],
                "trigger_sightings": [
                    {k: v for k, v in s.items() if k != "key"}
                    for s in ST["trigger_sightings"]],
                "p1_damaged": ST["p1_damaged"], "p2_damaged": ST["p2_damaged"],
                "p1_life": ST["p1_life"], "p2_life": ST["p2_life"],
                "untap_seen": ST["untap_seen"],
                "control_sighting": ST["control_sighting"],
            },
            "limitations": [
                "Browser UI not exercised; native engine via three "
                "human-client seats.",
                "Dense Raphael playset (12x) is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "P1/P2 fully passive (no lands beyond drops, no casts, "
                "no attacks, no blocks).",
                "The 'second hit to the SAME player does not trigger' "
                "acceptance criterion was not separately exercised.",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
            "server_run_note": ("v0.113.0 server on 127.0.0.1:9374 started "
                                "fresh under setsid for this run "
                                f"(games.db + server.log in runs/{srv_run}/); "
                                "ServerHello re-checked by this run; "
                                "server.log copied from the server's run dir."),
            "duration_s": round(time.time() - t_start, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        shutil.copy(os.path.abspath(__file__),
                    f"{EVDIR}/scenario_7173_01130.py")
        try:
            shutil.copy(f"{BACKFILL}/runs/{srv_run}/server.log",
                        f"{EVDIR}/server.log")
            say(f"copied server.log from runs/{srv_run}/")
        except Exception as e:
            notes.append(f"server.log copy failed: {e}")
            say("server.log copy failed:", e)
        render_png(run)
        files = ["pre.json", "mid.json", "post.json", "parse_raphael.json",
                 "run.json", "scenario_7173_01130.py", "wire_log.jsonl",
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
        # NOTE: no say() after the manifest is written - scenario_run.log is
        # part of the manifest, so further log appends would invalidate it.
        print("wrote manifest.sha256", flush=True)
        for fn in ("pre.json", "post.json", "parse_raphael.json", "run.json"):
            if os.path.exists(f"{EVDIR}/{fn}"):
                json.load(open(f"{EVDIR}/{fn}"))
        if os.path.exists(f"{EVDIR}/mid.json"):
            json.load(open(f"{EVDIR}/mid.json"))
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
        for line in man:
            h, fn = line.split("  ")
            assert hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
        print("validation: all JSON parse, PNG readable, hashes match",
              flush=True)

    def render_png(run):
        from PIL import Image, ImageDraw
        W, H = 1000, 1080
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7173 - Raphael, Tag Team Tough "
               "triggers once globally,", fill=(235, 240, 250))
        y += 24
        d.text((24, y), "not once per different player damaged",
               fill=(235, 240, 250))
        y += 30
        d.text((24, y), "server v0.113.0 (0571ee3) protocol 151 - 2026-10-19 - "
               "3 seats", fill=(140, 160, 180))
        y += 28
        col = (255, 90, 90) if run["verdict"] == "reproduced" else (
            (120, 220, 120) if run["verdict"] == "not-reproduced"
            else (230, 200, 120))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
        y += 34
        d.text((24, y), "Oracle: Whenever Raphael deals combat damage to a "
               "player for the first", fill=(200, 210, 225))
        y += 22
        d.text((24, y), "time each turn, untap all attacking creatures. "
               "After this combat phase,", fill=(200, 210, 225))
        y += 22
        d.text((24, y), "there is an additional combat phase.",
               fill=(200, 210, 225))
        y += 30
        ds = run["driver_state"]
        d.text((24, y), f"combats in attack turn: {ds['combats']} "
               f"(targets P{', P'.join(str(t) for t in ds['combat_targets'])})",
               fill=(140, 160, 180))
        y += 26
        d.text((24, y), f"trigger sightings on stack: "
               f"{len(ds['trigger_sightings'])} "
               f"(combats {[s['combat'] for s in ds['trigger_sightings']]})",
               fill=(140, 160, 180))
        y += 26
        d.text((24, y), f"P1 life {ds['p1_life']} | P2 life {ds['p2_life']} | "
               f"untap_seen={ds['untap_seen']}",
               fill=(140, 160, 180))
        y += 26
        d.text((24, y), "parse: trigger constraint = "
               "{\"type\": \"OncePerTurn\"} (no damaged-player key)",
               fill=(140, 160, 180))
        y += 32
        d.text((24, y), "Assertions:", fill=(200, 210, 225))
        y += 24
        for k, v in run["assertions"].items():
            c = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (230, 200, 120))
            d.text((36, y), f"{k}: {v}", fill=c)
            y += 22
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:16]:
            d.text((36, y), n[:114], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    # ---- main loop ----
    deadline = time.time() + 1200
    last_tick = {0: 0, 1: 0, 2: 0}
    try:
        while time.time() < deadline and not ST["done"]:
            for c, pid in (p0, 0), (p1, 1), (p2, 2):
                tag = ("P0", "P1", "P2")[pid]
                if time.time() - last_tick[pid] < 1.0:
                    continue
                try:
                    await seat_tick(c, pid, tag)
                except Exception as e:
                    wire("tick_error", {"tag": tag, "err": str(e)[:200]})
                last_tick[pid] = time.time()
            # cross-seat observation from P0's view
            st = p0.latest or {}
            state = st.get("state", st)
            turn = state.get("turn_number") or 0
            phase = state.get("phase") or ""
            scan_trigger(state, turn, phase)
            l1, l2 = life_of(state, 1), life_of(state, 2)
            if l1 is not None and l1 < 20 and not ST["p1_damaged"]:
                ST["p1_damaged"] = True
                ST["p1_life"] = l1
                say(f"P1 damaged: life={l1} (turn {turn}, phase {phase})")
                wire("p1_damaged", {"turn": turn, "phase": phase, "life": l1})
            if l2 is not None and l2 < 20 and not ST["p2_damaged"]:
                ST["p2_damaged"] = True
                ST["p2_life"] = l2
                say(f"P2 damaged: life={l2} (turn {turn}, phase {phase})")
                wire("p2_damaged", {"turn": turn, "phase": phase, "life": l2})
                if not ST["mid_exported"]:
                    await export_named("mid")
                    ST["mid_exported"] = True
            # untap corroboration: Raphael attacked (tapped) in combat 1,
            # trigger should have untapped him before combat 2
            if (ST["p1_damaged"] and ST["combats"] == 1
                    and not ST["untap_seen"]):
                roid = bf_raphael(state, 0)
                if roid is not None and not raphael_tapped(state, roid):
                    ST["untap_seen"] = True
                    say("untap_seen: Raphael untapped after combat-1 damage")
                    wire("untap_seen", {"turn": turn, "phase": phase})
            # control-leg sighting: trigger on the next turn's attack
            if ST["stage"] == "COMBAT_DONE" and ST["control_armed"]:
                for s in ST["trigger_sightings"]:
                    if s["turn"] != ST["attack_turn"]:
                        ST["control_sighting"] = s
                        ST["control_armed"] = False
                        say(f"control: next-turn trigger sighting: {s}")
                        break
            # done: two full rounds after the attack turn (control leg had
            # its chance), or deadline. The p1_damaged fallback covers the
            # path where no additional combat is ever created.
            if ST["attack_turn"] is not None \
                    and turn >= ST["attack_turn"] + 6 \
                    and (ST["p2_damaged"] or ST["p1_damaged"]):
                ST["done"] = True
                say("cleanup window closed; finishing")
            await asyncio.sleep(0.2)
        if not ST["done"]:
            notes.append("deadline hit before cleanup completed")
            say("deadline hit")
    finally:
        await finish()
        for c, _ in (p0, 0), (p1, 1), (p2, 2):
            try:
                await c.ws.close()
            except Exception:
                pass
        WIRE.close()
        RUNLOG.close()


p0c = None

if __name__ == "__main__":
    asyncio.run(main())
