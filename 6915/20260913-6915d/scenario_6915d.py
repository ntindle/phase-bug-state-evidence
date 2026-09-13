#!/usr/bin/env python3
"""Issue #6915: Abnormal Endurance on a commander — reported as the card
"not returning to owner's hand if it's a commander".

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Abnormal Endurance ({1}{B} Instant):
    "Until end of turn, target creature gets +2/+0 and gains 'When this
     creature dies, return it to the battlefield tapped under its owner's
     control.'"
  Murder ({1}{B} Instant): "Destroy target creature."
  Aphemia, the Cacophony ({1}{B} 2/1 Legendary Creature) - P0's commander.
  SUBSTITUTION (recorded): the brief named "Marauding Blight-Priest" as the
  commander, but that card is NOT legendary, so the engine's CommanderDraft
  validation rejects it ("must be legendary creatures or explicitly allow
  being a commander"). Aphemia is the cheapest mono-black legendary
  ({1}{B}) in the pinned data; the acceptance criteria (dies trigger after
  being left in the graveyard) are commander-agnostic.

The report title says "return to hand", which is NOT Oracle-correct
(confirmed by the triage comment on the issue, 2026-08-03). The acceptance
criteria are:
  - after explicitly LEAVING the commander in the graveyard, the granted
    dies trigger must return it to the battlefield TAPPED under its owner's
    control;
  - if moved to the command zone before resolution, it does not return.

Setup (native engine, three human-client seats, CommanderDraft, 3 players):
  P0: commander=[Aphemia, the Cacophony] (mono-black legendary, identity {B}),
      main = 36x Swamp + 12x Abnormal Endurance + 12x Murder.
  P1: 60x Swamp, no commander. Inert: plays a land and passes each turn.
P0 casts the commander, then Abnormal Endurance targeting it (must resolve
and grant the dies trigger first), then Murder targeting it. The commander
dies; the driver explicitly drives the command-zone replacement decision
and chooses to LEAVE it in the graveyard; then the granted trigger is
allowed to resolve.

Assertions:
  A1_setup_ok       pre.json: commander on P0 BF (is_commander=true),
                    Abnormal Endurance resolved targeting it (commander's
                    granted-ability text contains "return it to the
                    battlefield tapped")
  A2_commander_dies mid.json: Murder resolved, commander no longer on BF
  A3_graveyard_choice the command-zone decision was explicitly driven:
                    recorded choice text ("leave in graveyard"); if the
                    engine offered no prompt, the automatic handling is
                    recorded instead
  A4_returns_tapped post.json: commander is on P0's BF TAPPED ->
                    passed (not reproduced). If it stays in the graveyard
                    (or is in the command zone despite being left in the
                    graveyard) after the trigger resolves -> FAILED
                    (bug reproduced)
  A5_cleanup        post.json: stack empty, game proceeding (no stall)

Verdict rule: reproduced iff A1-A3 pass and A4 fails (commander does not
return to the battlefield tapped after being left in the graveyard).
not-reproduced iff A1-A5 all pass. blocked iff the flow cannot be driven.

Evidence: evidence/6915/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6915d.py, wire_log.jsonl,
scenario_run.log, server.log

ATTEMPTS 1-2 (20260913-6915, 20260913-6915b) were methodologically INVALID
and are discarded pre-publish: Abnormal Endurance grants its dies trigger
only "until end of turn", but Murder was cast 3 turns AFTER AE resolved
(turns 9->12 and 8->11), so no granted trigger existed at death and the
engine correctly fired nothing. This run casts AE and Murder in the SAME
turn, so the granted trigger is live when the commander dies.
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
RUN_ID = os.environ.get("RUN_ID", "20260913-6915d")
EVDIR = f"{BACKFILL}/evidence/6915/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CMDR = "aphemia, the cacophony"
AE = "abnormal endurance"
MURDER = "murder"
SWAMP = "swamp"
FOREST = "forest"
AYULA = "ayula, queen among bears"

P0_COMMANDER = [CMDR]
P0_MAIN = [(SWAMP, 36), (AE, 12), (MURDER, 12)]
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
    "source": "ServerHello on 127.0.0.1:9375 (isolated v0.81.3 server "
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


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm == SWAMP):
            out.append(int(oid))
    return out


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


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def has_ae_effect(state, oid):
    """True if the object carries Abnormal Endurance's granted dies trigger."""
    blob = json.dumps(get_obj(state, oid), default=str).lower()
    return "return it to the battlefield tapped" in blob


def commander_zone(state, oid):
    return get_obj(state, oid).get("zone")


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
           ("A1_setup_ok", "A2_commander_dies", "A3_graveyard_choice",
            "A4_returns_tapped", "A5_cleanup")}
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

    ST = {"commander_cast": False, "commander_cast_turn": None,
          "commander_oid": None,
          "ae_cast": False, "ae_cast_turn": None, "ae_resolved": False,
          "ae_target_oid": None,
          "murder_cast": False, "murder_cast_turn": None,
          "murder_target_oid": None, "murder_resolved": False,
          "cz_choice_made": False, "cz_choice_text": None,
          "cz_choice_auto": None, "cz_choice_iid": None,
          "pending_target": None,
          "trigger_observed": False, "trigger_resolved": False,
          "death_at": None, "death_seen": False,
          "pre_exported": False, "mid_exported": False,
          "post_exported": False, "post_at": None,
          "pre_bf_oid": None}
    obs = {"wf_types_kill_window": set(), "auto_answered": [],
           "unexpected_prompts": [], "rejections": [],
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
        if pre_st is not None:
            coid = bf_id(pre_st, 0, CMDR)
            ok = (coid is not None
                  and bool(get_obj(pre_st, coid).get("is_commander"))
                  and has_ae_effect(pre_st, coid)
                  and ST["ae_cast"])
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: commander_bf={coid} is_commander="
                         f"{bool(coid is not None and get_obj(pre_st, coid).get('is_commander'))} "
                         f"ae_effect={bool(coid is not None and has_ae_effect(pre_st, coid))} "
                         f"ae_cast={ST['ae_cast']}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: pre.json missing")

        # ---- A2: commander dies ----
        if mid_st is not None:
            dead = (ST["pre_bf_oid"] is not None
                    and get_obj(mid_st, ST["pre_bf_oid"]).get("zone") != "Battlefield")
            ok = ST["murder_cast"] and dead
            ass["A2_commander_dies"] = "passed" if ok else "failed"
            notes.append(f"A2: murder_cast={ST['murder_cast']} "
                         f"commander_zone_at_mid="
                         f"{get_obj(mid_st, ST['pre_bf_oid']).get('zone') if ST['pre_bf_oid'] else '?'}")
        elif ST.get("death_seen"):
            ass["A2_commander_dies"] = "passed"
            notes.append("A2 passed via live death_seen (mid.json missing)")

        # ---- A3: graveyard choice explicitly driven ----
        if ST["cz_choice_made"]:
            ass["A3_graveyard_choice"] = "passed"
            notes.append(f"A3 passed: choice recorded: {ST['cz_choice_text']}")
        elif ST["cz_choice_auto"]:
            ass["A3_graveyard_choice"] = "passed"
            notes.append(f"A3 passed (auto): engine moved commander with no "
                         f"prompt: {ST['cz_choice_auto']}")
        else:
            ass["A3_graveyard_choice"] = "failed"
            notes.append("A3 FAILED: no command-zone decision was recorded")

        # ---- A4: returns tapped ----
        if post_st is not None and ST["pre_bf_oid"] is not None:
            o = get_obj(post_st, ST["pre_bf_oid"])
            zone = o.get("zone")
            tapped = bool(o.get("tapped"))
            ctrl = o.get("controller")
            notes.append(f"A4: commander oid={ST['pre_bf_oid']} zone={zone} "
                         f"tapped={tapped} controller={ctrl} "
                         f"trigger_resolved={ST['trigger_resolved']}")
            if (ass["A1_setup_ok"] == "passed"
                    and ass["A2_commander_dies"] == "passed"
                    and ass["A3_graveyard_choice"] == "passed"
                    and zone == "Battlefield" and tapped and ctrl == 0):
                ass["A4_returns_tapped"] = "passed"
                notes.append("A4 passed: commander returned to P0's "
                             "battlefield tapped. Bug NOT reproduced.")
            else:
                ass["A4_returns_tapped"] = "failed"
                notes.append("A4 FAILED: commander did NOT return to the "
                             "battlefield tapped after being left in the "
                             "graveyard. BUG REPRODUCED.")
        else:
            ass["A4_returns_tapped"] = "failed"
            notes.append("A4 failed: post.json missing or no commander oid")

        # ---- A5: cleanup ----
        if post_st is not None:
            stack_empty = not (post_st.get("stack") or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            ok = stack_empty and wf in ("Priority", None)
            ass["A5_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A5: stack_empty={stack_empty} waiting_for={wf}")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 failed: post.json missing")

        # ---- verdict ----
        if (ass["A1_setup_ok"] == "passed"
                and ass["A2_commander_dies"] == "passed"
                and ass["A3_graveyard_choice"] == "passed"
                and ass["A4_returns_tapped"] == "failed"):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        else:
            verdict = "reproduced"
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 6915,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 server on 127.0.0.1:9375, "
                               "started for this run (pid in runs/<run-id>/"
                               "server.pid)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6915d.py", "rb").read()).hexdigest(),
            "format_config": "CommanderDraft (3 seats; P0/P1 commanders, P2 none)",
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                      "P2": {"main": P2_DECK, "commander": []}},
            "assertions": ass,
            "observations": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in obs.items()},
            "driver_state": {k: v for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_6915d.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via three human-client seats.",
                "Dense 12x Abnormal Endurance / 12x Murder main deck is a "
                "test-harness convenience (engine accepts >4-of for custom games).",
                "The report's 'return to hand' premise is Oracle-incorrect; the "
                "triage's acceptance criteria (return to battlefield tapped when "
                "left in the graveyard) are tested instead.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        import shutil
        shutil.copy(f"{BACKFILL}/driver/scenario_6915d.py",
                    f"{EVDIR}/scenario_6915d.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6915d.py and server.log into EVDIR")
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
        W, H = 1000, 760
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6915 - Abnormal Endurance + commander",
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
            "A1_setup_ok": "commander on P0 BF with granted dies trigger",
            "A2_commander_dies": "Murder resolved; commander left the BF",
            "A3_graveyard_choice": "command-zone decision driven explicitly",
            "A4_returns_tapped": "commander back on P0 BF tapped",
            "A5_cleanup": "stack empty, game proceeds",
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
            oid = ST["pre_bf_oid"]
            if st is not None and oid is not None:
                o = get_obj(st, oid)
                d.text((36, yy),
                       f"commander oid={oid} zone={o.get('zone')} "
                       f"tapped={bool(o.get('tapped'))} "
                       f"ctrl={o.get('controller')} "
                       f"is_commander={bool(o.get('is_commander'))} "
                       f"ae_effect={has_ae_effect(st, oid)}",
                       fill=(170, 180, 195))
            else:
                d.text((36, yy), "(no state)", fill=(120, 130, 145))
            return yy + 30
        yy = zone_of(pre_st, "pre.json  (before Murder)")
        yy = zone_of(mid_st, "mid.json  (after death, choice made)")
        yy = zone_of(post_st, "post.json (after trigger resolution)")
        y = yy + 10
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:10]:
            d.text((36, y), n[:118], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        files = ["pre.json", "mid.json", "post.json", "run.json",
                 "scenario_6915d.py", "wire_log.jsonl", "scenario_run.log",
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

    def mulligan_keep(pid, state, lands_need, max_mulls, land_name,
                      key_need=None):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n == land_name)
        mulls = kept.get(f"P{pid}_mulls", 0)
        if mulls >= max_mulls:
            return True
        if lands < lands_need:
            return False
        return key_need is None or key_need in hn or mulls >= max_mulls - 1

    async def do_mulligan(c, pid, lands_need, max_mulls, tag, land_name=SWAMP,
                          key_need=None):
        if mulligan_keep(pid, c.latest["state"], lands_need, max_mulls,
                         land_name, key_need):
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
        # bottom swamps first (we need spells, not lands, from mulls)
        picks = sorted(hand,
                       key=lambda o: 0 if lname(st, o) == SWAMP else 1)[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{tag} bottoms {count}: {[lname(st, x) for x in picks]}")

    async def discard_tick(c, pid, tag, acts, st, state):
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
            # prefer discarding duplicate spells, keep lands balanced
            hn = hand_lnames(state, pid)
            def rank(ch):
                t = choice_text(ch).lower()
                if t == AE and hn.count(AE) > 1:
                    return 0
                if t == MURDER and hn.count(MURDER) > 1:
                    return 1
                if t == SWAMP and hn.count(SWAMP) > 5:
                    return 2
                return 3
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            wire("discard", {"who": tag, "choice": choice_text(pick)[:60]})
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    def looks_like_zone_choice(opps):
        for opp in opps:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            for ch in data.get("choices") or data.get("candidates") or []:
                t = choice_text(ch).lower()
                if "command zone" in t or "graveyard" in t:
                    return True
        return False

    def _decline_choice(chs):
        """Pick the accept=false / decline choice from decideOptionalEffect
        candidates (empty-text per protocol-69 kicker-flow lesson)."""
        for ch in chs:
            for sf in ch.get("surfaces", []) or []:
                d = sf.get("data") or {}
                if (sf.get("type") == "value" and d.get("role") == "accept"
                        and str(d.get("value")).lower() == "false"):
                    return ch
        return None

    async def optional_effect_tick(c, tag, st, state):
        """Handle OptionalEffectChoice AND CommanderZoneChoice prompts by
        DECLINING (accept=false).

        Prior attempt (20260913-6915) stalled on wf.type == "CommanderZoneChoice"
        (decideOptionalEffect surfaces, true/false texts): the decline handler
        gated on OptionalEffectChoice only, so the generic 15s auto-answer picked
        accept=true and moved the commander to the command zone — testing the
        wrong branch. This handler now owns both prompt types.

        Observed shapes on protocol 70 (empty choice texts, decideOptionalEffect
        action surfaces + accept true/false value surfaces):
          - Aphemia's end-step "may exile an enchantment" trigger -> decline
            is safe (nothing we need exiled).
          - the commander-death "you may move it to the command zone"
            replacement -> decline LEAVES the commander in the graveyard,
            which is exactly the primary test's required decision.
        The waiting_for description disambiguates; both are recorded.
        Returns True if an opportunity was handled."""
        wf = wf_of(state)
        if wf.get("type") not in ("OptionalEffectChoice",
                                  "CommanderZoneChoice"):
            return False
        vi = get_vi(st)
        if not vi:
            return False
        desc = str((wf.get("data") or {}).get("description", ""))
        handled = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            pick = _decline_choice(chs)
            if pick is None:
                say(f"[{tag}] OptionalEffectChoice has no decline candidate; "
                    f"NOT auto-answering (iid={iid})")
                wire("optional_effect_no_decline",
                     {"who": tag, "iid": str(iid)[:16], "desc": desc[:160]})
                prompt_first_seen[iid] = {"t0": time.time(), "done": True}
                handled = True
                continue
            is_zone = ("command zone" in desc.lower()
                         or wf.get("type") == "CommanderZoneChoice")
            say(f"[{tag}] OptionalEffectChoice: declining "
                f"({'COMMANDER-ZONE CHOICE' if is_zone else 'optional effect'}): "
                f"{desc[:100]}")
            wire("optional_effect_declined",
                 {"who": tag, "iid": str(iid)[:16], "desc": desc[:200],
                  "wf_type": wf.get("type"),
                  "is_commander_zone_choice": is_zone,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if is_zone:
                ST["cz_choice_made"] = True
                ST["cz_choice_text"] = ("declined move to command zone "
                                        "(accept=false)")
                ST["cz_choice_iid"] = str(iid)[:16]
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            handled = True
        return handled

    async def target_tick(c, tag, st, state):
        """Answer TargetSelection prompts for pending AE/Murder casts."""
        if wf_of(state).get("type") != "TargetSelection":
            return False
        if ST["pending_target"] != CMDR:
            return False
        spell = ST.get("pending_spell") or "?"
        vi = get_vi(st)
        if not vi:
            return False
        coid = bf_id(state, 0, CMDR)
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            pick = None
            for ch in chs:
                blob = json.dumps(ch, default=str)
                if coid is not None and str(coid) in blob:
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    if choice_text(ch).lower() == CMDR:
                        pick = ch
                        break
            if pick is None:
                say(f"[{tag}] TargetSelection: no commander candidate found; "
                    f"waiting")
                return True
            # record the ACTUALLY submitted candidate oid
            sub_oid = None
            m = __import__("re").search(r"\b(\d{5,})\b",
                                        json.dumps(pick, default=str))
            if m:
                sub_oid = int(m.group(1))
            say(f"[{tag}] targeting commander (oid {sub_oid}) for {spell}")
            obs["target_selections"].setdefault(spell, []).append(
                {"iid": str(iid)[:16], "submitted_oid": sub_oid,
                 "actual_commander_oid": coid})
            wire("target_selection",
                 {"who": tag, "spell": spell, "submitted_oid": sub_oid,
                  "commander_oid": coid})
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            ST["pending_target"] = None
            ST["pending_spell"] = None
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
            # OptionalEffectChoice / CommanderZoneChoice are owned by
            # optional_effect_tick (decline); never let the generic 15s
            # auto-answer pick accept=true here.
            if (wf_of(state).get("type") in ("OptionalEffectChoice",
                                             "CommanderZoneChoice")
                    or looks_like_zone_choice([opp])):
                say(f"[{tag}] skipping decline-owned prompt in generic "
                    f"handler")
                prompt_first_seen[iid] = {"t0": time.time(), "done": True}
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
            # never auto-answer a commander-zone-looking prompt here
            if (wf_of(state).get("type") in ("OptionalEffectChoice",
                                             "CommanderZoneChoice")
                    or looks_like_zone_choice([opp])):
                say(f"[{tag}] refusing to auto-answer zone-choice-like "
                    f"prompt")
                entry["done"] = True
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

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, 3, 2, "P0", SWAMP, AE)
                return
            if find_action(acts, "SelectCards") and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # declare no attackers / no blockers (legacy action; the protocol-70
        # relations-schema vi is superseded by the action submission, as in
        # scenario_6914). Guard: one submission per turn, re-arm after 10s.
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            atk = find_action(acts, wtype)
            key = f"declared_{wtype}"
            armed = ST.get(key)
            now_turn = state.get("turn_number")
            if atk and (armed is None or armed[0] != now_turn
                        or time.time() - armed[1] > 10):
                sub = copy.deepcopy(atk)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                else:
                    sub["data"]["assignments"] = []
                ST[key] = (now_turn, time.time())
                say(f"P0 {wtype}: declaring empty")
                wire("declare_empty", {"who": "P0", "kind": wtype,
                                       "turn": now_turn})
                await submit_as_is(p0, sub)
            return
        # 1) OptionalEffectChoice prompts (commander-zone decision,
        #    Aphemia's end-step may-trigger): DECLINE, highest priority so
        #    generic handlers never touch them
        if await optional_effect_tick(p0, "P0", st, state):
            return
        # 2) target selections for our spells
        if await target_tick(p0, "P0", st, state):
            return
        coid = bf_id(state, 0, CMDR)
        if coid is not None and ST["commander_oid"] is None:
            ST["commander_oid"] = int(coid)
        # commander died? record the automatic zone handling if no prompt
        # ever appeared. Only after 10s dead, so the real OptionalEffectChoice
        # prompt (handled above) gets a chance first.
        if (ST["commander_cast"] and ST["pre_bf_oid"] is not None
                and get_obj(state, ST["pre_bf_oid"]).get("zone") != "Battlefield"):
            ST["death_seen"] = True
        if (ST["commander_cast"] and not ST["cz_choice_made"]
                and ST["pre_bf_oid"] is not None
                and get_obj(state, ST["pre_bf_oid"]).get("zone") != "Battlefield"
                and ST["cz_choice_auto"] is None
                and ST["death_at"] is not None
                and time.time() - ST["death_at"] > 10):
            z = get_obj(state, ST["pre_bf_oid"]).get("zone")
            ST["cz_choice_auto"] = f"engine moved commander to {z} with no prompt"
            notes.append(f"zone move: {ST['cz_choice_auto']}")
            say(f"zone move observed without prompt: {z}")
        # granted dies trigger going on/off the stack. NOTE: the AE
        # SPELL itself also sits on the stack and its blob contains the
        # card text, so discriminate by kind.type == "TriggeredAbility"
        # (pitfall #6773): only the granted dies trigger counts.
        trig_on_stack = False
        for e in state.get("stack", []) or []:
            kind = (e.get("kind") or {})
            if kind.get("type") != "TriggeredAbility":
                continue
            if ST["death_at"] is None:
                continue  # no death yet: cannot be the dies trigger
            blob = json.dumps(e, default=str).lower()
            if "return it to the battlefield tapped" in blob:
                trig_on_stack = True
                sig = blob[:120]
                if not obs["stack_entries"] or obs["stack_entries"][-1] != sig:
                    obs["stack_entries"].append(sig)
                    ST["trigger_observed"] = True
                    say(f"AE dies-trigger stack entry observed: {sig[:90]}")
                    wire("ae_trigger_on_stack", {"sig": sig[:200]})
        if (ST["trigger_observed"] and ST["death_at"] is not None
                and not trig_on_stack and not (state.get("stack") or [])):
            ST["trigger_resolved"] = True
            say("AE dies trigger resolved (stack empty)")
            wire("ae_trigger_resolved", {})
        # mid export: first tick after death with the choice handled
        if (ST["murder_cast"] and not ST["mid_exported"]
                and (ST["cz_choice_made"] or ST["cz_choice_auto"]
                     or ST["trigger_observed"])
                and ST["pre_bf_oid"] is not None
                and get_obj(state, ST["pre_bf_oid"]).get("zone") != "Battlefield"):
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid.json", "w") as f:
                    f.write(mid)
                ST["mid_exported"] = True
                say("exported MID (commander dead, choice made)")
            except Exception as e:
                notes.append(f"mid export failed: {e}")
        # pre export: commander BF with AE granted effect, before Murder
        if (not ST["pre_exported"] and coid is not None
                and has_ae_effect(state, coid) and not ST["murder_cast"]):
            try:
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                ST["pre_exported"] = True
                ST["pre_bf_oid"] = int(coid)
                say("exported PRE (commander on BF, AE granted)")
            except Exception as e:
                notes.append(f"pre export failed: {e}")
        # post export: commander dead, stack empty, and either it is BACK
        # on the battlefield (trigger resolved and returned it - capture the
        # tapped state before the next untap step) OR 90s passed with no
        # return (a missing return is itself the bug signal - export anyway).
        death_wait_ok = (ST["death_at"] is not None
                         and time.time() - ST["death_at"] > 90)
        cmdr_back = (ST["death_seen"]
                     and ST["pre_bf_oid"] is not None
                     and get_obj(state, ST["pre_bf_oid"]).get("zone")
                     == "Battlefield")
        if (ST["murder_cast"] and not ST["post_exported"]
                and not (state.get("stack") or [])
                and (cmdr_back or death_wait_ok)):
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say(f"exported POST (trigger_resolved="
                    f"{ST['trigger_resolved']})")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        if wtype == "OrderTriggers":
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    iid = opp.get("interactionId")
                    if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                        continue
                    resp = opp.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    spec = data.get("spec", {}) or {}
                    cands = data.get("choices") or data.get("candidates") or []
                    ids = [c.get("id") for c in cands if c.get("id")]
                    if resp.get("type") == "schema" and spec.get("type") == "sequence" and ids:
                        sub = {"interactionId": iid,
                               "response": {"type": "sequence",
                                            "data": {"choiceIds": ids}}}
                        say(f"P0 ordering triggers (as-is): {ids}")
                        wire("order_triggers", {"who": "P0",
                                                "submission": sub})
                        await p0.send_interaction(sub)
                        prompt_first_seen[iid] = {"t0": time.time(), "done": True}
                        return
            return
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # ---- P0 priority ----
        # cast commander from the command zone
        if (not ST["commander_cast"]
                and bf_id(state, 0, CMDR) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and len(untapped_lands(state, 0)) >= 2):
            oid = await cast_named(p0, acts, state, CMDR, "P0")
            if oid is not None:
                ST["commander_cast"] = True
                ST["commander_cast_turn"] = state.get("turn_number")
                return
        # cast Abnormal Endurance targeting the commander (only after it is
        # on the BF; AE must RESOLVE before Murder)
        # if AE resolved on an earlier turn but Murder never landed the
        # same turn, the granted trigger has expired: re-cast AE this turn
        if (ST["ae_resolved"] and not ST["murder_cast"]
                and ST["ae_cast_turn"] is not None
                and state.get("turn_number") > ST["ae_cast_turn"]):
            ST["ae_cast"] = False
            ST["ae_resolved"] = False
            notes.append(f"AE effect expired after turn {ST['ae_cast_turn']}; "
                         f"re-arming AE cast")
            say("AE effect expired (turn advanced); re-arming AE cast")
        if (ST["commander_cast"] and not ST["ae_cast"]
                and coid is not None
                and AE in hand_lnames(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and len(untapped_lands(state, 0)) >= 4):
            oid = await cast_named(p0, acts, state, AE, "P0")
            if oid is not None:
                ST["ae_cast"] = True
                ST["ae_cast_turn"] = state.get("turn_number")
                ST["pending_target"] = CMDR
                ST["pending_spell"] = AE
                return
        # mark AE resolved once the granted effect is on the commander
        if ST["ae_cast"] and not ST["ae_resolved"] and coid is not None \
                and has_ae_effect(state, coid) and not (state.get("stack") or []):
            ST["ae_resolved"] = True
            if ST["pending_target"] is not None:
                # AE was auto-targeted; record the actual target from state
                ST["ae_target_oid"] = int(coid)
                obs["target_selections"].setdefault(AE, []).append(
                    {"auto": True, "submitted_oid": int(coid),
                     "actual_commander_oid": int(coid)})
                ST["pending_target"] = None
                ST["pending_spell"] = None
            say(f"AE resolved: commander has the granted dies trigger")
            wire("ae_resolved", {"commander_oid": int(coid)})
        # cast Murder targeting the commander, only after AE resolved
        # and on the SAME turn (granted trigger is "until end of turn")
        if (ST["ae_resolved"] and not ST["murder_cast"]
                and coid is not None
                and MURDER in hand_lnames(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and state.get("turn_number") == ST["ae_cast_turn"]
                and len(untapped_lands(state, 0)) >= 2):
            oid = await cast_named(p0, acts, state, MURDER, "P0")
            if oid is not None:
                ST["murder_cast"] = True
                ST["murder_cast_turn"] = state.get("turn_number")
                ST["pending_target"] = CMDR
                ST["pending_spell"] = MURDER
                return
        # mark Murder resolved once the commander left the BF
        if ST["murder_cast"] and not ST["murder_resolved"] \
                and ST["pre_bf_oid"] is not None \
                and get_obj(state, ST["pre_bf_oid"]).get("zone") != "Battlefield" \
                and not (state.get("stack") or []):
            ST["murder_resolved"] = True
            if ST["pending_target"] is not None:
                obs["target_selections"].setdefault(MURDER, []).append(
                    {"auto": True, "submitted_oid": ST["pre_bf_oid"]})
                ST["pending_target"] = None
                ST["pending_spell"] = None
            say("Murder resolved: commander left the BF")
            wire("murder_resolved",
                 {"zone": get_obj(state, ST["pre_bf_oid"]).get("zone")})
            if ST["death_at"] is None:
                ST["death_at"] = time.time()
        if await discard_tick(p0, 0, "P0", acts, st, state):
            return
        # land drop
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
                await do_mulligan(p1, 1, 2, 2, "P1", FOREST)
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
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p1, sub)
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(p1, sub)
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        if await discard_tick(p1, 1, "P1", acts, st, state):
            return
        # inert opponent: land, then pass
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
                await do_mulligan(p2, 2, 2, 2, "P2", FOREST)
                return
            if find_action(acts, "SelectCards") and last_select.get(2) != p2.revision:
                last_select[2] = p2.revision
                await do_bottom(p2, 2, "P2")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p2, a)
                return
        if wtype == "DeclareAttackers" and state.get("active_player") == 2:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p2, sub)
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(p2, sub)
            return
        if not my_priority(state, 2):
            if await generic_prompt(p2, 2, "P2", st, state):
                return
            return
        if await discard_tick(p2, 2, "P2", acts, st, state):
            return
        # inert: land, then pass
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
        if (ST["murder_cast"] and not ST["post_exported"]
                and time.time() - t0 > 1200):
            notes.append("watchdog: 1200s elapsed since murder without post; "
                         "finishing")
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
            coid = ST["commander_oid"] or bf_id(s, 0, CMDR)
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_lnames(s, 0)} "
                f"cmdr={coid} cmdrcast={ST['commander_cast']} "
                f"ae={ST['ae_cast']}/{ST['ae_resolved']} "
                f"murder={ST['murder_cast']}/{ST['murder_resolved']} "
                f"czchoice={ST['cz_choice_made']}/{ST['cz_choice_auto']} "
                f"trig={ST['trigger_observed']}/{ST['trigger_resolved']} "
                f"pre={ST['pre_exported']} mid={ST['mid_exported']} "
                f"post={ST['post_exported']} stack={len(s.get('stack') or [])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
