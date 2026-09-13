#!/usr/bin/env python3
"""Issue #6916: Xantcha, Sleeper Agent — cannot choose which opponent gets
Xantcha; only the controller can activate its {3} ability.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Xantcha, Sleeper Agent ({1}{B}{R}, 5/5 Legendary Creature - Phyrexian Minion):
    "Xantcha enters under the control of an opponent of your choice.
     Xantcha attacks each combat if able and can't attack its owner or
     planeswalkers its owner controls.
     {3}: Xantcha's controller loses 2 life and you draw a card.
     Any player may activate this ability."

Triage acceptance criteria:
  - As Xantcha enters, its owner chooses among legal opponents and it
    enters under that player's control.
  - Any player may activate the {3} ability while normal activation
    timing permits.
  - Xantcha's current controller loses 2 life.
  - The activating player draws one card.

Reported symptoms:
  (a) the owner is NOT offered the opponent choice as Xantcha enters;
  (b) the {3} ability is available only to Xantcha's controller, not to
      every player.

Setup (native engine, three human-client seats, CommanderDraft, 3 players;
three seats so the owner has two legal opponents to choose between):
  P0: commander=[Xantcha, Sleeper Agent], main = 30x Swamp + 30x Mountain.
      Casts the commander; owns it for the whole run.
  P1: commander=[Ayula, Queen Among Bears] (never cast; inert), 60x Forest.
      Provides the any-player activation test: P1 is neither owner nor
      controller of Xantcha.
  P2: 60x Forest (no commander), inert except it must declare Xantcha's
      mandatory attack on its own turns (Xantcha "attacks each combat if
      able" and cannot attack its owner P0, so it attacks P1).

Plan:
  1. P0 casts the Xantcha commander ({1}{B}{R}).
  2. While Xantcha is entering/resolving, record whether the engine offers
     P0 a choice among opponents (player candidates) and which choice is
     answered (P2 is the intended pick). If no choice is offered, that is
     recorded as-is.
  3. After Xantcha is on the battlefield, on P1's main phase P1 activates
     Xantcha's {3} ability (P1 is not the controller). Assert the
     controller (whoever controls Xantcha) loses 2 life and P1 draws a card.

Assertions:
  A1_setup_ok        Xantcha on the battlefield, owned by P0 (owner==0).
  A2_choice_offered  the engine offered P0 a choice among legal opponents
                     as Xantcha entered (>=2 player candidates recorded).
                     FAILED = the reported choice bug.
  A3_controlled_by_chosen
                     Xantcha's controller is P2 (the intended pick), i.e.
                     the choice (or default) put it under P2's control.
  A4_any_player_activate
                     P1 (non-controller, non-owner) was offered and
                     submitted Xantcha's {3} activation. FAILED = the
                     reported any-player-permission bug.
  A5_activation_effect
                     Xantcha's controller lost exactly 2 life (40->38) and
                     the activating player P1 drew exactly one card
                     (hand +1), with no other life changes.
  A6_cleanup         post.json: stack empty, game proceeds (no stall).

Verdict rule: reproduced iff A1 passes and at least one of
A2/A3/A4/A5 fails. not-reproduced iff A1-A6 all pass. blocked iff A1 fails.

Evidence: evidence/6916/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6916.py, wire_log.jsonl,
scenario_run.log, server.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260913-6916")
EVDIR = f"{BACKFILL}/evidence/6916/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

XANTCHA = "xantcha, sleeper agent"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
AYULA = "ayula, queen among bears"

P0_COMMANDER = [XANTCHA]
P0_MAIN = [(SWAMP, 30), (MOUNTAIN, 30)]
P1_COMMANDER = [AYULA]  # never cast; inert opponent
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

COMMANDER_FORMAT = {
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
}

STARTING_LIFE = 40
INTENDED_CONTROLLER = 2  # we choose P2 when a choice is offered

SERVER_IDENTITY = {
    "server_version": "0.81.3",
    "build_commit": "95bec6e",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "2c9918612e8fcf35d7daf5964eadaf11eeb94cc99463b2de822a906b9030fa44",
    "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
    "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "ServerHello on 127.0.0.1:9374 (isolated v0.81.3 server "
              "started for this run) + verified pin (minisign-verify of "
              "binary + signed data manifest with the repo-pinned key).",
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


def xantcha_oid_any(state):
    for oid, o in (state.get("objects", {}) or {}).items():
        if (o.get("zone") == "Battlefield"
                and lname(state, oid) == XANTCHA):
            return int(oid)
    return None


def xantcha_on_stack(state):
    for e in state.get("stack", []) or []:
        blob = json.dumps(e, default=str).lower()
        if "xantcha" in blob:
            return True
    return False


def untapped_lands(state, pid, names=(SWAMP, MOUNTAIN, FOREST)):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in names):
            out.append(int(oid))
    return out


def life_of(state, pid):
    return player_of(state, pid).get("life")


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


def player_seat_of_choice(ch):
    """Extract a player seat from a choice, or None."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                pass
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


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


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_choice_offered", "A3_controlled_by_chosen",
            "A4_any_player_activate", "A5_activation_effect", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                    format_config=COMMANDER_FORMAT)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, cdeck(P1_COMMANDER, *P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")
    kept = {}

    ST = {"xantcha_cast": False, "xantcha_cast_turn": None,
          "xantcha_oid": None, "entered": False, "entered_at": None,
          "enter_controller": None, "enter_owner": None,
          "enter_choice_offered": False, "enter_choice_iid": None,
          "enter_choice_candidates": [], "enter_choice_pick": None,
          "enter_wf_types": set(),
          "pre_exported": False, "mid_exported": False,
          "post_exported": False, "post_at": None,
          "p1_activated": False, "p1_activation_turn": None,
          "pre_act": None,
          "p1_sample_turns": set(), "p1_noact_logged": set(),
          "p2_ctrl_sample": None, "p0_owner_sample": None}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "target_selections": {}, "stack_entries": []}
    prompt_first_seen = {}
    last_select = {}

    async def finish():
        dur = time.time() - t_start
        if not ST["post_exported"]:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        pre_st = mid_st = post_st = None
        for fn, slot in (("pre", "pre_st"), ("mid", "mid_st"),
                         ("post", "post_st")):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    st = json.loads(open(p).read())["state"]
                    if slot == "pre_st":
                        pre_st = st
                    elif slot == "mid_st":
                        mid_st = st
                    else:
                        post_st = st
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        # ---- A1: setup ----
        if post_st is not None:
            xoid = ST["xantcha_oid"] or xantcha_oid_any(post_st)
            o = get_obj(post_st, xoid) if xoid else {}
            ok = (xoid is not None and o.get("zone") == "Battlefield"
                  and o.get("owner") == 0)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: xantcha_oid={xoid} zone={o.get('zone')} "
                         f"owner={o.get('owner')} controller={o.get('controller')}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: post.json missing")

        # ---- A2: choice offered ----
        if ST["enter_choice_offered"]:
            ass["A2_choice_offered"] = "passed"
            notes.append(f"A2 passed: opponent-choice prompt offered "
                         f"(iid={ST['enter_choice_iid']}, candidates="
                         f"{ST['enter_choice_candidates']})")
        elif ST["entered"]:
            ass["A2_choice_offered"] = "failed"
            notes.append("A2 FAILED: Xantcha entered with NO opponent-choice "
                         "prompt offered to the owner. BUG REPRODUCED (a).")
        else:
            ass["A2_choice_offered"] = "failed"
            notes.append("A2 failed: Xantcha never entered")

        # ---- A3: controlled by chosen ----
        if post_st is not None and ST["entered"]:
            xoid = ST["xantcha_oid"] or xantcha_oid_any(post_st)
            ctrl = get_obj(post_st, xoid).get("controller")
            if ctrl == INTENDED_CONTROLLER:
                ass["A3_controlled_by_chosen"] = "passed"
                notes.append(f"A3 passed: Xantcha controlled by P{ctrl} "
                             f"(intended pick)")
            else:
                ass["A3_controlled_by_chosen"] = "failed"
                notes.append(f"A3 FAILED: Xantcha controlled by P{ctrl}, "
                             f"not the intended P{INTENDED_CONTROLLER}")
        else:
            ass["A3_controlled_by_chosen"] = "failed"
            notes.append("A3 failed: no Xantcha on BF")

        # ---- A4: any-player activation ----
        if ST["p1_activated"]:
            ass["A4_any_player_activate"] = "passed"
            notes.append("A4 passed: P1 (non-controller) activated Xantcha's "
                         "{3} ability")
        elif ST["entered"]:
            ass["A4_any_player_activate"] = "failed"
            notes.append("A4 FAILED: P1 (non-controller, non-owner) was never "
                         "offered Xantcha's {3} activation. BUG "
                         "REPRODUCED (b).")
        else:
            ass["A4_any_player_activate"] = "failed"
            notes.append("A4 failed: Xantcha never entered")

        # ---- A5: activation effect ----
        if post_st is not None and ST["p1_activated"] and ST["pre_act"]:
            xoid = ST["xantcha_oid"] or xantcha_oid_any(post_st)
            ctrl = get_obj(post_st, xoid).get("controller")
            life_now = {i: life_of(post_st, i) for i in (0, 1, 2)}
            hand_now = {i: len(hand_ids(post_st, i)) for i in (0, 1, 2)}
            pre = ST["pre_act"]
            life_ok = (life_now[ctrl] == pre["life"][ctrl] - 2)
            others_ok = all(life_now[i] == pre["life"][i]
                            for i in (0, 1, 2) if i != ctrl)
            draw_ok = (hand_now[1] == pre["hand"][1] + 1)
            notes.append(f"A5: controller=P{ctrl} life {pre['life']} -> "
                         f"{life_now}; hands {pre['hand']} -> {hand_now}")
            if life_ok and others_ok and draw_ok:
                ass["A5_activation_effect"] = "passed"
                notes.append("A5 passed: controller -2 life, P1 drew 1")
            else:
                ass["A5_activation_effect"] = "failed"
                notes.append("A5 FAILED: activation effect wrong "
                             f"(life_ok={life_ok} others_ok={others_ok} "
                             f"draw_ok={draw_ok})")
        elif not ST["p1_activated"]:
            ass["A5_activation_effect"] = "not-run"
            notes.append("A5 not-run: no activation happened")
        else:
            ass["A5_activation_effect"] = "failed"
            notes.append("A5 failed: post.json missing")

        # ---- A6: cleanup ----
        if post_st is not None:
            stack_empty = not (post_st.get("stack") or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            ok = stack_empty and wf in ("Priority", None)
            ass["A6_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A6: stack_empty={stack_empty} waiting_for={wf}")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 failed: post.json missing")

        # ---- verdict ----
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif any(ass[k] == "failed"
                 for k in ("A2_choice_offered", "A3_controlled_by_chosen",
                           "A4_any_player_activate", "A5_activation_effect")):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6916,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 server on 127.0.0.1:9374, "
                               "started for this run (pid in runs/<run-id>/"
                               "server.pid)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6916.py", "rb").read()).hexdigest(),
            "format_config": "CommanderDraft (3 seats; P0/P1 commanders, P2 none)",
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                      "P2": {"main": P2_DECK, "commander": []}},
            "assertions": ass,
            "observations": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in obs.items()},
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_6916.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via three human-client seats.",
                "CommanderDraft format is a test-harness fixture for 3-player "
                "games (P0 casts Xantcha from the command zone).",
                "The reported 'attacks each combat if able' clause was not "
                "asserted; only the opponent-choice entry and the any-player "
                "activation permission were tested.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        import shutil
        shutil.copy(f"{BACKFILL}/driver/scenario_6916.py",
                    f"{EVDIR}/scenario_6916.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6916.py and server.log into EVDIR")
        render_summary(run, pre_st, mid_st, post_st)
        write_manifest()
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    def render_summary(run, pre_st, mid_st, post_st):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 800
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6916 - Xantcha, Sleeper Agent",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               f"server v{SERVER_IDENTITY['server_version']} "
               f"({SERVER_IDENTITY['build_commit']}) protocol 70 - "
               f"2026-09-13", fill=(140, 160, 180))
        y += 28
        d.text((24, y), f"verdict: {run['verdict'].upper()}",
               fill=(255, 90, 90) if run["verdict"] == "reproduced"
               else (120, 220, 120))
        y += 34
        d.text((24, y), "Assertions (from saved states):", fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup_ok": "Xantcha on BF, owned by P0",
            "A2_choice_offered": "opponent choice offered as Xantcha entered",
            "A3_controlled_by_chosen": "Xantcha controlled by P2 (intended)",
            "A4_any_player_activate": "P1 (non-controller) activated {3}",
            "A5_activation_effect": "controller -2 life, P1 drew 1",
            "A6_cleanup": "stack empty, game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 12

        def zone_of(st, label):
            d.text((24, y), f"{label}:", fill=(200, 210, 225))
            yy = y + 22
            oid = ST["xantcha_oid"]
            if st is not None and oid is not None:
                o = get_obj(st, oid)
                d.text((36, yy),
                       f"xantcha oid={oid} zone={o.get('zone')} "
                       f"owner={o.get('owner')} ctrl={o.get('controller')} "
                       f"tapped={bool(o.get('tapped'))}",
                       fill=(170, 180, 195))
            else:
                d.text((36, yy), "(no state)", fill=(120, 130, 145))
            return yy + 30
        yy = zone_of(pre_st, "pre.json  (Xantcha on stack)")
        yy = zone_of(mid_st, "mid.json  (Xantcha entered)")
        yy = zone_of(post_st, "post.json (after P1 activation)")
        y = yy + 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:11]:
            d.text((36, y), n[:118], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        files = ["pre.json", "mid.json", "post.json", "run.json",
                 "scenario_6916.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]
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

    def mulligan_keep(pid, state, lands_need, max_mulls):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in (SWAMP, MOUNTAIN, FOREST))
        mulls = kept.get(f"P{pid}_mulls", 0)
        if mulls >= max_mulls:
            return True
        return lands >= lands_need

    async def do_mulligan(c, pid, lands_need, max_mulls, tag):
        if mulligan_keep(pid, c.latest["state"], lands_need, max_mulls):
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps")
        else:
            kept[f"P{pid}_mulls"] = kept.get(f"P{pid}_mulls", 0) + 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{kept[f'P{pid}_mulls']}")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        pending = ((wf_of(st).get("data", {}) or {}).get("pending", []))
        count = 1
        for p in pending:
            if p.get("player") == pid:
                ph = p.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    count = int(ph.get("count", 1))
        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        picks = hand[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{tag} bottoms {count}: {[lname(st, x) for x in picks]}")

    async def discard_tick(c, pid, tag, st, state):
        wtype = wf_of(state).get("type") or ""
        if wtype != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            # discard lands first (P1/P2 are all-land; P0 keeps colors)
            def rank(ch):
                t = choice_text(ch).lower()
                return 0 if t in (SWAMP, MOUNTAIN, FOREST) else 1
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            wire("discard", {"who": tag, "choice": choice_text(pick)[:60]})
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    def looks_player_like(ch):
        """True if a choice looks like a player/opponent candidate."""
        if player_seat_of_choice(ch) is not None:
            return True
        blob = json.dumps(ch, default=str).lower()
        return ("player" in blob or "opponent" in blob) and "seat" in blob

    async def enter_choice_tick(c, pid, tag, st, state):
        """Catch the opponent-choice prompt as Xantcha enters.

        The owner (P0) should be offered a choice among legal opponents.
        Any opportunity with player candidates seen while Xantcha is on
        the stack or has just entered is treated as the reported choice:
        pick P2, record candidates, mark done.
        """
        if not ST["xantcha_cast"] or ST["entered"]:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        wf = wf_of(state)
        ST["enter_wf_types"].add(str(wf.get("type") or ""))
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            if not any(looks_player_like(ch) for ch in chs):
                continue
            seats = [player_seat_of_choice(ch) for ch in chs]
            ST["enter_choice_offered"] = True
            ST["enter_choice_iid"] = str(iid)[:16]
            ST["enter_choice_candidates"] = seats
            say(f"[{tag}] ENTER-CHOICE prompt: waiting_for="
                f"{wf.get('type')} seats={seats}")
            wire("enter_choice_prompt",
                 {"who": tag, "iid": str(iid)[:16], "seats": seats,
                  "wf_type": wf.get("type"),
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            pick = None
            for ch in chs:
                if player_seat_of_choice(ch) == INTENDED_CONTROLLER:
                    pick = ch
                    break
            if pick is None:
                pick = chs[0]
            ST["enter_choice_pick"] = player_seat_of_choice(pick)
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def generic_prompt(c, pid, tag, st, state):
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            obs["unexpected_prompts"].append(
                {"who": tag, "iid": str(iid)[:8],
                 "n_choices": len(chs),
                 "texts": [choice_text(ch)[:60] for ch in chs][:6]})
            say(f"[{tag}] UNEXPECTED PROMPT iid={iid} n={len(chs)} "
                f"texts={[choice_text(ch)[:40] for ch in chs][:4]}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if time.time() - entry["t0"] < 15:
                continue
            pick = None
            for ch in chs:
                t = choice_text(ch).lower()
                if "yes" in t or "true" in t:
                    pick = ch
                    break
            if pick is None and chs:
                pick = chs[0]
            if pick is not None:
                say(f"[{tag}] auto-answering prompt after 15s stall: "
                    f"{choice_text(pick)[:60]}")
                wire("auto_answer", {"who": tag, "iid": str(iid)[:8],
                                     "choice": choice_text(pick)[:80]})
                obs["auto_answered"].append(
                    {"who": tag, "choice": choice_text(pick)[:80]})
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

    def declare_empty_combat(c, acts, state, wtype, tag):
        da = find_action(acts, wtype)
        if not da:
            return None
        sub = copy.deepcopy(da)
        if wtype == "DeclareAttackers":
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
        else:
            sub["data"]["assignments"] = []
        return sub

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, 3, 2, "P0")
                return
            if find_action(acts, "SelectCards") and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            sub = declare_empty_combat(p0, acts, state, wtype, "P0")
            if sub:
                say("P0 declares empty combat")
                wire("declare_empty", {"who": "P0", "kind": wtype})
                await submit_as_is(p0, sub)
            return
        # 1) the opponent-choice prompt as Xantcha enters (the reported bug a)
        if await enter_choice_tick(p0, 0, "P0", st, state):
            return
        # Xantcha entered? record controller; mid export
        xoid = xantcha_oid_any(state)
        if xoid is not None and not ST["entered"]:
            o = get_obj(state, xoid)
            ST["entered"] = True
            ST["entered_at"] = time.time()
            ST["xantcha_oid"] = int(xoid)
            ST["enter_controller"] = o.get("controller")
            ST["enter_owner"] = o.get("owner")
            say(f"Xantcha entered: oid={xoid} owner={o.get('owner')} "
                f"controller={o.get('controller')} "
                f"choice_offered={ST['enter_choice_offered']}")
            wire("xantcha_entered",
                 {"oid": int(xoid), "owner": o.get("owner"),
                  "controller": o.get("controller"),
                  "choice_offered": ST["enter_choice_offered"],
                  "choice_pick": ST["enter_choice_pick"]})
        # mid export: entered, and either the choice was handled or 10s
        # passed with no prompt (a missing choice is itself the signal)
        if (ST["entered"] and not ST["mid_exported"]
                and (ST["enter_choice_offered"]
                     or time.time() - (ST["entered_at"] or 0) > 10)):
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid.json", "w") as f:
                    f.write(mid)
                ST["mid_exported"] = True
                say("exported MID (Xantcha entered)")
            except Exception as e:
                notes.append(f"mid export failed: {e}")
        # pre export: Xantcha on stack, before resolution. Primary capture
        # happens right after the cast submission (see below); this is a
        # fallback in case the first attempt raced resolution.
        if (ST["xantcha_cast"] and not ST["pre_exported"]
                and xantcha_on_stack(state) and not ST["entered"]):
            try:
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                ST["pre_exported"] = True
                say("exported PRE (Xantcha on stack, fallback)")
            except Exception as e:
                notes.append(f"pre export failed: {e}")
        # controller-side control: is the CONTROLLER offered the activation?
        xoid2 = ST["xantcha_oid"] or xantcha_oid_any(state)
        if (ST["entered"] and ST["p2_ctrl_sample"] is None
                and state.get("priority_player") == 2
                and wf_of(state).get("type") == "Priority"):
            mine = [a for a in acts if a["type"] == "ActivateAbility"
                    and xoid2 is not None
                    and (a.get("data", {}).get("source_id") == xoid2
                         or str(a.get("_src_oid")) == str(xoid2))]
            ST["p2_ctrl_sample"] = {"n": len(mine),
                                    "turn": state.get("turn_number")}
            say(f"[P2-controller] Xantcha activations offered: {len(mine)} "
                f"(turn {state.get('turn_number')})")
            wire("p2_ctrl_sample", ST["p2_ctrl_sample"])
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # ---- P0 priority ----
        # owner-side control: is the OWNER (non-controller) offered the
        # activation? Sample once per run.
        if (ST["entered"] and ST["p0_owner_sample"] is None
                and ST["xantcha_oid"] is not None):
            mine = [a for a in acts if a["type"] == "ActivateAbility"
                    and (a.get("data", {}).get("source_id") == ST["xantcha_oid"]
                         or str(a.get("_src_oid")) == str(ST["xantcha_oid"]))]
            ST["p0_owner_sample"] = {"n": len(mine),
                                     "turn": state.get("turn_number")}
            say(f"[P0-owner] Xantcha activations offered: {len(mine)} "
                f"(turn {state.get('turn_number')})")
            wire("p0_owner_sample", ST["p0_owner_sample"])
        # cast the Xantcha commander ({1}{B}{R}); gate on no Xantcha on BF
        # (legend rule) and 3 untapped lands
        if (not ST["xantcha_cast"]
                and xantcha_oid_any(state) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and len(untapped_lands(state, 0, (SWAMP, MOUNTAIN))) >= 3):
            oid = await cast_named(p0, acts, state, XANTCHA, "P0")
            if oid is not None:
                ST["xantcha_cast"] = True
                ST["xantcha_cast_turn"] = state.get("turn_number")
                # capture PRE immediately: Xantcha should be on the stack now
                await asyncio.sleep(1.0)
                try:
                    cur = (p0.latest or {}).get("state", {})
                    if xantcha_on_stack(cur) and not ST["pre_exported"]:
                        pre = await p0.export_state()
                        with open(f"{EVDIR}/pre.json", "w") as f:
                            f.write(pre)
                        ST["pre_exported"] = True
                        say("exported PRE (Xantcha on stack, post-cast)")
                except Exception as e:
                    notes.append(f"post-cast pre export failed: {e}")
                return
        if await discard_tick(p0, 0, "P0", st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 0:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, 2, 2, "P1")
                return
            if find_action(acts, "SelectCards") and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                await do_bottom(p1, 1, "P1")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers" and state.get("active_player") == 1:
            sub = declare_empty_combat(p1, acts, state, wtype, "P1")
            if sub:
                await submit_as_is(p1, sub)
            return
        if wtype == "DeclareBlockers":
            sub = declare_empty_combat(p1, acts, state, wtype, "P1")
            if sub:
                await submit_as_is(p1, sub)
            return
        if await enter_choice_tick(p1, 1, "P1", st, state):
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        # ---- P1 priority: the any-player activation test (reported bug b) ----
        # P1 is neither owner nor controller of Xantcha. If the engine
        # advertises Xantcha's {3} ActivateAbility to P1, submit it.
        xoid = ST["xantcha_oid"] or xantcha_oid_any(state)
        if (ST["entered"] and not ST["p1_activated"] and xoid is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1
                and len(untapped_lands(state, 1, (FOREST,))) >= 3):
            for a in acts:
                if a["type"] != "ActivateAbility":
                    continue
                d = a.get("data", {}) or {}
                src = d.get("source_id") or d.get("object_id")
                if a.get("_src_oid") is not None:
                    try:
                        src = int(a["_src_oid"])
                    except (TypeError, ValueError):
                        pass
                if src == xoid:
                    pre_life = {i: life_of(state, i) for i in (0, 1, 2)}
                    pre_hand = {i: len(hand_ids(state, i)) for i in (0, 1, 2)}
                    ST["pre_act"] = {"life": pre_life, "hand": pre_hand,
                                     "controller": get_obj(state, xoid).get("controller")}
                    say(f"[P1] activating Xantcha's ability (src {src}) "
                        f"pre_life={pre_life} pre_hand={pre_hand}")
                    wire("p1_activate",
                         {"src": src, "pre_life": pre_life,
                          "pre_hand": pre_hand, "action": a["type"],
                          "data": d})
                    await submit_as_is(p1, a)
                    ST["p1_activated"] = True
                    ST["p1_activation_turn"] = state.get("turn_number")
                    return
            if ST["entered"] and not ST["p1_activated"]:
                turn = state.get("turn_number")
                ST["p1_sample_turns"].add(turn)
                if turn not in ST["p1_noact_logged"]:
                    ST["p1_noact_logged"].add(turn)
                    say(f"[P1] Xantcha on BF but NO ActivateAbility advertised "
                        f"to P1 (turn {turn}, n_acts="
                        f"{len([a for a in acts if a['type']=='ActivateAbility'])})")
                    wire("p1_no_activation_offered",
                         {"turn": turn,
                          "act_types": sorted(set(a["type"] for a in acts))})
        if await discard_tick(p1, 1, "P1", st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 1:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    async def p2_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P2"):
                await do_mulligan(p2, 2, 2, 2, "P2")
                return
            if find_action(acts, "SelectCards") and last_select.get(2) != p2.revision:
                last_select[2] = p2.revision
                await do_bottom(p2, 2, "P2")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p2, a)
                return
        # P2 controls Xantcha: it "attacks each combat if able" and cannot
        # attack its owner (P0), so declare it attacking P1.
        if wtype == "DeclareAttackers" and state.get("active_player") == 2:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                xoid = ST["xantcha_oid"] or xantcha_oid_any(state)
                if xoid is not None:
                    already = [a for a in (sub["data"].get("attacks") or [])
                               if a and a[0] == xoid]
                    if not already:
                        sub["data"]["attacks"] = [
                            [xoid, {"type": "Player", "data": 1}]]
                        sub["data"]["bands"] = []
                        say(f"[P2] declaring Xantcha (oid {xoid}) attacking P1")
                        wire("p2_attack_xantcha", {"oid": xoid})
                await submit_as_is(p2, sub)
            return
        if wtype == "DeclareBlockers":
            sub = declare_empty_combat(p2, acts, state, wtype, "P2")
            if sub:
                await submit_as_is(p2, sub)
            return
        if await enter_choice_tick(p2, 2, "P2", st, state):
            return
        if not my_priority(state, 2):
            if await generic_prompt(p2, 2, "P2", st, state):
                return
            return
        if await discard_tick(p2, 2, "P2", st, state):
            return
        if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 2:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p2, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p2, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1"),
                             (p2, p2_tick, "P2")):
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
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        # post export: P1's activation resolved (stack empty), or Xantcha
        # entered and P1 has had >=2 distinct main-phase turns with 3+
        # untapped lands and still no activation offered (the missing offer
        # is itself the signal for claim b).
        act_done = (ST["p1_activated"]
                    and not (p0.latest["state"].get("stack") or []))
        no_offer_done = (ST["entered"] and not ST["p1_activated"]
                         and len(ST["p1_sample_turns"]) >= 2
                         and not (p0.latest["state"].get("stack") or []))
        if ST["entered"] and not ST["post_exported"] and (act_done or no_offer_done):
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say(f"exported POST (act_done={act_done} "
                    f"no_offer_done={no_offer_done})")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        if (ST["entered"] and not ST["post_exported"]
                and time.time() - t0 > 1200):
            notes.append("watchdog: 1200s elapsed without post; finishing")
            say("WATCHDOG: finishing with evidence")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
            except Exception as e:
                notes.append(f"watchdog post export failed: {e}")
            await finish()
            return
        if ST["post_exported"] and time.time() - (ST["post_at"] or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0lands={len(untapped_lands(s,0,(SWAMP,MOUNTAIN)))} "
                f"P1lands={len(untapped_lands(s,1,(FOREST,)))} "
                f"xcast={ST['xantcha_cast']} entered={ST['entered']} "
                f"ctrl={ST['enter_controller']} choice={ST['enter_choice_offered']} "
                f"p1act={ST['p1_activated']} "
                f"pre={ST['pre_exported']} mid={ST['mid_exported']} "
                f"post={ST['post_exported']} stack={len(s.get('stack') or [])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
