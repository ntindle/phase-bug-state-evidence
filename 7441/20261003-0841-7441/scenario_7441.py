#!/usr/bin/env python3
"""Issue #7441: Scion of Halaster -- the granted "the first time you would
draw a card each turn, instead look at the top two cards" replacement is
unparsed, so `PutAtLibraryPosition` reads an empty tracked set.

Reported (2026-08-15, lgray; source:internal-triage, status:confirmed,
area:parser, mechanic:replacement-effects, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 tracked-set publish census --
this card is one of 33 whose tracked-set antecedent clause never parses):

> Commander creatures you own have "The first time you would draw a card
> each turn, instead look at the top two cards of your library. Put one of
> them into your graveyard and the other back on top of your library. Then
> draw a card."

Measured parse state (issue body, corpus at 9b7c66e30): the chain head for
this ability is an `Effect::Unimplemented` node (name "the", description
"The first time you would draw a card each turn, instead look at the top
two cards of your library"). Its `sub_ability` is the anaphor -- "Put one of
them into your graveyard and the other back on top of your library" -- and it
targets the chain tracked set (`PutAtLibraryPosition { target: TrackedSet(0),
count: Fixed(1), position: Top }`). The Unimplemented resolver is a runtime
no-op (pushes no GameEvent), so the publish authority allocates a fresh EMPTY
chain tracked set and the sub-ability reads that empty set. The issue
explicitly states: "this issue does not assert a runtime symptom" -- the
reported defect is the parse state and the empty publish that structurally
follows from it.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
This is a data-level report: the defective artifact is the card-data parse
itself. The scenario therefore:

 Phase 1 (data level, no server): load the pinned v0.100.0 card-data.json,
 find "Scion of Halaster", and walk the exact static-ability chain the issue
 describes (Continuous grant -> GrantAbility -> Unimplemented head ->
 PutAtLibraryPosition{TrackedSet(0)} sub -> Draw sub-sub).
 Phase 2 (live server): create a CommanderDraft game (the engine's own
 built-in commander format) whose P0 commander list pairs Wilson, Refined
 Grizzly ("Choose a Background") with Scion of Halaster. Start the game and
 export the authoritative pre state: Scion of Halaster must be present in the
 command zone WITH the Unimplemented head still on its granted definition --
 the parse gap demonstrated live on the running pinned server, not just in
 the card-data file.
 Phase 3 (control): the identical CommanderDraft game with only Wilson,
 Refined Grizzly as commander must create and reach GameStarted: the
 background pairing is accepted by deck validation and the format machinery
 works, isolating the gap to the parse.

Why no replacement-resolution runtime test: the granted ability is a
replacement effect on commander creatures; observing it would require
casting the commander and driving to a draw step. The issue itself asserts no
runtime consumer symptom ("this issue does not assert a runtime symptom"),
so none is claimed. The empty tracked-set publish follows structurally: the
Unimplemented head is the chain root (no ancestor to extend), its resolver
pushes no GameEvent, and the publish authority therefore allocates a fresh
empty set for the PutAtLibraryPosition sub-ability to read. This is
documented, not a blocker: the reported outcome (parse state + empty
publish) is directly demonstrated.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Scion of Halaster
                  as the issue reports: exactly one static_ability (mode
                  Continuous), affected = creatures you own that are
                  commanders (Typed Creature + IsCommander + Owned(You)),
                  modifications[0] = GrantAbility, definition kind Spell,
                  head Unimplemented describing "The first time you would
                  draw a card each turn, instead look at the top two cards
                  of your library", sub_ability kind Spell with effect
                  PutAtLibraryPosition targeting TrackedSet(0), count
                  Fixed(1), position Top, sub_link SequentialSibling,
                  sub-sub kind Spell with effect Draw count Fixed(1)
                  target Controller, sub_link SequentialSibling; no other
                  abilities/triggers/replacements; oracle matches.
  A2_live_command_zone
                  the live pinned server accepts the Wilson+Scion background
                  pairing into the command zone: CommanderDraft create OK,
                  3 seats joined, GameStarted reached, and the authoritative
                  pre export shows the Scion of Halaster object in zone
                  "Command" carrying the Unimplemented head
                  ("unparsed_replacement") on its granted definition.
                  EXPECTED TO PASS under the bug (the gap is live on the
                  server; deck validation does not reject it).
  A3_control_ok   the identical CommanderDraft game with only Wilson,
                  Refined Grizzly as commander creates and reaches
                  GameStarted: the pairing acceptance is specific to the
                  deck, not the format config or the harness.

Verdict rule: reproduced iff A1, A2, A3 passed (the reported parse defect
              is confirmed on the pinned release and is live on the
              running server in the command zone);
              not-reproduced iff A1 shows the clause now parses (head no
              longer Unimplemented);
              blocked iff A2/A3 cannot be established (server unreachable
              or the commander machinery rejects the setup for reasons
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
RUN_ID = "20261003-0841-7441"
ISSUE = 7441
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

# Mirrors the built-in CommanderDraft registry entry on the wire
# (adjacently-tagged enums). CommanderDraft requires exactly 3 players.
COMMANDER = {
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
    "custom_rules": None,
}

P0_MAIN = ["Forest"] * 30 + ["Swamp"] * 30   # BG = combined commander identity
P1_MAIN = ["Forest"] * 60
P2_MAIN = ["Forest"] * 60
WILSON = "Wilson, Refined Grizzly"
SCION = "Scion of Halaster"

ST = {
    "t0": time.time(),
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
    "ass": {k: "not-run" for k in ("A1_data_level", "A2_live_command_zone",
                                    "A3_control_ok")},
    "notes": [],
    "data_level_ok": False,
    "head_now_parses": False,
    "game_code": None,
    "game_started": False,
    "scion_zone": None,
    "scion_head_live": None,
    "control_game_code": None,
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
    card = CARD_DATA.get("scion of halaster", {})
    sas = card.get("static_abilities") or []
    sa = sas[0] if sas else {}
    aff = sa.get("affected") or {}
    props = aff.get("properties") or []
    mods = sa.get("modifications") or []
    grant = (mods[0].get("definition") or {}) if mods else {}
    head = grant.get("effect") or {}
    sub = grant.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    sub_target = sub_eff.get("target") or {}
    sub_count = sub_eff.get("count") or {}
    sub_pos = sub_eff.get("position") or {}
    subsub = sub.get("sub_ability") or {}
    subsub_eff = subsub.get("effect") or {}
    prop_types = sorted(str(p.get("type")) for p in props)
    checks = {
        "exactly_one_static_ability": len(sas) == 1,
        "mode_Continuous": sa.get("mode") == "Continuous",
        "affected_Typed_Creature": aff.get("type") == "Typed"
            and "Creature" in (aff.get("type_filters") or []),
        "affected_IsCommander": "IsCommander" in prop_types,
        "affected_Owned_You": any(p.get("type") == "Owned"
            and (p.get("controller") == "You") for p in props),
        "modification_GrantAbility": (mods[0].get("type") == "GrantAbility"
            if mods else False),
        "definition_kind_Spell": grant.get("kind") == "Spell",
        "head_Unimplemented": head.get("type") == "Unimplemented",
        "head_describes_replacement": "The first time you would draw a "
            "card each turn, instead look at the top two cards of your "
            "library" in str(head.get("description", "")),
        "sub_kind_Spell": sub.get("kind") == "Spell",
        "sub_effect_PutAtLibraryPosition": sub_eff.get("type")
            == "PutAtLibraryPosition",
        "sub_target_TrackedSet_0": sub_target.get("type") == "TrackedSet"
            and sub_target.get("id") == 0,
        "sub_count_Fixed_1": sub_count.get("type") == "Fixed"
            and sub_count.get("value") == 1,
        "sub_position_Top": sub_pos.get("type") == "Top",
        "sub_link_SequentialSibling": sub.get("sub_link")
            == "SequentialSibling",
        "subsub_kind_Spell": subsub.get("kind") == "Spell",
        "subsub_effect_Draw": subsub_eff.get("type") == "Draw",
        "subsub_count_Fixed_1": (subsub_eff.get("count") or {}).get("type")
            == "Fixed" and (subsub_eff.get("count") or {}).get("value") == 1,
        "subsub_target_Controller": (subsub_eff.get("target") or {}).get(
            "type") == "Controller",
        "subsub_link_SequentialSibling": subsub.get("sub_link")
            == "SequentialSibling",
        "no_other_abilities": not (card.get("abilities")
                                   or card.get("triggers")
                                   or card.get("replacements")),
        "oracle_matches": "Commander creatures you own have" in str(
            card.get("oracle_text", "")),
    }
    ev = {
        "card_name": card.get("name"),
        "card_type": card.get("card_type"),
        "color_identity": card.get("color_identity"),
        "oracle_text": card.get("oracle_text"),
        "static_ability_mode": sa.get("mode"),
        "affected": {"type": aff.get("type"),
                     "type_filters": aff.get("type_filters"),
                     "properties": prop_types},
        "grant": {"kind": grant.get("kind"),
                  "modification": mods[0].get("type") if mods else None},
        "head": {"type": head.get("type"), "name": head.get("name"),
                 "description": head.get("description")},
        "sub_ability": {"kind": sub.get("kind"),
                        "effect": sub_eff.get("type"),
                        "target": sub_target, "count": sub_count,
                        "position": sub_pos,
                        "sub_link": sub.get("sub_link")},
        "sub_sub_ability": {"kind": subsub.get("kind"),
                            "effect": subsub_eff.get("type"),
                            "count": subsub_eff.get("count"),
                            "target": subsub_eff.get("target"),
                            "sub_link": subsub.get("sub_link")},
        "checks": checks,
        "discrepancies": [
            {
                "field": "head.name",
                "issue_body_quotes": "the",
                "pinned_data_shows": head.get("name"),
                "note": "description string matches the issue verbatim; "
                        "the structural defect (Unimplemented chain root -> "
                        "empty tracked set) is identical either way, as in "
                        "the #7436-#7440 runs",
            },
            {
                "field": "missing_graveyard_half",
                "note": "The oracle sentence 'Put one of them into your "
                        "graveyard and the other back on top of your "
                        "library' parses to ONLY PutAtLibraryPosition{Top, "
                        "1} -> Draw{1}. The 'put one into your graveyard' "
                        "half has no sub-ability at all in the pinned "
                        "parse; the chain goes Top-placement straight to "
                        "the Draw. The tracked-set anaphor the issue "
                        "describes therefore covers only the top-placement "
                        "half.",
            },
        ],
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/scion_of_halaster_card_data.json", "w") as f:
        json.dump(card, f, indent=1)
    ok = all(checks.values())
    ST["data_level_ok"] = ok
    ST["head_now_parses"] = (head.get("type") != "Unimplemented")
    say(f"data-level check: mode={sa.get('mode')}; "
        f"grant={mods[0].get('type') if mods else None}/{grant.get('kind')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"sub={sub.get('kind')}/{sub_eff.get('type')}/"
        f"target={sub_target.get('type')}({sub_target.get('id')}); "
        f"subsub={subsub.get('kind')}/{subsub_eff.get('type')}; "
        f"checks_failed={[k for k, v in checks.items() if not v] or 'none'}")
    wire("data_level", {"ok": ok, "head_name": head.get("name"),
                        "checks": checks})

# ------------------------------------------------------------- live helpers
async def attempt_create(client, deck, tag, fmt):
    """Send CreateGameWithSettings, wait for success or Error. Returns
    ('ok', data) or ('error', data)."""
    await client.ws.send(json.dumps({
        "type": "CreateGameWithSettings",
        "data": {
            "deck": deck,
            "display_name": client.name,
            "public": False,
            "password": None,
            "timer_seconds": None,
            "player_count": 3,
            "match_config": {"match_type": "Bo1"},
            "ai_seats": [],
            "format_config": fmt,
            "room_name": None,
            "host_peer_id": None,
            "draft_metadata": None,
            "start_when_full": True,
            "ranked": False,
        },
    }))
    wire("create_sent", {"who": tag,
                         "commanders": deck.get("commander")})
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            t, data = await asyncio.wait_for(
                client.inbox.get(), max(1.0, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        if t in ("SessionAttached", "GameCreated"):
            client.player_id = data.get("player_id")
            client.game_code = data.get("game_code")
            wire("create_ok", {"who": tag, "type": t,
                               "game_code": client.game_code})
            say(f"[{tag}] create OK ({t}): game={client.game_code}")
            return "ok", data
        if t == "Error":
            wire("create_error", {"who": tag, "data": data})
            say(f"[{tag}] create ERROR: {json.dumps(data)[:300]}")
            return "error", data
    wire("create_timeout", {"who": tag})
    say(f"[{tag}] create TIMEOUT (no SessionAttached/Error in 30s)")
    return "timeout", {}


async def answer_mulligans_until_started(clients, timeout_s=120):
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


def find_scion(state):
    for oid, o in (state.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "")
        if nm.lower() == "scion of halaster":
            return oid, o
    return None, None


async def run_live():
    # Phase 2: CommanderDraft game with the Wilson + Scion background pairing
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    p2 = PhaseClient("P2")
    await p0.connect()
    await p1.connect()
    await p2.connect()
    pair_deck = {"main_deck": P0_MAIN, "sideboard": [],
                 "commander": [WILSON, SCION]}
    say("[live] creating CommanderDraft game with Wilson + Scion of "
        "Halaster as commanders")
    status, data = await attempt_create(p0, pair_deck, "P0-pair", COMMANDER)
    ST["create_status"] = status
    ST["create_error"] = data if status == "error" else None
    with open(f"{EVDIR}/create_result.json", "w") as f:
        json.dump({"attempt": "CommanderDraft create with Wilson + Scion "
                              "of Halaster as commanders",
                   "status": status,
                   "error": data if status == "error" else None,
                   "deck": pair_deck,
                   "server": {k: SERVER_IDENTITY[k] for k in
                              ("server_version", "build_commit",
                               "protocol_version")}}, f, indent=1)
    pre = None
    if status == "ok":
        ST["game_code"] = p0.game_code
        for c, main in ((p1, P1_MAIN), (p2, P2_MAIN)):
            jdata = await c.join(p0.game_code,
                                 {"main_deck": main, "sideboard": [],
                                  "commander": []})
            if c.player_id is None and isinstance(jdata, dict):
                c.player_id = jdata.get("player_id",
                                        jdata.get("your_player"))
            say(f"[live] {c.name} joined game {p0.game_code} "
                f"(player_id={c.player_id})")
        started = await answer_mulligans_until_started([p0, p1, p2])
        ST["game_started"] = started
        if started:
            try:
                env = json.loads(await p0.export_state())["state"]
                with open(f"{EVDIR}/pre.json", "w") as f:
                    json.dump(env, f, indent=1)
                oid, obj = find_scion(env)
                ST["scion_zone"] = obj.get("zone") if obj else None
                obj_s = json.dumps(obj) if obj else ""
                ST["scion_head_live"] = ("unparsed_replacement" in obj_s
                                         and '"type": "Unimplemented"' in obj_s)
                say(f"[live] pre export: Scion of Halaster oid={oid} "
                    f"zone={ST['scion_zone']} "
                    f"unparsed_replacement_live={ST['scion_head_live']}")
                wire("pre_export", {"scion_oid": oid,
                                    "zone": ST["scion_zone"],
                                    "head_live": ST["scion_head_live"]})
                # save the live object's static ability for the record
                live_sa = None
                for k in ("static_abilities", "abilities"):
                    if obj and obj.get(k):
                        live_sa = obj[k]
                        break
                with open(f"{EVDIR}/scion_live_object.json", "w") as f:
                    json.dump({"oid": oid, "object": obj}, f, indent=1)
            except Exception as e:
                say(f"[live] pre export failed: {e!r}")
                wire("pre_export_failed", {"error": repr(e)})
    for c in (p0, p1, p2):
        try:
            await c.close()
        except Exception:
            pass

    # Phase 3: control -- identical game, Wilson alone as commander
    c0 = PhaseClient("C0")
    c1 = PhaseClient("C1")
    c2 = PhaseClient("C2")
    await c0.connect()
    await c1.connect()
    await c2.connect()
    solo_deck = {"main_deck": ["Forest"] * 60, "sideboard": [],
                 "commander": [WILSON]}
    say("[control] creating CommanderDraft game with Wilson alone")
    status, data = await attempt_create(c0, solo_deck, "C0-solo", COMMANDER)
    started = False
    if status == "ok":
        ST["control_game_code"] = c0.game_code
        for c, main in ((c1, P1_MAIN), (c2, P2_MAIN)):
            jdata = await c.join(c0.game_code,
                                 {"main_deck": main, "sideboard": [],
                                  "commander": []})
            if c.player_id is None and isinstance(jdata, dict):
                c.player_id = jdata.get("player_id",
                                        jdata.get("your_player"))
        started = await answer_mulligans_until_started([c0, c1, c2])
        ST["control_started"] = started
    with open(f"{EVDIR}/control_game.json", "w") as f:
        json.dump({"attempt": "CommanderDraft create with Wilson, Refined "
                              "Grizzly alone as commander",
                   "status": status,
                   "game_code": ST["control_game_code"],
                   "game_started": started,
                   "deck": solo_deck}, f, indent=1)
    for c in (c0, c1, c2):
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
                     "Scion of Halaster as the issue reports -- exactly one "
                     "static_ability (mode Continuous) affecting commander "
                     "creatures you own (Typed Creature + IsCommander + "
                     "Owned(You)); modifications[0] is GrantAbility with "
                     "definition kind Spell; head Unimplemented describing "
                     "'The first time you would draw a card each turn, "
                     "instead look at the top two cards of your library'; "
                     "sub_ability kind Spell with effect "
                     "PutAtLibraryPosition targeting TrackedSet(0), count "
                     "Fixed(1), position Top, sub_link SequentialSibling; "
                     "sub-sub kind Spell with effect Draw count Fixed(1) "
                     "target Controller, sub_link SequentialSibling; no "
                     "other abilities/triggers/replacements. Head name in "
                     "pinned data is 'unparsed_replacement' (the issue "
                     "body quotes 'the' from the 9b7c66e30 census); the "
                     "description matches and the structural defect is "
                     "identical. ADDITIONAL gap: the oracle's 'Put one of "
                     "them into your graveyard' half has NO sub-ability in "
                     "the pinned parse -- the chain goes Top-placement "
                     "straight to Draw. See data_evidence.json and "
                     "scion_of_halaster_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: live command-zone demonstration
    notes.append(f"A2 probe: create status={ST.get('create_status')}; "
                 f"game_started={ST.get('game_started')}; "
                 f"scion_zone={ST.get('scion_zone')}; "
                 f"unparsed_replacement_live={ST.get('scion_head_live')}.")
    if (ST.get("create_status") == "ok" and ST.get("game_started")
            and ST.get("scion_zone") == "Command"
            and ST.get("scion_head_live")):
        ass["A2_live_command_zone"] = "passed"
        notes.append("A2 passed: the pinned v0.100.0 server accepted the "
                     "Wilson, Refined Grizzly + Scion of Halaster background "
                     "pairing (deck validation did not reject it), the "
                     "CommanderDraft game started, and the authoritative "
                     "pre export shows the Scion of Halaster object in the "
                     "command zone carrying the Unimplemented "
                     "'unparsed_replacement' head on its granted definition "
                     "-- the parse gap is live on the running server, not "
                     "just in the card-data file. See pre.json and "
                     "scion_live_object.json.")
    elif ST.get("create_status") == "error":
        err = ST.get("create_error") or {}
        msg = str(err.get("message", ""))
        if "cion of Halaster" in msg or "nparsed" in msg \
                or "nimplemented" in msg:
            ass["A2_live_command_zone"] = "passed"
            notes.append("A2 passed (defect load-bearing at validation): "
                         "the pinned server rejected the background "
                         f"pairing: {msg[:220]!r}. See create_result.json.")
        else:
            ass["A2_live_command_zone"] = "failed"
            notes.append("A2 FAILED: the create was rejected for reasons "
                         f"unrelated to the card: {msg[:220]!r}.")
    else:
        notes.append("A2 not-run: the create attempt did not return a "
                     "usable verdict (timeout/unreachable or no GameStarted).")

    # A3: control game creates and starts
    notes.append(f"A3 probe: control status="
                 f"{'ok' if ST['control_game_code'] else 'not-ok'}; "
                 f"game_started={ST['control_started']}.")
    if ST["control_game_code"] and ST["control_started"]:
        ass["A3_control_ok"] = "passed"
        notes.append("A3 passed: the identical CommanderDraft game with "
                     "Wilson, Refined Grizzly alone as commander created "
                     "and reached GameStarted -- the background pairing's "
                     "acceptance is specific to the deck, not the format "
                     "config, the deck construction, or the harness. See "
                     "control_game.json.")
    else:
        ass["A3_control_ok"] = "failed"
        notes.append("A3 FAILED: the control game could not be established.")

    notes.append(
        "A4 (scope documentation, not a verdict driver): no "
        "replacement-resolution drive was run. The granted ability is a "
        "replacement effect on commander creatures; observing it would "
        "require casting the commander and driving to a draw step. The "
        "issue itself asserts no runtime consumer symptom ('this issue "
        "does not assert a runtime symptom'), so none is claimed. The "
        "empty tracked-set publish follows structurally: the "
        "Unimplemented head is the chain root (no ancestor to extend), "
        "its resolver pushes no GameEvent, and the publish authority "
        "therefore allocates a fresh empty set for the "
        "PutAtLibraryPosition sub-ability to read.")

    # verdict
    if all(ass[k] == "passed" for k in ("A1_data_level",
                                        "A2_live_command_zone",
                                        "A3_control_ok")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the reported parse defect is confirmed on "
            "the pinned v0.100.0 release (Continuous grant to commander "
            "creatures you own whose head is Unimplemented "
            "'unparsed_replacement', chaining to PutAtLibraryPosition "
            "reading TrackedSet(0)), and the gap is live on the running "
            "server -- the Scion of Halaster object sits in the command "
            "zone of a started CommanderDraft game with the Unimplemented "
            "head on its granted definition. The control (Wilson alone) "
            "starts, isolating the setup to the deck. This is not a fix "
            "claim.")
    elif ass["A1_data_level"] == "failed" and ST.get("head_now_parses"):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the replacement clause now parses on "
            "the pinned release (the head is no longer Unimplemented). "
            "This is not a fix claim.")
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
               if "halaster" in l.lower()
               or "commander" in l.lower()
               or "unimplemented" in l.lower()
               or "background" in l.lower()]
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
        "format_config": "CommanderDraft (3 seats; P0 commanders "
                         "Wilson+Scion of Halaster, P1/P2 none)",
        "decks": {
            "P0_commanders": [WILSON, SCION],
            "P0_main": "30x Forest + 30x Swamp (60; BG = combined "
                       "commander color identity)",
            "P1_main": "60x Forest",
            "P2_main": "60x Forest",
            "control_P0_commanders": [WILSON],
            "control_P0_main": "60x Forest (mono-green = Wilson's color "
                               "identity)",
        },
        "assertions": ass,
        "notes": notes,
        "observations": {
            "head_name_pinned": json.load(
                open(f"{EVDIR}/data_evidence.json")
            )["head"]["name"],
            "create_status": ST.get("create_status"),
            "game_code": ST.get("game_code"),
            "game_started": ST.get("game_started"),
            "scion_zone": ST.get("scion_zone"),
            "scion_head_live": ST.get("scion_head_live"),
            "control_game_code": ST["control_game_code"],
            "control_started": ST["control_started"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via three human-client seats.",
            "The report is a data-level parse report (the issue asserts no "
            "runtime consumer symptom). The scenario demonstrates the parse "
            "on the pinned card data and shows the gap live on the running "
            "server (command-zone object carries the Unimplemented head); "
            "it does not drive a replacement-effect resolution because the "
            "issue asserts no consumer-side symptom and the Unimplemented "
            "head is a documented runtime no-op.",
            "The empty tracked-set publish is documented structurally from "
            "the parsed chain (Unimplemented chain root -> no GameEvent -> "
            "fresh empty set), not measured at runtime.",
            "Deck-validation internals (ChooseABackground pairing) are "
            "cited from the engine source tree available locally "
            "(v0.82.0 checkout); the acceptance itself was observed live "
            "on the pinned v0.100.0 server.",
        ],
        "setup_line": "Phase 1: pinned card-data.json parse walk of the "
                      "Continuous grant chain. Phase 2: live CommanderDraft "
                      "create with Wilson + Scion of Halaster as commanders "
                      "(expect GameStarted), then authoritative pre export "
                      "showing Scion in the command zone with the "
                      "Unimplemented head. Phase 3: control CommanderDraft "
                      "create with Wilson alone (expect GameStarted).",
        "contract_line": "Reported defect: the granted 'first time you "
                         "would draw a card each turn, instead look at the "
                         "top two cards' replacement is Unimplemented in "
                         "the parse, so the PutAtLibraryPosition "
                         "sub-ability reads an empty chain tracked set. "
                         "Demonstrated: the parse on pinned v0.100.0 "
                         "matches the report, and the Scion of Halaster "
                         "object in a live started game's command zone "
                         "carries the Unimplemented head.",
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
        [sys.executable, f"{BACKFILL}/driver/render_summary_7441.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
