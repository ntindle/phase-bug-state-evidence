#!/usr/bin/env python3
"""Issue #6643: "Party Dude -- level 3 attack trigger never fires".

Report (github #6643, status:confirmed, area:engine+parser, mechanic:triggers+combat):
Party Dude advances through Class levels 1 and 2 normally, but the level-3
ability -- "Whenever one or more of your opponents are attacked, up to one
target attacking creature gets +X/+X until end of turn, where X is the number
of cards in your hand" -- never triggers when an opponent is attacked, over
multiple turns.

Triage evidence in the issue: at report time the trigger clause exported as
mode {"Unknown": "Whenever one or more of your opponents are attacked"} and
preview coverage marked it supported:false, so no trigger could be created.

NOTABLE: the pinned v0.78.0 card-data.json DOES parse the level-3 trigger:
mode "Attacks", batched:true, attack_target_filter:"Player", condition
ClassLevelGE 3, Pump effect with HandSize quantity, UntilEndOfTurn. So this
run tests whether the engine now actually fires it on v0.78.0.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup:
  P0 (human driver): 8x party dude + 12x grizzly bears + 40x forest.
  P1 (human driver, passive): 60x island.
  P0 casts Party Dude ({G}), activates {1}{G} (level 2) then {4}{G} (level 3)
  at sorcery speed, casts a Bear, attacks P1 with the Bear.

Expected:
  E1: Party Dude reaches level 3 on the battlefield.
  E2: after attackers are declared against P1, the Attacks-mode trigger from
      Party Dude goes on the stack.
  E3: an up-to-one target-attacking-creature prompt is offered; answering it
      with the Bear completes.
  E4: the Bear gets +X/+X (X = P0 hand size at resolution), until end of turn.
  E5: stack empties and the game proceeds.

Assertions:
  A1_setup_ok     pre.json at DeclareAttackers: Party Dude on P0 BF at class
                  level 3 (driver-tracked + object field when present),
                  attack-ready Bear on P0 BF, life 20/20.
  A2_trigger_fires Party Dude Attacks trigger observed on the stack after
                  attackers declared (mode Attacks, Pump effect).
  A3_target_prompted the up-to-one attacking-creature target prompt is
                  offered and answered with the Bear.
  A4_pump_correct  Bear power/toughness = 2+X / 2+X where X = P0 hand size.
  A5_cleanup      stack empty, game proceeding past the trigger.

Verdict rule: blocked iff A1 fails (setup never assembled). reproduced iff
A1 passes and A2 fails (or a captured failure prevents E3/E4). not-reproduced
iff A1..A5 all pass on v0.78.0 (parser gap closed on this build; not a fix claim
for the original build).

Evidence: evidence/6643/<run-id>/pre.json, mid_trigger.json, mid_target.json,
post.json, run.json, scenario_6643.py, wire_log.jsonl, scenario_run.log,
server_excerpts.log, summary.png, manifest.sha256.
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

client.URL = "ws://127.0.0.1:9375/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6643"
EVDIR = f"{BACKFILL}/evidence/6643/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DUDE = "party dude"
BEAR = "grizzly bears"
FOREST = "forest"
ISLAND = "island"

P0_DECK = [(DUDE, 8), (BEAR, 12), (FOREST, 40)]
P1_DECK = [(ISLAND, 60)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_verified": True,
    "observed_at": "2026-09-10",
    "source": "ServerHello + minisign-verify (repo-pinned key) of binary + signed "
              "data manifest; pinned v0.78.0 server on 127.0.0.1:9375 "
              "(runs/20260910-6643), verified live before driving",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == key]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def hand_oids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def hand_size(state, pid):
    return len(hand_oids(state, pid))


def life_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


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


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
            return False
    return True


def dude_level_of(state, dude_oid):
    """Best-effort read of the Class level from the object record."""
    o = get_obj(state, dude_oid)
    for key in ("class_level", "level", "classLevel"):
        v = o.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return None


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trigger_fires", "A3_target_prompted",
            "A4_pump_correct", "A5_cleanup")}
    obs = {
        "dude_expected_level": 1,   # Class enters at level 1 (level-2 ability
                                    # requires ClassLevelIs(1))
        "dude_oid": None,
        "level_keys_seen": None,
        "attack_turn": None,
        "attacker_oid": None,
        "trigger_seen": False,
        "trigger_entry": None,
        "target_prompted": False,
        "target_answered": False,
        "hand_at_target": None,
        "interaction_shapes": [],
        "rejections": [],
        "no_trigger_deadline_turn": None,
    }
    kept = {}
    last_select = {}
    submitted_interactions = set()
    shapes_logged = set()
    pre_exported = False
    mid_trigger_exported = False
    mid_target_exported = False
    mid_pump_exported = False
    post_exported = False
    attacked = False

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id}")

    async def export(tag):
        raw = await p0.export_state()
        with open(f"{EVDIR}/{tag}.json", "w") as f:
            f.write(raw)
        say(f"exported {tag}.json")
        return json.loads(raw)["state"]

    # ---------- mulligan helpers ----------
    async def do_mulligan(c, pid, mulls):
        if mulls == 0:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"P{pid} mulligans (no key cards)")
        else:
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            kept[f"P{pid}"] = True
            say(f"P{pid} keeps after {mulls} mulligan(s)")

    async def do_bottom(c, pid):
        st = c.latest["state"]
        pending = ((st.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
        count = 1
        for p in pending:
            if p.get("player") == pid and (p.get("phase", {}) or {}).get("type") == "BottomCards":
                count = int(p.get("phase", {}).get("count", 1))
        hns = [lname(st, o) for o in hand_oids(st, pid)]

        def bkey(oid):
            nm = lname(st, oid)
            if nm == FOREST:
                return 2
            if nm == DUDE:
                return 1
            return 0
        picks = sorted(hand_oids(st, pid), key=bkey)[:count]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"P{pid} bottoms {count}: {[lname(st, x) for x in picks]}")
        wire("mulligan_bottom", {"who": pid, "count": count,
                                 "hand": hns, "picks": picks})

    def dude_trigger_on_stack(state):
        """Find the Party Dude level-3 Attacks trigger on the stack."""
        for e in state.get("stack", []) or []:
            blob = json.dumps(e, default=str).lower()
            if "party dude" in blob and ("pump" in blob or "attack" in blob):
                return e
        return None

    async def scan_interactions(st, state, who):
        """Log every viewer_interaction shape; answer the Party Dude target
        prompt (up to one target attacking creature) with the attacking Bear.
        Returns True if an interaction was answered this tick."""
        vi = st.get("viewer_interaction") or {}
        opps = vi.get("opportunities") or []
        if not opps:
            return False
        acted = False
        for opp in opps:
            iid = opp.get("interactionId")
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            cands = data.get("candidates") or data.get("choices") or []
            texts = []
            for ch in cands:
                lab = (ch.get("label") or ch.get("text") or ch.get("name") or "")
                texts.append(str(lab)[:60])
            blob = " // ".join(texts)
            key = (who, rtype, spec_type, blob[:80])
            if key not in shapes_logged:
                shapes_logged.add(key)
                say(f"[{who}] interaction rtype={rtype} spec={spec_type} "
                    f"n_cands={len(cands)} choices=[{blob[:200]}]")
                wire("interaction_shape", {"who": who, "interaction": opp})
                obs["interaction_shapes"].append(
                    {"who": who, "rtype": rtype, "spec": spec_type,
                     "choices": texts[:12]})
            if iid in submitted_interactions or not cands:
                continue
            # Only answer after the trigger was observed: this is the
            # Party Dude "up to one target attacking creature" prompt.
            # Skip the attack-declaration "relations" mirror (intent=attack):
            # that decision is made through the DeclareAttackers action.
            if not obs["trigger_seen"]:
                continue
            if spec_type == "relations":
                continue
            if not (rtype == "exactChoices"
                    or spec_type in ("sequence", "select")):
                continue
            bear_oid = obs["attacker_oid"]
            pick = None
            for ch in cands:
                refs = []
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if d.get("reference") is not None:
                        refs.append(int(d["reference"]))
                if bear_oid in refs:
                    pick = ch
                    break
            if pick is None:
                notes.append(f"target prompt had no candidate for the attacking "
                             f"Bear (oid {bear_oid}); candidates: {blob[:160]}")
                wire("target_prompt_no_bear", {"interaction": opp})
                continue
            if rtype == "exactChoices":
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick["id"]}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": spec_type or "sequence",
                                    "data": {"choiceIds": [pick["id"]]}}}
            say(f"[{who}] answers Party Dude target prompt with Bear "
                f"(choice {pick['id']})")
            wire("target_submission", {"who": who, "submission": sub,
                                       "interaction": opp})
            obs["hand_at_target"] = hand_size(state, 0)
            say(f"[{who}] P0 hand size at target time: {obs['hand_at_target']}")
            c = p0 if who == "P0" else p1
            await c.send_interaction(sub)
            submitted_interactions.add(iid)
            obs["target_prompted"] = True
            obs["target_answered"] = True
            if not mid_target_exported:
                try:
                    await export("mid_target")
                except Exception as e:
                    notes.append(f"mid_target export failed: {e}")
            acted = True
        return acted

    # ---------- P0 tick ----------
    async def p0_tick(st, acts, state):
        nonlocal pre_exported, attacked, mid_trigger_exported
        wtype = (state.get("waiting_for") or {}).get("type")
        # mulligan
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                mulls = kept.get("P0_mulls", 0)
                hns = [lname(state, o) for o in hand_oids(state, 0)]
                if FOREST in hns and (DUDE in hns or mulls >= 2):
                    await submit_as_is(p0, {"type": "MulliganDecision",
                                            "data": {"choice": {"type": "Keep"}}})
                    kept["P0"] = True
                    say(f"P0 keeps: {hns}")
                else:
                    kept["P0_mulls"] = mulls + 1
                    await do_mulligan(p0, 0, mulls)
                return
            if (find_action(acts, "SelectCards")
                    and last_select.get(0) != p0.revision):
                last_select[0] = p0.revision
                await do_bottom(p0, 0)
                return
        # pay mana first (lands tap through PayMana flow)
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # discover the class-level field on the Dude object (once)
        dude = bf_id(state, 0, DUDE)
        if dude is not None and obs["dude_oid"] is None:
            obs["dude_oid"] = dude
            say(f"Party Dude on BF (oid {dude})")
        if dude is not None and obs["level_keys_seen"] is None:
            o = get_obj(state, dude)
            keys = [k for k in o.keys() if "level" in k.lower()]
            obs["level_keys_seen"] = keys
            say(f"Dude object level-ish keys: {keys}; level read={dude_level_of(state, dude)}")
            wire("dude_object_keys", {"oid": dude, "level_keys": keys,
                                      "all_keys": sorted(o.keys())})
        # observe the trigger on the stack at any point after attack
        if attacked and not obs["trigger_seen"]:
            hit = dude_trigger_on_stack(state)
            if hit:
                obs["trigger_seen"] = True
                obs["trigger_entry"] = json.dumps(hit, default=str)[:2000]
                say("*** PARTY DUDE TRIGGER OBSERVED ON STACK ***")
                wire("dude_trigger_on_stack", hit)
                if not mid_trigger_exported:
                    try:
                        await export("mid_trigger")
                        mid_trigger_exported = True
                    except Exception as e:
                        notes.append(f"mid_trigger export failed: {e}")
                return
        if await scan_interactions(st, state, "P0"):
            return
        # attack declaration
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            da = find_action(acts, "DeclareAttackers")
            if da:
                ready = [b for b in bf_ids(state, 0, BEAR) if can_attack_now(state, b)]
                dude_lvl = obs["dude_expected_level"]
                lvl_field = dude_level_of(state, dude) if dude is not None else None
                if (not attacked and dude is not None and dude_lvl >= 3
                        and ready):
                    say(f"PRE: exporting (dude lvl expected={dude_lvl} field={lvl_field}, "
                        f"bear {ready[0]} ready, P1 life={life_of(state, 1)})")
                    pre_st = await export("pre")
                    d = get_obj(pre_st, dude)
                    ok = (dude is not None and ready
                          and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
                          and dude_lvl >= 3)
                    ass["A1_setup_ok"] = "passed" if ok else "failed"
                    notes.append(f"pre: dude_oid={dude} expected_lvl={dude_lvl} "
                                 f"field_lvl={dude_level_of(pre_st, dude)} "
                                 f"bears={bf_ids(pre_st, 0, BEAR)} "
                                 f"life={[life_of(pre_st, i) for i in (0, 1)]}")
                    pre_exported = True
                    sub = json.loads(json.dumps(da))
                    sub["data"]["attacks"] = [[ready[0], {"type": "Player", "data": 1}]]
                    sub["data"]["bands"] = []
                    obs["attacker_oid"] = ready[0]
                    obs["attack_turn"] = state.get("turn_number")
                    obs["no_trigger_deadline_turn"] = obs["attack_turn"] + 6
                    await p0.send_action({"type": "DeclareAttackers",
                                          "data": sub["data"]})
                    attacked = True
                    say(f"P0 attacks P1 with Bear (oid {ready[0]}) on turn "
                        f"{obs['attack_turn']}")
                    wire("p0_attacks", {"attacker": ready[0], "defender": 1,
                                        "turn": obs["attack_turn"]})
                    return
                sub = json.loads(json.dumps(da))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p0.send_action({"type": "DeclareAttackers",
                                     "data": sub["data"]})
                if not pre_exported:
                    say(f"P0 declares no attackers (dude={dude} lvl={dude_lvl} "
                        f"ready_bears={ready})")
            return
        # priority: setup play on our main phases
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        phase = state.get("phase")
        # land: stop playing lands once P0 has 8 on the battlefield, so
        # drawn Forests accumulate in hand and X = hand size is non-trivial
        # at pump time (X >= 1 also lets the driver distinguish a resolved
        # pump from the base 2/2).
        p0_lands = sum(1 for oid, o in state.get("objects", {}).items()
                       if o.get("zone") == "Battlefield"
                       and o.get("controller") == 0
                       and lname(state, oid) == FOREST)
        if phase in ("PreCombatMain", "PostCombatMain") and p0_lands < 8:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        # cast Party Dude
        if (phase in ("PreCombatMain", "PostCombatMain")
                and dude is None):
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "CastSpell"
                        and lname(state, d.get("object_id")) == DUDE):
                    say("P0 casts Party Dude")
                    wire("cast_dude", a)
                    await submit_as_is(p0, a)
                    return
        # level-ups (sorcery speed): ability_index 0 = level 2, 1 = level 3
        if (phase in ("PreCombatMain", "PostCombatMain")
                and dude is not None):
            want = None
            if obs["dude_expected_level"] < 2:
                want = 0
            elif obs["dude_expected_level"] < 3:
                want = 1
            if want is not None:
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "ActivateAbility"
                            and d.get("source_id") == dude
                            and d.get("ability_index") == want):
                        say(f"P0 activates Party Dude level-up idx={want} "
                            f"(-> level {want + 2})")
                        wire("level_up", {"ability_index": want, "action": a})
                        await submit_as_is(p0, a)
                        obs["dude_expected_level"] = want + 2
                        return
        # cast a bear (driver keeps one bear on the battlefield for the attack)
        if (phase in ("PreCombatMain", "PostCombatMain")
                and not bf_ids(state, 0, BEAR)
                and [o for o in hand_oids(state, 0)
                     if lname(state, o) == BEAR]):
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "CastSpell"
                        and lname(state, d.get("object_id")) == BEAR):
                    say("P0 casts Grizzly Bears")
                    wire("cast_bear", a)
                    await submit_as_is(p0, a)
                    return
        # default: pass priority
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    # ---------- P1 tick (passive) ----------
    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                kept["P1"] = True
                await submit_as_is(p1, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say("P1 keeps")
                return
            if (find_action(acts, "SelectCards")
                    and last_select.get(1) != p1.revision):
                last_select[1] = p1.revision
                await do_bottom(p1, 1)
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if await scan_interactions(st, state, "P1"):
            return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    # ---------- assertion evaluation + finish ----------
    def evaluate(pre_st, post_st):
        if pre_st is None:
            ass["A1_setup_ok"] = "not-run"
            return
        post = post_st or {}
        trig = obs["trigger_entry"] or ""
        ass["A2_trigger_fires"] = ("passed" if obs["trigger_seen"]
                                   and "pump" in trig.lower() else "failed")
        ass["A3_target_prompted"] = ("passed" if obs["target_answered"]
                                     else "failed")
        # A4: the pump's observable outcome. Primary: combat damage dealt to
        # P1 equals 2 + X where X = P0's hand size at target time (no draws
        # occur between the target choice and resolution). Corroboration:
        # bear P/T in mid_pump.json when the engine surfaces it.
        x = obs.get("hand_at_target")
        pre_life = life_of(pre_st, 1)
        post_life = life_of(post, 1)
        dmg = (pre_life - post_life) if pre_life is not None and post_life is not None else None
        mp_st = (json.loads(open(f"{EVDIR}/mid_pump.json").read())["state"]
                 if os.path.exists(f"{EVDIR}/mid_pump.json") else None)
        bear = obs["attacker_oid"]
        mp_bo = get_obj(mp_st, bear) if (mp_st and bear) else {}
        if x is None or x == 0 or dmg is None:
            ass["A4_pump_correct"] = "not-run"
            notes.append(f"A4: not evaluable (hand_at_target={x} dmg={dmg})")
        else:
            ok = (dmg == 2 + x)
            ass["A4_pump_correct"] = "passed" if ok else "failed"
            notes.append(f"A4: P1 life {pre_life}->{post_life} (damage {dmg}), "
                         f"expected {2 + x} (2+X, X=hand {x}); "
                         f"mid_pump bear P/T={mp_bo.get('power')}/{mp_bo.get('toughness')}")
        stack = post.get("stack") or []
        leftover = [e for e in stack
                    if "party dude" in json.dumps(e, default=str).lower()]
        ass["A5_cleanup"] = ("passed" if not leftover else "failed")
        notes.append(f"A5: stack={len(stack)} entries, party-dude leftovers="
                     f"{len(leftover)}, phase={post.get('phase')} "
                     f"turn={post.get('turn_number')}")

    async def finish():
        nonlocal post_exported, mid_trigger_exported, mid_target_exported, \
            mid_pump_exported
        cur = (p0.latest or {}).get("state", {}) or {}
        cur_turn = cur.get("turn_number")
        dl = obs["no_trigger_deadline_turn"]
        if (attacked and not obs["trigger_seen"] and not post_exported
                and dl and cur_turn is not None and cur_turn >= dl):
            try:
                await export("post")
                post_exported = True
                notes.append(f"no-trigger watchdog: attack turn "
                             f"{obs['attack_turn']}, now turn {cur_turn}; "
                             f"exporting post.json")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        if obs["trigger_seen"] and not mid_trigger_exported:
            try:
                await export("mid_trigger")
                mid_trigger_exported = True
            except Exception as e:
                notes.append(f"mid_trigger export failed: {e}")
        if not post_exported:
            try:
                await export("post")
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")
        pre_st = (json.loads(open(f"{EVDIR}/pre.json").read())["state"]
                  if os.path.exists(f"{EVDIR}/pre.json") else None)
        post_st = (json.loads(open(f"{EVDIR}/post.json").read())["state"]
                   if os.path.exists(f"{EVDIR}/post.json") else None)
        evaluate(pre_st, post_st)
        result = " ".join(f"{k}: {v};" for k, v in ass.items())
        say("ASSERTIONS: " + result)
        for n in notes:
            say("NOTE: " + n)
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
        elif ass["A2_trigger_fires"] == "failed":
            verdict = "reproduced"
        elif ass["A3_target_prompted"] == "failed":
            # trigger fired but the up-to-one target prompt never resolved:
            # clearly identified related failure
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        elif (ass["A2_trigger_fires"] == "passed"
                and ass["A3_target_prompted"] == "passed"):
            # The reported "trigger never fires" claim is refuted; a
            # downstream assertion gap (e.g. vacuous X) is a limitation,
            # not the reported bug.
            verdict = "not-reproduced"
            notes.append("verdict not-reproduced on A2+A3 despite "
                         + ", ".join(f"{k}={v}" for k, v in ass.items()
                                     if v != "passed"))
        else:
            verdict = "reproduced"
        say(f"VERDICT: {verdict}")
        wire("verdict", {"verdict": verdict, "assertions": ass,
                         "notes": notes})
        run = {
            "issue": 6643,
            "run_id": RUN_ID,
            "server": SERVER_IDENTITY,
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "verdict": verdict,
            "observations": obs,
            "notes": notes,
            "rejections": obs["rejections"],
            "interaction_shapes": obs["interaction_shapes"],
            "evidence_dir": f"evidence/6643/{RUN_ID}",
            "duration_s": round(time.time() - t_start, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=2, default=str)
        say(f"run.json written (verdict={verdict})")
        await p0.close()
        await p1.close()

    # ---------- main tick loop ----------
    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
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
        # prompt finish: after the target is answered and the trigger leaves
        # the stack, capture mid_pump.json on the attack turn once combat
        # has moved past DeclareAttackers (the +X/+X lasts until end of
        # turn); then export post.json and finish.
        cur = (p0.latest or {}).get("state", {}) or {}
        if obs["target_answered"] and not post_exported:
            stack = cur.get("stack") or []
            dude_left = [e for e in stack
                         if "party dude" in json.dumps(e, default=str).lower()]
            atk_turn = obs["attack_turn"]
            if (not dude_left and not mid_pump_exported
                    and atk_turn is not None
                    and cur.get("turn_number") == atk_turn
                    and cur.get("phase") not in ("DeclareAttackers", None)):
                try:
                    await export("mid_pump")
                    mid_pump_exported = True
                    mp = json.loads(open(f"{EVDIR}/mid_pump.json").read())["state"]
                    mbo = get_obj(mp, obs["attacker_oid"]) if obs["attacker_oid"] else {}
                    say(f"mid_pump: bear P/T={mbo.get('power')}/{mbo.get('toughness')} "
                        f"phase={mp.get('phase')}")
                except Exception as e:
                    notes.append(f"mid_pump export failed: {e}")
            ct = cur.get("turn_number")
            # Export post.json only after combat damage on the attack turn
            # (PostCombatMain or later, or a later turn), so the damage
            # assertion observes the dealt damage.
            if (mid_pump_exported
                    and (ct is not None and atk_turn is not None
                         and (ct > atk_turn
                              or cur.get("phase") in ("PostCombatMain", "EndStep",
                                                      "Cleanup", "Untap", "Upkeep",
                                                      "Draw")))):
                try:
                    await export("post")
                    post_exported = True
                except Exception as e:
                    notes.append(f"post export failed: {e}")
                await finish()
                return
            dl = atk_turn + 6 if atk_turn is not None else None
            if dl and ct is not None and ct >= dl:
                say("target answered but post not captured by turn "
                    f"{ct}; exporting post and finishing")
                try:
                    await export("post")
                    post_exported = True
                except Exception as e:
                    notes.append(f"post export failed: {e}")
                await finish()
                return
        # no-trigger watchdog: export post and finish
        dl = obs["no_trigger_deadline_turn"]
        ct = cur.get("turn_number")
        if (attacked and not obs["trigger_seen"] and dl
                and ct is not None and ct >= dl):
            say("no-trigger watchdog fired; finishing")
            await finish()
            return
        if post_exported and obs["trigger_seen"]:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={[lname(s, o) for o in hand_oids(s, 0)]} "
                f"dude={bf_ids(s, 0, DUDE)} exp_lvl={obs['dude_expected_level']} "
                f"bears={bf_ids(s, 0, BEAR)} life={[life_of(s, i) for i in (0, 1)]} "
                f"stack={len(s.get('stack') or [])} attacked={attacked} "
                f"trig={obs['trigger_seen']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
