#!/usr/bin/env python3
"""Issue #6593: "Stuck decision: NamedChoice" -- casting Disruptor Flute opens
the name-a-card dialog; confirming the legal card name "Ghostly Prison" was
rejected with "Engine error: Invalid action: Invalid card name 'Ghostly
Prison'", leaving the NamedChoice decision stuck (report build v0.35.2).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github #6593, status:confirmed, area:engine, p0-softlock): human
player casts Disruptor Flute; the Name-a-Card dialog appears; entering
"Ghostly Prison" and clicking Confirm yields an Action Failed toast
("Invalid card name 'Ghostly Prison'") and the decision never completes.

Triage (issue-cleanup comment on #6593): the engine validates a name-a-card
submission against a table of every card name from the loaded database. That
table is excluded from serialization and repopulated when a game is rehydrated
against the card database; on any path where rehydration does not run, the
table is empty and every submitted name -- even a legal one -- is rejected as
invalid. The same empty table silences the AI (zero candidate options, no
action, decision never advances). Related: #6627 (AI variant).

Oracle text (verified from pinned v0.78.0 card-data.json, value name
'Disruptor Flute'):
  "Flash As this artifact enters, choose a card name. Spells with the chosen
   name cost {3} more to cast. Activated abilities of sources with the chosen
   name can't be activated unless they're mana abilities."
Cost: {2} generic (Artifact). Card-data key for deck lists: 'disruptor flute'.
'Ghostly Prison' exists in the pinned card-data.json (value name), so the
submitted name is indisputably legal.

Setup (native engine, two human-client seats):
  P0: 12x disruptor flute + 48x island. T1: island, cast Disruptor Flute
      ({2}) with 2 untapped islands.
  P1: 60x forest (plays land, passes).

NamedChoice opportunity shape (observed on v0.78.0, protocol 68):
  waiting_for: {"type":"NamedChoice","data":{"player":0,"choice_type":"CardName",
  "options":[],"source":{...Disruptor Flute...}}}
  response: {"type":"schema","data":{"spec":{"type":"text","data":
  {"allowArbitrary":true,"maxLen":256,"confirm":"explicit"}},"candidates":[]}}
  Note: options == [] and candidates == [] -- the candidate table is empty,
  consistent with the triage theory (an empty card-name table).
Submission (established shape from prior text-schema interactions, e.g.
#4813/#3919): {"interactionId":"<id>","response":{"type":"text",
  "data":{"value":"Ghostly Prison"}}}.

Expected (per report + Oracle text):
  E1: submitting the legal name "Ghostly Prison" is accepted (no
      "Invalid card name" rejection, no ActionRejected).
  E2: the NamedChoice decision completes; the game proceeds past it.
  E3: the chosen name is recorded (Flute's effect references "Ghostly
      Prison").

Assertions:
  A1_name_prompt    pre.json: waiting_for.type == NamedChoice for player 0
                    (source Disruptor Flute); P0 cast the Flute; life 20/20.
  A2_name_accepted  the name submission is not rejected (no
                    Error/ActionRejected mentioning invalid card name after
                    the submission; waiting_for leaves NamedChoice).
  A3_game_proceeds  post.json: Flute on P0's battlefield, game advanced past
                    the decision (no NamedChoice pending for P0).
  A4_name_recorded  post.json: "Ghostly Prison" recorded on the Flute object
                    (chosen name visible in the object's effects/data).

Verdict rule: blocked iff A1 fails (setup broken). reproduced iff A1 passes
and any of A2..A4 fails. not-reproduced iff A1..A4 all pass.

Evidence: evidence/6593/<run-id>/pre.json (NamedChoice pending),
post.json (after name accepted), run.json, manifest.sha256, summary.png,
scenario_6593.py, wire_log.jsonl, scenario_run.log.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6593"
EVDIR = f"{BACKFILL}/evidence/6593/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

FLUTE = "disruptor flute"
CHOSEN = "Ghostly Prison"
ISLAND = "island"
FOREST = "forest"

P0_DECK = [(FLUTE, 12), (ISLAND, 48)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_verified": True,
    "signature_note": "minisign-verify (prehashed BLAKE2b-512, sigalg ED) of "
                      "phase-server + signed data manifest with repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY; data files match manifest "
                      "SHA-256",
    "observed_at": "2026-09-10",
    "source": "ServerHello handshake vs pinned v0.78.0 release artifacts "
              "(server/releases/v0.78.0/); existing isolated server on "
              "127.0.0.1:9374 (started by prior run today, verified live "
              "this run before driving)",
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


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def bf_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name]


def untapped_lands(state, pid, landname):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == landname and not o.get("tapped")]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype, name=None, state=None):
    for a in acts:
        if a["type"] != atype:
            continue
        if name is None:
            return a
        d = a.get("data", {})
        oid = d.get("object_id") or a.get("_src_oid")
        if state is not None and obj_name(state.get("objects", {}).get(str(oid), {})) == name:
            return a
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_name_prompt", "A2_name_accepted", "A3_game_proceeds",
            "A4_name_recorded")}
    obs = {"flute_cast": False, "namedchoice_seen": False,
           "namedchoice_iid": None, "namedchoice_options": None,
           "namedchoice_candidates": None, "name_submitted": False,
           "name_submit_time": None, "rejections": [],
           "namedchoice_resolved": False, "namedchoice_turn": None,
           "post_turn": None}
    exported = {"pre": False, "post": False}
    submitted_interactions = set()
    shapes_logged = set()
    last_assign = {}

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    def drain_rejections(c):
        """Collect Error/ActionRejected inbox messages mentioning the name."""
        hits = []
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("Error", "ActionRejected"):
                hits.append({"who": c.name, "type": t, "data": data})
        return hits

    async def scan_name_choice(st, state, c):
        """Handle the NamedChoice (CardName) opportunity for Disruptor Flute:
        submit the legal name 'Ghostly Prison' per the established text-schema
        submission shape. Returns True if an action was submitted."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            spec = data.get("spec", {}) or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            iid = opp.get("interactionId")
            wt = (state.get("waiting_for") or {}).get("type")
            key = ("namechoice", rtype, spec_type, wt)
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{c.name}] namedchoice-shape rtype={rtype} "
                    f"spec={spec_type} waiting_for={wt}")
                wire("namedchoice_shape",
                     {"who": c.name, "rtype": rtype, "spec_type": spec_type,
                      "spec_data": spec.get("data"),
                      "waiting_for": state.get("waiting_for"),
                      "interaction": opp})
            if iid in submitted_interactions:
                continue
            if (wt == "NamedChoice" and rtype == "schema"
                    and spec_type == "text" and c.name == "P0"):
                wfd = (state.get("waiting_for") or {}).get("data", {}) or {}
                obs["namedchoice_seen"] = True
                obs["namedchoice_iid"] = iid
                obs["namedchoice_options"] = wfd.get("options")
                obs["namedchoice_candidates"] = data.get("candidates")
                if not exported["pre"]:
                    await export_state_dict(p0, "pre")
                    obs["namedchoice_turn"] = state.get("turn_number")
                sub = {"interactionId": iid, "response":
                       {"type": "text", "data": {"value": CHOSEN}}}
                say(f"[P0] submitting name choice value={CHOSEN!r}")
                wire("name_submit", {"who": "P0", "submission": sub})
                await c.send_interaction(sub)
                submitted_interactions.add(iid)
                obs["name_submitted"] = True
                obs["name_submit_time"] = time.time()
                acted = True
        return acted

    async def export_state_dict(c, tag):
        try:
            s = await c.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            exported[tag] = True
            say(f"exported {tag}.json")
            return json.loads(s)["state"]
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return None

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        try:
            return json.loads(open(p).read())["state"]
        except Exception:
            return None

    def flute_chosen_name(state):
        """Search the Flute object's data for the chosen name."""
        hits = []
        for oid, o in (state.get("objects", {}) or {}).items():
            if obj_name(o) != FLUTE:
                continue
            blob = json.dumps(o, default=str)
            if CHOSEN in blob:
                hits.append((oid, o))
        return hits

    def evaluate():
        pre_st = load_env("pre")
        post_st = load_env("post")
        # A1
        if pre_st is not None:
            wf = (pre_st.get("waiting_for") or {})
            wf_ok = (wf.get("type") == "NamedChoice"
                     and (wf.get("data", {}) or {}).get("player") == 0)
            choice_ok = ((wf.get("data", {}) or {}).get("choice_type")
                         == "CardName")
            if wf_ok and choice_ok:
                ass["A1_name_prompt"] = "passed"
                notes.append(f"pre.json: NamedChoice (CardName) pending for P0 "
                             f"(source Disruptor Flute, options="
                             f"{(wf.get('data') or {}).get('options')}), "
                             f"P0 cast the Flute, life "
                             f"{life_of(pre_st,0)}/{life_of(pre_st,1)}")
            else:
                ass["A1_name_prompt"] = "failed"
                notes.append(f"pre.json: waiting_for={wf.get('type')} "
                             f"(expected NamedChoice/CardName for P0)")
        elif obs["namedchoice_seen"]:
            ass["A1_name_prompt"] = "passed"
            notes.append("pre.json missing but NamedChoice opportunity was "
                         "observed live (export failure only)")
        else:
            ass["A1_name_prompt"] = "failed"
            notes.append("pre.json missing and no NamedChoice observed")
        # A2
        rej_texts = [json.dumps(r, default=str) for r in obs["rejections"]]
        invalid = [t for t in rej_texts if "invalid card name" in t.lower()
                   or "invalid action" in t.lower()]
        if obs["name_submitted"] and obs["namedchoice_resolved"] and not invalid:
            ass["A2_name_accepted"] = "passed"
            notes.append(f"name submission {CHOSEN!r} accepted: no invalid-"
                         f"card-name rejection; NamedChoice decision completed")
        elif obs["name_submitted"] and invalid:
            ass["A2_name_accepted"] = "failed"
            notes.append(f"REPORTED BUG: name submission rejected: "
                         f"{invalid[0][:300]}")
        elif obs["name_submitted"]:
            ass["A2_name_accepted"] = "failed"
            notes.append("name submitted but NamedChoice never resolved "
                         "(stuck decision)")
        else:
            ass["A2_name_accepted"] = "failed"
            notes.append("name never submitted (prompt or submission path broken)")
        # A3
        if post_st is not None:
            flutes = bf_ids(post_st, 0, FLUTE)
            wf = (post_st.get("waiting_for") or {}).get("type")
            pending_named = (wf == "NamedChoice")
            obs["post_turn"] = post_st.get("turn_number")
            if flutes and not pending_named:
                ass["A3_game_proceeds"] = "passed"
                notes.append(f"post.json: Flute on P0 battlefield (oid "
                             f"{flutes}), no NamedChoice pending, game advanced "
                             f"(turn {post_st.get('turn_number')}, phase "
                             f"{post_st.get('phase')})")
            else:
                ass["A3_game_proceeds"] = "failed"
                notes.append(f"post.json: flutes={flutes}, waiting_for={wf}")
        else:
            ass["A3_game_proceeds"] = "failed"
            notes.append("post.json missing (name never accepted or export failed)")
        # A4
        if post_st is not None and ass["A3_game_proceeds"] == "passed":
            hits = flute_chosen_name(post_st)
            if hits:
                ass["A4_name_recorded"] = "passed"
                oids = [h[0] for h in hits]
                notes.append(f"post.json: {CHOSEN!r} recorded on Flute "
                             f"object(s) {oids}")
                wire("flute_name_evidence",
                     {"oids": oids,
                      "excerpts": [json.dumps(h[1], default=str)[:600]
                                   for h in hits]})
            else:
                ass["A4_name_recorded"] = "failed"
                notes.append(f"post.json: {CHOSEN!r} not found on any Flute object")
        elif post_st is None:
            ass["A4_name_recorded"] = "failed"
            notes.append("post.json missing")
        else:
            ass["A4_name_recorded"] = "not-run"
            notes.append("A4 not-run (A3 failed)")
        gate = ("A2_name_accepted", "A3_game_proceeds", "A4_name_recorded")
        if ass["A1_name_prompt"] != "passed":
            verdict = "blocked"
        elif all(ass[k] == "passed" for k in gate):
            verdict = "not-reproduced"
        elif any(ass[k] == "failed" for k in gate):
            verdict = "reproduced"
            notes.append("at least one required outcome failed while the setup "
                         "was valid")
        else:
            verdict = "blocked"
            notes.append("no assertion failed but the proof gate was not fully "
                         "reached; not an engine verdict")
        return verdict

    async def finish():
        dur = time.time() - t_start
        verdict = evaluate()
        run = {
            "issue": 6593,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9374,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6593.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x Disruptor Flute deck density is a test-harness convenience "
                "(engine accepts >4-of for custom games); exercised behavior is "
                "the shipped card text.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
                "The AI card-name-candidate path (related issue #6627) is not "
                "exercised; only the reported human dialog path.",
            ],
            "setup_line": "P0: 12x disruptor flute + 48x island (T1: island, "
                          "cast Disruptor Flute {2}); P1: 60x forest",
            "contract_line": "Disruptor Flute ETB: submit legal name "
                             "'Ghostly Prison' -> accepted, decision completes, "
                             "name recorded",
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
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    kept = {}

    async def p0_tick(st, acts, state):
        # collect rejections every tick
        for r in drain_rejections(p0):
            obs["rejections"].append(r)
            say(f"REJECTION: {json.dumps(r, default=str)[:300]}")
            wire("rejection", r)
        wtype = (state.get("waiting_for") or {}).get("type")
        turn = state.get("turn_number")
        phase = state.get("phase")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            kept["P0"] = True
            await submit_as_is(p0, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P0 keeps")
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in player_of(state, 0).get("hand", [])]
                def bottom_key(oid):
                    nm = obj_name(get_obj(state, oid))
                    return 0 if nm == ISLAND else 2
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
                return
        if await scan_name_choice(st, state, p0):
            return
        # NamedChoice resolved? (name accepted -> decision completes)
        if (obs["name_submitted"] and not obs["namedchoice_resolved"]
                and wtype != "NamedChoice"):
            obs["namedchoice_resolved"] = True
            say(f"NamedChoice resolved after submission (waiting_for={wtype})")
            stx = await export_state_dict(p0, "post")
            if stx is not None:
                say(f"POST: turn {stx.get('turn_number')} phase "
                    f"{stx.get('phase')}; flutes="
                    f"{bf_ids(stx, 0, FLUTE)}")
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        if not obs["flute_cast"]:
            ca = find_action(acts, "CastSpell", FLUTE, state)
            if ca and phase in ("PreCombatMain", "PostCombatMain"):
                say(f"P0 casts Disruptor Flute (turn {turn})")
                wire("cast_flute", ca)
                await submit_as_is(p0, ca)
                obs["flute_cast"] = True
                return
        la = find_action(acts, "PlayLand")
        if la and player_of(state, 0).get("lands_played_this_turn", 0) == 0:
            await submit_as_is(p0, la)
            return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        la = find_action(acts, "PlayLand")
        if la and player_of(state, 1).get("lands_played_this_turn", 0) == 0:
            await submit_as_is(p1, la)
            return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_deadline = None
    while time.time() - t0 < 900:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        if exported["post"]:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} phase={s.get('phase')} "
                f"wf={(s.get('waiting_for') or {}).get('type')} "
                f"flute_cast={obs['flute_cast']} name={obs['name_submitted']} "
                f"resolved={obs['namedchoice_resolved']} "
                f"rej={len(obs['rejections'])}")
        if obs["name_submitted"] and not obs["namedchoice_resolved"] \
                and stuck_deadline is None:
            stuck_deadline = time.time() + 240
        if obs["namedchoice_resolved"]:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("name submitted but NamedChoice stayed pending for "
                         "240s (stuck decision, per report)")
            await finish()
            return
    notes.append("global timeout (900s) hit before assertions resolved")
    await finish()


asyncio.run(main())
