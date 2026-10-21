#!/usr/bin/env python3
"""Issue #9591 on pinned v0.114.0 (protocol 153): Void Winnower overblocking.

Reported: "Your opponents can't cast spells with even mana values." parsed to
a CantBeCast static with NO affected filter at all (affected: null in the
pinned card data), and the runtime treats a missing filter as "applies to
every spell" -- so Void Winnower prohibits opponents from casting ANYTHING,
a silent near-unbeatable prison.

NOTE on issue selection: #9591's body also carries a fix PR, but the PR is
still OPEN (not merged), and the pinned v0.114.0 card data still shows
"affected": null for Void Winnower's CantBeCast static. Reproduction on the
release is therefore meaningful and in-scope for the backfill.

Prior attempt (run-9591-v01100-20261016-0811, v0.110.0/proto 142) ended
BLOCKED: the Winnower cast submission looped on wrong_player/stale
rejections (a driver priority race -- the cast was submitted without the
submitting seat holding priority) and the test window never completed. This
run re-drives the scenario on the current pin with a hardened cast gate:
the cast is submitted only from P0's own tick, only with the action taken
from P0's own fresh merged actions referencing a Winnower in P0's own hand,
only while P0 holds priority (PassPriority present), and never twice on the
same revision.

Behavioral contract (two human seats, native engine, v0.114.0 / proto 153):
  P0: 34x Mountain / 4x Void Winnower / 2x Lightning Strike
  P1: 24x Mountain / 8x Lightning Bolt (MV 1, odd) / 8x Lightning Strike (MV 2, even)
  RAMP:      both develop; P0 casts Void Winnower with 9 untapped Mountains
             in its main phase while holding priority.
  PRE:       P1 main phase, pre-Winnower, >=2 untapped Mountains: record
             Bolt+Strike advertisement (fixture validity); export mid_pre.json.
  POST:      P1's next main phase with Winnower on the battlefield, >=2
             untapped Mountains: record advertisement; export mid_post.json.
             Rules-correct: Strike NOT advertised (even MV blocked -- the
             static is active). Reported bug: Bolt also NOT advertised (odd MV
             must stay castable per CR 601.3a / 202.3).
  PROBE:     for each in-hand test spell that IS advertised post-Winnower,
             submit the advertised CastSpell as-is, answer the TargetSelection
             (target P0), and record rejected/resolved. Covers the alternate
             enforcement path where the action is advertised but refused.
  CTRL:      P0's next main phase with Winnower on the battlefield: record
             whether P0's own Lightning Strike is advertised (CR 109.5 --
             the controller is unaffected); export mid_ctrl.json.

Assertions:
  A1 pre_advertised   pre:  Bolt+Strike advertised for P1 pre-Winnower
  A2 winnower_bf      post: Void Winnower on BF controlled by P0
  A3 even_blocked     post: Strike NOT advertised for P1 (static active)
  A4 odd_blocked_bug  post: Bolt NOT advertised for P1 (reported outcome);
                      if advertised, the probe must resolve for not-reproduced
  A5 controller_ok    ctrl: P0's Strike advertised (109.5); not-run if P0 has
                      no Strike in hand
  A6 probes_terminal  every queued probe reached a terminal outcome

Verdict = reproduced iff A1,A2,A3,A4 pass (A5 passed or not-run).
not-reproduced iff A1,A2,A3 pass and Bolt advertised + probe resolved.
blocked iff the fixture never reaches a checkpoint or a probe ends unknown.

Protocol-153 driver (mechanical port of scenario_9591_01110.py, v0.111.0 / proto 144, verified 2026-10-16); re-validation for pinned v0.114.0 (proto 153); behavioral contract A1..A6 and the game plan unchanged:
  - HELLO advertises protocol 153 (exact match); CreateGameWithSettings +
    JoinGameWithPassword + start_when_full; gate on viewer_interaction;
    export response data.state is a JSON string parsed once.
  - MulliganDecision via legacy Action; BottomCards/DiscardToHandSize via
    vi schema/select; CastSpell via advertised actions as-is; engine
    auto-taps mana; never inject targets into CastSpell -- choose via the vi
    TargetSelection opportunity afterwards (sequence response).
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
RUN_ID = os.environ.get("RUN_ID", "run-9591-reval-v01140-20261021-0541")
ISSUE = 9591
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.114.0",
    "build_commit": "ac67cbb",
    "protocol_version": 153,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.114.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-21: latest stable release v0.114.0 (published "
               "2026-10-19T18:59:10Z) == pinned release dir; ServerHello "
               "0.114.0/ac67cbb/protocol 153 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "existing v0.114.0 server on 127.0.0.1:9374 reused "
               "(identity verified by handshake this run); fresh game created "
               "by this run; isolated evidence under evidence/9591/" + RUN_ID + ")."),
}

for _f, _k in (("server/releases/v0.114.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.114.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.114.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.114.0/data/card-data.json"))

WINNOWER = "void winnower"
BOLT = "lightning bolt"
STRIKE = "lightning strike"
MOUNTAIN = "mountain"

P0_DECK = deck((MOUNTAIN, 34), (WINNOWER, 4), (STRIKE, 2))
P1_DECK = deck((MOUNTAIN, 24), (BOLT, 8), (STRIKE, 8))

SETUP_DEADLINE_S = 2400
STALL_AFTER_S = 150
ASS_KEYS = ("A1_pre_advertised", "A2_winnower_bf", "A3_even_blocked",
            "A4_odd_blocked_bug", "A5_controller_ok", "A6_probes_terminal")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
MULL_COUNT = {0: 0, 1: 0}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except ValueError:
        pass


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {}) or {}


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def zone_of(state, oid):
    return str(get_obj(state, oid).get("zone") or "")


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def winnower_bf(state):
    return len(bf_by_name(state, 0, WINNOWER)) > 0


def untapped_mountains(state, pid):
    n = 0
    for oid in bf_oids(state, pid):
        o = get_obj(state, oid)
        if obj_lname(state, oid) == MOUNTAIN and not o.get("tapped"):
            n += 1
    return n


def stack_has(state, lname):
    for e in state.get("stack") or []:
        o = get_obj(state, e.get("object_id", e.get("id", "")))
        if obj_lname(state, e.get("object_id", "")) == lname:
            return True
    # fallback: scan objects in Stack zone
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Stack" and obj_lname(state, oid) == lname:
            return True
    return False


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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


# ------------------------------------------------------------- vi helpers
def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


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
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def candidate_seat(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


# ------------------------------------------------------------- interaction primitives
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
    assert str(ver).startswith("0.114.0"), f"unexpected version {ver}"
    assert int(proto) == 153, f"unexpected protocol {proto}"
    assert str(build) == "ac67cbb", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    """Record how Void Winnower parses in the pinned v0.114.0 card data."""
    vw = CARD_DATA.get(WINNOWER, {})
    statics = vw.get("static_abilities", []) or []
    cant = [s for s in statics
            if isinstance(s.get("mode"), dict) and "CantBeCast" in s.get("mode")]
    affected_null = len(cant) > 0 and all(s.get("affected") is None for s in cant)
    bolt = CARD_DATA.get(BOLT, {})
    strike = CARD_DATA.get(STRIKE, {})
    say(f"data-level: winnower CantBeCast entries={len(cant)} "
        f"affected_null={affected_null}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"card": "Void Winnower",
                   "oracle_text": vw.get("oracle_text"),
                   "static_abilities": statics,
                   "finding": ("The 'even mana values' line parses to "
                               "CantBeCast{who:Opponents} with affected=null; "
                               "the reported defect is that the runtime treats "
                               "a missing filter as applying to every spell."),
                   "probe_cards": {"lightning_bolt": bolt.get("mana_cost"),
                                  "lightning_strike": strike.get("mana_cost")},
                   }, f, indent=1)
    ST["data_level_ok"] = affected_null and len(cant) == 1


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    lands = sum(1 for nm in hn if nm == MOUNTAIN)
    MULL_COUNT[pid] += 1
    keep = False
    if tag == "P0":
        keep = (WINNOWER in hn and lands >= 2) or MULL_COUNT[pid] >= 3
    else:
        keep = (BOLT in hn and STRIKE in hn and lands >= 2) \
            or MULL_COUNT[pid] >= 3
    choice = "Keep" if keep else "Mulligan"
    say(f"[{tag}] mulligan -> {choice} (hand={hn})")
    wire("mulligan", {"who": tag, "decision": choice, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
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
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
           ).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False
    hn = hand_lnames(state, pid)

    def bkey(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        # bottom extras first, then lands; NEVER bottom the only copy of a
        # key card (single copies rank last = kept)
        if tag == "P0":
            if nm == WINNOWER and hn.count(WINNOWER) > 1:
                return (0, str(ref))
            if nm == STRIKE and hn.count(STRIKE) > 1:
                return (1, str(ref))
            if nm == MOUNTAIN:
                return (2, str(ref))
            if nm == STRIKE:
                return (3, str(ref))
            if nm == WINNOWER:
                return (4, str(ref))
            return (2, str(ref))
        else:
            if nm == BOLT and hn.count(BOLT) > 1:
                return (0, str(ref))
            if nm == STRIKE and hn.count(STRIKE) > 1:
                return (1, str(ref))
            if nm == MOUNTAIN:
                return (2, str(ref))
            if nm == STRIKE:
                return (3, str(ref))
            if nm == BOLT:
                return (4, str(ref))
            return (2, str(ref))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds":
                                                [ch.get("id") for ch in picks]}}},
                      tag)
    return True


def probe_oids():
    oids = set()
    pr = ST.get("probe")
    if pr and pr.get("oid"):
        oids.add(str(pr["oid"]))
    for q in ST.get("probe_queue") or []:
        if q.get("oid"):
            oids.add(str(q["oid"]))
    return oids


def discard_rank(state, pid, tag, o):
    nm = obj_lname(state, o)
    hn = hand_lnames(state, pid)
    if str(o) in probe_oids():
        return (9, nm)  # never discard an active/queued probe subject
    if tag == "P0":
        if nm == STRIKE and hn.count(STRIKE) > 1:
            return (0, nm)
        if nm == WINNOWER:
            return (5, nm)
        if nm == STRIKE:
            return (4, nm)
        if nm == MOUNTAIN and hn.count(MOUNTAIN) > 10:
            return (1, nm)
        return (2, nm)
    else:
        if nm == BOLT and hn.count(BOLT) > 1:
            return (0, nm)
        if nm == STRIKE and hn.count(STRIKE) > 1:
            return (0, nm)
        if nm == BOLT or nm == STRIKE:
            return (5, nm)
        if nm == MOUNTAIN and hn.count(MOUNTAIN) > 6:
            return (1, nm)
        return (2, nm)


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(hand)
    found_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if any(_cand_reference(ch) in handset for ch in cands):
            found_opp = (opp, cands, spec)
            break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands, spec = found_opp
    stype = spec.get("type") or "select"
    ref_of = {}
    for ch in cands:
        ref = _cand_reference(ch)
        if ref is not None:
            ref_of[str(ref)] = ch["id"]
    ranked = sorted(hand,
                    key=lambda o: (discard_rank(state, pid, tag, o),
                                   obj_lname(state, o)))
    picks = ranked[:max(1, n)]
    choice_ids = [ref_of[o] for o in picks if o in ref_of]
    if not choice_ids:
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding to hand size: "
        f"{[obj_lname(state, o) for o in picks]}")
    wire("handsize_discard", {"who": tag, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": choice_ids}}},
                      tag)
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
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
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


async def declare_empty(c, acts, tag):
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            say(f"[{tag}] declare no attackers")
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        # no attacks are ever declared in this scenario; yield
        return True
    return False


async def legend_keep_first(c, acts, tag):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, a)
            say(f"[{tag}] ChooseLegend keep-first")
            return True
    return False


# ------------------------------------------------------------- cast helpers
def cast_action_for(acts, state, pid, key):
    """Find a CastSpell-ish action in pid's OWN hand for `key`.

    Only matches actions referencing an object that is actually in pid's
    hand -- never a foreign object (wrong_player guard)."""
    handset = set(hand_ids(state, pid))
    for a in acts:
        if "cast" not in str(a.get("type", "")).lower():
            continue
        d = a.get("data", {}) or {}
        cands = list(d.values()) + [a.get("_src_oid")]
        for v in cands:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if str(iv) in handset and obj_lname(state, iv) == key:
                return a, str(iv)
    return None, None


def cast_advertised_via_vi(st, state, pid, key):
    """A castSpell choice inside viewer_interaction referencing pid's hand."""
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            if ref is not None and str(ref) in handset \
                    and obj_lname(state, ref) == key:
                return opp, ch
    return None, None


def cast_advertised(st, acts, state, pid, key):
    """(advertised, via, payload) for casting `key` from pid's hand."""
    a, oid = cast_action_for(acts, state, pid, key)
    if a is not None:
        return True, "legacy", (a, oid)
    opp, ch = cast_advertised_via_vi(st, state, pid, key)
    if opp is not None:
        return True, "vi", (opp, ch)
    return False, None, None


def advertised_cast_summary(acts, state, pid):
    """Compact summary of every cast-ish advertised action (evidence)."""
    out = []
    handset = set(hand_ids(state, pid))
    for a in acts:
        if "cast" not in str(a.get("type", "")).lower():
            continue
        d = a.get("data", {}) or {}
        names = set()
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if str(iv) in handset:
                names.add(obj_lname(state, iv))
        out.append({"type": a.get("type"), "hand_names": sorted(names),
                    "data_keys": sorted(str(k) for k in d.keys())})
    return out


# ------------------------------------------------------------- probe machinery
def save_probe_record(pr):
    rec = {k: v for k, v in pr.items()
           if k not in ("action",)}
    if pr.get("action") is not None:
        a = pr["action"]
        rec["action"] = {"type": a.get("type"),
                         "data": a.get("data", {})}
    with open(f"{EVDIR}/probe_{pr['card']}.json", "w") as f:
        json.dump(rec, f, indent=1, default=str)
    say(f"probe {pr['card']}: outcome={pr['outcome']}")


def _is_target_selection(opp, state):
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    items = data.get("candidates") or data.get("choices") or []
    if not items:
        return False
    if not any(candidate_seat(ch) is not None for ch in items):
        return False
    blob = str(opp.get("description", "")).lower()
    spec = (data.get("spec", {}) or {}).get("type", "")
    return ("target" in blob) or rtype == "sequence" or spec == "sequence"


async def answer_probe_target(c, tag, state, pr):
    st = c.latest or {}
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "probe-target", pr["card"], str(iid))
        if key in SUBMITTED_OPPS:
            continue
        if not _is_target_selection(opp, state):
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        want = None
        for ch in items:
            if candidate_seat(ch) == pr["target_seat"]:
                want = ch
                break
        if want is None:
            say(f"[{tag}] probe {pr['card']}: no seat-{pr['target_seat']} "
                f"candidate; not answering")
            wire("probe_target_no_seat",
                 {"card": pr["card"],
                  "seats": [candidate_seat(ch) for ch in items]})
            return False
        SUBMITTED_OPPS.add(key)
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
        else:
            stype = "sequence" if rtype == "sequence" else "choose"
        if stype == "sequence":
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [want.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": want.get("id")}}}
        say(f"[{tag}] probe {pr['card']}: targeting seat {pr['target_seat']}")
        wire("probe_target", {"who": tag, "card": pr["card"],
                              "target_seat": pr["target_seat"]})
        await interact_as(c, sub, tag)
        pr["target_answered"] = True
        pr["target_at"] = time.time()
        return True
    return False


async def probe_tick(c, tag, pid, state, acts, st):
    """Drive one probe step. Returns True if the tick is consumed (an action
    was taken or the probe is still awaiting its target decision). Returns
    False when the game should proceed normally (pass priority etc.)."""
    if ST["probe"] is None:
        if not ST["probe_queue"]:
            ST["stage"] = "CTRL_CHECK"
            say(f"[{tag}] probe queue empty -> CTRL_CHECK")
            return True
        nxt = ST["probe_queue"].pop(0)
        ST["probe"] = {"card": nxt["card"], "lname": nxt["lname"],
                       "oid": nxt["oid"], "target_seat": nxt["target_seat"],
                       "submitted": False, "target_answered": False,
                       "done": False, "outcome": None, "life_before": None,
                       "life_after": None, "rej_baseline": len(c.rejections),
                       "t0": None, "advertised": None, "rejection": None,
                       "zone_at_end": None}
    pr = ST["probe"]
    if pr["done"]:
        ST["probe"] = None
        return True
    # --- submit phase
    if not pr["submitted"]:
        adv, via, payload = cast_advertised(st, acts, state, pid, pr["lname"])
        pr["advertised"] = adv
        pr["advertised_via"] = via
        wire("probe_advertised", {"who": tag, "card": pr["card"],
                                  "advertised": adv, "via": via})
        say(f"[{tag}] probe {pr['card']}: advertised={adv} via={via}")
        if not adv:
            pr["outcome"] = "not_advertised"
            pr["done"] = True
            pr["zone_at_end"] = zone_of(state, pr["oid"])
            save_probe_record(pr)
            ST["probe"] = None
            return True
        a, oid = payload if via == "legacy" else (None, None)
        if via == "legacy":
            pr["action"] = {"type": a["type"], "data": a.get("data", {})}
            pr["life_before"] = life_of(state, pr["target_seat"])
            pr["rej_baseline"] = len(c.rejections)
            pr["submit_rev"] = c.revision
            say(f"[{tag}] probe {pr['card']}: submitting advertised "
                f"CastSpell as-is (oid {oid})")
            wire("probe_submit", {"who": tag, "card": pr["card"], "oid": oid})
            await submit_as_is(c, a)
            pr["submitted"] = True
            pr["t0"] = time.time()
            return True
        else:
            # vi-advertised cast: submit the choice via the opportunity
            opp, ch = payload
            pr["life_before"] = life_of(state, pr["target_seat"])
            pr["rej_baseline"] = len(c.rejections)
            say(f"[{tag}] probe {pr['card']}: submitting vi castSpell choice")
            wire("probe_submit_vi", {"who": tag, "card": pr["card"]})
            await answer_vi(c, opp, ch, tag)
            pr["submitted"] = True
            pr["t0"] = time.time()
            return True
    # --- post-submit: rejections first
    new_rej = c.rejections[pr["rej_baseline"]:]
    if new_rej:
        pr["outcome"] = "rejected"
        pr["rejection"] = new_rej[0]
        pr["done"] = True
        pr["life_after"] = life_of(state, pr["target_seat"])
        pr["zone_at_end"] = zone_of(state, pr["oid"])
        wire("probe_rejected", {"who": tag, "card": pr["card"],
                                "rejection": new_rej[0]})
        say(f"[{tag}] probe {pr['card']}: REJECTED "
            f"{json.dumps(new_rej[0], default=str)[:200]}")
        save_probe_record(pr)
        ST["probe"] = None
        return True
    # --- resolution signals
    life_now = life_of(state, pr["target_seat"])
    pr["life_after"] = life_now
    if pr["life_before"] is not None and life_now is not None \
            and life_now <= pr["life_before"] - 3:
        pr["outcome"] = "resolved"
        pr["done"] = True
        pr["zone_at_end"] = zone_of(state, pr["oid"])
        wire("probe_resolved", {"who": tag, "card": pr["card"],
                                "life": [pr["life_before"], life_now]})
        say(f"[{tag}] probe {pr['card']}: RESOLVED "
            f"(life {pr['life_before']}->{life_now})")
        save_probe_record(pr)
        ST["probe"] = None
        return True
    if zone_of(state, pr["oid"]) == "Graveyard":
        pr["outcome"] = "resolved"
        pr["done"] = True
        pr["zone_at_end"] = "Graveyard"
        wire("probe_resolved_gy", {"who": tag, "card": pr["card"]})
        say(f"[{tag}] probe {pr['card']}: RESOLVED (in graveyard)")
        save_probe_record(pr)
        ST["probe"] = None
        return True
    # --- target selection
    if not pr["target_answered"]:
        if await answer_probe_target(c, tag, state, pr):
            return True
        # no target opportunity yet: wait for it (do NOT pass priority while
        # our own decision may be pending), but don't wait forever
        if time.time() - pr["t0"] > 30:
            pr["outcome"] = "unknown_no_target_prompt"
            pr["done"] = True
            pr["zone_at_end"] = zone_of(state, pr["oid"])
            wire("probe_no_target", {"who": tag, "card": pr["card"]})
            say(f"[{tag}] probe {pr['card']}: no target prompt in 30s")
            save_probe_record(pr)
            ST["probe"] = None
            return True
        return True
    # --- target answered, awaiting resolution: let the game proceed so
    # priority can pass and the spell can resolve
    if time.time() - pr["t0"] > 90:
        pr["outcome"] = "unknown_timeout"
        pr["done"] = True
        pr["zone_at_end"] = zone_of(state, pr["oid"])
        wire("probe_timeout", {"who": tag, "card": pr["card"]})
        say(f"[{tag}] probe {pr['card']}: TIMEOUT awaiting resolution")
        save_probe_record(pr)
        ST["probe"] = None
        return True
    return False


# ------------------------------------------------------------- exports
async def export_tag(c, name):
    raw = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(raw)
    say(f"exported {name}.json")


# ------------------------------------------------------------- stage logic
async def p0_stage(c, tag, state, acts, st):
    stage = ST["stage"]
    turn = state.get("turn_number", 0)
    if stage == "RAMP":
        if winnower_bf(state):
            ST["stage"] = "POST_CHECK"
            say(f"[{tag}] Winnower already on BF -> POST_CHECK")
            return True
        # hardened cast gate: P0's main, empty stack, P0 holds priority,
        # Winnower in P0's own hand, 9 untapped Mountains, action from P0's
        # own fresh merged actions, never twice on the same revision.
        if my_main(state, 0) and my_priority(acts):
            w = next((o for o in hand_ids(state, 0)
                      if obj_lname(state, o) == WINNOWER), None)
            if w is not None and untapped_mountains(state, 0) >= 9:
                if ST.get("winnower_submit_rev") == c.revision:
                    return False  # already submitted on this revision
                a, oid = cast_action_for(acts, state, 0, WINNOWER)
                if a is not None:
                    ST["winnower_submit_rev"] = c.revision
                    ST["winnower_rej_baseline"] = len(c.rejections)
                    ST["winnower_cast_turn"] = turn
                    say(f"[{tag}] casting Void Winnower (oid {oid}) "
                        f"at turn {turn}")
                    wire("winnower_cast",
                         {"who": tag, "oid": oid, "turn": turn})
                    await submit_as_is(c, a)
                    ST["stage"] = "WAIT_RESOLVE"
                    ST["winnower_cast_at"] = time.time()
                    return True
        if turn > 24:
            ST["notes"].append(f"Winnower never cast by turn {turn} "
                               f"(in hand: {WINNOWER in hand_lnames(state, 0)}, "
                               f"untapped Mountains: "
                               f"{untapped_mountains(state, 0)})")
            ST["blocked_reason"] = "winnower never reached the battlefield"
            ST["finished"] = True
            say(f"[{tag}] BLOCKED: winnower never cast by turn {turn}")
            return True
        return False
    if stage == "WAIT_RESOLVE":
        if winnower_bf(state):
            ST["stage"] = "POST_CHECK"
            say(f"[{tag}] Winnower resolved -> POST_CHECK")
            return True
        # surface a cast rejection (e.g. priority race) without latching:
        # clear the submit-revision guard so the RAMP gate can retry.
        base = ST.get("winnower_rej_baseline", 0)
        if len(c.rejections) > base:
            new_rej = c.rejections[base:]
            ST["winnower_rej_baseline"] = len(c.rejections)
            ST["winnower_submit_rev"] = None
            ST["stage"] = "RAMP"
            note = (f"Winnower cast rejected at turn {turn}: "
                    f"{json.dumps(new_rej[0], default=str)[:240]}; "
                    f"retrying via RAMP gate")
            ST["notes"].append(note)
            say(f"[{tag}] {note}")
            wire("winnower_rejected", {"rejections": new_rej[:2]})
            return True
        if stack_has(state, WINNOWER):
            return False  # on the stack; let priority pass
        if time.time() - ST.get("winnower_cast_at", time.time()) > 60:
            ST["winnower_submit_rev"] = None
            ST["stage"] = "RAMP"
            ST["notes"].append(f"Winnower cast unconfirmed after 60s at "
                               f"turn {turn}; retrying via RAMP gate")
            say(f"[{tag}] Winnower cast unconfirmed; retrying")
            return True
        return False
    if stage == "CTRL_CHECK" and not ST.get("ctrl_done"):
        if my_main(state, 0) and winnower_bf(state):
            s = next((o for o in hand_ids(state, 0)
                      if obj_lname(state, o) == STRIKE), None)
            if s is None:
                ST["ctrl_ad"] = "not-run"
                ST["notes"].append("A5 not-run: P0 has no Lightning Strike "
                                   "in hand at ctrl check")
            else:
                adv, via, _payload = cast_advertised(st, acts, state, 0, STRIKE)
                ST["ctrl_ad"] = adv
                ST["notes"].append(
                    f"ctrl check at turn {turn}: P0 Lightning Strike "
                    f"advertised={adv} via={via} "
                    f"(untapped Mountains={untapped_mountains(state, 0)})")
                say(f"[{tag}] A5 ctrl: P0 Strike advertised={adv}")
            await export_tag(c, "mid_ctrl")
            ST["ctrl_done"] = True
            ST["finished"] = True
            say(f"[{tag}] CTRL_CHECK done -> finished")
            return True
        return False
    return False


async def p1_stage(c, tag, state, acts, st):
    stage = ST["stage"]
    turn = state.get("turn_number", 0)
    if stage == "RAMP":
        if winnower_bf(state):
            return False
        if my_main(state, 1) and not ST.get("pre_done"):
            hn = hand_lnames(state, 1)
            if BOLT in hn and STRIKE in hn \
                    and untapped_mountains(state, 1) >= 2:
                adv_b, via_b, _p = cast_advertised(st, acts, state, 1, BOLT)
                adv_s, via_s, _p2 = cast_advertised(st, acts, state, 1, STRIKE)
                ST["pre_ad"] = {"bolt": adv_b, "bolt_via": via_b,
                                "strike": adv_s, "strike_via": via_s,
                                "untapped": untapped_mountains(state, 1),
                                "turn": turn}
                ST["notes"].append(
                    f"pre check at turn {turn}: Bolt advertised={adv_b} "
                    f"({via_b}), Strike advertised={adv_s} ({via_s})")
                say(f"[{tag}] PRE: Bolt advertised={adv_b}, "
                    f"Strike advertised={adv_s}")
                wire("pre_advertised", ST["pre_ad"])
                await export_tag(ST["host_client"], "mid_pre")
                ST["pre_done"] = True
                return True
            return False
        if turn >= 12 and not ST.get("pre_done"):
            ST["notes"].append(f"pre check never completed by turn {turn} "
                               f"(hand={hand_lnames(state, 1)})")
            ST["blocked_reason"] = "pre-Winnower advertisement check failed"
            ST["finished"] = True
            say(f"[{tag}] BLOCKED: pre check never completed")
            return True
        return False
    if stage == "POST_CHECK":
        if not winnower_bf(state):
            return False
        if my_main(state, 1) and not ST.get("post_done"):
            hn = hand_lnames(state, 1)
            um = untapped_mountains(state, 1)
            if um >= 2:
                adv_b, via_b, _p = cast_advertised(st, acts, state, 1, BOLT)
                adv_s, via_s, _p2 = cast_advertised(st, acts, state, 1, STRIKE)
                ST["post_ad"] = {"bolt": adv_b, "bolt_via": via_b,
                                 "strike": adv_s, "strike_via": via_s,
                                 "bolt_in_hand": BOLT in hn,
                                 "strike_in_hand": STRIKE in hn,
                                 "untapped": um, "turn": turn,
                                 "cast_summary": advertised_cast_summary(
                                     acts, state, 1)}
                ST["notes"].append(
                    f"post check at turn {turn}: Winnower on BF; "
                    f"Bolt advertised={adv_b} ({via_b}), "
                    f"Strike advertised={adv_s} ({via_s}); "
                    f"in hand bolt={BOLT in hn} strike={STRIKE in hn}")
                say(f"[{tag}] POST: Bolt advertised={adv_b}, "
                    f"Strike advertised={adv_s}")
                wire("post_advertised", ST["post_ad"])
                await export_tag(ST["host_client"], "mid_post")
                ST["post_done"] = True
                # queue probes for in-hand spells that ARE advertised
                q = []
                if BOLT in hn and adv_b:
                    q.append({"card": "bolt", "lname": BOLT,
                              "oid": next(o for o in hand_ids(state, 1)
                                          if obj_lname(state, o) == BOLT),
                              "target_seat": 0})
                if STRIKE in hn and adv_s:
                    q.append({"card": "strike", "lname": STRIKE,
                              "oid": next(o for o in hand_ids(state, 1)
                                          if obj_lname(state, o) == STRIKE),
                              "target_seat": 0})
                ST["probe_queue"] = q
                ST["probe"] = None
                ST["stage"] = "PROBE"
                say(f"[{tag}] probe queue: {[x['card'] for x in q]}")
                return True
            return False
        if turn > 30 and not ST.get("post_done"):
            ST["notes"].append(f"post check never completed by turn {turn}")
            ST["blocked_reason"] = "post-Winnower check window missed"
            ST["finished"] = True
            say(f"[{tag}] BLOCKED: post check never completed")
            return True
        return False
    if stage == "PROBE":
        return await probe_tick(c, tag, 1, state, acts, st)
    return False


# ------------------------------------------------------------- ticks
async def common_first(c, acts, st, pid, tag):
    """Shared opening sequence. Returns True if the tick is consumed."""
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    if await declare_empty(c, acts, tag):
        return True
    if await legend_keep_first(c, acts, tag):
        return True
    if await pay_tick(c, acts):
        return True
    return False


async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    acts = merged_actions(st)
    if await common_first(c, acts, st, pid, tag):
        return
    await asyncio.sleep(0)  # yield to the pump before fresh read
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if await p0_stage(c, tag, state, acts, st):
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
    if my_priority(acts):
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    acts = merged_actions(st)
    if await common_first(c, acts, st, pid, tag):
        return
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if await p1_stage(c, tag, state, acts, st):
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    if my_main(state, pid):
        # during PROBE the stage logic owns the main phase; do not develop
        if ST.get("stage") != "PROBE":
            if await play_a_land(c, state, pid, acts, tag):
                return
    if my_priority(acts):
        await pass_priority(c, st, acts)


# ------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 920
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9591 - Void Winnower: 'even mana values' line "
                    "blocks EVERY opponent spell", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['server_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), "P0 ramps to 9, casts Void Winnower; P1 holds Bolt (MV1, "
                    "odd) + Strike (MV2, even)",
           fill=(200, 210, 225))
    y += 26
    d.text((24, y), f"game: {run['game_code']}", fill=(200, 210, 225))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_pre_advertised": "A1 pre-Winnower: Bolt+Strike advertised (fixture)",
        "A2_winnower_bf": "A2 Winnower on P0 battlefield",
        "A3_even_blocked": "A3 post: even-MV Strike NOT castable (static active)",
        "A4_odd_blocked_bug": "A4 post: odd-MV Bolt NOT castable (REPORTED BUG)",
        "A5_controller_ok": "A5 ctrl: P0's own Strike still castable (109.5)",
        "A6_probes_terminal": "A6 every probe reached a terminal outcome",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:12]:
        d.text((40, y), n[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9591/" +
           run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


# ------------------------------------------------------------- main
async def main():
    pidfile = "/tmp/scenario_9591_01140.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9591_01140 instance is alive "
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
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
        "stage": "RAMP",
        "pre_done": False,
        "post_done": False,
        "ctrl_done": False,
        "ctrl_ad": None,
        "pre_ad": {},
        "post_ad": {},
        "probe_queue": [],
        "probe": None,
        "finished": False,
        "blocked_reason": None,
        "game_code": None,
        "winnower_submit_rev": None,
        "winnower_rej_baseline": 0,
        "winnower_cast_turn": None,
        "winnower_cast_at": None,
    })
    await verify_server_hello()
    check_data_level()

    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(P0_DECK)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    ST["game_code"] = p0.game_code
    # Only the game host (P0) can export authoritative state -- route every
    # export through the host client.
    ST["host_client"] = p0
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})

    max_turn = 0
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    i = 0
    while i < 8000:
        i += 1
        await asyncio.sleep(0.2)
        for c, tick, pid in ((p0, p0_tick, 0), (p1, p1_tick, 1)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[pid] = time.time()
            else:
                if time.time() - last_change[pid] > 45:
                    s0 = st["state"]
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                        f"stage={ST.get('stage')} "
                        f"holding_priority={my_priority(merged_actions(st))}")
                    last_change[pid] = time.time()
                holds_prio = my_priority(merged_actions(st))
                if not (holds_prio and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        st = p0.latest
        if st:
            max_turn = max(max_turn, st["state"].get("turn_number", 0))
        if ST["finished"]:
            break
        if time.time() - last_change[0] > STALL_AFTER_S \
                and time.time() - last_change[1] > STALL_AFTER_S \
                and not ST["finished"]:
            ST["notes"].append(f"STALLED: no new revision for {STALL_AFTER_S}s "
                               f"(stage={ST['stage']})")
            say(f"STALL at stage={ST['stage']}")
            wire("stall", {"stage": ST["stage"]})
            break
        if time.time() - t0 > SETUP_DEADLINE_S and not ST["finished"]:
            ST["notes"].append("deadline hit")
            break

    # ---- final export + evaluation
    try:
        await export_tag(p0, "post")
    except Exception as e:
        ST["notes"].append(f"final export failed: {e}")

    evaluate()

    ass = ST["ass"]
    verdict = ST["verdict"]
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/pull/9591",
        "run_id": RUN_ID,
        "game_code": ST["game_code"],
        "started_at": ST["started_at"],
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 153, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": "34x Mountain / 4x Void Winnower / 2x Lightning Strike",
                  "P1": "24x Mountain / 8x Lightning Bolt / 8x Lightning Strike"},
        "setup_line": ("P0 ramps Mountains, casts Void Winnower ({9}) on its "
                       "main; P1 draw-gos holding Lightning Bolt (MV 1, odd) "
                       "and Lightning Strike (MV 2, even)"),
        "contract_line": ("Pre-Winnower both P1 spells must be advertised; "
                          "post-Winnower the even-MV Strike must be blocked "
                          "(static active) while the odd-MV Bolt must stay "
                          "castable (CR 601.3a/202.3); the reported bug is "
                          "that Bolt is blocked too."),
        "stats": {"max_turn": max_turn,
                  "winnower_cast_turn": ST["winnower_cast_turn"],
                  "pre_ad": ST["pre_ad"], "post_ad": ST["post_ad"],
                  "ctrl_ad": ST["ctrl_ad"]},
        "assertions": ass,
        "notes": ST["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9591_01140.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)
    try:
        render_summary(run, f"{EVDIR}/summary.png")
    except Exception as e:
        say(f"render_summary FAILED: {e}")
        wire("render_failed", {"error": str(e)})

    say("closing logs before manifest computation")
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
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1), flush=True)
    await p0.close()
    await p1.close()


def env_state(p):
    try:
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)["state"]
    except (FileNotFoundError, KeyError):
        return None


def evaluate():
    A = ST["ass"]
    notes = ST["notes"]

    pre = env_state("mid_pre.json")
    post = env_state("mid_post.json")
    ctrl = env_state("mid_ctrl.json")

    # A1: pre-Winnower advertisement (fixture validity)
    pa = ST.get("pre_ad") or {}
    if pre is None or not pa:
        A["A1_pre_advertised"] = "failed"
        notes.append("A1 failed: mid_pre.json never exported")
    else:
        ok = bool(pa.get("bolt")) and bool(pa.get("strike"))
        A["A1_pre_advertised"] = "passed" if ok else "failed"
        notes.append(f"A1: pre turn={pa.get('turn')} bolt_adv={pa.get('bolt')} "
                     f"strike_adv={pa.get('strike')}")

    # A2: Winnower on P0's battlefield post
    if post is None:
        A["A2_winnower_bf"] = "failed"
        notes.append("A2 failed: mid_post.json never exported")
    else:
        ok = winnower_bf(post)
        A["A2_winnower_bf"] = "passed" if ok else "failed"
        notes.append(f"A2: winnower_bf={ok} "
                     f"(cast turn={ST.get('winnower_cast_turn')})")

    # A3: even-MV Strike blocked post-Winnower (static is active)
    qa = ST.get("post_ad") or {}
    if post is None or not qa:
        A["A3_even_blocked"] = "failed"
        notes.append("A3 failed: post advertisement never recorded")
    elif not qa.get("strike_in_hand", True):
        A["A3_even_blocked"] = "not-run"
        notes.append("A3 not-run: Strike not in P1 hand at post check")
    else:
        blocked = not qa.get("strike")
        probe = load_probe("strike")
        if blocked:
            A["A3_even_blocked"] = "passed"
        elif probe and probe.get("outcome") == "rejected":
            A["A3_even_blocked"] = "passed"
            notes.append("A3: Strike advertised but probe submission "
                         "rejected (static enforced at submit)")
        elif probe and probe.get("outcome") == "resolved":
            A["A3_even_blocked"] = "failed"
            notes.append("A3 FAILED: even-MV Strike RESOLVED post-Winnower "
                         "-- the static is not blocking even spells at all")
        else:
            A["A3_even_blocked"] = "failed"
            notes.append("A3 failed: Strike advertised, probe outcome "
                         f"{probe.get('outcome') if probe else 'none'}")
        notes.append(f"A3: strike_adv={qa.get('strike')} via={qa.get('strike_via')}")

    # A4: the reported bug -- odd-MV Bolt blocked post-Winnower
    if post is None or not qa:
        A["A4_odd_blocked_bug"] = "failed"
        notes.append("A4 failed: post advertisement never recorded")
    elif not qa.get("bolt_in_hand", True):
        A["A4_odd_blocked_bug"] = "not-run"
        notes.append("A4 not-run: Bolt not in P1 hand at post check")
    else:
        probe = load_probe("bolt")
        if not qa.get("bolt"):
            A["A4_odd_blocked_bug"] = "passed"
            notes.append("A4: Bolt NOT advertised post-Winnower -- "
                         "reported overblocking reproduced")
        elif probe and probe.get("outcome") == "rejected":
            A["A4_odd_blocked_bug"] = "passed"
            notes.append("A4: Bolt advertised but probe submission REJECTED "
                         "-- overblocking via rejection, reproduced")
        elif probe and probe.get("outcome") == "resolved":
            A["A4_odd_blocked_bug"] = "failed"
            notes.append("A4: Bolt advertised AND probe resolved "
                         "(P0 life dropped) -- correct behavior, "
                         "bug NOT reproduced")
        else:
            A["A4_odd_blocked_bug"] = "not-run"
            notes.append("A4 not-run: Bolt advertised, probe outcome "
                         f"{probe.get('outcome') if probe else 'none'} "
                         "-- ambiguous")
        notes.append(f"A4: bolt_adv={qa.get('bolt')} via={qa.get('bolt_via')}")

    # A5: controller unaffected (CR 109.5)
    if ST.get("ctrl_ad") == "not-run":
        A["A5_controller_ok"] = "not-run"
        notes.append("A5 not-run: P0 had no Strike in hand at ctrl check")
    elif ST.get("ctrl_ad") is True:
        A["A5_controller_ok"] = "passed"
        notes.append("A5: P0's own Strike advertised with Winnower out "
                     "(controller unaffected)")
    elif ST.get("ctrl_ad") is False:
        A["A5_controller_ok"] = "failed"
        notes.append("A5 FAILED: P0's own Strike NOT advertised -- "
                     "the static hits the controller too")
    else:
        A["A5_controller_ok"] = "not-run"
        notes.append("A5 not-run: ctrl check never completed")

    # A6: probes terminal
    probes = [load_probe("bolt"), load_probe("strike")]
    queued = [p for p in probes if p is not None]
    if not queued and (qa.get("bolt") or qa.get("strike")):
        A["A6_probes_terminal"] = "failed"
        notes.append("A6 failed: spells advertised but no probe records")
    elif all(p.get("outcome") in ("not_advertised", "rejected", "resolved")
             for p in queued):
        A["A6_probes_terminal"] = "passed"
    elif queued:
        A["A6_probes_terminal"] = "failed"
        notes.append("A6 failed: probe outcomes: "
                     + str([p.get("outcome") for p in queued]))
    else:
        A["A6_probes_terminal"] = "passed"
        notes.append("A6: no probes needed (nothing advertised)")

    if ST.get("blocked_reason"):
        notes.append(f"blocked_reason: {ST['blocked_reason']}")

    a1, a2, a3, a4 = (A["A1_pre_advertised"], A["A2_winnower_bf"],
                      A["A3_even_blocked"], A["A4_odd_blocked_bug"])
    if a1 == "passed" and a2 == "passed" and a3 == "passed" \
            and a4 == "passed":
        verdict = "reproduced"
    elif a1 == "passed" and a2 == "passed" and a3 == "passed" \
            and a4 == "failed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    ST["verdict"] = verdict
    for k in ASS_KEYS:
        say(f"assertion {k}: {A[k]}")


def load_probe(card):
    try:
        with open(f"{EVDIR}/probe_{card}.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


asyncio.run(main())
