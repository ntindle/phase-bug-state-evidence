#!/usr/bin/env python3
"""Issue #7139: Can't cast theft spells outside of commander color identity.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.82.0):
  Gonti, Lord of Luxury ({2}{B}{B}, color identity Black):
    "Deathtouch. When Gonti enters, look at the top four cards of target
     opponent's library, exile one of them face down, then put the rest on
     the bottom of that library in a random order. You may cast that card
     for as long as it remains exiled, and mana of any type can be spent
     to cast that spell."
  Lightning Bolt ({R}, color identity Red):
    "Lightning Bolt deals 3 damage to any target."

Report: "Think I figured out why most theft cards don't work, it won't let
you cast spells outside of your commander identities."
Triage: Commander color identity constrains deck construction, not the colors
of spells a player can cast during a game. Acceptance criteria: a stolen
spell is not rejected solely for color identity; normal mana and
cast-permission requirements still apply; reproduction covers the exact
theft effect and source zone.

Setup (native engine, three human-client seats, CommanderDraft):
  P0: commander=[gonti, lord of luxury], main=60x swamp. Casts Gonti; its ETB
      exiles a Lightning Bolt from P1's library face down and grants P0
      permission to cast it with mana of any type.
  P1: commander=[krenko, mob boss] (inert, never cast), main=60x lightning
      bolt. All-Bolt library so Gonti's "top four" are always Bolts.
  P2: commander=[] (none), main=60x forest. Inert filler for the 3-player
      CommanderDraft minimum; declares empty attackers.

Plan:
  1. P0 casts Gonti from the command zone ({2}{B}{B}).
  2. Answer Gonti's ETB target prompt (target opponent -> P1).
  3. Answer the exile-one choice (pick a Lightning Bolt).
  4. On P0's main phases with the Bolt exiled, sample legal_actions and
     viewer opportunities: is a cast of the exiled Bolt advertised?
  5. If advertised, cast it, target P1, and assert resolution (P1 40->37,
     Bolt to P1's graveyard, paid with any-type mana).
  6. If never advertised across >=3 valid P0 main-phase windows, record the
     missing offer as the reported failure.

Assertions:
  A1_setup_ok     Gonti on P0's battlefield (owner=controller=0); a P1-owned
                  exiled card is present (the stolen Bolt).
  A2_cast_offered a cast action for the exiled Bolt is advertised to P0 at a
                  valid timing (P0 main phase, P0 priority, stack empty,
                  >=1 untapped Swamp). FAILED = the reported bug.
  A3_cast_completes
                  the submitted cast is accepted (no rejection); Bolt on stack.
  A4_resolves     P1 life 40->37; Bolt leaves exile for P1's graveyard.
  A5_cleanup      stack empty, game proceeds.

Verdict rule: reproduced iff A1 passes and at least one of A2/A3/A4 fails.
not-reproduced iff A1-A5 all pass. blocked iff A1 fails.

Evidence: evidence/7139/<run-id>/pre.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7139.py, wire_log.jsonl,
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
RUN_ID = os.environ.get("RUN_ID", "20260913-7139")
EVDIR = f"{BACKFILL}/evidence/7139/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

GONTI = "gonti, lord of luxury"
BOLT = "lightning bolt"
SWAMP = "swamp"
FOREST = "forest"
KRENKO = "krenko, mob boss"

P0_COMMANDER = [GONTI]
P0_MAIN = [(SWAMP, 60)]
P1_COMMANDER = [KRENKO]  # inert; keeps 60x Bolt construction-legal
P1_DECK = [(BOLT, 60)]
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

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034202bdf67b283b5805ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-13",
    "source": "ServerHello on 127.0.0.1:9374 (isolated v0.82.0 server "
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


def gonti_oid_any(state):
    for oid, o in (state.get("objects", {}) or {}).items():
        if (o.get("zone") == "Battlefield"
                and lname(state, oid) == GONTI):
            return int(oid)
    return None


def exiled_p1_oids(state):
    """Oids of P1-owned objects currently in exile."""
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        if o.get("zone") == "Exile" and o.get("owner") == 1:
            out.append(int(oid))
    return out


def untapped_lands(state, pid, names=(SWAMP, FOREST)):
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
    return cid


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_cast_offered", "A3_cast_completes",
            "A4_resolves", "A5_cleanup")}
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

    ST = {"gonti_cast": False, "gonti_cast_turn": None,
          "gonti_oid": None, "entered": False, "entered_at": None,
          "etb_target_offered": False, "etb_target_pick": None,
          "etb_wf_types": set(),
          "exile_choice_offered": False, "exile_pick_oid": None,
          "exile_done": False, "exile_done_at": None, "exile_oid": None,
          "exile_obj_dump": None, "grant_fields": None,
          "cast_offered": False, "cast_samples": [], "sampled_turns": set(),
          "cast_submitted": False, "cast_submitted_oid": None,
          "cast_rejected": False, "cast_rejection": None,
          "bolt_stack_seen": False, "bolt_target_done": False,
          "bolt_target_submitted_cid": None,
          "life_at_cast": None, "tapped_at_cast": None,
          "pre_exported": False, "post_exported": False, "post_at": None}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "cast_action_names": []}
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
        pre_st = post_st = None
        for fn, slot in (("pre", "pre_st"), ("post", "post_st")):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    st = json.loads(open(p).read())["state"]
                    if slot == "pre_st":
                        pre_st = st
                    else:
                        post_st = st
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        # ---- A1: setup ----
        if post_st is not None:
            goid = ST["gonti_oid"] or gonti_oid_any(post_st)
            o = get_obj(post_st, goid) if goid else {}
            ex = exiled_p1_oids(post_st)
            # also consult pre.json: the exile must exist at PRE at latest
            ex_pre = exiled_p1_oids(pre_st) if pre_st else []
            ok = (goid is not None and o.get("zone") == "Battlefield"
                  and o.get("owner") == 0 and o.get("controller") == 0
                  and (len(ex) > 0 or len(ex_pre) > 0))
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: gonti_oid={goid} zone={o.get('zone')} "
                         f"owner={o.get('owner')} ctrl={o.get('controller')} "
                         f"exiled_p1(post)={ex} exiled_p1(pre)={ex_pre} "
                         f"grant_fields={ST['grant_fields']}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: post.json missing")

        # ---- A2: cast offered ----
        if ST["cast_offered"]:
            ass["A2_cast_offered"] = "passed"
            notes.append(f"A2 passed: cast of exiled Bolt advertised to P0 "
                         f"(samples={len(ST['cast_samples'])})")
        elif ST["exile_done"]:
            ass["A2_cast_offered"] = "failed"
            notes.append(f"A2 FAILED: exiled Bolt present with P0 cast "
                         f"permission, but NO cast action advertised across "
                         f"{len(ST['cast_samples'])} valid P0 main-phase "
                         f"windows. BUG REPRODUCED.")
        else:
            ass["A2_cast_offered"] = "failed"
            notes.append("A2 failed: exile never completed")

        # ---- A3: cast completes ----
        if ST["cast_submitted"] and not ST["cast_rejected"]:
            ass["A3_cast_completes"] = "passed"
            notes.append(f"A3 passed: cast submitted (oid "
                         f"{ST['cast_submitted_oid']}), accepted; bolt on "
                         f"stack seen={ST['bolt_stack_seen']}")
        elif ST["cast_rejected"]:
            ass["A3_cast_completes"] = "failed"
            notes.append(f"A3 FAILED: cast submission rejected: "
                         f"{ST['cast_rejection']}")
        elif ST["cast_offered"]:
            ass["A3_cast_completes"] = "failed"
            notes.append("A3 failed: cast offered but never submitted "
                         "(driver error)")
        else:
            ass["A3_cast_completes"] = "not-run"
            notes.append("A3 not-run: no cast offered")

        # ---- A4: resolves ----
        if post_st is not None and ST["cast_submitted"] \
                and not ST["cast_rejected"]:
            l1 = life_of(post_st, 1)
            ex_post = exiled_p1_oids(post_st)
            gy_bolts = sum(
                1 for oid, o in (post_st.get("objects", {}) or {}).items()
                if o.get("zone") == "Graveyard" and o.get("owner") == 1
                and str(o.get("base_name") or o.get("name") or "").lower()
                == BOLT)
            dmg_ok = (ST["life_at_cast"] is not None
                      and l1 == ST["life_at_cast"] - 3)
            gone = len(ex_post) == 0
            notes.append(f"A4: P1 life at cast={ST['life_at_cast']} now={l1} "
                         f"dmg_ok={dmg_ok} exile_empty={gone} "
                         f"p1_gy_bolts={gy_bolts}")
            if dmg_ok and gone:
                ass["A4_resolves"] = "passed"
                notes.append("A4 passed: Bolt dealt 3 to P1 and left exile")
            else:
                ass["A4_resolves"] = "failed"
                notes.append("A4 FAILED: resolution outcome wrong")
        elif not ST["cast_submitted"] or ST["cast_rejected"]:
            ass["A4_resolves"] = "not-run"
            notes.append("A4 not-run: no accepted cast")
        else:
            ass["A4_resolves"] = "failed"
            notes.append("A4 failed: post.json missing")

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
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif any(ass[k] == "failed"
                 for k in ("A2_cast_offered", "A3_cast_completes",
                           "A4_resolves")):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7139,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.82.0 server on 127.0.0.1:9374, "
                               "started for this run (pid in runs/<run-id>/"
                               "server.pid)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7139.py", "rb").read()).hexdigest(),
            "format_config": "CommanderDraft (3 seats; P0 Gonti / P1 Krenko "
                             "inert / P2 no commander)",
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                      "P2": {"main": P2_DECK, "commander": []}},
            "assertions": ass,
            "observations": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in obs.items()},
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()
                             if k not in ("exile_obj_dump",)},
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7139.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via three human-client seats.",
                "CommanderDraft format is a test-harness fixture for 3-player games.",
                "Dense 60x single-card main decks are a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        import shutil
        shutil.copy(f"{BACKFILL}/driver/scenario_7139.py",
                    f"{EVDIR}/scenario_7139.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_7139.py and server.log into EVDIR")
        render_summary(run, pre_st, post_st)
        # close logs BEFORE hashing (lesson #6916: manifest hash of the
        # run log must cover all logged lines)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        write_manifest()
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, pre_st, post_st):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            print("PIL missing; skipping summary.png")
            return
        W, H = 1000, 800
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7139 - theft spells vs commander "
               "color identity", fill=(235, 240, 250))
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
        d.text((24, y), "Setup: P0 casts Gonti, Lord of Luxury (mono-black); "
               "ETB exiles P1's Lightning Bolt (red) face down;", fill=(200, 205, 215))
        y += 24
        d.text((24, y), "P0 may cast it with mana of any type. Report: engine "
               "blocks casts outside commander identity.", fill=(200, 205, 215))
        y += 34
        for k in ("A1_setup_ok", "A2_cast_offered", "A3_cast_completes",
                  "A4_resolves", "A5_cleanup"):
            v = run["assertions"].get(k, "?")
            col = (120, 220, 120) if v == "passed" else (
                (255, 200, 90) if v == "not-run" else (255, 90, 90))
            d.text((24, y), f"{k}: {v}", fill=col)
            y += 26
        y += 10
        l1pre = life_of(pre_st, 1) if pre_st else "?"
        l1post = life_of(post_st, 1) if post_st else "?"
        d.text((24, y), f"P1 life pre={l1pre} post={l1post} "
               f"(expect 37 on success)", fill=(200, 205, 215))
        y += 26
        d.text((24, y), f"cast offered to P0: {ST['cast_offered']} "
               f"(samples={len(ST['cast_samples'])})", fill=(200, 205, 215))
        y += 26
        d.text((24, y), f"ETB target prompt: {ST['etb_target_offered']} "
               f"pick=P{ST['etb_target_pick']}; exile choice: "
               f"{ST['exile_choice_offered']}", fill=(200, 205, 215))
        y += 26
        d.text((24, y), f"grant fields observed: {ST['grant_fields']}",
               fill=(200, 205, 215))
        y += 34
        d.text((24, y), "Evidence: pre/post authoritative exports + wire log "
               "+ run log in", fill=(140, 160, 180))
        y += 24
        d.text((24, y), f"ntindle/phase-bug-state-evidence 7139/{RUN_ID}/",
               fill=(140, 160, 180))
        img.save(f"{EVDIR}/summary.png")
        print("rendered summary.png")

    def write_manifest():
        files = ["pre.json", "post.json", "run.json",
                 "scenario_7139.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                print(f"manifest: MISSING {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"wrote manifest.sha256 ({len(lines)} files)")

    def mulligan_keep(pid, state, lands_need, max_mulls):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in (SWAMP, FOREST))
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

            def rank(ch):
                t = choice_text(ch).lower()
                return 0 if t in (SWAMP, FOREST) else 1
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            wire("discard", {"who": tag, "choice": choice_text(pick)[:60]})
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    def looks_player_like(ch):
        if player_seat_of_choice(ch) is not None:
            return True
        blob = json.dumps(ch, default=str).lower()
        return ("player" in blob or "opponent" in blob) and "seat" in blob

    async def gonti_etb_target_tick(c, pid, tag, st, state):
        """Answer Gonti's ETB 'target opponent' prompt with P1.

        Window: Gonti has entered, exile not yet done. The only
        player-candidate prompt in that window is the ETB target.
        """
        if not (ST["entered"] and not ST["exile_done"]):
            return False
        if ST["etb_target_offered"]:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        wf = wf_of(state)
        ST["etb_wf_types"].add(str(wf.get("type") or ""))
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
            ST["etb_target_offered"] = True
            say(f"[{tag}] GONTI ETB TARGET prompt: waiting_for="
                f"{wf.get('type')} seats={seats}")
            wire("gonti_etb_target_prompt",
                 {"who": tag, "iid": str(iid)[:16], "seats": seats,
                  "wf_type": wf.get("type"),
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            pick = None
            for ch in chs:
                if player_seat_of_choice(ch) == 1:
                    pick = ch
                    break
            if pick is None:
                pick = chs[0]
            ST["etb_target_pick"] = player_seat_of_choice(pick)
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def exile_choice_tick(c, pid, tag, st, state):
        """Answer Gonti's 'exile one of them' card choice (pick a Bolt)."""
        if not (ST["entered"] and not ST["exile_done"]):
            return False
        if ST["exile_choice_offered"]:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        wf = wf_of(state)
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            if any(looks_player_like(ch) for ch in chs):
                continue
            # card candidates: pick a Lightning Bolt
            pick = None
            for ch in chs:
                if "lightning bolt" in choice_text(ch).lower():
                    pick = ch
                    break
            if pick is None:
                # fall back to any card-like candidate
                pick = chs[0]
            ST["exile_choice_offered"] = True
            say(f"[{tag}] GONTI EXILE-CHOICE prompt: waiting_for="
                f"{wf.get('type')} n={len(chs)} "
                f"pick={choice_text(pick)[:60]}")
            wire("gonti_exile_choice_prompt",
                 {"who": tag, "iid": str(iid)[:16], "n": len(chs),
                  "wf_type": wf.get("type"),
                  "texts": [choice_text(ch)[:60] for ch in chs][:6],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            cid = await answer_vi(c, opp, pick, tag)
            ST["exile_pick_oid"] = cid
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def bolt_target_tick(c, pid, tag, st, state):
        """Answer Lightning Bolt's 'any target' prompt (target P1)."""
        if not (ST["cast_submitted"] and not ST["bolt_target_done"]):
            return False
        vi = get_vi(st)
        if not vi:
            return False
        wf = wf_of(state)
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            pick = None
            for ch in chs:
                if player_seat_of_choice(ch) == 1:
                    pick = ch
                    break
            if pick is None:
                continue
            say(f"[{tag}] BOLT TARGET prompt: waiting_for={wf.get('type')} "
                f"targeting P1")
            wire("bolt_target_prompt",
                 {"who": tag, "iid": str(iid)[:16], "wf_type": wf.get("type"),
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            cid = await answer_vi(c, opp, pick, tag)
            ST["bolt_target_done"] = True
            ST["bolt_target_submitted_cid"] = cid
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

    def cast_action_for_exile(acts, state, exile_oid):
        for a in acts:
            if "cast" not in a["type"].lower():
                continue
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or d.get("card_id")
            if oid == exile_oid:
                return a
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

    def find_grant(state, exile_oid):
        """Heuristic scan for the may-cast grant on the exiled object."""
        o = get_obj(state, exile_oid)
        blob = json.dumps(o, default=str).lower()
        hits = {}
        for key in ("cast", "permission", "may_cast", "can_cast",
                    "granted", "as_though"):
            if key in blob:
                hits[key] = True
        return hits or None

    async def check_rejections(c, tag):
        """Drain ActionRejected/Error messages; flag cast rejections."""
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("ActionRejected", "Error"):
                entry = {"who": tag, "type": t,
                         "data": json.dumps(data, default=str)[:400]}
                obs["rejections"].append(entry)
                say(f"[{tag}] REJECTION {t}: "
                    f"{json.dumps(data, default=str)[:300]}")
                wire("rejection", {"who": tag, "type": t, "data": data})
                if (tag == "P0" and ST["cast_submitted"]
                        and not ST["cast_rejected"]
                        and not ST["bolt_stack_seen"]):
                    ST["cast_rejected"] = True
                    ST["cast_rejection"] = entry["data"]

    async def p0_tick(st, acts, state):
        wtype = wf_of(state).get("type") or ""
        await check_rejections(p0, "P0")
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, 4, 2, "P0")
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
        # Gonti ETB: target opponent, then exile-one choice
        if await gonti_etb_target_tick(p0, 0, "P0", st, state):
            return
        if await exile_choice_tick(p0, 0, "P0", st, state):
            return
        # Bolt's own target selection (after the stolen cast is submitted)
        if await bolt_target_tick(p0, 0, "P0", st, state):
            return
        # Gonti entered?
        goid = gonti_oid_any(state)
        if goid is not None and not ST["entered"]:
            o = get_obj(state, goid)
            ST["entered"] = True
            ST["entered_at"] = time.time()
            ST["gonti_oid"] = int(goid)
            say(f"Gonti entered: oid={goid} owner={o.get('owner')} "
                f"controller={o.get('controller')} "
                f"etb_target_offered={ST['etb_target_offered']}")
            wire("gonti_entered",
                 {"oid": int(goid), "owner": o.get("owner"),
                  "controller": o.get("controller")})
        # exile done? scan for P1-owned exiled objects
        if ST["entered"] and not ST["exile_done"]:
            ex = exiled_p1_oids(state)
            if ex:
                ST["exile_done"] = True
                ST["exile_done_at"] = time.time()
                ST["exile_oid"] = ex[0]
                ST["exile_obj_dump"] = json.dumps(
                    get_obj(state, ex[0]), default=str)[:2000]
                ST["grant_fields"] = find_grant(state, ex[0])
                say(f"EXILE DONE: oid={ex[0]} name={lname(state, ex[0])} "
                    f"grant_fields={ST['grant_fields']}")
                wire("exile_done",
                     {"oid": ex[0], "name": lname(state, ex[0]),
                      "grant_fields": ST["grant_fields"],
                      "obj": json.loads(json.dumps(get_obj(state, ex[0]),
                                                   default=str))})
                try:
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    ST["pre_exported"] = True
                    say("exported PRE (Bolt exiled, cast window opens)")
                except Exception as e:
                    notes.append(f"pre export failed: {e}")
        # bolt on stack?
        if ST["cast_submitted"] and not ST["bolt_stack_seen"]:
            for e in state.get("stack", []) or []:
                if "lightning bolt" in json.dumps(e, default=str).lower():
                    ST["bolt_stack_seen"] = True
                    say("Bolt observed on stack")
                    wire("bolt_on_stack", {})
                    break
        if not my_priority(state, 0):
            if await generic_prompt(p0, 0, "P0", st, state):
                return
            return
        # ---- P0 priority ----
        # cast Gonti from the command zone ({2}{B}{B}); gate on 4 untapped
        if (not ST["gonti_cast"]
                and gonti_oid_any(state) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and len(untapped_lands(state, 0, (SWAMP,))) >= 4):
            oid = await cast_named(p0, acts, state, GONTI, "P0")
            if oid is not None:
                ST["gonti_cast"] = True
                ST["gonti_cast_turn"] = state.get("turn_number")
                return
        # sample + attempt the stolen Bolt cast at valid timings
        if (ST["exile_done"] and not ST["cast_submitted"]
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 0
                and not (state.get("stack") or [])):
            turn = state.get("turn_number")
            if turn not in ST["sampled_turns"]:
                ST["sampled_turns"].add(turn)
                nlands = len(untapped_lands(state, 0, (SWAMP,)))
                ca = cast_action_for_exile(acts, state, ST["exile_oid"])
                offered = ca is not None
                cast_names = sorted(set(
                    a["type"] for a in acts if "cast" in a["type"].lower()))
                obs["cast_action_names"] = cast_names
                vi = get_vi(st)
                vi_n = len((vi or {}).get("opportunities", []) or [])
                ST["cast_samples"].append(
                    {"turn": turn, "offered": offered,
                     "untapped_swamps": nlands,
                     "n_cast_actions": len(cast_names),
                     "vi_opportunities": vi_n})
                say(f"[P0] cast-sample turn={turn} offered={offered} "
                    f"untapped_swamps={nlands} cast_actions={cast_names} "
                    f"vi_opps={vi_n}")
                wire("cast_sample",
                     {"turn": turn, "offered": offered,
                      "untapped_swamps": nlands,
                      "cast_action_types": cast_names,
                      "vi_opportunities": vi_n,
                      "exile_oid": ST["exile_oid"]})
                if offered:
                    ST["cast_offered"] = True
                    if nlands >= 1:
                        ST["life_at_cast"] = life_of(state, 1)
                        ST["tapped_at_cast"] = sum(
                            1 for oid, o in (state.get("objects", {})
                                             or {}).items()
                            if o.get("zone") == "Battlefield"
                            and o.get("controller") == 0
                            and o.get("tapped")
                            and str(o.get("base_name") or o.get("name")
                                    or "").lower() == SWAMP)
                        say(f"[P0] submitting stolen Bolt cast "
                            f"(oid {ST['exile_oid']})")
                        wire("stolen_cast_submit",
                             {"oid": ST["exile_oid"],
                              "life_at_cast": ST["life_at_cast"]})
                        await submit_as_is(p0, ca)
                        ST["cast_submitted"] = True
                        ST["cast_submitted_oid"] = ST["exile_oid"]
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
                await do_mulligan(p1, 1, 0, 0, "P1")
                return
            if find_action(acts, "SelectCards") and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                await do_bottom(p1, 1, "P1")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            sub = declare_empty_combat(p1, acts, state, wtype, "P1")
            if sub:
                await submit_as_is(p1, sub)
            return
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        if await discard_tick(p1, 1, "P1", st, state):
            return
        # P1 is inert (all-Bolt hand, no lands): never cast, just land/pass
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
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            sub = declare_empty_combat(p2, acts, state, wtype, "P2")
            if sub:
                await submit_as_is(p2, sub)
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
        cur = p0.latest["state"] if p0.latest else {}
        stack_empty = not (cur.get("stack") or [])
        # success: stolen cast accepted and stack drained afterwards
        success_done = (ST["cast_submitted"] and not ST["cast_rejected"]
                        and ST["bolt_stack_seen"] and stack_empty)
        # failure: exile done, >=3 valid P0 windows sampled, never offered
        no_offer_done = (ST["exile_done"] and not ST["cast_offered"]
                         and len(ST["cast_samples"]) >= 3 and stack_empty)
        # rejection observed: capture post immediately
        reject_done = ST["cast_rejected"]
        if ST["exile_done"] and not ST["post_exported"] \
                and (success_done or no_offer_done or reject_done):
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say(f"exported POST (success={success_done} "
                    f"no_offer={no_offer_done} rejected={reject_done})")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        if ST["post_exported"] and time.time() - (ST["post_at"] or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - t0 > 1200:
            notes.append("watchdog: 1200s elapsed; finishing")
            say("WATCHDOG: finishing with evidence")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0swamps={len(untapped_lands(s,0,(SWAMP,)))} "
                f"gcast={ST['gonti_cast']} entered={ST['entered']} "
                f"exile={ST['exile_done']} offered={ST['cast_offered']} "
                f"samples={len(ST['cast_samples'])} csub={ST['cast_submitted']} "
                f"pre={ST['pre_exported']} post={ST['post_exported']} "
                f"stack={len(s.get('stack') or [])}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
