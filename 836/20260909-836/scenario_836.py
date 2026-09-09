#!/usr/bin/env python3
"""Issue #836: Hero of Bladehold - Tokens are being spawned but Battle cry
trigger is not happening.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, confirmed; labels: area:engine, mechanic:triggers/tokens/combat,
priority:p2-wrong-game-result): "Tokens are being spawned but Battle cry trigger
is not happening."

Oracle text (pinned card-data.json, Hero of Bladehold):
  "Battle cry (Whenever this creature attacks, each other attacking creature
  gets +1/+0 until end of turn.)
  Whenever this creature attacks, create two 1/1 white Soldier creature tokens
  that are tapped and attacking."
Parsed as: keywords [Battlecry] + two Attacks triggers:
  triggers[0] = Effect::Token (2x 1/1 white Soldier, tapped, enters_attacking)
  triggers[1] = Effect::PumpAll {+1/+0, creature + Attacking + Another}

The report conflates two sub-behaviors:
  (a) whether the battle cry trigger fires at all (pumps declared co-attackers), and
  (b) whether the tokens get pumped, which is ORDER-dependent and
      rules-correct either way per the MTGJSON ruling on this card:
        "If the token-creating ability resolves first, the tokens each get
         +1/+0 until end of turn from the battle cry ability" and
        "Although the tokens are attacking, they never were declared as
         attacking creatures."
  (CR 702.91a: battle cry pumps each OTHER attacking creature +1/+0 EOT.)

Setup (native engine, two human-client seats):
  P0: 8x Hero of Bladehold, 4x Silvercoat Lion (1W 2/2 vanilla co-attacker),
      48x Plains. (8x Hero density: engine accepts >4-of for custom games;
      mulligan to Hero + 3 lands. Lion is plain white 2/2 vanilla.)
  P1: 60x Forest dummy (plays a land, passes; declares no blockers).

Expected (per card text + ruling):
  E1: Hero attacks with a declared co-attacker (Silvercoat Lion).
  E2: two 1/1 Soldier tokens are created tapped and attacking (token trigger).
  E3: battle cry pumps every other attacking creature +1/+0 until EOT:
      the declared co-attacker (2/2 -> 3/2) ALWAYS, the tokens (1/1 -> 2/1)
      only if the token trigger resolved before battle cry (else 1/1, which is
      ruling-correct, not a bug).
  E4: Hero itself is NOT pumped (Another excludes self): stays 3/4.
  E5: combat completes, stack empties, game proceeds.

Assertions:
  A1_setup_ok        pre-attack: Hero + Lion on BF (P0), both untapped,
                     summoning-sickness-free, exported pre.json
  A2_two_triggers    exactly 2 Soldier tokens created AND Lion pumped to 3/2
                     (joint proof both Attacks triggers fired)
  A3_battlecry_pumps_attacker  Lion 2/2 -> 3/2 after both triggers resolve
  A4_tokens_order_aware        if token trigger resolved first: tokens 2/1
                     (ruling branch); if battle cry first: tokens 1/1
                     (also ruling-correct). FAIL only if the observed order
                     and sizes contradict the ruling.
  A5_hero_unpumped   Hero stays 3/4 (Another excludes self)
  A6_cleanup         tokens remain on BF (attacking), stack empty, Hero in
                     combat, game proceeds; post.json exported

Verdict rule: reproduced iff A2/A3/A5 fail on the exercised path
(tokens spawned but no pump on the declared co-attacker = reported bug).
not-reproduced iff A1..A6 all pass. blocked iff the attack/trigger flow
cannot be driven to completion.

Evidence: evidence/836/<run-id>/pre.json (post DeclareAttackers, pre-trigger
resolution), mid_*.json (after first trigger resolved), post.json (combat
done), run.json, manifest.sha256, summary.png, scenario_836.py, wire_log.jsonl,
scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-836"
EVDIR = f"{BACKFILL}/evidence/836/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

HERO = "Hero of Bladehold"
LION = "Silvercoat Lion"
PLAINS = "Plains"
SOLDIER = "Soldier"
FOREST = "Forest"

P0_DECK = [(HERO, 8), (LION, 4), (PLAINS, 48)]
P1_DECK = [(FOREST, 60)]

SERVER_IDENTITY = {
    "server_version": "0.77.0",
    "build_commit": "61715b5",
    "protocol_version": 67,
    "mode": "Full",
    "binary_sha256": "a52293b754605baa63e4d987a5208902ec394868d49c4bfd86b3fd57f3267c6a",
    "card_data_sha256": "698350d9b6323011a5b86a74a4d2ea54d13b4ed26a520579be7d044f0a3692e5",
    "draft_pools_sha256": "56e030fdc74b2385759310de8564a58b8716035b2dccc0da709f37250cc2d3c7",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 match of pinned verified artifacts; "
              "server started fresh this run (runs/20260909-836)",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return None


def pt(obj):
    return num(obj.get("power")), num(obj.get("toughness"))


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == name]


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def untapped_plains(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == PLAINS and not o.get("tapped")]


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
            return False
    return True


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
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_two_triggers", "A3_battlecry_pumps_attacker",
            "A4_tokens_order_aware", "A5_hero_unpumped", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}

    pre_exported = False
    mid_exported = set()
    post_exported = False
    attackers_declared = False
    ordered = False
    hero_submitted = False
    lion_submitted = False
    order_obs = {"first_resolved": None, "tokens": None, "lion_p": None}
    stack_ever_nonempty = False
    obj_dumped = False

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")
        core = ["A1_setup_ok", "A2_two_triggers", "A3_battlecry_pumps_attacker",
                "A4_tokens_order_aware", "A5_hero_unpumped"]
        if all(ass[k] == "passed" for k in core):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] == "passed" and any(ass[k] == "failed" for k in core):
            verdict = "reproduced"
        else:
            verdict = "blocked"
            notes.append("inconclusive or setup incomplete; see notes")
        notes.append(f"A6_cleanup={ass['A6_cleanup']} (advisory, not verdict-driving)")
        run = {
            "issue": 836,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_836.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "verdict": verdict,
            "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
            "setup_line": "P0: 8x Hero of Bladehold + 4x Silvercoat Lion + 48x Plains "
                          "(8x density so Hero lands in the opener; mulligan-to-hero); "
                          "P1: 60x Forest dummy",
            "contract_line": "P0 attacks with Hero + Silvercoat Lion; both Attacks triggers "
                             "must fire (2 Soldier tokens + battle cry +1/+0 on other "
                             "attackers); Hero stays 3/4; token pump depends on observed "
                             "resolution order per the card ruling",
            "stats": {},
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

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def stack_len(state):
        return len(state.get("stack", []) or [])

    def resolution_snapshot(state):
        toks = battlefield_ids(state, 0, SOLDIER)
        lions = battlefield_ids(state, 0, LION)
        lion_p = pt(get_obj(state, lions[0]))[0] if lions else None
        return toks, lion_p, stack_len(state)

    async def observe_triggers(state):
        """After DeclareAttackers + ordering, watch which trigger resolves first."""
        nonlocal ordered
        toks, lion_p, slen = resolution_snapshot(state)
        if slen > 0:
            stack_ever_nonempty_note.append(slen)
        # first resolution observed while the other trigger is still pending
        if attackers_declared and ordered and order_obs["first_resolved"] is None:
            if len(toks) == 2 and slen > 0:
                order_obs.update(first_resolved="token_trigger",
                                 tokens=[pt(get_obj(state, t)) for t in toks],
                                 lion_p=lion_p)
                say(f"FIRST RESOLVED: token trigger (2 tokens on BF, stack still {slen})")
                wire("first_resolved", order_obs)
                if "token" not in mid_exported:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_tokenfirst.json", "w") as f:
                        f.write(mid)
                    mid_exported.add("token")
                    say("exported MID (token trigger resolved first)")
            elif lion_p == 3 and slen > 0:
                order_obs.update(first_resolved="battlecry",
                                 tokens=[pt(get_obj(state, t)) for t in toks],
                                 lion_p=lion_p)
                say(f"FIRST RESOLVED: battle cry (Lion 3/2, stack still {slen})")
                wire("first_resolved", order_obs)
                if "battlecry" not in mid_exported:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_battlecryfirst.json", "w") as f:
                        f.write(mid)
                    mid_exported.add("battlecry")
                    say("exported MID (battle cry resolved first)")

    stack_ever_nonempty_note = []

    def dump_pending_triggers(st, state):
        # try to find pending trigger descriptions for the wire log
        for key in ("pending_triggers", "triggers", "trigger_queue", "pending"):
            v = state.get(key)
            if v:
                say(f"state[{key}]: " + json.dumps(v, default=str)[:1200])
                wire("pending_triggers_" + key, v)

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, attackers_declared, ordered, obj_dumped, post_exported
        nonlocal hero_submitted, lion_submitted
        wtype = (state.get("waiting_for") or {}).get("type")
        if not obj_dumped:
            for oid, o in state.get("objects", {}).items():
                if o.get("zone") == "Battlefield" and o.get("controller") == 0 \
                        and (o.get("base_name") or o.get("name")) in (HERO, LION):
                    say("creature object sample: " + json.dumps(o, default=str)[:700])
                    wire("creature_object_sample", o)
                    obj_dumped = True
                    break
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            hn = hand_names(state, 0)
            lands = sum(1 for n in hn if n == PLAINS)
            mulls = kept.get("P0_mulls", 0)
            if (HERO in hn and lands >= 3) or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps opening hand (hero={HERO in hn}, lands={lands})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1} (hero={HERO in hn}, lands={lands})")
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
                hand_ids = [o for pl in state.get("players", [])
                            if pl.get("id") == 0 for o in pl.get("hand", [])]
                def bottom_key(oid):
                    nm = obj_name(state, oid)
                    return 0 if nm == PLAINS else (2 if nm == HERO else 1)
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}: {[obj_name(state, x) for x in picks]}")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                dump_pending_triggers(st, state)
                say(f"OrderTriggers options: {json.dumps(oa.get('data'))[:300]}")
                wire("order_triggers", oa.get("data"))
                await submit_as_is(p0, oa)
                if attackers_declared:
                    ordered = True
                    notes.append(f"trigger order submitted as advertised: {oa.get('data')}")
                say("P0 submits trigger order as advertised")
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                heroes = battlefield_ids(state, 0, HERO)
                lions = battlefield_ids(state, 0, LION)
                ready_h = [o for o in heroes if can_attack_now(state, o)]
                ready_l = [o for o in lions if can_attack_now(state, o)]
                # KEY: attack only when Hero AND a Lion are both ready; never
                # attack solo, so the game cannot end before the joint attack.
                if len(ready_h) >= 1 and len(ready_l) >= 1 and not attackers_declared:
                    atks = [[ready_h[0], {"type": "Player", "data": 1}],
                            [ready_l[0], {"type": "Player", "data": 1}]]
                    sub["data"]["attacks"] = atks
                    sub["data"]["bands"] = []
                    say(f"P0 attacks with {[obj_name(state, a[0]) for a in atks]}")
                    wire("p0_attacks", sub["data"])
                    await p0.send_action({"type": "DeclareAttackers", "data": sub["data"]})
                    attackers_declared = True
                else:
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                    if not attackers_declared:
                        say(f"P0 holds attackers (hero={len(ready_h)} lion={len(ready_l)})")
                    await p0.send_action({"type": "DeclareAttackers", "data": sub["data"]})
                return
        await observe_triggers(state)
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        # export PRE right before attacking (PreCombatMain of the attack turn)
        if (not pre_exported and state.get("phase") == "PreCombatMain"
                and battlefield_ids(state, 0, HERO)
                and any(can_attack_now(state, o) for o in battlefield_ids(state, 0, HERO))
                and any(can_attack_now(state, o) for o in battlefield_ids(state, 0, LION))):
            say("SETUP READY; exporting PRE")
            pre = await p0.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre)
            pre_st = json.loads(pre)["state"]
            ok = (len(battlefield_ids(pre_st, 0, HERO)) >= 1
                  and len(battlefield_ids(pre_st, 0, LION)) >= 1
                  and len(untapped_plains(pre_st, 0)) >= 1)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"pre-attack setup: hero+lion on BF, "
                         f"untapped plains={len(untapped_plains(pre_st,0))}")
            pre_exported = True
        # cast Hero when affordable (once)
        if (not hero_submitted
                and not battlefield_ids(state, 0, HERO)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and HERO in hand_names(state, 0)
                and len(untapped_plains(state, 0)) >= 4):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == HERO:
                    say("P0 casts Hero of Bladehold")
                    wire("cast_hero", a)
                    await submit_as_is(p0, a)
                    hero_submitted = True
                    return
        # cast Lion when affordable (once)
        if (not lion_submitted
                and not battlefield_ids(state, 0, LION)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and LION in hand_names(state, 0)
                and len(untapped_plains(state, 0)) >= 2):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == LION:
                    say("P0 casts Silvercoat Lion")
                    wire("cast_lion", a)
                    await submit_as_is(p0, a)
                    lion_submitted = True
                    return
        # post-combat assertions
        if attackers_declared and ordered and not post_exported:
            toks, lion_p, slen = resolution_snapshot(state)
            heroes = battlefield_ids(state, 0, HERO)
            hero_p, hero_t = (pt(get_obj(state, heroes[0])) if heroes else (None, None))
            both_resolved = (slen == 0 and order_obs["first_resolved"] is not None)
            past_combat = state.get("phase") in ("PostCombatMain", "End", "Cleanup",
                                                 "DeclareBlockers", "CombatDamage")
            if both_resolved or (len(toks) == 2 and lion_p == 3 and slen == 0):
                say(f"both triggers resolved: tokens={len(toks)} lion={lion_p} "
                    f"hero={hero_p}/{hero_t} stack={slen} phase={state.get('phase')}")
                wire("both_resolved", {"tokens": len(toks), "lion_p": lion_p,
                                       "hero": [hero_p, hero_t],
                                       "order": order_obs["first_resolved"]})
                n_tok = len(toks)
                # A2: joint proof both Attacks triggers fired
                if n_tok == 2 and lion_p == 3:
                    ass["A2_two_triggers"] = "passed"
                    notes.append("both Attacks triggers fired: 2 Soldier tokens created, "
                                 "Lion pumped 2/2->3/2")
                else:
                    ass["A2_two_triggers"] = "failed"
                    notes.append(f"trigger proof wrong: tokens={n_tok}, lion power={lion_p}")
                # A3: battle cry pumps the declared co-attacker
                if lion_p == 3:
                    ass["A3_battlecry_pumps_attacker"] = "passed"
                    notes.append("battle cry pumped declared co-attacker Lion 2/2->3/2")
                else:
                    ass["A3_battlecry_pumps_attacker"] = "failed"
                    notes.append(f"battle cry did NOT pump Lion: power={lion_p} (REPORTED BUG)")
                # A4: order-aware token assertion per the card ruling
                tok_pts = [pt(get_obj(state, t)) for t in toks]
                first = order_obs["first_resolved"]
                if first == "token_trigger":
                    if all(p == 2 and t == 1 for p, t in tok_pts):
                        ass["A4_tokens_order_aware"] = "passed"
                        notes.append("token trigger resolved first; tokens 1/1->2/1 per ruling")
                    else:
                        ass["A4_tokens_order_aware"] = "failed"
                        notes.append(f"token trigger resolved first but tokens not pumped: {tok_pts}")
                elif first == "battlecry":
                    if all(p == 1 and t == 1 for p, t in tok_pts):
                        ass["A4_tokens_order_aware"] = "passed"
                        notes.append("battle cry resolved first; tokens 1/1 (ruling-correct, "
                                     "they were not attacking when it resolved)")
                    else:
                        ass["A4_tokens_order_aware"] = "failed"
                        notes.append(f"battle cry first but tokens wrong: {tok_pts}")
                else:
                    ass["A4_tokens_order_aware"] = "failed"
                    notes.append("resolution order not observed; cannot evaluate ruling branch")
                # A5: Hero not pumped (Another excludes self)
                if hero_p == 3 and hero_t == 4:
                    ass["A5_hero_unpumped"] = "passed"
                    notes.append("Hero stays 3/4 (battle cry Another excludes self)")
                else:
                    ass["A5_hero_unpumped"] = "failed"
                    notes.append(f"Hero wrongly {hero_p}/{hero_t}")
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("exported POST (both triggers resolved)")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
        # normal setup play
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    codes = [s.get("data", {}).get("code")
                             for s in ch.get("surfaces", []) or []]
                    if "passPriority" in codes and ch.get("status", {}).get("type") == "available":
                        rtype = (opp.get("response", {}) or {}).get("type")
                        if rtype == "exactChoices":
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "choose",
                                                "data": {"choiceId": ch["id"]}}}
                        else:
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "sequence",
                                                "data": {"choiceIds": [ch["id"]]}}}
                        await p0.send_interaction(sub)
                        return

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps opening hand")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {})); d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
                say("P1 declares no blockers")
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
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []):
                data = (opp.get("response", {}) or {}).get("data", {}) or {}
                for ch in data.get("choices") or []:
                    codes = [s.get("data", {}).get("code")
                             for s in ch.get("surfaces", []) or []]
                    if "passPriority" in codes and ch.get("status", {}).get("type") == "available":
                        rtype = (opp.get("response", {}) or {}).get("type")
                        if rtype == "exactChoices":
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "choose",
                                                "data": {"choiceId": ch["id"]}}}
                        else:
                            sub = {"interactionId": opp.get("interactionId"),
                                   "response": {"type": "sequence",
                                                "data": {"choiceIds": [ch["id"]]}}}
                        await p1.send_interaction(sub)
                        return

    t0 = time.time()
    last = {}
    last_diag = 0.0
    last_tick_at = {}
    stuck_deadline = None
    while time.time() - t0 < 1500:
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
        # A6: after both triggers resolved and game proceeds
        if post_exported and ass["A6_cleanup"] == "not-run":
            s = p0.latest["state"] if p0.latest else {}
            wf = (s.get("waiting_for") or {}).get("type")
            toks = battlefield_ids(s, 0, SOLDIER)
            slen = stack_len(s)
            if slen == 0 and len(toks) == 2 \
                    and wf in ("Priority", "DeclareAttackers", "DeclareBlockers",
                               "CombatDamage", "PostCombatMain", None,
                               "DiscardToHandSize", "Untap"):
                ass["A6_cleanup"] = "passed"
                notes.append(f"both triggers resolved; tokens remain on BF; stack empty; "
                             f"game proceeding (wf={wf}, phase={s.get('phase')})")
                say("all assertions resolved; finishing")
                await finish()
                return
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"cleanup wrong: tokens={len(toks)}, stack={slen}, wf={wf}")
                say("all assertions resolved; finishing")
                await finish()
                return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            acts0 = [a["type"] for a in merged_actions(p0.latest)] if p0.latest else []
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)} "
                f"hero={len(battlefield_ids(s,0,HERO))} lion={len(battlefield_ids(s,0,LION))} "
                f"P0acts={acts0[:10]} declared={attackers_declared} ordered={ordered} "
                f"first={order_obs['first_resolved']} post={post_exported}")
        in_flight = attackers_declared and not post_exported
        if in_flight and stuck_deadline is None:
            stuck_deadline = time.time() + 240
        if not in_flight:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("attackers declared but trigger flow stalled 240s; see wire log")
            await finish()
            return
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
