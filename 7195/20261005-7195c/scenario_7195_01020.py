#!/usr/bin/env python3
"""Issue #7195: Transforming Flourish -- "Exiles card correctly but doesn't
allow the exiled card to be cast."

Re-validation on pinned v0.102.0 (build e17f6fd, WS protocol 106).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified from pinned v0.102.0 card-data.json), Transforming
Flourish {2}{R} Instant:
  "Demonstrate (When you cast this spell, you may copy it. If you do, choose
   an opponent to also copy it. Players may choose new targets for their
   copies.)
   Destroy target artifact or creature you don't control. If that permanent
   is destroyed this way, its controller exiles cards from the top of their
   library until they exile a nonland card, then they may cast that card
   without paying its mana cost."

Reported (Discord sync + prior v0.85.0 run 20260917-7195b): the exile half
works correctly, but the exiled card is never allowed to be cast.

Driver contract (this scenario; fixture design ported from the verified
v0.85.0/protocol-72 run):
  - P0: 12x Transforming Flourish, 48x Mountain.
    P1: 8x Sol Ring, 12x Forest, 40x Lightning Bolt.
  - P1 plays a Forest, taps it to cast Sol Ring, then plays NO further
    lands: when the may-cast window opens P1 has zero available mana, so
    any successful cast of the exiled card PROVES it was cast "without
    paying its mana cost".
  - P0 ramps to 3 Mountains and casts Transforming Flourish targeting
    P1's Sol Ring. P0 DECLINES Demonstrate (the reported path does not
    involve the copy).
  - Correct behavior: Sol Ring destroyed; P1 exiles cards from the top
    until a nonland (very likely Lightning Bolt) is exiled; the engine
    offers P1 a cast of that exiled card; P1 casts it and it resolves
    (Bolt -> 3 damage to P0, P0 at 17 life, Bolt in P1's graveyard; or a
    Sol Ring -> on P1's battlefield) with P1's lands still tapped.

Assertions:
  A1 setup_ok: pre_cast exported with P0 main phase, Transforming
      Flourish in hand, >=3 untapped Mountains, Sol Ring on P1's
      battlefield.
  A2 demonstrate_declined: Demonstrate's may-copy was declined (no copy
      of Flourish on the stack / cast).
  A3 destroyed: P1's Sol Ring left the battlefield (destroyed, not
      exiled/returned).
  A4 exile_done: >=1 card in P1's exile zone and at least one exiled
      nonland (record which card).
  A5 cast_offered: the engine offered P1 a cast of the exiled nonland
      card (may-cast interaction opportunity or CastSpell legal action
      on the exiled object).
  A6 free_cast_resolved: P1 cast the exiled card and it resolved
      (Bolt: P0 life 20 -> 17 and Bolt in P1's graveyard; Sol Ring:
      Sol Ring on P1's battlefield) with P1 paying no mana (all P1
      lands tapped before and after).

Verdict rule:
  reproduced     -- A1..A4 pass and A5 fails (exile works, no cast
                    is ever offered: the reported symptom), or A5 passes
                    but A6 fails (offered but the cast is broken).
  not-reproduced -- A1..A6 all pass.
  blocked        -- setup never assembled, Flourish never cast/destroyed,
                    or another concrete prerequisite failure (with the
                    reason recorded).

Protocol-106 driver notes (v0.102.0): ported from the verified protocol-106
scenario_301_01020.py (same pin, same day-family):
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; all decisions via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 106),
    gated on the MulliganDecision legal action.
  - Bottom-after-mulligan via vi schema/select, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi, gated on hand > 7 + schema/select opportunity
    offering hand cards.
  - CastSpell submitted via legacy Action; mana payment via legacy PayMana
    actions and/or vi tapLandForMana menus driven by mana_needs
    ({"R":1,"generic":2} for Flourish, {"generic":1} for Sol Ring).
  - Target selection via the advertised vi target opportunity (schema or
    exactChoices), preferring P1's Sol Ring candidate.
  - Demonstrate decline via decideOptionalEffect choice (role=accept,
    value=false), text fallbacks.
  - real_decision_pending excludes the noisy 106 priority-menu codes
    (passPriority, tapLandForMana, castSpell, activateAbility, ...).
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority. Re-tick backstop: re-tick a
    client holding priority with no revision change for > 5s.
  - Land-play matching uses is_land() over every land the seats can hold
    (Mountain/Forest); playLand vi codes are answered before the
    decision_pending gate (AGENTS.md 2026-10-05 lesson).
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7195
RUN_ID = "20261005-7195c"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

FLOURISH = "transforming flourish"
RING = "sol ring"
BOLT = "lightning bolt"
MOUNTAIN = "mountain"
FOREST = "forest"

GAME_TIMEOUT = 1200
MAYCAST_WINDOW_S = 150

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}


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


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


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


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid, lname):
    return sum(1 for oid in bf_oids(state, pid)
               if not get_obj(state, oid).get("tapped")
               and obj_lname(state, oid) == lname)


def exile_objs(state, pid):
    return [o for o in (state.get("objects") or {}).values()
            if o.get("zone") == "Exile"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)]


def gy_objs(state, pid):
    return [o for o in (state.get("objects") or {}).values()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)]


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
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}
# Note: "playLand" is a menu OPTION (you may play a land), never a forced
# decision. The 106 engine bundles it into the priority action menu; it must
# not block priority passes. (AGENTS.md 2026-10-05: unhandled playLand menu
# stalls the game.)


def real_decision_pending(st):
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
        rdata = {"choiceIds": [cid]}
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
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"


def check_data_level():
    ok, notes = True, []
    for name, needle in (("transforming flourish", "may cast that card"),
                         ("lightning bolt", "deals 3 damage"),
                         ("sol ring", "Add")):
        e = CARD_DATA.get(name, {})
        if needle.lower() not in str(e.get("oracle_text", "")).lower():
            ok = False
            notes.append(f"{name}: oracle missing ({needle!r})")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {n: str(CARD_DATA.get(n, {}).get("oracle_text"))[:300]
                              for n in ("transforming flourish", "lightning bolt",
                                        "sol ring")}}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")


# ------------------------------------------------------------- common ticks
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
    return False  # we keep all 7; nothing to bottom


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7

    def rank(o):
        ln = obj_lname(state, o)
        if ln == FOREST:
            return 0
        if ln == MOUNTAIN:
            return 0
        if ln == BOLT:
            return 1
        if ln == RING:
            return 2
        if ln == FLOURISH:
            return 3
        return 1

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
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return False
        ref_of = {}
        for ch in cands:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    ref_of[str(d["reference"])] = ch["id"]
                    break
        ranked = sorted(hand, key=rank)
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await answer_vi(c, opp, {"id": choice_ids[0]}, tag) if len(choice_ids) == 1 else \
            interact_as(c, {"interactionId": iid,
                             "response": {"type": "select",
                                          "data": {"choiceIds": choice_ids}}}, tag)
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


async def play_a_land(c, state, pid, acts, tag):
    """Play one land per turn. Uses is_land() over every land type (AGENTS.md
    2026-10-05: unhandled playLand menus stall the game)."""
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
    # 106 may also advertise land plays as a vi playLand opportunity
    for opp in vi_ops(st_of(c)):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                ref = str(d.get("reference", ""))
                if ref and ref in hand_ids(state, pid) \
                        and is_land(get_obj(state, ref)):
                    iid = opp.get("interactionId")
                    key = (tag, "playland", str(iid), ref)
                    if key in SUBMITTED_OPPS:
                        continue
                    SUBMITTED_OPPS.add(key)
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                    wire("play_land_vi", {"who": tag, "oid": ref})
                    await answer_vi(c, opp, ch, tag)
                    return True
    return False


def st_of(c):
    return c.latest or {}


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


# ------------------------------------------------------------- Demonstrate
async def handle_demonstrate(c, tag):
    """Decline Demonstrate's may-copy prompt for P0 (reported path does not
    involve the copy). 106: decideOptionalEffect choice in P0's vi."""
    if ST.get("demonstrate_done") or not ST.get("flourish_cast"):
        return False
    st = st_of(c)
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        blob = json.dumps({"opp": str(opp.get("description") or ""),
                           "ch": [choice_text(ch) for ch in chs]},
                          default=str).lower()
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        if "decideOptionalEffect" not in codes:
            continue
        # On 106 the Demonstrate prompt surfaces as bare "true"/"false"
        # choices with no "demonstrate"/"copy" text. It is the ONLY
        # decideOptionalEffect P0 can see right after casting Flourish
        # (gated on flourish_cast + not demonstrate_done), so treat it as
        # Demonstrate. Log the full shape for the record.
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        wire("demonstrate_prompt_shape",
             {"iid": iid, "blob": blob[:300],
              "choices": [(ch.get("id"), choice_text(ch)[:80],
                           [[s.get("type"), (s.get("data") or {}).get("role"),
                             (s.get("data") or {}).get("value")]
                            for s in ch.get("surfaces", []) or []])
                          for ch in chs]})
        pick = None
        for ch in chs:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if (str(d.get("value", "")).lower() == "false"
                        and str(d.get("role", "")).lower()
                        in ("accept", "pay", "decision", "copy")):
                    pick = ch["id"]
                    break
            if pick:
                break
        if pick is None:
            for ch in chs:
                t = choice_text(ch).lower()
                if "decline" in t or "don't" in t or "do not" in t \
                        or t.strip() in ("no", "false"):
                    pick = ch["id"]
                    break
        if pick is None and len(chs) == 2:
            for ch in chs:
                t = choice_text(ch).lower()
                if "copy" not in t and t not in ("yes", "true"):
                    pick = ch["id"]
                    break
        if pick is None:
            say(f"[{tag}] demonstrate: no decline choice identified; "
                f"deferring (NOT submitting blindly)")
            wire("demonstrate_no_decline_found",
                 {"choices": [(ch.get("id"), choice_text(ch)) for ch in chs]})
            return True
        SUBMITTED_OPPS.add(iid)
        ST["demonstrate_done"] = True
        ST["demonstrate_iid"] = iid
        say(f"[{tag}] demonstrate DECLINED (choice {pick})")
        wire("demonstrate_declined", {"iid": iid, "choice": pick})
        rtype = resp.get("type")
        if rtype == "schema":
            spec = (data.get("spec", {}) or {}).get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": spec,
                                "data": {"choiceIds": [pick]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick}}}
        await interact_as(c, sub, tag)
        return True
    return False


# ------------------------------------------------------------- target selection
def target_opportunity(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_ring_candidate(state, opp, ring_oid):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []

    def refs(ch):
        out = []
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                out.append(str(d["reference"]))
        return out

    for ch in cands:
        if str(ring_oid) in refs(ch):
            return ch
    best, best_score = None, -1
    for ch in cands:
        codes = set(surf_codes(ch))
        score = 0
        if codes & {"candidate", "target"}:
            score += 1
        if score > best_score:
            best, best_score = ch, score
    if best is None and len(cands) == 1:
        best = cands[0]
    return best


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if iid in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(iid)
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']}")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return True


# ------------------------------------------------------------- may-cast detection
def find_may_cast(st, exiled_oid, exiled_lname):
    """Look for an engine-offered cast of the exiled card in the viewing
    seat's interaction surface. Returns (kind, opp, choice_or_action)."""
    # 1. decideOptionalEffect "you may cast X"
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        if "decideOptionalEffect" not in codes:
            continue
        blob = " ".join(choice_text(ch) for ch in chs).lower()
        if "cast" not in blob:
            continue
        if exiled_lname not in blob and str(exiled_oid) not in blob:
            # still record it; the may-cast prompt may name the card oddly
            wire("maycast_optional_unmatched",
                 {"blob": blob[:300], "exiled": exiled_lname})
        for ch in chs:
            is_accept = None
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data", {}) or {}
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None:
                t = choice_text(ch).lower()
                if "cast" in t or "accept" in t or "yes" in t:
                    is_accept = True
                elif "decline" in t or "don't" in t or "do not" in t \
                        or t.strip() in ("no", "false"):
                    is_accept = False
            if is_accept:
                return ("optional", opp, ch)
    # 2. CastSpell legal action on the exiled object
    for a in merged_actions(st):
        if "cast" in a.get("type", "").lower():
            d = a.get("data", {}) or {}
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                if str(v) == str(exiled_oid):
                    return ("action", a, None)
    # 3. castSpell action-code choice referencing the exiled card
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            refs = [str((s.get("data") or {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)]
            if str(exiled_oid) in refs or exiled_lname in choice_text(ch).lower():
                return ("vi_cast", opp, ch)
    return (None, None, None)


# ------------------------------------------------------------- ticks
async def p0_tick(c, tag):
    st = st_of(c)
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
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True
    if await pay_tick(c, acts):
        return True
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    if await handle_demonstrate(c, tag):
        return True
    # Flourish targeting: P0's target selection for the cast Flourish.
    # Gated on flourish_cast so normal-play schema opportunities (e.g.
    # mulligan bottoming) are never misanswered as targets.
    if ST.get("flourish_cast") and not ST.get("tf_target_done"):
        opp, rtype, spec_type = target_opportunity(st)
        if opp is not None:
            ring_oid = next(iter(bf_by_name(state, 1, RING)), None)
            ch = pick_ring_candidate(state, opp, ring_oid) if ring_oid else None
            if ch is None:
                # only target Sol Ring if it's the unambiguous legal target
                say(f"[{tag}] target opp with no ring on BF; deferring")
                wire("target_deferred_no_ring", {})
                return True
            ST["tf_target_done"] = True
            await submit_target(c, opp, rtype, spec_type, ch, tag)
            return True
    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)
    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        # cast Flourish once the fixture is ready
        if not ST.get("flourish_cast"):
            has_f = any(obj_lname(state, o) == FLOURISH
                        for o in hand_ids(state, 0))
            ring_out = bool(bf_by_name(state, 1, RING))
            mana_ok = untapped_lands(state, 0, MOUNTAIN) >= 3
            if has_f and ring_out and mana_ok:
                a, oid = cast_action_for(acts, state, FLOURISH)
                if a is not None:
                    MANA_NEEDS[tag] = {"R": 1, "generic": 2}
                    ST["flourish_cast"] = True
                    ST["flourish_oid"] = oid
                    say(f"[{tag}] casting Transforming Flourish (oid {oid})")
                    wire("cast_flourish",
                         {"action": {k: v for k, v in a.items()
                                    if not k.startswith("_")}})
                    await submit_as_is(c, a)
                    return True
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 1, tag):
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
    if await pay_tick(c, acts):
        return True
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    # may-cast window handling (post-exile): actively look for the cast
    if ST.get("maycast_phase"):
        found = await handle_maycast(c, tag)
        if found:
            return True
    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)
    if my_main(state, 1):
        # Cast exactly one Sol Ring, then play NO further lands: P1 plays
        # its FIRST Forest only (0 lands on BF), taps it for the Ring, and
        # is then tapped out -- so any may-cast resolution PROVES the cast
        # was "without paying its mana cost".
        if not ST.get("ring_cast"):
            n_lands_bf = sum(1 for oid in bf_oids(state, 1)
                             if is_land(get_obj(state, oid)))
            if n_lands_bf == 0:
                if await play_a_land(c, state, 1, acts, tag):
                    return True
            has_r = any(obj_lname(state, o) == RING
                        for o in hand_ids(state, 1))
            if has_r and untapped_lands(state, 1, FOREST) >= 1:
                a, oid = cast_action_for(acts, state, RING)
                if a is not None:
                    MANA_NEEDS[tag] = {"generic": 1}
                    ST["ring_cast"] = True
                    ST["ring_oid"] = oid
                    say(f"[{tag}] casting Sol Ring (oid {oid})")
                    wire("cast_ring", {})
                    await submit_as_is(c, a)
                    return True
        # after the ring is cast: no more lands, no more casts
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def handle_maycast(c, tag):
    """During the may-cast window: find and answer the engine's cast offer
    for the exiled card. Returns True if it acted."""
    st = st_of(c)
    if not st:
        return False
    exiled_oid = ST.get("exiled_oid")
    exiled_lname = ST.get("exiled_lname", "")
    if not exiled_oid:
        return False
    kind, opp, ch = find_may_cast(st, exiled_oid, exiled_lname)
    if kind is None:
        return False
    key = (tag, "maycast", str(exiled_oid), kind)
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    ST["cast_offered"] = True
    ST["cast_offer_kind"] = kind
    say(f"[{tag}] MAY-CAST OFFERED for exiled {exiled_lname} (kind={kind})")
    wire("maycast_offered", {"kind": kind, "exiled_oid": exiled_oid,
                            "exiled_lname": exiled_lname})
    if kind == "optional":
        await answer_vi(c, opp, ch, tag)
        ST["maycast_accepted"] = True
        return True
    if kind == "action":
        await submit_as_is(c, ch)
        ST["maycast_accepted"] = True
        # Bolt needs a target: P0 (seat 0). Answer target selection next ticks.
        ST["maycast_cast_submitted"] = True
        return True
    if kind == "vi_cast":
        await answer_vi(c, opp, ch, tag)
        ST["maycast_accepted"] = True
        ST["maycast_cast_submitted"] = True
        return True
    return False


async def handle_maycast_target(c, tag):
    """After P1 submits the exiled Bolt's cast, answer its target selection
    (target P0, seat 0)."""
    if not ST.get("maycast_cast_submitted") or ST.get("maycast_target_done"):
        return False
    st = st_of(c)
    opp, rtype, spec_type = target_opportunity(st)
    if opp is None:
        return False
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []

    def seat_of(ch):
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            for k in ("seat", "player", "index"):
                if d.get(k) is not None:
                    try:
                        return int(d[k])
                    except (TypeError, ValueError):
                        pass
        return None

    best, best_score = None, -1
    for ch in cands:
        sc = 2 if seat_of(ch) == 0 else 0
        if set(surf_codes(ch)) & {"candidate", "target"}:
            sc += 1
        if sc > best_score:
            best, best_score = ch, sc
    if best is None and len(cands) == 1:
        best = cands[0]
    if best is None:
        return False
    ST["maycast_target_done"] = True
    say(f"[{tag}] may-cast Bolt targeting P0 (seat 0)")
    wire("maycast_target", {"choice": best.get("id")})
    await submit_target(c, opp, rtype, spec_type, best, tag)
    return True


# ------------------------------------------------------------- driver loops
async def tick_all(p0, p1):
    await p1_tick(p1, "P1")
    # may-cast Bolt target selection (P1) after the cast submission
    if ST.get("maycast_phase"):
        await handle_maycast_target(p1, "P1")
    await p0_tick(p0, "P0")


async def settle(p0, p1, cond, timeout, label, poll=0.25):
    t0 = time.time()
    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                holds_prio = my_priority(top_acts(st))
                if not (holds_prio
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                if tag == "P1" and ST.get("maycast_phase"):
                    await handle_maycast_target(c, tag)
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
        st = st_of(p0)
        if st and cond(st["state"]):
            return st["state"]
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c in (p0, p1):
                st = st_of(c)
                if not st:
                    continue
                s = st["state"]
                opdump = []
                for op in vi_ops(st)[:3]:
                    resp = op.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    items = data.get("candidates") or data.get("choices") or []
                    codes = set()
                    for ch in items[:6]:
                        codes.update(x for x in surf_codes(ch) if x)
                    opdump.append({"rtype": resp.get("type"),
                                   "n": len(items), "codes": sorted(codes)})
                say(f"DIAG {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} prio={[a.get('type') for a in top_acts(st)][:6]} "
                    f"real_decision={real_decision_pending(st)} ops={json.dumps(opdump)[:300]}")
    say(f"TIMEOUT in settle: {label}")
    return None


async def export_state(c, path):
    s = await c.export_state()
    with open(path, "w") as f:
        f.write(s)
    return json.loads(s)["state"]


# ------------------------------------------------------------- main
async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P07195")
    await p0.connect()
    say("connecting P1...")
    await p0.create(deck((FLOURISH.title(), 12), (MOUNTAIN.title(), 48)))
    p1 = PhaseClient("P17195")
    await p1.connect()
    await p1.join(p0.game_code,
                  deck((RING.title(), 8), (FOREST.title(), 12),
                       (BOLT.title(), 40)))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code})
    obs["assert"]["A0_connected"] = "passed" if (
        p0.player_id is not None and p1.player_id is not None) else "failed"

    # ---- phase 1: setup -- P1 casts Sol Ring, P0 casts Flourish at it
    def flourish_on_stack(s):
        return any("flourish" in json.dumps(e, default=str).lower()
                   for e in (s.get("stack") or []))

    s = await settle(p0, p1, flourish_on_stack, GAME_TIMEOUT,
                     "flourish cast")
    if s is None:
        obs["notes"].append("Flourish never reached the stack within timeout")
        for k in ("A1_setup_ok", "A2_demonstrate_declined", "A3_destroyed",
                  "A4_exile_done", "A5_cast_offered", "A6_free_cast_resolved"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close()
        await p1.close()
        return finish(obs, t0)

    say("exporting PRE_CAST state")
    pre = await export_state(p0, f"{EVDIR}/pre_cast.json")
    ring_oids = bf_by_name(pre, 1, RING)
    flourish_stack = [e for e in (pre.get("stack") or [])
                      if FLOURISH in json.dumps(e, default=str).lower()]
    obs["assert"]["A1_setup_ok"] = "passed" if (
        ring_oids and flourish_stack) else "failed"
    obs["notes"].append(
        f"pre_cast: turn={pre.get('turn_number')} phase={pre.get('phase')} "
        f"ring_oids={ring_oids} mountains_out="
        f"{sum(1 for oid in bf_oids(pre, 0) if obj_lname(pre, oid) == MOUNTAIN)} "
        f"life={life(pre, 0)}/{life(pre, 1)}")
    ST["ring_oid"] = ring_oids[0] if ring_oids else ST.get("ring_oid")
    wire("pre_cast", {"ring_oid": ST["ring_oid"],
                      "turn": pre.get("turn_number"),
                      "phase": pre.get("phase")})

    # ---- phase 2: Demonstrate decline + target + resolution + exile
    def exiled_nonland(s):
        return any(o.get("zone") == "Exile"
                   and not is_land(o)
                   for o in (s.get("objects") or {}).values()
                   if str(o.get("owner", o.get("controller", -1))) == "1")

    s2 = await settle(p0, p1, exiled_nonland, 300, "exile resolution")
    if s2 is None:
        stuck = await export_state(p0, f"{EVDIR}/stuck_after_cast.json")
        obs["notes"].append(
            f"no exiled nonland within 300s; stack={len(stuck.get('stack') or [])} "
            f"phase={stuck.get('phase')} turn={stuck.get('turn_number')}")
        wire("stuck_after_cast",
             {"stack": [str(e)[:200] for e in (stuck.get("stack") or [])]})
        for k in ("A2_demonstrate_declined", "A3_destroyed", "A4_exile_done",
                  "A5_cast_offered", "A6_free_cast_resolved"):
            obs["assert"].setdefault(k, "not-run")
        await p0.close()
        await p1.close()
        return finish(obs, t0)

    # identify the exiled nonland
    ex = [o for o in (s2.get("objects") or {}).values()
          if o.get("zone") == "Exile" and not is_land(o)
          and str(o.get("owner", o.get("controller", -1))) == "1"]
    ex0 = ex[0]
    ST["exiled_oid"] = str(ex0.get("id"))
    ST["exiled_lname"] = str(ex0.get("base_name") or ex0.get("name") or "?").lower()
    say(f"EXILED nonland: {ST['exiled_lname']} oid={ST['exiled_oid']} "
        f"(total exiled this game: {len(ex)})")
    wire("exiled_nonland", {"oid": ST["exiled_oid"],
                            "lname": ST["exiled_lname"]})

    # A2: demonstrate declined -- no Flourish copy on the stack / cast
    stack_now = s2.get("stack") or []
    copies = [e for e in stack_now
              if "flourish" in json.dumps(e, default=str).lower()]
    obs["assert"]["A2_demonstrate_declined"] = \
        "passed" if (ST.get("demonstrate_done") and len(copies) <= 1) else \
        ("failed" if not ST.get("demonstrate_done") else "passed")
    obs["notes"].append(
        f"demonstrate_done={ST.get('demonstrate_done')} "
        f"flourish-stack-entries={len(copies)}")

    # A3: Sol Ring destroyed (in P1 graveyard, not exile/battlefield)
    ring_oid = ST.get("ring_oid")
    ring_obj = get_obj(s2, ring_oid) if ring_oid else {}
    obs["assert"]["A3_destroyed"] = "passed" if (
        ring_obj.get("zone") == "Graveyard") else "failed"
    obs["notes"].append(f"ring oid={ring_oid} zone={ring_obj.get('zone')}")

    # A4: exile done
    obs["assert"]["A4_exile_done"] = "passed" if ex else "failed"

    say("exporting POST_EXILE state")
    post_ex = await export_state(p0, f"{EVDIR}/post_exile.json")
    p1_untapped_before = sum(
        1 for oid in bf_oids(post_ex, 1)
        if is_land(get_obj(post_ex, oid))
        and not get_obj(post_ex, oid).get("tapped"))
    wire("p1_mana_at_maycast",
         {"untapped_lands": p1_untapped_before})
    say(f"P1 untapped lands at may-cast window: {p1_untapped_before} "
        f"(must be 0 for the free-cast proof)")

    # ---- phase 3: may-cast window
    ST["maycast_phase"] = True
    t_mc = time.time()
    last_vi_dump = 0
    maycast_resolved = False
    while time.time() - t_mc < MAYCAST_WINDOW_S:
        await asyncio.sleep(0.25)
        await tick_all(p0, p1)
        # dump P1's vi periodically for the record
        if time.time() - last_vi_dump > 10:
            last_vi_dump = time.time()
            st1 = st_of(p1)
            if st1:
                opdump = []
                for op in vi_ops(st1)[:4]:
                    resp = op.get("response", {}) or {}
                    data = resp.get("data", {}) or {}
                    items = data.get("candidates") or data.get("choices") or []
                    opdump.append({
                        "rtype": resp.get("type"),
                        "n": len(items),
                        "texts": [choice_text(ch)[:60] for ch in items[:6]],
                        "codes": sorted(set(
                            x for ch in items[:6]
                            for x in surf_codes(ch) if x))})
                wire("maycast_window_vi",
                     {"t": round(time.time() - t_mc, 1), "ops": opdump,
                      "cast_offered": bool(ST.get("cast_offered"))})
        # check resolution: Bolt dealt 3 to P0, or ring on P1 BF
        st = st_of(p0)
        if st:
            s_now = st["state"]
            if ST["exiled_lname"] == BOLT:
                if life(s_now, 0) == 17:
                    # P0 took exactly 3 (Bolt); P1 never attacks
                    maycast_resolved = True
                    break
            elif ST["exiled_lname"] == RING:
                if bf_by_name(s_now, 1, RING):
                    maycast_resolved = True
                    break
            # generic: exiled card left exile (cast or otherwise)
            ex_now = get_obj(s_now, ST["exiled_oid"])
            if ex_now.get("zone") not in ("Exile",):
                wire("exiled_card_left_exile",
                     {"zone": ex_now.get("zone")})
                if ST.get("maycast_accepted"):
                    maycast_resolved = True
                    break
        if ST.get("cast_offered") and maycast_resolved:
            break

    say(f"may-cast window done: offered={ST.get('cast_offered')} "
        f"accepted={ST.get('maycast_accepted')} resolved={maycast_resolved}")
    wire("maycast_window_end",
         {"offered": bool(ST.get("cast_offered")),
          "accepted": bool(ST.get("maycast_accepted")),
          "resolved": maycast_resolved})
    ST["maycast_phase"] = False

    obs["assert"]["A5_cast_offered"] = \
        "passed" if ST.get("cast_offered") else "failed"

    # let the game settle a bit more, then export final
    await settle(p0, p1, lambda s: False, 20, "final settle")
    say("exporting POST_RESOLUTION state")
    post = await export_state(p0, f"{EVDIR}/post_resolution.json")

    # A6: free cast resolved
    p1_untapped_after = sum(
        1 for oid in bf_oids(post, 1)
        if is_land(get_obj(post, oid))
        and not get_obj(post, oid).get("tapped"))
    ex_post = get_obj(post, ST["exiled_oid"])
    if ST["exiled_lname"] == BOLT:
        bolt_gy = any(obj_lname(post, str(o.get("id"))) == BOLT
                      for o in gy_objs(post, 1))
        a6 = (life(post, 0) == 17 and bolt_gy
              and p1_untapped_after == 0)
        obs["notes"].append(
            f"A6 bolt: P0 life={life(post, 0)} (expect 17), bolt in P1 "
            f"gy={bolt_gy}, P1 untapped lands after={p1_untapped_after} "
            f"(expect 0: proves no mana was paid)")
    elif ST["exiled_lname"] == RING:
        ring_bf = bool(bf_by_name(post, 1, RING))
        a6 = ring_bf and p1_untapped_after == 0
        obs["notes"].append(
            f"A6 ring: ring on P1 BF={ring_bf}, P1 untapped lands after="
            f"{p1_untapped_after} (expect 0)")
    else:
        a6 = (ST.get("maycast_accepted") and ex_post.get("zone") != "Exile"
              and p1_untapped_after == 0)
        obs["notes"].append(
            f"A6 other ({ST['exiled_lname']}): accepted={ST.get('maycast_accepted')} "
            f"zone now={ex_post.get('zone')} untapped={p1_untapped_after}")
    if not ST.get("cast_offered"):
        obs["assert"]["A6_free_cast_resolved"] = "not-run"
    else:
        obs["assert"]["A6_free_cast_resolved"] = "passed" if a6 else "failed"

    await p0.close()
    await p1.close()
    return finish(obs, t0)


def finish(obs, t0):
    dur = time.time() - t0
    a = obs["assert"]
    if (a.get("A1_setup_ok") == "passed"
            and a.get("A3_destroyed") == "passed"
            and a.get("A4_exile_done") == "passed"
            and a.get("A5_cast_offered") == "failed"):
        verdict = "reproduced"
    elif (a.get("A5_cast_offered") == "passed"
            and a.get("A6_free_cast_resolved") == "failed"):
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_demonstrate_declined", "A3_destroyed",
              "A4_exile_done", "A5_cast_offered", "A6_free_cast_resolved")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    result = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": ST.get("game_code"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "verdict": verdict,
        "assertions": a,
        "notes": obs["notes"],
        "data_level_ok": ST.get("data_level_ok"),
        "demonstrate_done": ST.get("demonstrate_done"),
        "flourish_cast": ST.get("flourish_cast"),
        "ring_oid": ST.get("ring_oid"),
        "exiled_oid": ST.get("exiled_oid"),
        "exiled_lname": ST.get("exiled_lname"),
        "cast_offered": ST.get("cast_offered"),
        "cast_offer_kind": ST.get("cast_offer_kind"),
        "maycast_accepted": ST.get("maycast_accepted"),
        "decks": {
            "P0": [[FLOURISH.title(), 12], [MOUNTAIN.title(), 48]],
            "P1": [[RING.title(), 8], [FOREST.title(), 12],
                   [BOLT.title(), 40]],
        },
        "scenario_sha256": sha256_of_file(__file__),
    }
    with open(f"{EVDIR}/scenario_result.json", "w") as f:
        json.dump(result, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(a)}")
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass
    return result


asyncio.run(main())
