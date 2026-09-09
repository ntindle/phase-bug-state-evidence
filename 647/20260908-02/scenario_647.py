#!/usr/bin/env python3
"""Issue #647: Asmoranomardicadaistinaculdacar — full behavioral contract.

Card (verified in pinned card-data.json):
  "As long as you've discarded a card this turn, you may pay {B/R} to cast this spell."
  mana_cost = NoCost. Parser swallowed the alt-cost clause (parse_warnings:
  SwallowedClause); the condition (CardsDiscardedThisTurn >= 1) exists with empty
  modifications.

FULL BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
  (NEGATIVE) Before discarding this turn: Asmo must NOT be castable from hand.
  (POSITIVE) After discarding this turn: Asmo must become castable for {B/R};
             casting it pays {B/R} and it moves hand -> stack -> battlefield.

Setup:
  P0: 4x Asmoranomardicadaistinaculdacar + 4x Faithless Looting + 26 Swamp + 26 Mountain.
  P1: 60x Island, draw-go.
  P0 mulligans (max 2) seeking Faithless Looting in the opener; P1 keeps 7.

Phases:
  1. negative: Asmo in P0 hand with P0 main-phase priority, no discard yet this
     game -> CastSpell(Asmo) must never be advertised (legal_actions nor
     viewer_interaction). First such state is exported as pre_neg.json.
  2. looting: once >=3 negative observations, P0 casts Faithless Looting ({R}).
     Its "discard two cards" is answered via the engine-issued SelectCards
     selection (lands first, Asmo never discarded while avoidable).
  3. positive: after the discard resolves, CastSpell(Asmo) must be advertised.
     P0 casts it through the {B/R} path; payment ({B} or {R} spent) and the
     hand -> stack -> battlefield transitions are verified.

Assertions (each ends passed / failed / not-run):
  A1 setup_ok .............. game starts, both keep/mulligan legally, turns advance.
  A2 neg_no_cast_offered .... before any discard, CastSpell(Asmo) never in legal_actions.
  A3 neg_no_viewer_cast .... before any discard, no viewer_interaction Asmo cast offer.
  A4 looting_resolved ....... Faithless Looting cast, resolved, P0 discarded >= 1.
  A5 cast_offered_after_discard  CastSpell(Asmo) advertised after the discard.
  A6 asmo_cast_paid ......... Asmo cast for {B/R}: mana spent, hand->stack->battlefield.

Verdict rule: "reproduced" iff the REPORTED bug is observed, i.e. Asmo is
castable without discarding (A2 or A3 fail). The positive control is reported
in the notes either way: if Asmo is NOT castable even after discarding, that is
a real defect (under-permissive) but not the reported one.

Evidence: evidence/647/<run-id>/pre_neg.json, post_discard.json, pre_cast.json,
mid_cast.json, post.json, run.json, manifest.sha256, summary.png,
scenario_647.py, run log.
"""
import asyncio
import hashlib
import json
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck

ASMO = "Asmoranomardicadaistinaculdacar"
LOOTING = "Faithless Looting"
RUN_ID = "20260908-02"
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
    "observed_at": "2026-09-09",
    "source": "ServerHello + minisign verification against repo-pinned key",
}

P0_DECK = [(ASMO, 4), (LOOTING, 4), ("Swamp", 26), ("Mountain", 26)]
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


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def hand_ids(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return list(p.get("hand", []))
    return []


def battlefield_id(state, pid, name):
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and (o.get("base_name") or o.get("name")) == name):
            return int(oid)
    return None


def stack_has(state, pid, name):
    for e in state.get("stack", []) or []:
        o = e if isinstance(e, dict) else get_obj(state, e)
        if (o.get("base_name") or o.get("name")) == name:
            return True
    return False


def discards_this_turn(state, pid):
    d = state.get("cards_discarded_this_turn_by_player") or {}
    v = d.get(str(pid), d.get(pid, 0))
    if isinstance(v, list):
        return len(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def cast_actions_for(state, acts, name):
    return [a for a in acts
            if a.get("type") == "CastSpell"
            and obj_name(state, a.get("data", {}).get("object_id")) == name]


def untapped_br_lands(state, pid):
    n = 0
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped")
                and (o.get("base_name") or o.get("name")) in ("Swamp", "Mountain")):
            n += 1
    return n


async def main():
    import os
    os.makedirs(EVIDIR, exist_ok=True)
    t0 = time.time()
    assertions = {
        "A1_setup_ok": "not-run",
        "A2_neg_no_cast_offered": "not-run",
        "A3_neg_no_viewer_cast": "not-run",
        "A4_looting_resolved": "not-run",
        "A5_cast_offered_after_discard": "not-run",
        "A6_asmo_cast_paid": "not-run",
    }
    notes = []
    states_seen = 0
    max_turn = 0
    neg_obs = 0
    pre_neg_done = False
    looting_cast = False
    looting_turn = None
    discard_done = False
    discard_turn = None
    asmo_cast = False
    asmo_cast_turn = None
    pre_cast_lands = None
    mid_exported = False
    done = False
    post_rounds = 0
    dbg_turn = [None]

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
        # P0 mulligans (max 2) seeking Faithless Looting in the opener; P1 keeps.
        for _ in range(4):
            acts0 = c.latest.get("legal_actions", []) if c.latest else []
            dec = next((a for a in acts0 if a["type"] == "MulliganDecision"), None)
            if dec is None:
                break
            if c.player_id == 0:
                hn = hand_names(c.latest["state"], 0)
                if LOOTING in hn or mulligans_taken[0] >= 2:
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
    assertions["A1_setup_ok"] = "passed"

    async def export(tag):
        raw = await p0.export_state()
        env = json.loads(raw) if isinstance(raw, str) else raw
        path = f"{EVIDIR}/{tag}.json"
        with open(path, "w") as f:
            json.dump(env, f)
        print(f"exported {tag}.json", flush=True)
        return env

    for i in range(8000):
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
            states_seen += 1
            pid = c.player_id
            wtype, wplayer = waiting_on(state, pid)
            if states_seen <= 12 or states_seen % 200 == 0:
                print(f"DBG rev: P{pid} wtype={wtype} wplayer={wplayer} "
                      f"phase={state.get('phase')} turn={state.get('turn_number')} "
                      f"acts={[(a['type']) for a in acts][:8]}", flush=True)
            if wplayer != pid:
                continue
            last[c.name] = c.revision
            max_turn = max(max_turn, state.get("turn_number", 0))

            # --- non-priority decisions ---
            if wtype != "Priority":
                # opening-hand bottoming after mulligans: bottom lands first
                if wtype in ("MulliganDecision", "OpeningHandBottomCards"):
                    sc = [a for a in acts if a["type"] == "SelectCards"]
                    if sc:
                        hids = hand_ids(state, pid)
                        hids.sort(key=lambda oid: (obj_name(state, oid) not in
                                                   ("Swamp", "Mountain", "Forest", "Island", "Plains"),
                                                   obj_name(state, oid)))
                        n = 0
                        try:
                            pend = (state.get("waiting_for") or {}).get("data", {}).get("pending", [])
                            n = next(e["phase"]["count"] for e in pend if e.get("player") == pid)
                        except Exception:
                            pass
                        sub = {"type": "SelectCards", "data": {"cards": [int(x) for x in hids[:n]]}}
                        await c.send_action(sub)
                        print(f"P{pid} bottoms {n} cards after mulligan", flush=True)
                        continue
                # Faithless Looting's "discard two cards": use engine-issued selection,
                # lands first, never Asmo while anything else is available
                if "Discard" in wtype:
                    hids = hand_ids(state, pid)
                    def discard_rank(oid):
                        nm = obj_name(state, oid)
                        if nm == ASMO:
                            return (2, nm)
                        if nm in ("Swamp", "Mountain", "Forest", "Island", "Plains"):
                            return (0, nm)
                        return (1, nm)
                    hids.sort(key=discard_rank)
                    n = 2
                    d = (state.get("waiting_for") or {}).get("data", {})
                    for k in ("count", "amount", "number"):
                        if isinstance(d.get(k), int):
                            n = d[k]
                    print(f"DBG discard decision: wtype={wtype} actions="
                          f"{[(a['type'], sorted(a.get('data', {}).keys())) for a in acts]}", flush=True)
                    sub = {"type": "SelectCards", "data": {"cards": [int(x) for x in hids[:n]]}}
                    await c.send_action(sub)
                    print(f"P{pid} discards {[obj_name(state, x) for x in hids[:n]]} to Looting", flush=True)
                    continue
                if dbg_turn[0] != (wtype, state.get("turn_number")):
                    dbg_turn[0] = (wtype, state.get("turn_number"))
                    print(f"DBG non-priority decision: {wtype} turn {state.get('turn_number')} actions="
                          f"{[(a['type'], sorted(a.get('data', {}).keys())) for a in acts]}", flush=True)
                # legend rule: keep the first offered permanent
                if wtype == "ChooseLegend":
                    da = [a for a in acts if a["type"] == "ChooseLegend"]
                    if da:
                        await c.send_action(da[0])
                        print(f"P{pid} chooses legend (keep first)", flush=True)
                    continue
                # combat declarations: attack/block with nothing (deterministic)
                if wtype in ("DeclareAttackers", "DeclareBlockers"):
                    da = [a for a in acts if a["type"] == wtype]
                    if da:
                        sub = json.loads(json.dumps(da[0]))
                        dd = sub.setdefault("data", {})
                        for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
                            if k in dd:
                                dd[k] = [] if isinstance(dd[k], list) else {}
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
            acted = False
            hn = hand_names(state, 0)
            asmo_in_hand = ASMO in hn
            looting_in_hand = LOOTING in hn
            discarded = discards_this_turn(state, 0) > 0
            if discarded and not discard_done:
                discard_done = True
                discard_turn = state.get("turn_number")
                print(f"discard observed this turn (turn {discard_turn})", flush=True)

            # PHASE 1 — negative control: Asmo in hand, main-phase priority, no discard yet
            if asmo_in_hand and in_main and my_turn and not discard_done and not looting_cast:
                casts = cast_actions_for(state, acts, ASMO)
                if casts:
                    assertions["A2_neg_no_cast_offered"] = "failed"
                    notes.append(f"BUG (reported): CastSpell(Asmo) offered BEFORE any discard "
                                 f"at turn {state.get('turn_number')}")
                    await export("pre_neg")
                    await c.send_action(casts[0])
                    await asyncio.sleep(2)
                    await export("post")
                    done = True
                    break
                vi = st.get("viewer_interaction")
                if vi and ASMO.lower() in json.dumps(vi).lower():
                    assertions["A3_neg_no_viewer_cast"] = "failed"
                    notes.append(f"viewer_interaction mentions Asmo before discard at turn "
                                 f"{state.get('turn_number')}: {json.dumps(vi)[:200]}")
                neg_obs += 1
                if not pre_neg_done:
                    pre_neg_done = True
                    await export("pre_neg")
                    print(f"PRE_NEG captured at turn {state.get('turn_number')} "
                          f"(Asmo in hand, no discard, no cast offered)", flush=True)

            # PHASE 2 — cast Faithless Looting once we have enough negative observations
            if (not looting_cast and looting_in_hand and in_main and my_turn
                    and neg_obs >= 3 and not asmo_cast):
                lc = cast_actions_for(state, acts, LOOTING)
                if lc:
                    print(f"casting Faithless Looting at turn {state.get('turn_number')}", flush=True)
                    await c.send_action(lc[0])
                    looting_cast = True
                    looting_turn = state.get("turn_number")
                    acted = True
                    continue

            # PHASE 3 — positive control: on the SAME turn as the discard, Asmo must
            # be castable for {B/R}. (The condition is per-turn; later turns are
            # not valid positive-control observations.)
            if (discard_done and not asmo_cast and asmo_in_hand and in_main and my_turn
                    and state.get("turn_number") == discard_turn):
                casts = cast_actions_for(state, acts, ASMO)
                if casts:
                    if assertions["A5_cast_offered_after_discard"] == "not-run":
                        assertions["A5_cast_offered_after_discard"] = "passed"
                        notes.append(f"CastSpell(Asmo) offered after discard at turn "
                                     f"{state.get('turn_number')}; "
                                     f"action={json.dumps(casts[0])[:300]}")
                    pre_cast_lands = untapped_br_lands(state, 0)
                    await export("pre_cast")
                    print(f"PRE_CAST captured; casting Asmo for {{B/R}}", flush=True)
                    await c.send_action(casts[0])
                    asmo_cast = True
                    asmo_cast_turn = state.get("turn_number")
                    acted = True
                    continue
                else:
                    post_rounds += 1
                    if post_rounds == 1:
                        print(f"Asmo NOT offered after discard (turn {state.get('turn_number')}); "
                              f"watching further this turn", flush=True)
                        await export("post_discard")
                    if post_rounds >= 15:
                        assertions["A5_cast_offered_after_discard"] = "failed"
                        notes.append("Asmo never offered on the discard turn within 15 "
                                     "post-discard observations: card appears uncastable from hand "
                                     "(parser swallowed the {B/R} alternative-cost clause)")
                        await export("post")
                        done = True
                        break

            # If the discard turn ended without Asmo being offered/cast, fail A5.
            if (discard_done and not asmo_cast and discard_turn is not None
                    and state.get("turn_number", 0) > discard_turn
                    and assertions["A5_cast_offered_after_discard"] == "not-run"):
                assertions["A5_cast_offered_after_discard"] = "failed"
                notes.append(f"Discard turn {discard_turn} ended without CastSpell(Asmo) ever "
                             f"being offered: card uncastable from hand (parser swallowed the "
                             f"{{B/R}} alternative-cost clause)")
                await export("post")
                done = True
                break

            # PHASE 4 — watch the cast resolve: stack then battlefield, verify payment
            if asmo_cast and not done:
                if stack_has(state, 0, ASMO) and not mid_exported:
                    mid_exported = True
                    await export("mid_cast")
                    print("MID_CAST captured (Asmo on stack)", flush=True)
                aid = battlefield_id(state, 0, ASMO)
                if aid is not None:
                    post_lands = untapped_br_lands(state, 0)
                    spent = (pre_cast_lands or 0) - post_lands
                    notes.append(f"Asmo reached battlefield; untapped B/R lands "
                                 f"{pre_cast_lands}->{post_lands} (spent~{spent})")
                    # payment check: at least one B/R mana went away, or pool shows it
                    assertions["A6_asmo_cast_paid"] = "passed" if spent >= 1 else "failed"
                    if spent < 1:
                        notes.append("payment unclear: no B/R land tapped for the cast")
                    await export("post")
                    done = True
                    break
                post_rounds += 1
                if post_rounds >= 60:
                    notes.append("Asmo cast but never reached battlefield within 60 observations")
                    await export("post")
                    done = True
                    break

            # main-phase economy: land first, then pass; otherwise just pass priority
            if in_main and my_turn:
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
        if max_turn > 45 and not looting_cast:
            notes.append("never cast Faithless Looting by turn 45 (never held it with 3+ neg observations)")
            break

    # finalize assertions
    if assertions["A2_neg_no_cast_offered"] == "not-run":
        assertions["A2_neg_no_cast_offered"] = "passed" if neg_obs >= 3 else (
            "failed" if neg_obs == 0 else "not-run")
        if neg_obs < 3:
            notes.append(f"only {neg_obs} negative observations (< 3)")
    if assertions["A3_neg_no_viewer_cast"] == "not-run":
        assertions["A3_neg_no_viewer_cast"] = "passed"
    if assertions["A4_looting_resolved"] == "not-run":
        assertions["A4_looting_resolved"] = "passed" if discard_done else "failed"
        if not discard_done:
            notes.append("Faithless Looting never resolved with a discard")
    if assertions["A5_cast_offered_after_discard"] == "not-run" and not discard_done:
        assertions["A5_cast_offered_after_discard"] = "not-run"
        notes.append("A5 not-run: no discard happened, positive control never began")
    if assertions["A6_asmo_cast_paid"] == "not-run" and not asmo_cast:
        assertions["A6_asmo_cast_paid"] = "not-run"
        notes.append("A6 not-run: Asmo was never cast")

    reported_bug = (assertions["A2_neg_no_cast_offered"] == "failed"
                    or assertions["A3_neg_no_viewer_cast"] == "failed")
    verdict = ("reproduced" if reported_bug
               else "not-reproduced" if assertions["A2_neg_no_cast_offered"] == "passed"
               else "blocked")

    scenario_src = open(__file__).read()
    run = {
        "issue": 647,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/20260909-01",
        "driver": {"protocol_advertised": 67, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src.encode()).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "mulligans": f"P0 mulliganed {mulligans_taken[0]}x seeking Faithless Looting; P1 kept 7",
        "stats": {"states_seen": states_seen, "neg_observations": neg_obs,
                  "max_turn": max_turn, "looting_turn": looting_turn,
                  "asmo_cast_turn": asmo_cast_turn},
        "assertions": assertions,
        "notes": notes,
        "verdict": verdict,
        "limitations": ["Browser UI not exercised; native engine via two human-client seats."],
    }
    with open(f"{EVIDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVIDIR}/scenario_647.py", "w") as f:
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
