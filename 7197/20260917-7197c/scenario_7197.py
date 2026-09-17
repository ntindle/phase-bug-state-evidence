#!/usr/bin/env python3
"""Issue #7197: Moraug, Fury of Akoum — "Shows it triggers but doesn't
actually give additional combat steps."

Oracle (pinned card-data v0.86.0): "Each creature you control gets +1/+0 for
each time it has attacked this turn. Landfall — Whenever a land you control
enters, if it's your main phase, there's an additional combat phase after
this phase. At the beginning of that combat, untap all creatures you
control."

Reported: the landfall trigger shows (fires) but no additional combat
phase is granted.

Behavioral contract (single game), pinned v0.86.0 / protocol 72:
  Setup: P0: 4x Moraug, Fury of Akoum + 56x Mountain. P1: 60x Island,
         draw-go, no blockers, no removal.
  A1 setup_ok    Moraug on P0's battlefield pre-landfall; life 20/20 at
                 the pre_landfall checkpoint.
  A2 trigger_fired  Moraug's landfall TriggeredAbility (source_id ==
                 Moraug's battlefield oid, desc mentions combat) observed
                 live on the stack during the landfall turn.
  A3 additional_combat  The landfall turn grants a USABLE additional combat:
                 >= 2 DeclareAttackers phases (one per combat) before
                 PostCombatMain. The reported defect is an extra combat
                 shell (BeginCombat -> EndCombat) with no combat steps, so
                 no additional attack can be declared.
  A4 cleanup     the landfall turn completes and the game proceeds to the
                 next turn with an empty stack.

Verdict = reproduced iff A1 passed and A3 failed (setup valid, trigger
may or may not fire, but no additional combat). not-reproduced iff
A1-A4 all pass. blocked otherwise.

Protocol-72 driver conventions (per AGENTS.md): CreateGameWithSettings +
JoinGameWithPassword + start_when_full, merged_actions (legal_actions +
legal_actions_by_object), per-seat tick with revision-change-or-5s re-tick,
default PassPriority gated on my_priority, MulliganDecision Keep via
advertised action as-is, engine auto-taps mana for casts, DiscardToHandSize
answered via viewer_interaction select (gated on named player).
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
RUN_ID = "20260917-7197c"
EVDIR = f"{BACKFILL}/evidence/7197/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MORAUG = "Moraug, Fury of Akoum"
LAND = "Mountain"

GAME_TIMEOUT = 900
PHASE_TIMEOUT = 300


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def lname(state, oid):
    o = state["objects"].get(str(oid)) or {}
    return o.get("base_name") or o.get("name")


def is_moraug(state, oid):
    n = (lname(state, oid) or "").lower()
    return "moraug" in n


def player_of(state, pid):
    return state["players"][pid]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def bf_objs(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_hand_moraug(state, pid):
    for oid in hand_ids(state, pid):
        if is_moraug(state, oid):
            return oid
    return None


def find_bf_moraug(state, pid):
    for o in bf_objs(state, pid):
        if is_moraug(state, o.get("id")):
            return o
    return None


def untapped_mountains(state, pid):
    return sum(1 for o in bf_objs(state, pid)
               if (o.get("base_name") or o.get("name")) == LAND
               and not o.get("tapped"))


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


def wf_type(state):
    return wf_of(state).get("type")


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def stack_snapshot(state):
    return [{"id": e.get("id"),
             "kind": (e.get("kind") or {}).get("type"),
             "source_id": e.get("source_id"),
             "controller": e.get("controller"),
             "desc": ((e.get("kind") or {}).get("description")
                      or "")[:160]}
            for e in (state.get("stack") or [])]


def is_landfall_trigger(entry, moraug_oid):
    if (entry.get("kind") or "") != "TriggeredAbility":
        return False
    if moraug_oid is not None and entry.get("source_id") == moraug_oid:
        return True
    d = (entry.get("desc") or "").lower()
    return "additional combat" in d or "combat phase after" in d


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if a.get("data") not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


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
                say(f"{c.name} keeps opening hand")
                return True
    say(f"{c.name}: no mulligan decision seen in 60s")
    return False


SUBMITTED = set()


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def find_select_opportunity(st):
    """Schema select/sequence opportunity whose candidates carry refs
    (used for DiscardToHandSize)."""
    vi = get_vi(st)
    if not vi:
        return None
    for op in vi.get("opportunities", []):
        if op.get("interactionId") in SUBMITTED:
            continue
        resp = op.get("response", {})
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if spec_type not in ("sequence", "select"):
            continue
        chs = data.get("choices") or data.get("candidates") or []
        if any(cand_ref(ch) is not None for ch in chs):
            return op.get("interactionId"), spec_type, chs
    return None


class Ctx:
    def __init__(self):
        self.p0_id = None
        self.moraug_oid = None        # Moraug's battlefield oid
        self.moraug_cast_turn = None
        self.cast_attempt_turns = set()
        self.landfall_turn = None     # P0 turn when landfall trigger fired
        self.landfall_done = False    # land played this turn (landfall turn)
        self.trigger_seen = False
        self.mid_trigger_exported = False
        self.land_turns = {0: set(), 1: set()}
        self.wf_seen = []
        self.rejections = []
        self.game_code = None
        self.logged_wait = set()
        self.stack_history = []
        self.phase_log = []           # (turn, active_player, phase)
        self._last_phase_key = None
        self.trigger_observations = 0


def record_wf(ctx, state):
    wt = wf_type(state)
    if wt and (not ctx.wf_seen or ctx.wf_seen[-1] != wt):
        ctx.wf_seen.append(wt)
        wire("waiting_for", {"type": wt, "data": wf_of(state).get("data")})


def record_phase(ctx, state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if key != ctx._last_phase_key:
        ctx._last_phase_key = key
        ctx.phase_log.append(list(key))
        wire("phase", {"turn": key[0], "active": key[1], "phase": key[2]})


async def answer_discard(c, pid, ctx):
    """Protocol-72 DiscardToHandSize: discard spare Mountains, keep Moraug."""
    st = c.latest
    if not st:
        return False
    state = st.get("state") or {}
    if wf_type(state) != "DiscardToHandSize":
        return False
    if wf_player(state) != pid:
        return False
    count = (wf_of(state).get("data") or {}).get("count", 1)
    found = find_select_opportunity(st)
    hand = hand_ids(state, pid)
    if found:
        iid, spec_type, chs = found
        ranked = sorted(hand,
                        key=lambda oid: 0 if not is_moraug(state, oid) else 1)
        pick = ranked[:count]
        ref_to_id = {str(cand_ref(ch)): ch["id"] for ch in chs
                     if cand_ref(ch) is not None}
        choice_ids = [ref_to_id[str(o)] for o in pick
                      if str(o) in ref_to_id]
        if choice_ids:
            sub = {"interactionId": iid,
                   "response": {"type": spec_type,
                                "data": {"choiceIds": choice_ids}}}
            say(f"{c.name} discards {choice_ids} "
                f"(DiscardToHandSize count={count})")
            wire("discard_submit", sub)
            await c.send_interaction(sub)
            SUBMITTED.add(iid)
            return True
    # fallback: SelectCards legal action with cardIds
    for a in merged_actions(st):
        if a["type"] == "SelectCards":
            data = dict(a.get("data") or {})
            ranked = sorted(hand,
                            key=lambda o: 0 if not is_moraug(state, o) else 1)
            data["cardIds"] = ranked[:count]
            say(f"{c.name} discards via SelectCards fallback")
            await c.send_action({"type": "SelectCards", "data": data})
            return True
    return False


async def tick(c, pid, ctx):
    """One decision tick for a seat. Returns True if it acted."""
    st = c.latest
    if not st:
        return False
    state = st.get("state") or {}
    acts = merged_actions(st)
    if not acts:
        return False
    wtype = wf_type(state)
    my_priority = (wtype == "Priority"
                   and state.get("priority_player") == pid)

    # 1. mana payment prompts: answer as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True

    # 2. DiscardToHandSize (cleanup) for the named player
    if wtype == "DiscardToHandSize" and wf_player(state) == pid:
        if await answer_discard(c, pid, ctx):
            return True
        return False

    # 3. other non-priority waits naming this seat: do not pass blindly
    # (combat declarations have their own branches below)
    if (wtype not in (None, "Priority", "DeclareAttackers",
                      "DeclareBlockers")
            and wf_player(state) == pid):
        key = (c.name, c.revision, wtype)
        if key not in ctx.logged_wait:
            ctx.logged_wait.add(key)
            say(f"{c.name} waiting_for={wtype} names seat {pid}; "
                f"acts={[a['type'] for a in acts]}; not passing")
            wire("unhandled_wait", {"who": c.name, "wtype": wtype,
                                    "acts": [a["type"] for a in acts],
                                    "data": wf_of(state).get("data")})
        return False

    # 4. P0 main-phase development
    if (pid == ctx.p0_id and my_priority
            and state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
        turn = state.get("turn_number")
        moraug = find_bf_moraug(state, pid)
        if moraug:
            ctx.moraug_oid = moraug["id"]
        # land drop. Play exactly ONE landfall land total: the first land
        # played in PreCombatMain while Moraug is out sets landfall_turn.
        # Later turns play no lands at all, so the phase log keeps a
        # single unambiguous landfall event.
        if ctx.landfall_turn is not None:
            land_ok = False
        else:
            land_ok = True
        if turn not in ctx.land_turns[pid] and land_ok:
            pl = next((a for a in acts
                       if a["type"] == "PlayLand"
                       and (lname(state, (a.get("data") or {})
                                  .get("object_id")) or "") == LAND), None)
            if pl:
                await submit_as_is(c, pl)
                ctx.land_turns[pid].add(turn)
                if (moraug is not None
                        and state.get("phase") == "PreCombatMain"):
                    ctx.landfall_turn = turn
                    ctx.landfall_done = True
                    say(f"{c.name} LANDFALL: plays {LAND} with Moraug out "
                        f"(turn {turn})")
                    wire("landfall_land_played", {"turn": turn})
                else:
                    say(f"{c.name} plays {LAND} (turn {turn})")
                return True
        # cast Moraug once affordable (6 mana: 4 generic + RR)
        if (moraug is None and turn not in ctx.cast_attempt_turns
                and untapped_mountains(state, pid) >= 6):
            mid = find_hand_moraug(state, pid)
            if mid is not None:
                cs = next((a for a in acts
                           if a["type"] == "CastSpell"
                           and str((a.get("data") or {}).get("object_id"))
                           == str(mid)), None)
                if cs:
                    ctx.cast_attempt_turns.add(turn)
                    await submit_as_is(c, cs)
                    ctx.moraug_cast_turn = turn
                    say(f"{c.name} casts {MORAUG} (hand oid {mid}, "
                        f"turn {turn})")
                    wire("moraug_cast", {"turn": turn, "hand_oid": mid})
                    return True

    # 5. P1 draw-go: land drop, never attacks, never blocks
    if pid != ctx.p0_id:
        if (state.get("active_player") == pid
                and state.get("phase") in ("PreCombatMain",
                                           "PostCombatMain")):
            turn = state.get("turn_number")
            if turn not in ctx.land_turns[pid]:
                pl = next((a for a in acts
                           if a["type"] == "PlayLand"
                           and (lname(state, (a.get("data") or {})
                                      .get("object_id")) or "")
                           == "Island"), None)
                if pl:
                    await submit_as_is(c, pl)
                    ctx.land_turns[pid].add(turn)
                    return True
        if state.get("phase") == "DeclareAttackers":
            da = next((a for a in acts
                       if a["type"] == "DeclareAttackers"), None)
            if da:
                na = copy.deepcopy(da)
                nd = na.setdefault("data", {})
                nd["attacks"] = []
                nd["bands"] = []
                await c.send_action(na)
                return True
        if state.get("phase") == "DeclareBlockers":
            db = next((a for a in acts
                       if a["type"] == "DeclareBlockers"), None)
            if db:
                na = copy.deepcopy(db)
                nd = na.setdefault("data", {})
                nd["blocks"] = []
                await c.send_action(na)
                return True

    # 6. P0 combat: attack with Moraug once it can (no summoning sickness)
    if (pid == ctx.p0_id and state.get("phase") == "DeclareAttackers"):
        da = next((a for a in acts
                   if a["type"] == "DeclareAttackers"), None)
        if da:
            na = copy.deepcopy(da)
            nd = na.setdefault("data", {})
            turn = state.get("turn_number")
            moraug = find_bf_moraug(state, pid)
            if moraug:
                ctx.moraug_oid = moraug["id"]
            ready = (moraug is not None and not moraug.get("tapped")
                     and ctx.moraug_cast_turn is not None
                     and turn > ctx.moraug_cast_turn)
            if ready:
                nd["attacks"] = [[moraug["id"],
                                  {"type": "Player",
                                   "data": 1 - ctx.p0_id}]]
                nd["bands"] = []
                await c.send_action(na)
                say(f"{c.name} attacks with Moraug {moraug['id']} "
                    f"(turn {turn})")
            else:
                nd["attacks"] = []
                nd["bands"] = []
                await c.send_action(na)
                say(f"{c.name} holds attack (turn {turn}, "
                    f"moraug={'out' if moraug else 'none'})")
            return True

    # 7. default: pass priority only when it is actually ours
    if my_priority:
        pp = next((a for a in acts if a["type"] == "PassPriority"), None)
        if pp:
            await submit_as_is(c, pp)
            return True
    return False


def sample_stack(c, ctx):
    """Record every non-empty stack snapshot; called on each drive
    iteration so fast trigger windows are sampled at full rate."""
    st = c.latest
    if not st:
        return
    state = st.get("state") or {}
    stk = state.get("stack") or []
    if not stk:
        return
    key = (c.revision, tuple(e.get("id") for e in stk))
    if ctx.stack_history and ctx.stack_history[-1].get("_key") == key:
        return
    snap = stack_snapshot(state)
    entry = {"rev": c.revision, "turn": state.get("turn_number"),
             "phase": state.get("phase"),
             "life": (life_of(state, 0), life_of(state, 1)),
             "stack": snap, "_key": key}
    ctx.stack_history.append(entry)
    wire("stack_sample", {k: v for k, v in entry.items() if k != "_key"})
    for e in snap:
        if is_landfall_trigger(e, ctx.moraug_oid) and not ctx.trigger_seen:
            ctx.trigger_seen = True
            ctx.trigger_observations += 1
            say("Moraug landfall trigger observed on stack:",
                json.dumps(snap)[:600])
            wire("landfall_trigger_stack", snap)


async def drive(p0, p1, ctx, want_fn, timeout_s, label, track_wf=False,
                track_phase=True):
    """Drive both seats until want_fn(p0 state) is true or timeout."""
    t0 = time.time()
    last = {p0.name: (-1, 0.0), p1.name: (-1, 0.0)}
    warned = set()
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.25)
        for c, pid in ((p0, ctx.p0_id), (p1, 1 - ctx.p0_id)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            last_rev, last_tick = last[c.name]
            if rev == last_rev and c.name not in warned \
                    and time.time() - last_tick > 45:
                warned.add(c.name)
                say(f"WATCHDOG: {c.name} revision {rev} unchanged for 45s")
                wire("watchdog_stall", {"who": c.name, "rev": rev})
            may_act = (rev != last_rev) or (time.time() - last_tick > 5)
            if may_act:
                try:
                    acted = await tick(c, pid, ctx)
                except Exception as e:
                    say(f"tick error for {c.name}: {e}")
                    acted = False
                if acted:
                    last[c.name] = (rev, time.time())
        st = p0.latest
        if st:
            state = st.get("state") or {}
            sample_stack(p0, ctx)
            if track_phase:
                record_phase(ctx, state)
            if (ctx.trigger_seen and not ctx.mid_trigger_exported
                    and ctx.landfall_turn is not None):
                ctx.mid_trigger_exported = True
                say("trigger checkpoint hit; exporting mid_trigger")
                mid = await export_state(p0, f"{EVDIR}/mid_trigger.json")
                wire("stack_at_mid_trigger", stack_snapshot(mid))
                obs_mid = {"life": (life_of(mid, 0), life_of(mid, 1)),
                           "stack": stack_snapshot(mid)}
                wire("mid_trigger", obs_mid)
            if track_wf:
                record_wf(ctx, state)
            if want_fn(state):
                return state
            if state.get("game_over") or state.get("winner") is not None:
                say("game ended during", label)
                return None
    say(f"TIMEOUT in drive: {label}")
    return None


async def export_state(c, path):
    s = await c.export_state()
    with open(path, "w") as f:
        f.write(s)
    return json.loads(s)["state"]


def life_of(state, pid):
    return (state["players"][pid] or {}).get("life")


def landfall_turn_combats(ctx):
    """Count BeginCombat phases during the landfall turn (P0 active)."""
    n = 0
    for turn, active, phase in ctx.phase_log:
        if (turn == ctx.landfall_turn and active == ctx.p0_id
                and phase == "BeginCombat"):
            n += 1
    return n


def landfall_turn_attack_steps(ctx):
    """Count DeclareAttackers phases during the landfall turn (P0 active).

    This is the decisive outcome measure: an additional combat phase that
    contains no DeclareAttackers step grants no additional attacks — the
    exact reported defect ("doesn't actually give additional combat
    steps")."""
    n = 0
    for turn, active, phase in ctx.phase_log:
        if (turn == ctx.landfall_turn and active == ctx.p0_id
                and phase == "DeclareAttackers"):
            n += 1
    return n


def empty_combat_shells(ctx):
    """BeginCombat phases immediately followed by EndCombat (no steps)."""
    shells = 0
    pl = ctx.phase_log
    for i, (turn, active, phase) in enumerate(pl):
        if (turn == ctx.landfall_turn and active == ctx.p0_id
                and phase == "BeginCombat"
                and i + 1 < len(pl) and pl[i + 1][2] == "EndCombat"):
            shells += 1
    return shells


def landfall_phase_seq(ctx):
    return [(t, p) for (t, a, p) in ctx.phase_log
            if t == ctx.landfall_turn and a == ctx.p0_id]


async def main():
    t0 = time.time()
    ctx = Ctx()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck((MORAUG, 4), (LAND, 56)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 60),))
    ctx.p0_id = p0.player_id
    ctx.game_code = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id}")

    await keep_mulligan(p0)
    await keep_mulligan(p1)

    # Phase 1: develop; cast Moraug when affordable
    def moraug_out(st):
        return find_bf_moraug(st, ctx.p0_id) is not None

    s = await drive(p0, p1, ctx, moraug_out, GAME_TIMEOUT,
                    "cast Moraug", track_wf=True)
    if s is None:
        obs["notes"].append("Moraug never reached the battlefield "
                            "within the game timeout")
        for k in ("A1_setup_ok", "A2_trigger_fired",
                  "A3_additional_combat", "A4_cleanup"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close()
        await p1.close()
        return finish(obs, t0, ctx)

    pre = await export_state(p0, f"{EVDIR}/pre_landfall.json")
    pre_life0 = life_of(pre, ctx.p0_id)
    pre_life1 = life_of(pre, 1 - ctx.p0_id)
    moraug = find_bf_moraug(pre, ctx.p0_id)
    ctx.moraug_oid = moraug["id"] if moraug else None
    obs["assert"]["A1_setup_ok"] = (
        "passed" if (moraug is not None and pre_life0 == 20
                     and pre_life1 == 20) else "failed")
    obs["notes"].append(
        f"pre_landfall: moraug oid={ctx.moraug_oid} "
        f"cast_turn={ctx.moraug_cast_turn} turn={pre.get('turn_number')} "
        f"phase={pre.get('phase')} life={pre_life0}/{pre_life1} "
        f"mountains={sum(1 for o in bf_objs(pre, ctx.p0_id) if (o.get('base_name') or o.get('name')) == LAND)}")
    say("pre_landfall exported; waiting for a post-cast PreCombatMain "
        "to play the landfall land")

    # Phase 2: play one land in PreCombatMain with Moraug out, then play
    # out the whole turn and observe the phase sequence.
    def landfall_turn_complete(st):
        if ctx.landfall_turn is None:
            return False
        return (st.get("turn_number") != ctx.landfall_turn
                and st.get("active_player") == ctx.p0_id
                and st.get("phase") == "PreCombatMain"
                and not (st.get("stack") or []))

    s2 = await drive(p0, p1, ctx, landfall_turn_complete, GAME_TIMEOUT,
                     "landfall turn", track_wf=True)
    if s2 is None:
        obs["notes"].append("landfall turn never completed "
                            f"(landfall_turn={ctx.landfall_turn})")
    if ctx.landfall_turn is None:
        obs["notes"].append("no landfall land was ever played in "
                            "PreCombatMain with Moraug out")
        for k in ("A2_trigger_fired", "A3_additional_combat",
                  "A4_cleanup"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close()
        await p1.close()
        return finish(obs, t0, ctx)

    # backstop: scan stack history for the trigger in case the live
    # sample missed it
    hist_hits = [e for e in ctx.stack_history
                 if any(is_landfall_trigger(x, ctx.moraug_oid)
                        for x in e["stack"])]
    if hist_hits and not ctx.trigger_seen:
        ctx.trigger_seen = True
        say(f"landfall trigger found in stack history "
            f"({len(hist_hits)} samples)")
        wire("landfall_trigger_history",
             [{"rev": e["rev"], "turn": e["turn"], "phase": e["phase"],
               "stack": e["stack"]} for e in hist_hits])
    obs["notes"].append(
        f"stack_history: {len(ctx.stack_history)} non-empty samples, "
        f"landfall-trigger samples={len(hist_hits)}")
    mh_path = f"{EVDIR}/mid_trigger.json"
    if os.path.exists(mh_path):
        mh = json.load(open(mh_path))["state"]
        obs["notes"].append(
            f"mid_trigger (turn {mh.get('turn_number')} "
            f"{mh.get('phase')}): life={life_of(mh, 0)}/{life_of(mh, 1)} "
            f"stack={json.dumps(stack_snapshot(mh))[:400]}")

    obs["assert"]["A2_trigger_fired"] = \
        "passed" if ctx.trigger_seen else "failed"

    seq = landfall_phase_seq(ctx)
    n_combats = landfall_turn_combats(ctx)
    n_attack_steps = landfall_turn_attack_steps(ctx)
    n_shells = empty_combat_shells(ctx)
    obs["notes"].append(
        f"landfall turn {ctx.landfall_turn} phase sequence (P0 active): "
        f"{' -> '.join(p for _, p in seq)}")
    obs["notes"].append(
        f"landfall turn: BeginCombat phases={n_combats}, "
        f"DeclareAttackers phases={n_attack_steps}, "
        f"empty combat shells (BeginCombat->EndCombat)={n_shells} "
        f"(expected: an additional FULL combat with its own "
        f"DeclareAttackers step)")
    obs["assert"]["A3_additional_combat"] = \
        "passed" if n_attack_steps >= 2 else "failed"

    post = await export_state(p0, f"{EVDIR}/post_turn.json")
    wire("stack_at_post", stack_snapshot(post))
    post_life0 = life_of(post, ctx.p0_id)
    post_life1 = life_of(post, 1 - ctx.p0_id)
    say(f"post_turn: life={post_life0}/{post_life1} "
        f"phase={post.get('phase')} turn={post.get('turn_number')}")
    obs["notes"].append(
        f"post_turn: life={post_life0}/{post_life1}, "
        f"phase={post.get('phase')} turn={post.get('turn_number')} "
        f"waiting={wf_type(post)} stack={len(post.get('stack') or [])}")
    stack_empty = not (post.get("stack") or [])
    turn_advanced = (post.get("turn_number") != ctx.landfall_turn)
    obs["assert"]["A4_cleanup"] = \
        "passed" if (stack_empty and turn_advanced) else "failed"

    await p0.close()
    await p1.close()
    return finish(obs, t0, ctx)


def finish(obs, t0, ctx):
    dur = time.time() - t0
    a = obs["assert"]
    # Outcome-level verdict: the reported defect is "the landfall trigger
    # shows but no additional combat steps are granted". reproduced iff the
    # setup was valid and the landfall turn granted fewer than 2 usable
    # attacker-declaration steps. (An earlier A3 variant counted
    # BeginCombat shells and wrongly passed: the engine inserts an empty
    # BeginCombat->EndCombat shell, which grants no attacks.)
    if a.get("A1_setup_ok") == "passed" \
            and a.get("A3_additional_combat") == "failed":
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_trigger_fired", "A3_additional_combat",
              "A4_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    result = {
        "issue": 7197,
        "run_id": RUN_ID,
        "game_code": ctx.game_code,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "verdict": verdict,
        "assertions": a,
        "notes": obs["notes"],
        "wf_seen": ctx.wf_seen,
        "rejections": ctx.rejections,
        "landfall_turn": ctx.landfall_turn,
        "moraug_oid": ctx.moraug_oid,
        "phase_log": ctx.phase_log,
        "decks": {
            "P0": [[MORAUG, 4], [LAND, 56]],
            "P1": [["Island", 60]],
        },
        "scenario_sha256": sha256_of_file(__file__),
    }
    with open(f"{EVDIR}/scenario_result.json", "w") as f:
        json.dump(result, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(a)}")
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass
    return result


asyncio.run(main())
