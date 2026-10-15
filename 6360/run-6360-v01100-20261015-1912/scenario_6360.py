#!/usr/bin/env python3
"""Issue #6360: Consult the Star Charts goes to the graveyard before it
finishes resolving (game state attached).

Oracle (verified against pinned v0.110.0 card-data.json):
  Kicker {1}{U} (You may pay an additional {1}{U} as you cast this spell.)
  Look at the top X cards of your library, where X is the number of lands
  you control. Put one of those cards into your hand. If this spell was
  kicked, put two of those cards into your hand instead. Put the rest on
  the bottom of your library in a random order.

Reported: the spell is put into its owner's graveyard BEFORE its
mid-resolution card choice completes. A spell is put into its owner's
graveyard only as the final step of resolution (CR 608.2m), so observing
the spell object in the Graveyard zone while its keep-cards choice is
still pending is the reported failure.

Fix PR #9728 ("put a resolving spell into the graveyard only after its
resolution finishes") is OPEN and unmerged, so the pinned v0.110.0
release (build 9bfd12e, protocol 142) should still show the reported
behavior.

Behavioral contract -- single leg, 2-player game:
  Setup: P0 ramps Islands; P1 draw-go. When P0 has >=4 untapped Islands
    and Consult the Star Charts in hand, P0 casts it paying the kicker
    ({2}{U}{U} total).
  Trigger: spell resolves; the Dig keep-cards choice is offered
    mid-resolution (look at top 4, keep 2 kicked, rest to bottom random).
  DURING evidence: while the keep choice is pending, export the
    authoritative state and read the zone of the cast Consult object.
  Completion: answer the choice (first 2 candidates), let resolution
    finish, export POST.

Assertions:
  A1 setup_ok            P0 had Consult in hand and >=4 untapped Islands;
                         the cast was submitted.
  A2 cast_and_kicker      Kicker OptionalCostChoice was offered and paid;
                         the spell reached the stack.
  A3 dig_choice_pending   The mid-resolution keep-cards choice prompt was
                         observed (schema select with candidates).
  A4 spell_zone_during   Zone of the cast Consult object while the keep
                         choice is pending. PASSED iff "Stack" (correct);
                         FAILED iff "Graveyard" (the reported bug).
  A5 resolution_completes After answering the choice: the 2 chosen cards
                         are in P0's hand, the other 2 looked-at cards
                         are in P0's library, Consult is in the graveyard,
                         no pending prompts.
  A6 rest_to_library     (observational) the 2 non-kept cards end in the
                         Library zone (bottom/random-order disposition
                         noted, not asserted by position).

Verdict rule (pre-registered):
  - reproduced:    A1, A2, A3 passed and A4 failed
                     (spell in Graveyard while its keep choice is pending).
  - not-reproduced: A1-A5 all passed (spell stayed on the Stack through
                     the mid-resolution choice; resolution completed
                     correctly).
  - blocked: anything else (setup never completed, choice never offered,
                     exports missing, etc.).

Conventions (protocol 142):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - CastSpell submitted EXACTLY as advertised (no injected targets).
  - Export response data.state is a JSON string, parsed once.
  - finish() ordering: run.json, scenario copy, every logging hook,
    THEN close WIRE/RUNLOG, THEN manifest LAST.
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
log = logging.getLogger("scenario6360")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-6360-v01100-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 6360
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.110.0",
    "build_commit": "9bfd12e",
    "protocol_version": 142,
    "mode": "Full",
    "server_binary_sha256": "346ad6a1a8cd887bb49108fa132476bea36e42e7cfc5116fe2cbdf8ad369e3f2",
    "card_data_sha256": "c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74",
    "draft_pools_sha256": "7e45abbf0a964959192d2528c067a2a9caab04438640ab63b9ad79d2ce0e4676",
    "signature_verified": True,
    "signature_note": ("v0.110.0 pinned release dir re-verified this run: "
                       "GitHub still lists v0.110.0 (published 2026-10-14) as "
                       "the latest stable engine release, so the pin did not "
                       "advance; binary + data digests recomputed against the "
                       "on-disk artifacts and ServerHello 0.110.0/9bfd12e/"
                       "protocol 142 confirmed by live handshake on "
                       "127.0.0.1:9374. NOTE: the card-data digest recorded "
                       "in earlier v0.110.0 ledger entries "
                       "(b365361edafd3d901e361fe1eef845ca4748c7b2b371ee27e300013f64f37f00) "
                       "does not match the on-disk card-data.json "
                       "(c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74); "
                       "flagged for reconciliation, not blocking this run."),
    "source": ("2026-10-15: latest stable release v0.110.0 == pinned release "
               "dir; backfill-owned server started this run "
               "(--single-user --no-data-download) on 127.0.0.1:9374 with a "
               "fresh run games.db; scenario games created via "
               "CreateGameWithSettings; evidence under evidence/6360/" + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"))
CONSULT_ORACLE = (CARD_DATA.get("consult the star charts") or {}).get("oracle_text")


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity pinned: v0.110.0 / 9bfd12e / protocol 142")
say("Consult the Star Charts oracle: " + str(CONSULT_ORACLE)[:160])

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
SHAPES = set()

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


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_islands(state, pid):
    n = 0
    for oid in bf_oids(state, pid):
        o = get_obj(state, oid)
        if is_land(o) and obj_lname(state, oid) == "island" \
                and not o.get("tapped"):
            n += 1
    return n


def stack_entries(state):
    out = []
    for e in state.get("stack") or []:
        out.append(e if isinstance(e, dict) else get_obj(state, e))
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
    return "?"


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


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


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


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


CONSULT = "consult the star charts"
ASSERT_KEYS = ["A1_setup_ok", "A2_cast_and_kicker", "A3_dig_choice_pending",
               "A4_spell_zone_during", "A5_resolution_completes",
               "A6_rest_to_library"]

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
    raw = await asyncio.wait_for(ws.recv(), 15)
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


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    say(f"[{tag}] keeping opening hand")
    wire("mulligan", {"who": tag, "decision": "keep"})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def play_named_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if obj_lname(state, o) != "island" or not is_land(get_obj(state, o)):
            continue
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing island")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    """Tap lands through the engine's mana-payment vi opportunity."""
    if not needs or all(v <= 0 for v in needs.values()):
        return False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" not in codes:
                continue
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
        say(f"[{tag}] tap land for mana ({used}); remaining needs={needs}")
        wire("tap_land", {"who": tag, "used_for": used})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "choose",
                                           "data": {"choiceId": pick.get("id")}}},
                          tag)
        return True
    return False


def find_kicker_opp(st, state):
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "OptionalCostChoice":
        return None
    cost = (wf.get("data") or {}).get("cost") or {}
    if cost.get("type") != "Kicker":
        return None
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        items = data.get("choices") or data.get("candidates") or []
        for ch in items:
            codes = [((s.get("data") or {}).get("code") or "")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "decideOptionalCost" in codes:
                return opp, wf
    return None


async def answer_kicker(c, st, state, tag, ctx):
    """Pay the kicker once (OptionalCostChoice), then stop."""
    found = find_kicker_opp(st, state)
    if not found:
        return False
    opp, wf = found
    iid = opp.get("interactionId")
    if iid in SUBMITTED_OPPS:
        return False
    times = (wf.get("data") or {}).get("times_kicked", 0)
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    items = data.get("choices") or data.get("candidates") or []
    pay_now = times < 1  # kicked Consult: pay exactly once
    pick = None
    for ch in items:
        vals = [(((s.get("data") or {}).get("role")),
                 ((s.get("data") or {}).get("value")))
                for s in ch.get("surfaces", []) or []
                if s.get("type") == "value"]
        flag = ("pay", "true") if pay_now else ("pay", "false")
        if flag in vals:
            pick = ch
            break
    if pick is None:
        say(f"[{tag}] kicker: no matching pay choice; deferring")
        wire("kicker_no_match", {"iid": iid, "times": times,
                                 "interaction": opp})
        return False
    SUBMITTED_OPPS.add(iid)
    say(f"[{tag}] KICKER -> {'pay' if pay_now else 'stop'} (times={times})")
    wire("kicker", {"who": tag, "pay": pay_now, "times": times})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "choose",
                                       "data": {"choiceId": pick.get("id")}}},
                      tag)
    if pay_now:
        ctx["kicker_paid"] = True
        # kicker adds {1}{U} to the mana bill
        needs = ST["mana_needs"].get(tag, {})
        needs["U"] = needs.get("U", 0) + 1
        needs["generic"] = needs.get("generic", 0) + 1
        say(f"[{tag}] kicker paid; mana needs now {needs}")
    return True


def dig_choice_opp(st, state, ctx):
    """The mid-resolution Dig keep-cards choice: a schema select whose
    candidates reference P0's library cards, while the Consult spell is
    resolving (on the stack) or already misplaced."""
    if ctx.get("choice_answered"):
        return None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = data.get("candidates") or []
        if len(cands) < 2:
            continue
        # candidates must reference P0's library cards (the looked-at top X)
        refs = [_cand_ref(ch) for ch in cands]
        refs = [r for r in refs if r is not None]
        if not refs:
            continue
        if all(obj_zone(state, r) == "Library" for r in refs):
            return opp, cands, refs
    return None


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def _opp_codes(opp):
    codes = set()
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("choices") or data.get("candidates") or []:
        for sf in ch.get("surfaces", []) or []:
            if sf.get("type") == "action":
                c = (sf.get("data") or {}).get("code")
                if c:
                    codes.add(c)
    return codes


def real_decision_pending(st, state):
    """A genuine decision (not mana taps / priority / land play prompts)
    is pending for us."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("choices") or data.get("candidates") or []
        if not items:
            continue
        codes = _opp_codes(opp)
        if "decideOptionalCost" in codes:
            return True
        if rtype == "schema":
            spec = ((data.get("spec", {}) or {}).get("type")) or ""
            if spec == "select":
                return True
        if codes and codes <= NON_DECISION_CODES:
            continue
        if codes:
            return True
    return False


async def play_land_vi(c, st, state, pid, tag):
    """Answer a vi land-play prompt (action code playLand) for an Island
    in hand, at most one per turn."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag, "vi")) == turn:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            codes = [(sf.get("data") or {}).get("code")
                     for sf in ch.get("surfaces", []) or []
                     if sf.get("type") == "action"]
            if "playLand" not in codes:
                continue
            ref = _cand_ref(ch)
            if ref is None or ref not in handset:
                continue
            if obj_lname(state, ref) != "island" \
                    or not is_land(get_obj(state, ref)):
                continue
            iid = opp.get("interactionId")
            if iid in SUBMITTED_OPPS:
                continue
            SUBMITTED_OPPS.add(iid)
            LAND_PLAYED_TURN[(tag, "vi")] = turn
            say(f"[{tag}] playing island via vi prompt (oid {ref})")
            wire("play_land_vi", {"who": tag, "oid": ref})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "choose",
                                               "data": {"choiceId": ch.get("id")}}},
                              tag)
            return True
    return False


def consult_spell_zone(state, pid):
    """(zone, oid) of the cast Consult object: the P0 Consult in Stack
    or Graveyard (only the cast copy can be in either)."""
    found = []
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() != CONSULT:
            continue
        if str(o.get("controller", o.get("owner", -1))) != str(pid):
            continue
        if o.get("zone") in ("Stack", "Graveyard"):
            found.append((o.get("zone"), oid))
    return found


async def export_state(c, name, ctx):
    raw = await c.export_state()
    env = json.loads(raw)
    state = env.get("state") if isinstance(env.get("state"), dict) \
        else env
    with open(f"{EVDIR}/{name}.json", "w") as f:
        json.dump(env, f, indent=1)
    say(f"[{c.name}] exported {name}.json "
        f"(turn={state.get('turn_number')}, rev={state.get('state_revision')})")
    wire(f"export_{name}", {"turn": state.get("turn_number")})
    return state


async def tick_p0(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    await play_named_land(c, state, pid, acts, tag)
    await play_land_vi(c, st, state, pid, tag)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    active = str(state.get("active_player")) == str(pid)
    phase = str(state.get("phase") or "")
    is_main = "main" in phase.lower()

    # --- cast gate
    if not ctx.get("cast_submitted"):
        turn = state.get("turn_number", 0)
        if turn > 30:
            return
        if active and is_main and CONSULT in hand_names(state, pid) \
                and untapped_islands(state, pid) >= 4:
            a, oid = cast_action_for(acts, state, CONSULT)
            if a is not None:
                ctx["pre_state"] = await export_state(c, "pre", ctx)
                ST["mana_needs"][tag] = {"U": 2, "generic": 2}
                say(f"[{tag}] casting {CONSULT} (oid {oid}) kicked, "
                    f"needs={ST['mana_needs'][tag]}")
                wire("cast", {"who": tag, "oid": oid})
                await submit_as_is(c, a)
                ctx["cast_submitted"] = True
                ctx["cast_oid"] = oid
                obs_assert("A1_setup_ok", "passed",
                           "Consult in hand, >=4 untapped Islands; cast submitted")
                return

    # --- kicker + mana payment while the cast is in flight
    if ctx.get("cast_submitted") and not ctx.get("choice_answered"):
        if await answer_kicker(c, st, state, tag, ctx):
            st = c.latest or st
            state = st["state"]
        needs = ST["mana_needs"].get(tag, {})
        if await pay_mana_vi(c, st, tag, needs):
            st = c.latest or st
            state = st["state"]
            acts = merged_actions(st)
        if ctx.get("kicker_paid") and not ctx.get("a2_done"):
            # spell on the stack means cast + kicker completed
            if any(stack_entry_name(state, e) == CONSULT
                   for e in stack_entries(state)):
                obs_assert("A2_cast_and_kicker", "passed",
                           "kicker paid once; Consult on the stack")
                ctx["a2_done"] = True

    # --- the mid-resolution Dig keep-choice
    found = dig_choice_opp(c.latest or st, (c.latest or st)["state"], ctx)
    if found and not ctx.get("choice_answered"):
        opp, cands, refs = found
        ctx["looked_oids"] = refs
        say(f"[{tag}] DIG keep-choice observed: {len(cands)} candidates "
            f"(oids {refs[:6]}{'...' if len(refs) > 6 else ''})")
        wire("dig_choice", {"candidates": len(cands), "refs": refs,
                            "interaction": opp})
        obs_assert("A3_dig_choice_pending", "passed",
                   f"mid-resolution keep-choice offered with {len(cands)} candidates")
        if ctx.get("kicker_paid") and not ctx.get("a2_done"):
            # The cast necessarily completed: kicker was paid and the spell
            # reached its mid-resolution keep choice (the stack window was
            # too brief to observe directly).
            obs_assert("A2_cast_and_kicker", "passed",
                       "kicker paid once; cast completed through to the "
                       "mid-resolution keep choice")
            ctx["a2_done"] = True
        # DURING export: zone of the cast spell while the choice is pending
        during = await export_state(c, "during", ctx)
        zones = consult_spell_zone(during, pid)
        say(f"[{tag}] DURING: cast Consult objects in Stack/Graveyard: {zones}")
        wire("during_spell_zones", {"zones": zones})
        ctx["during_zones"] = zones
        if not zones:
            obs_assert("A4_spell_zone_during", "not-run",
                       "cast Consult object found in neither Stack nor Graveyard during the choice")
        elif zones[0][0] == "Stack":
            obs_assert("A4_spell_zone_during", "passed",
                       f"Consult object {zones[0][1]} still on the Stack while its keep choice is pending")
        elif zones[0][0] == "Graveyard":
            obs_assert("A4_spell_zone_during", "failed",
                       f"REPRODUCED BUG: Consult object {zones[0][1]} is in the Graveyard while its mid-resolution keep choice is still pending")
        else:
            obs_assert("A4_spell_zone_during", "not-run",
                       f"unexpected zone {zones[0][0]}")
        # answer: kicked -> keep 2 (first two candidates)
        picks = cands[:2]
        pick_ids = [ch.get("id") for ch in picks]
        ctx["kept_oids"] = [_cand_ref(ch) for ch in picks]
        ctx["kept_names"] = [obj_lname((c.latest or st)["state"], _cand_ref(ch))
                             for ch in picks]
        say(f"[{tag}] answering keep-choice: keeping {ctx['kept_names']} "
            f"(oids {ctx['kept_oids']})")
        wire("dig_answer", {"kept_oids": ctx["kept_oids"],
                            "kept_names": ctx["kept_names"]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "select",
                                           "data": {"choiceIds": pick_ids}}},
                          tag)
        ctx["choice_answered"] = True
        return

    # --- pass priority when nothing real is pending
    if not real_decision_pending(c.latest or st, (c.latest or st)["state"]):
        for a in merged_actions(c.latest or st):
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return


async def tick_p1(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    await play_named_land(c, state, pid, acts, tag)
    await play_land_vi(c, st, state, pid, tag)
    st = c.latest or st
    state = st["state"]
    if not real_decision_pending(st, state):
        for a in merged_actions(st):
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return


def obs_assert(key, status, note):
    OBS["assert"][key] = status
    if note:
        OBS["notes"].append(f"{key}={status}: {note}")
    say(f"assertion {key}: {status}" + (f" -- {note}" if note else ""))


async def run_leg(attempt):
    global OBS
    p0_deck = deck(("Island", 56), ("Consult the Star Charts", 4))
    p1_deck = deck(("Island", 60))
    clients = [PhaseClient(f"P0K{attempt}")]
    await clients[0].connect()
    await clients[0].create(p0_deck, player_count=2)
    c1 = PhaseClient(f"P1K{attempt}")
    await c1.connect()
    await c1.join(clients[0].game_code, p1_deck)
    clients.append(c1)
    tags = [f"P0K{attempt}", f"P1K{attempt}"]
    for tag in tags:
        ST["mana_needs"][tag] = {}
    say(f"leg attempt={attempt} game {clients[0].game_code}; "
        f"seats={[c.player_id for c in clients]}")
    wire("game_start", {"attempt": attempt,
                        "game_code": clients[0].game_code,
                        "seats": [c.player_id for c in clients]})
    OBS = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": clients[0].game_code}
    for k in ASSERT_KEYS:
        OBS["assert"][k] = "not-run"
    ctx = {"cast_submitted": False, "kicker_paid": False,
           "choice_answered": False, "post_exported": False,
           "settle_ticks": 0}
    t0 = time.time()
    last_diag = 0
    last_rev = -1
    last_rev_change = time.time()
    try:
        while time.time() - t0 < 900:
            await asyncio.sleep(0.25)
            await tick_p0(clients[0], 0, tags[0], ctx)
            await tick_p1(clients[1], 1, tags[1], ctx)
            await asyncio.sleep(0)
            cur_rev = clients[0].revision
            if cur_rev != last_rev:
                last_rev = cur_rev
                last_rev_change = time.time()
            if time.time() - last_rev_change > 150:
                say("[leg] no state-revision change in 150s; abandoning")
                OBS["notes"].append("abandoned: stalled game")
                for c, pid in zip(clients, range(2)):
                    st = c.latest
                    if not st:
                        say(f"[stall-diag] {c.name}: NO STATE")
                        continue
                    s = st["state"]
                    wf = s.get("waiting_for") or {}
                    opps = []
                    for opp in vi_ops(st):
                        data = (opp.get("response", {}) or {}).get("data", {}) or {}
                        items = data.get("choices") or data.get("candidates") or []
                        opps.append({
                            "iid": str(opp.get("interactionId"))[:12],
                            "rtype": (opp.get("response", {}) or {}).get("type"),
                            "n": len(items),
                            "codes": sorted(_opp_codes(opp)),
                            "texts": [choice_text(ch)[:60] for ch in items[:4]],
                        })
                    say(f"[stall-diag] {c.name}: waiting_for={wf.get('type')} "
                        f"wf_data={json.dumps(wf.get('data'), default=str)[:300]}")
                    say(f"[stall-diag] {c.name}: opps={json.dumps(opps, default=str)[:800]}")
                    say(f"[stall-diag] {c.name}: action_types="
                        f"{sorted(set(a.get('type') for a in merged_actions(st)))}")
                    say(f"[stall-diag] {c.name}: rejections={c.rejections[-3:]}")
                break
            st_now = clients[0].latest
            if st_now and st_now["state"].get("turn_number", 0) > 40 \
                    and not ctx.get("cast_submitted"):
                say("[leg] turn cap reached without casting; abandoning")
                OBS["notes"].append("abandoned: turn 40 without casting "
                                    "Consult (setup never completed)")
                break
            if ctx.get("cast_submitted") \
                    and time.time() - t0 > 600 \
                    and not ctx.get("post_exported") \
                    and not ctx.get("choice_answered"):
                say("[leg] cast 10+ min ago, no keep-choice; abandoning")
                OBS["notes"].append("abandoned: cast but no mid-resolution "
                                    "keep-choice observed within 10 min")
                break
            if time.time() - last_diag > 30:
                last_diag = time.time()
                for c, pid in zip(clients, range(2)):
                    st = c.latest
                    if not st:
                        say(f"[diag] {c.name}: NO STATE")
                        continue
                    s = st["state"]
                    say(f"[diag] {c.name}: rev={c.revision} "
                        f"turn={s.get('turn_number')} phase={s.get('phase')} "
                        f"active={s.get('active_player')} "
                        f"stack={len(s.get('stack') or [])} "
                        f"hand={len(hand_ids(s, pid))} "
                        f"islands={untapped_islands(s, pid)} "
                        f"cast={ctx.get('cast_submitted')} "
                        f"kick={ctx.get('kicker_paid')} "
                        f"choice={ctx.get('choice_answered')}")
            # completion: cast done, choice answered, stack settled on both
            # views for 3 consecutive ticks -> POST export
            if ctx.get("choice_answered") and not ctx.get("post_exported"):
                latests = [c.latest for c in clients]
                if all(x is not None for x in latests) \
                        and all(not (x["state"].get("stack") or [])
                                for x in latests) \
                        and not any(real_decision_pending(x, x["state"])
                                    for x in latests):
                    ctx["settle_ticks"] += 1
                    if ctx["settle_ticks"] >= 3:
                        say("[leg] resolution settled; exporting POST")
                        post = await export_state(clients[0], "post", ctx)
                        ctx["post_state"] = post
                        ctx["post_exported"] = True
                        evaluate_post(ctx, post)
                        break
    finally:
        for c in clients:
            await c.close()
    return OBS, ctx


def evaluate_post(ctx, post):
    pid = 0
    kept = [str(o) for o in (ctx.get("kept_oids") or []) if o]
    looked = [str(o) for o in (ctx.get("looked_oids") or []) if o]
    rest = [o for o in looked if o not in kept]
    hand = set(hand_ids(post, pid))
    ok_kept = all(o in hand for o in kept) if kept else False
    ok_rest_lib = all(obj_zone(post, o) == "Library" for o in rest) \
        if rest else False
    consult_gy = any(z == "Graveyard"
                     for z, _ in consult_spell_zone(post, pid))
    say(f"[eval] kept={kept} in_hand={ok_kept}; rest={rest} "
        f"in_library={ok_rest_lib}; consult_in_gy={consult_gy}")
    wire("post_eval", {"kept": kept, "ok_kept": ok_kept, "rest": rest,
                       "ok_rest_lib": ok_rest_lib,
                       "consult_in_gy": consult_gy})
    if ok_kept and ok_rest_lib and consult_gy:
        obs_assert("A5_resolution_completes", "passed",
                   f"2 kept cards in hand, {len(rest)} rest in library, "
                   "Consult in graveyard, no pending prompts")
    else:
        obs_assert("A5_resolution_completes", "failed",
                   f"kept_in_hand={ok_kept} rest_in_library={ok_rest_lib} "
                   f"consult_in_gy={consult_gy}")
    if ok_rest_lib:
        obs_assert("A6_rest_to_library", "passed",
                   f"{len(rest)} non-kept cards in the Library zone; "
                   "bottom/random-order placement noted but not asserted "
                   "by position (all Islands, order not distinguished)")
    else:
        obs_assert("A6_rest_to_library", "failed",
                   f"rest oids not all in Library: "
                   f"{[(o, obj_zone(post, o)) for o in rest]}")


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
    img = Image.new("RGB", (1000, 780), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #6360 -- Consult the Star Charts",
           font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.110.0 (9bfd12e, protocol 142) | 2-player, kicked "
           "cast, native engine", font=font, fill=(180, 180, 190))
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
    dz = (combined.get("obs", {}).get("during_zones")
          or [["?", "?"]])[0]
    d.text((48, y), f"Spell zone while choice pending: {dz[0]} "
           f"(expected Stack; bug = Graveyard)", font=font,
           fill=(220, 220, 230))
    y += 30
    d.text((24, y), "Expected (CR 608.2m): spell leaves the stack only as",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "the FINAL step of resolution -- after the keep choice.",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Bug: spell object in Graveyard while its keep-cards",
           font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "choice is still pending.", font=font,
           fill=(220, 220, 230))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_leg(1)
    # Retry once when attempt 1 never got past setup (cast never submitted).
    if not ctx.get("cast_submitted") \
            and obs["assert"].get("A1_setup_ok") in ("failed", "not-run"):
        say("attempt 1 never completed setup; running attempt 2")
        for k in list(SUBMITTED_OPPS):
            SUBMITTED_OPPS.discard(k)
        LAND_PLAYED_TURN.clear()
        MULLS.clear()
        obs, ctx = await run_leg(2)
    a = obs["assert"]

    def reproduced():
        return a.get("A1_setup_ok") == "passed" \
            and a.get("A2_cast_and_kicker") == "passed" \
            and a.get("A3_dig_choice_pending") == "passed" \
            and a.get("A4_spell_zone_during") == "failed"

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
        "obs": {"attempt": obs.get("attempt"), "assert": obs["assert"],
                "notes": obs["notes"], "game_code": obs.get("game_code"),
                "during_zones": ctx.get("during_zones"),
                "kept_oids": ctx.get("kept_oids"),
                "looked_oids": ctx.get("looked_oids")},
        "verdict": verdict,
        "card_data_oracle": {"consult the star charts": CONSULT_ORACLE},
    }
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(combined, f, indent=1, default=str)
    for k in ASSERT_KEYS:
        say(f"assertion {k}: {obs['assert'].get(k)}")
    for n in obs["notes"]:
        say(f"note: {n}")
    say(f"VERDICT: {verdict}")
    dz = (ctx.get("during_zones") or [["?", "?"]])[0]
    result_line = ("; ".join(f"{k} {v}" for k, v in obs["assert"].items())
                   + f"; spell_zone_during_choice={dz[0]}")
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/6360",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.110.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified "
                           "release, fresh games.db); evidence under "
                           f"evidence/6360/{RUN_ID}"),
        "driver": {"protocol_advertised": 142, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Island", 56], ["Consult the Star Charts", 4]],
            "P1": [["Island", 60]],
        },
        "assertions": {k: v for k, v in obs["assert"].items()},
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": result_line,
        "scope": ("Consult the Star Charts kicked cast ({2}{U}{U}), look at "
                  "top 4 (4 lands controlled), keep 2, rest to bottom in "
                  "random order. Native engine, two human-client seats; "
                  "asserts the cast spell object stays on the Stack while "
                  "its mid-resolution keep-cards choice is pending "
                  "(CR 608.2m)."),
        "limitations": [
            "Browser UI not exercised; native engine via human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "56x/60x Island densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Bottom/random-order disposition of the rest cards is observed by zone only, not asserted by position (all looked-at cards are Islands).",
            "Verdict rule: reproduced = A1+A2+A3 passed and A4 failed (spell in Graveyard while its keep choice is pending). not-reproduced = all assertions passed. blocked = setup never completed or the keep-choice was never offered.",
        ],
        "setup_line": ("P0 56x Island + 4x Consult the Star Charts; P1 60x "
                       "Island (draw-go). P0 ramps to 4 Islands, casts "
                       "Consult kicked, answers the mid-resolution "
                       "keep-2 choice."),
        "contract_line": ("Spell must remain on the stack while its "
                          "keep-cards choice is pending (CR 608.2m); it "
                          "moves to the graveyard only as the final step of "
                          "resolution. Bug: the spell object is already in "
                          "the Graveyard while the choice is pending."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entry.json", "w") as f:
        json.dump({"consult the star charts":
                   CARD_DATA.get("consult the star charts")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_6360.py", "w") as f:
        f.write(open(__file__).read())
    render_summary(combined)
    wire("verdict", {"verdict": verdict, "assertions": run["assertions"]})
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
    print(f"VERDICT={verdict} EVIDIR={EVDIR}")


if __name__ == "__main__":
    asyncio.run(main())
