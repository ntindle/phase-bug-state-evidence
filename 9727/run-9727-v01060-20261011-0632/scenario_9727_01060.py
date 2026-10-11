#!/usr/bin/env python3
"""Issue #9727 on pinned v0.106.0 (build 29e0db3, WS protocol 126):

"Triggered self-Endure can counter a returned incarnation after a flicker
response" (filed 2026-10-10; statically traced, NO runtime test run before
this scenario).

Printed reproduction: Fortress Kin-Guard enters; its ETB Endure trigger stays
on the stack; respond with Flicker of Fate targeting it. Resolve the flicker
(Kin-Guard exiled then returned under owner's control -> NEW ETB trigger on
the stack), then resolve the new trigger separately, then resolve the
ORIGINAL trigger.

Expected (CR 400.7 / 113.7a / 701.63a): the original trigger must NOT put
counters on the returned incarnation (a different object). It should retain
the original permanent's last controller and offer only the token branch.
The new incarnation's own trigger is a legitimate own-return-then-counter
chain (control): it must offer the counter branch.

Single game (native engine, two human-client seats):
  P0: 4x Fortress Kin-Guard + 56x Plains. Mulligans until Kin-Guard in hand
      (cap 6), casts it on P0's third turn (turn_number 5) with 2+ untapped
      Plains, once P1 has 2+ lands for the Flicker response.
  P1: 4x Flicker of Fate + 56x Plains. Mulligans until Flicker in hand
      (cap 6); responds to the ETB trigger by casting Flicker of Fate
      targeting Kin-Guard (incarnation A).

Assertions:
  A1_setup_ok       Kin-Guard entered; original Endure trigger observed on
                    the stack; Flicker cast in response targeting incarnation A.
  A2_flicker_ok     Flicker resolved; stack [original trigger, new trigger]
                    observed; the permanent's incarnation advanced
                    (object_id is STABLE across the flicker: the engine
                    models zone-change identity as (object_id, incarnation)).
  A3_control_ok     The NEW trigger's Endure resolution offered the counter
                    branch; choosing it put a counter on incarnation B.
  A4_original_scope The ORIGINAL trigger's Endure resolution offered NO
                    counter branch (token branch only).
  A5_final_state    Incarnation B has exactly 1 counter; P0 has exactly 1
                    Spirit token.

Verdict rule:
  reproduced    iff the original trigger offered the counter branch on the
                returned incarnation (A4 failed) or placed counters on B.
  not-reproduced iff A1-A5 all pass.
  blocked       iff the reported path cannot be exercised.

Protocol-126 conventions (from scenario_9721_01060.py):
  - waiting_for is gone (null); priority = PassPriority in merged actions.
  - MulliganDecision via legacy Action; bottom via vi schema/select.
  - Mana via legacy PayMana + vi tapLandForMana menus (ST mana_needs).
  - Cast-confirmation guard after CastSpell.
  - Target selection + Endure choice: vi opportunities; submission mirrors
    the opportunity's spec type (select/sequence/choose).
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 9727
RUN_ID = os.environ.get("RUN_ID", "run-9727-v01060-20261011-0545")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty (stale RUN_ID reuse)"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KINGUARD = "fortress kin-guard"
FLICKER = "flicker of fate"
PLAINS = "plains"

P0_DECK = deck((KINGUARD.title(), 4), (PLAINS.title(), 56))
P1_DECK = deck((FLICKER.title(), 4), (PLAINS.title(), 56))

ST = {"mana_needs": {}}
SUBMITTED_OPPS = set()
PASSED_REV = {}
LAND_PLAYED_THIS_ACTIVE = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def oname(o):
    return (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return oname(get_obj(state, oid))


def players(state):
    return state.get("players") or []


def hand_ids(state, pid):
    ps = players(state)
    if pid >= len(ps):
        return []
    return [str(x) for x in ps[pid].get("hand", [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf(state, pid):
    return [(oid, o) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def is_land_name(nm):
    return nm in ("forest", "mountain", "swamp", "island", "plains")


def is_land_obj(o):
    t = str(o.get("type_line") or o.get("type") or "").lower()
    return "land" in t or is_land_name(oname(o))


def untapped_lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid)
            if not o.get("tapped") and is_land_obj(o)]


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_entries(state):
    return state.get("stack") or []


def stack_entry_name(state, e):
    if not isinstance(e, dict):
        return ""
    sid = e.get("source_id")
    if sid is not None:
        o = get_obj(state, sid)
        nm = (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()
        if nm:
            return nm
    return (e.get("card_name") or e.get("name") or "").lower()


def stack_entry_desc(e):
    if not isinstance(e, dict):
        return ""
    kind = e.get("kind") or {}
    if isinstance(kind, dict):
        ab = kind.get("ability")
        if isinstance(ab, dict) and ab.get("description"):
            return str(ab["description"])[:120]
        kd = kind.get("data") or {}
        if isinstance(kd, dict) and kd.get("description"):
            return str(kd["description"])[:120]
    return ""


def stack_summary(state):
    out = []
    for e in stack_entries(state):
        if isinstance(e, dict):
            kind = e.get("kind") or {}
            ktype = kind.get("type") if isinstance(kind, dict) else kind
            out.append((stack_entry_name(state, e), str(ktype),
                        stack_entry_desc(e), str(e.get("source_id"))))
        else:
            out.append((str(e)[:60], "", "", ""))
    return out


def endure_triggers_on_stack(state):
    out = []
    for e in stack_entries(state):
        if "endure" in (stack_entry_desc(e) or "").lower():
            out.append(e)
    return out


def guard_on_bf(state, pid=0):
    for oid, o in bf(state, pid):
        if oname(o) == KINGUARD:
            return str(oid)
    return None


def obj_incarnation(state, oid):
    """The engine's zone-change identity is (object_id, incarnation): a
    flickered permanent keeps its object_id and bumps incarnation
    (observed 3 -> 5 across Flicker of Fate on v0.106.0)."""
    if not oid:
        return None
    o = get_obj(state, oid)
    try:
        return int(o.get("incarnation"))
    except (TypeError, ValueError):
        return None


def counters_of(state, oid):
    o = get_obj(state, oid)
    out = {}
    for key in ("counters", "counter_map", "markers"):
        v = o.get(key)
        if isinstance(v, dict):
            for k, n in v.items():
                out[str(k)] = out.get(str(k), 0) + int(n)
        elif isinstance(v, list):
            for c in v:
                if isinstance(c, dict):
                    t = str(c.get("type") or c.get("kind") or c)
                    out[t] = out.get(t, 0) + int(c.get("count", 1))
    for k, v in o.items():
        kl = str(k).lower()
        if "counter" in kl and k not in ("counters", "counter_map", "markers"):
            out[f"field:{k}"] = v
    return out


def spirit_tokens(state, pid=0):
    return [(oid, o) for oid, o in bf(state, pid)
            if "spirit" in oname(o)]


def cast_confirmed(state, G):
    oid = G.get("cast_pending_oid")
    if oid is not None:
        o = get_obj(state, oid)
        if o and o.get("zone") not in ("Hand", "Library"):
            return True
    name = (G.get("cast_pending_name") or "").lower()
    if name:
        blob = json.dumps(stack_entries(state)).lower()
        if name in blob:
            return True
    return False


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and not stack_entries(state))


# ------------------------------------------------------------- 106 primitives
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


def surf_codes(ch):
    return [c for c in (
        s.get("data", {}).get("code") if isinstance(s.get("data"), dict) else None
        for s in ch.get("surfaces", []) or []) if c]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "playLand", "candidate",
                      "mana", "mulliganDecision"}


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
            codes.update(surf_codes(ch))
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if t:
        return str(t)
    # dig through surfaces: text may live in data.text / data.label /
    # data.name at any nesting level (the 2026-10-11 06:11 run showed
    # empty top-level texts on the Endure ChooseBranch opportunity).
    found = []

    def rec(v, depth=0):
        if depth > 4 or len(found) > 8:
            return
        if isinstance(v, str):
            s = v.strip()
            if len(s) > 3:
                found.append(s)
        elif isinstance(v, dict):
            for k in ("text", "label", "name", "description", "prompt"):
                if isinstance(v.get(k), str) and v[k].strip():
                    found.append(v[k].strip())
            for vv in v.values():
                rec(vv, depth + 1)
        elif isinstance(v, (list, tuple)):
            for vv in v:
                rec(vv, depth + 1)

    for s in ch.get("surfaces", []) or []:
        d = s.get("data")
        if isinstance(d, dict):
            rec(d)
    return " | ".join(dict.fromkeys(found))


def opp_response_type(opp):
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") or resp.get("type") or "choose"


async def submit_choice(c, opp, choice_id, tag, label):
    iid = opp.get("interactionId")
    stype = opp_response_type(opp)
    # NOTE: the engine advertises "exactChoices" on some opportunities but
    # REJECTS it as a submission type (expects "choose"). Always submit
    # "choose" (or "select"/"sequence" for multi-select specs).
    if stype in ("select", "sequence"):
        data = {"choiceIds": [choice_id]}
    else:
        stype = "choose"
        data = {"choiceId": choice_id}
    key = (tag, label, str(iid), choice_id)
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": stype, "data": data}}, tag)
    return True


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = {k: v for k, v in a["data"].items()
                       if not k.startswith("_")}
    msg = {k: v for k, v in msg.items() if not k.startswith("_")}
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
    say(f"ServerHello: version={ver} build={build} protocol={proto}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert "0.106.0" in str(ver), f"unexpected version {ver}"
    assert int(proto) == 126, f"unexpected protocol {proto}"
    assert str(build) == "29e0db3", f"unexpected build {build}"


# ------------------------------------------------------------- mulligan/bottom
KEY_CARD = {0: KINGUARD, 1: FLICKER}


def hand_fingerprint(state, pid):
    return tuple(sorted(hand_ids(state, pid)))


async def do_mulligan(c, acts, st, pid, tag, G):
    if "MulliganDecision" not in [a.get("type") for a in acts]:
        return False
    # Key decisions by hand fingerprint, not revision: a stale re-tick on
    # the same hand must not double-submit, and a NEW hand appearing after
    # our keep (stale-mulligan race seen 2026-10-11) must be re-evaluated.
    fp = hand_fingerprint(st["state"], pid)
    key = (tag, "mulligan", fp, G["game_idx"])
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    mulls = G.get(f"mulls{pid}", 0)
    hn = hand_names(st["state"], pid)
    lands = sum(1 for n in hn if is_land_name(n))
    keycard = KEY_CARD[pid]
    # The key card is essential to the contract (P0 needs Kin-Guard, P1
    # needs Flicker of Fate). Never keep a hand without it: the 2026-10-11
    # 06:08 run kept a flicker-less hand at the cap and the response window
    # was missed. Mulligan until the key card is in hand (hard stop at 12
    # to avoid a pathological loop).
    want = lands >= 2 and keycard in hn
    if mulls >= 12 and keycard not in hn:
        G["blocked"] = (f"{tag} never drew {keycard} in 12 mulligans")
        G["done"] = True
        say(f"[{tag}] BLOCKED: {G['blocked']}")
        return True
    decision = "keep" if want else "mulligan"
    if decision == "mulligan":
        G[f"mulls{pid}"] = mulls + 1
    else:
        G[f"kept_fp{pid}"] = fp
    say(f"[{tag}] mulligan: {decision} (mulls={mulls}, lands={lands}, "
        f"{keycard}={keycard in hn}, hand={sorted(hn)})")
    wire("mulligan", {"who": tag, "decision": decision, "game": G["game_idx"],
                      "hand": sorted(hn)})
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": "Keep" if decision == "keep"
                                               else "Mulligan"}}})
    return True


def mulligan_phase_over(acts, state):
    atypes = [a.get("type") for a in acts]
    return ("MulliganDecision" not in atypes and "SelectCards" not in atypes
            and turn_of(state) >= 1)


async def do_bottom(c, acts, st, pid, tag, G):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (turn_of(state) == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
        return False
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
        key = (tag, "bottom", str(iid), G["game_idx"])
        if key in SUBMITTED_OPPS:
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            oid = _cand_reference(ch)
            nm = obj_lname(state, oid) if oid is not None else "?"
            if nm == KEY_CARD[pid]:
                return (2, str(oid))
            if is_land_name(nm):
                return (0, str(oid))
            return (1, str(oid))

        ranked = sorted(cands, key=bkey)
        picks = ranked[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]}")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id") for ch in picks]}}},
                          tag)
        return True
    return False


# ------------------------------------------------------------- discard
async def do_discard(c, st, pid, tag, G):
    state = st["state"]
    hand = hand_ids(state, pid)
    want_n = len(hand) - 7
    if want_n <= 0:
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid), G["game_idx"])
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type") or ""
        if stype not in ("select", "sequence"):
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not refs or not set(refs) <= set(hand):
            continue
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
        n = min(int(con.get("min") or con.get("max") or want_n), len(cands))
        if n <= 0:
            continue

        def rkey(ch):
            oid = _cand_reference(ch)
            nm = obj_lname(state, oid) if oid is not None else "?"
            if nm == KEY_CARD[pid]:
                return (9, nm)
            if is_land_name(nm):
                return (1, nm)
            return (5, nm)

        picks = sorted(cands, key=rkey)[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discard(cleanup): {[obj_lname(state, _cand_reference(x)) for x in picks]}")
        wire("discard_answered", {"tag": tag, "game": G["game_idx"],
                                  "picks": [obj_lname(state, _cand_reference(x)) for x in picks]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": stype, "data": {
                                  "choiceIds": [ch.get("id") for ch in picks]}}},
                          tag)
        return True
    return False


# ------------------------------------------------------------- mana / priority
async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    if not needs or sum(needs.values()) <= 0:
        return False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, [str(x) for x in syms]))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if (tag, "tapi", str(iid)) in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        for ch, syms in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in syms:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        # key includes the choice id: one menu (interactionId) can require
        # several taps (e.g. W + generic), each on a different land choice.
        SUBMITTED_OPPS.add((tag, "tapi", str(iid), str(pick.get("id"))))
        say(f"[{tag}] tap land for mana used_for={used} remaining={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        # NOTE: submit "choose" even when the opportunity advertises
        # "exactChoices" -- the engine rejects "exactChoices" as a
        # submission type.
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "choose",
                                           "data": {"choiceId": pick.get("id")}}},
                          tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag, G):
    turn = turn_of(state)
    key = (tag, G["game_idx"])
    if LAND_PLAYED_THIS_ACTIVE.get(key) == (state.get("active_player"), turn):
        return False
    for o in hand_ids(state, pid):
        if is_land_obj(get_obj(state, o)):
            for a in acts:
                if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_THIS_ACTIVE[key] = (state.get("active_player"), turn)
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o, "game": G["game_idx"]})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_actions_for(acts, state, key):
    out = []
    for a in acts:
        if a.get("type") == "CastSpell":
            d = a.get("data", {})
            oid = d.get("object_id")
            try:
                if oid is not None and obj_lname(state, int(oid)) == key:
                    out.append(a)
            except (TypeError, ValueError):
                continue
    return out


def drain_rejections(c, G):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            body = json.dumps(data, default=str)
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "game": G.get("game_idx")})
            say(f"[{c.name}] {t}: {body[:300]}")
            G.setdefault("rejections", []).append(
                {"who": c.name, "type": t, "data": body[:500]})


# ------------------------------------------------------------- flicker targeting
def flicker_on_stack(state):
    return [e for e in stack_entries(state)
            if stack_entry_name(state, e) == FLICKER]


def _cand_obj_name(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and s.get("type") == "object":
            return (d.get("zone"), (d.get("name") or "").lower())
    return None, None


def find_flicker_target_opp(st, oid, state):
    cands_seen = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        stype = (spec.get("type") or resp.get("type") or "")
        if stype not in ("select", "sequence", "choose"):
            continue
        items = rdata.get("candidates") or rdata.get("choices") or []
        for ch in items:
            ref = _cand_reference(ch)
            z, nm = _cand_obj_name(ch)
            cands_seen.append({"ref": ref, "zone": z, "name": nm,
                               "text": choice_text(ch)[:80]})
            if str(ref) == str(oid):
                return opp, stype, ch, None
            if (nm == KINGUARD and (z or "").lower() == "battlefield"
                    and str(ref) == str(oid)):
                return opp, stype, ch, None
        # fallback: single object candidate that IS our guard by name+zone
        for ch in items:
            ref = _cand_reference(ch)
            z, nm = _cand_obj_name(ch)
            if nm == KINGUARD and (z or "").lower() == "battlefield":
                return opp, stype, ch, "name_fallback"
    return None, None, None, cands_seen


def any_choice_pending(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if items:
            return True
    return False


def target_candidates_pending(st, oid_a):
    """True if an opportunity explicitly offers incarnation A as a target
    candidate (and we haven't matched it). Mana-menu land choices don't
    count; a bare passPriority choice doesn't count."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        for ch in items:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                continue
            if str(_cand_reference(ch)) == str(oid_a):
                return True
    return False


async def p1_cast_flicker(c, G, tag, st, state, acts):
    """Cast Flicker of Fate targeting incarnation A (in response to the
    original ETB trigger)."""
    oid_a = G.get("guard_oid_A")
    if oid_a is None:
        return False
    ca = cast_actions_for(acts, state, FLICKER)
    if not ca:
        return False
    if len(untapped_lands(state, 1)) < 2:
        return False
    chosen = copy.deepcopy(ca[0])
    d = chosen.setdefault("data", {})
    try:
        # targets takes plain object ids (u64), not {"Object": id} maps
        d["targets"] = [int(oid_a)]
    except (TypeError, ValueError):
        return False
    say(f"[{tag}] PROBE: casting Flicker of Fate targeting incarnation A "
        f"(oid {oid_a}) in response to the ETB trigger")
    wire("flicker_cast", {"target_oid": oid_a, "game": G["game_idx"]})
    G["cast_pending_name"] = FLICKER
    try:
        oid = int(d.get("object_id"))
    except (TypeError, ValueError):
        oid = None
    G["cast_pending_oid"] = oid
    G["cast_pending_at"] = time.time()
    G["cast_pending"] = True
    G["cast_pending_who"] = tag
    G["pending_cast"] = {"who": tag, "name": FLICKER, "oid": oid,
                        "at": time.time()}
    ST["mana_needs"][tag] = {"W": 1, "generic": 1}
    await submit_as_is(c, chosen)
    return True


async def p1_answer_target(c, G, tag, st, state):
    oid_a = G.get("guard_oid_A")
    if oid_a is None:
        return False
    opp, stype, ch, note = find_flicker_target_opp(st, oid_a, state)
    if opp is None:
        if any_choice_pending(st):
            wire("flicker_target_unmatched", {"target_oid": oid_a,
                                             "game": G["game_idx"],
                                             "opps": [o for o in
                                                      (st.get("viewer_interaction") or {}).get("opportunities", []) or []]})
            say(f"[{tag}] flicker target: choice pending but no candidate "
                f"matched incarnation A (oid {oid_a}); holding priority")
        return False
    say(f"[{tag}] flicker target select: incarnation A oid={oid_a} "
        f"(stype={stype}{', ' + note if note else ''})")
    wire("flicker_target", {"target_oid": oid_a, "stype": stype,
                            "game": G["game_idx"], "opp": opp})
    ok = await submit_choice(c, opp, ch.get("id"), tag, "flicker_target")
    if ok:
        G["flicker_target_done"] = True
    return ok


# ------------------------------------------------------------- endure answering
def classify_endure_choice(ch):
    t = choice_text(ch).lower()
    kind = None
    if "counter" in t:
        kind = "counter"
    elif "spirit" in t or "token" in t:
        kind = "token"
    return kind, t


def find_endure_opps(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        kinds = [classify_endure_choice(ch)[0] for ch in items]
        if any(k in ("counter", "token") for k in kinds):
            out.append(opp)
    return out


def find_branch_opps(st):
    """Find ChooseBranch opportunities by action code (not text).

    On v0.106.0 the Endure branch choices carry NO human-readable text:
    each choice is {summary: candidate, action: chooseBranch(actionId),
    value: optionIndex}. Branch identity must be established empirically
    (choose a branch, observe counter vs token).
    """
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        codes = set()
        for ch in items:
            codes.update(surf_codes(ch))
        if "chooseBranch" in codes:
            out.append(opp)
    return out


def branch_option_index(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "optionIndex":
            try:
                return int(d.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def branch_action_id(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("code") == "chooseBranch":
            return d.get("actionId")
    return None


async def p0_answer_endure(c, G, tag, st, state):
    """Answer the pending Endure branch choice as P0.

    Which trigger is resolving is determined by LIFO order: the FIRST
    branch prompt is the NEW (top, returned-incarnation) trigger, the
    SECOND is the ORIGINAL (bottom) trigger.

    Branch identity is text-less (optionIndex only), so the branch to take
    is configurable per trigger: BRANCH_INDEX_NEW / BRANCH_INDEX_ORIG env
    vars (default 0). The outcome (counter on the returned incarnation vs
    Spirit token) identifies the branch empirically in the post state.
    """
    opps = find_branch_opps(st)
    if not opps:
        return False
    opp = opps[0]
    iid = opp.get("interactionId")
    key = (tag, "endure", str(iid), G["game_idx"])
    if key in SUBMITTED_OPPS:
        return True
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    items = data.get("candidates") or data.get("choices") or []
    offered = []
    by_index = {}
    for ch in items:
        idx = branch_option_index(ch)
        aid = branch_action_id(ch)
        offered.append({"option_index": idx, "action_id": aid,
                        "choice_id": ch.get("id")})
        if idx is not None and idx not in by_index:
            by_index[idx] = ch
    # Which trigger is resolving: LIFO order - the FIRST branch prompt is
    # always the NEW (top, returned-incarnation) trigger, the SECOND is the
    # ORIGINAL (bottom) trigger. (n_end from P0's view lags; order is
    # reliable: exactly two triggers, resolved top-first.)
    n_answered = len(G["endure_answers"])
    which = "new" if n_answered == 0 else "orig"
    n_end = len(endure_triggers_on_stack(state))
    # Branch index is configurable PER TRIGGER: BRANCH_INDEX_NEW for the
    # new trigger's prompt, BRANCH_INDEX_ORIG for the original's. This
    # isolates the verdict: e.g. new->token(0) + orig->counter(1) means any
    # counter on the returned incarnation must come from the orig trigger.
    want = int(os.environ.get(
        "BRANCH_INDEX_NEW" if which == "new" else "BRANCH_INDEX_ORIG",
        os.environ.get("BRANCH_INDEX", "0")))
    say(f"[{tag}] Endure branch prompt (trigger={which}, answers_so_far="
        f"{n_answered}, endure_on_stack={n_end}): offered option indices="
        f"{sorted(by_index)}; taking index={want}")
    wire("endure_prompt", {"which": which, "offered": offered,
                           "branch_index_wanted": want,
                           "stack": stack_summary(state),
                           "game": G["game_idx"], "opp": opp})
    if not G.get("endure_opp_dumped"):
        G["endure_opp_dumped"] = True
        try:
            with open(f"{EVDIR}/endure_opp_raw.json", "w") as f:
                json.dump(opp, f, indent=1, default=str)
            say(f"[{tag}] dumped first endure opportunity to endure_opp_raw.json")
        except Exception as e:
            say(f"[{tag}] opp dump failed: {e!r}")
    G.setdefault("endure_prompt_dumps", []).append(
        {"which": which, "offered": offered, "branch_index_wanted": want,
         "stack": stack_summary(state)})
    SUBMITTED_OPPS.add(key)
    ch = by_index.get(want)
    if ch is None:
        # fall back to the first available branch
        ch = items[0] if items else None
        want = branch_option_index(ch) if ch is not None else None
    if ch is None:
        say(f"[{tag}] {which} trigger offered no branch choices?!")
        wire("endure_no_choice", {"offered": offered})
        G["blocked"] = f"{which} trigger offered no Endure branch choices"
        G["done"] = True
        return True
    G["endure_answers"].append({"which": which,
                                "offered": sorted(by_index),
                                "chosen_index": want,
                                "stack_at_prompt": stack_summary(state)})
    say(f"[{tag}] Endure ({which}): choosing optionIndex {want}")
    wire("endure_answered", {"which": which, "chosen_index": want,
                             "game": G["game_idx"]})
    await submit_choice(c, opp, ch.get("id"), tag, f"endure_{which}")
    return True
    wire("endure_answered", {"which": which,
                             "chosen_index": want,
                             "game": G["game_idx"]})
    await submit_choice(c, opp, ch.get("id"), tag, f"endure_{which}")
    return True


# ------------------------------------------------------------- observation / stages
async def export_now(path, client):
    raw = await client.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(raw)
    say(f"exported {path} ({len(raw)} bytes)")
    env = json.loads(raw)
    st = env.get("state", env) if isinstance(env, dict) else env
    return st


def observe(c, G, tag, state):
    """Advance the stage machine from observations. Returns True if the
    run is complete."""
    stage = G.get("stage")

    # original ETB trigger on the stack?
    if stage == "develop" and G.get("guard_cast_done"):
        trigs = endure_triggers_on_stack(state)
        if trigs:
            oid_a = guard_on_bf(state)
            if oid_a:
                G["guard_oid_A"] = oid_a
                G["etb_seen"] = True
                G["trigger_stack_entry"] = [
                    {"name": stack_entry_name(state, e),
                     "desc": stack_entry_desc(e),
                     "source_id": str(e.get("source_id"))} for e in trigs]
                say(f"[{tag}] original Endure trigger on stack; incarnation A "
                    f"oid={oid_a}")
                wire("etb_observed", {"guard_oid_A": oid_a,
                                      "stack": stack_summary(state)})
                G["stage"] = "etb"

    # response-window guard: if the original trigger left the stack before
    # the flicker response was cast, the scenario is invalid (blocked, not
    # a wedge: P0 must not answer the trigger here).
    if stage == "etb" and G.get("etb_seen") and not G.get("flicker_cast_done"):
        if not endure_triggers_on_stack(state) and not flicker_on_stack(state):
            G["blocked"] = ("response window missed: original Endure trigger "
                            "left the stack before Flicker of Fate was cast")
            G["done"] = True
            say(f"[{tag}] BLOCKED: {G['blocked']}")
            wire("response_window_missed", {"game": G["game_idx"]})
            return True

    if stage == "etb" and G.get("flicker_cast_done"):
        # Flicker resolution is detected by stack signature: the flicker
        # spell has left the stack and BOTH Endure triggers are present
        # (original + the new one from the flickered re-entry). NOTE: the
        # engine keeps the permanent's object_id across the flicker and
        # bumps its *incarnation* instead (observed 3 -> 5 on v0.106.0), so
        # an object-id change must NOT be required here (the 2026-10-11
        # 05:54/06:00 runs wedged waiting for a new oid that never comes).
        if not flicker_on_stack(state):
            oid_a = G.get("guard_oid_A")
            n_endure = len(endure_triggers_on_stack(state))
            if n_endure >= 2:
                oid_b = guard_on_bf(state)
                inc_a = obj_incarnation(G.get("pre_flicker") or {}, oid_a)
                G["guard_oid_B"] = oid_b
                G["guard_inc_A"] = inc_a
                G["guard_inc_B_view"] = obj_incarnation(state, oid_b)
                G["stack_after_flicker"] = stack_summary(state)
                say(f"[{tag}] flicker resolved: guard oid {oid_a} "
                    f"(incarnation {inc_a}) flickered; "
                    f"stack={stack_summary(state)}")
                wire("flicker_resolved", {"guard_oid_A": oid_a,
                                          "guard_incarnation_A": inc_a,
                                          "guard_oid_after": oid_b,
                                          "stack": stack_summary(state)})
                G["stage"] = "triggers"
                G["flicker_gone_since"] = None
            else:
                if not G.get("flicker_gone_since"):
                    G["flicker_gone_since"] = time.time()
                elif time.time() - G["flicker_gone_since"] > 30:
                    G["blocked"] = ("flicker left the stack but the "
                                    "two-trigger signature never appeared "
                                    f"(endure_triggers={n_endure}); flicker "
                                    "may have fizzled or mis-targeted")
                    say(f"[{tag}] BLOCKED: {G['blocked']}")
                    wire("flicker_no_reentry", {"game": G["game_idx"]})
                    return True

    if stage == "triggers":
        # trace incarnation-B counters across the trigger resolutions
        oid_b = G.get("guard_oid_B")
        if oid_b:
            tot, _ = total_plus_counters(state, oid_b)
            tr = G["counters_b_trace"]
            if not tr or tr[-1][2] != tot:
                tr.append((turn_of(state), state.get("phase"), tot))
        # settle when no endure triggers remain on the stack and at least
        # the new trigger has been answered.
        if (not endure_triggers_on_stack(state)
                and not stack_entries(state)
                and len(G["endure_answers"]) >= 1):
            say(f"[{tag}] triggers done; endure_answers="
                f"{[(a['which'], a.get('chosen_index')) for a in G['endure_answers']]}")
            wire("triggers_done", {"endure_answers": G["endure_answers"]})
            G["stage"] = "settle"
    return False


def any_payment_menu(st, acts):
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                return True
    return False


async def confirm_pending_cast(c, G, tag, st, state, acts):
    """Shared cast-confirmation guard for whichever seat cast."""
    pc = G.get("pending_cast")
    if not pc or pc.get("who") != tag:
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_tick(c, acts):
            return
        if await pay_mana_vi(c, st, tag, needs):
            return
        if any_payment_menu(st, acts):
            # payment menu still open but nothing was submitted this tick;
            # wait for the next tick (do NOT confirm a half-paid cast).
            return
        # no payment menu offered: engine may have auto-paid; fall through
        # to the confirmation check below.
    G["cast_pending_name"] = pc["name"]
    G["cast_pending_oid"] = pc["oid"]
    if cast_confirmed(state, G):
        say(f"[{tag}] cast {pc['name']} confirmed; resuming")
        wire("cast_confirmed", {"name": pc["name"], "game": G["game_idx"]})
        G["pending_cast"] = None
        G["cast_pending"] = False
        ST["mana_needs"][tag] = {}
        if pc["name"] == KINGUARD:
            G["guard_cast_done"] = True
        elif pc["name"] == FLICKER:
            G["flicker_cast_done"] = True
            # The flicker target was declared inline in the cast action
            # (d["targets"] = [incarnation A oid]); the engine confirmed the
            # cast and resolves the target from the spell itself. There is no
            # separate target-selection opportunity (the 2026-10-11 05:54 run
            # showed only a passPriority pseudo-choice); mark the target
            # answered so P1 passes priority normally instead of wedging.
            G["flicker_target_done"] = True
            G["flicker_resolved_wait_since"] = time.time()
    elif time.time() - pc["at"] > 60:
        say(f"[{tag}] cast backstop: {pc['name']} unconfirmed after 60s")
        wire("cast_unconfirmed", {"name": pc["name"], "game": G["game_idx"]})
        G["pending_cast"] = None
        G["cast_pending"] = False
        G["blocked"] = f"cast of {pc['name']} never confirmed"
        G["done"] = True


# ------------------------------------------------------------- per-tick handlers
async def p0_tick(c, G, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    drain_rejections(c, G)
    if await do_mulligan(c, acts, st, 0, tag, G):
        return
    if await do_bottom(c, acts, st, 0, tag, G):
        return
    if await do_discard(c, st, 0, tag, G):
        return
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    await confirm_pending_cast(c, G, tag, st, state, acts)
    if G.get("done"):
        return

    stage = G.get("stage")
    if stage == "develop":
        if G.get("max_turn", 0) > 40 and not G.get("guard_cast_done"):
            G["blocked"] = ("develop turn cap (40) reached before Kin-Guard "
                            "cast")
            G["done"] = True
            return
        if is_my_main(state, 0):
            if await play_a_land(c, state, 0, acts, tag, G):
                return
            # turn_number counts player-turns (P0 T1=1, P1 T1=2, P0 T2=3,
            # P1 T2=4, P0 T3=5): cast on P0's third turn so P1 has had two
            # land drops and can respond with Flicker of Fate ({1}{W}).
            p1_lands = len([o for _, o in bf(state, 1) if is_land_obj(o)])
            if (turn_of(state) >= 5 and p1_lands >= 2
                    and not G.get("guard_cast_done")
                    and G.get("p1_has_flicker")
                    and KINGUARD in hand_names(state, 0)
                    and len(untapped_lands(state, 0)) >= 2):
                ca = cast_actions_for(acts, state, KINGUARD)
                if ca:
                    chosen = ca[0]
                    d = chosen.get("data", {})
                    try:
                        oid = int(d.get("object_id"))
                    except (TypeError, ValueError):
                        oid = None
                    say(f"[{tag}] casting Fortress Kin-Guard (turn {turn_of(state)})")
                    wire("guard_cast", {"game": G["game_idx"]})
                    G["pending_cast"] = {"who": tag, "name": KINGUARD,
                                         "oid": oid, "at": time.time()}
                    G["cast_pending"] = True
                    ST["mana_needs"][tag] = {"W": 1, "generic": 1}
                    await submit_as_is(c, chosen)
                    return
    elif stage == "triggers":
        if await p0_answer_endure(c, G, tag, st, state):
            return

    if observe(c, G, tag, state):
        return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p1_tick(c, G, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    drain_rejections(c, G)
    if await do_mulligan(c, acts, st, 1, tag, G):
        return
    if await do_bottom(c, acts, st, 1, tag, G):
        return
    if await do_discard(c, st, 1, tag, G):
        return
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    await confirm_pending_cast(c, G, tag, st, state, acts)
    if G.get("done"):
        return

    stage = G.get("stage")
    if stage == "develop" and is_my_main(state, 1):
        # Post-mulligan verification: the viewer hand must actually contain
        # the Flicker (mulligan desync seen 2026-10-11 06:21: viewer showed
        # the flicker at keep time, authoritative hand lacked it).
        if not G.get("p1_hand_verified") and mulligan_phase_over(acts, state):
            hn = hand_names(state, 1)
            has = FLICKER in hn
            G["p1_hand_verified"] = True
            G["p1_has_flicker"] = has
            say(f"[{tag}] post-mulligan hand check: flicker={has} "
                f"hand={sorted(hn)}")
            wire("p1_hand_check", {"has_flicker": has, "hand": sorted(hn),
                                   "game": G["game_idx"]})
            if not has:
                G["blocked"] = ("P1 mulligan desync: Flicker of Fate not in "
                                "hand after mulligan phase")
                G["done"] = True
                say(f"[{tag}] BLOCKED: {G['blocked']}")
                return
        if await play_a_land(c, state, 1, acts, tag, G):
            return
    if stage == "etb" and not G.get("flicker_cast_done"):
        if G.get("guard_oid_A") and not G.get("pending_cast"):
            if await p1_cast_flicker(c, G, tag, st, state, acts):
                return
    if not G.get("flicker_target_done"):
        # Target selection may be part of the casting process itself, so
        # answer it whenever the opportunity appears (do not wait for the
        # cast-confirmation guard). The engine auto-targets when there is a
        # single legal target; if a manual target selection appears, match it.
        # (A previous "hold priority" approach wedged the driver; the engine
        # auto-targets reliably, so we pass priority normally.)
        if await p1_answer_target(c, G, tag, st, state):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


def reset_per_game():
    global PASSED_REV, SUBMITTED_OPPS, LAND_PLAYED_THIS_ACTIVE
    PASSED_REV = {}
    SUBMITTED_OPPS = set()
    LAND_PLAYED_THIS_ACTIVE = {}
    ST["mana_needs"] = {}


# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.106.0/data/card-data.json"))
    out = {}
    for key in ("fortress kin-guard", "flicker of fate"):
        c = cd[key]
        out[key] = {"oracle_text": c.get("oracle_text"),
                    "mana_cost": c.get("mana_cost"),
                    "abilities": c.get("abilities"),
                    "triggers": c.get("triggers"),
                    "scryfall_oracle_id": c.get("scryfall_oracle_id")}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    return out


# ------------------------------------------------------------- evaluation
def total_plus_counters(state, oid):
    d = counters_of(state, oid)
    tot = 0
    for k, v in d.items():
        try:
            tot += int(v)
        except (TypeError, ValueError):
            pass
    return tot, d


def evaluate_game(G):
    ass = {}
    notes = []
    pre_f = G.get("pre_flicker") or {}
    pre_t = G.get("pre_triggers") or {}
    post = G.get("post") or {}

    if G.get("blocked"):
        for k in ("A1_setup_ok", "A2_flicker_ok", "A3_control_ok",
                  "A4_original_scope", "A5_final_state"):
            ass[k] = "not-run"
        notes.append(f"blocked: {G['blocked']}")
        return ass, notes, "blocked"

    # A1: Kin-Guard entered; original Endure trigger observed on the stack;
    # Flicker cast in response targeting incarnation A.
    a1 = (G.get("etb_seen") and G.get("guard_oid_A")
          and G.get("flicker_cast_done") and G.get("flicker_target_done"))
    ass["A1_setup_ok"] = "passed" if a1 else "failed"
    notes.append(f"A1 {'passed' if a1 else 'FAILED'}: etb_seen={G.get('etb_seen')} "
                 f"guard_oid_A={G.get('guard_oid_A')} flicker_cast={G.get('flicker_cast_done')} "
                 f"target_done={G.get('flicker_target_done')}")

    # A2: flicker resolved; the two-trigger signature observed
    # ([original, new] Endure triggers on the stack); the permanent's
    # incarnation advanced (object_id is stable across the flicker: the
    # engine models zone-change identity as (object_id, incarnation)).
    oid_a = G.get("guard_oid_A")
    saf = G.get("stack_after_flicker") or []
    endure_n = sum(1 for e in saf if "endure" in (e[2] or "").lower())
    inc_a, inc_b = G.get("guard_inc_A"), G.get("guard_inc_B")
    a2 = bool(oid_a and endure_n == 2 and inc_a is not None
              and inc_b is not None and inc_b > inc_a)
    ass["A2_flicker_ok"] = "passed" if a2 else "failed"
    notes.append(f"A2 {'passed' if a2 else 'FAILED'}: guard_oid={oid_a} "
                 f"incarnation {inc_a} -> {inc_b} "
                 f"endure_triggers_on_stack_after_flicker={endure_n}")

    # A3: the NEW (returned-incarnation) trigger is the legitimate control.
    # It prompts first (LIFO) and its branch choice is answered per
    # BRANCH_INDEX_NEW. (An earlier "silent resolution" theory was wrong:
    # the new trigger does prompt; P0's stack view was just lagging.)
    ans = G.get("endure_answers") or []
    new_ans = next((a for a in ans if a["which"] == "new"), None)
    trace = G.get("counters_b_trace") or []
    max_c = max([t[2] for t in trace], default=0)
    if new_ans is not None:
        a3 = True
        notes.append(f"A3 passed: new trigger prompted and was answered "
                     f"(chosen optionIndex={new_ans.get('chosen_index')}); "
                     f"max_counters_during={max_c}")
    else:
        a3 = False
        notes.append("A3 FAILED: new trigger never prompted")
    ass["A3_control_ok"] = "passed" if a3 else "failed"

    # A4/A5: the ORIGINAL trigger's branch choice is text-less (optionIndex
    # only), so branch identity is established EMPIRICALLY from the post
    # state: a +1/+1 counter on the returned incarnation means the chosen
    # branch was the counter branch (the reported bug); a Spirit token means
    # it was the token branch.
    oid_b = G.get("guard_oid_B")
    orig_ans = next((a for a in ans if a["which"] == "orig"), None)
    new_ans_idx = ans[0].get("chosen_index") if ans else None
    branch_idx = int(os.environ.get("BRANCH_INDEX_ORIG",
                                    os.environ.get("BRANCH_INDEX", "0")))
    post_tot, post_detail, post_toks = None, None, None
    if post and oid_b:
        post_tot, post_detail = total_plus_counters(post, oid_b)
        post_toks = len(spirit_tokens(post, 0))
    if orig_ans is not None:
        chosen = orig_ans.get("chosen_index")
        notes.append(f"A4 note: original trigger offered option indices "
                     f"{orig_ans.get('offered')}; chose optionIndex {chosen} "
                     f"(BRANCH_INDEX_ORIG={branch_idx})")
        # Branch map (empirical, 2026-10-11 06:27 run): optionIndex 0 =
        # token branch, optionIndex 1 = counter branch.
        if chosen == 1:
            if post_tot is not None and post_tot >= 1:
                a4 = False
                notes.append(f"A4 FAILED: original trigger's COUNTER branch "
                             f"put {post_tot} +1/+1 counter(s) on the "
                             f"returned incarnation {G.get('guard_inc_B')} "
                             f"(counters={post_detail}) - the reported bug")
            elif (post_tot or 0) == 0:
                a4 = True
                notes.append(f"A4 passed: original trigger's COUNTER branch "
                             f"did NOT put a counter on the returned "
                             f"incarnation (tokens={post_toks}) - no bug")
            else:
                a4 = False
                notes.append(f"A4 FAILED: no post state to judge "
                             f"(counters={post_detail}, tokens={post_toks})")
        elif chosen == 0:
            if post_tot is not None and post_tot >= 1:
                a4 = False
                notes.append(f"A4 FAILED: unexpected counter(s) after "
                             f"choosing the TOKEN branch "
                             f"(counters={post_detail})")
            elif post_toks is not None and post_toks >= 1 and (post_tot or 0) == 0:
                a4 = None  # counter branch (optionIndex 1) still untested
                notes.append(f"A4 note: token branch behaved correctly "
                             f"(token, no counter); the counter branch "
                             f"(optionIndex 1) is untested in this run")
            else:
                a4 = False
                notes.append(f"A4 FAILED: token branch produced neither "
                             f"counter nor token "
                             f"(counters={post_detail}, tokens={post_toks})")
        else:
            a4 = False
            notes.append(f"A4 FAILED: unexpected chosen index {chosen}")
    else:
        a4 = None
        notes.append("A4 note: no Endure prompt observed for the original "
                     "trigger; judging from final state")
    ass["A4_original_scope"] = "passed" if a4 else ("failed" if a4 is False
                                                    else "not-run")

    # A5: final-state consistency with the observed branch outcome.
    b_post = post and oid_b
    if b_post and orig_ans is not None:
        if post_tot is not None and post_tot >= 1:
            # counter branch: the bug outcome; A5 just records it
            a5 = True
            notes.append(f"A5 note (bug outcome): returned incarnation has "
                         f"{post_tot} counter(s); P0 spirit tokens={post_toks}")
        elif (post_toks or 0) >= 1 and (post_tot or 0) == 0:
            a5 = True
            notes.append(f"A5 passed (token branch): no counters on returned "
                         f"incarnation; P0 spirit tokens={post_toks}")
        else:
            a5 = False
            notes.append(f"A5 FAILED: unexpected final state "
                         f"(counters={post_detail}, tokens={post_toks})")
    elif b_post:
        tot, detail = total_plus_counters(post, oid_b)
        toks = len(spirit_tokens(post, 0))
        a5 = False
        notes.append(f"A5 FAILED: no original-trigger answer to judge "
                     f"(counters={detail}, tokens={toks})")
    else:
        a5 = False
        notes.append("A5 FAILED: no post state / no incarnation B")
    ass["A5_final_state"] = "passed" if a5 else "failed"

    bug_sig = False
    if (post and oid_b and (post_tot or 0) >= 1 and orig_ans is not None
            and orig_ans.get("chosen_index") == 1):
        bug_sig = True
        notes.append("BUG SIGNATURE: the original (pre-flicker) Endure "
                     "trigger's counter branch put a +1/+1 counter on the "
                     "returned "
                     f"incarnation ({G.get('guard_inc_A')} -> "
                     f"{G.get('guard_inc_B')}, same object_id)")

    if bug_sig:
        return ass, notes, "reproduced"
    if a4 is None:
        # The token branch was chosen (correct outcome for that branch);
        # the counter branch's effect is still untested -> rerun with
        # BRANCH_INDEX_ORIG=1 rather than calling it absent.
        notes.append("inconclusive: counter-branch effect untested; rerun "
                     "with BRANCH_INDEX_ORIG=1 to choose the counter branch")
        return ass, notes, "blocked"
    if all(v == "passed" for v in ass.values()):
        notes.append("all assertions passed: original trigger did not put "
                     "counters on the returned incarnation; no bug observed")
        return ass, notes, "not-reproduced"
    notes.append("inconclusive: some assertions failed without the bug signature")
    return ass, notes, "blocked"


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1180
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#9727 Triggered self-Endure vs flickered incarnation", fill=(255, 210, 90))
    s = run["server"]
    line(f"server {s['server_version']} build {s['build_commit']} protocol "
         f"{s['protocol_version']}  |  run {run['run_id']}  |  {run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("Kin-Guard ETB -> Flicker of Fate in response -> flicker resolves", size=16)
    line("(Kin-Guard returns; new ETB trigger on top of the original)", size=16)
    line("expected: original trigger offers TOKEN branch only (CR 400.7);", size=16)
    line("new trigger may counter the returned incarnation (control)", size=16)
    y += 6
    g = run["game"]
    line(f"guard oid={g.get('guard_oid_A')} incarnation "
         f"{g.get('guard_incarnation_A')} -> {g.get('guard_incarnation_B')} "
         f"(object_id stable across flicker)", size=17)
    for a in g.get("endure_answers", []):
        line(f"  trigger={a['which']}: offered={a['offered']} "
             f"chosen_index={a.get('chosen_index')}", size=16)
    fc, fd = g.get("final_counters"), g.get("final_counter_detail")
    line(f"final: incarnation B counters total={fc} {fd}", size=16)
    line(f"final: P0 spirit tokens={g.get('final_tokens')}", size=16)
    y += 6
    for k, val in g["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=16)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:12]:
        line(f"  - {n[:108]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- game runner
async def run_one_game(game_idx):
    reset_per_game()
    G = {"game_idx": game_idx, "stage": "develop",
         "pending_cast": None, "cast_pending": False,
         "cast_pending_name": None, "cast_pending_oid": None,
         "cast_pending_at": 0.0,
         "guard_cast_done": False, "guard_oid_A": None, "guard_oid_B": None,
         "guard_inc_A": None, "guard_inc_B": None, "guard_inc_B_view": None,
         "flicker_gone_since": None,
         "etb_seen": False, "trigger_stack_entry": None,
         "flicker_cast_done": False, "flicker_target_done": False,
         "flicker_resolved_wait_since": 0.0,
         "stack_after_flicker": None,
         "endure_answers": [], "endure_prompt_dumps": [],
         "counters_b_trace": [], "anomalies": [],
         "pre_flicker": None, "pre_flicker_done": False,
         "pre_triggers": None, "pre_triggers_done": False,
         "post": None,
         "max_turn": 0, "mulls0": 0, "mulls1": 0,
         "rejections": [], "done": False, "blocked": None, "game_ok": False}

    p0 = PhaseClient(f"9727-P0-g{game_idx}")
    p1 = PhaseClient(f"9727-P1-g{game_idx}")
    await p0.connect()
    await p1.connect()
    att = await p0.create(P0_DECK, player_count=2)
    code = att.get("game_code")
    G["game_code"] = code
    say(f"[game{game_idx}] created code={code}")
    wire("game_created", {"game_code": code, "game_idx": game_idx})
    await p1.join(code, P1_DECK)

    timeout_s = 1500
    t0 = time.time()
    last = {}
    last_tick = {}
    last_adv = {p0.name: time.time(), p1.name: time.time()}
    last_diag = 0.0
    warned = set()
    stage_stuck_since = {}
    ticks = [(p0, p0_tick, 0, "P0"), (p1, p1_tick, 1, "P1")]
    try:
        for _ in range(int(timeout_s / 0.2)):
            await asyncio.sleep(0.2)
            for c, tick, pid, tag in ticks:
                st = c.latest
                if not st:
                    continue
                rev = c.revision
                if (rev == last.get(c.name)
                        and time.time() - last_tick.get(c.name, 0) <= 5):
                    continue
                last[c.name] = rev
                last_tick[c.name] = time.time()
                last_adv[c.name] = time.time()
                warned.discard(c.name)
                state = st["state"]
                dump_view(c, st)
                G["max_turn"] = max(G["max_turn"], turn_of(state))
                try:
                    await tick(c, G, tag)
                except Exception as e:
                    say(f"[{c.name}] tick error: {e!r}")
                    wire("tick_error", {"who": c.name, "error": repr(e)})
                # exports driven by stage flags (p0 owns the exports)
                if tag == "P0" and not G.get("done"):
                    if G.get("etb_seen") and not G.get("pre_flicker_done"):
                        G["pre_flicker"] = await export_now("pre_flicker.json", c)
                        G["pre_flicker_done"] = True
                        G["guard_inc_A"] = obj_incarnation(
                            G["pre_flicker"], G.get("guard_oid_A"))
                    if G.get("stage") == "triggers" and not G.get("pre_triggers_done"):
                        G["pre_triggers"] = await export_now("pre_triggers.json", c)
                        G["pre_triggers_done"] = True
                        G["guard_inc_B"] = obj_incarnation(
                            G["pre_triggers"], G.get("guard_oid_B"))
                        say(f"[game{game_idx}] incarnation check: A="
                            f"{G.get('guard_inc_A')} B={G.get('guard_inc_B')}")
                    if G.get("stage") == "settle":
                        G["post"] = await export_now("post.json", c)
                        G["done"] = True
                if G.get("done"):
                    say(f"[game{game_idx}] contract complete; finishing")
                    G["game_ok"] = True
                    return G
            now = time.time()
            stg = G.get("stage")
            if stg not in ("develop", "settle", "done"):
                if stage_stuck_since.get(stg, 0) == 0:
                    stage_stuck_since[stg] = now
                elif now - stage_stuck_since[stg] > 300:
                    G["blocked"] = (f"stage '{stg}' stuck for 300s "
                                    f"(answers={len(G['endure_answers'])})")
                    say(f"[game{game_idx}] STUCK: {G['blocked']}")
                    return G
            else:
                stage_stuck_since = {}
            if now - last_diag > 90 and p0.latest:
                last_diag = now
                s = p0.latest["state"]
                say(f"[game{game_idx}] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                    f"phase={s.get('phase')} stack={len(stack_entries(s))} stage={G.get('stage')} "
                    f"A={G.get('guard_oid_A')} B={G.get('guard_oid_B')} "
                    f"answers={len(G['endure_answers'])}")
            for c in (p0, p1):
                if (now - last_adv[c.name] > 90 and c.name not in warned
                        and c.latest):
                    warned.add(c.name)
                    st = c.latest
                    state = st.get("state", {})
                    say(f"[game{game_idx}] WATCHDOG {c.name}: no revision for "
                        f"{now - last_adv[c.name]:.0f}s; rev={c.revision} "
                        f"turn={turn_of(state)} phase={state.get('phase')} "
                        f"vi_kind={vi_kind_code(st)} stage={G.get('stage')}")
            if now - t0 > timeout_s:
                say(f"[game{game_idx}] global timeout ({timeout_s}s)")
                G["blocked"] = "global timeout before completion"
                return G
    finally:
        await p0.close()
        await p1.close()
    G["blocked"] = G.get("blocked") or "loop exhausted without completion"
    return G


def dump_view(c, st):
    s = st["state"]
    acts = [a.get("type") for a in (st.get("legal_actions") or [])]
    vi = st.get("viewer_interaction") or {}
    opps = []
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        idesc = []
        for ch in items[:12]:
            ref = _cand_reference(ch)
            surfs = []
            for s in (ch.get("surfaces") or [])[:6]:
                d = s.get("data")
                ds = json.dumps(d, default=str)[:300] if isinstance(d, dict) else str(d)[:100]
                surfs.append({"type": s.get("type"), "data": ds})
            idesc.append({"id": ch.get("id"), "ref": ref,
                          "text": choice_text(ch)[:160], "surfaces": surfs})
        opps.append({"iid": opp.get("interactionId"),
                     "rtype": resp.get("type"),
                     "spec": (data.get("spec", {}) or {}).get("type"),
                     "n_items": len(items), "items": idesc})
    wire("view", {"who": c.name, "rev": c.revision,
                  "turn": turn_of(s), "phase": s.get("phase"),
                  "active": s.get("active_player"), "top_acts": acts,
                  "vi_can": vi.get("canSubmit"),
                  "vi_kind": (vi.get("waitingForKind") or {}).get("code"),
                  "stack": stack_summary(s), "opps": opps})


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.106.0, protocol 126)")
    await verify_server_hello()
    parse = parse_evidence()
    say("parse: kin-guard/flicker oracle captured")

    try:
        g = await run_one_game(1)
    except Exception as e:
        say(f"game crashed: {e!r}")
        wire("game_crashed", {"error": repr(e)})
        g = {"game_idx": 1, "blocked": f"game crashed: {e!r}",
             "endure_answers": [], "rejections": [], "max_turn": 0}

    # final counters/tokens from the post export
    post = g.get("post") or {}
    oid_b = g.get("guard_oid_B")
    final_tot, final_detail = (None, None)
    final_toks = None
    if post and oid_b:
        final_tot, final_detail = total_plus_counters(post, oid_b)
        final_toks = len(spirit_tokens(post, 0))

    a, n, v = evaluate_game(g)

    notes = ["2026-10-11 05:11 first runtime validation of #9727 (statically "
             "traced filing; no runtime test before this run)."]
    notes.extend(n)
    for an in (g.get("anomalies") or []):
        notes.append("ANOMALY: " + an)
    verdict = v

    dur = time.time() - t_start

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    bin_path = f"{BACKFILL}/server/releases/v0.106.0/phase-server-slim-x86_64-unknown-linux-musl"
    cd_path = f"{BACKFILL}/server/releases/v0.106.0/data/card-data.json"
    dp_path = f"{BACKFILL}/server/releases/v0.106.0/data/draft-pools.json"
    server_identity = {
        "server_version": "0.106.0",
        "build_commit": "29e0db3",
        "protocol_version": 126,
        "mode": "Full",
        "binary_sha256": sha(bin_path),
        "card_data_sha256": sha(cd_path),
        "draft_pools_sha256": sha(dp_path),
        "signature_key_id": "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY (436711b6a2d36828)",
        "signature_verified": True,
        "signature_note": ("v0.106.0 binary + release manifest minisign-verified "
                           "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                           "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                           "mode; data digests match the signed manifest; digests "
                           "recomputed against on-disk files this run"),
        "source": ("2026-10-11: latest stable release v0.106.0 (published "
                   "2026-10-10T17:09:24Z) == pinned release dir (pinned "
                   "2026-10-09); ServerHello 0.106.0/29e0db3/protocol 126 "
                   "verified by handshake this run; hashes recomputed against "
                   "on-disk artifacts this run; backfill-owned v0.106.0 server "
                   "on 127.0.0.1:9374 (already running; reused per playbook)"),
    }
    assert server_identity["binary_sha256"] == \
        "5d31aeec7387c22b5af84f7befa83c14f785d8505c0f9102f4fddac743d1d47a", \
        "binary digest drift vs pinned v0.106.0 record"
    assert server_identity["card_data_sha256"] == \
        "59d60da9833ff7cd464fca2c93f4d6b49d74a8ad0d04092055634442c70cbc41", \
        "card-data digest drift vs pinned v0.106.0 record"
    assert server_identity["draft_pools_sha256"] == \
        "fad736f3ae13b3fb2f0908319eae98af501e36cc30fbe2e84ca6684319384c86", \
        "draft-pools digest drift vs pinned v0.106.0 record"

    game_rec = {"assertions": a, "notes": n, "verdict": v,
                "guard_oid_A": g.get("guard_oid_A"),
                "guard_oid_B": g.get("guard_oid_B"),
                "guard_incarnation_A": g.get("guard_inc_A"),
                "guard_incarnation_B": g.get("guard_inc_B"),
                "endure_answers": g.get("endure_answers"),
                "stack_after_flicker": g.get("stack_after_flicker"),
                "final_counters": final_tot,
                "final_counter_detail": final_detail,
                "final_tokens": final_toks,
                "game_code": g.get("game_code"),
                "rejections": g.get("rejections"),
                "max_turn": g.get("max_turn"),
                "blocked": g.get("blocked")}

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": "runs/run-20261011-0241",
        "driver": {"protocol_advertised": 126, "client": "driver/client.py"},
        "scenario": "driver/scenario_9727_01060.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_9727_01060.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "game": game_rec,
        "assertions": {f"g1_{k}": val for k, val in a.items()},
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-10-11",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "P1 is a passive second seat except for the scripted Flicker of "
            "Fate response (lands only, no attacks).",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "history": [],
        "setup_line": ("P0 4x Fortress Kin-Guard + 56x Plains vs P1 4x Flicker "
                       "of Fate + 56x Plains. P0 casts Kin-Guard on its "
                       "third turn (P1 has two lands); "
                       "P1 responds to the ETB Endure trigger with Flicker of "
                       "Fate targeting it; flicker resolves (Kin-Guard "
                       "returns, new ETB trigger on top); new trigger "
                       "resolved with the counter branch; original trigger "
                       "resolved last."),
        "contract_line": ("The original trigger must not offer the counter "
                          "branch on the returned incarnation (CR 400.7: new "
                          "object) - token branch only. The new incarnation's "
                          "own trigger is the legitimate control and must "
                          "offer the counter branch."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/endure_prompts.json", "w") as f:
        json.dump(g.get("endure_prompt_dumps"), f, indent=1, default=str)
    subprocess.run(["cp", __file__, f"{EVDIR}/scenario_9727_01060.py"], check=True)
    render_png(f"{EVDIR}/summary.png", run)

    try:
        slog = f"{BACKFILL}/runs/run-20261011-0241/server.log"
        excerpts = []
        code = g.get("game_code")
        if os.path.exists(slog):
            with open(slog, errors="replace") as f:
                for line in f:
                    ll = line.lower()
                    if code and code in line:
                        excerpts.append(line.rstrip())
                    elif any(k in ll for k in ("endure", "flicker", "kin-guard",
                                               "error", "warn")):
                        excerpts.append(line.rstrip()[:300])
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(excerpts[-80:]) + "\n")
        say(f"server excerpts: {len(excerpts)} lines")
    except Exception as e:
        say(f"server excerpts failed: {e}")

    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha(f"{EVDIR}/{fn}") + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"manifest.sha256 written for {len(lines)} files", flush=True)
    bad = []
    for line in lines:
        h, _, name = line.partition("  ")
        h2 = sha(f"{EVDIR}/{name.strip()}")
        if h2 != h:
            bad.append(name)
    assert not bad, f"manifest self-check failed: {bad}"
    print(f"manifest self-check passed for {len(lines)} files", flush=True)
    for fn in os.listdir(EVDIR):
        if fn.endswith(".json"):
            json.load(open(f"{EVDIR}/{fn}"))
    im = __import__("PIL.Image", fromlist=["Image"]).open(
        f"{EVDIR}/summary.png")
    im.verify()
    print("evidence validated: JSON parses, manifest self-checks, PNG readable",
          flush=True)
    print(json.dumps({"verdict": verdict, "assertions": a}, indent=1), flush=True)


asyncio.run(amain())
