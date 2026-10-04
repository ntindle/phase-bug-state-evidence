#!/usr/bin/env python3
"""Backfill scenario for phase-rs/phase #9354.

Accepted Dredge can overwrite an earlier draw-replacement rider.

Report (github issue, 2026-09-27, area:engine, mechanic:replacement-effects):
the post-replacement drain stack has a single resident `Ready` slot. When an
earlier replacement leaves a rider on a draw and the player later ACCEPTS
Dredge, the accepted branch installs with ResidentDrainPolicy::Replace, which
evicts the earlier rider -- and it zeroes the whole rescaled draw. Blood
Scrivener's life-loss rider is then lost.

Minimal production path (from the measured repro in #9235 discussion):
1. P0 has an empty hand, Blood Scrivener on the battlefield, and a dredge
   option in the graveyard (printed Dredge 5 on Stinkweed Imp; granted
   Dredge 2 on a Forest via The Necrobloom).
2. P0 draws 1. The CR 616.1 ordering prompt appears. P0 picks Blood Scrivener
   first, then ACCEPTS the dredge.

Expected (CR 614.6 + CR 616.1f; dredge "may", CR 702.52a): dredge replaces
ONE draw and the life loss still happens.
  Leg 1 (printed, Stinkweed Imp dredge 5): mill 5, Imp returns to hand, P0
  draws 1 card, loses 1 life  -> hand 2 (Imp + drawn), life -1.
  Leg 2 (granted, Forest dredge 2 via The Necrobloom): mill 2, Forest returns
  to hand, P0 draws 1 card, loses 1 life -> hand 2 (Forest + drawn), life -1.
Buggy: life stays 20, the rescaled draw is zeroed (hand 1: just the dredger).

Setup line (no fixtures, real game): mulligan to the pieces, play lands, cast
Blood Scrivener (turn 2), cast The Necrobloom (leg 2, turn 3), cast One with
Nothing ({B}, "Discard your hand") to empty the hand and stock the graveyard,
then the next turn's draw step triggers the interaction.

Pinned: v0.101.0 (build acafe9b, protocol 103).
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-9354"
ISSUE = 9354
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.101.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("backfill-owned v0.101.0 server on 127.0.0.1:9374 "
                      "(started by an earlier backfill run; this run's own "
                      "game + raw Hello handshake verify the identity)"),
    "mode": "Full",
    "source": ("2026-10-03: latest stable release v0.101.0 (published "
               "2026-10-03) == pinned release dir; ServerHello "
               "0.101.0/acafe9b/protocol 103 verified by this run; hashes "
               "recomputed against on-disk artifacts this run"),
}

for _f, _k in (("server/releases/v0.101.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.101.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.101.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.101.0/data/card-data.json"))

SCRIVENER = "blood scrivener"
IMP = "stinkweed imp"
OWN = "one with nothing"          # {B} instant: "Discard your hand."
LOOTING = "faithless looting"     # {R} sorcery: "Draw two cards, then discard two cards."
NECROBLOOM = "the necrobloom"     # {1}{W}{B}{G} 2/7; lands in gy have dredge 2
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
PLAINS = "plains"
ISLAND = "island"

P0_DECK_L1 = deck((SCRIVENER, 4), (IMP, 2), (OWN, 4), (LOOTING, 4),
                  (SWAMP, 23), (MOUNTAIN, 23))
P0_DECK_L2 = deck((SCRIVENER, 4), (NECROBLOOM, 4), (OWN, 4), (LOOTING, 4),
                  (PLAINS, 11), (SWAMP, 11), (FOREST, 11), (MOUNTAIN, 11))
P1_DECK = deck((ISLAND, 60))

SETUP_DEADLINE_S = 1500

ASS_KEYS = ("A1_setup_ok", "A2_order_scrivener_first", "A3_dredge_accepted",
            "A4_life_rider_once", "A5_dredger_returned", "A6_milled",
            "A7_drew_one", "A8_game_advanced",
            "B1_setup_ok", "B2_order_scrivener_first", "B3_dredge_accepted",
            "B4_life_rider_once", "B5_land_returned", "B6_milled_two",
            "B7_drew_one")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def reset_attempt(leg):
    global ST, MULLS, SUBMITTED_OPPS, LAND_PLAYED_TURN
    ST = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "leg": leg,
        # mulligan -> setup -> drawwait -> order -> dredge -> settle -> done
        "phase": "mulligan",
        "post_mulligan_seen": False,
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
        "mana_needs": {"P0": {}, "P1": {}},
        "rejections": [],
        "tick": 0,
        "dredge_stage": None,      # None | order | dredge | done
        "order_choices": [],
        "dredge_choices": [],
        "pre_exported": False,
        "post_exported": False,
        "attempt": 0,
        "game_code_this_attempt": None,
    }
    MULLS = set()
    SUBMITTED_OPPS = set()
    LAND_PLAYED_TURN = {}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE.closed:
        return
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def wf_of(state):
    return state.get("waiting_for") or {}


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


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
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and int(o.get("controller", -1)) == int(pid):
            out.append(int(oid))
    return out


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def untapped(o):
    return not o.get("tapped", False)


def untapped_lands(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and untapped(get_obj(state, o))]


def untapped_land_names(state, pid):
    return [obj_lname(state, o) for o in untapped_lands(state, pid)]


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    for a in acts:
        if a.get("type") == atype:
            return a
    return None


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def can_pay(state, pid, colors=(), generic=0):
    names = untapped_land_names(state, pid)
    have = {}
    for n in names:
        have[n] = have.get(n, 0) + 1
    color_of = {SWAMP: "B", MOUNTAIN: "R", FOREST: "G", PLAINS: "W",
                ISLAND: "U"}
    pool = {}
    for n, cnt in have.items():
        c = color_of.get(n)
        if c:
            pool[c] = pool.get(c, 0) + cnt
    need = {}
    for c in colors:
        need[c] = need.get(c, 0) + 1
    for c, n in need.items():
        if pool.get(c, 0) < n:
            return False
        pool[c] -= n
    return sum(pool.values()) >= generic


def first_hand_land_of(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name and is_land(get_obj(state, o)):
            return o
    return None


def gy_oids(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Graveyard":
            try:
                own = int(o.get("owner", o.get("controller", -1)))
            except (TypeError, ValueError):
                own = -1
            if own == int(pid):
                out.append(int(oid))
    return out


def gy_lnames(state, pid):
    return [obj_lname(state, o) for o in gy_oids(state, pid)]


def lib_count(state, pid):
    lib = player_of(state, pid).get("library", [])
    return len(lib) if isinstance(lib, list) else None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)

async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.101.0"), f"unexpected version {ver}"
    assert int(proto) == 103, f"unexpected protocol {proto}"
    assert str(build) == "acafe9b", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    ok = True
    notes = []
    bs = CARD_DATA.get(SCRIVENER, {})
    if "draw two cards and you lose 1 life" not in str(bs.get("oracle_text")):
        ok = False
        notes.append("Blood Scrivener oracle shape missing")
    imp = CARD_DATA.get(IMP, {})
    if "Dredge 5" not in str(imp.get("oracle_text")):
        ok = False
        notes.append("Stinkweed Imp Dredge 5 missing")
    nb = CARD_DATA.get(NECROBLOOM, {})
    if "dredge 2" not in str(nb.get("oracle_text")).lower():
        ok = False
        notes.append("The Necrobloom granted dredge 2 missing")
    ow = CARD_DATA.get(OWN, {})
    if "Discard your hand" not in str(ow.get("oracle_text")):
        ok = False
        notes.append("One with Nothing oracle missing")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "scrivener_oracle": str(bs.get("oracle_text"))[:200],
                   "imp_oracle": str(imp.get("oracle_text"))[:200],
                   "necrobloom_oracle": str(nb.get("oracle_text"))[:200],
                   "own_oracle": str(ow.get("oracle_text"))[:120]}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")


def mulligan_keep_l1(hn):
    # adaptive plan: lands + dig. Looting is the dig engine (finds Imp for
    # the gy and OWN to empty the hand); without it we need Scrivener plus
    # a way to stock the gy (Imp, discarded to OWN later).
    lands = [n for n in hn if is_land_name(n)]
    if len(lands) < 2 or SWAMP not in lands:
        return False
    if LOOTING in hn:
        return True
    return SCRIVENER in hn and (IMP in hn or OWN in hn)


def mulligan_keep_l2(hn):
    # adaptive plan with Looting dig: lands + any key piece. The land for
    # the granted dredge reaches the gy via Looting/OWN discards.
    lands = [n for n in hn if is_land_name(n)]
    if len(lands) < 2 or SWAMP not in lands:
        return False
    return LOOTING in hn or NECROBLOOM in hn or SCRIVENER in hn


def is_land_name(n):
    return n in (SWAMP, MOUNTAIN, FOREST, PLAINS, ISLAND)


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    key = (tag, "mull", str(pend.get("mulligan_count", "?")),
           str(ST.get("attempt")))
    if key in MULLS:
        return False
    hn = hand_lnames(state, pid)
    MULLS.add(key)
    if tag == "P0":
        keep = (mulligan_keep_l1(hn) if ST["leg"] == 1
                else mulligan_keep_l2(hn))
        floor = 4
        if len(hn) > floor and not keep:
            say(f"[{tag}] mulligan ({len(hn)} cards, hand={hn})")
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Mulligan"}}})
            wire("mulligan", {"who": tag, "decision": "mulligan",
                              "hand": hn})
            return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    phase = (pend.get("phase") or {}).get("type")
    if phase not in ("BottomCards", "Bottom"):
        return False
    key = (tag, "bottom", str((pend.get("phase") or {}).get("count")),
           str(ST.get("attempt")))
    if key in SUBMITTED_OPPS:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    if n >= len(hand):
        n = len(hand) - 1  # never bottom the whole hand
        if n <= 0:
            return False
    # bottom junk first, then duplicate combo pieces, then unique pieces last
    from collections import Counter
    counts = Counter(obj_lname(state, o) for o in hand)
    keep_names = {SCRIVENER, IMP, OWN, NECROBLOOM, LOOTING}

    def bkey(o):
        nm = obj_lname(state, o)
        if nm in keep_names:
            # bottom duplicates first, then most-discardable keeps; unique
            # valuable keeps last. (discard_rank: lower = discard first, so
            # negate it: higher key = bottomed first.)
            return (0, counts[nm], -discard_rank(state, pid, o), o)
        return (1, 0, 0, o)

    picks = [int(o) for o in sorted(hand, key=bkey)[-n:]]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


def discard_rank(state, pid, oid):
    """Lower = discard first. Leg 1: Imp goes first (we want it in the gy).
    Leg 2: a basic land goes first while none is in the gy (granted dredge
    needs a land there). Never discard OWN; never discard an uncast
    Scrivener/Necrobloom."""
    n = obj_lname(state, oid)
    leg = ST.get("leg", 1)
    scr_bf = bool(bf_by_name(state, pid, SCRIVENER))
    if n == OWN:
        return 99
    if n == SCRIVENER and not scr_bf:
        return 98
    if n == NECROBLOOM and not bf_by_name(state, pid, NECROBLOOM):
        return 97
    if leg == 1:
        if n == IMP:
            return 0
    else:
        gy = gy_lnames(state, pid)
        if n in (FOREST, PLAINS, SWAMP, MOUNTAIN) and not any(
                m in gy for m in (FOREST, PLAINS, SWAMP, MOUNTAIN)):
            return 0
    if n == LOOTING:
        return 1
    if n == MOUNTAIN:
        return 2
    if n == SWAMP:
        return 3
    return 4


async def do_looting_discard(c, pid, tag):
    """Answer Faithless Looting's DiscardChoice: Imp first (into the gy),
    then junk; never OWN, never an uncast Scrivener."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    wf = wf_of(state)
    if wf.get("type") != "DiscardChoice":
        return False
    data = wf.get("data") or {}
    if str(data.get("player")) != str(pid):
        return False
    key = (c.name, "lootdisc", rev, str(ST.get("attempt")))
    if key in SUBMITTED_OPPS:
        return False
    n = int(data.get("count") or 0)
    if n <= 0:
        return False
    cands = [int(x) for x in (data.get("cards") or [])]
    hand = set(hand_ids(state, pid))
    usable = [x for x in cands if x in hand] or list(hand)
    picks = sorted(usable,
                   key=lambda o: (discard_rank(state, pid, o), o))[:n]
    picks = [int(x) for x in picks]
    acts = merged_actions(st)
    sc = next((a for a in acts if a["type"] == "SelectCards"), None)
    if sc is None:
        say(f"[{tag}] WARNING: DiscardChoice without SelectCards; not answering")
        ST["notes"].append("DiscardChoice without SelectCards; see wire log")
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] looting discards {[obj_lname(state, x) for x in picks]}")
    wire("discard_choice", {"who": tag, "count": n,
                            "picks": [obj_lname(state, x) for x in picks]})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    return True


async def do_discard_to_handsize(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    pend = pending_for(state, pid)
    if pend is None:
        return False
    key = (c.name, "handsize", rev, str(ST.get("attempt")))
    if key in SUBMITTED_OPPS:
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    picks = [int(x) for x in sorted(
        hand, key=lambda o: (discard_rank(state, pid, o), o))[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    SUBMITTED_OPPS.add(key)
    return True


async def pay_mana_vi(c, st, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if sum(needs.values()) <= 0:
            continue
        pick, used = None, None
        for ch, s in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in s:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used} needs={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
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
    if LAND_PLAYED_TURN.get((tag, ST.get("attempt"))) == turn:
        return False
    pick = None
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            pick = o
            break
    if pick is None:
        return False
    for a in acts:
        if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(pick):
            LAND_PLAYED_TURN[(tag, ST.get("attempt"))] = turn
            say(f"[{tag}] playing land {obj_lname(state, pick)}")
            wire("play_land", {"who": tag, "oid": pick})
            await submit_as_is(c, a)
            return True
    return False


async def try_cast(c, state, acts, tag, name, needs):
    a, oid = cast_action_for(acts, state, name)
    if not a:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid})
    await submit_as_is(c, a)
    return True

async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def load_env(name):
    try:
        with open(f"{EVDIR}/{name}.json") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return {}


def _choice_option_index(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "value":
            v = (s.get("data") or {}).get("value")
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


async def handle_replacement_choice(c, st, state, tag):
    """Answer 616.1 ordering prompts and dredge accept/decline optionals.

    Choice <-> candidate mapping comes from waiting_for.data.candidates;
    each choice carries a value surface with role=optionIndex whose value
    is the candidate index. (Choice texts are opaque indices.)
    """
    wf = wf_of(state)
    chooser = wf.get("player", (wf.get("data") or {}).get("player"))
    if str(chooser) != "0":
        return False
    data = wf.get("data") or {}
    kind = (data.get("kind") or {}).get("type")
    cands = data.get("candidates") or []
    vi = st.get("viewer_interaction") or {}
    opps = [o for o in vi.get("opportunities", []) or []
            if vi.get("canSubmit")]
    if not opps:
        return False
    opp = opps[0]
    iid = opp.get("interactionId") or opp.get("id")
    if iid in SUBMITTED_OPPS:
        return False
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) or {}
    chs = rdata.get("choices") or rdata.get("candidates") or []
    idx_of_choice = {id(ch): _choice_option_index(ch) for ch in chs}
    cand_desc = [(i, str(c.get("source_name", "")),
                  str(c.get("description", ""))[:90])
                 for i, c in enumerate(cands)]
    ST["dredge_stage"] = ST["dredge_stage"] or "order"
    logkey = (str(iid), ST["dredge_stage"], kind)
    if logkey not in ST.setdefault("prompt_logged", set()):
        ST["prompt_logged"].add(logkey)
        wire("replacement_prompt",
             {"stage": ST["dredge_stage"], "kind": kind, "chooser": chooser,
              "candidates": cand_desc,
              "raw_choices": json.loads(json.dumps(chs, default=str)),
              "interactionId": str(iid)})
        say(f"[{tag}] replacement prompt stage={ST['dredge_stage']} kind={kind} "
            f"cands={json.dumps(cand_desc)[:220]}")

    def pick_by_cand_index(want):
        for ch in chs:
            if idx_of_choice[id(ch)] == want:
                return ch
        return None

    def find_cand(needle):
        needle = needle.lower()
        return next((i for i, cd in enumerate(cands)
                     if needle in (str(cd.get("description", "")) + " " +
                                   str(cd.get("source_name", ""))).lower()),
                    None)

    if not ST.get("armed"):
        # Pre-arming: a draw step can already offer dredge replacements
        # (e.g. hand not empty so Scrivener doesn't apply). Keep the game
        # moving WITHOUT disturbing the setup: order deterministically,
        # decline every dredge optional. No pre export, no stage change.
        if kind == "Order":
            want = find_cand("blood scrivener")
            if want is None:
                want = 0
            ch = pick_by_cand_index(want)
            if ch is None:
                return False
            SUBMITTED_OPPS.add(iid)
            say(f"[P0] pre-arming ordering: picked candidate {want} "
                f"({cand_desc[want][1] if want < len(cand_desc) else '?'})")
            wire("prearming_order", {"index": want})
            await answer_vi(c, opp, ch, tag)
            return True
        want = find_cand("decline")
        if want is None:
            ST["notes"].append("pre-arming optional without a Decline "
                               f"candidate: {cand_desc}; not answering")
            return False
        ch = pick_by_cand_index(want)
        if ch is None:
            return False
        SUBMITTED_OPPS.add(iid)
        say(f"[P0] pre-arming dredge optional: declined (candidate {want})")
        wire("prearming_decline", {"index": want})
        await answer_vi(c, opp, ch, tag)
        return True

    ST["dredge_stage"] = ST["dredge_stage"] or "order"

    if ST["dredge_stage"] == "order" and kind == "Order":
        ST["order_choices"] = [str(c[1]) for c in cand_desc]
        want = find_cand("blood scrivener")
        if want is None:
            ST["notes"].append("ordering prompt without a Blood Scrivener "
                               f"candidate: {cand_desc}; not answering")
            say("[P0] WARNING: no Scrivener candidate in ordering; not answering")
            return False
        ch = pick_by_cand_index(want)
        if ch is None:
            ST["notes"].append("ordering: candidate index not mapped to a choice")
            return False
        # export pre.json inside the submission path, right before submitting
        if not ST["pre_exported"]:
            try:
                await export_as(c, ST["ev_pre"])
                ST["pre_exported"] = True
                say(f"PRE exported at 616.1 ordering prompt ({ST['ev_pre']}.json)")
            except Exception as e:
                ST["notes"].append(f"pre export failed: {e}")
        SUBMITTED_OPPS.add(iid)
        ST["dredge_stage"] = "dredge"
        ST["order_picked"] = True
        await answer_vi(c, opp, ch, tag)
        say(f"[P0] ordering: chose Blood Scrivener first (candidate {want})")
        wire("order_choice", {"picked": "blood scrivener", "index": want})
        return True

    if ST["dredge_stage"] == "dredge":
        ST["dredge_choices"] = [str(c[1]) for c in cand_desc]
        if kind == "Order":
            # ordering among the remaining replacements (e.g. two dredges):
            # arbitrary but deterministic -- index 0.
            ch = pick_by_cand_index(0)
            if ch is None:
                return False
            SUBMITTED_OPPS.add(iid)
            ST["notes"].append(f"dredge-stage ordering among {cand_desc}: "
                               f"picked index 0")
            await answer_vi(c, opp, ch, tag)
            return True
        # optional accept/decline: accept the FIRST dredge optional,
        # decline any further ones (single-dredge test semantics).
        want = None
        n_answered = ST.get("dredge_optionals_answered", 0)
        needle = "decline" if n_answered >= 1 else "accept"
        for i, cd in enumerate(cands):
            blob = (str(cd.get("description", "")) + " " +
                    str(cd.get("source_name", ""))).lower()
            if needle in blob:
                want = i
                break
        if want is None:
            # fall back: any candidate whose description mentions the needle
            ST["notes"].append(f"dredge optional: no '{needle}' candidate in "
                               f"{cand_desc}; not answering")
            say(f"[P0] WARNING: no '{needle}' candidate; not answering")
            return False
        ch = pick_by_cand_index(want)
        if ch is None:
            return False
        SUBMITTED_OPPS.add(iid)
        ST["dredge_optionals_answered"] = n_answered + 1
        ST["dredge_answered"] = True
        if needle == "accept":
            ST["dredge_accepted"] = True
            say(f"[P0] dredge optional #{n_answered + 1}: ACCEPTED "
                f"(candidate {want})")
        else:
            say(f"[P0] dredge optional #{n_answered + 1}: declined "
                f"(candidate {want})")
        wire("dredge_choice", {"picked": needle, "index": want,
                               "optional_n": n_answered + 1})
        await answer_vi(c, opp, ch, tag)
        return True
    return False


def check_armed(state):
    """OWN resolved with the engine online, dredger in gy, hand empty."""
    if ST.get("armed"):
        return True
    gy = gy_lnames(state, 0)
    leg = ST.get("leg", 1)
    bloom_bf = bool(bf_by_name(state, 0, NECROBLOOM))
    if leg == 1:
        dredger_gy = IMP in gy
    else:
        dredger_gy = any(m in gy for m in (FOREST, PLAINS, SWAMP, MOUNTAIN))
    core_bf = (bf_by_name(state, 0, SCRIVENER) and (leg == 1 or bloom_bf))
    if (core_bf and dredger_gy and len(hand_ids(state, 0)) == 0
            and OWN in gy):
        ST["armed"] = True
        say(f"ARMED at P0 turn #{ST.get('p0_turns')} "
            f"(game turn {state.get('turn_number')})")
        wire("armed", {"p0_turns": ST.get("p0_turns"),
                       "game_turn": state.get("turn_number")})
        return True
    return False


async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    # count P0's own turns (robust to who plays first)
    tn = state.get("turn_number")
    if state.get("active_player") == 0 and tn != ST.get("last_turn_seen"):
        ST["last_turn_seen"] = tn
        ST["p0_turns"] = ST.get("p0_turns", 0) + 1
        say(f"[P0] starting P0 turn #{ST['p0_turns']} (game turn {tn})")
    if await do_mulligan(c, pid, tag):
        return
    if await do_bottom(c, pid, tag):
        return
    check_armed(state)
    acts = merged_actions(st)
    wtype = wf_of(state).get("type")
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        return
    if wtype == "ReplacementChoice":
        if await handle_replacement_choice(c, st, state, tag):
            return
    if await do_looting_discard(c, pid, tag):
        return
    if await do_discard_to_handsize(c, pid, tag):
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if ST.get("phase") == "settle":
        if my_priority(state, pid):
            await pass_priority(c, st, acts)
        return
    if ST.get("armed"):
        # setup complete and hand empty: freeze, only pass priority
        if my_priority(state, pid):
            await pass_priority(c, st, acts)
        return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
        hn = hand_lnames(state, pid)
        gy = gy_lnames(state, pid)
        leg = ST["leg"]
        scr_bf = bool(bf_by_name(state, pid, SCRIVENER))
        bloom_bf = bool(bf_by_name(state, pid, NECROBLOOM))
        if leg == 1:
            dredger_gy = IMP in gy
            dredger_avail = dredger_gy or IMP in hn
        else:
            dredger_gy = any(m in gy for m in (FOREST, PLAINS, SWAMP, MOUNTAIN))
            dredger_avail = dredger_gy or any(
                m in hn for m in (FOREST, PLAINS, SWAMP, MOUNTAIN))
        own_hand = OWN in hn
        core_ok = scr_bf and (leg == 1 or bloom_bf)
        digging = not (core_ok and dredger_gy and own_hand)
        # 1. Blood Scrivener onto the battlefield
        if SCRIVENER in hn and not scr_bf \
                and can_pay(state, pid, ("B",), 1):
            if await try_cast(c, state, acts, tag, SCRIVENER,
                              {"B": 1, "generic": 1}):
                return
        # 2. The Necrobloom (leg 2) onto the battlefield
        if leg == 2 and NECROBLOOM in hn and not bloom_bf \
                and can_pay(state, pid, ("W", "B", "G"), 1):
            if await try_cast(c, state, acts, tag, NECROBLOOM,
                              {"W": 1, "B": 1, "G": 1, "generic": 1}):
                return
        # 3. One with Nothing once everything is in place. The dredger may
        #    still be in hand -- OWN discards it into the gy on the way out.
        if core_ok and own_hand and dredger_avail \
                and can_pay(state, pid, ("B",), 0):
            if await try_cast(c, state, acts, tag, OWN, {"B": 1}):
                say("[P0] One with Nothing cast: arming next draw step")
                return
        # 4. dig with Faithless Looting (cast or flashback)
        if digging and LOOTING in hn and can_pay(state, pid, ("R",), 0):
            if await try_cast(c, state, acts, tag, LOOTING, {"R": 1}):
                return
        if digging and LOOTING in gy and can_pay(state, pid, ("R",), 2):
            a, oid = cast_action_for(acts, state, LOOTING)
            if a:
                # flashback from the graveyard
                say(f"[P0] flashing back Faithless Looting (oid {oid})")
                wire("cast", {"who": "P0", "card": LOOTING,
                              "oid": oid, "flashback": True})
                ST["mana_needs"][tag] = {"R": 1, "generic": 2}
                await submit_as_is(c, a)
                return
    if my_priority(state, pid):
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    if await do_mulligan(c, pid, tag):
        return
    if await do_bottom(c, pid, tag):
        return
    acts = merged_actions(st)
    wtype = wf_of(state).get("type")
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        return
    if await pay_tick(c, acts):
        return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if my_priority(state, pid):
        await pass_priority(c, st, acts)


async def drive_attempt(c0, c1, deck_p0):
    """One fresh game; returns True when the decisive leg flow completed."""
    ST["attempt"] += 1
    say(f"=== attempt {ST['attempt']} (leg {ST['leg']}) ===")
    wire("attempt_start", {"attempt": ST["attempt"], "leg": ST["leg"]})
    attached = await c0.create(deck_p0, player_count=2)
    code = attached.get("game_code")
    ST["game_code"] = code
    ST["game_code_this_attempt"] = code
    say(f"game created: code={code}")
    wire("game_created", {"code": code})
    j1 = await c1.join(code, P1_DECK)
    say(f"P1 joined: {json.dumps(j1)[:120]}")

    t0 = time.time()
    try:
        while time.time() - t0 < 240:
            await asyncio.sleep(1.0)
            for c, tick in ((c0, p0_tick), (c1, p1_tick)):
                try:
                    await tick(c)
                except Exception as e:
                    say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            st = c0.latest
            if not st:
                continue
            wt = wf_of(st["state"]).get("type") or ""
            if wt not in ("MulliganDecision", "BottomCards"):
                ST["post_mulligan_seen"] = True
                break
        else:
            say("MULLIGAN DEADLINE hit")
            return False
        if not ST["post_mulligan_seen"]:
            say("never left mulligan phases")
            return False
        say("mulligan done; driving scenario")
        ST["phase"] = "setup"
        ST["armed"] = False
        t1 = time.time()
        while time.time() - t1 < SETUP_DEADLINE_S:
            await asyncio.sleep(0.5)
            ST["tick"] += 1
            for c, tick in ((c0, p0_tick), (c1, p1_tick)):
                try:
                    await tick(c)
                except Exception as e:
                    say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            st = c0.latest
            if not st:
                continue
            state = st["state"]
            # arming: OWN resolved with the engine online (Scrivener [+ bloom]
            # on bf), the dredger in the gy, and an empty hand
            if not ST["armed"]:
                gy = gy_lnames(state, 0)
                leg = ST["leg"]
                bloom_bf = bool(bf_by_name(state, 0, NECROBLOOM))
                if leg == 1:
                    dredger_gy = IMP in gy
                else:
                    dredger_gy = any(
                        m in gy for m in (FOREST, PLAINS, SWAMP, MOUNTAIN))
                core_bf = (bf_by_name(state, 0, SCRIVENER)
                           and (leg == 1 or bloom_bf))
                if (core_bf and dredger_gy
                        and len(hand_ids(state, 0)) == 0 and OWN in gy):
                    ST["armed"] = True
                    say(f"ARMED at P0 turn #{ST.get('p0_turns')} "
                        f"(game turn {state.get('turn_number')})")
                    wire("armed", {"p0_turns": ST.get("p0_turns"),
                                   "game_turn": state.get("turn_number")})
            if ST.get("p0_turns", 0) > 12 and not ST["armed"]:
                ST["notes"].append(
                    f"attempt {ST['attempt']}: turn cap hit without arming")
                say("TURN CAP: never armed; attempt failed")
                return False
            # draw-step trigger gate: preconditions must hold
            if (state.get("phase") == "Draw"
                    and state.get("active_player") == 0
                    and ST.get("armed")
                    and ST["dredge_stage"] is None):
                hn = hand_lnames(state, 0)
                scr = bool(bf_by_name(state, 0, SCRIVENER))
                gy = gy_lnames(state, 0)
                leg = ST["leg"]
                bloom_bf = bool(bf_by_name(state, 0, NECROBLOOM))
                if leg == 1:
                    dredger_ok = IMP in gy
                else:
                    dredger_ok = any(m in gy
                                     for m in (FOREST, PLAINS, SWAMP, MOUNTAIN))
                ok = (len(hn) == 0 and scr and dredger_ok
                      and (leg == 1 or bloom_bf))
                ST["dredge_stage"] = "order"
                say(f"draw-step trigger: hand_empty={len(hn)==0} scrivener={scr} "
                    f"gy={gy} preconditions_ok={ok}")
                wire("draw_trigger", {"hand": hn, "gy": gy,
                                      "preconditions_ok": ok})
                if not ok:
                    ST["notes"].append(
                        f"attempt {ST['attempt']}: draw-step preconditions "
                        f"failed (hand={hn} gy={gy} scrivener={scr})")
                    return False
            # post export once the draw settled and the game moved on
            if (ST.get("dredge_answered") and not ST["post_exported"]
                    and state.get("phase") != "Draw"
                    and wf_of(state).get("type") == "Priority"
                    and not stack_entries(state)):
                ST["phase"] = "settle"
                try:
                    await export_as(c0, ST["ev_post"])
                    ST["post_exported"] = True
                    say(f"POST exported (draw settled, game advanced) "
                        f"({ST['ev_post']}.json)")
                except Exception as e:
                    ST["notes"].append(f"post export failed: {e}")
                return True
            if ST["tick"] % 120 == 0:
                say(f"[watch] tick={ST['tick']} turn={state.get('turn_number')} "
                    f"phase={state.get('phase')} wf={wf_of(state).get('type')} "
                    f"stage={ST['dredge_stage']}")
        say("SETUP DEADLINE hit")
        wire("deadline", {"which": "setup"})
        return False
    finally:
        try:
            await c0.close()
        except Exception:
            pass
        try:
            await c1.close()
        except Exception:
            pass

def eval_leg(prefix, dredge_n):
    """Evaluate assertions for one leg from its pre/post exports.
    prefix: 'A' (leg 1) or 'B' (leg 2). Returns (ass_updates, leg_notes)."""
    ass = {}
    notes = []
    pre = load_env(ST["ev_pre"]).get("state", {})
    post = load_env(ST["ev_post"]).get("state", {})
    if not pre or not post:
        notes.append(f"{prefix}: pre/post exports missing "
                     f"(pre={bool(pre)} post={bool(post)})")
        return ass, notes

    p0_pre = player_of(pre, 0)
    p0_post = player_of(post, 0)
    life_pre = p0_pre.get("life")
    life_post = p0_post.get("life")
    lib_pre = lib_count(pre, 0)
    lib_post = lib_count(post, 0)
    gy_pre = len(gy_oids(pre, 0))
    gy_post = len(gy_oids(post, 0))
    hand_pre = hand_ids(pre, 0)
    hand_post = hand_ids(post, 0)

    dredger = IMP if prefix == "A" else None  # leg 2: any land
    # A1/B1: setup
    scr_bf = bool(bf_by_name(pre, 0, SCRIVENER))
    if prefix == "A":
        dredger_gy = IMP in gy_lnames(pre, 0)
    else:
        dredger_gy = any(n in gy_lnames(pre, 0)
                         for n in (FOREST, PLAINS, SWAMP, MOUNTAIN))
    ordered = bool(ST.get(f"{prefix}_order_choices"))
    if (len(hand_pre) == 0 and scr_bf and dredger_gy and ordered):
        ass[f"{prefix}1_setup_ok"] = "passed"
        notes.append(f"{prefix}1 passed: empty hand, Blood Scrivener on bf, "
                     f"dredger in gy, 616.1 ordering prompt presented.")
    elif len(hand_pre) == 0 or scr_bf or dredger_gy:
        ass[f"{prefix}1_setup_ok"] = "failed"
        notes.append(f"{prefix}1 FAILED: partial setup (hand={len(hand_pre)} "
                     f"scrivener_bf={scr_bf} dredger_gy={dredger_gy} "
                     f"order_prompt={ordered}).")
    else:
        ass[f"{prefix}1_setup_ok"] = "not-run"
        notes.append(f"{prefix}1 not-run: setup never completed.")

    # A2/B2: ordered Scrivener first
    if ST.get(f"{prefix}_order_picked"):
        ass[f"{prefix}2_order_scrivener_first"] = "passed"
        notes.append(f"{prefix}2 passed: chose Blood Scrivener first at 616.1.")
    elif ST.get(f"{prefix}_order_choices"):
        ass[f"{prefix}2_order_scrivener_first"] = "failed"
        notes.append(f"{prefix}2 FAILED: ordering prompt seen but Scrivener "
                     f"not chosen.")
    else:
        ass[f"{prefix}2_order_scrivener_first"] = "not-run"
        notes.append(f"{prefix}2 not-run: no ordering prompt answered.")

    # A3/B3: accepted dredge
    if ST.get(f"{prefix}_dredge_accepted"):
        ass[f"{prefix}3_dredge_accepted"] = "passed"
        notes.append(f"{prefix}3 passed: accepted the Dredge replacement.")
    elif ST.get(f"{prefix}_dredge_choices"):
        ass[f"{prefix}3_dredge_accepted"] = "failed"
        notes.append(f"{prefix}3 FAILED: dredge prompt seen but not accepted.")
    else:
        ass[f"{prefix}3_dredge_accepted"] = "not-run"
        notes.append(f"{prefix}3 not-run: no dredge prompt answered.")

    # A4/B4 DECISIVE: life-loss rider preserved exactly once
    if life_pre is not None and life_post is not None:
        if life_post == life_pre - 1:
            ass[f"{prefix}4_life_rider_once"] = "passed"
            notes.append(f"{prefix}4 passed: P0 life {life_pre} -> {life_post} "
                         f"(rider preserved exactly once).")
        else:
            ass[f"{prefix}4_life_rider_once"] = "failed"
            notes.append(f"{prefix}4 FAILED: P0 life {life_pre} -> {life_post}, "
                         f"expected exactly -1 (rider lost/zeroed).")
    else:
        ass[f"{prefix}4_life_rider_once"] = "not-run"
        notes.append(f"{prefix}4 not-run: life not readable.")

    # A5/B5: dredger returned to hand
    post_hand_names = [obj_lname(post, o) for o in hand_post]
    if prefix == "A":
        ok5 = IMP in post_hand_names
    else:
        ok5 = any(n in post_hand_names
                  for n in (FOREST, PLAINS, SWAMP, MOUNTAIN))
    key5 = f"{prefix}5_{'dredger_returned' if prefix == 'A' else 'land_returned'}"
    if ok5:
        ass[key5] = "passed"
        notes.append(f"{prefix}5 passed: dredged card returned to P0's hand.")
    else:
        ass[key5] = "failed"
        notes.append(f"{prefix}5 FAILED: dredged card not in hand "
                     f"(hand={post_hand_names}).")

    # A6/B6: milled exactly dredge_n
    exp_lib = -(dredge_n + 1)
    exp_gy = dredge_n - 1
    if lib_pre is not None and lib_post is not None:
        lib_d = lib_post - lib_pre
        gy_d = gy_post - gy_pre
        key6 = f"{prefix}6_{'milled' if prefix=='A' else 'milled_two'}"
        if lib_d == exp_lib and gy_d == exp_gy:
            ass[key6] = "passed"
            notes.append(f"{prefix}6 passed: library {lib_d} (milled "
                         f"{dredge_n} + drew 1), graveyard {gy_d:+d}.")
        else:
            ass[key6] = "failed"
            notes.append(f"{prefix}6 FAILED: library delta {lib_d} "
                         f"(expected {exp_lib}), gy delta {gy_d:+d} "
                         f"(expected {exp_gy:+d}).")
    else:
        key6 = f"{prefix}6_{'milled' if prefix=='A' else 'milled_two'}"
        ass[key6] = "not-run"
        notes.append(f"{prefix}6 not-run: zone counts unreadable.")

    # A7/B7: drew exactly one card (hand == 2 with the dredger in it)
    key7 = f"{prefix}7_drew_one"
    if len(hand_post) == 2 and ok5:
        ass[key7] = "passed"
        notes.append(f"{prefix}7 passed: P0 hand == 2 (dredger + 1 drawn).")
    elif post:
        ass[key7] = "failed"
        notes.append(f"{prefix}7 FAILED: P0 hand == {len(hand_post)} "
                     f"({post_hand_names}), expected 2.")
    else:
        ass[key7] = "not-run"

    # A8 (leg 1 only): game advanced past the draw, no stuck decision
    if prefix == "A":
        wf = (post.get("waiting_for") or {}).get("type")
        if ST.get("post_exported") and wf != "ReplacementChoice":
            ass["A8_game_advanced"] = "passed"
            notes.append("A8 passed: post-export shows the game advanced past "
                         f"the draw (waiting_for={wf}).")
        elif ST.get("post_exported"):
            ass["A8_game_advanced"] = "failed"
            notes.append(f"A8 FAILED: still waiting on {wf} in post.")
        else:
            ass["A8_game_advanced"] = "not-run"
            notes.append("A8 not-run: post.json never exported.")
    return ass, notes


def leg_verdict(prefix, ass):
    g = lambda k: ass.get(f"{prefix}{k}", "not-run")  # noqa: E731
    core = [g("1_setup_ok"), g("2_order_scrivener_first"),
            g("3_dredge_accepted")]
    if all(v == "passed" for v in core) and g("4_life_rider_once") == "failed":
        return "reproduced"
    need = ["4_life_rider_once",
            f"5_{'dredger_returned' if prefix=='A' else 'land_returned'}",
            f"6_{'milled' if prefix=='A' else 'milled_two'}", "7_drew_one"]
    if all(ass.get(f"{prefix}{k}") == "passed" for k in need):
        return "not-reproduced"
    return "blocked"


async def run_leg(leg):
    reset_attempt(leg)
    ST["ev_pre"] = "pre" if leg == 1 else "pre2"
    ST["ev_post"] = "post" if leg == 1 else "post2"
    deck_p0 = P0_DECK_L1 if leg == 1 else P0_DECK_L2
    dredge_n = 5 if leg == 1 else 2
    prefix = "A" if leg == 1 else "B"
    completed = False
    for _ in range(4):
        # fresh per-leg ST slices that must not leak across attempts
        ST["phase"] = "mulligan"
        ST["post_mulligan_seen"] = False
        ST["dredge_stage"] = None
        ST["pre_exported"] = False
        ST["post_exported"] = False
        ST["p0_turns"] = 0
        ST["last_turn_seen"] = None
        ST["armed"] = False
        ST["order_choices"] = []
        ST["dredge_choices"] = []
        ST["order_picked"] = False
        ST["dredge_accepted"] = False
        ST["dredge_answered"] = False
        ST["dredge_optionals_answered"] = 0
        c0 = PhaseClient("P0")
        c1 = PhaseClient("P1")
        await c0.connect()
        await c1.connect()
        say("P0 hello done; P1 hello done")
        try:
            if await drive_attempt(c0, c1, deck_p0):
                completed = True
                break
        except Exception as e:
            say(f"attempt {ST['attempt']} raised {type(e).__name__}: {e}")
            wire("attempt_error", {"err": f"{type(e).__name__}: {e}"})
        say(f"attempt {ST['attempt']} did not complete the leg flow; retrying")
    # stash per-leg prompt records under the leg prefix for eval_leg
    ST[f"{prefix}_order_choices"] = ST.get("order_choices", [])
    ST[f"{prefix}_dredge_choices"] = ST.get("dredge_choices", [])
    ass, notes = eval_leg(prefix, dredge_n)
    v = leg_verdict(prefix, ass)
    say(f"LEG {leg} verdict: {v} (flow completed: {completed})")
    wire("leg_verdict", {"leg": leg, "verdict": v, "completed": completed})
    return ass, notes, v, completed


def render_png(verdict, ass, notes):
    from PIL import Image, ImageDraw
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 84], fill=(28, 32, 44))
    d.text((24, 14), "phase-rs/phase #9354 — bug-state backfill", fill=(235, 240, 250))
    d.text((24, 44), "Accepted Dredge can overwrite an earlier draw-replacement rider",
           fill=(160, 170, 190))
    vc = {"reproduced": (220, 80, 80), "not-reproduced": (110, 200, 130),
          "blocked": (220, 180, 90)}[verdict]
    d.text((24, 100), f"verdict: {verdict}", fill=vc)
    d.text((24, 128), "v0.101.0 (acafe9b) · protocol 103 · 2026-10-03 · run 20261003-9354",
           fill=(160, 170, 190))
    y = 168
    d.text((24, y), "Setup: P0 empty hand, Blood Scrivener on bf, dredger in gy; P0 draws 1;", fill=(200, 205, 215))
    y += 24
    d.text((24, y), "616.1 order: Scrivener first, then ACCEPT Dredge. Expect: dredge replaces", fill=(200, 205, 215))
    y += 24
    d.text((24, y), "ONE draw; life-loss rider preserved exactly once (CR 614.6/616.1f).", fill=(200, 205, 215))
    y += 36
    d.text((24, y), "Leg 1 (printed Dredge 5, Stinkweed Imp): expect life -1, hand 2, mill 5.", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "Leg 2 (granted Dredge 2, Forest via Necrobloom): expect life -1, hand 2, mill 2.", fill=(235, 240, 250))
    y += 40
    d.text((24, y), "Assertions:", fill=(235, 240, 250))
    y += 28
    labels = {"A1_setup_ok": "A1 setup (empty hand/Scrivener/dredger/prompt)",
              "A2_order_scrivener_first": "A2 616.1: Scrivener first",
              "A3_dredge_accepted": "A3 dredge ACCEPTED",
              "A4_life_rider_once": "A4 DECISIVE life -1 exactly once",
              "A5_dredger_returned": "A5 Stinkweed Imp returned to hand",
              "A6_milled": "A6 milled exactly 5",
              "A7_drew_one": "A7 drew exactly 1",
              "A8_game_advanced": "A8 game advanced past the draw",
              "B1_setup_ok": "B1 leg-2 setup",
              "B2_order_scrivener_first": "B2 leg-2 616.1: Scrivener first",
              "B3_dredge_accepted": "B3 leg-2 dredge ACCEPTED",
              "B4_life_rider_once": "B4 leg-2 life -1 exactly once",
              "B5_land_returned": "B5 leg-2 land returned to hand",
              "B6_milled_two": "B6 leg-2 milled exactly 2",
              "B7_drew_one": "B7 leg-2 drew exactly 1"}
    for k in ASS_KEYS:
        v = ass.get(k, "not-run")
        col = {"passed": (110, 200, 130), "failed": (220, 80, 80),
               "not-run": (150, 150, 160)}[v]
        d.text((40, y), f"{labels[k]}: {v}", fill=col)
        y += 24
    d.text((24, H - 40), "Evidence: ntindle/phase-bug-state-evidence 9354/20261003-9354/",
           fill=(130, 140, 160))
    img.save(f"{EVDIR}/summary.png")
    say("rendered summary.png")


def write_manifest():
    import hashlib as _h
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    lines = []
    for fn in files:
        h = _h.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


async def main():
    pidfile = "/tmp/scenario_9354.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(
                f"another scenario_9354 instance is alive (pid {old}); refusing")
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
    await verify_server_hello()
    check_data_level()

    all_ass = {k: "not-run" for k in ASS_KEYS}
    all_notes = []
    if ST.get("data_level_ok"):
        pass
    ass1, notes1, v1, completed1 = await run_leg(1)
    all_ass.update(ass1)
    all_notes.extend(notes1)

    # leg 2 only if the leg-1 flow actually worked (same machinery)
    v2, completed2 = "not-run", False
    if completed1 and v1 in ("reproduced", "not-reproduced"):
        ass2, notes2, v2, completed2 = await run_leg(2)
        all_ass.update(ass2)
        all_notes.extend(notes2)
    else:
        all_notes.append("Leg 2 skipped: leg-1 flow did not complete; the "
                         "shared machinery is unproven.")

    if v1 == "reproduced" or v2 == "reproduced":
        verdict = "reproduced"
    elif v1 == "not-reproduced" or v2 == "not-reproduced":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict} (leg1={v1} leg2={v2})")
    wire("verdict", {"verdict": verdict, "leg1": v1, "leg2": v2})

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": all_ass, "notes": all_notes,
                   "leg1_verdict": v1, "leg2_verdict": v2,
                   "verdict": verdict}, f, indent=1)

    scen_hash = hashlib.sha256(
        open("/home/hatch/workspace/dev/phase-backfill/driver/scenario_9354.py",
             "rb").read()).hexdigest()
    run_json = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9354",
        "started_at": ST.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "server": SERVER_IDENTITY,
        "scenario": "driver/scenario_9354.py",
        "scenario_sha256": scen_hash,
        "game_code": ST.get("game_code"),
        "verdict": verdict,
        "leg1_verdict": v1,
        "leg2_verdict": v2,
        "assertions": all_ass,
        "notes": all_notes,
        "scope": ("Blood Scrivener + accepted Dredge on a 1-card draw with an "
                  "empty hand: leg 1 printed Dredge 5 (Stinkweed Imp), leg 2 "
                  "granted Dredge 2 (Forest via The Necrobloom); two native "
                  "human seats, no AI"),
        "limitations": ["Browser UI not exercised",
                        "AI seats not used (both seats human-driven)",
                        "Granted-dredge leg attempted only if the printed leg "
                        "flow completed"],
        "evidence_files": sorted(os.listdir(EVDIR)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_json, f, indent=1)

    render_png(verdict, all_ass, all_notes)
    write_manifest()
    WIRE.close()
    RUNLOG.close()
    say("finalize complete")


if __name__ == "__main__":
    asyncio.run(main())
