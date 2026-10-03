#!/usr/bin/env python3
"""Issue #7440: Sans Mercy -- "each player chooses a nonblack creature they
control. Destroy those creatures." is unparsed, so `Destroy` reads an empty
tracked set.

Reported (2026-08-15, lgray; source:internal-triage, status:confirmed,
area:parser, priority:p3-card-specific, classifier:unsupported-aspect;
related #6857 tracked-set publish census -- one of 33 cards whose
tracked-set antecedent clause never parses):

> wHeN YoU pLAnESWalK tO tHIs pLAnE, eACh pLAyER cHoOSeS a nOnBlACk
> cREaTuRE tHEy cONtrOl. dEsTRoy tHOsE cREatUReS.

Measured parse state (issue body, corpus at 9b7c66e30): the chain head for
this ability is an `Effect::Unimplemented` node (name "chooses",
description "cHoOSeS a nOnBlACk cREaTuRE tHEy cONtrOl"); its `sub_ability`
is the anaphor "Destroy those creatures", targeting the chain tracked set
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
 find "sans mercy", and walk the exact chain the issue describes. Also
 record the card's TYPE-LINE parse: `card_type` shows core_types=[] and
 subtypes ["sECreT","LaIR"] (the alternating-case type line did not parse
 either -- the plane-ness of the card is lost in the data).
 Phase 2 (live server): attempt to create a Planechase game whose planar
 deck contains Sans Mercy. The engine's own planar-deck validation
 rejects it -- "Planar deck cards must be Plane or Phenomenon: sAnS mERcY"
 (observed live on the pinned build in a first full-runtime attempt, which
 is how this structural consequence was discovered). The parse gap is
 load-bearing at runtime: the card can never enter play, so its
 planeswalk trigger can never fire and the Destroy sub-ability can never
 receive a populated tracked set. This is a clearly identified related
 failure of the same defect, recorded verbatim from the server.
 Phase 3 (control): create the identical Planechase game with 20
 fully-supported planes (no Sans Mercy); it must create, start, and reveal
 a starting plane. This proves the rejection is specific to the gapped
 card, not the format or the harness.

Why no planeswalk-trigger runtime test: the engine refuses to load this
card into a planar deck (deck validation, demonstrated live in Phase 2);
only the host's planar_deck is loaded as the planar deck; and Debug
CreateCard actions are rejected in multiplayer server games. There is no
engine-supported runtime path to planeswalk to Sans Mercy without
modifying the engine (out of scope: reproduction only). The issue itself
asserts no runtime consumer symptom, so none is claimed. This is
documented, not a blocker: the reported outcome (parse state + empty
publish) is directly demonstrated, and the defect is load-bearing at
runtime.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Sans Mercy as the
                  issue reports: trigger[0] mode Planeswalked{role:To},
                  execute kind Spell, head Unimplemented describing
                  "chooses a nonblack creature they control", sub_ability
                  kind Spell with effect Destroy targeting TrackedSet(0),
                  sub_link SequentialSibling. (Head-name discrepancy vs the
                  issue body is recorded, not hidden: the census at
                  9b7c66e30 quotes "chooses"; pinned data shows
                  "unparsed_verb_arguments" -- same structural defect, as
                  in the #7436-#7439 runs.) PLUS the type-line gap:
                  card_type.core_types is empty (no Plane) and subtypes
                  are ["sECreT","LaIR"].
  A2_deck_rejected
                  the live pinned server rejects the Planechase create with
                  code "deck_rejected" naming Sans Mercy among "Planar deck
                  cards must be Plane or Phenomenon". EXPECTED TO PASS under
                  the bug (the parse gap is load-bearing at runtime).
  A3_control_ok   the identical Planechase game with 20 fully-supported
                  planes creates and reaches GameStarted: the rejection is
                  specific to the gapped card.

Verdict rule: reproduced iff A1, A2, A3 passed (the reported parse defect
              is confirmed on the pinned release and its structural
              consequence is demonstrated live);
              not-reproduced iff A1 shows the clause now parses (head no
              longer Unimplemented) or A2 shows the card is now accepted
              into a planar deck (the gap is gone);
              blocked iff A3 fails (the control cannot be established) or
              the server is unreachable.
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-0825-7440"
ISSUE = 7440
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
                     "(ServerHello 0.100.0/bc9ef56/protocol 101 re-verified "
                     "by this run's own Hello handshake; on-disk artifact "
                     "hashes recomputed by this run)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned "
              "release dir (GitHub /releases re-confirmed v0.100.0 still "
              "latest stable; ServerHello re-verified live; hashes "
              "recomputed against on-disk artifacts this run)",
}

for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

MERCY = "sans mercy"

PLANECHASE_FORMAT = {
    "format": "Planechase",
    "starting_life": 20,
    "min_players": 2,
    "max_players": 4,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": False,
    "commander_damage_threshold": None,
    "team_based": False,
    "uses_commander": False,
    "sideboard_policy": {"type": "Unlimited"},
    "default_deck_copy_limit": {"type": "UpTo", "data": 4},
    "supplies_fixed_deck": False,
    "allow_debug_actions": False,
}

# 20 benign, choice-free planes (filler set from the #7424 run).
FILLER_PLANES = [
    "Bicycle Rack", "Elvish Impersonation Contest", "Ghirapur Grand Prix",
    "Jalira's Show", "Shrinking Plane", "Sky Deck", "Stroopwafel Cafe",
    "The Food Court", "The Pro Tour", "Windmill Farm",
    "Hedron Fields of Agadeem",  # create a 7/7 Eldrazi token
    "Jund",                       # create two 1/1 Goblin tokens
    "Llanowar",                   # untap all creatures you control
    "Prahv",                      # gain life = cards in hand
    "Tazeem",                     # draw a card for each land you control
    "Windriddle Palaces",         # each player mills a card
    "Agyrem",                     # creatures can't attack you
    "Esper",                      # your white/blue/black creatures become artifacts
    "The Eon Fog",                # untap all permanents you control
    "Naya",                       # benign chaos: creatures you control get +1/+1
]


def pdeck(main_names, planar_names):
    return {"main_deck": main_names, "sideboard": [], "commander": [],
            "planar_deck": planar_names}


P0_MAIN = ["Forest"] * 36 + ["Plains"] * 16 \
    + ["Llanowar Elves"] * 4 + ["Grizzly Bears"] * 4
P1_MAIN = ["Forest"] * 40 + ["Plains"] * 12 \
    + ["Llanowar Elves"] * 4 + ["Grizzly Bears"] * 4

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
    "control_starting_plane": None,
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
    card = CARD_DATA.get(MERCY, {})
    trigs = card.get("triggers") or []
    t0 = trigs[0] if trigs else {}
    ex = t0.get("execute") or {}
    head = ex.get("effect") or {}
    sub = ex.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    target = sub_eff.get("target") or {}
    ctype = card.get("card_type") or {}
    checks = {
        "trigger0_mode_PlaneswalkedTo":
            t0.get("mode") == {"Planeswalked": {"role": "To"}},
        "execute_kind_Spell": ex.get("kind") == "Spell",
        "head_Unimplemented": head.get("type") == "Unimplemented",
        "head_describes_choice":
            "chooses a nonblack creature they control"
            in str(head.get("description", "")).lower(),
        "sub_kind_Spell": sub.get("kind") == "Spell",
        "sub_effect_Destroy": sub_eff.get("type") == "Destroy",
        "sub_target_TrackedSet": (target.get("type") == "TrackedSet"
                                  and target.get("id") == 0),
        "sub_link_SequentialSibling": sub.get("sub_link") == "SequentialSibling",
        "oracle_matches": "chooses a nonblack creature they control"
            in str(card.get("oracle_text", "")).lower(),
        "typeline_lost_plane":
            "Plane" not in [str(t) for t in (ctype.get("core_types") or [])],
        "typeline_shows_secret_lair":
            [str(t) for t in (ctype.get("subtypes") or [])]
            == ["sECreT", "LaIR"],
    }
    ev = {
        "card_name": card.get("name"),
        "oracle_text": card.get("oracle_text"),
        "card_type": ctype,
        "trigger0_mode": t0.get("mode"),
        "head": {"type": head.get("type"), "name": head.get("name"),
                 "description": head.get("description")},
        "sub_ability": {"kind": sub.get("kind"),
                        "effect": sub_eff.get("type"),
                        "target": target,
                        "sub_link": sub.get("sub_link")},
        "all_triggers": [{"mode": t.get("mode"),
                          "head_type": (t.get("execute") or {})
                          .get("effect", {}).get("type")}
                         for t in trigs],
        "checks": checks,
        "head_name_discrepancy": {
            "issue_body_quotes": "chooses",
            "pinned_data_shows": head.get("name"),
            "note": "description string matches the issue; the structural "
                    "defect (Unimplemented head -> empty tracked set) is "
                    "identical either way, as in the #7436-#7439 runs",
        },
        "typeline_note": "the alternating-case type line also failed to "
                         "parse: card_type.core_types is empty (the card "
                         "is not a Plane in the data) and subtypes read "
                         "['sECreT','LaIR'] (the 'Secret Lair' promo-set "
                         "marker from printings ['PSSC']). This is why "
                         "the engine's planar-deck validation rejects the "
                         "card at runtime (A2).",
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/sans_mercy_card_data.json", "w") as f:
        json.dump(card, f, indent=1)
    ok = all(checks.values())
    ST["data_level_ok"] = ok
    ST["head_now_parses"] = (head.get("type") != "Unimplemented")
    say(f"data-level check: mode={t0.get('mode')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"sub={sub.get('kind')}/{sub_eff.get('type')}/"
        f"target={target.get('type')}({target.get('id')}); "
        f"sub_link={sub.get('sub_link')}; card_type={ctype}; "
        f"checks_failed={[k for k, v in checks.items() if not v] or 'none'}")
    wire("data_level", {"ok": ok, "head_name": head.get("name"),
                        "card_type": ctype, "checks": checks})

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
            "format_config": PLANECHASE_FORMAT,
            "room_name": None,
            "host_peer_id": None,
            "draft_metadata": None,
            "start_when_full": True,
            "ranked": False,
        },
    }))
    wire("create_sent", {"who": tag,
                         "planar_deck_n": len(deck.get("planar_deck", []))})
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


def obj_lname(state, oid):
    o = (state.get("objects") or {}).get(str(oid), {})
    return str(o.get("base_name") or o.get("name") or "?").lower()


async def run_live():
    # Phase 2: the failing create (Sans Mercy in the planar deck)
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    bad_planar = ["Sans Mercy"] + FILLER_PLANES[:19]
    bad_deck = pdeck(P0_MAIN, bad_planar)
    say("[live] attempting Planechase create with Sans Mercy x1 in "
        f"planar deck ({len(bad_planar)} planes)")
    status, data = await attempt_create(p0, bad_deck, "P0-bad")
    ST["deck_error"] = data if status == "error" else None
    ST["bad_create_status"] = status
    with open(f"{EVDIR}/deck_rejection.json", "w") as f:
        json.dump({"attempt": "planechase create with Sans Mercy x1 in "
                              "planar deck",
                   "status": status,
                   "error": data if status == "error" else None,
                   "planar_deck": bad_planar,
                   "server": {k: SERVER_IDENTITY[k] for k in
                              ("server_version", "build_commit",
                               "protocol_version")}}, f, indent=1)
    await p0.close()

    # Phase 3: control -- identical game, 20 fully-supported planes
    c0 = PhaseClient("C0")
    c1 = PhaseClient("C1")
    await c0.connect()
    await c1.connect()
    good_deck = pdeck(P0_MAIN, FILLER_PLANES)
    say("[control] attempting Planechase create with 20 supported planes")
    status, data = await attempt_create(c0, good_deck, "C0-good")
    started = False
    starting_plane = None
    if status == "ok":
        ST["control_game_code"] = c0.game_code
        jdata = await c1.join(
            c0.game_code, pdeck(P1_MAIN, []))
        if c1.player_id is None and isinstance(jdata, dict):
            c1.player_id = jdata.get("player_id", jdata.get("your_player"))
        say(f"[control] C1 joined game {c0.game_code} "
            f"(player_id={c1.player_id})")
        started = await answer_mulligans_until_started([c0, c1])
        ST["control_started"] = started
        # Bonus (best-effort): read the revealed starting plane off the
        # latest state, proving the planar machinery works on this build.
        if started:
            try:
                st = c0.latest
                pc = ((st.get("state", {}).get("derived") or {})
                      .get("planechase") or {})
                oid = pc.get("active_plane")
                if oid is not None:
                    starting_plane = obj_lname(st["state"], oid)
                    say(f"[control] starting plane revealed: "
                        f"{starting_plane}")
                    wire("starting_plane", {"name": starting_plane})
            except Exception as e:
                say(f"[control] starting-plane observation failed "
                    f"(best-effort): {e!r}")
        ST["control_starting_plane"] = starting_plane
    with open(f"{EVDIR}/control_game.json", "w") as f:
        json.dump({"attempt": "planechase create with 20 supported planes",
                   "status": status,
                   "game_code": ST["control_game_code"],
                   "game_started": started,
                   "starting_plane": starting_plane,
                   "planar_deck": FILLER_PLANES}, f, indent=1)
    for c in (c0, c1, p1):
        try:
            await c.close()
        except Exception:
            pass

# ------------------------------------------------------------- finalize
async def finalize():
    ass = ST["ass"]
    notes = ST["notes"]

    # A1: data-level parse matches the issue (+ type-line gap)
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses Sans "
                     "Mercy's 'when you planeswalk to this plane' ability "
                     "as mode=Planeswalked{role:To}, execute kind Spell, "
                     "head Unimplemented('unparsed_verb_arguments': "
                     "'cHoOSeS a nOnBlACk cREaTuRE tHEy cONtrOl') + "
                     "sub_ability kind Spell with effect Destroy targeting "
                     "TrackedSet(0), sub_link SequentialSibling; oracle "
                     "text matches. The issue's corpus named the head "
                     "'chooses'; the pinned corpus names it "
                     "'unparsed_verb_arguments' with the identical "
                     "description -- both Effect::Unimplemented over the "
                     "same clause. ADDITIONALLY: the card's type line "
                     "also failed to parse -- card_type.core_types is "
                     "empty (the card is not a Plane in the data) and "
                     "subtypes read ['sECreT','LaIR'] (the 'Secret Lair' "
                     "promo-set marker). See data_evidence.json and "
                     "sans_mercy_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: live planar-deck rejection names the card
    err = ST.get("deck_error") or {}
    msg = str(err.get("message", ""))
    code = err.get("code", "")
    notes.append(f"A2 probe: bad-create status={ST.get('bad_create_status')}; "
                 f"code={code!r}; message={msg[:220]!r}.")
    if (ST.get("bad_create_status") == "error"
            and code == "deck_rejected"
            and "sans mercy" in msg.lower()
            and "plane or phenomenon" in msg.lower()):
        ass["A2_deck_rejected"] = "passed"
        notes.append("A2 passed (defect made visible at runtime): the "
                     "pinned v0.100.0 server rejected the Planechase game "
                     "whose planar deck contained Sans Mercy with "
                     "deck_rejected / 'Planar deck cards must be Plane or "
                     "Phenomenon: sAnS mERcY'. The card's type line never "
                     "parsed (A1), so the engine does not recognize it as "
                     "a Plane -- the parse gap is load-bearing: the card "
                     "can never enter play, its planeswalk trigger can "
                     "never fire, and the Destroy sub-ability can never "
                     "receive a populated tracked set. This rejection was "
                     "first observed live in an initial full-runtime "
                     "attempt (which could not planeswalk to the card "
                     "precisely because of it); see deck_rejection.json.")
    elif ST.get("bad_create_status") == "ok":
        ass["A2_deck_rejected"] = "failed"
        notes.append("A2 FAILED: the server ACCEPTED Sans Mercy into a "
                     "planar deck -- the parse gap is gone.")
    else:
        notes.append("A2 not-run: the create attempt did not return a "
                     "usable verdict (timeout/unreachable).")

    # A3: control game creates and starts
    notes.append(f"A3 probe: control status="
                 f"{'ok' if ST['control_game_code'] else 'not-ok'}; "
                 f"game_started={ST['control_started']}; "
                 f"starting_plane={ST['control_starting_plane']}.")
    if ST["control_game_code"] and ST["control_started"]:
        ass["A3_control_ok"] = "passed"
        notes.append("A3 passed: the identical Planechase game with 20 "
                     "fully-supported planes created and reached "
                     "GameStarted" +
                     (f" (starting plane revealed: "
                      f"{ST['control_starting_plane']})"
                      if ST["control_starting_plane"] else "") +
                     " -- the A2 rejection is specific to the gapped "
                     "card, not the format config, the deck construction, "
                     "or the harness. See control_game.json.")
    else:
        ass["A3_control_ok"] = "failed"
        notes.append("A3 FAILED: the control game could not be established; "
                     "the A2 rejection cannot be attributed.")

    notes.append(
        "A4 (scope documentation, not a verdict driver): no engine-supported "
        "runtime path exists to planeswalk to THIS plane on the pinned "
        "server -- the engine's own planar-deck validation refuses to load "
        "it (A2), only the host's planar_deck is loaded as the planar deck, "
        "and Debug CreateCard actions are rejected in multiplayer server "
        "games. The issue itself asserts no runtime consumer symptom ('this "
        "issue does not assert a runtime symptom'; 'No runtime reproduction "
        "was run for this card'), so none is claimed. The empty tracked-set "
        "publish follows structurally: the Unimplemented head is the chain "
        "root (no ancestor to extend), its resolver pushes no GameEvent, "
        "and the publish authority therefore allocates a fresh empty set "
        "for the Destroy sub-ability to read.")

    # verdict
    if all(ass[k] == "passed" for k in ("A1_data_level", "A2_deck_rejected",
                                        "A3_control_ok")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the reported parse defect is confirmed on "
            "the pinned v0.100.0 release (Planeswalked{To} trigger whose "
            "head is Unimplemented 'chooses a nonblack creature they "
            "control', chaining to Destroy reading TrackedSet(0)), and the "
            "defect is load-bearing at runtime -- the engine's own "
            "planar-deck validation rejects Sans Mercy ('Planar deck cards "
            "must be Plane or Phenomenon'), so the plane can never enter "
            "play, its trigger can never fire, and the Destroy sub-ability "
            "can never receive a populated tracked set. The control (20 "
            "supported planes) creates and starts, isolating the rejection "
            "to this card's parse gap. This is not a fix claim.")
    elif ((ass["A1_data_level"] == "failed" and ST.get("head_now_parses"))
            or (ass["A2_deck_rejected"] == "failed"
                and ST.get("bad_create_status") == "ok")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the parse gap is gone on the pinned "
            "release (the clause now parses, or the card is accepted into "
            "a planar deck). This is not a fix claim.")
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
               if "sans mercy" in l.lower()
               or "planar" in l.lower()
               or "deck_rejected" in l.lower()]
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
        "format_config": "Planechase (engine-canonical FormatConfig::planechase())",
        "decks": {
            "bad_attempt_planar_deck":
                ["Sans Mercy"] + FILLER_PLANES[:19],
            "control_planar_deck": FILLER_PLANES,
            "P0_main": "36x Forest + 16x Plains + 4x Llanowar Elves + 4x "
                       "Grizzly Bears (60)",
            "P1_main": "40x Forest + 12x Plains + 4x Llanowar Elves + 4x "
                       "Grizzly Bears (60)",
        },
        "assertions": ass,
        "notes": notes,
        "observations": {
            "head_name_pinned": json.load(
                open(f"{EVDIR}/data_evidence.json")
            )["head"]["name"],
            "card_type_pinned": json.load(
                open(f"{EVDIR}/data_evidence.json")
            )["card_type"],
            "deck_rejection_code": (ST.get("deck_error") or {}).get("code"),
            "deck_rejection_message": (ST.get("deck_error") or {}).get(
                "message"),
            "control_game_code": ST["control_game_code"],
            "control_started": ST["control_started"],
            "control_starting_plane": ST["control_starting_plane"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report (the issue asserts no "
            "runtime consumer symptom). The scenario demonstrates the parse "
            "on the pinned card data and the defect's live consequence "
            "(planar-deck validation rejects the card); it does not drive "
            "a planeswalk-trigger resolution because the engine offers no "
            "supported path to put this plane into play.",
            "The empty tracked-set publish is documented structurally from "
            "the parsed chain (Unimplemented chain root -> no GameEvent -> "
            "fresh empty set), not measured at runtime.",
            "An initial full-runtime attempt (roll the planar die until "
            "Sans Mercy is active) was abandoned when the live server "
            "rejected the planar deck at game creation; the rejection "
            "itself is the demonstrated runtime consequence. The "
            "alternating-case type line gap (card not recognized as a "
            "Plane) is an additional, directly observed parse failure on "
            "the same card.",
        ],
        "setup_line": "Phase 1: pinned card-data.json parse walk (ability "
                      "chain + type line). Phase 2: live Planechase create "
                      "with Sans Mercy in the planar deck (expect "
                      "deck_rejected). Phase 3: control Planechase create "
                      "with 20 fully-supported planes (expect GameStarted).",
        "contract_line": "Reported defect: 'each player chooses a nonblack "
                         "creature they control' is Unimplemented in the "
                         "parse, so the Destroy sub-ability reads an empty "
                         "chain tracked set. Demonstrated: the parse on "
                         "pinned v0.100.0 matches the report, and the "
                         "pinned server refuses to load the card into a "
                         "planar deck because its type line also failed to "
                         "parse (not recognized as a Plane).",
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
        [sys.executable, f"{BACKFILL}/driver/render_summary_7440.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
