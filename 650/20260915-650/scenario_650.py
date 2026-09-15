#!/usr/bin/env python3
"""Issue #650: Tyvar the Bellicose — granted trigger does not work.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Card (verified in pinned card-data.json, v0.83.0):
  Tyvar the Bellicose {2}{B}{G}, Legendary Creature — Elf Warrior 5/4.
  Oracle: "Whenever one or more Elves you control attack, they gain deathtouch
  until end of turn. Each creature you control has 'Whenever a mana ability of
  this creature resolves, put a number of +1/+1 counters on it equal to the
  amount of mana this creature produced. This ability triggers only once each
  turn.'"

Reported failure: tapping creatures for mana does not produce the counters;
the reporter saw the ability fire at most once per turn instead of once per
creature-tap (the granted instance is per-creature, once per turn).

Setup:
  P0: 12x Tyvar the Bellicose, 8x Llanowar Elves, 20x Forest, 20x Swamp
  (dense playsets in a custom game; the engine accepts >4-of).
  P1: 60x Island, draw-go.
  Both keep 7 (P0 mulligans toward an Elves opener, max 3).

Trigger (leg A): with Tyvar on the battlefield and a sickness-free untapped
Llanowar Elves A, P0 activates Elves A's mana ability ({T}: Add {G}) in its
own main phase. Expected: the granted trigger resolves and A ends with
exactly 1 +1/+1 counter.

Trigger (leg B, same turn as leg A): activate a second sickness-free
Llanowar Elves B's mana ability on the same turn. Expected: B's own granted
instance also resolves and B ends with exactly 1 +1/+1 counter — the printed
"only once each turn" limit is per creature, not global.

Assertions:
  A1 setup_ok ........... Tyvar + sickness-free Elves A on battlefield under P0.
  A2 mana_produced ...... Elves A's mana ability resolves; A tapped, {G} in pool.
  A3 counters_added ..... Elves A has exactly 1 +1/+1 counter after window.
  A4 per_creature ....... (same turn) Elves B has exactly 1 +1/+1 counter.

Verdict rule: reproduced iff A1+A2 pass and A3 fails (trigger does not work at
all), or A1..A3 pass and A4 fails (global once-per-turn throttle). not-
reproduced iff A1..A4 all pass. A4 is not-run when A3 fails (trigger never
fires, so per-creature throttling cannot be evaluated) or when no second
sickness-free Elves is available the same turn.

Evidence: evidence/650/<run-id>/pre.json (before Elves A activation),
mid.json (mana produced), post.json (after trigger window for A),
pre2.json/post2.json (leg B), run.json, manifest.sha256, summary.png,
scenario_650.py
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck

TYVAR = "Tyvar the Bellicose"
ELVES = "Llanowar Elves"
RUN_ID = "20260915-650"
EVIDIR = f"/home/hatch/workspace/dev/phase-backfill/evidence/650/{RUN_ID}"
MAIN_PHASES = ("PreCombatMain", "PostCombatMain")

# Server identity: recomputed 2026-09-15 against the on-disk pinned release.
SERVER_IDENTITY = {
    "server_version": "0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "ServerHello + minisign verification against repo-pinned key (pin_release.py)",
}

P0_DECK = [(TYVAR, 12), (ELVES, 8), ("Forest", 20), ("Swamp", 20)]  # dense playsets: custom game, engine accepts >4-of
P1_DECK = [("Island", 60)]


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


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


def is_mine(state, pid):
    wf = state.get("waiting_for") or {}
    return wf.get("type") == "Priority" and wf.get("data", {}).get("player") == pid


def battlefield_ids(state, pid, name):
    out = []
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and (o.get("base_name") or o.get("name")) == name):
            out.append(int(oid))
    return sorted(out)


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def mana_pool_g(state, pid):
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
    os.makedirs(EVIDIR, exist_ok=True)
    t0 = time.time()
    assertions = {
        "A1_setup_ok": "not-run",
        "A2_mana_produced": "not-run",
        "A3_counters_added": "not-run",
        "A4_per_creature": "not-run",
    }
    notes = []

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    print(f"game={p0.game_code} run={RUN_ID}", flush=True)

    # mulligans: P0 toward an Elves opener (max 3); P1 always keeps
    last = {}
    mulligans_taken = {0: 0, 1: 0}
    for c in (p0, p1):
        for _ in range(60):
            if c.latest:
                break
            await asyncio.sleep(0.25)
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
            await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": choice}}})
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

    # run state
    max_turn = 0
    elves_seen_turn = {}   # oid -> turn first seen on battlefield
    pre_turn = None         # turn of leg-A activation
    b_turn = None           # turn of leg-B activation
    elves_a = None
    elves_b = None
    activated = False       # leg-A activation submitted
    need_mid = False
    post_rounds = 0
    a_done = False          # leg-A window finished
    b_activated = False
    need_pre2 = False
    post_rounds2 = 0
    b_done = False
    finished = False
    pre_pool_g = None

    for i in range(6000):
        await asyncio.sleep(0.2)
        for c in (p0, p1):
            st = c.latest
            if not st or c.revision == last.get(c.name):
                continue
            state = st["state"]
            acts = list(st.get("legal_actions", []))
            for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
                for p in payloads or []:
                    acts.append({"type": p.get("type"), "data": p.get("data", {})})
            pid = c.player_id
            wtype, wplayer = waiting_on(state, pid)
            if wplayer != pid:
                continue
            last[c.name] = c.revision
            max_turn = max(max_turn, state.get("turn_number", 0))

            if wtype in ("MulliganDecision", "OpeningHandBottomCards"):
                sc = [a for a in acts if a["type"] == "SelectCards"]
                if sc:
                    hn_ids = []
                    for p in state.get("players", []):
                        if p.get("id") == pid:
                            hn_ids = list(p.get("hand", []))

                    def is_land_oid(oid):
                        return obj_name(state, oid) in ("Forest", "Swamp", "Island", "Mountain", "Plains")

                    hn_ids.sort(key=lambda oid: (not is_land_oid(oid), obj_name(state, oid)))
                    n = 0
                    try:
                        pend = (state.get("waiting_for") or {}).get("data", {}).get("pending", [])
                        n = next(e["phase"]["count"] for e in pend if e.get("player") == pid)
                    except Exception:
                        pass
                    await c.send_action({"type": "SelectCards", "data": {"cards": [int(x) for x in hn_ids[:n]]}})
                    print(f"P{pid} bottoms {n} cards after mulligan", flush=True)
                    continue

            if wtype != "Priority":
                discards = [a for a in acts if "iscard" in a["type"]]
                if discards:
                    def is_land(a):
                        return "land" in json.dumps(a.get("data", {})).lower()

                    discards.sort(key=lambda a: not is_land(a))
                    await c.send_action(discards[0])
                    print(f"P{pid} discards via {discards[0]['type']}", flush=True)
                    continue
                if wtype == "ChooseLegend":
                    da = [a for a in acts if a["type"] == "ChooseLegend"]
                    if da:
                        await c.send_action(da[0])
                        print(f"P{pid} chooses legend (keep first)", flush=True)
                    continue
                if wtype in ("DeclareAttackers", "DeclareBlockers"):
                    da = [a for a in acts if a["type"] == wtype]
                    if da:
                        sub = json.loads(json.dumps(da[0]))
                        d = sub.setdefault("data", {})
                        for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
                            if k in d:
                                d[k] = [] if isinstance(d[k], list) else {}
                        await c.send_action(sub)
                        print(f"P{pid} declares no {wtype}", flush=True)
                    continue
                for a in acts:
                    if a["type"] == "PassPriority":
                        await c.send_action(a)
                        break
                continue
            if not is_mine(state, pid):
                continue

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
                            await c.send_action(a)
                            acted = True
                            break
                if not acted:
                    for a in acts:
                        if a["type"] == "PassPriority":
                            await c.send_action(a)
                            break
                continue

            # ---- P0 ----
            in_main = state.get("phase") in MAIN_PHASES
            my_turn = state.get("active_player") == 0
            tyvar_id = battlefield_ids(state, 0, TYVAR)[0] if battlefield_ids(state, 0, TYVAR) else None
            elves_oids = battlefield_ids(state, 0, ELVES)
            hn = hand_names(state, 0)
            turn = state.get("turn_number", 0)
            for eo_id in elves_oids:
                if eo_id not in elves_seen_turn:
                    elves_seen_turn[eo_id] = turn

            if tyvar_id and elves_oids and assertions["A1_setup_ok"] == "not-run":
                assertions["A1_setup_ok"] = "passed"
                print(f"setup ok at turn {turn} (Tyvar + {len(elves_oids)} Elves)", flush=True)

            def sickness_free(oid):
                return turn > elves_seen_turn.get(oid, turn)

            def activate_mana_ability(oid):
                for a in acts:
                    d = a.get("data", {})
                    if a.get("type") == "ActivateAbility" and d.get("source_id") == oid:
                        return a
                return None

            # ---- leg A: activate first sickness-free Elves ----
            if (tyvar_id and in_main and my_turn and not activated and not finished):
                cand = next((o for o in elves_oids if sickness_free(o) and not get_obj(state, o).get("tapped")), None)
                if cand:
                    act = activate_mana_ability(cand)
                    if act:
                        elves_a = cand
                        pre_turn = turn
                        pre_pool_g = mana_pool_g(state, 0)
                        print(f"PRE export at turn {turn}; activating Elves A mana ability (oid {elves_a})", flush=True)
                        await export("pre")
                        await c.send_action(act)
                        activated = True
                        need_mid = True
                        continue

            # mid checkpoint: prove the mana ability resolved
            if need_mid and pid == 0 and is_mine(state, 0):
                env = await export("mid")
                eo = get_obj(env["state"], elves_a)
                pool_g = mana_pool_g(env["state"], 0)
                notes.append(f"mid: Elves A tapped={eo.get('tapped')} pool_g={pool_g} (pre pool_g={pre_pool_g})")
                if eo.get("tapped") and pool_g > (pre_pool_g or 0):
                    assertions["A2_mana_produced"] = "passed"
                else:
                    assertions["A2_mana_produced"] = "failed"
                need_mid = False

            # leg-A trigger window
            if activated and not a_done and not need_mid and not finished:
                post_rounds += 1
                if post_rounds >= 40:
                    env = await export("post")
                    eo = get_obj(env["state"], elves_a)
                    n, sample = plus_counters(eo)
                    notes.append(f"post: Elves A counters raw sample: {json.dumps(sample)[:300]}")
                    notes.append(f"post: Elves A tapped={eo.get('tapped')}")
                    if n == 1:
                        assertions["A3_counters_added"] = "passed"
                    elif n == 0:
                        assertions["A3_counters_added"] = "failed"
                        notes.append("BUG: no +1/+1 counters on Elves A after granted trigger window")
                    else:
                        assertions["A3_counters_added"] = "failed"
                        notes.append(f"unexpected counter count on Elves A: {n}")
                    a_done = True

            # ---- leg B: same-turn second creature ----
            if a_done and assertions["A3_counters_added"] == "passed" and not b_done and not finished:
                if turn > pre_turn:
                    assertions["A4_per_creature"] = "not-run"
                    notes.append("A4 not-run: turn advanced past leg-A turn before leg-B activation")
                    b_done = True
                elif not b_activated and in_main and my_turn and is_mine(state, 0):
                    cand = next((o for o in elves_oids
                                 if o != elves_a and sickness_free(o) and not get_obj(state, o).get("tapped")), None)
                    if cand:
                        act = activate_mana_ability(cand)
                        if act:
                            elves_b = cand
                            b_turn = turn
                            print(f"PRE2 export at turn {turn}; activating Elves B mana ability (oid {elves_b})", flush=True)
                            await export("pre2")
                            await c.send_action(act)
                            b_activated = True
                if b_activated:
                    post_rounds2 += 1
                    if post_rounds2 >= 40:
                        env = await export("post2")
                        eo = get_obj(env["state"], elves_b)
                        n, sample = plus_counters(eo)
                        notes.append(f"post2: Elves B counters raw sample: {json.dumps(sample)[:300]}")
                        if n == 1:
                            assertions["A4_per_creature"] = "passed"
                        elif n == 0:
                            assertions["A4_per_creature"] = "failed"
                            notes.append("BUG: no +1/+1 counters on Elves B after same-turn activation (global once-per-turn throttle?)")
                        else:
                            assertions["A4_per_creature"] = "failed"
                            notes.append(f"unexpected counter count on Elves B: {n}")
                        b_done = True
                        finished = True

            # if leg-A bug confirmed, leg B is moot
            if a_done and assertions["A3_counters_added"] == "failed" and not finished:
                assertions["A4_per_creature"] = "not-run"
                notes.append("A4 not-run: granted trigger never fires (A3 failed), per-creature throttling cannot be evaluated")
                finished = True

            if finished:
                break

            # main-phase economy: land first, then cast (cap Elves at 2, skip 2nd Tyvar)
            if in_main and my_turn:
                acted = False
                for a in acts:
                    if a["type"] == "PlayLand":
                        await c.send_action(a)
                        acted = True
                        break
                if not acted:
                    wants = []
                    if len(elves_oids) < 2:
                        wants.append(ELVES)
                    if not tyvar_id:
                        wants.append(TYVAR)
                    for want in wants:
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
        if finished:
            break
        if max_turn > 40 and not activated:
            notes.append("could not reach leg-A trigger by turn 40 (Tyvar/Elves never both deployed)")
            break

    if assertions["A1_setup_ok"] == "not-run":
        assertions["A1_setup_ok"] = "failed" if max_turn > 40 else "not-run"
    if not activated:
        assertions["A2_mana_produced"] = "not-run"
        assertions["A3_counters_added"] = "not-run"
        if assertions["A4_per_creature"] == "not-run" and "A4 not-run" not in " ".join(notes):
            notes.append("A4 not-run: leg-A activation never happened")
    if a_done and assertions["A3_counters_added"] == "passed" and not b_done and not b_activated:
        assertions["A4_per_creature"] = "not-run"
        notes.append("A4 not-run: no second sickness-free Elves available on the leg-A turn")
    notes.append("parser corroboration: card-data.json (v0.83.0) parses Tyvar's granted trigger as mode {Unknown: 'Whenever a mana ability of ~ resolves'} with effect {Unimplemented: unparsed_quantity}; constraint {OncePerTurn} recorded")

    if assertions["A1_setup_ok"] == "passed" and assertions["A2_mana_produced"] == "passed":
        if assertions["A3_counters_added"] == "failed" or assertions["A4_per_creature"] == "failed":
            verdict = "reproduced"
        elif assertions["A3_counters_added"] == "passed" and assertions["A4_per_creature"] == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked" if assertions["A1_setup_ok"] != "passed" or assertions["A2_mana_produced"] != "passed" else "blocked"

    scenario_src = open(__file__).read()
    run = {
        "issue": 650,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/run-650-20260915-1542",
        "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src.encode()).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "mulligans": f"P0 mulliganed {mulligans_taken[0]}x seeking Elves in opener; P1 kept 7",
        "setup_line": "P0: 12x Tyvar the Bellicose + 8x Llanowar Elves + 20 Forest + 20 Swamp | P1: 60 Island (draw-go)",
        "contract_line": "Tap Elves for {G} with Tyvar out: granted trigger must put 1 +1/+1 counter on the tapped creature (once per turn, per creature).",
        "stats": {"max_turn": max_turn, "pre_turn": pre_turn, "b_turn": b_turn,
                  "activated": activated, "b_activated": b_activated},
        "assertions": assertions,
        "notes": notes,
        "verdict": verdict,
        "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
    }
    with open(f"{EVIDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVIDIR}/scenario_650.py", "w") as f:
        f.write(scenario_src)
    lines = []
    for fn in sorted(os.listdir(EVIDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVIDIR}/{fn}", "rb").read()).hexdigest() + "  " + fn)
    with open(f"{EVIDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": assertions, "notes": notes}, indent=1), flush=True)
    await p0.close()
    await p1.close()


asyncio.run(main())
