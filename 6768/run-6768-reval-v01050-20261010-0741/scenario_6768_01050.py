#!/usr/bin/env python3
"""Issue #6768: Bruna and Gisela don't merge. -- they just exile themselves.

Protocol-120 port (v0.105.0, 2026-10-10) of scenario_6768_01040.py
(verified 2026-10-09, run run-6768-reval-v01040-20261009-0711,
v0.104.0/protocol 118).
Cast-confirmation guard (per the 2026-10-09 #6765 lesson): on protocol 120
a bare CastSpell (payment_mode Auto, engine auto-pays) can lose a race to
the driver's own PassPriority, so after every CastSpell the driver holds
(no priority pass, no new main-phase plays) until the spell is confirmed
(object leaves the hand: stack/battlefield/graveyard/exile/command, or a
same-name object found on the stack); a 30s backstop clears a
still-in-hand cast as dropped and resets mana_needs/bruna_cast_pending so
the cast block retries.

Reported (Discord): "they just exile thmeselves."
[[gisela, the broken blade]][[bruna, the fading light]]

Oracle (pinned v0.105.0 card-data.json; re-verified by check_data_level each run, unchanged since v0.103.0):
  Gisela, the Broken Blade (3W, 4/3, Flying/first strike/lifelink):
    "At the beginning of your end step, if you both own and control Gisela
     and a creature named Bruna, the Fading Light, exile them, then meld
     them into Brisela, Voice of Nightmares."
  Bruna, the Fading Light (5WW, 5/7, Flying/vigilance, meld partner):
    "When you cast this spell, you may return target Angel or Human
     creature card from your graveyard to the battlefield."
Parser state (v0.105.0 data; re-verified each run): Gisela's end-step trigger still carries the
supported {"type": "Meld", "source": "Gisela, the Broken Blade",
"partner": "Bruna, the Fading Light", "result": "Brisela, Voice of
Nightmares"} effect payload (mode "Phase", phase "End", condition
own+control both). Brisela exists in card data.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, v0.105.0/protocol 120):
  P0: 12x Bruna, the Fading Light + 12x Gisela, the Broken Blade + 36x Plains.
      Mulligan: always keep 7.
  P1: 60x Forest dummy (plays a land, passes; never attacks/blocks).

Expected (per card text):
  RAMP: P0 plays Plains, casts Gisela (3W), then Bruna (5WW). Bruna's
        cast trigger (may return Angel/Human from gy — gy is empty) is
        DECLINED via the 106 decideOptionalEffect bare "false" choice
        (per the #7195 driver lesson). No attacks (empty declarations).
  PRE:  at P0's PostCombatMain with both halves on P0's battlefield:
        export pre.json, stage -> OBSERVE.
  OBSERVE: let the end-step trigger run its course; export mid_trigger.json
        while a Meld/Gisela/Brisela entry is on the stack; export
        post_trigger.json once the stack empties in EndStep/Cleanup
        (fallback: P1's turn); export post.json when P1's turn begins.

Assertions:
  A1 setup_ok        pre.json: Gisela + Bruna on P0 BF, life 20/20.
  A2 trigger_fires    Meld trigger observed (stack entry or
                      triggers_fired_this_turn mentioning Meld/Gisela).
  A3 exile_observed   both halves leave the battlefield for Exile around
                      the trigger resolution.
  A4 meld_correct     Brisela, Voice of Nightmares on P0's battlefield after
                      resolution (the reported outcome is its absence).
  A5 cleanup           stack empty in post.json; game advanced.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and A4 fails.
Verdict = not-reproduced iff A1..A5 all pass.

Evidence: evidence/6768/<run-id>/pre.json, mid_trigger.json,
post_trigger.json, post.json, run.json, manifest.sha256, summary.png,
scenario_6768.py, wire_log.jsonl, scenario_run.log, data_evidence.json,
server_excerpts.log
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

ISSUE = 6768
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-6768-reval-v01050-20261010-0741"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"))

BRUNA = "bruna, the fading light"
GISELA = "gisela, the broken blade"
BRISELA = "brisela, voice of nightmares"
PLAINS = "plains"
FOREST = "forest"

P0_DECK = [("Bruna, the Fading Light", 12), ("Gisela, the Broken Blade", 12),
           ("Plains", 36)]
P1_DECK = [("Forest", 60)]

# mana needs: Gisela 3W, Bruna 5WW
NEEDS_GISELA = {"W": 3, "generic": 1}
NEEDS_BRUNA = {"W": 2, "generic": 5}

ST = {"stage": "RAMP", "stop": False,
      "cast_pending": None,
      "bruna_cast_pending": False, "bruna_declined": False,
      "mid_exported": False, "post_trigger_exported": False,
      "mana_needs": {}, "pre_turn": None, "stall_since": None,
      "states_seen": 0, "trigger_observations": 0}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ------------------------------------------------------------------ helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def is_land(o):
    # covers Plains/Forest and every other land the seats can hold
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def hand_ids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def exile_by_name(state, lname):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Exile" and obj_lname(state, oid) == lname]


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def life(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        for a in opp.get("actions", []) or []:
            acts.append(a)
    return acts


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
            d = s.get("data") or {}
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


def stack_desc(state):
    out = []
    for sid in state.get("stack", []) or []:
        o = get_obj(state, sid)
        out.append({"id": sid,
                    "name": o.get("base_name") or o.get("name") or "?",
                    "zone": o.get("zone"),
                    "kind": str(o.get("kind", ""))[:80]})
    return out


def meldish(blob):
    b = str(blob).lower()
    return ("meld" in b or "gisela" in b or "brisela" in b or "bruna" in b)


# ------------------------------------------------------- interaction prims

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


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = json.loads(raw).get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.105.0"), f"unexpected version {ver}"
    assert int(proto) == 120, f"unexpected protocol {proto}"
    assert str(build) == "965e243", f"unexpected build {build}"
    return ver, build, proto


def check_data_level():
    ok, notes = True, []
    g = CARD_DATA.get(GISELA, {})
    oracle = str(g.get("oracle_text", ""))
    if "meld" not in oracle.lower() or "brisela" not in oracle.lower():
        ok = False
        notes.append("gisela oracle text shape missing")
    meld_payload = False
    for t in g.get("triggers", []) or []:
        eff = ((t.get("execute") or {}).get("effect")) or {}
        if eff.get("type") == "Meld" \
                and str(eff.get("result", "")).lower() == BRISELA:
            meld_payload = True
    if not meld_payload:
        ok = False
        notes.append("gisela Meld effect payload missing from parsed data")
    b = CARD_DATA.get(BRUNA, {})
    if not any("cast this spell" in str(t.get("description", "")).lower()
               for t in b.get("triggers", []) or []):
        notes.append("bruna cast trigger missing from parsed data")
    if not CARD_DATA.get(BRISELA):
        ok = False
        notes.append("brisela missing from card data")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "gisela_oracle": oracle[:300],
                   "meld_payload_present": meld_payload}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    hn = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        codes = set()
        for ch in cands:
            codes.update(x for x in surf_codes(ch) if x)
        if not any("discard" in (x or "").lower() for x in codes) \
                and "discard" not in str(opp.get("description", "")).lower():
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"

        def rank(o):
            nm = obj_lname(state, o)
            if is_land(get_obj(state, o)):
                return (0, nm)
            if nm in (BRUNA, GISELA):
                return (3, nm)
            return (2, nm)

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
        say(f"[{tag}] tap land for mana used_for={used} needs_left={needs}")
        wire("tap_land", {"who": tag, "used_for": used, "needs_left": dict(needs or {})})
        iid2 = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        cid = pick.get("id")
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid2,
                   "response": {"type": stype, "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid2,
                   "response": {"type": "choose", "data": {"choiceId": cid}}}
        await interact_as(c, sub, tag)
        return True
    return False


def cast_zone(state, oid):
    return str(get_obj(state, oid).get("zone") or "").lower()


def cast_guard_tick(ctx, state, tag):
    """Protocol-120 cast-confirmation guard (2026-10-09 lesson), ST-adapted.

    Returns "hold" while our CastSpell is in flight but unconfirmed: the
    driver must not pass priority or start new main-phase plays in that
    window -- a bare CastSpell can lose a race to our own PassPriority
    submitted on the next tick and be silently dropped by the server.
    Returns "proceed" once confirmed; a 30s backstop clears a
    still-unconfirmed cast and resets mana_needs[tag] + bruna_cast_pending
    so the cast block re-triggers and retries. Must run BEFORE any
    early-return block (attackers/blockers); mana taps / may-declines /
    empty attacker declarations stay legal while holding."""
    pend = ctx.get("cast_pending")
    if not pend:
        return "proceed"
    zone = cast_zone(state, pend["oid"])
    if zone in ("stack", "battlefield", "graveyard", "exile", "command"):
        kind = pend["kind"]
        ctx["cast_pending"] = None
        ctx[kind + "_cast"] = True
        say(f"[{tag}] cast confirmed on {zone}: {pend['name']} "
            f"(oid {pend['oid']})")
        wire("cast_confirmed", {"kind": kind, "oid": pend["oid"],
                                "zone": zone})
        return "proceed"
    # fallback: the spell may live under a new object id on the stack
    want = pend["name"].split(" (")[0].lower()
    for oid2, o2 in (state.get("objects") or {}).items():
        nm = str(o2.get("base_name") or o2.get("name") or "").lower()
        if nm == want and str(o2.get("zone") or "").lower() == "stack":
            ctx["cast_pending"] = None
            ctx[pend["kind"] + "_cast"] = True
            say(f"[{tag}] cast confirmed (stack name-scan, oid {oid2}): "
                f"{pend['name']}")
            wire("cast_confirmed", {"kind": pend["kind"], "oid": str(oid2),
                                    "zone": "stack", "via": "name_scan"})
            return "proceed"
    if zone == "hand" and time.time() - pend["since"] > 30:
        ctx["cast_pending"] = None
        kind = pend["kind"]
        ctx["mana_needs"][tag] = {}
        if kind == "bruna":
            ctx["bruna_cast_pending"] = False
        say(f"[{tag}] cast NOT confirmed after 30s (still in hand): "
            f"{pend['name']} (oid {pend['oid']}); will retry")
        wire("cast_dropped_retry", {"kind": kind, "oid": pend["oid"]})
        return "proceed"
    return "hold"


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
                await interact_as(c, {"interactionId": opp.get("interactionId"),
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def can_pay(state, pid, needs):
    pool = [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")]
    return len(pool) >= sum(needs.values())


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


async def decline_bruna_may(c, st, tag):
    """Decline Bruna's cast trigger via the 106 decideOptionalEffect bare
    "false" choice. Gated on bruna_cast_pending + not bruna_declined (the
    only decideOptionalEffect P0 can see in this game), per the #7195 lesson."""
    if not ST["bruna_cast_pending"] or ST["bruna_declined"]:
        return False
    state = st["state"]
    if bf_by_name(state, 0, BRUNA):
        # already resolved; nothing to decline
        ST["bruna_cast_pending"] = False
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        if "decideOptionalEffect" not in codes:
            continue
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            return True
        wire("bruna_prompt_shape",
             {"iid": iid,
              "choices": [(ch.get("id"), choice_text(ch)[:60],
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
        if pick is None:
            say(f"[{tag}] bruna may: no decline choice identified; deferring")
            wire("bruna_no_decline_found",
                 {"choices": [(ch.get("id"), choice_text(ch)) for ch in chs]})
            return True
        SUBMITTED_OPPS.add(iid)
        ST["bruna_declined"] = True
        ST["bruna_cast_pending"] = False
        say(f"[{tag}] Bruna may-return DECLINED (choice {pick})")
        wire("bruna_declined", {"iid": iid, "choice": pick})
        rtype = resp.get("type")
        if rtype == "schema":
            spec = (data.get("spec", {}) or {}).get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": spec, "data": {"choiceIds": [pick]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose", "data": {"choiceId": pick}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------------ ticks

async def p1_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return
    # cast-confirmation guard (protocol 120): hold while a CastSpell is in
    # flight but unconfirmed. Mana taps / may-declines / empty attacker
    # declarations stay legal while holding; priority passes and new
    # main-phase plays wait. Runs before any early-return block so a
    # dropped cast is always detected (30s backstop, then retry).
    holding = cast_guard_tick(ST, state, tag) == "hold"
    if await decline_bruna_may(c, st, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        # PRE checkpoint: both halves on BF at P0's combat declaration step
        if (not ST.get("pre_exported") and ST["stage"] == "RAMP"
                and bf_by_name(state, 0, GISELA) and bf_by_name(state, 0, BRUNA)):
            pre = await do_export(c, "pre.json")
            ST["pre_exported"] = True
            ST["pre_turn"] = pre.get("turn_number")
            wire("pre_export",
                 {"gisela_bf": len(bf_by_name(pre, 0, GISELA)),
                  "bruna_bf": len(bf_by_name(pre, 0, BRUNA)),
                  "life": [life(pre, 0), life(pre, 1)]})
            ST["stage"] = "OBSERVE"
            say(f"[pre] both halves on P0 BF at turn {ST['pre_turn']}")
        if da:
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if holding:
        return
    if my_main(state, 0) and ST["stage"] == "RAMP":
        if await play_a_land(c, state, 0, acts, tag):
            return
        # cast Gisela (3W) when 4+ untapped lands, one copy only
        if not bf_by_name(state, 0, GISELA) \
                and any(obj_lname(state, o) == GISELA
                        for o in hand_ids(state, 0)) \
                and can_pay(state, 0, NEEDS_GISELA):
            a, oid = cast_action_for(acts, state, GISELA)
            if a is not None:
                ST["mana_needs"][tag] = dict(NEEDS_GISELA)
                ST["cast_pending"] = {"kind": "gisela", "oid": str(oid),
                                      "name": "Gisela, the Broken Blade",
                                      "since": time.time()}
                say(f"[{tag}] casting Gisela, the Broken Blade (oid {oid})")
                wire("cast_gisela", {"oid": oid})
                await submit_as_is(c, a)
                return
        # cast Bruna (5WW) when 7+ untapped lands, one copy only
        if not bf_by_name(state, 0, BRUNA) \
                and any(obj_lname(state, o) == BRUNA
                        for o in hand_ids(state, 0)) \
                and can_pay(state, 0, NEEDS_BRUNA):
            a, oid = cast_action_for(acts, state, BRUNA)
            if a is not None:
                ST["mana_needs"][tag] = dict(NEEDS_BRUNA)
                ST["bruna_cast_pending"] = True
                ST["cast_pending"] = {"kind": "bruna", "oid": str(oid),
                                      "name": "Bruna, the Fading Light",
                                      "since": time.time()}
                say(f"[{tag}] casting Bruna, the Fading Light (oid {oid})")
                wire("cast_bruna", {"oid": oid})
                await submit_as_is(c, a)
                return
    if real_decision_pending(st):
        return
    if holding:
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------------- main

async def main():
    t0 = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_trigger_fires", "A3_exile_observed",
            "A4_meld_correct", "A5_cleanup")}

    ver, build, proto = await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    last = {}
    last_tick_at = {}
    last_stack_sig = None
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if rev == last.get(c.name) and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, st, tag)
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        ST["states_seen"] += 1

        if ST["stage"] == "OBSERVE":
            sd = stack_desc(state)
            sig = json.dumps(sd, sort_keys=True, default=str)
            if sig != last_stack_sig:
                last_stack_sig = sig
                hits = [e for e in sd
                        if meldish(json.dumps(e, default=str))]
                if hits:
                    ST["trigger_observations"] += 1
                wire("stack", {"entries": sd,
                               "triggers_fired":
                                   state.get("triggers_fired_this_turn")})
                if hits:
                    say(f"[stack meld] {json.dumps(hits)[:400]}")

            # mid export: meld trigger on the stack
            if not ST["mid_exported"] and any(
                    meldish(json.dumps(e, default=str)) for e in sd):
                mid = await do_export(p0, "mid_trigger.json")
                ST["mid_exported"] = True
                wire("mid_trigger", {"phase": mid.get("phase"),
                                     "turn": mid.get("turn_number"),
                                     "stack": stack_desc(mid)})
                say(f"[mid] trigger on stack at phase={mid.get('phase')}")

            # stall watchdog: stack stuck >90s during OBSERVE
            if state.get("stack"):
                if ST["stall_since"] is None:
                    ST["stall_since"] = time.time()
                elif time.time() - ST["stall_since"] > 90:
                    await do_export(p0, "mid_stall.json")
                    notes.append("stall watchdog fired: stack non-empty >90s "
                                 "during OBSERVE")
                    say("[stall] watchdog fired; capturing mid_stall.json")
                    ST["stop"] = True
            else:
                ST["stall_since"] = None

            # post-trigger export: first stack-empty tick in EndStep/Cleanup.
            # The game advances fast after resolution (per the v0.86.0 run
            # lesson): fall back to capturing at P1's turn start if the
            # EndStep/Cleanup window was missed.
            if ST["mid_exported"] and not ST["post_trigger_exported"] \
                    and not (state.get("stack") or []) \
                    and ((state.get("phase") or "") in ("EndStep", "Cleanup")
                         or state.get("active_player") == 1):
                await asyncio.sleep(0.5)
                ptr = await do_export(p0, "post_trigger.json")
                ST["post_trigger_exported"] = True
                wire("post_trigger",
                     {"phase": ptr.get("phase"), "turn": ptr.get("turn_number"),
                      "active": ptr.get("active_player"),
                      "bf_brisela": exile_by_name(ptr, BRISELA),
                      "exile_bruna": exile_by_name(ptr, BRUNA),
                      "exile_gisela": exile_by_name(ptr, GISELA)})
                say(f"[post_trigger] phase={ptr.get('phase')} "
                    f"active={ptr.get('active_player')}")

            # post export: P1's turn begins (game advanced past resolution)
            if ST["post_trigger_exported"] and state.get("active_player") == 1:
                await asyncio.sleep(1.0)
                post = await do_export(p0, "post.json")
                wire("post", {"phase": post.get("phase"),
                              "turn": post.get("turn_number"),
                              "bf_brisela": len(bf_by_name(post, 0, BRISELA))})
                say(f"[post] phase={post.get('phase')} "
                    f"turn={post.get('turn_number')}")
                ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre.json")
    mid = env_state("mid_trigger.json")
    ptr = env_state("post_trigger.json")
    post = env_state("post.json")

    # A1
    if pre is None:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre.json was never exported (both halves never "
                     "on P0 BF at DeclareAttackers)")
    else:
        g = bf_by_name(pre, 0, GISELA)
        b = bf_by_name(pre, 0, BRUNA)
        ok = (len(g) >= 1 and len(b) >= 1
              and life(pre, 0) == 20 and life(pre, 1) == 20)
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"pre: gisela_bf={len(g)} bruna_bf={len(b)} "
                     f"life={[life(pre, 0), life(pre, 1)]}")

    # A2: trigger fired (stack entry or triggers_fired_this_turn)
    trig_hits = []
    for src, s in (("mid", mid), ("ptr", ptr), ("post", post)):
        if s is None:
            continue
        for entry in s.get("triggers_fired_this_turn") or []:
            blob = json.dumps(entry, default=str)
            if meldish(blob):
                trig_hits.append(f"{src}:triggers_fired:{blob[:160]}")
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "stack":
            for e in d["payload"].get("entries", []) or []:
                if meldish(json.dumps(e, default=str)):
                    trig_hits.append("stack:" + str(e.get("name")))
                    break
    trig_hits = list(dict.fromkeys(trig_hits))
    ass["A2_trigger_fires"] = "passed" if trig_hits else "failed"
    notes.append(f"trigger evidence hits: {trig_hits[:4] or 'none'}")

    # A3: both halves exiled around resolution
    ex_states = [s for s in (ptr, post) if s is not None]
    if ex_states:
        s = ex_states[0]
        eb, eg = exile_by_name(s, BRUNA), exile_by_name(s, GISELA)
        bb, bg = bf_by_name(s, 0, BRUNA), bf_by_name(s, 0, GISELA)
        ass["A3_exile_observed"] = "passed" if (eb or eg) and not bb and not bg \
            else "failed"
        notes.append(f"resolution state: exile bruna={len(eb)} "
                     f"gisela={len(eg)}; bf bruna={len(bb)} gisela={len(bg)}")
    else:
        ass["A3_exile_observed"] = "not-run"
        notes.append("no post-resolution state; A3 not-run")

    # A4: Brisela on P0 battlefield after resolution
    if ex_states:
        br = [bf_by_name(s, 0, BRISELA) for s in ex_states]
        found = any(br)
        ass["A4_meld_correct"] = "passed" if found else "failed"
        notes.append(f"brisela_bf={[len(x) for x in br]} "
                     f"(expected 1 melded permanent under P0)")
        if not found:
            zones = {}
            for s in ex_states:
                for o in (s.get("objects", {}) or {}).values():
                    nm = str(o.get("base_name") or o.get("name") or "?").lower()
                    if nm in (BRUNA, GISELA, BRISELA):
                        zones.setdefault(o.get("zone"), []).append(nm)
            notes.append(f"half/brisela zones: {zones}")
    else:
        ass["A4_meld_correct"] = "not-run"
        notes.append("no post-resolution state; A4 not-run")

    # A5
    if post is not None:
        empty = not (post.get("stack") or [])
        advanced = (post.get("turn_number") or 0) > (ST.get("pre_turn") or 0) \
            or post.get("active_player") == 1
        ass["A5_cleanup"] = "passed" if (empty and advanced) else "failed"
        notes.append(f"post: phase={post.get('phase')} "
                     f"turn={post.get('turn_number')} "
                     f"stack_empty={empty} advanced={advanced}")
    else:
        ass["A5_cleanup"] = "not-run"
        notes.append("post.json missing; A5 not-run")

    for k in sorted(ass):
        say(f"{k}: {ass[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes}, f, indent=2)

    verdict = "blocked"
    if ass.get("A1_setup_ok") not in ("failed", "not-run"):
        if ass.get("A4_meld_correct") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
    say("verdict:", verdict)

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_6768_01050.py", "w") as f:
        f.write(scenario_src.decode())

    binary_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.105.0/"
             f"phase-server-slim-x86_64-unknown-linux-musl", "rb").read()).hexdigest()
    card_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json", "rb").read()).hexdigest()
    draft_sha = hashlib.sha256(
        open(f"{BACKFILL}/server/releases/v0.105.0/data/draft-pools.json", "rb").read()).hexdigest()
    run_meta = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00", time.localtime(t0)),
        "server": {
            "server_version": "v0.105.0",
            "build_commit": "965e243",
            "protocol_version": 120,
            "binary_sha256": binary_sha,
            "card_data_sha256": card_sha,
            "draft_pools_sha256": draft_sha,
            "run_dir": "runs/run-6729-reval-v01050-20261010-0441 (reused live "
                         "server pid 2974; ServerHello re-verified 0.105.0/965e243/"
                         "protocol 120 instead of starting a second server)",
            "port": 9374,
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": dict(P0_DECK), "P1": dict(P1_DECK)},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"],
                  "trigger_observations": ST["trigger_observations"]},
        "setup_line": ("P0: 12x Bruna + 12x Gisela + 36x Plains; "
                       "P1: 60x Forest dummy; native human seats"),
        "contract_line": ("cast Gisela (3W), cast Bruna (5WW, may-return "
                          "declined), end-step Meld trigger; assert Brisela "
                          "enters the battlefield"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Bruna/Gisela deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "driver_notes": [
            "Protocol-120 port of scenario_6768_01040.py (verified 2026-10-09, "
            "run run-6768-reval-v01040-20261009-0711, v0.104.0/protocol 118).",
            "Cast-confirmation guard (protocol 120): after every CastSpell "
            "submission the driver holds (no priority pass, no new "
            "main-phase plays) until the spell is confirmed (leaves the hand: "
            "stack/battlefield/graveyard/exile/command, or a same-name object "
            "on the stack); 30s backstop clears a still-in-hand cast as "
            "dropped and resets mana_needs/bruna_cast_pending for retry.",
            "Bruna's may-return declined via 106 decideOptionalEffect bare "
            "\"false\" choice, gated on bruna_cast_pending + not "
            "bruna_declined (per the #7195 driver lesson).",
            "Payment via vi tapLandForMana with needs={W:3,generic:1} "
            "(Gisela) / {W:2,generic:5} (Bruna); legacy PayMana/ "
            "PayManaAbilityMana honored as fallback.",
            "playLand included in NON_DECISION_CODES (per the #7195 lesson); "
            "land plays via legal_actions PlayLand matched by _src_oid with "
            "the is_land() helper.",
            "post_trigger export falls back to P1's turn start if the "
            "EndStep/Cleanup window is missed (v0.86.0 run lesson).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=1)

    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
