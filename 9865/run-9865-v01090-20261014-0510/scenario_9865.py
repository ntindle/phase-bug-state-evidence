#!/usr/bin/env python3
"""Issue #9865: Kathril, Aspect Warper -- "+1/+1 counter for each counter
put on a creature this way" counts objects, not counters.

Oracle:
  "When Kathril enters, put a flying counter on any creature you control
  if a creature card in your graveyard has flying. Repeat this process
  for first strike, double strike, deathtouch, hexproof, indestructible,
  lifelink, menace, reach, trample, and vigilance. Then put a +1/+1
  counter on Kathril for each counter put on a creature this way."

Reported (read from the lowering, not re-measured for the full
eleven-keyword run): the quantity counts the objects in the tracked set
rather than the counters placed on them (a creature that receives two
different counters is counted once). Confirmed in the pinned v0.109.0
card-data lowering: the final sub-ability is PutCounter P1P1 on
SelfRef with count = Ref { qty: TrackedSetSize } -- a tracked-SET size,
not a placed-counter count (see kathril_lowering.json in evidence).

Behavioral contract -- single leg, 2-player game, native engine:
  Setup: P0 plays Grizzly Bears (the counter recipient), Viscera Seer,
    and Vampire Nighthawk (flying + deathtouch + lifelink), then
    activates Seer sacrificing Nighthawk (Nighthawk -> graveyard, the
    only keyword-bearing creature card there), then activates Seer
    sacrificing Seer itself (so Bears is P0's only non-Kathril creature
    when Kathril enters; any counter placement must land on Bears or
    on Kathril itself -- both keep the distinct-recipient count at 1).
  PRE is exported when the Kathril cast is imminent (Kathril in hand,
    affordable, Nighthawk in graveyard, Bears on BF). P0 casts Kathril.
  ETB: the engine should offer one counter-placement choice per live
    keyword (flying, deathtouch, lifelink -- 3 prompts). The driver
    answers every one with the SAME creature (Grizzly Bears), so the
    discriminating configuration is 3 counters on 1 object.
  POST is exported once the ETB has settled (Kathril on BF, stack
    empty, no ETB prompt pending, 3 quiet ticks).

Assertions:
  A1_setup_ok         PRE has Kathril in P0 hand, Nighthawk in P0 GY,
                      Bears on P0 BF.
  A2_counters_placed  POST: exactly 3 keyword counters (flying +
                      deathtouch + lifelink) were placed on P0's
                      creatures (the ETB fired for all 3 live keywords).
  A2b_single_recipient POST: those 3 counters sit on exactly 1 distinct
                      creature (the discriminating configuration).
  A3_kathril_p1p1    POST: Kathril has exactly 3 +1/+1 counters
                      (expected under the oracle). The reported bug
                      gives 1 (TrackedSetSize counts the 1 recipient
                      object). Kathril P/T: expected 6/6, bug 4/4.
  A4_cleanup          POST stack empty, Kathril on P0 BF.

Verdict rule (pre-registered):
  - reproduced: A1 passed + A2 passed + A2b passed + A3 failed
    (Kathril +1/+1 != 3; the object-count signature is +1).
  - not-reproduced: all assertions passed (Kathril +3).
  - blocked: setup never completed, counters never placed (A2 failed),
    or recipients spread (A2b failed -- non-discriminating), etc.

Prompt shapes (NOT pre-verified for this card -- the driver dumps
every ETB/sac/scry opportunity to evidence and answers generically):
  - Counter-placement choices are expected as schema selects (or
    exactChoices) whose candidates reference P0 BF creatures; the
    driver answers each with Grizzly Bears, submitting with the
    opportunity's own response type (select/sequence/choose).
  - Viscera Seer activation is the legacy ActivateAbility action
    {source_id, ability_index}; its Sacrifice cost is expected as a
    schema select over P0 BF creatures (the 9861 sacrifice-cost
    prompt shared the SelectCards+vi-schema surface).
  - Scry 1 is incidental (any legal answer is fine); the driver
    answers top/keep when offered, else the first choice, and logs
    the opportunity.

Conventions (from scenario_9862.py, protocol 140):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - Export response data.state is a JSON string, parsed once.
  - finish() ordering: run.json, scenario copy, every logging hook,
    THEN close WIRE/RUNLOG, THEN manifest LAST.
  - DiscardToHandSize is gated on the state's waiting_for type.
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
log = logging.getLogger("scenario9865")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9865-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9865
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.109.0",
    "build_commit": "932e389",
    "protocol_version": 140,
    "mode": "Full",
    "server_binary_sha256": "b2522f3657873456f1a5d17c2c97842e2ea2c6833afcbb7bc492696ff89142d5",
    "card_data_sha256": "9089f823081843e8ad700ce362a448a3ba165d377a23465c86355e8ed07e5225",
    "draft_pools_sha256": "2543f1bdfb3e4c2469ff3193b91a936d3ddc6bde99c6f5d19926cae4743c7309",
    "signature_verified": True,
    "signature_note": ("v0.109.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-14: latest stable release v0.109.0 (published "
               "2026-10-13T18:43:00Z) == pinned release dir; ServerHello "
               "0.109.0/932e389/protocol 140 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.109.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release); "
               "scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9865/" + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


for _f, _k in (("server/releases/v0.109.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.109.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.109.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against the pinned v0.109.0 digests",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.109.0/data/card-data.json"))


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
RETRIED_OPPS = set()
RECORDED_OPPS = set()
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


def player_life(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal", "life_total_value"):
        if p.get(k) is not None:
            try:
                return int(p.get(k))
            except (TypeError, ValueError):
                pass
    return None


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def library_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_named(state, pid, name):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == name]


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def is_land(o):
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def stack_entries(state):
    out = []
    for e in state.get("stack") or []:
        if isinstance(e, dict):
            out.append(e)
        else:
            out.append(get_obj(state, e))
    return out


def stack_entry_name(state, e):
    if isinstance(e, dict):
        for k in ("base_name", "name"):
            if e.get(k):
                return str(e.get(k))
        for k in ("source_id", "id", "oid"):
            if e.get(k) is not None:
                o = get_obj(state, e.get(k))
                for kk in ("base_name", "name"):
                    if o.get(kk):
                        return str(o.get(kk))
    elif isinstance(e, (int, str)):
        o = get_obj(state, e)
        for kk in ("base_name", "name"):
            if o.get(kk):
                return str(o.get(kk))
    return "?"


def in_graveyard(state, pid, name):
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() == name \
                and o.get("zone") == "Graveyard" \
                and str(o.get("controller", o.get("owner", -1))) == str(pid):
            return True
    return False


def envelope_state(exported):
    if not isinstance(exported, dict):
        return {}
    if "objects" in exported:
        return exported
    if isinstance(exported.get("state"), dict):
        return exported["state"]
    return exported


def counters_of(state, oid):
    """Normalized counter map for one object: keys lowercased, '+'/' ' stripped."""
    o = get_obj(state, oid)
    c = o.get("counters") or {}
    out = {}
    if isinstance(c, dict):
        for k, v in c.items():
            out[str(k).lower().replace("+", "").replace(" ", "")] = v
    elif isinstance(c, list):
        for item in c:
            if isinstance(item, dict):
                t = str(item.get("type") or item.get("counter_type") or "?")
                out[t.lower().replace("+", "").replace(" ", "")] = \
                    out.get(t.lower().replace("+", "").replace(" ", ""), 0) + int(item.get("count", 1) or 1)
    return out


KATHRIL = "kathril, aspect warper"
NIGHTHAWK = "vampire nighthawk"
SEER = "viscera seer"
BEARS = "grizzly bears"
SWAMP = "swamp"
FOREST = "forest"
PLAINS = "plains"
KEYWORD_COUNTERS = ("flying", "deathtouch", "lifelink")


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


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


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def _cand_surface(ch):
    """The object surface of a schema candidate (reference/name/zone/...)."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return d
    return {}


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
                    "stype": (resp.get("data", {}) or {}).get("spec", {}).get("type")
                    if isinstance((resp.get("data", {}) or {}).get("spec"), dict) else None,
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c)})
    return out

# ------------------------------------------------------------- action helpers

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
    assert str(ver).startswith("0.109.0"), f"unexpected version {ver}"
    assert int(proto) == 140, f"unexpected protocol {proto}"
    assert str(build) == "932e389", f"unexpected build {build}"


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_names(state, pid)
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Cleanup discard, gated on the state's waiting_for type. Prefers
    a land, else a duplicate spell copy, else the first card -- never
    the only copy of a setup piece."""
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
        names = [obj_lname(state, _cand_ref(ch)) for ch in cands]
        counts = {}
        for n in hand_names(state, pid):
            counts[n] = counts.get(n, 0) + 1
        picks = [ch for ch, n in zip(cands, names)
                 if is_land(get_obj(state, _cand_ref(ch)))] or \
                [ch for ch, n in zip(cands, names)
                 if counts.get(n, 0) > 1] or cands[:1]
        picks = picks[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:40] for x in picks]}")
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
                manas = [s for s in ch.get("surfaces", []) or []]
                syms = []
                for m in manas:
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
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
        await interact_as(c, sub, tag)
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
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_named_land(c, state, pid, acts, tag, name):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    lands = [o for o in hand_ids(state, pid)
             if is_land(get_obj(state, o))
             and obj_lname(state, o) == name]
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and \
                    str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


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


def untapped_land_count(state, pid, name):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == name
               and not get_obj(state, o).get("tapped"))


def can_afford(state, pid, needs):
    """True if P0's untapped lands cover the color + generic needs."""
    color_land = {"W": PLAINS, "B": SWAMP, "G": FOREST, "U": "island",
                  "R": "mountain"}
    total = 0
    for color, n in needs.items():
        if color == "generic":
            continue
        have = untapped_land_count(state, pid, color_land.get(color, ""))
        if have < n:
            return False
        total += have
    generic = needs.get("generic", 0)
    total_untapped = sum(1 for o in bf_oids(state, pid)
                         if is_land(get_obj(state, o))
                         and not get_obj(state, o).get("tapped"))
    return total_untapped >= sum(v for k, v in needs.items()
                                 if k != "generic") + generic


def p0_main(state, pid=0, empty_stack=True):
    if state.get("phase") not in ("PreCombatMain", "PostCombatMain"):
        return False
    if int(state.get("active_player", -1)) != pid:
        return False
    if empty_stack and (state.get("stack") or []):
        return False
    return True


async def declare_no_attackers(c, acts):
    da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
    if da:
        import copy as _copy
        d = _copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    return False


def opp_text_blob(opp):
    parts = []
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    for k in ("prompt", "description", "title", "message"):
        if data.get(k):
            parts.append(str(data.get(k)))
    for ch in data.get("choices") or data.get("candidates") or []:
        parts.append(choice_text(ch))
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                for k in ("description", "prompt", "name", "value"):
                    if d.get(k) is not None:
                        parts.append(str(d.get(k)))
    return " | ".join(parts)


def dump_opp(state, opp, kind, tag, ctx):
    n = ctx.get("opp_dumps", 0) + 1
    ctx["opp_dumps"] = n
    with open(f"{EVDIR}/opp_{kind}_{n}.json", "w") as f:
        json.dump({"kind": kind, "tag": tag,
                   "waiting_for": state.get("waiting_for"),
                   "opportunity": opp, "text_blob": opp_text_blob(opp)},
                  f, indent=1, default=str)
    wire("opp_dump", {"n": n, "kind": kind, "tag": tag,
                      "blob": opp_text_blob(opp)[:400]})
    say(f"[{tag}] {kind} opp #{n}: blob={opp_text_blob(opp)[:220]}")


# ------------------------------------------------------- Kathril handlers

def _pick_bf_creature_candidate(st, opp, pid, want_oid):
    """Find the candidate in opp whose reference is want_oid (a P0 BF
    creature). Returns the candidate's choice id, or None."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    items = data.get("candidates") or data.get("choices") or []
    for ch in items:
        if _cand_ref(ch) == str(want_oid):
            return ch.get("id")
    return None


def _submit_pick(opp, choice_id):
    """Build the interaction submission for picking ONE candidate from
    opp. The submission type comes from the opportunity's own shape:
    a schema/select opportunity takes {"type": "select", "choiceIds":
    [...]}, an exactChoices opportunity takes {"type": "choose",
    "choiceId": ...}. (Submitting "choose" to a schema/select opp is
    silently ignored by the engine -- caught on the #9865 sac-cost
    prompt: the game stalled at rev 217 with progress selected == 0.)"""
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    if rtype == "schema" and (spec.get("type") or "") == "select":
        return {"interactionId": opp.get("interactionId"),
                "response": {"type": "select",
                             "data": {"choiceIds": [choice_id]}}}
    if rtype == "sequence":
        return {"interactionId": opp.get("interactionId"),
                "response": {"type": "sequence",
                             "data": {"choiceIds": [choice_id]}}}
    return {"interactionId": opp.get("interactionId"),
            "response": {"type": "choose",
                         "data": {"choiceId": choice_id}}}


async def answer_sac_cost(c, st, pid, tag, ctx):
    """Viscera Seer Sacrifice cost: a schema/exactChoices prompt whose
    candidates reference P0 BF creatures. Pick the designated sac
    target (Nighthawk first, then Seer itself)."""
    want_name = ctx.get("awaiting_sac")
    if not want_name:
        return False
    state = st["state"]
    want_oids = bf_named(state, pid, want_name)
    if not want_oids:
        return False
    want_oid = want_oids[0]
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            # Still pending after a submission: one bounded retry with
            # the same payload if the engine recorded no progress.
            if iid in RETRIED_OPPS:
                continue
            prog = opp.get("progress", {}) or {}
            if int(prog.get("selected", 0) or 0) > 0:
                continue
            prev = ctx.get("submitted_subs", {}).get(iid)
            if prev is None:
                continue
            RETRIED_OPPS.add(iid)
            say(f"[{tag}] sac opp {str(iid)[:16]} still pending; "
                f"retrying once with the same payload")
            await interact_as(c, dict(prev), tag)
            return True
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        refs = {_cand_ref(ch) for ch in items}
        # The sac prompt's candidates are P0 BF creatures (or a subset).
        bfset = set(bf_oids(state, pid))
        if not (refs & bfset):
            continue
        # Do not steal the ETB counter-placement prompt: the sac
        # handler only runs while awaiting_sac is set, and the ETB
        # phase never sets awaiting_sac -- but the ETB also offers
        # BF-creature candidates, so gate on the phase too.
        if ctx.get("phase") == "etb":
            continue
        choice_id = _pick_bf_creature_candidate(st, opp, pid, want_oid)
        if choice_id is None:
            continue
        dump_opp(state, opp, "sac_cost", tag, ctx)
        SUBMITTED_OPPS.add(iid)
        ctx["sac_picks"] = ctx.get("sac_picks", []) + [
            {"want": want_name, "oid": str(want_oid)}]
        ctx["awaiting_sac"] = None
        say(f"[{tag}] sac cost: sacrificing {want_name} (oid {want_oid})")
        wire("sac_cost", {"who": tag, "sac": want_name, "oid": str(want_oid)})
        sub = _submit_pick(opp, choice_id)
        ctx.setdefault("submitted_subs", {})[iid] = sub
        await interact_as(c, sub, tag)
        return True
    return False


async def answer_scry(c, st, pid, tag, ctx):
    """Scry 1 (incidental -- any legal answer is fine). Only fires when
    the opportunity or waiting_for actually says Scry."""
    state = st["state"]
    wf = str((state.get("waiting_for") or {}).get("type", ""))
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        blob = opp_text_blob(opp).lower()
        if "scry" not in wf.lower() and "scry" not in blob:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        dump_opp(state, opp, "scry", tag, ctx)
        SUBMITTED_OPPS.add(iid)
        # Prefer a keep-on-top choice; else the first choice.
        pick = None
        for ch in items:
            t = choice_text(ch).lower()
            if "top" in t or "keep" in t or "leave" in t:
                pick = ch
                break
        if pick is None:
            pick = items[0]
        say(f"[{tag}] scry -> {choice_text(pick)[:60]}")
        wire("scry", {"who": tag, "pick": choice_text(pick)[:80]})
        await interact_as(c, _submit_pick(opp, pick.get("id")), tag)
        return True
    # Fallback: during setup, an unidentified schema select that no
    # other handler claimed is almost certainly the Scry 1 prompt
    # (mulligan/bottom, sac cost, discard, and ETB prompts are all
    # handled or gated elsewhere). Answer the first candidate rather
    # than stalling; scry's outcome is irrelevant to the measurement.
    if ctx.get("phase") in ("setup", "sac_seer"):
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            if iid in SUBMITTED_OPPS:
                continue
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            data = resp.get("data", {}) or {}
            spec = data.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            items = data.get("candidates") or data.get("choices") or []
            if not items:
                continue
            wf2 = str((state.get("waiting_for") or {}).get("type", ""))
            if "Discard" in wf2:
                continue
            dump_opp(state, opp, "scry_fallback", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            say(f"[{tag}] scry fallback: answering unidentified schema "
                f"select with first candidate "
                f"(wf={wf2}, blob={opp_text_blob(opp)[:120]})")
            wire("scry_fallback", {"who": tag, "wf": wf2})
            await interact_as(c, _submit_pick(opp, items[0].get("id")), tag)
            return True
    return False


async def answer_etb_counter(c, st, pid, tag, ctx):
    """Kathril ETB counter placement: one choice per live keyword.
    Candidates reference P0 BF creatures; answer every one with the
    SAME creature (Grizzly Bears), so all counters land on 1 object."""
    if ctx.get("phase") != "etb":
        return False
    state = st["state"]
    bears = bf_named(state, pid, BEARS)
    if not bears:
        return False
    want_oid = bears[0]
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            # One bounded retry for a still-pending iid with no
            # recorded progress (a silently-ignored submission).
            if iid in RETRIED_OPPS:
                continue
            prog = opp.get("progress", {}) or {}
            if int(prog.get("selected", 0) or 0) > 0:
                continue
            prev = ctx.get("submitted_subs", {}).get(iid)
            if prev is None:
                continue
            RETRIED_OPPS.add(iid)
            say(f"[{tag}] ETB opp {str(iid)[:16]} still pending; "
                f"retrying once with the same payload")
            await interact_as(c, dict(prev), tag)
            return True
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        refs = {_cand_ref(ch) for ch in items}
        bfset = set(bf_oids(state, pid))
        if not (refs & bfset):
            continue
        # Never steal a cleanup discard (waiting_for-gated) or a sac
        # cost (awaiting_sac is only set outside the ETB phase).
        wf = str((state.get("waiting_for") or {}).get("type", ""))
        if "Discard" in wf:
            continue
        choice_id = _pick_bf_creature_candidate(st, opp, pid, want_oid)
        if choice_id is None:
            continue
        dump_opp(state, opp, "etb_counter", tag, ctx)
        SUBMITTED_OPPS.add(iid)
        blob = opp_text_blob(opp).lower()
        keyword = next((k for k in
                        ("flying", "first strike", "double strike",
                         "deathtouch", "hexproof", "indestructible",
                         "lifelink", "menace", "reach", "trample",
                         "vigilance") if k in blob), "?")
        ctx["etb_picks"] = ctx.get("etb_picks", []) + [
            {"keyword": keyword, "oid": str(want_oid), "name": BEARS}]
        say(f"[{tag}] ETB counter ({keyword}): placing on {BEARS} "
            f"(oid {want_oid}); picks so far={len(ctx['etb_picks'])}")
        wire("etb_counter", {"who": tag, "keyword": keyword,
                             "oid": str(want_oid)})
        sub = _submit_pick(opp, choice_id)
        ctx.setdefault("submitted_subs", {})[iid] = sub
        await interact_as(c, sub, tag)
        return True
    return False


def etb_pending(st, ctx):
    """True if an ETB counter-placement prompt is pending on this view."""
    if not st or ctx.get("phase") != "etb":
        return False
    state = st["state"]
    wf = str((state.get("waiting_for") or {}).get("type", ""))
    if "Discard" in wf:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        refs = {_cand_ref(ch) for ch in items}
        if refs & set(bf_oids(state, 0)):
            return True
    return False


# ------------------------------------------------------------------- ticks

async def common_tick_head(c, acts, st, pid, tag):
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareBlockers" in atypes:
        return True  # never block in this scenario; hold
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    return None


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


async def try_activate_seer(c, state, acts, tag, ctx, sac_target):
    """Activate Viscera Seer (ability_index 0) to sacrifice sac_target."""
    seers = bf_named(state, 0, SEER)
    if not seers:
        return False
    seer_oid = seers[0]
    for a in acts:
        if a.get("type") == "ActivateAbility" and \
                str(a.get("_src_oid")) == str(seer_oid):
            say(f"[{tag}] activating {SEER} (oid {seer_oid}) to "
                f"sacrifice {sac_target}")
            wire("activate_seer", {"who": tag, "seer_oid": str(seer_oid),
                                   "sac_target": sac_target})
            ctx["awaiting_sac"] = sac_target
            ctx["seer_activations"] = ctx.get("seer_activations", 0) + 1
            await submit_as_is(c, a)
            return True
    return False


async def play_setup_land(c, state, pid, acts, tag, ctx):
    """One land per turn. Preference: Swamp until 3, Forest until 2,
    Plains until 2, then Swamp."""
    if pid != 0:
        # P1 (draw-go): any land in hand.
        for name in (FOREST, SWAMP, PLAINS):
            if await play_named_land(c, state, pid, acts, tag, name):
                return True
        return False
    n_swamp = untapped_land_count(state, pid, SWAMP) + sum(
        1 for o in bf_oids(state, pid)
        if obj_lname(state, o) == SWAMP and get_obj(state, o).get("tapped"))
    n_forest = sum(1 for o in bf_oids(state, pid)
                   if obj_lname(state, o) == FOREST)
    n_plains = sum(1 for o in bf_oids(state, pid)
                   if obj_lname(state, o) == PLAINS)
    for name in ((SWAMP,) if n_swamp < 3 else ()) + \
            ((FOREST,) if n_forest < 2 else ()) + \
            ((PLAINS,) if n_plains < 2 else ()) + (SWAMP, FOREST, PLAINS):
        if await play_named_land(c, state, pid, acts, tag, name):
            return True
    return False


async def generic_tick(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)

    r = await common_tick_head(c, acts, st, pid, tag)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    if "DeclareAttackers" in atypes:
        await declare_no_attackers(c, acts)
        return

    # --- Cost / incidental prompts (P0 only, any phase).
    if pid == 0:
        if await answer_sac_cost(c, st, pid, tag, ctx):
            return
        if await answer_scry(c, st, pid, tag, ctx):
            return
        if await answer_etb_counter(c, st, pid, tag, ctx):
            return

    # --- Cleanup discard (waiting_for-gated; runs after the prompt
    # handlers so a counter-placement select is never stolen).
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return

    # --- P0 setup + Kathril cast on its own main phase.
    if pid == 0 and p0_main(state, 0):
        phase = ctx.get("phase")
        # Phase transitions from observed state.
        if phase == "setup" and in_graveyard(state, 0, NIGHTHAWK):
            ctx["phase"] = "sac_seer"
            say(f"[{tag}] phase setup -> sac_seer (Nighthawk in GY)")
        if phase == "sac_seer" and not bf_named(state, 0, SEER):
            ctx["phase"] = "cast_kathril"
            say(f"[{tag}] phase sac_seer -> cast_kathril (Seer gone)")
        if phase == "cast_kathril" and bf_named(state, 0, KATHRIL):
            ctx["phase"] = "etb"
            say(f"[{tag}] phase cast_kathril -> etb (Kathril on BF)")
        phase = ctx.get("phase")

        if phase == "setup":
            # Cast Bears / Seer / Nighthawk when affordable.
            if not bf_named(state, 0, BEARS) and BEARS in hand_names(state, 0):
                if can_afford(state, 0, {"G": 1, "generic": 1}):
                    if await try_cast(c, state, acts, tag, BEARS,
                                      {"G": 1, "generic": 1}, pid=0, ctx=ctx):
                        return
            if not bf_named(state, 0, SEER) and SEER in hand_names(state, 0) \
                    and not in_graveyard(state, 0, NIGHTHAWK):
                if can_afford(state, 0, {"B": 1}):
                    if await try_cast(c, state, acts, tag, SEER,
                                      {"B": 1}, pid=0, ctx=ctx):
                        return
            if not bf_named(state, 0, NIGHTHAWK) and NIGHTHAWK in hand_names(state, 0) \
                    and not in_graveyard(state, 0, NIGHTHAWK):
                if can_afford(state, 0, {"B": 2, "generic": 1}):
                    if await try_cast(c, state, acts, tag, NIGHTHAWK,
                                      {"B": 2, "generic": 1}, pid=0, ctx=ctx):
                        return
            # Sac Nighthawk via Seer once both are on the BF.
            if bf_named(state, 0, NIGHTHAWK) and bf_named(state, 0, SEER) \
                    and not ctx.get("awaiting_sac"):
                if await try_activate_seer(c, state, acts, tag, ctx, NIGHTHAWK):
                    return
            if await play_setup_land(c, state, pid, acts, tag, ctx):
                return
        elif phase == "sac_seer":
            # Sac Seer to itself so Bears is the only non-Kathril
            # creature when Kathril enters.
            if bf_named(state, 0, SEER) and not ctx.get("awaiting_sac"):
                if await try_activate_seer(c, state, acts, tag, ctx, SEER):
                    return
            if await play_setup_land(c, state, pid, acts, tag, ctx):
                return
        elif phase == "cast_kathril":
            if KATHRIL in hand_names(state, 0) and bf_named(state, 0, BEARS):
                if can_afford(state, 0, {"W": 1, "B": 1, "G": 1, "generic": 2}):
                    if not ctx.get("pre_exported"):
                        pre = json.loads(await c.export_state())
                        with open(f"{EVDIR}/pre.json", "w") as f:
                            json.dump(pre, f, indent=1)
                        ctx["pre_exported"] = True
                        wire("pre_export", {})
                        say(f"[{tag}] PRE exported; casting {KATHRIL}")
                    if await try_cast(c, state, acts, tag, KATHRIL,
                                      {"W": 1, "B": 1, "G": 1, "generic": 2},
                                      pid=0, ctx=ctx):
                        ctx["kathril_cast"] = True
                        ctx["cast_at"] = time.time()
                        return
            if await play_setup_land(c, state, pid, acts, tag, ctx):
                return
        elif phase == "etb":
            # Nothing to do here except answer ETB prompts (above) and
            # pass priority; the settle detector exports POST.
            pass

    # --- Setup land plays for P0 before the spell is cast (other
    # phases), and for P1 every turn.
    if pid == 1 and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and int(state.get("active_player", -1)) == pid \
            and not (state.get("stack") or []):
        if await play_setup_land(c, state, pid, acts, tag, ctx):
            return

    if real_decision_pending(st):
        key = (tag, "prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("prompt", {"who": tag, "rev": c.revision,
                            "opps": _opp_summary(st)})
            say(f"[{tag}] prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if any(a.get("type") == "PassPriority" for a in acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------------- run

def new_ctx(attempt):
    return {
        "attempt": attempt,
        "phase": "setup",
        "awaiting_sac": None,
        "sac_picks": [],
        "seer_activations": 0,
        "etb_picks": [],
        "opp_dumps": 0,
        "submitted_subs": {},
        "kathril_cast": False,
        "cast_oid": None,
        "cast_at": None,
        "pre_exported": False,
        "post_exported": False,
        "settle_ticks": 0,
    }


async def run_leg(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    RETRIED_OPPS.clear()
    RECORDED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    ctx = new_ctx(attempt)
    p0_deck = deck((KATHRIL, 6), (NIGHTHAWK, 6), (SEER, 6), (BEARS, 6),
                   (SWAMP, 16), (FOREST, 10), (PLAINS, 10))
    p1_deck = deck((FOREST, 60))
    tags = []
    clients = []
    for pid in range(2):
        tag = f"P{pid}K{attempt}"
        tags.append(tag)
        ST["mana_needs"][tag] = {}
    clients.append(PhaseClient(f"P0K{attempt}"))
    await clients[0].connect()
    await clients[0].create(p0_deck, player_count=2)
    c1 = PhaseClient(f"P1K{attempt}")
    await c1.connect()
    await c1.join(clients[0].game_code, p1_deck)
    clients.append(c1)
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
    while time.time() - t0 < 900:
        await asyncio.sleep(0.25)
        for c, pid, tag in zip(clients, range(2), tags):
            await generic_tick(c, pid, tag, ctx)
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
            obs["notes"].append(
                "abandoned: client websocket(s) closed mid-leg "
                f"({dead}); server-side stall/disconnect, not a game "
                "decision")
            break
        if time.time() - last_rev_change > 150:
            say(f"[leg] no state-revision change in 150s "
                f"(rev={cur_rev}); abandoning leg (stalled game)")
            obs["notes"].append(
                "abandoned: no state-revision change in 150s "
                f"(rev={cur_rev}); game stalled with no client-side "
                "action available")
            break
        st_now = clients[0].latest
        if st_now and st_now["state"].get("turn_number", 0) > 40 \
                and not ctx.get("kathril_cast"):
            say(f"[leg] turn {st_now['state'].get('turn_number')} "
                f"exceeded without casting Kathril; abandoning leg")
            obs["notes"].append(
                "abandoned: game reached turn "
                f"{st_now['state'].get('turn_number')} without casting "
                "Kathril (setup never completed)")
            break
        if ctx.get("kathril_cast") \
                and time.time() - ctx.get("cast_at", 0) > 300 \
                and not ctx.get("post_exported"):
            say(f"[leg] Kathril cast 300s ago, ETB never settled; "
                f"abandoning leg")
            obs["notes"].append(
                "abandoned: Kathril cast but its ETB never settled "
                "within 300s")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pid in zip(clients, range(2)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                say(f"[diag] {c.name}: rev={c.revision} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} "
                    f"phase_ctx={ctx.get('phase')} "
                    f"awaiting_sac={ctx.get('awaiting_sac')} "
                    f"etb_picks={len(ctx.get('etb_picks', []))}")
        # --- completion: export POST once the ETB has observably
        # settled: Kathril cast, stack empty on every view, no ETB
        # prompt pending on any view, for 3 consecutive ticks.
        if ctx.get("kathril_cast") and not ctx.get("post_exported"):
            latests = [c.latest for c in clients]
            if all(x is not None for x in latests) \
                    and all(not (x["state"].get("stack") or [])
                            for x in latests) \
                    and not any(etb_pending(x, ctx) for x in latests):
                ctx["settle_ticks"] += 1
                if ctx["settle_ticks"] >= 3:
                    say("[leg] ETB settled; exporting POST")
                    wire("post_settled", {})
                    try:
                        post = json.loads(await clients[0].export_state())
                        with open(f"{EVDIR}/post.json", "w") as f:
                            json.dump(post, f, indent=1)
                        ctx["post_exported"] = True
                        say("[leg] POST exported after ETB settled")
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


ASSERT_KEYS = ("A1_setup_ok", "A2_counters_placed", "A2b_single_recipient",
               "A3_kathril_p1p1", "A4_cleanup")


def evaluate(obs, ctx):
    """Score the assertions from PRE/POST + ctx."""
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
    obs["ctx"] = {"phase": ctx.get("phase"),
                  "kathril_cast": ctx.get("kathril_cast"),
                  "cast_oid": ctx.get("cast_oid"),
                  "sac_picks": ctx.get("sac_picks", []),
                  "seer_activations": ctx.get("seer_activations", 0),
                  "etb_picks": ctx.get("etb_picks", []),
                  "opp_dumps": ctx.get("opp_dumps", 0)}

    # ---- A1: setup.
    if pre_state:
        kathril_hand = KATHRIL in hand_names(pre_state, 0)
        hawk_gy = in_graveyard(pre_state, 0, NIGHTHAWK)
        bears_bf = bool(bf_named(pre_state, 0, BEARS))
        if kathril_hand and hawk_gy and bears_bf:
            obs["assert"]["A1_setup_ok"] = "passed"
            obs["notes"].append(
                "A1 passed: PRE has Kathril in P0 hand, Nighthawk in P0 "
                f"graveyard, Bears on P0 BF (P0 life="
                f"{player_life(pre_state, 0)}, P1 life="
                f"{player_life(pre_state, 1)})")
        else:
            obs["assert"]["A1_setup_ok"] = "failed"
            obs["notes"].append(
                f"A1 FAILED: PRE kathril_in_hand={kathril_hand} "
                f"nighthawk_in_gy={hawk_gy} bears_on_bf={bears_bf}")
    else:
        obs["notes"].append("A1 not-run: no PRE export")

    # ---- A2 / A2b / A3: counter measurements from POST.
    if post_state:
        kathril_oids = bf_named(post_state, 0, KATHRIL)
        bears_oids = bf_named(post_state, 0, BEARS)
        kathril_counters = counters_of(post_state, kathril_oids[0]) \
            if kathril_oids else {}
        bears_counters = counters_of(post_state, bears_oids[0]) \
            if bears_oids else {}
        kathril_obj = get_obj(post_state, kathril_oids[0]) \
            if kathril_oids else {}
        bears_obj = get_obj(post_state, bears_oids[0]) \
            if bears_oids else {}
        # Total keyword counters + distinct recipients across P0's BF.
        total_kw = 0
        recipients = []
        per_creature = {}
        for oid in bf_oids(post_state, 0):
            co = counters_of(post_state, oid)
            kw = sum(int(co.get(k, 0) or 0) for k in KEYWORD_COUNTERS)
            per_creature[oid] = {"name": obj_lname(post_state, oid),
                                 "counters": co, "keyword_total": kw}
            if kw > 0:
                total_kw += kw
                recipients.append(oid)
        obs["kathril"] = {"oids": kathril_oids,
                          "counters": kathril_counters,
                          "p1p1": int(kathril_counters.get("p1p1", 0) or 0),
                          "power": kathril_obj.get("power"),
                          "toughness": kathril_obj.get("toughness")}
        obs["bears"] = {"oids": bears_oids, "counters": bears_counters}
        obs["keyword_counters"] = {"total": total_kw,
                                   "recipients": recipients,
                                   "per_creature": per_creature}
        obs["post_life"] = [player_life(post_state, 0),
                            player_life(post_state, 1)]

        if total_kw == 3:
            obs["assert"]["A2_counters_placed"] = "passed"
            obs["notes"].append(
                f"A2 passed: exactly 3 keyword counters placed on P0 "
                f"creatures (per-creature: {per_creature})")
        else:
            obs["assert"]["A2_counters_placed"] = "failed"
            obs["notes"].append(
                f"A2 FAILED: {total_kw} keyword counters placed on P0 "
                f"creatures (expected 3: flying + deathtouch + lifelink "
                f"for the Nighthawk in the graveyard). Per-creature: "
                f"{per_creature}; ETB picks={ctx.get('etb_picks')}")
        if len(recipients) == 1:
            obs["assert"]["A2b_single_recipient"] = "passed"
            obs["notes"].append(
                f"A2b passed: all keyword counters on exactly 1 "
                f"recipient (oid {recipients[0]} = "
                f"{obj_lname(post_state, recipients[0])}) -- the "
                f"discriminating configuration (3 counters, 1 object)")
        else:
            obs["assert"]["A2b_single_recipient"] = "failed"
            obs["notes"].append(
                f"A2b FAILED: keyword counters spread over "
                f"{len(recipients)} recipients {recipients} "
                f"(expected 1) -- non-discriminating configuration; "
                f"the object-count vs counter-count question cannot "
                f"be answered from this run")
        p1p1 = int(kathril_counters.get("p1p1", 0) or 0)
        if p1p1 == 3:
            obs["assert"]["A3_kathril_p1p1"] = "passed"
            obs["notes"].append(
                f"A3 passed: Kathril has 3 +1/+1 counters "
                f"(P/T {kathril_obj.get('power')}/"
                f"{kathril_obj.get('toughness')}) -- counts counters, "
                f"not objects")
        else:
            obs["assert"]["A3_kathril_p1p1"] = "failed"
            sig = ("object-count signature (TrackedSetSize = 1 recipient)"
                   if p1p1 == 1 else "unclassified count")
            obs["notes"].append(
                f"A3 FAILED: Kathril has {p1p1} +1/+1 counter(s) "
                f"(expected 3; P/T {kathril_obj.get('power')}/"
                f"{kathril_obj.get('toughness')}, bug predicts 4/4) -- "
                f"{sig}")
        # ---- A4: cleanup.
        stack_empty = not (post_state.get("stack") or [])
        kathril_bf = bool(kathril_oids)
        if stack_empty and kathril_bf:
            obs["assert"]["A4_cleanup"] = "passed"
            obs["notes"].append(
                "A4 passed: POST stack empty, Kathril on P0 BF")
        else:
            obs["assert"]["A4_cleanup"] = "failed"
            obs["notes"].append(
                f"A4 FAILED: stack_empty={stack_empty} "
                f"kathril_on_bf={kathril_bf}")
    else:
        for k in ("A2_counters_placed", "A2b_single_recipient",
                  "A3_kathril_p1p1", "A4_cleanup"):
            obs["notes"].append(f"{k} not-run: no POST export")
    return


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
    img = Image.new("RGB", (1000, 760), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #9865 -- Kathril, Aspect Warper", font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.109.0 (932e389, protocol 140) | 2-player game, native engine", font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = str(combined.get("verdict", "blocked")).upper().replace("_", "-")
    if verdict not in ("REPRODUCED", "NOT-REPRODUCED", "BLOCKED"):
        verdict = "BLOCKED / NOT-RUN"
    color = (255, 90, 90) if verdict == "REPRODUCED" else ((90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    lo = combined.get("obs", {})
    a = lo.get("assert", {})
    d.text((24, y), f"Game {lo.get('game_code')}:", font=font_b, fill=(220, 220, 230))
    y += 28
    for k in ASSERT_KEYS:
        v = a.get(k, "not-run")
        vc = (90, 255, 140) if v == "passed" else ((255, 90, 90) if v == "failed" else (255, 200, 60))
        d.text((48, y), f"{k}: {v}", font=font_b, fill=vc)
        y += 28
    kw = lo.get("keyword_counters", {})
    d.text((48, y), f"Keyword counters placed: {kw.get('total')} on "
           f"{len(kw.get('recipients', []))} recipient(s)", font=font, fill=(220, 220, 230))
    y += 28
    kath = lo.get("kathril", {})
    d.text((48, y), f"Kathril +1/+1: {kath.get('p1p1')}  P/T: "
           f"{kath.get('power')}/{kath.get('toughness')} "
           f"(expected 3, 6/6; bug: 1, 4/4)", font=font, fill=(220, 220, 230))
    y += 28
    d.text((48, y), f"Bears counters: {lo.get('bears', {}).get('counters')}  "
           f"ETB picks: {len(lo.get('ctx', {}).get('etb_picks', []))}",
           font=font, fill=(220, 220, 230))
    y += 34
    d.text((24, y), "Expected (oracle): 3 counters on 1 creature -> Kathril +3 +1/+1.",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Bug: TrackedSetSize counts the 1 recipient object -> Kathril +1.",
           font=font, fill=(220, 220, 230))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_leg(1)
    # Retry once when the leg never got its setup (Kathril never cast,
    # or PRE/POST missing).
    if (not ctx.get("kathril_cast") or not ctx.get("post_exported")) \
            and obs["assert"].get("A1_setup_ok") in ("failed", "not-run"):
        say("attempt 1 never completed setup; running attempt 2")
        obs, ctx = await run_leg(2)

    a = obs["assert"]

    def reproduced():
        return a.get("A1_setup_ok") == "passed" \
            and a.get("A2_counters_placed") == "passed" \
            and a.get("A2b_single_recipient") == "passed" \
            and a.get("A3_kathril_p1p1") == "failed"

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
            "kathril, aspect warper": (CARD_DATA.get("kathril, aspect warper") or {}).get("oracle_text"),
            "vampire nighthawk": (CARD_DATA.get("vampire nighthawk") or {}).get("oracle_text"),
            "viscera seer": (CARD_DATA.get("viscera seer") or {}).get("oracle_text"),
            "grizzly bears": (CARD_DATA.get("grizzly bears") or {}).get("oracle_text"),
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
        + f"; kathril={obs.get('kathril')}; bears={obs.get('bears')}; " \
        + f"keyword_counters={obs.get('keyword_counters')}; " \
        + f"etb_picks={obs.get('ctx', {}).get('etb_picks')}"
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9865",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                          "(--single-user --no-data-download, verified release); "
                          f"evidence under evidence/9865/{RUN_ID}",
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Kathril, Aspect Warper", 6], ["Vampire Nighthawk", 6],
                   ["Viscera Seer", 6], ["Grizzly Bears", 6],
                   ["Swamp", 16], ["Forest", 10], ["Plains", 10]],
            "P1": [["Forest", 60]],
        },
        "assertions": {k: v for k, v in obs["assert"].items()},
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": result_line,
        "scope": ("Kathril ETB 'for each counter put on a creature this "
                  "way' quantity: 3 keyword counters (flying, deathtouch, "
                  "lifelink from the Vampire Nighthawk in the graveyard) "
                  "all placed on the same creature (Grizzly Bears); "
                  "Kathril's +1/+1 count measured. Native engine, two "
                  "human-client seats."),
        "limitations": [
            "Browser UI not exercised; native engine via human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "6x deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Only the 3-keyword configuration was exercised in-game (Nighthawk in the graveyard); the full eleven-keyword run was not attempted. The 3-on-1 configuration already discriminates counter-counting (3) from object-counting (1).",
            "Verdict rule: reproduced = A1 + A2 + A2b passed and A3 failed. not-reproduced = all assertions passed. blocked = setup never completed, counters never placed, or recipients spread (non-discriminating).",
        ],
        "setup_line": ("P0 6x Kathril + 6x Vampire Nighthawk + 6x Viscera Seer + 6x Grizzly Bears + 16x Swamp + 10x Forest + 10x Plains; "
                       "P1 60x Forest (draw-go). P0: Bears -> Seer -> Nighthawk -> sac Nighthawk via Seer -> sac Seer to itself -> cast Kathril, "
                       "answering all 3 ETB counter placements with Grizzly Bears."),
        "contract_line": ("3 keyword counters on 1 creature -> Kathril gets 3 +1/+1 counters (6/6). "
                          "Bug: TrackedSetSize counts the 1 recipient object -> Kathril gets 1 +1/+1 counter (4/4)."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump({k: CARD_DATA.get(k) for k in
                   ("kathril, aspect warper", "vampire nighthawk",
                    "viscera seer", "grizzly bears")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/kathril_lowering.json", "w") as f:
        json.dump({"triggers": (CARD_DATA.get("kathril, aspect warper") or {}).get("triggers"),
                   "note": "Final sub-ability: PutCounter P1P1 on SelfRef with count Ref { qty: TrackedSetSize } -- "
                           "the quantity reads the tracked-SET size (objects), not the placed-counter count."},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9865.py", "w") as f:
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
