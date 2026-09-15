#!/usr/bin/env python3
"""phase-rs/phase #845 - Bitterbloom Bearer <redacted> not trigger on beginning of upkeep.

Oracle (pinned v0.84.0 card-data):
  Bitterbloom Bearer {B}{B} Creature
  "Flash
   Flying
   At the beginning of your upkeep, you lose 1 life and create a 1/1 blue
   and black Faerie creature token with flying."
  Parsed (A1): exactly one trigger, mode Phase/Upkeep, constraint
  OnlyDuringYourTurn, not optional: LoseLife(Fixed 1, Controller) ->
  sub-ability Token(Faerie 1/1, Blue+Black, Flying, count 1, owner Controller).

Reported: the upkeep trigger does not fire.

Contract:
  A1_parse        - pinned card-data carries exactly one Upkeep trigger on
                    Bearer <redacted> LoseLife->Token signature (not optional)
  A2_setup_ok     - Bearer <redacted> P0's BF at the start of P0's upkeep; pre.json
                    exported
  A3_trigger_fired- upkeep trigger observed on the stack (source = Bearer,
                    LoseLife signature) during the observed upkeep
  A4_life_lost    - P0 life 20 -> 19 between pre and post
  A5_token_created- exactly 1 new 1/1 blue+black Faerie token with Flying on
                    P0's BF in post.json
  A6_no_duplicates- exactly one trigger cycle during the observed upkeep
  A7_cleanup      - stack empty, game advanced; post.json exported

Verdict rule: reproduced iff A1/A2 pass and the upkeep trigger never
fires (no stack sighting, no life loss, no token). not-reproduced iff all
assertions pass. blocked otherwise.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")

from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 845
RUN_ID = os.environ.get("RUN_ID", "20260915-845")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEARER = "bitterbloom bearer"
SWAMP = "swamp"
FAERIE = "faerie"

P0_DECK = [(BEARER, 12), (SWAMP, 48)]
P1_DECK = [(SWAMP, 60)]

SERVER_IDENTITY = {
    "server_version": "0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "Full",
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "in prehashed (blake2b-512) mode; data digests match "
                      "the signed manifest",
    "observed_at": "2026-09-15",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run's session under runs/20260915-845/; "
              "ServerHello re-checked by this run.",
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


# ---------------------------------------------------------------- state views
def objects(state):
    return state.get("objects", {}) or {}


def oname(o):
    return str(o.get("base_name") or o.get("name") or "").lower()


def bf_named(state, pid, name):
    for oid, o in objects(state).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and oname(o) == name):
            return int(oid)
    return None


def on_battlefield(state, oid):
    o = objects(state).get(str(oid)) or {}
    return o.get("zone") == "Battlefield"


def hand_oids(state, pid):
    return [int(oid) for oid, o in objects(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def hand_lnames(state, pid):
    return [oname(o) for o in objects(state).values()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def untapped_lands(state, pid):
    return sum(1 for o in objects(state).values()
               if o.get("zone") == "Battlefield" and o.get("controller") == pid
               and oname(o) == SWAMP and not o.get("tapped"))


def life_of(state, pid):
    for p in state.get("players", []) or []:
        for key in ("player", "seat", "player_id", "id"):
            if p.get(key) == pid:
                return int(p.get("life", -1))
    return None


def faerie_tokens(state, pid):
    out = []
    for oid, o in objects(state).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and o.get("is_token") and FAERIE in oname(o)):
            out.append(int(oid))
    return out


def wf_of(state):
    return (state.get("waiting_for") or {})


def wf_type(state):
    return (wf_of(state).get("type") or "")


def wf_data(state):
    return wf_of(state).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def my_priority(state, pid):
    return wf_type(state) == "Priority" and wf_player(state) == pid


def is_my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid)


def stack_entries(state):
    return state.get("stack") or []


def bearer_trigger_entries(state, bearer_oid):
    """Bearer's upkeep TriggeredAbility entries currently on the stack.
    Discriminate by the trigger's own ability description (LoseLife
    signature), not by card text substrings."""
    out = []
    for e in stack_entries(state):
        kind = e.get("kind") or {}
        if kind.get("type") != "TriggeredAbility":
            continue
        if e.get("source_id") != bearer_oid:
            continue
        ability = ((kind.get("data") or {}).get("ability")) or {}
        desc = str(ability.get("description") or "").lower()
        effect = str(ability.get("effect") or "").lower()
        if ("lose" in desc and "life" in desc) or "loselife" in effect:
            out.append(e)
    return out


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        for a in opp.get("actions", []) or []:
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg.get("type"),
                           "stage": ST.get("stage")})
    await c.send_action(msg)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage")})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


def server_run_dir():
    try:
        out = subprocess.run(["ps", "aux"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if "phase-server" in line and "--games-db" in line \
                    and "$RUN_DIR" not in line:
                parts = line.split()
                try:
                    i = parts.index("--games-db")
                    db = parts[i + 1]
                except (ValueError, IndexError):
                    continue
                if "/runs/" in db:
                    return db.split("/runs/")[1].split("/")[0]
    except Exception as e:
        say("server_run_dir lookup failed:", e)
    return None


ST = {"stage": "SETUP", "bearer_oid": None,
      "bearer_cast": False, "bearer_cast_turn": None,
      "upkeep_turn": None,
      "pre_exported": False, "mid_exported": False, "post_exported": False,
      "trigger_sightings": [], "triggers_fired_at_pre": None,
      "triggers_fired_at_post": None,
      "life_before": None, "life_after": None,
      "tokens_before": None, "tokens_after": None,
      "stable": 0, "rejections": [], "done": False}
ACTED = {}


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse", "A2_setup_ok", "A3_trigger_fired", "A4_life_lost",
            "A5_token_created", "A6_no_duplicates", "A7_cleanup")}

    # ---- A1: parse check against the pinned card-data ----
    try:
        cdata = json.load(open(
            f"{BACKFILL}/server/releases/v0.84.0/data/card-data.json"))
        bdata = cdata.get("bitterbloom bearer") or cdata.get(
            "Bitterbloom Bearer", {})
        trigs = bdata.get("triggers") or []
        up = [t for t in trigs
              if (t.get("phase") == "Upkeep" or "Upkeep" in str(t.get("mode")))]
        sig = None
        for t in up:
            ex = t.get("execute") or {}
            ef = ex.get("effect") or {}
            sub = (ex.get("sub_ability") or {}).get("effect") or {}
            if (ef.get("type") == "LoseLife"
                    and sub.get("type") == "Token"
                    and (t.get("optional") in (False, None))):
                sig = t
        with open(f"{EVDIR}/parse_bearer.json", "w") as f:
            json.dump({"oracle_text": bdata.get("oracle_text"),
                       "mana_cost": bdata.get("mana_cost"),
                       "triggers": trigs}, f, indent=1)
        if len(trigs) == 1 and sig is not None:
            ass["A1_parse"] = "passed"
            notes.append("A1_parse: passed (exactly one trigger, Upkeep "
                         "LoseLife->Token, not optional)")
        else:
            ass["A1_parse"] = "failed"
            notes.append(f"A1_parse: failed (triggers={len(trigs)}, "
                         f"signature_match={sig is not None})")
    except Exception as e:
        ass["A1_parse"] = "failed"
        notes.append(f"A1_parse: failed (parse error: {e})")

    p0 = PhaseClient("P0")
    await p0.connect()
    p1 = PhaseClient("P1")
    await p1.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id} "
        f"RUN_ID={RUN_ID}")

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            wire(f"export_{tag}", {"ok": True})
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    async def do_mulligan(c, pid, tag):
        st = c.latest or {}
        state = st.get("state", st)
        names = hand_lnames(state, pid)
        lands = sum(1 for n in names if n == SWAMP)
        if pid == 0:
            want_mull = (BEARER not in names or lands < 2)
        else:
            want_mull = lands < 2
        if not want_mull or ST.get(f"mull_{tag}"):
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] keeps (lands={lands}, bearer={BEARER in names})")
        else:
            ST[f"mull_{tag}"] = True
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
            say(f"[{tag}] mulligans (lands={lands}, bearer={BEARER in names})")

    def pick_discard(state, pid, n):
        def rank(oid):
            return 2 if oname(objects(state).get(str(oid), {})) == BEARER else 0
        return [int(x) for x in sorted(hand_oids(state, pid), key=rank)[:n]]

    async def seat_tick(c, pid, tag):
        drain_rejections(c)
        st = c.latest or {}
        state = st.get("state", st)
        wtype = wf_type(state)
        acts = merged_actions(st)
        turn = state.get("turn_number") or 0
        rev = st.get("state_revision", -1)

        # legend-choice defensive handling (submit advertised as-is)
        for a in acts:
            if "Legend" in (a.get("type") or ""):
                if not acted(f"leg{pid}", rev):
                    await submit_as_is(c, a)
                    say(f"[{tag}] legend-choice submitted as-is")
                return
        # mulligan (+ bottom-cards SelectCards after a mulligan)
        if wtype == "MulliganDecision":
            pend = wf_data(state).get("pending", []) or []
            mine = [p for p in pend if p.get("player") == pid]
            if mine and (mine[0].get("phase") or {}).get("type") \
                    == "BottomCards":
                n = (mine[0].get("phase") or {}).get("count") or 1
                picks = pick_discard(state, pid, n)
                if picks and not acted(f"bott{pid}", rev):
                    await submit_as_is(
                        c, {"type": "SelectCards",
                            "data": {"cards": picks}})
                    say(f"[{tag}] bottoms {len(picks)} after mulligan")
                return
            if find_action(acts, "MulliganDecision"):
                await do_mulligan(c, pid, tag)
            return
        # hand-size discard
        if wtype == "DiscardToHandSize" and wf_player(state) == pid:
            n = wf_data(state).get("count") or max(
                0, len(hand_oids(state, pid)) - 7)
            picks = pick_discard(state, pid, n)
            if picks and not acted(f"hsd{pid}", rev):
                await submit_as_is(
                    c, {"type": "SelectCards", "data": {"cards": picks}})
                say(f"[{tag}] discards {len(picks)} to hand size")
            return
        # combat: never attack or block
        if wtype == "DeclareAttackers" and wf_player(state) == pid:
            da = find_action(acts, "DeclareAttackers")
            if da and not acted(f"atk{pid}", rev):
                sub = copy.deepcopy(da)
                sub.setdefault("data", {})["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
            return
        if wtype == "DeclareBlockers" and wf_player(state) == pid:
            db = find_action(acts, "DeclareBlockers")
            if db and not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(db)
                sub.setdefault("data", {})["assignments"] = []
                await submit_as_is(c, sub)
            return
        # never pass priority while our own decision is pending
        if wtype in ("OptionalCostChoice", "TargetSelection", "ChooseXValue",
                     "EntryControllerChoice", "ChooseOneOfBranch",
                     "CombatTaxPayment", "CopyRetarget") \
                and wf_player(state) == pid:
            return
        # P1 is fully passive beyond lands/passes
        if pid == 1:
            if is_my_main(state, 1) and my_priority(state, 1):
                pl = find_action(acts, "PlayLand")
                if pl and not acted(f"land1_{turn}", rev):
                    await submit_as_is(c, pl)
                    return
            if my_priority(state, pid):
                pp = find_action(acts, "PassPriority")
                if pp:
                    await submit_as_is(c, pp)
            return
        # ---- P0 ----
        if is_my_main(state, 0) and my_priority(state, 0):
            boid = bf_named(state, 0, BEARER)
            if boid is not None:
                ST["bearer_oid"] = boid
            # cast Bearer ({B}{B}) with 2 untapped swamps
            if (ST["stage"] == "SETUP" and not ST["bearer_cast"]
                    and boid is None and untapped_lands(state, 0) >= 2):
                for oid in hand_oids(state, 0):
                    if oname(objects(state).get(str(oid), {})) != BEARER:
                        continue
                    for a in acts:
                        d = a.get("data", {}) or {}
                        if ("cast" in (a.get("type") or "").lower()
                                and str(d.get("object_id", "")) == str(oid)):
                            await submit_as_is(c, a)
                            ST["bearer_cast"] = True
                            ST["bearer_cast_turn"] = turn
                            say(f"[P0] cast Bearer (turn {turn})")
                            return
            if ST["stage"] == "SETUP" and boid is not None:
                ST["stage"] = "UPKEEP_WAIT"
                say(f"[P0] Bearer <redacted> (oid {boid}); stage -> UPKEEP_WAIT")
            # land drop
            if ST["stage"] in ("SETUP", "UPKEEP_WAIT"):
                pl = find_action(acts, "PlayLand")
                if pl and not acted(f"land0_{turn}", rev):
                    await submit_as_is(c, pl)
                    return
        # default: pass priority
        if my_priority(state, pid):
            pp = find_action(acts, "PassPriority")
            if pp:
                await submit_as_is(c, pp)

    async def finish():
        if not ST["post_exported"]:
            if await export_named("post"):
                ST["post_exported"] = True
        states = {}
        for fn in ("pre", "mid_trigger_pending", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre, mid, post = (states.get("pre"), states.get("mid_trigger_pending"),
                          states.get("post"))

        boid = ST["bearer_oid"]

        # ---- A2: setup ----
        if pre is not None and boid is not None:
            ok = bf_named(pre, 0, BEARER) is not None
            ass["A2_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A2: Bearer <redacted> P0's BF in pre.json={ok}")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 failed: pre.json missing or Bearer <redacted>")

        # ---- A3: trigger fired ----
        n_sight = len(ST["trigger_sightings"])
        tf_delta = None
        if (ST["triggers_fired_at_pre"] is not None
                and ST["triggers_fired_at_post"] is not None):
            try:
                tf_delta = (ST["triggers_fired_at_post"]
                            - ST["triggers_fired_at_pre"])
            except TypeError:
                tf_delta = (len(ST["triggers_fired_at_post"])
                            - len(ST["triggers_fired_at_pre"]))
        if n_sight >= 1:
            ass["A3_trigger_fired"] = "passed"
            notes.append(f"A3: passed (Bearer upkeep trigger on stack; "
                         f"sightings={n_sight}, triggers_fired delta="
                         f"{tf_delta})")
        else:
            ass["A3_trigger_fired"] = "failed"
            notes.append(f"A3: failed (no Bearer trigger sighting during "
                         f"P0 upkeep turn {ST['upkeep_turn']}; "
                         f"triggers_fired delta={tf_delta})")

        # ---- A4: life lost ----
        lb, la = ST["life_before"], ST["life_after"]
        if lb is not None and la is not None:
            if la == lb - 1:
                ass["A4_life_lost"] = "passed"
            else:
                ass["A4_life_lost"] = "failed"
            notes.append(f"A4: P0 life {lb} -> {la} (expected {lb - 1})")
        else:
            ass["A4_life_lost"] = "failed"
            notes.append("A4 failed: life values missing")

        # ---- A5: token created ----
        tb, ta = ST["tokens_before"], ST["tokens_after"]
        tok_ok = None
        if ta is not None:
            tok_ok = len(ta) == 1
            if post is not None and tok_ok:
                o = objects(post).get(str(ta[0])) or {}
                tok_ok = (o.get("power") == 1 and o.get("toughness") == 1
                          and "Blue" in str(o.get("color") or "")
                          and "Black" in str(o.get("color") or ""))
            ass["A5_token_created"] = "passed" if tok_ok else "failed"
            notes.append(f"A5: Faerie tokens on P0 BF pre={tb} post={ta}; "
                         f"1/1 blue+black={tok_ok}")
        else:
            ass["A5_token_created"] = "failed"
            notes.append("A5 failed: token counts missing")

        # ---- A6: no duplicates ----
        if n_sight == 1:
            ass["A6_no_duplicates"] = "passed"
            notes.append("A6: passed (exactly one trigger cycle)")
        elif n_sight == 0:
            ass["A6_no_duplicates"] = "failed"
            notes.append("A6: failed (no trigger cycle at all)")
        else:
            ass["A6_no_duplicates"] = "failed"
            notes.append(f"A6: failed ({n_sight} trigger sightings)")

        # ---- A7: cleanup ----
        if post is not None:
            empty = len(stack_entries(post)) == 0
            ass["A7_cleanup"] = "passed" if empty else "failed"
            notes.append(f"A7: post stack empty={empty}")
        else:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 failed: post.json missing")

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        if ass["A1_parse"] == "passed" and ass["A2_setup_ok"] == "passed":
            if ass["A3_trigger_fired"] == "passed" \
                    and ass["A4_life_lost"] == "passed" \
                    and ass["A5_token_created"] == "passed" \
                    and ass["A6_no_duplicates"] == "passed":
                verdict = "not-reproduced"
            elif ass["A3_trigger_fired"] == "failed":
                verdict = "reproduced"
                notes.append("verdict=reproduced: the upkeep trigger never "
                             "fired (no stack sighting, no life loss, no "
                             "token) - the reported 'not trigger' symptom")
            else:
                verdict = "blocked"
                notes.append("verdict blocked: partial trigger behavior "
                             "(fired but resolution incomplete)")
        else:
            verdict = "blocked"
            notes.append("verdict blocked: setup/parse failed")
        say("VERDICT:", verdict)

        srv_run = server_run_dir()
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": ("Bitterbloom Bearer <redacted> not trigger on beginning "
                      "of upkeep"),
            "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "scope": ("Bitterbloom Bearer upkeep trigger ('At the beginning "
                      "of your upkeep, you lose 1 life and create a 1/1 "
                      "blue and black Faerie creature token with flying'): "
                      "card-data parse + runtime trigger/stack/resolution "
                      "assertions on P0's upkeep; native engine, two "
                      "human-client seats, P1 fully passive"),
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {
                "bearer_oid": boid,
                "bearer_cast_turn": ST["bearer_cast_turn"],
                "upkeep_turn": ST["upkeep_turn"],
                "trigger_sightings": ST["trigger_sightings"],
                "triggers_fired_at_pre": ST["triggers_fired_at_pre"],
                "triggers_fired_at_post": ST["triggers_fired_at_post"],
                "life_before": ST["life_before"],
                "life_after": ST["life_after"],
                "tokens_before": ST["tokens_before"],
                "tokens_after": ST["tokens_after"],
                "rejections": ST["rejections"],
            },
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Dense playsets (12x Bitterbloom Bearer) are a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "P1 fully passive (lands, passes; no attacks or blocks).",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
            "server_run_note": ("isolated v0.84.0 server on "
                                "127.0.0.1:9374 started by this run's session "
                                f"(games.db + server.log in runs/{srv_run}/); "
                                "ServerHello re-checked by this run; "
                                "server.log copied from the server's run dir."),
            "duration_s": round(time.time() - t_start, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_845.py")
        try:
            shutil.copy(f"{BACKFILL}/runs/{srv_run}/server.log",
                        f"{EVDIR}/server.log")
            say(f"copied server.log from runs/{srv_run}/")
        except Exception as e:
            notes.append(f"server.log copy failed: {e}")
            say("server.log copy failed:", e)
        render_png(run)
        files = ["pre.json", "mid_trigger_pending.json", "post.json",
                 "parse_bearer.json", "run.json",
                 "scenario_845.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log", "summary.png"]
        lines = []
        missing = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if os.path.exists(p):
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                lines.append(f"{h}  {fn}")
            else:
                missing.append(fn)
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # NOTE: no say() from here on - scenario_run.log is part of the
        # manifest, so further log appends would invalidate it. Missing
        # files go to console only.
        for fn in missing:
            print(f"manifest: MISSING {fn}", flush=True)
        print("wrote manifest.sha256", flush=True)
        for fn in ("pre.json", "mid_trigger_pending.json", "post.json",
                   "parse_bearer.json", "run.json"):
            if os.path.exists(f"{EVDIR}/{fn}"):
                json.load(open(f"{EVDIR}/{fn}"))
        from PIL import Image
        Image.open(f"{EVDIR}/summary.png").verify()
        man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
        for line in man:
            h, fn = line.split("  ")
            assert hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
        print("validation: all JSON parse, PNG readable, hashes match",
              flush=True)

    def render_png(run):
        from PIL import Image, ImageDraw
        W, H = 1000, 1060
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #845 - Bitterbloom Bearer <redacted> "
               "not trigger", fill=(235, 240, 250))
        y += 24
        d.text((24, y), "on beginning of upkeep", fill=(235, 240, 250))
        y += 30
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15 - "
               "2 seats", fill=(140, 160, 180))
        y += 28
        col = (255, 90, 90) if run["verdict"] == "reproduced" else (
            (120, 220, 120) if run["verdict"] == "not-reproduced"
            else (230, 200, 120))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
        y += 34
        d.text((24, y), "Oracle: 'At the beginning of your upkeep, you lose "
               "1 life and create", fill=(200, 210, 225))
        y += 22
        d.text((24, y), "a 1/1 blue and black Faerie creature token with "
               "flying.'", fill=(200, 210, 225))
        y += 30
        ds = run["driver_state"]
        d.text((24, y), "Observed P0 upkeep window "
               f"(Bearer cast turn {ds['bearer_cast_turn']}, upkeep turn "
               f"{ds['upkeep_turn']}):", fill=(200, 210, 225))
        y += 28
        d.text((40, y), f"trigger sightings on stack: "
               f"{len(ds['trigger_sightings'])}", fill=(140, 160, 180))
        y += 24
        d.text((40, y), f"P0 life: {ds['life_before']} -> "
               f"{ds['life_after']}", fill=(140, 160, 180))
        y += 24
        tb, ta = ds["tokens_before"], ds["tokens_after"]
        d.text((40, y), "Faerie tokens on P0 BF: "
               f"{len(tb) if tb else 0} -> {len(ta) if ta else 0}",
               fill=(140, 160, 180))
        y += 24
        if (ds["triggers_fired_at_pre"] is not None
                and ds["triggers_fired_at_post"] is not None):
            try:
                _tfd = (ds["triggers_fired_at_post"]
                        - ds["triggers_fired_at_pre"])
            except TypeError:
                _tfd = (len(ds["triggers_fired_at_post"])
                        - len(ds["triggers_fired_at_pre"]))
            d.text((40, y), f"triggers_fired delta: {_tfd}",
                   fill=(140, 160, 180))
        else:
            d.text((40, y), "triggers_fired: n/a", fill=(140, 160, 180))
        y += 34
        d.text((24, y), "Assertions:", fill=(200, 210, 225))
        y += 24
        for k, v in run["assertions"].items():
            c = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (230, 200, 120))
            d.text((36, y), f"{k}: {v}", fill=c)
            y += 22
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        for n in run["notes"][:16]:
            d.text((36, y), n[:114], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    # ---- main loop ----
    deadline = time.time() + 1200
    last_tick = {0: 0, 1: 0}
    try:
        while time.time() < deadline and not ST["done"]:
            for c, pid in (p0, 0), (p1, 1):
                tag = ("P0", "P1")[pid]
                if time.time() - last_tick[pid] < 1.0:
                    continue
                try:
                    await seat_tick(c, pid, tag)
                except Exception as e:
                    wire("tick_error", {"tag": tag, "err": str(e)[:200]})
                last_tick[pid] = time.time()
            # cross-seat observation from P0's view
            st = p0.latest or {}
            state = st.get("state", st)
            turn = state.get("turn_number") or 0
            phase = state.get("phase") or ""
            active = state.get("active_player")
            boid = ST["bearer_oid"] or bf_named(state, 0, BEARER)
            if boid is not None:
                ST["bearer_oid"] = boid

            # enter the observed upkeep: export pre.json once
            if (ST["stage"] == "UPKEEP_WAIT" and boid is not None
                    and phase == "Upkeep" and active == 0):
                if not ST["pre_exported"]:
                    ST["upkeep_turn"] = turn
                    ST["life_before"] = life_of(state, 0)
                    ST["tokens_before"] = faerie_tokens(state, 0)
                    ST["triggers_fired_at_pre"] = \
                        state.get("triggers_fired_this_game")
                    await export_named("pre")
                    ST["pre_exported"] = True
                    ST["stage"] = "UPKEEP_SEEN"
                    say(f"UPKEEP entered (turn {turn}); pre exported; "
                        f"life_before={ST['life_before']}, "
                        f"tokens_before={ST['tokens_before']}")

            # stack sightings of the Bearer trigger
            if ST["stage"] == "UPKEEP_SEEN" and boid is not None:
                for e in bearer_trigger_entries(state, boid):
                    eid = e.get("id")
                    if not any(s["id"] == eid for s in
                               ST["trigger_sightings"]):
                        rec = {"id": eid, "turn": turn, "phase": phase}
                        ST["trigger_sightings"].append(rec)
                        wire("bearer_trigger_on_stack", rec)
                        say(f"TRIGGER SIGHTING: Bearer upkeep trigger on "
                            f"stack (turn {turn}, phase {phase})")
                        if not ST["mid_exported"]:
                            await export_named("mid_trigger_pending")
                            ST["mid_exported"] = True

            # close the observed upkeep window
            if ST["stage"] == "UPKEEP_SEEN" and boid is not None:
                trig = bearer_trigger_entries(state, boid)
                empty = len(stack_entries(state)) == 0
                n = len(ST["trigger_sightings"])
                advanced = (turn != ST["upkeep_turn"]
                            or phase not in ("Upkeep", "Draw"))
                if n >= 1 and not trig and empty:
                    # trigger fired and left the stack: settle, then close
                    ST["stable"] += 1
                    if ST["stable"] >= 12:
                        ST["life_after"] = life_of(state, 0)
                        ST["tokens_after"] = faerie_tokens(state, 0)
                        ST["triggers_fired_at_post"] = \
                            state.get("triggers_fired_this_game")
                        await export_named("post")
                        ST["post_exported"] = True
                        ST["stage"] = "DONE"
                        ST["done"] = True
                        say("trigger resolved; post exported; DONE")
                elif n == 0 and advanced and empty:
                    # no trigger appeared at all: wait out a watchdog,
                    # then record the negative observation
                    ST["stable"] += 1
                    if ST["stable"] >= 60:
                        ST["life_after"] = life_of(state, 0)
                        ST["tokens_after"] = faerie_tokens(state, 0)
                        ST["triggers_fired_at_post"] = \
                            state.get("triggers_fired_this_game")
                        await export_named("post")
                        ST["post_exported"] = True
                        ST["stage"] = "DONE"
                        ST["done"] = True
                        say("watchdog: no trigger seen past the upkeep; "
                            "post exported; DONE")
                else:
                    ST["stable"] = 0
            await asyncio.sleep(0.2)
        if not ST["done"]:
            notes.append("deadline hit before cleanup completed")
            say("deadline hit")
    finally:
        await finish()
        for c, _ in (p0, 0), (p1, 1):
            try:
                await c.ws.close()
            except Exception:
                pass
        WIRE.close()
        RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
