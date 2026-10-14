#!/usr/bin/env python3
"""Issue #9225: Locke, Treasure Hunter -- Mug creates one Treasure per
opponent in multiplayer instead of one total.

Oracle (FIC):
  "Locke can't be blocked by creatures with greater power.
  Mug -- Whenever Locke attacks, each player mills a card. If a land card
  was milled this way, create a Treasure token. Until end of turn, you may
  cast a spell from among those cards."

WotC ruling (2025-06-06): "As long as one or more lands were milled this
way, you'll create a Treasure token. Additional land cards milled beyond
the first won't cause you to create additional Treasures."

Reported (v0.91.0, 4-player): each player mills a land -> 3 Treasures
appear (one per opponent); exactly 1 is correct. 2-player reads correct,
which is why it went unnoticed.

Behavioral contract -- single leg, 4-player free-for-all, native engine:
  Decks: P0 = 4x Locke + 28x Swamp + 28x Mountain (60).
         P1/P2/P3 = 60x Forest each (draw-go; their library top is always
         a land, so every mill hits a land deterministically).
  Mulligans: P0 keeps iff Locke is in hand (up to 2 mulligans);
             P1-P3 always keep. Any BottomCards bottoms arbitrary cards.
  P0 ramps: plays one land per turn (Swamp first, then Mountain, then
            anything), casts Locke when {B:1,R:1,generic:1} is affordable
            (normally turn 3), attacks with Locke at the next combat
            (normally turn 4) targeting P1; all seats declare no blockers.
  The Mug trigger resolves: each player mills 1 (a land, by construction).
  PRE is exported at P0's pre-combat main on the attack turn (Locke on BF).
  POST is exported after the trigger observably resolved (Treasure count
  on P0's BF goes 0 -> N) plus stack-empty and 3 quiet ticks.

Assertions:
  A1_setup_ok      PRE: Locke on P0 BF, active player is P0, pre-combat.
  A2_all_milled_land  POST-PRE graveyard delta is exactly 1 card per
                      player, and every milled card is a land.
  A3_treasure_count   POST: exactly 1 Treasure token on the battlefield
                      (expected under the oracle + ruling). The reported
                      bug gives 3 (one per opponent).
  A4_cleanup          POST stack empty, game not over.

Verdict rule (pre-registered):
  - reproduced: A1 passed + A2 passed + A3 failed (Treasure count = 3,
    the issue's measured signature; any count != 1 with A1/A2 passing
    is the same defect).
  - not-reproduced: all assertions passed (exactly 1 Treasure).
  - blocked: setup never completed (Locke never cast/attacked), a mill
    missed a land (non-discriminating fixture -> retry the leg), or the
    trigger never observably resolved.

The third clause ("you may cast a spell from among those cards") is a
separate defect (fixed upstream by #9230, not part of this report). The
milled cards are all lands (not castable as spells); if the engine offers
an OptionalEffectChoice for it, the driver declines and logs the shape.

Conventions (from scenario_9865.py / scenario_7239.py, protocol 142):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - Export response data.state is a JSON string, parsed once.
  - finish() ordering: run.json, scenario copy, every logging hook,
    THEN close WIRE/RUNLOG, THEN manifest LAST.
  - DeclareAttackers via the relations schema; empty blockers the same way.
"""
import asyncio
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
log = logging.getLogger("scenario9225")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9225-v01100-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9225
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "0.110.0",
    "build_commit": "9bfd12e",
    "protocol_version": 142,
    "mode": "Full",
    "server_binary_sha256": "346ad6a1a8cd887bb49108fa132476bea36e42e7cfc5116fe2cbdf8ad369e3f2",
    "card_data_sha256": "c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74",
    "draft_pools_sha256": "7e45abbf0a964959192d2528c067a2a9caab04438640ab63b9ad79d2ce0e4676",
    "signature_verified": True,
    "signature_note": ("v0.110.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-14: latest stable release v0.110.0 (published "
               "2026-10-14) == pinned release dir; ServerHello "
               "0.110.0/9bfd12e/protocol 142/Full read live this run"),
}

CARD_DATA = {}
with open(f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json") as f:
    CARD_DATA = json.load(f)

LOCKE = "Locke, Treasure Hunter"
SWAMP = "Swamp"
MOUNTAIN = "Mountain"
FOREST = "Forest"
TREASURE = "Treasure"


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(kind, payload):
    WIRE.write(json.dumps({"t": time.time(), "kind": kind,
                           "data": payload}, default=str) + "\n")


for _f, _k in (("server/releases/v0.110.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.110.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.110.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")


# ------------------------------------------------------------------ helpers

def oname(o):
    return o.get("base_name") or o.get("card_name") or o.get("name") or ""


def obj_lname(state, oid):
    return oname(state["objects"].get(str(oid), {}))


def get_obj(state, oid):
    return state["objects"].get(str(oid), {})


def hand_ids(state, pid):
    return [oid for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and str(o.get("controller", -1)) == str(pid)]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_named(state, pid, name):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == name]


def is_land(o):
    ct = o.get("card_types") or o.get("card_type") or {}
    return "Land" in (ct.get("core_types") or [])


def gy_oids(state, pid):
    return [oid for oid, o in state["objects"].items()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)]


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_ops(st):
    if not st:
        return []
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def choice_text(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and (d.get("name") or d.get("value")):
            t = d.get("name") or d.get("value")
            break
    else:
        t = ch.get("id")
    return str(t)


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            spec = (data.get("spec", {}) or {}).get("type")
            if spec == "relations":
                continue
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes or "chooseBranch" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def _opp_summary(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        out.append({"iid": str(opp.get("interactionId"))[:12],
                    "rtype": resp.get("type"),
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c)})
    return out


MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
RECORDED_OPPS = set()


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] is not None:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


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
    assert str(ver) == "0.110.0", f"unexpected version {ver}"
    assert int(proto) == 142, f"unexpected protocol {proto}"
    assert str(build) == "9bfd12e", f"unexpected build {build}"


# ------------------------------------------------------------- action helpers

async def do_mulligan(c, acts, st, pid, tag, keep_fn):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    mull_count = sum(1 for k in MULLS if k[0] == tag and k[1] == "mull") - 1
    keep = keep_fn(state, pid)
    choice = "Keep" if (keep or mull_count >= 2) else "Mulligan"
    say(f"[{tag}] mulligan -> {choice} (hand: {hand_names(state, pid)})")
    wire("mulligan", {"who": tag, "decision": choice,
                      "hand": hand_names(state, pid)})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    state = st["state"]
    pend = None
    for p in ((wf_of(state).get("data") or {}).get("pending") or []):
        if str(p.get("player")) == str(pid):
            pend = p
            break
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    if (tag, "bottomed", f"rev{c.revision}") in MULLS:
        return False
    MULLS.add((tag, "bottomed", f"rev{c.revision}"))
    n = (pend.get("phase") or {}).get("count") or 1
    picks = [int(x) for x in hand_ids(state, pid)[:n]]
    say(f"[{tag}] bottoming {n}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "count": n})
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    wf = (state.get("waiting_for") or {}).get("type", "")
    if "Discard" not in str(wf):
        return False
    if len(hand_ids(state, pid)) <= 7:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if _cand_ref(ch) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "handsize", iid)
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        # Prefer a basic land; never discard the key spell (Locke).
        # Fall back to any non-Locke card, else the first candidate.
        picks = [ch for ch in cands
                 if is_land(get_obj(state, _cand_ref(ch)))] or \
                [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) != LOCKE] or \
                cands[:1]
        picks = picks[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:30] for x in picks]}")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
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
                syms = []
                for m in ch.get("surfaces", []) or []:
                    if m.get("type") == "mana":
                        syms = (m.get("data") or {}).get("symbols", []) or []
                        break
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
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "choose",
                                           "data": {"choiceId": pick.get("id")}}}, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action({"type": "PassPriority"})
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
                                                   "data": {"choiceId": ch.get("id")}}}, c.name)
                return True
    return False


async def play_ramp_land(c, state, pid, acts, tag):
    """P0 plays one land per turn: first a Swamp (B), then a Mountain (R),
    then anything -- so {B:1,R:1,generic:1} is reachable by turn 3."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    bf_names = [obj_lname(state, o) for o in bf_oids(state, pid)]
    order = []
    if SWAMP not in bf_names:
        order.append(SWAMP)
    if MOUNTAIN not in bf_names:
        order.append(MOUNTAIN)
    order += [SWAMP, MOUNTAIN]
    pref = []
    for name in order:
        pref += [o for o in hand_ids(state, pid)
                 if is_land(get_obj(state, o))
                 and obj_lname(state, o) == name and o not in pref]
    pref += [o for o in hand_ids(state, pid)
             if is_land(get_obj(state, o)) and o not in pref]
    for o in pref:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


def untapped_land_count(state, pid, name):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == name
               and not get_obj(state, o).get("tapped"))


def can_afford(state, pid, needs):
    color_land = {"W": "Plains", "B": SWAMP, "G": FOREST, "U": "Island",
                  "R": MOUNTAIN}
    for color, n in needs.items():
        if color == "generic":
            continue
        if untapped_land_count(state, pid, color_land.get(color, "")) < n:
            return False
    generic = needs.get("generic", 0)
    total_untapped = sum(1 for o in bf_oids(state, pid)
                         if is_land(get_obj(state, o))
                         and not get_obj(state, o).get("tapped"))
    return total_untapped >= generic + sum(v for k, v in needs.items()
                                           if k != "generic")


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


async def try_cast(c, state, acts, tag, name, needs, pid=0, ctx=None):
    if name not in hand_names(state, pid):
        return False
    a, oid = cast_action_for(acts, state, name)
    if a is None:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid, "needs": needs})
    if ctx is not None:
        ctx["cast_oid"] = oid
    await submit_as_is(c, a)
    return True


def find_relations_op(c):
    for op in vi_ops(c.latest):
        resp = op.get("response", {}) or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return op
    return None


async def answer_declare_attack(c, pid, tag, attacker_name, target_seat):
    state = c.latest["state"]
    wf = wf_of(state)
    if wf.get("type") != "DeclareAttackers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    data = (op.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", []) or []
    cands = {ch.get("id"): ch for ch in data.get("candidates", []) or []}

    def cand_obj_name(cid):
        ch = cands.get(cid, {})
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "object":
                ref = str((s.get("data") or {}).get("reference"))
                return oname(state["objects"].get(ref, {}))
        return None

    def cand_seat(cid):
        ch = cands.get(cid, {})
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if dd.get("seat") is not None:
                return str(dd.get("seat"))
        return None

    seen_src = set()
    rels = []
    for e in edges:
        src = e.get("sourceId")
        tids = e.get("targetIds") or []
        if not src or not tids or src in seen_src:
            continue
        if cand_obj_name(src) != attacker_name:
            continue
        pick = next((t for t in tids if cand_seat(t) == str(target_seat)), tids[0])
        rels.append({"sourceId": src, "targetId": pick, "group": None})
        seen_src.add(src)
    if not rels:
        return False
    iid = op.get("interactionId") or op.get("id")
    wire("declare_attack", {"who": tag, "n_edges": len(edges),
                            "n_rels": len(rels)})
    say(f"[{tag}] declares attack: {len(rels)} relations")
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "relations",
                                       "data": {"relations": rels}}}, tag)
    return True


async def answer_declare_empty(c, pid, tag):
    state = c.latest["state"]
    wf = wf_of(state)
    if wf.get("type") not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    wire("declare_empty", {"who": tag, "wf": wf.get("type")})
    say(f"[{tag}] declares empty ({wf.get('type')})")
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "relations",
                                       "data": {"relations": []}}}, tag)
    return True


async def decline_optional_effect(c, st, pid, tag, ctx):
    """Decline the 'you may cast a spell from among those cards' prompt
    (milled cards are all lands -- nothing castable)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        codes = set()
        for ch in items:
            codes.update(x for x in surf_codes(ch) if x)
        if "decideOptionalEffect" not in codes:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        key = (tag, "decline", iid)
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        # Dump the full opportunity for the evidence record.
        with open(f"{EVDIR}/optional_effect_opp.json", "w") as f:
            json.dump(opp, f, indent=1, default=str)
        texts = [choice_text(ch) for ch in items]
        say(f"[{tag}] OptionalEffectChoice offers: {texts}")
        wire("optional_effect", {"who": tag, "choices": texts})
        pick = None
        for ch in items:
            t = choice_text(ch).lower()
            if any(w in t for w in ("decline", "\"no\"", " no ", "don't",
                                    "do not", "skip", "none")):
                pick = ch
                break
        if pick is None:
            # Default to the last choice (decline is conventionally last);
            # record the ambiguity.
            pick = items[-1]
            ctx["decline_ambiguous"] = texts
            say(f"[{tag}] WARNING: no explicit decline text; "
                f"picking last choice '{choice_text(pick)}'")
        say(f"[{tag}] declining optional effect: '{choice_text(pick)}'")
        wire("optional_decline", {"who": tag,
                                 "picked": choice_text(pick)})
        if resp.get("type") == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "exactChoices",
                                "data": {"choiceId": pick.get("id")}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        await interact_as(c, sub, tag)
        return True
    return False


def treasure_count(state):
    n = 0
    oids = []
    for oid, o in state["objects"].items():
        if o.get("zone") == "Battlefield" and oname(o) == TREASURE:
            n += 1
            oids.append((oid, o.get("controller"), o.get("owner")))
    return n, oids


ST = {"mana_needs": {}}


# ------------------------------------------------------------- per-seat tick

async def generic_tick(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    # --- mulligans / bottoming / hand-size discards (all seats)
    if "MulliganDecision" in atypes:
        keep_fn = (lambda s, p: LOCKE in hand_names(s, p)) if pid == 0 \
            else (lambda s, p: True)
        if await do_mulligan(c, acts, st, pid, tag, keep_fn):
            return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return

    # --- mana payments always get priority
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # --- combat declarations
    if pid == 0 and ctx.get("phase") == "attack":
        if await answer_declare_attack(c, pid, tag, LOCKE, 1):
            ctx["attack_declared"] = True
            return
    if await answer_declare_empty(c, pid, tag):
        return

    # --- the cast-permission tail of the Mug trigger: decline it
    if await decline_optional_effect(c, st, pid, tag, ctx):
        return

    # --- P0's game plan
    if pid == 0:
        phase = ctx.get("phase")
        if phase == "ramp":
            if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                    and int(state.get("active_player", -1)) == 0:
                if await play_ramp_land(c, state, pid, acts, tag):
                    return
                if LOCKE in hand_names(state, 0) and can_afford(
                        state, 0, {"B": 1, "R": 1, "generic": 1}):
                    if await try_cast(c, state, acts, tag, LOCKE,
                                      {"B": 1, "R": 1, "generic": 1},
                                      pid=0, ctx=ctx):
                        ctx["locke_cast"] = True
                        ctx["cast_at"] = time.time()
                        return
        elif phase == "attack":
            # PRE export at the attack turn's pre-combat main, then the
            # DeclareAttackers handler above fires during combat.
            if not ctx.get("pre_exported") \
                    and state.get("phase") == "PreCombatMain" \
                    and int(state.get("active_player", -1)) == 0 \
                    and bf_named(state, 0, LOCKE):
                pre = json.loads(await c.export_state())
                with open(f"{EVDIR}/pre.json", "w") as f:
                    json.dump(pre, f, indent=1)
                ctx["pre_exported"] = True
                ctx["pre_gy"] = {p: len(gy_oids(pre_state_of(pre), p))
                                 for p in range(4)}
                wire("pre_export", {"gy": ctx["pre_gy"]})
                say(f"[{tag}] PRE exported (attack turn); gy={ctx['pre_gy']}")
                return
        elif phase == "observe":
            pass

    # --- unknown real decisions: record and hold
    if real_decision_pending(st):
        key = (tag, "prompt", c.revision)
        if key not in RECORDED_OPPS:
            RECORDED_OPPS.add(key)
            wire("prompt", {"who": tag, "rev": c.revision,
                            "opps": _opp_summary(st)})
            say(f"[{tag}] prompt (held): {_opp_summary(st)}")
            with open(f"{EVDIR}/held_prompt_{tag}_{c.revision}.json", "w") as f:
                json.dump(vi_ops(st), f, indent=1, default=str)
        return

    # --- otherwise pass priority
    if any(a.get("type") == "PassPriority" for a in acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


def pre_state_of(pre_envelope):
    """Parse the trusted envelope once: {'state': {...}, ...}."""
    s = pre_envelope.get("state")
    if isinstance(s, str):
        return json.loads(s)
    return s or {}


def envelope_state(envelope):
    return pre_state_of(envelope)


# ------------------------------------------------------------------- run leg

ASSERT_KEYS = ("A1_setup_ok", "A2_all_milled_land", "A3_treasure_count",
               "A4_cleanup")


def new_ctx(attempt):
    return {
        "attempt": attempt,
        "phase": "ramp",
        "locke_cast": False,
        "cast_oid": None,
        "cast_at": None,
        "pre_exported": False,
        "pre_gy": {},
        "attack_declared": False,
        "trigger_resolved": False,
        "treasure_seen": 0,
        "post_exported": False,
        "settle_ticks": 0,
        "decline_ambiguous": None,
    }


async def run_leg(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    RECORDED_OPPS.clear()
    ctx = new_ctx(attempt)
    p0_deck = deck((LOCKE, 4), (SWAMP, 28), (MOUNTAIN, 28))
    opp_deck = deck((FOREST, 60))
    tags = []
    clients = []
    for pid in range(4):
        tag = f"P{pid}K{attempt}"
        tags.append(tag)
        ST["mana_needs"][tag] = {}
    clients.append(PhaseClient(f"P0K{attempt}"))
    await clients[0].connect()
    await clients[0].create(p0_deck, player_count=4)
    for i in range(1, 4):
        c = PhaseClient(tags[i])
        await c.connect()
        await c.join(clients[0].game_code, opp_deck)
        clients.append(c)
    say(f"leg attempt={attempt} game {clients[0].game_code}; seats="
        f"{[c.player_id for c in clients]}")
    wire("game_start", {"attempt": attempt,
                        "game_code": clients[0].game_code,
                        "seats": [c.player_id for c in clients]})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": clients[0].game_code}
    for k in ASSERT_KEYS:
        obs["assert"][k] = "not-run"

    t0 = time.time()
    last_diag = 0
    last_rev = -1
    last_rev_change = time.time()
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT:
        await asyncio.sleep(0.25)
        for c, pid, tag in zip(clients, range(4), tags):
            try:
                await generic_tick(c, pid, tag, ctx)
            except Exception as e:
                say(f"tick error {tag}: {e}")
        await asyncio.sleep(0)
        cur_rev = clients[0].revision
        if cur_rev != last_rev:
            last_rev = cur_rev
            last_rev_change = time.time()
        dead = [c.name for c in clients
                if getattr(c, "ws", None) is not None
                and not getattr(c.ws, "open", True)]
        if dead:
            say(f"[leg] client websocket(s) closed: {dead}; abandoning leg")
            obs["notes"].append(f"abandoned: websockets closed {dead}")
            break
        if time.time() - last_rev_change > 150:
            say(f"[leg] no revision change in 150s (rev={cur_rev}); abandoning")
            obs["notes"].append("abandoned: no state-revision change in 150s")
            break
        st_now = clients[0].latest
        if st_now:
            s = st_now["state"]
            # phase transitions
            if ctx["phase"] == "ramp" and ctx.get("locke_cast") \
                    and bf_named(s, 0, LOCKE) and not (s.get("stack") or []):
                ctx["phase"] = "attack"
                say("[leg] phase ramp -> attack (Locke on BF, stack empty)")
                wire("phase_attack", {})
            if ctx["phase"] == "attack":
                n, _ = treasure_count(s)
                if n > 0 and not ctx["trigger_resolved"]:
                    ctx["trigger_resolved"] = True
                    ctx["treasure_seen"] = n
                    ctx["phase"] = "observe"
                    say(f"[leg] Mug trigger observably resolved: "
                        f"{n} Treasure(s); phase -> observe")
                    wire("trigger_resolved", {"treasures": n})
            if st_now["state"].get("turn_number", 0) > 12 \
                    and not ctx.get("locke_cast"):
                say("[leg] turn 12 exceeded without casting Locke; abandoning")
                obs["notes"].append("abandoned: Locke never cast by turn 12")
                break
            if ctx.get("locke_cast") \
                    and time.time() - ctx.get("cast_at", 0) > 420 \
                    and not ctx.get("post_exported"):
                say("[leg] Locke cast 420s ago, trigger never resolved; "
                    "abandoning")
                obs["notes"].append("abandoned: Locke cast but Mug trigger "
                                    "never observably resolved within 420s")
                break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pid in zip(clients, range(4)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                q = st["state"]
                n, _ = treasure_count(q)
                say(f"[diag] {c.name}: rev={c.revision} "
                    f"turn={q.get('turn_number')} phase={q.get('phase')} "
                    f"active={q.get('active_player')} "
                    f"stack={len(q.get('stack') or [])} "
                    f"phase_ctx={ctx.get('phase')} treasures={n}")
        # completion: trigger resolved, stack empty everywhere, no
        # pending prompts, 3 quiet ticks -> export POST.
        if ctx.get("trigger_resolved") and not ctx.get("post_exported"):
            latests = [c.latest for c in clients]
            if all(x is not None for x in latests) \
                    and all(not (x["state"].get("stack") or [])
                            for x in latests) \
                    and not any(real_decision_pending(x) for x in latests):
                ctx["settle_ticks"] += 1
                if ctx["settle_ticks"] >= 3:
                    say("[leg] settled; exporting POST")
                    wire("post_settled", {})
                    try:
                        post = json.loads(await clients[0].export_state())
                        with open(f"{EVDIR}/post.json", "w") as f:
                            json.dump(post, f, indent=1)
                        ctx["post_exported"] = True
                        say("[leg] POST exported")
                        wire("post_export", {})
                    except Exception as e:
                        say(f"POST export failed: {e}")
                        obs["notes"].append(f"POST export failed: {e}")
                    break
            else:
                ctx["settle_ticks"] = 0

    evaluate(obs, ctx)
    for c in clients:
        await c.close()
    return obs, ctx


def evaluate(obs, ctx):
    pre_state = {}
    try:
        with open(f"{EVDIR}/pre.json") as f:
            pre_state = envelope_state(json.load(f))
    except Exception as e:
        obs["notes"].append(f"PRE export missing/unreadable: {e}")
    post_state = {}
    try:
        with open(f"{EVDIR}/post.json") as f:
            post_state = envelope_state(json.load(f))
    except Exception as e:
        obs["notes"].append(f"POST export missing/unreadable: {e}")
    obs["ctx"] = {k: ctx.get(k) for k in
                  ("attempt", "phase", "locke_cast", "cast_oid",
                   "attack_declared", "trigger_resolved", "treasure_seen",
                   "decline_ambiguous")}

    # ---- A1: setup.
    if pre_state:
        locke_bf = bool(bf_named(pre_state, 0, LOCKE))
        active0 = int(pre_state.get("active_player", -1)) == 0
        precombat = pre_state.get("phase") == "PreCombatMain"
        if locke_bf and active0 and precombat:
            obs["assert"]["A1_setup_ok"] = "passed"
            obs["notes"].append(
                "A1 passed: PRE has Locke on P0 BF, P0 active in "
                f"PreCombatMain (turn {pre_state.get('turn_number')})")
        else:
            obs["assert"]["A1_setup_ok"] = "failed"
            obs["notes"].append(
                f"A1 FAILED: locke_on_bf={locke_bf} active_is_p0={active0} "
                f"precombat={precombat}")
    else:
        obs["notes"].append("A1 not-run: no PRE export")

    # ---- A2: everyone milled exactly one land.
    if pre_state and post_state:
        pre_gy = ctx.get("pre_gy") or \
            {p: len(gy_oids(pre_state, p)) for p in range(4)}
        post_gy = {p: len(gy_oids(post_state, p)) for p in range(4)}
        pre_ids = {p: set(gy_oids(pre_state, p)) for p in range(4)}
        ok = True
        detail = {}
        for p in range(4):
            new = [o for o in gy_oids(post_state, p) if o not in pre_ids[p]]
            lands = [o for o in new if is_land(get_obj(post_state, o))]
            detail[p] = {"milled": len(new),
                         "names": [obj_lname(post_state, o) for o in new],
                         "all_land": len(new) == len(lands) and len(new) > 0}
            if not (len(new) == 1 and len(lands) == 1):
                ok = False
        obs["mill_detail"] = detail
        if ok:
            obs["assert"]["A2_all_milled_land"] = "passed"
            obs["notes"].append(
                f"A2 passed: each player milled exactly 1 land "
                f"({detail})")
        else:
            obs["assert"]["A2_all_milled_land"] = "failed"
            obs["notes"].append(
                f"A2 FAILED: mill detail per player: {detail} "
                f"(expected exactly 1 land each)")
    else:
        for k in ("A2_all_milled_land",):
            obs["notes"].append(f"{k} not-run: PRE/POST export missing")

    # ---- A3: Treasure count.
    if post_state:
        n, oids = treasure_count(post_state)
        obs["treasures"] = {"count": n, "objects": oids}
        if n == 1:
            obs["assert"]["A3_treasure_count"] = "passed"
            obs["notes"].append(
                f"A3 passed: exactly 1 Treasure token on the battlefield "
                f"{oids} -- matches oracle + ruling")
        else:
            obs["assert"]["A3_treasure_count"] = "failed"
            sig = ("issue signature (one per opponent)"
                   if n == 3 else "non-1 count")
            obs["notes"].append(
                f"A3 FAILED: {n} Treasure tokens on the battlefield "
                f"(expected 1) -- {sig}; objects={oids}")
    else:
        obs["notes"].append("A3 not-run: no POST export")

    # ---- A4: cleanup.
    if post_state:
        stack_empty = not (post_state.get("stack") or [])
        game_over = bool(post_state.get("game_over") or
                         post_state.get("winner") is not None)
        if stack_empty and not game_over:
            obs["assert"]["A4_cleanup"] = "passed"
            obs["notes"].append("A4 passed: POST stack empty, game ongoing")
        else:
            obs["assert"]["A4_cleanup"] = "failed"
            obs["notes"].append(
                f"A4 FAILED: stack_empty={stack_empty} game_over={game_over}")
    else:
        obs["notes"].append("A4 not-run: no POST export")


# ------------------------------------------------------------------ rendering

def render_summary(combined):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        font_b = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_t = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
        font_b = font
        font_t = font
    img = Image.new("RGB", (1000, 800), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #9225 -- Locke, Treasure Hunter",
           font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.110.0 (9bfd12e, protocol 142) | 4-player game, native engine",
           font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = str(combined.get("verdict", "blocked")).upper().replace("_", "-")
    if verdict not in ("REPRODUCED", "NOT-REPRODUCED", "BLOCKED"):
        verdict = "BLOCKED / NOT-RUN"
    color = (255, 90, 90) if verdict == "REPRODUCED" else (
        (90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    lo = combined.get("obs", {})
    a = lo.get("assert", {})
    d.text((24, y), f"Game {lo.get('game_code')}:", font=font_b,
           fill=(220, 220, 230))
    y += 28
    for k in ASSERT_KEYS:
        v = a.get(k, "not-run")
        vc = (90, 255, 140) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (255, 200, 60))
        d.text((48, y), f"{k}: {v}", font=font_b, fill=vc)
        y += 28
    tr = lo.get("treasures", {})
    d.text((48, y), f"Treasure tokens on BF: {tr.get('count')} "
           f"(expected 1; bug: 3, one per opponent)", font=font,
           fill=(220, 220, 230))
    y += 28
    md = lo.get("mill_detail", {})
    mills = ", ".join(f"P{p}: {v.get('milled')}x "
                      f"{','.join(v.get('names', []))}" for p, v in sorted(md.items())) \
        if md else "n/a"
    d.text((48, y), f"Milled per player: {mills}", font=font,
           fill=(220, 220, 230))
    y += 34
    d.text((24, y), "Expected (oracle + 2025-06-06 ruling): one or more lands",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "milled this way -> exactly ONE Treasure total.",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Bug: player_scope: All fan-out re-runs the gated Token",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "link once per opponent -> 3 Treasures in a 4-player game.",
           font=font, fill=(220, 220, 230))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


# ------------------------------------------------------------------ main

async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_leg(1)
    # Retry the leg when the fixture was non-discriminating (a mill
    # missed a land) or setup never completed.
    a0 = obs["assert"]
    need_retry = (not ctx.get("locke_cast")) or \
        (a0.get("A2_all_milled_land") == "failed"
         and a0.get("A1_setup_ok") == "passed")
    if need_retry:
        say("attempt 1 inconclusive (setup incomplete or mill missed a "
            "land); running attempt 2")
        wire("retry", {"reason": "inconclusive attempt 1"})
        obs, ctx = await run_leg(2)

    a = obs["assert"]

    def reproduced():
        return a.get("A1_setup_ok") == "passed" \
            and a.get("A2_all_milled_land") == "passed" \
            and a.get("A3_treasure_count") == "failed"

    def clean():
        return all(a.get(k) == "passed" for k in ASSERT_KEYS)

    if reproduced():
        verdict = "reproduced"
    elif clean():
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    combined = {
        "server_identity": SERVER_IDENTITY,
        "issue": ISSUE,
        "run_id": RUN_ID,
        "obs": obs,
        "verdict": verdict,
        "card_data_oracle": {
            "locke, treasure hunter":
                (CARD_DATA.get("locke, treasure hunter") or {}).get("oracle_text"),
        },
    }
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(combined, f, indent=1, default=str)
    for k in ASSERT_KEYS:
        say(f"assertion {k}: {obs['assert'].get(k)}")
    for n in obs["notes"]:
        say(f"note: {n}")
    say(f"VERDICT: {verdict}")
    result_line = "; ".join(f"{k} {v}" for k, v in obs["assert"].items()) \
        + f"; treasures={obs.get('treasures')}; mill_detail={obs.get('mill_detail')}"
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9225",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.110.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified release); "
                           f"evidence under evidence/9225/{RUN_ID}"),
        "driver": {"protocol_advertised": 142, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Locke, Treasure Hunter", 4], ["Swamp", 28], ["Mountain", 28]],
            "P1": [["Forest", 60]],
            "P2": [["Forest", 60]],
            "P3": [["Forest", 60]],
        },
        "assertions": {k: v for k, v in obs["assert"].items()},
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": result_line,
        "scope": ("Locke, Treasure Hunter's Mug trigger in a 4-player "
                  "free-for-all: each player mills 1 card (a land, by "
                  "construction); the 'if a land was milled this way, "
                  "create a Treasure token' clause must create exactly 1 "
                  "Treasure total. Native engine, four human-client seats."),
        "limitations": [
            "Browser UI not exercised; native engine via human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "4x/28x/60x deck densities are test-harness conveniences (engine accepts >4-of for custom games); all-land opponent decks make every mill hit a land deterministically.",
            "The third clause ('you may cast a spell from among those cards') is a separate defect fixed upstream by #9230 and is out of scope; the driver declines its prompt when offered. Milled cards are lands (not castable as spells).",
            "Verdict rule: reproduced = A1 + A2 passed and A3 failed (Treasure count != 1). not-reproduced = all assertions passed. blocked = setup never completed, a mill missed a land, or the trigger never observably resolved.",
        ],
        "setup_line": ("P0 4x Locke + 28x Swamp + 28x Mountain; P1/P2/P3 60x Forest (draw-go). "
                       "P0 mulligans until Locke in hand, ramps 3 lands, casts Locke, attacks P1 with Locke; "
                       "all seats declare no blockers; driver passes priority and declines the cast-permission prompt."),
        "contract_line": ("Each player mills exactly 1 land -> exactly 1 Treasure token total "
                          "(oracle + 2025-06-06 ruling). Bug: 3 Treasures (one per opponent)."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump({"locke, treasure hunter":
                   CARD_DATA.get("locke, treasure hunter")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/locke_lowering.json", "w") as f:
        json.dump({"triggers": (CARD_DATA.get("locke, treasure hunter") or {}).get("triggers"),
                   "note": "Mug trigger: mode Attacks, player_scope All; Mill 1 -> Controller; "
                           "sub_ability gated by ZoneChangedThisWay{Land} -> Token Treasure "
                           "(owner Controller). This is the exact shape the issue describes."},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9225.py", "w") as f:
        f.write(open(__file__).read())
    render_summary(combined)
    wire("verdict", {"verdict": verdict,
                     "assertions": run["assertions"]})
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        with open(f"{EVDIR}/{fn}", "rb") as f:
            lines.append(hashlib.sha256(f.read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(run['assertions'])}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
