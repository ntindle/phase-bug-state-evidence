#!/usr/bin/env python3
"""Issue #9858: Team formats -- "an opponent controls" / "each opponent"
include teammates (ControllerRef::Opponent tests != controller).

Oracle (Rocky Rebuke): "Target creature you control deals damage equal to
its power to target creature an opponent controls."

Report (phase-rs/phase#9858, opened 2026-10-12, open, bug/area:engine):
the parser maps "an opponent controls", "your opponents control" and
"you don't control" to the same ControllerRef::Opponent; the object
evaluator (game/filter.rs, the ControllerRef::Opponent arm, and the
owner-relative variant) admits any alive controller other than the
source's controller. It does not use the team-aware topology::is_opponent.
In a team game (Two-Headed Giant) the phrasings need different answers:
"an opponent controls" = players not on your team (CR 102.3); a
teammate's creature is NOT a legal target for Rocky Rebuke's second
target. Read from code, unmeasured in a team game -- this run measures
it in a real 2HG game.

Behavioral contract (per playbook: test the reported OUTCOME):
  Setup: a 4-player Two-Headed Giant game (format_config TwoHeadedGiant,
  FixedTeams team_size 2, team_count 2, SharedTeamTurns). Seats by join
  order: P0/P1 = team 0, P2/P3 = team 1 (topology::team_id =
  PlayerId / team_size). In this engine's 2HG, priority is team-level
  (CR 117.6 + CR 805.5b): only the team representatives (P0, P2) ever
  hold priority/actions; teammates P1/P3 never act. The fixture therefore
  needs a way to put a creature under teammate P1's control WITHOUT P1
  acting. Two vehicles, tried in order (the first attempt's Donate leg
  failed: the engine prompted for Donate's permanent target but never
  for its player target, so Donate resolved with no control change --
  probe-9858-3; Donate is abandoned as a vehicle):
  (a) Forbidden Orchard: in this engine's card data it is a mana ability
      plus a TapsForMana trigger ("Whenever you tap this land for mana,
      target opponent creates a 1/1 colorless Spirit creature token").
      P0 taps an Orchard for mana; if the trigger prompts for a target
      opponent, the driver answers teammate P1 (seat 1) -- if the engine
      offers P1 as an "opponent" there, that is the same bug family
      manifesting on the trigger path, recorded as a corroborating
      observation. If the trigger auto-assigns, the driver observes who
      gets the Spirit. A Spirit under P1 = fixture ready.
  (b) Fractured Identity (fallback if the Orchard path cannot deliver a
      P1 creature): "Exile target creature. Each other player creates a
      token that's a copy of the exiled creature." P0 exiles a P2 Bears;
      each other player (P1, P2, P3) creates a copy -- P1's copy is the
      teammate creature. Single object target, no player target.
  Decisive: at the second-target selection ("target creature an
  opponent controls"), the teammate's creature must NOT be offered.
  Control: an opponent's creature MUST be offered.
  End-to-end: if the teammate's creature IS offered (bug), targeting
  it must deal damage to it when the spell resolves.

Assertions:
  A1 setup_ok            2HG game created, 4 seats, P0/P1 teammates and
                         P2/P3 opponents per topology; P0, P1, P2 each
                         control a creature when P0 casts Rocky Rebuke
  A2 teammate_not_offered FAILED = the reported bug: a P1-controlled
                         creature is among the second-target candidates;
                         passed = no teammate creature offered
  A3 opponent_offered    passed = a P2/P3-controlled creature is among
                         the second-target candidates (the targeting
                         machinery works for true opponents)
  A4 resolution_correct  FAILED = bug end-to-end: the targeted
                         teammate's creature took damage/died from
                         Rocky Rebuke; passed = the spell resolved
                         dealing damage to the targeted opponent's
                         creature and the teammate's creature was
                         untouched; not-run = resolution never observed

Verdict:
  reproduced      A2 failed (teammate offered as "an opponent controls"),
                  corroborated by A4 if the bug-path spell resolves
  not-reproduced  A2 passed and A4 passed (correct targeting + resolution)
  blocked         fixture never reached the target selection

Driver conventions (verified scenario_9855.py, protocol 134; updated
for v0.109.0 / protocol 140):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (viewer_interaction)
  - exact card-data names; export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; vi candidate references are strings
  - real_decision_pending filters None surface codes (9845 lesson)
  - genuine card-choice candidates have NO action surface (9842 lesson)
  - runtime state objects use "card_types" (plural); card-data.json uses
    "card_type" (singular) (9847 lesson)
  - NEVER inject targets into CastSpell (9855 lesson): submit as
    advertised; choose targets via the engine's vi TargetSelection
    opportunity afterwards (CR 601.2c)
  - 2HG: team of a seat = seat // 2 (topology::team_id); shared team
    turns, so a "my turn" gate is active_player in my team
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario9858")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9858-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9858
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.109.0",
    "build_commit": "932e389",
    "protocol_version": 140,
    "mode": "Full",
    "server_binary_sha256": "b2522f3657873456f1a5d17c2c97842e2ea2c6833afcbb7bc492696ff89142d5",
    "card_data_sha256": "9089f823081843e8ad700ce362a448a3ba165d377a23465c86355e8ed07e5225",
    "draft_pools_sha256": "2543f1bdfb3e4c2469ff3193b91a936d3ddc6bde99c6f5d19926cae4743c7309",
    "signature_verified": True,
    "signature_note": ("v0.109.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.109.0 (published "
               "2026-10-13T18:43:00Z) == pinned release dir; ServerHello "
               "0.109.0/932e389/protocol 140 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.109.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release); "
               "scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9858/" + RUN_ID),
}

FORMAT_2HG = {
    "format": "TwoHeadedGiant",
    "starting_life": 30,
    "min_players": 4,
    "max_players": 4,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": False,
    "commander_damage_threshold": None,
    "range_of_influence": None,
    "team_based": True,
    "archenemy_player": None,
    "uses_commander": False,
    "sideboard_policy": {"type": "Unlimited"},
    "default_deck_copy_limit": {"type": "UpTo", "data": 4},
    "supplies_fixed_deck": False,
    "allow_debug_actions": False,
    "custom_rules": None,
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


for _f, _k in (("server/releases/v0.109.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.109.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.109.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against the pinned v0.109.0 digests",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.109.0/data/card-data.json"))


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


# ---------------------------------------------------------------- state helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_names(state, pid):
    return [obj_lname(state, o) for o in bf_oids(state, pid)]


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def is_land(o):
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def is_creature(o):
    return "Creature" in (((o.get("card_types") or {}).get("core_types")) or [])


def team_of(pid):
    # topology::team_id: FixedTeams team_size 2 -> PlayerId / 2
    return int(pid) // 2


def creatures_of(state, pid):
    return [o for o in bf_oids(state, pid) if is_creature(get_obj(state, o))]


BEARS = "grizzly bears"
REBUKE = "rocky rebuke"
ORCHARD = "forbidden orchard"
FRACTURED = "fractured identity"
SPIRIT = "spirit"
FOREST = "forest"
ISLAND = "island"
PLAINS = "plains"


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


def my_team_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and team_of(state.get("active_player", -1)) == team_of(pid)
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
            spec = (data.get("spec", {}) or {}).get("type")
            if spec == "relations":
                continue
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def _opp_summary(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        out.append({"iid": str(opp.get("interactionId"))[:12],
                    "rtype": resp.get("type"),
                    "stype": (resp.get("data", {}) or {}).get("spec", {}).get("type")
                    if isinstance((resp.get("data", {}) or {}).get("spec"), dict) else None,
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c)})
    return out


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] is not None:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


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
    assert str(ver).startswith("0.109.0"), f"unexpected version {ver}"
    assert int(proto) == 140, f"unexpected protocol {proto}"
    assert str(build) == "932e389", f"unexpected build {build}"

# ------------------------------------------------------------- action helpers

async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_names(state, pid)
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    if len(hand_ids(state, pid)) <= 7:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if _cand_ref(ch) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "handsize", iid)
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        picks = cands[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:40] for x in picks]}")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
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
                manas = [s for s in ch.get("surfaces", []) or []]
                syms = []
                for m in manas:
                    if m.get("type") == "mana":
                        syms = (m.get("data") or {}).get("symbols", []) or []
                        break
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
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action({"type": "PassPriority"})
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
    lands = [o for o in hand_ids(state, pid)
             if is_land(get_obj(state, o))]
    if pid == 0:
        # P0 preference: Forbidden Orchard first (fixture vehicle +
        # wildcolor mana), then a basic of a color P0 has none of on
        # the battlefield (color fixing for Fractured Identity
        # {3}{W}{U}), then the first land.
        on_bf = {obj_lname(state, o) for o in bf_oids(state, pid)}
        cmap = {"plains": "W", "island": "U", "swamp": "B",
                "mountain": "R", "forest": "G"}
        have_cols = {cmap[n] for n in on_bf if n in cmap}
        def _key(o):
            n = obj_lname(state, o)
            if n == ORCHARD:
                return (0, 0)
            if n in cmap and cmap[n] not in have_cols:
                return (1, 0)
            return (2, 0)
        lands = sorted(lands, key=_key)
    for o in lands:
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


def untapped_land_total(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def untapped_colored_lands(state, pid):
    out = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0}
    cmap = {"plains": "W", "island": "U", "swamp": "B",
            "mountain": "R", "forest": "G"}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = cmap.get(obj_lname(state, o))
            if c:
                out[c] += 1
    return out


def color_sources(state, pid):
    """Untapped basic-land colors + untapped Forbidden Orchard wilds
    (Orchard taps for AnyOneColor, so each untapped Orchard covers one
    missing color). Basics are counted by untapped_colored_lands;
    Orchard is not a basic name, so there is no double-count."""
    cols = untapped_colored_lands(state, pid)
    wild = sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == ORCHARD
               and not get_obj(state, o).get("tapped"))
    return cols, wild


def has_colors(state, pid, need):
    """True if P0's untapped basics + Orchard wilds can cover the
    distinct-color needs (e.g. {W:1, U:1} with W=0, U=1, wild=1)."""
    cols, wild = color_sources(state, pid)
    deficit = sum(max(0, need.get(c, 0) - cols.get(c, 0))
                  for c in ("W", "U", "B", "R", "G"))
    return deficit <= wild


# --------------------------------------------- declare-attackers observation

def attack_targets_offered(st, state):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        if spec.get("type") != "relations":
            continue
        attackers, targets = [], []
        for ch in data.get("candidates", []) or []:
            role, ref, name, seat = None, None, None, None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if not isinstance(d, dict):
                    continue
                if d.get("role") in ("attacker", "attackTarget"):
                    role = d.get("role")
                if d.get("reference") is not None:
                    ref = str(d.get("reference"))
                if d.get("name"):
                    name = str(d.get("name"))
                if d.get("seat") is not None:
                    seat = d.get("seat")
            if role == "attacker":
                attackers.append({"id": ch.get("id"), "ref": ref, "name": name})
            elif role == "attackTarget":
                if seat is not None:
                    targets.append({"kind": "player", "seat": seat})
                else:
                    targets.append({"kind": "object", "ref": ref,
                                    "name": name or
                                    (obj_lname(state, ref) if ref else None)})
        return attackers, targets, opp
    return None, None, None


# ------------------------------------------------------- target selection

def target_opportunity(st):
    """First vi opportunity that looks like a target selection: schema
    select/sequence with candidates, or exactChoices with candidate/target
    codes (passPriority menus excluded)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def classify_target_candidates(state, opp):
    """Classify a target opportunity's candidates by controller.

    Returns a list of dicts: {id, ref, name, controller, is_own (P0),
    is_teammate (P1), is_opponent (P2/P3)}."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    out = []
    for ch in cands:
        ref = _cand_ref(ch)
        obj = get_obj(state, ref) if ref else {}
        ctrl = obj.get("controller")
        try:
            ctrl = int(ctrl) if ctrl is not None else None
        except (TypeError, ValueError):
            ctrl = None
        out.append({"id": ch.get("id"), "ref": ref,
                    "name": obj_lname(state, ref) if ref else choice_text(ch),
                    "controller": ctrl,
                    "is_own": ctrl == 0,
                    "is_teammate": ctrl == 1,
                    "is_opponent": ctrl in (2, 3),
                    "text": choice_text(ch)[:80]})
    return out


def classify_player_candidates(state, opp):
    """Classify a target opportunity's candidates that are PLAYERS
    (e.g. Donate's recipient). Returns a list of dicts
    {id, seat, text} for candidates carrying a seat/player reference,
    or [] when no candidate looks like a player."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    out = []
    for ch in cands:
        seat = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            # player candidates carry the seat under "seat"/"player"/"index"
            # (cf. scenario_301_01090.py seat_of), sometimes on a "player"
            # surface or a role=="player" object surface.
            for k in ("seat", "player", "index"):
                if d.get(k) is not None:
                    try:
                        seat = int(d.get(k))
                    except (TypeError, ValueError):
                        seat = None
                    if seat is not None:
                        break
            if seat is not None:
                break
            if d.get("role") == "player" and d.get("reference") is not None:
                try:
                    seat = int(d.get("reference"))
                except (TypeError, ValueError):
                    seat = None
        if seat is not None:
            out.append({"id": ch.get("id"), "seat": seat,
                        "text": choice_text(ch)[:80]})
    return out


async def submit_target(c, opp, rtype, spec_type, chs, tag):
    """Submit target choice(s). chs: list of candidate dicts (from
    classify) to answer with. For schema select/sequence submits
    choiceIds; for exactChoices submits a single choose."""
    iid = opp.get("interactionId")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [ch["id"] for ch in chs]}}}
    else:
        assert len(chs) == 1
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": chs[0]["id"]}}}
    say(f"[{tag}] submitting target(s): "
        f"{[(ch.get('name'), ch.get('controller')) for ch in chs]}")
    wire("target_submission", {"who": tag, "submission": sub,
                               "targets": chs})
    await interact_as(c, sub, tag)
    return iid

async def tap_orchard_for_mana(c, st, tag, orchard_oid):
    """Tap P0's Forbidden Orchard for mana via the priority menu's
    tapLandForMana choice (identified by the choice's object surface
    name). Mana abilities resolve immediately; the TapsForMana trigger
    then goes on the stack. Returns True if submitted."""
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" not in codes:
                continue
            src = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and d.get("role") == "source":
                    src = d
                    break
            if src is None:
                continue
            if str(src.get("reference")) != str(orchard_oid):
                continue
            if str(src.get("name", "")).lower() != ORCHARD:
                continue
            iid = opp.get("interactionId") or opp.get("id")
            if iid in SUBMITTED_OPPS:
                continue
            SUBMITTED_OPPS.add(iid)
            say(f"[{tag}] tapping Forbidden Orchard (oid {orchard_oid}) "
                f"for mana (TapsForMana trigger)")
            wire("tap_orchard", {"who": tag, "oid": orchard_oid})
            await interact_as(c, {"interactionId": opp.get("interactionId"),
                                  "response": {"type": "choose",
                                               "data": {"choiceId": ch.get("id")}}},
                              tag)
            return True
    return False


def spirit_oids(state):
    """Battlefield Spirit tokens (Forbidden Orchard trigger product)."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and \
                obj_lname(state, oid) == SPIRIT:
            try:
                ctrl = int(o.get("controller"))
            except (TypeError, ValueError):
                ctrl = None
            out.append({"oid": str(oid), "controller": ctrl})
    return out


# ------------------------------------------------------------------- ticks

async def common_tick_head(c, acts, st, pid, tag):
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareBlockers" in atypes:
        return True  # never block in this scenario; hold
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    return None


async def declare_no_attackers(c, acts):
    da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
    if da:
        import copy as _copy
        d = _copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    return False


async def p0_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)

    r = await common_tick_head(c, acts, st, 0, tag)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    if "DeclareAttackers" in atypes:
        await declare_no_attackers(c, acts)
        return

    # --- Forbidden Orchard trigger prompt (after P0 taps an Orchard
    # for mana). The TapsForMana trigger ("target opponent creates a
    # 1/1 Spirit token") may prompt for a target opponent: answer with
    # teammate P1 (seat 1). If the engine offers P1 as an "opponent",
    # that is the same bug family on the trigger path -- recorded as a
    # corroborating observation, not as the A2 verdict (A2 is about
    # Rocky Rebuke's target selection).
    # UNGATED (2026-10-13 fix for the 1943 stall): this branch must
    # answer EVERY Orchard trigger instance, not just the first while
    # orchard_pending. Orchard also taps for mana inside pay_mana_vi
    # (e.g. paying for Grizzly Bears), and each such tap fires a fresh
    # trigger; the 1943 attempt sat 12 min at rev 62 because the
    # second trigger's prompt was never answered (orchard_pending had
    # cleared when the first Spirit landed). A pure-player prompt can
    # never be a Rocky Rebuke target prompt (Rebuke targets
    # creatures), so answering player prompts here cannot steal the
    # Rebuke prompt; a non-player opportunity falls through to the
    # branches below unconsumed (its interaction id is NOT marked
    # submitted).
    opp, rtype, stype = target_opportunity(st)
    if opp is not None:
        iid = opp.get("interactionId")
        if iid not in SUBMITTED_OPPS:
            players = classify_player_candidates(state, opp)
            cls = classify_target_candidates(state, opp)
            is_player_prompt = bool(players) and all(
                x["ref"] is None for x in cls)
            if is_player_prompt:
                SUBMITTED_OPPS.add(iid)
                n = ctx.get("orchard_opp_count", 0) + 1
                ctx["orchard_opp_count"] = n
                with open(f"{EVDIR}/orchard_target_opp_{n}.json", "w") as f:
                    json.dump({"rev": c.revision, "rtype": rtype,
                               "spec_type": stype, "opportunity": opp,
                               "player_classified": players,
                               "object_classified": cls}, f, indent=1, default=str)
                say(f"[{tag}] Orchard trigger opp #{n}: rtype={rtype} "
                    f"stype={stype} players={players} objects={cls}")
                wire("orchard_target_opp", {"n": n, "rtype": rtype,
                                            "stype": stype, "players": players,
                                            "objects": cls})
                pick = next((p for p in players if p["seat"] == 1), None)
                if n == 1:
                    # First observation only: later trigger instances
                    # repeat the same legal-target set.
                    ctx["orchard_trigger_offered_teammate"] = pick is not None
                    ctx["orchard_trigger_players"] = players
                if pick is None:
                    # Trigger path is team-aware (P1 not offered as an
                    # opponent): answer with the first offered player
                    # so the game proceeds; the Orchard vehicle
                    # cannot deliver a P1 creature, so it is
                    # exhausted after this resolution.
                    pick = players[0]
                    say(f"[{tag}] Orchard trigger prompt does NOT offer "
                        f"teammate P1 (players={players}); answering "
                        f"with seat {pick['seat']} so the game proceeds")
                else:
                    say(f"[{tag}] Orchard trigger prompt OFFERS teammate "
                        f"P1 as an 'opponent' (corroborating observation)")
                await submit_target(c, opp, rtype, stype,
                                    [{"id": pick["id"], "name": f"P{pick['seat']}",
                                      "controller": pick["seat"]}], tag)
                return True
    # No Orchard prompt pending: the trigger may auto-assign. Fall
    # through so the game proceeds; the Spirit-appearance check below
    # clears orchard_pending when the token lands (or times out).

    # --- tap a Forbidden Orchard for mana (fixture vehicle a).
    # Only while P1 controls no creature and the Orchard path is not
    # exhausted; takes priority over the Bears cast below.
    if ctx.get("fixture_stage", "orchard") == "orchard" \
            and not ctx.get("fixture_ready") \
            and not ctx.get("orchard_pending") \
            and not ctx.get("rebuke_cast") \
            and my_team_main(state, 0):
        if not creatures_of(state, 1):
            orchards = [o for o in bf_oids(state, 0)
                        if obj_lname(state, o) == ORCHARD
                        and not get_obj(state, o).get("tapped")]
            if orchards:
                if await tap_orchard_for_mana(c, st, tag, orchards[0]):
                    ctx["orchard_pending"] = True
                    ctx["orchard_tapped_at"] = time.time()
                    ctx["orchard_oid"] = orchards[0]
                    return True

    # --- Fractured Identity (fixture vehicle b, fallback): exile a
    # P2/P3 creature; each other player creates a copy -- P1's copy
    # is the teammate creature. Cast only after the Orchard path is
    # exhausted (trigger never delivered a P1 creature).
    if ctx.get("fixture_stage") == "fractured" \
            and not ctx.get("fixture_ready") \
            and not ctx.get("fractured_cast"):
        if my_team_main(state, 0):
            opp_c = creatures_of(state, 2) + creatures_of(state, 3)
            hn = hand_names(state, 0)
            # Fractured Identity {3}{W}{U}: Orchard wilds cover a
            # missing color (1945 lesson: P0 held 5 Fractured with
            # 2 Islands + 0 Plains on the battlefield and the leg never
            # fired because it counted Plains only).
            mana_ok = opp_c and FRACTURED in hn \
                and has_colors(state, 0, {"W": 1, "U": 1}) \
                and untapped_land_total(state, 0) >= 5
            if mana_ok:
                a, oid = cast_action_for(acts, state, FRACTURED)
                if a is not None:
                    ctx["fractured_cast"] = True
                    ctx["fractured_cast_at"] = time.time()
                    ST["mana_needs"][tag] = {"W": 1, "U": 1, "generic": 3}
                    say(f"[{tag}] casting Fractured Identity (oid {oid}) "
                        f"targeting an opponent creature; each other "
                        f"player should create a copy")
                    wire("cast_fractured", {"oid": oid})
                    await submit_as_is(c, a)
                    return True
                key = (tag, "fractured-not-advertised", c.revision)
                if key not in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.add(key)
                    say(f"[{tag}] Fractured Identity mana-ok but cast "
                        f"action NOT advertised -- engine may not "
                        f"implement the cast")
                    wire("fractured_not_advertised",
                         {"hand": hn,
                          "untapped": untapped_land_total(state, 0)})

    # --- Fractured Identity target selection (single object target).
    if ctx.get("fractured_cast") and not ctx.get("fractured_targets_done"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                cls = classify_target_candidates(state, opp)
                n = ctx.get("fractured_opp_count", 0) + 1
                ctx["fractured_opp_count"] = n
                with open(f"{EVDIR}/fractured_target_opp_{n}.json", "w") as f:
                    json.dump({"rev": c.revision, "rtype": rtype,
                               "spec_type": stype, "opportunity": opp,
                               "classified": cls}, f, indent=1, default=str)
                say(f"[{tag}] Fractured target opp #{n}: rtype={rtype} "
                    f"stype={stype} candidates={cls}")
                wire("fractured_target_opp", {"n": n, "rtype": rtype,
                                              "stype": stype, "classified": cls})
                opps = [x for x in cls if x["is_opponent"]
                        and x["name"] == BEARS]
                if opps:
                    ctx["fractured_exile_oid"] = opps[0]["ref"]
                    ctx["fractured_targets_done"] = True
                    ctx["fractured_targets_done_at"] = time.time()
                    say(f"[{tag}] Fractured Identity exiling opponent "
                        f"Bears (oid {opps[0]['ref']})")
                    await submit_target(c, opp, rtype, stype, [opps[0]], tag)
                    return True
                say(f"[{tag}] Fractured target opp has no opponent Bears "
                    f"candidate -- recorded, not answered")
                wire("fractured_target_unclassified",
                     {"n": n, "classified": cls})
                return True
        key = (tag, "fractured-no-target-opp", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("fractured_no_target_opp",
                 {"rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] Fractured cast but no target opportunity "
                f"recognized; vi={_opp_summary(st)}")
        return True

    # --- fixture-ready detection: ANY creature under P1's control
    # counts (P1 never acts in 2HG, so a P1 creature can only have
    # arrived via a fixture vehicle). Also resolves the pending
    # Orchard tap: a Spirit under P1 = Orchard vehicle worked; a
    # Spirit under P2/P3 (or no Spirit 60s after the tap) = the
    # Orchard path cannot deliver a P1 creature -> stage fractured.
    if not ctx.get("fixture_ready"):
        team_c = creatures_of(state, 1)
        if team_c:
            ctx["fixture_ready"] = True
            ctx["fixture_vehicle"] = ctx.get("fixture_vehicle") or (
                "orchard" if ctx.get("orchard_pending") else "fractured")
            ctx["orchard_pending"] = False
            say(f"[{tag}] fixture ready: P1 controls "
                f"{[obj_lname(state, o) for o in team_c]} "
                f"(vehicle={ctx['fixture_vehicle']})")
            wire("fixture_ready", {"vehicle": ctx["fixture_vehicle"],
                                   "p1_creatures": team_c})
        elif ctx.get("orchard_pending"):
            spirits = spirit_oids(state)
            if spirits:
                ctx["orchard_pending"] = False
                ctrls = sorted({s["controller"] for s in spirits})
                say(f"[{tag}] Orchard Spirit token(s) landed: {spirits}")
                wire("orchard_spirit", {"spirits": spirits})
                if 1 in ctrls:
                    ctx["fixture_ready"] = True
                    ctx["fixture_vehicle"] = "orchard"
                    say(f"[{tag}] fixture ready via Orchard: P1 controls "
                        f"a Spirit token")
                    wire("fixture_ready", {"vehicle": "orchard"})
                else:
                    ctx["fixture_stage"] = "fractured"
                    say(f"[{tag}] Orchard Spirit went to {ctrls}, not P1; "
                        f"Orchard vehicle exhausted -> stage fractured")
                    wire("orchard_exhausted",
                         {"reason": "spirit_to_non_teammate",
                          "spirits": spirits})
            elif time.time() - ctx.get("orchard_tapped_at", 0) > 60:
                ctx["orchard_pending"] = False
                ctx["fixture_stage"] = "fractured"
                say(f"[{tag}] no Spirit token 60s after tapping Orchard; "
                    f"Orchard vehicle exhausted -> stage fractured")
                wire("orchard_exhausted",
                     {"reason": "no_spirit_60s"})
        elif ctx.get("fractured_targets_done") \
                and time.time() - ctx.get("fractured_targets_done_at", 0) > 120:
            ctx["fixture_failed"] = True
            say(f"[{tag}] Fractured Identity FAILED: no P1 creature 120s "
                f"after targeting; fixture cannot be built")
            wire("fixture_failed", {"vehicle": "fractured"})
    # --- Rocky Rebuke target selection (after the cast, CR 601.2c).
    # Never inject targets into CastSpell; answer the engine's vi
    # TargetSelection opportunity/ies here.
    if ctx.get("rebuke_cast") and not ctx.get("rebuke_targets_done"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                cls = classify_target_candidates(state, opp)
                n = ctx.get("rebuke_opp_count", 0) + 1
                ctx["rebuke_opp_count"] = n
                with open(f"{EVDIR}/rebuke_target_opp_{n}.json", "w") as f:
                    json.dump({"rev": c.revision, "rtype": rtype,
                               "spec_type": stype, "opportunity": opp,
                               "classified": cls}, f, indent=1, default=str)
                say(f"[{tag}] Rebuke target opp #{n}: rtype={rtype} "
                    f"stype={stype} candidates={cls}")
                wire("rebuke_target_opp", {"n": n, "rtype": rtype,
                                           "stype": stype, "classified": cls})
                own = [x for x in cls if x["is_own"]]
                team = [x for x in cls if x["is_teammate"]]
                opps = [x for x in cls if x["is_opponent"]]
                others = [x for x in cls if not x["is_own"]]
                if others:
                    # prompt 2 ("an opponent controls") or a combined
                    # sequence carrying both targets.
                    ctx["a2_teammate_offered"] = bool(team)
                    ctx["a2_candidates"] = cls
                    ctx["a3_opponent_offered"] = bool(opps)
                    ctx["a3_candidates"] = cls
                    say(f"[{tag}] A2 check: teammate offered={bool(team)} "
                        f"(teammate cands={[x['name'] for x in team]}); "
                        f"A3 check: opponent offered={bool(opps)}")
                    if own and team + opps:
                        # combined sequence: both targets in one opportunity
                        second = (team or opps)[0]
                        first = own[0]
                        ctx["rebuke_own_target"] = first["ref"]
                        ctx["rebuke_second_target"] = second["ref"]
                        ctx["rebuke_second_is_teammate"] = bool(team)
                        ctx["rebuke_second_is_token"] = bool(
                            get_obj(state, second["ref"]).get("is_token"))
                        ctx["rebuke_targets_done"] = True
                        ctx["rebuke_targets_done_at"] = time.time()
                        await submit_target(c, opp, rtype, stype,
                                            [first, second], tag)
                        return True
                    # sequential: this prompt is target 2
                    second = (team or opps)[0]
                    ctx["rebuke_second_target"] = second["ref"]
                    ctx["rebuke_second_is_teammate"] = bool(team)
                    ctx["rebuke_second_is_token"] = bool(
                        get_obj(state, second["ref"]).get("is_token"))
                    ctx["rebuke_targets_done"] = True
                    ctx["rebuke_targets_done_at"] = time.time()
                    await submit_target(c, opp, rtype, stype, [second], tag)
                    return True
                elif own:
                    # prompt 1 ("creature you control")
                    first = own[0]
                    ctx["rebuke_own_target"] = first["ref"]
                    say(f"[{tag}] Rebuke target prompt #1: own creature "
                        f"{first['name']} (oid {first['ref']})")
                    await submit_target(c, opp, rtype, stype, [first], tag)
                    return True
                else:
                    say(f"[{tag}] Rebuke target opp has no classifiable "
                        f"creature candidates -- recorded, not answered")
                    wire("rebuke_target_unclassified",
                         {"n": n, "classified": cls})
                    return True
        # cast submitted but no target opportunity recognized yet: log
        # (rate-limited) and keep waiting.
        key = (tag, "rebuke-no-target-opp", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("rebuke_no_target_opp",
                 {"rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] Rebuke cast but no target opportunity recognized; "
                f"vi={_opp_summary(st)}")
        return True

    # --- track the targeted second creature's fate after resolution
    if ctx.get("rebuke_targets_done") and not ctx.get("a4_recorded"):
        tgt = ctx.get("rebuke_second_target")
        own_tgt = ctx.get("rebuke_own_target")
        if tgt and not (state.get("stack") or []):
            z = obj_zone(state, tgt)
            if z in ("Graveyard", "Exile"):
                ctx["a4_recorded"] = True
                ctx["a4_zone"] = z
                ctx["a4_second_target"] = tgt
                ctx["a4_second_is_teammate"] = ctx.get("rebuke_second_is_teammate")
                ctx["a4_second_is_token"] = ctx.get("rebuke_second_is_token")
                ctx["a4_own_target_zone"] = obj_zone(state, own_tgt) if own_tgt else None
                say(f"[{tag}] A4: targeted second creature (oid {tgt}, "
                    f"teammate={ctx.get('rebuke_second_is_teammate')}) "
                    f"zone after resolution = {z}; own-target zone = "
                    f"{ctx['a4_own_target_zone']}")
                wire("rebuke_resolution",
                     {"second_target": tgt,
                      "second_is_teammate": ctx.get("rebuke_second_is_teammate"),
                      "second_zone": z,
                      "own_target_zone": ctx["a4_own_target_zone"]})
                post = json.loads(await c.export_state())
                with open(f"{EVDIR}/post_resolution.json", "w") as f:
                    json.dump(post, f, indent=1)
                wire("post_resolution_export", {})
            elif ctx.get("rebuke_targets_done_at") and \
                    time.time() - ctx["rebuke_targets_done_at"] > 120:
                # Timeout path: the target's zone is whatever the live
                # state reports (None = object absent from the state,
                # e.g. a token that ceased to exist after leaving the
                # battlefield, CR 704.5d -- NOT "still on the
                # battlefield"). Must record the target identity here:
                # the 2100 run's A4 note fell into the wrong
                # (opponent-target) branch because this path left
                # a4_second_is_teammate unset.
                ctx["a4_recorded"] = True
                ctx["a4_zone"] = z
                ctx["a4_second_target"] = tgt
                ctx["a4_second_is_teammate"] = ctx.get("rebuke_second_is_teammate")
                ctx["a4_second_is_token"] = ctx.get("rebuke_second_is_token")
                ctx["a4_own_target_zone"] = obj_zone(state, own_tgt) if own_tgt else None
                ctx["a4_timeout"] = True
                say(f"[{tag}] A4 timeout: second target (oid {tgt}, "
                    f"teammate={ctx.get('rebuke_second_is_teammate')}, "
                    f"token={ctx.get('rebuke_second_is_token')}) zone = {z} "
                    f"120s after targeting; stack empty")
                wire("rebuke_resolution_timeout",
                     {"second_target": tgt, "zone": z,
                      "second_is_teammate": ctx.get("rebuke_second_is_teammate"),
                      "second_is_token": ctx.get("rebuke_second_is_token")})

    # --- cast Rocky Rebuke when the fixture is ready
    if my_team_main(state, 0) and not ctx.get("rebuke_cast") \
            and ctx.get("fixture_ready"):
        own_c = creatures_of(state, 0)
        team_c = creatures_of(state, 1)
        opp_c = creatures_of(state, 2) + creatures_of(state, 3)
        hn = hand_names(state, 0)
        if own_c and team_c and opp_c and REBUKE in hn \
                and has_colors(state, 0, {"G": 1}) \
                and untapped_land_total(state, 0) >= 2:
            a, oid = cast_action_for(acts, state, REBUKE)
            if a is not None:
                ctx["rebuke_cast"] = True
                ctx["rebuke_cast_at"] = time.time()
                ctx["rebuke_own_creature"] = own_c[0]
                ctx["rebuke_teammate_creature"] = team_c[0]
                ctx["rebuke_opponent_creature"] = opp_c[0]
                ctx["a1_ready"] = True
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                pre = json.loads(await c.export_state())
                with open(f"{EVDIR}/pre_rebuke.json", "w") as f:
                    json.dump(pre, f, indent=1)
                wire("pre_rebuke_export", {})
                say(f"[{tag}] casting Rocky Rebuke (oid {oid}); own="
                    f"{own_c[0]} teammate={team_c[0]} opponent={opp_c[0]}")
                wire("cast_rebuke", {"oid": oid,
                                     "own_creature": own_c[0],
                                     "teammate_creature": team_c[0],
                                     "opponent_creature": opp_c[0],
                                     "cast_action_data": a.get("data")})
                await submit_as_is(c, a)
                return True

    # --- cast bears for attack-leg fodder / board presence
    if my_team_main(state, 0):
        hn = hand_names(state, 0)
        if BEARS in hn and has_colors(state, 0, {"G": 1}) \
                and untapped_land_total(state, 0) >= 2:
            a, oid = cast_action_for(acts, state, BEARS)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                wire("cast_bears", {"oid": oid})
                await submit_as_is(c, a)
                return True
        if await play_a_land(c, state, 0, acts, tag):
            return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def other_tick(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, pid, tag)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    if "DeclareAttackers" in atypes:
        await declare_no_attackers(c, acts)
        return

    # P1/P2 cast bears when affordable on their team's turn
    if pid in (1, 2) and my_team_main(state, pid):
        hn = hand_names(state, pid)
        cols = untapped_colored_lands(state, pid)
        if BEARS in hn and cols["G"] >= 1 and untapped_land_total(state, pid) >= 2:
            a, oid = cast_action_for(acts, state, BEARS)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                wire("cast_bears", {"oid": oid})
                await submit_as_is(c, a)
                return True
        if await play_a_land(c, state, pid, acts, tag):
            return
    elif pid == 3 and my_team_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision

# ------------------------------------------------------------------- run

async def create_2hg_game(c):
    """Create a 4-player Two-Headed Giant game via CreateGameWithSettings
    with the full FormatConfig (the client helper defaults to 2-player
    Standard, which cannot express teams)."""
    await c.ws.send(json.dumps({
        "type": "CreateGameWithSettings",
        "data": {
            "deck": deck((FOREST.capitalize(), 16), (PLAINS.capitalize(), 8),
                         (ISLAND.capitalize(), 8),
                         ("Forbidden Orchard", 4),
                         ("Grizzly Bears", 8),
                         ("Fractured Identity", 8),
                         ("Rocky Rebuke", 8)),
            "display_name": c.name,
            "public": False,
            "password": None,
            "timer_seconds": None,
            "player_count": 4,
            "match_config": {"match_type": "Bo1"},
            "ai_seats": [],
            "format_config": FORMAT_2HG,
            "room_name": None,
            "host_peer_id": None,
            "draft_metadata": None,
            "start_when_full": True,
            "ranked": False,
        },
    }))
    for _ in range(50):
        t, data = await asyncio.wait_for(c.inbox.get(), 10)
        if t == "SessionAttached":
            return data
    raise RuntimeError("no SessionAttached")


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9858-{attempt}"
    ST["mana_needs"][tag] = {}
    for i in range(1, 4):
        ST["mana_needs"][f"P{i}{tag}"] = {}
    ctx = {}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await create_2hg_game(p0)
    clients = [p0]
    decks = [
        deck((FOREST.capitalize(), 28), ("Grizzly Bears", 32)),  # P1 teammate
        deck((FOREST.capitalize(), 28), ("Grizzly Bears", 32)),  # P2 opponent
        deck((FOREST.capitalize(), 60)),  # P3 filler
    ]
    for i, d in enumerate(decks, start=1):
        c = PhaseClient(f"P{i}{tag}")
        await c.connect()
        await c.join(p0.game_code, d)
        clients.append(c)
        say(f"joined P{i} seat={c.player_id}")
    say(f"game {p0.game_code} attempt={attempt}; seats="
        f"{[c.player_id for c in clients]}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "seats": [c.player_id for c in clients]})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    for k in ("A1_setup_ok", "A2_teammate_not_offered", "A3_opponent_offered",
              "A4_resolution_correct"):
        obs["assert"][k] = "not-run"

    t0 = time.time()
    last_diag = 0
    settle_ticks = 0
    while time.time() - t0 < 780:
        await asyncio.sleep(0.25)
        await p0_tick(p0, tag, ctx)
        for c, pid in zip(clients[1:], (1, 2, 3)):
            await other_tick(c, pid, f"P{pid}{tag}", ctx)
        await asyncio.sleep(0)
        st_now = p0.latest
        if ctx.get("fixture_failed"):
            say(f"[{tag}] fixture failed; cannot be built -- "
                f"abandoning attempt")
            obs["notes"].append(
                "abandoned: neither fixture vehicle delivered a creature "
                "to teammate P1")
            break
        if st_now and st_now["state"].get("turn_number", 0) > 25 \
                and not ctx.get("rebuke_cast"):
            say(f"[{tag}] turn {st_now['state'].get('turn_number')} exceeded "
                f"without the Rebuke cast; abandoning attempt")
            obs["notes"].append(
                f"abandoned: game reached turn "
                f"{st_now['state'].get('turn_number')} without casting Rocky "
                f"Rebuke (fixture never ready)")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pid in zip(clients, (0, 1, 2, 3)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"rebuke_cast={ctx.get('rebuke_cast')} "
                    f"targets_done={ctx.get('rebuke_targets_done')} "
                    f"a4={ctx.get('a4_recorded')} "
                    f"bf0={bf_names(s, 0)[:4]} bf1={bf_names(s, 1)[:4]} "
                    f"bf2={bf_names(s, 2)[:4]}")
        if ctx.get("a4_recorded"):
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break
        if ctx.get("rebuke_cast") and not ctx.get("rebuke_targets_done") \
                and time.time() - ctx.get("rebuke_cast_at", 0) > 240:
            say(f"[{tag}] Rebuke cast 240s ago, target selection never "
                f"completed; abandoning attempt")
            obs["notes"].append(
                "abandoned: Rocky Rebuke cast but its target selection "
                "never completed within 240s")
            break

    st = p0.latest
    state = st["state"] if st else {}
    # ---- A1
    if ctx.get("a1_ready"):
        obs["assert"]["A1_setup_ok"] = "passed"
        obs["notes"].append(
            "A1 passed: 2HG game (format TwoHeadedGiant, teams P0/P1 vs "
            f"P2/P3 per topology PlayerId/2); fixture vehicle "
            f"{ctx.get('fixture_vehicle')} put a creature under teammate "
            f"P1's control; P0 cast Rocky Rebuke with own creature oid "
            f"{ctx.get('rebuke_own_creature')}, teammate creature oid "
            f"{ctx.get('rebuke_teammate_creature')}, opponent creature oid "
            f"{ctx.get('rebuke_opponent_creature')} all on the battlefield")
    elif ctx.get("rebuke_cast"):
        obs["assert"]["A1_setup_ok"] = "passed"
        obs["notes"].append("A1 passed: Rocky Rebuke was cast (fixture met)")
    elif ctx.get("fixture_failed"):
        obs["assert"]["A1_setup_ok"] = "failed"
        obs["notes"].append(
            "A1 FAILED: neither fixture vehicle (Forbidden Orchard, "
            "Fractured Identity) delivered a creature to teammate P1 "
            "(fixture could not be built)")
    else:
        obs["assert"]["A1_setup_ok"] = "not-run"
        obs["notes"].append("A1 not-run: Rocky Rebuke never cast")
    # ---- A2
    if ctx.get("a2_teammate_offered") is True:
        obs["assert"]["A2_teammate_not_offered"] = "failed"
        obs["notes"].append(
            "A2 FAILED (reported bug): at the second-target selection "
            "('target creature an opponent controls'), a teammate-controlled "
            f"creature was offered: "
            f"{[{'oid': x['ref'], 'name': x['name'], 'controller': x['controller']} for x in ctx.get('a2_candidates', []) if x['is_teammate']]}. "
            "A teammate is not an opponent (CR 102.3).")
    elif ctx.get("a2_teammate_offered") is False:
        obs["assert"]["A2_teammate_not_offered"] = "passed"
        obs["notes"].append(
            "A2 passed: no teammate-controlled creature was offered at "
            "the second-target selection")
    else:
        obs["assert"]["A2_teammate_not_offered"] = "not-run"
        obs["notes"].append("A2 not-run: second-target selection never observed")
    # ---- A3
    if ctx.get("a3_opponent_offered") is True:
        obs["assert"]["A3_opponent_offered"] = "passed"
        obs["notes"].append(
            "A3 passed: an opponent-controlled creature was offered at "
            "the second-target selection (targeting works for true opponents)")
    elif ctx.get("a3_opponent_offered") is False:
        obs["assert"]["A3_opponent_offered"] = "failed"
        obs["notes"].append(
            "A3 FAILED: no opponent-controlled creature was offered at "
            "the second-target selection (targeting broken for true opponents)")
    else:
        obs["assert"]["A3_opponent_offered"] = "not-run"
        obs["notes"].append("A3 not-run: second-target selection never observed")
    # ---- A4
    if ctx.get("a4_recorded"):
        if ctx.get("a4_second_is_teammate"):
            if ctx.get("a4_zone") in ("Graveyard", "Exile"):
                obs["assert"]["A4_resolution_correct"] = "failed"
                obs["notes"].append(
                    "A4 FAILED (bug end-to-end): Rocky Rebuke resolved "
                    "dealing damage to the targeted TEAMMATE's creature "
                    f"(oid {ctx.get('a4_second_target')}), which left the "
                    f"battlefield to the {ctx.get('a4_zone')}; own-target "
                    f"zone = {ctx.get('a4_own_target_zone')}")
            elif ctx.get("a4_zone") is None:
                # Object absent from the state: for a token target this
                # is the expected cease-to-exist outcome after lethal
                # damage (CR 704.5d) -- the token died, it did not
                # survive. For a non-token the destination is
                # unrecorded, but the creature is gone from the
                # battlefield after being illegally targeted.
                if ctx.get("a4_second_is_token"):
                    obs["notes"].append(
                        "A4 FAILED (bug end-to-end, token): Rocky Rebuke "
                        "resolved dealing damage to the targeted "
                        "TEAMMATE's creature "
                        f"(oid {ctx.get('a4_second_target')}), a token; it "
                        "is absent from the post-resolution export "
                        "(tokens cease to exist on leaving the "
                        "battlefield, CR 704.5d) and its controller "
                        "controls no creatures -- the illegally-targeted "
                        "Rebuke dealt lethal damage to it")
                else:
                    obs["notes"].append(
                        "A4 FAILED (bug end-to-end, destination "
                        "unrecorded): Rocky Rebuke resolved with the "
                        "targeted TEAMMATE's creature "
                        f"(oid {ctx.get('a4_second_target')}) absent from "
                        "the post-resolution export (not on the "
                        "battlefield, not in Graveyard/Exile); the "
                        "illegal target was accepted and the creature "
                        "left the battlefield")
                obs["assert"]["A4_resolution_correct"] = "failed"
            else:
                obs["assert"]["A4_resolution_correct"] = "failed"
                obs["notes"].append(
                    "A4 FAILED (bug, partial): the teammate's creature was "
                    "targeted as 'an opponent controls' but did not leave "
                    f"the battlefield (zone {ctx.get('a4_zone')}); the "
                    "illegal target was accepted at selection time")
        else:
            if ctx.get("a4_zone") in ("Graveyard", "Exile"):
                obs["assert"]["A4_resolution_correct"] = "passed"
                obs["notes"].append(
                    "A4 passed: Rocky Rebuke resolved dealing damage to "
                    "the targeted opponent's creature "
                    f"(oid {ctx.get('a4_second_target')}), which left the "
                    f"battlefield to the {ctx.get('a4_zone')}")
            elif ctx.get("a4_zone") is None:
                obs["assert"]["A4_resolution_correct"] = "failed"
                obs["notes"].append(
                    "A4 FAILED: Rocky Rebuke targeted an opponent's "
                    f"creature (oid {ctx.get('a4_second_target')}) but it "
                    "is absent from the post-resolution export (not on "
                    "the battlefield, not in Graveyard/Exile); "
                    "destination unrecorded")
            else:
                obs["assert"]["A4_resolution_correct"] = "failed"
                obs["notes"].append(
                    "A4 FAILED: Rocky Rebuke targeted an opponent's "
                    f"creature but it did not leave the battlefield "
                    f"(zone {ctx.get('a4_zone')})")
    else:
        obs["assert"]["A4_resolution_correct"] = "not-run"
        obs["notes"].append("A4 not-run: resolution never observed")

    # final state export
    try:
        final = json.loads(await p0.export_state())
        with open(f"{EVDIR}/post_final.json", "w") as f:
            json.dump(final, f, indent=1)
    except Exception as e:
        say(f"final export failed: {e}")

    for c in clients:
        await c.close()
    return obs, ctx


# ------------------------------------------------------------------- render

def render_summary(obs, ctx):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        font_b = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_t = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
        font_b = font
        font_t = font
    img = Image.new("RGB", (1000, 700), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #9858 -- Team formats: \"an opponent controls\" includes teammates", font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.109.0 (932e389, protocol 140) | Two-Headed Giant (teams P0/P1 vs P2/P3) | Rocky Rebuke", font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID} | game {obs.get('game_code')}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = ("REPRODUCED" if obs["assert"].get("A2_teammate_not_offered") == "failed"
               else ("NOT-REPRODUCED" if obs["assert"].get("A2_teammate_not_offered") == "passed"
                     else "BLOCKED / NOT-RUN"))
    color = (255, 90, 90) if verdict == "REPRODUCED" else ((90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    for k in ("A1_setup_ok", "A2_teammate_not_offered", "A3_opponent_offered",
              "A4_resolution_correct"):
        v = obs["assert"].get(k, "not-run")
        vc = (90, 255, 140) if v == "passed" else ((255, 90, 90) if v == "failed" else (255, 200, 60))
        d.text((24, y), f"{k}: {v}", font=font_b, fill=vc)
        y += 30
    y += 10
    d.text((24, y), "Expected (CR 102.3): a teammate's creature is NOT a legal target for", font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "\"target creature an opponent controls\" -- opponents are players not on your team.", font=font, fill=(220, 220, 230))
    y += 26
    if ctx.get("a2_teammate_offered"):
        d.text((24, y), "Observed: the teammate's creature WAS offered as a second target,", font=font, fill=(255, 160, 160))
        y += 26
        d.text((24, y), "and targeting it dealt damage to it -- ControllerRef::Opponent admits != controller.", font=font, fill=(255, 160, 160))
    elif ctx.get("a2_teammate_offered") is False:
        d.text((24, y), "Observed: no teammate creature offered; spell resolved on the opponent's creature.", font=font, fill=(160, 255, 180))
    else:
        d.text((24, y), "Observed: second-target selection never reached in this run.", font=font, fill=(255, 200, 60))
    y += 40
    d.text((24, y), "Energy Field (\"you don't control\") contrast leg NOT exercised in this run;", font=font, fill=(160, 160, 170))
    y += 26
    d.text((24, y), "its != controller reading is the correct one for that phrasing.", font=font, fill=(160, 160, 170))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_attempt(1)
    obs["server_identity"] = SERVER_IDENTITY
    obs["issue"] = ISSUE
    obs["run_id"] = RUN_ID
    obs["format_config"] = FORMAT_2HG
    obs["card_data_oracle"] = {
        "rocky rebuke": (CARD_DATA.get("rocky rebuke") or {}).get("oracle_text"),
        "forbidden orchard": (CARD_DATA.get("forbidden orchard") or {}).get("oracle_text"),
        "fractured identity": (CARD_DATA.get("fractured identity") or {}).get("oracle_text"),
        "grizzly bears": (CARD_DATA.get("grizzly bears") or {}).get("oracle_text"),
    }
    a = obs["assert"]
    if a.get("A2_teammate_not_offered") == "failed":
        verdict = "reproduced"
    elif a.get("A2_teammate_not_offered") == "passed" and \
            a.get("A4_resolution_correct") == "passed":
        verdict = "not-reproduced"
    elif a.get("A2_teammate_not_offered") == "passed" and \
            a.get("A4_resolution_correct") == "failed":
        # Targeting was correct but resolution still failed: a clearly
        # identified related failure (playbook: reproduced).
        verdict = "reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    obs["fixture_vehicle"] = ctx.get("fixture_vehicle")
    obs["orchard_trigger_offered_teammate"] = ctx.get("orchard_trigger_offered_teammate")
    obs["orchard_trigger_players"] = ctx.get("orchard_trigger_players")
    obs["a4_second_target"] = ctx.get("a4_second_target")
    obs["a4_second_is_teammate"] = ctx.get("a4_second_is_teammate")
    obs["a4_second_is_token"] = ctx.get("a4_second_is_token")
    obs["a4_zone"] = ctx.get("a4_zone")
    obs["a4_timeout"] = ctx.get("a4_timeout")
    obs["a4_own_target_zone"] = ctx.get("a4_own_target_zone")
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(obs, f, indent=1, default=str)
    say("assertions: " + json.dumps(obs["assert"]))
    for n in obs["notes"]:
        say("note: " + n)
    say(f"VERDICT: {verdict}")
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9858",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                          "(--single-user --no-data-download, verified release); "
                          f"evidence under evidence/9858/{RUN_ID}",
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Forest", 16], ["Plains", 8], ["Island", 8],
                   ["Forbidden Orchard", 4], ["Grizzly Bears", 8],
                   ["Fractured Identity", 8], ["Rocky Rebuke", 8]],
            "P1": [["Forest", 28], ["Grizzly Bears", 32]],
            "P2": [["Forest", 28], ["Grizzly Bears", 32]],
            "P3": [["Forest", 60]],
        },
        "assertions": dict(a),
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; fixture_vehicle=%s"
                   % (a.get("A1_setup_ok"), a.get("A2_teammate_not_offered"),
                      a.get("A3_opponent_offered"),
                      a.get("A4_resolution_correct"),
                      ctx.get("fixture_vehicle"))),
        "scope": ("Rocky Rebuke second-target filter ('target creature an "
                  "opponent controls', ControllerRef::Opponent) in a real "
                  "Two-Headed Giant game; native engine, four human-client "
                  "seats; fixture vehicles Forbidden Orchard / Fractured "
                  "Identity to put a creature under teammate P1's control"),
        "limitations": [
            "Browser UI not exercised; native engine via four human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "12x/8x deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Forbidden Orchard's second ability is parsed by the engine as a TapsForMana trigger, not a second activated ability; the fixture uses that trigger as implemented.",
            "Verdict rule: reproduced = A2 failed (teammate offered), or A2 passed but A4 failed (related resolution failure). not-reproduced = A2 passed and A4 passed. blocked = target selection never observed.",
        ],
        "setup_line": ("P0 16x Forest + 8x Plains + 8x Island + 4x Forbidden Orchard + 8x Grizzly Bears + 8x Fractured Identity + 8x Rocky Rebuke; P1/P2 28x Forest + 32x Grizzly Bears; P3 60x Forest. 2HG, teams P0/P1 vs P2/P3, starting life 30."),
        "contract_line": ("Put a creature under teammate P1's control (Orchard Spirit token or Fractured Identity copy), then cast Rocky Rebuke: first target own creature, second target must NOT include P1's creature (CR 102.3) but MUST include a P2/P3 creature; if P1's creature is offered and targeted, its death = the bug end-to-end."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump({k: CARD_DATA.get(k) for k in
                   ("rocky rebuke", "forbidden orchard",
                    "fractured identity", "grizzly bears")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9858.py", "w") as f:
        f.write(open(__file__).read())
    render_summary(obs, ctx)
    wire("verdict", {"verdict": verdict, "assertions": dict(a)})
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        with open(f"{EVDIR}/{fn}", "rb") as f:
            lines.append(hashlib.sha256(f.read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(dict(a))}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
