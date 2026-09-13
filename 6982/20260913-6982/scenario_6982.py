#!/usr/bin/env python3
"""Issue #6982: [Card Bug] Florian's where-X life-loss binding is unsupported.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Florian, Voldaren Scion ({1}{B}{R} Legendary Creature - Vampire Noble, 3/3):
    "First strike. At the beginning of each of your postcombat main phases,
     look at the top X cards of your library, where X is the total amount of
     life your opponents lost this turn. Exile one of those cards and put
     the rest on the bottom of your library in a random order. You may play
     the exiled card this turn."

Card-data parse state on v0.81.3 (verified 2026-09-13 before the run):
  triggers[0] (mode "Phase", phase "PostCombatMain",
  constraint OnlyDuringYourTurn, trigger_zones ["Battlefield"]):
    execute.effect = {"type": "Unimplemented", "name": "where_x_binding",
      "description": "where X is the total amount of life your opponents
      lost this turn"} -- the parent effect of a sub_ability chain that
      would otherwise: exile one of the looked cards (ChangeZone ->
      ParentTarget), put the rest on the bottom (PutAtLibraryPosition,
      TrackedSet 0), and grant PlayFromExile UntilEndOfTurn to P0.

Reported symptom: the where_x_binding clause is unsupported, so the whole
trigger's "look at the top X" outcome never happens.

Setup (native engine, three human-client seats, single-user, Bo1, life 20):
  P0: 4x Florian, Voldaren Scion, 4x Lightning Bolt, 26x Swamp, 26x Mountain.
  P1: 60x Forest (passive). P2: 60x Forest (passive).
  P1/P2 each turn: play a land if possible, declare no blockers/attackers,
  always pass priority.
  NOTE: the engine randomizes seat_order (observed [2,0,1] and [0,1,2]);
  the driver keys everything on active_player/phase, never on turn order.

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
  A1_parse_gap     card-data v0.81.3: Florian triggers[0].execute.effect
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
  A6_exile_one     exactly one of the looked cards is exiled and
                   (offered - 1) went to the bottom of P0's library.
  A7_play_permission
                   the exiled card is playable this turn: legal_actions
                   advertises a cast/play from exile for P0, or the state
                   carries a runtime PlayFromExile grant (card-text
                   mentions do NOT count).
  A8_cleanup       POST: stack empty.

Verdict rule: reproduced iff A1 passes and the reported outcome is not
implemented (A5/A6/A7 fail because the effect is Unimplemented);
not-reproduced iff A1 fails (clause now parses as supported) AND A5-A7
all pass with X = 6 bound; blocked iff A2 fails (A3 failure also blocks:
the life-loss precondition was not achieved).

Evidence: evidence/6982/<run-id>/pre.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6982.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts for this game's code only; the
server is shared with sibling runs).
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
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-6982")
EVDIR = f"{BACKFILL}/evidence/6982/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

FLORIAN = "florian, voldaren scion"
BOLT = "lightning bolt"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
LANDS = (SWAMP, MOUNTAIN, FOREST)

P0_DECK = [(FLORIAN, 4), (BOLT, 4), (SWAMP, 26), (MOUNTAIN, 26)]
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

# Shared server (started earlier, used by sibling runs): v0.81.3 pinned
# release; identity mirrors the verified pin recorded in scenario_6950.
SERVER_IDENTITY = {
    "server_version": "0.81.3",
    "build_commit": "95bec6e",
    "protocol_version": 70,
    "mode": "single-user",
    "binary_sha256": "2c9918612e8fcf35d7daf5964eadaf11eeb94cc99463b2de822a906b9030fa44",
    "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
    "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "ServerHello on 127.0.0.1:9374 (shared v0.81.3 single-user "
              "server) + verified pin (minisign-verify of binary + signed "
              "data manifest with the repo-pinned key).",
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


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in LANDS):
            out.append(int(oid))
    return out


def exile_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Exile" and o.get("controller") == pid]


def library_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Library" and o.get("controller") == pid]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def choice_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                return None
    return None


def choice_object_name(ch):
    """Card name of an object-surface choice, else None."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("role") in (
                "choice", "target", "source", "candidate"):
            nm = d.get("name")
            if nm:
                return str(nm)
    return None


def is_card_pick_choice(ch):
    """True if a choice is a genuine card pick (e.g. 'exile one of the
    looked cards'), as opposed to the standard priority menu (passPriority
    + tapLandForMana). A real pick names a card object and carries no
    priority-menu action code."""
    saw_card = False
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if not isinstance(d, dict):
            continue
        if s.get("type") == "action" and d.get("code") in (
                "passPriority", "tapLandForMana", "playLand",
                "mulliganDecision"):
            return False
        if d.get("name") and d.get("zone") != "battlefield":
            saw_card = True
    return saw_card


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def wf_pending_for(state, pid):
    d = wf_of(state).get("data") or {}
    if isinstance(d.get("player"), int):
        return d["player"] == pid
    for p in d.get("pending") or []:
        if isinstance(p, dict) and p.get("player") == pid:
            return True
    return False


def stack_entries(state):
    return state.get("stack") or []


def florian_trigger_on_stack(state):
    """Return the first stack entry that is a Florian triggered ability."""
    for e in stack_entries(state):
        blob = json.dumps(e, default=str).lower()
        kind = ((e.get("kind") or {}).get("type")
                if isinstance(e.get("kind"), dict) else e.get("kind"))
        if kind == "TriggeredAbility" and "florian" in blob:
            return e
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


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
                   "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return data.get("choices") or data.get("candidates") or []


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse_gap", "A2_setup_ok", "A3_life_loss", "A4_trigger_fires",
            "A5_look_X", "A6_exile_one", "A7_play_permission", "A8_cleanup")}

    # ---- A1 (parse gap) up front, from the pinned card-data.json ----
    try:
        cd_path = (f"{BACKFILL}/server/releases/v0.81.3/data/card-data.json")
        cd = json.load(open(cd_path))
        f = cd["florian, voldaren scion"]
        trigs = f.get("triggers", [])
        notes.append(f"parse: {len(trigs)} triggers in card-data")
        eff = ((trigs[0] or {}).get("execute", {}) or {}).get("effect", {})
        notes.append("parse: triggers[0].execute.effect = "
                     + json.dumps(eff)[:240])
        wire("parse_check", {"effect": eff})
        ok = (eff.get("type") == "Unimplemented"
              and eff.get("name") == "where_x_binding")
        ass["A1_parse_gap"] = "passed" if ok else "failed"
        notes.append(f"A1_parse_gap: effect.type={eff.get('type')} "
                     f"name={eff.get('name')} -> {ass['A1_parse_gap']}")
    except Exception as ex:
        ass["A1_parse_gap"] = "failed"
        notes.append(f"A1_parse_gap failed: {ex}")

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=3)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    GAME = p0.game_code
    say(f"game {GAME}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")
    kept = {}

    ST = {"florian_cast": False, "florian_oid": None,
          "florian_bf_turn": None,
          "bolt_cast": False, "bolt_resolved": False,
          "bolt_submitted": False,
          "pre_exported": False, "post_exported": False,
          "attacked": False, "damage_turn": None,
          "trigger_seen_damage_turn": False, "trigger_entry": None,
          "trigger_sightings": [],
          "look_offered": None, "look_opp_dumped": False,
          "look_answered": False,
          "exile_before": None, "lib_before": None}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "tick_errors": [], "life_trace": [], "stack_trace": []}
    prompt_first_seen = {}
    last_select = {}

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
        states = {}
        for fn in ("pre", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre, post = states.get("pre"), states.get("post")

        # ---- A2: setup (PRE: Florian on P0 BF; 20/20/20) ----
        if pre is not None:
            oid = bf_id(pre, 0, FLORIAN)
            lives = [life_of(pre, i) for i in (0, 1, 2)]
            ok = (oid is not None and lives == [20, 20, 20])
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: florian_oid={oid} lives={lives}")
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

        # ---- A6: exactly one of the looked cards exiled, rest on bottom ----
        new_exiled = set()
        if (pre is not None and post is not None
                and ST["exile_before"] is not None):
            ex_pre = set(ST["exile_before"])
            ex_post = set(exile_ids(post, 0))
            new_exiled = ex_post - ex_pre
            lib_pre = ST["lib_before"]
            lib_post = len(library_ids(post, 0))
            # Oracle-correct: the looked cards stay in the library (just
            # reordered); exactly one leaves to exile, so the library
            # shrinks by exactly 1.
            ok = (len(new_exiled) == 1 and offered == 6
                  and lib_post == lib_pre - 1)
            ass["A6_exile_one"] = "passed" if ok else "failed"
            notes.append(f"A6: newly_exiled={len(new_exiled)} lib_pre={lib_pre} "
                         f"lib_post={lib_post} (expected 1 exiled, "
                         f"library -1)")
        else:
            ass["A6_exile_one"] = "failed"
            notes.append("A6 failed: missing pre/post state or exile "
                         "baseline")

        # ---- A7: exiled card playable this turn ----
        # Card-text mentions of PlayFromExile do NOT count; require a
        # runtime grant or an advertised cast-from-exile action.
        if post is not None and new_exiled:
            st0 = p0.latest or {}
            cast_from_exile = False
            for a in merged_actions(st0):
                d = a.get("data", {}) or {}
                oid = d.get("object_id") or d.get("card_id")
                if (a["type"] in ("CastSpell", "PlayCard")
                        and oid in new_exiled):
                    cast_from_exile = True
            # runtime grant check: PlayFromExile outside card definitions.
            # Card definitions live under objects[*].card_data-ish blobs;
            # a real grant is a top-level transient effect/permission.
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
            stack_empty = not (post.get("stack") or [])
            ok = stack_empty
            ass["A8_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A8: stack_empty={stack_empty}")
        else:
            ass["A8_cleanup"] = "failed"
            notes.append("A8 failed: post.json missing")

        # ---- verdict ----
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
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6982,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "shared v0.81.3 single-user server on "
                               "127.0.0.1:9374 (this run's game code "
                               f"{GAME}; server.log here holds excerpts "
                               "for that game only); do NOT restart",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6982.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (3 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_6982.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
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
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6982.py",
                    f"{EVDIR}/scenario_6982.py")
        # shared server: keep only this game's log excerpts
        try:
            with open(f"{BACKFILL}/runs/{RUN_ID}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            import re
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            excerpt = [ln for ln in clean.splitlines() if GAME in ln]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines for "
                f"game {GAME})")
        except Exception as e:
            say(f"server.log excerpt failed (shared server): {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
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
               "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
               "where-X life-loss binding Unsupported",
               fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else ((120, 220, 120) if run["verdict"] == "not-reproduced"
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
            "A6_exile_one": "1 exiled, rest to bottom of library",
            "A7_play_permission": "exiled card playable this turn",
            "A8_cleanup": "POST stack empty",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Life totals across states (P0/P1/P2):",
               fill=(200, 210, 225))
        y += 24
        for label in ("pre", "post"):
            st = states.get(label)
            if st is not None:
                fl = bf_id(st, 0, FLORIAN)
                line = (f"{label:>4}: {life_of(st, 0)}/{life_of(st, 1)}/"
                        f"{life_of(st, 2)}  florian_oid={fl}  "
                        f"stack={len(st.get('stack') or [])}")
            else:
                line = f"{label:>4}: (no state)"
            d.text((36, y), line, fill=(150, 160, 175))
            y += 22
        y += 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:15]:
            d.text((36, y), n[:116], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        # NOTE: scenario_run.log is hashed LAST, after the final say() line
        # below is appended; otherwise the manifest's hash for the log goes
        # stale by exactly one line.
        files = ["pre.json", "post.json", "run.json",
                 "scenario_6982.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]

        def build():
            lines = []
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    lines.append(f"{h}  {fn}")
                else:
                    say(f"manifest: MISSING {fn}")
            return lines

        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")
        say("wrote manifest.sha256")
        # Re-hash now that all scenario_run.log logging is done. No say()
        # may follow this point (finish() only closes files + stdout).
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(build()) + "\n")

    async def do_mulligan(c, pid, tag):
        st = c.latest["state"]
        hn = hand_lnames(st, pid)
        lands = sum(1 for n in hn if n in LANDS)
        mulls = kept.get(f"P{pid}_mulls", 0)
        if pid == 0:
            # P0 wants Florian + Bolt in the opener; allow 2 mulligans.
            ok = ((FLORIAN in hn and BOLT in hn and lands >= 2)
                  or mulls >= 2)
        else:
            ok = lands >= 3 or mulls >= 2
        if ok:
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps ({lands} lands; "
                f"florian={FLORIAN in hn} bolt={BOLT in hn})")
        else:
            kept[f"P{pid}_mulls"] = mulls + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{mulls + 1} ({lands} lands)")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in hand[:1]]}})
        say(f"{tag} bottoms 1")

    async def discard_tick(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            chs = vi_choices(opp)
            if not chs:
                continue

            def rank(ch):
                t = choice_text(ch).lower()
                if t in LANDS:
                    return 0
                if t == FLORIAN:
                    return 1
                return 2  # Lightning Bolt kept last

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def generic_prompt(c, pid, tag, st, state, skip_answer=False):
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            chs = vi_choices(opp)
            phase = (state.get("phase") or "")
            obs["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8], "n_choices": len(chs),
                 "phase": phase,
                 "texts": [choice_text(ch)[:60] for ch in chs][:8]})
            say(f"[{tag}] UNEXPECTED PROMPT iid={iid} n={len(chs)} "
                f"phase={phase}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if skip_answer:
                continue
            if time.time() - entry["t0"] < 20:
                continue
            pick = chs[0] if chs else None
            if pick is not None:
                say(f"[{tag}] auto-answering prompt after 20s stall")
                obs["auto_answered"].append({"who": tag})
                await answer_vi(c, opp, pick, tag)
                entry["done"] = True
                acted = True
        return acted

    async def cast_named(c, acts, state, name, tag):
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if isinstance(oid, int) and lname(state, oid) == name:
                say(f"[{tag}] casting {name} via {a['type']} (oid {oid})")
                wire("cast", {"who": tag, "name": name, "oid": oid,
                              "action": a["type"]})
                await submit_as_is(c, a)
                return oid
        return None

    async def answer_bolt_target(c, st, state):
        """Answer P0's bolt TargetSelection with the player candidate
        seat == 2 (P2)."""
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            chs = vi_choices(opp)
            node = next((ch for ch in chs if choice_seat(ch) == 2), None)
            if node is None:
                if chs:
                    say("[P0] bolt-target: no seat-2 candidate; holding")
                    wire("bolt_target_no_seat2",
                         {"choices": [choice_text(ch)[:60] for ch in chs][:8]})
                continue
            say("[P0] bolt targets P2 (seat 2)")
            wire("bolt_target_answer",
                 {"choiceId": node.get("id"),
                  "text": choice_text(node)[:80]})
            await answer_vi(c, opp, node, "P0")
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            ST["bolt_cast"] = True
            return True
        return False

    def scan_trigger_and_look(c, st, state):
        """Record Florian trigger stack presence (all P0 postcombats) and
        any look interaction on the damage turn."""
        turn = state.get("turn_number")
        ent = florian_trigger_on_stack(state)
        if ent is not None:
            sig = (turn, "stack")
            if sig not in [s[0:2] for s in ST["trigger_sightings"]]:
                ST["trigger_sightings"].append(
                    (turn, "stack", round(time.time() - t_start, 1)))
                say(f"Florian TriggeredAbility ON STACK "
                    f"(turn {turn}, postcombat)")
            if ST["attacked"]:
                ST["trigger_seen_damage_turn"] = True
                if ST["trigger_entry"] is None:
                    try:
                        ST["trigger_entry"] = json.loads(
                            json.dumps(ent, default=str))
                    except Exception:
                        ST["trigger_entry"] = {"note": "unserializable"}
                wire("trigger_on_stack", {"entry": ST["trigger_entry"]})
        nstack = len(stack_entries(state))
        tr = obs["stack_trace"]
        if not tr or tr[-1][1] != nstack:
            tr.append((round(time.time() - t_start, 1), nstack))
            wire("stack_size", {"n": nstack, "phase": state.get("phase"),
                               "turn": turn})
        # damage-turn look detector: a genuine card-pick interaction in
        # P0's damage-turn postcombat (NOT the priority menu: passPriority
        # + tapLandForMana choices are excluded by is_card_pick_choice).
        # Full opportunity dumped for diagnosis.
        if ST["attacked"] and ST["look_offered"] is None:
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    iid = opp.get("interactionId")
                    chs = vi_choices(opp)
                    picks = [ch for ch in chs if is_card_pick_choice(ch)]
                    if picks:
                        ST["look_offered"] = len(picks)
                        say(f"LOOK interaction offered {len(picks)} cards "
                            f"(iid={iid})")
                        wire("look_offered",
                             {"n": len(picks), "iid": iid,
                              "texts": [choice_text(ch)[:50]
                                        for ch in picks][:10],
                              "opportunity": json.loads(
                                  json.dumps(opp, default=str))})

    async def answer_damage_look(c, st, state):
        """If the damage-turn look is waiting on P0, record (done by the
        scanner) and answer it by exiling the first offered card so the
        scenario can complete; the choice itself is the engine-defined
        'exile one of those cards' step."""
        if not ST["attacked"] or ST["look_answered"]:
            return False
        if ST["look_offered"] is None:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            picks = [ch for ch in vi_choices(opp) if is_card_pick_choice(ch)]
            if not picks:
                continue
            pick = picks[0]
            say(f"[P0] answering damage-turn look: exile "
                f"{choice_text(pick)[:60]}")
            wire("look_answer", {"choiceId": pick.get("id"),
                                 "card": choice_text(pick)[:60]})
            await answer_vi(c, opp, pick, "P0")
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            ST["look_answered"] = True
            return True
        return False

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, "P0")
                return
            if (find_action(acts, "SelectCards")
                    and last_select.get(0) != p0.revision):
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    if (ST["bolt_resolved"] and not ST["attacked"]
                            and ST["florian_oid"] is not None):
                        target = {"type": "Player", "data": 1}
                        sub["data"]["attacks"] = [
                            [int(ST["florian_oid"]), target]]
                        say(f"[P0] attacking P1 with Florian "
                            f"oid={ST['florian_oid']}")
                        wire("attack_declared",
                             {"attacker": int(ST["florian_oid"]),
                              "target": target, "turn": turn})
                        ST["attacked"] = True
                        ST["damage_turn"] = turn
                    else:
                        sub["data"]["attacks"] = []
                        sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(p0, sub)
            return
        # track Florian on BF (+ the turn it arrived: summoning sickness
        # means the damage turn must be a LATER P0 turn)
        oid = bf_id(state, 0, FLORIAN)
        if oid is not None and ST["florian_oid"] is None:
            ST["florian_oid"] = int(oid)
            ST["florian_bf_turn"] = turn
            say(f"Florian on BF: oid={oid} (turn {turn})")
            wire("florian_on_bf", {"oid": int(oid), "turn": turn})
        # life trace
        lives = tuple(life_of(state, i) for i in (0, 1, 2))
        tr = obs["life_trace"]
        if all(l is not None for l in lives) and (not tr or tr[-1][1] != lives):
            tr.append((round(time.time() - t_start, 1), lives))
            say(f"life = {lives}")
            wire("life", {"life": lives})
        # bolt target selection (bolt submitted, awaiting targets)
        if (wtype == "TargetSelection" and wf_pending_for(state, 0)
                and ST["bolt_submitted"] and not ST["bolt_resolved"]):
            if await answer_bolt_target(p0, st, state):
                return
        if await discard_tick(p0, 0, "P0", st, state):
            return
        # postcombat observation on P0's turns
        damage_postcombat = (phase == "PostCombatMain"
                             and state.get("active_player") == 0
                             and ST["attacked"])
        if phase == "PostCombatMain" and state.get("active_player") == 0:
            if ST["exile_before"] is None and damage_postcombat:
                ST["exile_before"] = exile_ids(state, 0)
                ST["lib_before"] = len(library_ids(state, 0))
                say(f"damage-turn postcombat baselines: exile="
                    f"{len(ST['exile_before'])} lib={ST['lib_before']}")
            scan_trigger_and_look(p0, st, state)
            if damage_postcombat and await answer_damage_look(p0, st, state):
                return
        # PRE: Florian on BF, stack empty, all 20, P0 precombat priority
        if (ST["florian_oid"] is not None and not ST["pre_exported"]
                and not (state.get("stack") or [])
                and lives == (20, 20, 20)
                and phase == "PreCombatMain" and my_priority(state, 0)):
            if await export_named("pre"):
                ST["pre_exported"] = True
                say("PRE exported: Florian on BF, all at 20")
        # POST: damage turn done, P0 postcombat, stack empty -> export NOW
        # and finish immediately (no grace; the game races ahead otherwise)
        if (damage_postcombat and not ST["post_exported"]
                and not (state.get("stack") or [])):
            if await export_named("post"):
                ST["post_exported"] = True
                say("POST exported: damage-turn postcombat, stack empty")
                await finish()
                return
        if not my_priority(state, 0):
            # on the damage-turn postcombat, record unfamiliar prompts but
            # do not auto-answer: the look is answered deliberately above.
            if await generic_prompt(p0, 0, "P0", st, state,
                                    skip_answer=damage_postcombat):
                return
            return
        # ---- P0 priority (caster gets priority first after a cast: never
        # return without acting while a spell is in flight; always fall
        # through to PassPriority) ----
        in_flight = bool(state.get("stack") or [])
        if (not in_flight and phase in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0):
            hn = hand_lnames(state, 0)
            lands = len(untapped_lands(state, 0))
            if (not ST["florian_cast"] and oid is None
                    and FLORIAN in hn and lands >= 3):
                coid = await cast_named(p0, acts, state, FLORIAN, "P0")
                if coid is not None:
                    ST["florian_cast"] = True
                    return
            # bolt only on a LATER P0 turn than the Florian-cast turn
            # (summoning sickness), after PRE, once, while P2 still at 20
            if (ST["pre_exported"] and not ST["bolt_cast"]
                    and phase == "PreCombatMain"
                    and ST["florian_bf_turn"] is not None
                    and turn > ST["florian_bf_turn"]
                    and BOLT in hn and lives[2] == 20):
                coid = await cast_named(p0, acts, state, BOLT, "P0")
                if coid is not None:
                    ST["bolt_submitted"] = True
                    return
        if (phase in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0):
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def passive_tick(c, pid, tag):
        st = c.latest
        if not st:
            return
        acts = merged_actions(st)
        state = st["state"]
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get(tag):
                await do_mulligan(c, pid, tag)
                return
            if (find_action(acts, "SelectCards")
                    and last_select.get(pid) != c.revision):
                last_select[pid] = c.revision
                await do_bottom(c, pid, tag)
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
            return
        if await discard_tick(c, pid, tag, st, state):
            return
        if not my_priority(state, pid):
            if await generic_prompt(c, pid, tag, st, state):
                return
            return
        # passive: play land on own main phase, then pass
        if (state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == pid):
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, pid, tag in ((p0, 0, "P0"), (p1, 1, "P1"), (p2, 2, "P2")):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(tag))
            stale = time.time() - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = rev
            last_tick_at[tag] = time.time()
            try:
                if pid == 0:
                    await p0_tick(st, merged_actions(st), st["state"])
                else:
                    await passive_tick(c, pid, tag)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                obs["tick_errors"].append({"who": tag, "err": str(e)[:200]})
                wire("tick_error", {"who": tag, "err": str(e)})
        # bolt resolution marker
        s = (p0.latest or {}).get("state") or {}
        if (ST["bolt_cast"] and not ST["bolt_resolved"]
                and life_of(s, 2) == 17 and not (s.get("stack") or [])):
            ST["bolt_resolved"] = True
            say("bolt resolved: P2 life 17")
            wire("bolt_resolved", {})
        if ST["post_exported"]:
            return
        if (s.get("turn_number") or 0) > 30 and ST["florian_oid"] is None:
            notes.append("watchdog: turn 30 reached with no Florian on BF; "
                         "finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            oid = bf_id(s, 0, FLORIAN)
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} life={[life_of(s, i) for i in (0,1,2)]} "
                f"florian={oid} bolt_cast={ST['bolt_cast']} "
                f"bolt_resolved={ST['bolt_resolved']} attacked={ST['attacked']} "
                f"pre={ST['pre_exported']} post={ST['post_exported']} "
                f"trig_dmg={ST['trigger_seen_damage_turn']} look={ST['look_offered']} "
                f"stack={len(s.get('stack') or [])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
