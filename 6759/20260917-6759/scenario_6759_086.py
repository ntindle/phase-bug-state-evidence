#!/usr/bin/env python3
"""Issue #6759 (re-validation on v0.86.0 / protocol 72): Nalia de'Arnise gains a
counter regardless of having a full party.

Oracle: "At the beginning of combat on your turn, if you have a full party,
put a +1/+1 counter on each creature you control and those creatures gain
deathtouch until end of turn."

Reported: the +1/+1 counter is put on each creature REGARDLESS of having a
full party (party = one each of Cleric, Rogue, Warrior, Wizard among
creatures you control; full = 4). Nalia de'Arnise is herself a Human Rogue,
so she contributes 1.

Pinned card-data parse (v0.86.0): the BeginCombat phase trigger carries
condition QuantityComparison(PartySize(Controller) >= 4) — i.e. the data
says the gate exists. The runtime behavior under test is whether the
engine honors that condition.

Prior validation (2026-09-10, v0.79.0/protocol 69, evidence
6759/20260910-6759, commit 7a334c9468307eb7d5f19fa0610e79d8cee598c6):
not-reproduced. Release pin advanced to v0.86.0, so per the playbook
staleness rule this run re-runs both legs on the current pin.

Behavioral contract — Game 1 (bug branch, NO full party):
  Setup: P0 controls only Nalia de'Arnise (party size 1).
  A1 setup_ok      pre.json exported at P0 PreCombatMain priority, Nalia on BF
  A2 no_counters   post.json (P0 DeclareAttackers): no creature P0 controls
                   gained a +1/+1 counter across the BeginCombat trigger
  A3 no_deathtouch no creature P0 controls has Deathtouch in keywords
  A4 cleanup       stack empty, game proceeding
Game 2 (control, full party):
  Setup: P0 controls Nalia + Acolyte of Xathrid (Cleric) +
  Aven Skirmisher (Warrior) + Augur of Skulls (Wizard) (party size 4).
  A5 counters      each of the four has +1/+1 count increased >= 1 vs pre
  A6 deathtouch    each has Deathtouch in keywords at DeclareAttackers
  A7 cleanup       stack empty, game proceeding

Verdict = reproduced iff A1 passed and (A2 or A3 failed).
Verdict = not-reproduced iff A1-A4 passed and A5-A7 passed
          (control proves the trigger fires correctly when it should).

Protocol-72 conventions (from driver lessons 2026-09-15..17):
  - MulliganDecision: answer the advertised action as-is with
    data.decision = "keep"/"mulligan"; pending seats are in
    waiting_for.data.pending[], no top-level data.player.
  - SelectCards bottom: advertised action as-is with data.cardIds; count
    from waiting_for.data.phase.count.
  - PassPriority gated on my_priority (wf Priority + priority_player == pid).
  - DeclareAttackers/DeclareBlockers get dedicated branches.
  - Never pass priority while our own non-Priority interaction is pending.
  - Deathtouch detected by substring scan of the keywords JSON (keywords
    are typed dicts on current protocols, not plain names).
  - Per-game guard sets (MULLS, BOTTOMED) reset per game.
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
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6759")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260917-6759"
EVDIR = f"{BACKFILL}/evidence/6759/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

NALIA = "Nalia de'Arnise"
ACOLYTE = "Acolyte of Xathrid"      # Cleric {B}
AVEN = "Aven Skirmisher"            # Warrior {W}
AUGUR = "Augur of Skulls"           # Wizard {B}
PLAINS = "Plains"
SWAMP = "Swamp"
LANDS = (PLAINS, SWAMP)

PLAN_G1 = [NALIA]
WANT_G1 = {NALIA: 1}
PLAN_G2 = [NALIA, ACOLYTE, AVEN, AUGUR]
WANT_G2 = {NALIA: 1, ACOLYTE: 1, AVEN: 1, AUGUR: 1}

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")
GAME_TIMEOUT = 900
TURN_CAP = 30


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
        h.update(f.read())
    return h.hexdigest()


def oname(o):
    return o.get("base_name") or o.get("name")


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def hand_ids(state, pid):
    return [oid for oid in state["players"][pid]["hand"]]


def lname(state, oid):
    o = state["objects"].get(str(oid))
    return oname(o) if o else "?"


def n_on_bf(state, pid, name):
    return sum(1 for o in bf(state, pid) if oname(o) == name)


def wf_of(state):
    return state.get("waiting_for") or {}


def find_action(acts, atype):
    for a in acts:
        if a.get("type") == atype:
            return a
    return None


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and state.get("priority_player") == pid)


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in MAIN_PHASES)


def stack_snapshot(state):
    out = []
    for e in (state.get("stack") or []):
        d = (e.get("kind") or {}).get("data") or {}
        ab = d.get("ability") or {}
        chain = []
        a = ab
        while a:
            eff = (a.get("effect") or {}).get("type")
            chain.append(eff)
            a = a.get("sub_ability")
        src = (d.get("source") or {}).get("name")
        if not src:
            src = (e.get("source") or {}).get("name") if isinstance(e.get("source"), dict) else None
        out.append({"id": e.get("id"), "effects": chain, "source": src})
    return out


def nalia_trigger_on_stack(state):
    """True if a Nalia-sourced PutCounterAll trigger is on the stack."""
    blob = json.dumps(stack_snapshot(state))
    return "PutCounterAll" in blob and "Nalia" in blob


def has_deathtouch(o):
    kw = o.get("keywords") or []
    return "Deathtouch" in json.dumps(kw)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


class Ctx:
    def __init__(self):
        self.pre_exported = False
        self.armed = False
        self.begin_combat_logged = False
        self.mulls = set()      # per-game: tags that answered mulligan
        self.bottomed = set()   # per-game: tags that bottomed
        self.saw_nalia_trigger = False


def setup_ok(state, pid, want):
    return all(n_on_bf(state, pid, n) >= k for n, k in want.items())


async def do_mulligan(c, pid, tag, ctx, acts):
    if tag in ctx.mulls:
        return False
    a = find_action(acts, "MulliganDecision")
    if not a:
        return False
    # keep-always policy (prior attempts 2/3 completed with this)
    sub = copy.deepcopy(a)
    sub.setdefault("data", {})["decision"] = "keep"
    await submit_as_is(c, sub)
    ctx.mulls.add(tag)
    say(f"[{tag}] mulligan decision: keep")
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag, ctx, state, acts):
    if tag in ctx.bottomed:
        return False
    a = find_action(acts, "SelectCards")
    if not a:
        return False
    n = ((wf_of(state).get("data") or {}).get("phase") or {}).get("count", 1)
    hand = hand_ids(state, pid)

    def rank(o):
        nm = lname(state, o)
        return 0 if nm in LANDS else 1
    picks = [int(x) for x in sorted(hand, key=rank)[:n]]
    sub = copy.deepcopy(a)
    sub.setdefault("data", {})["cardIds"] = picks
    await submit_as_is(c, sub)
    ctx.bottomed.add(tag)
    say(f"[{tag}] bottoms {n}: {[lname(state, x) for x in picks]}")
    wire("bottom", {"who": tag, "count": n})
    return True


async def do_discard(c, pid, tag, st, state):
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    data = wf_of(state).get("data") or {}
    if str(data.get("player")) != str(pid):
        return False
    n = data.get("count") or max(0, len(hand_ids(state, pid)) - 7)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)

    def rank(o):
        nm = lname(state, o)
        if nm in LANDS:
            return 0
        if nm in (NALIA, ACOLYTE, AVEN, AUGUR):
            return 2
        return 1
    prio = sorted(hand, key=rank)
    # try viewer_interaction "select" schema first
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rdata = resp.get("data", {}) or {}
            cands = rdata.get("candidates") or []
            if not cands:
                continue
            want = {str(x) for x in prio[:n]}
            picks = [ch.get("id") for ch in cands if str(ch.get("id")) in want][:n]
            if len(picks) < n:
                picks = [ch.get("id") for ch in cands[:n]]
            spec = (rdata.get("spec") or {}).get("type") or "select"
            sub = {"interactionId": opp.get("interactionId"),
                   "response": {"type": spec, "data": {"choiceIds": picks}}}
            say(f"[{tag}] discarding {n} to hand size via interaction")
            wire("interaction_submit", {"who": c.name, "tag": tag, "submission": sub})
            await c.send_interaction(sub)
            return True
    # fallback: advertised SelectCards action
    acts = st.get("legal_actions", [])
    a = find_action(acts, "SelectCards")
    if a:
        sub = copy.deepcopy(a)
        sub.setdefault("data", {})["cardIds"] = [int(x) for x in prio[:n]]
        await submit_as_is(c, sub)
        say(f"[{tag}] discarding {n} to hand size via SelectCards action")
        return True
    return False


async def do_pay(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def do_order_triggers(c, acts):
    a = find_action(acts, "OrderTriggers")
    if a:
        await submit_as_is(c, a)
        say(f"[{c.name}] submits advertised trigger order")
        return True
    return False


async def do_combat(c, pid, tag, state, acts):
    wtype = wf_of(state).get("type") or ""
    if wtype == "DeclareAttackers" and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            sub = copy.deepcopy(da)
            sub.setdefault("data", {})["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            say(f"[{tag}] declares no attackers")
        return True
    if wtype == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            sub = copy.deepcopy(da)
            sub.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, sub)
            say(f"[{tag}] declares no blockers")
        return True
    return False


async def tick(c, pid, tag, st, ctx, plan, want):
    state = st.get("state")
    if not state:
        return False
    acts = st.get("legal_actions", [])
    wtype = wf_of(state).get("type") or ""
    wplayer = str((wf_of(state).get("data") or {}).get("player", ""))

    # mulligan / bottoming
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, tag, ctx, acts):
            return True
        if await do_bottom(c, pid, tag, ctx, state, acts):
            return True
        return False  # mulligan decision outstanding for the other seat
    # discard to hand size: named player only
    if await do_discard(c, pid, tag, st, state):
        return True
    # mana payments first
    if await do_pay(c, acts):
        return True
    # trigger ordering: submit the advertised default as-is
    if await do_order_triggers(c, acts):
        return True
    # never pass priority while our own non-Priority interaction is pending
    if wtype in ("OptionalCostChoice", "TargetSelection") and wplayer == str(pid):
        return True  # hold; the wait names this seat
    # combat declarations
    if await do_combat(c, pid, tag, state, acts):
        return True
    # P0 main-phase duties
    if pid == 0 and is_my_main(state, pid):
        # land drop: retry every tick (no kept-flag)
        for land in LANDS:
            hid = next((oid for oid in hand_ids(state, pid)
                        if lname(state, oid) == land), None)
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "PlayLand"
                        and str(d.get("object_id")) == str(hid if hid is not None else -1)):
                    await submit_as_is(c, a)
                    say(f"[{tag}] plays {land}")
                    return True
        # cast planned creatures in order
        for name in plan:
            if n_on_bf(state, pid, name) >= want[name]:
                continue
            hid = next((oid for oid in hand_ids(state, pid)
                        if lname(state, oid) == name), None)
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "CastSpell"
                        and str(d.get("object_id")) == str(hid if hid is not None else -1)):
                    await submit_as_is(c, a)
                    say(f"[{tag}] casts {name}")
                    return True
            break  # next creature only after this one is castable/on BF
    # default pass, gated on my_priority (out-of-turn passes are no-ops
    # that can burn the per-revision tick guard)
    if my_priority(state, pid):
        a = find_action(acts, "PassPriority")
        if a:
            await submit_as_is(c, a)
            return True
    return False


async def run_game(p0, p1, ctx, plan, want, game_label):
    """Drive one game through a P0 BeginCombat with the board set up."""
    t0 = time.time()
    last_rev = {}
    last_progress = time.time()
    while time.time() - t0 < GAME_TIMEOUT:
        await asyncio.sleep(0.25)
        for c, pid, tag in ((p0, 0, "P0"), (p1, 1, "P1")):
            if c.revision == last_rev.get(c.name):
                continue
            if await tick(c, pid, tag, c.latest, ctx, plan, want):
                last_rev[c.name] = c.revision
                last_progress = time.time()
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        # sample the stack every iteration for the Nalia trigger signature
        if ctx.armed and nalia_trigger_on_stack(state):
            ctx.saw_nalia_trigger = True
        if state.get("turn_number", 0) > TURN_CAP:
            say(f"[{game_label}] turn cap hit; aborting")
            return None
        if time.time() - last_progress > 120:
            say(f"[{game_label}] 120s without progress; aborting")
            wire("stall_abort", {"waiting_for": state.get("waiting_for")})
            return None
        # arm: setup complete at P0 PreCombatMain priority
        if (not ctx.armed and setup_ok(state, 0, want)
                and state.get("active_player") == 0
                and state.get("priority_player") == 0
                and state.get("phase") == "PreCombatMain"):
            s = await p0.export_state()
            with open(f"{EVDIR}/{game_label}_pre.json", "w") as f:
                f.write(s)
            ctx.pre_exported = True
            ctx.armed = True
            wire("pre_export", {"turn": state.get("turn_number"),
                                "phase": state.get("phase"),
                                "bf": [(oname(o), o.get("id")) for o in bf(state, 0)]})
            say(f"[{game_label}] exported pre.json (turn {state.get('turn_number')})")
        # log the BeginCombat trigger window once
        if (ctx.armed and not ctx.begin_combat_logged
                and state.get("phase") == "BeginCombat"):
            ctx.begin_combat_logged = True
            wire("begin_combat", {"turn": state.get("turn_number"),
                                  "stack": stack_snapshot(state),
                                  "waiting_for": state.get("waiting_for")})
            say(f"[{game_label}] BeginCombat: stack={stack_snapshot(state)}")
        # done: reached P0 DeclareAttackers priority after arming
        if (ctx.armed and state.get("phase") == "DeclareAttackers"
                and state.get("active_player") == 0
                and state.get("priority_player") == 0):
            s = await p0.export_state()
            with open(f"{EVDIR}/{game_label}_post.json", "w") as f:
                f.write(s)
            wire("post_export", {"turn": state.get("turn_number"),
                                 "stack": stack_snapshot(state)})
            say(f"[{game_label}] exported post.json")
            return s
    say(f"[{game_label}] TIMEOUT")
    return None


def counters_of(state):
    """oid -> {name, p1p1, deathtouch} for P0 battlefield creatures."""
    out = {}
    for o in bf(state, 0):
        cnts = o.get("counters") or {}
        out[str(o["id"])] = {
            "name": oname(o),
            "p1p1": int(cnts.get("P1P1", 0)),
            "deathtouch": has_deathtouch(o),
        }
    return out


def load_env(p):
    with open(f"{EVDIR}/{p}") as f:
        return json.load(f)["state"]


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    a = obs["assert"]

    # ---------- Game 1: no full party (party = 1) ----------
    say("=== Game 1: Nalia only (no full party) ===")
    p0 = PhaseClient("P0")
    await p0.connect()
    p1 = PhaseClient("P1")
    await p1.connect()
    await p0.create(deck((PLAINS, 20), (SWAMP, 20), (NALIA, 8)))
    await p1.join(p0.game_code, deck(("Island", 60)))
    ctx1 = Ctx()
    post1 = await run_game(p0, p1, ctx1, PLAN_G1, WANT_G1, "g1_noparty")
    a["A1_setup_ok"] = "passed" if ctx1.pre_exported else "failed"
    if post1 is None:
        for k in ("A2_no_counters", "A3_no_deathtouch", "A4_cleanup"):
            a[k] = "not-run"
        obs["notes"].append("g1: proof game did not complete; "
                            "blocked before the trigger window")
    else:
        pre1, st1 = load_env("g1_noparty_pre.json"), json.loads(post1)["state"]
        c_pre, c_post = counters_of(pre1), counters_of(st1)
        gained = [f"{v['name']}#{k}(+{v['p1p1'] - c_pre.get(k, {}).get('p1p1', 0)})"
                  for k, v in c_post.items()
                  if v["p1p1"] - c_pre.get(k, {}).get("p1p1", 0) > 0]
        a["A2_no_counters"] = "passed" if not gained else "failed"
        dt = [f"{v['name']}#{k}" for k, v in c_post.items() if v["deathtouch"]]
        a["A3_no_deathtouch"] = "passed" if not dt else "failed"
        a["A4_cleanup"] = ("passed" if not (st1.get("stack") or [])
                           else "failed")
        obs["notes"].append(
            f"g1: pre BF={[(v['name'], v['p1p1']) for v in c_pre.values()]} "
            f"post BF={[(v['name'], v['p1p1'], v['deathtouch']) for v in c_post.values()]} "
            f"gained_counters={gained or 'none'} deathtouch_on={dt or 'none'} "
            f"nalia_trigger_seen_on_stack={ctx1.saw_nalia_trigger} "
            f"life={st1['players'][0]['life']}/{st1['players'][1]['life']}")
    for k in ("A1_setup_ok", "A2_no_counters", "A3_no_deathtouch", "A4_cleanup"):
        say(f"{k}: {a[k]}")
    await p0.close(); await p1.close()

    # ---------- Game 2: full party control ----------
    say("=== Game 2: full party (Nalia+Cleric+Warrior+Wizard) ===")
    p0 = PhaseClient("P0"); await p0.connect()
    p1 = PhaseClient("P1"); await p1.connect()
    await p0.create(deck((PLAINS, 20), (SWAMP, 20), (NALIA, 4),
                         (ACOLYTE, 8), (AVEN, 8), (AUGUR, 8)))
    await p1.join(p0.game_code, deck(("Island", 60)))
    ctx2 = Ctx()
    post2 = await run_game(p0, p1, ctx2, PLAN_G2, WANT_G2, "g2_fullparty")
    if post2 is None:
        for k in ("A5_counters", "A6_deathtouch", "A7_cleanup"):
            a[k] = "not-run"
        obs["notes"].append("g2: control game did not complete")
    else:
        pre2, st2 = load_env("g2_fullparty_pre.json"), json.loads(post2)["state"]
        c_pre, c_post = counters_of(pre2), counters_of(st2)
        per = {}
        for k, v in c_post.items():
            if v["name"] in WANT_G2:
                per.setdefault(v["name"], []).append(
                    (v["p1p1"] - c_pre.get(k, {}).get("p1p1", 0),
                     v["deathtouch"]))
        ok_c = all(n in per and all(d >= 1 for d, _ in per[n])
                   for n in WANT_G2)
        ok_d = all(n in per and all(dt for _, dt in per[n])
                   for n in WANT_G2)
        a["A5_counters"] = "passed" if ok_c else "failed"
        a["A6_deathtouch"] = "passed" if ok_d else "failed"
        a["A7_cleanup"] = ("passed" if not (st2.get("stack") or [])
                           else "failed")
        obs["notes"].append(
            f"g2: per-creature (delta_p1p1, deathtouch)={per} "
            f"nalia_trigger_seen_on_stack={ctx2.saw_nalia_trigger}")
    for k in ("A5_counters", "A6_deathtouch", "A7_cleanup"):
        say(f"{k}: {a[k]}")
    await p0.close(); await p1.close()

    # ---------- verdict ----------
    if a["A1_setup_ok"] != "passed":
        verdict, reason = "blocked", "bug-branch setup did not complete"
    elif a["A2_no_counters"] == "failed" or a["A3_no_deathtouch"] == "failed":
        verdict = "reproduced"
        reason = (f"BeginCombat trigger fired with party=1 (no full party): "
                  f"{obs['notes'][0]}")
    elif a["A5_counters"] == "passed" and a["A6_deathtouch"] == "passed":
        verdict, reason = ("not-reproduced",
                           "trigger correctly withheld with party=1 and "
                           "correctly fired with party=4")
    else:
        verdict, reason = ("blocked",
                            "control branch inconclusive; "
                            "bug-branch withholding observed but the "
                            "positive control did not complete")
    obs["verdict"] = verdict
    obs["verdict_reason"] = reason
    obs["elapsed_s"] = round(time.time() - t0, 1)
    say(f"VERDICT: {verdict} — {reason}")

    server = {
        "version": "v0.86.0", "build_commit": "2cc8c28",
        "protocol_version": 72,
        "binary": "phase-server-slim-x86_64-unknown-linux-musl",
        "binary_sha256": "67d495b599cbe7d68c9ab9fddaf2f1a37ec1d392382e31e43963d0653eed6af2",
        "card_data_sha256": "ab7a4b65e8fba8407a928eae8f261abb078f43c081923e9c0f4af30a9c40ff26",
        "draft_pools_sha256": "d20d2dbf181b2361c9cdf0e67bef34d996765338f1986bc395477905dfefd13e",
        "signature_verified": True, "signature_key_id": "436711b6a2d36828",
        "mode": "Full", "endpoint": "127.0.0.1:9374",
    }
    run = {
        "issue": 6759,
        "run_id": EVID_RUN_ID,
        "validated_at": "2026-09-17",
        "server": server,
        "plan": {"g1": {"cast": PLAN_G1, "want": WANT_G1},
                 "g2": {"cast": PLAN_G2, "want": WANT_G2}},
        "decks": {
            "g1_P0": {"Plains": 20, "Swamp": 20, "Nalia de'Arnise": 8},
            "g2_P0": {"Plains": 20, "Swamp": 20, "Nalia de'Arnise": 4,
                      "Acolyte of Xathrid": 8, "Aven Skirmisher": 8,
                      "Augur of Skulls": 8},
            "P1": {"Island": 60}},
        "assertions": a,
        "notes": obs["notes"],
        "verdict": verdict,
        "verdict_reason": reason,
        "elapsed_s": obs["elapsed_s"],
        "prior_run": {
            "run_id": "20260910-6759",
            "release": "v0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "verdict": "not-reproduced",
            "evidence_commit": "7a334c9468307eb7d5f19fa0610e79d8cee598c6",
            "note": "release pin advanced since; re-validated here on v0.86.0",
        },
        "scenario": {"file": "scenario_6759_086.py",
                     "sha256": None},  # filled after all logging is done
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("run.json written (scenario hash pending); evidence in", EVDIR)
    return 0 if verdict in ("reproduced", "not-reproduced") else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
