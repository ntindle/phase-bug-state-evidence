#!/usr/bin/env python3
"""Issue #7166 re-validation on v0.110.0 (protocol 142): Tibalt's Trickery
does not work at all, other than countering a spell.

Oracle: "Counter target spell. Choose 1, 2, or 3 at random. Its controller
mills that many cards, then exiles cards from the top of their library until
they exile a nonland card with a different name than that spell. They may
cast that card without paying its mana cost. Then they put the exiled cards
on the bottom of their library in a random order."

Reported (Discord): only the initial counter occurs; the random mill,
exile-until, free cast, and cleanup sequence never execute. The pinned
v0.110.0 card-data parse still shows the chain as Counter ->
Unimplemented("Choose 1, 2, or 3 at random") -> Mill -> ExileFromTopUntil ->
GrantCastingPermission(PlayFromExile) -> PutAtLibraryPosition, so the
unimplemented random choice is expected to gate the whole chain.

Prior run (2026-09-14, v0.83.0/protocol 70, run 20260914-7166f): reproduced
- A1/A2/A7 passed; A3 mill FAILED (0 milled); A4 exile FAILED (nothing
exiled); A5/A6 not-run. Published + comment maintained. Driver note: the
engine auto-targets a counterspell at the single spell on the stack (no
TargetSelection prompt); proven via selected_target_incarnations in
pre_trickery.json.

This script is a protocol-142 port of scenario_7166.py (driver conventions
from the verified protocol-142 scenario_7159_01100.py):
  - waiting_for is gone (null); prompt detection via
    viewer_interaction.waitingForKind.code + opportunities (canSubmit).
  - PlayLand/CastSpell/ActivateAbility live in legal_actions_by_object;
    merged_actions() merges them with _src_oid (plain top-level
    legal_actions hides them).
  - CastSpell submitted EXACTLY as advertised (no target injection); the
    engine auto-targets Trickery at the lone stack spell (proven via the
    stack entry's target fields in pre_trickery.json). If a TargetSelection
    vi opportunity naming the Bolt appears, it is answered; the driver never
    stalls waiting for one.
  - Bolt's player target IS answered via vi (candidates reference players;
    pick seat 0, P1's opponent).
  - Priority = PassPriority legal action. Cast-in-flight guard: withhold
    our own PassPriority until the engine processes a submitted CastSpell.
  - MulliganDecision answered via legacy Action; bottoming via vi
    schema/select (waitingForKind.code == 'mulligan') or verbatim
    SelectCards; DiscardToHandSize via vi schema/select.

Behavioral contract (2 human seats, native engine, Bo1, protocol 142):
  P0: 20x Tibalt's Trickery + 40x Mountain. Counters P1's Bolt with
      Trickery in response.
  P1: 20x Mountain + 20x Lightning Bolt + 20x Grizzly Bears. Casts Bolt at
      P0 once P0 can answer (Trickery in hand + 2 untapped Mountains, read
      from P0's unfiltered state); accepts the free cast if offered.

  A1 setup_ok      P1 cast Lightning Bolt targeting P0; P0 cast Tibalt's
                   Trickery in response; Trickery reached the stack
                   (pre_trickery.json exported).
  A2 countered     Bolt reached P1's graveyard, P0 life stayed 20 (no 3
                   damage), Trickery resolved to P0's graveyard.
  A3 mill          P1 milled 1-3 cards (gy delta beyond the countered Bolt
                   and discards). failed = 0 (the reported cutoff).
  A4 exile_until   the exile-until stopped at a nonland card with a
                   different name than "Lightning Bolt" from P1's library.
                   failed = no exile / wrong library.
  A5 free_cast     the exiled card was cast for free and reached P1's
                   battlefield (accept branch exercised). not-run if the
                   offer never appeared.
  A6 bottom_rest   P1's exile zone empty at post; P1 card conservation
                   holds (60 cards across zones). not-run if A4 not-run.
  A7 cleanup       no stall; game advanced past the resolution; post
                   exported.

Verdict: reproduced iff A2 passes and (A3 fails or A4 fails) - the counter
         worked but the chain did not. not-reproduced iff A2-A6 pass (or the
         chain executed through the offer and only the accept-branch
         recognizer could not fire, recorded as a limitation).
         blocked iff A1 fails or the resolution window was interrupted.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402
import websockets as wsmod  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-7166-reval-v01100-20261015-0011")
ISSUE = 7166
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TRICKERY = "Tibalt's Trickery"
BOLT = "Lightning Bolt"
MOUNTAIN = "Mountain"
BEARS = "Grizzly Bears"
P0_DECK = [(TRICKERY, 20), (MOUNTAIN, 40)]
P1_DECK = [(MOUNTAIN, 20), (BOLT, 20), (BEARS, 20)]
TIMEOUT = 900
LANDS = ("Plains", "Mountain", "Forest", "Swamp", "Island")

CD_PATH = f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"

SERVER_IDENTITY = {
    "server_version": "0.110.0",
    "build_commit": "9bfd12e",
    "protocol_version": 142,
    "mode": "Full",
    "binary_sha256": "346ad6a1a8cd887bb49108fa132476bea36e42e7cfc5116fe2cbdf8ad369e3f2",
    "card_data_sha256": "c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74",
    "draft_pools_sha256": "7e45abbf0a964959192d2528c067a2a9caab04438640ab63b9ad79d2ce0e4676",
    "signature_verified": True,
    "signature_key_id": "436711b6a2d36828",
    "observed_at": "2026-10-15",
    "source": "isolated v0.110.0 single-user server on 127.0.0.1:9374, "
              "started fresh by this run (run-7166-reval-v01100-20261015-0035) "
              "with its own games.db; ServerHello re-verified at run start; "
              "verified pin (minisign-verify of binary + signed data manifest "
              "with the repo-pinned key).",
}

ST = {}
WF_SEEN = []
ACTED = {}
C0 = C1 = None
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}


def reset():
    ST.clear()
    ACTED.clear()
    ST.update({
        "stage": "SETUP",  # SETUP -> CAST -> TRICKERY -> RESOLVING -> DONE
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "hello_ok": False,
        "exports": {},
        "rejections": [],
        "life": {0: 20, 1: 20},
        "parse_rec": None,
        "parse_chain": None,
        "bolt_cast": False, "bolt_oid": None, "bolt_stack_id": None,
        "bolt_target_answered": False,
        "trickery_cast": False, "trickery_oid": None,
        "trickery_target_answered": False,
        "trickery_target_record": None,
        "trickery_on_stack": False, "trickery_stack_id": None,
        "trickery_resolved": False, "resolve_turn": None,
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "p1_gy_before": None, "p1_discards": 0,
        "mill_count": None,
        "exile_seen": set(), "exile_events": [],
        "exile_stop_name": None, "exile_library_owner": None,
        "free_name": None, "free_cast_oid": None,
        "free_cast_offered": False, "free_cast_offer_shape": None,
        "free_cast_done": False, "free_on_battlefield": False,
        "post_turn": None,
        "cast_pending": None,  # (oid, rev, t0) cast-in-flight guard
        "p0_can_answer": False,
        "turns_seen": set(), "game_started": False, "game_over": False,
        "stall_observed": False,
        "unexpected": [],
        "notes": [],
        "t0": time.time(),
    })
    WF_SEEN.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "error": str(e)}) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("card_name") or o.get("name")
               or o.get("base_name") or "").lower()


def hand_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Hand"
            and str(o.get("controller", -1)) == str(pid)]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def find_hand(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name.lower():
            return o
    return None


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o))
            and not get_obj(state, o).get("tapped")]


def untapped_land_count(state, pid):
    return len(untapped_lands(state, pid))


def life(state, pid):
    for p in state.get("players", []):
        if str(p.get("id", p.get("seat", -1))) == str(pid):
            return p.get("life")
    return None


def zone_count(state, owner, zone):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == zone and str(o.get("owner", -1)) == str(owner))


def gy_names(state, owner):
    return [str(o.get("card_name") or o.get("name") or "")
            for o in (state.get("objects") or {}).values()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", -1)) == str(owner)]


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


def merged_actions(st):
    """Top-level legal_actions + legal_actions_by_object (with _src_oid).
    Protocol 118+: PlayLand/CastSpell/ActivateAbility are hidden unless
    merged."""
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


def action_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def action_type_codes(ch):
    """Codes from action-type surfaces only (summary/value surfaces carry
    marker codes like "candidate" that are not decisions)."""
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if s.get("type") == "action"
            and isinstance(s.get("data"), dict)]


def my_priority(acts):
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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def _cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg = {"type": a["type"], "data": a["data"]}
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
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = action_codes(ch)
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def verify_server_hello():
    ws = await wsmod.connect("ws://127.0.0.1:9374/ws", max_size=1_000_000)
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
    assert str(ver).startswith("0.110.0"), f"unexpected version {ver}"
    assert int(proto) == 142, f"unexpected protocol {proto}"
    assert str(build) == "9bfd12e", f"unexpected build {build}"
    ST["server_hello"] = hello
    ST["hello_ok"] = True


def parse_check():
    """Record the pinned card-data parse of Tibalt's Trickery."""
    cd = json.load(open(CD_PATH))
    t = cd.get("tibalt's trickery")
    if t is None:
        for k, v in cd.items():
            if isinstance(v, dict) and \
                    str(v.get("name") or "").lower() == "tibalt's trickery":
                t = v
                break
    rec = {"found": t is not None}
    if t is not None:
        ab = (t.get("abilities") or [None])[0]
        chain = []
        unimpl = []
        while ab:
            eff = ab.get("effect") or {}
            et = str(eff.get("type") or eff.get("name"))
            chain.append(et)
            if et.lower() == "unimplemented":
                unimpl.append(str(eff.get("description")
                                  or eff.get("reason") or "")[:160])
            ab = ab.get("sub_ability")
        rec["effect_chain"] = chain
        rec["unimplemented_nodes"] = unimpl
        ST["parse_chain"] = chain
        with open(f"{EVDIR}/parse_trickery.json", "w") as f:
            json.dump(rec, f, indent=1)
        say(f"parse_trickery.json written; chain: {' -> '.join(chain)}; "
            f"unimplemented: {unimpl}")
    else:
        say("parse check: Tibalt's Trickery NOT FOUND in card-data")
    wire("parse_check", rec)
    ST["parse_rec"] = rec
    return rec


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    ST["exports"][path] = True
    say(f"exported {path}")
    return json.loads(s)


def load_state(path):
    try:
        with open(f"{EVDIR}/{path}") as f:
            env = json.load(f)
        return env.get("state", env)
    except Exception:
        return None


# ---------------- preamble (mulligan / bottom / discard / land) -----------
async def do_mulligan(c, acts, st, pid, tag, keep_pred):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in action_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", str(iid or c.revision))
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    state = st["state"]
    mull_count = sum(1 for k in SUBMITTED_OPPS
                     if k[0] == tag and k[1] == "mull")
    keep = keep_pred(state, pid, mull_count)
    say(f"[{tag}] mulligan #{mull_count}: hand={hand_lnames(state, pid)} -> "
        f"{'keep' if keep else 'redo'}")
    wire("mulligan", {"who": tag, "decision": "keep" if keep else "mulligan",
                      "hand": hand_lnames(state, pid),
                      "iid": str(iid)[:12]})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if keep else "Mulligan"}}})
    return True


def p0_keep_pred(state, pid, mull_count):
    hn = hand_lnames(state, pid)
    nlands = sum(1 for n in hn if n in [l.lower() for l in LANDS])
    return ((TRICKERY.lower() in hn and nlands >= 2) or mull_count >= 3)


def p1_keep_pred(state, pid, mull_count):
    hn = hand_lnames(state, pid)
    nlands = sum(1 for n in hn if n in [l.lower() for l in LANDS])
    return ((BOLT.lower() in hn and nlands >= 1) or mull_count >= 3)


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan: gated on waitingForKind.code == 'mulligan' at
    turn 1 / Untap. Prefer the vi schema/select opportunity; fall back to
    the verbatim per-card SelectCards actions."""
    state = st["state"]
    if vi_kind_code(st) != "mulligan":
        return False
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
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    con = None
    if target:
        con = (((target[2].get("data") or {}).get("constraint") or {})
               .get("data") or {})
    else:
        a0 = sel_acts[0].get("data", {}) or {}
        con = a0.get("constraint") or {}
        if isinstance(con, dict):
            con = (con.get("data") or {})
    n = int((con or {}).get("min") or (con or {}).get("max") or 1)
    if n <= 0:
        return False
    key = (tag, "bottom", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    protect = {TRICKERY.lower()} if pid == 0 else {BOLT.lower()}

    def rank_oid(o):
        nm = obj_lname(state, o)
        if nm in protect:
            return (2, str(o))
        if is_land(get_obj(state, o)):
            return (0, str(o))
        return (1, str(o))

    picks = sorted(hand_ids(state, pid), key=rank_oid)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, o) for o in picks]}")
    if target and all(_cand_reference(ch) is not None
            for ch in target[1]):
        opp = target[0]
        id_by_ref = {}
        for ch in target[1]:
            id_by_ref[str(_cand_reference(ch))] = ch.get("id")
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "select",
                            "data": {"choiceIds": [id_by_ref[str(o)] for o in picks
                                                   if str(o) in id_by_ref]}}}
        wire("bottom", {"who": tag, "via": "vi", "picks": picks})
        await interact_as(c, sub, tag)
        return True
    wire("bottom", {"who": tag, "via": "verbatim_selectcards", "picks": picks})
    for a in sel_acts:
        cards = (a.get("data", {}) or {}).get("cards") or []
        if cards and int(cards[0]) in picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(cards[0])]}})
            await asyncio.sleep(0.4)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    protect = {TRICKERY.lower()} if pid == 0 else {BOLT.lower()}
    handset = set(str(x) for x in hand)
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
        if any(str(_cand_reference(ch)) in handset for ch in cands):
            found_opp = (opp, cands, spec)
            break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    opp, cands, spec = found_opp
    id_by_ref = {}
    for ch in cands:
        r = _cand_reference(ch)
        if r is not None:
            id_by_ref[str(r)] = ch.get("id")

    def rank_oid(o):
        nm = obj_lname(state, o)
        if nm in protect:
            return (5, str(o))
        if is_land(get_obj(state, o)):
            return (0, str(o))
        return (2, str(o))

    picks = sorted(hand, key=rank_oid)[:max(1, n)]
    stype = spec.get("type") or "select"
    say(f"[{tag}] discarding to hand size via vi ({stype}): "
        f"{[obj_lname(state, o) for o in picks]}")
    wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": [id_by_ref[str(o)]
                                                               for o in picks
                                                               if str(o) in id_by_ref]}}},
                      tag)
    return True


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


# ---------------- target selection ----------------------------------------
def stack_entries(state):
    return state.get("stack") or []


def bolt_stack_entry(state):
    """Locate P1's Bolt on the stack: by source oid, else by name."""
    if ST.get("bolt_oid") is not None:
        for e in stack_entries(state):
            sid = e.get("source_id")
            if sid is not None and str(sid) == str(ST["bolt_oid"]):
                return e
    for e in stack_entries(state):
        nm = str(e.get("name") or e.get("card_name") or "").lower()
        if nm == "lightning bolt":
            return e
    return None


def trickery_stack_entry(state):
    if ST.get("trickery_oid") is not None:
        for e in stack_entries(state):
            sid = e.get("source_id")
            if sid is not None and str(sid) == str(ST["trickery_oid"]):
                return e
    for e in stack_entries(state):
        nm = str(e.get("name") or e.get("card_name") or "").lower()
        if nm == "tibalt's trickery":
            return e
    return None


def log_prompt_shape(tag, opp, purpose):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    shape = {"who": tag, "purpose": purpose,
             "iid": str(opp.get("interactionId"))[:16],
             "rtype": resp.get("type"),
             "n_choices": len(chs),
             "texts": [choice_text(ch)[:60] for ch in chs[:8]],
             "seats": [_cand_seat(ch) for ch in chs[:8]],
             "refs": [str(_cand_reference(ch)) for ch in chs[:8]]}
    wire("prompt_shape", shape)
    say(f"[{tag}] prompt ({purpose}): rtype={shape['rtype']} "
        f"n={shape['n_choices']} texts={shape['texts']} "
        f"seats={shape['seats']} refs={shape['refs']}")


def pick_player_target(seat):
    """Pick the candidate naming player `seat`. P1's opponent is seat 0."""
    def _pick(chs):
        for ch in chs:
            r = _cand_reference(ch)
            if r is not None and str(r) == str(seat):
                return ch
        for ch in chs:
            if _cand_seat(ch) == seat:
                return ch
        for ch in chs:
            t = choice_text(ch).lower()
            if f"player {seat}" in t:
                return ch
        if seat == 0:
            for ch in chs:
                if "opponent" in choice_text(ch).lower():
                    return ch
        return chs[0] if len(chs) == 1 else None
    return _pick


def pick_bolt_candidate(chs, bolt_refs):
    for ch in chs:
        r = _cand_reference(ch)
        if r is not None and str(r) in bolt_refs:
            return ch
    for ch in chs:
        if "lightning bolt" in choice_text(ch).lower():
            return ch
    return chs[0] if len(chs) == 1 else None


async def answer_target_selection(c, st, state, pid, tag, purpose, pick_fn):
    """Answer a target-selection vi opportunity if one is present and the
    picker finds a match. Never blocks: returns False when no matching
    opportunity exists (the engine may auto-target)."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "tgt", str(iid))
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        if rtype not in ("sequence", "schema", "exactChoices", "choose"):
            continue
        log_prompt_shape(tag, opp, purpose)
        pick = pick_fn(chs)
        if pick is None:
            say(f"[{tag}] target selection ({purpose}): no matching "
                f"candidate; leaving it")
            wire("target_no_match", {"who": tag, "purpose": purpose})
            SUBMITTED_OPPS.add(key)
            return False
        await answer_vi(c, opp, pick, tag)
        SUBMITTED_OPPS.add(key)
        ST["trickery_target_record"] = {
            "who": tag, "purpose": purpose,
            "choice_id": pick.get("id"),
            "choice_text": choice_text(pick)[:120],
            "ref": str(_cand_reference(pick)),
            "seat": _cand_seat(pick),
        }
        say(f"[{tag}] answered {purpose}: {choice_text(pick)[:60]}")
        return True
    return False


def cast_action_for(acts, state, name):
    for a in acts:
        if a.get("type") == "CastSpell":
            oid = a.get("data", {}).get("object_id")
            if oid is not None and obj_lname(state, oid) == name.lower():
                return a, int(oid)
        if a.get("type") == "CastSpell":
            oid = a.get("_src_oid")
            if oid is not None and obj_lname(state, oid) == name.lower():
                return a, int(oid)
    return None, None


# ---------------- per-seat ticks ------------------------------------------
async def p0_tick(c):
    """P0: mulligan, land drops, counter the Bolt with Trickery, pass."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    rev = st.get("state_revision", -1)

    if await do_mulligan(c, acts, st, 0, "P0", p0_keep_pred):
        return True
    if await do_bottom(c, acts, st, 0, "P0"):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, "P0"):
        return True
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True

    # Trickery target: answer opportunistically if the engine raises a
    # TargetSelection naming the Bolt. Never blocks: the engine
    # auto-targets a counterspell at the lone stack spell (proven via the
    # stack entry's target fields in pre_trickery.json).
    if ST["trickery_cast"] and not ST["trickery_resolved"] \
            and trickery_stack_entry(state) is not None \
            and not ST["trickery_target_answered"]:
        bolt_ent = bolt_stack_entry(state)
        bolt_refs = set()
        if bolt_ent is not None:
            for k in ("id", "source_id"):
                if bolt_ent.get(k) is not None:
                    bolt_refs.add(str(bolt_ent.get(k)))
        if ST.get("bolt_oid") is not None:
            bolt_refs.add(str(ST["bolt_oid"]))
        done = await answer_target_selection(
            c, st, state, 0, "P0", "trickery-target",
            lambda chs: pick_bolt_candidate(chs, bolt_refs))
        if done:
            ST["trickery_target_answered"] = True
            return True

    # Cast Trickery in response to the Bolt on the stack. Submitted EXACTLY
    # as advertised: never inject targets (the engine ignores them and a
    # mistyped oid silently drops the whole cast).
    if not ST["trickery_cast"] and bolt_stack_entry(state) is not None:
        tid = find_hand(state, 0, TRICKERY)
        if tid and untapped_land_count(state, 0) >= 2:
            ca, _ = cast_action_for(acts, state, TRICKERY)
            if ca and not acted("casttrickery", rev):
                ST["p1_gy_before"] = zone_count(state, 1, "Graveyard")
                await submit_as_is(c, ca)
                ST["trickery_cast"] = True
                ST["trickery_oid"] = int(tid)
                ST["cast_pending"] = (int(tid), rev, time.time())
                ST["stage"] = "TRICKERY"
                say(f"[P0] casts Tibalt's Trickery (oid={tid}) in response "
                    f"to Bolt")
                wire("trickery_cast", {"turn": state.get("turn_number"),
                                       "oid": int(tid)})
                return True

    # Export the pre-resolution state once Trickery is on the stack.
    if ST["trickery_cast"] and not ST["pre_exported"] \
            and trickery_stack_entry(state) is not None:
        ST["trickery_on_stack"] = True
        ent = trickery_stack_entry(state)
        ST["trickery_stack_id"] = ent.get("id")
        env = await export_now("pre_trickery.json")
        if env is not None:
            ST["pre_exported"] = True
            pst = env.get("state", env)
            for e in pst.get("stack") or []:
                if str(e.get("id")) == str(ST["trickery_stack_id"]) \
                        or (ST.get("trickery_oid") is not None
                            and str(e.get("source_id"))
                            == str(ST["trickery_oid"])):
                    ST["stack_target_evidence"] = {
                        k: e.get(k) for k in
                        ("id", "source_id", "name", "card_name",
                         "selected_target_incarnations", "targets",
                         "chosen_targets")
                        if k in e}
                    break
            say(f"pre_trickery exported; stack target evidence: "
                f"{json.dumps(ST.get('stack_target_evidence'))[:300]}")
        return True

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, "P0"):
            return True
    return False


async def p1_tick(c):
    """P1: mulligan, land drops, Bolt at P0, accept the free cast, pass."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    rev = st.get("state_revision", -1)

    if await do_mulligan(c, acts, st, 1, "P1", p1_keep_pred):
        return True
    if await do_bottom(c, acts, st, 1, "P1"):
        return True
    if await do_discard_to_handsize(c, acts, st, 1, "P1"):
        return True
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True

    # Free cast of the exiled card (GrantCastingPermission PlayFromExile):
    # (a) a CastSpell advertised for the exiled card, or (b) a vi may-cast
    # opportunity referencing it. Accept branch is exercised deliberately.
    if ST.get("free_cast_oid") and not ST["free_cast_done"]:
        foid = str(ST["free_cast_oid"])
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            oid = str(a.get("data", {}).get("object_id", ""))
            if not oid:
                oid = str(a.get("_src_oid", ""))
            o = get_obj(state, oid)
            if oid == foid and o.get("zone") == "Exile" \
                    and str(o.get("controller", -1)) == "1":
                if not acted("freecast", rev):
                    await submit_as_is(c, a)
                    ST["free_cast_done"] = True
                    ST["free_cast_offered"] = True
                    ST["free_cast_offer_shape"] = "CastSpell-advertised"
                    say(f"[P1] casts exiled {ST['free_name']} for free "
                        f"(oid={oid})")
                    wire("free_cast", {"turn": state.get("turn_number"),
                                       "oid": int(oid),
                                       "name": ST["free_name"]})
                    return True
        # (b) vi may-cast / optional prompt for P1: accept iff it names the
        # exiled card (by reference) or is an obvious cast-accept choice.
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            key = ("P1", "maycast", str(iid))
            if key in SUBMITTED_OPPS:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            log_prompt_shape("P1", opp, "free-cast-offer")
            ST["free_cast_offered"] = True
            ST["free_cast_offer_shape"] = vi_kind_code(st) or resp.get("type")
            pick = None
            for ch in chs:
                if str(_cand_reference(ch)) == foid:
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    t = choice_text(ch).lower()
                    codes = action_codes(ch)
                    if ("cast" in t or "cast" in " ".join(codes).lower()) \
                            and "decline" not in t and "don't" not in t \
                            and "not " not in t:
                        pick = ch
                        break
            if pick is not None:
                await answer_vi(c, opp, pick, "P1")
                SUBMITTED_OPPS.add(key)
                ST["free_cast_done"] = True
                say(f"[P1] accepted the free-cast offer "
                    f"({ST['free_cast_offer_shape']})")
                return True
            say("[P1] free-cast offer shape unrecognized; not answering")
            wire("free_cast_unrecognized",
                 {"kind": vi_kind_code(st), "stage": ST.get("stage")})
            SUBMITTED_OPPS.add(key)

    # Answer Bolt's target selection (target P0, seat 0).
    if ST.get("pending_bolt_target"):
        done = await answer_target_selection(
            c, st, state, 1, "P1", "bolt-target",
            lambda chs: pick_player_target(0)(chs))
        if done:
            ST["pending_bolt_target"] = False
            ST["bolt_target_answered"] = True
            return True
        # the prompt may have vanished (auto-target); do not wedge
        ST["pending_bolt_target"] = False

    # Cast Bolt at P0 once P0 can answer with Trickery. P0's hand is hidden
    # from P1's viewer-filtered state, so readiness is evaluated from P0's
    # own state in the main loop (ST["p0_can_answer"]).
    if not ST["bolt_cast"] and my_main(state, 1) and ST.get("p0_can_answer"):
        bid = find_hand(state, 1, BOLT)
        if bid and untapped_land_count(state, 1) >= 1:
            ca, _ = cast_action_for(acts, state, BOLT)
            if ca and not acted("castbolt", rev):
                await submit_as_is(c, ca)
                ST["bolt_cast"] = True
                ST["bolt_oid"] = int(bid)
                ST["pending_bolt_target"] = True
                ST["cast_pending"] = (int(bid), rev, time.time())
                ST["stage"] = "CAST"
                say(f"[P1] casts Lightning Bolt (oid={bid}) targeting P0")
                wire("bolt_cast", {"turn": state.get("turn_number"),
                                   "oid": int(bid)})
                return True

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, "P1"):
            return True
    return False


# ---------------- observation ------------------------------------------------
async def observe(state):
    """Track Trickery resolution: counter, mill, exile-until, free cast."""
    turn = state.get("turn_number") or 0
    objs = state.get("objects") or {}

    # --- exile tracking (the exile-until leg) ---
    # Only during the cast->resolution window: game-over cleanup (e.g. a
    # decked player losing) also moves cards to Exile and must not be
    # mistaken for the Trickery's exile-until leg.
    rt = ST.get("resolve_turn")
    in_window = ST["trickery_cast"] and (rt is None or turn <= rt + 3)
    for oid, o in objs.items():
        if o.get("zone") != "Exile":
            continue
        if oid in ST["exile_seen"]:
            continue
        ST["exile_seen"].add(oid)
        nm = str(o.get("card_name") or o.get("name") or "")
        ev = {"turn": turn, "oid": int(oid), "name": nm,
              "owner": o.get("owner"), "controller": o.get("controller")}
        if not in_window:
            # outside the resolution window: record only as noise (e.g.
            # game-over cleanup), never as the exile-until leg
            wire("exiled_outside_window", ev)
            continue
        ST["exile_events"].append(ev)
        say(f"exiled: {nm} oid={oid} owner={o.get('owner')} turn={turn}")
        wire("exiled", ev)
        is_land_card = ("Land" in ((o.get("card_types") or {})
                                   .get("core_types") or [])) \
            or nm.lower() == "mountain"
        # the until-stop: first nonland card with a different name than
        # Lightning Bolt (Oracle: from the countered spell's controller's
        # library)
        if (ST["exile_stop_name"] is None and not is_land_card
                and nm.lower() != "lightning bolt" and nm):
            ST["exile_stop_name"] = nm
            ST["exile_library_owner"] = o.get("owner")
            say(f"exile-until STOP at {nm} (owner={o.get('owner')})")
            wire("exile_stop", ev)
        # the free-cast candidate: a P1-owned nonland in exile
        if (ST.get("free_cast_oid") is None and not is_land_card and nm
                and str(o.get("owner", -1)) == "1"):
            ST["free_cast_oid"] = int(oid)
            ST["free_name"] = nm
            say(f"free-cast candidate: {nm} oid={oid}")
            wire("free_cast_candidate", ev)
            if not ST["mid_exported"] and ST["trickery_resolved"]:
                if await export_now("mid_mill.json") is not None:
                    ST["mid_exported"] = True

    # --- Trickery resolution ---
    if ST["trickery_cast"] and not ST["trickery_resolved"]:
        in_gy = any(
            obj_lname(state, o) == "tibalt's trickery"
            and get_obj(state, o).get("zone") == "Graveyard"
            and str(get_obj(state, o).get("controller", -1)) == "0"
            for o in objs)
        on_stack = trickery_stack_entry(state) is not None
        if in_gy or (ST["trickery_on_stack"] and not on_stack):
            ST["trickery_resolved"] = True
            ST["resolve_turn"] = turn
            ST["resolve_wall"] = time.time()
            ST["stage"] = "RESOLVING"
            gy = gy_names(state, 1)
            n_bolt = sum(1 for n in gy if n == BOLT)
            ST["mill_count"] = len(gy) - n_bolt - ST["p1_discards"]
            say(f"Trickery resolved turn={turn}; P1 gy={gy} "
                f"mill_count={ST['mill_count']}")
            wire("trickery_resolved",
                 {"turn": turn, "p1_gy": gy,
                  "mill_count": ST["mill_count"],
                  "p0_life": life(state, 0)})

    # --- free-cast card on the battlefield ---
    if ST["free_cast_done"] and not ST["free_on_battlefield"] \
            and ST.get("free_name"):
        if any(obj_lname(state, o) == ST["free_name"].lower()
               and get_obj(state, o).get("zone") == "Battlefield"
               and str(get_obj(state, o).get("controller", -1)) == "1"
               for o in objs):
            ST["free_on_battlefield"] = True
            say(f"free-cast {ST['free_name']} reached P1 battlefield "
                f"turn={turn}")
            wire("free_cast_battlefield", {"turn": turn})

    # --- post export: shortly after resolution with an empty stack.
    # (A vi-kind-gated quiet window never triggers: P0's viewer always
    # carries ambient select/choose opportunities. The state 10s after
    # resolution IS the post-resolution state.)
    if ST["stage"] == "RESOLVING" and not ST["post_exported"]:
        stack_empty = not (state.get("stack") or [])
        waited = time.time() - ST.get("resolve_wall", 0) >= 10
        if stack_empty and waited:
            say(f"exporting post.json turn={turn} "
                f"(10s after resolution, stack empty)")
            if await export_now("post.json") is not None:
                ST["post_exported"] = True
                ST["post_turn"] = turn
                ST["stage"] = "DONE"
            return
    # after post: play a few more turns for cleanup, then stop
    if ST["stage"] == "DONE" and ST["post_turn"] is not None:
        if turn >= ST["post_turn"] + 3:
            ST["done_reason"] = f"cleanup window closed turn={turn}"
            say("DONE: " + ST["done_reason"])
            ST["stop"] = True
            return
    _vi0 = (C0.latest or {}).get("viewer_interaction") or {}
    if ((_vi0.get("waitingForKind") or {}).get("code") or "") == "gameOver" \
            and not ST["game_over"]:
        say("game over; exporting post.json before the session expires")
        ST["game_over"] = True
        if not os.path.exists(f"{EVDIR}/post.json"):
            if await export_now("post.json") is not None:
                ST["post_exported"] = True
        ST["stop"] = True


# ---------------- tick + main loop ----------------------------------------
def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage"),
                                     "t": time.time()})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


async def tick(c, pid):
    st = c.latest
    if not st or ST["stop"]:
        return False
    state = st.get("state")
    if state is None:
        return False
    acts = merged_actions(st)
    drain_rejections(c)
    kind = vi_kind_code(st)
    if kind and (not WF_SEEN or WF_SEEN[-1] != kind):
        WF_SEEN.append(kind)
        wire("waiting_for_kind", {"kind": kind, "stage": ST["stage"]})
        say(f"[{c.name}] vi waitingForKind: {kind}")
    for q in (0, 1):
        lv = life(state, q)
        if lv is not None and lv != ST["life"][q]:
            say(f"[{c.name}] P{q} life {ST['life'][q]} -> {lv} "
                f"(turn {state.get('turn_number')} phase {state.get('phase')})")
            wire("life_change", {"player": q, "from": ST["life"][q], "to": lv,
                                 "turn": state.get("turn_number"),
                                 "phase": state.get("phase")})
            ST["life"][q] = lv
    try:
        if pid == 0:
            did = await p0_tick(c)
        else:
            did = await p1_tick(c)
    except Exception as e:
        say(f"tick error [{c.name}]: {e}")
        wire("tick_error", {"who": c.name, "error": str(e)[:300]})
        return False
    if did:
        return True
    # never pass priority while a cast we submitted is still in flight
    # (the engine must process it first, else our pass races the cast)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    cp = ST.get("cast_pending")
    if cp:
        coid, crev, ct0 = cp
        cur_rev = st.get("state_revision", -1)
        o = get_obj(state, coid)
        if cur_rev <= crev and o.get("zone") == "Hand" \
                and time.time() - ct0 < 30:
            return False
        ST["cast_pending"] = None
    if my_priority(acts):
        await pass_priority(c, st, acts)
        return True
    return False


# ---------------- finalize -------------------------------------------------
async def finalize():
    if ST.get("finalized"):
        return None, {}
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "flow complete"
    say("finalizing...")

    try:
        shutil.copy(__file__, f"{EVDIR}/scenario_7166_01100.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")
    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("server.log copied to evidence (this run's isolated server)")
    except Exception as e:
        say(f"server.log copy failed: {e}")
        wire("server_log_copy_failed", {"error": str(e)[:200]})

    if "post.json" not in ST["exports"]:
        await export_now("post.json")

    post = load_state("post.json")
    pre = load_state("pre_trickery.json")
    ass, notes = {}, []

    def note(m):
        notes.append(m)
        say("NOTE:", m)

    # ---- A1: setup ----
    if ST["bolt_cast"] and ST["trickery_cast"] and ST["pre_exported"]:
        tgt = ST.get("trickery_target_record") or {}
        stev = ST.get("stack_target_evidence") or {}
        ass["A1_setup_ok"] = "passed"
        notes.append(
            f"A1: P1 cast Bolt (oid={ST['bolt_oid']}) targeting P0 "
            f"(answered={ST['bolt_target_answered']}); P0 cast Trickery "
            f"(oid={ST['trickery_oid']}) in response; pre_trickery.json "
            f"exported with Trickery on the stack. Target evidence: "
            f"answered_choice={tgt.get('choice_id')} "
            f"stack_target={json.dumps(stev)[:200]}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"A1 failed: bolt_cast={ST['bolt_cast']} "
                     f"trickery_cast={ST['trickery_cast']} "
                     f"pre_exported={ST['pre_exported']}")

    # ---- A2: counter ----
    if ass["A1_setup_ok"] == "passed" and post is not None:
        p1gy = gy_names(post, 1)
        bolt_gy = BOLT in p1gy
        trickery_gy = any(
            n.lower() == "tibalt's trickery"
            for n in gy_names(post, 0))
        p0life = life(post, 0)
        if bolt_gy and trickery_gy and p0life == 20:
            ass["A2_countered"] = "passed"
            notes.append(f"A2: Bolt countered to P1 gy; Trickery resolved "
                         f"to P0 gy; P0 life={p0life} (no 3 damage)")
        else:
            ass["A2_countered"] = "failed"
            notes.append(f"A2 FAILED: bolt_gy={bolt_gy} "
                         f"trickery_gy={trickery_gy} p0_life={p0life}")
    elif ass["A1_setup_ok"] == "passed":
        ass["A2_countered"] = "failed"
        notes.append("A2 failed: post.json missing")
    else:
        ass["A2_countered"] = "not-run"
        notes.append("A2 not-run: setup failed")

    # ---- A3: mill ----
    if ass["A2_countered"] == "passed":
        mc = ST["mill_count"]
        if mc is not None and 1 <= mc <= 3:
            ass["A3_mill"] = "passed"
            notes.append(f"A3: P1 milled {mc} cards (random 1-3 choice "
                         f"executed)")
        elif mc == 0:
            ass["A3_mill"] = "failed"
            notes.append("A3 FAILED: P1 milled 0 cards - the chain stopped "
                         "after the counter (reported bug)")
        else:
            ass["A3_mill"] = "failed"
            notes.append(f"A3 FAILED: mill_count={mc} (expected 1-3)")
    else:
        ass["A3_mill"] = "not-run"
        notes.append("A3 not-run: counter leg not established")

    # ---- A4: exile-until ----
    if ass["A2_countered"] == "passed":
        stop = ST["exile_stop_name"]
        owner = ST["exile_library_owner"]
        if stop and stop.lower() != "lightning bolt" \
                and str(owner) == "1":
            ass["A4_exile_until"] = "passed"
            notes.append(f"A4: exile-until stopped at {stop} from P1's "
                         f"library (nonland, different name than Bolt)")
        elif ST["exile_events"]:
            ass["A4_exile_until"] = "failed"
            notes.append(f"A4 FAILED: exile events={ST['exile_events']}; "
                         f"stop={stop} owner={owner} (expected a non-Bolt "
                         f"nonland from P1's library)")
        else:
            ass["A4_exile_until"] = "failed"
            notes.append("A4 FAILED: no cards exiled - the chain stopped "
                         "after the counter (reported bug)")
    else:
        ass["A4_exile_until"] = "not-run"
        notes.append("A4 not-run: counter leg not established")

    # ---- A5: free cast ----
    if ass["A4_exile_until"] == "passed":
        if ST["free_on_battlefield"]:
            ass["A5_free_cast"] = "passed"
            notes.append(f"A5: exiled {ST['free_name']} cast for free "
                         f"(offer={ST['free_cast_offer_shape']}) and "
                         f"reached P1's battlefield")
        elif ST["free_cast_offered"]:
            ass["A5_free_cast"] = "not-run"
            notes.append(f"A5 not-run: free-cast offer appeared "
                         f"({ST['free_cast_offer_shape']}) but the accept "
                         f"branch could not be driven to the battlefield")
        else:
            ass["A5_free_cast"] = "failed"
            notes.append(f"A5 FAILED: {ST['free_name']} exiled but no "
                         f"free-cast offer ever appeared")
    else:
        ass["A5_free_cast"] = "not-run"
        notes.append("A5 not-run: exile-until leg not established")

    # ---- A6: bottom rest + conservation ----
    if ass["A4_exile_until"] == "passed" and post is not None:
        p1exile = [str(o.get("card_name") or o.get("name") or "")
                   for o in (post.get("objects") or {}).values()
                   if o.get("zone") == "Exile"
                   and str(o.get("owner", -1)) == "1"]
        p0exile = [str(o.get("card_name") or o.get("name") or "")
                   for o in (post.get("objects") or {}).values()
                   if o.get("zone") == "Exile"
                   and str(o.get("owner", -1)) == "0"]
        total_p1 = sum(1 for o in (post.get("objects") or {}).values()
                       if str(o.get("owner", -1)) == "1")
        if not p1exile and not p0exile and total_p1 == 60:
            ass["A6_bottom_rest"] = "passed"
            notes.append("A6: exile zones empty; P1 card conservation "
                         "holds (60/60) - exiled cards bottomed")
        else:
            ass["A6_bottom_rest"] = "failed"
            notes.append(f"A6 FAILED: p1_exile={p1exile} p0_exile={p0exile} "
                         f"p1_total={total_p1}/60")
    else:
        ass["A6_bottom_rest"] = "not-run"
        notes.append("A6 not-run: exile-until leg not established")

    # ---- A7: cleanup ----
    post_ok = post is not None
    if ST["game_over"] and not post_ok:
        ok = not ST["stall_observed"]
        ass["A7_cleanup"] = "passed" if ok else "failed"
        notes.append("A7: game over (post unexportable); "
                     f"stall={ST['stall_observed']}")
    elif post_ok:
        ok = (not ST["stall_observed"]
              and len(ST["turns_seen"]) >= 3
              and (len(post.get("stack") or []) == 0 or ST["game_over"]))
        ass["A7_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A7: stall={ST['stall_observed']} "
                     f"turns_seen={len(ST['turns_seen'])} "
                     f"stack_empty={len(post.get('stack') or []) == 0}")
    else:
        ass["A7_cleanup"] = "failed"
        notes.append("A7 failed: post.json missing")

    for k, v in ass.items():
        say(f"{k}: {v}")

    # ---- verdict ----
    a = ass
    if a["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif a["A2_countered"] == "passed" and a["A3_mill"] == "failed":
        verdict = "reproduced"
    elif a["A2_countered"] == "passed" and a["A4_exile_until"] == "failed":
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in (
            "A1_setup_ok", "A2_countered", "A3_mill", "A4_exile_until",
            "A5_free_cast", "A6_bottom_rest")):
        verdict = "not-reproduced"
    elif (a["A2_countered"] == "passed" and a["A3_mill"] == "passed"
          and a["A4_exile_until"] == "passed"
          and a["A5_free_cast"] == "not-run"):
        # chain executed through the offer; only the accept-branch
        # recognizer could not fire - the reported "nothing after counter"
        # is disproven by the mill + exile.
        verdict = "not-reproduced"
        notes.append("limitation: free-cast offer appeared but the accept "
                     "branch was not driven (unrecognized offer shape)")
    elif ST["trickery_cast"] and not ST["trickery_resolved"]:
        verdict = "blocked"
        notes.append("blocked: Trickery cast but resolution never observed")
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    run = {
        "issue": ISSUE,
        "title": "Tibalt's Trickery does not work at all, other than "
                 "countering a spell",
        "run_id": RUN_ID,
        "validated_at": "2026-10-15",
        "validated_version": "v0.110.0",
        "server": SERVER_IDENTITY,
        "server_hello_observed": ST.get("server_hello"),
        "protocol_version": 142,
        "scope": "Native engine, two human-client seats; P1 casts Lightning "
                 "Bolt at P0, P0 counters with Tibalt's Trickery; the "
                 "post-counter chain (random mill, exile-until, free cast, "
                 "bottom rest) is exercised to explicit assertions. "
                 "Re-validation of 20260914-7166f (v0.83.0/protocol 70, "
                 "reproduced) on the v0.110.0 pin.",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": "; ".join(f"{k}: {v}" for k, v in ass.items()),
        "parse_chain": ST.get("parse_chain"),
        "driver_state": {
            "bolt_oid": ST["bolt_oid"],
            "bolt_stack_id": ST.get("bolt_stack_id"),
            "trickery_oid": ST["trickery_oid"],
            "trickery_stack_id": ST["trickery_stack_id"],
            "trickery_target_record": ST.get("trickery_target_record"),
            "stack_target_evidence": ST.get("stack_target_evidence"),
            "resolve_turn": ST["resolve_turn"],
            "mill_count": ST["mill_count"],
            "exile_events": ST["exile_events"],
            "exile_stop_name": ST["exile_stop_name"],
            "exile_library_owner": ST["exile_library_owner"],
            "free_name": ST.get("free_name"),
            "free_cast_oid": ST.get("free_cast_oid"),
            "free_cast_offered": ST["free_cast_offered"],
            "free_cast_offer_shape": ST["free_cast_offer_shape"],
            "free_cast_done": ST["free_cast_done"],
            "free_on_battlefield": ST["free_on_battlefield"],
            "p1_discards": ST["p1_discards"],
            "turns_seen": sorted(ST["turns_seen"]),
            "rejections": ST["rejections"][:10],
            "wf_kinds_seen": WF_SEEN,
            "unexpected": ST["unexpected"],
        },
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Isolated single-user server started fresh by this run with its "
            "own games.db; binary identity re-verified at run start "
            "(sha256 matches the signed v0.110.0 manifest).",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
        "done_reason": ST.get("done_reason"),
        "prior_run": {
            "run_id": "20260914-7166f",
            "validated_version": "v0.83.0",
            "verdict": "reproduced",
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # render summary.png via the standalone renderer (reads run.json +
    # saved states; must run before manifest)
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable,
             f"{BACKFILL}/driver/render_summary_7166.py", EVDIR],
            capture_output=True, text=True, timeout=120)
        say(r.stdout.strip())
        if r.returncode != 0:
            say("renderer failed: " + r.stderr[-500:])
            wire("renderer_failed", {"stderr": r.stderr[-500:]})
    except Exception as e:
        say(f"renderer error: {e}")
        wire("renderer_error", {"error": str(e)[:200]})

    # manifest LAST (after all logging is done) - no say()/wire() calls
    # may happen after this point or the hashed logs will drift.
    say(f"writing manifest; done: {ST.get('done_reason')}")
    RUNLOG.flush()
    WIRE.flush()
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            h = hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            f.write(f"{h}  {fn}\n")
    RUNLOG.flush()
    WIRE.flush()
    return verdict, ass


# ---------------- main -----------------------------------------------------
async def main():
    reset()
    global C0, C1
    await verify_server_hello()
    parse_check()

    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=2)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "run_id": RUN_ID})

    t0 = time.time()
    last_rev_seen = {0: -1, 1: -1}
    last_rev_time = time.time()
    stall_dumped = False
    try:
        while time.time() - t0 < TIMEOUT and not ST["stop"]:
            await tick(C0, 0)
            await tick(C1, 1)
            st = C0.latest
            if st:
                state = st["state"]
                # P0's counter-readiness from P0's own (unfiltered) view;
                # P1's viewer-filtered state hides P0's hand.
                ST["p0_can_answer"] = (
                    find_hand(state, 0, TRICKERY) is not None
                    and untapped_land_count(state, 0) >= 2)
                turn = state.get("turn_number") or 0
                if turn >= 1:
                    ST["game_started"] = True
                    ST["turns_seen"].add(turn)
                await observe(state)
            # stall watchdog: no revision progress on either client for 90s
            cur0 = (C0.latest or {}).get("state_revision", -1)
            cur1 = (C1.latest or {}).get("state_revision", -1)
            if cur0 != last_rev_seen[0] or cur1 != last_rev_seen[1]:
                last_rev_seen = {0: cur0, 1: cur1}
                last_rev_time = time.time()
                stall_dumped = False
            elif ST["game_started"] and time.time() - last_rev_time > 90 \
                    and not stall_dumped:
                stall_dumped = True
                ST["stall_observed"] = True
                say("STALL WATCHDOG: no revision progress for 90s; "
                    "dumping client views")
                wire("stall_watchdog", {"rev0": cur0, "rev1": cur1})
                for cc, tag in ((C0, "P0"), (C1, "P1")):
                    sst = cc.latest or {}
                    sstate = (sst.get("state") or {})
                    aats = sorted(set(a.get("type")
                                       for a in merged_actions(sst)))
                    vvi = sst.get("viewer_interaction") or {}
                    opps = []
                    for o in (vvi.get("opportunities") or [])[:4]:
                        rr = o.get("response", {}) or {}
                        cchs = (rr.get("data") or {}).get("choices") or \
                            (rr.get("data") or {}).get("candidates") or []
                        opps.append({
                            "iid": str(o.get("interactionId"))[:16],
                            "rtype": rr.get("type"),
                            "choices": [
                                {"id": str(ch.get("id"))[:16],
                                 "action_codes": action_type_codes(ch),
                                 "text": choice_text(ch)[:60]}
                                for ch in cchs]})
                    dump = {
                        "rev": sst.get("state_revision"),
                        "turn": sstate.get("turn_number"),
                        "phase": sstate.get("phase"),
                        "active": sstate.get("active_player"),
                        "stack_n": len(sstate.get("stack") or []),
                        "atypes": aats,
                        "vi_kind": (vvi.get("waitingForKind") or {})
                        .get("code"),
                        "vi_canSubmit": vvi.get("canSubmit"),
                        "vi_opps": opps,
                    }
                    say(f"[{tag}] STALLDUMP " + json.dumps(dump)[:1500])
                    wire("stall_dump", {"who": tag, "dump": dump})
                ST["stop"] = True
            await asyncio.sleep(0.25)
    except Exception as e:
        say(f"main loop error: {e}")
        wire("main_error", {"error": str(e)[:300]})
    finally:
        if not ST.get("finalized"):
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"pre-close post export failed: {e}")
                wire("post_export_failed", {"error": str(e)[:200]})
        try:
            await C0.close()
        except Exception:
            pass
        try:
            await C1.close()
        except Exception:
            pass
    return await finalize()


if __name__ == "__main__":
    verdict, ass = asyncio.run(main())
    print(f"FINAL verdict={verdict} "
          + " ".join(f"{k}={v}" for k, v in ass.items()))
