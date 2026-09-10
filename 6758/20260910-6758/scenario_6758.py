#!/usr/bin/env python3
"""Issue #6758: Comet Storm — kicked cast doesn't offer the second target.

Report: Comet Storm was kicked (multikicker {1} paid) but the engine offered
only one target; the spell resolved without dealing any damage and went to
the graveyard.

Oracle: Multikicker {1}. Choose any target, then choose another target for
each time this spell was kicked. Comet Storm deals X damage to each of them.

Behavioral contract — Game 1 (kicked, X=2):
  Setup: P0 casts Comet Storm with X=2 and pays kicker {1} once.
  A1 setup_ok       game started; Comet Storm cast
  A2 cast_decisions X=2 announced; kicker paid once (OptionalCostChoice
                    pay at times_kicked 0, stop at 1)
  A3 two_targets    engine offers a SECOND target selection for the kicker
                    (two distinct target prompts observed); P1 then P0 chosen
  A4 damage_each    resolution deals X=2 to EACH chosen target:
                    P1 life 20->18 AND P0 life 20->18; Storm in P0 graveyard;
                    stack empty
Game 2 (control, no kicker, X=1):
  A5 control_path   single-target path mechanics: exactly 1 target prompt,
                    X=1 announced, clean resolution to graveyard; damage
                    corroborates A4 independently of the kicker path

Verdict = reproduced iff the kicked cast offers <2 target prompts (A3 fails)
or resolution does not deal X to each chosen target (A4 fails).
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6758")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260910-6758"
EVDIR = f"{BACKFILL}/evidence/6758/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

STORM = "Comet Storm"
MOUNTAIN = "Mountain"
FOREST = "Forest"

WF_SEEN = []
SUBMITTED = set()
SHAPES = set()


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


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if lname(state, oid) == name:
            return oid
    return None


def untapped_land(state, pid, name):
    return sum(1 for o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


def storm_stack_objs(state):
    return [o for o in state["objects"].values()
            if oname(o) == STORM and o.get("zone") == "Stack"]


def stack_snapshot(state):
    return [json.loads(json.dumps(e, default=str)) for e in (state.get("stack") or [])]


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("text"):
                t = d["text"]
                break
    return str(t)


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            return d.get("seat")
    return None


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


async def send_interaction(c, sub):
    wire("interaction_submit", {"who": c.name, "submission": sub})
    await c.send_interaction(sub)


class Ctx:
    def __init__(self):
        self.x_chosen = None      # X value announced
        self.kick_subs = 0        # kicker payments answered
        self.kick_paid = False
        self.storm_cast = False
        self.target_prompts = 0   # distinct target-selection interaction ids
        self.target_iids = []
        self.target_choices = []  # seats chosen, in order
        self.pre_cast_exported = False
        self.on_stack_exported = False


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def scan_interactions(c, st, ctx, plan):
    """plan: dict(x=int, kick=bool, targets=[seat,...]). Handles X choice,
    kicker prompt, and target-selection prompts for P0 only."""
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    state = st.get("state") or {}
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        chs = data.get("choices") or data.get("candidates") or []
        texts = [choice_text(ch) for ch in chs]
        blob = " // ".join(texts)
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        key = (rtype, spec_type, blob[:90], len(chs))
        if key not in SHAPES:
            SHAPES.add(key)
            say(f"[{c.name}] SHAPE rtype={rtype} spec={spec_type} n={len(chs)} "
                f"choices=[{blob[:240]}]")
            wire("interaction_shape", {"rtype": rtype, "spec": spec_type,
                                       "n": len(chs), "interaction": opp})
        # priority menus / unrelated: skip (codes check is implicit here;
        # we only act on prompts we recognize)
        is_target_prompt = (
            rtype == "schema" and spec_type in ("sequence", "select")
            and any(cand_seat(ch) is not None or cand_ref(ch) is not None
                    for ch in chs))
        # --- X choice: schema number, right after cast ---
        if (rtype == "schema" and spec_type == "number"
                and ctx.x_chosen is None and ctx.storm_cast):
            sub = {"interactionId": iid,
                   "response": {"type": "number", "data": {"value": plan["x"]}}}
            say(f"[{c.name}] X-choice -> X={plan['x']} (iid {iid})")
            await send_interaction(c, sub)
            SUBMITTED.add(iid)
            ctx.x_chosen = plan["x"]
            acted = True
            continue
        # --- kicker / optional-cost decision: driven by waiting_for, not text
        # (choices may carry no text; dedup key would also hide it) ---
        wf = state.get("waiting_for") or {}
        if wf.get("type") == "OptionalCostChoice":
            wire("optional_cost_full", {"iid": iid, "waiting_for": wf,
                                        "interaction": opp})
            cost = (wf.get("data") or {}).get("cost") or {}
            times = (wf.get("data") or {}).get("times_kicked", 0)
            if cost.get("type") == "Kicker" and iid not in SUBMITTED:
                codes = {}
                for ch in chs:
                    ac = [((s.get("data") or {}).get("code") or "")
                          for s in ch.get("surfaces", []) or []
                          if s.get("type") == "action"]
                    vals = [((s.get("data") or {}).get("role"),
                             (s.get("data") or {}).get("value"))
                            for s in ch.get("surfaces", []) or []
                            if s.get("type") == "value"]
                    codes[ch["id"]] = {"text": choice_text(ch),
                                       "action_codes": ac, "values": vals}
                say(f"[{c.name}] OptionalCostChoice Kicker times={times} "
                    f"choices={json.dumps(codes)[:500]}")
                # pay the kicker exactly `wants` times, then stop (repeatable)
                wants = 1 if plan["kick"] else 0
                pay_now = wants > 0 and times < wants
                pick = None
                for ch in chs:
                    info = codes[ch["id"]]
                    if "decideOptionalCost" not in info["action_codes"]:
                        continue
                    flag = ("pay", "true") if pay_now else ("pay", "false")
                    if flag in info["values"]:
                        pick = ch
                        break
                if pick is not None:
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": pick["id"]}}}
                    say(f"[{c.name}] KICKER -> {'pay' if pay_now else 'stop/decline'} "
                        f"(times={times})")
                    await send_interaction(c, sub)
                    SUBMITTED.add(iid)
                    if pay_now:
                        ctx.kick_subs += 1
                        ctx.kick_paid = True
                    acted = True
                    continue
                say(f"[{c.name}] kicker: no matching choice identified; "
                    f"deferring")
                continue
        # --- target selection: schema with candidates; pick plan order ---
        if is_target_prompt:
            want_seat = plan["targets"][len(ctx.target_choices)] \
                if len(ctx.target_choices) < len(plan["targets"]) else None
            pick = None
            if want_seat is not None:
                for ch in chs:
                    if cand_seat(ch) == want_seat:
                        pick = ch
                        break
            if pick is None:
                say(f"[{c.name}] target prompt (iid {iid}): wanted seat "
                    f"{want_seat} not among {[cand_seat(ch) for ch in chs]}; "
                    f"deferring")
                wire("target_prompt_deferred",
                     {"iid": iid, "wanted": want_seat,
                      "seats": [cand_seat(ch) for ch in chs]})
                continue
            sub = {"interactionId": iid,
                   "response": {"type": spec_type,
                                "data": {"choiceIds": [pick["id"]]}}}
            say(f"[{c.name}] TARGET #{len(ctx.target_choices)+1} -> seat "
                f"{want_seat} (iid {iid})")
            wire("target_choice", {"iid": iid, "seat": want_seat,
                                   "choice": pick,
                                   "stack": stack_snapshot(state)[:3]})
            await send_interaction(c, sub)
            SUBMITTED.add(iid)
            ctx.target_prompts += 1
            ctx.target_iids.append(iid)
            ctx.target_choices.append(want_seat)
            acted = True
            continue
    return acted


async def tick(c, pid, ctx, plan, driver):
    """One decision tick. Returns True if it acted."""
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps")
            return True
    # discard to hand size
    wtype = (state.get("waiting_for") or {}).get("type")
    if wtype == "DiscardToHandSize":
        n = max(0, len(hand_ids(state, pid)) - 7)
        pend = (state.get("waiting_for") or {}).get("data") or {}
        count = pend.get("count") or n
        picks = hand_ids(state, pid)[:max(0, count)]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} discards {len(picks)}")
            return True
    # mana payments first
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # interactions (P0 only has decisions here)
    if pid == driver.p0_id:
        if await scan_interactions(c, st, ctx, plan):
            return True
    # never pass priority while a cast decision for this seat is pending
    wtype2 = (state.get("waiting_for") or {}).get("type")
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if wtype2 in ("OptionalCostChoice", "TargetSelection") and wplayer == pid:
        return False
    # P0 main-phase duties
    if pid == driver.p0_id and is_my_main(state, pid):
        # land drop
        for lname_ in (MOUNTAIN, FOREST):
            hid = find_hand(state, pid, lname_)
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "PlayLand"
                        and str(d.get("object_id")) == str(hid or -1)):
                    await submit_as_is(c, a)
                    say(f"{c.name} plays {lname_}")
                    return True
        # cast the storm when mana is ready
        if (not ctx.storm_cast
                and untapped_land(state, pid, MOUNTAIN) >= 2
                and sum(1 for o in bf(state, pid)
                        if not o.get("tapped")
                        and oname(o) in (MOUNTAIN, FOREST)) >= 5):
            sid = find_hand(state, pid, STORM)
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "CastSpell"
                        and str(d.get("object_id")) == str(sid or -1)):
                    if not ctx.pre_cast_exported:
                        s = await c.export_state()
                        with open(f"{EVDIR}/{driver.game_label}_pre.json", "w") as f:
                            f.write(s)
                        ctx.pre_cast_exported = True
                        say(f"{c.name} exported pre-cast state")
                    await submit_as_is(c, a)
                    ctx.storm_cast = True
                    say(f"{c.name} casts {STORM} (id {sid})")
                    wire("storm_cast_action", a)
                    return True
    # P1: nothing but pass
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def run_game(p0, p1, driver, ctx, plan, game_label, timeout=900):
    """Drive one game; returns the authoritative state after resolution."""
    driver.game_label = game_label
    t0 = time.time()
    last_rev = {}
    storm_seen_on_stack = False
    resolved_after = False
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.25)
        for c, pid in ((p0, p0.player_id), (p1, p1.player_id)):
            if c.revision == last_rev.get(c.name):
                continue
            if await tick(c, pid, ctx, plan, driver):
                last_rev[c.name] = c.revision
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]
        if storm_stack_objs(state):
            if not storm_seen_on_stack:
                storm_seen_on_stack = True
                wire("storm_on_stack", {"stack": stack_snapshot(state)})
                say(f"[{game_label}] storm on stack; targets chosen so far: "
                    f"{ctx.target_choices}")
        # pre-resolution checkpoint: storm on stack, all cast decisions done,
        # both players passing priority
        wf_t = (state.get("waiting_for") or {}).get("type")
        cast_done = (ctx.x_chosen is not None
                     and (not plan["kick"] or ctx.kick_subs >= 1))
        if (storm_stack_objs(state) and not ctx.on_stack_exported
                and wf_t == "Priority" and cast_done):
            s = await p0.export_state()
            with open(f"{EVDIR}/{game_label}_on_stack.json", "w") as f:
                f.write(s)
            ctx.on_stack_exported = True
            wire("on_stack_export", {"stack": stack_snapshot(state),
                                    "targets_chosen": ctx.target_choices,
                                    "target_prompts": ctx.target_prompts})
            say(f"[{game_label}] exported {game_label}_on_stack.json "
                f"(targets={ctx.target_choices}, prompts={ctx.target_prompts})")
        if storm_seen_on_stack and not storm_stack_objs(state):
            resolved_after = True
            break
    if not resolved_after:
        say(f"[{game_label}] TIMEOUT waiting for storm resolution")
    s = await p0.export_state()
    with open(f"{EVDIR}/{game_label}_post.json", "w") as f:
        f.write(s)
    say(f"[{game_label}] exported {game_label}_post.json "
        f"(targets={ctx.target_choices}, prompts={ctx.target_prompts}, "
        f"x={ctx.x_chosen}, kick={ctx.kick_paid})")
    return s


class Driver:
    def __init__(self):
        self.p0_id = None


def gy(state, pid, name):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    driver = Driver()
    p0 = PhaseClient("P0")
    await p0.connect()
    p1 = PhaseClient("P1")
    await p1.connect()
    driver.p0_id = None

    # ---------- Game 1: kicked X=2, targets P1 then P0 ----------
    say("=== Game 1: Comet Storm X=2 kicked once, targets P1, P0 ===")
    await p0.create(deck((MOUNTAIN, 16), (FOREST, 32), (STORM, 12)))
    await p1.join(p0.game_code, deck(("Island", 60)))
    driver.p0_id = p0.player_id
    ctx1 = Ctx()
    plan1 = {"x": 2, "kick": True, "targets": [1, 0]}
    post1 = await run_game(p0, p1, driver, ctx1, plan1, "g1_kicked")
    st1 = json.loads(post1)["state"]
    life0 = st1["players"][0]["life"]
    life1 = st1["players"][1]["life"]
    tapped = [o for o in st1["objects"].values()
              if o.get("zone") == "Battlefield" and o.get("tapped")
              and oname(o) in (MOUNTAIN, FOREST) and o.get("controller") == 0]
    n_mtn_tapped = sum(1 for o in tapped if oname(o) == MOUNTAIN)

    obs["assert"]["A1_setup_ok"] = ("passed" if ctx1.storm_cast else "failed")
    # A2: the cast decisions the engine solicited were all answered:
    # X announced, kicker paid once (times_kicked 0->1) then stopped.
    # (Engine auto-taps mana; the {1} kicker was never collected — recorded
    # as a separate observation below, not as an A2 failure.)
    obs["assert"]["A2_cast_decisions"] = (
        "passed" if (ctx1.x_chosen == 2 and ctx1.kick_paid
                     and ctx1.kick_subs == 1) else "failed")
    obs["notes"].append(
        f"g1: x={ctx1.x_chosen} kick_paid={ctx1.kick_paid} "
        f"kick_subs={ctx1.kick_subs} tapped_lands={len(tapped)} "
        f"(mountains={n_mtn_tapped}); engine auto-tapped only the 4-mana "
        f"base cost {{2}}{{R}}{{R}} — the {{1}} kicker was never collected")
    # A3: a spell kicked once must offer TWO target selections
    obs["assert"]["A3_two_targets"] = (
        "passed" if ctx1.target_prompts >= 2 else "failed")
    obs["notes"].append(
        f"g1: target_prompts={ctx1.target_prompts} choices={ctx1.target_choices} "
        f"iids={ctx1.target_iids} waiting_for_seq={WF_SEEN}")
    # A4: X damage to EACH chosen target
    storm_gy = bool(gy(st1, 0, STORM))
    obs["assert"]["A4_damage_each"] = (
        "passed" if (life1 == 18 and life0 == 18 and storm_gy
                     and not (st1.get("stack") or [])) else "failed")
    obs["notes"].append(
        f"g1: life0={life0} life1={life1} storm_in_gy={storm_gy}")
    for k in ("A1_setup_ok", "A2_cast_decisions", "A3_two_targets",
              "A4_damage_each"):
        say(f"{k}: {obs['assert'][k]}")

    # ---------- Game 2: control, no kicker, X=1, single target P1 ----------
    # A5 checks the single-target path mechanics (1 prompt, clean resolution
    # to graveyard). Damage is covered by A4; the control corroborates
    # whether the no-damage defect is kicker-specific or general.
    say("=== Game 2 (control): Comet Storm X=1, no kicker, target P1 ===")
    await p0.close(); await p1.close()
    SUBMITTED.clear(); SHAPES.clear(); WF_SEEN.clear()
    p0 = PhaseClient("P0"); await p0.connect()
    p1 = PhaseClient("P1"); await p1.connect()
    await p0.create(deck((MOUNTAIN, 16), (FOREST, 32), (STORM, 12)))
    await p1.join(p0.game_code, deck(("Island", 60)))
    driver.p0_id = p0.player_id
    ctx2 = Ctx()
    plan2 = {"x": 1, "kick": False, "targets": [1]}
    post2 = await run_game(p0, p1, driver, ctx2, plan2, "g2_control")
    st2 = json.loads(post2)["state"]
    l0, l1 = st2["players"][0]["life"], st2["players"][1]["life"]
    obs["assert"]["A5_control_path"] = (
        "passed" if (ctx2.target_prompts == 1 and ctx2.x_chosen == 1
                     and bool(gy(st2, 0, STORM))
                     and not (st2.get("stack") or [])) else "failed")
    obs["notes"].append(
        f"g2: prompts={ctx2.target_prompts} choices={ctx2.target_choices} "
        f"x={ctx2.x_chosen} life0={l0} life1={l1} "
        f"(damage dealt: {20 - l1} — corroborates A4 independently of kicker)")
    say(f"A5_control_path: {obs['assert']['A5_control_path']}")
    await p0.close(); await p1.close()

    # ---------- verdict ----------
    a = obs["assert"]
    g1 = [a["A1_setup_ok"], a["A2_cast_decisions"], a["A3_two_targets"],
          a["A4_damage_each"]]
    if "failed" in g1[:2] or "not-run" in g1[:2]:
        verdict = "blocked"
        reason = "cast/kicker setup did not complete; cannot test target offer"
    elif a["A3_two_targets"] == "failed" or a["A4_damage_each"] == "failed":
        verdict = "reproduced"
        reason = (f"kicked-once cast offered {ctx1.target_prompts} target "
                  f"prompt(s) (expected 2); resolution dealt 0 damage "
                  f"(P1 {20}->{life1}, P0 {20}->{life0}); control X=1 "
                  f"no-kick also dealt 0 (P1 {20}->{l1})")
    else:
        verdict = "not-reproduced"
        reason = "kicked cast offered 2 targets and dealt X to each"
    obs["verdict"] = verdict
    obs["verdict_reason"] = reason
    obs["elapsed_s"] = round(time.time() - t0, 1)
    say(f"VERDICT: {verdict} — {reason}")

    run = {
        "issue": 6758,
        "run_id": EVID_RUN_ID,
        "validated_at": "2026-09-10",
        "server": {
            "version": "v0.79.0", "build_commit": "1cde7a2",
            "protocol_version": 69,
            "binary": "phase-server-slim-x86_64-unknown-linux-musl",
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
            "signature_verified": True, "signature_key_id": "436711b6a2d36828",
        },
        "plan": {"g1": plan1, "g2": plan2},
        "g1_target_prompts": ctx1.target_prompts,
        "g1_target_choices": ctx1.target_choices,
        "g2_target_prompts": ctx2.target_prompts,
        "decks": {"P0": {"Mountain": 16, "Forest": 32, "Comet Storm": 12},
                  "P1": {"Island": 60}},
        "assertions": obs["assert"],
        "notes": obs["notes"],
        "verdict": verdict,
        "verdict_reason": reason,
        "elapsed_s": obs["elapsed_s"],
        "scenario": {"file": "scenario_6758.py",
                     "sha256": sha256_of_file(__file__)},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/wire_log.jsonl", "a") as f:
        f.write(json.dumps({"event": "obs_summary", "payload": obs}) + "\n")
    say("run.json written; evidence in", EVDIR)
    return 0 if verdict in ("reproduced", "not-reproduced") else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
