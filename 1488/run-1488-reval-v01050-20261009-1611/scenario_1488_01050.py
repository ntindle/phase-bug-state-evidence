#!/usr/bin/env python3
"""Issue #1488: Fateful Tempest -- "Card just." (does not work).

Re-validation run on v0.105.0 / protocol 120 (prior: v0.104.0 / protocol 118,
run run-1488-reval-v01040-20261008-1711, verdict reproduced).

Pin-only port of scenario_1488_01040.py: game logic, decks, assertions and the
verdict rule are byte-identical; only the version/build/provenance pins are
updated to v0.105.0 (protocol 120; interaction conventions unchanged from the
verified 118 run). The
protocol-120 interaction-surface notes below carry over unchanged:

Protocol-106 driver notes (v0.103.0, 2026-10-06) [original text preserved]:
ported from the verified v0.102.0 scenario_1488_01020.py (protocol-106; the
2026-10-04 v0.102.0 run reproduced the bug, A1-A6 as recorded in the ledger).
  - MulliganDecision: legacy legal action (verified accepted on 120), the
    canonical surface being the vi exactChoices opportunity with action code
    mulliganDecision and waitingForKind.code == "mulligan". P0 keeps with
    Tempest in hand (mulligan to tempest + 2 lands); P1 dummy always keeps.
  - Bottom-after-mulligan: per-card SelectCards legal actions + vi
    schema/select opportunity, gated on waitingForKind.code == "mulligan"
    AND turn 1 / Untap (the Altar/KCI sacrifice-cost prompt shares the
    identical surface mid-game; the gate keeps them disjoint).
  - DiscardToHandSize: vi only; 120 uses a generic waitingForKind.code of
    "choose", so the heuristic additionally requires hand > 7 plus a
    schema/select opportunity offering own hand cards, with a verbatim
    SelectCards fallback guarded against the mulligan-bottom prompt.
  - Priority: waiting_for is GONE (null) on 120; the viewing seat holds
    priority iff a PassPriority legal action is advertised to it.
  - Priority menus are noisy on 120 (tapLandForMana / untapLandForMana /
    castSpell / activateAbility / passPriority offered at ordinary windows):
    real_decision_pending must never treat those as decisions, and the vote
    scan only fires on choices mentioning past/present/vote.
  - CastSpell: merged_action first, else the vi castSpell choice.
  - Mana payment while casting: vi tapLandForMana with needs
    {"R": 1, "generic": 2}, plus PayMana*/PayManaAbilityMana submitted as-is,
    plus ActivateAbility taps on untapped Mountains as a fallback.
  - PassPriority: legacy action first, then the viewer_interaction
    passPriority action-code fallback.
  - Deck schema on 120 is {"name", "main_deck": [<card-name strings>]}
    (client.py deck() already returns it).
  - await asyncio.sleep(0) yield before leg evaluation (driver-race guard);
    never return after an export while holding priority (fall through to the
    priority pass); 5s re-tick backstop for a priority holder.
  - ExportAuthoritativeState has NO data field; data.state is a JSON string
    parsed once.
  - manifest.sha256 computed AFTER wire_log.jsonl / scenario_run.log close.
  - zero observations map to not-run (never fabricated as failed).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Report (discord, 2026-05-30): "Fateful Tempest -- Card just."
Triage comment (mike-theDude, 2026-07-10): the parsed spell ability's
top-level effect is Effect::Unimplemented { name: "vote", ... } and casting
"currently does nothing for the vote/mill/damage portion".
Prior backfill validations (v0.78.0, v0.84.0, v0.101.0): verdict reproduced --
the spell resolves with no observable effect; no vote offered, no mill, no
damage, no exile.

Oracle text (verified from pinned v0.105.0 card-data.json, key 'fateful tempest'):
  "Council's dilemma -- Starting with you, each player votes for past or
   present. You mill a card for each past vote, then Fateful Tempest deals
   damage to each opponent equal to the total mana value of cards milled this
   way. Exile the top card of your library for each present vote. Until the
   end of your next turn, you may play the exiled cards."
Pinned parse (v0.102.0 dataset; re-checked against v0.105.0 at run start):
  top-level effect = Unimplemented { name: "unrecognized_clause_head",
      description: "vote for past or present" }
  sub-chain (SequentialSibling):
    Mill { Fixed 1, target Controller, dest Graveyard }
    -> DamageEachPlayer { Ref PropertyAggregate Sum(ManaValue, TrackedSet),
                          filter Opponent }
    -> ExileTop { Controller, Fixed 1 }
    -> CastFromZone { ExiledBySource, Play, UntilEndOfNextTurnOf Controller }
The vote itself remains Unimplemented; the mill/damage/exile sub-effects are
parsed with FIXED counts of 1 instead of per-vote counts.

Setup (native engine, two human-client seats, protocol-120 driver):
  P0: 12x Fateful Tempest ({2}{R} sorcery) + 48x Mountain.
      (12x density: engine accepts >4-of for custom games; mulligan to
      Tempest + 2+ lands.)
  P1: 60x Mountain dummy (plays a land, passes; never attacks).

Expected (per Oracle text):
  E1: after P0 casts Fateful Tempest, each player is offered a past/present
      vote (council's dilemma, starting with P0).
  E2: P0 mills one card per past vote.
  E3: each opponent is dealt damage equal to the total mana value of the
      cards milled this way.
  E4: P0 exiles the top card of their library per present vote and may play
      those cards until the end of P0's next turn.
  E5: Tempest goes to P0's graveyard, stack empties, game proceeds.

Assertions:
  A1_setup_ok    pre.json: P0 main phase, Tempest in hand, >=3 untapped
                 Mountains, life 20/20.
  A2_vote_prompted  a past/present vote opportunity is advertised to the
                 players between cast and resolution (recorded in wire log).
  A3_mill        P0 graveyard grows by N cards during resolution; N and the
                 milled card names recorded.
  A4_damage      P1 life delta during resolution; consistency check:
                 damage == sum of MV of milled cards.
  A5_exile       exile-zone delta during resolution (cards exiled by the
                 Tempest resolution), names recorded.
  A6_cleanup     post.json: Tempest in P0's graveyard, stack empty, turn
                 advanced past the cast turn, game proceeding.

Verdict rule: reproduced iff A1 passed and the Oracle-mandated
vote/mill/damage/exile flow is observably broken (no vote prompt, and/or
mill/exile counts inconsistent with any legal 2-player vote outcome, and/or
no mill/damage/exile at all). not-reproduced iff a vote prompt appeared,
votes were recorded, and mill/damage/exile matched the votes per Oracle.
blocked iff the game cannot be driven to a Tempest cast.

Evidence: evidence/1488/<run-id>/{pre,cast_on_stack,mid_vote,post}.json,
run.json, manifest.sha256, summary.png, scenario_1488_01050.py,
wire_log.jsonl, scenario_run.log
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
RUN_ID = os.environ.get("RUN_ID", "run-1488-reval-v01050-20261009-1611")
EVDIR = f"{BACKFILL}/evidence/1488/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

URL = "ws://127.0.0.1:9374/ws"

TEMPEST = "Fateful Tempest"   # display name in card-data.json
MOUNTAIN = "Mountain"

P0_DECK = [(TEMPEST, 12), (MOUNTAIN, 48)]
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
    "observed_at": "2026-10-09",
    "source": "digests recomputed via sha256sum against the pinned v0.105.0 "
              "release files under server/releases/v0.105.0/; raw ServerHello "
              "on 127.0.0.1:9374 verified by this run "
              "(server_version 0.105.0 / build 965e243 / protocol 120 / Full)",
}

CARD_DATA = json.load(
    open(f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"))

SUBMITTED_OPPS = set()  # answered viewer_interaction interactionIds
MULLS = set()           # (tag, "mull", iid|rev) guards


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


def mana_value(o):
    for key in ("mana_cost", "base_mana_cost"):
        c = o.get(key)
        if isinstance(c, dict):
            if c.get("type") == "NoCost":
                return 0
            if c.get("type") == "Cost":
                try:
                    return int(c.get("generic", 0)) + len(c.get("shards", []) or [])
                except Exception:
                    return None
    return None


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


def tempest_in_hand_oid(state, pid):
    for o in player_of(state, pid).get("hand", []):
        if obj_name(state, o) == TEMPEST.lower():
            return int(o)
    return None


def tempest_spell_on_stack(state, cast_oid=None):
    """Stack entries for P0's Tempest. Protocol-118 stack entries may not
    carry the card name; match by name blob, by cast object id, or -- P0
    casts no other spells in this scenario -- by controller/kind."""
    out = []
    for e in (state.get("stack", []) or []):
        blob = json.dumps(e, default=str).lower()
        if "fateful tempest" in blob:
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


def exile_names(state):
    return [obj_name(state, o) for o in (state.get("exile", []) or [])]


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def vi_kind_code(st):
    """viewer_interaction waitingForKind code (protocol 118 replaces the
    old waiting_for decision surface; e.g. 'mulligan')."""
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


def my_priority(acts):
    """Protocol 118: the viewing seat holds priority iff a PassPriority
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


async def pay_mana_vi(c, st, tag, needs=None):
    """Answer vi tapLandForMana payment prompts (protocol 118). `needs` is
    a dict like {"R": 1, "generic": 2}; consumed as lands are tapped."""
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
    """Protocol 118: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission); the canonical
    surface is the viewer_interaction exactChoices opportunity whose
    interactionId is tracked to avoid double-answering."""
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
    """Protocol 118: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Answer via the vi opportunity
    with all chosen candidate ids in one submission.

    CRITICAL: the sacrifice-cost prompt shares this exact surface mid-game.
    Disambiguate by the waitingForKind code AND game stage: bottoming only
    happens while the code is 'mulligan' at turn 1 / Untap.
    """
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
            f"not answering")
        wire(f"{tag}_bottom_no_vi",
             {"acts": [a.get("data") for a in sel_acts][:8]})
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
        if nm == TEMPEST.lower():
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
    """Protocol 118: DiscardToHandSize surfaces via viewer_interaction.
    118 uses a generic 'choose' waitingForKind code for the cleanup discard
    prompt, so the heuristic additionally requires hand > 7 plus a
    schema/select opportunity offering our own hand cards. A verbatim
    per-card SelectCards fallback is guarded against the mulligan-bottom
    and sacrifice prompts (never steals them)."""
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
            if nm == TEMPEST.lower():
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
    # ---- verbatim SelectCards fallback (never steals mulligan-bottom) ----
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
            if nm == TEMPEST.lower():
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


def find_vote_opp(st, g):
    """Return (opp, past_choice, texts) for a council's-dilemma vote
    opportunity, else None. Choices mentioning past AND present (or a vote
    prompt) count. The noisy 118 priority menus (tapLandForMana /
    castSpell / passPriority / activateAbility) never carry these texts,
    so they cannot trigger a false vote."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        texts = [choice_text(ch).lower() for ch in chs]
        blob = " // ".join(texts)
        low = blob + " " + json.dumps(opp, default=str).lower()
        has_past = any("past" in t for t in texts)
        has_present = any("present" in t for t in texts)
        if (has_past and has_present) or ("vote" in low and (has_past or has_present)):
            past = next((ch for ch, t in zip(chs, texts) if "past" in t), None)
            return opp, past, texts
    return None


async def scan_vote(c, st, state, tag, note, g):
    """Log interaction shapes; vote 'past' on any council's-dilemma prompt.
    Returns True if it acted."""
    found = find_vote_opp(st, g)
    if not found:
        return False
    opp, past, texts = found
    iid = opp.get("interactionId")
    rtype = (opp.get("response", {}) or {}).get("type")
    if not g["obs"]["vote_seen"]:
        g["obs"]["vote_seen"] = True
        say(f"[{tag}] *** VOTE PROMPT OBSERVED ***")
    g["obs"]["vote_opportunities"].append(
        {"who": tag, "rtype": rtype, "choices": texts[:12],
         "interactionId": iid})
    wire("vote_prompt", {"who": tag, "interaction": opp})
    if past is None:
        note(f"vote prompt had no 'past' choice: {' // '.join(texts)[:200]}")
        return False
    note(f"{tag} votes PAST (deterministic test policy)")
    wire("vote_submission", {"who": tag, "choiceId": past.get("id")})
    await answer_vi(c, opp, past, tag)
    SUBMITTED_OPPS.add(iid)
    g["votes_cast"] += 1
    if not g.get("mid_vote_exported"):
        try:
            mid = await c.export_state()
            with open(f"{EVDIR}/mid_vote.json", "w") as f:
                f.write(mid)
            g["mid_vote_exported"] = True
            note("exported MID (vote prompt observed)")
        except Exception as e:
            note(f"mid vote export failed: {e}")
    return True


async def p0_tick(c, g, st):
    state = st["state"]

    def note(m):
        say(f"[P0] {m}")
        g["notes"].append(m)

    acts = merged_actions(st)
    # ---- mulligan / bottom / discard (decision-driven, not priority) ----
    if await do_mulligan(c, acts, st, 0, "P0",
                         lambda hn, mulls: (TEMPEST.lower() in hn) or mulls >= 4):
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

    # ---- mana payments advertised by the engine are submitted as-is ----
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("p0_auto_pay", {"action": {k: v for k, v in a.items()
                                            if not k.startswith("_")}})
            await submit_as_is(c, a)
            return

    # ---- vote scan (council's dilemma prompt, if the engine ever offers it) ----
    if await scan_vote(c, st, state, "P0", note, g):
        return

    # ---- mid export: Tempest spell on the stack (fall through afterwards) ----
    # Once the spell is on the stack, payment is done -- clear the paying
    # flag so priority passes.
    on_stack = tempest_spell_on_stack(state, g.get("cast_oid"))
    if g["cast_submitted"] and on_stack:
        g["paying"] = False
    if g["cast_submitted"] and not g.get("cast_on_stack_exported") and on_stack:
        g["paying"] = False
        try:
            mid = await c.export_state()
            with open(f"{EVDIR}/cast_on_stack.json", "w") as f:
                f.write(mid)
            g["cast_on_stack_exported"] = True
            note("exported MID (Tempest on stack)")
        except Exception as e:
            note(f"cast-on-stack export failed: {e}")
        # fall through: never return after an export while holding priority

    # ---- post export: Tempest resolved (in gy), stack empty, game advanced ----
    if (g["cast_submitted"] and not g.get("post_exported")
            and TEMPEST.lower() in gy_names(state, 0)
            and not tempest_spell_on_stack(state, g.get("cast_oid"))
            and len(state.get("stack", []) or []) == 0
            and (state.get("turn_number", 0) > (g.get("cast_turn") or 0)
                 or state.get("phase") != g.get("cast_phase"))):
        note("Tempest resolved; exporting POST")
        try:
            post = await c.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            g["post_exported"] = True
            g["phase"] = "done"
            note("exported POST")
        except Exception as e:
            note(f"post export failed: {e}")
        return

    if not my_priority(acts):
        return

    # ---- paying for a pending cast: tap Mountains via the payment menu ----
    if g.get("paying"):
        needs = g.get("mana_needs", {})
        if sum(needs.values()) > 0:
            if await pay_mana_vi(c, st, "P0", needs):
                return
            for a in acts:
                if a["type"] == "ActivateAbility":
                    src = str(a.get("data", {}).get("source_id") or a.get("_src_oid"))
                    if obj_name(state, src) == MOUNTAIN.lower() \
                            and int(src) in untapped_mountains(state, 0):
                        wire("p0_tap_for_cast", {"mountain_oid": int(src)})
                        await submit_as_is(c, a)
                        return
        # Backstop: if no payment affordance appears and the spell never
        # reaches the stack, do not hold priority hostage forever.
        if g.get("paying_since") and time.time() - g["paying_since"] > 90:
            note("paying backstop: no payment affordance for 90s; clearing "
                 "paying flag")
            wire("paying_backstop", {})
            g["paying"] = False
            g["mana_needs"] = {}
        else:
            return  # paying; wait for the next revision

    # ---- P0 priority: cast Tempest when set up ----
    if (not g["cast_submitted"]
            and my_main(state, 0)
            and tempest_in_hand_oid(state, 0) is not None
            and len(untapped_mountains(state, 0)) >= 3):
        note("P0 main phase: exporting PRE, then casting Fateful Tempest")
        pre = await c.export_state()
        with open(f"{EVDIR}/pre.json", "w") as f:
            f.write(pre)
        g["pre_exported"] = True
        offered, act = cast_spell_offered(state, st, TEMPEST.lower())
        if offered and "_vi_choice" not in act:
            d = act.get("data", {})
            wire("cast_tempest", {k: v for k, v in act.items()
                                  if not k.startswith("_")})
            g["cast_turn"] = state.get("turn_number")
            g["cast_phase"] = state.get("phase")
            g["cast_oid"] = d.get("object_id")
            await submit_as_is(c, act)
            g["cast_submitted"] = True
            g["paying"] = True
            g["paying_since"] = time.time()
            g["mana_needs"] = {"R": 1, "generic": 2}
            note(f"P0 casts Fateful Tempest (turn {g['cast_turn']}, "
                 f"oid {g['cast_oid']})")
            return
        if offered:  # vi castSpell choice
            wire("cast_tempest_vi", {"choiceId": act["_vi_choice"].get("id")})
            g["cast_turn"] = state.get("turn_number")
            g["cast_phase"] = state.get("phase")
            await answer_vi(c, act["_vi_opp"], act["_vi_choice"], "P0")
            g["cast_submitted"] = True
            g["paying"] = True
            g["paying_since"] = time.time()
            g["mana_needs"] = {"R": 1, "generic": 2}
            note(f"P0 casts Fateful Tempest via vi (turn {g['cast_turn']})")
            return
        note("cast action not offered on this tick; falling through to "
             "setup play")
        # fall through to normal play while the cast action is not yet offered
    # ---- normal setup play ----
    for a in acts:
        if a["type"] == "PlayLand":
            await submit_as_is(c, a)
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
    if await scan_vote(c, st, state, "P1", note, g):
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
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return
    if await pay_mana_vi(c, st, "P1"):
        return
    if my_main(state, 1):
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    if my_priority(acts):
        await pass_priority(c, st, acts)

def evaluate(g):
    """Evaluate A1..A6 from saved states + observations."""
    ass = g["ass"]
    notes = g["notes"]
    obs = g["obs"]
    try:
        pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
            if os.path.exists(f"{EVDIR}/pre.json") else None
        post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
            if os.path.exists(f"{EVDIR}/post.json") else None
    except Exception as e:
        notes.append(f"state reload failed: {e}")
        pre_st, post_st = None, None
    # A1
    if pre_st is not None:
        ok = (tempest_in_hand_oid(pre_st, 0) is not None
              and len(untapped_mountains(pre_st, 0)) >= 3
              and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
              and pre_st.get("phase") in ("PreCombatMain", "PostCombatMain"))
        obs["life_pre"] = [life_of(pre_st, 0), life_of(pre_st, 1)]
        if ok:
            ass["A1_setup_ok"] = "passed"
            notes.append("pre.json: P0 main phase, Tempest in hand, "
                         f"{len(untapped_mountains(pre_st, 0))} untapped "
                         "Mountains, life 20/20")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("pre.json setup precondition not met")
    else:
        ass["A1_setup_ok"] = "not-run"
        notes.append("no pre.json exported (cast never reached)")
    # A2
    if obs["vote_seen"]:
        ass["A2_vote_prompted"] = "passed"
        notes.append(f"vote prompt observed: {obs['vote_opportunities']}")
    elif g["cast_submitted"]:
        ass["A2_vote_prompted"] = "failed"
        notes.append("NO past/present vote opportunity was advertised to any "
                     "player during the Tempest cast/resolution (REPORTED BUG: "
                     "council's dilemma vote never happens)")
    else:
        ass["A2_vote_prompted"] = "not-run"
        notes.append("cast never submitted; vote prompt not observable")
    # A3/A4/A5 from pre/post deltas
    if pre_st is not None and post_st is not None:
        obs["life_post"] = [life_of(post_st, 0), life_of(post_st, 1)]
        pre_gy = set(str(o) for o in player_of(pre_st, 0).get("graveyard", []))
        post_gy = [o for o in player_of(post_st, 0).get("graveyard", [])]
        new_gy = [o for o in post_gy if str(o) not in pre_gy]
        excluded = {str(g["cast_oid"])} if g.get("cast_oid") else set()
        milled = [(obj_name(post_st, o), mana_value(get_obj(post_st, o)))
                  for o in new_gy if str(o) not in excluded]
        obs["milled"] = milled
        obs["mv_sum_milled"] = sum(m for _, m in milled if m is not None)
        obs["damage_to_p1"] = obs["life_pre"][1] - obs["life_post"][1]
        pre_ex = set(str(o) for o in (pre_st.get("exile", []) or []))
        new_ex = [o for o in (post_st.get("exile", []) or [])
                  if str(o) not in pre_ex]
        obs["exiled"] = [obj_name(post_st, o) for o in new_ex]
        say(f"milled={milled} mv_sum={obs['mv_sum_milled']} "
            f"dmg_p1={obs['damage_to_p1']} exiled={obs['exiled']}")
        ass["A3_mill"] = "passed" if milled else "failed"
        notes.append(f"mill during resolution: {len(milled)} card(s) "
                     f"{[n for n, _ in milled]} (Oracle: 1 per past vote)")
        if obs["damage_to_p1"] == obs["mv_sum_milled"] and milled:
            ass["A4_damage"] = "passed"
            notes.append(f"P1 took {obs['damage_to_p1']} damage == total MV "
                         f"of milled cards ({obs['mv_sum_milled']})")
        elif obs["damage_to_p1"] == 0 and not milled:
            ass["A4_damage"] = "failed"
            notes.append("no mill and no damage to P1 during resolution "
                         "(vote/mill/damage portion did nothing)")
        else:
            ass["A4_damage"] = "failed"
            notes.append(f"P1 damage={obs['damage_to_p1']} vs milled-MV-sum="
                         f"{obs['mv_sum_milled']} (inconsistent)")
        ass["A5_exile"] = "passed" if new_ex else "failed"
        notes.append(f"exiled during resolution: {len(new_ex)} card(s) "
                     f"{obs['exiled']} (Oracle: 1 per present vote)")
        # A6
        t_in_gy = TEMPEST.lower() in gy_names(post_st, 0)
        stack_empty = len(post_st.get("stack", []) or []) == 0
        advanced = (post_st.get("turn_number", 0) > (g.get("cast_turn") or 0)
                    or post_st.get("phase") != g.get("cast_phase"))
        if t_in_gy and stack_empty and advanced:
            ass["A6_cleanup"] = "passed"
            notes.append("post.json: Tempest in P0 graveyard, stack empty, "
                         f"game advanced (turn {post_st.get('turn_number')}, "
                         f"phase {post_st.get('phase')})")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"post.json: tempest_in_gy={t_in_gy} "
                         f"stack_empty={stack_empty} advanced={advanced}")
    else:
        for k in ("A3_mill", "A4_damage", "A5_exile", "A6_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} could not be evaluated (missing pre/post state)")
    # verdict
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("setup incomplete; see notes")
    elif ass["A2_vote_prompted"] == "failed":
        verdict = "reproduced"
    elif all(ass[k] == "passed" for k in
             ("A2_vote_prompted", "A3_mill", "A4_damage", "A5_exile",
              "A6_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
        notes.append("mixed assertion outcome; the Oracle-mandated vote "
                     "flow is broken in at least one required step")
    return verdict


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
    e = CARD_DATA.get("fateful tempest", {})
    if not e:
        ok = False
        notes.append("fateful tempest: missing from card data")
    elif "council's dilemma" not in str(e.get("oracle_text", "")).lower():
        ok = False
        notes.append("fateful tempest: oracle shape missing "
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


def capture_server_excerpts(game_codes):
    """Grep the live server's log for this run's game codes plus any
    CastSpell 'action applied' lines (the silent-cast-drop diagnosis hook
    from the protocol-118/120 cast-confirmation guard lesson) and write
    server_excerpts.txt into the evidence dir. Called before the manifest
    is computed so the file is covered by manifest.sha256."""
    srv_log = os.path.expanduser(
        "~/workspace/dev/phase-backfill/runs/run-1488-reval-v01050-20261009-1611/server.log")
    try:
        with open(srv_log, "r", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        with open(f"{EVDIR}/server_excerpts.txt", "w") as f:
            f.write(f"server log unreadable: {e}\n")
        say(f"server excerpts: log unreadable ({e})")
        return
    codes = {str(c) for c in game_codes if c}
    hits, cast_lines = {}, []
    for ln in all_lines:
        for code in codes:
            if code in ln:
                hits.setdefault(code, []).append(ln)
        if 'action_type="CastSpell"' in ln and "action applied" in ln:
            cast_lines.append(ln)
    out = [f"server log: {srv_log} ({len(all_lines)} lines scanned); "
           f"game codes: {sorted(codes)}\n"]
    for code in sorted(hits):
        buf = hits[code][-40:]
        out.append(f"\n===== game code {code} (last {len(buf)} of "
                   f"{len(hits[code])} lines) =====")
        out.extend(buf)
    if cast_lines:
        buf = cast_lines[-30:]
        out.append(f"\n===== CastSpell action-applied (last {len(buf)} of "
                   f"{len(cast_lines)} lines) =====")
        out.extend(buf)
    else:
        out.append("\n===== no CastSpell 'action applied' lines in log =====")
    with open(f"{EVDIR}/server_excerpts.txt", "w") as f:
        f.write("\n".join(l.rstrip("\n") for l in out) + "\n")
    say(f"server excerpts: {sum(len(v) for v in hits.values())} game-code "
        f"lines, {len(cast_lines)} CastSpell-applied lines")


async def finish(g):
    dur = time.time() - g["t_start"]
    if not g.get("post_exported"):
        try:
            post = await g["p0"].export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            g["post_exported"] = True
            say("post exported at finish")
        except Exception as e:
            g["notes"].append(f"post export at finish failed: {e}")
    verdict = evaluate(g)
    run = {
        "issue": 1488,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(g["t_start"])),
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 120, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_1488_01050.py", "rb").read()
        ).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": g["ass"],
        "notes": g["notes"],
        "observations": g["obs"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Fateful Tempest deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "If a vote prompt had appeared, the driver would have voted "
            "'past' for both seats (deterministic test policy).",
            "States are authoritative exports, restorable only via full game replay."],
        "setup_line": "P0: 12x Fateful Tempest + 48x Mountain (mulligan to tempest + 2 lands); "
                      "P1: 60x Mountain dummy",
        "contract_line": "Cast Fateful Tempest: council's-dilemma vote (past/present) "
                         "offered to each player, then mill 1 per past vote, damage each "
                         "opponent = total MV milled, exile top 1 per present vote, may "
                         "play exiled cards until end of next turn",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copyfile(f"{BACKFILL}/driver/scenario_1488_01050.py",
                    f"{EVDIR}/scenario_1488_01050.py")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    # server excerpts BEFORE the manifest so the file is covered by it
    _codes = []
    try:
        _codes = [g["p0"].game_code]
    except Exception:
        pass
    capture_server_excerpts(_codes)
    # manifest LAST, after all logging/writes are done
    await write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(g['ass'])}", flush=True)


async def main():
    pidfile = "/tmp/scenario_1488_01050.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_1488_01050 instance is alive "
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
    g = {"t_start": t_start, "phase": "setup",
         "notes": [],
         "ass": {k: "not-run" for k in
                 ("A1_setup_ok", "A2_vote_prompted", "A3_mill",
                  "A4_damage", "A5_exile", "A6_cleanup")},
         "obs": {"vote_seen": False, "vote_opportunities": [],
                 "cast_submitted": False, "cast_turn": None,
                 "cast_phase": None, "cast_oid": None,
                 "votes_cast": 0, "milled": [], "exiled": [],
                 "life_pre": None, "life_post": None,
                 "damage_to_p1": None, "mv_sum_milled": None},
         "cast_submitted": False, "paying": False, "paying_since": None,
         "mana_needs": {},
         "pre_exported": False, "cast_on_stack_exported": False,
         "mid_vote_exported": False, "post_exported": False,
         "votes_cast": 0}
    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P0-1488")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1-1488")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    g["p0"], g["p1"] = p0, p1
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    last_rev = {}
    last_change = {}
    last_tick_at = {}
    stuck_deadline = None
    last_diag = 0.0
    t0 = t_start
    try:
        while time.time() - t0 < 1200:
            await asyncio.sleep(0.15)
            for c, tick in ((p0, p0_tick), (p1, p1_tick)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                    last_change[c.name] = time.time()
                else:
                    if time.time() - last_change.get(c.name, t0) > 45:
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
                    # Safety net: if this client holds priority but produced
                    # no revision for a while, re-tick anyway -- a tick that
                    # returned without submitting must not stall the game.
                    holds_prio = any(
                        a.get("type") == "PassPriority"
                        for a in (st.get("legal_actions") or []))
                    if not (holds_prio
                            and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    await tick(c, g, st)
                except Exception as e:
                    say(f"tick error {c.name}: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            if g.get("phase") == "done" and g.get("post_exported"):
                say("post exported; finishing")
                await finish(g)
                return
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                s = p0.latest["state"]
                say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} vikind={vi_kind_code(p0.latest)} "
                    f"P0hand={hand_names(s, 0)[:8]} "
                    f"P0lands={len(untapped_mountains(s, 0))} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"stack={len(s.get('stack') or [])} cast={g['cast_submitted']} "
                    f"vote={g['obs']['vote_seen']} pre={g['pre_exported']} "
                    f"post={g['post_exported']}")
            if g["cast_submitted"] and not g.get("post_exported") \
                    and stuck_deadline is None:
                stuck_deadline = time.time() + 300
            if not g["cast_submitted"] or g.get("post_exported"):
                stuck_deadline = None
            if stuck_deadline and time.time() > stuck_deadline:
                g["notes"].append(
                    "Tempest cast but post-resolution state not reached in 300s; "
                    "see wire log (possible unhandled interaction)")
                await finish(g)
                return
        g["notes"].append("global timeout (1200s) hit before assertions resolved")
        await finish(g)
    finally:
        try:
            await p0.close()
        except Exception:
            pass
        try:
            await p1.close()
        except Exception:
            pass


asyncio.run(main())
