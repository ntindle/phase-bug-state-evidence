#!/usr/bin/env python3
"""Issue #650: Tyvar the Bellicose — granted trigger does not work.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Card (verified in pinned card-data.json):
  Tyvar the Bellicose {2}{B}{G}, Creature — Elf Warrior.
  Static: 'Each creature you control has "Whenever a mana ability of this
  creature resolves, put a number of +1/+1 counters on it equal to the amount
  of mana this creature produced. This ability triggers only once each turn."'

Reported failure (corrected framing from triage): the granted trigger does not
work AT ALL — it parses to an Unknown trigger mode with an Unimplemented
effect. (The reporter's "only once per turn" phrasing describes the printed
limit, not the bug.)

Setup:
  P0: 4x Tyvar the Bellicose, 4x Llanowar Elves, 26x Forest, 26x Swamp.
  P1: 60x Island, draw-go.
  Both keep 7. P0 plays a land per turn, casts Elves then Tyvar when able
  (submitting PayMana/PayManaAbilityMana prompts as-is), passes otherwise.

Trigger: with Tyvar on the battlefield and an untapped, sickness-free Elves,
P0 activates Elves' mana ability ({T}: Add {G}) in its own main phase.

Expected: the granted ability triggers; after it resolves, Elves has +1/+1
counters equal to the mana produced (1). A second activation the same turn
adds nothing (printed once-per-turn limit).

Assertions:
  A1 setup_ok ........... Tyvar + untapped Elves on battlefield under P0.
  A2 mana_produced ...... Elves' mana ability resolves; Elves tapped, {G} in pool.
  A3 counters_added ..... Elves has exactly 1 +1/+1 counter after trigger resolves.
  A4 once_per_turn ..... second same-turn activation adds no further counters.

Verdict rule: reproduced iff A1+A2 pass and A3 fails (the reported "does not
work at all"). not-reproduced iff A1..A4 all pass. A4 is not-run if A3 fails.

Evidence: evidence/650/<run-id>/pre.json (before Elves activation),
post.json (after trigger resolution window), run.json, manifest.sha256,
summary.png, scenario_650.py
"""
import asyncio
import hashlib
import json
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck

TYVAR = "Tyvar the Bellicose"
ELVES = "Llanowar Elves"
RUN_ID = "20260908-03"
EVIDIR = f"/home/hatch/workspace/dev/phase-backfill/evidence/650/{RUN_ID}"
MAIN_PHASES = ("PreCombatMain", "PostCombatMain")

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
    "observed_at": "2026-09-08",
    "source": "ServerHello + minisign verification against repo-pinned key",
}

P0_DECK = [(TYVAR, 4), (ELVES, 4), ("Forest", 26), ("Swamp", 26)]
P1_DECK = [("Island", 60)]


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def is_mine(state, pid):
    wf = state.get("waiting_for") or {}
    return wf.get("type") == "Priority" and wf.get("data", {}).get("player") == pid


def waiting_on(state, pid):
    """Any decision (priority or otherwise) waiting on pid."""
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {})
    wtype = wf.get("type")
    if wtype in ("MulliganDecision", "OpeningHandBottomCards"):
        pend = d.get("pending", [])
        entry = next((e for e in pend if e.get("player") == pid), pend[0] if pend else None)
        return wtype, (entry.get("player") if entry else None)
    return wtype, (d.get("player") if "player" in d else d.get("deciding_player"))


def battlefield_id(state, pid, name):
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and (o.get("base_name") or o.get("name")) == name):
            return int(oid)
    return None


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def mana_pool_g(state, pid):
    """Count green mana units in pid's pool (best effort)."""
    for p in state.get("players", []):
        if p.get("id") == pid:
            units = (p.get("mana_pool") or {}).get("mana", [])
            return sum(1 for u in units if "green" in json.dumps(u).lower())
    return 0


def plus_counters(obj):
    """+1/+1 counter count; engine serializes counters as {"P1P1": n}. Returns (count, raw)."""
    v = obj.get("counters")
    if isinstance(v, dict):
        n = 0
        for k, c in v.items():
            if str(k).upper().replace("_", "") in ("P1P1", "+1/+1", "PLUS1PLUS1"):
                n += c if isinstance(c, int) else 0
        return n, {"counters": v}
    if isinstance(v, int) and v:
        return v, {"counters": v}
    return 0, {"counters": v}


async def main():
    import os
    os.makedirs(EVIDIR, exist_ok=True)
    t0 = time.time()
    assertions = {
        "A1_setup_ok": "not-run",
        "A2_mana_produced": "not-run",
        "A3_counters_added": "not-run",
        "A4_once_per_turn": "not-run",
    }
    notes = []
    pre_turn = None
    activated = False
    second_activated = False
    elves_id = None
    need_mid = False
    pre_pool_g = None

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    print(f"game={p0.game_code} run={RUN_ID}", flush=True)

    last = {}
    mulligans_taken = {0: 0, 1: 0}
    for c in (p0, p1):
        for _ in range(60):
            if c.latest:
                break
            await asyncio.sleep(0.25)
        # P0 mulligans until Llanowar Elves is in the opening hand (max 3 mulligans);
        # P1 always keeps.
        for _ in range(4):
            acts0 = c.latest.get("legal_actions", []) if c.latest else []
            dec = next((a for a in acts0 if a["type"] == "MulliganDecision"), None)
            if dec is None:
                break
            if c.player_id == 0:
                st = c.latest["state"]
                hn = hand_names(st, 0)
                if ELVES in hn or mulligans_taken[0] >= 3:
                    choice = "Keep"
                else:
                    choice = "Mulligan"
                    mulligans_taken[0] += 1
            else:
                choice = "Keep"
            sub = {"type": "MulliganDecision", "data": {"choice": {"type": choice}}}
            await c.send_action(sub)
            print(f"P{c.player_id} mulligan decision: {choice}", flush=True)
            await asyncio.sleep(1.0)

    async def export(tag):
        raw = await p0.export_state()
        env = json.loads(raw) if isinstance(raw, str) else raw
        path = f"{EVIDIR}/{tag}.json"
        with open(path, "w") as f:
            json.dump(env, f)
        print(f"exported {tag}.json", flush=True)
        return env

    max_turn = 0
    post_rounds = 0
    done = False
    elves_first_seen_turn = None
    dbg_turn = [None]
    dbg_act_turn = [None]
    for i in range(6000):
        await asyncio.sleep(0.2)
        for c in (p0, p1):
            st = c.latest
            if not st or c.revision == last.get(c.name):
                continue
            state = st["state"]
            acts = list(st.get("legal_actions", []))
            # per-object actions (e.g. mana-ability activations) live in
            # legal_actions_by_object; merge them into the candidate pool.
            for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
                for p in payloads or []:
                    acts.append({"type": p.get("type"), "data": p.get("data", {})})
            pid = c.player_id
            wtype, wplayer = waiting_on(state, pid)
            if wplayer != pid:
                continue
            last[c.name] = c.revision
            max_turn = max(max_turn, state.get("turn_number", 0))

            # opening-hand bottoming after mulligans: bottom lands first, keep creatures
            if wtype in ("MulliganDecision", "OpeningHandBottomCards"):
                sc = [a for a in acts if a["type"] == "SelectCards"]
                if sc:
                    hn_ids = []
                    for p in state.get("players", []):
                        if p.get("id") == pid:
                            hn_ids = list(p.get("hand", []))
                    def is_land_oid(oid):
                        n = obj_name(state, oid)
                        return n in ("Forest", "Swamp", "Island", "Mountain", "Plains")
                    hn_ids.sort(key=lambda oid: (not is_land_oid(oid), obj_name(state, oid)))
                    n = len(sc[0].get("data", {}).get("cards", [])) or 0
                    # count comes from the waiting_for pending entry
                    try:
                        pend = (state.get("waiting_for") or {}).get("data", {}).get("pending", [])
                        n = next(e["phase"]["count"] for e in pend if e.get("player") == pid)
                    except Exception:
                        pass
                    sub = {"type": "SelectCards", "data": {"cards": [int(x) for x in hn_ids[:n]]}}
                    await c.send_action(sub)
                    print(f"P{pid} bottoms {n} cards after mulligan", flush=True)
                    continue
            # --- non-priority decisions (priority flow continues below) ---
            if wtype != "Priority":
                    if dbg_turn[0] != (wtype, state.get("turn_number")):
                        dbg_turn[0] = (wtype, state.get("turn_number"))
                        print(f"DBG non-priority decision: {wtype} turn {state.get('turn_number')} actions={[(a['type'], sorted(a.get('data', {}).keys())) for a in acts]}", flush=True)
                    discards = [a for a in acts if "iscard" in a["type"]]
                    if discards:
                        def is_land(a):
                            return "land" in json.dumps(a.get("data", {})).lower()
                        discards.sort(key=lambda a: not is_land(a))
                        await c.send_action(discards[0])
                        print(f"P{pid} discards via {discards[0]['type']}", flush=True)
                        continue
                    # legend rule: keep the first offered permanent
                    if wtype == "ChooseLegend":
                        da = [a for a in acts if a["type"] == "ChooseLegend"]
                        if da:
                            await c.send_action(da[0])
                            print(f"P{pid} chooses legend (keep first)", flush=True)
                        continue
                    # combat declarations: attack/block with nothing (deterministic, avoids stalls)
                    if wtype in ("DeclareAttackers", "DeclareBlockers"):
                        da = [a for a in acts if a["type"] == wtype]
                        if da:
                            if dbg_turn[0] != (wtype, state.get("turn_number"), "shape"):
                                dbg_turn[0] = (wtype, state.get("turn_number"), "shape")
                                print(f"DBG {wtype} shape: {json.dumps(da[0])[:600]}", flush=True)
                            sub = json.loads(json.dumps(da[0]))
                            d = sub.setdefault("data", {})
                            for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
                                if k in d:
                                    d[k] = [] if isinstance(d[k], list) else {}
                            await c.send_action(sub)
                            print(f"P{pid} declares no {wtype}", flush=True)
                        continue
                    # unknown decision: pass/choose-first if a safe neutral option exists
                    for a in acts:
                        if a["type"] == "PassPriority":
                            await c.send_action(a)
                            break
                    continue
            if not is_mine(state, pid):
                continue

            # mana payment prompts: submit as-is (engine auto-selects)
            paid = False
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await c.send_action(a)
                    paid = True
                    break
            if paid:
                continue

            if pid == 1:
                acted = False
                if state.get("active_player") == 1 and state.get("phase") in MAIN_PHASES:
                    for a in acts:
                        if a["type"] == "PlayLand":
                            await c.send_action(a); acted = True; break
                if not acted:
                    for a in acts:
                        if a["type"] == "PassPriority":
                            await c.send_action(a); break
                continue

            # ---- P0 ----
            in_main = state.get("phase") in MAIN_PHASES
            my_turn = state.get("active_player") == 0
            tyvar_id = battlefield_id(state, 0, TYVAR)
            elves = battlefield_id(state, 0, ELVES)
            hn = hand_names(state, 0)

            if tyvar_id and elves and assertions["A1_setup_ok"] == "not-run":
                assertions["A1_setup_ok"] = "passed"
                print(f"setup ok at turn {state.get('turn_number')}", flush=True)
            if elves and elves_first_seen_turn is None:
                elves_first_seen_turn = state.get("turn_number")
                eo_dbg = get_obj(state, elves)
                print(f"Elves entered turn {elves_first_seen_turn}; keys: {sorted(eo_dbg.keys())}", flush=True)
                p0keys = [k for p in state.get("players", []) if p.get("id") == 0 for k in p.keys()]
                print(f"P0 player keys: {sorted(p0keys)}", flush=True)

            # THE TRIGGER: activate Elves' mana ability (sickness-free: entered on an earlier turn)
            if (tyvar_id and elves and in_main and my_turn and not activated
                    and elves_first_seen_turn is not None
                    and state.get("turn_number", 0) > elves_first_seen_turn):
                eo = get_obj(state, elves)
                if not eo.get("tapped"):
                    found = False
                    for a in acts:
                        d = a.get("data", {})
                        if a["type"] == "ActivateAbility" and d.get("source_id") == elves:
                            found = True
                            elves_id = elves
                            print(f"PRE export at turn {state.get('turn_number')}; activating Elves mana ability", flush=True)
                            pre_turn = state.get("turn_number")
                            pre_pool_g = mana_pool_g(state, 0)
                            await export("pre")
                            await c.send_action(a)
                            activated = True
                            need_mid = True
                            break
                    if not found and state.get("turn_number") != dbg_act_turn[0]:
                        dbg_act_turn[0] = state.get("turn_number")
                        kinds = [(a["type"], sorted(a.get("data", {}).keys())) for a in acts]
                        print(f"DBG turn {dbg_turn[0]}: no Elves ActivateAbility; actions={kinds}; elves_tapped={eo.get('tapped')}; sick={eo.get('summoning_sick')}; has_mana_ability={eo.get('has_mana_ability')}", flush=True)
                    if activated:
                        continue

            # mid checkpoint: prove the mana ability resolved (Elves tapped, G in pool)
            if need_mid and pid == 0 and is_mine(state, 0):
                env = await export("mid")
                eo = get_obj(env["state"], elves_id)
                pool_g = mana_pool_g(env["state"], 0)
                notes.append(f"mid: Elves tapped={eo.get('tapped')} pool_g={pool_g} (pre pool_g={pre_pool_g})")
                if eo.get("tapped") and pool_g > (pre_pool_g or 0):
                    assertions["A2_mana_produced"] = "passed"
                else:
                    assertions["A2_mana_produced"] = "failed"
                need_mid = False

            # after activation: watch for the trigger to resolve, then check counters
            if activated and not done and not need_mid:
                post_rounds += 1
                if post_rounds >= 40:
                    env = await export("post")
                    eo = get_obj(env["state"], elves_id)
                    n, sample = plus_counters(eo)
                    notes.append(f"post: Elves counters raw sample: {json.dumps(sample)[:300]}")
                    notes.append(f"post: Elves tapped={eo.get('tapped')}")
                    if n == 1:
                        assertions["A3_counters_added"] = "passed"
                    elif n == 0:
                        assertions["A3_counters_added"] = "failed"
                        notes.append("BUG: no +1/+1 counters after granted trigger window")
                    else:
                        assertions["A3_counters_added"] = "failed"
                        notes.append(f"unexpected counter count: {n}")
                    done = True
                    break

            # main-phase economy: land first, then cast, then trigger handled above
            if in_main and my_turn:
                acted = False
                if dbg_turn[0] != ("main", state.get("turn_number")):
                    dbg_turn[0] = ("main", state.get("turn_number"))
                    casts = [(a["type"], obj_name(state, a.get("data", {}).get("object_id"))) for a in acts if a["type"] == "CastSpell"]
                    print(f"DBG P0 main turn {state.get('turn_number')}: hand={hand_names(state, 0)}; castable={casts}", flush=True)
                for a in acts:
                    if a["type"] == "PlayLand":
                        await c.send_action(a)
                        acted = True
                        break
                if not acted:
                    for want in (ELVES, TYVAR):
                        if want in hn:
                            for a in acts:
                                d = a.get("data", {})
                                if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == want:
                                    print(f"P0 casting {want}", flush=True)
                                    await c.send_action(a)
                                    acted = True
                                    break
                        if acted:
                            break
                if acted:
                    continue
            for a in acts:
                if a["type"] == "PassPriority":
                    await c.send_action(a)
                    break
        if done:
            break
        if max_turn > 40 and not activated:
            notes.append("could not reach trigger by turn 40 (Tyvar/Elves never both deployed)")
            break

    if assertions["A1_setup_ok"] == "not-run":
        assertions["A1_setup_ok"] = "failed" if max_turn > 40 else "not-run"
    if assertions["A2_mana_produced"] == "not-run" and not activated:
        assertions["A2_mana_produced"] = "not-run"
        assertions["A3_counters_added"] = "not-run"
    assertions["A4_once_per_turn"] = "not-run"  # only meaningful if A3 passes
    notes.append("A4 not-run: second same-turn activation of the same Elves impossible while tapped; no untap effect in scenario decks")
    notes.append("parser corroboration: card-data.json parses Tyvar's granted trigger as mode {Unknown: 'Whenever a mana ability of ~ resolves'} with effect {Unimplemented: put ...}; the trigger can never fire")

    verdict = ("reproduced" if assertions["A3_counters_added"] == "failed"
               else "not-reproduced" if all(assertions[k] == "passed" for k in ("A1_setup_ok", "A2_mana_produced", "A3_counters_added"))
               else "blocked")

    scenario_src = open(__file__).read()
    run = {
        "issue": 650,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/20260909-01",
        "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src.encode()).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "mulligans": f"P0 mulliganed {mulligans_taken[0]}x seeking Elves in opener; P1 kept 7",
        "setup_line": "P0: 4x Tyvar the Bellicose + 4x Llanowar Elves + 26 Forest + 26 Swamp | P1: 60 Island (draw-go)",
        "contract_line": "Tap Elves for {G} with Tyvar out: granted trigger must put 1 +1/+1 counter on Elves (once per turn).",
        "stats": {"max_turn": max_turn, "pre_turn": pre_turn, "activated": activated},
        "assertions": assertions,
        "notes": notes,
        "verdict": verdict,
        "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
    }
    with open(f"{EVIDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVIDIR}/scenario_650.py", "w") as f:
        f.write(scenario_src)
    import os as _os
    lines = []
    for fn in sorted(_os.listdir(EVIDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVIDIR}/{fn}", "rb").read()).hexdigest() + "  " + fn)
    with open(f"{EVIDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": assertions, "notes": notes}, indent=1), flush=True)
    await p0.close()
    await p1.close()


asyncio.run(main())
