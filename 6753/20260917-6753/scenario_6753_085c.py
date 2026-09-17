#!/usr/bin/env python3
"""Issue #6753 supplementary capture C (same run 20260917-6753, v0.85.0).

Wrong-library secondary-defect re-test with a HUMAN-driven caster: the
v0.85.0 AI policy targets only its own permanents (owner == caster), so the
AI-driven runs cannot distinguish the v0.78.0 defect (Audacious Swap exiling
the top card of the CASTER's library instead of the targeted permanent's
OWNER's library). The defect lives in the ENGINE's resolution
(ExileTop player = ParentTarget resolved to the spell's controller), which
is identical regardless of who casts -- so a human P1 casting Audacious
Swap at a P0-owned permanent exercises the same code path with
owner (0) != caster (1).

Setup: P0 passive (60x island). P1 human: 4x audacious swap + 28x island +
28x mountain. P1 casts Audacious Swap targeting a P0 island, then pre/post
states are exported around the cast and the exile source is evaluated.
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

client.URL = "ws://localhost:9374/ws"
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260917-6753"
EVDIR = f"{BACKFILL}/evidence/6753/{RUN_ID}"
RUNLOG = open(f"{EVDIR}/scenario_run_supplement_c.log", "w")

SWAP = "audacious swap"
P0_DECK = [("island", 60)]
P1_DECK = [(SWAP, 4), ("island", 28), ("mountain", 28)]
GAME_TIMEOUT = 600


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def ref_key(ref):
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
        return None
    return None


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def candidate_oid_map(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    m = {}
    for ch in chs:
        rk = ref_key(ch.get("id"))
        if rk is not None:
            m[rk] = ch
        for s in ch.get("surfaces", []) or []:
            rk2 = ref_key((s.get("data") or {}).get("reference"))
            if rk2 is not None and rk2 not in m:
                m[rk2] = ch
    return m


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if a.get("data") not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def swap_on_stack(state):
    return [oid for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Stack" and lname(state, oid) == SWAP]


def analyze_pair(pre_st, post_st):
    pre_objs = pre_st.get("objects", {})
    post_objs = post_st.get("objects", {})
    swap_oids = [oid for oid, o in pre_objs.items()
                 if o.get("zone") == "Stack" and
                 str(o.get("base_name") or "").lower() == SWAP]
    swap_oid = swap_oids[0] if swap_oids else None
    moved = [(oid, str(pre_objs[oid].get("base_name") or "").lower(),
              pre_objs[oid].get("controller"), pre_objs[oid].get("owner"))
             for oid in pre_objs
             if pre_objs[oid].get("zone") == "Battlefield"
             and str(pre_objs[oid].get("base_name") or "").lower() != SWAP
             and post_objs.get(oid, {}).get("zone") == "Library"]
    target_owner = moved[0][3] if len(moved) == 1 else None
    exiled = []
    for oid, o in pre_objs.items():
        if oid == swap_oid or oid in [m[0] for m in moved]:
            continue
        if o.get("zone") == "Library":
            po = post_objs.get(oid, {})
            if po.get("zone") in ("Battlefield", "Exile", "Hand"):
                src = next((p.get("id") for p in pre_st.get("players", [])
                            if str(oid) in [str(x) for x in p.get("library", [])]),
                           None)
                exiled.append({"oid": oid,
                               "name": str(o.get("base_name") or "").lower(),
                               "source_library": src,
                               "post_zone": po.get("zone"),
                               "post_controller": po.get("controller")})
    gy = [oid for oid, o in post_objs.items()
          if o.get("zone") == "Graveyard" and o.get("controller") == 1
          and str(o.get("base_name") or "").lower() == SWAP]
    libs = {}
    for p in pre_st.get("players", []):
        pid = p.get("id")
        po = next((q for q in post_st.get("players", []) if q.get("id") == pid), {})
        libs[pid] = (len(p.get("library", [])), len(po.get("library", [])))
    return {"swap_oid": swap_oid,
            "target_moved": [{"oid": m[0], "name": m[1], "controller": m[2],
                              "owner": m[3]} for m in moved],
            "target_owner": target_owner, "exiled_candidates": exiled,
            "swap_in_caster_graveyard": len(gy), "library_deltas": libs,
            "turn": pre_st.get("turn_number")}


async def main():
    p0 = PhaseClient("P0c")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    say(f"game {p0.game_code}")
    p1 = PhaseClient("P1c")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"P1 joined seat {p1.player_id}")

    async def keep_mulligan(c):
        t0 = time.time()
        while time.time() - t0 < 60:
            await asyncio.sleep(0.25)
            st = c.latest
            if not st:
                continue
            for a in merged_actions(st):
                if a["type"] == "MulliganDecision":
                    await submit_as_is(
                        c, {"type": "MulliganDecision",
                            "data": {"choice": {"type": "Keep"}}})
                    return True
        return False

    await keep_mulligan(p0)
    await keep_mulligan(p1)
    say("both kept")

    land_turns = {0: set(), 1: set()}
    answered_iids = set()
    cast_pending = None   # {"oid": hand oid, "stage": "cast"|"targeted"}
    pre_state = None
    pair = None
    p1_acts = 0
    rejections = []
    t_loop = time.time()
    last = {0: (-1, 0.0), 1: (-1, 0.0)}

    def drain(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("Error", "ActionRejected"):
                rejections.append({"who": c.name, "type": t, "data": data})

    while time.time() - t_loop < GAME_TIMEOUT and pair is None:
        await asyncio.sleep(0.25)
        for c, pid in ((p0, 0), (p1, 1)):
            drain(c)
            msg = c.latest
            if not msg:
                continue
            rev = c.revision
            last_rev, last_tick = last[pid]
            may_act = (rev != last_rev) or (time.time() - last_tick > 5)
            state = msg.get("state", {})
            wf = wf_of(state)
            wfd = wf.get("data") or {}
            wtype = wf.get("type")

            # --- P1 cast tracking ---
            if pid == 1:
                soids = swap_on_stack(state)
                if cast_pending and cast_pending["stage"] == "targeted" and soids:
                    cast_pending["stage"] = "on_stack"
                    cast_pending["stack_oid"] = soids[0]
                    cast_pending["cast_t"] = time.time()
                    try:
                        pre_state = json.loads(await p0.export_state())["state"]
                        with open(f"{EVDIR}/pre_human.json", "w") as f:
                            json.dump({"state": pre_state}, f)
                        say(f"P1 swap on stack (oid {soids[0]}); pre_human.json exported")
                    except Exception as e:
                        say(f"pre_human export failed: {e}")
                if (cast_pending and cast_pending["stage"] == "on_stack"
                        and not soids):
                    cast_pending["stage"] = "resolved"
                    cast_pending["resolve_s"] = round(
                        time.time() - cast_pending["cast_t"], 2)
                    say(f"P1 swap resolved in {cast_pending['resolve_s']}s")
                    try:
                        post_state = json.loads(await p0.export_state())["state"]
                        with open(f"{EVDIR}/post_human.json", "w") as f:
                            json.dump({"state": post_state}, f)
                        if pre_state is not None:
                            pair = analyze_pair(pre_state, post_state)
                            pair["resolve_s"] = cast_pending["resolve_s"]
                            say(f"pair analyzed: target_owner="
                                f"{pair['target_owner']} exiled="
                                f"{pair['exiled_candidates']}")
                    except Exception as e:
                        say(f"post_human export/analyze failed: {e}")
                    if pair is None:
                        say("pair capture failed; will retry with next swap")
                        cast_pending = None
                    break

            # --- P1 Casualty OptionalCostChoice: always decline (no creatures) ---
            if pid == 1 and cast_pending and cast_pending["stage"] == "cast" \
                    and wtype == "OptionalCostChoice" and wf_player(state) == 1:
                vi = get_vi(msg)
                if vi:
                    for opp in vi.get("opportunities", []) or []:
                        iid = opp.get("interactionId")
                        if iid in answered_iids:
                            continue
                        resp = opp.get("response", {}) or {}
                        data = resp.get("data", {}) or {}
                        chs = data.get("choices") or data.get("candidates") or []
                        pick = None
                        for ch in chs:
                            ac = [((s.get("data") or {}).get("code") or "")
                                  for s in ch.get("surfaces", []) or []
                                  if s.get("type") == "action"]
                            vals = [((s.get("data") or {}).get("role"),
                                     (s.get("data") or {}).get("value"))
                                    for s in ch.get("surfaces", []) or []
                                    if s.get("type") == "value"]
                            if "decideOptionalCost" in ac and \
                                    ("pay", "false") in vals:
                                pick = ch
                                break
                        if pick is not None:
                            sub = {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId": pick.get("id")}}}
                            await p1.send_interaction(sub)
                            answered_iids.add(iid)
                            say("P1 declined Casualty optional cost")
                        break
                # never pass priority while the cost decision is pending
                last[pid] = (rev, time.time())
                continue

            # --- P1 target selection: choose a P0 island ---
            if pid == 1 and cast_pending and cast_pending["stage"] == "cast" \
                    and wtype == "TargetSelection" and wf_player(state) == 1:
                vi = get_vi(msg)
                if vi:
                    sel = (wfd.get("selection") or {})
                    legal = {ref_key(t) for t in
                             (sel.get("current_legal_targets") or [])}
                    want = next((str(o) for o in bf_ids(state, 0, "island")
                                 if str(o) in legal), None)
                    for opp in vi.get("opportunities", []) or []:
                        iid = opp.get("interactionId")
                        if iid in answered_iids:
                            continue
                        cmap = candidate_oid_map(opp)
                        ch = cmap.get(want) if want else None
                        if ch is None:
                            say(f"P1 target: want P0 island {want} not among "
                                f"candidates {list(cmap)[:6]}; holding")
                            break
                        resp = opp.get("response", {}) or {}
                        rtype = resp.get("type")
                        data = resp.get("data", {}) or {}
                        if rtype == "schema":
                            stype = (data.get("spec") or {}).get("type") or "sequence"
                            sub = {"interactionId": iid,
                                   "response": {"type": stype,
                                                "data": {"choiceIds": [ch.get("id")]}}}
                        else:
                            sub = {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId": ch.get("id")}}}
                        await p1.send_interaction(sub)
                        answered_iids.add(iid)
                        cast_pending["stage"] = "targeted"
                        cast_pending["target_oid"] = want
                        say(f"P1 targeted P0 island oid {want}")
                        break

            # while P1's target decision is in flight, take no other actions
            if pid == 1 and cast_pending and \
                    cast_pending["stage"] == "targeted":
                last[pid] = (rev, time.time())
                continue

            # --- drive ---
            if may_act:
                acts = merged_actions(msg)
                turn = state.get("turn_number")
                my_priority = (wtype == "Priority"
                               and state.get("priority_player") == pid)
                acted = False
                da = next((a for a in acts if a["type"] == "DeclareAttackers"),
                          None)
                if da and wf_player(state) == pid:
                    sub = copy.deepcopy(da)
                    dd = sub.setdefault("data", {})
                    for k in ("attacks", "attackers", "blocks", "blockers",
                              "assignments"):
                        if k in dd:
                            dd[k] = [] if isinstance(dd[k], list) else {}
                    await submit_as_is(c, sub)
                    acted = True
                if not acted and pid == 1 and cast_pending is None:
                    # try to cast Audacious Swap on own main phase
                    swaps = [o for o in hand_ids(state, 1)
                             if lname(state, o) == SWAP]
                    if swaps and state.get("active_player") == 1 and \
                            state.get("phase") in ("PreCombatMain",
                                                   "PostCombatMain") and my_priority:
                        cs = next((a for a in acts
                                   if a["type"] == "CastSpell"
                                   and (a.get("data") or {}).get("object_id")
                                   in swaps), None)
                        if cs:
                            await submit_as_is(p1, cs)
                            cast_pending = {"oid": cs["data"]["object_id"],
                                            "stage": "cast"}
                            p1_acts += 1
                            say(f"P1 casts Audacious Swap "
                                f"(hand oid {cs['data']['object_id']})")
                            acted = True
                if not acted:
                    pl = next((a for a in acts if a["type"] == "PlayLand"
                               and lname(state, (a.get("data") or {}).get("object_id"))
                               in ("island", "mountain")), None)
                    if pl and turn not in land_turns[pid]:
                        await submit_as_is(c, pl)
                        land_turns[pid].add(turn)
                        acted = True
                if not acted and my_priority:
                    pp = next((a for a in acts if a["type"] == "PassPriority"),
                              None)
                    if pp:
                        await submit_as_is(c, pp)
                last[pid] = (rev, time.time())
        if pair is not None:
            break
        st0 = p0.latest.get("state", {}) if p0.latest else {}
        if st0.get("game_over") or st0.get("winner") is not None:
            say("game ended")
            break

    supplement = {
        "run_id": RUN_ID,
        "purpose": "wrong-library secondary-defect re-test on v0.85.0 with a "
                   "HUMAN-driven caster (owner 0 != caster 1); the v0.85.0 AI "
                   "policy targets only its own permanents, so AI-driven runs "
                   "cannot distinguish the defect",
        "p1_acts": p1_acts,
        "rejections": rejections,
        "pair": pair,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if pair and pair["target_owner"] == 0 and len(pair["target_moved"]) == 1:
        ex = pair["exiled_candidates"]
        if len(ex) == 1 and ex[0]["source_library"] == 0:
            supplement["wrong_library_defect"] = "not-observed (fixed)"
            supplement["explanation"] = (
                f"P1 cast Audacious Swap targeting P0's "
                f"{pair['target_moved'][0]['name']} (owner 0): target -> P0's "
                f"library; exiled {ex[0]['name']} came from P0's library "
                f"(source_library=0) -> {ex[0]['post_zone']}. Oracle-correct "
                f"on v0.85.0.")
        elif ex:
            supplement["wrong_library_defect"] = "observed (persists)"
            supplement["explanation"] = (
                f"Target owner 0 but exiled card came from player "
                f"{ex[0]['source_library']}'s library: {ex}")
        else:
            supplement["wrong_library_defect"] = "inconclusive"
            supplement["explanation"] = "no exiled candidate identified"
    else:
        supplement["wrong_library_defect"] = "inconclusive"
        supplement["explanation"] = "no P0-owned-target pair captured"
    with open(f"{EVDIR}/supplement_human.json", "w") as f:
        json.dump(supplement, f, indent=2)
    say(f"SUPPLEMENT_C: {supplement['wrong_library_defect']} -- "
        f"{supplement['explanation'][:200]}")
    RUNLOG.close()
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
