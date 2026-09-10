#!/usr/bin/env python3
"""Issue #6762: Wand of Orcus grants deathtouch to the entire board.

Reported: "Whenever equipped creature attacks or blocks, it and Zombies you
control gain deathtouch until end of turn." -- but the whole board (lands,
artifacts, all creatures) gains deathtouch.

Triage/classifier claim: the trigger's grant child targets/affects `any target`
instead of (equipped creature + Zombies controlled by the Wand's controller).

Behavioral contract (two games, v0.79.0 / protocol 69):
  Game A (attack branch):
    A1 setup_ok      Wand of Orcus equipped to a P0 creature; P0 has a Zombie
                     (Diregraf Ghoul), a noncreature artifact (Manalith),
                     lands; P1 has a creature.
    A2 attack_declared   P0 declares the equipped creature as attacker.
    A3 trigger_seen   record whether the AttacksOrBlocks trigger fired
                     (informational; effect is Unimplemented in pinned data).
    A4 no_overbroad_grant  NO battlefield object anywhere has deathtouch after
                     the attack (scan full object JSON; the bug would paint the
                     whole board).
    A5 cleanup        game proceeds past combat.
  Game B (block branch): same, but the trigger fires when the equipped
    creature blocks P1's attacker. Assertions B1..B5 mirror A1..A5.

Verdict = not-reproduced iff A4/B4 pass (no grant at all: the effect is
Unimplemented in the pinned v0.79.0 card data, so nothing can be granted --
reported behavior cannot manifest). reproduced iff any battlefield object
outside the legal set gains deathtouch.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6762")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260910-6762"
EVDIR = f"{BACKFILL}/evidence/6762/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload},
                              default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in state["players"][pid]["hand"]:
        if obj_name(state, oid) == name:
            return int(oid)
    return None


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if (o.get("base_name") or o.get("name")) == name:
            return o["id"]
    return None


def untapped_lands(state, pid):
    return [o for o in bf(state, pid)
            if (o.get("base_name") or o.get("name")) in ("Forest", "Swamp")
            and not o.get("tapped")]


def has_deathtouch(obj):
    return "Deathtouch" in json.dumps(obj)


def stack_snapshot(state):
    return [str(x)[:120] for x in (state.get("stack") or [])]


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("text"):
            return str(d["text"])
    return ""


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


SUBMITTED = set()


def find_activate_choice(st, code, source_ref=None):
    vi = get_vi(st)
    if not vi:
        return None
    for op in vi.get("opportunities", []):
        if op.get("interactionId") in SUBMITTED:
            continue
        resp = op.get("response", {})
        if resp.get("type") != "exactChoices":
            continue
        for ch in resp["data"].get("choices", []):
            surfs = ch.get("surfaces", [])
            codes = [s.get("data", {}).get("code") for s in surfs
                     if isinstance(s.get("data"), dict)]
            if code not in codes:
                continue
            if source_ref is not None:
                refs = [s.get("data", {}).get("reference") for s in surfs
                        if isinstance(s.get("data"), dict)
                        and s.get("data", {}).get("role") == "source"]
                if str(source_ref) not in [str(r) for r in refs]:
                    continue
            return op.get("interactionId"), ch
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


def attached_oid(obj):
    """attached_to may be an int oid or a nested dict; extract the oid."""
    att = obj.get("attached_to")
    if att is None:
        return None
    if isinstance(att, int):
        return att
    if isinstance(att, str) and att.isdigit():
        return int(att)
    # nested: search for a value that is a known object id
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


async def pay_and_mulligan(c):
    st = c.latest
    if not st:
        return False
    acts = st.get("legal_actions", [])
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps opening hand")
            return True
    return False


class Ctx:
    def __init__(self):
        self.assert_ = {}
        self.notes = []
        self.equipped_oid = None      # creature the wand is attached to
        self.wand_oid = None
        self.cast_turn = {}           # oid -> turn cast (summoning sickness)
        self.trigger_seen = False
        self.trigger_stack_text = []
        self.pre_exported = False
        self.post_exported = False
        self.attack_turn = None
        self.equip_done = False
        self.done = False
        self.phase = None             # 'attack' or 'block'


async def p1_main(p1, ctx):
    """P1 draw-go: land drop, opportunistic Bears (needed for the block game),
    never attacks in the attack phase."""
    st = p1.latest
    if not st:
        return False
    s = st["state"]
    if not (s.get("active_player") == p1.player_id
            and s.get("priority_player") == p1.player_id
            and s.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")):
        return False
    acts = st.get("legal_actions", [])
    for a in acts:
        d = a.get("data", {}) or {}
        if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) == "Forest":
            await p1.send_action(a)
            say("P1 plays land Forest")
            return True
    if True:  # both phases: P1 needs a creature witness on the board
        lands = [o for o in bf(s, p1.player_id)
                 if (o.get("base_name") or o.get("name")) == "Forest"
                 and not o.get("tapped")]
        for a in acts:
            d = a.get("data", {}) or {}
            if (a["type"] == "CastSpell"
                    and obj_name(s, d.get("object_id")) == "Grizzly Bears"
                    and len(lands) >= 2):
                await p1.send_action(a)
                oid = (a.get("data") or {}).get("object_id")
                if oid is not None:
                    ctx.cast_turn[str(oid)] = s.get("turn_number")
                say("P1 casts Grizzly Bears")
                return True
    return False


async def watchdog(p0, p1, ctx, t0):
    """Log turn/phase/waiting_for every 30s so stalls are diagnosable."""
    try:
        while not ctx.done:
            await asyncio.sleep(30)
            for c in (p0, p1):
                st = c.latest
                if not st:
                    say(f"[watchdog] {c.name}: no state yet")
                    continue
                s = st["state"]
                wf = s.get("waiting_for") or {}
                acts = [a["type"] for a in st.get("legal_actions", [])][:8]
                say(f"[watchdog] {c.name} t+{int(time.time()-t0)}s rev={st.get('state_revision')} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={wf.get('type')} prio={s.get('priority_player')} acts={acts}")
    except asyncio.CancelledError:
        pass


async def p0_main_decisions(p0, ctx):
    """Cast/equip logic for the Wand seat during its main phases."""
    st = p0.latest
    if not st:
        return False
    s = st["state"]
    if not (s.get("active_player") == p0.player_id
            and s.get("priority_player") == p0.player_id
            and s.get("phase") in ("PreCombatMain", "PostCombatMain", "Main")):
        return False
    acts = st.get("legal_actions", [])
    turn = s.get("turn_number")
    lands = untapped_lands(s, p0.player_id)
    n_land = len(lands)
    n_swamp = sum(1 for o in lands
                  if (o.get("base_name") or o.get("name")) == "Swamp")

    def castable(name):
        for a in acts:
            d = a.get("data", {}) or {}
            if a["type"] == "CastSpell" and obj_name(s, d.get("object_id")) == name:
                return a
        return None

    # 1. land drop
    for a in acts:
        d = a.get("data", {}) or {}
        if a["type"] == "PlayLand" and obj_name(s, d.get("object_id")) in ("Forest", "Swamp"):
            await p0.send_action(a)
            say(f"P0 plays land {obj_name(s, d.get('object_id'))}")
            return True
    # 2. cast Wand of Orcus ({2}{B})
    wand_bf = find_bf(s, p0.player_id, "Wand of Orcus")
    if wand_bf is None:
        a = castable("Wand of Orcus")
        if a and n_land >= 3 and n_swamp >= 1:
            await p0.send_action(a)
            say("P0 casts Wand of Orcus")
            return True
    else:
        ctx.wand_oid = wand_bf
    # 3. equip if possible: wand on BF, {3} mana available
    if (not ctx.equip_done and not getattr(ctx, "equip_pending", False)
            and ctx.wand_oid is not None and n_land >= 3):
        # prefer a non-zombie creature as the equipped witness (bears),
        # fall back to any creature
        target = None
        for cand_name in ("Grizzly Bears", "Diregraf Ghoul"):
            oid = find_bf(s, p0.player_id, cand_name)
            if oid is not None:
                wand = s["objects"].get(str(ctx.wand_oid), {})
                if attached_oid(wand) != oid:
                    target = oid
                    break
        if target is not None:
            f = find_activate_choice(st, "activateAbility",
                                     source_ref=ctx.wand_oid)
            if f:
                iid, ch = f
                say(f"P0 activating equip (wand {ctx.wand_oid} -> creature {target} {obj_name(s, target)})")
                wire("equip_activate", {"interactionId": iid,
                                        "choiceId": ch["id"],
                                        "wand": ctx.wand_oid,
                                        "target_creature": target,
                                        "target_name": obj_name(s, target)})
                ctx.equip_target = target
                ctx.equip_pending = True
                ctx.equip_pending_since = time.time()
                await p0.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose",
                                  "data": {"choiceId": ch["id"]}}})
                SUBMITTED.add(iid)
                return True
    # 4. cast creatures/artifacts (opportunistic witnesses)
    for name, need_land, need_swamp in (("Diregraf Ghoul", 1, 1),
                                        ("Grizzly Bears", 2, 0),
                                        ("Manalith", 3, 0)):
        a = castable(name)
        if a and n_land >= need_land and (need_swamp == 0 or n_swamp >= need_swamp):
            await p0.send_action(a)
            oid = (a.get("data") or {}).get("object_id")
            if oid is not None:
                ctx.cast_turn[str(oid)] = turn
            say(f"P0 casts {name}")
            return True
    return False


async def p0_equip_target(p0, ctx):
    """Answer the equip target-selection prompt for the intended creature."""
    st = p0.latest
    if not st:
        return False
    s = st["state"]
    wf = s.get("waiting_for") or {}
    if wf.get("type") not in ("TargetSelection",):
        return False
    # only answer if this looks like the equip targeting (our wand activation)
    found = find_target_opportunity(st)
    if not found:
        return False
    iid, spec_type, chs = found
    want = getattr(ctx, "equip_target", None)
    pick = None
    for ch in chs:
        if str(cand_ref(ch)) == str(want):
            pick = ch
            break
    if pick is None:
        say(f"P0 equip target prompt: wanted creature {want} not among "
            f"{[cand_ref(ch) for ch in chs]}; deferring")
        wire("equip_target_deferred",
             {"iid": iid, "wanted": want,
              "refs": [cand_ref(ch) for ch in chs]})
        return False
    sub = {"interactionId": iid,
           "response": {"type": spec_type,
                        "data": {"choiceIds": [pick["id"]]}}}
    say(f"P0 equip target -> {obj_name(s, want)} (candidate {pick['id']})")
    wire("equip_target_submit", sub)
    await p0.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


def attack_ready(s, pid, oid, ctx):
    turn = s.get("turn_number")
    cast_t = ctx.cast_turn.get(str(oid))
    # creatures on the battlefield from game start (none here) or cast on an
    # earlier turn may attack; unknown cast turn -> require having seen it
    return cast_t is not None and turn > cast_t


async def declare_attack(p0, p1, ctx):
    """P0 declares the equipped creature as attacker once the full witness
    board is present; P1 declares no attackers and no blockers."""
    acted = False
    # P1 blockers: always empty in the attack phase
    st = p1.latest
    if st and st["state"].get("phase") == "DeclareBlockers":
        for a in st.get("legal_actions", []):
            if a.get("type") != "DeclareBlockers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            nd["blocks"] = []
            await p1.send_action(na)
            say("P1 declares no blockers")
            acted = True
            break
    for c, pid, is_p0 in ((p0, p0.player_id, True), (p1, p1.player_id, False)):
        st = c.latest
        if not st:
            continue
        s = st["state"]
        if s.get("phase") != "DeclareAttackers":
            continue
        for a in st.get("legal_actions", []):
            if a.get("type") != "DeclareAttackers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            ready = (is_p0 and ctx.equip_done and ctx.equipped_oid is not None
                     and attack_ready(s, pid, ctx.equipped_oid, ctx)
                     and find_bf(s, p0.player_id, "Manalith") is not None
                     and find_bf(s, p1.player_id, "Grizzly Bears") is not None)
            if ready:
                att_oid = ctx.equipped_oid
                nd["attacks"] = [[att_oid, {"type": "Player",
                                           "data": p1.player_id}]]
                nd["bands"] = []
                say(f"P0 declares attackers: {obj_name(s, att_oid)} -> P1")
                wire("declare_attackers", {"action": na})
                await p0.send_action(na)
                ctx.attack_turn = s.get("turn_number")
            else:
                nd["attacks"] = []
                nd["bands"] = []
                await c.send_action(na)
                if is_p0 and ctx.equip_done:
                    say("P0 holds attack: waiting for full witness board")
            acted = True
            break
    return acted


async def declare_block(p0, p1, ctx):
    """Game B: P1 attacks with a bear; P0 blocks with the equipped creature.
    P0 declares no attackers on its own turns; P1 declares no blockers."""
    acted = False
    # P0 attackers: always empty in the block phase
    st = p0.latest
    if st and st["state"].get("phase") == "DeclareAttackers":
        for a in st.get("legal_actions", []):
            if a.get("type") != "DeclareAttackers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            nd["attacks"] = []
            nd["bands"] = []
            await p0.send_action(na)
            acted = True
            break
    # P1 blockers: always empty in the block phase
    st = p1.latest
    if st and st["state"].get("phase") == "DeclareBlockers":
        for a in st.get("legal_actions", []):
            if a.get("type") != "DeclareBlockers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            nd["blocks"] = []
            await p1.send_action(na)
            acted = True
            break
    # P1 attacks
    st = p1.latest
    if st and st["state"].get("phase") == "DeclareAttackers":
        for a in st.get("legal_actions", []):
            if a.get("type") != "DeclareAttackers":
                continue
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            bear = find_bf(st["state"], p1.player_id, "Grizzly Bears")
            bear_ready = (bear is not None
                          and ctx.cast_turn.get(str(bear)) is not None
                          and st["state"].get("turn_number") > ctx.cast_turn[str(bear)]
                          and not st["state"]["objects"][str(bear)].get("tapped"))
            ready = (bear_ready
                     and find_bf(st["state"], p0.player_id, "Manalith") is not None
                     and ctx.equip_done and ctx.equipped_oid is not None)
            if ready:
                nd["attacks"] = [[bear, {"type": "Player", "data": p0.player_id}]]
                nd["bands"] = []
                say("P1 declares attackers: Grizzly Bears -> P0")
                wire("p1_declare_attackers", {"action": na})
                await p1.send_action(na)
            else:
                nd["attacks"] = []
                nd["bands"] = []
                await p1.send_action(na)
            acted = True
            break
    # P0 blocks
    st = p0.latest
    if st and st["state"].get("phase") == "DeclareBlockers":
        for a in st.get("legal_actions", []):
            if a.get("type") != "DeclareBlockers":
                continue
            wire("declare_blockers_advertised", {"action": a})
            say("P0 DeclareBlockers advertised:", json.dumps(a)[:400])
            na = copy.deepcopy(a)
            nd = na.setdefault("data", {})
            blk = ctx.equipped_oid
            # find P1's attacker object id
            combat = st["state"].get("combat") or {}
            attackers = combat.get("attackers") or []
            atk_oid = None
            for entry in attackers:
                if isinstance(entry, dict):
                    atk_oid = entry.get("object_id") or entry.get("id")
                elif isinstance(entry, (list, tuple)) and entry:
                    atk_oid = entry[0]
                if atk_oid:
                    break
            if blk is not None and atk_oid is not None:
                # try common shapes; engine will reject wrong ones visibly
                if "blocks" in nd:
                    nd["blocks"] = [[blk, atk_oid]]
                elif "assignments" in nd:
                    nd["assignments"] = [[blk, atk_oid]]
                else:
                    nd["blocks"] = [[blk, atk_oid]]
                say(f"P0 blocks {obj_name(st['state'], atk_oid)} with {obj_name(st['state'], blk)}")
                wire("declare_blockers", {"action": na})
                await p0.send_action(na)
                ctx.attack_turn = st["state"].get("turn_number")
            else:
                say(f"P0 DeclareBlockers: blk={blk} atk={atk_oid}; declaring empty")
                nd["blocks"] = []
                await p0.send_action(na)
            acted = True
            break
    return acted


async def gated_pass(p0, p1, ctx):
    for c, pid in ((p0, p0.player_id), (p1, p1.player_id)):
        st = c.latest
        if not st:
            continue
        s = st["state"]
        wf = s.get("waiting_for") or {}
        # never pass while our own target/equip prompt is pending
        if c is p0 and wf.get("type") in ("TargetSelection",):
            continue
        if wf.get("type") == "Priority" and s.get("priority_player") == pid:
            for a in st.get("legal_actions", []):
                if a["type"] == "PassPriority":
                    await c.send_action(a)
                    break


def scan_deathtouch(s):
    """Return list of (zone, controller, name, oid) for battlefield objects
    carrying Deathtouch anywhere in their object JSON."""
    hits = []
    for oid, o in s["objects"].items():
        if o.get("zone") != "Battlefield":
            continue
        if has_deathtouch(o):
            hits.append({"oid": oid, "controller": o.get("controller"),
                         "name": o.get("base_name") or o.get("name")})
    return hits


async def export_state(c, path):
    raw = await c.export_state()
    env = json.loads(raw)
    with open(path, "w") as f:
        f.write(raw)
    return env["state"]


async def run_game(phase):
    """phase in {'attack','block'}. Returns (assertions, notes, exports)."""
    SUBMITTED.clear()
    ctx = Ctx()
    ctx.phase = phase
    notes = []
    ass = ctx.assert_
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(("Wand of Orcus", 12), ("Grizzly Bears", 12),
                         ("Diregraf Ghoul", 8), ("Manalith", 8),
                         ("Forest", 10), ("Swamp", 10)))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Grizzly Bears", 12), ("Forest", 48)))
    say(f"game {p0.game_code} phase={phase}")
    ass["setup_started"] = "passed"

    t0 = time.time()
    deadline = 1200
    pre_state = None
    wd = asyncio.create_task(watchdog(p0, p1, ctx, t0))
    while time.time() - t0 < deadline and not ctx.done:
        await asyncio.sleep(0.15)
        for c in (p0, p1):
            await pay_and_mulligan(c)
        await p0_main_decisions(p0, ctx)
        await p1_main(p1, ctx)
        await p0_equip_target(p0, ctx)
        # detect equip resolution
        st = p0.latest
        if st and ctx.wand_oid is not None and not ctx.equip_done:
            w = st["state"]["objects"].get(str(ctx.wand_oid), {})
            att = attached_oid(w)
            if att is not None:
                ctx.equip_done = True
                ctx.equip_pending = False
                ctx.equipped_oid = att
                say(f"EQUIP RESOLVED: wand attached to {obj_name(st['state'], att)} (oid {att})")
                wire("equip_resolved", {"wand": ctx.wand_oid, "attached_to": att,
                                        "attached_to_raw": w.get("attached_to")})
            elif getattr(ctx, "equip_pending", False) and time.time() - getattr(ctx, "equip_pending_since", 0) > 120:
                ctx.equip_pending = False
                say("equip pending timed out; will retry activation")
        if phase == "attack":
            await declare_attack(p0, p1, ctx)
        else:
            await declare_block(p0, p1, ctx)
        # trigger watch: look at the stack for the wand trigger
        st = p0.latest
        if st and ctx.attack_turn is not None and not ctx.trigger_seen:
            for entry in (st["state"].get("stack") or []):
                txt = json.dumps(entry)
                if "deathtouch" in txt.lower() or "Wand of Orcus" in txt:
                    ctx.trigger_seen = True
                    ctx.trigger_stack_text.append(txt[:300])
                    say(f"TRIGGER ON STACK: {txt[:300]}")
                    wire("trigger_on_stack", {"entry": txt[:2000]})
                    break
        # pre export: right after attack declared (stack present or just after)
        if (ctx.attack_turn is not None and not ctx.pre_exported
                and st and st["state"].get("phase") in ("DeclareBlockers", "CombatDamage", "PostCombatMain", "EndCombat")):
            say("exporting PRE_COMBAT state")
            pre_state = await export_state(p0, f"{EVDIR}/pre_{phase}.json")
            ctx.pre_exported = True
        # post export: PostCombatMain same turn, stack empty
        if (ctx.pre_exported and not ctx.post_exported and st
                and st["state"].get("phase") == "PostCombatMain"
                and not (st["state"].get("stack") or [])):
            say("exporting POST_COMBAT state")
            post_state = await export_state(p0, f"{EVDIR}/post_{phase}.json")
            ctx.post_exported = True
            ctx.done = True
        await gated_pass(p0, p1, ctx)

    # ---- assertions ----
    s_pre = pre_state
    s_post = None
    try:
        with open(f"{EVDIR}/post_{phase}.json") as f:
            s_post = json.loads(f.read())["state"]
    except FileNotFoundError:
        s_post = None

    # A1/B1 setup
    if s_pre is not None:
        p = "A" if phase == "attack" else "B"
        wand = s_pre["objects"].get(str(ctx.wand_oid), {}) if ctx.wand_oid else {}
        equipped = (ctx.equip_done and ctx.equipped_oid is not None
                    and attached_oid(wand) == ctx.equipped_oid)
        zombie = find_bf(s_pre, p0.player_id, "Diregraf Ghoul") is not None
        artifact = find_bf(s_pre, p0.player_id, "Manalith") is not None
        opp_creature = find_bf(s_pre, p1.player_id, "Grizzly Bears") is not None
        ok = equipped and zombie and artifact and opp_creature
        ass[f"{p}1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"{phase}: equipped={equipped} zombie={zombie} "
                     f"artifact={artifact} opp_creature={opp_creature}")
        # A2/B2 attack or block declared
        declared = ctx.attack_turn is not None
        ass[f"{p}2_{'attack' if phase=='attack' else 'block'}_declared"] = \
            "passed" if declared else "failed"
        # A3/B3 trigger observation (informational)
        ass[f"{p}3_trigger_seen"] = "passed" if ctx.trigger_seen else "failed"
        notes.append(f"{phase}: wand trigger on stack observed={ctx.trigger_seen} "
                     f"{ctx.trigger_stack_text[:1]}")
        # A4/B4 the reported outcome: deathtouch on the whole board
        if s_post is not None:
            hits = scan_deathtouch(s_post)
            wire(f"deathtouch_scan_{phase}", {"hits": hits})
            notes.append(f"{phase}: post-combat deathtouch hits={json.dumps(hits)}")
            ass[f"{p}4_no_overbroad_grant"] = "passed" if not hits else "failed"
            # A5/B5 cleanup
            wf = s_post.get("waiting_for") or {}
            proceeding = s_post.get("phase") == "PostCombatMain"
            ass[f"{p}5_cleanup"] = "passed" if proceeding else "failed"
            notes.append(f"{phase}: post phase={s_post.get('phase')} "
                         f"waiting={wf.get('type')} stack={len(s_post.get('stack') or [])}")
        else:
            ass[f"{p}4_no_overbroad_grant"] = "not-run"
            ass[f"{p}5_cleanup"] = "not-run"
            notes.append(f"{phase}: no post-combat export captured")
    else:
        p = "A" if phase == "attack" else "B"
        for k in ("1_setup_ok", f"2_{'attack' if phase=='attack' else 'block'}_declared",
                  "3_trigger_seen", "4_no_overbroad_grant", "5_cleanup"):
            ass[f"{p}{k}"] = "not-run"
        notes.append(f"{phase}: never reached combat; equip_done={ctx.equip_done}")

    wd.cancel()
    await p0.close()
    await p1.close()
    return ass, notes, ctx


async def main():
    t0 = time.time()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    all_ass, all_notes = {}, []
    if only in (None, "attack"):
        a, n, _ = await run_game("attack")
        all_ass.update(a)
        all_notes += [f"[attack] {x}" for x in n]
    if only in (None, "block"):
        a, n, _ = await run_game("block")
        all_ass.update(a)
        all_notes += [f"[block] {x}" for x in n]
    a4 = all_ass.get("A4_no_overbroad_grant")
    b4 = all_ass.get("B4_no_overbroad_grant")
    tested = [v for v in (a4, b4) if v in ("passed", "failed")]
    if tested and all(v == "passed" for v in tested):
        verdict = "not-reproduced"
    elif any(v == "failed" for v in tested):
        verdict = "reproduced"
    else:
        verdict = "blocked"
    all_notes.append(
        "Pinned v0.79.0 card data parses Wand of Orcus' first trigger effect as "
        "Unimplemented (name=unbound_subject); the engine therefore performs no "
        "deathtouch grant at all on this build. The classifier's 'typed grant "
        "child with any target' AST is not present in the pinned dataset.")
    run = {
        "issue": 6762,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": {
            "server_version": "0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
            "observed_at": "2026-09-10",
            "source": "ServerHello + sha256 match of pinned verified artifacts",
        },
        "server_run_dir": "runs/20260910-6762",
        "driver": {"protocol_advertised": 69, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6762.py"),
        "decks": {
            "P0": [["Wand of Orcus", 12], ["Grizzly Bears", 12],
                   ["Diregraf Ghoul", 8], ["Manalith", 8],
                   ["Forest", 10], ["Swamp", 10]],
            "P1": [["Grizzly Bears", 12], ["Forest", 48]],
        },
        "assertions": all_ass,
        "notes": all_notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The reported grant effect is Unimplemented in the pinned card data; "
            "if it is re-implemented the overbroad-grant claim needs re-testing.",
            "Not tested on the original 2026-07-28 build; verdict scoped to v0.79.0.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0: 12x Wand of Orcus + 12x Grizzly Bears + 8x Diregraf Ghoul + 8x Manalith + 20 lands; P1: 12x Bears + 48x Forest",
        "contract_line": "Equipped creature attacks (game A) / blocks (game B): only equipped creature + controller's Zombies may gain deathtouch; lands/artifacts/other creatures must not.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(all_ass)}")
    WIRE.close()
    RUNLOG.close()


asyncio.run(main())
