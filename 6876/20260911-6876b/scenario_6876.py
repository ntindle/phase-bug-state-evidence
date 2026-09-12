#!/usr/bin/env python3
"""Issue #6876: Agent Maria Hill — Gets counters anytime she is tapped.

Reported (Discord): "Gets counters anytime she is tapped. Should only get
counters when tapped to pay teamwork costs."

Oracle: "Whenever Agent Maria Hill becomes tapped to pay a teamwork cost,
put a +1/+1 counter on her and draw a card."

Triage acceptance criteria:
- The ability triggers when Agent Maria Hill is tapped as payment of a
  teamwork cost.
- It does not trigger from attacking, another tap ability, or an external
  tap effect.
- A valid trigger puts one +1/+1 counter on her and draws one card.

Plan (native engine, v0.80.0 / protocol 69, two human-client seats):
  P0: Agent Maria Hill ({W} 2/1) + Grizzly Bears + Beast Mode
      ({1}{G}, "Target creature gets +2/+2 and gains trample until end of
      turn", Teamwork 1: "you may tap any number of creatures you control
      with total power 1 or more") + Plains/Forests.
      Beast Mode targets ANY creature, so it can be cast main-phase with no
      combat setup; its own teamwork bonus counter lands on the Bears, not
      Hill, keeping Hill's counter count a clean signal.
  P1: passive (Plains only; land drops + empty attacks/blocks).

  SETUP: develop mana, cast Maria Hill then Grizzly Bears.
  TEAMWORK (true branch): P0 main phase, Hill untapped. Cast Beast Mode
      targeting the Bears -> pay the teamwork optional cost -> tap ONLY
      Maria Hill for the TapCreatures payment. Export pre_teamwork.json at
      stage entry. The trigger (if correct) fires on the tap: Hill should
      gain exactly 1 +1/+1 counter and P0 draws 1 card. When the stack is
      empty afterwards, export post_teamwork.json.
  ATTACK (reported bug): P0's next turn, attack with Maria Hill (tapped via
      attacking - NOT a teamwork cost). At PostCombatMain export
      post_attack.json.

Behavioral contract:
  A1 setup_ok            pre_teamwork: Hill on P0 BF untapped with 0
                         counters, Bears on P0 BF, Beast Mode in P0 hand,
                         P0 PreCombatMain
  A2 teamwork_cast       Beast Mode was cast with teamwork paid (Hill was
                         the tapped creature); spell resolved to graveyard
  A3 teamwork_trigger_ok post_teamwork: Hill has exactly 1 +1/+1 counter;
                         P0 hand == pre hand (cast -1, trigger draw +1)
  A4 attack_no_trigger   post_attack: Hill still exactly 1 counter; P0
                         hand == post_teamwork hand + 1 (normal turn draw
                         only); P1 life dropped by 2 (unblocked attack)
  A5 cleanup             stack empty, game not over

Verdict: reproduced iff A1 passes and A4 fails (attack-tap fires the
trigger - the exact reported claim), or A3 fails while A1 passes (related
trigger-qualifier failure: teamwork tap does not fire it). not-reproduced
iff A1..A5 all pass. blocked otherwise.
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
RUN_ID = "20260911-6876b"
ISSUE = "6876"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

HILL = "Agent Maria Hill"
BEAST = "Beast Mode"
BEAR = "Grizzly Bears"
PLAINS = "Plains"
FOREST = "Forest"
LANDS = {PLAINS, FOREST}
P0_DECK = [(HILL, 4), (BEAST, 8), (BEAR, 8), (PLAINS, 15), (FOREST, 15)]
P1_DECK = [(PLAINS, 40)]

TIMEOUT = 1500
TEAMWORK_WATCHDOG = 420

ST = {}
OBS = {}
SUBMITTED = set()
MULLS = {}
SHAPES = set()
WF_SEEN = []
LAST_SUBMIT = {"iid": None}
C0 = None


def reset():
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False})
    OBS.clear()
    OBS.update({"rejections": [], "beast_casts": 0,
                "teamwork_pay_subs": 0, "tap_select_subs": 0,
                "beast_stack_seen": False, "hill_trigger_stack_seen": False,
                "hill_counter_first_seen": None, "attack_declared": False,
                "attack_turn": None, "teamwork_turn": None,
                "pre_teamwork_hand": None, "post_teamwork_hand": None,
                "teamwork_t0": None, "prompt_shapes": []})
    SUBMITTED.clear()
    MULLS.clear()
    MULLS.update({"P0": 0, "P1": 0})
    SHAPES.clear()
    WF_SEEN.clear()
    LAST_SUBMIT.update({"iid": None})


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


def perm_objs(state, pid, name):
    return [o for _, o in bf(state, pid) if oname(o) == name]


def tapped_of(o):
    return bool(o.get("tapped"))


def p1p1_count(o):
    """Count +1/+1 counters on an object; robust to shape variants."""
    total = 0
    ctrs = o.get("counters")
    if isinstance(ctrs, dict):
        for k, v in ctrs.items():
            if "P1P1" in str(k) or "plus_one" in str(k).lower():
                total += v if isinstance(v, int) else 1
    elif isinstance(ctrs, list):
        for c in ctrs:
            if isinstance(c, dict):
                t = str(c.get("type", c.get("kind", c.get("name", ""))))
                n = c.get("count", c.get("n", 1))
                if "P1P1" in t:
                    total += n if isinstance(n, int) else 1
            elif "P1P1" in str(c):
                total += 1
    return total


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def stack_entry(state, sid):
    for e in (state.get("stack") or []):
        if str(e.get("id")) == str(sid):
            return e
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
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if "seat" in d:
                    seat = d["seat"]
        o = state["objects"].get(str(ref), {}) if ref else {}
        se = stack_entry(state, ref) if ref else None
        if se is not None:
            name, zone, controller = ("stack-entry", "Stack",
                                      se.get("controller"))
        else:
            name, zone, controller = oname(o), o.get("zone"), \
                o.get("controller")
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": name, "zone": zone, "controller": controller,
                    "text": ch.get("text")})
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


def beast_on_stack(state):
    for e in (state.get("stack") or []):
        blob = json.dumps(e, default=str)
        if "gets +2/+2 and gains trample" in blob:
            return True
    return False


def hill_trigger_on_stack(state):
    """Hill's triggered ability on the stack: kind TriggeredAbility whose
    own ability description is her Oracle text."""
    for e in (state.get("stack") or []):
        kind = (e.get("kind") or {})
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        if ktype != "TriggeredAbility":
            continue
        blob = json.dumps(e, default=str)
        if "Agent Maria Hill" in blob and "draw a card" in blob:
            return True
    return False

def hill_ready_for_tap(state):
    hills = perm_oids(state, 0, HILL)
    return bool(hills) and not tapped_of(state["objects"][hills[0]])


async def handle_beast_prompts(c, state, acts, st):
    """Drive P0's Beast Mode cast prompts: TargetSelection -> OptionalCostChoice
    (teamwork) -> TapCreatures selection. Returns True if a submission was made.
    Never submits blindly: every submission is gated on candidates matching
    the intended objects.
    """
    if wf_player(state) != 0:
        return False
    if wf_type(state) in (None, "Priority", "GameOver"):
        return False
    vi = get_vi(st)
    if not vi:
        # Fallback: the TapCreatures payment may surface as an advertised
        # SelectCards action instead of a viewer_interaction opportunity.
        # Only answer when its candidates include Hill on the battlefield.
        if ST["stage"] == "TEAMWORK" and hill_ready_for_tap(state):
            hill_oid = perm_oids(state, 0, HILL)[0]
            for a in acts:
                if a["type"] != "SelectCards":
                    continue
                d = a.get("data", {}) or {}
                wire("selectcards_advertised",
                     {"wf": wf_type(state), "data": d,
                      "stage": ST["stage"]})
                cands = d.get("candidates") or d.get("cards") or []
                refs = set()
                for cnd in cands:
                    if isinstance(cnd, dict):
                        refs.add(str(cnd.get("reference",
                                             cnd.get("object_id",
                                                     cnd.get("id", "")))))
                    else:
                        refs.add(str(cnd))
                if hill_oid in refs:
                    sub = {"type": "SelectCards",
                           "data": {"cards": [int(hill_oid)]}}
                    await submit_as_is(c, sub)
                    OBS["tap_select_subs"] += 1
                    say(f"[P0] teamwork taps Hill oid={hill_oid} "
                        f"(advertised SelectCards)")
                    return True
        return False
    hills = perm_oids(state, 0, HILL)
    bears = perm_oids(state, 0, BEAR)
    hill_oid = hills[0] if hills else None
    bear_oid = bears[0] if bears else None

    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        cands = candidate_info(opp, state)

        # --- TargetSelection: Beast Mode's target creature -> the Bears
        if wf_type(state) == "TargetSelection" and bear_oid:
            want = next((x for x in cands if x["ref"] == bear_oid), None)
            if not want:
                continue
            shape = ("TargetSelection", resp.get("type"), len(cands))
            if shape not in SHAPES:
                SHAPES.add(shape)
                wire("beast_prompt", {"wf": "TargetSelection",
                                      "rtype": resp.get("type"),
                                      "candidates": cands})
                say(f"[P0] Beast Mode target prompt: "
                    f"{[(x['name'], x['ref']) for x in cands]} -> Bears")
            resp_out = build_target_response(resp, want)
            if not resp_out:
                say("[P0] target prompt: unexpected response shape "
                    f"{resp.get('type')}; holding")
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            SUBMITTED.add(iid)
            say(f"[P0] Beast Mode targets Bears oid={bear_oid}")
            return True

        # --- OptionalCostChoice: teamwork pay/decline
        if wf_type(state) == "OptionalCostChoice":
            wire("optional_cost_full",
                 {"iid": iid, "waiting_for": wf_data(state),
                  "interaction": opp})
            cost = wf_data(state).get("cost") or {}
            say(f"[P0] OptionalCostChoice cost.type={cost.get('type')} "
                f"choices={len(cands)}")
            pick = None
            for ch in (resp.get("data", {}) or {}).get("choices", []) or []:
                ac = [((s.get("data") or {}).get("code") or "")
                      for s in ch.get("surfaces", []) or []
                      if s.get("type") == "action"]
                vals = [((s.get("data") or {}).get("role"),
                         (s.get("data") or {}).get("value"))
                        for s in ch.get("surfaces", []) or []
                        if s.get("type") == "value"]
                if "decideOptionalCost" in ac and ("pay", "true") in vals:
                    pick = ch
                    break
            if pick is not None:
                await send_interaction(
                    c, {"interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": pick["id"]}}})
                SUBMITTED.add(iid)
                OBS["teamwork_pay_subs"] += 1
                say("[P0] teamwork -> PAY (true)")
                return True
            say("[P0] OptionalCostChoice: no pay/true choice found; holding")
            return False

        # --- TapCreatures payment: select ONLY Maria Hill
        if hill_oid and cands and any(
                x["ref"] == hill_oid and x["zone"] == "Battlefield"
                for x in cands):
            shape = (wf_type(state), resp.get("type"), len(cands))
            if shape not in SHAPES:
                SHAPES.add(shape)
                wire("teamwork_tap_prompt",
                     {"wf": wf_type(state), "rtype": resp.get("type"),
                      "waiting_for": wf_data(state),
                      "candidates": cands, "opportunity": opp})
                say(f"[P0] teamwork tap prompt ({wf_type(state)}/"
                    f"{resp.get('type')}): "
                    f"{[(x['name'], x['ref']) for x in cands]} -> Hill only")
            want = next((x for x in cands
                         if x["ref"] == hill_oid
                         and x["zone"] == "Battlefield"), None)
            resp_out = build_target_response(resp, want)
            if not resp_out:
                say("[P0] tap prompt: unexpected response shape "
                    f"{resp.get('type')}; holding")
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            SUBMITTED.add(iid)
            OBS["tap_select_subs"] += 1
            say(f"[P0] teamwork taps Hill oid={hill_oid} (only)")
            return True
    return False


async def p0_land_drop(c, pid, state, acts):
    lid = find_hand(state, pid, PLAINS) or find_hand(state, pid, FOREST)
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
                keep_ok = PLAINS in names and HILL in names
            else:
                keep_ok = any(n in LANDS for n in names)
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
            keep = {HILL, BEAST} if is_p0 else set()
            pref = [o for o in h if oname(state["objects"][o]) in LANDS]
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

    # --- P0 attack declaration (ATTACK stage only)
    if is_p0 and ST["stage"] == "ATTACK" and not OBS["attack_declared"]:
        for a in acts:
            if a["type"] == "DeclareAttackers":
                hills = perm_oids(state, pid, HILL)
                hill_oid = hills[0] if hills else None
                if hill_oid and not tapped_of(
                        state["objects"][hill_oid]):
                    sub = dict(a)
                    sub["data"] = dict(a.get("data", {}))
                    sub["data"]["attacks"] = [
                        [int(hill_oid),
                         {"type": "Player", "data": 1}]]
                    sub["data"]["bands"] = []
                    await submit_as_is(c, sub)
                    OBS["attack_declared"] = True
                    OBS["attack_turn"] = state.get("turn_number")
                    say(f"[P0] attacks with Hill oid={hill_oid} "
                        f"(turn {OBS['attack_turn']})")
                    return True
                # Hill not attack-ready yet: declare empty, wait
                sub = dict(a)
                sub["data"] = dict(a.get("data", {}))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True

    # --- generic attackers/blockers
    for a in acts:
        if a["type"] == "DeclareAttackers" and not (
                is_p0 and ST["stage"] == "ATTACK"):
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

    # --- P0's Beast Mode prompts take precedence over the main plan
    if is_p0 and ST["stage"] == "TEAMWORK":
        if await handle_beast_prompts(c, state, acts, st):
            return True
        # never pass priority while a P0 cast decision is pending
        if wf_type(state) in ("OptionalCostChoice", "TargetSelection") \
                and wf_player(state) == 0:
            return False

    # --- mana producers: submit advertised payments as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True

    # --- P0 main-phase plan
    if is_p0 and is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        stage = ST["stage"]
        if stage == "SETUP":
            hill_oid = find_hand(state, pid, HILL)
            if hill_oid and not perm_oids(state, pid, HILL):
                ca = castspell_advertised(acts, hill_oid)
                if ca:
                    await submit_as_is(c, ca)
                    say("[P0] casts Agent Maria Hill")
                    return True
            bear_oid = find_hand(state, pid, BEAR)
            if bear_oid and len(perm_oids(state, pid, BEAR)) < 1:
                ca = castspell_advertised(acts, bear_oid)
                if ca:
                    await submit_as_is(c, ca)
                    say("[P0] casts Grizzly Bears")
                    return True
        elif stage == "TEAMWORK":
            if not OBS["beast_casts"]:
                beast_oid = find_hand(state, pid, BEAST)
                if beast_oid:
                    ca = castspell_advertised(acts, beast_oid)
                    if ca:
                        await submit_as_is(c, ca)
                        OBS["beast_casts"] += 1
                        say("[P0] casts Beast Mode")
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

        # --- SETUP -> TEAMWORK transition
        if ST["stage"] == "SETUP" and is_my_main(state, 0):
            hills = perm_objs(state, 0, HILL)
            bears = perm_oids(state, 0, BEAR)
            beast_hand = find_hand(state, 0, BEAST)
            hill_ready = any(not tapped_of(h) and p1p1_count(h) == 0
                             for h in hills)
            if hills and hill_ready and bears and beast_hand:
                turn = state.get("turn_number")
                say(f"=== stage -> TEAMWORK (turn {turn}) ===")
                wire("teamwork_armed", {"turn": turn,
                                        "hand": hand_count(state, 0)})
                OBS["pre_teamwork_hand"] = hand_count(state, 0)
                OBS["teamwork_turn"] = turn
                await export_now("pre_teamwork.json")
                ST["stage"] = "TEAMWORK"
                OBS["teamwork_t0"] = time.time()

        # --- TEAMWORK watches
        if ST["stage"] == "TEAMWORK":
            if not OBS["teamwork_t0"]:
                OBS["teamwork_t0"] = time.time()
            if beast_on_stack(state):
                OBS["beast_stack_seen"] = True
            if hill_trigger_on_stack(state):
                OBS["hill_trigger_stack_seen"] = True
            hills = perm_objs(state, 0, HILL)
            if hills and OBS["hill_counter_first_seen"] is None:
                n = p1p1_count(hills[0])
                if n > 0:
                    OBS["hill_counter_first_seen"] = n
                    say(f"Hill counter observed: {n}")
            # conclusion: spell + trigger both left the stack
            if OBS["beast_stack_seen"] and not beast_on_stack(state) \
                    and not hill_trigger_on_stack(state) \
                    and not (state.get("stack") or []):
                say("stack empty after Beast Mode + Hill trigger; "
                    "exporting post_teamwork")
                wire("stack_emptied_teamwork", {})
                OBS["post_teamwork_hand"] = hand_count(state, 0)
                await export_now("post_teamwork.json")
                ST["stage"] = "ATTACK"
                say("=== stage -> ATTACK ===")
            elif time.time() - OBS["teamwork_t0"] > TEAMWORK_WATCHDOG:
                say("TEAMWORK watchdog fired; stopping")
                wire("teamwork_watchdog", {})
                obs["notes"].append("teamwork stage watchdog fired")
                ST["stop"] = True
                break

        # --- ATTACK watches
        if ST["stage"] == "ATTACK" and OBS["attack_declared"]:
            atk_turn = OBS["attack_turn"]
            turn = state.get("turn_number")
            phase = state.get("phase")
            if turn == atk_turn and phase == "PostCombatMain":
                say("PostCombatMain on attack turn; "
                    "exporting post_attack")
                await export_now("post_attack.json")
                ST["stop"] = True
                break

    say(f"loop ended: stage={ST['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": ST["stage"]})

    # observations for the finalizer
    observations = {
        "beast_casts": OBS["beast_casts"],
        "teamwork_pay_subs": OBS["teamwork_pay_subs"],
        "tap_select_subs": OBS["tap_select_subs"],
        "beast_stack_seen": OBS["beast_stack_seen"],
        "hill_trigger_stack_seen": OBS["hill_trigger_stack_seen"],
        "hill_counter_first_seen": OBS["hill_counter_first_seen"],
        "attack_declared": OBS["attack_declared"],
        "attack_turn": OBS["attack_turn"],
        "teamwork_turn": OBS["teamwork_turn"],
        "pre_teamwork_hand": OBS["pre_teamwork_hand"],
        "post_teamwork_hand": OBS["post_teamwork_hand"],
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
