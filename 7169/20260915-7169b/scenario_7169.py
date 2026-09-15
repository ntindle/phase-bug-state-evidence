#!/usr/bin/env python3
"""Issue #7169: Rooftop Storm -- {0} alternative cost not offered from graveyard.

Oracle: "You may pay {0} rather than pay the mana cost for Zombie creature
spells you cast."

Reported: the {0} alternative cost does NOT apply to Zombie creature spells
cast from the graveyard (e.g. Gravecrawler).

Parse (pinned v0.83.0 card-data.json):
  Rooftop Storm static: CastWithAlternativeCost {cost {0}},
    affected = Typed{Creature, Subtype Zombie}, controller You,
    affected_zone null, active_zones [Battlefield].
  Gravecrawler static: GraveyardCastPermission (active_zones [Graveyard]),
    condition "control a Zombie" (Typed Subtype Zombie, controller You,
    InZone Battlefield).
Engine source (v0.82.0 checkout, same family as v0.83.0): casting.rs
  granted_spell_alternative_cost_for() documents that zone-less grants
  (Rooftop Storm class) keep "hand-only reach" for non-hand origins --
  the alternative-cost offer is raised as AdditionalCost::Choice via
  WaitingFor::OptionalCostChoice; for non-hand origins the match requires an
  origin-zone-scoped filter branch, which Rooftop Storm lacks. The offer is
  therefore expected (per the engine's own design) to be absent from GY casts.

Plan (native engine, v0.83.0 / protocol 70, two human driver seats):
  P0: 8x rooftop storm / 8x gravecrawler / 8x diregraf ghoul /
      8x augur of bolas (non-Zombie control) / 14x island / 14x swamp.
  P1: 60x forest, fully passive (lands, passes).
  P0 casts Rooftop Storm ({5}{U}) from hand, then Diregraf Ghoul from hand
  (HAND leg: accept the {0} offer), keeps a Ghoul on the battlefield (also
  enables Gravecrawler's graveyard-cast permission), discards a Gravecrawler
  to the graveyard at cleanup (deliberate pick), then casts Gravecrawler from
  the graveyard (GY leg: observe only), then casts Augur of Bolas from hand
  (NONZ leg: observe only).

Behavioral contract:
  A1 setup_ok      Storm on P0 BF; Diregraf Ghoul on P0 BF; a Gravecrawler in
                   P0's graveyard; pre.json exported before the GY cast.
  A2 hand_offer    OptionalCostChoice with AdditionalCost::Choice observed
                   when casting Diregraf Ghoul from hand; {0} accepted; Ghoul
                   resolved to the battlefield. (control passes)
  A3 gy_offer_absent no such offer observed during the Gravecrawler-from-GY
                   cast window. (expect this to PASS == bug reproduced)
  A4 gy_cast_completes the GY cast completed and Gravecrawler resolved onto
                   the battlefield (the permission path works; only the alt
                   cost is missing).
  A5 nonz_unaffected no {0} offer for Augur of Bolas (non-Zombie) from hand;
                   cast completed normally.
  A6 cleanup       stack empty, post.json exported.

Verdict: reproduced iff A1+A2 pass and A3 passes (offer missing from the GY
         cast while present from hand).
         not-reproduced iff A2 passes and the GY cast DID offer {0}.
         blocked iff A1 or A2 fails.

Driver notes:
  - The offer surfaces as WaitingFor::OptionalCostChoice with
    waiting_for.data.cost containing the AdditionalCost::Choice variant;
    the pay=true choice selects the preferred ({0}) branch (casting_costs.rs
    handle_decide_additional_cost). Identify via action-surface code
    decideOptionalCost + value surface (role=pay, value=true), falling back
    to (role=accept, value=true).
  - Never pass priority while an OptionalCostChoice for seat 0 is pending;
    return without acting. Always fall through to PassPriority otherwise.
  - Record the ACTUALLY submitted choice id per leg.
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
RUN_ID = "20260915-7169b"
ISSUE = 7169
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

STORM = "rooftop storm"
CRAWLER = "gravecrawler"
GHOUL = "diregraf ghoul"
AUGUR = "augur of bolas"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [(STORM, 8), (CRAWLER, 8), (GHOUL, 8), (AUGUR, 8),
           (ISLAND, 14), (SWAMP, 14)]
P1_DECK = [(FOREST, 60)]

TIMEOUT = 1500
TURN_CAP = 50
WINDOW_TIMEOUT = 25

SERVER_IDENTITY = {
    "validated_version": "v0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "server_binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
}

ST = {}
SUBMITTED = set()
MULLS = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> HAND -> STOCK -> PRE_READY -> GY ->
                           #   MID -> NONZ -> DONE
        "stop": False,
        "storm_oid": None,
        "ghoul_oid": None,
        "crawler_gy_oid": None,
        "augur_oid": None,
        "cast_window": None,  # {"spell","leg","t0","origin"}
        "offer_seen": {"hand": False, "gy": False, "nonz": False},
        "offer_accepted": {"hand": False, "gy": False, "nonz": False},
        "wf_seen": {"hand": [], "gy": [], "nonz": []},
        "submitted_choice": {"hand": None, "gy": None, "nonz": None},
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "hand_leg_cast": False,
        "gy_leg_cast": False,
        "nonz_leg_cast": False,
        "quiescent_ticks": 0,
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


def gy_ids(state, pid, key=None):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        if o.get("zone") != "Graveyard":
            continue
        if key is not None and lname(state, oid) != key:
            continue
        own = o.get("owner")
        ctrl = o.get("controller")
        if own == pid or ctrl == pid or (own is None and ctrl is None):
            out.append(int(oid))
    return out


def life_of(state, pid):
    return player_of(state, pid).get("life")


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


def ref_key(ref):
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
        return None
    return None


def candidate_ref_oid(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d["reference"]
    return None


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


def find_pay_choice(chs):
    """Pick the 'pay the preferred branch' choice of an OptionalCostChoice:
    decideOptionalCost action code + value surface (role=pay, value=true),
    falling back to (role=accept, value=true)."""
    fallback = None
    for ch in chs:
        has_code = False
        pay_true = False
        accept_true = False
        for sf in ch.get("surfaces", []) or []:
            d = sf.get("data") or {}
            code = str(d.get("code") or "")
            if "decideOptionalCost" in code or "decideAdditionalCost" in code:
                has_code = True
            if sf.get("type") == "value":
                role = str(d.get("role") or "").lower()
                val = str(d.get("value") or "").lower()
                if role == "pay" and val == "true":
                    pay_true = True
                if role == "accept" and val == "true":
                    accept_true = True
        if has_code and pay_true:
            return ch
        if accept_true and fallback is None:
            fallback = ch
    return fallback


async def do_mulligan(c, pid, tag):
    st = c.latest
    a = find_action(merged_actions(st), "MulliganDecision")
    if not a:
        return False
    names = hand_lnames(st["state"], pid)
    nlands = sum(1 for n in names if n in (ISLAND, SWAMP, FOREST))
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


async def discard_handsize_tick(c, pid, tag, st, state):
    """P0: deliberately discard a Gravecrawler while stocking; default
    priority (lands first) otherwise. P1: default priority."""
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
    crawler_in_hand = [o for o in hand if lname(state, o) == CRAWLER]
    if (tag == "P0" and ST["crawler_gy_oid"] is None and crawler_in_hand):
        prio = crawler_in_hand + [o for o in hand
                                  if o not in crawler_in_hand]
        deliberate = True
    else:
        prio = sorted(
            hand, key=lambda o: (0 if lname(state, o) in
                                 (ISLAND, SWAMP, FOREST) else 1))
        deliberate = False
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
        ref2cid = {}
        for ch in cands:
            r = ref_key(candidate_ref_oid(ch))
            if r is not None:
                ref2cid[r] = ch.get("id")
        picks = [ref2cid[str(x)] for x in prio if str(x) in ref2cid][:n]
        if len(picks) < n:
            picks = [ch.get("id") for ch in cands[:n]]
        SUBMITTED.add(key)
        spec = (rdata.get("spec") or {}).get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": spec, "data": {"choiceIds": picks}}}
        say(f"[{tag}] discarding {n} to hand size"
            f"{' (deliberate crawler)' if deliberate else ''}")
        wire("interaction_submission",
             {"who": tag, "submission": sub, "kind": "DiscardToHandSize",
              "deliberate_crawler": deliberate,
              "picked": [lname(state, x) for x in prio[:n]]})
        await c.send_interaction(sub)
        if deliberate:
            ST["crawler_gy_oid"] = int(prio[0])
            say(f"[P0] Gravecrawler stocked to GY (hand oid={prio[0]})")
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


async def cast_named(c, acts, state, name, tag, leg):
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        d = a.get("data") or {}
        oid = (d.get("object_id") or d.get("source_id") or a.get("_src_oid"))
        if oid is not None and lname(state, int(oid)) == name:
            say(f"[{tag}] casting {name} (oid={oid}) leg={leg}")
            wire("cast", {"who": tag, "name": name, "oid": int(oid),
                          "leg": leg})
            await submit_as_is(c, a)
            if leg in ST["offer_seen"]:
                ST["cast_window"] = {"spell": name, "leg": leg,
                                     "t0": time.time(), "oid": int(oid)}
                if leg == "hand":
                    ST["hand_leg_cast"] = True
                if leg == "nonz":
                    ST["nonz_leg_cast"] = True
            return True
    return False


async def cast_crawler_from_gy(c, acts, state, tag):
    """GY leg: submit ONLY a CastSpell whose object is actually in P0's
    graveyard (never a hand cast mislabeled as the GY leg)."""
    crawlers = set(gy_ids(state, 0, CRAWLER))
    if not crawlers:
        return False
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        d = a.get("data") or {}
        oid = d.get("object_id") or d.get("source_id") or a.get("_src_oid")
        if oid is not None and int(oid) in crawlers:
            o = get_obj(state, int(oid))
            say(f"[{tag}] casting gravecrawler from GRAVEYARD (oid={oid})")
            wire("cast", {"who": tag, "name": CRAWLER, "oid": int(oid),
                          "leg": "gy", "origin_zone": o.get("zone")})
            await submit_as_is(c, a)
            ST["cast_window"] = {"spell": CRAWLER, "leg": "gy",
                                 "t0": time.time(), "oid": int(oid)}
            ST["gy_leg_cast"] = True
            ST["crawler_gy_oid"] = int(oid)
            return True
    return False


async def play_land_pref(c, acts, state, pref, tag):
    plays = [a for a in acts if a["type"] == "PlayLand"]
    if not plays:
        return False
    for name in pref:
        for a in plays:
            d = a.get("data") or {}
            oid = d.get("object_id") or a.get("_src_oid")
            if oid is not None and lname(state, int(oid)) == name:
                say(f"[{tag}] playing land {name}")
                wire("play_land", {"who": tag, "name": name,
                                   "oid": int(oid)})
                await submit_as_is(c, a)
                return True
    await submit_as_is(c, plays[0])
    return True


async def altcost_window_tick(c, st, state, tag):
    """Handle the OptionalCostChoice window for an in-flight cast.

    Records every OptionalCostChoice seen during the leg's cast window;
    answers only the AdditionalCost::Choice variant (the alt-cost offer)
    with pay=true (the preferred {0} branch). Never passes priority while
    the prompt is pending.
    """
    win = ST.get("cast_window")
    if win is None:
        return False
    leg = win["leg"]
    wf = wf_of(state)
    wtype = wf.get("type") or ""
    if wtype == "OptionalCostChoice" and str(
            (wf.get("data") or {}).get("player")) == "0":
        if wtype not in ST["wf_seen"][leg]:
            ST["wf_seen"][leg].append(wtype)
        wf_data = json.loads(json.dumps(wf.get("data") or {}, default=str))
        wire("cast_prompt", {"leg": leg, "spell": win["spell"],
                             "wf": wf_data,
                             "opportunities": json.loads(json.dumps(
                                 ((st.get("viewer_interaction") or {})
                                  .get("opportunities") or []),
                                 default=str))})
        cost_blob = json.dumps(wf_data.get("cost", {}), default=str).lower()
        is_choice = "choice" in cost_blob
        if not is_choice:
            say(f"[{tag}] OptionalCostChoice WITHOUT Choice variant "
                f"(leg={leg}); leaving unanswered")
            return True  # hold priority; do not auto-answer
        ST["offer_seen"][leg] = True
        vi = get_vi(st)
        if not vi:
            return True
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            key = (tag, "altcost", leg, str(iid))
            if key in SUBMITTED:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            pick = find_pay_choice(chs)
            if pick is None:
                wire("altcost_no_pay_choice",
                     {"leg": leg,
                      "opp": json.loads(json.dumps(opp, default=str))})
                say(f"[{tag}] alt-cost offer WITHOUT identifiable pay "
                    f"choice (leg={leg}); leaving unanswered")
                SUBMITTED.add(key)
                return True
            SUBMITTED.add(key)
            ST["offer_accepted"][leg] = True
            ST["submitted_choice"][leg] = pick.get("id")
            say(f"[{tag}] alt-cost offer ACCEPTED (pay={{0}}) leg={leg} "
                f"choice={pick.get('id')}")
            await answer_vi(c, opp, pick, tag)
            return True
        return True
    return False


def window_clear_check(state):
    """Clear the cast window once the spell has left its origin zone or is
    on the stack; detect a silent stall via timeout."""
    win = ST.get("cast_window")
    if win is None:
        return
    oid = win["oid"]
    o = get_obj(state, oid)
    on_stack = any(str(e.get("id")) == str(oid)
                   or win["spell"] in str(e.get("name") or "").lower()
                   for e in stack_entries(state))
    left_origin = o and o.get("zone") not in ("Hand", "Graveyard", None)
    if on_stack or left_origin or time.time() - win["t0"] > WINDOW_TIMEOUT:
        say(f"[P0] cast window closed leg={win['leg']} "
            f"offer_seen={ST['offer_seen'][win['leg']]} "
            f"accepted={ST['offer_accepted'][win['leg']]} "
            f"on_stack={on_stack} left_origin={left_origin}")
        wire("cast_window_closed",
             {"leg": win["leg"], "offer_seen": ST["offer_seen"][win["leg"]],
              "accepted": ST["offer_accepted"][win["leg"]]})
        ST["cast_window"] = None


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
    if await discard_handsize_tick(c, 0, "P0", st, state):
        return
    # The alt-cost window owns OptionalCostChoice prompts; never pass
    # priority while one is pending for our seat.
    if await altcost_window_tick(c, st, state, "P0"):
        return
    if not my_priority(state, 0):
        return
    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 0 \
            and ST["cast_window"] is None:
        stage = ST["stage"]
        stack_empty = not (state.get("stack") or [])
        if stage == "SETUP":
            await play_land_pref(c, acts, state, [ISLAND, SWAMP], "P0")
            if (stack_empty and ST["storm_oid"] is None
                    and STORM in hand_lnames(state, 0)
                    and untapped_lands(state, 0, ISLAND)
                    and len(untapped_lands(state, 0)) >= 6):
                if await cast_named(c, acts, state, STORM, "P0", "storm"):
                    ST["stage"] = "HAND"
                    return
        elif stage == "HAND":
            await play_land_pref(c, acts, state, [ISLAND, SWAMP], "P0")
            if (stack_empty and not ST["hand_leg_cast"]
                    and GHOUL in hand_lnames(state, 0)):
                if await cast_named(c, acts, state, GHOUL, "P0", "hand"):
                    return
        elif stage == "STOCK":
            await play_land_pref(c, acts, state, [ISLAND, SWAMP], "P0")
            # accumulate; cleanup discards stock the crawler
        elif stage == "GY":
            await play_land_pref(c, acts, state, [ISLAND, SWAMP], "P0")
            if stack_empty and not ST["gy_leg_cast"]:
                if await cast_crawler_from_gy(c, acts, state, "P0"):
                    return
        elif stage == "NONZ":
            await play_land_pref(c, acts, state, [ISLAND, SWAMP], "P0")
            if (stack_empty and not ST["nonz_leg_cast"]
                    and AUGUR in hand_lnames(state, 0)):
                if await cast_named(c, acts, state, AUGUR, "P0", "nonz"):
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
    if await discard_handsize_tick(c, 1, "P1", st, state):
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
        raw = open(p).read()
        return json.loads(raw)["state"]
    return None


def evaluate():
    a = {}
    pre_st = load_st("pre.json")
    mid_st = load_st("mid.json")
    post_st = load_st("post.json")

    storm_oid = ST.get("storm_oid")
    ghoul_oid = ST.get("ghoul_oid")
    crawler_oid = ST.get("crawler_gy_oid")

    # A1: fixture ready + pre exported before the GY cast.
    storm_bf = (storm_oid is not None and pre_st is not None
                and get_obj(pre_st, storm_oid).get("zone") == "Battlefield")
    ghoul_bf = (ghoul_oid is not None and pre_st is not None
                and get_obj(pre_st, ghoul_oid).get("zone") == "Battlefield")
    crawler_gy = (crawler_oid is not None and pre_st is not None
                  and get_obj(pre_st, crawler_oid).get("zone") == "Graveyard")
    a["A1_setup_ok"] = ("passed" if (storm_bf and ghoul_bf and crawler_gy
                                     and ST["pre_exported"]) else "failed")
    say(f"A1: storm_bf={storm_bf} ghoul_bf={ghoul_bf} "
        f"crawler_gy={crawler_gy} pre_exported={ST['pre_exported']}")

    # A2: hand control -- {0} offer observed and accepted for the Ghoul.
    a["A2_hand_offer"] = ("passed" if (ST["offer_seen"]["hand"]
                                       and ST["offer_accepted"]["hand"]
                                       and ghoul_bf) else "failed")

    # A3: GY leg -- no {0} offer observed during the graveyard cast.
    a["A3_gy_offer_absent"] = ("passed" if not ST["offer_seen"]["gy"]
                               else "failed")

    # A4: the GY cast itself completed (crawler resolved to the BF).
    crawler_bf = False
    if mid_st is not None and crawler_oid is not None:
        crawler_bf = (get_obj(mid_st, crawler_oid).get("zone")
                      == "Battlefield")
    elif post_st is not None and crawler_oid is not None:
        crawler_bf = (get_obj(post_st, crawler_oid).get("zone")
                      == "Battlefield")
    a["A4_gy_cast_completes"] = ("passed" if crawler_bf else "failed")
    say(f"A4: crawler_bf={crawler_bf} (mid={mid_st is not None})")

    # A5: non-Zombie control -- no {0} offer for Augur, cast completed.
    augur_bf = False
    if post_st is not None and ST.get("augur_oid") is not None:
        augur_bf = (get_obj(post_st, ST["augur_oid"]).get("zone")
                    == "Battlefield")
    a["A5_nonz_unaffected"] = ("passed" if (not ST["offer_seen"]["nonz"]
                                            and augur_bf) else "failed")
    say(f"A5: nonz_offer_seen={ST['offer_seen']['nonz']} "
        f"augur_bf={augur_bf}")

    # A6: cleanup.
    if post_st is not None:
        a["A6_cleanup"] = ("passed" if not (post_st.get("stack") or [])
                           else "failed")
    else:
        a["A6_cleanup"] = "not-run"

    if a["A1_setup_ok"] == "failed" or a["A2_hand_offer"] == "failed":
        verdict = "blocked"
    elif (a["A3_gy_offer_absent"] == "passed"
            and a["A4_gy_cast_completes"] == "passed"):
        verdict = "reproduced"
    elif (a["A2_hand_offer"] == "passed"
            and ST["offer_seen"]["gy"]):
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
    W, H = 1000, 1010
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7169 - Rooftop Storm: {0} alt cost "
           "not offered from graveyard", fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           f"server v{SERVER_IDENTITY['validated_version']} "
           f"({SERVER_IDENTITY['build_commit']}) protocol 70 - 2026-09-15",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else (120, 220, 120))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire observations):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "Storm+Ghoul on P0 BF; Crawler in P0 GY; pre exported",
        "A2_hand_offer": "hand Ghoul: {0} offer seen+accepted, resolved",
        "A3_gy_offer_absent": "GY Crawler cast: NO {0} offer [bug => PASS]",
        "A4_gy_cast_completes": "GY Crawler cast resolved to the BF",
        "A5_nonz_unaffected": "non-Zombie Augur: no {0} offer, resolved",
        "A6_cleanup": "stack empty, post.json exported",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12
    d.text((24, y), "Parse evidence (v0.83.0 card-data.json):",
           fill=(200, 210, 225))
    y += 24
    for line in [
        "Rooftop Storm: CastWithAlternativeCost{cost {0}},",
        "  affected Typed{Creature, Subtype Zombie} controller You,",
        "  affected_zone null, active_zones [Battlefield]",
        "Gravecrawler: GraveyardCastPermission, active_zones [Graveyard],",
        "  condition control a Zombie (InZone Battlefield)",
    ]:
        d.text((36, y), line[:112], fill=(170, 180, 195))
        y += 22
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:12]:
        d.text((36, y), n[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    try:
        subprocess.run(["cp", os.path.abspath(__file__),
                        f"{EVDIR}/scenario_7169.py"], check=False)
        say("copied scenario_7169.py into EVDIR")
    except Exception as e:
        say(f"scenario copy failed: {e}")
    files = ["pre.json", "mid.json", "post.json", "run.json",
             "scenario_7169.py", "wire_log.jsonl", "scenario_run.log",
             "summary.png", "parse_rooftop.json", "parse_gravecrawler.json"]
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


async def finish(a, verdict, notes):
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.83.0/data/"
                            f"card-data.json"))
        for key, fn in (("rooftop storm", "parse_rooftop.json"),
                        ("gravecrawler", "parse_gravecrawler.json")):
            card = cd.get(key, {})
            with open(f"{EVDIR}/{fn}", "w") as f:
                json.dump({"name": card.get("name"),
                           "oracle_text": card.get("oracle_text"),
                           "static_abilities":
                               card.get("static_abilities")}, f, indent=2)
    except Exception as e:
        notes.append(f"parse json failed: {e}")
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "verdict": verdict,
        "assertions": a,
        "server_identity": SERVER_IDENTITY,
        "fixture": {
            "storm_oid": ST.get("storm_oid"),
            "ghoul_oid": ST.get("ghoul_oid"),
            "crawler_gy_oid": ST.get("crawler_gy_oid"),
            "augur_oid": ST.get("augur_oid"),
            "offer_seen": ST["offer_seen"],
            "offer_accepted": ST["offer_accepted"],
            "submitted_choice": ST["submitted_choice"],
            "wf_seen": ST["wf_seen"],
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
    say("server identity pinned: v0.83.0 (b7a59d4) protocol 70, mode Full")

    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
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
            if ST["storm_oid"] is None:
                sv = bf_id(state, 0, STORM)
                if sv is not None:
                    ST["storm_oid"] = sv
                    say(f"Storm on BF oid={sv}")
                    wire("storm_bf", {"oid": sv})
            if ST["ghoul_oid"] is None:
                g = bf_id(state, 0, GHOUL)
                if g is not None:
                    ST["ghoul_oid"] = g
                    say(f"Ghoul on BF oid={g}")
                    wire("ghoul_bf", {"oid": g})
            if ST["augur_oid"] is None:
                au = bf_id(state, 0, AUGUR)
                if au is not None:
                    ST["augur_oid"] = au
                    say(f"Augur on BF oid={au}")
                    wire("augur_bf", {"oid": au})
            if ST["crawler_gy_oid"] is None:
                cr = gy_ids(state, 0, CRAWLER)
                if cr:
                    ST["crawler_gy_oid"] = cr[0]
                    say(f"Crawler in GY oid={cr[0]}")
                    wire("crawler_gy", {"oid": cr[0]})

            # Stage transitions.
            if ST["stage"] == "HAND" and ST["ghoul_oid"] is not None \
                    and ST["cast_window"] is None:
                ST["stage"] = "STOCK"
                say("HAND leg complete; entering STOCK")
                wire("stage", {"stage": "STOCK"})
            if ST["stage"] == "STOCK" and ST["crawler_gy_oid"] is not None:
                ST["stage"] = "GY"
                say("STOCK complete; entering GY")
                wire("stage", {"stage": "GY"})
            # pre.json: fixture ready, P0 to act in main phase with the
            # GY crawler castable.
            if (ST["stage"] == "GY" and not ST["pre_exported"]
                    and ST["storm_oid"] is not None
                    and ST["ghoul_oid"] is not None
                    and ST["crawler_gy_oid"] is not None
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain")
                    and state.get("active_player") == 0
                    and my_priority(state, 0)
                    and ST["cast_window"] is None
                    and not (state.get("stack") or [])):
                await export_named("pre")
                ST["pre_exported"] = True
                notes.append(
                    f"pre: turn {state.get('turn_number')} "
                    f"storm_oid={ST['storm_oid']} "
                    f"ghoul_oid={ST['ghoul_oid']} "
                    f"crawler_gy_oid={ST['crawler_gy_oid']}")

            window_clear_check(state)

            # mid.json: GY cast resolved (crawler on BF), before NONZ leg.
            if (ST["stage"] == "GY" and ST["cast_window"] is None
                    and not ST["mid_exported"]
                    and ST["crawler_gy_oid"] is not None
                    and get_obj(state, ST["crawler_gy_oid"]).get("zone")
                    == "Battlefield"
                    and not (state.get("stack") or [])):
                await export_named("mid")
                ST["mid_exported"] = True
                ST["stage"] = "NONZ"
                notes.append(
                    f"mid: turn {state.get('turn_number')} "
                    f"gy_offer_seen={ST['offer_seen']['gy']} "
                    f"crawler resolved to BF")
                say(f"GY leg resolved: offer_seen={ST['offer_seen']['gy']}")

            # post.json: NONZ leg resolved, stack empty.
            if (ST["stage"] == "NONZ" and ST["cast_window"] is None
                    and not ST["post_exported"]
                    and ST.get("augur_oid") is not None
                    and not (state.get("stack") or [])):
                await export_named("post")
                ST["post_exported"] = True
                ST["stage"] = "DONE"
                notes.append(
                    f"post: turn {state.get('turn_number')} "
                    f"nonz_offer_seen={ST['offer_seen']['nonz']} "
                    f"life={life_of(state, 0)}/{life_of(state, 1)}")
                say("NONZ leg resolved; exporting post and stopping")
                ST["stop"] = True
                break

        if p0.latest and (p0.latest["state"].get("turn_number") or 0) > TURN_CAP \
                and ST["stage"] in ("SETUP", "HAND", "STOCK"):
            notes.append(f"TURN_CAP {TURN_CAP} hit in {ST['stage']}; finishing")
            say("TURN_CAP: finishing")
            break
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} stage={ST['stage']} "
                f"storm={ST['storm_oid']} ghoul={ST['ghoul_oid']} "
                f"crawler_gy={ST['crawler_gy_oid']} "
                f"window={ST['cast_window'] is not None} "
                f"offers={ST['offer_seen']} "
                f"P0hand={hand_lnames(s, 0)[:6]}")
        if time.time() - t0 > TIMEOUT - 5:
            notes.append("TIMEOUT hit; finishing")
            break

    a, verdict = evaluate()
    say(f"FINAL verdict={verdict} assertions={a}")
    notes.append(f"final stage={ST['stage']} offers={ST['offer_seen']} "
                 f"accepted={ST['offer_accepted']}")
    if not ST["post_exported"]:
        try:
            await export_named("post")
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")
    a, verdict = evaluate()
    await finish(a, verdict, notes)
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    RUNLOG.close()
    WIRE.close()
    write_manifest()
    print(f"DONE issue={ISSUE} run={RUN_ID} verdict={verdict}", flush=True)
    return verdict


if __name__ == "__main__":
    print(asyncio.run(main()))
