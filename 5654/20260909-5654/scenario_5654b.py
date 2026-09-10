#!/usr/bin/env python3
"""Issue #5654, game B rerun: Plagiarize target handling instrumentation.

Game B (run 20260909-5654) showed Plagiarize resolving to the graveyard with
no target prompt ever advertised and no replacement established. This rerun
instruments every tick between CastSpell submission and resolution: full
waiting_for, legal action types, stack entries (with recorded targets), and
every viewer_interaction opportunity verbatim. It answers target prompts
through whichever surface the engine uses (schema interaction OR a
ChooseTargets-style legal action).
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
RUN_ID = "20260909-5654"
EVDIR = f"{BACKFILL}/evidence/5654/{RUN_ID}"
WIRE = open(f"{EVDIR}/wire_log_B2.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run_B2.log", "w")

PLAG = "plagiarize"
ISLAND = "island"
B_DECK_P0 = [(PLAG, 8), (ISLAND, 52)]
B_DECK_P1 = [(ISLAND, 60)]


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "payload": payload}, default=str) + "\n")
    WIRE.flush()


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def lname(state, oid):
    return obj_name(state.get("objects", {}).get(str(oid), {}))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def stack_summary(state):
    out = []
    for e in state.get("stack", []) or []:
        out.append({k: e.get(k) for k in ("id", "source", "targets",
                                          "controller", "effect")
                    if k in e} or str(e)[:120])
    return out


async def main():
    t_start = time.time()
    p0 = PhaseClient("B2-P0")
    await p0.connect()
    await p0.create(deck(*B_DECK_P0))
    p1 = PhaseClient("B2-P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*B_DECK_P1))
    say(f"game {p0.game_code}")

    obs = {"cast_submitted": False, "cast_rev": None, "target_via": None,
           "ticks_after_cast": 0, "snapshots": []}
    pre = post = None
    pre_done = post_done = False
    submitted = set()

    def snap(tag, state):
        obs["ticks_after_cast"] += 1
        if obs["ticks_after_cast"] > 40:
            return
        wf = state.get("waiting_for") or {}
        s = {"tag": tag, "rev": p0.revision,
             "waiting_for_type": wf.get("type"),
             "waiting_for_data": str(wf.get("data", {}))[:500],
             "priority_player": state.get("priority_player"),
             "active_player": state.get("active_player"),
             "phase": state.get("phase"),
             "turn": state.get("turn_number"),
             "stack": stack_summary(state)}
        obs["snapshots"].append(s)
        wire("B2_tick", s)

    async def answer_target(st, state):
        """Answer a target prompt on any surface. Returns True if acted."""
        # surface 1: viewer_interaction schema opportunity
        vi = st.get("viewer_interaction") or {}
        if vi.get("canSubmit"):
            for opp in vi.get("opportunities", []) or []:
                iid = opp.get("interactionId")
                if iid in submitted:
                    continue
                wire("B2_opportunity", {"opp": opp})
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                chs = data.get("choices") or data.get("candidates") or []
                pick = None
                for ch in chs:
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data") or {}
                        if isinstance(d, dict) and d.get("seat") == 1:
                            pick = ch
                            break
                    if pick:
                        break
                if pick is not None:
                    spec = data.get("spec", {}) or {}
                    stype = spec.get("type") if isinstance(spec, dict) else None
                    sub = {"interactionId": iid,
                           "response": {"type": stype or resp.get("type") or "sequence",
                                        "data": {"choiceIds": [pick["id"]]}}}
                    # exactChoices uses choose/choiceId singular
                    if (resp.get("type") or "") == "exactChoices":
                        sub = {"interactionId": iid,
                               "response": {"type": "choose",
                                            "data": {"choiceId": pick["id"]}}}
                    wire("B2_target_submit", {"submission": sub, "opp": opp})
                    await p0.send_interaction(sub)
                    submitted.add(iid)
                    obs["target_via"] = f"interaction:{resp.get('type')}/{stype}"
                    say(f"answered target prompt via {obs['target_via']}")
                    return True
        # surface 2: legal actions mentioning target
        for a in merged_actions(st):
            at = a.get("type", "")
            if "arget" in at:
                wire("B2_target_action", a)
                say(f"saw target-ish legal action: {at}; NOT auto-answering, "
                    f"recording only")
        return False

    async def p0_tick(st, acts, state):
        nonlocal pre, post, pre_done, post_done
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = next((a for a in acts if a["type"] == "MulliganDecision"), None)
        if ma:
            hn = [lname(state, o) for o in player_of(state, 1 - 1).get("hand", [])]
            # keep if plagiarize + 2 lands (mulligan bookkeeping minimal)
            from collections import Counter
            c = Counter(hn)
            keep = PLAG in hn and c[ISLAND] >= 2
            await p0.send_action({"type": "MulliganDecision",
                                  "data": {"choice": {"type": "Keep" if keep else "Mulligan"}}})
            say(f"P0 {'keeps' if keep else 'mulligans'}")
            return
        if wtype == "MulliganDecision":
            # post-mulligan BottomCards phase
            sc = next((a for a in acts if a["type"] == "SelectCards"), None)
            if sc:
                pending = (state.get("waiting_for") or {}).get("data", {}) or {}
                count = 1
                for p in pending.get("pending", []) or []:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hids = [int(o) for o in player_of(state, 0).get("hand", [])]
                picks = hids[:count]
                await p0.send_action({"type": "SelectCards",
                                      "data": {"cards": picks}})
                say(f"P0 bottoms {count}")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                msg = {"type": a["type"]}
                if "data" in a:
                    msg["data"] = a["data"]
                await p0.send_action(msg)
                return
        if obs["cast_submitted"] and not post_done:
            snap("after_cast", state)
        if await answer_target(st, state):
            return
        # post: P1 past draw step
        if (obs["cast_submitted"] and not post_done
                and any(lname(state, o) == PLAG
                        for o in player_of(state, 0).get("graveyard", []))
                and state.get("active_player") == 1
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(state.get("stack", []) or []) == 0):
            say("P1 past draw step; exporting POST_B2")
            post = json.loads(await p0.export_state())["state"]
            open(f"{EVDIR}/post_B2.json", "w").write(json.dumps({"state": post}))
            post_done = True
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # cast Plagiarize during P1's Upkeep
        ui = [o for oid, o in state.get("objects", {}).items()
              if o.get("zone") == "Battlefield" and o.get("controller") == 0
              and not o.get("tapped") and obj_name(o) == "island"]
        has_plag = any(lname(state, o) == PLAG
                       for o in player_of(state, 0).get("hand", []))
        if (not obs["cast_submitted"] and has_plag
                and state.get("active_player") == 1
                and state.get("phase") == "Upkeep" and len(ui) >= 4):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == PLAG:
                    say("P1 Upkeep: exporting PRE_B2, casting Plagiarize")
                    pre = json.loads(await p0.export_state())["state"]
                    open(f"{EVDIR}/pre_B2.json", "w").write(json.dumps({"state": pre}))
                    pre_done = True
                    msg = {"type": "CastSpell", "data": d}
                    wire("B2_cast", msg)
                    await p0.send_action(msg)
                    obs["cast_submitted"] = True
                    obs["cast_rev"] = p0.revision
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                msg = {"type": "PlayLand"}
                if "data" in a:
                    msg["data"] = a["data"]
                await p0.send_action(msg)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await p0.send_action({"type": "PassPriority"})
                return

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = next((a for a in acts if a["type"] == "MulliganDecision"), None)
        if ma:
            await p1.send_action({"type": "MulliganDecision",
                                  "data": {"choice": {"type": "Keep"}}})
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                msg = {"type": a["type"]}
                if "data" in a:
                    msg["data"] = a["data"]
                await p1.send_action(msg)
                return
        if wtype == "DeclareAttackers":
            da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
            if da:
                d = dict(da.get("data", {})); d["attacks"] = []; d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        for a in acts:
            if a["type"] == "PlayLand":
                msg = {"type": "PlayLand"}
                if "data" in a:
                    msg["data"] = a["data"]
                await p1.send_action(msg)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await p1.send_action({"type": "PassPriority"})
                return

    t0 = time.time()
    last, last_tick = {}, {}
    while time.time() - t0 < 1200:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            if c.revision == last.get(c.name) and \
                    time.time() - last_tick.get(c.name, 0) <= 5:
                continue
            last[c.name] = c.revision
            last_tick[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        if post_done:
            break
    if post is None and pre is not None:
        try:
            post = json.loads(await p0.export_state())["state"]
            open(f"{EVDIR}/post_B2.json", "w").write(json.dumps({"state": post}))
            post_done = True
        except Exception as e:
            say(f"final export failed: {e}")

    # evaluate B2
    ass = {}
    notes = []
    if pre is not None:
        h0pre = len(player_of(pre, 0)["hand"]); l0pre = len(player_of(pre, 0)["library"])
        h1pre = len(player_of(pre, 1)["hand"]); l1pre = len(player_of(pre, 1)["library"])
        ok = (any(lname(pre, o) == PLAG for o in player_of(pre, 0)["hand"])
              and pre.get("active_player") == 1 and pre.get("phase") == "Upkeep")
        ass["A1B2_setup"] = "passed" if ok else "failed"
        notes.append(f"pre_B2: plag in hand, P1 upkeep: {'ok' if ok else 'BAD'}")
    else:
        ass["A1B2_setup"] = "failed"; notes.append("pre_B2 missing")
    ass["A_target_prompt"] = ("passed" if obs["target_via"] else "failed")
    notes.append(f"target prompt surfaced and answered via: {obs['target_via']}")
    if pre is not None and post is not None:
        h0post = len(player_of(post, 0)["hand"]); l0post = len(player_of(post, 0)["library"])
        h1post = len(player_of(post, 1)["hand"]); l1post = len(player_of(post, 1)["library"])
        say(f"[B2] P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        obs["deltas"] = {"P0": (h0pre, h0post, l0pre, l0post),
                         "P1": (h1pre, h1post, l1pre, l1post)}
        skip_ok = (h1post == h1pre and l1post == l1pre)
        draw_ok = (h0post == h0pre and l0post == l0pre - 1)
        ass["A4B2_skip_holds"] = "passed" if skip_ok else "failed"
        ass["A5B2_ctrl_draws"] = "passed" if draw_ok else "failed"
        notes.append(f"skip: P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post} "
                     f"({'ok' if skip_ok else 'BROKEN'})")
        notes.append(f"controller draw: P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post} "
                     f"({'ok' if draw_ok else 'BROKEN'})")
        gy_ok = any(lname(post, o) == PLAG for o in player_of(post, 0)["graveyard"])
        ass["A6B2_cleanup"] = "passed" if gy_ok and not post.get("stack") else "failed"
    run = {"issue": 5654, "run_id": RUN_ID, "sub_run": "B2",
           "duration_s": round(time.time() - t_start, 1),
           "assertions": ass, "notes": notes, "observations": obs,
           "purpose": "instrumented rerun of game B target handling"}
    open(f"{EVDIR}/run_B2.json", "w").write(json.dumps(run, indent=1))
    WIRE.close(); RUNLOG.close()
    print(f"DONE B2 assertions={json.dumps(ass)}", flush=True)


asyncio.run(main())
