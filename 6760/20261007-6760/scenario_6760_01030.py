#!/usr/bin/env python3
"""Issue #6760 revalidation on v0.103.0 (protocol 106): Ojer Kaslem, Deepest Growth.

Oracle: "Trample. Whenever Ojer Kaslem deals combat damage to a player, reveal
that many cards from the top of your library. You may put a creature card
and/or a land card from among them onto the battlefield. Put the rest on the
bottom in a random order."

Reported: the engine only permits choosing ONE card total (creature OR land)
instead of independently one creature AND one land.

Driver plan (protocol 106, v0.103.0, native engine, two human-client seats):
  P0: 4x Ojer Kaslem, Deepest Growth + 24x Forest + 32x Llanowar Elves (60)
  P1: 60x Island (draw-go, never blocks)
  P0 ramps, casts Ojer Kaslem (3GG), attacks unblocked the following turn
  (6 damage -> reveal 6), answers the may-choice, then at the card-selection
  prompt attempts to select BOTH one creature and one land.
  If the 2-card submission is rejected (or only one card enters), the bug is
  reproduced: the driver then selects a single card to confirm the one-card
  behavior and checks the rest go to the bottom of the library.

Assertions:
  A1 setup_ok             Ojer Kaslem on P0 battlefield, attack declared
  A2 damage_and_reveal    P1 20->14 from Ojer combat damage; 6 cards revealed
  A3 may_accepted         may-choice answered (decideOptionalEffect) or select
                          submitted directly (may folded into select)
  A4 both_categories      >=1 creature and >=1 land among revealed cards
  A5 two_cards_enter      2-card (creature+land) selection accepted AND both
                          enter P0's battlefield
  A6 rest_to_bottom       unchosen revealed cards end in P0's library
  A7 cleanup              stack empty, trigger fully resolved, game proceeds

Verdict = reproduced iff A4 passed and A5 failed;
          not-reproduced iff A4 passed and A5 passed;
          blocked otherwise.

Protocol-106 driver notes (v0.103.0): ported from the verified protocol-72
scenario_6760_086.py using the conventions from the verified protocol-106
scenario_301_01020.py:
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 106),
    gated on the MulliganDecision legal action.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi, gated on hand > 7 + schema/select opportunity
    offering hand cards (106 uses generic 'choose' kind code).
  - Ojer cast via CastSpell legacy action; {3}{G}{G} driven through
    PayManaAbilityMana/PayMana legacy actions + vi tapLandForMana menus.
  - The 106 engine also advertises land plays as a vi exactChoices
    opportunity carrying a playLand action code: answered before the
    decision gate (otherwise the driver stalls holding priority).
  - A single `await asyncio.sleep(0)` yield after the priority gate, before
    reading fresh state for leg evaluation (leg-engagement race fix).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority.
  - deck schema {"main_deck": [...]} via client.py deck() helper; HELLO
    advertises 106 (exact match enforced).
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
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6760_106")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261007-6760"
ISSUE = 6760
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.103.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-07: latest stable release v0.103.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.103.0/ec27a8d/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "reusing the pinned v0.103.0 server on 127.0.0.1:9374 started "
               "by run 20261005-6758 with isolated run dir "
               "runs/20261005-6758"),
}

for _f, _k in (("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.103.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

OJER = "Ojer Kaslem, Deepest Growth"
FOREST = "Forest"
ELVES = "Llanowar Elves"
ISLAND = "Island"


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


# ---------------------------------------------------------------- state helpers

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
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def lib_size(state, pid):
    return len(player_of(state, pid).get("library") or [])


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def is_creature(o):
    ct = (o.get("card_types") or {}).get("core_types") or []
    if "Creature" in ct:
        return True
    tl = str(o.get("type_line") or "")
    return "Creature" in tl


def classify(state, oid):
    o = get_obj(state, oid)
    if is_creature(o):
        return "Creature"
    if is_land(o):
        return "Land"
    return "?"


def untapped_mana_sources(state, pid):
    n = 0
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if ob.get("tapped"):
            continue
        if is_land(ob):
            n += 1
        elif obj_lname(state, o) == ELVES.lower():
            n += 1
    return n


def untapped_green(state, pid):
    n = 0
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if ob.get("tapped"):
            continue
        nm = obj_lname(state, o)
        if nm == FOREST.lower() or nm == ELVES.lower():
            n += 1
    return n


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability/land-play menu) in its viewer_interaction."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


# ---------------------------------------------------------------- interaction primitives

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
        rdata = {"choiceIds": [cid]}
        if stype == "manaGroups":
            rdata["count"] = 1
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": rdata}}
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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
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


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    if target is None:
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; not answering")
        wire(f"{tag}_bottom_no_vi", {})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm == OJER.lower():
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        found = False
        handset = set(hand)
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            rdata = resp.get("data", {}) or {}
            spec = rdata.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            cands = rdata.get("candidates") or []
            if any(str(_cand_reference(ch)) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        if nm == OJER.lower():
            return (3, nm)
        if nm == FOREST.lower():
            return (1, nm)
        return (0, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
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
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
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


async def answer_vi_play_land(c, state, pid, acts, tag):
    """Protocol 106 advertises land plays as a vi exactChoices opportunity
    carrying a playLand action code. Answer it before the decision gate so
    the driver never stalls holding priority with only a land-play menu."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for opp in vi_ops(state and c.latest):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            if ref is None or obj_lname(state, ref) != FOREST.lower():
                continue
            if str(ref) not in hand_ids(state, pid):
                continue
            LAND_PLAYED_TURN[(tag,)] = turn
            say(f"[{tag}] playing land via vi playLand: {FOREST}")
            wire("play_land_vi", {"who": tag, "ref": ref})
            await answer_vi(c, opp, ch, tag)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)} (legacy)")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


async def my_land_or_vi_land(c, state, pid, acts, tag):
    if await play_a_land(c, state, pid, acts, tag):
        return True
    return await answer_vi_play_land(c, state, pid, acts, tag)


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


# ---------------------------------------------------------------- game ticks

async def p1_tick(c, g, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            say("[P1] declares no blockers")
            await submit_as_is(c, d)
        return
    if await pay_tick(c, acts):
        return
    if my_main(state, 1):
        if await my_land_or_vi_land(c, state, 1, acts, tag):
            return
    if await answer_vi_play_land(c, state, 1, acts, tag):
        return
    if decision_pending(st):
        return
    if my_priority(acts):
        await pass_priority(c, st, acts)


async def p0_tick_ramp(c, g, tag, ctx):
    """One ramp tick for P0. Drives land drops, the Ojer cast, and (once
    summoning sickness clears) the attack via the DeclareAttackers branch.
    Returns True when it acted; the ramp loop ends when ctx["attacked"]."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return True
    if "DeclareAttackers" in atypes:
        ojer_oid = next(iter(bf_by_name(state, 0, OJER.lower())), None)
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        cast_turn = ctx.get("ojer_cast_turn")
        cur_turn = state.get("turn_number", 0)
        if (ojer_oid is not None and cast_turn is not None
                and cur_turn > cast_turn and not ctx.get("attacked")):
            d = copy.deepcopy(da)
            # NB: the engine expects the attacker object id as u64, not the
            # JSON string key used in the state objects map.
            d.setdefault("data", {}).update(
                {"attacks": [[int(ojer_oid), {"type": "Player", "data": 1}]],
                 "bands": []})
            wire("declare_attackers", d["data"])
            await submit_as_is(c, d)
            ctx["attacked"] = True
            ctx["attack_turn"] = cur_turn
            ctx["assert"]["A1_setup_ok"] = "passed"
            say(f"[{tag}] P0 attacks P1 with Ojer Kaslem (turn {cur_turn})")
            return True
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if ctx.get("ojer_cast_turn") is None and bf_by_name(state, 0, OJER.lower()):
        ctx["ojer_cast_turn"] = state.get("turn_number")
        # The cast resolved: any leftover mana need is stale (payment went
        # through legacy PayMana actions or an earlier vi menu). Clear it so
        # the noisy per-priority tapLandForMana menus are not tapped.
        ST["mana_needs"][tag] = {}
        say(f"[{tag}] Ojer Kaslem on battlefield at turn {ctx['ojer_cast_turn']}")

    if my_main(state, 0):
        if await my_land_or_vi_land(c, state, 0, acts, tag):
            return True
        if (not bf_by_name(state, 0, OJER.lower())
                and next((o for o in hand_ids(state, 0)
                          if obj_lname(state, o) == OJER.lower()), None) is not None
                and state.get("phase") == "PreCombatMain"
                and untapped_mana_sources(state, 0) >= 5
                and untapped_green(state, 0) >= 2):
            a, oid = cast_action_for(acts, state, OJER.lower())
            if a is not None:
                ST["mana_needs"][tag] = {"G": 2, "generic": 3}
                say(f"[{tag}] casting Ojer Kaslem, Deepest Growth (oid {oid})")
                wire("cast_ojer", {"action": {k: v for k, v in a.items()
                                             if not k.startswith("_")}})
                await submit_as_is(c, a)
                return True
        # NOTE: no early ATTACK_WINDOW return here. The attack is declared in
        # the DeclareAttackers branch above once summoning sickness clears;
        # returning early would end the ramp loop before any attack.
    if await answer_vi_play_land(c, state, 0, acts, tag):
        return True
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def generic_tick(c, g, tag, pid):
    """Post-attack driver tick: keep the game moving without answering real
    decisions (those are handled explicitly)."""
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return
    if my_main(state, pid):
        if await my_land_or_vi_land(c, state, pid, acts, tag):
            return
    if await answer_vi_play_land(c, state, pid, acts, tag):
        return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def tick_all_generic(p0, p1, g, ramp_tag):
    await generic_tick(p1, g, f"P1{ramp_tag}", 1)
    await generic_tick(p0, g, ramp_tag, 0)


# ---------------------------------------------------------------- may-choice + select

def find_optional_choice_opp(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


async def answer_may_accept(c, tag, ctx):
    st = c.latest
    opp = find_optional_choice_opp(st)
    if opp is None:
        return False
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "accept" in txt or "yes" in txt or "put" in txt:
                is_accept = True
            elif "decline" in txt or "no " in txt:
                is_accept = False
        if is_accept:
            wire("may_choice_opportunity", {"iid": opp.get("interactionId")})
            say(f"[{tag}] P0 ACCEPTS Ojer may-choice")
            await answer_vi(c, opp, ch, tag)
            ctx["may_answered"] = True
            return True
    say(f"[{tag}] may-choice opportunity found but no accept choice identified")
    return False


def select_opportunity(st):
    """Schema opportunity with revealed-card candidates. Excludes the
    DeclareAttackers/DeclareBlockers 'relations' prompts (attacker->defender
    edges), which are not the Ojer reveal selection."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or []
        if not cands:
            continue
        spec = data.get("spec", {}) or {}
        if (spec.get("type") or "") == "relations":
            continue
        return opp, spec, cands
    return None, None, None


async def run_select_test(p0, ctx, tag):
    """Core test: at the revealed-card selection prompt, attempt to select
    one creature AND one land. Mirrors the verified protocol-72 flow."""
    opp, spec, cands = select_opportunity(p0.latest)
    if opp is None:
        ctx["notes"].append("no schema select opportunity surfaced")
        return False
    st = p0.latest
    s = st["state"]
    classified = []
    for ch in cands:
        ref = _cand_reference(ch)
        name = obj_lname(s, ref) if ref is not None else None
        if not name or name == "?":
            name = choice_text(ch)
        ctype = classify(s, ref) if ref is not None else "?"
        zone = get_obj(s, ref).get("zone") if ref is not None else "?"
        classified.append({"choice_id": ch.get("id"), "ref": ref,
                           "name": name, "type": ctype, "zone": zone})
    wire("select_opportunity_full",
         {"iid": opp.get("interactionId"), "spec": spec,
          "n_candidates": len(classified),
          "constraint": ((spec.get("data") or {}).get("constraint") if isinstance(spec, dict) else None)})
    say(f"[{tag}] select prompt: spec.type={(spec or {}).get('type')} "
        f"constraint={json.dumps(((spec or {}).get('data') or {}).get('constraint'))[:200]} "
        f"n_candidates={len(classified)}")
    for cc in classified:
        say(f"  candidate {cc['choice_id']}: {cc['name']} ({cc['type']}) "
            f"zone={cc['zone']} ref={cc['ref']}")

    ctx["assert"]["A2_damage_and_reveal"] = (
        "passed" if (life(s, 1) == 14 and len(classified) == 6) else "failed")
    if life(s, 1) != 14 or len(classified) != 6:
        ctx["notes"].append(f"A2 detail: P1 life={life(s, 1)} (expect 14), "
                            f"candidates={len(classified)} (expect 6)")

    pre_s = await p0.export_state()
    with open(f"{EVDIR}/pre_select_{ctx['attempt']}.json", "w") as f:
        f.write(pre_s)
    ctx["pre_select"] = json.loads(pre_s)["state"]
    ctx["select_iid"] = opp.get("interactionId")
    ctx["select_spec"] = spec
    ctx["candidates"] = classified

    has_c = any(c["type"] == "Creature" for c in classified)
    has_l = any(c["type"] == "Land" for c in classified)
    ctx["assert"]["A4_both_categories"] = "passed" if (has_c and has_l) else "failed"
    if not (has_c and has_l):
        ctx["notes"].append(
            f"revealed set lacks a category (creature={has_c} land={has_l}); "
            "cannot test the reported outcome on this attempt")
        return False

    c1 = next(c["choice_id"] for c in classified if c["type"] == "Creature")
    c2 = next(c["choice_id"] for c in classified if c["type"] == "Land")
    ctx["two_ids"] = [c1, c2]
    stype = (spec or {}).get("type") or "sequence"
    rdata = {"choiceIds": [c1, c2]}
    sub = {"interactionId": ctx["select_iid"],
           "response": {"type": stype, "data": rdata}}
    say(f"[{tag}] submitting 2-card selection (creature {c1} + land {c2}) via {stype}")
    wire("two_card_submission", sub)
    await p0.send_interaction(sub)
    ctx["two_sent_at"] = time.time()
    return True


def prompt_still_pending(p0, iid):
    st = p0.latest
    if not st:
        return True
    for op in vi_ops(st):
        if op.get("interactionId") == iid:
            return True
    return False


async def finish_select(p0, p1, ctx, tag, path):
    await asyncio.sleep(2)
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post_select_{ctx['attempt']}.json", "w") as f:
        f.write(post_s)
    post = json.loads(post_s)["state"]
    classified = ctx["candidates"]
    entered = []
    for c in classified:
        ref = c["ref"]
        if ref is None:
            continue
        o = post["objects"].get(str(ref))
        if o and o.get("zone") == "Battlefield" and str(o.get("controller")) == "0":
            entered.append(c)
    p0lib = set(str(x) for x in (player_of(post, 0).get("library") or []))
    bottomed = [c for c in classified
                if c["ref"] is not None and str(c["ref"]) in p0lib]
    say(f"[{tag}] select outcome path={path}: entered={[c['name'] for c in entered]} "
        f"bottomed={len(bottomed)}/{len(classified)}")
    wire("select_outcome", {"path": path,
                            "entered": [c["name"] for c in entered],
                            "bottomed": [c["name"] for c in bottomed],
                            "waiting_for": post.get("waiting_for")})
    if path == "two_accepted":
        both = (len(entered) == 2
                and {c["type"] for c in entered} == {"Creature", "Land"})
        ctx["assert"]["A5_two_cards_enter"] = "passed" if both else "failed"
        if not both:
            ctx["notes"].append(
                f"2-card submission accepted but battlefield shows "
                f"{[c['name'] for c in entered]} (expected 1 creature + 1 land)")
    else:  # one_accepted after the 2-card submission was rejected
        one_in = (len(entered) == 1 and entered[0]["choice_id"] == ctx["two_ids"][0])
        other = next(c for c in classified if c["choice_id"] == ctx["two_ids"][1])
        other_obj = post["objects"].get(str(other["ref"])) if other["ref"] else None
        other_zone = other_obj.get("zone") if other_obj else "missing"
        ctx["notes"].append(
            f"2-card submission rejected ({(ctx.get('two_rejection') or '')[:160]}); "
            f"single-card fallback entered={one_in}; the land '{other['name']}' "
            f"ended in zone {other_zone} (never put onto the battlefield)")
        ctx["assert"]["A5_two_cards_enter"] = "failed"
    n_rest = len(classified) - len(entered)
    ctx["assert"]["A6_rest_to_bottom"] = (
        "passed" if (n_rest == len(bottomed)) else "failed")
    if n_rest != len(bottomed):
        ctx["notes"].append(
            f"expected {n_rest} non-entered revealed cards in library, found {len(bottomed)}")
    stack = post.get("stack", []) or []
    ctx["assert"]["A7_cleanup"] = (
        "passed" if (not stack and not real_decision_pending(p0.latest)) else "failed")
    ctx["assert"]["A3_may_accepted"] = (
        "passed" if (ctx["may_answered"] or path in ("two_accepted", "one_accepted"))
        else "failed")
    return post


# ---------------------------------------------------------------- attempt runner

def new_ctx(attempt):
    return {
        "attempt": attempt,
        "assert": {"A1_setup_ok": "failed"},
        "notes": [],
        "rejections": [],
        "may_answered": False,
        "attacked": False,
        "ojer_cast_turn": None,
        "two_ids": [],
        "two_sent_at": None,
        "two_rejected": False,
        "two_rejection": "",
        "select_iid": None,
        "select_spec": None,
        "candidates": [],
        "pre_select": None,
        "finished": False,
    }


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    ST["mana_needs"] = {}
    ctx = new_ctx(attempt)
    tag = f"6760-a{attempt}"

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck((OJER, 4), (FOREST, 24), (ELVES, 32)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck((ISLAND, 60)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code})

    try:
        # ---- phase 1: ramp to Ojer + attack
        t0 = time.time()
        last_rev = {}
        last_change = {}
        last_tick_at = {}
        last_diag = time.time()
        while time.time() - t0 < 1500:
            await asyncio.sleep(0.15)
            progressed = False
            for c, is_p0 in ((p0, True), (p1, False)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                    last_change[c.name] = time.time()
                    progressed = True
                else:
                    holds_prio = my_priority(top_acts(st))
                    if not (holds_prio and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    if is_p0:
                        await p0_tick_ramp(c, {"mode": "ramp"}, tag, ctx)
                    else:
                        await p1_tick(c, {"mode": "ramp"}, f"P1{tag}")
                except Exception as e:
                    say(f"[{tag}] tick error {c.name}: {type(e).__name__}: {e}")
                    wire(f"{tag}_tick_error",
                         {"who": c.name, "err": f"{type(e).__name__}: {e}"})
            if ctx["attacked"]:
                break
            if time.time() - last_diag > 30:
                last_diag = time.time()
                for c in (p0, p1):
                    st = c.latest
                    if not st:
                        say(f"[{tag}] DIAG {c.name}: no state yet")
                        continue
                    s = st["state"]
                    say(f"[{tag}] DIAG {c.name}: rev={c.revision} "
                        f"turn={s.get('turn_number')} phase={s.get('phase')} "
                        f"prio={[a.get('type') for a in top_acts(st)][:8]} "
                        f"vikind={vi_kind_code(st)!r} "
                        f"real_decision={real_decision_pending(st)} "
                        f"my_prio={my_priority(top_acts(st))} "
                        f"ojer_cast_turn={ctx.get('ojer_cast_turn')} "
                        f"attacked={ctx.get('attacked')}")
        if not ctx["attacked"]:
            ctx["notes"].append("never attacked with Ojer (ramp phase incomplete)")
            return ctx

        # ---- phase 2: drive to the may-choice / select prompts.
        # First wait for combat damage to actually land (P1 life < 20);
        # the Ojer trigger only fires after damage, and the DeclareAttackers
        # 'relations' prompt must not be mistaken for the reveal selection.
        t1 = time.time()
        damage_done = False
        while time.time() - t1 < 240:
            await asyncio.sleep(0.5)
            while True:
                try:
                    t, data = p0.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t in ("ActionRejected", "Error"):
                    rec = json.dumps(data, default=str)[:400]
                    ctx["rejections"].append({"who": "P0", "type": t, "at": time.time(), "data": rec})
                    wire("rejection", {"who": "P0", "type": t, "data": rec})
            await tick_all_generic(p0, p1, {"mode": "ramp"}, tag)
            await asyncio.sleep(0)
            if p0.latest and life(p0.latest["state"], 1) < 20:
                damage_done = True
                say(f"[{tag}] combat damage landed: P1 life={life(p0.latest['state'], 1)}")
                break
        if not damage_done:
            ctx["notes"].append("combat damage never landed within 240s of the attack")
            return ctx
        t1 = time.time()
        may_done = False
        select_ready = False
        while time.time() - t1 < 240:
            await asyncio.sleep(0.5)
            # drain P0 rejections
            while True:
                try:
                    t, data = p0.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t in ("ActionRejected", "Error"):
                    rec = json.dumps(data, default=str)[:400]
                    ctx["rejections"].append({"who": "P0", "type": t, "at": time.time(), "data": rec})
                    wire("rejection", {"who": "P0", "type": t, "data": rec})
            if not may_done and p0.latest and find_optional_choice_opp(p0.latest):
                if await answer_may_accept(p0, tag, ctx):
                    may_done = True
                    continue
            if p0.latest:
                opp, _spec, _cands = select_opportunity(p0.latest)
                if opp is not None and _cands:
                    select_ready = True
                    break
            await tick_all_generic(p0, p1, {"mode": "ramp"}, tag)
            await asyncio.sleep(0)
        if not select_ready:
            ctx["notes"].append("no revealed-card select opportunity surfaced within 240s")
            wire(f"{tag}_select_missing_vi",
                 p0.latest.get("viewer_interaction") if p0.latest else None)
            return ctx

        # ---- phase 3: the select test
        if not await run_select_test(p0, ctx, tag):
            if ctx["assert"].get("A4_both_categories") != "failed":
                ctx["notes"].append("select test could not start")
            return ctx

        # ---- phase 4: watch the 2-card submission
        t2 = time.time()
        resolved = False
        while time.time() - t2 < 90:
            await asyncio.sleep(0.5)
            while True:
                try:
                    t, data = p0.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t in ("ActionRejected", "Error"):
                    rec = json.dumps(data, default=str)[:400]
                    ctx["rejections"].append({"who": "P0", "type": t, "at": time.time(), "data": rec})
                    wire("rejection", {"who": "P0", "type": t, "data": rec})
                    if ctx["two_sent_at"] and t == "ActionRejected" \
                            and time.time() >= ctx["two_sent_at"]:
                        ctx["two_rejected"] = True
                        ctx["two_rejection"] = rec
                        say(f"[{tag}] 2-card submission REJECTED: {rec[:220]}")
            rej = [r for r in ctx["rejections"]
                   if r["type"] == "ActionRejected" and r["at"] >= ctx["two_sent_at"]]
            if rej:
                ctx["two_rejected"] = True
                ctx["two_rejection"] = rej[0]["data"]
                say(f"[{tag}] 2-card submission REJECTED: {rej[0]['data'][:220]}")
                # fall back: submit a single card (the creature) to confirm the
                # one-card behavior and let the game proceed
                opp, spec, _ = select_opportunity(p0.latest) if p0.latest else (None, None, None)
                iid = ctx["select_iid"]
                stype = ((spec or ctx["select_spec"] or {}).get("type")) or "sequence"
                if opp is not None:
                    iid = opp.get("interactionId")
                sub = {"interactionId": iid,
                       "response": {"type": stype,
                                    "data": {"choiceIds": [ctx["two_ids"][0]]}}}
                wire("one_card_submission", sub)
                say(f"[{tag}] submitting 1-card fallback (creature {ctx['two_ids'][0]})")
                await p0.send_interaction(sub)
                ctx["one_sent_at"] = time.time()
                # wait for the fallback to resolve
                t3 = time.time()
                while time.time() - t3 < 60:
                    await asyncio.sleep(0.5)
                    await tick_all_generic(p0, p1, {"mode": "ramp"}, tag)
                    if not prompt_still_pending(p0, iid):
                        break
                await finish_select(p0, p1, ctx, tag, path="one_accepted")
                resolved = True
                break
            if not prompt_still_pending(p0, ctx["select_iid"]):
                await finish_select(p0, p1, ctx, tag, path="two_accepted")
                resolved = True
                break
            await tick_all_generic(p0, p1, {"mode": "ramp"}, tag)
            await asyncio.sleep(0)
        if not resolved:
            ctx["notes"].append("2-card submission: no rejection, prompt still pending after 90s")
        ctx["finished"] = True
    finally:
        await p0.close()
        await p1.close()
    for k in ("A1_setup_ok", "A2_damage_and_reveal", "A3_may_accepted",
              "A4_both_categories", "A5_two_cards_enter", "A6_rest_to_bottom",
              "A7_cleanup"):
        ctx["assert"].setdefault(k, "not-run")
    return ctx


async def main():
    await verify_server_hello()
    t0 = time.time()
    final = None
    for attempt in (1, 2, 3):
        say(f"===== attempt {attempt} =====")
        ctx = await run_attempt(attempt)
        say(f"attempt {attempt} assertions: {json.dumps(ctx['assert'])}")
        if ctx["assert"].get("A4_both_categories") == "passed":
            final = ctx
            break
        final = ctx
        say("attempt lacked both categories in the revealed set; retrying")
    dur = time.time() - t0
    ass = final["assert"]
    notes = final["notes"] + [
        "protocol-106 driver (v0.103.0): Ojer cast via CastSpell legacy action "
        "with {G:2, generic:3} driven through PayManaAbilityMana/PayMana legacy "
        "actions + vi tapLandForMana menus; attack via DeclareAttackers "
        "attacks=[[oid,{type:Player,data:1}]]; P1 declares no blockers via "
        "DeclareBlockers assignments=[]; mulligan answered as-is with "
        "decision='keep'; discard via DiscardToHandSize viewer_interaction "
        "select submission; land plays via legacy PlayLand + vi playLand "
        "opportunity (answered before the decision gate); may-choice via "
        "exactChoices decideOptionalEffect accept choice; revealed-card "
        "selection via schema prompt submitted as {type:<spec.type>, "
        "data:{choiceIds:[...]}}.",
        "P0 deck 4x Ojer Kaslem, Deepest Growth + 24x Forest + 32x Llanowar "
        "Elves; P1 60x Island draw-go, never blocks (engine accepts >4-of "
        "for custom games).",
    ]
    a4 = ass.get("A4_both_categories")
    a5 = ass.get("A5_two_cards_enter")
    if a4 == "passed" and a5 == "failed":
        verdict = "reproduced"
    elif a4 == "passed" and a5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.103.0",
            "build_commit": "ec27a8d",
            "protocol_version": 106,
            "mode": "Full",
            **{k: v for k, v in SERVER_IDENTITY.items()
                if k in ("server_binary_sha256", "card_data_sha256",
                         "draft_pools_sha256", "signature_verified")},
            "observed_at": "2026-10-07",
            "source": SERVER_IDENTITY["source"],
        },
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6760_01030.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [[OJER, 4], [FOREST, 24], [ELVES, 32]],
            "P1": [[ISLAND, 60]],
        },
        "assertions": ass,
        "notes": notes,
        "rejections": final["rejections"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Not tested on the original 2026-07-29 build; verdict is scoped to "
            "v0.103.0, not a fix claim.",
            "4x/24x/32x deck density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
        ],
        "setup_line": "P0: 4x Ojer Kaslem, Deepest Growth + 24x Forest + 32x Llanowar Elves; P1: 60x Island (never blocks)",
        "contract_line": "Ojer deals 6 combat damage -> reveal 6 -> may put a creature AND a land onto the battlefield",
        "stats": {"states_seen": "n/a", "trigger_observations": "n/a"},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6760_01030.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6760_01030.py").read())
    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    WIRE.close()
    RUNLOG.close()


asyncio.run(main())
