#!/usr/bin/env python3
"""Issue #1488: Fateful Tempest -- "Card just." (does not work).

Re-validation run on v0.101.0 / protocol 103 (prior: v0.84.0 / protocol 71,
run 20260915-1488, verdict reproduced; original: v0.78.0 / protocol 68,
run 20260909-1488, reproduced).

Protocol-103 port of scenario_1488.py. Game logic, decks, assertions and the
verdict rule are unchanged; only the interaction surface is adapted:
  - MulliganDecision as {choice:{type:Keep}} gated on
    waiting_for.data.pending[] Declare entries keyed by mulligan_count
    (permanent per-player guard; always-Keep for the P1 dummy)
  - BottomCards via the advertised viewer_interaction opportunity first,
    legacy SelectCards as fallback
  - DiscardToHandSize answered ONLY through viewer_interaction (the legacy
    SelectCards action is silently ignored on protocol 103)
  - CastSpell: merged_action first, else viewer_interaction castSpell choice
  - Mana payment while casting: PayMana*/PayManaAbilityMana as-is, plus
    ActivateAbility taps on untapped Mountains while a cast is pending
  - PassPriority: legacy action first, then the viewer_interaction
    passPriority action-code fallback
  - Council's-dilemma vote (if it ever appears): detected by scanning
    viewer_interaction opportunities for past/present choices; both seats
    vote "past" (deterministic test policy) via the advertised response type
  - await asyncio.sleep(0) yield before leg evaluation (driver-race guard)
  - never return after an export while holding priority (fall through to the
    priority pass); 5s re-tick safety net for a priority holder
  - ExportAuthoritativeState has NO data field; data.state is a JSON string
    parsed once
  - manifest.sha256 computed AFTER wire_log.jsonl / scenario_run.log close
  - zero observations map to not-run (never fabricated as failed)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Report (discord, 2026-05-30): "Fateful Tempest -- Card just."
Triage comment (mike-theDude, 2026-07-10): the parsed spell ability's
top-level effect is Effect::Unimplemented { name: "vote", ... } and casting
"currently does nothing for the vote/mill/damage portion".
Prior backfill validations (v0.78.0, v0.84.0): verdict reproduced -- the
spell resolves with no observable effect; no vote offered, no mill, no
damage, no exile.

Oracle text (verified from pinned v0.101.0 card-data.json, key 'fateful tempest'):
  "Council's dilemma -- Starting with you, each player votes for past or
   present. You mill a card for each past vote, then Fateful Tempest deals
   damage to each opponent equal to the total mana value of cards milled this
   way. Exile the top card of your library for each present vote. Until the
   end of your next turn, you may play the exiled cards."
Pinned parse (v0.101.0 dataset), same shape as v0.78.0/v0.84.0:
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

Setup (native engine, two human-client seats, protocol-103 driver):
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
run.json, manifest.sha256, summary.png, scenario_1488_01010.py,
wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261004-1488"
EVDIR = f"{BACKFILL}/evidence/1488/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TEMPEST = "Fateful Tempest"   # display name in card-data.json
MOUNTAIN = "Mountain"

P0_DECK = [(TEMPEST, 12), (MOUNTAIN, 48)]
P1_DECK = [(MOUNTAIN, 60)]

SERVER_IDENTITY = {
    "server_version": "0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "mode": "Full",
    "binary_sha256": "c32eabdcf93d04f61186558c223a12e6edbe15a678050863ae6d535165359b0a",
    "card_data_sha256": "b365361edafd3d901e361fe1eef845ca4748c7b2b371ee27e300013f64f37f00",
    "draft_pools_sha256": "75bb313864c341a99747e2a2a446dda5d83763bf073f5215d39289fdf8d34b06",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-10-04",
    "source": "digests recomputed via sha256sum against the pinned v0.101.0 "
              "release files under server/releases/v0.101.0/; raw ServerHello "
              "on 127.0.0.1:9374 verified by this run "
              "(server_version 0.101.0 / build acafe9b / protocol 103 / Full)",
}


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
    """Stack entries for P0's Tempest. Protocol-103 stack entries may not
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


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_ops(st):
    """Submittable vi opportunities. Protocol 103: vi is persistently
    canSubmit, so callers gate on expected game state."""
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_player(state):
    return ((state.get("waiting_for") or {}).get("data") or {}).get("player")


def my_prio(state, pid):
    if wf_type(state) != "Priority":
        return False
    return str(wf_player(state) if wf_player(state) is not None
               else state.get("priority_player")) == str(pid)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


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
    await c.send_interaction(sub)
    return sub


async def pass_priority(c, st, acts, tag):
    """Legacy PassPriority action first, then the viewer_interaction
    passPriority action-code fallback (protocol 103)."""
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
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": ch.get("id")}}}
                wire("pass_priority_vi", {"who": tag})
                await c.send_interaction(sub)
                return True
    return False


def map_oids_to_choice_ids(cands, oids):
    want = [str(o) for o in oids]
    picks = [ch.get("id") for ch in cands if str(ch.get("id")) in want]
    if len(picks) < len(oids):
        refmap = {}
        for ch in cands:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    refmap[str(d.get("reference"))] = ch.get("id")
        picks = [refmap[w] for w in want if w in refmap]
    return picks[:len(oids)]


async def do_bottom(c, state, st, acts, tag, note, g):
    """BottomCards phase. Protocol-103: vi opportunity first, legacy
    SelectCards fallback. Guarded per (tag, count)."""
    pending = ((state.get("waiting_for") or {}).get("data", {})
               or {}).get("pending", [])
    count = 0
    for p in pending:
        if str(p.get("player")) == str(g["pid"]):
            ph = p.get("phase", {}) or {}
            if ph.get("type") in ("BottomCards", "Bottom"):
                count = int(ph.get("count", 0))
    if count <= 0:
        return False
    key = (tag, "bottom", count)
    if key in g.get("answered_bottom", set()):
        return False
    hand = [o for pl in state.get("players", [])
            if str(pl.get("id")) == str(g["pid"]) for o in pl.get("hand", [])]
    oids = sorted(
        hand,
        key=lambda oid: 0 if obj_name(state, oid) == MOUNTAIN.lower()
        else (2 if obj_name(state, oid) == TEMPEST.lower() else 1))[:count]
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands:
            continue
        picks = map_oids_to_choice_ids(cands, oids)
        if not picks:
            continue
        spec = data.get("spec") or {}
        rtype = spec.get("type") or "select"
        iid = opp.get("interactionId")
        g.setdefault("answered_bottom", set()).add(key)
        g["answered"].add(iid)
        note(f"P{g['pid']} bottoms {count} after mulligan via vi ({rtype})")
        wire(f"{tag}_bottom_vi", {"count": count, "picks": picks})
        sub = {"interactionId": iid,
               "response": {"type": rtype, "data": {"choiceIds": picks}}}
        await c.send_interaction(sub)
        return True
    sc = next((a for a in acts if a["type"] == "SelectCards"), None)
    if sc:
        g.setdefault("answered_bottom", set()).add(key)
        picks = [int(x) for x in oids]
        note(f"P{g['pid']} bottoms {count} after mulligan via legacy SelectCards")
        wire(f"{tag}_bottom_legacy", {"count": count, "picks": picks})
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": picks}})
        return True
    return False


async def do_discard_to_handsize(c, state, st, tag, note, g):
    """DiscardToHandSize answered ONLY through viewer_interaction."""
    if str(wf_player(state)) != str(g["pid"]):
        return False
    hand = [o for pl in state.get("players", [])
            if str(pl.get("id")) == str(g["pid"]) for o in pl.get("hand", [])]
    prio = sorted(hand, key=lambda oid:
                  0 if obj_name(state, oid) == MOUNTAIN.lower()
                  else (2 if obj_name(state, oid) == TEMPEST.lower() else 1))
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in g["answered"]:
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands:
            continue
        want = {str(x) for x in prio[:1]}
        pick = next((ch for ch in cands if str(ch.get("id")) in want), None)
        if pick is None:
            continue
        spec = (data.get("spec") or {}).get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": spec, "data": {"choiceIds": [pick.get("id")]}}}
        await c.send_interaction(sub)
        g["answered"].add(iid)
        note(f"P{g['pid']} discards to hand size: "
             f"{obj_name(state, pick.get('id'))}")
        wire(f"{tag}_discard_handsize", {"submission": sub})
        return True
    return False


def find_vote_opp(st, g):
    """Return (opp, past_choice, texts) for a council's-dilemma vote
    opportunity, else None. Choices mentioning past AND present (or a vote
    prompt) count."""
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in g["answered"]:
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
        say(f"[{tag}] *** VOTE PROMPT OBSERVED ({g['who']}) ***")
    g["obs"]["vote_opportunities"].append(
        {"who": g["who"], "rtype": rtype, "choices": texts[:12],
         "interactionId": iid})
    wire("vote_prompt", {"who": g["who"], "interaction": opp})
    if past is None:
        note(f"vote prompt had no 'past' choice: {' // '.join(texts)[:200]}")
        return False
    note(f"{g['who']} votes PAST (deterministic test policy)")
    wire("vote_submission", {"who": g["who"], "choiceId": past.get("id")})
    await answer_vi(c, opp, past, tag)
    g["answered"].add(iid)
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


async def do_mulligan(c, state, acts, tag, note, g):
    """Protocol-103: MulliganDecision gated on pending[] Declare entries
    keyed by mulligan_count (permanent per-player guard)."""
    ma = next((a for a in acts if a["type"] == "MulliganDecision"), None)
    if not ma:
        return False
    pending = ((state.get("waiting_for") or {}).get("data", {})
               or {}).get("pending", [])
    pend = next((x for x in pending if str(x.get("player")) == str(g["pid"])),
                None)
    if pend is None:
        return False
    mcount = pend.get("mulligan_count", 0)
    if g.get("mull_answered_count") == mcount:
        return False
    hn = hand_names(state, g["pid"])
    mulls = g.get("mulls", 0)
    keep = g["keep_rule"](hn, mulls)
    g["mull_answered_count"] = mcount
    if keep:
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        note(f"P{g['pid']} keeps "
             f"(tempest={TEMPEST.lower() in hn}, lands={sum(1 for n in hn if n == MOUNTAIN.lower())}, "
             f"mulls={mulls})")
    else:
        g["mulls"] = mulls + 1
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        note(f"P{g['pid']} mulligans #{mulls + 1}")
    return True


async def p0_tick(g, st, acts, state):
    c = g["p0"]

    def note(m):
        say(f"[P0] {m}")
        g["notes"].append(m)

    # ---- mulligan ----
    if await do_mulligan(c, state, acts, "P0", note, g):
        return
    if wf_type(state) == "MulliganDecision":
        if await do_bottom(c, state, st, acts, "P0", note, g):
            return
        return
    if await do_discard_to_handsize(c, state, st, "P0", note, g):
        return

    # ---- driver-race guard: yield before evaluating legs ----
    await asyncio.sleep(0)

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
    # Payment was submitted with mode Auto: once the spell is on the stack,
    # payment is done -- clear the paying flag so priority passes.
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

    if not my_prio(state, 0):
        return

    # ---- paying for a pending cast: tap untapped Mountains ----
    if g.get("paying"):
        for a in acts:
            if a["type"] == "ActivateAbility":
                src = str(a.get("data", {}).get("source_id") or a.get("_src_oid"))
                if obj_name(state, src) == MOUNTAIN.lower() \
                        and int(src) in untapped_mountains(state, 0):
                    wire("p0_tap_for_cast", {"mountain_oid": int(src)})
                    await submit_as_is(c, a)
                    return
        # payment may also surface as a vi mana choice; answer generically
        for opp in vi_ops(st):
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            spec = data.get("spec") or {}
            if spec.get("type") in ("manaGroups",):
                cands = data.get("candidates") or data.get("choices") or []
                pick = next((ch for ch in cands
                             if "mountain" in choice_text(ch).lower()), None)
                if pick:
                    await answer_vi(c, opp, pick, "P0")
                    return
        # Backstop: payment was submitted with mode Auto; if the spell never
        # reaches the stack and no payment affordance appears, do not hold
        # priority hostage forever.
        if g.get("paying_since") and time.time() - g["paying_since"] > 90:
            note("paying backstop: no payment affordance for 90s; clearing "
                 "paying flag (payment was Auto)")
            wire("paying_backstop", {})
            g["paying"] = False
        else:
            return  # paying; wait for the next revision

    # ---- P0 priority: cast Tempest when set up ----
    if (not g["cast_submitted"]
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and tempest_in_hand_oid(state, 0) is not None
            and len(untapped_mountains(state, 0)) >= 3):
        for a in acts:
            d = a.get("data", {})
            if a["type"] == "CastSpell" \
                    and obj_name(state, d.get("object_id")) == TEMPEST.lower():
                note("P0 main phase: exporting PRE, then casting Fateful Tempest")
                pre = await c.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                g["pre_exported"] = True
                wire("cast_tempest", a)
                g["cast_turn"] = state.get("turn_number")
                g["cast_phase"] = state.get("phase")
                g["cast_oid"] = d.get("object_id")
                await submit_as_is(c, a)
                g["cast_submitted"] = True
                g["paying"] = True
                g["paying_since"] = time.time()
                note(f"P0 casts Fateful Tempest (turn {g['cast_turn']}, "
                     f"oid {g['cast_oid']})")
                return
        # castSpell may live in viewer_interaction on protocol 103
        for opp in vi_ops(st):
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "castSpell" in codes \
                        and TEMPEST.lower() in choice_text(ch).lower():
                    note("P0 main phase: exporting PRE, then casting via vi")
                    pre = await c.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    g["pre_exported"] = True
                    wire("cast_tempest_vi", {"choiceId": ch.get("id")})
                    g["cast_turn"] = state.get("turn_number")
                    g["cast_phase"] = state.get("phase")
                    await answer_vi(c, opp, ch, "P0")
                    g["cast_submitted"] = True
                    g["paying"] = True
                    note(f"P0 casts Fateful Tempest via vi (turn {g['cast_turn']})")
                    return
        # fall through to normal play while the cast action is not yet offered
    # ---- normal setup play ----
    for a in acts:
        if a["type"] == "PlayLand":
            await submit_as_is(c, a)
            return
    await pass_priority(c, st, acts, "P0")


async def p1_tick(g, st, acts, state):
    c = g["p1"]

    def note(m):
        say(f"[P1] {m}")
        g["notes"].append(m)

    if await do_mulligan(c, state, acts, "P1", note, g):
        return
    if wf_type(state) == "MulliganDecision":
        if await do_bottom(c, state, st, acts, "P1", note, g):
            return
        return
    if await do_discard_to_handsize(c, state, st, "P1", note, g):
        return

    await asyncio.sleep(0)

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return
    if await scan_vote(c, st, state, "P1", note, g):
        return

    wtype = wf_type(state)
    if wtype == "DeclareAttackers" and str(wf_player(state)) == "1":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = dict(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
        return
    if wtype == "DeclareBlockers" and str(wf_player(state)) == "1":
        da = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if da:
            d = dict(da.get("data", {}))
            d["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": d})
        return
    if wtype == "OrderTriggers" and str(wf_player(state)) == "1":
        oa = next((a for a in acts if a["type"] == "OrderTriggers"), None)
        if oa:
            await submit_as_is(c, oa)
        return
    if not my_prio(state, 1):
        return
    for a in acts:
        if a["type"] == "PlayLand":
            await submit_as_is(c, a)
            return
    await pass_priority(c, st, acts, "P1")


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


async def write_manifest():
    fnames = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    lines = []
    for fn in fnames:
        with open(os.path.join(EVDIR, fn), "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


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
        "driver": {"protocol_advertised": 103, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_1488_01010.py", "rb").read()
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
    shutil.copyfile(f"{BACKFILL}/driver/scenario_1488_01010.py",
                    f"{EVDIR}/scenario_1488_01010.py")
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
    print(f"DONE verdict={verdict} assertions={json.dumps(g['ass'])}", flush=True)


async def main():
    g = {"t_start": time.time(), "phase": "setup",
         "notes": [],
         "ass": {k: "not-run" for k in
                 ("A1_setup_ok", "A2_vote_prompted", "A3_mill",
                  "A4_damage", "A5_exile", "A6_cleanup")},
         "answered": set(), "answered_bottom": set(),
         "obs": {"vote_seen": False, "vote_opportunities": [],
                 "cast_submitted": False, "cast_turn": None,
                 "cast_phase": None, "cast_oid": None,
                 "votes_cast": 0, "milled": [], "exiled": [],
                 "life_pre": None, "life_post": None,
                 "damage_to_p1": None, "mv_sum_milled": None},
         "cast_submitted": False, "paying": False,
         "pre_exported": False, "cast_on_stack_exported": False,
         "mid_vote_exported": False, "post_exported": False,
         "votes_cast": 0}
    # raw ServerHello identity check before doing anything
    import websockets as _ws
    async with _ws.connect("ws://127.0.0.1:9374/ws", max_size=2**26) as _w:
        hello = json.loads(await asyncio.wait_for(_w.recv(), 10))
    d = hello.get("data", {})
    say(f"ServerHello: {d.get('server_version')}/{d.get('build_commit')}/"
        f"protocol {d.get('protocol_version')}/{d.get('mode')}")
    assert d.get("server_version") == "0.101.0", hello
    assert d.get("build_commit") == "acafe9b", hello
    assert d.get("protocol_version") == 103, hello

    p0 = PhaseClient("P0-1488")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1-1488")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    g["p0"], g["p1"] = p0, p1
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    # per-player tick contexts (mulligan guards, keep rules)
    # share mutable run state by reference for obs/notes/answered
    g0 = {"pid": 0, "who": "P0", "p0": p0, "answered": g["answered"],
          "answered_bottom": g["answered_bottom"], "obs": g["obs"],
          "notes": g["notes"], "votes_cast": 0, "mid_vote_exported": False,
          "keep_rule": lambda hn, mulls: (TEMPEST.lower() in hn) or mulls >= 4}
    g1 = {"pid": 1, "who": "P1", "p1": p1, "answered": g["answered"],
          "answered_bottom": g["answered_bottom"], "obs": g["obs"],
          "notes": g["notes"], "votes_cast": 0, "mid_vote_exported": False,
          "keep_rule": lambda hn, mulls: True}
    for gg in (g0, g1):
        gg.update({k: v for k, v in g.items()
                   if k in ("cast_submitted", "paying", "pre_exported",
                            "cast_on_stack_exported", "post_exported",
                            "cast_turn", "cast_phase", "cast_oid", "phase")})
    tick_state = {"g0": g0, "g1": g1}

    async def sync_back():
        for k in ("cast_submitted", "paying", "pre_exported",
                  "cast_on_stack_exported", "post_exported",
                  "cast_turn", "cast_phase", "cast_oid", "phase"):
            g[k] = tick_state["g0"].get(k, g.get(k))
        g["votes_cast"] = (tick_state["g0"].get("votes_cast", 0)
                           + tick_state["g1"].get("votes_cast", 0))
        g["mid_vote_exported"] = (tick_state["g0"].get("mid_vote_exported")
                                  or tick_state["g1"].get("mid_vote_exported"))

    last_rev = {}
    last_change = {}
    last_tick_at = {}
    stuck_deadline = None
    last_diag = 0.0
    t0 = g["t_start"]
    try:
        while time.time() - t0 < 1200:
            await asyncio.sleep(0.15)
            await sync_back()
            for c, ts, tick in ((p0, tick_state["g0"], p0_tick),
                                (p1, tick_state["g1"], p1_tick)):
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
                        say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                            f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                            f"wf={(s0.get('waiting_for') or {}).get('type')}")
                        wire("watchdog_stale",
                             {"who": c.name, "rev": c.revision,
                              "turn": s0.get("turn_number"),
                              "phase": s0.get("phase"),
                              "wf": (s0.get("waiting_for") or {}).get("type")})
                        last_change[c.name] = time.time()
                    wf = st["state"].get("waiting_for") or {}
                    holds_prio = (wf.get("type") == "Priority" and str(
                        (wf.get("data") or {}).get("player")) == str(c.player_id))
                    if not (holds_prio
                            and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    await tick(ts, st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"tick error {c.name}: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": c.name,
                                        "err": f"{type(e).__name__}: {e}"})
            await sync_back()
            if g.get("phase") == "done" and g.get("post_exported"):
                say("post exported; finishing")
                await finish(g)
                return
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                s = p0.latest["state"]
                say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
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
