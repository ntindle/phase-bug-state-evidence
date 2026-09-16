#!/usr/bin/env python3
"""Issue #7189: Karazikar, the Eye Tyrant - second ability triggers even when
an opponent attacks its controller instead of "another one of your opponents".

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.85.0, key 'karazikar, the eye tyrant'):
  Karazikar, the Eye Tyrant ({3}{B}{R}, 5/5 Legendary Creature - Beholder):
    "Whenever you attack a player, tap target creature that player controls
     and goad it. (Until your next turn, that creature attacks each combat
     if able and attacks a player other than you if able.)
     Whenever an opponent attacks another one of your opponents, you and the
     attacking player each draw a card and lose 1 life."

Card-data parse state on v0.85.0 (verified 2026-09-16 before the run):
  triggers[0] = YouAttack, attack_target_filter="Player" -> SetTapState + Goad.
  triggers[1] = Attacks, valid_source.controller="Opponent", but NO
    attack_target_filter restricting the attack's target. The "another one of
    your opponents" qualifier is absent from the parsed trigger, so the engine
    fires it on ANY attack by an opponent's creature - including attacks aimed
    at Karazikar's own controller. The "you and the attacking player each draw
    a card" effect parses as Unimplemented (name "unbound_subject") with a
    LoseLife 1 sub-ability.

Reported symptom (Discord): the second ability goes off even when an opponent
attacks its controller.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 4x Karazikar, the Eye Tyrant, 18x Swamp, 18x Mountain (40 cards;
      engine accepted the 40-card custom deck at game create/join).
  P1: 12x Grizzly Bears, 48x Forest (60 cards; passive except attacking).
  In a 2-player game "another one of your opponents" can never exist, so the
  trigger must NEVER fire on P1's attacks at P0. Any firing is the bug.

Planned line:
  Turns 1-5: both seats drop a land each turn. P1 casts Grizzly Bears when
    able. P0 casts Karazikar as soon as it is castable ({3}{B}{R}).
  P1's first turn with Karazikar on P0's battlefield and an attack-ready
    (pre-existing, untapped) Bear: export PRE, declare all ready Bears
    attacking P0. P0 declares no blockers.
  Observation window: after attackers are declared, watch the stack for a
    TriggeredAbility entry sourced from Karazikar. P0 holds one priority tick
    to guarantee the stack sample, then both seats pass priority normally.
  POST: exported once the turn advances past the attack turn with an empty
    stack. Life/hand deltas document the (partially unimplemented) resolution.

Assertions (each passed / failed / not-run):
  A1_parse            card-data: triggers[1].mode == "Attacks",
                      valid_source.controller == "Opponent", and NO
                      attack_target_filter (the missing qualifier).
  A2_setup            PRE: exactly one Karazikar on P0's battlefield, life
                      20/20, at least one Bear on P1's battlefield.
  A3_trigger_fired    a TriggeredAbility stack entry sourced from Karazikar
                      appears after P1's attackers are declared (primary), or
                      P1 loses exactly 1 life with no other cause (backup).
                      THE reported outcome: in a 2-player game this trigger
                      must never fire.
  A4_effect_detail    if the trigger fired, the implemented half of the
                      effect (each player loses 1 life) is checked: P1
                      20 -> 19 with no combat damage dealt to P1. (The draw
                      half is Unimplemented in card-data.)
  A5_cleanup          POST: stack empty, game advanced past the attack turn.

Verdict rule:
  reproduced     iff A2 passes and A3 passes (trigger fired on an attack
                 aimed at its own controller in a 2-player game).
  not-reproduced iff A2 passes, the attack turn completes, and A3 fails
                 (no trigger on any attack at P0).
  blocked        iff A2 fails (setup never reached).

Evidence: evidence/7189/<run-id>/pre.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7189.py, wire_log.jsonl,
scenario_run.log, server.log (excerpts).
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
RUN_ID = os.environ.get("RUN_ID", "20260916-7189")
EVDIR = f"{BACKFILL}/evidence/7189/{RUN_ID}"
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

SERVER_IDENTITY = {
    "server_version": "0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "mode": "single-user",
    "binary_sha256": "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f",
    "card_data_sha256": "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed",
    "draft_pools_sha256": "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e",
    "signature_verified": True,
    "observed_at": "2026-09-16",
    "source": "isolated v0.85.0 single-user server on 127.0.0.1:9374 "
              "(started fresh by this run) + verified pin (minisign-verify "
              "of binary + signed data manifest with the repo-pinned key; "
              "binary digest also matched the GitHub asset digest).",
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


def hand_size(state, pid):
    return len(player_of(state, pid).get("hand", []))


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def bf_lands(state, pid, land_names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in land_names):
            out.append(int(oid))
    return out


def stack_triggers(state):
    """TriggeredAbility stack entries: (id, source_id, description)."""
    out = []
    for e in state.get("stack") or []:
        kind = e.get("kind") or {}
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        if ktype == "TriggeredAbility":
            desc = ""
            ab = e.get("ability") or {}
            if isinstance(ab, dict):
                desc = str(ab.get("description") or "")
            out.append({"id": e.get("id"),
                        "source_id": e.get("source_id"),
                        "description": desc[:160]})
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


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"))


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") if isinstance(d, dict) else None
        if t:
            return str(t)
    return str(ch.get("id", "?"))


def accept_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "accept":
            return str(d.get("value"))
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
    ST = {
        "finished": False,
        "stage": "setup",          # setup -> attack_watch -> done
        "pre_exported": False, "post_exported": False,
        "prompt_first_seen": {},
        "kara_cast": False,
        "kara_oid": None,
        "attack_turn": None,
        "attack_declared": False,
        "bears_seen": {},          # oid -> first-seen turn (summoning sick)
        "stack_recorded": False,
        "trigger_seen": False,     # TriggeredAbility from Karazikar on stack
        "trigger_entries": [],
        "p0_life_pre": None, "p1_life_pre": None,
        "p0_hand_pre": None, "p1_hand_pre": None,
        "p0_life_post": None, "p1_life_post": None,
        "p0_hand_post": None, "p1_hand_post": None,
        "game_code": None,
        "last_rev_acted": {},
        "mull_done": {},
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": [],
           "stack_snapshots": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-kara")
    p1 = PhaseClient("P1-bears")
    await p0.connect()
    await p1.connect()
    say("both clients connected")
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or sess.get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say("P1 joined")

    async def export_named(name):
        try:
            raw = await p0.export_state()
            env = json.loads(raw)
            assert "state" in env, "envelope missing 'state'"
            with open(f"{EVDIR}/{name}.json", "w") as f:
                json.dump(env, f, indent=1)
            say(f"exported {name}.json "
                f"(turn={env['state'].get('turn_number')})")
            return env["state"]
        except Exception as e:
            say(f"export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return None

    def acted(key, rev):
        if ST["last_rev_acted"].get(key) == rev:
            return True
        ST["last_rev_acted"][key] = rev
        return False

    async def mulligan(c, pid, tag, st, state):
        """P0 keeps iff Karazikar is in the opener (else mulligans, max 2
        attempts, then keeps); P1 always keeps. Time-based resubmission so
        a rejected Mulligan choice falls back to Keep instead of stalling."""
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if ST["mull_done"].get(pid):
            return True
        now = time.time()
        last = ST.setdefault(f"mull_last_{pid}", 0.0)
        if now - last < 3.0:
            return True
        ST[f"mull_last_{pid}"] = now
        hn = hand_lnames(state, pid)
        tries = ST.setdefault(f"mull_tries_{pid}", 0)
        if pid == 0 and KARA not in hn and len(hn) > 4 and tries < 2:
            ST[f"mull_tries_{pid}"] = tries + 1
            say(f"[{tag}] mulligan: no Karazikar in {len(hn)}-card hand, "
                f"mulliganing (attempt {tries + 1})")
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            return True
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        ST["mull_done"][pid] = True
        say(f"[{tag}] mulligan: keep {len(hn)} "
            f"(kara={'yes' if KARA in hn else 'no'})")
        return True

    async def handle_discard(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        d = wf_of(state).get("data") or {}
        pl = d.get("player")
        if isinstance(pl, int) and pl != pid:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            ent = ST["prompt_first_seen"].setdefault(
                iid, {"t0": time.time(), "done": False})
            if ent["done"]:
                continue
            chs, _rtype = vi_choices(opp)
            if not chs:
                continue
            lands = P0_LANDS if pid == 0 else (FOREST,)
            def rank(ch):
                nm = str(choice_text(ch)).lower()
                if nm in lands:
                    return 0
                if nm in (KARA, BEAR):
                    return 2
                return 1
            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            ent["done"] = True
            return True
        return False

    async def pay_mana(c, tag, acts):
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(c, a)
                return True
        return False

    def cast_spell_action(acts, state, pid, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state):
            return
        if await pay_mana(p0, "P0", acts):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")

        kara_bf = bf_ids(state, 0, KARA)
        if kara_bf and ST["kara_oid"] is None:
            ST["kara_oid"] = kara_bf[0]
            say(f"[P0] Karazikar on battlefield oid={kara_bf[0]} turn={turn}")

        # combat declarations: P0 never attacks. P0 declares blockers as the
        # defender (DeclareBlockers belongs to the defending player, NOT the
        # active player), so it is answered whenever advertised to P0.
        if wtype == "DeclareAttackers" and active == 0:
            da = find_action(acts, wtype)
            if da and not acted("p0_DeclareAttackers", rev):
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p0, sub)
                say("[P0] DeclareAttackers: empty")
                return
        if wtype == "DeclareBlockers":
            da = find_action(acts, wtype)
            if da and not acted("p0_DeclareBlockers", rev):
                sub = copy.deepcopy(da)
                sub["data"]["blockers"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p0, sub)
                say("[P0] DeclareBlockers: empty (no blocks)")
                return

        # attack watch: hold one priority tick to guarantee the stack sample
        if ST["stage"] == "attack_watch" and not ST["stack_recorded"]:
            if state.get("stack"):
                trigs = stack_triggers(state)
                ST["trigger_entries"] = trigs
                wire("attack_watch_stack",
                     {"turn": turn, "phase": phase, "triggers": trigs,
                      "n_stack": len(state.get("stack") or [])})
                say(f"[P0] attack_watch stack: {len(state.get('stack') or [])} "
                    f"entries, triggers={json.dumps(trigs)[:400]}")
                for t in trigs:
                    if t["source_id"] == ST.get("kara_oid") or \
                            "another one of your opponents" in t["description"]:
                        ST["trigger_seen"] = True
                        say(f"[P0] KARAZIKAR TRIGGER ON STACK: {t}")
                ST["stack_recorded"] = True
                return  # hold this tick; pass next tick

        # main-phase actions
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            # land drop (retry every tick; no kept-flag - #6690 lesson)
            for oid in hand_ids(state, 0):
                if lname(state, oid) in P0_LANDS:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("land", rev):
                        say(f"[P0] playing land {lname(state, oid)}")
                        await submit_as_is(p0, pla)
                        return
            # cast Karazikar (legendary: only if none on BF - #6773 lesson)
            if not kara_bf and not ST["kara_cast"]:
                ca = cast_spell_action(acts, state, 0, KARA)
                if ca and not acted("kara", rev):
                    say("[P0] casting Karazikar, the Eye Tyrant")
                    await submit_as_is(p0, ca)
                    ST["kara_cast"] = True
                    return

        # attack_watch completion: turn advanced past attack turn, stack empty
        if ST["stage"] == "attack_watch" and ST["attack_declared"]:
            if turn > (ST["attack_turn"] or 0) and not (state.get("stack") or []):
                ST["p0_life_post"] = life_of(state, 0)
                ST["p1_life_post"] = life_of(state, 1)
                ST["p0_hand_post"] = hand_size(state, 0)
                ST["p1_hand_post"] = hand_size(state, 1)
                say(f"[P0] attack turn done: life "
                    f"{ST['p0_life_pre']}/{ST['p1_life_pre']} -> "
                    f"{ST['p0_life_post']}/{ST['p1_life_post']}, hands "
                    f"{ST['p0_hand_pre']}/{ST['p1_hand_pre']} -> "
                    f"{ST['p0_hand_post']}/{ST['p1_hand_post']}")
                if await export_named("post"):
                    ST["post_exported"] = True
                    ST["stage"] = "done"

        # default: pass priority (gated on my_priority - #4509 lesson)
        if my_priority(state, 0) and not acted("pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan(p1, 1, "P1", st, state):
            return
        if await handle_discard(p1, 1, "P1", st, state):
            return
        if await pay_mana(p1, "P1", acts):
            return
        wtype = wf_of(state).get("type") or ""
        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        active = state.get("active_player")

        # track bears for summoning-sickness gating
        for oid in bf_ids(state, 1, BEAR):
            ST["bears_seen"].setdefault(oid, turn)

        kara_bf = bf_ids(state, 0, KARA)

        if wtype == "DeclareAttackers" and active == 1:
            da = find_action(acts, "DeclareAttackers")
            if da and not acted("p1_atk", rev):
                sub = copy.deepcopy(da)
                sub["data"]["bands"] = []
                if (ST["stage"] == "setup" and kara_bf
                        and not ST["attack_declared"]):
                    ready = [oid for oid in bf_ids(state, 1, BEAR)
                             if ST["bears_seen"].get(oid, turn) < turn
                             and not get_obj(state, oid).get("tapped")]
                    if ready:
                        # PRE export BEFORE the investigated operation
                        pre = await export_named("pre")
                        if pre is not None:
                            ST["pre_exported"] = True
                            ST["p0_life_pre"] = life_of(pre, 0)
                            ST["p1_life_pre"] = life_of(pre, 1)
                            ST["p0_hand_pre"] = hand_size(pre, 0)
                            ST["p1_hand_pre"] = hand_size(pre, 1)
                            sub["data"]["attacks"] = [
                                [oid, {"type": "Player", "data": 0}]
                                for oid in ready]
                            ST["attack_turn"] = turn
                            ST["attack_declared"] = True
                            ST["stage"] = "attack_watch"
                            say(f"[P1] declaring attackers: "
                                f"{len(ready)} bears -> P0 (turn {turn})")
                            wire("attack_declared",
                                 {"turn": turn, "attackers": ready,
                                  "kara_oid": kara_bf[0]})
                        else:
                            sub["data"]["attacks"] = []
                    else:
                        sub["data"]["attacks"] = []
                else:
                    sub["data"]["attacks"] = []
                await submit_as_is(p1, sub)
                return

        if wtype == "DeclareBlockers":
            # P1 never defends in this line; answer only if the engine
            # actually advertises the decision to P1.
            da = find_action(acts, "DeclareBlockers")
            if da and not acted("p1_DeclareBlockers", rev):
                sub = copy.deepcopy(da)
                sub["data"]["blockers"] = []
                sub["data"]["bands"] = []
                await submit_as_is(p1, sub)
                say("[P1] DeclareBlockers: empty")
                return

        # main-phase: land drop + cast bears
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 1):
                if lname(state, oid) == FOREST:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p1land", rev):
                        say("[P1] playing land forest")
                        await submit_as_is(p1, pla)
                        return
            if BEAR in hand_lnames(state, 1):
                ca = cast_spell_action(acts, state, 1, BEAR)
                if ca and not acted("p1bear", rev):
                    say("[P1] casting grizzly bears")
                    await submit_as_is(p1, ca)
                    return

        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST["post_exported"]:
                return
            if c.latest is None:
                continue
            st = c.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 600:
            await asyncio.sleep(1)
            if ST["post_exported"]:
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                kara = bf_ids(s, 0, KARA)
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"kara_bf={len(kara)} bears={len(bf_ids(s, 1, BEAR))} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"stack={len(s.get('stack') or [])}")
            # stale-client watchdog (#4509 lesson)
            for c, tag in ((p0, "P0"), (p1, "P1")):
                rev = (c.latest or {}).get("state_revision", -1)
                key = f"wd_{tag}"
                last = ST.setdefault(key, (rev, time.time()))
                if rev != last[0]:
                    ST[key] = (rev, time.time())
                elif time.time() - last[1] > 45:
                    st = (c.latest or {}).get("state") or {}
                    say(f"[watchdog] {tag} revision {rev} stale >45s: "
                        f"turn={st.get('turn_number')} "
                        f"phase={st.get('phase')} "
                        f"wf={(st.get('waiting_for') or {}).get('type')} "
                        f"pp={st.get('priority_player')}")
                    ST[key] = (rev, time.time())
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        if not ST["post_exported"]:
            post = await export_named("post")
            if post is not None:
                ST["post_exported"] = True
                ST["p0_life_post"] = life_of(post, 0)
                ST["p1_life_post"] = life_of(post, 1)
                ST["p0_hand_post"] = hand_size(post, 0)
                ST["p1_hand_post"] = hand_size(post, 1)
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

        # ---- A1: parse ----
        try:
            cd = json.load(open(
                f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
            c = cd["karazikar, the eye tyrant"]
            trigs = c.get("triggers", [])
            t2 = trigs[1] if len(trigs) > 1 else {}
            mode_ok = t2.get("mode") == "Attacks"
            src = (t2.get("valid_source") or {})
            src_ok = src.get("controller") == "Opponent"
            filt_missing = t2.get("attack_target_filter") in (None, "")
            notes.append(f"A1: triggers={len(trigs)} mode={t2.get('mode')} "
                         f"(expect Attacks); valid_source.controller="
                         f"{src.get('controller')} (expect Opponent); "
                         f"attack_target_filter={t2.get('attack_target_filter')!r} "
                         f"(expect missing/None)")
            ok = mode_ok and src_ok and filt_missing
        except Exception as e:
            ok = False
            notes.append(f"A1 failed: parse check error {e!r}")
        ass["A1_parse"] = "passed" if ok else "failed"

        # ---- A2: setup ----
        if pre is not None and ST["pre_exported"]:
            kara = bf_ids(pre, 0, KARA)
            bears = bf_ids(pre, 1, BEAR)
            ok = (len(kara) == 1 and ST["p0_life_pre"] == 20
                  and ST["p1_life_pre"] == 20 and len(bears) >= 1)
            notes.append(f"A2: kara_on_P0_BF={len(kara)} (expect 1) "
                         f"bears_on_P1_BF={len(bears)} (expect >=1) "
                         f"life_pre={ST['p0_life_pre']}/{ST['p1_life_pre']} "
                         f"(expect 20/20)")
        else:
            ok = False
            notes.append("A2 failed: pre.json missing "
                         f"(pre_exported={ST['pre_exported']})")
        ass["A2_setup"] = "passed" if ok else "failed"

        # ---- A3: trigger fired? ----
        # Primary: TriggeredAbility stack entry sourced from Karazikar.
        # Backup: P1 lost exactly 1 life with no combat damage dealt to P1
        # (only the trigger's LoseLife can do that in this line).
        p1_delta = None
        if ST["p1_life_pre"] is not None and ST["p1_life_post"] is not None:
            p1_delta = ST["p1_life_post"] - ST["p1_life_pre"]
        backup = (p1_delta == -1)
        fired = ST["trigger_seen"] or backup
        notes.append(f"A3: trigger_seen_on_stack={ST['trigger_seen']} "
                     f"trigger_entries={json.dumps(ST['trigger_entries'])[:300]} "
                     f"p1_life {ST['p1_life_pre']}->{ST['p1_life_post']} "
                     f"(delta={p1_delta}; backup_fired={backup})")
        ass["A3_trigger_fired"] = "passed" if fired else "failed"

        # ---- A4: effect detail ----
        # If the trigger fired, the implemented half ("each lose 1 life")
        # should show P1 20->19 (P1 takes no combat damage in this line).
        # The draw half is Unimplemented in card-data; hand deltas are
        # recorded for the record.
        p0_delta = None
        if ST["p0_life_pre"] is not None and ST["p0_life_post"] is not None:
            p0_delta = ST["p0_life_post"] - ST["p0_life_pre"]
        if fired and post is not None:
            ok = (p1_delta == -1)
            notes.append(f"A4: trigger fired; P1 delta={p1_delta} "
                         f"(expect -1: each player loses 1 life); "
                         f"P0 delta={p0_delta} (combat damage + trigger); "
                         f"hands {ST['p0_hand_pre']}/{ST['p1_hand_pre']} -> "
                         f"{ST['p0_hand_post']}/{ST['p1_hand_post']} "
                         f"(draw half is Unimplemented in card-data)")
        elif not fired and post is not None:
            ok = (p1_delta == 0 and p0_delta is not None and p0_delta < 0)
            notes.append(f"A4: no trigger; P1 delta={p1_delta} (expect 0), "
                         f"P0 delta={p0_delta} (combat damage only)")
        else:
            ok = False
            notes.append("A4 failed: post.json missing")
        ass["A4_effect_detail"] = "passed" if ok else "failed"

        # ---- A5: cleanup ----
        if post is not None:
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
            ok = stack_empty
            notes.append(f"A5: stack_empty={stack_empty} post_wf={wft} "
                         f"post_turn={post.get('turn_number')} "
                         f"(attack_turn={ST['attack_turn']})")
        else:
            ok = False
            notes.append("A5 failed: post.json missing")
        ass["A5_cleanup"] = "passed" if ok else "failed"

        # ---- verdict ----
        if ass["A2_setup"] != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: setup (A2) failed")
        elif ass["A3_trigger_fired"] == "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: Karazikar's 'opponent attacks "
                         "another one of your opponents' trigger fired when "
                         "P1 attacked its own controller in a 2-player game, "
                         "where 'another one of your opponents' cannot exist")
        else:
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: the attack turn completed "
                         "with no Karazikar trigger on any attack at P0")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": 7189,
            "verdict": verdict, "validated_at": "2026-09-16",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.85.0 single-user server on "
                               "127.0.0.1:9374 (started fresh for this "
                               "run's session; this game's states are "
                               "isolated per game code)",
            "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_7189.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: v for k, v in ST.items()
                             if k not in ("prompt_first_seen",
                                          "last_rev_acted")},
            "notes": notes,
            "evidence_files": ["pre.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_7189.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "2-player game: 'another one of your opponents' can never "
                "exist, so any firing of the trigger on P1's attack at P0 "
                "is the reported defect. The correct-fire control (P1 "
                "attacks a third player) was not exercised.",
                "4x Karazikar density is a test-harness convenience; the "
                "engine accepts >4-of for custom games. P0 mulligans "
                "aggressively until Karazikar is in the opener.",
                "P0 never attacks and never blocks, isolating the trigger "
                "observation to P1's attack at P0.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        # manifest hash of the scenario must be written AFTER all logging
        # (#6916 lesson): close logs first, then hash below in
        # write_manifest(); run.json written before that keeps the
        # scenario hash only.
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_7189.py",
                    f"{EVDIR}/scenario_7189.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        RUN_LOG_DIR = f"/home/hatch/workspace/dev/phase-backfill/runs/{srv_run}"
        try:
            with open(f"{RUN_LOG_DIR}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            gc = ST.get("game_code") or ""
            excerpt = [ln for ln in clean.splitlines()
                       if gc and gc in ln]
            if not excerpt:
                excerpt = clean.splitlines()[-400:]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines)")
        except Exception as e:
            say(f"server.log excerpt failed: {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
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

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 900
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7189 - Karazikar, the Eye Tyrant",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.85.0 (cb58ef5) protocol 72 - 2026-09-16 - "
               "'opponent attacks another opponent' trigger",
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
        d.text((24, y), "attacking player each draw a card and lose 1 life.'",
               fill=(200, 210, 225))
        y += 30
        d.text((24, y), "Assertions (from saved states / live views):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_parse": "card-data: Attacks trigger, Opponent source, "
                        "NO attack_target_filter",
            "A2_setup": "PRE: 1x Karazikar on P0 BF, 20/20 life, Bear on P1 BF",
            "A3_trigger_fired": "Karazikar trigger fired on attack at its "
                                "own controller (2-player: must never fire)",
            "A4_effect_detail": "resolution: P1 20->19 (each loses 1 life); "
                                "draw half Unimplemented",
            "A5_cleanup": "POST: stack empty, turn advanced past attack",
        }
        for key, label in labels.items():
            st = run["assertions"].get(key, "not-run")
            col = (120, 220, 120) if st == "passed" else (
                (255, 90, 90) if st == "failed" else (160, 160, 160))
            d.text((40, y), f"{key}: {st}", fill=col)
            y += 20
            d.text((64, y), label[:110], fill=(150, 165, 185))
            y += 26
        y += 6
        pre, post = states.get("pre"), states.get("post")
        d.text((24, y), "Pre/post values:", fill=(200, 210, 225))
        y += 24
        ds = run.get("driver_state", {}) or {}
        lines = [
            f"attack turn: {ds.get('attack_turn')}",
            f"trigger seen on stack: {ds.get('trigger_seen')}",
            f"P0 life pre->post: {ds.get('p0_life_pre')} -> "
            f"{ds.get('p0_life_post')}",
            f"P1 life pre->post: {ds.get('p1_life_pre')} -> "
            f"{ds.get('p1_life_post')} (no combat damage to P1 in this line)",
            f"hands P0/P1 pre->post: {ds.get('p0_hand_pre')}/"
            f"{ds.get('p1_hand_pre')} -> {ds.get('p0_hand_post')}/"
            f"{ds.get('p1_hand_post')}",
        ]
        for ln in lines:
            d.text((40, y), ln[:110], fill=(170, 185, 205))
            y += 22
        y += 8
        d.text((24, y), "Limitations:", fill=(200, 210, 225))
        y += 24
        for lim in run.get("limitations", [])[:4]:
            d.text((40, y), "- " + lim[:104], fill=(150, 165, 185))
            y += 20
            if len(lim) > 104:
                d.text((52, y), lim[104:208], fill=(150, 165, 185))
                y += 20
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        files = ["pre.json", "post.json", "run.json", "summary.png",
                 "scenario_7189.py", "wire_log.jsonl", "scenario_run.log",
                 "server.log"]
        lines = []
        for fn in files:
            p = f"{EVDIR}/{fn}"
            if not os.path.exists(p):
                lines.append(f"MISSING  {fn}")
                continue
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        say("wrote manifest.sha256")

    await finish()
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
