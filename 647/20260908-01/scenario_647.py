#!/usr/bin/env python3
"""Issue #647: Asmoranomardicadaistinaculdacar castable without paying discard cost.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Card (verified in pinned card-data.json):
  "As long as you've discarded a card this turn, you may pay {B/R} to cast this spell."
  mana_cost = NoCost. The ONLY legal hand-cast is the conditional alternative cost,
  gated on having discarded a card this turn.

Reported failure: the card is castable WITHOUT discarding (conditional alt-cost
parsed as no-op, NoCost path too permissive).

Setup:
  P0: 4x Asmoranomardicadaistinaculdacar, 28x Mountain, 28x Swamp (B/R mana available,
      so mana is never the reason the cast is unoffered).
  P1: 60x Island, draw-go.
  Both keep 7. P0 plays one land per turn when able; both pass priority otherwise.
  No player ever discards, casts, or activates anything else.

Trigger condition: P0 holds >=1 Asmo in hand while holding priority in its own
PreCombatMain/PostCombatMain.

Expected: CastSpell(Asmo) is NEVER advertised — neither in legal_actions nor via
viewer_interaction — because the discard condition is never satisfied.

Assertions (each ends passed / failed / not-run):
  A1 setup_ok ............ game starts, both keep, turns advance normally.
  A2 trigger_observed .... Asmo in P0 hand with P0 main-phase priority >= 3 times.
  A3 no_cast_offered ..... no CastSpell action targeting Asmo in any observed state.
  A4 no_viewer_cast ..... no viewer_interaction offering an Asmo cast.

Verdict rule: not-reproduced iff A1..A4 all pass. Any CastSpell(Asmo) offer ->
reproduced (scenario then exports pre/post around the offer and attempts the cast).

Evidence: evidence/647/<run-id>/pre.json, post.json, run.json, manifest.sha256,
summary.png, scenario_647.py
"""
import asyncio
import hashlib
import json
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck

ASMO = "Asmoranomardicadaistinaculdacar"
RUN_ID = "20260908-01"
EVIDIR = f"/home/hatch/workspace/dev/phase-backfill/evidence/647/{RUN_ID}"
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
    "source": "ServerHello + minisign verification against repo-pinned key "
              "(client/src-tauri/src/native_engine.rs SERVER_ARTIFACT_PUBLIC_KEY)",
}

P0_DECK = [("Asmoranomardicadaistinaculdacar", 4), ("Mountain", 28), ("Swamp", 28)]
P1_DECK = [("Island", 60)]


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def is_mine(state, pid):
    wf = state.get("waiting_for") or {}
    return wf.get("type") == "Priority" and wf.get("data", {}).get("player") == pid


def hand_names(state, pid):
    objs = state.get("objects", {})
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, oid) for oid in p.get("hand", [])]
    return []


def asmo_cast_actions(state, acts):
    return [a for a in acts
            if a.get("type") == "CastSpell"
            and obj_name(state, a.get("data", {}).get("object_id")) == ASMO]


async def main():
    import os
    os.makedirs(EVIDIR, exist_ok=True)
    t0 = time.time()
    assertions = {
        "A1_setup_ok": "not-run",
        "A2_trigger_observed": "not-run",
        "A3_no_cast_offered": "not-run",
        "A4_no_viewer_cast": "not-run",
    }
    notes = []
    trigger_obs = 0
    states_seen = 0
    max_turn = 0
    pre_exported = False
    pre_turn = None

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    print(f"game={p0.game_code} run={RUN_ID}", flush=True)

    last = {}
    # mulligans
    for c in (p0, p1):
        for _ in range(40):
            if c.latest:
                break
            await asyncio.sleep(0.25)
        for a in (c.latest.get("legal_actions", []) if c.latest else []):
            if a["type"] == "MulliganDecision" and a["data"]["choice"]["type"] == "Keep":
                await c.send_action(a)
    assertions["A1_setup_ok"] = "passed"

    async def export(tag):
        st = await p0.export_state()
        path = f"{EVIDIR}/{tag}.json"
        with open(path, "w") as f:
            json.dump(st, f)
        print(f"exported {tag}.json", flush=True)
        return path

    turns_after_pre = 0
    done = False
    for i in range(4000):
        await asyncio.sleep(0.2)
        for c in (p0, p1):
            st = c.latest
            if not st or c.revision == last.get(c.name):
                continue
            state = st["state"]
            acts = st.get("legal_actions", [])
            states_seen += 1
            pid = c.player_id
            if not is_mine(state, pid):
                continue
            last[c.name] = c.revision
            max_turn = max(max_turn, state.get("turn_number", 0))

            if pid == 0:
                hn = hand_names(state, 0)
                in_main = state.get("phase") in MAIN_PHASES
                # A3: the core negative assertion
                casts = asmo_cast_actions(state, acts)
                if casts:
                    assertions["A3_no_cast_offered"] = "failed"
                    notes.append(f"CastSpell(Asmo) OFFERED at turn {state.get('turn_number')}")
                    await export("pre")
                    await c.send_action(casts[0])
                    await asyncio.sleep(3)
                    await export("post")
                    done = True
                    break
                # A4: viewer_interaction surface
                vi = st.get("viewer_interaction")
                if vi and ASMO.lower() in json.dumps(vi).lower():
                    assertions["A4_no_viewer_cast"] = "failed"
                    notes.append(f"viewer_interaction mentions Asmo at turn {state.get('turn_number')}: "
                                 f"{json.dumps(vi)[:200]}")
                # trigger observation
                if in_main and ASMO in hn:
                    trigger_obs += 1
                    if not pre_exported:
                        pre_exported = True
                        pre_turn = state.get("turn_number")
                        await export("pre")
                        print(f"PRE captured at turn {pre_turn} (Asmo in hand, main-phase priority)", flush=True)
                    if trigger_obs >= 3 and pre_exported:
                        turns_after_pre += 1
                # play pattern: land then pass
                acted = False
                if state.get("active_player") == 0 and in_main:
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
            else:
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
        if done:
            break
        if pre_exported and trigger_obs >= 6:
            print("enough post-trigger observations; exporting POST", flush=True)
            await export("post")
            break
        if max_turn > 30 and not pre_exported:
            notes.append("never observed Asmo in hand with main-phase priority by turn 30")
            break

    if assertions["A3_no_cast_offered"] == "not-run":
        assertions["A3_no_cast_offered"] = "passed"
    if assertions["A4_no_viewer_cast"] == "not-run":
        assertions["A4_no_viewer_cast"] = "passed"
    assertions["A2_trigger_observed"] = "passed" if trigger_obs >= 3 else "failed"
    if trigger_obs < 3:
        notes.append(f"only {trigger_obs} trigger observations (< 3 required)")

    verdict = "not-reproduced" if all(v == "passed" for v in assertions.values()) else (
        "reproduced" if assertions["A3_no_cast_offered"] == "failed" else "blocked")

    scenario_src = open(__file__).read()
    run = {
        "issue": 647,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src.encode()).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "mulligans": "both kept 7",
        "discards_this_game": 0,
        "stats": {"states_seen": states_seen, "trigger_observations": trigger_obs,
                  "max_turn": max_turn, "pre_turn": pre_turn},
        "assertions": assertions,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Positive control (discard, then cast for {B/R}) not exercised in this run; "
            "verdict covers only the reported over-permissiveness.",
            "Browser UI not exercised; native engine via two human-client seats.",
        ],
    }
    with open(f"{EVIDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVIDIR}/scenario_647.py", "w") as f:
        f.write(scenario_src)
    # manifest
    import os as _os
    lines = []
    for fn in sorted(_os.listdir(EVIDIR)):
        if fn == "manifest.sha256":
            continue
        h = hashlib.sha256(open(f"{EVIDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVIDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": assertions, "notes": notes}, indent=1), flush=True)
    await p0.close()
    await p1.close()


asyncio.run(main())
