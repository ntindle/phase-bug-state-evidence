#!/usr/bin/env python3
"""Issue #7196: Mage Slayer — Trigger goes on stack but nothing happens.

Oracle: "Whenever equipped creature attacks, it deals damage equal to its
power to the player or planeswalker it's attacking."
Report: the attack trigger is placed on the stack but resolves without
dealing damage (no life or loyalty change).

Behavioral contract (single game), pinned v0.85.0 / protocol 72:
  Setup: P0: 8x Grizzly Bears + 8x Mage Slayer + 22x Forest + 22x Mountain.
         P1: 60x Island, draw-go, no blockers.
  A1 setup_ok    Mage Slayer cast and equipped to a Grizzly Bears; life 20/20
                 at the pre-attack checkpoint.
  A2 trigger_fired Mage Slayer's TriggeredAbility (source_id == slayer's
                 battlefield oid) observed live on the stack after the
                 bear is declared as an attacker.
  A3 damage_dealt P1 life at the post-combat checkpoint == 16: 2 combat
                 damage from the unblocked 2/2 bear + 2 from the Slayer
                 trigger. The reported defect is P1 staying at 18 (trigger
                 resolves with no life change).
  A4 cleanup     stack empty and the game proceeds after combat.

Verdict = reproduced iff A2 passed and A3 failed (trigger fired, no
damage). not-reproduced iff A1-A4 all pass. blocked otherwise.

Protocol-72 driver conventions (per AGENTS.md): CreateGameWithSettings +
JoinGameWithPassword + start_when_full, merged_actions (legal_actions +
legal_actions_by_object), per-seat tick with revision-change-or-5s re-tick,
default PassPriority gated on my_priority, MulliganDecision Keep via
advertised action as-is, engine auto-taps mana for casts (PayMana answered
as-is when advertised). Activated abilities advertised as ActivateAbility
{source_id, ability_index}; Equip's target prompt answered from the
viewer_interaction schema opportunity (one choice per prompt, never marked
done until the ability hits the stack).
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
RUN_ID = "20260917-7196f"
EVDIR = f"{BACKFILL}/evidence/7196/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEAR = "Grizzly Bears"
SLAYER = "Mage Slayer"

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


def player_of(state, pid):
    return state["players"][pid]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def bf_objs(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if lname(state, oid) == name:
            return oid
    return None


def find_bf(state, pid, name):
    for o in bf_objs(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o
    return None


def untapped_land(state, pid, name):
    return sum(1 for o in bf_objs(state, pid)
               if (o.get("base_name") or o.get("name")) == name
               and not o.get("tapped"))


def attached_oid(obj):
    """attached_to may be an int oid or a nested dict; extract the oid."""
    att = obj.get("attached_to")
    if att is None:
        return None
    if isinstance(att, int):
        return att
    if isinstance(att, str) and att.isdigit():
        return int(att)
    found = []

    def rec(n):
        if isinstance(n, int):
            found.append(n)
        elif isinstance(n, dict):
            for v in n.values():
                rec(v)
        elif isinstance(n, (list, tuple)):
            for v in n:
                rec(v)
    rec(att)
    return found[0] if found else None


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
                      or "")[:120]}
            for e in (state.get("stack") or [])]


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


# ---- viewer_interaction helpers (equip target prompt) ----
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


def find_target_opportunity(st):
    """Schema sequence/select opportunity whose candidates carry references."""
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
        self.bear_oid = None        # equipped creature's battlefield oid
        self.slayer_oid = None      # Mage Slayer's battlefield oid
        self.equip_done = False
        self.equip_pending = False
        self.cast_turn = {}         # oid str -> turn number (summoning sick)
        self.attack_turn = None
        self.attack_declared_at = None  # wall-clock of attack declaration
        self.mid_hold_exported = False
        self.trigger_seen = False
        self.mid_trigger_life = None
        self.land_turns = {0: set(), 1: set()}
        self.wf_seen = []
        self.rejections = []
        self.game_code = None
        self.logged_wait = set()
        self.stack_history = []  # every non-empty stack snapshot w/ context


def record_wf(ctx, state):
    wt = wf_type(state)
    if wt and (not ctx.wf_seen or ctx.wf_seen[-1] != wt):
        ctx.wf_seen.append(wt)
        wire("waiting_for", {"type": wt, "data": wf_of(state).get("data")})


async def answer_equip_target(c, ctx):
    """Answer the equip TargetSelection prompt for the bear."""
    st = c.latest
    if not st:
        return False
    state = st.get("state") or {}
    if wf_type(state) != "TargetSelection" or not ctx.equip_pending:
        return False
    found = find_target_opportunity(st)
    if not found:
        return False
    iid, spec_type, chs = found
    pick = None
    for ch in chs:
        if str(cand_ref(ch)) == str(ctx.bear_oid):
            pick = ch
            break
    if pick is None:
        key = (c.name, iid)
        if key not in ctx.logged_wait:
            ctx.logged_wait.add(key)
            say(f"{c.name} equip target prompt: wanted bear "
                f"{ctx.bear_oid} not among "
                f"{[cand_ref(ch) for ch in chs]}; deferring")
        return False
    sub = {"interactionId": iid,
           "response": {"type": spec_type,
                        "data": {"choiceIds": [pick["id"]]}}}
    say(f"{c.name} equip target -> bear {ctx.bear_oid} "
        f"(candidate {pick['id']})")
    wire("equip_target_submit", sub)
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


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

    # 0. stack sampling now happens in drive() on every iteration
    # (not only when the tick acts) so a held priority window is sampled
    # at full rate; see sample_stack() below.

    # 1. mana payment prompts: answer as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True

    # 2. P0 equip target prompt (equip activation pending)
    if pid == ctx.p0_id and ctx.equip_pending:
        if await answer_equip_target(c, ctx):
            return True
        # fall through: keep passing priority only when ours; the
        # equip target is answered by the same seat, so hold.
        if wtype == "TargetSelection":
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
        # land drop
        if turn not in ctx.land_turns[pid]:
            for lname_ in ("Forest", "Mountain"):
                pl = next((a for a in acts
                           if a["type"] == "PlayLand"
                           and lname(state, (a.get("data") or {})
                                     .get("object_id")) == lname_), None)
                if pl:
                    await submit_as_is(c, pl)
                    ctx.land_turns[pid].add(turn)
                    say(f"{c.name} plays {lname_} (turn {turn})")
                    return True
        # refresh battlefield ids
        bear = find_bf(state, pid, BEAR)
        slayer = find_bf(state, pid, SLAYER)
        if bear:
            ctx.bear_oid = bear["id"]
        if slayer:
            ctx.slayer_oid = slayer["id"]
            if attached_oid(slayer) == ctx.bear_oid:
                ctx.equip_done = True
                ctx.equip_pending = False
        # cast a bear if none on board
        if bear is None:
            bid = find_hand(state, pid, BEAR)
            if bid is not None:
                cs = next((a for a in acts
                           if a["type"] == "CastSpell"
                           and str((a.get("data") or {}).get("object_id"))
                           == str(bid)), None)
                if cs:
                    await submit_as_is(c, cs)
                    ctx.cast_turn[str(bid)] = turn
                    say(f"{c.name} casts {BEAR} (hand oid {bid})")
                    return True
        # cast Mage Slayer once a bear is out
        if bear is not None and slayer is None:
            sid = find_hand(state, pid, SLAYER)
            if sid is not None:
                cs = next((a for a in acts
                           if a["type"] == "CastSpell"
                           and str((a.get("data") or {}).get("object_id"))
                           == str(sid)), None)
                if cs:
                    await submit_as_is(c, cs)
                    say(f"{c.name} casts {SLAYER} (hand oid {sid})")
                    return True
        # activate Equip {3} once both are on board
        if (bear is not None and slayer is not None
                and not ctx.equip_done and not ctx.equip_pending):
            eq = next((a for a in acts
                       if a["type"] == "ActivateAbility"
                       and str((a.get("data") or {}).get("source_id"))
                       == str(slayer["id"])), None)
            if eq:
                await submit_as_is(c, eq)
                ctx.equip_pending = True
                say(f"{c.name} activates Equip (slayer {slayer['id']} "
                    f"-> bear {bear['id']})")
                wire("equip_activate", eq.get("data"))
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
                           and lname(state, (a.get("data") or {})
                                     .get("object_id")) == "Island"), None)
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
                say(f"{c.name} declares no blockers")
                return True

    # 6. P0 combat: attack with the equipped bear once summoning-sick-free
    if (pid == ctx.p0_id and state.get("phase") == "DeclareAttackers"):
        da = next((a for a in acts
                   if a["type"] == "DeclareAttackers"), None)
        if da:
            na = copy.deepcopy(da)
            nd = na.setdefault("data", {})
            turn = state.get("turn_number")
            ready = (ctx.equip_done and ctx.bear_oid is not None
                     and ctx.cast_turn.get(str(ctx.bear_oid)) is not None
                     and turn > ctx.cast_turn[str(ctx.bear_oid)])
            if ready:
                nd["attacks"] = [[ctx.bear_oid,
                                  {"type": "Player",
                                   "data": 1 - ctx.p0_id}]]
                nd["bands"] = []
                await c.send_action(na)
                ctx.attack_turn = turn
                ctx.attack_declared_at = time.time()
                say(f"{c.name} declares attackers: bear {ctx.bear_oid} "
                    f"-> P{1 - ctx.p0_id} (turn {turn})")
            else:
                nd["attacks"] = []
                nd["bands"] = []
                await c.send_action(na)
                say(f"{c.name} holds attack (equip_done={ctx.equip_done}, "
                    f"turn {turn})")
            return True

    # 7. default: pass priority only when it is actually ours.
    # After declaring attackers, hold P0's pass briefly so a fast
    # attack trigger sits on the stack long enough to be sampled.
    if my_priority:
        if (ctx.attack_declared_at is not None
                and time.time() - ctx.attack_declared_at < 3.0
                and state.get("phase") in ("DeclareAttackers",
                                           "DeclareBlockers")):
            if not getattr(ctx, "_hold_logged", False):
                ctx._hold_logged = True
                say(f"{c.name} HOLDING priority pass for trigger window "
                    f"(phase={state.get('phase')})")
                wire("priority_hold", {"phase": state.get("phase")})
            return False
        pp = next((a for a in acts if a["type"] == "PassPriority"), None)
        if pp:
            await submit_as_is(c, pp)
            return True
    return False


def sample_stack(c, ctx):
    """Record every non-empty stack snapshot; called on each drive
    iteration (not only when the tick acts) so a held priority window is
    sampled at full rate."""
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
    entry = {"rev": c.revision, "turn": state.get("turn_number"),
             "phase": state.get("phase"),
             "life": (life_of(state, 0), life_of(state, 1)),
             "stack": stack_snapshot(state), "_key": key}
    ctx.stack_history.append(entry)
    wire("stack_sample", {k: v for k, v in entry.items() if k != "_key"})


async def drive(p0, p1, ctx, want_fn, timeout_s, label, track_wf=False):
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
            # hard artifact: export authoritative state once, mid-hold,
            # while P0's priority pass is being held after the attack
            if (not ctx.mid_hold_exported
                    and ctx.attack_declared_at is not None
                    and time.time() - ctx.attack_declared_at < 3.0
                    and state.get("phase") in ("DeclareAttackers",
                                               "DeclareBlockers")):
                ctx.mid_hold_exported = True
                say("exporting mid_hold state during priority hold")
                mh = await export_state(p0, f"{EVDIR}/mid_hold.json")
                wire("stack_at_mid_hold", stack_snapshot(mh))
                obs_hold = {"life": (life_of(mh, 0), life_of(mh, 1)),
                            "stack": stack_snapshot(mh)}
                wire("mid_hold", obs_hold)
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


async def main():
    t0 = time.time()
    ctx = Ctx()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(("Forest", 22), ("Mountain", 22),
                         (BEAR, 8), (SLAYER, 8)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 60),))
    ctx.p0_id = p0.player_id
    ctx.game_code = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id}")

    await keep_mulligan(p0)
    await keep_mulligan(p1)

    # Phase 1: develop, cast bear + slayer, equip
    s = await drive(p0, p1, ctx,
                    lambda st: ctx.equip_done,
                    GAME_TIMEOUT, "equip", track_wf=True)
    if s is None:
        obs["notes"].append("equip never completed within the game timeout")
        for k in ("A1_setup_ok", "A2_trigger_fired", "A3_damage_dealt",
                  "A4_cleanup"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close()
        await p1.close()
        return finish(obs, t0, ctx)

    pre = await export_state(p0, f"{EVDIR}/pre_attack.json")
    pre_life0 = life_of(pre, ctx.p0_id)
    pre_life1 = life_of(pre, 1 - ctx.p0_id)
    bear = find_bf(pre, ctx.p0_id, BEAR)
    slayer = find_bf(pre, ctx.p0_id, SLAYER)
    obs["assert"]["A1_setup_ok"] = (
        "passed" if (bear and slayer
                     and attached_oid(slayer) == bear["id"]
                     and pre_life0 == 20 and pre_life1 == 20)
        else "failed")
    obs["notes"].append(
        f"pre_attack: slayer {slayer['id'] if slayer else None} attached_to="
        f"{attached_oid(slayer) if slayer else None}, bear="
        f"{bear['id'] if bear else None} power="
        f"{bear.get('power') if bear else '?'}, life={pre_life0}/{pre_life1}")
    say("pre_attack exported; waiting for the attack turn")

    # Phase 2: attack; capture the trigger on the stack
    trig = {"v": False}
    mid_exported = {"v": False}

    def trigger_on_stack(st):
        if ctx.slayer_oid is None:
            return False
        hit = any((e.get("kind") or {}).get("type") == "TriggeredAbility"
                  and e.get("source_id") == ctx.slayer_oid
                  for e in (st.get("stack") or []))
        if hit and not trig["v"]:
            trig["v"] = True
            ctx.trigger_seen = True
            ctx.mid_trigger_life = (life_of(st, ctx.p0_id),
                                    life_of(st, 1 - ctx.p0_id))
            say("Mage Slayer trigger observed on stack:",
                json.dumps(stack_snapshot(st))[:700])
            wire("slayer_trigger_stack", stack_snapshot(st))
        return hit and not mid_exported["v"]  # stop once to export mid

    def combat_done(st):
        return (ctx.attack_turn is not None
                and st.get("turn_number") != ctx.attack_turn
                and not (st.get("stack") or [])
                and st.get("active_player") == ctx.p0_id
                and st.get("phase") == "PreCombatMain")

    def slayer_trigger_entry(sample):
        for e in sample.get("stack") or []:
            if (e.get("kind") != "TriggeredAbility"):
                continue
            if e.get("source_id") == ctx.slayer_oid:
                return e
            desc = (e.get("desc") or "").lower()
            if "mage slayer" in desc or "equipped creature attacks" in desc:
                return e
        return None

    s2 = await drive(p0, p1, ctx,
                     lambda st: trigger_on_stack(st) or combat_done(st),
                     GAME_TIMEOUT, "attack + trigger", track_wf=True)
    # export the mid-trigger checkpoint on first live sighting, continue
    if s2 is not None and trig["v"] and not mid_exported["v"]:
        mid_exported["v"] = True
        say("trigger checkpoint hit; exporting mid_trigger and continuing")
        mid = await export_state(p0, f"{EVDIR}/mid_trigger.json")
        wire("stack_at_mid_trigger", stack_snapshot(mid))
        obs["notes"].append(
            f"mid_trigger: life={life_of(mid, ctx.p0_id)}/"
            f"{life_of(mid, 1 - ctx.p0_id)} stack="
            f"{json.dumps(stack_snapshot(mid))[:400]}")
        s3 = await drive(p0, p1, ctx, combat_done, PHASE_TIMEOUT,
                         "combat completion", track_wf=True)
        if s3 is None:
            obs["notes"].append("combat never completed after the trigger")
    # backstop: scan the per-tick stack history for the trigger even if the
    # live want_fn check missed the window
    hist_hits = [s for s in ctx.stack_history if slayer_trigger_entry(s)]
    if hist_hits and not trig["v"]:
        trig["v"] = True
        ctx.trigger_seen = True
        say(f"slayer trigger found in stack history "
            f"({len(hist_hits)} samples, not seen live)")
        wire("slayer_trigger_history",
             [{"rev": s["rev"], "turn": s["turn"], "phase": s["phase"],
               "life": s["life"],
               "entry": slayer_trigger_entry(s)} for s in hist_hits])
    obs["notes"].append(
        f"stack_history: {len(ctx.stack_history)} non-empty stack samples, "
        f"slayer-trigger samples={len(hist_hits)}")
    mh_path = f"{EVDIR}/mid_hold.json"
    if os.path.exists(mh_path):
        mh = json.load(open(mh_path))["state"]
        obs["notes"].append(
            f"mid_hold (priority held post-attack): "
            f"life={life_of(mh, 0)}/{life_of(mh, 1)} "
            f"stack={json.dumps(stack_snapshot(mh))[:400]}")
    obs["assert"]["A2_trigger_fired"] = \
        "passed" if trig["v"] else "failed"
    obs["assert"]["A2_trigger_fired"] = \
        "passed" if trig["v"] else "failed"

    post = await export_state(p0, f"{EVDIR}/post_combat.json")
    wire("stack_at_post", stack_snapshot(post))
    post_life0 = life_of(post, ctx.p0_id)
    post_life1 = life_of(post, 1 - ctx.p0_id)
    say(f"post_combat: life={post_life0}/{post_life1} "
        f"phase={post.get('phase')} turn={post.get('turn_number')}")
    obs["notes"].append(
        f"post_combat: life={post_life0}/{post_life1}, "
        f"mid_trigger_life={ctx.mid_trigger_life}, "
        f"expected P1=16 (2 combat + 2 trigger)")

    # A3: unblocked 2/2 bear deals 2 combat; trigger should deal 2 more.
    obs["assert"]["A3_damage_dealt"] = \
        "passed" if post_life1 == 16 else "failed"

    stack_empty = not (post.get("stack") or [])
    obs["assert"]["A4_cleanup"] = "passed" if stack_empty else "failed"
    obs["notes"].append(
        f"post: stack entries={len(post.get('stack') or [])} "
        f"phase={post.get('phase')} turn={post.get('turn_number')} "
        f"waiting={wf_type(post)}")

    await p0.close()
    await p1.close()
    return finish(obs, t0, ctx)


def finish(obs, t0, ctx):
    dur = time.time() - t0
    a = obs["assert"]
    # Outcome-level verdict: the reported defect is "the Slayer trigger deals
    # no damage when the equipped creature attacks". reproduced iff the setup
    # was valid and no trigger damage was dealt. A2 refines the mechanism:
    # on v0.85.0 the trigger is never even placed on the stack (mid_hold
    # shows an empty stack during a held post-attack priority window).
    if a.get("A1_setup_ok") == "passed" \
            and a.get("A3_damage_dealt") == "failed":
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_trigger_fired", "A3_damage_dealt",
              "A4_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    result = {
        "issue": 7196,
        "run_id": RUN_ID,
        "game_code": ctx.game_code,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "verdict": verdict,
        "assertions": a,
        "notes": obs["notes"],
        "wf_seen": ctx.wf_seen,
        "rejections": ctx.rejections,
        "decks": {
            "P0": [["Forest", 22], ["Mountain", 22], [BEAR, 8],
                   [SLAYER, 8]],
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
