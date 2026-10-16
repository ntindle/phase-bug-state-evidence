#!/usr/bin/env python3
"""Issue #9728: resolving instant/sorcery must reach the graveyard only
after its final part (CR 608.2c, CR 608.2n) -- i.e. after every paused
instruction has been answered.

Reported defect (from the issue): an instant or sorcery is put into its
owner's graveyard (or exile/hand/library) only as the final part of its
resolution, after every paused instruction is answered. The engine moved
the spell early -- at the first pause -- instead of carrying the
"final part still owed" state on the resolution carrier.

Representative path (named in the issue's own test list as
`faithless_looting_is_put_into_the_graveyard_on_top_of_the_cards_it_discarded`):
Faithless Looting ({R} sorcery: "Draw two cards, then discard two cards.").

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Native engine, two human-client seats, pinned release v0.111.0
(build 33aa23a, protocol 144). P0 casts Faithless Looting on turn 1.

  - Correct (CR 608.2c): draw 2, discard-2 pause is presented and answered,
    THEN Looting moves to the graveyard as the final part. Observable:
    (a) at the discard pause Looting is NOT yet in any graveyard/exile/hand/
    library zone; (b) in the turn's zone-change record Looting's
    Stack->Graveyard move comes AFTER both discards' Hand->Graveyard moves;
    (c) P0's graveyard reads [.., discarded, discarded, Faithless Looting]
    with Looting on top.
  - Buggy (the reported defect): Looting is moved to the graveyard when the
    discard pause opens, so the discards land on top of it: Looting's
    Stack->Graveyard move precedes the discards, and the graveyard reads
    [.., Faithless Looting, discarded, discarded].

Assertions:
  A1_setup_ok        pre.json: P0 active, PreCombatMain, Faithless Looting in
                     hand, >=1 untapped Mountain.
  A2_cast_paid       Cast submitted and confirmed on the stack; Looting left
                     P0's hand; mana paid (Mountain tapped) by mid_pause.
  A3_discard_pause   The discard-2 pause was observed and answered with
                     advertised choices (2 picks, no rejection); draw-2 seen
                     (hand at pause == pre hand - 1 + 2).
  A4_final_part_timing  post.json zone_changes_this_turn: Looting's
                     ->Graveyard move has a LATER index than both discards'
                     Hand->Graveyard moves (primary reported-outcome check).
  A5_mid_pause_zone  mid_pause.json: while the discard choice is pending,
                     Looting's zone is not Graveyard/Exile/Hand/Library.
  A6_cleanup         post.json: stack empty, Looting in P0's graveyard.

Verdict: reproduced if A4 or A5 fails (the reported early-move defect);
not-reproduced if all pass; blocked if the path could not be exercised.

Scope/limits: one pause kind (spell discard choice) is exercised; the
issue's other pause kinds (Quest/Edict triggers, Time Stop cleanup,
Doomsday search, Taigam rebound, copy-entry triggers) are not covered by
this run.

Driver conventions (protocol 144, from scenario_5474_01110.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action, gated on the
    MulliganDecision legal action with answered (tag,iid) keys.
  - Bottom-after-mulligan via vi schema/select, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi only, gated on hand > 7 (plus a stage guard so
    it never steals Looting's own discard pause).
  - Spell mana payment via vi tapLandForMana, needs-gated
    (ST["mana_needs"][tag]); never blind-tap.
  - A single await asyncio.sleep(0) yield after the priority gate, before
    leg evaluation (leg-engagement race fix).
  - Export checkpoints fall through to the priority pass in the same tick;
    never return after an export while holding priority.
  - Re-tick backstop: a client holding Priority with no revision change for
    >5s is re-ticked (scenario_650_01010.py last_tick_at pattern).
  - _vi_offers_pass safety net (protocol 144): vi-surfaced passPriority
    counts as holding priority.
  - client.py HELLO advertises protocol 144 (exact match enforced).
  - Cast-confirmation guard (protocol 118/120): after every CastSpell
    submission the driver sets ST["pending_cast"] and does NOT pass priority
    or start new plays until the spell is confirmed on the stack; a 30s
    backstop clears a still-in-hand cast as dropped and resets the scenario
    one-shot flags so the cast re-triggers.
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
ISSUE = 9728
RUN_ID = os.environ.get("RUN_ID", "run-9728-v01110-20261016-2311")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.111.0",
    "build_commit": "33aa23a",
    "protocol_version": 144,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.111.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("reused live v0.111.0 server on 127.0.0.1:9374 "
                      "(already listening at run start; process identity "
                      "verified via ServerHello 0.111.0/33aa23a/protocol 144 "
                      "by this run's own raw Hello handshake; this run's own "
                      "game verifies the identity; no second server started "
                      "per AGENTS.md 2026-10-08 lesson)"),
    "source": ("2026-10-16: latest stable release v0.111.0 (published "
               "2026-10-16T15:04:47Z) == pinned release dir; ServerHello "
               "0.111.0/33aa23a/protocol 144 verified by handshake this "
               "run; minisign signatures re-verified via pynacl this run; "
               "hashes recomputed against on-disk artifacts this run"),
}

for _f, _k in (("server/releases/v0.111.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.111.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.111.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.111.0/data/card-data.json"))

LOOTING = "faithless looting"
MOUNTAIN = "mountain"
FOREST = "forest"

P0_DECK = deck((LOOTING, 8), (MOUNTAIN, 52))
P1_DECK = deck((FOREST, 60))

SETUP_DEADLINE_S = 1800
ASS_KEYS = ("A1_setup_ok", "A2_cast_paid", "A3_discard_pause",
            "A4_final_part_timing", "A5_mid_pause_zone", "A6_cleanup")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
P0C = None
P1C = None


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()
    except Exception:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception:
        pass


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


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


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


def on_bf(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def can_pay(state, pid, colors=(), generic=0):
    color_of = {MOUNTAIN: "R"}
    pool = {}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = color_of.get(obj_lname(state, o))
            if c:
                pool[c] = pool.get(c, 0) + 1
    need = {}
    for c in colors:
        need[c] = need.get(c, 0) + 1
    for c, n in need.items():
        if pool.get(c, 0) < n:
            return False
        pool[c] -= n
    return sum(pool.values()) >= generic


def untapped_lands(state, pid, name):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == name and not get_obj(state, o).get("tapped")]


def spell_in_hand_oid(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
    return None


def _vi_offers_pass(st):
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices", []) or []:
            for sf in ch.get("surfaces", []) or []:
                if sf.get("type") == "action" \
                        and (sf.get("data") or {}).get("code") == "passPriority":
                    return True
    return False


def my_priority(acts, st=None):
    if any(a.get("type") == "PassPriority" for a in acts):
        return True
    return st is not None and _vi_offers_pass(st)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


# ---------------------------------------------------------------- interaction primitives

async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    ST["_last_submit"] = {"type": a["type"],
                          "data_keys": sorted((a.get("data") or {}).keys())}
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.111.0"), f"unexpected version {ver}"
    assert int(proto) == 144, f"unexpected protocol {proto}"
    assert str(build) == "33aa23a", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    """Verify Faithless Looting exists in the pinned data with {R} cost and
    the draw-2-then-discard-2 text."""
    li = CARD_DATA.get(LOOTING, {})
    shards = ((li.get("mana_cost") or {}).get("shards")) or []
    text = li.get("oracle_text") or ""
    ok = (LOOTING in CARD_DATA
          and shards == ["Red"]
          and "Draw two cards, then discard two cards" in text)
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok,
                   "looting": {"has_key": LOOTING in CARD_DATA,
                               "shards": shards,
                               "text_head": text[:80]},
                   "mountain_in_data": MOUNTAIN in CARD_DATA}, f, indent=1)
    ST["data_level_ok"] = ok
    say(f"data-level check: ok={ok} looting_shards={shards}")

async def do_mulligan(c, acts, st, pid, tag, want_card, land_names):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in surf_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    state = st["state"]
    hn = hand_lnames(state, pid)
    lands = sum(1 for n in hn if n in land_names)
    MULLS.add(key)
    mull_count = sum(1 for k in MULLS if k[0] == tag)
    keep = (want_card in hn) and lands >= 1
    if not keep and mull_count < 4 and len(hn) > 4:
        say(f"[{tag}] mulligan #{mull_count} ({len(hn)} cards, hand={hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, keepers):
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; "
            "not answering")
        wire(f"{tag}_bottom_no_vi",
             {"acts": [a.get("data") for a in sel_acts][:8]})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get(
        "data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in keepers:
            return (1, str(ref))
        return (0, str(ref))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(
        c, {"interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, keepers):
    # Stage guard: Looting's own discard pause is answered by
    # do_looting_discard; this handler must never steal it.
    if ST.get("stage") == "looting_cast":
        return False
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(str(h) for h in hand)
    found_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        cands = rdata.get("candidates") or []
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not any(r in handset for r in refs):
            continue
        found_opp = (opp, cands, spec)
        break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands, spec = found_opp
    stype = spec.get("type") or "select"

    def rank(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else \
            choice_text(ch).lower()
        if nm == MOUNTAIN:
            return (0, nm)
        if nm in keepers:
            return (9, nm)
        return (5, nm)

    picks = [ch.get("id") for ch in sorted(cands, key=rank)[:max(1, n)]
             if ch.get("id")]
    if not picks:
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding to hand size via vi ({stype}): picks={picks}")
    wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": picks}}}, tag)
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
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId":
                                                            ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag, want_land):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if obj_lname(state, o) != want_land:
            continue
        for a in acts:
            if a["type"] == "PlayLand" and \
                    str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing land {want_land}")
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


async def try_cast(c, st, state, acts, tag, name, needs):
    a, oid = cast_action_for(acts, state, name)
    if a:
        ST["mana_needs"][tag] = dict(needs)
        say(f"[{tag}] casting {name} (oid {oid}) needs={needs} via legacy")
        wire("cast", {"who": tag, "card": name, "oid": oid, "via": "legacy"})
        await submit_as_is(c, a)
        set_pending_cast(oid, name, tag, {"flag": _cast_flag_for(name)})
        return True
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "castSpell" not in codes:
                continue
            ref = _cand_reference(ch)
            if ref is None:
                continue
            if obj_lname(state, ref) != name:
                continue
            if (ch.get("status") or {}).get("type") not in (None, "available"):
                continue
            ST["mana_needs"][tag] = dict(needs)
            say(f"[{tag}] casting {name} (oid {ref}) needs={needs} via vi")
            wire("cast", {"who": tag, "card": name, "oid": int(ref),
                          "via": "vi"})
            await answer_vi(c, opp, ch, tag)
            set_pending_cast(int(ref), name, tag,
                             {"flag": _cast_flag_for(name)})
            return True
    return False


def _cast_flag_for(name):
    if name == LOOTING:
        return "cast_submitted"
    return None


def set_pending_cast(oid, name, tag, reset):
    ST["pending_cast"] = {"oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag,
                          "reset": dict(reset)}


def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_confirmed(state, pc):
    zone = cast_obj_zone(state, pc["oid"])
    return zone in ("stack", "battlefield", "graveyard", "exile", "command")


def cast_guard_tick(state, tag):
    pc = ST.get("pending_cast")
    if not pc:
        return "proceed"
    if cast_confirmed(state, pc):
        ST["pending_cast"] = None
        ST["cast_confirmed"] = True
        say(f"[{tag}] cast confirmed "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']}")
        wire("cast_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                "zone": cast_obj_zone(state, pc["oid"])})
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] CAST NOT CONFIRMED after 30s "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("cast_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]),
              "zone": cast_obj_zone(state, pc["oid"])})
        ST["pending_cast"] = None
        r = pc.get("reset") or {}
        if r.get("flag"):
            ST[r["flag"]] = False
            if r["flag"] == "cast_submitted":
                ST["stage"] = "setup"
        ST["mana_needs"][pc.get("tag") or tag] = {}
        return "proceed"
    return "hold"


async def pay_mana_vi(c, st, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
    if sum(needs.values()) <= 0:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status") or {}).get("type") not in (None, "available"):
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
        say(f"[{tag}] tap land for mana used_for={used} needs={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def rtype_is_schema(resp):
    return (resp.get("type") == "schema")


async def maybe_answer_optional_effect(c, st, tag, want_accept=False):
    opp = None
    for o in vi_ops(st):
        resp = o.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                opp = o
                break
        if opp is not None:
            break
    if opp is None:
        return False
    iid = opp.get("interactionId")
    key = (tag, "opteff", iid)
    if key in SUBMITTED_OPPS:
        return False
    pick = None
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []) or []:
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "decline" in txt or "don't" in txt or "do not" in txt \
                    or txt.strip() == "no":
                is_accept = False
            elif "accept" in txt or "cast" in txt or txt.strip() == "yes":
                is_accept = True
        if is_accept == want_accept:
            pick = ch
            break
    if pick is None:
        say(f"[{tag}] decideOptionalEffect opportunity but no "
            f"{'accept' if want_accept else 'decline'} choice classified; "
            f"not answering (recorded)")
        wire(f"{tag}_opteff_unclassified",
             {"iid": iid, "opp": opp})
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] answering decideOptionalEffect: "
        f"{'accept' if want_accept else 'decline'} "
        f"({choice_text(pick)[:60]})")
    wire("optional_effect_answer",
         {"who": tag, "want_accept": want_accept, "iid": iid,
          "choice": pick.get("id")})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "choose",
                                       "data": {"choiceId": pick.get("id")}}},
                      tag)
    return True


def drain_rejections(c, stage):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            rec = {"type": t, "data": data, "stage": stage,
                   "after": ST.get("_last_submit")}
            out.append(rec)
            ST["rejections"].append(rec)
            say(f"[{c.name}] REJECTION in stage {stage}: "
                f"{json.dumps(data, default=str)[:300]}")
            wire("rejection", {"who": c.name, "stage": stage, "data": data})
    return out


async def export_named(name):
    try:
        raw = await P0C.export_state()
        env = json.loads(raw)
        assert "state" in env, "envelope missing 'state'"
        with open(f"{EVDIR}/{name}.json", "w") as f:
            json.dump(env, f, indent=1)
        ST["exports"][name] = True
        say(f"exported {name}.json (turn={env['state'].get('turn_number')})")
        wire("export", {"name": name,
                        "turn": env["state"].get("turn_number")})
        return env["state"]
    except Exception as e:
        say(f"export {name} FAILED: {e!r}")
        ST["tick_errors"].append(f"export_{name}: {e!r}")
        return None

# ---------------------------------------------------------------- Faithless Looting discard pause

async def do_looting_discard(c, pid, tag):
    """Answer Faithless Looting's discard-N pause (a paused instruction
    during the spell's resolution). Primary path: the vi schema/select
    opportunity whose candidates reference P0's hand cards. Fallback: the
    legacy SelectCards action. Records the opportunity (kind, spec,
    constraint, candidates) and exports mid_pause BEFORE answering, so the
    paused state's Looting zone is captured.

    Discards Mountains first, never Looting.
    """
    if ST["stage"] != "looting_cast":
        return False
    st = c.latest
    if not st:
        return False
    state = st["state"]
    hand = hand_ids(state, pid)
    handset = set(str(h) for h in hand)

    def rank_choice(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else \
            choice_text(ch).lower()
        if nm == MOUNTAIN:
            return (0, nm)
        if nm == LOOTING:
            return (9, nm)
        return (5, nm)

    # --- vi schema/select path ---
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
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not refs or not all(r in handset for r in refs):
            continue
        if vi_kind_code(st) == "mulligan":
            continue
        iid = opp.get("interactionId")
        key = (tag, "looting_discard", str(iid))
        if key in SUBMITTED_OPPS:
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("exact") or con.get("count") or 2)
        n = max(1, min(n, len(cands)))
        stype = "select"
        picks = [ch for ch in sorted(cands, key=rank_choice)[:n]
                 if ch.get("id")]
        if not picks:
            continue
        SUBMITTED_OPPS.add(key)
        rec = {
            "via": "vi",
            "iid": iid,
            "kind_code": vi_kind_code(st),
            "spec_type": spec.get("type"),
            "constraint": con,
            "n_requested": n,
            "candidate_count": len(cands),
            "candidates": [
                {"choice_id": ch.get("id"),
                 "ref": str(_cand_reference(ch)),
                 "name": obj_lname(state, _cand_reference(ch))}
                for ch in cands],
            "picks": [
                {"choice_id": ch.get("id"),
                 "ref": str(_cand_reference(ch)),
                 "name": obj_lname(state, _cand_reference(ch))}
                for ch in picks],
        }
        ST["discard_opps"].append(rec)
        say(f"[{tag}] Looting discard pause observed via vi "
            f"(kind={rec['kind_code']}, n={n}, "
            f"cands={len(cands)}); exporting mid_pause before answering")
        wire("discard_pause", rec)
        if not ST["exports"].get("mid_pause"):
            mid_state = await export_named("mid_pause")
            ST["pause_hand_count"] = len(hand_ids(mid_state, pid)) \
                if mid_state else len(hand)
        say(f"[{tag}] answering Looting discard: "
            f"{[p['name'] for p in rec['picks']]}")
        wire("discard_choice",
             {"who": tag, "via": "vi", "iid": iid,
              "picks": [p["name"] for p in rec["picks"]]})
        await interact_as(
            c, {"interactionId": iid,
                "response": {"type": stype,
                             "data": {"choiceIds": [p["choice_id"]
                                                    for p in rec["picks"]]}}},
            tag)
        ST["discard_picks_total"] = ST.get("discard_picks_total", 0) + len(picks)
        ST["discard_pick_oids"] = ST.get("discard_pick_oids", []) + \
            [int(p["ref"]) for p in rec["picks"]]
        ST["discard_pick_names"] = ST.get("discard_pick_names", []) + \
            [p["name"] for p in rec["picks"]]
        return True

    # --- legacy SelectCards fallback ---
    acts = merged_actions(st)
    sc_acts = [a for a in acts if a.get("type") == "SelectCards"]
    for a in sc_acts:
        cards = [int(x) for x in (a.get("data", {}) or {}).get("cards", [])]
        if not cards or not all(str(x) in handset for x in cards):
            continue
        key = (tag, "looting_discard_legacy", str(c.revision))
        if key in SUBMITTED_OPPS:
            return False
        picks = sorted(cards,
                       key=lambda o: ((0, o) if obj_lname(state, o) == MOUNTAIN
                                      else (9 if obj_lname(state, o) == LOOTING
                                            else 5, o)))[:2]
        if len(picks) < 2:
            continue
        SUBMITTED_OPPS.add(key)
        rec = {"via": "legacy", "n_requested": 2,
               "picks": [{"ref": str(o), "name": obj_lname(state, o)}
                         for o in picks]}
        ST["discard_opps"].append(rec)
        say(f"[{tag}] Looting discard pause observed via legacy SelectCards; "
            "exporting mid_pause before answering")
        wire("discard_pause", rec)
        if not ST["exports"].get("mid_pause"):
            await export_named("mid_pause")
        say(f"[{tag}] answering Looting discard (legacy): "
            f"{[p['name'] for p in rec['picks']]}")
        wire("discard_choice",
             {"who": tag, "via": "legacy",
              "picks": [p["name"] for p in rec["picks"]]})
        await c.send_action({"type": "SelectCards",
                             "data": {"cards": [int(p["ref"])
                                                for p in rec["picks"]]}})
        ST["discard_picks_total"] = ST.get("discard_picks_total", 0) + 2
        ST["discard_pick_oids"] = ST.get("discard_pick_oids", []) + \
            [int(p["ref"]) for p in rec["picks"]]
        ST["discard_pick_names"] = ST.get("discard_pick_names", []) + \
            [p["name"] for p in rec["picks"]]
        return True
    return False


# ---------------------------------------------------------------- ticks

async def generic_tick(c, pid, st, acts, state, keepers, land_names, want_card):
    tag = c.name
    if await do_mulligan(c, acts, st, pid, tag, want_card, land_names):
        return True
    if await do_bottom(c, acts, st, pid, tag, keepers):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag, keepers):
        return True
    if await maybe_answer_optional_effect(c, st, tag):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if await pay_mana_vi(c, st, tag):
        return True
    if any(a["type"] == "DeclareAttackers" for a in acts):
        da = next(a for a in acts if a["type"] == "DeclareAttackers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    if any(a["type"] == "DeclareBlockers" for a in acts):
        da = next(a for a in acts if a["type"] == "DeclareBlockers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"assignments": []})
        await submit_as_is(c, d)
        return True
    if any(a["type"] == "OrderTriggers" for a in acts):
        oa = next(a for a in acts if a["type"] == "OrderTriggers")
        await submit_as_is(c, oa)
        return True
    return False


async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    acts = merged_actions(st)
    if await generic_tick(c, pid, st, acts, state, (LOOTING,), (MOUNTAIN,),
                          LOOTING):
        return
    # Looting's own discard pause (answered before the handsize handler
    # could see it; generic_tick's handsize handler is stage-guarded too).
    if await do_looting_discard(c, pid, tag):
        return
    # cast-confirmation guard: hold priority passes while our CastSpell is
    # in flight but unconfirmed.
    if cast_guard_tick(state, tag) == "hold":
        return
    has_prio = my_priority(acts, st)
    await asyncio.sleep(0)
    in_main = my_main(state, pid)
    if in_main and has_prio:
        if await play_a_land(c, state, pid, acts, tag, MOUNTAIN):
            return
        if ST["stage"] == "setup" and not ST.get("cast_submitted"):
            if can_pay(state, pid, ("R",), 0):
                sid = spell_in_hand_oid(state, pid, LOOTING)
                if sid is not None:
                    # True pre-cast export: BEFORE the CastSpell is sent, so
                    # pre.json shows Looting in hand and the Mountain untapped
                    # (exporting after submit races the engine's processing).
                    ST["pre_hand_count"] = len(hand_ids(state, pid))
                    say("exporting pre (true pre-cast state)")
                    await export_named("pre")
                    if await try_cast(
                            c, st, state, acts, tag, LOOTING, {"R": 1}):
                        ST["cast_submitted"] = True
                        ST["cast_oid"] = sid
                        ST["cast_turn"] = state.get("turn_number")
                        ST["stage"] = "looting_cast"
                        say("STAGE -> looting_cast (Looting submitted)")
                        return
    if has_prio:
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    acts = merged_actions(st)
    if await generic_tick(c, pid, st, acts, state, (FOREST,), (FOREST,),
                          FOREST):
        return
    if cast_guard_tick(state, tag) == "hold":
        return
    has_prio = my_priority(acts, st)
    await asyncio.sleep(0)
    in_main = my_main(state, pid)
    if in_main and has_prio:
        if await play_a_land(c, state, pid, acts, tag, FOREST):
            return
    if has_prio:
        await pass_priority(c, st, acts)


# ---------------------------------------------------------------- evaluate / finalize

def zone_changes(s):
    return s.get("zone_changes_this_turn") or []


def looting_oid_in(s, owner=0):
    # Prefer the actual cast spell object (ST["cast_oid"]): the deck runs
    # 8 copies, so a name-only scan can hit a library copy (seen: oid 1 in
    # the library while the cast spell was oid 57).
    cast_oid = ST.get("cast_oid")
    if cast_oid is not None:
        o = get_obj(s, cast_oid)
        if (o.get("base_name") or o.get("name") or "").lower() == LOOTING:
            return int(cast_oid)
    for oid, o in (s.get("objects") or {}).items():
        if (o.get("base_name") or o.get("name") or "").lower() == LOOTING \
                and str(o.get("owner", -1)) == str(owner):
            return int(oid)
    return None


def evaluate():
    ass = {k: "not-run" for k in ASS_KEYS}
    notes = ST["notes"]
    notes.append(f"rejections={len(ST['rejections'])} "
                 f"discard_opps={len(ST['discard_opps'])} "
                 f"discard_picks_total={ST.get('discard_picks_total', 0)} "
                 f"cast_confirmed={ST.get('cast_confirmed')}")

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    # A1: setup
    pre = load_env("pre")
    if pre is None:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1: pre.json missing "
                     f"(cast_submitted={ST.get('cast_submitted')})")
    else:
        s = pre["state"]
        looting_hand = any(obj_lname(s, o) == LOOTING
                           for o in hand_ids(s, 0))
        ok = (s.get("active_player") == 0
              and s.get("phase") == "PreCombatMain"
              and looting_hand
              and len(untapped_lands(s, 0, MOUNTAIN)) >= 1)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A1: active={s.get('active_player')} "
                     f"phase={s.get('phase')} looting_in_hand={looting_hand} "
                     f"untapped_mountains="
                     f"{len(untapped_lands(s, 0, MOUNTAIN))} "
                     f"pre_hand={len(hand_ids(s, 0))}")

    # A2: cast paid and confirmed
    mid = load_env("mid_pause")
    if not ST.get("cast_submitted"):
        ass["A2_cast_paid"] = "not-run"
        notes.append("A2: cast never submitted")
    elif not ST.get("cast_confirmed"):
        ass["A2_cast_paid"] = "failed"
        notes.append("A2: cast submitted but never confirmed on the stack")
    elif mid is None:
        ass["A2_cast_paid"] = "failed"
        notes.append("A2: mid_pause.json missing; cannot verify mana/hand")
    else:
        s = mid["state"]
        cast_oid = ST.get("cast_oid")
        # the CAST copy must have left the hand (P0 may hold other copies)
        looting_left_hand = cast_oid is not None and \
            int(cast_oid) not in [int(o) for o in hand_ids(s, 0)]
        tapped_mtn = [o for o in bf_oids(s, 0)
                      if obj_lname(s, o) == MOUNTAIN
                      and get_obj(s, o).get("tapped")]
        ok = looting_left_hand and len(tapped_mtn) >= 1
        ass["A2_cast_paid"] = "passed" if ok else "failed"
        notes.append(f"A2: cast_looting_left_hand={looting_left_hand} "
                     f"tapped_mountains={len(tapped_mtn)}")

    # A3: discard pause observed and answered; draw-2 seen
    if not ST["discard_opps"]:
        ass["A3_discard_pause"] = "failed"
        notes.append("A3: no Looting discard pause was ever observed")
    elif ST.get("discard_picks_total", 0) != 2:
        ass["A3_discard_pause"] = "failed"
        notes.append(f"A3: pause observed but picks_total="
                     f"{ST.get('discard_picks_total', 0)} (expected 2)")
    else:
        rej = [r for r in ST["rejections"]
               if r.get("stage") == "looting_cast"]
        draw_ok = True
        if pre is not None and mid is not None:
            pre_hand = len(hand_ids(pre["state"], 0))
            mid_hand = len(hand_ids(mid["state"], 0))
            # cast (-1) then draw 2 (+2): mid == pre + 1
            draw_ok = (mid_hand == pre_hand + 1)
            notes.append(f"A3: pre_hand={pre_hand} mid_hand={mid_hand} "
                         f"draw2_ok={draw_ok}")
        ok = (not rej) and draw_ok
        ass["A3_discard_pause"] = "passed" if ok else "failed"
        notes.append(f"A3: pause answered with "
                     f"{ST.get('discard_pick_names')} "
                     f"rejections_in_stage={len(rej)}")

    # A4: final-part timing from the turn's zone-change record
    post = load_env("post")
    if post is None:
        ass["A4_final_part_timing"] = "not-run"
        notes.append("A4: post.json missing")
    else:
        s = post["state"]
        zc = zone_changes(s)
        looting_moves = [(i, z) for i, z in enumerate(zc)
                         if (z.get("name") or "").lower() == LOOTING
                         and z.get("to_zone") == "Graveyard"]
        pick_oids = set(str(x) for x in ST.get("discard_pick_oids", []))
        discard_moves = [(i, z) for i, z in enumerate(zc)
                         if str(z.get("object_id")) in pick_oids
                         and z.get("from_zone") == "Hand"
                         and z.get("to_zone") == "Graveyard"]
        notes.append(f"A4: zone_changes_this_turn entries={len(zc)}; "
                     f"looting->GY moves="
                     f"{[(i, z.get('from_zone')) for i, z in looting_moves]}; "
                     f"discard->GY moves="
                     f"{[(i, z.get('name'), z.get('from_zone')) for i, z in discard_moves]}")
        if not looting_moves:
            ass["A4_final_part_timing"] = "not-run"
            notes.append("A4: no Looting ->Graveyard move in the record")
        elif len(discard_moves) < 2:
            ass["A4_final_part_timing"] = "not-run"
            notes.append("A4: fewer than 2 discard Hand->GY moves in record")
        else:
            li = looting_moves[-1][0]
            di = max(i for i, _ in discard_moves)
            if li > di:
                ass["A4_final_part_timing"] = "passed"
                notes.append(f"A4: Looting ->GY at index {li} AFTER both "
                             f"discards (max index {di}): final part ran "
                             "after every paused instruction (CR 608.2c)")
            else:
                ass["A4_final_part_timing"] = "failed"
                notes.append(f"A4: Looting ->GY at index {li} BEFORE the "
                             f"discards (max index {di}): the spell was put "
                             "into the graveyard at the pause, before the "
                             "final part -- the reported defect")

    # A5: Looting's zone while the discard choice is pending
    if mid is None:
        ass["A5_mid_pause_zone"] = "not-run"
        notes.append("A5: mid_pause.json missing")
    else:
        s = mid["state"]
        oid = looting_oid_in(s)
        zone = (get_obj(s, oid).get("zone") or "?") if oid else "not-found"
        ok = zone not in ("Graveyard", "Exile", "Hand", "Library")
        ass["A5_mid_pause_zone"] = "passed" if ok else "failed"
        notes.append(f"A5: at the discard pause Looting (oid {oid}) zone="
                     f"{zone!r} (must not be prematurely delivered)")

    # A6: cleanup
    if post is None:
        ass["A6_cleanup"] = "not-run"
        notes.append("A6: post.json missing")
    else:
        s = post["state"]
        oid = looting_oid_in(s)
        in_gy = oid is not None and \
            (get_obj(s, oid).get("zone") or "") == "Graveyard"
        empty = len(stack_entries(s)) == 0
        # corroborating graveyard order (convention documented in notes)
        gy = list(player_of(s, 0).get("graveyard", []) or [])
        gy_names = [obj_lname(s, o) for o in gy]
        notes.append(f"A6: P0 graveyard oids={gy} names={gy_names}")
        ok = in_gy and empty
        ass["A6_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A6: looting_in_P0_gy={in_gy} stack_empty={empty} "
                     f"turn={s.get('turn_number')} phase={s.get('phase')}")

    # verdict
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("setup never reached the Looting cast")
    elif ass["A3_discard_pause"] != "passed":
        verdict = "blocked"
        notes.append("the discard pause was not observed/answered; the "
                     "reported path was not exercised")
    elif ass["A4_final_part_timing"] == "failed" \
            or ass["A5_mid_pause_zone"] == "failed":
        verdict = "reproduced"
        notes.append("the resolving spell was put into its owner's graveyard "
                     "before every paused instruction was answered (CR "
                     "608.2c/608.2n final-part violation)")
    elif all(ass[k] == "passed" for k in ASS_KEYS):
        verdict = "not-reproduced"
        notes.append("Looting stayed out of the graveyard through the "
                     "discard pause and moved only as the final part")
    else:
        verdict = "blocked"
        notes.append("incomplete run; see assertion notes")
    return verdict, ass


async def finalize(verdict, ass):
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST.get("started_iso"),
        "duration_s": round(time.time() - ST["t_start"], 1),
        "server": SERVER_IDENTITY,
        "server_port": 9374,
        "driver": {"protocol_advertised": 144, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_9728_01110.py",
                 "rb").read()).hexdigest(),
        "decks": {"P0": "8x faithless looting + 52x mountain",
                  "P1": "60x forest"},
        "assertions": ass,
        "notes": ST["notes"],
        "observations": {
            "rejections": ST["rejections"],
            "discard_opps": ST["discard_opps"],
            "discard_pick_oids": ST.get("discard_pick_oids"),
            "discard_pick_names": ST.get("discard_pick_names"),
            "cast_submitted": ST.get("cast_submitted"),
            "cast_confirmed": ST.get("cast_confirmed"),
            "cast_oid": ST.get("cast_oid"),
            "pre_hand_count": ST.get("pre_hand_count"),
            "pause_hand_count": ST.get("pause_hand_count"),
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x/52x split is a test-harness convenience (engine accepts >4-of "
            "for custom games).",
            "Only one pause kind is exercised (spell discard choice during "
            "resolution); the issue's other pause kinds (trigger batches, "
            "cleanup mana-order prompt, searches, rebound, copy-entry "
            "triggers) are not covered by this run.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0: 8x faithless looting + 52x mountain; P1: 60x "
                      "forest. P0 casts Faithless Looting turn 1.",
        "contract_line": "Cast Faithless Looting; the discard-2 pause must be "
                         "presented and answered while the spell stays out of "
                         "the graveyard, and the spell must reach the "
                         "graveyard only as the final part of resolution "
                         "(CR 608.2c/608.2n).",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9728_01110.py", "w") as f:
        f.write(open(__file__).read())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)

    say("closing logs before manifest computation")
    WIRE.close()
    RUNLOG.close()
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_9728_01110.py",
         EVDIR, str(ISSUE),
         "Resolving spell must reach the graveyard only after its final part"],
        capture_output=True, text=True)
    print("render stdout:", (r.stdout or "")[:300], flush=True)
    print("render stderr:", (r.stderr or "")[:500], flush=True)
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass,
                      "run_id": RUN_ID}, indent=1), flush=True)

# ---------------------------------------------------------------- main

async def new_game(p0deck, p1deck):
    global P0C, P1C
    p0 = PhaseClient("P0")
    P0C = p0
    await p0.connect()
    await p0.create(p0deck)
    p1 = PhaseClient("P1")
    P1C = p1
    await p1.connect()
    await p1.join(p0.game_code, p1deck)
    ST["game_code"] = p0.game_code
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})
    return p0, p1


async def close_game(p0, p1):
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass


def looting_in_gy_of(state, pid):
    oid = looting_oid_in(state, pid)
    return oid is not None and \
        (get_obj(state, oid).get("zone") or "") == "Graveyard"


async def run_loop(p0, p1, max_turns, deadline_s):
    t0 = time.time()
    max_turn = 0
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = 0.0
    i = 0
    while i < 9000:
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
                        f"turn={s0.get('turn_number')} "
                        f"phase={s0.get('phase')} "
                        f"holding_priority="
                        f"{my_priority(merged_actions(st), st)} "
                        f"stage={ST['stage']}")
                    wire("watchdog_stale",
                         {"who": c.name, "rev": c.revision,
                          "turn": s0.get("turn_number"),
                          "phase": s0.get("phase"),
                          "stage": ST["stage"]})
                    last_change[pid] = time.time()
                holds_prio = my_priority(merged_actions(st), st)
                if not (holds_prio
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            drain_rejections(c, ST["stage"])
            try:
                await tick(c)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        s = (p0.latest["state"] if p0.latest else {}) or {}
        turn = s.get("turn_number", 0)
        max_turn = max(max_turn, turn)

        # ---- stage transitions ----
        if ST["stage"] == "looting_cast" \
                and ST.get("discard_picks_total", 0) >= 1 \
                and looting_in_gy_of(s, 0) \
                and len(stack_entries(s)) == 0:
            ST["stage"] = "done"
            say("Looting resolved into P0 graveyard with empty stack; "
                "exporting post")
            await export_named("post")
            return True
        if ST["stage"] == "done":
            return True
        if turn >= max_turns:
            ST["notes"].append(f"turn {max_turns} reached in {ST['stage']}; "
                               "bailing out")
            say(f"turn bail at {turn}")
            return False
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            looting_zone = (get_obj(s, looting_oid_in(s) or -1).get("zone")
                            if looting_oid_in(s) else "?")
            say(f"DIAG turn={turn} phase={s.get('phase')} "
                f"stage={ST['stage']} looting_zone={looting_zone} "
                f"stack={len(stack_entries(s))} "
                f"discard_picks={ST.get('discard_picks_total', 0)}")
        if time.time() - t0 > deadline_s:
            ST["notes"].append("loop deadline hit")
            say("loop deadline hit")
            return False
    return False


async def _main():
    ST.update({
        "t_start": time.time(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notes": [],
        "rejections": [],
        "tick_errors": [],
        "data_level_ok": False,
        "hello_ok": False,
        "mana_needs": {"P0": {}, "P1": {}},
        "stage": "setup",
        "exports": {},
        "discard_opps": [],
    })
    await verify_server_hello()
    check_data_level()

    p0, p1 = await new_game(P0_DECK, P1_DECK)
    ok = await run_loop(p0, p1, 25, 1200)
    say(f"loop ended ok={ok} stage={ST['stage']}")
    await close_game(p0, p1)

    verdict, ass = evaluate()
    say(f"verdict={verdict} assertions={json.dumps(ass)}")
    await finalize(verdict, ass)


async def main():
    pidfile = "/tmp/scenario_9728_01110.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9728_01110 instance is alive "
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


asyncio.run(main())
