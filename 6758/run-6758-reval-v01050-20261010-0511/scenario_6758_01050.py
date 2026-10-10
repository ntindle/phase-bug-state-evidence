#!/usr/bin/env python3
"""Issue #6758: Comet Storm — kicked cast doesn't offer the second target.

Re-validation run on v0.105.0 / protocol 120 (prior: v0.104.0 / protocol 118,
run run-6758-reval-v01040-20261009-0441, verdict reproduced; before: v0.103.0 /
protocol 106, run 20261007-6758, verdict reproduced; earlier: v0.102.0 /
protocol 106, run 20261005-6758; v0.86.0 / protocol 72, run 20260917-6758;
original: v0.79.0 / protocol 69, run 20260910-6758, verdict reproduced).

Report (discord): "It didn't let me choose second target eventhough it was
kicked. It did not deal any damage either. I chose a target, it went on the
stack, resolved but didn't deal damage and it just went to the graveyard."

Oracle text (verified from pinned v0.105.0 card-data.json, key 'comet storm'):
  "Multikicker {1} (You may pay an additional {1} any number of times as you
   cast this spell.) Choose any target, then choose another target for each
   time this spell was kicked. Comet Storm deals X damage to each of them."

Triage (mike-theDude, 2026-08-03, on the issue): the AST has a first
TargetOnly, one `another` TargetOnly child, and DamageAll, but does not
encode that the second target slot repeats once per multikicker payment; the
damage child references only its parent target rather than the complete
chosen set.

Protocol-120 driver notes (v0.105.0, 2026-10-10): mechanical port of the
verified v0.104.0 scenario_6758_01080.py (protocol 118), following the verified
scenario_5654_01040 -> scenario_5654_01050 port pattern:
  - client.py HELLO already advertises protocol 120 (updated for v0.105.0);
  - engine auto-pays mana: bare CastSpell via submit_as_is, no pay_mana_vi /
    PayMana* / ActivateAbility-tap payment machinery and no per-cast needs
    bookkeeping (need_lands stays as the setup precondition);
  - cast-confirmation guard (2026-10-09 lesson): after every CastSpell the
    driver holds priority (no pass, no new plays) until the storm is confirmed
    on the stack (cast oid or name scan); a 30s backstop clears a dropped cast
    and re-arms the cast block; schema/target answers stay allowed while
    holding; the guard runs before any priority-passing path;
  - merged_actions also merges vi-opportunity actions;
  - legacy ChooseTarget target path alongside the vi-schema target path
    (schema preferred; legacy only when no schema target seen this scan);
  - drive loop re-ticks on every revision change plus a 5s backstop
    (was: priority-hold + 5s only).

Protocol-118 driver notes (v0.104.0, 2026-10-09) [preserved]: ported from the
verified v0.103.0 scenario_6758_01070.py (protocol-106; the 2026-10-07 v0.103.0
run reproduced the bug, A3/A4 failed). Protocol 106 -> 118 was wire-compatible
for this flow (same pattern as the #301 106->118 port, 2026-10-08): no
interaction-shape changes were needed, only version/digest/run-id updates.
ServerHello assertions changed for this v0.104.0 re-validation run.

Protocol-106 driver notes (v0.103.0, 2026-10-07) [original text preserved]:
port of scenario_6758b.py. Game logic, decks, assertions and the
verdict rule are unchanged; only the interaction surface is adapted:
  - MulliganDecision: legacy legal action (verified accepted on 106/118).
  - Bottom-after-mulligan: vi schema/select gated on waitingForKind code
    'mulligan' AND turn 1 / Untap (the sacrifice-cost prompt shares the
    surface mid-game).
  - DiscardToHandSize: vi schema/select with hand>7 heuristic + verbatim
    SelectCards fallback guarded against mulligan-bottom.
  - Priority: waiting_for is GONE (null) on 106; the viewing seat holds
    priority iff a PassPriority legal action is advertised to it.
  - Priority menus are noisy (tapLandForMana / untapLandForMana / castSpell /
    activateAbility / passPriority): real_decision_pending excludes those
    codes; only schema opportunities and unknown action codes block passes.
  - CastSpell: merged_action first, else the vi castSpell choice.
  - X choice: number-schema opportunity, {"type":"number","data":{"value":N}}.
  - Kicker (OptionalCostChoice): choices identified via decideOptionalCost
    action codes + (pay,true/false) value surfaces (protocol-72 shape, kept
    as the hypothesis; full opportunity is wire-logged on first sight so a
    106 shape change is diagnosable). Pay once, then stop (multikicker is
    repeatable: pay at times_kicked 0, stop at 1).
  - Target selection: schema sequence/select opportunity; seat candidates
    chosen per plan order; single-composite-choice 106 style handled.
  - Mana payment while casting: vi tapLandForMana with needs, plus
    PayMana*/PayManaAbilityMana as-is, plus ActivateAbility taps fallback.
  - Land play: legacy PlayLand first, vi playLand choice fallback (an
    unhandled playLand menu silently stalls the game on 106).
  - Deck schema {"main_deck": [<name strings>]}; sleep(0) yield before leg
    evaluation; never return after an export while holding priority; 5s
    re-tick backstop for a priority holder.
  - ExportAuthoritativeState has NO data field; data.state is a JSON string
    parsed once. manifest.sha256 computed AFTER wire_log.jsonl /
    scenario_run.log close.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Game 1 (kicked, X=2): P0 casts Comet Storm with X=2, pays kicker {1} once,
targets P1 then P0.
  A1 setup_ok       game started; Comet Storm cast from a legal main-phase
                    window with mana available
  A2 cast_decisions X=2 announced; kicker paid once (pay at times 0, stop
                    at 1)
  A3 two_targets    engine offers a SECOND target selection for the kicker
                    (two distinct target prompts observed); P1 then P0 chosen
  A4 damage_each    resolution deals X=2 to EACH chosen target:
                    P1 life 20->18 AND P0 life 20->18; Storm in P0 graveyard;
                    stack empty
Game 2 (control, no kicker, X=1): single-target path mechanics —
  A5 control_path   exactly 1 target prompt, X=1 announced, clean resolution
                    to graveyard; damage corroborates A4 independently of the
                    kicker path

Verdict = reproduced iff the kicked cast offers <2 target prompts (A3 fails)
or resolution does not deal X to each chosen target (A4 fails); blocked iff
A1/A2 cannot complete; not-reproduced iff all pass.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-6758-reval-v01050-20261010-0511")
EVDIR = f"{BACKFILL}/evidence/6758/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

URL = "ws://127.0.0.1:9374/ws"

STORM = "Comet Storm"
MOUNTAIN = "Mountain"

P0_DECK = [(STORM, 12), (MOUNTAIN, 48)]
P1_DECK = [(MOUNTAIN, 60)]

SERVER_IDENTITY = {
    "server_version": "0.105.0",
    "build_commit": "965e243",
    "protocol_version": 120,
    "mode": "Full",
    "binary_sha256": "9466a5fb7e9931103ebeca3d1764e0b2c085f27240ae9e220edf932b55525c38",
    "card_data_sha256": "08270e6109ed5247c0438431d5c4ddcedbfc3016b5097a347227d0b9bb4f8716",
    "draft_pools_sha256": "d8d4664a45d095d5f30f570c5463d9a1a5f231049ff30403477efbf4384c79dc",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-10-10",
    "source": "2026-10-10: latest stable release v0.105.0 (published "
              "2026-10-09T17:49:54Z) == pinned release dir; digests verified "
              "via sha256sum against server/releases/v0.105.0/ this run; raw "
              "ServerHello on 127.0.0.1:9374 verified by this run "
              "(server_version 0.105.0 / build 965e243 / protocol 120 / Full); "
              "reused the already-listening v0.105.0 server (not started by "
              "this run; see server_run_dir)",
}

CARD_DATA = json.load(
    open(f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"))

SUBMITTED_OPPS = set()  # answered viewer_interaction interactionIds
MULLS = set()           # (tag, "mull", iid|rev) guards

NON_DECISION_CODES = {"tapLandForMana", "untapLandForMana", "castSpell",
                      "activateAbility", "passPriority", "mulliganDecision"}


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


def obj_name(state, oid):
    o = (state.get("objects", {}) or {}).get(str(oid), {})
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return (state.get("objects", {}) or {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [obj_name(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def gy_names(state, pid):
    return [obj_name(state, o) for o in player_of(state, pid).get("graveyard", [])]


def untapped_mountains(state, pid):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and str(o.get("controller")) == str(pid)
            and obj_name(state, oid) == MOUNTAIN.lower() and not o.get("tapped")]


def storm_in_hand_oid(state, pid):
    for o in player_of(state, pid).get("hand", []):
        if obj_name(state, o) == STORM.lower():
            return int(o)
    return None


def storm_spell_on_stack(state, cast_oid=None):
    """Stack entries for P0's Comet Storm. P0 casts no other spells, so
    match by name blob, cast object id, or controller/kind fallback."""
    out = []
    for e in (state.get("stack", []) or []):
        blob = json.dumps(e, default=str).lower()
        if "comet storm" in blob:
            out.append(e)
            continue
        if cast_oid is not None:
            ids = {str(e.get("source")), str(e.get("object_id")),
                   str(e.get("card_id"))}
            if str(cast_oid) in ids:
                out.append(e)
                continue
        kind = e.get("kind") or {}
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        if ktype == "Spell" and str(e.get("controller")) == "0":
            out.append(e)
    return out


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


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


def choice_text(ch):
    bits = []

    def rec(v):
        if isinstance(v, dict):
            for k, val in v.items():
                if k in ("name", "code", "label", "value", "description",
                         "text", "prompt", "title") and isinstance(val, str):
                    bits.append(val)
                else:
                    rec(val)
        elif isinstance(v, list):
            for x in v:
                rec(x)
    rec(ch.get("surfaces", []))
    return " | ".join(bits)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_seat(ch):
    """Seat (player id) a target choice refers to, or None."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict):
            if d.get("seat") is not None:
                return d["seat"]
            if d.get("role") in ("target", "player") and "controller" in d:
                return d["controller"]
            if d.get("role") == "player" and "seat" in d:
                return d["seat"]
    return None


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


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
    """Submit a viewer_interaction answer per the advertised response type."""
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
    return sub


async def submit_number(c, opp, value, tag):
    sub = {"interactionId": opp.get("interactionId"),
           "response": {"type": "number", "data": {"value": int(value)}}}
    say(f"[{tag}] submitting number X={value}")
    await interact_as(c, sub, tag)
    return sub


def cast_guard_tick(g, state, tag):
    """Protocol-120 cast-confirmation guard (2026-10-09 lesson).

    Returns "hold" while our bare CastSpell is in flight but unconfirmed:
    the driver must not pass priority or start new plays in that window --
    a bare CastSpell can lose a race to our own PassPriority submitted on
    the next tick and be silently dropped by the server (no rejection).
    Returns "proceed" once the storm is confirmed on the stack (by cast oid
    or name scan); a 30s backstop clears a still-unconfirmed cast and resets
    cast_submitted so the cast block re-triggers. Schema/target answers
    stay allowed while holding (scan_cast_decisions runs before the gate).
    Must run BEFORE any priority-passing path.
    """
    pc = g.get("cast_pending")
    if not pc:
        return "proceed"
    if storm_spell_on_stack(state, pc.get("oid")):
        g["cast_pending"] = None
        say(f"[{tag}] cast confirmed on stack: Comet Storm "
            f"oid {pc.get('oid')}")
        wire("cast_confirmed", {"oid": str(pc.get("oid"))})
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] CAST NOT CONFIRMED after 30s (oid {pc.get('oid')}) "
            f"-- clearing for retry")
        wire("cast_dropped_retry", {"oid": str(pc.get("oid"))})
        g["cast_pending"] = None
        g["cast_submitted"] = False
        return "proceed"
    return "hold"


def _find_player_refs(data, want=1):
    """Player/seat references inside an action payload equal to `want`."""
    refs = []
    if isinstance(data, dict):
        t = data.get("type")
        if t == "Player":
            try:
                if int(data.get("data")) == want:
                    refs.append(data)
            except (TypeError, ValueError):
                pass
        for k in ("player", "seat", "player_id", "seat_index"):
            try:
                if data.get(k) is not None and int(data[k]) == want:
                    refs.append({k: data[k]})
            except (TypeError, ValueError):
                pass
        for v in data.values():
            refs.extend(_find_player_refs(v, want))
    elif isinstance(data, list):
        for v in data:
            refs.extend(_find_player_refs(v, want))
    return refs


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


def cast_spell_offered(state, st, spell):
    """True if a CastSpell action for `spell` is currently offered."""
    for a in merged_actions(st):
        if a["type"] == "CastSpell" \
                and obj_name(state, a.get("data", {}).get("object_id")) == spell:
            return True, a
    vi = st.get("viewer_interaction") or {}
    if vi.get("canSubmit"):
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "castSpell" in codes and spell in choice_text(ch).lower():
                    return True, {"_vi_choice": ch, "_vi_opp": opp}
    return False, None


async def do_mulligan(c, acts, st, pid, tag, keep_rule):
    """Protocol 106: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission)."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if isinstance(s.get("data"), dict)]
            if "mulliganDecision" in codes:
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_names(state, pid)
    mull_count = sum(1 for k in MULLS if k[0] == tag)
    keep = keep_rule(hn, mull_count)
    say(f"[{tag}] {'keep' if keep else 'mulligan'} {len(hn)}: {hn[:8]} "
        f"(mull_count={mull_count})")
    wire("mulligan", {"who": tag, "decision": "keep" if keep else "mulligan",
                      "hand": hn, "iid": iid})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if keep else "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a vi schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Gated on turn 1 / Untap so it
    cannot steal the sacrifice-cost prompt (identical surface mid-game)."""
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
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_name(state, oid) if oid is not None else "?"
        if nm == STORM.lower():
            return (2, str(oid))
        if oid is not None and nm == MOUNTAIN.lower():
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_name(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    sub = {"interactionId": iid,
           "response": {"type": "select",
                        "data": {"choiceIds": [ch.get("id") for ch in picks]}}}
    await interact_as(c, sub, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Protocol 106: DiscardToHandSize surfaces via viewer_interaction.
    Generic 'choose' waitingForKind code, so the heuristic additionally
    requires hand > 7 plus a schema/select opportunity offering our own hand
    cards; verbatim SelectCards fallback guarded against mulligan-bottom."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    handset = {str(x) for x in hand}
    found = None
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if any(str(_cand_reference(ch)) in handset for ch in cands):
            found = (opp, cands, spec)
            break
    if found:
        opp, cands, spec = found
        iid = opp.get("interactionId")
        SUBMITTED_OPPS.add(iid)

        def drank(ch):
            oid = _cand_reference(ch)
            nm = obj_name(state, oid) if oid is not None else "?"
            if nm == MOUNTAIN.lower():
                return (0, nm)
            if nm == STORM.lower():
                return (2, nm)
            return (1, nm)

        ranked = sorted((ch for ch in cands
                         if str(_cand_reference(ch)) in handset),
                        key=drank)
        picks = ranked[:n]
        say(f"[{tag}] discarding to hand size via vi: "
            f"{[obj_name(state, _cand_reference(x)) for x in picks]}")
        wire(f"{tag}_discard_vi",
             {"picks": [x.get("id") for x in picks]})
        sub = {"interactionId": iid,
               "response": {"type": "select",
                            "data": {"choiceIds": [ch.get("id")
                                                  for ch in picks]}}}
        await interact_as(c, sub, tag)
        return True
    if code == "mulligan":
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    oids_ok = True
    act_by_oid = {}
    for a in sel_acts:
        cards = (a.get("data") or {}).get("cards") or []
        if len(cards) != 1 or str(cards[0]) not in handset:
            oids_ok = False
            break
        act_by_oid[cards[0]] = a
    if sel_acts and oids_ok and len(sel_acts) == len(hand):
        def orank(o):
            nm = obj_name(state, o)
            if nm == MOUNTAIN.lower():
                return (0, nm)
            if nm == STORM.lower():
                return (2, nm)
            return (1, nm)

        chosen = sorted(act_by_oid, key=orank)[:n]
        say(f"[{tag}] discarding to hand size via verbatim SelectCards: "
            f"{[obj_name(state, o) for o in chosen]}")
        wire(f"{tag}_discard_verbatim", {"oids": chosen})
        for o in chosen:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [o]}})
            await asyncio.sleep(0.4)
        return True
    return False


def _is_land_name(nm):
    return nm in ("mountain", "forest", "island", "plains", "swamp",
                  "wastes") or "land" in nm


async def do_play_land(c, acts, st, pid, tag):
    """Play a land: legacy PlayLand action first; vi playLand choice as
    fallback (an unhandled playLand menu silently stalls the game on 106/118)."""
    for a in acts:
        if a.get("type") == "PlayLand":
            await submit_as_is(c, a)
            say(f"[{tag}] plays land (legacy PlayLand)")
            return True
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "playLand" not in codes:
                continue
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            SUBMITTED_OPPS.add(iid)
            say(f"[{tag}] plays land via vi playLand (iid {iid})")
            wire("play_land_vi", {"who": tag, "iid": iid})
            await answer_vi(c, opp, ch, tag)
            return True
    return False


async def answer_kicker(c, opp, g):
    """Answer the kicker OptionalCostChoice: pay once (multikicker), then
    stop. Choices identified via decideOptionalCost action codes +
    (pay,true/false) value surfaces (protocol-72 shape, kept as hypothesis);
    text fallback; the full opportunity is always wire-logged for diagnosis."""
    iid = opp.get("interactionId")
    chs = ((opp.get("response") or {}).get("data") or {}).get("choices") or []
    info = {}
    for ch in chs:
        codes = [s.get("data", {}).get("code")
                 for s in ch.get("surfaces", []) or []
                 if s.get("type") == "action"]
        vals = [(s.get("data", {}).get("role"), s.get("data", {}).get("value"))
                for s in ch.get("surfaces", []) or []
                if s.get("type") == "value"]
        info[ch.get("id")] = {"codes": codes, "vals": vals,
                              "text": choice_text(ch)[:120]}
    wire("kicker_full", {"iid": iid, "times_kicked": g.get("times_kicked"),
                         "choices": info})
    say(f"[P0] KICKER prompt iid={iid} choices={json.dumps(info)[:400]}")
    pay = g["plan"]["kick"] and not g.get("kick_paid")
    pick = None
    for ch in chs:
        ci = info[ch.get("id")]
        if "decideOptionalCost" not in ci["codes"]:
            continue
        # 106 carries the pay flag as a STRING ("true"/"false"), not a bool
        for role, val in ci["vals"]:
            if str(role).lower() != "pay":
                continue
            is_pay = str(val).lower() in ("true", "1", "yes")
            if is_pay == pay:
                pick = ch
                break
        if pick is not None:
            break
    if pick is None:
        for ch in chs:
            txt = info[ch.get("id")]["text"].lower()
            if pay and "pay" in txt and "don't" not in txt:
                pick = ch
                break
            if not pay and ("decline" in txt or "don't" in txt
                            or "stop" in txt or "no" == txt.strip()):
                pick = ch
                break
    if pick is None:
        say(f"[P0] kicker: no pay/stop choice identified; deferring "
            f"(see wire kicker_full)")
        return False
    sub = {"interactionId": iid,
           "response": {"type": "choose",
                        "data": {"choiceId": pick.get("id")}}}
    say(f"[P0] KICKER -> {'PAY' if pay else 'STOP'} "
        f"(choice {pick.get('id')})")
    await interact_as(c, sub, "P0")
    if pay:
        g["kick_paid"] = True
        g["kick_subs"] = g.get("kick_subs", 0) + 1
    else:
        g["kicker_done"] = True
    return True


async def answer_target(c, opp, st, g):
    """Answer one Comet Storm target prompt with the next planned seat.
    Handles both per-candidate choices (seat surfaces) and the 106
    single-composite-choice style."""
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    stype = spec.get("type") or "sequence"
    chs = data.get("choices") or data.get("candidates") or []
    want_idx = len(g["target_choices"])
    want_seat = (g["plan"]["targets"][want_idx]
                 if want_idx < len(g["plan"]["targets"]) else None)
    pick = None
    if want_seat is not None:
        for ch in chs:
            if cand_seat(ch) == want_seat or str(cand_seat(ch)) == str(want_seat):
                pick = ch
                break
    if pick is None and len(chs) == 1 and want_seat is not None:
        pick = chs[0]  # composite single-choice id (106 bolt style)
    if pick is None:
        say(f"[P0] target prompt iid={iid}: wanted seat {want_seat} not among "
            f"{[cand_seat(ch) for ch in chs]}; deferring")
        wire("target_deferred",
             {"iid": iid, "wanted": want_seat,
              "seats": [cand_seat(ch) for ch in chs], "opp": opp})
        return False
    sub = {"interactionId": iid,
           "response": {"type": stype,
                        "data": {"choiceIds": [pick.get("id")]}}}
    say(f"[P0] TARGET #{len(g['target_choices']) + 1} -> seat {want_seat} "
        f"(iid {iid})")
    wire("target_choice", {"iid": iid, "seat": want_seat,
                           "choice_id": pick.get("id"),
                           "stack": [json.loads(json.dumps(e, default=str))
                                     for e in (st["state"].get("stack") or [])][:2]})
    await interact_as(c, sub, "P0")
    g["target_prompts"] += 1
    g["target_iids"].append(iid)
    g["target_choices"].append(want_seat)
    return True


async def scan_cast_decisions(c, st, g):
    """Answer Comet Storm cast decisions for P0 in any engine order: X
    number choice, kicker OptionalCostChoice, target selections. Returns
    True if it acted."""
    if not g.get("cast_submitted") or g.get("cast_done"):
        return False
    state = st["state"]
    schema_target_seen = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) if isinstance(data.get("spec"), dict) else {}
        stype = spec.get("type")
        chs = data.get("choices") or data.get("candidates") or []
        key = (rtype, stype, len(chs))
        if key not in g["shapes"]:
            g["shapes"].add(key)
            say(f"[P0] SHAPE rtype={rtype} spec={stype} n={len(chs)} "
                f"vikind={vi_kind_code(st)}")
            wire("cast_decision_shape", {"iid": iid, "opp": opp})
        # --- X choice: number schema, right after cast ---
        if stype == "number" and not g.get("x_chosen"):
            await submit_number(c, opp, g["plan"]["x"], "P0")
            SUBMITTED_OPPS.add(iid)
            g["x_chosen"] = g["plan"]["x"]
            say(f"[P0] X-choice -> X={g['plan']['x']} (iid {iid})")
            wire("x_choice", {"iid": iid, "x": g["plan"]["x"]})
            return True
        # --- kicker / optional-cost decision ---
        blob = json.dumps(opp, default=str).lower()
        if (("kicker" in blob or "optional" in blob
             or "decideoptionalcost" in blob)
                and not g.get("kicker_done")):
            if await answer_kicker(c, opp, g):
                SUBMITTED_OPPS.add(iid)
                return True
            continue
        # --- target selection: schema with candidate seats ---
        if rtype == "schema" and stype in ("sequence", "select"):
            schema_target_seen = True
            if await answer_target(c, opp, st, g):
                SUBMITTED_OPPS.add(iid)
                return True
    # --- legacy ChooseTarget actions (protocol 120): one per candidate.
    # Only when no schema target opportunity was seen this scan, so the two
    # surfaces can never double-answer the same target slot. ---
    if (g.get("cast_submitted") and not g.get("cast_done")
            and not schema_target_seen):
        want_idx = len(g["target_choices"])
        want_seat = (g["plan"]["targets"][want_idx]
                     if want_idx < len(g["plan"]["targets"]) else None)
        if want_seat is not None:
            for a in merged_actions(st):
                if a.get("type") != "ChooseTarget":
                    continue
                akey = json.dumps(a.get("data", {}), sort_keys=True,
                                  default=str)
                if not _find_player_refs(a.get("data", {}), want_seat):
                    if akey not in g["legacy_tgt_seen"]:
                        g["legacy_tgt_seen"].add(akey)
                        say(f"[P0] legacy ChooseTarget (seat-{want_seat} "
                            f"not matched): {akey[:200]}")
                        wire("target_legacy_unmatched",
                             {"seat": want_seat, "action": akey[:300]})
                    continue
                if akey in SUBMITTED_OPPS:
                    continue
                SUBMITTED_OPPS.add(akey)
                say(f"[P0] legacy ChooseTarget -> seat {want_seat}")
                wire("target_legacy", {"seat": want_seat,
                                       "action": akey[:300]})
                await submit_as_is(c, a)
                g["target_prompts"] += 1
                g["target_iids"].append("legacy:" + akey[:16])
                g["target_choices"].append(want_seat)
                return True
    return False


def real_decision_pending(st):
    """True if an unanswered viewer_interaction opportunity carries a real
    decision: any schema opportunity, or any action code outside the noisy
    priority-menu set. Prevents passing priority while a cast decision
    (kicker/target) is still pending."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        if resp.get("type") == "schema":
            return True
        for ch in (resp.get("data", {}) or {}).get("choices") or []:
            codes = {s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"}
            codes.discard(None)
            if codes - NON_DECISION_CODES:
                return True
    return False


async def p0_tick(c, g, st):
    state = st["state"]

    def note(m):
        say(f"[P0] {m}")
        g["notes"].append(m)

    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, "P0",
                         lambda hn, mulls: (STORM.lower() in hn) or mulls >= 4):
        return
    if await do_bottom(c, acts, st, 0, "P0"):
        return
    if await do_discard_to_handsize(c, acts, st, 0, "P0"):
        return

    # ---- driver-race guard: yield before evaluating legs ----
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # ---- protocol-120 cast-confirmation guard: never pass priority or
    # start new plays while our bare CastSpell is in flight but unconfirmed
    # (silent-drop race, 2026-10-09 lesson). Schema/target answers stay
    # allowed while holding (scan_cast_decisions runs before the gate). ----
    hold_priority = cast_guard_tick(g, state, "P0") == "hold"

    # ---- cast decisions (X / kicker / targets), any engine order ----
    if await scan_cast_decisions(c, st, g):
        return

    # ---- mid export: storm spell on the stack with cast decisions done ----
    on_stack = storm_spell_on_stack(state, g.get("cast_oid"))
    if (g.get("cast_submitted") and not g.get("cast_on_stack_exported")
            and on_stack and g.get("x_chosen") is not None
            and (not g["plan"]["kick"] or g.get("kicker_done"))):
        try:
            mid = await c.export_state()
            with open(f"{EVDIR}/{g['label']}_on_stack.json", "w") as f:
                f.write(mid)
            g["cast_on_stack_exported"] = True
            note(f"exported {g['label']}_on_stack.json "
                 f"(targets={g['target_choices']}, prompts={g['target_prompts']}, "
                 f"x={g['x_chosen']}, kick_paid={g.get('kick_paid')})")
            wire("on_stack_export", {"targets": g["target_choices"],
                                     "prompts": g["target_prompts"]})
        except Exception as e:
            note(f"cast-on-stack export failed: {e}")
        # fall through: never return after an export while holding priority

    # ---- post export: storm resolved (was on stack, now gone) ----
    if (g.get("cast_submitted") and not g.get("post_exported")
            and g.get("storm_was_on_stack")
            and not storm_spell_on_stack(state, g.get("cast_oid"))):
        note("Storm left the stack; exporting POST")
        try:
            post = await c.export_state()
            with open(f"{EVDIR}/{g['label']}_post.json", "w") as f:
                f.write(post)
            g["post_exported"] = True
            g["phase"] = "done"
            note(f"exported {g['label']}_post.json")
        except Exception as e:
            note(f"post export failed: {e}")
        return
    if on_stack:
        g["storm_was_on_stack"] = True

    if not my_priority(acts):
        return

    # ---- never pass priority while a cast decision is pending ----
    if g.get("cast_submitted") and not g.get("cast_done"):
        pend_iids = [o.get("interactionId") for o in vi_ops(st)
                     if o.get("interactionId") not in SUBMITTED_OPPS]
        if pend_iids and real_decision_pending(st):
            note(f"deferring pass: unanswered decisions {pend_iids} "
                 f"(shapes logged; scan will retry)")
            return

    # ---- P0 priority: cast Comet Storm when set up ----
    # (protocol 120: engine auto-pays; bare CastSpell, no mana bookkeeping)
    need_lands = g["plan"]["need_lands"]
    if (not g["cast_submitted"] and my_main(state, 0)
            and storm_in_hand_oid(state, 0) is not None
            and len(untapped_mountains(state, 0)) >= need_lands):
        note(f"P0 main phase: exporting {g['label']}_pre.json, then casting "
             f"Comet Storm")
        pre = await c.export_state()
        with open(f"{EVDIR}/{g['label']}_pre.json", "w") as f:
            f.write(pre)
        g["pre_exported"] = True
        offered, act = cast_spell_offered(state, st, STORM.lower())
        if offered and "_vi_choice" not in act:
            d = act.get("data", {})
            wire("cast_storm", {k: v for k, v in act.items()
                                if not k.startswith("_")})
            g["cast_turn"] = state.get("turn_number")
            g["cast_phase"] = state.get("phase")
            g["cast_oid"] = d.get("object_id")
            await submit_as_is(c, act)
            g["cast_submitted"] = True
            g["cast_pending"] = {"oid": g["cast_oid"],
                                 "since": time.time()}
            note(f"P0 casts Comet Storm (turn {g['cast_turn']}, "
                 f"oid {g['cast_oid']}); cast-confirmation guard armed")
            return
        if offered:  # vi castSpell choice
            wire("cast_storm_vi", {"choiceId": act["_vi_choice"].get("id")})
            g["cast_turn"] = state.get("turn_number")
            g["cast_phase"] = state.get("phase")
            await answer_vi(c, act["_vi_opp"], act["_vi_choice"], "P0")
            g["cast_submitted"] = True
            g["cast_pending"] = {"oid": None, "since": time.time()}
            note(f"P0 casts Comet Storm via vi (turn {g['cast_turn']}); "
                 f"cast-confirmation guard armed")
            return
        note("cast action not offered on this tick; falling through to "
             "setup play")
    # ---- normal setup play ----
    if my_main(state, 0):
        if await do_play_land(c, acts, st, 0, "P0"):
            return
    if hold_priority:
        note("holding priority: cast in flight, awaiting confirmation")
        return
    await pass_priority(c, st, acts)


async def p1_tick(c, g, st):
    state = st["state"]

    def note(m):
        say(f"[P1] {m}")
        g["notes"].append(m)

    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, "P1", lambda hn, mulls: True):
        return
    if await do_bottom(c, acts, st, 1, "P1"):
        return
    if await do_discard_to_handsize(c, acts, st, 1, "P1"):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
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
    if my_main(state, 1):
        if await do_play_land(c, acts, st, 1, "P1"):
            return
    if my_priority(acts):
        await pass_priority(c, st, acts)


def load_env_state(path):
    try:
        with open(path) as f:
            return json.load(f)["state"]
    except Exception:
        return None


def evaluate(g):
    """Evaluate A1..A5 from saved states + observations."""
    ass = g["ass"]
    notes = g["notes"]
    obs = g["obs"]
    pre_st = load_env_state(f"{EVDIR}/{g['label']}_pre.json")
    post_st = load_env_state(f"{EVDIR}/{g['label']}_post.json")
    if pre_st is None:
        ass["A1_setup_ok"] = "not-run"
        notes.append("no pre.json exported (cast never reached)")
    else:
        need = g["plan"]["need_lands"]
        ok = (storm_in_hand_oid(pre_st, 0) is not None
              and len(untapped_mountains(pre_st, 0)) >= need
              and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
              and pre_st.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(f"{g['label']}_pre.json: P0 main phase, Storm in hand, "
                         f"{len(untapped_mountains(pre_st, 0))} untapped "
                         f"Mountains, life 20/20")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(f"{g['label']}_pre.json setup precondition not met")
        obs["life_pre"] = [life_of(pre_st, 0), life_of(pre_st, 1)]
    # A2
    if g.get("x_chosen") == g["plan"]["x"] and (
            not g["plan"]["kick"] or (g.get("kick_paid") and g.get("kicker_done"))):
        ass["A2_cast_decisions"] = "passed"
        notes.append(f"cast decisions: X={g.get('x_chosen')} "
                     f"kick_paid={g.get('kick_paid')} kick_subs={g.get('kick_subs', 0)} "
                     f"kicker_done={g.get('kicker_done')}")
    elif g.get("cast_submitted"):
        ass["A2_cast_decisions"] = "failed"
        notes.append(f"cast decisions incomplete: x={g.get('x_chosen')} "
                     f"kick_paid={g.get('kick_paid')} kicker_done={g.get('kicker_done')}")
    else:
        ass["A2_cast_decisions"] = "not-run"
        notes.append("cast never submitted")
    # A3
    if g["plan"]["kick"]:
        if g.get("target_prompts", 0) >= 2:
            ass["A3_two_targets"] = "passed"
            notes.append(f"target prompts: {g['target_prompts']} "
                         f"(choices={g['target_choices']}, iids={g['target_iids']})")
        elif g.get("cast_submitted"):
            ass["A3_two_targets"] = "failed"
            notes.append(f"target prompts: {g.get('target_prompts')} "
                         f"(choices={g['target_choices']}) — expected 2 for a "
                         f"spell kicked once (REPORTED BUG)")
        else:
            ass["A3_two_targets"] = "not-run"
    # A4
    if pre_st is not None and post_st is not None and g["plan"]["kick"]:
        obs["life_post"] = [life_of(post_st, 0), life_of(post_st, 1)]
        l0, l1 = obs["life_post"]
        storm_gy = STORM.lower() in gy_names(post_st, 0)
        stack_empty = len(post_st.get("stack", []) or []) == 0
        dmg0, dmg1 = 20 - l0, 20 - l1
        exp = g["plan"]["x"]
        if l1 == 20 - exp and l0 == 20 - exp and storm_gy and stack_empty:
            ass["A4_damage_each"] = "passed"
            notes.append(f"resolution: P1 20->{l1}, P0 20->{l0}, storm in gy, "
                         f"stack empty")
        else:
            ass["A4_damage_each"] = "failed"
            notes.append(f"resolution: P1 took {dmg1} (expected {exp}), P0 took "
                         f"{dmg0} (expected {exp}); storm_in_gy={storm_gy} "
                         f"stack_empty={stack_empty} (REPORTED BUG: no damage)")
    else:
        ass["A4_damage_each"] = "not-run" if not g["plan"]["kick"] else ass.get("A4_damage_each", "not-run")
        if g["plan"]["kick"]:
            notes.append("A4 could not be evaluated (missing pre/post state)")
    # A5 (control game only)
    if not g["plan"]["kick"]:
        if (g.get("target_prompts") == 1 and g.get("x_chosen") == 1
                and post_st is not None
                and STORM.lower() in gy_names(post_st, 0)
                and len(post_st.get("stack", []) or []) == 0):
            ass["A5_control_path"] = "passed"
            notes.append(f"control: 1 target prompt, X=1, storm in gy, stack "
                         f"empty; damage dealt: P1 20->{life_of(post_st, 1)}")
        elif g.get("cast_submitted"):
            ass["A5_control_path"] = "failed"
            notes.append(f"control path mechanics broken: prompts="
                         f"{g.get('target_prompts')} x={g.get('x_chosen')}")
        else:
            ass["A5_control_path"] = "not-run"
            notes.append("control cast never submitted")
    return ass


async def verify_server_hello():
    async with websockets.connect(URL, max_size=2**26) as _w:
        hello = json.loads(await asyncio.wait_for(_w.recv(), 10))
    d = hello.get("data", {})
    say(f"ServerHello: {d.get('server_version')}/{d.get('build_commit')}/"
        f"protocol {d.get('protocol_version')}/{d.get('mode')}")
    assert str(d.get("server_version")).startswith("0.105.0"), hello
    assert d.get("build_commit") == "965e243", hello
    assert int(d.get("protocol_version")) == 120, hello


def check_data_level():
    ok, notes = True, []
    e = CARD_DATA.get("comet storm", {})
    if not e:
        ok = False
        notes.append("comet storm: missing from card data")
    elif "multikicker" not in str(e.get("oracle_text", "")).lower():
        ok = False
        notes.append("comet storm: oracle shape missing "
                     f"({str(e.get('oracle_text'))[:120]!r})")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": str(e.get("oracle_text"))[:400] if e else None},
                  f, indent=1, default=str)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok


async def write_manifest():
    fnames = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    lines = []
    for fn in fnames:
        with open(os.path.join(EVDIR, fn), "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


def new_game_state(label, plan):
    return {
        "t_start": time.time(), "phase": "setup", "notes": [],
        "label": label, "plan": plan,
        "ass": {k: "not-run" for k in
                 ("A1_setup_ok", "A2_cast_decisions", "A3_two_targets",
                  "A4_damage_each", "A5_control_path")},
        "obs": {"life_pre": None, "life_post": None},
        "shapes": set(),
        "cast_submitted": False, "cast_pending": None,
        "cast_oid": None, "cast_turn": None,
        "cast_phase": None, "x_chosen": None, "kick_paid": False,
        "kick_subs": 0, "kicker_done": False,
        "target_prompts": 0, "target_iids": [], "target_choices": [],
        "storm_was_on_stack": False, "legacy_tgt_seen": set(),
        "pre_exported": False, "cast_on_stack_exported": False,
        "post_exported": False, "cast_done": False,
    }


async def drive_game(p0, p1, g):
    """Drive one game until post export or timeout. Returns True if the
    post-resolution export was captured."""
    last_rev = {}
    last_change = {}
    last_tick_at = {}
    stuck_deadline = None
    last_diag = 0.0
    t0 = g["t_start"]
    try:
        while time.time() - t0 < 900:
            await asyncio.sleep(0.15)
            for c, tick in ((p0, p0_tick), (p1, p1_tick)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                    last_change[c.name] = time.time()
                elif time.time() - last_change.get(c.name, t0) > 45:
                    s0 = st["state"]
                    la = [a.get("type") for a in
                          (st.get("legal_actions") or [])][:8]
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                        f"legal={la} vikind={vi_kind_code(st)}")
                    wire("watchdog_stale",
                         {"who": c.name, "rev": c.revision,
                          "turn": s0.get("turn_number"),
                          "phase": s0.get("phase"),
                          "legal": la, "vikind": vi_kind_code(st)})
                    last_change[c.name] = time.time()
                # protocol-120 re-tick: tick on every revision change, plus
                # a 5s backstop so a live game never stalls silently.
                stale = time.time() - last_tick_at.get(c.name, 0) > 5
                if not rev_changed and not stale:
                    continue
                last_tick_at[c.name] = time.time()
                try:
                    await tick(c, g, st)
                except Exception as e:
                    say(f"tick error {c.name}: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            if g.get("phase") == "done" and g.get("post_exported"):
                say(f"{g['label']}: post exported; game done")
                return True
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                s = p0.latest["state"]
                say(f"DIAG {g['label']} turn={s.get('turn_number')} "
                    f"active={s.get('active_player')} phase={s.get('phase')} "
                    f"vikind={vi_kind_code(p0.latest)} "
                    f"P0hand={hand_names(s, 0)[:8]} "
                    f"P0lands={len(untapped_mountains(s, 0))} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"stack={len(s.get('stack') or [])} cast={g['cast_submitted']} "
                    f"x={g['x_chosen']} kick={g.get('kick_paid')} "
                    f"targets={g['target_choices']}")
            if g["cast_submitted"] and not g.get("post_exported") \
                    and stuck_deadline is None:
                stuck_deadline = time.time() + 300
            if not g["cast_submitted"] or g.get("post_exported"):
                stuck_deadline = None
            if stuck_deadline and time.time() > stuck_deadline:
                g["notes"].append(
                    "Storm cast but post-resolution state not reached in 300s; "
                    "see wire log (possible unhandled interaction)")
                return False
        g["notes"].append("game timeout (900s) hit before assertions resolved")
        return False
    finally:
        pass


async def verdict_for(games):
    """Combine per-game evaluations into the final verdict."""
    g1, g2 = games
    a1 = evaluate(g1)
    a2 = evaluate(g2)
    ass = {}
    for k in ("A1_setup_ok", "A2_cast_decisions", "A3_two_targets",
              "A4_damage_each"):
        ass[k] = a1[k]
    ass["A5_control_path"] = a2["A5_control_path"]
    notes = g1["notes"] + ["--- control game ---"] + g2["notes"]
    if "failed" in (a1["A1_setup_ok"], a1["A2_cast_decisions"]) or \
            "not-run" in (a1["A1_setup_ok"], a1["A2_cast_decisions"],
                          a1["A4_damage_each"]):
        verdict = "blocked"
        reason = "cast/kicker setup did not complete or resolution state " \
                 "unreachable; cannot test target offer"
    elif a1["A3_two_targets"] == "failed" or a1["A4_damage_each"] == "failed":
        verdict = "reproduced"
        reason = (f"kicked-once cast offered {g1['target_prompts']} target "
                  f"prompt(s) (expected 2); resolution: "
                  f"P1 {20}->{(g1['obs']['life_post'] or [None, None])[1]}, "
                  f"P0 {20}->{(g1['obs']['life_post'] or [None, None])[0]}; "
                  f"control X=1 no-kick: prompts={g2['target_prompts']} "
                  f"P1 {20}->{(g2['obs']['life_post'] or [None, None])[1]}")
    else:
        verdict = "not-reproduced"
        reason = "kicked cast offered 2 targets and dealt X to each"
    return ass, notes, verdict, reason


async def finish(games, ass, notes, verdict, reason, t_start):
    dur = time.time() - t_start
    per_game = []
    for g in games:
        per_game.append({
            "label": g["label"], "plan": g["plan"],
            "x_chosen": g.get("x_chosen"), "kick_paid": g.get("kick_paid"),
            "kick_subs": g.get("kick_subs"), "kicker_done": g.get("kicker_done"),
            "target_prompts": g.get("target_prompts"),
            "target_iids": g.get("target_iids"),
            "target_choices": g.get("target_choices"),
            "cast_turn": g.get("cast_turn"),
            "obs": g["obs"],
        })
    run = {
        "issue": 6758,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID} (reused the already-listening "
                            "v0.105.0 server on 127.0.0.1:9374; games are "
                            "isolated per scenario game code)",
        "driver": {"protocol_advertised": 120,
                   "client": "driver/client.py",
                   "scenario": "driver/scenario_6758_01050.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6758_01050.py", "rb").read()
        ).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "games": per_game,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "verdict_reason": reason,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Comet Storm deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay.",
        ],
        "setup_line": "P0: 12x Comet Storm + 48x Mountain (mulligan to storm); "
                      "P1: 60x Mountain dummy",
        "contract_line": "Cast Comet Storm X=2 with kicker {1} paid once: 2 "
                         "target prompts (P1 then P0), X=2 damage to EACH "
                         "target, storm to graveyard; control X=1 no kicker: "
                         "1 prompt, clean resolution",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copyfile(f"{BACKFILL}/driver/scenario_6758_01050.py",
                    f"{EVDIR}/scenario_6758_01050.py")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    # manifest LAST, after all logging/writes are done
    await write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return 0 if verdict in ("reproduced", "not-reproduced") else 2


async def main():
    pidfile = "/tmp/scenario_6758_01050.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_6758_01050 instance is alive "
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
    t_start = time.time()
    await verify_server_hello()
    check_data_level()

    plan1 = {"x": 2, "kick": True, "targets": [1, 0], "need_lands": 5}
    plan2 = {"x": 1, "kick": False, "targets": [1], "need_lands": 3}

    games = []
    for label, plan in (("g1_kicked", plan1), ("g2_control", plan2)):
        say(f"=== {label}: plan={plan} ===")
        SUBMITTED_OPPS.clear()
        MULLS.clear()
        g = new_game_state(label, plan)
        p0 = PhaseClient(f"P0-6758-{label}")
        await p0.connect()
        await p0.create(deck(*P0_DECK))
        p1 = PhaseClient(f"P1-6758-{label}")
        await p1.connect()
        await p1.join(p0.game_code, deck(*P1_DECK))
        say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
        try:
            await drive_game(p0, p1, g)
        finally:
            try:
                await p0.close()
            except Exception:
                pass
            try:
                await p1.close()
            except Exception:
                pass
        games.append(g)
        wire("game_summary", {"label": label,
                              "assert": {k: v for k, v in g["ass"].items()},
                              "notes": g["notes"][-5:]})

    ass, notes, verdict, reason = await verdict_for(games)
    say(f"VERDICT: {verdict} -- {reason}")
    rc = await finish(games, ass, notes, verdict, reason, t_start)
    sys.exit(rc)


asyncio.run(main())
