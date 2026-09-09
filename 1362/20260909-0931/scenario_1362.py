#!/usr/bin/env python3
"""Issue #1362: Kediss, Emberclaw Familiar doesn't trigger when creature does
trample damage to a player.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-05-29, classifier: supported_aspect_defect): Kediss
didn't trigger when a commander with trample was blocked and did excess
damage to the player.

Oracle text (pinned card-data.json, 'kediss, emberclaw familiar'):
  "Whenever a commander you control deals combat damage to an opponent, it
   deals that much damage to each other opponent."
Parsed trigger (DamageDone, CombatOnly, valid_source=IsCommander+You,
valid_target=Opponent, effect=DamageEachPlayer OpponentOtherThanTriggering
amount=EventContextAmount). Parser coverage is complete; the suspected
defect is runtime: trample-routed damage not recognized as combat damage
from the commander source.

Setup (native engine, three human-client seats):
  P0: commander = Zilortha, Strength Incarnate (7/3 trample, {3}{R}{G});
      main = 4x Kediss, Emberclaw Familiar + 28x Forest + 28x Mountain.
      (dense: engine accepts >4-of for custom games; mulligan to Kediss +
      2+ lands.)
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
                   all life 20
  A2_trample_damage post.json: P1 life 40 -> 34 (7 power - 1 toughness),
                   blocking Elves no longer on BF
  A3_kediss_triggered Kediss trigger observed on the stack (source=Kediss,
                   DamageEachPlayer effect signature) after combat damage
  A4_fanout_correct post.json: P2 life 40 -> 34, P0 life unchanged at 40
  A5_cleanup        post.json: stack empty, game proceeding, Zilortha and
                   Kediss still on P0's BF

Verdict rule: reproduced iff A1 passed and (A3 failed or A4 failed) - the
reported "does not trigger" outcome. not-reproduced iff A1..A5 pass.
blocked iff the game cannot be driven to the DeclareAttackers setup
(e.g. commander cannot be cast from the command zone in this build).

Evidence: evidence/1362/<run-id>/pre.json (DeclareAttackers, board ready),
post.json (after combat + trigger resolution), run.json, manifest.sha256,
summary.png, scenario_1362.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-0931"
EVDIR = f"{BACKFILL}/evidence/1362/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KEDISS = "kediss, emberclaw familiar"   # card-data.json key (exact)
ZILORTHA = "zilortha, strength incarnate"
ELVES = "llanowar elves"
FOREST = "forest"
MOUNTAIN = "mountain"

P0_MAIN = [(KEDISS, 4), (FOREST, 28), (MOUNTAIN, 28)]
P0_COMMANDER = [ZILORTHA]
P1_DECK = [(ELVES, 4), (FOREST, 56)]
P2_DECK = [(FOREST, 60)]

# Canonical Commander Draft FormatConfig (mirrors engine
# FormatConfig::commander_draft(); CR 903.13f: >=60 cards, NO singleton
# restriction, commander placed in the command zone with is_commander=true).
# Commander proper was rejected by the server's deck legality gate
# ("Commander deck must have exactly 100 cards; Singleton violations"),
# while CommanderDraft admits the dense test decks.
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
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + minisign-verify (repo-pinned key) of binary + signed "
              "data manifest; binary sha256 matches GitHub asset digest; data "
              "files sha256-verified against manifest; fresh pinned v0.78.0 "
              "server on 127.0.0.1:9374 (runs/20260909-0931)",
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
            and lname(state, oid) == key]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def hand_lnames(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [lname(state, o) for o in p.get("hand", [])]
    return []


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def untapped_lands(state, pid, key=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped")
                and str(o.get("base_name") or o.get("name") or "").lower()
                in (FOREST, MOUNTAIN)):
            if key is None or lname(state, oid) == key:
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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def my_priority(state, pid):
    wf = state.get("waiting_for") or {}
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
            return False
    return True


def kediss_trigger_on_stack(state):
    for e in state.get("stack", []) or []:
        low = json.dumps(e, default=str).lower()
        if "kediss" in low and "damageeachplayer" in low:
            return e
    return None


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
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} P2={p2.player_id}")
    kept = {}

    obs = {"trigger_entry": None, "trigger_seen": False,
           "pre_life": None, "post_life": None,
           "zil_power": None, "expected_trample": None,
           "p0_cmdr_act_types": [], "cmdr_zone_seen": False,
           "combat_done": False}
    pre_exported = False
    post_exported = False
    attacked = False
    last_select = {}
    last_assign = {}

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
            # expected trample from pre.json board
            zil = bf_id(pre_st, 0, ZILORTHA)
            if zil is not None:
                obs["zil_power"] = num(get_obj(pre_st, zil).get("power"))
            exp = (obs["zil_power"] - 1) if obs["zil_power"] else None
            obs["expected_trample"] = exp
            # A2: P1 took exactly the trample amount; blocker gone
            elves_post = bf_ids(post_st, 1, ELVES)
            if (pre_life[1] == STARTING_LIFE and exp
                    and post_life[1] == STARTING_LIFE - exp
                    and len(elves_post) == 0):
                ass["A2_trample_damage"] = "passed"
                notes.append(f"P1 life {STARTING_LIFE} -> {post_life[1]} (trample {exp} = "
                             f"Zilortha power {obs['zil_power']} - 1); blocking "
                             f"Elves left the battlefield")
            else:
                ass["A2_trample_damage"] = "failed"
                notes.append(f"A2 wrong: pre_life={pre_life} post_life={post_life} "
                             f"expected_trample={exp} elves_on_bf_post={len(elves_post)}")
            # A3 from live observation
            if obs["trigger_seen"]:
                ass["A3_kediss_triggered"] = "passed"
                notes.append("Kediss trigger observed on stack (source=Kediss, "
                             "DamageEachPlayer effect; see wire log / mid_trigger_pending.json)")
            else:
                ass["A3_kediss_triggered"] = "failed"
                notes.append("NO Kediss trigger observed on the stack after combat "
                             "damage (REPORTED BUG)")
            # A4: fan-out to P2 only
            if exp and post_life[2] == STARTING_LIFE - exp \
                    and post_life[0] == STARTING_LIFE:
                ass["A4_fanout_correct"] = "passed"
                notes.append(f"P2 life {STARTING_LIFE} -> {post_life[2]} (fan-out {exp}); "
                             f"P0 unchanged at {STARTING_LIFE}")
            else:
                ass["A4_fanout_correct"] = "failed"
                notes.append(f"A4 wrong: post_life={post_life} expected P2="
                             f"{STARTING_LIFE - exp if exp else '?'} P0={STARTING_LIFE} "
                             f"(REPORTED BUG if trigger missing)")
            # A5 cleanup
            slen = len(post_st.get("stack", []) or [])
            zil_post = bf_ids(post_st, 0, ZILORTHA)
            ked_post = bf_ids(post_st, 0, KEDISS)
            if slen == 0 and zil_post and ked_post:
                ass["A5_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, Zilortha+Kediss on P0 BF, "
                             f"phase={post_st.get('phase')}")
            else:
                ass["A5_cleanup"] = "failed"
                notes.append(f"A5 wrong: stack={slen} zil={len(zil_post)} "
                             f"ked={len(ked_post)} phase={post_st.get('phase')}")
        else:
            for k in ("A2_trample_damage", "A3_kediss_triggered",
                      "A4_fanout_correct", "A5_cleanup"):
                if ass[k] == "not-run":
                    ass[k] = "failed"
                    notes.append(f"{k} could not be evaluated (missing pre/post state)")
        core = ["A1_setup_ok", "A2_trample_damage", "A3_kediss_triggered",
                "A4_fanout_correct", "A5_cleanup"]
        if all(ass[k] == "passed" for k in core):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] == "passed" and any(
                ass[k] == "failed" for k in ("A3_kediss_triggered", "A4_fanout_correct")):
            verdict = "reproduced"
        else:
            verdict = "blocked"
            notes.append("inconclusive or setup incomplete; see notes")
        run = {
            "issue": 1362,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_1362.py", "rb").read()).hexdigest(),
            "format_config": "CommanderDraft (canonical FormatConfig::commander_draft(); "
                             "CR 903.13f: >=60 cards, no singleton; commander placed "
                             "in command zone)",
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": P1_DECK, "P2": P2_DECK},
            "assertions": ass,
            "notes": notes + [
                "Attempt 1 (run 20260909-1362, v0.77.0/proto 67) died: P2's "
                "websocket hit keepalive ping timeouts and P1 never fielded "
                "an Elves blocker, so the DeclareAttackers setup gate never "
                "opened (turn 50, all life 40). Partial artifacts remain in "
                "evidence/1362/20260909-1362/ (not published).",
                "Attempt 2 (this run, v0.78.0/proto 68): fresh scenario run; "
                "v0.78.0 released 2026-09-09, so the pin was advanced per the "
                "playbook (minisign-verify + signed data manifest + GitHub "
                "asset digest match)."
            ],
            "observations": obs,
            "verdict": verdict,
            "limitations": ["Browser UI not exercised; native engine via three human-client seats.",
                            "4x Kediss deck density is a test-harness convenience "
                            "(engine accepts >4-of for custom games).",
                            "Pinned card data prices Zilortha at {3}{R}{G} as a 7/3 "
                            "(no P/T-setting ability parsed); the trample contract "
                            "under test is unaffected."],
            "setup_line": "P0: Zilortha (7/3 trample) commander + 4x Kediss + 28x Forest + 28x Mountain; "
                          "P1: 4x Llanowar Elves + 56x Forest; P2: 60x Forest",
            "contract_line": "Blocked commander with trample deals excess combat damage to a "
                             "player -> Kediss triggers -> each other opponent takes that much",
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

    def mulligan_keep(pid, state, key_need, lands_need, max_mulls):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in (FOREST, MOUNTAIN))
        mulls = kept.get(f"P{pid}_mulls", 0)
        has_key = key_need is None or key_need in hn
        return (has_key and lands >= lands_need) or mulls >= max_mulls

    async def do_mulligan(c, pid, key_need, lands_need, max_mulls):
        hn = hand_lnames(c.latest["state"], pid)
        if mulligan_keep(pid, c.latest["state"], key_need, lands_need, max_mulls):
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"P{pid} keeps (key={key_need in hn if key_need else 'n/a'})")
        else:
            kept[f"P{pid}_mulls"] = kept.get(f"P{pid}_mulls", 0) + 1
            kept.pop(f"P{pid}_bottomed", None)
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"P{pid} mulligans #{kept[f'P{pid}_mulls']}")

    async def do_bottom(c, pid, key_need):
        st = c.latest["state"]
        pending = ((st.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
        count = 1
        for p in pending:
            if p.get("player") == pid:
                ph = p.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    count = int(ph.get("count", 1))
        hand_ids = [o for pl in st.get("players", [])
                    if pl.get("id") == pid for o in pl.get("hand", [])]

        def bkey(oid):
            nm = lname(st, oid)
            if nm in (FOREST, MOUNTAIN):
                return 2
            if key_need and nm == key_need:
                return 1
            return 0
        picks = sorted(hand_ids, key=bkey)[:count]
        kept[f"P{pid}_bottomed"] = True
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"P{pid} bottoms {count}: {[lname(st, x) for x in picks]}")

    def cast_action_for(acts, key):
        for a in acts:
            if "cast" in a["type"].lower():
                d = a.get("data", {})
                for v in list(d.values()):
                    if isinstance(v, int):
                        # resolved lazily by caller via lname
                        a["_obj"] = v
                        break
                return a
        return None

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, post_exported, attacked
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, KEDISS, 2, 6)
                return
            # every BottomCards phase must be answered (multi-mulligan games
            # present one SelectCards per mulligan); gate on revision so a
            # stale re-tick never double-submits.
            if find_action(acts, "SelectCards") and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                await do_bottom(p0, 0, KEDISS)
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # observe the Kediss trigger on the stack at any point
        hit = kediss_trigger_on_stack(state)
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
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            da = find_action(acts, "DeclareAttackers")
            if da:
                zil = bf_id(state, 0, ZILORTHA)
                ked = bf_id(state, 0, KEDISS)
                elves = bf_ids(state, 1, ELVES)
                if (not attacked and zil is not None and ked is not None and elves
                        and can_attack_now(state, zil) and not pre_exported):
                    say("PRE: exporting at DeclareAttackers (Zilortha+Kediss ready, "
                        f"P1 has {len(elves)} blocker(s))")
                    pre = await p0.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_st = json.loads(pre)["state"]
                    zil_pre = bf_id(pre_st, 0, ZILORTHA)
                    is_cmdr = bool(zil_pre is not None
                                   and get_obj(pre_st, zil_pre).get("is_commander"))
                    ok = (is_cmdr and bf_id(pre_st, 0, KEDISS) is not None
                          and bf_ids(pre_st, 1, ELVES)
                          and all(life_of(pre_st, i) == STARTING_LIFE for i in (0, 1, 2)))
                    ass["A1_setup_ok"] = "passed" if ok else "failed"
                    notes.append(f"pre: zil_is_commander={is_cmdr}, life="
                                 f"{[life_of(pre_st, i) for i in (0, 1, 2)]}")
                    pre_exported = True
                    sub = json.loads(json.dumps(da))
                    sub["data"]["attacks"] = [[zil, {"type": "Player", "data": 1}]]
                    sub["data"]["bands"] = []
                    await p0.send_action({"type": "DeclareAttackers",
                                          "data": sub["data"]})
                    attacked = True
                    say(f"P0 attacks P1 with Zilortha (oid {zil})")
                    wire("p0_attacks", {"attacker": zil, "defender": 1})
                    return
                else:
                    sub = json.loads(json.dumps(da))
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                    await p0.send_action({"type": "DeclareAttackers",
                                          "data": sub["data"]})
                    if not pre_exported:
                        say(f"P0 declares no attackers (waiting: zil={zil} ked={ked} "
                            f"elves_p1={len(elves)})")
            return
        if wtype == "AssignCombatDamage":
            aa = find_action(acts, "AssignCombatDamage")
            if aa and last_assign.get(0) != p0.revision:
                last_assign[0] = p0.revision
                d = json.loads(json.dumps(aa.get("data", {})))
                say("AssignCombatDamage advertised: " + json.dumps(d)[:500])
                wire("assign_combat_damage_advertised", d)
                combat = state.get("combat", {}) or {}
                b2a = combat.get("blocker_to_attacker", {}) or {}
                a2b = {}
                for b, a in b2a.items():
                    try:
                        a2b.setdefault(int(a), []).append(int(b))
                    except (TypeError, ValueError):
                        pass
                # The engine advertises its default assignment; verify it sums
                # to the attacker's power with lethal to each blocker, then
                # submit it verbatim (CR 510.1c/d + 702.19b: 1 lethal to the
                # 1/1 Elves, 6 trample to P1).
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
                    await p0.send_action({"type": "AssignCombatDamage", "data": d})
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
            # P0 never blocks in this scenario
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": sub["data"]})
            return
        if not my_priority(state, 0):
            return
        # ---- P0 priority ----
        # 1) cast Kediss from hand when affordable
        if (bf_id(state, 0, KEDISS) is None
                and KEDISS in hand_lnames(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
            mtn = len(untapped_lands(state, 0, MOUNTAIN))
            tot = len(untapped_lands(state, 0))
            if mtn >= 1 and tot >= 2:
                for a in acts:
                    if "cast" in a["type"].lower():
                        d = a.get("data", {})
                        oid = d.get("object_id") or d.get("card") or d.get("source")
                        if isinstance(oid, int) and lname(state, oid) == KEDISS:
                            say(f"P0 casts Kediss via {a['type']}")
                            wire("cast_kediss", {"action": a["type"]})
                            await submit_as_is(p0, a)
                            return
        # 2) cast Zilortha from the command zone when affordable
        if (bf_id(state, 0, ZILORTHA) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
            mtn = len(untapped_lands(state, 0, MOUNTAIN))
            fst = len(untapped_lands(state, 0, FOREST))
            tot = len(untapped_lands(state, 0))
            if not obs["p0_cmdr_act_types"]:
                obs["p0_cmdr_act_types"] = sorted(set(a["type"] for a in acts))
                cz = [(o.get("name"), o.get("is_commander"), o.get("zone"))
                      for o in state.get("objects", {}).values()
                      if "zilortha" in str(o.get("name", "")).lower()]
                obs["cmdr_zone_seen"] = bool(cz)
                say(f"P0 PreCombatMain action types: {obs['p0_cmdr_act_types']}")
                say(f"Zilortha objects: {cz}")
                wire("p0_main_actions", {"types": obs["p0_cmdr_act_types"],
                                        "zilortha": cz})
            if mtn >= 1 and fst >= 1 and tot >= 5:
                for a in acts:
                    if "cast" in a["type"].lower():
                        d = a.get("data", {})
                        oid = d.get("object_id") or d.get("card") or d.get("source")
                        if isinstance(oid, int) and lname(state, oid) == ZILORTHA:
                            say(f"P0 casts Zilortha (commander) via {a['type']}")
                            wire("cast_zilortha", {"action": a["type"]})
                            await submit_as_is(p0, a)
                            return
        # 3) post-export once combat + trigger are done
        if (pre_exported and attacked and not post_exported
                and state.get("active_player") == 0
                and state.get("phase") in ("PostCombatMain", "EndStep", "Cleanup")):
            say("post-combat reached; exporting POST")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        # 4) normal play: land, then pass
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
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, ELVES, 1, 6)
                return
            if find_action(acts, "SelectCards") and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                await do_bottom(p1, 1, ELVES)
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": sub["data"]})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                zil = bf_id(state, 0, ZILORTHA)
                elves = bf_ids(state, 1, ELVES)
                sub = json.loads(json.dumps(da))
                if zil is not None and elves:
                    sub["data"]["assignments"] = [[elves[0], zil]]
                    say(f"P1 blocks Zilortha (oid {zil}) with Elves (oid {elves[0]})")
                    wire("p1_blocks", {"blocker": elves[0], "attacker": zil})
                else:
                    sub["data"]["assignments"] = []
                    say(f"P1 declares no blockers (zil={zil}, elves={len(elves)})")
                await p1.send_action({"type": "DeclareBlockers", "data": sub["data"]})
            return
        if wtype == "AssignBlockerDamage":
            aa = find_action(acts, "AssignBlockerDamage")
            if aa and last_assign.get(1) != p1.revision:
                last_assign[1] = p1.revision
                d = json.loads(json.dumps(aa.get("data", {})))
                zil = bf_id(state, 0, ZILORTHA)
                assigns = []
                for e in bf_ids(state, 1, ELVES):
                    pw = num(get_obj(state, e).get("power")) or 0
                    if zil is not None and pw:
                        assigns.append([int(zil), int(pw)])
                d["assignments"] = assigns
                await p1.send_action({"type": "AssignBlockerDamage", "data": d})
                say(f"P1 assigns blocker damage: {assigns}")
                wire("assign_blocker_damage_submitted", d)
            return
        if not my_priority(state, 1):
            return
        # cast Elves when affordable, play land, else pass
        if (state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1):
            if ELVES in hand_lnames(state, 1):
                forests = len(untapped_lands(state, 1, FOREST))
                cast_acts = [a for a in acts if "cast" in a["type"].lower()]
                elves_casts = [a for a in cast_acts
                               if isinstance((a.get("data", {}).get("object_id")
                                              or a.get("data", {}).get("card")
                                              or a.get("data", {}).get("source")), int)
                               and lname(state, a["data"].get("object_id")
                                         or a["data"].get("card")
                                         or a["data"].get("source")) == ELVES]
                if forests >= 1 and elves_casts:
                    for a in elves_casts:
                        say("P1 casts Llanowar Elves")
                        await submit_as_is(p1, a)
                        return
                if not elves_casts or forests < 1:
                    say(f"P1 cannot cast Elves: hand={hand_lnames(state, 1)} "
                        f"untapped_forests={forests} "
                        f"cast_actions={[ (a['type'], str(a.get('data'))[:80]) for a in cast_acts ]}")
                    wire("p1_elves_cast_blocked",
                         {"hand": hand_lnames(state, 1), "untapped_forests": forests,
                          "cast_types": [a["type"] for a in cast_acts],
                          "turn": state.get("turn_number"), "phase": state.get("phase")})
        if (ELVES in hand_lnames(state, 1)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(untapped_lands(state, 1, FOREST)) >= 1):
            for a in acts:
                if "cast" in a["type"].lower():
                    d = a.get("data", {})
                    oid = d.get("object_id") or d.get("card") or d.get("source")
                    if isinstance(oid, int) and lname(state, oid) == ELVES:
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

    async def p2_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P2"):
                await do_mulligan(p2, 2, None, 2, 2)
                return
            if find_action(acts, "SelectCards") and last_select.get(2) != p2.revision:
                last_select[2] = p2.revision
                await do_bottom(p2, 2, None)
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p2, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p2.send_action({"type": "DeclareAttackers", "data": sub["data"]})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["assignments"] = []
                await p2.send_action({"type": "DeclareBlockers", "data": sub["data"]})
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
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_lnames(s, 0)} "
                f"zil={bf_ids(s, 0, ZILORTHA)} ked={bf_ids(s, 0, KEDISS)} "
                f"elves={bf_ids(s, 1, ELVES)} life={[life_of(s, i) for i in (0, 1, 2)]} "
                f"stack={len(s.get('stack') or [])} pre={pre_exported} "
                f"attacked={attacked} post={post_exported} "
                f"trig={obs['trigger_seen']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
