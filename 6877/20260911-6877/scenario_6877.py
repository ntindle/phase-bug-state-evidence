#!/usr/bin/env python3
"""Issue #6877: Krark, the Thumbless - multiple triggers; win-after-loss.

Reported (Discord): "[[Krark, the Thumbless]] when there are multiple Krark
triggers on the stack, if one trigger returns the spell to hand, all the rest
after have no effect regardless if the flip is won or not."

Oracle: "Whenever you cast an instant or sorcery spell, flip a coin. If you
lose the flip, return that spell to its owner's hand. If you win the flip,
copy that spell, and you may choose new targets for the copy."

Official ruling (Gatherer 11/10/2020): "If you win the flip, but the spell
that caused Krark's triggered ability to trigger isn't on the stack anymore
(most likely because it was countered), the copy is still created."

Plan (native engine, v0.80.0 / protocol 69, two human-client seats):
  P0: 4x Krark, the Thumbless + 4x Mirror Box + 12x Lightning Bolt + 20x Mountain.
      Mirror Box suspends the legend rule so two Krarks coexist; every Bolt
      cast then puts two Krark triggers on the stack.
  P1: passive (40x Mountain; land drops only).
  Coin flips are engine-automatic (no player interaction; verified by probe:
  the trigger resolves with only passPriority opportunities). Each trigger's
  flip is therefore observed via its effects:
    win  -> a Bolt copy appears on the stack (new bolt-like spell entry)
    loss -> the Bolt leaves the stack with no copy (returns to hand)
  For the FIRST-resolving trigger the spell is always on the stack, so its
  outcome is unambiguous. Trials where the first trigger demonstrably LOST
  (Bolt -> hand, no copy) form the test set: per the ruling the second
  trigger's wins must still create copies; per the bug report they never do.

Behavioral contract:
  A1 setup_ok            pre_trial_0: 2x Krark + Mirror Box on P0 BF, Bolt in
                         P0 hand, P0 PreCombatMain, stack empty
  A2 two_triggers        every trial began with exactly 2 Krark FlipCoin
                         triggers above the Bolt on the stack
  A3 firstloss_set       >=6 trials where the first-resolving trigger lost
                         (Bolt left the stack, no copy)
  A4 win_after_loss_copies  in every first-loss trial, a second-trigger win
                         created a Bolt copy (== the reported outcome)
  A5 control_first_win   every first-resolving win created a copy (sanity:
                         wins copy while the spell is on the stack)
  A6 cleanup             stack empty, game not over

Verdict: reproduced iff A1+A2+A3 pass, A5 != failed, and A4 fails (zero
second-trigger copies across >=6 first-loss trials; P(0 | correct) = 2^-N).
not-reproduced iff A1..A6 all pass. blocked otherwise.
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
RUN_ID = "20260912-01"
ISSUE = "6877"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KRARK = "Krark, the Thumbless"
BOX = "Mirror Box"
BOLT = "Lightning Bolt"
MTN = "Mountain"
P0_DECK = [(KRARK, 4), (BOX, 4), (BOLT, 12), (MTN, 20)]
P1_DECK = [(MTN, 40)]

TIMEOUT = 1500
MAX_TRIALS = 12
MIN_FIRSTLOSS = 6
CAST_WATCHDOG = 60
TRIAL_IDLE_WATCHDOG = 240

ST = {}
OBS = {}
SUBMITTED = set()
MULLS = {}
SHAPES = set()
WF_SEEN = []
LAST_SUBMIT = {"iid": None}
C0 = None

TR = {}


def reset():
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False})
    OBS.clear()
    OBS.update({"cast_pending": None, "cast_pending_t0": None,
                "trials": [], "trials_done": 0,
                "trial_idle_t0": None, "krark_casts": 0, "box_casts": 0,
                "bolt_casts": 0, "copyretarget_answers": 0,
                "bolt_target_answers": 0, "rejections": []})
    SUBMITTED.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    LAST_SUBMIT.update({"iid": None})
    TR.clear()
    TR.update({"active": False, "i": -1, "pre_p1_life": None,
               "prev_ids": None, "prev_entries": {}, "prev_boltlike": set(),
               "resolutions": [], "coalesced": False})


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
    except Exception:
        pass


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def hand_count(state, pid):
    return len(hand_oids(state, pid))


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def perm_oids(state, pid, name):
    return [str(oid) for oid, o in bf(state, pid) if oname(o) == name]


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_player(state):
    return (state.get("waiting_for") or {}).get("data", {}).get("player")


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST["stage"]})


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
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST["stage"]})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
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
        ref, seat = None, None
        codes = []
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if "seat" in d:
                    seat = d["seat"]
            if s.get("type") == "action":
                codes.append((d.get("code") or "") if isinstance(d, dict)
                             else "")
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "action_codes": codes, "text": ch.get("text")})
    return out


def build_target_response(resp, want):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [want["choice_id"]]}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": want["choice_id"]}}
    return None


# ---- Krark-specific stack analysis ----
def kind_type(e):
    k = e.get("kind")
    return k.get("type") if isinstance(k, dict) else k


def is_krark_trigger(e):
    if kind_type(e) != "TriggeredAbility":
        return False
    return "flip a coin" in json.dumps(e, default=str).lower()


def is_boltlike(e):
    if not isinstance(e, dict):
        return False
    if kind_type(e) != "Spell":
        return False
    eff = (((e.get("kind") or {}).get("data") or {}).get("ability")
           or {}).get("effect") or {}
    return (eff.get("type") == "DealDamage"
            and (eff.get("amount") or {}).get("value") == 3)


def stack_entries(state):
    return state.get("stack") or []


def krark_triggers(state):
    return [e for e in stack_entries(state) if is_krark_trigger(e)]


def boltlike_ids(state):
    return set(str(e.get("id")) for e in stack_entries(state)
               if is_boltlike(e))


def stack_sig(state):
    return tuple((str(e.get("id")), kind_type(e))
                 for e in stack_entries(state))


async def handle_copy_retarget(c, state, acts, st):
    """MayChooseNewTargets for a Krark-created copy: keep existing targets
    (keepAllCopyTargets), so the copy still hits P1."""
    if wf_type(state) != "CopyRetarget" or wf_player(state) != 0:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        cands = candidate_info(opp, state)
        shape = ("CopyRetarget", resp.get("type"), len(cands))
        if shape not in SHAPES:
            SHAPES.add(shape)
            wire("copyretarget_prompt",
                 {"waiting_for": wf_data(state), "candidates": cands,
                  "opportunity": opp})
            say(f"[P0] CopyRetarget: rtype={resp.get('type')} "
                f"cands={[(x['choice_id'], x['action_codes'], x['seat']) for x in cands]}")
        want = next((x for x in cands
                     if "keepAllCopyTargets" in (x["action_codes"] or [])),
                    None)
        if want is None:
            want = next((x for x in cands if x.get("seat") == 1), None)
        if want is None:
            say("[P0] CopyRetarget: no keep/P1 choice; holding")
            return False
        rout = build_target_response(resp, want)
        if not rout:
            say("[P0] CopyRetarget: unexpected response shape; holding")
            return False
        await send_interaction(c, {"interactionId": iid, "response": rout})
        SUBMITTED.add(iid)
        OBS["copyretarget_answers"] += 1
        say(f"[P0] CopyRetarget answered: {want['choice_id']} "
            f"(keep targets)")
        return True
    return False


async def handle_bolt_target(c, state, acts, st):
    if wf_type(state) != "TargetSelection" or wf_player(state) != 0:
        return False
    if OBS["cast_pending"] is None:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        cands = candidate_info(opp, state)
        want = next((x for x in cands if x.get("seat") == 1), None)
        if want is None:
            continue
        shape = ("BoltTarget", resp.get("type"), len(cands))
        if shape not in SHAPES:
            SHAPES.add(shape)
            wire("bolt_target_prompt",
                 {"candidates": cands, "opportunity": opp})
        rout = build_target_response(resp, want)
        if not rout:
            continue
        await send_interaction(c, {"interactionId": iid, "response": rout})
        SUBMITTED.add(iid)
        OBS["bolt_target_answers"] += 1
        say(f"[P0] Bolt targets P1 (seat 1)")
        return True
    return False


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, MTN)
    if lid:
        for a in acts:
            if a["type"] == "PlayLand" and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])

    # --- mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            names = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
            if is_p0:
                keep_ok = MTN in names
            else:
                keep_ok = MTN in names
            choice = "Keep" if (keep_ok or MULLS[c.name] >= 2) else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            pending = wf_data(state).get("pending", [])
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
    if wf_type(state) == "DiscardToHandSize":
        pend = wf_data(state)
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            keep = {KRARK, BOX, BOLT}
            mtns = [o for o in h if oname(state["objects"][o]) == MTN]
            pref = mtns[5:]
            pref += [o for o in h if o not in pref
                     and oname(state["objects"][o]) not in keep]
            pref += [o for o in h if o not in pref]
            picks = pref[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}")
                return True
    # legend rule: submit advertised choice as-is (keeps first)
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"{c.name} legend-choice submitted as-is: {a['type']}")
            return True

    # --- generic attackers/blockers: always empty
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "DeclareBlockers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True

    # --- P0 decision prompts take precedence
    if is_p0:
        if await handle_bolt_target(c, state, acts, st):
            return True
        if await handle_copy_retarget(c, state, acts, st):
            return True
        # never pass priority while a P0 cast decision is pending
        if wf_type(state) in ("TargetSelection", "CopyRetarget") \
                and wf_player(state) == 0:
            return False
        # hold priority while waiting for the 2-trigger stack to appear
        if OBS["cast_pending"] is not None and not TR["active"]:
            return False

    # --- mana producers: submit advertised payments as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True

    # --- P0 main-phase plan
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await p0_land_drop(c, pid, state, acts):
                return True
            box_bf = perm_oids(state, pid, BOX)
            krark_bf = perm_oids(state, pid, KRARK)
            if not box_bf:
                box_hand = find_hand(state, pid, BOX)
                ca = castspell_advertised(acts, box_hand)
                if ca:
                    await submit_as_is(c, ca)
                    OBS["box_casts"] += 1
                    say("[P0] casts Mirror Box")
                    return True
            if len(krark_bf) < 2 and (box_bf or not krark_bf):
                kr_hand = find_hand(state, pid, KRARK)
                ca = castspell_advertised(acts, kr_hand)
                if ca:
                    await submit_as_is(c, ca)
                    OBS["krark_casts"] += 1
                    say("[P0] casts Krark")
                    return True
        elif ST["stage"] == "TRIALS":
            if not TR["active"] and OBS["cast_pending"] is None:
                if await p0_land_drop(c, pid, state, acts):
                    return True
                if not stack_entries(state):
                    bolt_oid = find_hand(state, pid, BOLT)
                    ca = castspell_advertised(acts, bolt_oid)
                    if ca and life_of(state, 1) > 9 \
                            and OBS["trials_done"] < MAX_TRIALS:
                        await submit_as_is(c, ca)
                        OBS["cast_pending"] = OBS["trials_done"]
                        OBS["cast_pending_t0"] = time.time()
                        OBS["bolt_casts"] += 1
                        say(f"[P0] casts Bolt (trial {OBS['trials_done']})")
                        return True

    # --- P1: land drops only, otherwise passive
    if not is_p0 and is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True

    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def trial_record(i, cls):
    res = TR["resolutions"]
    return {"trial": i, "class": cls,
            "resolutions": res,
            "first_outcome": res[0]["outcome"] if res else None,
            # For a first-loss trial the second trigger's flip is not
            # directly observable (a win the engine ignores looks exactly
            # like a loss). The observable is whether a copy was created.
            "second_copy": (res[1]["created_copy"] if len(res) >= 2
                            else None),
            "coalesced": TR["coalesced"],
            "pre_p1_life": TR["pre_p1_life"],
            "post_p1_life": TR.get("post_p1_life"),
            "pre_bolt_hand": TR.get("pre_bolt_hand"),
            "post_bolt_hand": TR.get("post_bolt_hand")}


def firstloss_trials():
    return [t for t in OBS["trials"]
            if t["first_outcome"] == "loss" and not t["coalesced"]]


async def attempt():
    reset()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0_deck": P0_DECK,
                  "p1_deck": P1_DECK})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej:
                if LAST_SUBMIT["iid"] in SUBMITTED:
                    SUBMITTED.discard(LAST_SUBMIT["iid"])
                    LAST_SUBMIT["iid"] = None
                OBS.setdefault("rejections", []).extend(
                    {"at": now, "who": c.name,
                     "type": r["type"], "data": r["data"]} for r in rej)
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
        state = st["state"]

        if wf_type(state) == "GameOver" and not ST["stop"]:
            obs["notes"].append("game over before sequence completed")
            say("game over -> stopping")
            ST["stop"] = True
            break

        # --- SETUP -> TRIALS transition
        if ST["stage"] == "SETUP" and is_my_main(state, 0) \
                and not stack_entries(state):
            kr = perm_oids(state, 0, KRARK)
            bx = perm_oids(state, 0, BOX)
            if len(kr) >= 2 and bx:
                say(f"=== stage -> TRIALS (turn {state.get('turn_number')}) ===")
                wire("trials_armed", {"turn": state.get("turn_number")})
                ST["stage"] = "TRIALS"
                OBS["trial_idle_t0"] = now

        if ST["stage"] == "TRIALS":
            sig = stack_sig(state)
            bl_ids = boltlike_ids(state)

            # --- a bolt was cast; wait for the 2-trigger stack shape
            if OBS["cast_pending"] is not None and not TR["active"]:
                kts = krark_triggers(state)
                n_bolt = len(bl_ids)
                if len(kts) >= 2 and n_bolt >= 1 \
                        and wf_type(state) not in ("TargetSelection",):
                    i = OBS["cast_pending"]
                    say(f"trial {i}: 2-trigger stack seen; exporting pre")
                    await export_now(f"pre_trial_{i}.json")
                    TR.update({"active": True, "i": i,
                               "pre_p1_life": life_of(state, 1),
                               "pre_bolt_hand": sum(
                                   1 for o in state["objects"].values()
                                   if oname(o) == BOLT
                                   and o.get("zone") == "Hand"
                                   and o.get("controller") == 0),
                               "prev_ids": sig,
                               "prev_entries": {str(e.get("id")): e
                                                for e in stack_entries(state)},
                               "prev_boltlike": bl_ids,
                               "resolutions": [], "coalesced": False})
                    wire("trial_start", {"trial": i, "stack": sig})
                    OBS["cast_pending"] = None
                elif now - OBS["cast_pending_t0"] > CAST_WATCHDOG:
                    say(f"trial {OBS['cast_pending']}: 2-trigger stack never "
                        f"appeared; clearing cast_pending")
                    wire("trial_cast_watchdog",
                         {"trial": OBS["cast_pending"]})
                    obs["notes"].append(
                        f"trial {OBS['cast_pending']}: triggers never reached "
                        f"2 on the stack")
                    OBS["cast_pending"] = None
                    OBS["trials_done"] += 1

            # --- active trial: diff the stack, classify trigger resolutions
            elif TR["active"]:
                if sig != TR["prev_ids"]:
                    prev_ids = set(x[0] for x in TR["prev_ids"])
                    new_ids = set(x[0] for x in sig)
                    left = prev_ids - new_ids
                    trig_left = [TR["prev_entries"][x] for x in left
                                 if x in TR["prev_entries"]
                                 and is_krark_trigger(TR["prev_entries"][x])]
                    created = bl_ids - TR["prev_boltlike"]
                    if trig_left:
                        if len(trig_left) > 1:
                            # two triggers resolved between observations: the
                            # per-trigger flip attribution is unreliable.
                            TR["coalesced"] = True
                            say(f"trial {TR['i']}: {len(trig_left)} triggers "
                                f"resolved between observations (coalesced)")
                        for te in trig_left:
                            if len(trig_left) > 1:
                                outcome, cc = "unknown", False
                            elif created:
                                outcome, cc = "win", True
                            elif len(bl_ids) < len(TR["prev_boltlike"]):
                                outcome, cc = "loss", False
                            else:
                                outcome, cc = "unknown", False
                            TR["resolutions"].append(
                                {"trigger_id": str(te.get("id")),
                                 "outcome": outcome, "created_copy": cc,
                                 "boltlike_after": len(bl_ids)})
                            say(f"trial {TR['i']}: trigger "
                                f"{te.get('id')} -> {outcome} "
                                f"(copy={cc}, boltlike={len(bl_ids)})")
                            wire("trigger_resolution",
                                 {"trial": TR["i"],
                                  "trigger_id": str(te.get("id")),
                                  "outcome": outcome, "created_copy": cc,
                                  "stack": sig})
                    TR["prev_ids"] = sig
                    TR["prev_entries"] = {str(e.get("id")): e
                                          for e in stack_entries(state)}
                    TR["prev_boltlike"] = bl_ids

                # --- trial end: stack empty
                if not stack_entries(state) and wf_type(state) not in (
                        "TargetSelection", "CopyRetarget"):
                    i = TR["i"]
                    TR["post_p1_life"] = life_of(state, 1)
                    TR["post_bolt_hand"] = sum(
                        1 for o in state["objects"].values()
                        if oname(o) == BOLT and o.get("zone") == "Hand"
                        and o.get("controller") == 0)
                    cls = "".join(
                        "W" if r["outcome"] == "win" else
                        "L" if r["outcome"] == "loss" else "?"
                        for r in TR["resolutions"])
                    rec = trial_record(i, cls)
                    rec["damage"] = (rec["pre_p1_life"] or 0) - (
                        rec["post_p1_life"] or 0)
                    OBS["trials"].append(rec)
                    OBS["trials_done"] += 1
                    say(f"trial {i} done: class={cls} "
                        f"damage={rec['damage']} "
                        f"first_loss_trials={len(firstloss_trials())}")
                    wire("trial_end", rec)
                    TR.update({"active": False, "i": -1})
                    OBS["trial_idle_t0"] = now
                    if len(firstloss_trials()) >= 8:
                        say("8 first-loss trials collected; stopping")
                        ST["stop"] = True
                        break
                    if OBS["trials_done"] >= MAX_TRIALS:
                        say("max trials reached; stopping")
                        ST["stop"] = True
                        break

            # --- idle watchdog: no trial started for a while
            if not TR["active"] and OBS["cast_pending"] is None:
                if OBS["trial_idle_t0"] is None:
                    OBS["trial_idle_t0"] = now
                if now - OBS["trial_idle_t0"] > TRIAL_IDLE_WATCHDOG:
                    say("trial idle watchdog fired; stopping")
                    wire("trial_idle_watchdog", {})
                    obs["notes"].append("trial idle watchdog fired")
                    ST["stop"] = True
                    break
            else:
                OBS["trial_idle_t0"] = now

    say(f"loop ended: stage={ST['stage']} elapsed={time.time()-t0:.0f}s "
        f"trials={OBS['trials_done']}")
    wire("loop_end", {"stage": ST["stage"]})

    if not stack_entries(p0.latest["state"]):
        await export_now("post_trials.json")

    observations = {
        "krark_casts": OBS["krark_casts"],
        "box_casts": OBS["box_casts"],
        "bolt_casts": OBS["bolt_casts"],
        "bolt_target_answers": OBS["bolt_target_answers"],
        "copyretarget_answers": OBS["copyretarget_answers"],
        "trials": OBS["trials"],
        "trials_done": OBS["trials_done"],
        "rejections": OBS.get("rejections", []),
        "notes": obs["notes"],
        "waiting_for_seq": WF_SEEN,
    }
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": observations}, f, indent=1, default=str)
    say("wrote observations.json")
    return obs


async def main():
    await attempt()
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
