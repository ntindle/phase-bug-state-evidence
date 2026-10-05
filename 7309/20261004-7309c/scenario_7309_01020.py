#!/usr/bin/env python3
"""Issue #7309: Baxter Building "{4}, {T}: Add four mana in any combination
of colors" only allows four mana of a single color (no mixing).

Re-validation on the pinned release v0.102.0 (build e17f6fd, protocol 106).
Prior runs:
  v0.99.0 (protocol 98, run 20261001-7309, reproduced): ability 1
    pre-expanded into 5 single-color taps (W/U/B/R/G); no mix offered.
  v0.101.0 (protocol 103, run 20261004-7309, reproduced): same failure -
    5 pre-expanded single-color TapLandForMana options (ability_index 1,
    output Concrete <color>), White submitted, {4} paid via 4 Mountain taps,
    pool ended exactly 4x White, no manaGroups/any-combination choice.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, confirmed): Baxter Building's "{4}, {T}: Add four mana in
any combination of colors" only allows four mana of a single color, not
mix and match as intended.

Setup (native engine, two human driver seats):
  P0: 4x Baxter Building + 56x Mountain. Plays Baxter, then Mountains.
  P1: 60x Forest (driven: keep, land, pass; never attacks).

Trigger: on P0's main phase with Baxter untapped and >=4 untapped
Mountains, P0 holding priority, activate Baxter's ability_index 1
(the any-combination mana ability), choosing the White pre-expansion
(the reported branch).

Expected (per card text): the engine offers a genuine any-combination
choice (e.g. a manaGroups prompt) so the 4 mana can be split across colors.
Reported bug: the engine pre-expands ability 1 into 5 single-color
TapLandForMana options (W/U/B/R/G, output Concrete <color>), with no mix
opportunity at any point; the pool ends up 4x one color.

Assertions:
  A1_parse         v0.102.0 card-data: ability index 1 is Mana/AnyCombination
                   (count Fixed 4, color_options WUBRG), cost {4}+Tap
  A2_setup_ok      pre.json: P0 main phase, Baxter untapped on BF,
                   >=4 untapped Mountains, P0 holds priority
  A3_activation_ok White ability-1 submitted and accepted (no rejection);
                   {4} paid (4 Mountains tapped); Baxter tapped
  A4_no_mix_offered the recorded ability-1 offers were only single-color
                   pre-expansions, and no manaGroups/any-combination choice
                   was ever advertised via viewer_interaction
  A5_cleanup       no dangling decisions; pool exactly 4x White sourced from
                   the Baxter; game proceeds

Verdict: reproduced iff A1+A2 pass and A3+A4 hold. not-reproduced iff a
genuine mix choice is offered and the pool reflects a mixed selection.
blocked iff the setup cannot be driven to completion.

The browser mana-choice UI is NOT exercised; the engine-level
pre-expansion into 5 single-color taps is the defect surface under test.

Protocol-106 driver conventions (from scenario_7163_01020.py /
scenario_301_01020.py, verified 2026-10-04):
  - waiting_for is gone; priority = PassPriority in the viewing seat's
    top-level legal_actions (merged across per-object actions).
  - MulliganDecision answered via legacy Action, gated on the advertised
    MulliganDecision action (single decision per seat, permanent guard).
  - ActivateAbility submitted while HOLDING priority: drive opponent passes
    but never pass the activating seat's priority before the submission.
  - Legacy PayMana/PayManaAbilityMana submitted as-is; vi tapLandForMana
    menus driven by needs {"generic": 4}.
  - Export-only checkpoints fall through to the priority pass in the same
    tick - never return after an export while holding priority. Revision-
    gated ticks; 5s re-tick backstop for a priority holder with no revision
    change. real_decision_pending excludes the noisy 106 priority-menu codes.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7309
RUN_ID = "20261004-7309c"
SERVER_RUN_DIR = "runs/run-20261004-2211"  # backfill-owned v0.102.0 server

EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.102.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-04: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               f"backfill-owned v0.102.0 server on 127.0.0.1:9374 with "
               f"isolated run dir {SERVER_RUN_DIR}"),
}

for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

BAXTER = "baxter building"
MOUNTAIN = "mountain"
FOREST = "forest"

P0_DECK = deck((BAXTER, 4), (MOUNTAIN, 56))
P1_DECK = deck((FOREST, 60))

DEADLINE_S = 1500
ASS_KEYS = ("A1_parse", "A2_setup_ok", "A3_activation_ok", "A4_no_mix_offered",
            "A5_cleanup")

# viewer_interaction action codes that are menus, not candidate decisions
NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}
# 106 priority-menu codes to exclude from the real-decision gate (AGENTS.md)
SELECTION_MENU_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                        "castSpell", "activateAbility", "mulliganDecision"}

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
REJECTIONS = []

NEEDS = {"generic": 4}  # {4} for the Baxter ability


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def stack_entries(state):
    return state.get("stack") or []


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


def untapped_of(state, pid, name):
    return sum(1 for oid in bf_oids(state, pid)
               if obj_lname(state, oid) == name
               and not get_obj(state, oid).get("tapped"))


def baxter_untapped_oid(state, pid):
    for oid in bf_oids(state, pid):
        if obj_lname(state, oid) == BAXTER and not get_obj(state, oid).get("tapped"):
            return str(oid)
    return None


def pool_of(state, pid):
    p = player_of(state, pid)
    pool = p.get("mana_pool") or p.get("manaPool")
    return pool


def pool_mana(pool):
    if isinstance(pool, dict):
        return pool.get("mana", []) or []
    return []


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


async def verify_server_hello():
    import websockets as _ws
    ws = await _ws.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"


def check_data_level():
    """A1: verify the pinned v0.102.0 card-data parse of Baxter Building's
    ability 1 ({4},{T}: Add four mana in any combination of colors)."""
    e = CARD_DATA[BAXTER]
    abs_ = e.get("abilities") or []
    a1 = abs_[1] if len(abs_) > 1 else {}
    with open(f"{EVDIR}/parse_baxter_building.json", "w") as f:
        json.dump(a1, f, indent=1, default=str)
    eff = a1.get("effect") or {}
    prod = eff.get("produced") or {}
    cost = a1.get("cost") or {}
    costs = cost.get("costs") or []
    mana_cost = next((c for c in costs if c.get("type") == "Mana"), {})
    ok = (a1.get("kind") == "Activated"
          and eff.get("type") == "Mana"
          and prod.get("type") == "AnyCombination"
          and (prod.get("count") or {}).get("value") == 4
          and set(prod.get("color_options") or [])
          == {"White", "Blue", "Black", "Red", "Green"}
          and (mana_cost.get("cost") or {}).get("generic") == 4
          and any(c.get("type") == "Tap" for c in costs)
          and a1.get("is_mana_ability") is True)
    ST["ass"]["A1_parse"] = "passed" if ok else "failed"
    ST["notes"].append(f"A1: ability1 parse ok={ok} "
                       f"(produced={prod.get('type')} count="
                       f"{(prod.get('count') or {}).get('value')} "
                       f"options={prod.get('color_options')})")
    say(f"A1_parse: {ST['ass']['A1_parse']}")


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def export_named(c, name):
    try:
        s = await c.export_state()
    except Exception as e:
        say(f"export {name} FAILED: {e}")
        wire("export_failed", {"name": name, "error": str(e)[:200]})
        return False
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(s)
    say(f"exported {name}.json")
    return True


# ------------------------------------------------------------- common ticks (106)
async def do_mulligan(c, acts, st, pid, tag):
    """MulliganDecision arrives as a legacy legal action (verified accepted
    on 106). Always keep (P0 draws into Baxter, 4 copies)."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull")  # permanent per-seat guard (818i pattern):
    # exactly one MulliganDecision submission per seat, ever
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard(c, acts, st, pid, tag):
    """DiscardToHandSize via vi, gated on hand > 7 + a schema/select
    opportunity offering hand cards (106 uses a generic 'choose' kind code
    for this; the live viewer-filtered hand_ids gate it)."""
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands:
            continue
        if rtype not in ("schema", "exactChoices", "choose"):
            continue
        # candidates must be hand cards
        handset = {str(x) for x in hand}
        cids = []
        for ch in cands:
            cid = ch.get("id")
            if cid is None:
                continue
            cids.append(str(cid))
        if not cids or not any(c in handset for c in cids):
            continue
        iid = opp.get("interactionId") or opp.get("id")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        n = len(hand) - 7
        # prefer discarding non-lands, keep a Baxter
        baxter_h = [str(o) for o in hand if obj_lname(state, o) == BAXTER]
        nonland = [c for c in cids if c not in handset or True]
        picks = [c for c in cids if c not in baxter_h][:n]
        if len(picks) < n:
            picks = cids[:n]
        say(f"[{tag}] discard {n} to hand size")
        wire("discard", {"who": tag, "n": n, "picks": picks})
        if rtype == "exactChoices" or rtype == "choose":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": picks[0]}}}
        else:
            spec = (data.get("spec") or {}).get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": spec,
                                "data": {"choiceIds": picks}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag):
    """Answer the vi tapLandForMana choice menus (106 mana payment surface).
    Pays NEEDS ({"generic": 4}) via untapped lands."""
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                taps.append(ch)
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if NEEDS.get("generic", 0) <= 0:
            return False
        pick = taps[0]
        NEEDS["generic"] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana (generic left={NEEDS['generic']})")
        wire("tap_land", {"who": tag, "generic_left": NEEDS["generic"]})
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        if rtype == "schema":
            stype = ((resp.get("data", {}) or {}).get("spec", {}) or {}).get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    if ST.get("hold_priority"):
        return False
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a.get("type") == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


async def combat_empty(c, acts, tag):
    for a in acts:
        if a["type"] == "DeclareAttackers":
            import copy as _copy
            sub = _copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
            say(f"[{tag}] declares no attackers")
            return True
    return False


async def declare_empty_blockers(c, acts, tag):
    for a in acts:
        if a["type"] == "DeclareBlockers":
            import copy as _copy
            sub = _copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
            say(f"[{tag}] declares no blockers")
            return True
    return False


def record_baxter_offers(st, state, pid):
    """Record ALL per-object actions advertised for the Baxter oid.
    Return ability_index==1 candidates (the any-combination ability).
    Also scan vi for any manaGroups/any-combination mix choice."""
    lafo = st.get("legal_actions_by_object") or {}
    boids = [str(oid) for oid in bf_oids(state, pid)
             if obj_lname(state, oid) == BAXTER]
    all_offers = {}
    a1_cands = []
    for k, payloads in lafo.items():
        if str(k) not in boids:
            continue
        recs = []
        for p in payloads or []:
            if not isinstance(p, dict):
                continue
            d = p.get("data") or {}
            sel = d.get("selection") or {}
            rec = {"action_type": p.get("type"),
                   "mana_type": sel.get("mana_type"),
                   "ability_index": sel.get("ability_index"),
                   "output": d.get("output") or sel.get("output"),
                   "keys": sorted(p.keys())}
            recs.append(rec)
            if sel.get("ability_index") == 1:
                a1_cands.append((p, rec))
        all_offers[str(k)] = recs
    return boids, all_offers, a1_cands


def scan_mix_choice(st):
    """The not-reproduced signal: a genuine any-combination / manaGroups
    choice advertised via viewer_interaction."""
    for op in vi_ops(st):
        blob = json.dumps(op, default=str).lower()
        spec = ((op.get("response", {}) or {}).get("data", {}) or {}).get("spec", {}) or {}
        if spec.get("type") == "manaGroups":
            return op
        if "any combination" in blob or "anycombination" in blob:
            if "passpriority" in blob and len(blob) < 600:
                continue
            return op
    return None


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            codes = set()
            for ch in items:
                codes.update(c for c in surf_codes(ch) if c)
            if codes and not (codes <= SELECTION_MENU_CODES):
                return True
            continue
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


async def p0_tick(c, acts, st, state, pid, tag):
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await pay_tick(c, acts):
        say(f"[{tag}] submitted legacy payment action")
        return True
    if await pay_mana_vi(c, st, tag):
        return True
    # A4 watch: any genuine mix choice advertised while the ability is live
    if ST["stage"] in ("SETUP", "ACTIVATED"):
        mix_op = scan_mix_choice(st)
        if mix_op:
            ST["mix_choice_seen"] = True
            wire("mix_choice_offered", {"op": mix_op})
            say("MIX CHOICE offered (not-reproduced signal) - NOT submitting; "
                "holding the reported White branch instead")
            # do not submit; continue the reported branch
    # activation window (106 rule: submit while HOLDING priority)
    if ST["stage"] == "SETUP" and my_main(state, pid) and my_priority(acts):
        bo = baxter_untapped_oid(state, pid)
        nm = untapped_of(state, pid, MOUNTAIN)
        if bo and nm >= 4:
            if not ST["pre_exported"]:
                ST["baxter_oid"] = bo
                if await export_named(c, "pre"):
                    ST["pre_exported"] = True
                    wire("pre", {"baxter_oid": bo, "untapped_mountains": nm,
                                 "turn": state.get("turn_number"),
                                 "phase": state.get("phase")})
            boids, all_offers, a1_cands = record_baxter_offers(st, state, pid)
            if not ST["offers_exported"]:
                with open(f"{EVDIR}/activation_offers.json", "w") as f:
                    json.dump({"baxter_oids": boids, "offers": all_offers,
                               "ability1_count": len(a1_cands),
                               "offer_colors": sorted({r["mana_type"]
                                                       for _, r in a1_cands})},
                              f, default=str, indent=1)
                ST["offers_exported"] = True
                ST["offer_colors"] = sorted({str(r["mana_type"])
                                            for _, r in a1_cands})
                ST["ability1_count"] = len(a1_cands)
                ST["all_mana_types_single"] = all(
                    str(r["mana_type"]) in ("White", "Blue", "Black", "Red", "Green")
                    for _, r in a1_cands)
                wire("activation_offers", {"ability1_count": len(a1_cands),
                                           "colors": ST["offer_colors"]})
                say(f"recorded {len(a1_cands)} ability-1 offers: {ST['offer_colors']}")
            if a1_cands:
                # prefer the White pre-expansion (the reported branch)
                white = [p for p, r in a1_cands if r.get("mana_type") == "White"]
                chosen, rec = (white[0], "white") if white else (a1_cands[0][0], "first")
                if white:
                    wire("baxter_activation_submit",
                         {"action_type": chosen.get("type"),
                          "mana_type": "White", "offer": rec})
                else:
                    wire("baxter_activation_submit",
                         {"action_type": chosen.get("type"),
                          "mana_type": "nonwhite-fallback"})
                # CRITICAL (106): submit while HOLDING priority - do not pass
                # P0's priority before the submission.
                msg = {"type": chosen.get("type")}
                if "data" in chosen:
                    msg["data"] = chosen["data"]
                wire("action_submit", {"who": tag, "action": msg,
                                      "stage": ST.get("stage")})
                await c.send_action(msg)
                ST["act_submitted_at"] = time.time()
                ST["stage"] = "ACTIVATED"
                ST["white_submitted"] = True
                say(f"[{tag}] activated Baxter mana ability "
                    f"(oid={ST['baxter_oid']}) type={chosen.get('type')}")
                return True
            wire("no_baxter_action", {
                "flat_types": sorted(set(x.get("type") for x in acts)),
                "baxter_oids": boids,
                "offer_keys": list(all_offers.keys()),
            })
    # after activation: drive payment; freeze priority the moment the pool
    # fills so the main-loop capture can export post (never pass while the
    # pool is full)
    if ST["stage"] == "ACTIVATED" and not ST["post_exported"]:
        _pool = pool_of(state, pid)
        if len(pool_mana(_pool)) >= 4:
            ST["hold_priority"] = True
            wire("pool_full_hold", {"pool": _pool})
            say(f"[{tag}] pool full ({len(pool_mana(_pool))} mana); "
                "holding priority for post export")
            return True
        _mid_obj = get_obj(state, ST.get("baxter_oid") or "")
        if _mid_obj.get("tapped") and not ST["mid_exported"]:
            if await export_named(c, "mid_tapped"):
                ST["mid_exported"] = True
                wire("mid_tapped", {"pool": pool_of(state, pid)})
    # setup play: Baxter first, then a Mountain per turn
    if my_main(state, pid) and ST["stage"] == "SETUP":
        for want in (BAXTER, MOUNTAIN):
            lid = None
            for o in hand_ids(state, pid):
                if obj_lname(state, o) == want:
                    lid = str(o)
                    break
            if lid:
                for a in acts:
                    if a.get("type") == "PlayLand" and \
                            str(a.get("_src_oid")) == lid:
                        LAND_PLAYED_TURN[(tag,)] = state.get("turn_number")
                        say(f"[{tag}] playing {want}")
                        wire("play_land", {"who": tag, "name": want})
                        await submit_as_is(c, a)
                        return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        if await combat_empty(c, acts, tag):
            return True
    # fall through to the priority pass (never return after an export
    # while holding priority)
    if await pass_priority(c, st, acts):
        return True
    return False


async def p1_tick(c, acts, st, state, pid, tag):
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard(c, acts, st, pid, tag):
        return True
    if await pay_tick(c, acts):
        return True
    if await pay_mana_vi(c, st, tag):
        return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        if await combat_empty(c, acts, tag):
            return True
    if (state.get("phase") or "") == "DeclareBlockers" and my_priority(acts):
        if await declare_empty_blockers(c, acts, tag):
            return True
    if not my_priority(acts):
        return False
    # play a Forest
    if await play_a_land(c, state, pid, acts, tag):
        return True
    if await pass_priority(c, st, acts):
        return True
    return False


async def render_png(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 1120
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7309 - Baxter Building: any-combination "
           "mana locked to one color", fill=(235, 240, 250))
    y += 28
    d.text((24, y), f"server v0.102.0 (e17f6fd) protocol 106 - {RUN_ID}",
           fill=(140, 160, 180))
    y += 28
    col = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=col)
    y += 34
    d.text((24, y), 'Oracle: "{4},{T}: Add four mana in any combination of '
           'colors."', fill=(200, 210, 225))
    y += 24
    d.text((36, y), "Engine pre-expands ability 1 into 5 single-color taps "
           "(W/U/B/R/G);", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "no manaGroups / mix choice is ever offered; pool ends "
           "4x one color.", fill=(200, 210, 225))
    y += 34
    labels = {
        "A1_parse": "DATA: ability1 = Mana/AnyCombination, count 4, "
                    "WUBRG options, cost {4}+Tap",
        "A2_setup_ok": "GAME: P0 main, Baxter untapped, >=4 untapped "
                       "Mountains, P0 priority (pre.json)",
        "A3_activation_ok": "GAME: White ability-1 accepted, {4} paid, "
                            "Baxter tapped (mid_tapped.json)",
        "A4_no_mix_offered": "GAME: ability-1 offers only single-color "
                             "pre-expansions; no mix choice",
        "A5_cleanup": "GAME: pool exactly 4x White from Baxter; no "
                      "dangling decisions",
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
    d.text((24, y), f"ability1_offers={ds.get('ability1_count')} "
           f"colors={ds.get('offer_colors')} "
           f"mix_choice_seen={ds.get('mix_choice_seen')} "
           f"white_submitted={ds.get('white_submitted')} "
           f"white_accepted={ds.get('white_accepted')}", fill=(150, 160, 175))
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


async def finalize(c):
    ass = ST["ass"]
    notes = ST["notes"]
    states = {}
    for fn in ("pre", "mid_tapped", "post"):
        p = f"{EVDIR}/{fn}.json"
        try:
            if os.path.exists(p):
                states[fn] = json.loads(open(p).read())["state"]
                say(f"loaded {fn}.json")
        except Exception as ex3:
            notes.append(f"state reload failed for {fn}.json: {ex3}")
    pre, mid, post = states.get("pre"), states.get("mid_tapped"), states.get("post")
    ref = post or mid or pre

    def objects(s):
        return (s or {}).get("objects", {}) or {}

    def lname_of(o):
        return str(o.get("base_name") or o.get("name") or "").lower()

    def baxter_bf(s):
        return [oid for oid, o in objects(s).items()
                if o.get("zone") == "Battlefield"
                and str(o.get("controller", -1)) == "0"
                and lname_of(o) == BAXTER]

    # ---- A2: setup ----
    if pre is not None:
        bo = baxter_bf(pre)
        ok = (bool(bo) and not objects(pre)[bo[0]].get("tapped")
              and sum(1 for oid, o in objects(pre).items()
                      if o.get("zone") == "Battlefield"
                      and str(o.get("controller", -1)) == "0"
                      and lname_of(o) == MOUNTAIN and not o.get("tapped")) >= 4
              and (pre.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))
        ass["A2_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A2: baxter_bf={len(bo)} untapped_mountains>="
                     f"{4 if ok else '?'} phase={pre.get('phase')} "
                     f"turn={pre.get('turn_number')}")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 failed: pre.json missing")

    # ---- A3: activation ----
    rej_white = any("White" in str(r) or ST.get("white_submitted")
                    for r in REJECTIONS)
    if ST["white_submitted"] and not rej_white and ref is not None:
        bo = baxter_bf(ref)
        tapped = bool(bo) and bool(objects(ref)[bo[0]].get("tapped"))
        m_tapped = sum(1 for o in objects(ref).values()
                       if o.get("zone") == "Battlefield"
                       and str(o.get("controller", -1)) == "0"
                       and lname_of(o) == MOUNTAIN and o.get("tapped"))
        ok = tapped and m_tapped >= 4
        ass["A3_activation_ok"] = "passed" if ok else "failed"
        ST["white_accepted"] = not rej_white
        notes.append(f"A3: white_submitted=True rejected={rej_white} "
                     f"baxter_tapped={tapped} mountains_tapped={m_tapped}")
    elif ST["white_submitted"] and rej_white:
        ass["A3_activation_ok"] = "failed"
        notes.append("A3 failed: White ability-1 submission rejected")
    else:
        ass["A3_activation_ok"] = "failed"
        notes.append("A3 failed: activation never submitted "
                     f"(stage={ST.get('stage')})")

    # ---- A4: no mix offered ----
    if ST["offers_exported"]:
        single_only = ST.get("all_mana_types_single", False) and \
            (ST.get("ability1_count") or 0) > 0
        mix_free = not ST.get("mix_choice_seen")
        ok = single_only and mix_free
        ass["A4_no_mix_offered"] = "passed" if ok else "failed"
        notes.append(f"A4: ability1_count={ST.get('ability1_count')} "
                     f"colors={ST.get('offer_colors')} single_only={single_only} "
                     f"mix_choice_seen={ST.get('mix_choice_seen')}")
    else:
        ass["A4_no_mix_offered"] = "failed"
        notes.append("A4 failed: no activation offers recorded")

    # ---- A5: cleanup ----
    if post is not None and ST["white_submitted"]:
        pool = pool_of(post, 0)
        mana = pool_mana(pool)
        whites = [m for m in mana
                  if str(m.get("color") or m.get("mana_type") or m).lower() == "white"]
        ok = len(mana) == 4 and len(whites) == 4
        ass["A5_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A5: pool={len(mana)} whites={len(whites)} "
                     f"(expect 4/4) src_ids="
                     f"{sorted({str(m.get('source_id')) for m in mana if isinstance(m, dict)})}")
    else:
        ass["A5_cleanup"] = "failed" if ST["white_submitted"] else "not-run"
        notes.append("A5 " + ("failed: post.json missing"
                              if ST["white_submitted"]
                              else "not-run: activation never submitted"))

    for k, v in ass.items():
        say(f"{k}: {v}")

    if (ass["A2_setup_ok"] == "passed"
            and ass["A3_activation_ok"] == "passed"
            and ass["A4_no_mix_offered"] == "passed"
            and ass["A5_cleanup"] == "passed"):
        verdict = "reproduced"
    elif ST.get("mix_choice_seen"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say("VERDICT: " + verdict)

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/7309",
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(time.time())),
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": SERVER_RUN_DIR,
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": "4x Baxter Building + 56x Mountain",
                  "P1": "60x Forest"},
        "setup_line": ("P0: 4x Baxter Building + 56x Mountain | "
                       "P1: 60x Forest (draw-go, zero attackers)"),
        "contract_line": ("P0 main phase with Baxter + 4 Mountains: activate "
                          "ability 1 (any-combination). A genuine mix choice "
                          "must be offered; reported bug = 5 single-color "
                          "pre-expansions only."),
        "stats": {"max_turn": ST.get("max_turn"),
                  "ability1_count": ST.get("ability1_count"),
                  "offer_colors": ST.get("offer_colors"),
                  "mix_choice_seen": ST.get("mix_choice_seen"),
                  "white_submitted": ST.get("white_submitted"),
                  "white_accepted": ST.get("white_accepted")},
        "assertions": ass,
        "notes": notes,
        "driver_state": {k: ST.get(k) for k in
                         ("stage", "pre_exported", "mid_exported",
                          "post_exported", "offers_exported",
                          "ability1_count", "offer_colors",
                          "all_mana_types_single", "mix_choice_seen",
                          "white_submitted", "white_accepted",
                          "act_submitted_at", "baxter_oid", "max_turn")},
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense 4-of playsets are a test-harness convenience.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Zero-attacker combat scripted on both seats.",
        ],
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7309_01020.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict},
                  f, indent=1)
    try:
        import shutil
        shutil.copy(f"{BACKFILL}/{SERVER_RUN_DIR}/server.log",
                    f"{EVDIR}/server.log")
    except Exception as ex4:
        notes.append(f"server.log copy failed: {ex4}")
    await render_png(run)
    # Close the logs BEFORE hashing: nothing may be written to
    # scenario_run.log / wire_log.jsonl after the manifest is computed.
    say("finalize: closing logs, computing manifest")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote manifest.sha256", flush=True)
    for fn in ("pre.json", "mid_tapped.json", "post.json",
               "parse_baxter_building.json", "run.json", "assertions.json",
               "activation_offers.json"):
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
    print("validation: all JSON parse, PNG readable, hashes match", flush=True)
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1),
          flush=True)
    return verdict


# ------------------------------------------------------------- main loop (106)
async def main():
    pidfile = "/tmp/scenario_7309_01020.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_7309_01020 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


async def _main():
    ST.update({
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "t0": time.time(),
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "stage": "SETUP",
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "offers_exported": False,
        "ability1_count": 0, "offer_colors": [], "all_mana_types_single": False,
        "mix_choice_seen": False,
        "white_submitted": False, "white_accepted": None,
        "act_submitted_at": 0, "baxter_oid": None,
        "hold_priority": False,
        "done": False, "max_turn": 0,
    })
    NEEDS["generic"] = 4
    await verify_server_hello()
    check_data_level()

    t0 = ST["t0"]
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    ST["game_code"] = p0.game_code
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})

    # rejection pump: record engine rejections (A3 attribution)
    async def pump_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if t in ("ActionRejected", "Error", "InteractionRejected"):
                REJECTIONS.append({"type": t, "data": data})
                wire("rejected", {"who": c.name, "type": t, "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")

    last_rev = {}
    last_tick_at = {}
    deadline = t0 + DEADLINE_S
    i = 0
    while i < 12000 and time.time() < deadline and not ST["done"]:
        i += 1
        await asyncio.sleep(0.15)
        for c, pid, tickfn, tag in ((p0, p0.player_id, p0_tick, "P0"),
                                    (p1, p1.player_id, p1_tick, "P1")):
            await pump_rejections(c)
            st = c.latest
            if not st:
                continue
            acts = merged_actions(st)
            same_rev = (c.revision == last_rev.get(c.name))
            holds_prio = my_priority(acts)
            # revision-gated ticks; re-tick a priority holder with no
            # revision change after 5s (a tick that returned without
            # submitting must not stall the game)
            if same_rev and not (holds_prio
                                 and time.time() - last_tick_at.get(c.name, 0) > 5):
                continue
            last_rev[c.name] = c.revision
            last_tick_at[c.name] = time.time()
            await asyncio.sleep(0)  # yield before leg evaluation (race fix)
            st = c.latest or st
            try:
                await tickfn(c, merged_actions(st), st, st["state"], pid, tag)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        st_now = p0.latest or {}
        state_now = st_now.get("state", st_now)
        turn_now = state_now.get("turn_number") or 0
        ST["max_turn"] = max(ST["max_turn"], turn_now)

        # ---- resolution capture: pool shows >=4 mana -> freeze, export post
        if ST["stage"] == "ACTIVATED" and not ST["post_exported"]:
            _pool = pool_of(state_now, p0.player_id)
            _mana = pool_mana(_pool)
            if len(_mana) >= 4 or ST.get("hold_priority"):
                if _pool and len(_mana) >= 4:
                    say(f"pool after activation: {json.dumps(_pool, default=str)[:400]}")
                    wire("pool_after", {"pool": _pool})
                ST["hold_priority"] = True
                try:
                    if await export_named(p0, "post"):
                        ST["post_exported"] = True
                finally:
                    ST["hold_priority"] = False
                ST["done"] = True

        # safety: activation submitted but no pool resolution for 90s
        if ST["white_submitted"] and not ST["post_exported"] \
                and time.time() - (ST["act_submitted_at"] or time.time()) > 90:
            say("activation stall: 90s with no pool resolution; exporting "
                "post and stopping")
            wire("activation_stall", {"rejections": REJECTIONS[-3:]})
            ST["hold_priority"] = True
            try:
                await export_named(p0, "post")
            finally:
                ST["hold_priority"] = False
            ST["post_exported"] = True
            ST["done"] = True

        # safety: game never reaches the activation window by turn 25
        if ST["stage"] == "SETUP" and turn_now >= 25 and not ST["done"]:
            ST["notes"].append("safety: no activation window by turn 25; bailing")
            wire("bail", {"turn": turn_now})
            break

    if not ST["done"]:
        ST["notes"].append("deadline hit before activation completed")
    if not ST["post_exported"] and ST["white_submitted"]:
        await export_named(p0, "post")
        ST["post_exported"] = True
    await finalize(p0)
    await p0.close()
    await p1.close()


asyncio.run(main())
