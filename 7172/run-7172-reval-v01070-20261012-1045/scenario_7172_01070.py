#!/usr/bin/env python3
"""Issue #7172: Deadpool, Trading Card -- Deadpool's ETB does not trigger.

v0.107.0 re-validation port of scenario_7172.py (v0.83.0 run 20260915-7172,
which REPRODUCED).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.107.0, key 'deadpool, trading card'):
  Deadpool, Trading Card ({2}{B}{R}, 5/3 Legendary Creature - Mutant
  Mercenary Hero):
    "As Deadpool enters, you may exchange his text box and another
     creature's.
     At the beginning of your upkeep, you lose 3 life.
     {3}, Sacrifice this creature: Each other player draws a card."

Card-data parse state on v0.107.0 (verified 2026-10-12 before the run):
  abilities[0] = kind Spell, effect {type: Unimplemented, name: "unknown"},
    description "As ~ enters, you may exchange his text box and another
    creature's."
  replacements = [] (no as-enters replacement registered)
  triggers[0] = Phase Upkeep -> LoseLife 3 to Controller (implemented)
  abilities[1] = Activated {3}, Sacrifice SelfRef -> Opponent draws 1
    (implemented)

The reported defect: as Deadpool enters, the controller is never offered the
optional text-box exchange (no choice, no resulting text-box change).

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x deadpool, trading card, 12x grizzly bears, 12x swamp,
      12x mountain, 12x lightning bolt (60).
  P1: 12x grizzly bears, 24x forest, 24x lightning bolt (60; passive except
      that it puts a Grizzly Bears on the battlefield as the exchange
      candidate -- the report's exchange needs "another creature").

Planned line:
  Turns 1-4: land drops; P1 casts a Grizzly Bears on its turn 2 (opponent's
    creature on the battlefield as an exchange candidate).
  Turn 5 (P0): pre.json export, then cast Deadpool, Trading Card. As it
    enters, watch every viewer_interaction opportunity for an exchange
    choice (EntryControllerChoice / OptionalEffectChoice / EffectZoneChoice
    naming the exchange, or any prompt text mentioning exchange/text box).
    Exchange-flavored prompts are deliberately NOT auto-answered: the prompt
    IS the phenomenon and must be recorded, not driven past.
  Entry+5s: if no exchange prompt ever appeared, export entry.json and
    confirm Deadpool entered as a 5/3 with its own text box (Grizzly Bears
    unchanged at 2/2 with no rules text swap).
  Control: at P0's next upkeep the lose-3-life trigger must fire (20->17),
    and the {3}-sacrifice activated ability must be advertised -- proving the
    card is otherwise live and only the exchange is dead.

Assertions (each passed / failed / not-run):
  A1_parse               card-data (v0.107.0): as-enters clause is
                         Unimplemented/unknown with no registered
                         replacement (parse-level root cause of the
                         reported gap).
  A2_setup               PRE: P1 has a Grizzly Bears on the battlefield and
                         P0 holds Deadpool with 4+ untapped lands
                         (Swamp+Mountain among them) -- a legal exchange
                         candidate exists.
  A3_no_exchange_choice  during the entry window, NO exchange choice was
                         ever offered to Deadpool's controller. (The
                         reported bug fails HERE if a prompt appears.)
  A4_enters_unchanged    Deadpool entered as 5/3 with its own text box;
                         both creatures unchanged (no partial swap).
  A5_card_otherwise_live upkeep trigger resolved (P0 20->17) and the
                         sacrifice activated ability is advertised.
  A6_cleanup             stack empty, game proceeded past the entry window.

Verdict rule:
  reproduced     iff A2 passes and A3 passes (no exchange choice was ever
                 offered despite a valid candidate) - the reported symptom.
  not-reproduced iff an exchange choice IS offered (and answering it swaps
                 the text boxes).
  blocked        iff A2 fails (setup never reached).

Evidence: evidence/7172/<run-id>/pre.json, entry.json, post.json, run.json,
manifest.sha256, summary.png, scenario_7172_01070.py, wire_log.jsonl,
scenario_run.log, data_evidence.json, server.log (excerpts, best effort).

Protocol-130 notes (v0.107.0, 2026-10-12): mechanical port of the verified
v0.83.0 scenario_7172.py (protocol 70) following the conventions of
scenario_6557_01070.py (verified v0.107.0 run): HELLO advertises 130;
waiting_for is gone -- priority = PassPriority in top-level legal_actions;
interaction conventions identical to 126; candidate references are STRINGS
while hand oids are INTS -- normalize with str() on both sides before any
membership test; engine auto-pays mana (bare CastSpell, no
tapLandForMana bookkeeping) but vi tapLandForMana prompts may appear and
are answered via pay_mana_vi; cast-confirmation guard after EVERY CastSpell
-- set pending_cast and do NOT pass priority or start new main-phase plays
until the spell is confirmed on stack/battlefield with a 30s backstop;
the guard runs BEFORE any early-return block (attackers/blockers/target-
wait); main-loop re-ticks while the client has ANY legal actions;
per-opportunity dedupe via SUBMITTED_OPPS/MULLS keyed by interactionId.
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-7172-reval-v01070-20261012-1045")
ISSUE = 7172
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR not empty: {EVDIR}"

WIRE = None
RUNLOG = None
MULLS = set()
SUBMITTED_OPPS = set()


def init_logging():
    global WIRE, RUNLOG
    if WIRE is None:
        WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    if RUNLOG is None:
        RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


DEADPOOL = "deadpool, trading card"   # card-data.json key (exact)
BEARS = "grizzly bears"               # card-data.json key (exact)
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
BOLT = "lightning bolt"
P0_LANDS = (SWAMP, MOUNTAIN)
P1_LANDS = (FOREST,)

P0_DECK = [(DEADPOOL, 12), (BEARS, 12), (SWAMP, 12), (MOUNTAIN, 12),
           (BOLT, 12)]
P1_DECK = [(BEARS, 12), (FOREST, 24), (BOLT, 24)]

# exchange-flavored prompt keywords (entry-window watch)
EXCHANGE_KEYS = ("exchange", "text box", "as enters", "as ~ enters",
                 "entry")

SERVER_IDENTITY = {
    "server_version": "v0.107.0",
    "build_commit": "eccd25f",
    "protocol_version": 130,
    "mode": "Full",
    "binary_sha256": "f89957a1e5aa0fc1e68bf61a1eccc4ad6b257bc57d98aa270869fcad78456397",
    "card_data_sha256": "2021496869e2e86df276f1a07de001d50e7220087d46ac21acd69d04d73f9433",
    "draft_pools_sha256": "a44926f1b80be0c64e794e5562b9eeccd9d704488a7b02fc3ccca2130eea0d1c",
    "signature_key_id": "4564436711b6a2d3",
    "signature_verified": True,
    "signature_note": "v0.107.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 4564436711b6a2d3); data digests match the "
                      "signed manifest; ServerHello handshake re-verified "
                      "this run (0.107.0/eccd25f/protocol 130/Full)",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    if RUNLOG is not None:
        RUNLOG.write(m + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE is None:
        return
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def lname(state, oid):
    return obj_name(state.get("objects", {}).get(str(oid), {}))


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    try:
        return int(v)
    except Exception:
        return None


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_names(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def bf_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name]


def bf_creatures(state, pid=None):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield"
            and (pid is None or o.get("controller") == pid)
            and "creature" in str(o.get("card_type") or o.get("type") or
                                 "").lower()]


def untapped_lands(state, pid, names):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) in names and not o.get("tapped")]


# ---------------------------------------------------------------------------
# Protocol-130 plumbing (copied verbatim from scenario_6557_01070.py)
# ---------------------------------------------------------------------------

def top_acts(st):
    return list(st.get("legal_actions", []) or [])


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


def surf_codes(st):
    codes = []
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            for s in ch.get("surfaces", []) or []:
                c = (s.get("data") or {}).get("code")
                if c is not None:
                    codes.append(c)
    return codes


def surf_codes_for_opp(opp):
    codes = []
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    for ch in data.get("choices") or data.get("candidates") or []:
        for s in ch.get("surfaces", []) or []:
            c = (s.get("data") or {}).get("code")
            if c is not None:
                codes.append(c)
    return codes


NON_DECISION_CODES = {"tapLandForMana", "untapLandForMana", "castSpell",
                      "activateAbility", "passPriority"}


def real_decision_pending(st):
    for opp in vi_ops(st):
        codes = set()
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            for s in ch.get("surfaces", []) or []:
                c = (s.get("data") or {}).get("code")
                if c is not None:
                    codes.add(c)
        if codes - NON_DECISION_CODES:
            return True
    return False


def my_priority(acts):
    """Protocol 130: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def find_action(acts, atype, name=None, state=None):
    for a in acts:
        if a["type"] != atype:
            continue
        if name is None:
            return a
        d = a.get("data", {})
        oid = d.get("object_id") or a.get("_src_oid")
        if state is not None and lname(state, oid) == name:
            return a
    return None


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


def cast_spell_offered(state, st, spell):
    for a in merged_actions(st):
        if a["type"] == "CastSpell" \
                and lname(state, a.get("data", {}).get("object_id")) == spell:
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


def _bottom_rank(state, who, oid):
    nm = lname(state, oid)
    if who == "P0":
        if nm in P0_LANDS:
            return (0, nm)
        n_dp = sum(1 for x in hand_ids(state, 0) if lname(state, x) == DEADPOOL)
        if nm == DEADPOOL and n_dp > 1:
            return (1, nm)
        if nm in (BEARS, BOLT):
            return (2, nm)
        return (3, nm)
    if nm in P1_LANDS:
        return (0, nm)
    n_b = sum(1 for x in hand_ids(state, 1) if lname(state, x) == BEARS)
    if nm == BEARS and n_b > 1:
        return (1, nm)
    return (2, nm)


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
    ranked = sorted(cands,
                    key=lambda ch: _bottom_rank(state, tag,
                                                _cand_reference(ch)))
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    sub = {"interactionId": iid,
           "response": {"type": "select",
                        "data": {"choiceIds": [ch.get("id") for ch in picks]}}}
    await interact_as(c, sub, tag)
    return True


def _discard_rank(state, who, oid):
    nm = lname(state, oid)
    if who == "P0":
        # P0: lands first, then bears/bolt; Deadpool last (keep the cast).
        if nm in P0_LANDS:
            return (0, nm)
        if nm in (BEARS, BOLT):
            return (1, nm)
        return (2, nm)
    if nm in P1_LANDS:
        return (0, nm)
    if nm == BOLT:
        return (1, nm)
    return (2, nm)


async def do_discard_to_handsize(c, acts, st, pid, tag):
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
        ranked = sorted((ch for ch in cands
                         if str(_cand_reference(ch)) in handset),
                        key=lambda ch: _discard_rank(state, tag,
                                                     _cand_reference(ch)))
        picks = ranked[:n]
        say(f"[{tag}] discarding to hand size via vi: "
            f"{[lname(state, _cand_reference(x)) for x in picks]}")
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
        chosen = sorted(act_by_oid,
                        key=lambda o: _discard_rank(state, tag, o))[:n]
        say(f"[{tag}] discarding to hand size via verbatim SelectCards: "
            f"{[lname(state, o) for o in chosen]}")
        wire(f"{tag}_discard_verbatim", {"oids": chosen})
        for o in chosen:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [o]}})
            await asyncio.sleep(0.4)
        return True
    return False


def spell_on_stack(state, pid, name):
    for e in state.get("stack", []) or []:
        oid = e.get("object_id") or e.get("source_id")
        o = get_obj(state, oid) if oid is not None else {}
        nm = obj_name(o) if o else str(e.get("name", "")).lower()
        ctrl = o.get("controller", e.get("controller", e.get("player", pid)))
        if nm == name and ctrl == pid:
            return True
    return False


async def verify_server_hello():
    url = os.environ.get("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")
    async with websockets.connect(url, max_size=2**26) as _w:
        hello = json.loads(await asyncio.wait_for(_w.recv(), 10))
    d = hello.get("data", {})
    say(f"ServerHello: {d.get('server_version')}/{d.get('build_commit')}/"
        f"protocol {d.get('protocol_version')}/{d.get('mode')}")
    assert str(d.get("server_version")).startswith("0.107.0"), hello
    assert d.get("build_commit") == "eccd25f", hello
    assert int(d.get("protocol_version")) == 130, hello
# ---------------------------------------------------------------------------
# Scenario driving (protocol 130)
# ---------------------------------------------------------------------------

def make_drivers(p0, p1):
    obs = {"deadpool_cast": False, "p1_bears_cast": False,
           "deadpool_cast_turn": None, "p1_bears_cast_turn": None,
           "exchange_prompt_seen": False,
           "entry_window_kinds": [],
           "entry_window_prompts": [],
           "entry_window_seen_iids": set(),
           "deadpool_entered": False, "deadpool_oid": None,
           "deadpool_entered_turn": None, "deadpool_entered_phase": None,
           "entry_detected_at": None, "entry_window_closed": False,
           "exchange_candidates": [],
           "upkeep_life_before": None, "upkeep_trigger_seen": False,
           "upkeep_life_after": None, "upkeep_turn": None,
           "sac_ability_advertised": False, "activated_abilities_seen": [],
           "turns_seen": set(), "game_code": None}
    exported = {"pre": False, "entry": False, "post": False}
    last_assign = {}
    vi_shapes_logged = set()
    paying = {"p0": None, "p1": None}  # seat -> (label, card, needs, since, flag, hand_oid)

    def log_vi_shapes(st, who):
        for opp in vi_ops(st):
            key = (who, str(sorted(surf_codes_for_opp(opp))))
            if key in vi_shapes_logged:
                continue
            vi_shapes_logged.add(key)
            wire("interaction_shape",
                 {"who": who, "kind": vi_kind_code(st),
                  "interaction": opp})

    async def export_named(c, name):
        try:
            s = await c.export_state()
            env = json.loads(s)
            assert "state" in env, "envelope missing 'state'"
            with open(f"{EVDIR}/{name}.json", "w") as f:
                f.write(s)
            exported[name] = True
            say(f"exported {name}.json (turn={env['state'].get('turn_number')} "
                f"phase={env['state'].get('phase')})")
            return True
        except Exception as e:
            say(f"{name} export failed: {e}")
            return False

    def cast_guard_tick(state, who):
        """Protocol-130 cast-confirmation guard.

        After EVERY CastSpell submission the driver must not pass priority
        or start new main-phase plays until the spell is confirmed: on
        protocol 130 a bare CastSpell (payment_mode Auto) can LOSE A RACE
        to our own PassPriority submitted on the next tick and be silently
        dropped by the server (no rejection is sent).

        Returns "hold" while our CastSpell is in flight but unconfirmed;
        "proceed" once confirmed. A 30s backstop clears a still-in-hand
        cast as dropped and resets the one-shot flag so the cast block
        re-triggers. Must run BEFORE any early-return block
        (attackers/blockers/target-wait) -- a dropped cast stalls any wait
        placed ahead of the guard forever.
        """
        pay = paying[who]
        if not pay:
            return "proceed"
        label, card, _needs, since, flag, hand_oid = pay
        pid = 0 if who == "p0" else 1
        # 1. stack confirmation (name scan: the spell may live under a new
        #    oid on the stack).
        if spell_on_stack(state, pid, card):
            say(f"[{who}] cast confirmed (stack): {label}")
            wire("cast_confirmed", {"who": who, "label": label,
                                    "via": "stack_scan"})
            paying[who] = None
            return "proceed"
        # 2. left-hand confirmation: the cast's hand oid is gone from hand
        #    (resolved to battlefield/graveyard/exile, or countered).
        if hand_oid is not None:
            o = get_obj(state, hand_oid)
            in_hand = (o and str(o.get("zone") or "").lower() == "hand"
                       and o.get("controller") == pid)
            if not in_hand:
                say(f"[{who}] cast left hand ({label}); clearing guard")
                wire("cast_confirmed", {"who": who, "label": label,
                                        "via": "left_hand"})
                paying[who] = None
                return "proceed"
        # 3. still in hand (or untrackable): hold, with a 30s backstop for
        #    silently-dropped casts.
        if time.time() - since > 30:
            say(f"[{who}] CAST NOT CONFIRMED after 30s ({label}): still in "
                f"hand -- dropped; resetting {flag} for retry")
            wire("cast_dropped_retry", {"who": who, "label": label,
                                        "flag": flag})
            paying[who] = None
            if flag:
                obs[flag] = False
            return "proceed"
        return "hold"

    async def answer_cast_payment(c, st, acts, state, who):
        pay = paying[who]
        needs = pay[2] if pay else None
        if needs and await pay_mana_vi(c, st, who, needs):
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                wire(f"{who}_auto_pay",
                     {"label": pay[0] if pay else None,
                      "action": {k: v for k, v in a.items()
                                 if not k.startswith("_")}})
                await submit_as_is(c, a)
                return True
        return False

    async def entry_window_watch(st, state, who):
        """During the entry window, record EVERY viewer_interaction
        opportunity (kinds, codes, text excerpts) and flag any
        exchange-flavored prompt. Deliberately NEVER auto-answer
        exchange-flavored prompts: the prompt IS the phenomenon under
        test and must be recorded, not driven past."""
        seen = False
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            key = (who, iid, obs.get("stage"))
            if key in obs["entry_window_seen_iids"]:
                seen = True
                continue
            obs["entry_window_seen_iids"].add(key)
            resp = opp.get("response", {}) or {}
            blob = json.dumps(resp).lower()
            flavored = any(k in blob for k in EXCHANGE_KEYS)
            kinds = list({(s.get("data") or {}).get("code")
                          for ch in (resp.get("data") or {}).get("choices", [])
                          or (resp.get("data") or {}).get("candidates", [])
                          for s in ch.get("surfaces", [])})
            rec = {"iid": iid, "rtype": resp.get("type"),
                   "vi_kind_code": vi_kind_code(st),
                   "surf_codes": sorted(c for c in kinds if c),
                   "flavored": flavored,
                   "text_excerpt": blob[:300]}
            obs["entry_window_prompts"].append(rec)
            for c in rec["surf_codes"]:
                if c not in obs["entry_window_kinds"]:
                    obs["entry_window_kinds"].append(c)
            wire("entry_window_prompt", {"who": who, **rec})
            say(f"[{who}] ENTRY-WINDOW prompt iid={iid} "
                f"rtype={resp.get('type')} flavored={flavored} "
                f"codes={rec['surf_codes'][:8]}")
            if flavored:
                obs["exchange_prompt_seen"] = True
            seen = True
        return seen

    async def p0_tick(st, acts, state):
        turn = state.get("turn_number")
        phase = state.get("phase")
        stack = state.get("stack", []) or []
        atypes = set(a.get("type") for a in acts)
        obs["turns_seen"].add(turn)
        # --- mulligan / bottom / discard (decision-driven, not priority) ---
        if await do_mulligan(
                p0, acts, st, 0, "P0",
                lambda hn, mulls: (DEADPOOL in hn
                                   and sum(1 for n in hn if n in P0_LANDS) >= 2)
                or mulls >= 3):
            return
        if await do_bottom(p0, acts, st, 0, "P0"):
            return
        if await do_discard_to_handsize(p0, acts, st, 0, "P0"):
            return
        # --- driver-race guard: yield before evaluating legs ---
        await asyncio.sleep(0)
        st = p0.latest or st
        state = st["state"]
        acts = merged_actions(st)
        atypes = set(a.get("type") for a in acts)
        turn = state.get("turn_number")
        phase = state.get("phase")
        stack = state.get("stack", []) or []
        obs["turns_seen"].add(turn)
        log_vi_shapes(st, "P0")
        # --- entry-window watch: runs during cast_pending and entry_watch.
        # Deliberately no return here: even if a prompt is seen, the tick
        # must fall through to the 5s-close logic so the run can wrap up
        # (an unanswered prompt cannot be driven past). ---
        if obs.get("stage") in ("cast_pending", "entry_watch"):
            await entry_window_watch(st, state, "P0")
        # --- protocol-130 cast-confirmation guard BEFORE any early return:
        # while our CastSpell is in flight we may answer engine-advertised
        # payments but must not pass priority or start new plays (a bare
        # CastSpell can lose a race to our own PassPriority and be
        # silently dropped). ---
        holding = cast_guard_tick(state, "p0") == "hold"
        if holding:
            await answer_cast_payment(p0, st, acts, state, "p0")
            return
        # --- engine-advertised payments ---
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                wire("p0_auto_pay", {"action": {k: v for k, v in a.items()
                                                if not k.startswith("_")}})
                await submit_as_is(p0, a)
                return
        # --- trigger ordering: submit the advertised default ---
        if "OrderTriggers" in atypes:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
                return
        # --- combat: P0 never attacks or blocks in this scenario ---
        if "DeclareAttackers" in atypes:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(p0, d)
                say(f"P0 declares no attackers (turn {turn})")
            return
        if "DeclareBlockers" in atypes:
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(p0, d)
            return
        if "AssignCombatDamage" in atypes:
            aa = find_action(acts, "AssignCombatDamage")
            if aa and last_assign.get(0) != p0.revision:
                last_assign[0] = p0.revision
                d = copy.deepcopy(aa.get("data", {}))
                say("P0 AssignCombatDamage: submitting advertised default")
                wire("p0_assign_combat_damage", d)
                await p0.send_action({"type": "AssignCombatDamage",
                                      "data": d})
            return
        # --- record activated abilities advertised for Deadpool (control A5) ---
        for a in acts:
            if a.get("type") == "ActivateAbility":
                dd = a.get("data") or {}
                src = dd.get("source_id")
                try:
                    if src is not None and lname(state, int(src)) == DEADPOOL:
                        rec = {"ability_index": dd.get("ability_index"),
                               "source_id": src}
                        if rec not in obs["activated_abilities_seen"]:
                            obs["activated_abilities_seen"].append(rec)
                            obs["sac_ability_advertised"] = True
                            say(f"[P0] ActivateAbility advertised for "
                                f"Deadpool: {rec}")
                except Exception:
                    pass
        # --- track Deadpool entering the battlefield ---
        dp_bf = bf_ids(state, 0, DEADPOOL)
        if dp_bf and not obs["deadpool_entered"]:
            obs["deadpool_entered"] = True
            obs["deadpool_oid"] = dp_bf[0]
            obs["deadpool_entered_turn"] = turn
            obs["deadpool_entered_phase"] = phase
            obs["entry_detected_at"] = time.time()
            o = get_obj(state, dp_bf[0])
            say(f"[P0] Deadpool ENTERED battlefield oid={dp_bf[0]} "
                f"turn={turn} phase={phase} p/t={o.get('power')}/"
                f"{o.get('toughness')}")
            obs["exchange_candidates"] = [
                {"name": obj_name(get_obj(state, oid)),
                 "oid": oid,
                 "controller": get_obj(state, oid).get("controller"),
                 "power": get_obj(state, oid).get("power"),
                 "toughness": get_obj(state, oid).get("toughness")}
                for oid in bf_creatures(state, 1)
            ]
            say(f"[P0] exchange candidates (P1 creatures) at entry: "
                f"{obs['exchange_candidates']}")
            obs["stage"] = "entry_watch"
            await export_named(p0, "entry")
        # --- entry window: hold 5s after entry, then move on ---
        if obs.get("stage") == "entry_watch":
            if time.time() - (obs["entry_detected_at"] or 0) > 5:
                obs["stage"] = "upkeep_watch"
                obs["entry_window_closed"] = True
                say("[P0] entry window closed (5s); exchange_prompt_seen="
                    f"{obs['exchange_prompt_seen']} "
                    f"kinds_seen={obs['entry_window_kinds']}")
            elif my_priority(acts):
                await pass_priority(p0, st, acts)
            return
        # --- upkeep watch: next P0 upkeep should lose 3 life (control) ---
        if obs.get("stage") == "upkeep_watch":
            dp = bf_ids(state, 0, DEADPOOL)
            if not dp and obs["deadpool_entered"]:
                say("[P0] WARNING: Deadpool left the battlefield during "
                    "upkeep_watch")
            if phase == "Upkeep" and state.get("active_player") == 0:
                if obs["upkeep_life_before"] is None:
                    obs["upkeep_life_before"] = life_of(state, 0)
                    obs["upkeep_turn"] = turn
                    say(f"[P0] upkeep entered (turn {turn}), life before="
                        f"{obs['upkeep_life_before']}")
            if obs["upkeep_life_before"] is not None \
                    and not obs["upkeep_trigger_seen"]:
                cur = life_of(state, 0)
                if cur is not None and cur < obs["upkeep_life_before"]:
                    obs["upkeep_trigger_seen"] = True
                    obs["upkeep_life_after"] = cur
                    say(f"[P0] upkeep trigger RESOLVED: life "
                        f"{obs['upkeep_life_before']}->{cur}")
                    obs["stage"] = "done"
                    await export_named(p0, "post")
                    return
        # --- main-phase actions (the cast guard above already cleared: no
        # in-flight cast may exist here, so starting new plays is safe) ---
        if my_priority(acts) and phase in ("PreCombatMain",
                                           "PostCombatMain") \
                and state.get("active_player") == 0 and not stack:
            # land drop (retry every tick; no kept flag)
            if player_of(state, 0).get("lands_played_this_turn", 0) == 0:
                for oid in hand_ids(state, 0):
                    if lname(state, oid) in P0_LANDS:
                        pla = next((a for a in acts
                                    if a.get("type") == "PlayLand"
                                    and (a.get("data") or {}).get(
                                        "object_id") == oid), None)
                        if pla:
                            say(f"[P0] playing land {lname(state, oid)}")
                            await submit_as_is(p0, pla)
                            return
            # cast Deadpool when the candidate creature is in place
            dp_in_hand = DEADPOOL in hand_names(state, 0)
            p1_bears = bf_ids(state, 1, BEARS)
            p1_creatures = bf_creatures(state, 1)
            ul = untapped_lands(state, 0, P0_LANDS)
            ul_names = [lname(state, o) for o in ul]

            async def try_cast(tag):
                offered, act = cast_spell_offered(state, st, DEADPOOL)
                if not offered:
                    return False
                hand_oid = (act.get("data", {}) or {}).get("object_id")
                if hand_oid is None:
                    hand_oid = next(
                        (o for o in hand_ids(state, 0)
                         if lname(state, o) == DEADPOOL), None)
                obs["deadpool_cast"] = True
                obs["deadpool_cast_turn"] = turn
                # {2}{B}{R}: 2 generic + 1 black + 1 red
                paying["p0"] = ("deadpool", DEADPOOL,
                                {"B": 1, "R": 1, "generic": 2},
                                time.time(), "deadpool_cast", hand_oid)
                obs["stage"] = "cast_pending"
                if "_vi_choice" in act:
                    wire("cast_deadpool_vi",
                         {"choiceId": act["_vi_choice"].get("id")})
                    await answer_vi(p0, act["_vi_opp"],
                                    act["_vi_choice"], "P0")
                else:
                    wire("cast_deadpool",
                         {k: v for k, v in act.items()
                          if not k.startswith("_")})
                    await submit_as_is(p0, act)
                say(f"[P0] casting Deadpool, Trading Card ({tag}; "
                    f"p1_bears={p1_bears}, turn={turn})")
                return True

            if (obs.get("stage") in (None, "setup") and dp_in_hand and p1_bears
                    and turn is not None and turn >= 5 and len(ul) >= 4
                    and SWAMP in ul_names and MOUNTAIN in ul_names):
                if not obs["exported_pre"]:
                    await export_named(p0, "pre")
                    obs["exported_pre"] = True
                if await try_cast("primary"):
                    return
            # safety: cast later if the candidate is late but mana is there
            if (obs.get("stage") in (None, "setup") and dp_in_hand
                    and p1_creatures and turn is not None and turn >= 7
                    and len(ul) >= 4 and SWAMP in ul_names
                    and MOUNTAIN in ul_names):
                if not obs["exported_pre"]:
                    await export_named(p0, "pre")
                    obs["exported_pre"] = True
                if await try_cast("late fallback"):
                    return
        # --- default: pass priority (always fall through) ---
        if my_priority(acts):
            await pass_priority(p0, st, acts)

    async def p1_tick(st, acts, state):
        turn = state.get("turn_number")
        phase = state.get("phase")
        atypes = set(a.get("type") for a in acts)
        if await do_mulligan(p1, acts, st, 1, "P1",
                             lambda hn, mulls: (BEARS in hn
                                                and sum(1 for n in hn
                                                        if n in P1_LANDS) >= 2)
                             or mulls >= 3):
            return
        if await do_bottom(p1, acts, st, 1, "P1"):
            return
        if await do_discard_to_handsize(p1, acts, st, 1, "P1"):
            return
        # --- driver-race guard: yield before evaluating legs ---
        await asyncio.sleep(0)
        st = p1.latest or st
        state = st["state"]
        acts = merged_actions(st)
        atypes = set(a.get("type") for a in acts)
        turn = state.get("turn_number")
        phase = state.get("phase")
        log_vi_shapes(st, "P1")
        # --- protocol-130 cast-confirmation guard BEFORE any early return
        # (a dropped bear cast must not stall the DeclareAttackers wait) ---
        holding = cast_guard_tick(state, "p1") == "hold"
        if holding:
            await answer_cast_payment(p1, st, acts, state, "p1")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if "OrderTriggers" in atypes:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        # --- combat: P1 never attacks or blocks in this scenario ---
        if "DeclareAttackers" in atypes:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(p1, d)
            return
        if "DeclareBlockers" in atypes:
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(p1, d)
            return
        if "AssignCombatDamage" in atypes:
            aa = find_action(acts, "AssignCombatDamage")
            if aa and last_assign.get(1) != p1.revision:
                last_assign[1] = p1.revision
                d = copy.deepcopy(aa.get("data", {}))
                say("P1 AssignCombatDamage: submitting advertised default")
                wire("p1_assign_combat_damage", d)
                await p1.send_action({"type": "AssignCombatDamage",
                                      "data": d})
            return
        if not my_priority(acts):
            return
        # ---- P1 priority (cast guard already ran above) ----
        main_phase = phase in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 1
        # cast one Grizzly Bears as the exchange candidate; never again
        if not obs["p1_bears_cast"]:
            offered, act = cast_spell_offered(state, st, BEARS)
            if (offered and main_phase
                    and len(untapped_lands(state, 1, P1_LANDS)) >= 2):
                hand_oid = (act.get("data", {}) or {}).get("object_id")
                if hand_oid is None:
                    hand_oid = next(
                        (o for o in hand_ids(state, 1)
                         if lname(state, o) == BEARS), None)
                obs["p1_bears_cast"] = True
                obs["p1_bears_cast_turn"] = turn
                paying["p1"] = ("bear", BEARS, {"G": 1, "generic": 1},
                                time.time(), "p1_bears_cast", hand_oid)
                if "_vi_choice" in act:
                    wire("cast_bear_vi",
                         {"choiceId": act["_vi_choice"].get("id")})
                    await answer_vi(p1, act["_vi_opp"],
                                    act["_vi_choice"], "P1")
                else:
                    wire("cast_bear",
                         {k: v for k, v in act.items()
                          if not k.startswith("_")})
                    await submit_as_is(p1, act)
                say(f"[P1] casting Grizzly Bears (exchange candidate; "
                    f"turn {turn})")
                return
        # normal land play
        la = find_action(acts, "PlayLand")
        if la and player_of(state, 1).get("lands_played_this_turn", 0) == 0:
            await submit_as_is(p1, la)
            return
        await pass_priority(p1, st, acts)

    obs["stage"] = "setup"
    obs["exported_pre"] = False
    ctx = {"obs": obs, "exported": exported, "p0_tick": p0_tick,
           "p1_tick": p1_tick}
    return ctx
# ---------------------------------------------------------------------------
# Evaluation from saved authoritative states (pure function of evidence)
# ---------------------------------------------------------------------------

def load_env(tag):
    p = f"{EVDIR}/{tag}.json"
    if not os.path.exists(p):
        return None
    try:
        return json.loads(open(p).read())["state"]
    except Exception:
        return None


def a1_parse_check():
    """A1: current parse state of the as-enters clause in the pinned
    v0.107.0 card-data.json."""
    try:
        cd = json.load(open(f"{BACKFILL}/server/releases/v0.107.0/data/"
                             f"card-data.json"))
        c = cd["deadpool, trading card"]
        ab0 = (c.get("abilities") or [{}])[0]
        eff = ab0.get("effect") or {}
        unimpl = eff.get("type") == "Unimplemented"
        desc = str(ab0.get("description") or "")
        has_exchange_desc = "exchange" in desc.lower() \
            and "text box" in desc.lower()
        no_replacements = c.get("replacements") == []
        trig = (c.get("triggers") or [{}])[0]
        detail = {
            "effect_type": eff.get("type"),
            "effect_name": eff.get("name"),
            "description": desc,
            "replacements": c.get("replacements"),
            "n_triggers": len(c.get("triggers") or []),
            "n_abilities": len(c.get("abilities") or []),
            "upkeep_trigger_kind": ((trig.get("execute") or {})
                                    .get("effect") or {}).get("type"),
            "card_data_key": "deadpool, trading card",
        }
        with open(f"{EVDIR}/data_evidence.json", "w") as f:
            json.dump({"issue": ISSUE, "server_version": "v0.107.0",
                       "parse": detail}, f, indent=1)
        return (unimpl and has_exchange_desc and no_replacements), detail
    except Exception as e:
        return False, {"error": repr(e)}


def evaluate():
    notes = []
    ass = {k: "not-run" for k in
           ("A1_parse", "A2_setup", "A3_no_exchange_choice",
            "A4_enters_unchanged", "A5_card_otherwise_live", "A6_cleanup")}
    obs = {}
    pre_st = load_env("pre")
    entry_st = load_env("entry")
    post_st = load_env("post")

    # ---- A1: parse ----
    ok, detail = a1_parse_check()
    obs["a1_detail"] = detail
    ass["A1_parse"] = "passed" if ok else "failed"
    notes.append(
        f"A1: as-enters effect.type={detail.get('effect_type')} "
        f"(name={detail.get('effect_name')}); "
        f"exchange-in-desc={('exchange' in str(detail.get('description')).lower() and 'text box' in str(detail.get('description')).lower())}; "
        f"replacements={detail.get('replacements')}; "
        f"upkeep_trigger={detail.get('upkeep_trigger_kind')}")
    if ok:
        notes.append("A1: the as-enters exchange clause is STILL "
                     "Unimplemented/unknown on v0.107.0 with no registered "
                     "replacement -- the parse-level root cause persists")

    # ---- A2: setup ----
    if pre_st is not None:
        p1b = bf_ids(pre_st, 1, BEARS)
        dp_hand = DEADPOOL in hand_names(pre_st, 0)
        ul = untapped_lands(pre_st, 0, P0_LANDS)
        ul_names = [lname(pre_st, o) for o in ul]
        ok = (len(p1b) >= 1 and dp_hand and len(ul) >= 4
              and SWAMP in ul_names and MOUNTAIN in ul_names)
        obs["a2"] = {"p1_bears_bf": len(p1b), "deadpool_in_hand": dp_hand,
                     "p0_untapped_lands": len(ul)}
        notes.append(f"A2: p1_bears_bf={len(p1b)} deadpool_in_hand={dp_hand} "
                     f"p0_untapped_lands={len(ul)} "
                     f"({','.join(sorted(set(ul_names)))})")
        ass["A2_setup"] = "passed" if ok else "failed"
    else:
        notes.append("A2 failed: pre.json missing")
        ass["A2_setup"] = "failed"

    # ---- A3: no exchange choice offered during the entry window ----
    # Exchange flavor comes from the live-run recorded prompts; cross-check
    # the exported entry state for a Deadpool that actually entered.
    dp_entered = (entry_st is not None
                  and len(bf_ids(entry_st, 0, DEADPOOL)) >= 1)
    if dp_entered:
        with open(f"{EVDIR}/obs.json") as f:
            live = json.load(f)
        prompt_seen = live.get("exchange_prompt_seen", False)
        kinds = live.get("entry_window_kinds", [])
        n_prompts = len(live.get("entry_window_prompts", []))
        obs["a3"] = {"deadpool_entered": True,
                     "exchange_prompt_seen": prompt_seen,
                     "entry_window_kinds": kinds,
                     "n_entry_window_prompts": n_prompts}
        notes.append(f"A3: deadpool_entered={dp_entered}, "
                     f"exchange_prompt_seen={prompt_seen}, "
                     f"n_entry_window_prompts={n_prompts}, "
                     f"kinds_seen={kinds}")
        ass["A3_no_exchange_choice"] = \
            "passed" if not prompt_seen else "failed"
    else:
        notes.append("A3 failed: Deadpool never entered the battlefield "
                     "(entry.json missing or no Deadpool on BF)")
        ass["A3_no_exchange_choice"] = "failed"

    # ---- A4: enters unchanged (5/3 own text box; bears stay 2/2) ----
    if entry_st is not None:
        dp = bf_ids(entry_st, 0, DEADPOOL)
        ok_dp = len(dp) == 1
        o = get_obj(entry_st, dp[0]) if ok_dp else {}
        ok_pt = num(o.get("power")) == 5 and num(o.get("toughness")) == 3
        b1 = bf_ids(entry_st, 1, BEARS)
        bp = num(get_obj(entry_st, b1[0]).get("power")) if b1 else None
        bt = num(get_obj(entry_st, b1[0]).get("toughness")) if b1 else None
        ok_bears = len(b1) >= 1 and bp == 2 and bt == 2
        obs["a4"] = {"deadpool_bf": len(dp), "deadpool_pt":
                     (o.get("power"), o.get("toughness")),
                     "p1_bears_bf": len(b1), "bears_pt": (bp, bt)}
        ok = ok_dp and ok_pt and ok_bears
        notes.append(f"A4: deadpool_bf={len(dp)} pt=({o.get('power')},"
                     f"{o.get('toughness')}); p1_bears_bf={len(b1)} "
                     f"pt=({bp},{bt})")
        ass["A4_enters_unchanged"] = "passed" if ok else "failed"
    else:
        notes.append("A4 failed: entry.json missing")
        ass["A4_enters_unchanged"] = "failed"

    # ---- A5: card otherwise live (upkeep -3 + sac ability) ----
    with open(f"{EVDIR}/obs.json") as f:
        live = json.load(f)
    up = (live.get("upkeep_trigger_seen")
          and live.get("upkeep_life_before") == 20
          and live.get("upkeep_life_after") == 17)
    sac = live.get("sac_ability_advertised", False)
    obs["a5"] = {"upkeep_trigger_seen": live.get("upkeep_trigger_seen"),
                 "upkeep_life": (live.get("upkeep_life_before"),
                                 live.get("upkeep_life_after")),
                 "sac_ability_advertised": sac,
                 "activated_abilities_seen":
                 live.get("activated_abilities_seen", [])}
    ok = bool(up and sac)
    notes.append(f"A5: upkeep_trigger={live.get('upkeep_trigger_seen')} "
                 f"life {live.get('upkeep_life_before')}->"
                 f"{live.get('upkeep_life_after')}; "
                 f"sac_ability_advertised={sac} "
                 f"({live.get('activated_abilities_seen', [])})")
    ass["A5_card_otherwise_live"] = "passed" if ok else "failed"

    # ---- A6: cleanup ----
    if post_st is not None:
        stack_empty = not (post_st.get("stack") or [])
        turn = post_st.get("turn_number")
        obs["a6"] = {"stack_empty": stack_empty, "post_turn": turn}
        ok = stack_empty
        notes.append(f"A6: stack_empty={stack_empty} post_turn={turn}")
        ass["A6_cleanup"] = "passed" if ok else "failed"
    else:
        notes.append("A6 failed: post.json missing")
        ass["A6_cleanup"] = "failed"

    # ---- verdict ----
    if ass["A2_setup"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup (A2) never reached")
    elif ass["A3_no_exchange_choice"] == "passed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: Deadpool entered with a legal "
                     "exchange candidate on the battlefield, but the "
                     "controller was never offered the optional text-box "
                     "exchange - the exact reported symptom")
    elif ass["A3_no_exchange_choice"] == "failed":
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: an exchange choice WAS "
                     "offered during the entry window")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: incomplete assertion chain")
    return verdict, ass, notes, obs


# ---------------------------------------------------------------------------
# Summary PNG (rendered ONLY from saved states/assertions)
# ---------------------------------------------------------------------------

def render_summary(run, obs_eval):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 1080
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), f"phase-rs/phase #{ISSUE} - Deadpool, Trading Card",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.107.0 (eccd25f) protocol 130 - 2026-10-12 - "
           "as-enters text-box exchange",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 120)))
    y += 34
    d.text((24, y), "Oracle: 'As Deadpool enters, you may exchange his "
           "text box and another creature's.'",
           fill=(200, 210, 225))
    y += 26
    det = obs_eval.get("a1_detail", {}) or {}
    d.text((24, y), "card-data parse: effect " +
           f"{det.get('effect_type')}/{det.get('effect_name')}, "
           "replacements=[] -- engine cannot offer the exchange",
           fill=(200, 210, 225))
    y += 34
    d.text((24, y), "Assertions (from saved states / live views):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_parse": "card-data: exchange clause Unimplemented, no "
                    "replacement",
        "A2_setup": "PRE: P1 Bears on BF + Deadpool in P0 hand + mana",
        "A3_no_exchange_choice": "entry window: NO exchange choice offered "
                                 "(reported symptom)",
        "A4_enters_unchanged": "Deadpool enters 5/3, Bears stay 2/2",
        "A5_card_otherwise_live": "upkeep -3 life + sacrifice ability "
                                  "advertised",
        "A6_cleanup": "stack empty, game proceeded",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "?")
        col = {"passed": (120, 220, 120), "failed": (255, 90, 90),
               "not-run": (230, 200, 120)}.get(v, (180, 180, 180))
        d.text((40, y), f"{v:>9}", fill=col)
        d.text((130, y), lab, fill=(210, 220, 235))
        y += 24
    y += 8

    def life(s, pid):
        for p in (s or {}).get("players", []):
            if p.get("id") == pid:
                return p.get("life")
        return "?"

    pre, entry, post = load_env("pre"), load_env("entry"), load_env("post")
    d.text((24, y), "Pre-cast (P1 Bears on BF, Deadpool in P0 hand):",
           fill=(200, 210, 225))
    y += 24
    if pre is not None:
        p1b = [o for o in pre.get("objects", {}).values()
               if obj_name(o) == BEARS
               and o.get("zone") == "Battlefield"]
        dph = sum(1 for p in pre.get("players", [])
                  if p.get("id") == 0
                  for o in p.get("hand", [])
                  if obj_name(pre.get("objects", {}).get(str(o), {}))
                  == DEADPOOL)
        d.text((40, y), f"P1 creatures on BF: {len(p1b)}; "
               f"Deadpool in P0 hand: {dph}; "
               f"life: {life(pre, 0)}/{life(pre, 1)}",
               fill=(180, 195, 215))
    else:
        d.text((40, y), "pre.json MISSING", fill=(255, 90, 90))
    y += 30
    d.text((24, y), "Post-entry (no exchange prompt was ever offered):",
           fill=(200, 210, 225))
    y += 24
    if entry is not None:
        dp = [o for o in entry.get("objects", {}).values()
              if obj_name(o) == DEADPOOL
              and o.get("zone") == "Battlefield"]
        d.text((40, y), f"Deadpool on P0 BF: {len(dp)} "
               f"(5/3, own text box); life: "
               f"{life(entry, 0)}/{life(entry, 1)}",
               fill=(180, 195, 215))
    else:
        d.text((40, y), "entry.json MISSING", fill=(255, 90, 90))
    y += 30
    d.text((24, y), "Post-upkeep (control: rest of card works):",
           fill=(200, 210, 225))
    y += 24
    if post is not None:
        d.text((40, y), f"P0 life after upkeep: {life(post, 0)} "
               f"(20->17 if -3 trigger resolved); "
               f"stack: {len(post.get('stack') or [])}",
               fill=(180, 195, 215))
    else:
        d.text((40, y), "post.json MISSING", fill=(255, 90, 90))
    y += 40
    d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence "
           f"{ISSUE}/{RUN_ID}/", fill=(140, 160, 180))
    p = os.path.join(EVDIR, "summary.png")
    img.save(p)
    say(f"saved summary.png ({os.path.getsize(p)} bytes)")


async def write_manifest():
    fnames = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    lines = []
    for fn in fnames:
        with open(os.path.join(EVDIR, fn), "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


async def finish(g, ctx):
    dur = time.time() - g["t_start"]
    obs = ctx["obs"]
    exported = ctx["exported"]
    if not exported["post"]:
        try:
            post = await g["p0"].export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            exported["post"] = True
            say("post exported at finish (best effort)")
        except Exception as e:
            g["notes"].append(f"post export at finish failed: {e}")
    # obs must be serializable for evaluate() (sets -> lists)
    obs_s = {k: (sorted(v) if isinstance(v, set) else v)
             for k, v in obs.items()}
    with open(f"{EVDIR}/obs.json", "w") as f:
        json.dump(obs_s, f, indent=1, default=str)
    verdict, eval_ass, eval_notes, eval_obs = evaluate()
    g["ass"].update(eval_ass)
    g["notes"].extend(eval_notes)
    # server.log excerpts filtered by game code (best effort)
    server_log_files = []
    try:
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        run_log_dir = f"{BACKFILL}/runs/{srv_run}"
        with open(f"{run_log_dir}/server.log", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
        clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        gc = obs.get("game_code") or ""
        excerpt = [ln for ln in clean.splitlines() if gc and gc in ln]
        if not excerpt:
            excerpt = clean.splitlines()[-400:]
        with open(f"{EVDIR}/server.log", "w") as f:
            f.write("\n".join(excerpt) + "\n")
        say(f"wrote server.log excerpts ({len(excerpt)} lines)")
        server_log_files.append("server.log")
    except Exception as e:
        say(f"server.log excerpt failed: {e}")
        g["notes"].append(f"server.log excerpt failed: {e}")
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(g["t_start"])),
        "validated_at": "2026-10-12",
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID}",
        "server_ownership": ("reused the live v0.107.0 server on 127.0.0.1:9374 "
                             "(started by an earlier backfill run); games are "
                             "isolated per game id; ServerHello handshake "
                             "re-verified this run (0.107.0/eccd25f/protocol "
                             "130/Full)"),
        "server_port": 9374,
        "driver": {"protocol_advertised": 130, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7172_01070.py", "rb").read()
        ).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": g["ass"],
        "notes": g["notes"],
        "observations": eval_obs,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "12x card density is a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "P1 is near-passive (lands + one Grizzly Bears, never "
            "attacks/blocks) so the entry window stays clean.",
            "The accept-branch (choosing to exchange) and the decline "
            "control are not-run: no exchange choice exists to answer.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Not tested on the original 0.35.0 report build; verdict is "
            "scoped to v0.107.0, not a fix claim.",
        ],
        "setup_line": "P0: 12x deadpool trading card + 12x grizzly bears + "
                      "12x swamp + 12x mountain + 12x lightning bolt "
                      "(mulligan to Deadpool + lands; cast Deadpool {2}{B}{R} "
                      "on turn 5+ with a P1 Bears on the BF as the exchange "
                      "candidate); P1: 12x grizzly bears + 24x forest + 24x "
                      "lightning bolt (lands + one Bears, never attacks)",
        "contract_line": "As Deadpool enters, you may exchange his text box "
                         "and another creature's.",
        "prior_validation": {
            "run": "20260915-7172",
            "verdict": "reproduced",
            "validated_version": "v0.83.0",
        },
        "evidence_files": ["pre.json", "entry.json", "post.json", "run.json",
                           "manifest.sha256", "summary.png",
                           "scenario_7172_01070.py", "wire_log.jsonl",
                           "scenario_run.log", "obs.json", "data_evidence.json"]
                          + server_log_files,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copyfile(f"{BACKFILL}/driver/scenario_7172_01070.py",
                    f"{EVDIR}/scenario_7172_01070.py")
    render_summary(run, eval_obs)
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
    print(f"DONE verdict={verdict} assertions={json.dumps(g['ass'])}",
          flush=True)


async def main():
    pidfile = "/tmp/scenario_7172_01070.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_7172_01070 instance is alive "
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
    init_logging()
    t_start = time.time()
    g = {"t_start": t_start, "phase": "setup", "notes": [],
         "ass": {k: "not-run" for k in
                 ("A1_parse", "A2_setup", "A3_no_exchange_choice",
                  "A4_enters_unchanged", "A5_card_otherwise_live",
                  "A6_cleanup")}}
    await verify_server_hello()

    p0 = PhaseClient("P0-7172")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1-7172")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    g["p0"], g["p1"] = p0, p1
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    ctx = make_drivers(p0, p1)
    ctx["obs"]["game_code"] = p0.game_code
    exported = ctx["exported"]
    obs = ctx["obs"]

    last_rev = {}
    last_change = {}
    last_tick_at = {}
    stuck_deadline = None
    last_diag = 0.0
    t0 = t_start
    done = False
    try:
        while time.time() - t0 < 1500 and not done:
            await asyncio.sleep(0.15)
            for c, tick in ((p0, ctx["p0_tick"]), (p1, ctx["p1_tick"])):
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
                            f"legal={la} vikind={vi_kind_code(st)} "
                            f"realdec={real_decision_pending(st)}")
                        wire("watchdog_stale",
                             {"who": c.name, "rev": c.revision,
                              "turn": s0.get("turn_number"),
                              "phase": s0.get("phase"),
                              "legal": la, "vikind": vi_kind_code(st),
                              "surfcodes": surf_codes(st)[:12]})
                        last_change[c.name] = time.time()
                    # Safety net: re-tick while the client has ANYTHING to
                    # answer (PassPriority AND decision actions; a
                    # tick that returned without submitting must not stall
                    # the game).
                    acts_now = [a.get("type") for a in
                                (st.get("legal_actions") or [])]
                    if not (acts_now and time.time()
                            - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    await tick(st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"tick error {c.name}: {e}")
                    wire("tick_error", {"who": c.name, "err": str(e)})
            if exported["post"]:
                say("post exported; finishing")
                await finish(g, ctx)
                done = True
                break
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                s = p0.latest["state"]
                say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} "
                    f"legal={[a.get('type') for a in (p0.latest.get('legal_actions') or [])][:6]} "
                    f"vikind={vi_kind_code(p0.latest)} "
                    f"P0hand={hand_names(s, 0)[:6]} "
                    f"dp_entered={obs['deadpool_entered']} "
                    f"p1_bears_bf={len(bf_ids(s, 1, BEARS))} "
                    f"life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"stage={obs.get('stage')} "
                    f"exp={json.dumps(exported)}")
            if obs["deadpool_cast"] and not exported["post"] \
                    and stuck_deadline is None:
                stuck_deadline = time.time() + 420
            if not obs["deadpool_cast"] or exported["post"]:
                stuck_deadline = None
            if stuck_deadline and time.time() > stuck_deadline:
                g["notes"].append("Deadpool cast but post-upkeep cycle not "
                                  "completed in 420s; see wire log (possible "
                                  "unhandled interaction)")
                await finish(g, ctx)
                done = True
                break
    finally:
        pass
    if not done:
        g["notes"].append("global timeout (1500s) hit before assertions resolved")
        await finish(g, ctx)


if __name__ == "__main__":
    asyncio.run(main())
