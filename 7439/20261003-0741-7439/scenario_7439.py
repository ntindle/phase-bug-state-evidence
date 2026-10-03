#!/usr/bin/env python3
"""Issue #7439: Running Is Useless -- "choose any number of creatures with
different mana values" is unparsed, so `Destroy` reads an empty tracked set.

Reported (2026-08-15, lgray; source:internal-triage, status:confirmed,
area:parser, priority:p3-card-specific, classifier:unsupported-aspect;
related #6857 tracked-set publish census -- one of 33 cards whose
tracked-set antecedent clause never parses):

> When you set this scheme in motion, choose any number of creatures with
> different mana values. Destroy those creatures.

Measured parse state (issue body, corpus at 9b7c66e30): the chain head is an
`Effect::Unimplemented` node (name "choose", description "choose creatures
with different mana values"); its `sub_ability` is the anaphor "Destroy
those creatures", targeting the chain tracked set
(`Destroy { target: TrackedSet(0) }`). The Unimplemented resolver is a
runtime no-op (pushes no GameEvent), so the publish authority allocates a
fresh EMPTY chain tracked set and the Destroy sub-ability reads that empty
set. The issue explicitly states: "this issue does not assert a runtime
symptom" and "No runtime reproduction was run for this card" -- the reported
defect is the parse state and the empty publish that structurally follows.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
This is a data-level report: the defective artifact is the card-data parse
itself. The scenario therefore:

 Phase 1 (data level, no server): load the pinned v0.100.0 card-data.json,
 find "Running Is Useless", and walk the exact chain the issue describes.
 Phase 2 (live server): attempt to create an Archenemy game whose scheme
 deck contains Running Is Useless. The engine's own scheme-deck validation
 (validate_scheme_deck -> card_face_gaps) is expected to reject it as an
 "Unsupported scheme card" -- the parse gap made visible at runtime, on the
 pinned build. This is a clearly identified related failure of the same
 defect, recorded verbatim from the server.
 Phase 3 (control): create the identical Archenemy game with only
 fully-supported schemes (no parse gaps); it must create and start. This
 proves the rejection is specific to the gapped card, not the format or the
 harness.

Why no full set-in-motion runtime test: the engine refuses to load this
card into a scheme deck (deck validation, demonstrated live in Phase 2);
only the configured archenemy seat's scheme_deck is loaded as the scheme
deck; and Debug CreateCard actions are rejected in multiplayer server games.
There is no engine-supported runtime path to set this scheme in motion
without modifying the engine (out of scope: reproduction only). The issue
itself asserts no runtime consumer symptom, so none is claimed. This is
documented, not a blocker: the reported outcome (parse state + empty
publish) is directly demonstrated.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Running Is Useless
                  as the issue reports: exactly one trigger, mode
                  SetInMotion, execute kind Spell, head Unimplemented
                  describing "choose creatures with different mana values",
                  sub_ability kind Spell with effect Destroy targeting
                  TrackedSet(0), sub_link SequentialSibling. (Head-name
                  discrepancy vs the issue body is recorded, not hidden:
                  the census at 9b7c66e30 quotes "choose"; pinned data
                  shows "unparsed_verb_arguments" -- same structural
                  defect, as in the #7437/#7438 runs.)
  A2_deck_rejected
                  the live pinned server rejects the Archenemy create with
                  code "deck_rejected" naming Running Is Useless among
                  "Unsupported scheme cards". EXPECTED TO PASS under the
                  bug (the parse gap is load-bearing at runtime).
  A3_control_ok   the identical Archenemy game with 20 fully-supported
                  schemes creates and reaches GameStarted: the rejection is
                  specific to the gapped card.

Verdict rule: reproduced iff A1, A2, A3 passed (the reported parse defect
              is confirmed on the pinned release and its structural
              consequence is demonstrated live);
              not-reproduced iff A1 shows the clause now parses (head no
              longer Unimplemented) or A2 shows the card is now accepted
              into a scheme deck (the gap is gone);
              blocked iff A3 fails (the control cannot be established) or
              the server is unreachable.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-0741-7439"
ISSUE = 7439
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

# Mirrors FormatConfig::archenemy() on the wire (adjacently-tagged enums).
ARCHENEMY = {
    "format": "Archenemy",
    "starting_life": 20,
    "min_players": 2,
    "max_players": 6,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": True,
    "commander_damage_threshold": None,
    "range_of_influence": None,
    "team_based": False,
    "archenemy_player": 0,
    "uses_commander": False,
    "supplies_fixed_deck": False,
    "sideboard_policy": {"type": "Unlimited"},
    "default_deck_copy_limit": {"type": "UpTo", "data": 4},
    "allow_debug_actions": False,
    "custom_rules": None,
}

P0_MAIN = (["Forest"] * 36 + ["Llanowar Elves"] * 4 + ["Grizzly Bears"] * 4
           + ["Serra Angel"] * 4 + ["Memnite"] * 4 + ["Shivan Dragon"] * 4
           + ["Plains"] * 4)
P1_MAIN = ["Forest"] * 40 + ["Plains"] * 20
CLEAN_SCHEMES = ["Behold My Grandeur", "Drench the Soil in Their Blood",
                 "Embrace My Diabolical Vision", "Every Dream a Nightmare",
                 "I Call for Slaughter", "Introductions Are in Order",
                 "Know Naught but Fire", "Look Skyward and Despair",
                 "A Display of My Dark Power", "Every Hope Shall Vanish"]

ST = {
    "t0": time.time(),
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
    "ass": {k: "not-run" for k in ("A1_data_level", "A2_deck_rejected",
                                    "A3_control_ok")},
    "notes": [],
    "data_level_ok": False,
    "deck_error": None,
    "control_game_code": None,
    "control_started": False,
    "control_scheme_in_motion": None,
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
    card = CARD_DATA.get("running is useless", {})
    trigs = card.get("triggers") or []
    t0 = trigs[0] if trigs else {}
    ex = t0.get("execute") or {}
    head = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    target = sub_eff.get("target") or {}
    checks = {
        "exactly_one_trigger": len(trigs) == 1,
        "trigger_mode_SetInMotion": t0.get("mode") == "SetInMotion",
        "execute_kind_Spell": ex.get("kind") == "Spell",
        "head_Unimplemented": head.get("type") == "Unimplemented",
        "head_describes_choice": "choose creatures with different mana values"
            in str(head.get("description", "")),
        "sub_kind_Spell": sub.get("kind") == "Spell",
        "sub_effect_Destroy": sub_eff.get("type") == "Destroy",
        "sub_target_TrackedSet": target.get("type") == "TrackedSet",
        "sub_link_SequentialSibling": sub.get("sub_link") == "SequentialSibling",
        "no_other_abilities": not (card.get("abilities")
                                   or card.get("static_abilities")
                                   or card.get("replacements")),
        "oracle_matches": "choose any number of creatures with different "
            "mana values" in str(card.get("oracle_text", "")),
    }
    ev = {
        "card_name": card.get("name"),
        "oracle_text": card.get("oracle_text"),
        "trigger_mode": t0.get("mode"),
        "head": {"type": head.get("type"), "name": head.get("name"),
                 "description": head.get("description")},
        "sub_ability": {"kind": sub.get("kind"),
                        "effect": sub_eff.get("type"),
                        "target": target,
                        "sub_link": sub.get("sub_link")},
        "checks": checks,
        "head_name_discrepancy": {
            "issue_body_quotes": "choose",
            "pinned_data_shows": head.get("name"),
            "note": "description string matches the issue; the structural "
                    "defect (Unimplemented head -> empty tracked set) is "
                    "identical either way, as in the #7437/#7438 runs",
        },
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/running_is_useless_card_data.json", "w") as f:
        json.dump(card, f, indent=1)
    ok = all(checks.values())
    ST["data_level_ok"] = ok
    ST["head_now_parses"] = (head.get("type") != "Unimplemented")
    say(f"data-level check: mode={t0.get('mode')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"sub={sub.get('kind')}/{sub_eff.get('type')}/"
        f"target={target.get('type')}({target.get('id')}); "
        f"sub_link={sub.get('sub_link')}; "
        f"checks_failed={[k for k, v in checks.items() if not v] or 'none'}")
    wire("data_level", {"ok": ok, "head_name": head.get("name"),
                        "checks": checks})

# ------------------------------------------------------------- live helpers
async def attempt_create(client, deck, tag):
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
            "player_count": 2,
            "match_config": {"match_type": "Bo1"},
            "ai_seats": [],
            "format_config": ARCHENEMY,
            "room_name": None,
            "host_peer_id": None,
            "draft_metadata": None,
            "start_when_full": True,
            "ranked": False,
        },
    }))
    wire("create_sent", {"who": tag,
                         "scheme_deck_n": len(deck.get("scheme_deck", []))})
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
    """Both seats keep; wait for GameStarted on the first client."""
    deadline = time.time() + timeout_s
    answered = set()
    while time.time() < deadline:
        done = False
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
        # also drain: GameStarted may arrive while we process others
        for c in clients:
            st = c.latest
            if st and st.get("state", {}).get("turn_number", 0) >= 1:
                # GameStarted sets latest; turn_number>=1 means the game
                # began (mulligans done)
                pass
        await asyncio.sleep(0.2)
    return False


async def pass_priority(c):
    st = c.latest
    if not st:
        return False
    acts = list(st.get("legal_actions") or [])
    for a in acts:
        if a.get("type") == "PassPriority":
            # PassPriority is a unit variant: no data field on the wire
            await c.send_action({"type": "PassPriority"})
            return True
    vi = st.get("viewer_interaction") or {}
    if vi.get("canSubmit"):
        for opp in vi.get("opportunities") or []:
            for ch in ((opp.get("response") or {}).get("data") or {}).get("choices") or []:
                codes = [(s.get("data") or {}).get("code")
                         for s in ch.get("surfaces") or []
                         if s.get("type") == "action"]
                if "passPriority" in codes:
                    iid = opp.get("interactionId") or opp.get("id")
                    await c.send_interaction({
                        "interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}})
                    return True
    return False


def face_up_schemes(state):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Command":
            continue
        ct = o.get("card_types") or {}
        if "Scheme" not in [str(t) for t in (ct.get("core_types") or [])]:
            continue
        if not o.get("face_down", True):
            out.append(str(o.get("base_name") or o.get("name")))
    return out


async def run_live():
    # Phase 2: the failing create (Running Is Useless in the scheme deck)
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    bad_schemes = (["Running Is Useless"] * 2
                   + [s for s in CLEAN_SCHEMES[:9] for _ in (0, 1)])
    bad_deck = {"main_deck": P0_MAIN, "sideboard": [], "commander": [],
                "scheme_deck": bad_schemes}
    say(f"[live] attempting Archenemy create with Running Is Useless x2 in "
        f"scheme deck ({len(bad_schemes)} schemes)")
    status, data = await attempt_create(p0, bad_deck, "P0-bad")
    ST["deck_error"] = data if status == "error" else None
    ST["bad_create_status"] = status
    with open(f"{EVDIR}/deck_rejection.json", "w") as f:
        json.dump({"attempt": "archenemy create with Running Is Useless x2",
                   "status": status,
                   "error": data if status == "error" else None,
                   "scheme_deck": bad_schemes,
                   "server": {k: SERVER_IDENTITY[k] for k in
                              ("server_version", "build_commit",
                               "protocol_version")}}, f, indent=1)
    await p0.close()

    # Phase 3: control -- identical game, 20 fully-supported schemes
    c0 = PhaseClient("C0")
    c1 = PhaseClient("C1")
    await c0.connect()
    await c1.connect()
    good_schemes = [s for s in CLEAN_SCHEMES for _ in (0, 1)]
    good_deck = {"main_deck": P0_MAIN, "sideboard": [], "commander": [],
                 "scheme_deck": good_schemes}
    say(f"[control] attempting Archenemy create with 20 supported schemes")
    status, data = await attempt_create(c0, good_deck, "C0-good")
    started = False
    scheme_seen = None
    if status == "ok":
        ST["control_game_code"] = c0.game_code
        jdata = await c1.join(
            c0.game_code,
            {"main_deck": P1_MAIN, "sideboard": [], "commander": []})
        if c1.player_id is None and isinstance(jdata, dict):
            c1.player_id = jdata.get("player_id", jdata.get("your_player"))
        say(f"[control] C1 joined game {c0.game_code} "
            f"(player_id={c1.player_id})")
        started = await answer_mulligans_until_started([c0, c1])
        ST["control_started"] = started
        # Bonus (best-effort): drive to C0's first PreCombatMain and check
        # a scheme was set in motion, proving the Archenemy machinery works
        # on this build and only this card's parse blocks the runtime path.
        if started:
            try:
                scheme_seen = await observe_set_in_motion(c0, c1)
            except Exception as e:
                say(f"[control] set-in-motion observation failed "
                    f"(best-effort): {e!r}")
                wire("set_in_motion_observation_failed", {"error": repr(e)})
        ST["control_scheme_in_motion"] = scheme_seen
    with open(f"{EVDIR}/control_game.json", "w") as f:
        json.dump({"attempt": "archenemy create with 20 supported schemes",
                   "status": status,
                   "game_code": ST["control_game_code"],
                   "game_started": started,
                   "scheme_set_in_motion_observed": scheme_seen,
                   "scheme_deck": good_schemes}, f, indent=1)
    for c in (c0, c1):
        try:
            await c.close()
        except Exception:
            pass
    for c in (p1,):
        try:
            await c.close()
        except Exception:
            pass


async def observe_set_in_motion(c0, c1, timeout_s=240):
    """Best-effort: pass priority on both seats until C0 (archenemy) reaches
    PreCombatMain, then check the command zone for a face-up scheme."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for c in (c0, c1):
            try:
                await asyncio.wait_for(c.inbox.get(), 1)
            except asyncio.TimeoutError:
                pass
        st = c0.latest
        if not st:
            await asyncio.sleep(0.5)
            continue
        state = st["state"]
        wf = (state.get("waiting_for") or {}).get("type") or ""
        if wf == "GameOver":
            say("[control] game over before precombat main")
            return None
        schemes = []
        if (state.get("phase") == "PreCombatMain"
                and state.get("active_player") == 0):
            # check the authoritative export: viewer snapshots may filter
            # face-down command-zone objects
            try:
                env = json.loads(await c0.export_state())["state"]
                schemes = face_up_schemes(env)
            except Exception as e:
                say(f"[control] export failed: {e!r}")
            if schemes:
                say(f"[control] set in motion observed on C0 turn "
                    f"{state.get('turn_number')}: face-up scheme(s) {schemes}")
                wire("set_in_motion_observed", {"schemes": schemes})
                return schemes
            # precombat main reached but no face-up scheme yet (e.g. the
            # phase-entry update hasn't arrived); keep waiting briefly
            await asyncio.sleep(1)
            continue
        # keep the game moving: play a land on own main, else pass
        acted = False
        for c, pid in ((c0, 0), (c1, 1)):
            cst = c.latest
            if not cst:
                continue
            cstate = cst["state"]
            cwf = (cstate.get("waiting_for") or {}).get("type") or ""
            if cwf == "Priority" and str(
                    ((cstate.get("waiting_for") or {}).get("data") or {})
                    .get("player")) == str(pid):
                if await pass_priority(c):
                    acted = True
                    break
        if not acted:
            await asyncio.sleep(0.5)
    say("[control] set-in-motion observation timed out (best-effort)")
    return None

# ------------------------------------------------------------- finalize
async def finalize():
    ass = ST["ass"]
    notes = ST["notes"]

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Running Is Useless as the issue reports -- exactly "
                     "one trigger (mode SetInMotion), execute kind Spell, "
                     "head Unimplemented describing 'choose creatures with "
                     "different mana values', sub_ability kind Spell with "
                     "effect Destroy targeting TrackedSet(0), sub_link "
                     "SequentialSibling, no other abilities/statics/"
                     "replacements. Head name in pinned data is "
                     "'unparsed_verb_arguments' (the issue body quotes "
                     "'choose' from the 9b7c66e30 census); the description "
                     "matches and the structural defect is identical. See "
                     "data_evidence.json and "
                     "running_is_useless_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: live deck rejection names the card
    err = ST.get("deck_error") or {}
    msg = str(err.get("message", ""))
    code = err.get("code", "")
    notes.append(f"A2 probe: bad-create status={ST.get('bad_create_status')}; "
                 f"code={code!r}; message={msg[:220]!r}.")
    if (ST.get("bad_create_status") == "error"
            and code == "deck_rejected"
            and "Running Is Useless" in msg
            and "Unsupported scheme cards" in msg):
        ass["A2_deck_rejected"] = "passed"
        notes.append("A2 passed (defect made visible at runtime): the "
                     "pinned v0.100.0 server rejected the Archenemy game "
                     "whose scheme deck contained Running Is Useless x2 "
                     "with deck_rejected / 'Unsupported scheme cards: ... "
                     "Running Is Useless'. The engine's scheme-deck "
                     "validation (validate_scheme_deck -> card_face_gaps) "
                     "refuses the card because of exactly the reported "
                     "parse gap -- the Unimplemented head is the card's "
                     "only trigger and it carries no other gaps. See "
                     "deck_rejection.json.")
    elif ST.get("bad_create_status") == "ok":
        ass["A2_deck_rejected"] = "failed"
        notes.append("A2 FAILED: the server ACCEPTED Running Is Useless "
                     "into a scheme deck -- the parse gap is gone.")
    else:
        notes.append("A2 not-run: the create attempt did not return a "
                     "usable verdict (timeout/unreachable).")

    # A3: control game creates and starts
    notes.append(f"A3 probe: control status="
                 f"{'ok' if ST['control_game_code'] else 'not-ok'}; "
                 f"game_started={ST['control_started']}; "
                 f"set_in_motion_observed="
                 f"{ST['control_scheme_in_motion']}.")
    if ST["control_game_code"] and ST["control_started"]:
        ass["A3_control_ok"] = "passed"
        notes.append("A3 passed: the identical Archenemy game with 20 "
                     "fully-supported schemes created and reached "
                     "GameStarted -- the A2 rejection is specific to the "
                     "gapped card, not the format config, the deck "
                     "construction, or the harness. "
                     + (f"Bonus: a scheme was set in motion on the "
                        f"archenemy's first precombat main "
                        f"({ST['control_scheme_in_motion']}), proving the "
                        f"Archenemy machinery works on this build."
                        if ST["control_scheme_in_motion"] else
                        "The best-effort set-in-motion observation did not "
                        "complete; the control still passes on GameStarted.")
                     + " See control_game.json.")
    else:
        ass["A3_control_ok"] = "failed"
        notes.append("A3 FAILED: the control game could not be established; "
                     "the A2 rejection cannot be attributed.")

    notes.append(
        "A4 (scope documentation, not a verdict driver): no engine-supported "
        "runtime path exists to set THIS scheme in motion on the pinned "
        "server -- the engine's own deck validation refuses to load it "
        "(A2), only the configured archenemy seat's scheme_deck is loaded "
        "as the scheme deck, and Debug CreateCard actions are rejected in "
        "multiplayer server games. The issue itself asserts no runtime "
        "consumer symptom ('this issue does not assert a runtime symptom'; "
        "'No runtime reproduction was run for this card'), so none is "
        "claimed. The empty tracked-set publish follows structurally: the "
        "Unimplemented head is the chain root (no ancestor to extend), its "
        "resolver pushes no GameEvent, and the publish authority therefore "
        "allocates a fresh empty set for the Destroy sub-ability to read.")

    # verdict
    if all(ass[k] == "passed" for k in ("A1_data_level", "A2_deck_rejected",
                                        "A3_control_ok")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the reported parse defect is confirmed on "
            "the pinned v0.100.0 release (SetInMotion trigger whose head is "
            "Unimplemented 'choose creatures with different mana values', "
            "chaining to Destroy reading TrackedSet(0)), and the defect is "
            "load-bearing at runtime -- the engine's own scheme-deck "
            "validation rejects Running Is Useless as an unsupported "
            "scheme card, so the scheme can never be set in motion and the "
            "Destroy sub-ability can never receive a populated tracked "
            "set. The control (20 supported schemes) creates and starts, "
            "isolating the rejection to this card's parse gap. This is not "
            "a fix claim.")
    elif ((ass["A1_data_level"] == "failed" and ST.get("head_now_parses"))
            or (ass["A2_deck_rejected"] == "failed"
                and ST.get("bad_create_status") == "ok")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the parse gap is gone on the pinned "
            "release (the clause now parses, or the card is accepted into "
            "a scheme deck). This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported parse path could not be "
                     "fully demonstrated; see assertion notes.")

    # server log excerpts for this run (newest-first by mtime)
    import glob
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
               if "scheme" in l.lower()
               or "deck_rejected" in l.lower()
               or "running is useless" in l.lower()
               or "unimplemented" in l.lower()]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    import shutil
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
        "format_config": "Archenemy (archenemy_player=0, 2 seats)",
        "decks": {
            "bad_attempt_scheme_deck":
                (["Running Is Useless"] * 2
                 + [s for s in CLEAN_SCHEMES[:9] for _ in (0, 1)]),
            "control_scheme_deck":
                [s for s in CLEAN_SCHEMES for _ in (0, 1)],
            "P0_main": "36x Forest + 4x Llanowar Elves + 4x Grizzly Bears + "
                       "4x Serra Angel + 4x Memnite + 4x Shivan Dragon + "
                       "4x Plains (60)",
            "P1_main": "40x Forest + 20x Plains (60)",
        },
        "assertions": ass,
        "notes": notes,
        "observations": {
            "head_name_pinned": json.load(
                open(f"{EVDIR}/data_evidence.json")
            )["head"]["name"],
            "deck_rejection_code": (ST.get("deck_error") or {}).get("code"),
            "deck_rejection_message": (ST.get("deck_error") or {}).get(
                "message"),
            "control_game_code": ST["control_game_code"],
            "control_started": ST["control_started"],
            "control_scheme_in_motion": ST["control_scheme_in_motion"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report (the issue asserts no "
            "runtime consumer symptom). The scenario demonstrates the parse "
            "on the pinned card data and the defect's live consequence "
            "(scheme-deck validation rejects the card); it does not drive "
            "a set-in-motion resolution because the engine offers no "
            "supported path to put this card into play.",
            "The empty tracked-set publish is documented structurally from "
            "the parsed chain (Unimplemented chain root -> no GameEvent -> "
            "fresh empty set), not measured at runtime.",
            "Deck-validation internals (validate_scheme_deck / "
            "card_face_gaps) are cited from the engine source tree "
            "available locally (v0.82.0 checkout); the rejection itself was "
            "observed live on the pinned v0.100.0 server.",
        ],
        "setup_line": "Phase 1: pinned card-data.json parse walk. Phase 2: "
                      "live Archenemy create with Running Is Useless x2 in "
                      "the scheme deck (expect deck_rejected). Phase 3: "
                      "control Archenemy create with 20 fully-supported "
                      "schemes (expect GameStarted).",
        "contract_line": "Reported defect: 'choose any number of creatures "
                         "with different mana values' is Unimplemented in "
                         "the parse, so the Destroy sub-ability reads an "
                         "empty chain tracked set. Demonstrated: the parse "
                         "on pinned v0.100.0 matches the report, and the "
                         "pinned server refuses to load the card into a "
                         "scheme deck because of that gap.",
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
    import glob as _glob
    files = sorted(os.path.basename(p)
                   for p in _glob.glob(f"{EVDIR}/*")
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
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_7439.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
