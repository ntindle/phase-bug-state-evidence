#!/usr/bin/env python3
"""Issue #6864: Zariel, archduke of Avernus ultimate doesn't allow you to
choose to untap a creature.

Oracle: "[-6]: You get an emblem with 'At the end of the first combat phase
on your turn, untap target creature you control. After this phase, there is
an additional combat phase.'"

Card-data AST (v0.80.0) collapses the whole ability into a bare
AdditionalPhase node: no emblem, no end-of-combat trigger, no target slot,
no untap.

Plan (two human seats, native engine, v0.80.0 / protocol 69):
  SETUP   - land drops; P0 casts Grizzly Bears, then Zariel, Archduke of
            Avernus ({2}{R}{R}, enters at loyalty 4).
  LOYAL1  - P0 activates +1 (ability_index 0) -> loyalty 5.
  LOYAL2  - next P0 turn, +1 again -> loyalty 6.
  ACTIVATE- export pre_activate.json; activate -6 (ability_index 2) ->
            loyalty 0, Zariel to graveyard; export post_activate.json;
            scan for an emblem object owned by P0.
  COMBAT  - P0 attacks with the Bears; watch the end of the first combat
            phase on P0's turn for the emblem trigger + TargetSelection
            prompt ("target creature you control"); answer it with the
            tapped attacker; watch for the untap and the additional combat.
  DONE    - export post.json once the combat sequence is over.

Behavioral contract:
  A1 setup_ok        pre_activate.json: Zariel + >=1 Bears on P0 battlefield
  A2 loyalty_paid    post_activate.json: Zariel in Graveyard (0 loyalty)
  A3 emblem_created  an emblem object owned by P0 exists post-activation
                     (expected FAIL: AST has no emblem)
  A4 target_prompt   a TargetSelection prompt for P0's creature appears at
                     the end of the first combat phase (expected FAIL)
  A5 untap_resolves  the chosen creature is untapped after resolution
  A6 additional_combat an extra combat phase follows the first on P0's turn
                     (may PASS: the collapsed AST keeps AdditionalPhase)

Verdict = reproduced iff A3 fails (no emblem -> the reported "no choice to
untap") or A4 fails with A3 passed; not-reproduced iff A3..A6 all pass.
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
RUN_ID = "20260911-6864b"
EVID_ISSUE = "6864"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ZARIEL = "Zariel, Archduke of Avernus"
BEAR = "Grizzly Bears"
MOUNTAIN = "Mountain"
FOREST = "Forest"
PLAINS = "Plains"
ISLAND = "Island"
SWAMP = "Swamp"
LANDS = (MOUNTAIN, FOREST, PLAINS, ISLAND, SWAMP)

ST = {"stage": "SETUP", "stop": False, "retry": False}


def reset_globals():
    global C0
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False,
               "act_once": False})
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    TGT.clear()
    TGT.update({"emblem": None})
    PHASES.clear()
    TRIGGERS.clear()
    ZARLOG.clear()
    CPSLOG.clear()
    C0 = None
MULLS = {"P0": 0, "P1": 0}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
TGT = {"emblem": None}
PHASES = []       # (turn, active_player, phase) on change
TRIGGERS = []     # observed emblem-candidate triggers on the stack
ZARLOG = []       # Zariel object snapshots
CPSLOG = []       # (turn, combat_phases_started_this_turn)


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


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def n_untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in LANDS and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def loyalty_of(o):
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    for c in o.get("counters", []) or []:
        if isinstance(c, dict) and "loyal" in str(c).lower():
            return int(c.get("count", c.get("amount", 0)))
    return None


def zariel_oid(state, pid=0):
    for oid, o in state["objects"].items():
        if (oname(o) == ZARIEL and o.get("zone") == "Battlefield"
                and o.get("controller") == pid):
            return str(oid)
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data"),
                             "stage": ST["stage"]})


def record_phase(state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if not PHASES or PHASES[-1] != key:
        PHASES.append(key)
        wire("phase", {"turn": key[0], "active": key[1], "phase": key[2],
                       "stage": ST["stage"]})


def scan_triggers(state):
    """Look for emblem-candidate triggers on the stack."""
    for oid, o in state["objects"].items():
        if o.get("zone") != "Stack":
            continue
        kind = (o.get("kind") or {})
        if not isinstance(kind, dict):
            continue
        if kind.get("type") != "TriggeredAbility":
            continue
        desc = str(kind.get("ability", {}).get("description", ""))
        if "untap" in desc.lower() and oid not in [t[0] for t in TRIGGERS]:
            TRIGGERS.append((str(oid), desc[:200]))
            wire("emblem_trigger_seen",
                 {"oid": str(oid), "description": desc[:300],
                  "stage": ST["stage"]})
            say(f"[watch] emblem-candidate trigger on stack: {desc[:160]}")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"]})
    await c.send_interaction(sub)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:260]}")
    return found


async def export_now(path):
    s = await C0.export_state()
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


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                ref = str(d["reference"])
                break
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref,
                    "name": oname(o), "zone": o.get("zone"),
                    "tapped": o.get("tapped"),
                    "controller": o.get("controller")})
    return out


async def answer_emblem_prompt(c, state, st):
    """Answer P0's end-of-combat TargetSelection for the emblem trigger:
    pick a tapped creature P0 controls (the attacker)."""
    if TGT.get("emblem"):
        return False
    vi = get_vi(st)
    if not vi:
        return False
    wf = (state.get("waiting_for") or {})
    if wf.get("type") != "TargetSelection":
        return False
    if (wf.get("data") or {}).get("player") != 0:
        return False
    if ST["stage"] != "COMBAT":
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        mine = [x for x in cands
                if x["zone"] == "Battlefield" and x["controller"] == 0]
        if not mine:
            continue
        if ("emblem_prompt", len(chs)) not in SHAPES:
            SHAPES.add(("emblem_prompt", len(chs)))
            wire("emblem_target_prompt",
                 {"rtype": rtype, "candidates": cands,
                  "opportunity": opp})
            say(f"[P0] emblem target prompt: "
                f"{[(x['name'], x['tapped']) for x in cands]}")
        want = next((x for x in mine if x["tapped"]), mine[0])
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [want["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
        else:
            say(f"[P0] emblem: unexpected prompt shape {rtype}/{spec_type}")
            continue
        await send_interaction(c, {"interactionId": iid, "response": resp_out})
        SUBMITTED.add(iid)
        TGT["emblem"] = {"ref": want["ref"], "name": want["name"],
                         "at": time.time()}
        say(f"[P0] emblem: targeted {want['name']} (oid={want['ref']})")
        return True
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            lands = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
            n_lands = sum(1 for n in lands if n in LANDS)
            keep_ok = n_lands >= 2 and (MOUNTAIN in lands or MULLS[c.name] >= 1)
            choice = "Keep" if (keep_ok or MULLS[c.name] >= 2) else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and \
                (state.get("waiting_for") or {}).get("type") == \
                "MulliganDecision":
            pending = ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    wt0 = (state.get("waiting_for") or {}).get("type")
    if wt0 == "DiscardToHandSize":
        pend = (state.get("waiting_for") or {}).get("data") or {}
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            # protect Zariel + one Bears; shed extra Zariels, then lands
            rank = {}
            seen_z = seen_b = 0
            pref = []
            for o in h:
                nm = oname(state["objects"][o])
                if nm == ZARIEL:
                    seen_z += 1
                    pref.append((0 if seen_z == 1 else 1, o))
                elif nm == BEAR:
                    seen_b += 1
                    pref.append((0 if seen_b == 1 else 2, o))
                elif nm in LANDS:
                    pref.append((3, o))
                else:
                    pref.append((2, o))
            pref.sort(key=lambda x: x[0], reverse=True)
            picks = [o for _, o in pref[:n]]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}: "
                    f"{[oname(state['objects'][o]) for o in picks]}")
                return True
    for a in acts:
        if a["type"] == "DeclareAttackers" and is_p0:
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            if ST["stage"] == "COMBAT" and not ST.get("attacked"):
                attackers = [int(oid) for oid, o in bf(state, pid)
                             if oname(o) == BEAR and not o.get("tapped")]
                nd["attacks"] = [[y, {"type": "Player", "data": 1}]
                                 for y in attackers]
                nd["bands"] = []
                ST["attacked"] = True
                say(f"P0 declares attackers: {attackers} -> P1")
                wire("declare_attackers", {"attackers": attackers})
            else:
                nd["attacks"] = []
                nd["bands"] = []
            await submit_as_is(c, na)
            return True
        if a["type"] == "DeclareBlockers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "ChooseLegend" and is_p0:
            # defensive: submit advertised as-is (keeps first)
            await submit_as_is(c, a)
            say("P0 ChooseLegend: submitted as-is")
            return True
    # answer the emblem target prompt before anything else
    if is_p0:
        if await answer_emblem_prompt(c, state, st):
            return True
    # never pass while P0 has a decision pending
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                         "ManaPayment", "ChooseXValue", "DiscardChoice",
                         "ChooseLegend") and wplayer == 0:
        return False
    # P0's plan
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await p0_setup(c, pid, state, acts):
                return True
        else:
            if await p0_land_drop(c, pid, state, acts):
                return True
            if ST["stage"] in ("LOYAL1", "LOYAL2"):
                if await p0_loyal_plus(c, pid, state, acts):
                    return True
            elif ST["stage"] == "ACTIVATE":
                if await p0_activate_ult(c, pid, state, acts):
                    return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def p0_land_drop(c, pid, state, acts):
    for name in (MOUNTAIN, FOREST, PLAINS, ISLAND, SWAMP):
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def p0_setup(c, pid, state, acts):
    await p0_land_drop(c, pid, state, acts)
    if not any(oname(o) == BEAR for _, o in bf(state, pid)):
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, FOREST) >= 1 \
                and n_untapped_lands(state, pid) >= 2:
            await submit_as_is(c, a)
            say("P0 casts Grizzly Bears")
            return True
    if not zariel_oid(state, pid):
        oid = find_hand(state, pid, ZARIEL)
        a = castspell_advertised(acts, oid)
        if a and untapped_of(state, pid, MOUNTAIN) >= 2 \
                and n_untapped_lands(state, pid) >= 4:
            await submit_as_is(c, a)
            say("P0 casts Zariel, Archduke of Avernus")
            return True
    return False


def activate_options(acts, state, pid=0):
    z = zariel_oid(state, pid)
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src == z:
            out.append((d.get("ability_index"), a))
    return z, out


async def p0_loyal_plus(c, pid, state, acts):
    if ST["act_once"]:
        return False
    z = zariel_oid(state, pid)
    if not z:
        return False
    zo = state["objects"][z]
    if ("zarobj", z) not in SHAPES:
        SHAPES.add(("zarobj", z))
        wire("zariel_object", {"oid": z, "object": zo})
        say(f"[P0] Zariel object keys: {sorted(zo.keys())}; "
            f"loyalty={loyalty_of(zo)}")
    zid, options = activate_options(acts, state, pid)
    key = ("act_opts", tuple(sorted(str(i) for i, _ in options)))
    if key not in SHAPES:
        SHAPES.add(key)
        wire("activate_options",
             {"zariel": zid, "options": [(i, json.dumps(a)[:200])
                                         for i, a in options]})
        say(f"[P0] ActivateAbility options: "
            f"{[(i, json.dumps(a)[:120]) for i, a in options]}")
    choice = next((a for i, a in options if i == 0), None)
    if not choice:
        return False
    ST["act_once"] = True
    d = dict(choice.get("data", {}))
    for k in ("source_id", "object_id"):
        if k in d:
            d[k] = int(d[k])
    sub = {"type": "ActivateAbility", "data": d}
    await submit_as_is(c, sub)
    say(f"[P0] activates Zariel +1 (ability_index 0), loyalty was "
        f"{loyalty_of(zo)}")
    wire("zariel_plus1", {"zariel": zid})
    return True


async def p0_activate_ult(c, pid, state, acts):
    if ST["act_once"]:
        return False
    z = zariel_oid(state, pid)
    if not z:
        return False
    zid, options = activate_options(acts, state, pid)
    key = ("ult_opts", tuple(sorted(str(i) for i, _ in options)))
    if key not in SHAPES:
        SHAPES.add(key)
        wire("ult_activate_options",
             {"zariel": zid, "options": [(i, json.dumps(a)[:200])
                                         for i, a in options]})
        say(f"[P0] ult ActivateAbility options: "
            f"{[(i, json.dumps(a)[:120]) for i, a in options]}")
    choice = next((a for i, a in options if i == 2), None)
    if not choice:
        say("[P0] WARNING: -6 (ability_index 2) not advertised!")
        wire("ult_not_advertised", {"options": [i for i, _ in options]})
        return False
    await export_now("pre_activate.json")
    ST["act_once"] = True
    d = dict(choice.get("data", {}))
    for k in ("source_id", "object_id"):
        if k in d:
            d[k] = int(d[k])
    sub = {"type": "ActivateAbility", "data": d}
    await submit_as_is(c, sub)
    say(f"[P0] activates Zariel -6 (ability_index 2)")
    wire("zariel_ult", {"zariel": zid})
    return True


async def p1_step(c, pid, state, acts):
    lid = None
    for name in LANDS:
        lid = find_hand(state, pid, name)
        if lid:
            break
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


def emblem_objects(state, pid=0):
    """Heuristic scan for an emblem object owned by pid."""
    out = []
    for oid, o in state["objects"].items():
        blob = json.dumps(o).lower()
        name = oname(o).lower()
        zonesub = str(o.get("zone", "")).lower()
        subtypes = str(o.get("subtypes", "")).lower()
        if (o.get("owner") == pid or o.get("controller") == pid) and (
                "emblem" in name or "emblem" in zonesub
                or "emblem" in subtypes or o.get("is_emblem")):
            out.append((str(oid), o))
        elif "emblem" in blob and o.get("zone") not in (
                "Library", "Hand", "Graveyard", "Exile", "Battlefield",
                "Stack"):
            out.append((str(oid), o))
    return out


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((ZARIEL, 8), (BEAR, 12),
                         (MOUNTAIN, 20), (FOREST, 20)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((FOREST, 15), (PLAINS, 15),
                                    (ISLAND, 15), (SWAMP, 15)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1500
    ult_turn = None
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                LAST_SUBMIT["iid"] = None
            if rej:
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        record_phase(st["state"])
        scan_triggers(st["state"])
        cps = st["state"].get("combat_phases_started_this_turn")
        if cps and (not CPSLOG or CPSLOG[-1][1] != cps):
            CPSLOG.append((st["state"].get("turn_number"), cps))
            wire("combat_phases_started",
                 {"turn": st["state"].get("turn_number"), "count": cps,
                  "stage": ST["stage"]})
        state = st["state"]

        if (state.get("waiting_for") or {}).get("type") == "GameOver" \
                and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        z = zariel_oid(state, 0)
        if z:
            lo = loyalty_of(state["objects"][z])
            if not ZARLOG or ZARLOG[-1][1] != lo:
                ZARLOG.append((state.get("turn_number"), lo))
                wire("zariel_loyalty", {"turn": state.get("turn_number"),
                                        "loyalty": lo})
                say(f"[watch] Zariel loyalty={lo} "
                    f"(turn {state.get('turn_number')})")

        # --- stage transitions
        if ST["stage"] == "SETUP":
            if z and any(oname(o) == BEAR for _, o in bf(state, 0)):
                ST["stage"] = "LOYAL1"
                ST["act_once"] = False
                say("=== stage -> LOYAL1 ===")
        elif ST["stage"] == "LOYAL1":
            if z and loyalty_of(state["objects"][z]) == 5:
                ST["stage"] = "LOYAL2"
                ST["act_once"] = False
                say("=== stage -> LOYAL2 ===")
        elif ST["stage"] == "LOYAL2":
            if z and loyalty_of(state["objects"][z]) == 6:
                ST["stage"] = "ACTIVATE"
                ST["act_once"] = False
                say("=== stage -> ACTIVATE ===")
        elif ST["stage"] == "ACTIVATE" and ST["act_once"]:
            # ult submitted; wait until Zariel leaves the battlefield
            if not z:
                ult_turn = state.get("turn_number")
                await export_now("post_activate.json")
                embs = emblem_objects(state, 0)
                wire("emblem_scan_post_ult",
                     {"found": [(oid, oname(o), o.get("zone"))
                                for oid, o in embs]})
                say(f"[watch] post-ult emblem scan: "
                    f"{[(oid, oname(o), o.get('zone')) for oid, o in embs]}")
                ST["stage"] = "COMBAT"
                ST["attacked"] = False
                say("=== stage -> COMBAT ===")
        elif ST["stage"] == "COMBAT":
            # end observation once the combat sequence is over: first
            # PostCombatMain on P0's turn at/after the ult turn
            if (ult_turn is not None
                    and state.get("active_player") == 0
                    and (state.get("phase") or "") == "PostCombatMain"
                    and (state.get("turn_number") or 0) >= ult_turn):
                await asyncio.sleep(2)
                await export_now("post.json")
                ST["stop"] = True
                say("=== DONE (post-combat main reached) ===")
            # safety: 4 P0 turns after the ult with no combat end -> stop
            if (ult_turn is not None
                    and (state.get("turn_number") or 0) >= ult_turn + 8):
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
                say("=== DONE (safety: 8 turns past ult) ===")

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"Zariel loyalty trace: {ZARLOG}")
    obs["notes"].append(f"emblem triggers seen: {TRIGGERS}")
    obs["notes"].append(f"target answer: {TGT['emblem']}")
    obs["notes"].append(f"combat phases started trace: {CPSLOG}")
    obs["phases"] = PHASES
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "phases": PHASES,
                   "triggers": TRIGGERS, "tgt": TGT,
                   "zariel_loyalty": ZARLOG,
                   "combat_phases_started": CPSLOG}, f, indent=2)
    for n in obs["notes"]:
        say(f"NOTE: {n}")

    await p0.close()
    await p1.close()
    return obs, True


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = {"assert": {}, "notes": [f"attempt {n} crash: {e!r}"]}, False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs.get("assert", {}), indent=2))
