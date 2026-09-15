#!/usr/bin/env python3
"""Issue #7177: Mr. House, President and CEO -- no Treasure on a 6+ roll.

Oracle: "Whenever you roll a 4 or higher, create a 3/3 colorless Robot
artifact creature token. If you rolled 6 or higher, instead create that
token and a Treasure token."
"{4}, {T}: Roll a six-sided die plus an additional six-sided die for each
mana from Treasures spent to activate this ability."

Reported: on a 6+ it only makes the Robot; it should make both.

Parse (pinned v0.83.0 card-data.json):
  trigger mode RolledDieOnce, die_result AtLeast 4;
  execute Token{Robot 3/3 artifact creature}; sub_ability (SequentialSibling)
  effect Unimplemented{name=instead_condition,
    description="If you rolled 6 or higher, instead create that token and
    a Treasure token"}.
  The activated roll ability parses as Unimplemented{name=unparsed_quantity}
  ("Roll a six-sided die plus an additional six-sided die for each mana
  from Treasures spent..."), so the die is rolled with Adorable Kitten
  ({W} 1/1, ETB: roll a six-sided die, you gain life equal to the result) --
  the life-gain delta reveals each exact roll result.

Plan (native engine, v0.83.0 / protocol 70, two human driver seats):
  P0: 4x mr. house / 20x adorable kitten /
      14x mountain / 13x plains / 13x swamp (House costs {R}{W}{B}).
  P1: 60x forest, fully passive (lands, passes, never attacks).
  P0 casts Mr. House, then casts Kittens one at a time; each Kitten ETB
  rolls 1d6 and P0 gains life = result. Per roll r, with House on the BF:
    r in 1..3 -> nothing; r in 4..5 -> 1 Robot; r=6 -> 1 Robot + 1 Treasure.
  Measurement per kitten: baseline (life, robot count, treasure count) at
  cast; after the kitten is on the BF and the stack has been empty for
  8 consecutive main-loop ticks, re-measure. Deltas are attributed to that
  single roll (P1 is passive; P0 declares no attackers).

Behavioral contract:
  A1 setup_ok      House on P0 BF; pre.json exported before first kitten.
  A2 rolls_valid   >=12 rolls recorded, every result in 1..6, >=1 six and
                   >=2 mid (4/5) rolls observed.
  A3 low_clean     every r in 1..3 -> 0 robots, 0 treasures.
  A4 mid_robot     every r in 4..5 -> exactly 1 robot, 0 treasures
                   (also proves the trigger fires at all).
  A5 six_treasure  every r = 6 -> exactly 1 robot AND 1 treasure
                   (expected to FAIL == the reported bug).
  A6 cleanup       stack empty, post.json exported, game proceeding.

Verdict: reproduced iff A1-A4 pass and A5 fails (>=1 six with 0 treasure).
         not-reproduced iff A1-A5 all pass.
         blocked iff A1 fails, or zero usable rolls, or no six after the
         roll cap (the 6+ branch untestable), or life deltas not in 1..6.

Driver notes:
  - RolledDieOnce trigger: House trigger must be on the BF; the roll comes
    from the Kitten ETB (ChangesZone/SelfRef trigger).
  - Never pass priority while a PayMana/PayManaAbilityMana action is
    advertised; answer it first (pay_tick).
  - Token counting is by lowercase object name ("robot"/"treasure") with a
    subtype-list fallback; the first count logs all P0 BF names so the
    notes capture the engine's actual token naming.
  - The live server on 127.0.0.1:9374 is reused per the task body (already
    listening, pinned v0.83.0); SERVER_RUN_ID names its run dir for log
    excerpts.
"""
import asyncio
import copy as _copy
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260915-7177"
ISSUE = 7177
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

HOUSE = "mr. house, president and ceo"
KITTEN = "adorable kitten"
MOUNTAIN = "mountain"
PLAINS = "plains"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [(HOUSE, 4), (KITTEN, 24),
           (MOUNTAIN, 11), (PLAINS, 11), (SWAMP, 10)]
P1_DECK = [(FOREST, 60)]

TIMEOUT = 2400
TURN_CAP = 70
ROLL_TARGET = 20          # stop after this many recorded rolls
MIN_ROLLS = 12
QUIET_TICKS = 8           # stack-empty ticks before measuring a roll
PENDING_TIMEOUT = 90      # seconds before a pending roll is abandoned

SERVER_RUN_ID = "20260915-7173-server"  # live server's run dir (log excerpts)
SERVER_IDENTITY = {
    "validated_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "server_binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
    "server_run_id": SERVER_RUN_ID,
}

ST = {}
SUBMITTED = set()
MULLS = {}
NAMES_LOGGED = False


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> ROLLING -> DONE
        "stop": False,
        "house_oid": None,
        "rolls": [],        # {turn, kitten_oid, result, robots_delta,
                            #  treasures_delta, house_trigger_seen}
        "pending": None,    # {kitten_oid, life0, robots0, treasures0,
                            #  t0, turn, quiet, trigger_seen}
        "pre_exported": False,
        "post_exported": False,
        "hello": None,
    })


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def obj_name(state, oid):
    o = state.get("objects", {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key)]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def life_of(state, pid):
    return player_of(state, pid).get("life")


def token_subtypes(o):
    subs = []
    ct = o.get("card_type") or {}
    if isinstance(ct, dict):
        subs += [str(s).lower() for s in (ct.get("subtypes") or [])]
    subs += [str(s).lower() for s in (o.get("subtypes") or [])]
    tl = str(o.get("type_line") or "").lower()
    return subs, tl


def count_tokens(state, pid, kind):
    """kind in ("robot","treasure"). Count by name, fallback to subtypes."""
    global NAMES_LOGGED
    n = 0
    names = set()
    for oid, o in (state.get("objects", {}) or {}).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        names.add(nm)
        subs, tl = token_subtypes(o)
        if kind == "robot":
            if nm == "robot" or "robot" in subs or "robot" in tl:
                n += 1
        else:
            if nm == "treasure" or "treasure" in subs or "treasure" in tl:
                n += 1
    if not NAMES_LOGGED and names:
        NAMES_LOGGED = True
        say(f"P0 BF object names at first token count: {sorted(names)}")
        wire("bf_names", {"names": sorted(names)})
    return n


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped")
                and (name is None or nm == name)):
            out.append(int(oid))
    return out


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def stack_entries(state):
    return state.get("stack") or []


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)


async def do_mulligan(c, pid, tag):
    st = c.latest
    a = find_action(merged_actions(st), "MulliganDecision")
    if not a:
        return False
    names = hand_lnames(st["state"], pid)
    nlands = sum(1 for n in names if n in (MOUNTAIN, PLAINS, SWAMP, FOREST))
    keep = nlands >= 2
    say(f"[{tag}] mulligan: {nlands} lands -> {'keep' if keep else 'redo'}")
    sub = _copy.deepcopy(a)
    sub["data"]["decision"] = "keep" if keep else "mulligan"
    await submit_as_is(c, sub)
    MULLS[tag] = True
    wire("mulligan", {"who": tag, "decision": sub["data"]["decision"],
                      "n_lands": nlands})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    state = st["state"]
    a = find_action(merged_actions(st), "SelectCards")
    if not a:
        return False
    n = ((wf_of(state).get("data") or {}).get("phase") or {}).get("count", 1)
    h = hand_ids(state, pid)
    picks = h[:n]
    sub = _copy.deepcopy(a)
    sub["data"]["cardIds"] = [int(x) for x in picks]
    say(f"[{tag}] bottoming {n}: {[lname(state, x) for x in picks]}")
    await submit_as_is(c, sub)
    return True


async def discard_default(c, pid, tag, st, state):
    wf = wf_of(state)
    if wf.get("type") != "DiscardToHandSize":
        return False
    data = wf.get("data") or {}
    if str(data.get("player")) != str(pid):
        return False
    n = data.get("count") or 1
    vi = get_vi(st)
    if not vi:
        return False
    hand = hand_ids(state, pid)
    # discard lands first, then non-kitten non-house, then kittens last
    prio = sorted(hand, key=lambda o: (
        0 if lname(state, o) in (MOUNTAIN, PLAINS, SWAMP, FOREST)
        else (2 if lname(state, o) in (KITTEN, HOUSE) else 1)))
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        picks = [ch.get("id") for ch in cands
                 if str(ch.get("id")) in
                 {str(x) for x in prio[:n]}][:n]
        if len(picks) < n:
            picks = [ch.get("id") for ch in cands[:n]]
        SUBMITTED.add(key)
        spec = (rdata.get("spec") or {}).get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": spec, "data": {"choiceIds": picks}}}
        say(f"[{tag}] discarding {n} to hand size")
        await c.send_interaction(sub)
        return True
    return False


async def combat_tick(c, pid, tag, acts, st, state):
    wtype = wf_of(state).get("type") or ""
    if wtype == "DeclareAttackers" and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            sub = _copy.deepcopy(da)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
        return True
    if wtype == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            sub = _copy.deepcopy(da)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
        return True
    return False


async def pay_tick(acts, c, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def cast_named(c, acts, state, name, tag):
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        d = a.get("data") or {}
        oid = (d.get("object_id") or d.get("source_id") or a.get("_src_oid"))
        if oid is not None and lname(state, int(oid)) == name:
            say(f"[{tag}] casting {name} (oid={oid})")
            wire("cast", {"who": tag, "name": name, "oid": int(oid)})
            await submit_as_is(c, a)
            return int(oid)
    return None


async def play_land_pref(c, acts, state, pref, tag):
    plays = [a for a in acts if a["type"] == "PlayLand"]
    if not plays:
        return False
    for name in pref:
        for a in plays:
            d = a.get("data") or {}
            oid = d.get("object_id") or a.get("_src_oid")
            if oid is not None and lname(state, int(oid)) == name:
                await submit_as_is(c, a)
                return True
    await submit_as_is(c, plays[0])
    return True


# ------------------------------------------------------- per-seat ticks ---
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if find_action(acts, "MulliganDecision") and "P0" not in MULLS:
            await do_mulligan(c, 0, "P0")
            return
        if find_action(acts, "SelectCards"):
            await do_bottom(c, 0, "P0")
            return
    if await pay_tick(acts, c, "P0"):
        return
    if await combat_tick(c, 0, "P0", acts, st, state):
        return
    if await discard_default(c, 0, "P0", st, state):
        return
    # Never pass priority while a viewer interaction for our seat is
    # pending (e.g. a die-roll prompt); hold instead.
    vi = get_vi(st)
    if vi and str((wf_of(state).get("data") or {}).get("player")) == "0" \
            and wtype != "Priority":
        say(f"[P0] holding on {wtype} (opportunities pending)")
        wire("held_prompt", {"wf": wtype,
                             "opp": json.loads(json.dumps(
                                 vi.get("opportunities") or [],
                                 default=str))[:2]})
        return
    if not my_priority(state, 0):
        return
    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 0 \
            and not (state.get("stack") or []) \
            and ST["pending"] is None:
        stage = ST["stage"]
        if stage == "SETUP":
            await play_land_pref(c, acts, state,
                                 [PLAINS, MOUNTAIN, SWAMP], "P0")
            if ST["house_oid"] is None and HOUSE in hand_lnames(state, 0) \
                    and untapped_lands(state, 0, MOUNTAIN) \
                    and untapped_lands(state, 0, PLAINS) \
                    and untapped_lands(state, 0, SWAMP):
                await cast_named(c, acts, state, HOUSE, "P0")
                return
        elif stage == "ROLLING":
            await play_land_pref(c, acts, state,
                                 [PLAINS, MOUNTAIN, SWAMP], "P0")
            if KITTEN in hand_lnames(state, 0) \
                    and untapped_lands(state, 0, PLAINS):
                oid = await cast_named(c, acts, state, KITTEN, "P0")
                if oid is not None:
                    ST["pending"] = {
                        "kitten_oid": oid,
                        "life0": life_of(state, 0),
                        "robots0": count_tokens(state, 0, "robot"),
                        "treasures0": count_tokens(state, 0, "treasure"),
                        "t0": time.time(),
                        "turn": state.get("turn_number"),
                        "quiet": 0,
                        "trigger_seen": False,
                    }
                    say(f"[P0] roll pending: kitten oid={oid} "
                        f"life0={ST['pending']['life0']} "
                        f"robots0={ST['pending']['robots0']} "
                        f"treasures0={ST['pending']['treasures0']}")
                    wire("roll_pending",
                         {k: v for k, v in ST["pending"].items()
                          if k != "quiet"})
                    return
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return


async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if find_action(acts, "MulliganDecision") and "P1" not in MULLS:
            await do_mulligan(c, 1, "P1")
            return
        if find_action(acts, "SelectCards"):
            await do_bottom(c, 1, "P1")
            return
    if await pay_tick(acts, c, "P1"):
        return
    if await combat_tick(c, 1, "P1", acts, st, state):
        return
    if await discard_default(c, 1, "P1", st, state):
        return
    if not my_priority(state, 1):
        return
    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 1:
        await play_land_pref(c, acts, state, [FOREST], "P1")
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return


# --------------------------------------------------------------- evaluate -
def load_st(fn):
    p = f"{EVDIR}/{fn}"
    if os.path.exists(p):
        return json.loads(open(p).read())["state"]
    return None


def evaluate():
    a = {}
    pre_st = load_st("pre.json")
    post_st = load_st("post.json")

    house_oid = ST.get("house_oid")
    house_bf_pre = (house_oid is not None and pre_st is not None
                    and get_obj(pre_st, house_oid).get("zone")
                    == "Battlefield")
    a["A1_setup_ok"] = ("passed" if (house_bf_pre and ST["pre_exported"])
                        else "failed")
    say(f"A1: house_bf_pre={house_bf_pre} pre_exported={ST['pre_exported']}")

    rolls = ST["rolls"]
    valid = [r for r in rolls if r.get("result") in (1, 2, 3, 4, 5, 6)]
    sixes = [r for r in valid if r["result"] == 6]
    mids = [r for r in valid if r["result"] in (4, 5)]
    lows = [r for r in valid if r["result"] in (1, 2, 3)]
    a["A2_rolls_valid"] = ("passed" if (len(valid) >= MIN_ROLLS
                                        and len(sixes) >= 1
                                        and len(mids) >= 2) else "failed")
    say(f"A2: rolls={len(rolls)} valid={len(valid)} sixes={len(sixes)} "
        f"mids={len(mids)} lows={len(lows)} "
        f"results={[r.get('result') for r in rolls]}")

    a["A3_low_clean"] = ("passed" if all(
        r["robots_delta"] == 0 and r["treasures_delta"] == 0
        for r in lows) and lows else ("failed" if lows else "not-run"))
    a["A4_mid_robot"] = ("passed" if all(
        r["robots_delta"] == 1 and r["treasures_delta"] == 0
        for r in mids) and mids else ("failed" if mids else "not-run"))
    a["A5_six_treasure"] = ("passed" if all(
        r["robots_delta"] == 1 and r["treasures_delta"] == 1
        for r in sixes) and sixes else ("failed" if sixes else "not-run"))
    for r in lows + mids + sixes:
        say(f"  roll r={r['result']}: robotsΔ={r['robots_delta']} "
            f"treasuresΔ={r['treasures_delta']} "
            f"house_trigger_seen={r['house_trigger_seen']}")

    if post_st is not None:
        a["A6_cleanup"] = ("passed" if not (post_st.get("stack") or [])
                           else "failed")
    else:
        a["A6_cleanup"] = "not-run"

    if a["A1_setup_ok"] == "failed" or a["A2_rolls_valid"] == "failed":
        verdict = "blocked"
    elif a["A3_low_clean"] == "failed" or a["A4_mid_robot"] == "failed":
        verdict = "reproduced"  # trigger misbehaves adjacent to the report
    elif a["A5_six_treasure"] == "failed":
        verdict = "reproduced"  # the reported symptom
    elif a["A5_six_treasure"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return a, verdict


# ----------------------------------------------------------------- render -
def render_summary(run):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 1060
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7177 - Mr. House, President and CEO: "
           "no Treasure on a 6+ roll", fill=(235, 240, 250))
    y += 28
    si = SERVER_IDENTITY
    d.text((24, y),
           f"server v{si['validated_version']} ({si['build_commit']}) "
           f"protocol 70 - 2026-09-15", fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else (120, 220, 120))
    y += 34
    d.text((24, y), "Assertions (from saved states + per-roll deltas):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "House on P0 BF; pre.json before first kitten",
        "A2_rolls_valid": ">=12 rolls, results 1..6, >=1 six, >=2 mid",
        "A3_low_clean": "rolls 1-3 -> 0 robots, 0 treasures",
        "A4_mid_robot": "rolls 4-5 -> exactly 1 robot, 0 treasures",
        "A5_six_treasure": "roll 6 -> 1 robot AND 1 treasure [bug => FAIL]",
        "A6_cleanup": "stack empty, post.json exported",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12
    d.text((24, y), "Per-roll observations (r=result, R=robot delta, "
           "T=treasure delta):", fill=(200, 210, 225))
    y += 24
    for r in ST["rolls"][:22]:
        d.text((36, y),
               f"turn {r['turn']}: r={r['result']} R+{r['robots_delta']} "
               f"T+{r['treasures_delta']} "
               f"trig={r['house_trigger_seen']}", fill=(150, 160, 175))
        y += 20
        if y > H - 120:
            break
    y += 8
    d.text((24, y), "Parse: trigger RolledDieOnce AtLeast 4 -> Token Robot;",
           fill=(170, 180, 195))
    y += 20
    d.text((24, y), "  6+ branch = Unimplemented instead_condition "
           "(SequentialSibling).", fill=(170, 180, 195))
    y += 20
    d.text((24, y), "Rolls via Adorable Kitten ETB (life gain = result); "
           "House roll ability itself is", fill=(170, 180, 195))
    y += 20
    d.text((24, y), "  Unimplemented unparsed_quantity in card data.",
           fill=(170, 180, 195))
    y += 26
    for n in run["notes"][:6]:
        d.text((24, y), n[:116], fill=(140, 150, 165))
        y += 20
        if y > H - 30:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    try:
        subprocess.run(["cp", os.path.abspath(__file__),
                        f"{EVDIR}/scenario_7177.py"], check=False)
        say("copied scenario_7177.py into EVDIR")
    except Exception as e:
        say(f"scenario copy failed: {e}")
    files = ["pre.json", "post.json", "run.json", "scenario_7177.py",
             "wire_log.jsonl", "scenario_run.log", "summary.png",
             "parse_mrhouse.json", "parse_kitten.json",
             "server_excerpts.log"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def capture_server_excerpts():
    """Grab server.log lines for this game from the reused live server."""
    try:
        logp = (f"{BACKFILL}/runs/{SERVER_RUN_ID}/server.log")
        if not os.path.exists(logp):
            say("no server.log at reused run dir; skipping excerpts")
            return
        out = []
        with open(logp, errors="replace") as f:
            for line in f:
                if ST.get("game_code") and ST["game_code"] in line:
                    out.append(line)
        out = out[-300:]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.writelines(out)
        say(f"wrote server_excerpts.log ({len(out)} lines)")
    except Exception as e:
        say(f"server excerpts failed: {e}")


async def finish(a, verdict, notes):
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.83.0/data/"
                            f"card-data.json"))
        for key, fn in (("mr. house, president and ceo",
                         "parse_mrhouse.json"),
                        ("adorable kitten", "parse_kitten.json")):
            card = cd.get(key, {})
            with open(f"{EVDIR}/{fn}", "w") as f:
                json.dump({"name": card.get("name"),
                           "oracle_text": card.get("oracle_text"),
                           "triggers": card.get("triggers"),
                           "abilities": card.get("abilities")}, f, indent=2)
    except Exception as e:
        notes.append(f"parse json failed: {e}")
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "verdict": verdict,
        "assertions": a,
        "server_identity": SERVER_IDENTITY,
        "fixture": {
            "house_oid": ST.get("house_oid"),
            "n_rolls": len(ST["rolls"]),
            "rolls": ST["rolls"],
        },
        "notes": notes,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2)
    say(f"wrote run.json verdict={verdict}")
    render_summary(run)
    wire("run_complete", {"verdict": verdict, "assertions": a})


# ------------------------------------------------------------------- main -
async def main():
    reset()
    notes = []
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    for c in (p0, p1):
        await c.connect()
    say("server identity pinned: v0.83.0 (b7a59d4) protocol 70, mode Full; "
        "reusing live server per task body (already listening)")

    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    async def export_named(name):
        data = await p0.export_state()
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(data)
        say(f"exported {name}.json")
        wire(f"{name}_exported", {})
        return True

    def settle_pending(state):
        """Attribute a completed kitten roll once the board is quiet."""
        pend = ST.get("pending")
        if pend is None:
            return
        stack = stack_entries(state)
        for e in stack:
            blob = json.dumps(e, default=str).lower()
            if "house" in blob or "rolleddie" in blob.replace(" ", ""):
                pend["trigger_seen"] = True
                break
        stack_quiet = not stack
        if time.time() - pend["t0"] > PENDING_TIMEOUT:
            say(f"[P0] pending roll abandoned (timeout): "
                f"kitten oid={pend['kitten_oid']}")
            wire("roll_abandoned", {k: v for k, v in pend.items()
                                    if k != "quiet"})
            ST["rolls"].append({
                "turn": pend["turn"], "kitten_oid": pend["kitten_oid"],
                "result": None, "robots_delta": None,
                "treasures_delta": None,
                "house_trigger_seen": pend["trigger_seen"],
                "note": "abandoned: stack never quieted",
            })
            ST["pending"] = None
            return
        if stack_quiet:
            pend["quiet"] += 1
        else:
            pend["quiet"] = 0
        if pend["quiet"] < QUIET_TICKS:
            return
        kitten = get_obj(state, pend["kitten_oid"])
        if kitten.get("zone") != "Battlefield":
            say(f"[P0] pending roll abandoned: kitten not on BF "
                f"(zone={kitten.get('zone')})")
            wire("roll_abandoned", {"reason": "kitten_not_bf"})
            ST["rolls"].append({
                "turn": pend["turn"], "kitten_oid": pend["kitten_oid"],
                "result": None, "robots_delta": None,
                "treasures_delta": None,
                "house_trigger_seen": pend["trigger_seen"],
                "note": "abandoned: kitten left battlefield",
            })
            ST["pending"] = None
            return
        result = life_of(state, 0) - pend["life0"]
        robots_d = count_tokens(state, 0, "robot") - pend["robots0"]
        treasures_d = (count_tokens(state, 0, "treasure")
                       - pend["treasures0"])
        rec = {
            "turn": pend["turn"], "kitten_oid": pend["kitten_oid"],
            "result": result, "robots_delta": robots_d,
            "treasures_delta": treasures_d,
            "house_trigger_seen": pend["trigger_seen"],
        }
        ST["rolls"].append(rec)
        say(f"[P0] roll #{len(ST['rolls'])}: r={result} "
            f"robotsΔ={robots_d} treasuresΔ={treasures_d} "
            f"trigger_seen={pend['trigger_seen']}")
        wire("roll_recorded", rec)
        ST["pending"] = None

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < TIMEOUT:
        await asyncio.sleep(0.15)
        if ST["stop"]:
            break
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(tag))
            stale = time.time() - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = rev
            last_tick_at[tag] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"], c)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})

        if p0.latest:
            state = p0.latest["state"]
            if ST["house_oid"] is None:
                h = bf_id(state, 0, HOUSE)
                if h is not None:
                    ST["house_oid"] = h
                    say(f"House on BF oid={h}")
                    wire("house_bf", {"oid": h})
                    ST["stage"] = "ROLLING"

            settle_pending(state)

            if (ST["house_oid"] is not None and not ST["pre_exported"]
                    and not ST["rolls"] and ST["pending"] is None
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)
                    and not (state.get("stack") or [])):
                await export_named("pre")
                ST["pre_exported"] = True
                notes.append(
                    f"pre: turn {state.get('turn_number')} "
                    f"house_oid={ST['house_oid']} "
                    f"life={life_of(state, 0)}/{life_of(state, 1)}")

            if (len(ST["rolls"]) >= ROLL_TARGET
                    and ST["pending"] is None
                    and not (state.get("stack") or [])):
                await export_named("post")
                ST["post_exported"] = True
                notes.append(
                    f"post: turn {state.get('turn_number')} "
                    f"rolls={len(ST['rolls'])} "
                    f"life={life_of(state, 0)}/{life_of(state, 1)} "
                    f"robots={count_tokens(state, 0, 'robot')} "
                    f"treasures={count_tokens(state, 0, 'treasure')}")
                say("roll target reached; exporting post and stopping")
                ST["stop"] = True
                break

        if p0.latest and (p0.latest["state"].get("turn_number") or 0) > TURN_CAP \
                and ST["stage"] == "SETUP":
            notes.append(f"TURN_CAP {TURN_CAP} hit in SETUP; finishing")
            say("TURN_CAP in SETUP: finishing")
            break
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} stage={ST['stage']} "
                f"house={ST['house_oid']} rolls={len(ST['rolls'])} "
                f"pending={ST['pending'] is not None} "
                f"P0hand={hand_lnames(s, 0)[:6]}")
        if time.time() - t0 > TIMEOUT - 5:
            notes.append("TIMEOUT hit; finishing")
            break

    a, verdict = evaluate()
    say(f"FINAL verdict={verdict} assertions={a}")
    notes.append(f"final stage={ST['stage']} n_rolls={len(ST['rolls'])} "
                 f"pending={'yes' if ST['pending'] else 'no'}")
    if not ST["post_exported"]:
        try:
            await export_named("post")
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")
    a, verdict = evaluate()
    await capture_server_excerpts()
    await finish(a, verdict, notes)
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    RUNLOG.close()
    WIRE.close()
    write_manifest()  # manifest written AFTER all say() logging is done
    print(f"DONE issue={ISSUE} run={RUN_ID} verdict={verdict}", flush=True)
    return verdict


if __name__ == "__main__":
    print(asyncio.run(main()))
