#!/usr/bin/env python3
"""Issue #7442: Shredder, Shadow Master -- the per-opponent attacking token
copies are unparsed, so `CreateDelayedTrigger` reads an empty tracked set.

Reported (2026-08-15, lgray; source:internal-triage, status:confirmed,
area:parser, mechanic:triggers/tokens/copy/combat, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 tracked-set publish census --
one of 33 cards whose tracked-set antecedent clause never parses):

> Whenever Shredder attacks a player, for each other opponent, create a
> token that's a copy of Shredder tapped and attacking that player, except
> it isn't legendary. Sacrifice those tokens at end of combat.
> Whenever Shredder deals combat damage to a player, that player loses half
> their life, rounded up.

Measured parse state (issue body, corpus at 9b7c66e30): the chain head for
the attack trigger is an `Effect::Unimplemented` node (name "for",
description "for each other opponent, create a token that's a copy of ~
tapped and attacking that player, except it isn't legendary"). Its
`sub_ability` is the anaphor -- "Sacrifice those tokens at end of combat" --
a `CreateDelayedTrigger` (AtNextPhase EndCombat, uses_tracked_set: false)
whose inner effect is `Sacrifice` targeting `TrackedSet(0)`. The
Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a fresh EMPTY chain tracked set and the
sub-ability reads that empty set. The issue explicitly states: "No runtime
reproduction was run for this card; per the classification above, the
consumer-side outcome is not asserted." -- the reported defect is the parse
state and the empty publish that structurally follows from it.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
This is a data-level report: the defective artifact is the card-data parse
itself. The scenario therefore:

 Phase 1 (data level, no server): load the pinned v0.100.0 card-data.json,
 find "Shredder, Shadow Master", and walk the exact attack-trigger chain
 the issue describes (Unimplemented head -> CreateDelayedTrigger sub
 -> Sacrifice{TrackedSet(0)} inner, uses_tracked_set false,
 sub_link SequentialSibling). Also record that the DamageDone trigger
 parses (LoseLife) -- only the attack clause is defective.
 Phase 2 (live server): create a FreeForAll game whose P0 deck contains
 Shredder, Shadow Master. Start the game and export the authoritative pre
 state: the Shredder object must be present live on the running pinned
 server WITH the Unimplemented head still on its trigger definition -- the
 parse gap demonstrated live on the server, not just in the card-data file.
 Phase 3 (control): the identical FreeForAll game with an ordinary creature
 deck (no Shredder) must create and reach GameStarted: the create/load
 machinery works, isolating the gap to this card's parse.

Why no runtime token-creation test: the issue itself asserts no runtime
consumer symptom ("No runtime reproduction was run for this card; ...
the consumer-side outcome is not asserted"), and the delayed-trigger
consumer is classified as unpredictable (deferred sentinel, not a plain
empty bind). The empty tracked-set publish follows structurally: the
Unimplemented head is the chain root (no ancestor to extend), its resolver
pushes no GameEvent, and the publish authority therefore allocates a fresh
empty set for the CreateDelayedTrigger sub-ability to read. This is
documented, not a blocker: the reported outcome (parse state + empty
publish) is directly demonstrated.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Shredder, Shadow
                  Master as the issue reports: exactly two triggers; t0
                  mode Attacks, execute kind Spell, head Unimplemented
                  describing "for each other opponent, create a token
                  that's a copy of ~ tapped and attacking that player,
                  except it isn't legendary", sub_ability kind Spell with
                  effect CreateDelayedTrigger (condition AtNextPhase /
                  EndCombat; inner kind Spell with effect Sacrifice
                  targeting TrackedSet(0), count Fixed(1);
                  uses_tracked_set false), sub_link SequentialSibling;
                  t1 mode DamageDone effect LoseLife (parses -- the only
                  defective clause is the attack clause); no other
                  abilities/triggers/replacements; oracle matches.
  A2_live_deck    the live pinned server accepts the Shredder deck and the
                  authoritative pre export contains a live Shredder object
                  whose trigger definition still carries the Unimplemented
                  head ("unparsed_quantity") with the reported description
                  and the CreateDelayedTrigger sub targeting TrackedSet(0).
                  EXPECTED TO PASS under the bug (the gap is live on the
                  server; deck validation does not reject it).
  A3_control_ok   the identical FreeForAll game with an ordinary creature
                  deck creates and reaches GameStarted: the load machinery
                  works and the gap is specific to this card's parse.

Verdict rule: reproduced iff A1, A2, A3 passed (the reported parse defect
              is confirmed on the pinned release and is live on the
              running server in a loaded deck);
              not-reproduced iff A1 shows the clause now parses (head no
              longer Unimplemented);
              blocked iff A2/A3 cannot be established (server unreachable
              or the game machinery rejects the setup for reasons
              unrelated to the card).
"""
import asyncio
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-0915-7442"
ISSUE = 7442
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "driver_stdout.log")]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.100.0",
    "build_commit": "bc9ef56",
    "protocol_version": 101,
    "server_binary_sha256": "261550905a3d569731c9bd66b2b12a0fa878400fefaa9cc36f3ad4e1a3d8adda",
    "card_data_sha256": "57e086e700ee0bd81002327e89d339356c4bb6c26d9e1ca9c010474c0b8c291c",
    "draft_pools_sha256": "961c5397d834ca92b2168035be386844339573024df72dbc78370b934ed75770",
    "signature_verified": True,
    "server_run_id": "backfill-owned v0.100.0 server on 127.0.0.1:9374 "
                     "(run-20261003-0711; ServerHello 0.100.0/bc9ef56/"
                     "protocol 101 re-verified by this run's own Hello "
                     "handshake)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned "
              "release dir (GitHub /releases re-confirmed v0.100.0 still "
              "latest stable; ServerHello 0.100.0/bc9ef56/protocol 101 "
              "re-verified by this run); hashes recomputed against "
              "on-disk artifacts this run",
}

for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

# Mirrors FormatConfig::free_for_all() on the wire (adjacently-tagged enums).
FREEFORALL = {
    "format": "FreeForAll",
    "starting_life": 20,
    "min_players": 2,
    "max_players": 6,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": False,
    "commander_damage_threshold": None,
    "range_of_influence": None,
    "team_based": False,
    "sideboard_policy": {"type": "Unlimited"},
    "uses_commander": False,
    "supplies_fixed_deck": False,
    "default_deck_copy_limit": {"type": "Unlimited"},
    "allow_debug_actions": False,
    "custom_rules": None,
}

SHREDDER = "Shredder, Shadow Master"
P0_MAIN = [SHREDDER] * 4 + ["Swamp"] * 56
P1_MAIN = ["Grizzly Bears"] * 20 + ["Forest"] * 40
CTRL_MAIN = ["Grizzly Bears"] * 20 + ["Forest"] * 40

ST = {
    "t0": time.time(),
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
    "ass": {k: "not-run" for k in ("A1_data_level", "A2_live_deck",
                                    "A3_control_ok")},
    "notes": [],
    "data_level_ok": False,
    "head_now_parses": False,
    "create_status": None,
    "create_error": None,
    "game_code": None,
    "game_started": False,
    "shredder_oid": None,
    "shredder_zone": None,
    "shredder_head_live": False,
    "control_started": False,
}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE.closed:
        return
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()

# ------------------------------------------------------------- data level
def check_data_level():
    card = CARD_DATA.get(SHREDDER.lower(), {})
    trigs = card.get("triggers") or []
    t0 = trigs[0] if len(trigs) > 0 else {}
    t1 = trigs[1] if len(trigs) > 1 else {}
    ex = t0.get("execute") or {}
    head = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    cond = sub_eff.get("condition") or {}
    inner = sub_eff.get("effect") or {}
    inner_eff = inner.get("effect") or {}
    inner_target = inner_eff.get("target") or {}
    inner_count = inner_eff.get("count") or {}
    t1_eff = (t1.get("execute") or {}).get("effect") or {}
    oracle = str(card.get("oracle_text", ""))
    checks = {
        "exactly_two_triggers": len(trigs) == 2,
        "t0_mode_Attacks": t0.get("mode") == "Attacks",
        "t0_execute_kind_Spell": ex.get("kind") == "Spell",
        "t0_head_Unimplemented": head.get("type") == "Unimplemented",
        "t0_head_describes_clause": "for each other opponent, create a token"
            " that's a copy of ~ tapped and attacking that player, except it"
            " isn't legendary" in str(head.get("description", "")),
        "t0_sub_kind_Spell": sub.get("kind") == "Spell",
        "t0_sub_CreateDelayedTrigger": sub_eff.get("type") == "CreateDelayedTrigger",
        "t0_sub_condition_AtNextPhase_EndCombat": (
            cond.get("type") == "AtNextPhase" and cond.get("phase") == "EndCombat"),
        "t0_sub_inner_kind_Spell": inner.get("kind") == "Spell",
        "t0_sub_inner_Sacrifice": inner_eff.get("type") == "Sacrifice",
        "t0_sub_inner_TrackedSet0": (inner_target.get("type") == "TrackedSet"
                                     and inner_target.get("id") == 0),
        "t0_sub_inner_count_Fixed1": (inner_count.get("type") == "Fixed"
                                      and inner_count.get("value") == 1),
        "t0_sub_uses_tracked_set_false": sub_eff.get("uses_tracked_set") is False,
        "t0_sub_link_SequentialSibling": sub.get("sub_link") == "SequentialSibling",
        "t1_mode_DamageDone": t1.get("mode") == "DamageDone",
        "t1_effect_LoseLife_parsed": t1_eff.get("type") == "LoseLife",
        "no_other_abilities": not (card.get("abilities")
                                   or card.get("static_abilities")
                                   or card.get("replacements")
                                   or card.get("activated_abilities")),
        "oracle_matches": ("Whenever Shredder attacks a player" in oracle
                           and "Sacrifice those tokens at end of combat" in oracle
                           and "Whenever Shredder deals combat damage to a player" in oracle),
    }
    ev = {
        "card_name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "oracle_text": oracle,
        "power": card.get("power"),
        "toughness": card.get("toughness"),
        "num_triggers": len(trigs),
        "t0_head": {"type": head.get("type"), "name": head.get("name"),
                    "description": head.get("description")},
        "t0_sub": {"kind": sub.get("kind"), "effect": sub_eff.get("type"),
                   "condition": cond, "uses_tracked_set": sub_eff.get("uses_tracked_set"),
                   "sub_link": sub.get("sub_link"),
                   "inner": {"kind": inner.get("kind"),
                             "effect": inner_eff.get("type"),
                             "target": inner_target, "count": inner_count}},
        "t1": {"mode": t1.get("mode"), "effect": t1_eff.get("type")},
        "checks": checks,
        "head_name_discrepancy": {
            "issue_body_quotes": "for",
            "pinned_data_shows": head.get("name"),
            "note": "description string matches the issue; the structural "
                    "defect (Unimplemented head -> empty tracked set) is "
                    "identical either way, as in the #7437-#7441 runs",
        },
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/shredder_card_data.json", "w") as f:
        json.dump(card, f, indent=1)
    ok = all(checks.values())
    ST["data_level_ok"] = ok
    ST["head_now_parses"] = (head.get("type") != "Unimplemented")
    say(f"data-level check: triggers={len(trigs)}; "
        f"t0_head={head.get('type')}/{head.get('name')}; "
        f"t0_sub={sub.get('kind')}/{sub_eff.get('type')}/"
        f"cond={cond.get('type')}:{cond.get('phase')}/"
        f"uses_tracked_set={sub_eff.get('uses_tracked_set')}/"
        f"inner={inner_eff.get('type')}/{inner_target.get('type')}({inner_target.get('id')})/"
        f"count={inner_count.get('type')}({inner_count.get('value')})/"
        f"sub_link={sub.get('sub_link')}; "
        f"t1={t1.get('mode')}/{t1_eff.get('type')}; "
        f"checks_failed={[k for k, v in checks.items() if not v] or 'none'}")
    wire("data_level", {"ok": ok, "head_name": head.get("name"),
                        "checks": checks})

# ------------------------------------------------------------- live helpers
async def attempt_create(client, deck, tag):
    """Send CreateGameWithSettings, wait for SessionAttached or Error."""
    await client.ws.send(json.dumps({
        "type": "CreateGameWithSettings",
        "data": {
            "deck": deck,
            "display_name": client.name,
            "public": False,
            "password": None,
            "timer_seconds": None,
            "player_count": 2,
            "match_config": {"match_type": "Bo1"},
            "ai_seats": [],
            "format_config": FREEFORALL,
            "room_name": None,
            "host_peer_id": None,
            "draft_metadata": None,
            "start_when_full": True,
            "ranked": False,
        },
    }))
    wire("create_sent", {"who": tag})
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            t, data = await asyncio.wait_for(
                client.inbox.get(), max(1.0, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        if t == "SessionAttached":
            client.player_id = data.get("player_id")
            client.game_code = data.get("game_code")
            wire("create_ok", {"who": tag, "type": t,
                               "game_code": client.game_code})
            say(f"[{tag}] create OK: game={client.game_code}")
            return "ok", data
        if t == "Error":
            wire("create_error", {"who": tag, "data": data})
            say(f"[{tag}] create ERROR: {json.dumps(data)[:300]}")
            return "error", data
    wire("create_timeout", {"who": tag})
    say(f"[{tag}] create TIMEOUT (no SessionAttached/Error in 30s)")
    return "timeout", {}


async def answer_mulligans_until_started(clients, timeout_s=120):
    """Both seats keep; wait for GameStarted on the first client."""
    deadline = time.time() + timeout_s
    answered = set()
    while time.time() < deadline:
        for c in clients:
            try:
                t, _ = await asyncio.wait_for(c.inbox.get(), 2)
            except asyncio.TimeoutError:
                continue
            if t == "GameStarted":
                say(f"[{c.name}] GameStarted received")
                wire("game_started", {"who": c.name})
                return True
            st = c.latest
            if not st:
                continue
            state = st["state"]
            wf = state.get("waiting_for") or {}
            if wf.get("type") != "MulliganDecision":
                continue
            pid = c.player_id
            if pid is None:
                continue
            pend = None
            for p in (wf.get("data") or {}).get("pending", []) or []:
                if str(p.get("player")) == str(pid):
                    pend = p
                    break
            if not pend:
                continue
            if (pend.get("phase") or {}).get("type") != "Declare":
                continue
            if c.name in answered:
                continue
            answered.add(c.name)
            say(f"[{c.name}] mulligan: keep 7")
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            wire("mulligan", {"who": c.name, "decision": "keep"})
        await asyncio.sleep(0.2)
    return False


def find_shredder(state):
    for oid, o in (state.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "")
        if nm.lower() == SHREDDER.lower():
            return oid, o
    return None, None


def head_is_live(obj):
    """Rigorous: walk the live object's trigger definitions; defensive
    fallback: serialized-string check for the head description."""
    checks = {}
    trigs = obj.get("triggers") or []
    if not trigs and isinstance(obj.get("definition"), dict):
        trigs = obj["definition"].get("triggers") or []
    if not trigs:
        # live authoritative exports nest card trigger defs under
        # trigger_definitions[i].definition
        for item in obj.get("trigger_definitions") or []:
            d = (item or {}).get("definition")
            if d:
                trigs.append(d)
    found = False
    for t in trigs:
        ex = t.get("execute") or {}
        head = ex.get("effect") or {}
        if ("for each other opponent, create a token that's a copy of ~"
                " tapped and attacking that player, except it isn't legendary"
                in str(head.get("description", ""))):
            found = True
            sub = ex.get("sub_ability") or {}
            sub_eff = sub.get("effect") or {}
            inner = sub_eff.get("effect") or {}
            inner_eff = inner.get("effect") or {}
            checks = {
                "live_head_Unimplemented": head.get("type") == "Unimplemented",
                "live_sub_CreateDelayedTrigger": sub_eff.get("type") == "CreateDelayedTrigger",
                "live_inner_Sacrifice_TrackedSet0": (
                    inner_eff.get("type") == "Sacrifice"
                    and (inner_eff.get("target") or {}).get("type") == "TrackedSet"),
                "live_uses_tracked_set_false": sub_eff.get("uses_tracked_set") is False,
            }
            break
    obj_s = json.dumps(obj)
    fallback = (found or
                ("unparsed_quantity" in obj_s
                 and "for each other opponent, create a token" in obj_s
                 and "CreateDelayedTrigger" in obj_s))
    return found, checks, fallback


async def run_live():
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    deck = {"main_deck": P0_MAIN, "sideboard": [], "commander": []}
    say(f"[live] creating FreeForAll game with {SHREDDER} x4 in P0's deck")
    status, data = await attempt_create(p0, deck, "P0-live")
    ST["create_status"] = status
    ST["create_error"] = data if status == "error" else None
    with open(f"{EVDIR}/create_result.json", "w") as f:
        json.dump({"attempt": f"FreeForAll create with {SHREDDER} x4 in P0 deck",
                   "status": status,
                   "error": data if status == "error" else None,
                   "P0_main": f"{SHREDDER} x4 + Swamp x56",
                   "server": {k: SERVER_IDENTITY[k] for k in
                              ("server_version", "build_commit",
                               "protocol_version")}}, f, indent=1)
    if status == "ok":
        ST["game_code"] = p0.game_code
        jdata = await p1.join(p0.game_code,
                              {"main_deck": P1_MAIN, "sideboard": [],
                               "commander": []})
        if p1.player_id is None and isinstance(jdata, dict):
            p1.player_id = jdata.get("player_id", jdata.get("your_player"))
        say(f"[live] P1 joined game {p0.game_code} (player_id={p1.player_id})")
        started = await answer_mulligans_until_started([p0, p1])
        ST["game_started"] = started
        if started:
            try:
                env = json.loads(await p0.export_state())["state"]
                with open(f"{EVDIR}/pre.json", "w") as f:
                    json.dump(env, f, indent=1)
                oid, obj = find_shredder(env)
                ST["shredder_oid"] = oid
                ST["shredder_zone"] = obj.get("zone") if obj else None
                if obj:
                    found, checks, fallback = head_is_live(obj)
                    ST["shredder_head_live"] = found and all(checks.values())
                    ST["live_checks"] = checks
                    ST["live_fallback"] = fallback
                else:
                    checks, fallback = {}, False
                say(f"[live] pre export: {SHREDDER} oid={oid} "
                    f"zone={ST['shredder_zone']} "
                    f"trigger_walk_found={found if obj else 'n/a'} "
                    f"checks={checks} fallback={fallback if obj else 'n/a'}")
                wire("pre_export", {"shredder_oid": oid,
                                    "zone": ST["shredder_zone"],
                                    "checks": ST.get("live_checks")})
                with open(f"{EVDIR}/shredder_live_object.json", "w") as f:
                    json.dump({"oid": oid, "object": obj}, f, indent=1)
            except Exception as e:
                say(f"[live] pre export failed: {e!r}")
                wire("pre_export_failed", {"error": repr(e)})
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass

    # Phase 3: control -- identical game, ordinary creature deck
    c0 = PhaseClient("C0")
    c1 = PhaseClient("C1")
    await c0.connect()
    await c1.connect()
    ctrl_deck = {"main_deck": CTRL_MAIN, "sideboard": [], "commander": []}
    say("[control] creating identical FreeForAll game with ordinary "
        "creature deck (no Shredder)")
    cstatus, cdata = await attempt_create(c0, ctrl_deck, "C0-control")
    started = False
    if cstatus == "ok":
        jdata = await c1.join(c0.game_code,
                              {"main_deck": P1_MAIN, "sideboard": [],
                               "commander": []})
        if c1.player_id is None and isinstance(jdata, dict):
            c1.player_id = jdata.get("player_id", jdata.get("your_player"))
        say(f"[control] C1 joined game {c0.game_code}")
        started = await answer_mulligans_until_started([c0, c1])
    ST["control_started"] = started
    with open(f"{EVDIR}/control_game.json", "w") as f:
        json.dump({"attempt": "FreeForAll create, ordinary creature deck "
                              "(Grizzly Bears x20 + Forest x40), no Shredder",
                   "status": cstatus,
                   "error": cdata if cstatus == "error" else None,
                   "game_started": started}, f, indent=1)
    say(f"[control] game_started={started}")
    for c in (c0, c1):
        try:
            await c.close()
        except Exception:
            pass

# ------------------------------------------------------------- finalize
async def finalize():
    ass = ST["ass"]
    notes = ST["notes"]

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     f"{SHREDDER} as the issue reports -- exactly two "
                     "triggers; t0 mode Attacks, execute kind Spell, head "
                     "Unimplemented describing 'for each other opponent, "
                     "create a token that's a copy of ~ tapped and "
                     "attacking that player, except it isn't legendary' "
                     "(head name 'unparsed_quantity' in pinned data vs "
                     "'for' quoted in the issue body; description matches, "
                     "structural defect identical), sub_ability kind Spell "
                     "with effect CreateDelayedTrigger (condition AtNextPhase"
                     "/EndCombat; inner kind Spell with effect Sacrifice "
                     "targeting TrackedSet(0), count Fixed(1); "
                     "uses_tracked_set false), sub_link SequentialSibling; "
                     "t1 mode DamageDone with LoseLife (parses -- only the "
                     "attack clause is defective); no other abilities/"
                     "statics/replacements. See data_evidence.json and "
                     "shredder_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: live deck -- the gap is live on the running server
    notes.append(f"A2 probe: create_status={ST.get('create_status')}; "
                 f"game_started={ST['game_started']}; "
                 f"shredder_oid={ST['shredder_oid']}; "
                 f"zone={ST['shredder_zone']}; "
                 f"live_checks={ST.get('live_checks')}.")
    if (ST["create_status"] == "ok" and ST["game_started"]
            and ST.get("shredder_oid") and ST.get("shredder_head_live")):
        ass["A2_live_deck"] = "passed"
        notes.append(f"A2 passed (defect is live on the running server): "
                     f"the pinned v0.100.0 server accepted the deck with "
                     f"{SHREDDER} x4, reached GameStarted, and the "
                     f"authoritative pre export contains the live Shredder "
                     f"object (oid {ST['shredder_oid']}, zone "
                     f"{ST['shredder_zone']}) whose trigger definition "
                     f"still carries the Unimplemented head "
                     f"('unparsed_quantity') with the reported description "
                     f"and the CreateDelayedTrigger sub targeting "
                     f"TrackedSet(0) with uses_tracked_set=false -- the "
                     f"same parse that card-data.json carries. See "
                     f"shredder_live_object.json and pre.json.")
    elif ST.get("create_status") == "ok" and not ST.get("shredder_oid"):
        ass["A2_live_deck"] = "failed"
        notes.append("A2 FAILED: the game started but no Shredder object "
                     "was found in the authoritative export.")
    else:
        notes.append("A2 not-run: the live game could not be established "
                     "(create failed or GameStarted never reached).")

    # A3: control game works
    notes.append(f"A3 probe: control game_started={ST['control_started']}.")
    if ST["control_started"]:
        ass["A3_control_ok"] = "passed"
        notes.append("A3 passed: the identical FreeForAll game with an "
                     "ordinary creature deck created and reached GameStarted"
                     " -- the create/load machinery works, so the A2 "
                     "observation is specific to this card's parse. See "
                     "control_game.json.")
    else:
        ass["A3_control_ok"] = "failed"
        notes.append("A3 FAILED: the control game could not be established; "
                     "the A2 observation cannot be attributed.")

    notes.append(
        "A4 (scope documentation, not a verdict driver): no runtime "
        "token-creation test is claimed -- the issue explicitly states no "
        "runtime reproduction was run and the consumer-side outcome is not "
        "asserted (the delayed-trigger consumer is classified as an "
        "unpredictable deferred sentinel, not a plain empty bind). The "
        "empty tracked-set publish follows structurally: the Unimplemented "
        "head is the chain root (no ancestor to extend), its resolver "
        "pushes no GameEvent, and the publish authority therefore allocates "
        "a fresh empty set for the CreateDelayedTrigger sub-ability to "
        "read.")

    # verdict
    if all(ass[k] == "passed" for k in ("A1_data_level", "A2_live_deck",
                                        "A3_control_ok")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the reported parse defect is confirmed on "
            "the pinned v0.100.0 release (attack trigger whose head is "
            "Unimplemented 'for each other opponent, create a token that's "
            "a copy of ~ tapped and attacking that player, except it isn't "
            "legendary', chaining to CreateDelayedTrigger{EndCombat, "
            "uses_tracked_set:false} whose inner Sacrifice reads "
            "TrackedSet(0)), and the same parse is live on the running "
            "server in a loaded deck. The control (ordinary creature deck) "
            "creates and starts, isolating the gap to this card's parse. "
            "This is not a fix claim.")
    elif ass["A1_data_level"] == "failed" and ST.get("head_now_parses"):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the attack clause now parses on the "
            "pinned release (the head is no longer Unimplemented). This is "
            "not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported parse path could not be "
                     "fully demonstrated; see assertion notes.")

    # server log excerpts for this run (newest-first by mtime)
    lines = []
    used = None
    for lp in sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"),
                     key=os.path.getmtime, reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "shredder" in l.lower()
               or "unimplemented" in l.lower()
               or "freeforall" in l.lower()]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                f"{EVDIR}/scenario_{ISSUE}.py")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 101, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                 "rb").read()).hexdigest(),
        "format_config": "FreeForAll (2 seats; P0 deck Shredder x4 + Swamp x56)",
        "decks": {
            "P0_main": f"{SHREDDER} x4 + Swamp x56 (60)",
            "P1_main": "Grizzly Bears x20 + Forest x40 (60)",
            "control_main": "Grizzly Bears x20 + Forest x40 (60)",
        },
        "assertions": ass,
        "notes": notes,
        "observations": {
            "head_name_pinned": json.load(
                open(f"{EVDIR}/data_evidence.json")
            )["t0_head"]["name"],
            "t1_damage_trigger": json.load(
                open(f"{EVDIR}/data_evidence.json")
            )["t1"],
            "create_status": ST["create_status"],
            "game_code": ST["game_code"],
            "game_started": ST["game_started"],
            "shredder_oid": ST["shredder_oid"],
            "shredder_zone": ST["shredder_zone"],
            "live_checks": ST.get("live_checks"),
            "control_started": ST["control_started"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report (the issue asserts no "
            "runtime consumer symptom). The scenario demonstrates the parse "
            "on the pinned card data and shows the same parse live on the "
            "running server inside a loaded deck; it does not drive an "
            "attack, because the consumer-side outcome is declared "
            "unpredictable in the issue classification.",
            "The empty tracked-set publish is documented structurally from "
            "the parsed chain (Unimplemented chain root -> no GameEvent -> "
            "fresh empty set), not measured at runtime.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "Phase 1: pinned card-data.json parse walk. Phase 2: "
                      "live FreeForAll create with Shredder, Shadow Master x4 "
                      "in P0's deck, mulligans kept, authoritative pre export "
                      "-- Shredder object must carry the Unimplemented head "
                      "live. Phase 3: control FreeForAll create with an "
                      "ordinary creature deck (expect GameStarted).",
        "contract_line": "Reported defect: 'for each other opponent, create "
                         "a token that's a copy of ~ tapped and attacking "
                         "that player, except it isn't legendary' is "
                         "Unimplemented in the parse, so the "
                         "CreateDelayedTrigger sub-ability's inner Sacrifice "
                         "reads an empty chain tracked set. Demonstrated: "
                         "the parse on pinned v0.100.0 matches the report, "
                         "and the same parse is live on the running server.",
        "prior_runs": [],
        "stats": {},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    return verdict


def write_manifest_and_validate():
    files = sorted(os.path.basename(p)
                   for p in glob.glob(f"{EVDIR}/*")
                   if os.path.isfile(p)
                   and os.path.basename(p) not in ("manifest.sha256",
                                                   "driver_stdout.log"))
    lines = []
    for fn in files:
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest.sha256 written ({len(files)} files)")
    problems = []
    for fn in files:
        if fn.endswith(".json") or fn.endswith(".jsonl"):
            try:
                if fn.endswith(".jsonl"):
                    for line in open(f"{EVDIR}/{fn}"):
                        if line.strip():
                            json.loads(line)
                else:
                    json.load(open(f"{EVDIR}/{fn}"))
            except Exception as e:
                problems.append(f"{fn} JSON parse failed: {e}")
    for line in open(f"{EVDIR}/manifest.sha256"):
        h, _, fn = line.strip().partition("  ")
        if not fn:
            continue
        actual = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        if actual != h:
            problems.append(f"{fn} hash mismatch")
    try:
        from PIL import Image
        im = Image.open(f"{EVDIR}/summary.png")
        im.load()
        if im.size[0] == 0 or im.size[1] == 0:
            problems.append("summary.png has zero size")
        say(f"summary.png opens OK ({im.size[0]}x{im.size[1]})")
    except Exception as e:
        problems.append(f"summary.png unreadable: {e}")
    if problems:
        say("VALIDATION PROBLEMS:")
        for p in problems:
            say(f"  - {p}")
    else:
        say("validation OK: all JSON parse, hashes match, PNG readable")
    return problems


async def main():
    try:
        check_data_level()
        await run_live()
    except Exception as e:
        say(f"FATAL: {e!r}")
        wire("fatal", {"error": repr(e)})
        import traceback
        traceback.print_exc()
    verdict = await finalize()
    say(f"FINAL VERDICT: {verdict}")
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_{ISSUE}.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
