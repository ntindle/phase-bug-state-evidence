#!/usr/bin/env python3
"""Issue #1362: Kediss, Emberclaw Familiar doesn't trigger when creature does
trample damage to a player.

Re-validation run on v0.84.0 / protocol 71 (prior: v0.78.0 / protocol 68,
run 20260909-0931, verdict not-reproduced, published; the validation-ledger
entry for that run was lost in a VM replacement, so this run both re-validates
on the current pin and restores the ledger record).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-05-29, classifier: supported_aspect_defect, status:confirmed):
Kediss didn't trigger when a commander with trample was blocked and did excess
damage to the player. A 2026-07-26 review comment notes the path was fixed by
PR #3176 (source-filtered combat-damage observers now consume aggregate
trample damage and fan it out per source).

Oracle text (pinned v0.84.0 card-data.json, 'kediss, emberclaw familiar'):
  "Whenever a commander you control deals combat damage to an opponent, it
   deals that much damage to each other opponent. Partner (You can have two
   commanders if both have partner.)"
Parsed trigger (fully typed): DamageDone/CombatOnly, valid_source =
Typed{controller: You, properties: [IsCommander]}, valid_target =
Typed{controller: Opponent}, effect = DamageEachPlayer{amount:
EventContextAmount, player_filter: OpponentOtherThanTriggering}.

Setup (native engine, three human-client seats, CommanderDraft):
  P0: commander Zilortha, Strength Incarnate (7/3 trample, {3}{R}{G});
      main = 4x Kediss, Emberclaw Familiar + 28x Forest + 28x Mountain.
  P1: 4x Llanowar Elves + 56x Forest (provides the 1/1 blocker).
  P2: 60x Forest dummy (observes the Kediss fan-out as the "other opponent").

Expected (per card text):
  E1: Zilortha (commander) + Kediss on P0's battlefield; a 1/1 blocker on
      P1's battlefield; all players at 40 life (Commander starting life).
  E2: P0 attacks P1 with Zilortha; P1 blocks with Llanowar Elves.
  E3: trample assigns 6 (7 power - 1 toughness) combat damage to P1;
      P1 goes 40 -> 34 and the Elves die.
  E4: Kediss's trigger fires (it is on the stack with source=Kediss and the
      DamageEachPlayer effect).
  E5: on resolution each OTHER opponent (P2; P0 is the controller, P1 was
      the damage recipient) is dealt 6: P2 goes 40 -> 34, P0 stays 40.
  E6: stack empties, game proceeds.

Assertions:
  A1_setup_ok       pre.json at DeclareAttackers: Zilortha on P0 BF with
                   is_commander=true, Kediss on P0 BF, Elves on P1 BF,
                   all life 40
  A2_trample_damage post.json: P1 life 40 -> 34 (7 power - 1 toughness),
                   blocking Elves no longer on BF
  A3_kediss_triggered Kediss trigger observed on the stack (source=Kediss,
                   TriggeredAbility with DamageEachPlayer effect signature)
  A4_fanout_correct post.json: P2 life 40 -> 34, P0 life unchanged at 40
  A5_cleanup        post.json: stack empty, game proceeding, Zilortha and
                   Kediss still on P0's BF

Verdict rule: not-reproduced iff A1..A5 all pass. reproduced iff A1 passed
and (A3 failed or A4 failed) - the reported "does not trigger" outcome.
blocked iff the game cannot be driven to the DeclareAttackers setup.

Evidence: evidence/1362/<run-id>/{pre,mid_trigger_pending,post}.json,
run.json, manifest.sha256, summary.png, scenario_1362.py, wire_log.jsonl,
scenario_run.log, server.log excerpt
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
RUN_ID = "20260915-1362"
SERVER_RUN_ID = "20260915-1362"  # live server reused by this run (runs/<id>/)
EVDIR = f"{BACKFILL}/evidence/1362/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KEDISS = "Kediss, Emberclaw Familiar"
ZILORTHA = "Zilortha, Strength Incarnate"
ELVES = "Llanowar Elves"
FOREST = "Forest"
MOUNTAIN = "Mountain"

P0_MAIN = [(KEDISS, 4), (FOREST, 28), (MOUNTAIN, 28)]
P0_COMMANDER = [ZILORTHA]
P1_DECK = [(ELVES, 4), (FOREST, 56)]
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
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "Full",
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "sha256 recomputed against the on-disk pinned release files "
              "(minisign-verified 2026-09-15); server started by this run on "
              "127.0.0.1:9374, runs/20260915-1362",
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


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return None


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower()
            == key.lower()]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def untapped_lands(state, pid, key=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        nm = str(o.get("base_name") or o.get("name") or "")
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in (FOREST, MOUNTAIN)):
            if key is None or nm == key:
                out.append(int(oid))
    return out


def merged_actions(st):
    """legal_actions lives at the TOP LEVEL of the WS message data."""
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    bits = []

    def rec(v):
        if isinstance(v, dict):
            for k, val in v.items():
                if k in ("name", "code", "label", "value", "description",
                         "text", "prompt", "title") and isinstance(val, str):
                    bits.append(val)
                else:
                    rec(val)
        elif isinstance(v, list):
            for x in v:
                rec(x)
    rec(ch.get("surfaces", []))
    return " | ".join(bits)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def my_priority(state, pid):
    wf = state.get("waiting_for") or {}
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) if not isinstance(k, dict) else
                           k.get("name", "") for k in kws]:
            return False
    return True


def kediss_trigger_entry(state):
    """Return the Kediss DamageEachPlayer trigger stack entry if present."""
    for e in state.get("stack", []) or []:
        kind = (e.get("kind") or {}).get("type")
        low = json.dumps(e, default=str).lower()
        if "kediss" in low and "damageeachplayer" in low:
            if kind in (None, "TriggeredAbility"):
                return e
    return None


def pending_for(state, pid):
    return [p for p in ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            if p.get("player") == pid]


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trample_damage", "A3_kediss_triggered",
            "A4_fanout_correct", "A5_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                    format_config=COMMANDER_FORMAT)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id}")

    obs = {"trigger_entry": None, "trigger_seen": False,
           "pre_life": None, "post_life": None,
           "zil_power": None, "expected_trample": None,
           "p0_cmdr_act_types": [], "cmdr_zone_seen": False,
           "attack_turn": None}
    stack_history = []
    pre_exported = False
    post_exported = False
    attacked = False
    last_select = {}
    last_assign = {}
    mull_state = {}  # pid -> {"mulls": n, "answered_count": n}

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            pre_st, post_st = None, None
            notes.append(f"state reload failed: {e}")
        if pre_st is not None and post_st is not None:
            obs["pre_life"] = [life_of(pre_st, i) for i in (0, 1, 2)]
            obs["post_life"] = [life_of(post_st, i) for i in (0, 1, 2)]
            pre_life, post_life = obs["pre_life"], obs["post_life"]
            zil = bf_id(pre_st, 0, ZILORTHA)
            if zil is not None:
                obs["zil_power"] = num(get_obj(pre_st, zil).get("power"))
            exp = (obs["zil_power"] - 1) if obs["zil_power"] else None
            obs["expected_trample"] = exp
            elves_post = bf_ids(post_st, 1, ELVES)
            if (pre_life[1] == STARTING_LIFE and exp
                    and post_life[1] == STARTING_LIFE - exp
                    and len(elves_post) == 0):
                ass["A2_trample_damage"] = "passed"
                notes.append(f"P1 life {STARTING_LIFE} -> {post_life[1]} "
                             f"(trample {exp} = Zilortha power "
                             f"{obs['zil_power']} - 1); blocking Elves left "
                             f"the battlefield")
            else:
                ass["A2_trample_damage"] = "failed"
                notes.append(f"A2 wrong: pre_life={pre_life} "
                             f"post_life={post_life} expected_trample={exp} "
                             f"elves_on_bf_post={len(elves_post)}")
            if obs["trigger_seen"]:
                ass["A3_kediss_triggered"] = "passed"
                notes.append("Kediss trigger observed on stack (source=Kediss, "
                             "TriggeredAbility, DamageEachPlayer effect; see "
                             "wire log / mid_trigger_pending.json)")
            elif stack_history:
                # live tick missed the stack window but a sampled revision
                # carried the Kediss DamageEachPlayer trigger entry
                obs["trigger_seen"] = True
                ass["A3_kediss_triggered"] = "passed"
                notes.append(f"Kediss trigger observed via stack-history "
                             f"sampling ({len(stack_history)} sampled "
                             f"entries); see wire log")
            else:
                ass["A3_kediss_triggered"] = "failed"
                notes.append("NO Kediss trigger observed on the stack after "
                             "combat damage (REPORTED BUG)")
            if exp and post_life[2] == STARTING_LIFE - exp \
                    and post_life[0] == STARTING_LIFE:
                ass["A4_fanout_correct"] = "passed"
                notes.append(f"P2 life {STARTING_LIFE} -> {post_life[2]} "
                             f"(fan-out {exp}); P0 unchanged at "
                             f"{STARTING_LIFE}")
            else:
                ass["A4_fanout_correct"] = "failed"
                notes.append(f"A4 wrong: post_life={post_life} expected P2="
                             f"{STARTING_LIFE - exp if exp else '?'} "
                             f"P0={STARTING_LIFE}")
            slen = len(post_st.get("stack", []) or [])
            zil_post = bf_ids(post_st, 0, ZILORTHA)
            ked_post = bf_ids(post_st, 0, KEDISS)
            if slen == 0 and zil_post and ked_post:
                ass["A5_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, Zilortha+Kediss on P0 "
                             f"BF, phase={post_st.get('phase')}")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"A5 wrong: stack={slen} zil={len(zil_post)} "
                             f"ked={len(ked_post)} phase={post_st.get('phase')}")
        else:
            for k in ("A2_trample_damage", "A3_kediss_triggered",
                      "A4_fanout_correct", "A5_cleanup"):
                if ass[k] == "not-run":
                    ass[k] = "failed"
                    notes.append(f"{k} could not be evaluated (missing "
                                 f"pre/post state)")
        core = ["A1_setup_ok", "A2_trample_damage", "A3_kediss_triggered",
                "A4_fanout_correct", "A5_cleanup"]
        if all(ass[k] == "passed" for k in core):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] == "passed" and any(
                ass[k] == "failed" for k in ("A3_kediss_triggered",
                                            "A4_fanout_correct")):
            verdict = "reproduced"
        else:
            verdict = "blocked"
            notes.append("inconclusive or setup incomplete; see notes")
        # server.log excerpt (relevant lines) while the owning session lives
        slog = f"{BACKFILL}/runs/{SERVER_RUN_ID}/server.log"
        try:
            lines = open(slog).read().splitlines()
            excerpt = [l for l in lines
                       if "kediss" in l.lower() or "trigger" in l.lower()
                       or "combat" in l.lower() or "damage" in l.lower()]
            with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
                f.write("\n".join(excerpt[-120:]) + "\n")
            notes.append(f"server.log excerpt: {len(excerpt)} matching "
                         f"lines (of {len(lines)} total)")
        except Exception as e:
            notes.append(f"server.log excerpt failed: {e}")
        run = {
            "issue": 1362,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{SERVER_RUN_ID}",
            "driver": {"protocol_advertised": 71, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_1362.py", "rb").read()
            ).hexdigest(),
            "format_config": "CommanderDraft (canonical "
                             "FormatConfig::commander_draft(); CR 903.13f: "
                             ">=60 cards, no singleton; commander placed in "
                             "command zone)",
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": P1_DECK, "P2": P2_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via three "
                "human-client seats.",
                "4x Kediss deck density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "States are authoritative exports, restorable only via full "
                "game replay.",
            ],
            "setup_line": "P0: Zilortha (7/3 trample) commander + 4x Kediss "
                          "+ 28x Forest + 28x Mountain; P1: 4x Llanowar "
                          "Elves + 56x Forest; P2: 60x Forest",
            "contract_line": "Blocked commander with trample deals excess "
                             "combat damage to a player -> Kediss triggers "
                             "-> each other opponent takes that much",
            "prior_runs": [
                {"run_id": "20260909-1362", "release": "v0.77.0",
                 "verdict": "blocked",
                 "note": "P2 websocket keepalive ping timeouts; P1 never "
                         "fielded an Elves blocker (local only, unpublished)"},
                {"run_id": "20260909-0931", "release": "v0.78.0",
                 "verdict": "not-reproduced",
                 "note": "maintained comment posted 2026-09-09; ledger entry "
                         "lost in VM replacement, restored by this run"},
            ],
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
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    async def do_mulligan(c, pid, key_need, lands_need, max_mulls):
        st = c.latest
        pend = pending_for(st["state"], pid)
        declare = next((p for p in pend
                        if (p.get("phase") or {}).get("type") == "Declare"),
                       None)
        if declare is None:
            return False
        ms = mull_state.setdefault(pid, {"mulls": 0, "answered_count": -1})
        mcount = declare.get("mulligan_count", 0)
        if ms["answered_count"] == mcount:
            return False
        ma = find_action(merged_actions(st), "MulliganDecision")
        if not ma:
            return False
        hn = hand_names(st["state"], pid)
        lands = sum(1 for n in hn if n in (FOREST, MOUNTAIN))
        has_key = key_need is None or key_need in hn
        if (has_key and lands >= lands_need) or ms["mulls"] >= max_mulls:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"P{pid} keeps (key={key_need in hn if key_need else 'n/a'}, "
                f"lands={lands})")
        else:
            ms["mulls"] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"P{pid} mulligans #{ms['mulls']}")
        ms["answered_count"] = mcount
        return True

    async def do_bottom(c, pid, key_need):
        st = c.latest
        state = st["state"]
        pend = pending_for(state, pid)
        count = 1
        for p in pend:
            if (p.get("phase") or {}).get("type") == "BottomCards":
                count = int((p.get("phase") or {}).get("count", 1))
        sc = find_action(merged_actions(st), "SelectCards")
        if not sc:
            return False
        hand_ids = [o for pl in state.get("players", [])
                    if pl.get("id") == pid for o in pl.get("hand", [])]

        def bkey(oid):
            nm = obj_name(state, oid)
            if nm in (FOREST, MOUNTAIN):
                return 2
            if key_need and nm == key_need:
                return 1
            return 0
        picks = sorted(hand_ids, key=bkey)[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"P{pid} bottoms {count}: {[obj_name(state, x) for x in picks]}")
        return True

    def cast_action_for(acts, state, key):
        for a in acts:
            if "cast" in a["type"].lower():
                d = a.get("data", {})
                for v in list(d.values()):
                    if isinstance(v, int) and lname(state, v) == key.lower():
                        return a
        return None

    def p0_spell_in_flight(state):
        return any((e.get("kind") or {}).get("type") == "Spell"
                   and e.get("controller") == 0
                   for e in (state.get("stack") or []))

    async def vi_pass_fallback(c, st):
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    codes = [s.get("data", {}).get("code")
                             for s in ch.get("surfaces", []) or []]
                    if "passPriority" in codes and ch.get("status", {}) \
                            .get("type") == "available":
                        await c.send_interaction({
                            "interactionId": opp.get("interactionId"),
                            "response": {"type": "choose",
                                         "data": {"choiceId": ch["id"]}}})
                        return True
        return False

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, post_exported, attacked
        wtype = (state.get("waiting_for") or {}).get("type")
        # ---- mulligan ----
        if wtype == "MulliganDecision":
            if await do_mulligan(p0, 0, KEDISS, 2, 4):
                return
            if find_action(acts, "SelectCards") \
                    and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                if await do_bottom(p0, 0, KEDISS):
                    return
            return
        # ---- auto-pay (engine mostly auto-taps; answer advertised) ----
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # ---- observe the Kediss trigger on the stack at any point ----
        hit = kediss_trigger_entry(state)
        if hit and not obs["trigger_seen"]:
            obs["trigger_seen"] = True
            obs["trigger_entry"] = json.dumps(hit, default=str)[:1500]
            say("KEDISS TRIGGER observed on stack")
            wire("kediss_trigger_on_stack", hit)
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid_trigger_pending.json", "w") as f:
                    f.write(mid)
                say("exported MID (Kediss trigger pending)")
            except Exception as e:
                notes.append(f"mid export failed: {e}")
        # ---- declare attackers ----
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            da = find_action(acts, "DeclareAttackers")
            if da:
                zil = bf_id(state, 0, ZILORTHA)
                ked = bf_id(state, 0, KEDISS)
                elves = bf_ids(state, 1, ELVES)
                if (not attacked and zil is not None and ked is not None
                        and elves and can_attack_now(state, zil)
                        and not pre_exported):
                    say("PRE: exporting at DeclareAttackers (Zilortha+Kediss "
                        f"ready, P1 has {len(elves)} blocker(s))")
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_st = json.loads(pre)["state"]
                    zil_pre = bf_id(pre_st, 0, ZILORTHA)
                    is_cmdr = bool(zil_pre is not None and get_obj(
                        pre_st, zil_pre).get("is_commander"))
                    ok = (is_cmdr and bf_id(pre_st, 0, KEDISS) is not None
                          and bf_ids(pre_st, 1, ELVES)
                          and all(life_of(pre_st, i) == STARTING_LIFE
                                  for i in (0, 1, 2)))
                    ass["A1_setup_ok"] = "passed" if ok else "failed"
                    notes.append(f"pre: zil_is_commander={is_cmdr}, life="
                                 f"{[life_of(pre_st, i) for i in (0, 1, 2)]}")
                    pre_exported = True
                    obs["attack_turn"] = state.get("turn_number")
                    d = copy.deepcopy(da.get("data", {}))
                    d["attacks"] = [[zil, {"type": "Player", "data": 1}]]
                    d["bands"] = []
                    await p0.send_action({"type": "DeclareAttackers",
                                          "data": d})
                    attacked = True
                    say(f"P0 attacks P1 with Zilortha (oid {zil})")
                    wire("p0_attacks", {"attacker": zil, "defender": 1})
                    return
                d = copy.deepcopy(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
                if not pre_exported:
                    say(f"P0 declares no attackers (waiting: zil={zil} "
                        f"ked={ked} elves_p1={len(elves)})")
            return
        # ---- trample damage assignment ----
        if wtype == "AssignCombatDamage":
            aa = find_action(acts, "AssignCombatDamage")
            if aa and last_assign.get(0) != p0.revision:
                last_assign[0] = p0.revision
                d = copy.deepcopy(aa.get("data", {}))
                say("AssignCombatDamage advertised: " + json.dumps(d)[:500])
                wire("assign_combat_damage_advertised", d)
                zil = bf_id(state, 0, ZILORTHA)
                pwr = int(num(get_obj(state, zil).get("power"))
                          if zil is not None else 0)
                assigns = d.get("assignments", []) or []
                tram = int(d.get("trample_damage", 0) or 0)
                total = sum(int(x[1]) for x in assigns) + tram
                asgn_by_oid = {int(x[0]): int(x[1]) for x in assigns}
                elves_oid = bf_ids(state, 1, ELVES)
                elves_lethal_ok = all(
                    asgn_by_oid.get(e, 0) >= int(num(get_obj(state, e)
                                                     .get("toughness")) or 0)
                    for e in elves_oid)
                if total == pwr and pwr > 0 and elves_lethal_ok:
                    await p0.send_action({"type": "AssignCombatDamage",
                                          "data": d})
                    say(f"P0 assigns combat damage (advertised default): "
                        f"blockers={assigns} trample_damage={tram}")
                    wire("assign_combat_damage_submitted", d)
                else:
                    say(f"P0 REFUSES advertised assignment: power={pwr} "
                        f"assigns={assigns} trample={tram} "
                        f"elves_lethal_ok={elves_lethal_ok}")
                    wire("assign_combat_damage_refused", d)
            return
        if wtype == "DeclareBlockers":
            db = find_action(acts, "DeclareBlockers")
            if db:
                d = copy.deepcopy(db.get("data", {}))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers",
                                      "data": d})
            return
        if not my_priority(state, 0):
            return
        # ---- P0 priority ----
        in_flight = p0_spell_in_flight(state)
        # 1) cast Kediss from hand when affordable
        if (not in_flight and bf_id(state, 0, KEDISS) is None
                and KEDISS in hand_names(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
            if len(untapped_lands(state, 0, MOUNTAIN)) >= 1 \
                    and len(untapped_lands(state, 0)) >= 2:
                a = cast_action_for(acts, state, KEDISS)
                if a:
                    say(f"P0 casts Kediss via {a['type']}")
                    wire("cast_kediss", {"action": a["type"]})
                    await submit_as_is(p0, a)
                    return
        # 2) cast Zilortha from the command zone when affordable
        if (not in_flight and bf_id(state, 0, ZILORTHA) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
            if not obs["p0_cmdr_act_types"]:
                obs["p0_cmdr_act_types"] = sorted(
                    set(a["type"] for a in acts))
                cz = [(o.get("name"), o.get("is_commander"), o.get("zone"))
                      for o in state.get("objects", {}).values()
                      if "zilortha" in str(o.get("name", "")).lower()]
                obs["cmdr_zone_seen"] = bool(cz)
                say(f"P0 PreCombatMain action types: {obs['p0_cmdr_act_types']}")
                say(f"Zilortha objects: {cz}")
                wire("p0_main_actions", {"types": obs["p0_cmdr_act_types"],
                                        "zilortha": cz})
            if len(untapped_lands(state, 0, MOUNTAIN)) >= 1 \
                    and len(untapped_lands(state, 0, FOREST)) >= 1 \
                    and len(untapped_lands(state, 0)) >= 5:
                a = cast_action_for(acts, state, ZILORTHA)
                if a:
                    say(f"P0 casts Zilortha (commander) via {a['type']}")
                    wire("cast_zilortha", {"action": a["type"]})
                    await submit_as_is(p0, a)
                    return
        # 3) post-export once combat + trigger are done
        if (pre_exported and attacked and not post_exported
                and state.get("active_player") == 0
                and state.get("phase") in ("PostCombatMain", "End", "Cleanup")
                and len(state.get("stack", []) or []) == 0):
            say("post-combat reached with empty stack; exporting POST")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        # 4) normal play: land drop (retry every tick), then pass
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
        await vi_pass_fallback(p0, st)

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if await do_mulligan(p1, 1, ELVES, 1, 4):
                return
            if find_action(acts, "SelectCards") \
                    and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                if await do_bottom(p1, 1, ELVES):
                    return
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers" and state.get("active_player") == 0:
            db = find_action(acts, "DeclareBlockers")
            if db:
                zil = bf_id(state, 0, ZILORTHA)
                elves = bf_ids(state, 1, ELVES)
                d = copy.deepcopy(db.get("data", {}))
                if zil is not None and elves:
                    d["assignments"] = [[elves[0], zil]]
                    say(f"P1 blocks Zilortha (oid {zil}) with Elves "
                        f"(oid {elves[0]})")
                    wire("p1_blocks", {"blocker": elves[0], "attacker": zil})
                else:
                    d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "AssignBlockerDamage":
            aa = find_action(acts, "AssignBlockerDamage")
            if aa and last_assign.get(1) != p1.revision:
                last_assign[1] = p1.revision
                # engine default is fine; submit advertised as-is
                await p1.send_action({"type": "AssignBlockerDamage",
                                      "data": copy.deepcopy(aa.get("data", {}))})
                say("P1 assigns blocker damage (advertised default)")
            return
        if not my_priority(state, 1):
            return
        if (state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1
                and ELVES in hand_names(state, 1)
                and len(untapped_lands(state, 1, FOREST)) >= 1):
            a = cast_action_for(acts, state, ELVES)
            if a:
                say("P1 casts Llanowar Elves")
                await submit_as_is(p1, a)
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
        await vi_pass_fallback(p1, st)

    async def p2_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if await do_mulligan(p2, 2, None, 2, 2):
                return
            if find_action(acts, "SelectCards") \
                    and last_select.get(2) != p2.revision:
                last_select[2] = p2.revision
                if await do_bottom(p2, 2, None):
                    return
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p2, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p2.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            db = find_action(acts, "DeclareBlockers")
            if db:
                d = copy.deepcopy(db.get("data", {}))
                d["assignments"] = []
                await p2.send_action({"type": "DeclareBlockers", "data": d})
            return
        if not my_priority(state, 2):
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
        await vi_pass_fallback(p2, st)

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
            # record stack signatures for the trigger observation history
            try:
                sigs = []
                for e in (st["state"].get("stack", []) or []):
                    low = json.dumps(e, default=str).lower()
                    if "kediss" in low and "damageeachplayer" in low:
                        sigs.append(((e.get("kind") or {}).get("type"), rev))
                if sigs:
                    stack_history.extend(sigs)
            except Exception:
                pass
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)} "
                f"zil={bf_ids(s, 0, ZILORTHA)} ked={bf_ids(s, 0, KEDISS)} "
                f"elves={bf_ids(s, 1, ELVES)} life={[life_of(s, i) for i in (0, 1, 2)]} "
                f"stack={len(s.get('stack') or [])} pre={pre_exported} "
                f"attacked={attacked} post={post_exported} "
                f"trig={obs['trigger_seen']} stackhist={len(stack_history)}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
