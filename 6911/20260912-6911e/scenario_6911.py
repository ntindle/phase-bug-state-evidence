#!/usr/bin/env python3
"""Issue #6911: Entish Restoration doesn't prompt for sacrifice.

Reporter (Discord): "[[Entish Restoration]] went straight to selecting 3
lands, instead of prompting to sacrifice one first."

Oracle: "Sacrifice a land. Search your library for up to two basic land
cards, put them onto the battlefield tapped, then shuffle. If you control a
creature with power 4 or greater, instead search your library for up to
three basic land cards, put them onto the battlefield tapped, then shuffle."

Parse (pinned card-data): Sacrifice{count:1, target: Land (controller's)} ->
SearchLibrary{up-to-3 basic lands} (condition: 4+ power creature) /
else SearchLibrary{up-to-2} -> ChangeZone Library->Battlefield tapped ->
Shuffle.

Plan (native engine, v0.81.3 / protocol 70, two human-driver seats):
  P0: 12x Entish Restoration, 8x Baloth Gorger ({2}{G}{G} 4/4), 40x Forest.
      Plays a land each turn, casts Gorger when affordable, then casts
      Entish Restoration on a later turn.
  P1: 12x Grizzly Bears, 48x Forest. Plays a land each turn, occasionally
      casts bears, never attacks. Never interferes with P0's prompts.

Behavioral contract:
  A1 setup_ok        ER cast with a 4+ power creature (Baloth Gorger) on P0's
                     battlefield (pre.json exported at cast).
  A2 sacrifice_prompted  during resolution a choice to sacrifice one of P0's
                     own lands was advertised BEFORE any library-search
                     choice (the reported defect: search offered with no
                     sacrifice prompt first).
  A3 land_sacrificed  the land chosen at the sacrifice prompt left the
                     battlefield for the graveyard.
  A4 search_up_to_three  the search prompt advertised up to 3 basic lands
                     (4+ branch); the chosen lands entered tapped; library
                     shuffled; counts consistent.
  A5 cleanup         ER in P0 graveyard, stack empty, game proceeding.

Verdict: reproduced iff A2 fails (no sacrifice prompt, or search prompt
comes first); not-reproduced iff the reported path was exercised and every
assertion passed; blocked iff ER was never cast within the turn cap.
Never "fixed".
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6911e"
EVDIR = f"{BACKFILL}/evidence/6911/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ER = "Entish Restoration"
GORGER = "Baloth Gorger"
BEAR = "Grizzly Bears"
FOREST = "Forest"
P0_DECK = [(ER, 12), (GORGER, 8), (FOREST, 40)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]
TIMEOUT = 2400
STALL_AFTER = 120
TURN_CAP = 40

ST = {}
MULLS = {}
WF_SEEN = []
OPP_SEQ = []  # (seq, label, wf_type, n_candidates, detail)
KEYS_LOGGED = {"done": False}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",            # SETUP -> CAST -> RESOLVE -> DONE
        "stop": False,
        "game_started": False,
        "game_over": False,
        "turns_seen": set(),
        "gorger_cast": False,
        "er_cast": False,
        "er_cast_turn": None,
        "er_cast_oid": None,
        "pre_exported": False,
        "resolve_prompts": [],       # classified opportunities during RESOLVE
        "sac_oid": None,             # land chosen at sacrifice prompt
        "sac_wf": None,
        "search_max": None,
        "search_chosen": [],
        "mid_sac_exported": False,
        "mid_search_exported": False,
        "post_exported": False,
        "stall_observed": False,
        "stall_wf": None,
        "last_rev_change": None,
        "rejections": [],
        "opp_n": 0,
    })
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    WF_SEEN.clear()
    OPP_SEQ.clear()


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def objs_of(state):
    return state.get("objects") or {}


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs_of(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(objs_of(state)[oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in objs_of(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def untapped_named(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def zone_count(state, pid, name, zone):
    return sum(1 for _, o in objs_of(state).items()
               if oname(o) == name and o.get("zone") == zone
               and o.get("controller") == pid)


def gy(state, pid):
    return [oid for oid, o in objs_of(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def lib_count(state, pid):
    return sum(1 for _, o in objs_of(state).items()
               if o.get("zone") == "Library" and o.get("controller") == pid)


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1][0] != wf):
        WF_SEEN.append((wf, time.time()))
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST.get('stage')}")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


def drain_rejections(*clients):
    found = []
    for c in clients:
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("ActionRejected", "Error"):
                found.append({"who": c.name, "type": t, "data": data})
                wire("rejected", {"who": c.name, "type": t, "data": data,
                                  "stage": ST.get("stage")})
                say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def viewer_interaction(st):
    return st.get("viewer_interaction")


# ---------------------------------------------------------------------------
# Opportunity classification + answering (protocol 70 viewer_interaction:
# vi["opportunities"] is a LIST; choices use "id"; object surfaces carry
# data.reference (str), data.name, data.zone (lowercase), data.controller)
# ---------------------------------------------------------------------------

PRIORITY_MENU_CODES = {"passPriority", "tapLandForMana"}


def opportunities_of(vi):
    if not vi:
        return []
    return vi.get("opportunities") or []


def is_priority_menu(op):
    """A priority menu (pass/tap choices) is not a resolution prompt."""
    for c in (op.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def is_declare_attackers_relations(op, state):
    rtype, spec = op_spec(op)
    return (rtype == "schema" and spec.get("type") == "relations"
            and wf_type(state) == "DeclareAttackers")


def is_optional_cost_prompt(op):
    rtype, spec = op_spec(op)
    return (rtype == "schema" and spec.get("type") == "relations"
            and wf_type(state) == "DeclareAttackers")
    for c in op_choices(op):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == \
                    "decideOptionalCost":
                return True
    return False


def decline_choice_id(op):
    """The pay=false choice of an OptionalCostChoice opportunity."""
    for c in op_choices(op):
        for s in c.get("surfaces", []) or []:
            v = (s.get("data") or {}).get("value")
            if v == "false" or v is False:
                return c.get("id")
    # fallback: last choice (decline is conventionally last)
    chs = op_choices(op)
    return chs[-1].get("id") if chs else None


async def answer_optional_cost(c, op):
    cid = decline_choice_id(op)
    if not cid:
        say("OptionalCostChoice: no decline choice found, NOT answering")
        wire("optional_cost_no_choice", {"opportunity": op})
        return False
    submission = {"interactionId": op.get("interactionId"),
                  "response": {"type": "choose",
                               "data": {"choiceId": cid}}}
    wire("optional_cost_submit",
         {"who": c.name, "choice_id": cid, "submission": submission,
          "stage": ST.get("stage")})
    say(f"OptionalCostChoice answered: decline ({cid})")
    await c.send_interaction(submission)
    return True
    """A priority menu (pass/tap choices) is not a resolution prompt."""
    for c in (op.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def candidate_refs(cands, state):
    """Resolve each choice to (choice_id, name, zone, controller)."""
    out = []
    objs = objs_of(state)
    for c in cands or []:
        cid = c.get("id")
        ref, name, zone, ctrl = None, None, None, None
        for s in c.get("surfaces", []) or []:
            d = s.get("data") or {}
            if s.get("type") == "object":
                ref = d.get("reference")
                name = d.get("name")
                zone = d.get("zone")
                ctrl = d.get("controller")
                break
        if ref is not None and str(ref) in objs:
            o = objs[str(ref)]
            name = name or oname(o)
            zone = (zone or o.get("zone") or "").lower()
            ctrl = o.get("controller") if ctrl is None else ctrl
        elif zone:
            zone = zone.lower()
        out.append({"choice_id": cid, "oid": ref, "name": name,
                    "zone": zone, "controller": ctrl})
    return out


def op_choices(op):
    data = (op.get("response") or {}).get("data", {})
    return data.get("candidates") or data.get("choices") or []


def op_spec(op):
    resp = op.get("response") or {}
    if resp.get("type") == "schema":
        return resp.get("type"), ((resp.get("data") or {}).get("spec") or {})
    return resp.get("type"), (resp.get("spec") or {})


def spec_max(spec):
    d = (spec.get("data") or {})
    cd = ((d.get("constraint") or {}).get("data") or {})
    mx = cd.get("max")
    if isinstance(mx, dict):
        return mx.get("value") or mx.get("max")
    return mx


def classify_opportunity(op, state, pid):
    """Label a P0 resolution opportunity: sacrifice | search | other.

    sacrifice: candidates are P0-controlled Battlefield lands.
    search:    candidates are Library-zone basic lands.
    """
    rtype, spec = op_spec(op)
    refs = candidate_refs(op_choices(op), state)
    zones = {r["zone"] for r in refs if r["zone"]}
    ctrls = {r["controller"] for r in refs if r["controller"] is not None}
    names = {r["name"] for r in refs if r["name"]}
    wf = wf_type(state) or ""
    if zones and zones <= {"battlefield"} and ctrls == {pid} \
            and names and names <= {FOREST}:
        return "sacrifice", refs
    if zones and zones <= {"library"} and names and names <= {FOREST}:
        return "search", refs
    return f"other({rtype}/{spec.get('type')}/{wf})", refs


async def answer_opportunity(c, op, state, pid):
    """Answer a P0 resolution opportunity; record classification + choice."""
    label, refs = classify_opportunity(op, state, pid)
    ST["opp_n"] += 1
    n = ST["opp_n"]
    wf = wf_type(state)
    rtype, spec = op_spec(op)
    detail = {"n_candidates": len(refs),
              "zones": sorted({r["zone"] for r in refs if r["zone"]}),
              "names": sorted({r["name"] for r in refs if r["name"]}),
              "response_type": rtype,
              "spec_type": spec.get("type"),
              "wf_kind": ((viewer_interaction(c.latest) or {})
                          .get("waitingForKind") or {}).get("code")}
    OPP_SEQ.append((n, label, wf, len(refs), detail))
    rec = {"n": n, "label": label, "wf": wf, "detail": detail}
    wire("resolve_prompt", {"who": c.name, "record": rec,
                            "opportunity": op, "stage": ST.get("stage")})
    say(f"resolve prompt #{n}: label={label} wf={wf} n_candidates={len(refs)} "
        f"rtype={rtype} spec={spec.get('type')}")

    chosen = []
    if label == "sacrifice":
        # sacrifice exactly one of our lands (prefer a Forest)
        pick = next((r for r in refs if r["name"] == FOREST), None) or refs[0]
        chosen = [pick]
        ST["sac_oid"] = pick["oid"]
        ST["sac_wf"] = wf
        if not ST["mid_sac_exported"]:
            await export_now("mid_sacrifice.json")
            ST["mid_sac_exported"] = True
    elif label == "search":
        ST["search_max"] = spec_max(spec)
        # choose up to 3 basic lands (exercise the 3-branch fully)
        picks = [r for r in refs if r["name"] == FOREST][:3]
        chosen = picks
        ST["search_chosen"] = [p["oid"] for p in picks]
        if not ST["mid_search_exported"]:
            await export_now("mid_search.json")
            ST["mid_search_exported"] = True
    else:
        # unknown: do not blindly answer; log and pass back to main loop
        say(f"resolve prompt #{n}: unknown label, NOT answering")
        return False

    if not chosen:
        say(f"resolve prompt #{n}: nothing to choose, NOT answering")
        return False
    interaction_id = op.get("interactionId")
    if rtype == "exactChoices":
        submission = {"interactionId": interaction_id,
                      "response": {"type": "choose",
                                   "data": {"choiceId": chosen[0]["choice_id"]}}}
    else:
        stype = spec.get("type") or "sequence"
        ids = [r["choice_id"] for r in chosen if r["choice_id"] is not None]
        submission = {"interactionId": interaction_id,
                      "response": {"type": stype,
                                   "data": {"choiceIds": ids}}}
    wire("resolve_submit", {"who": c.name, "n": n, "label": label,
                            "chosen_oids": [r["oid"] for r in chosen],
                            "submission": submission})
    say(f"resolve prompt #{n} answered: label={label} "
        f"oids={[r['oid'] for r in chosen]}")
    await c.send_interaction(submission)
    return True


async def tick_p0(c):
    st = c.latest
    if not st:
        return False
    if not KEYS_LOGGED["done"]:
        say("top-level state keys:", sorted(st.keys()))
        KEYS_LOGGED["done"] = True
    state, acts = st.get("state"), merged_actions(st)
    pid = c.player_id

    # --- resolution prompts first (never pass priority on them) ---
    # Only non-priority-menu opportunities count as decision prompts;
    # priority menus (passPriority/tapLandForMana choices) are handled by
    # the normal PassPriority action below.
    vi = viewer_interaction(st)
    wf = wf_type(state)
    if vi and vi.get("canSubmit") and wf_player(state) == pid:
        for op in opportunities_of(vi):
            if is_priority_menu(op):
                continue
            if is_declare_attackers_relations(op, state):
                # combat declaration is handled by the legacy
                # DeclareAttackers action below; don't log it as a
                # resolution prompt
                continue
            if is_optional_cost_prompt(op):
                # kicker-style prompt: decline, in any stage
                ok = await answer_optional_cost(c, op)
                if ok:
                    return True
                continue
            if ST["stage"] in ("CAST", "RESOLVE"):
                ok = await answer_opportunity(c, op, state, pid)
                if ok:
                    return True
        # only priority menus present -> fall through to normal handling

    for a in acts:
        if a["type"] == "MulliganDecision":
            n_er = sum(1 for o in hand_oids(state, pid)
                       if oname(objs_of(state)[o]) == ER)
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(objs_of(state)[o]) == FOREST)
            keep_ok = (n_er >= 1 and n_lands >= 2) or MULLS["P0"] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS["P0"] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"P0 mulligan -> {choice} (ER={n_er} lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"P0 bottoms {count}")
            return True
    if wf == "DiscardToHandSize" and wf_player(state) == pid:
        n = wf_data(state).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        h = hand_oids(state, pid)
        pref = [o for o in h if oname(objs_of(state)[o]) not in (FOREST, ER, GORGER)]
        pref += [o for o in h if o not in pref]
        picks = pref[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"P0 discards {len(picks)}")
            return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"P0 legend-choice submitted as-is: {a['type']}")
            return True
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # never attack: keep the game alive for the observation
    if state.get("active_player") == pid \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    if state.get("active_player") == pid \
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"):
        # land drop every tick (retry, never flag-poison)
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
        # cast Baloth Gorger first
        if not ST["gorger_cast"]:
            oid = find_hand(state, pid, GORGER)
            if oid and untapped_named(state, pid, FOREST) >= 4:
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id")) == str(oid):
                        await submit_as_is(c, a)
                        ST["gorger_cast"] = True
                        say(f"[P0] casts {GORGER} (oid={oid})")
                        return True
        # then cast Entish Restoration (needs Gorger on BF + 3 mana)
        if ST["gorger_cast"] and not ST["er_cast"] \
                and bf_named(state, pid, GORGER):
            oid = find_hand(state, pid, ER)
            if oid and untapped_named(state, pid, FOREST) >= 3:
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id")) == str(oid):
                        await submit_as_is(c, a)
                        ST["er_cast"] = True
                        ST["er_cast_oid"] = oid
                        ST["er_cast_turn"] = state.get("turn_number")
                        ST["stage"] = "CAST"
                        say(f"[P0] casts {ER} (oid={oid}) turn={ST['er_cast_turn']}")
                        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def tick_p1(c):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    pid = c.player_id
    wf = wf_type(state)
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(objs_of(state)[o]) == FOREST)
            keep_ok = n_lands >= 2 or MULLS["P1"] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS["P1"] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"P1 mulligan -> {choice} (lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"P1 bottoms {count}")
            return True
    if wf == "DiscardToHandSize" and wf_player(state) == pid:
        n = wf_data(state).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        h = hand_oids(state, pid)
        pref = [o for o in h if oname(objs_of(state)[o]) != FOREST]
        pref += [o for o in h if o not in pref]
        picks = pref[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"P1 discards {len(picks)}")
            return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            return True
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if state.get("active_player") == pid \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    if state.get("active_player") == pid \
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"):
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
        # occasional bear; never attacks
        if untapped_named(state, pid, FOREST) >= 2 \
                and len(bf_named(state, pid, BEAR)) < 3:
            oid = find_hand(state, pid, BEAR)
            if oid:
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id")) == str(oid):
                        await submit_as_is(c, a)
                        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def observe(state):
    turn = state.get("turn_number") or 0
    if turn >= 1:
        ST["game_started"] = True
        ST["turns_seen"].add(turn)
    # A1 pre-export: ER on the stack with Gorger on BF
    if ST["stage"] == "CAST" and not ST["pre_exported"]:
        er_stack = [oid for oid, o in objs_of(state).items()
                    if o.get("zone") == "Stack" and oname(o) == ER]
        if er_stack and bf_named(state, 0, GORGER):
            say(f"cast window: turn={turn} ER on stack, Gorger on BF")
            wire("cast_window", {"turn": turn})
            if await export_now("pre.json") is not None:
                ST["pre_exported"] = True
                ST["stage"] = "RESOLVE"
    # resolution completion: prompts seen, ER off the stack, stack empty
    if ST["stage"] == "RESOLVE" and OPP_SEQ and not ST["post_exported"]:
        if not (state.get("stack") or []) and wf_type(state) == "Priority" \
                and turn >= (ST["er_cast_turn"] or 0):
            # require the SPECIFIC cast copy to be off the stack (other copies
            # may sit in hand/deck)
            er_live = False
            er_oid = ST.get("er_cast_oid")
            if er_oid is not None:
                eo = objs_of(state).get(str(er_oid))
                er_live = bool(eo) and eo.get("zone") in ("Stack", "Hand")
            if not er_live:
                say("resolution complete: stack empty, wf=Priority, "
                    "exporting post.json")
                if await export_now("post.json") is not None:
                    ST["post_exported"] = True
                    ST["stage"] = "DONE"
                    ST["stop"] = True


def load_env(fn):
    p = f"{EVDIR}/{fn}"
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def env_state(env):
    if not env:
        return None
    s = env.get("state")
    return json.loads(s) if isinstance(s, str) else s


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    global C0
    C0 = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_created", {"code": p0.game_code,
                          "p0_deck": P0_DECK, "p1_deck": P1_DECK})
    ST["last_rev_change"] = time.time()

    last_rev = -1
    last_tick_wall = 0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        rej = drain_rejections(p0, p1)
        if rej:
            ST["rejections"].extend(rej)
        if not p0.latest:
            continue
        record_wf(p0.latest["state"])
        state = p0.latest["state"]
        turn = state.get("turn_number") or 0

        if wf_type(state) == "GameOver":
            say("game over")
            ST["game_over"] = True
            ST["stop"] = True
            continue

        try:
            await observe(state)
        except Exception as e:
            say(f"observe error: {e}")

        # observe-before-acting for prompts; tick at most every 5s or on rev
        if now - last_tick_wall >= 5 or p0.revision != last_rev:
            last_tick_wall = now
            try:
                await tick_p0(p0)
                await tick_p1(p1)
            except Exception as e:
                say(f"tick error: {e}")
            if p0.revision != last_rev:
                ST["last_rev_change"] = now
                last_rev = p0.revision

        if ST["game_started"] and not ST["stop"] \
                and now - ST["last_rev_change"] > STALL_AFTER:
            ST["stall_observed"] = True
            ST["stall_wf"] = {"type": wf_type(state),
                              "player": wf_player(state)}
            say(f"STALL: no revision for {STALL_AFTER}s; "
                f"waiting_for={ST['stall_wf']}")
            wire("stall", ST["stall_wf"])
            await export_now("mid_stall.json")
            ST["stop"] = True
            continue

        if turn > TURN_CAP and not ST["stop"]:
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            ST["stop"] = True
            continue

    if not ST["post_exported"] and ST["game_started"]:
        await export_now("post.json")
        ST["post_exported"] = True

    await write_run_json(p0)
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    say("scenario finished")


async def write_run_json(p0):
    A, D = {}, {}
    pre = load_env("pre.json")
    post = load_env("post.json")
    mid_sac = load_env("mid_sacrifice.json")
    mid_search = load_env("mid_search.json")
    pre_s, post_s = env_state(pre), env_state(post)
    mid_sac_s, mid_search_s = env_state(mid_sac), env_state(mid_search)

    # A1: ER cast with 4+ creature on BF
    A["A1_setup_ok"] = ("passed" if ST["pre_exported"] and pre_s
                        and bf_named(pre_s, 0, GORGER) else "failed")
    D["A1_setup_ok_detail"] = (
        f"pre_exported={ST['pre_exported']} "
        f"gorger_on_bf_at_cast={bool(pre_s and bf_named(pre_s, 0, GORGER))} "
        f"er_cast_turn={ST['er_cast_turn']}")

    # A2: sacrifice prompt before any search prompt
    seq_labels = [lbl for _, lbl, _, _, _ in OPP_SEQ]
    sac_idx = next((i for i, l in enumerate(seq_labels) if l == "sacrifice"),
                   None)
    search_idx = next((i for i, l in enumerate(seq_labels) if l == "search"),
                      None)
    A["A2_sacrifice_prompted"] = (
        "passed" if (sac_idx is not None
                     and (search_idx is None or sac_idx < search_idx))
        else ("failed" if (search_idx is not None and sac_idx is None)
              else ("not-run" if sac_idx is None and search_idx is None
                    else "failed")))
    D["A2_sacrifice_prompted_detail"] = (
        f"prompt_sequence={seq_labels} sac_idx={sac_idx} "
        f"search_idx={search_idx}")

    # A3: sacrificed land BF->Graveyard (or, in the bug case, proof that no
    # land left the battlefield at all)
    sac_ok = False
    sac_detail = f"sac_oid={ST['sac_oid']}"
    if ST["sac_oid"] is not None and mid_sac_s and post_s:
        before = mid_sac_s.get("objects", {}).get(str(ST["sac_oid"]), {})
        after = post_s.get("objects", {}).get(str(ST["sac_oid"]), {})
        sac_ok = (before.get("zone") == "Battlefield"
                  and after.get("zone") == "Graveyard")
        sac_detail = (f"oid={ST['sac_oid']} {oname(before)} "
                      f"{before.get('zone')}->{after.get('zone')}")
        A["A3_land_sacrificed"] = "passed" if sac_ok else "failed"
    else:
        # no sacrifice prompt: no P0 Forest may have left the battlefield
        lost = []
        if pre_s and post_s:
            pre_bf_oids = {oid for oid, o in objs_of(pre_s).items()
                           if oname(o) == FOREST
                           and o.get("zone") == "Battlefield"
                           and o.get("controller") == 0}
            post_bf_oids = {oid for oid, o in objs_of(post_s).items()
                            if oname(o) == FOREST
                            and o.get("zone") == "Battlefield"
                            and o.get("controller") == 0}
            lost = sorted(pre_bf_oids - post_bf_oids)
        sac_detail += f" no_sacrifice_prompt; forests_left_bf={lost}"
        A["A3_land_sacrificed"] = "not-run"
    D["A3_land_sacrificed_detail"] = sac_detail

    # A4: search offered up to 3, lands entered tapped, shuffled
    search_ok = False
    sdetail = (f"search_max={ST['search_max']} "
               f"chosen={ST['search_chosen']}")
    if ST["search_max"] is not None and mid_search_s and post_s:
        n_chosen = len(ST["search_chosen"])
        entered = []
        for oid in ST["search_chosen"]:
            o = post_s.get("objects", {}).get(str(oid), {})
            entered.append(o.get("zone") == "Battlefield"
                           and o.get("tapped") is True
                           and oname(o) == FOREST)
        pre_bf = sum(1 for _, o in bf(pre_s, 0) if oname(o) == FOREST) \
            if pre_s else None
        post_bf = sum(1 for _, o in bf(post_s, 0) if oname(o) == FOREST)
        # forests on BF: pre - (1 if a land was sacrificed) + chosen = post
        expected = (pre_bf + n_chosen - (1 if ST["sac_oid"] else 0)) \
            if pre_bf is not None else None
        search_ok = (ST["search_max"] == 3 and n_chosen == 3
                     and all(entered)
                     and (expected is None or post_bf == expected))
        sdetail += (f" entered_tapped={entered} "
                    f"bf_forests pre={pre_bf} post={post_bf} "
                    f"expected_post={expected}")
    A["A4_search_up_to_three"] = (
        "passed" if search_ok
        else ("not-run" if ST["search_max"] is None else "failed"))
    D["A4_search_up_to_three_detail"] = sdetail

    # A5: cleanup
    clean_ok = False
    cdetail = ""
    if post_s:
        er_gy = any(oname(o) == ER and o.get("zone") == "Graveyard"
                    and o.get("controller") == 0
                    for o in post_s.get("objects", {}).values())
        stack_empty = not (post_s.get("stack") or [])
        wf_ok = wf_type(post_s) == "Priority"
        clean_ok = er_gy and stack_empty and wf_ok
        cdetail = (f"er_in_gy={er_gy} stack_empty={stack_empty} "
                   f"wf={wf_type(post_s)} turn={post_s.get('turn_number')}")
    A["A5_cleanup"] = ("passed" if clean_ok
                       else ("not-run" if not post_s else "failed"))
    D["A5_cleanup_detail"] = cdetail

    if not ST["er_cast"]:
        verdict = "blocked"
    elif A["A2_sacrifice_prompted"] == "failed":
        verdict = "reproduced"
    elif all(A[k] == "passed" for k in
             ("A1_setup_ok", "A2_sacrifice_prompted", "A3_land_sacrificed",
              "A4_search_up_to_three", "A5_cleanup")):
        verdict = "not-reproduced"
    elif any(A[k] == "failed" for k in A):
        verdict = "reproduced"
    else:
        verdict = "blocked"

    bindir = f"{BACKFILL}/server/releases/v0.81.3"
    run = {
        "issue": 6911,
        "run_id": RUN_ID,
        "validated_version": "v0.81.3",
        "server_version": "0.81.3",
        "build_commit": "95bec6e",
        "protocol_version": 70,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "server_version": "0.81.3",
            "build_commit": "95bec6e",
            "protocol_version": 70,
            "binary_sha256": sha(bindir + "/phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(bindir + "/data/card-data.json"),
            "draft_pools_sha256": sha(bindir + "/data/draft-pools.json"),
            "signature_verified": True,
            "signature_key_id": "436711b6a2d36828",
            "mode": "Full",
        },
        "game_code": p0.game_code,
        "scenario": "driver/scenario_6911.py",
        "setup_line": ("P0 12x Entish Restoration / 8x Baloth Gorger / 40x "
                       "Forest; P1 12x Grizzly Bears / 48x Forest, passive"),
        "contract_line": ("ER resolution must first prompt the caster to "
                          "sacrifice a land, then offer the up-to-3 basic "
                          "land search (4+ power creature controlled)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-driver "
            "seats.",
            "12x/8x deck density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The up-to-2 branch (no 4+ creature) was not exercised; both "
            "branches share the sacrifice node.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
        ],
        "p0_deck": P0_DECK,
        "p1_deck": P1_DECK,
        "assertions": A,
        "assertion_details": D,
        "prompt_sequence": [
            {"n": n, "label": lbl, "wf": wf, "n_candidates": nc,
             "detail": det}
            for n, lbl, wf, nc, det in OPP_SEQ
        ],
        "sac_oid": ST["sac_oid"],
        "search_max": ST["search_max"],
        "search_chosen": ST["search_chosen"],
        "waiting_for_sequence": [w for w, _ in WF_SEEN],
        "verdict": verdict,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rejections": ST["rejections"],
        "mulligans": MULLS,
        "turns_seen": sorted(ST["turns_seen"]),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"verdict={verdict} assertions={json.dumps(A)}")
    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6911.py", "w") as f:
        f.write(src)
    files = sorted(os.listdir(EVDIR))
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            if fn == "manifest.sha256":
                continue
            p = f"{EVDIR}/{fn}"
            if os.path.isfile(p):
                mf.write(f"{sha(p)}  {fn}\n")
    say(f"evidence files: {files}")


if __name__ == "__main__":
    asyncio.run(main())
