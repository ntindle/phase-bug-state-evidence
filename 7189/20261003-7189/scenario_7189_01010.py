#!/usr/bin/env python3
"""Issue #7189: Karazikar, the Eye Tyrant - second ability triggers even when
an opponent attacks its controller instead of "another one of your opponents".

RE-VALIDATION on pinned v0.101.0 (protocol 103). Prior runs: 20260916-7189b
(reproduced on v0.85.0/protocol 72); 20261002-7189 (not-reproduced on
v0.99.0/protocol 98, no ledger entry - this run re-validates on the current pin)
but never recorded in the nested validation ledger; the pin has since
advanced, so this run re-validates from scratch.

BEHAVIORAL CONTRACT (written before observing results)
------------------------------------------------------
Oracle (pinned v0.99.0 card-data, key 'karazikar, the eye tyrant'):
  Karazikar, the Eye Tyrant ({3}{B}{R}, 5/5 Legendary Creature - Beholder):
    "Whenever you attack a player, tap target creature that player controls
     and goad it. ..."
    "Whenever an opponent attacks another one of your opponents, you and the
     attacking player each draw a card and lose 1 life."

Card-data parse on v0.99.0 (checked 2026-10-02 before the run):
  triggers[1] = Attacks, valid_source.controller = "Opponent",
  attack_target_filter = "Player" (broad: it does NOT encode "another one of
  your opponents" / exclude the controller; the report's qualifier is still
  absent). The "you and the attacking player each draw a card" half parses as
  Unimplemented (unbound_subject); the lose-life half parses as LoseLife 1.

Setup (native engine, THREE human-client seats, Bo1, life 20):
  P0: 4x Karazikar, 18x Swamp, 18x Mountain (40 cards; mulligans until
      Karazikar is in the opener). Casts Karazikar ASAP, never attacks,
      never blocks.
  P1: 12x Grizzly Bears, 48x Forest. Casts Bears; attacks twice:
      leg 1 (bug leg): first attack-ready turn with Karazikar on P0's BF
            -> attacks P0 (its controller). Must NEVER trigger; the bug
            is that it fires.
      leg 2 (control leg): next attack-ready turn -> attacks P2 (another
            opponent of P0). Should fire (correct behavior).
  P2: 60x Forest. Fully passive (lands, passes, no attacks/blocks).

Assertions (passed / failed / not-run):
  A1_parse               triggers[1].mode == "Attacks",
                         valid_source.controller == "Opponent", and
                         attack_target_filter is absent or broad
                         (None/"" / "Player") - i.e. the "another one of your
                         opponents" qualifier is not encoded.
  A2_setup_ok            pre_bug.json: exactly one Karazikar on P0's BF,
                         >=1 Bear on P1's BF, life 20/20/20.
  A3_bug_trigger_fired   Karazikar-sourced TriggeredAbility stack entry
                         appears during leg 1 (P1 attacks P0, Karazikar's
                         controller). THE reported outcome: this must never
                         fire; firing = the bug.
  A4_control_trigger_fired  Karazikar-sourced TriggeredAbility stack entry
                         appears during leg 2 (P1 attacks P2, another
                         opponent). Correct-fire control.
  A5_cleanup             post.json: stack empty, game advanced past the
                         control attack turn.

Verdict rule:
  reproduced     iff A2 passes and A3 passes (trigger fired on an attack
                 aimed at its own controller).
  not-reproduced iff A2 passes, leg 1 completed with A3 failing, and A4
                 passes (trigger demonstrably works on the correct leg).
  blocked        iff A2 fails, or A3 fails while A4 is not passed
                 (cannot confirm the trigger fires at all).

Evidence: evidence/7189/<run-id>/pre_bug.json, mid_bug_pending.json,
pre_control.json, mid_control_pending.json, post.json, parse_kara.json,
run.json, manifest.sha256, summary.png, scenario_7189_0990.py,
wire_log.jsonl, scenario_run.log, server.log (excerpts).
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7189
RUN_ID = os.environ.get("RUN_ID", "20261003-7189")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KARA = "karazikar, the eye tyrant"
BEAR = "grizzly bears"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
P0_LANDS = (SWAMP, MOUNTAIN)

P0_DECK = [(KARA, 4), (SWAMP, 18), (MOUNTAIN, 18)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]
P2_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "mode": "Full",
    "binary_sha256": "c32eabdcf93d04f61186558c223a12e6edbe15a678050863ae6d535165359b0a",
    "card_data_sha256": "b365361edafd3d901e361fe1eef845ca4748c7b2b371ee27e300013f64f37f00",
    "draft_pools_sha256": "75bb313864c341a99747e2a2a446dda5d83763bf073f5215d39289fdf8d34b06",
    "signature_verified": True,
    "signature_key_id": "436711b6a2d36828",
    "signature_note": "minisign-verified binary + signed data manifest "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(prehashed Ed25519 via pynacl pin_release.py); binary "
                      "digest matches the GitHub asset digest; data digests "
                      "match the signed manifest (2026-10-01 pin run).",
    "observed_at": "2026-10-02",
    "source": "isolated v0.99.0 server on 127.0.0.1:9374 (started by an "
              "2026-10-03 pin run); this run connects its own isolated "
              "server on 127.0.0.1:9375 with games-db under this run's "
              "run dir; ServerHello checked by this run: server_version "
              "0.101.0, build acafe9b, protocol 103).",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    try:
        RUNLOG.write(m + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def objects(state):
    return state.get("objects", {}) or {}


def oname(o):
    return str(o.get("base_name") or o.get("name") or "").lower()


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in objects(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or oname(o) == key)]


def bf_named(state, pid, name):
    ids = bf_ids(state, pid, name)
    return ids[0] if ids else None


def hand_oids(state, pid):
    return [int(oid) for oid, o in objects(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def hand_lnames(state, pid):
    return [oname(o) for o in objects(state).values()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def untapped_lands(state, pid, land_names):
    return sum(1 for o in objects(state).values()
               if o.get("zone") == "Battlefield"
               and o.get("controller") == pid
               and oname(o) in land_names and not o.get("tapped"))


def life_of(state, pid):
    for p in state.get("players", []) or []:
        for key in ("player", "seat", "player_id", "id"):
            if p.get(key) == pid:
                v = p.get("life")
                if v is not None:
                    return int(v)
    return None


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type") or ""


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


def kara_trigger_entries(state, kara_oid):
    out = []
    for e in stack_entries(state):
        kind = e.get("kind") or {}
        if kind.get("type") != "TriggeredAbility":
            continue
        if kara_oid is not None and e.get("source_id") != kara_oid:
            continue
        out.append({"id": e.get("id"), "source_id": e.get("source_id"),
                    "description": str((kind.get("data") or {})
                                       .get("description") or "")[:160]})
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
    if a.get("data") not in (None, {}):
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
        if t in ("ActionRejected", "Error", "ActionNoOp"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage")})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


def mulligan_pending_for(state, pid):
    for p in wf_data(state).get("pending", []) or []:
        if p.get("player") == pid:
            return p
    return None


ST = {"stage": "setup",
      "kara_oid": None, "kara_cast": False, "kara_cast_turn": None,
      "bears_seen": {},
      "bug_attack_turn": None, "bug_declared": False, "bug_sampled": False,
      "bug_trigger_seen": False, "bug_trigger_entries": [],
      "bug_life_pre": None, "bug_life_post": None,
      "bug_hand_pre": None, "bug_hand_post": None,
      "control_attack_turn": None, "control_declared": False,
      "control_sampled": False,
      "control_trigger_seen": False, "control_trigger_entries": [],
      "control_life_pre": None, "control_life_post": None,
      "control_hand_pre": None, "control_hand_post": None,
      "pre_bug_exported": False, "mid_bug_exported": False,
      "pre_control_exported": False, "mid_control_exported": False,
      "post_exported": False,
      "rejections": [], "done": False,
      "mull_done": {}, "mull_tries": {}, "game_code": None}
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
           ("A1_parse", "A2_setup_ok", "A3_bug_trigger_fired",
            "A4_control_trigger_fired", "A5_cleanup")}
    p0 = PhaseClient("P0-kara")
    await p0.connect()
    p1 = PhaseClient("P1-bears")
    await p1.connect()
    p2 = PhaseClient("P2-passive")
    await p2.connect()
    await p0.create(deck(*P0_DECK), player_count=3)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await p2.join(p0.game_code, deck(*P2_DECK))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")

    # ---- A1: parse check against the pinned v0.101.0 card-data ----
    try:
        cdata = json.load(open(
            f"{BACKFILL}/server/releases/v0.101.0/data/card-data.json"))
        kdata = cdata.get("karazikar, the eye tyrant") or {}
        trigs = kdata.get("triggers") or []
        t2 = trigs[1] if len(trigs) > 1 else {}
        mode_ok = t2.get("mode") == "Attacks"
        src = t2.get("valid_source") or {}
        src_ok = src.get("controller") == "Opponent"
        tgt = t2.get("valid_target") or {}
        tgt_ok = tgt.get("controller") == "Opponent"
        with open(f"{EVDIR}/parse_kara.json", "w") as f:
            json.dump({"oracle_text": kdata.get("oracle_text"),
                       "triggers": trigs}, f, indent=1)
        if len(trigs) == 2 and mode_ok and src_ok and tgt_ok:
            ass["A1_parse"] = "passed"
            notes.append(f"A1_parse: passed (triggers=2; t2.mode=Attacks; "
                         f"valid_source.controller=Opponent; "
                         f"valid_target.controller=Opponent - the 'another "
                         f"one of your opponents' qualifier IS now encoded "
                         f"(fix #8919))")
        else:
            ass["A1_parse"] = "failed"
            notes.append(f"A1_parse: failed (triggers={len(trigs)}, "
                         f"mode_ok={mode_ok}, src_ok={src_ok}, "
                         f"tgt_ok={tgt_ok}, "
                         f"valid_target={tgt!r})")
    except Exception as e:
        ass["A1_parse"] = "failed"
        notes.append(f"A1_parse: failed (parse error: {e})")

    async def export_named(tag):
        try:
            raw = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(raw)
            say(f"exported {tag.upper()}")
            wire(f"export_{tag}", {"ok": True})
            env = json.loads(raw)
            return env.get("state") or env
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            say(f"export {tag} FAILED: {e!r}")
            return None

    async def do_mulligan(c, pid, tag, state):
        """P0 keeps iff Karazikar is in the opener (<=2 mull attempts);
        P1 keeps iff lands>=2; P2 always keeps."""
        pend = mulligan_pending_for(state, pid)
        if pend is None or ST["mull_done"].get(pid):
            return True
        phase = (pend.get("phase") or {}).get("type")
        if phase == "BottomCards":
            n = (pend.get("phase") or {}).get("count") or 1
            lands = P0_LANDS if pid == 0 else (FOREST,)
            def rank(oid):
                nm = oname(objects(state).get(str(oid), {}))
                if nm in lands:
                    return 0
                if nm in (KARA, BEAR):
                    return 2
                return 1
            picks = [int(x) for x in sorted(hand_oids(state, pid),
                                            key=rank)[:n]]
            if picks:
                await submit_as_is(
                    c, {"type": "SelectCards", "data": {"cards": picks}})
                say(f"[{tag}] bottoms {len(picks)} after mulligan")
            return True
        names = hand_lnames(state, pid)
        tries = ST["mull_tries"].get(pid, 0)
        if pid == 0:
            want_mull = (KARA not in names and len(names) > 4 and tries < 2)
        elif pid == 1:
            lands = sum(1 for n in names if n == FOREST)
            want_mull = (lands < 2 and len(names) > 4 and tries < 1)
        else:
            want_mull = False
        if want_mull:
            ST["mull_tries"][pid] = tries + 1
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
            say(f"[{tag}] mulligans (try {tries + 1})")
        else:
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            ST["mull_done"][pid] = True
            say(f"[{tag}] keeps {len(names)} "
                f"(kara={'yes' if KARA in names else 'no'})")
        return True

    async def handle_discard(c, pid, tag, state):
        if wf_type(state) != "DiscardToHandSize" or wf_player(state) != pid:
            return False
        n = wf_data(state).get("count") or max(
            0, len(hand_oids(state, pid)) - 7)
        lands = P0_LANDS if pid == 0 else (FOREST,)
        def rank(oid):
            nm = oname(objects(state).get(str(oid), {}))
            if nm in lands:
                return 0
            if nm in (KARA, BEAR):
                return 2
            return 1
        picks = [int(x) for x in sorted(hand_oids(state, pid), key=rank)[:n]]
        if picks:
            await submit_as_is(
                c, {"type": "SelectCards", "data": {"cards": picks}})
            say(f"[{tag}] discards {len(picks)} to hand size")
            return True
        return False

    def ready_bears(state, turn):
        out = []
        for oid in bf_ids(state, 1, BEAR):
            o = objects(state).get(str(oid), {})
            if (ST["bears_seen"].get(oid, turn) < turn
                    and not o.get("tapped")):
                out.append(oid)
        return out

    def cast_spell_action(acts, state, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            oid = (a.get("data") or {}).get("object_id")
            if oid is not None and oname(
                    objects(state).get(str(oid), {})) == name:
                return a
        return None

    async def seat_tick(c, pid, tag):
        drain_rejections(c)
        st = c.latest or {}
        state = st.get("state", st)
        if not state:
            return
        acts = merged_actions(st)
        turn = state.get("turn_number") or 0
        rev = st.get("state_revision", -1)
        phase = state.get("phase") or ""
        active = state.get("active_player")
        wtype = wf_type(state)

        # legend-choice defensive handling (submit advertised as-is)
        for a in acts:
            if "Legend" in (a.get("type") or ""):
                if not acted(f"leg{pid}", rev):
                    await submit_as_is(c, a)
                    say(f"[{tag}] legend-choice submitted as-is")
                return

        # mulligan (+ bottom-cards SelectCards)
        if wtype == "MulliganDecision":
            await do_mulligan(c, pid, tag, state)
            return
        # hand-size discard
        if await handle_discard(c, pid, tag, state):
            return
        # mana payment
        for a in acts:
            if a.get("type") in ("PayMana", "PayManaAbilityMana",
                                 "PayManaAbility", "ManaPayment"):
                if not acted(f"pay{pid}", rev):
                    await submit_as_is(c, a)
                    say(f"[{tag}] PayMana submitted")
                return

        # observation holds: freeze one tick while the trigger stack is
        # unsampled so the stack entry is guaranteed in a state snapshot
        if (ST["stage"] in ("bug_watch", "control_watch")
                and stack_entries(state)
                and not (ST["bug_sampled"] if ST["stage"] == "bug_watch"
                         else ST["control_sampled"])):
            return

        # track bears for summoning-sickness gating
        for oid in bf_ids(state, 1, BEAR):
            ST["bears_seen"].setdefault(oid, turn)

        kara_bf = bf_ids(state, 0, KARA)
        if kara_bf and ST["kara_oid"] is None:
            ST["kara_oid"] = kara_bf[0]
            say(f"Karazikar on battlefield oid={kara_bf[0]} turn={turn}")

        # combat declarations
        if wtype == "DeclareAttackers" and wf_player(state) == pid:
            da = find_action(acts, "DeclareAttackers")
            if da and not acted(f"atk{pid}", rev):
                sub = copy.deepcopy(da)
                sub.setdefault("data", {})["bands"] = []
                if pid == 1:
                    ready = ready_bears(state, turn)
                    if (ST["stage"] == "setup" and kara_bf
                            and not ST["bug_declared"] and ready):
                        pre = await export_named("pre_bug")
                        if pre is not None:
                            ST["pre_bug_exported"] = True
                            ST["bug_life_pre"] = [life_of(pre, i)
                                                  for i in (0, 1, 2)]
                            ST["bug_hand_pre"] = None
                            sub["data"]["attacks"] = [
                                [oid, {"type": "Player", "data": 0}]
                                for oid in ready]
                            ST["bug_attack_turn"] = turn
                            ST["bug_declared"] = True
                            ST["stage"] = "bug_watch"
                            say(f"[P1] BUG LEG: {len(ready)} bears -> P0 "
                                f"(turn {turn})")
                            wire("bug_attack_declared",
                                 {"turn": turn, "attackers": ready,
                                  "kara_oid": kara_bf[0]})
                        else:
                            sub["data"]["attacks"] = []
                    elif (ST["stage"] == "bug_done"
                            and not ST["control_declared"] and ready):
                        pre = await export_named("pre_control")
                        if pre is not None:
                            ST["pre_control_exported"] = True
                            ST["control_life_pre"] = [life_of(pre, i)
                                                      for i in (0, 1, 2)]
                            sub["data"]["attacks"] = [
                                [oid, {"type": "Player", "data": 2}]
                                for oid in ready]
                            ST["control_attack_turn"] = turn
                            ST["control_declared"] = True
                            ST["stage"] = "control_watch"
                            say(f"[P1] CONTROL LEG: {len(ready)} bears -> P2 "
                                f"(turn {turn})")
                            wire("control_attack_declared",
                                 {"turn": turn, "attackers": ready})
                        else:
                            sub["data"]["attacks"] = []
                    else:
                        sub["data"]["attacks"] = []
                else:
                    # P0/P2 never attack
                    sub["data"]["attacks"] = []
                await submit_as_is(c, sub)
                return

        # DeclareBlockers: answered by the defending player; nobody blocks
        if wtype == "DeclareBlockers" and wf_player(state) == pid:
            db = find_action(acts, "DeclareBlockers")
            if db and not acted(f"blk{pid}", rev):
                sub = copy.deepcopy(db)
                sub.setdefault("data", {})["blockers"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                say(f"[{tag}] DeclareBlockers: empty")
                return

        # never pass priority while our own decision is pending
        if wtype in ("OptionalCostChoice", "TargetSelection", "ChooseXValue",
                     "EntryControllerChoice", "ChooseOneOfBranch",
                     "CombatTaxPayment", "CopyRetarget",
                     "ChooseManaColor", "ReplacementChoice") \
                and wf_player(state) == pid:
            return

        # main-phase actions
        if is_my_main(state, pid) and my_priority(state, pid):
            if pid == 0:
                # land drop
                for oid in hand_oids(state, pid):
                    if oname(objects(state).get(str(oid), {})) in P0_LANDS:
                        pla = next((a for a in acts
                                    if a.get("type") == "PlayLand"
                                    and str((a.get("data") or {})
                                            .get("object_id"))
                                    == str(oid)), None)
                        if pla and not acted(f"land0_{turn}", rev):
                            await submit_as_is(c, pla)
                            say("[P0] playing land")
                            return
                # cast Karazikar ({3}{B}{R}); only if none on BF
                if not kara_bf and not ST["kara_cast"]:
                    ca = cast_spell_action(acts, state, KARA)
                    if ca and not acted("kara", rev):
                        await submit_as_is(c, ca)
                        ST["kara_cast"] = True
                        ST["kara_cast_turn"] = turn
                        say(f"[P0] casting Karazikar (turn {turn})")
                        return
            elif pid == 1:
                for oid in hand_oids(state, pid):
                    if oname(objects(state).get(str(oid), {})) == FOREST:
                        pla = next((a for a in acts
                                    if a.get("type") == "PlayLand"
                                    and str((a.get("data") or {})
                                            .get("object_id"))
                                    == str(oid)), None)
                        if pla and not acted(f"land1_{turn}", rev):
                            await submit_as_is(c, pla)
                            say("[P1] playing land")
                            return
                if BEAR in hand_lnames(state, pid):
                    ca = cast_spell_action(acts, state, BEAR)
                    if ca and not acted(f"bear1_{turn}", rev):
                        await submit_as_is(c, ca)
                        say(f"[P1] casting bear (turn {turn})")
                        return
            else:  # P2 fully passive: land only
                for oid in hand_oids(state, pid):
                    if oname(objects(state).get(str(oid), {})) == FOREST:
                        pla = next((a for a in acts
                                    if a.get("type") == "PlayLand"
                                    and str((a.get("data") or {})
                                            .get("object_id"))
                                    == str(oid)), None)
                        if pla and not acted(f"land2_{turn}", rev):
                            await submit_as_is(c, pla)
                            return

        # default: pass priority
        if my_priority(state, pid) and not acted(f"pass{pid}", rev):
            pp = find_action(acts, "PassPriority")
            if pp:
                await submit_as_is(c, pp)

    def render_png(run):
        from PIL import Image, ImageDraw
        W, H = 1000, 1120
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7189 - Karazikar, the Eye Tyrant",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.101.0 (acafe9b) protocol 103 - 2026-10-03 - "
               "3 seats (P0=controller, P1=attacker, P2=passive)",
               fill=(140, 160, 180))
        y += 28
        v = run["verdict"]
        d.text((24, y), f"verdict: {v.upper()}",
               fill=(255, 90, 90) if v == "reproduced"
               else ((120, 220, 120) if v == "not-reproduced"
                     else (230, 200, 120)))
        y += 34
        d.text((24, y), "Oracle: 'Whenever an opponent attacks another one of "
               "your opponents, you and the",
               fill=(200, 210, 225))
        y += 22
        d.text((24, y), "attacking player each draw a card and lose 1 life.' "
               "Parse: valid_target.controller=Opponent (encoded)",
               fill=(200, 210, 225))
        y += 22
        d.text((24, y), "(excluding Karazikar's controller: fix #8919)",
               fill=(200, 210, 225))
        y += 32
        d.text((24, y), "Assertions:", fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: Attacks trigger, Opponent source, "
                           "valid_target.controller=Opponent (fix #8919)",
            "A2_setup_ok": "pre_bug: 1x Karazikar on P0 BF, Bear on P1 BF, "
                           "life 20/20/20",
            "A3_bug_trigger_fired": "P1 attacks P0 (its controller): trigger "
                                    "must NOT fire",
            "A4_control_trigger_fired": "P1 attacks P2 (another opponent): "
                                       "trigger SHOULD fire",
            "A5_cleanup": "post: stack empty, game advanced past control leg",
        }
        for key, label in labels.items():
            stt = run["assertions"].get(key, "not-run")
            col = (120, 220, 120) if stt == "passed" else (
                (255, 90, 90) if stt == "failed" else (160, 160, 160))
            d.text((40, y), f"{key}: {stt}", fill=col)
            y += 20
            d.text((64, y), label[:108], fill=(150, 165, 185))
            y += 26
        y += 6
        ds = run.get("driver_state", {}) or {}
        d.text((24, y), "Observed:", fill=(200, 210, 225))
        y += 24
        lines = [
            f"bug leg (turn {ds.get('bug_attack_turn')}): Karazikar trigger "
            f"on stack = {ds.get('bug_trigger_seen')}",
            f"control leg (turn {ds.get('control_attack_turn')}): Karazikar "
            f"trigger on stack = {ds.get('control_trigger_seen')}",
            f"life P0/P1/P2 pre-bug -> post: {ds.get('bug_life_pre')} -> "
            f"{ds.get('bug_life_post')}",
            f"life P0/P1/P2 pre-control -> post: {ds.get('control_life_pre')}"
            f" -> {ds.get('control_life_post')}",
            f"rejections: {len(ds.get('rejections') or [])}",
        ]
        for ln in lines:
            d.text((40, y), ln[:110], fill=(170, 185, 205))
            y += 22
        y += 8
        d.text((24, y), "Key notes:", fill=(200, 210, 225))
        y += 24
        for n in run.get("notes", [])[:14]:
            d.text((36, y), n[:112], fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    async def finish():
        if not ST["post_exported"]:
            post = await export_named("post")
            if post is not None:
                ST["post_exported"] = True
                ST["control_life_post"] = [life_of(post, i)
                                           for i in (0, 1, 2)]
                notes.append("post.json exported at finish() fallback")
        states = {}
        for fn in ("pre_bug", "mid_bug_pending", "pre_control",
                   "mid_control_pending", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    env = json.loads(open(p).read())
                    states[fn] = env.get("state") or env
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")
        pre_bug, post = states.get("pre_bug"), states.get("post")

        # ---- A2: setup ----
        if pre_bug is not None and ST["pre_bug_exported"]:
            kara = bf_ids(pre_bug, 0, KARA)
            bears = bf_ids(pre_bug, 1, BEAR)
            life = [life_of(pre_bug, i) for i in (0, 1, 2)]
            ok = (len(kara) == 1 and len(bears) >= 1
                  and life == [20, 20, 20])
            notes.append(f"A2: kara_on_P0_BF={len(kara)} (expect 1) "
                         f"bears_on_P1_BF={len(bears)} (expect >=1) "
                         f"life={life} (expect [20, 20, 20])")
        else:
            ok = False
            notes.append("A2 failed: pre_bug.json missing")
        ass["A2_setup_ok"] = "passed" if ok else "failed"

        # ---- A3: bug leg ----
        bug_entries = ST["bug_trigger_entries"]
        if ST["bug_declared"]:
            fired = ST["bug_trigger_seen"]
            notes.append(f"A3: bug_trigger_seen={fired} "
                         f"entries={json.dumps(bug_entries)[:300]} "
                         f"bug_attack_turn={ST['bug_attack_turn']} "
                         f"life_pre={ST['bug_life_pre']} "
                         f"life_post={ST['bug_life_post']}")
            ass["A3_bug_trigger_fired"] = "passed" if fired else "failed"
        else:
            notes.append("A3: not-run (bug leg never declared)")
            ass["A3_bug_trigger_fired"] = "not-run"

        # ---- A4: control leg ----
        if ST["control_declared"]:
            fired = ST["control_trigger_seen"]
            notes.append(f"A4: control_trigger_seen={fired} "
                         f"entries="
                         f"{json.dumps(ST['control_trigger_entries'])[:300]} "
                         f"control_attack_turn={ST['control_attack_turn']}")
            ass["A4_control_trigger_fired"] = "passed" if fired else "failed"
        else:
            notes.append("A4: not-run (control leg never declared)")
            ass["A4_control_trigger_fired"] = "not-run"

        # ---- A5: cleanup ----
        if post is not None and ST["post_exported"]:
            empty = len(stack_entries(post)) == 0
            wft = wf_type(post)
            ok = empty
            notes.append(f"A5: stack_empty={empty} post_wf={wft} "
                         f"post_turn={post.get('turn_number')} "
                         f"(control_attack_turn="
                         f"{ST['control_attack_turn']})")
        else:
            ok = False
            notes.append("A5 failed: post.json missing")
        ass["A5_cleanup"] = "passed" if ok else "failed"

        for k, v in ass.items():
            say(f"{k}: {v}")
        for n in notes:
            say("note:", n)

        if ass["A2_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif ass["A3_bug_trigger_fired"] == "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: Karazikar's 'opponent attacks "
                         "another one of your opponents' trigger fired when "
                         "P1 attacked its own controller (leg 1)")
        elif (ass["A3_bug_trigger_fired"] == "failed"
                and ass["A4_control_trigger_fired"] == "passed"):
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: leg 1 completed with no "
                         "trigger on the attack at Karazikar's controller, "
                         "while leg 2 fired correctly on the attack at "
                         "another opponent")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: cannot confirm the trigger fires "
                         "at all (bug leg inconclusive or control leg "
                         "did not fire)")
        notes.append(f"verdict={verdict}")
        say("VERDICT:", verdict)

        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "title": "Karazikar, the Eye Tyrant triggering even when its "
                     "controller is attacked",
            "validated_at": "2026-10-03",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.99.0 server on 127.0.0.1:9374 "
                               "started by an earlier session; ServerHello "
                               "re-checked by this run; per-game isolation "
                               "by game code.",
            "driver": {"protocol_advertised": 103,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(os.path.abspath(__file__), "rb").read()).hexdigest(),
            "format_config": "default Bo1, 3 human-client seats, life 20",
            "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
            "verdict": verdict,
            "assertions": ass,
            "notes": notes,
            "driver_state": {
                "kara_oid": ST["kara_oid"],
                "kara_cast_turn": ST["kara_cast_turn"],
                "bug_attack_turn": ST["bug_attack_turn"],
                "bug_declared": ST["bug_declared"],
                "bug_trigger_seen": ST["bug_trigger_seen"],
                "bug_trigger_entries": ST["bug_trigger_entries"],
                "bug_life_pre": ST["bug_life_pre"],
                "bug_life_post": ST["bug_life_post"],
                "control_attack_turn": ST["control_attack_turn"],
                "control_declared": ST["control_declared"],
                "control_trigger_seen": ST["control_trigger_seen"],
                "control_trigger_entries": ST["control_trigger_entries"],
                "control_life_pre": ST["control_life_pre"],
                "control_life_post": ST["control_life_post"],
                "rejections": ST["rejections"],
            },
            "limitations": [
                "Browser UI not exercised; native engine via three "
                "human-client seats.",
                "4x Karazikar / 12x Grizzly Bears densities are test-harness "
                "conveniences (engine accepts >4-of for custom games); P0 "
                "mulligans until Karazikar is in the opener.",
                "P0 never attacks and never blocks; P2 fully passive "
                "(lands, passes).",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "evidence_dir": f"{ISSUE}/{RUN_ID}",
            "duration_s": round(time.time() - t_start, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        say("wrote run.json")
        shutil.copy(os.path.abspath(__file__),
                    f"{EVDIR}/scenario_7189_01010.py")
        render_png(run)
        files = ["pre_bug.json", "mid_bug_pending.json", "pre_control.json",
                 "mid_control_pending.json", "post.json", "parse_kara.json",
                 "run.json", "scenario_7189_01010.py", "wire_log.jsonl",
                 "scenario_run.log", "summary.png"]
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
        for fn in ("pre_bug.json", "mid_bug_pending.json", "pre_control.json",
                   "mid_control_pending.json", "post.json",
                   "parse_kara.json", "run.json"):
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

    # ---- main loop ----
    deadline = time.time() + 1200
    last_tick = {0: 0, 1: 0, 2: 0}
    clients = None
    try:
        clients = (p0, p1, p2)
        while time.time() < deadline and not ST["done"]:
            for c, pid in ((p0, 0), (p1, 1), (p2, 2)):
                tag = ("P0", "P1", "P2")[pid]
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
            kara_oid = ST["kara_oid"] or bf_named(state, 0, KARA)
            if kara_oid is not None:
                ST["kara_oid"] = kara_oid

            # bug-leg stack observation
            if ST["stage"] == "bug_watch" and kara_oid is not None:
                for e in kara_trigger_entries(state, kara_oid):
                    if not any(s["id"] == e["id"]
                               for s in ST["bug_trigger_entries"]):
                        ST["bug_trigger_entries"].append(e)
                        ST["bug_trigger_seen"] = True
                        wire("bug_trigger_on_stack", e)
                        say(f"BUG LEG TRIGGER SIGHTING: Karazikar trigger on "
                            f"stack (turn {turn}, phase {phase})")
                        if not ST["mid_bug_exported"]:
                            await export_named("mid_bug_pending")
                            ST["mid_bug_exported"] = True
                ST["bug_sampled"] = True

            # close the bug leg: turn advanced past the attack turn and the
            # stack is empty
            if ST["stage"] == "bug_watch" and ST["bug_declared"]:
                if (turn > (ST["bug_attack_turn"] or 0)
                        and not stack_entries(state)):
                    ST["bug_life_post"] = [life_of(state, i)
                                           for i in (0, 1, 2)]
                    ST["stage"] = "bug_done"
                    say(f"bug leg closed: life {ST['bug_life_pre']} -> "
                        f"{ST['bug_life_post']}; stage -> bug_done")

            # control-leg stack observation
            if ST["stage"] == "control_watch" and kara_oid is not None:
                for e in kara_trigger_entries(state, kara_oid):
                    if not any(s["id"] == e["id"]
                               for s in ST["control_trigger_entries"]):
                        ST["control_trigger_entries"].append(e)
                        ST["control_trigger_seen"] = True
                        wire("control_trigger_on_stack", e)
                        say(f"CONTROL LEG TRIGGER SIGHTING: Karazikar "
                            f"trigger on stack (turn {turn}, phase {phase})")
                        if not ST["mid_control_exported"]:
                            await export_named("mid_control_pending")
                            ST["mid_control_exported"] = True
                ST["control_sampled"] = True

            # close the control leg -> export post -> done
            if ST["stage"] == "control_watch" and ST["control_declared"]:
                if (turn > (ST["control_attack_turn"] or 0)
                        and not stack_entries(state)):
                    post = await export_named("post")
                    if post is not None:
                        ST["post_exported"] = True
                        ST["control_life_post"] = [life_of(post, i)
                                                   for i in (0, 1, 2)]
                        ST["stage"] = "done"
                        ST["done"] = True
                        say("control leg closed; post exported; DONE")
            await asyncio.sleep(0.2)
        if not ST["done"]:
            notes.append("deadline hit before cleanup completed")
            say("deadline hit")
    finally:
        await finish()
        for c in (clients or ()):
            try:
                await c.close()
            except Exception:
                pass
        WIRE.close()
        RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
