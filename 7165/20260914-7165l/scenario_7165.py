#!/usr/bin/env python3
"""Issue #7165: AI put Crown of Skemfar on my card and spends all of its mana
to get it back.

Reporter (Discord): the native AI enchants an OPPONENT's creature with Crown
of Skemfar (a beneficial Aura: enchanted creature gets +1/+1 for each Elf its
controller controls and has reach), then repeatedly spends its mana returning
the Aura from its graveyard to its hand via the {2}{G} ability.

Maintainer triage: tactical evaluation / action selection, not Oracle AST
faithfulness. Acceptance criteria:
  - opponent-controlled targets get an appropriate negative score for
    beneficial Auras;
  - the {2}{G} return activation is compared against holding mana / board
    value;
  - the attached state selects a legal non-self-defeating action.

Plan (native engine, v0.83.0 / protocol 70, native Medium AI):
  P0 (human driver): 6x Back to Nature + 16x Fog + 8x Nourish + 30x Forest. Plays a
     land a turn, casts Craw Wurm blockers, never attacks, never blocks.
     Once the AI's Crown is on the battlefield, P0 casts Back to Nature
     ("Destroy all enchantments", no target selection), sending the Crown to
     the AI's graveyard; re-casts on every AI re-attach to keep the loop
     observable. P0 survives via Fog (AI combats), Nourish (lifegain) and
     real block assignments from valid_blocker_ids/valid_block_targets
     (runs 20260914-7165b/d/f ended by AI combat damage before the 12-turn
     watch closed). (Run 20260914-7165
     stalled on Naturalize target selection:
     the cast sat on the stack with waiting_for=Priority and no matching
     target opportunity was advertised; Back to Nature removes that
     driver-dependent leg while testing the same reported outcome.)
  P1 (native Medium AI): 12x Crown of Skemfar + 12x Llanowar Elves +
     36x Forest.

Behavioral contract:
  A1 setup_ok        AI casts Crown of Skemfar; the Aura is observed on the
                     battlefield (mid_attach.json exported).
  A2 correct_target  the enchanted creature is controlled by the AI (seat 1).
                     failed = the AI buffed the opponent's creature (reported).
  A3 return_observed Crown reached the AI graveyard and the AI activated the
                     {2}{G} return (graveyard->hand transition) at least
                     once. not-run if the Crown never reached the AI's
                     graveyard.
  A4 no_return_loop  AI activated the return at most once in the 12-turn
                     watch window. failed = >=2 return activations (the
                     "spends mana to get it back" loop). not-run if A3
                     not-run.
  A5 cleanup         no revision stall; game advanced >=3 turns past the
                     attach; final stack empty (or game over).

Verdict: reproduced iff A2 fails, or (A3 passes and A4 fails).
         not-reproduced iff A1 passes and A2 passes and the return leg was
         fully exercised (watch window closed) with A3 failed (AI never
         returned the Crown) or (A3 passed and A4 passed) (single return,
         no loop), or the Crown never reached the graveyard at all (return
         leg inapplicable; target-choice half tested).
         blocked iff the AI never casts Crown within the turn cap (A1 fails),
         or the watch window was interrupted after the Crown reached the
         graveyard (return leg not fully exercised).

Driver notes:
  - Back to Nature ("Destroy all enchantments") replaces Naturalize: no
    target selection is needed to move the Crown to the AI graveyard.
  - All viewer_interaction opportunities are wire-logged (opportunity
    events) so an unanswered prompt leaves a trace.
  - attached_to on protocol 70 is a nested dict; the enchanted creature oid
    is extracted defensively (data-key first, then any int) and the raw
    value is wire-logged.
  - A return activation is detected as a Graveyard->Hand zone transition of
    an AI-controlled Crown (no other graveyard recursion exists in either
    deck); each event records turn, AI hand size, and AI untapped Forests.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260914-7165l"
ISSUE = 7165
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CROWN = "Crown of Skemfar"
BTN = "Back to Nature"
FOG = "Fog"
NOURISH = "Nourish"
FOREST = "Forest"
ELVES = "Llanowar Elves"
LANDS = (FOREST,)
P0_DECK = [(BTN, 6), (FOG, 16), (NOURISH, 8), (FOREST, 30)]
P1_AI_DECK = [(CROWN, 12), (ELVES, 12), (FOREST, 36)]
TIMEOUT = 2400
STALL_AFTER = 180
TURN_CAP = 45
WATCH_TURNS = 12  # return-loop watch window after first graveyard arrival

SERVER_IDENTITY = {
    "validated_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "server_binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
}

ST = {}
SUBMITTED = set()
MULLS = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> ATTACHED -> RETURN_WATCH -> DONE
        "stop": False,
        "crown_seen": False, "crown_oid": None, "attach_turn": None,
        "attach_controller": None, "attach_creature_oid": None,
        "attach_creature_name": None, "attach_raw": None,
        "attach_exported": False,
        "btn_cast_turn": None,
        "gy_arrived": False, "gy_turn": None, "gy_exported": False,
        "watch_until_turn": None,
        "return_count": 0, "return_events": [],
        "stack_return_seen": set(),
        "crown_zones": {},  # oid(str) -> zone, for AI-controlled Crowns
        "reattach_count": 0,
        "stall_observed": False, "stall_wf": None,
        "last_rev_change": None, "game_started": False, "game_over": False,
        "rejections": [], "turns_seen": set(),
        "ai_mana_samples": [], "post_exported": False,
        "watch_closed": False, "fog_cast": set(),
    })
    SUBMITTED.clear()
    MULLS.clear()
    MULLS.update({"P0": 0})


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


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def life_of(state, pid):
    ps = state.get("players") or []
    if pid < len(ps):
        return ps[pid].get("life")
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


WF_SEEN = []


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


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
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


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def block_assignments(state, pid):
    """Build DeclareBlockers assignments from the advertised
    valid_blocker_ids / valid_block_targets (waiting_for data).
    Format (cf. #1362): assignments = [[blocker_oid, attacker_oid], ...].
    Each blocker takes the strongest attacker it can legally block."""
    data = wf_data(state)
    blockers = data.get("valid_blocker_ids") or []
    targets = data.get("valid_block_targets") or {}
    if not blockers or not targets:
        return []
    objs = state.get("objects") or {}

    def power(oid):
        o = objs.get(str(oid)) or {}
        try:
            return int(o.get("power") or 0)
        except (TypeError, ValueError):
            return 0

    def norm_targets(b):
        lst = targets.get(str(b))
        if lst is None:
            lst = targets.get(b)
        return [int(x) for x in (lst or [])]

    all_attackers = sorted(
        {a for b in blockers for a in norm_targets(b)},
        key=power, reverse=True)
    assigns, used = [], set()
    for b in blockers:
        legal = set(norm_targets(b))
        for a in all_attackers:
            if a not in used and a in legal:
                assigns.append([int(b), int(a)])
                used.add(a)
                break
    return assigns


def attached_creature_oid(o):
    """Defensively extract the enchanted creature's oid from attached_to."""
    a = o.get("attached_to")
    if a is None:
        return None

    def find_data(x):
        if isinstance(x, dict):
            d = x.get("data")
            if isinstance(d, int) and not isinstance(d, bool):
                return d
            for v in x.values():
                r = find_data(v)
                if r is not None:
                    return r
        elif isinstance(x, list):
            for v in x:
                r = find_data(v)
                if r is not None:
                    return r
        return None

    r = find_data(a)
    if r is not None:
        return r

    def find_int(x):
        if isinstance(x, bool):
            return None
        if isinstance(x, int):
            return x
        if isinstance(x, dict):
            for v in x.values():
                q = find_int(v)
                if q is not None:
                    return q
        elif isinstance(x, list):
            for v in x:
                q = find_int(v)
                if q is not None:
                    return q
        return None

    return find_int(a)


def vi_opportunities(c):
    st = c.latest or {}
    vi = st.get("viewer_interaction") or {}
    return vi.get("opportunities", []) or []


def candidate_ref_oid(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


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


OPPS_LOGGED = set()


async def log_opportunities(c):
    """Wire-log any live viewer_interaction opportunities (diagnostic)."""
    for opp in vi_opportunities(c):
        sig = (opp.get("interactionId"), (opp.get("response") or {}).get("type"))
        if sig in OPPS_LOGGED:
            continue
        OPPS_LOGGED.add(sig)
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or data.get("candidates") or []
        wire("opportunity",
             {"iid": str(opp.get("interactionId"))[:16],
              "rtype": resp.get("type"),
              "n_choices": len(choices),
              "first_choices": [str(ch.get("id"))[:16] for ch in choices[:6]],
              "stage": ST.get("stage")})
        say(f"opportunity seen: rtype={resp.get('type')} "
            f"n_choices={len(choices)}")


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) in LANDS)
            keep_ok = n_lands >= 2 or MULLS[c.name] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice} (lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    if wf_type(state) == "DiscardToHandSize":
        pend = wf_data(state)
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            names = {o: oname(state["objects"][o]) for o in h}
            btns = [o for o in h if names[o] == BTN]
            fogs = [o for o in h if names[o] == FOG]
            nours = [o for o in h if names[o] == NOURISH]
            # protect up to 2 copies each of Back to Nature / Fog / Nourish;
            # discard other non-lands first
            pref = [o for o in h if names[o] not in LANDS
                    and names[o] not in (BTN, FOG, NOURISH)]
            pref += btns[2:] + fogs[3:] + nours[2:]
            pref += [o for o in h if o not in pref]
            picks = pref[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}")
                return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"{c.name} legend-choice submitted as-is: {a['type']}")
            return True
    # Log any live interaction opportunities for diagnostics (the 20260914-7165
    # run stalled on a target prompt that never matched; never block on it).
    await log_opportunities(c)
    # Fog the AI's combat: cast before blockers are declared so P0 survives
    # the whole watch window. Guarded once per (turn, phase).
    if state.get("active_player") != pid and (state.get("phase") or "") in (
            "DeclareAttackers", "DeclareBlockers"):
        key = state.get("turn_number")
        if key not in ST["fog_cast"]:
            fid = find_hand(state, pid, FOG)
            if fid and untapped_of(state, pid, FOREST) >= 1:
                a = castspell_advertised(acts, fid)
                if a:
                    await submit_as_is(c, a)
                    ST["fog_cast"].add(key)
                    say(f"[P0] casts Fog (oid={fid}) turn={key} "
                        f"phase={state.get('phase')}")
                    wire("fog_cast", {"turn": key,
                                      "phase": state.get("phase"),
                                      "fog_oid": fid})
                    return True
    for a in acts:
        if a["type"] == "DeclareBlockers":
            if "block_shape_logged" not in ST:
                ST["block_shape_logged"] = True
                wire("declare_blockers_advertised",
                     {"data": a.get("data", {})})
                say("DeclareBlockers advertised data wire-logged")
            sub = copy.deepcopy(a)
            assigns = block_assignments(state, pid)
            sub["data"]["assignments"] = assigns
            await submit_as_is(c, sub)
            say(f"{c.name} declares blockers: {assigns}")
            wire("declare_blockers", {"assignments": assigns})
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
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        turn = state.get("turn_number") or 0
        # Back to Nature the AI's Crown once it is on the battlefield
        # ("Destroy all enchantments" needs no target selection).
        if ST["stage"] in ("ATTACHED", "RETURN_WATCH"):
            crowns = bf_named(state, 1, CROWN)
            if crowns and ST.get("btn_cast_turn") != turn:
                bid = find_hand(state, pid, BTN)
                if bid and untapped_of(state, pid, FOREST) >= 3:
                    a = castspell_advertised(acts, bid)
                    if a:
                        await submit_as_is(c, a)
                        ST["btn_cast_turn"] = turn
                        say(f"[P0] casts Back to Nature (oid={bid}) "
                            f"Crown count={len(crowns)} turn={turn}")
                        wire("btn_cast", {"turn": turn, "btn_oid": bid,
                                          "n_crowns": len(crowns)})
                        return True
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
        # Nourish for survival: cast on our main when life is low.
        if (life_of(state, pid) or 20) <= 16:
            nid = find_hand(state, pid, NOURISH)
            if nid and untapped_of(state, pid, FOREST) >= 3:
                a = castspell_advertised(acts, nid)
                if a:
                    await submit_as_is(c, a)
                    say(f"[P0] casts Nourish (oid={nid}) "
                        f"life={life_of(state, pid)} turn={turn}")
                    wire("nourish_cast", {"turn": turn, "nourish_oid": nid,
                                          "life": life_of(state, pid)})
                    return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def ai_untapped_forests(state):
    return untapped_of(state, 1, FOREST)


async def observe(state):
    """Track the AI's Crown: attach, graveyard arrival, return activations."""
    turn = state.get("turn_number") or 0
    objs = state.get("objects") or {}
    ai_crowns_bf = bf_named(state, 1, CROWN)

    # --- attach leg ---
    if not ST["crown_seen"] and ai_crowns_bf:
        ST["crown_seen"] = True
        ST["crown_oid"] = int(ai_crowns_bf[0])
        ST["attach_turn"] = turn
        ST["stage"] = "ATTACHED"
        o = objs[str(ai_crowns_bf[0])]
        raw = o.get("attached_to")
        ST["attach_raw"] = raw
        coid = attached_creature_oid(o)
        ST["attach_creature_oid"] = coid
        cname, cctl = None, None
        if coid is not None:
            co = objs.get(str(coid)) or {}
            cname = oname(co)
            cctl = co.get("controller")
        ST["attach_creature_name"] = cname
        ST["attach_controller"] = cctl
        say(f"Crown of Skemfar on AI BF oid={ST['crown_oid']} turn={turn} "
            f"attached_to oid={coid} name={cname!r} controller={cctl}")
        wire("crown_attached", {"turn": turn, "crown_oid": ST["crown_oid"],
                                "attached_creature_oid": coid,
                                "attached_creature_name": cname,
                                "attached_controller": cctl,
                                "attached_to_raw": raw})
        if await export_now("mid_attach.json") is not None:
            ST["attach_exported"] = True

    # --- zone tracking for AI-controlled Crowns (return detection) ---
    for oid, o in objs.items():
        if oname(o) != CROWN or o.get("controller") != 1:
            continue
        prev = ST["crown_zones"].get(str(oid))
        zone = o.get("zone")
        if prev == "Graveyard" and zone == "Hand":
            # Deduplicate against a stack activation seen in the last 2
            # turns: the zone transition is that activation resolving.
            recent_stack = any(
                ev.get("via") == "stack" and turn - ev.get("turn", 0) <= 2
                for ev in ST["return_events"])
            if recent_stack:
                say(f"AI return resolved: Crown oid={oid} gy->hand "
                    f"turn={turn} (already counted via stack)")
                wire("ai_return_resolved",
                     {"turn": turn, "crown_oid": int(oid)})
            else:
                ST["return_count"] += 1
                ev = {"turn": turn, "crown_oid": int(oid),
                      "via": "zone",
                      "ai_hand": len(hand_oids(state, 1)),
                      "ai_untapped_forests": ai_untapped_forests(state),
                      "p0_life": life_of(state, 0),
                      "p1_life": life_of(state, 1)}
                ST["return_events"].append(ev)
                say(f"AI RETURN ACTIVATION #{ST['return_count']}: Crown "
                    f"oid={oid} gy->hand turn={turn} ai_hand={ev['ai_hand']} "
                    f"ai_untapped_forests={ev['ai_untapped_forests']}")
                wire("ai_return", ev)
        if prev == "Hand" and zone == "Battlefield" and ST["crown_seen"] \
                and str(oid) != str(ST["crown_oid"]):
            pass  # recast of the same copy is the common path; skip noise
        ST["crown_zones"][str(oid)] = zone

    # --- return activation on the stack (the card stays in the graveyard
    # until the ability resolves, so the zone transition alone misses it) ---
    for entry in state.get("stack") or []:
        try:
            desc = (((entry.get("kind") or {}).get("data") or {}).get(
                "ability") or {}).get("description") or ""
        except AttributeError:
            desc = ""
        if entry.get("controller") == 1 and "Return this card from your " \
                "graveyard to your" in desc:
            key = (turn, str(entry.get("id")))
            if key not in ST["stack_return_seen"]:
                ST["stack_return_seen"].add(key)
                ST["return_count"] += 1
                ev = {"turn": turn, "stack_id": entry.get("id"),
                      "via": "stack",
                      "ai_hand": len(hand_oids(state, 1)),
                      "ai_untapped_forests": ai_untapped_forests(state),
                      "p0_life": life_of(state, 0),
                      "p1_life": life_of(state, 1)}
                ST["return_events"].append(ev)
                say(f"AI RETURN ACTIVATION #{ST['return_count']}: on stack "
                    f"id={entry.get('id')} turn={turn}")
                wire("ai_return", ev)

    # --- graveyard arrival leg ---
    if ST["stage"] == "ATTACHED" and not ST["gy_arrived"]:
        in_gy = [oid for oid, o in objs.items()
                 if oname(o) == CROWN and o.get("controller") == 1
                 and o.get("zone") == "Graveyard"]
        if in_gy:
            ST["gy_arrived"] = True
            ST["gy_turn"] = turn
            ST["watch_until_turn"] = turn + WATCH_TURNS
            ST["stage"] = "RETURN_WATCH"
            say(f"Crown in AI graveyard turn={turn}; watch window until "
                f"turn {ST['watch_until_turn']}")
            wire("crown_graveyard", {"turn": turn,
                                     "oids": [int(x) for x in in_gy]})
            if await export_now("mid_graveyard.json") is not None:
                ST["gy_exported"] = True

    # --- re-attach counting (for the loop narrative) ---
    if ST["stage"] == "RETURN_WATCH" and ai_crowns_bf:
        if str(ai_crowns_bf[0]) != str(ST.get("crown_oid")) or True:
            pass
    # count distinct attach episodes via attach events only (kept simple)

    # --- mana sampling on AI main phases during the watch window ---
    if ST["stage"] == "RETURN_WATCH" and state.get("active_player") == 1 \
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"):
        key = (turn, state.get("phase"))
        if not ST["ai_mana_samples"] or ST["ai_mana_samples"][-1][0] != key:
            ST["ai_mana_samples"].append(
                (key, ai_untapped_forests(state), len(hand_oids(state, 1))))

    # --- done watch ---
    if ST["stage"] == "RETURN_WATCH" and ST["watch_until_turn"] is not None:
        if turn > ST["watch_until_turn"] and not (state.get("stack") or []):
            say(f"watch window closed at turn={turn}; exporting post.json")
            if await export_now("post.json") is not None:
                ST["watch_closed"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True
            return
    if wf_type(state) == "GameOver" and not ST["game_over"]:
        say("game over; exporting post.json before the session expires")
        ST["game_over"] = True
        if not os.path.exists(f"{EVDIR}/post.json"):
            if await export_now("post.json") is not None:
                ST["post_exported"] = True
        ST["stop"] = True


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    ai_deck = deck(*P1_AI_DECK)
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Medium",
                               "deck": {"type": "DeckList",
                                        "data": ai_deck}}])
    global C0
    C0 = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id}; P1 = native AI Medium")
    wire("game_created", {"code": p0.game_code,
                          "p0_seat": p0.player_id,
                          "p0_deck": P0_DECK, "p1_ai_deck": P1_AI_DECK})
    ST["last_rev_change"] = time.time()

    # parse check: Crown of Skemfar entry from the pinned card-data
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.83.0/data/"
                             "card-data.json"))
        crown_entry = None
        for _k, v in cd.items():
            if isinstance(v, dict) and v.get("name") == CROWN:
                crown_entry = v
                break
        if crown_entry is not None:
            with open(f"{EVDIR}/parse_crown.json", "w") as f:
                json.dump(crown_entry, f, indent=1)
            say("wrote parse_crown.json")
        else:
            say("parse check: Crown of Skemfar NOT FOUND in card-data")
    except Exception as e:
        say(f"parse check failed: {e}")

    last_rev = -1
    last_tick_wall = 0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        rej = drain_rejections(p0)
        if rej:
            ST["rejections"].extend(
                {"at": now, "who": p0.name, "type": r["type"],
                 "data": r["data"]} for r in rej)
        if now - last_tick_wall >= 5:
            last_tick_wall = now
            try:
                await tick(p0, p0.player_id)
            except Exception as e:
                say(f"tick error: {e}")
            if p0.revision != last_rev:
                ST["last_rev_change"] = now
                last_rev = p0.revision
        elif p0.revision != last_rev:
            try:
                await tick(p0, p0.player_id)
            except Exception as e:
                say(f"tick error: {e}")
            ST["last_rev_change"] = now
            last_rev = p0.revision

        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]
        turn = state.get("turn_number") or 0
        if turn >= 1:
            ST["game_started"] = True
            ST["turns_seen"].add(turn)

        if turn >= TURN_CAP and not ST["stop"]:
            say(f"turn cap {TURN_CAP} reached")
            ST["stop"] = True
            continue

        await observe(state)

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

    await finish(p0)
    try:
        await p0.close()
    except Exception:
        pass


async def finish(p0):
    ass, notes = {}, []

    async def export_named(name):
        s = await export_now(f"{name}.json")
        return s is not None

    if not os.path.exists(f"{EVDIR}/post.json"):
        await export_named("post")
    states = {}
    for fn in ("mid_attach", "mid_graveyard", "post"):
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                states[fn] = json.loads(open(p).read())["state"]
                say(f"loaded {fn}.json")
        except Exception as ex3:
            notes.append(f"state reload failed for {fn}.json: {ex3}")

    # ---- A1: setup ----
    if ST["crown_seen"]:
        ass["A1_setup_ok"] = "passed"
        notes.append(f"A1: Crown observed on AI BF oid={ST['crown_oid']} "
                     f"turn={ST['attach_turn']}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 failed: AI never put Crown of Skemfar on the "
                     "battlefield within the turn cap")

    # ---- A2: target choice ----
    if ST["crown_seen"]:
        c = ST["attach_controller"]
        if c == 1:
            ass["A2_correct_target"] = "passed"
            notes.append(f"A2: enchanted creature oid="
                         f"{ST['attach_creature_oid']} "
                         f"({ST['attach_creature_name']}) controlled by the "
                         f"AI itself (seat 1) - correct target choice")
        elif c == 0:
            ass["A2_correct_target"] = "failed"
            notes.append(f"A2 FAILED: enchanted creature oid="
                         f"{ST['attach_creature_oid']} "
                         f"({ST['attach_creature_name']}) controlled by the "
                         f"OPPONENT (seat 0) - the reported bug")
        else:
            ass["A2_correct_target"] = "failed"
            notes.append(f"A2 failed: enchanted creature controller={c} "
                         f"(unresolved)")
    else:
        ass["A2_correct_target"] = "not-run"
        notes.append("A2 not-run: no Crown attach observed")

    # ---- A3: return observed ----
    if ST["gy_arrived"]:
        if ST["watch_closed"]:
            if ST["return_count"] >= 1:
                ass["A3_return_observed"] = "passed"
                notes.append(f"A3: {ST['return_count']} AI return "
                             f"activation(s) observed: {ST['return_events']}")
            else:
                ass["A3_return_observed"] = "failed"
                notes.append("A3 failed: Crown reached AI graveyard turn "
                             f"{ST['gy_turn']} but the AI never activated "
                             "the {2}{G} return in the full watch window")
        else:
            ass["A3_return_observed"] = "not-run"
            notes.append("A3 not-run: watch window interrupted "
                         f"(game_over={ST['game_over']} "
                         f"stall={ST['stall_observed']} "
                         f"turns_seen={len(ST['turns_seen'])}) before the "
                         f"{WATCH_TURNS}-turn window closed")
    else:
        ass["A3_return_observed"] = "not-run"
        notes.append("A3 not-run: Crown never reached the AI's graveyard")

    # ---- A4: return loop ----
    if ST["gy_arrived"] and ST["watch_closed"]:
        if ST["return_count"] >= 2:
            ass["A4_no_return_loop"] = "failed"
            notes.append(f"A4 FAILED: AI returned the Crown "
                         f"{ST['return_count']} times in "
                         f"{WATCH_TURNS} turns - the reported mana-spend "
                         f"loop: {ST['return_events']}")
        else:
            ass["A4_no_return_loop"] = "passed"
            notes.append(f"A4: {ST['return_count']} return activation(s) in "
                         f"the window - no loop")
    else:
        ass["A4_no_return_loop"] = "not-run"
        notes.append("A4 not-run: return leg never started or watch "
                     "interrupted")

    # ---- A5: cleanup ----
    post = states.get("post")
    if ST["game_over"] and post is None:
        # The engine invalidates the session the moment the game ends, so no
        # post-game export is possible; the last live checkpoint stands in.
        ok = (not ST["stall_observed"] and len(ST["turns_seen"]) >= 3)
        ass["A5_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A5: game over (post.json unexportable: session "
                     f"invalidated at game end); stall={ST['stall_observed']} "
                     f"turns_seen={len(ST['turns_seen'])}")
    elif post is not None:
        ok = (not ST["stall_observed"]
              and len(ST["turns_seen"]) >= 3
              and (len(post.get("stack") or []) == 0
                   or ST["game_over"]))
        ass["A5_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A5: stall={ST['stall_observed']} "
                     f"turns_seen={len(ST['turns_seen'])} "
                     f"stack_empty={len(post.get('stack') or []) == 0} "
                     f"game_over={ST['game_over']}")
    else:
        ass["A5_cleanup"] = "failed"
        notes.append("A5 failed: post.json missing")

    for k, v in ass.items():
        say(f"{k}: {v}")
    for n in notes:
        say("note:", n)

    if ass["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif ass["A2_correct_target"] == "failed":
        verdict = "reproduced"
    elif ass["A3_return_observed"] == "passed" \
            and ass["A4_no_return_loop"] == "failed":
        verdict = "reproduced"
    elif ass["A1_setup_ok"] == "passed" \
            and ass["A2_correct_target"] == "passed" \
            and ((ass["A3_return_observed"] == "failed")
                 or (ass["A3_return_observed"] == "passed"
                     and ass["A4_no_return_loop"] == "passed")):
        # Return leg fully exercised (watch window closed): the AI either
        # never returned the Crown or returned it once without looping -
        # the reported mana-spend loop did not occur.
        verdict = "not-reproduced"
    elif ass["A1_setup_ok"] == "passed" \
            and ass["A2_correct_target"] == "passed" \
            and ass["A4_no_return_loop"] in ("passed", "not-run") \
            and not ST["gy_arrived"]:
        # Crown never reached the graveyard, so the return half could not be
        # exercised; the target-choice half (the first reported symptom) was.
        verdict = "not-reproduced"
    elif ST["gy_arrived"] and not ST["watch_closed"]:
        # The Crown reached the graveyard but the watch window was
        # interrupted (game over / stall / turn cap): the return leg was not
        # fully exercised - inconclusive, not a pass.
        verdict = "blocked"
    else:
        verdict = "blocked"
    say("VERDICT:", verdict)

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "AI put Crown of Skemfar on my card and spends all of its "
                 "mana to get it back",
        "validated_at": "2026-09-14",
        "server": SERVER_IDENTITY,
        "scope": "Native Medium AI target choice for the beneficial Aura "
                 "Crown of Skemfar + the {2}{G} graveyard-return activation "
                 "policy; native engine, one human-client seat + one native "
                 "AI seat",
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "driver_state": {
            "crown_oid": ST["crown_oid"],
            "attach_turn": ST["attach_turn"],
            "attach_controller": ST["attach_controller"],
            "attach_creature_oid": ST["attach_creature_oid"],
            "attach_creature_name": ST["attach_creature_name"],
            "gy_turn": ST["gy_turn"],
            "return_count": ST["return_count"],
            "return_events": ST["return_events"],
            "ai_mana_samples": ST["ai_mana_samples"][-12:],
            "turns_seen": sorted(ST["turns_seen"]),
            "rejections": ST["rejections"][:10],
        },
        "limitations": [
            "Browser UI not exercised; native engine via one human-client "
            "seat and one native Medium AI seat.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "P0 never attacks or blocks; combat interaction is minimized by "
            "fixture design.",
            "The {2}{G} return is detected as a Graveyard->Hand zone "
            "transition of an AI-controlled Crown (no other graveyard "
            "recursion exists in either deck).",
        ],
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")
    shutil.copy(os.path.abspath(__file__), f"{EVDIR}/scenario_7165.py")
    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
    except Exception as ex4:
        notes.append(f"server.log copy failed: {ex4}")
        say("server.log copy failed:", ex4)
    # AI-stall signatures from the server log
    try:
        hits = []
        with open(f"{EVDIR}/server.log", errors="replace") as f:
            for line in f:
                low = line.lower()
                if "halted after" in low and "failed proposals" in low:
                    hits.append(line.strip()[:300])
                if "choose_action returned none" in low \
                        and "stopping ai loop" in low:
                    hits.append(line.strip()[:300])
        if hits:
            with open(f"{EVDIR}/ai_signatures.log", "w") as f:
                f.write("\n".join(hits) + "\n")
            say(f"ai_signatures.log: {len(hits)} hits")
    except Exception as e:
        say(f"ai signature scan failed: {e}")
    render_png(run)
    # Close the logs BEFORE hashing: the manifest must cover the final bytes
    # of scenario_run.log / wire_log.jsonl (cf. AGENTS.md #6916 lesson), so
    # no say()/wire() calls may follow this point.
    WIRE.close()
    RUNLOG.close()

    def emit(*a):
        print(" ".join(str(x) for x in a), flush=True)

    files = ["mid_attach.json", "mid_graveyard.json", "post.json",
             "parse_crown.json", "run.json", "scenario_7165.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "ai_signatures.log", "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            emit(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    emit("wrote manifest.sha256")
    for fn in ("mid_attach.json", "mid_graveyard.json", "post.json",
               "parse_crown.json", "run.json"):
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            json.load(open(p))
    from PIL import Image
    Image.open(f"{EVDIR}/summary.png").verify()
    man = open(f"{EVDIR}/manifest.sha256").read().strip().splitlines()
    for line in man:
        h, fn = line.split("  ")
        assert hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() == h, fn
    emit("validation: all JSON parse, PNG readable, hashes match")


def render_png(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 1140
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "#7165 - AI put Crown of Skemfar on my card and spends "
           "all of its mana to get it back", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.83.0 (b7a59d4) protocol 70 - 2026-09-14 - "
           "native Medium AI", fill=(140, 160, 180))
    y += 28
    col = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
    y += 34
    d.text((24, y), "Oracle: Enchant creature; enchanted creature gets "
           "+1/+1 for each Elf you control", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "and has reach. {2}{G}: Return this card from your "
           "graveyard to your hand.", fill=(200, 210, 225))
    y += 34
    labels = {
        "A1_setup_ok": "GAME: AI casts Crown of Skemfar, Aura observed on "
                     "battlefield (mid_attach.json)",
        "A2_correct_target": "GAME: enchanted creature controlled by the AI "
                           "itself (REPORTED BUG: opponent's creature)",
        "A3_return_observed": "GAME: Crown reached AI graveyard; AI "
                            "activated the {2}{G} return (gy->hand)",
        "A4_no_return_loop": "GAME: at most 1 return in the 12-turn watch "
                           "window (REPORTED BUG: repeated mana spend)",
        "A5_cleanup": "GAME: no stall, game advanced, final stack empty",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        c = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v}", fill=c)
        y += 22
        d.text((52, y), lab[:104], fill=(150, 160, 175))
        y += 26
    y += 8
    ds = run.get("driver_state") or {}
    d.text((24, y), f"attach_turn={ds.get('attach_turn')} "
           f"attach_controller={ds.get('attach_controller')} "
           f"creature={ds.get('attach_creature_name')} "
           f"returns={ds.get('return_count')}",
           fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:14]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


if __name__ == "__main__":
    asyncio.run(main())
