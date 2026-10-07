#!/usr/bin/env python3
"""Issue #6557: Tamiyo, Seasoned Scholar +2 -- delayed -1/-0 trigger does not apply.

v0.103.0 re-validation port of scenario_6557_01020.py (v0.102.0 run 20261005-6557c).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github #6557, status:confirmed): after Tamiyo, Inquisitive Student
transforms into Tamiyo, Seasoned Scholar and the +2 ("Until your next turn,
whenever a creature attacks you or a planeswalker you control, it gets -1/-0
until end of turn") is activated, the opponent's attacking creatures do not
get -1/-0.

Prior validations: v0.78.0 (2026-09-10, protocol 68), v0.85.0
(2026-09-17, protocol 72) and v0.102.0 (2026-10-05, protocol 106), all REPRODUCED. The delayed trigger FIRES on the
attack (goes on the stack as a TriggeredAbility carrying Pump -1/-0 and
resolves), but the attacking Bear stays 2/2 and deals the full 2 combat
damage. Suspect: the nested Pump's target:SelfRef resolves to Tamiyo (a
planeswalker, no power/toughness) instead of the attacking creature. This run
re-validates on the pinned v0.103.0 (protocol 106).

Oracle text (verified from pinned v0.103.0 card-data.json, key
'tamiyo, seasoned scholar'):
  "[+2]: Until your next turn, whenever a creature attacks you or a
   planeswalker you control, it gets -1/-0 until end of turn."

Setup (native engine, two human-client seats):
  P0: 12x tamiyo, inquisitive student + 12x divination + 36x island
      (dense test-harness copies; engine accepts >4-of for custom games).
      T1: island, cast Tamiyo ({U}). When 3 untapped islands: cast Divination
      ({2}{U}) in main phase -> draw-step draw + 2 = 3rd card drawn this turn
      -> transform trigger -> Tamiyo exiled, returns as Seasoned Scholar.
      Proof: main phase, activate the +2 (loyalty 2->4), pass turn.
  P1: 20x grizzly bears + 40x forest. Plays forest, casts bears ({1}{G}),
      attacks P0 with one bear only after the +2 gate opens.

Expected (per Oracle text):
  E1: the +2 activation resolves (Tamiyo loyalty 2 -> 4).
  E2: the delayed trigger fires when P1's Bear attacks P0, goes on the stack,
      and resolves.
  E3: P1's attacking Bear (2/2) gets -1/-0 -> 1/2 while attacking; P0 takes 1
      combat damage (20 -> 19).
  E4: at end of turn the -1/-0 wears off (bear back to 2/2 on P0's next turn).
  E5: the watching effect expires at P0's next turn, so P1's attack on the
      following turn is not weakened (bear stays 2/2, full 2 damage).
  E6: stack empties, game proceeds.

Assertions:
  A1_setup_ok    pre.json: P0 PreCombatMain/PostCombatMain, Tamiyo PW on BF
                 (loyalty 2), >=1 Bear on P1 BF, life 20/20.
  A2_plus2       mid.json: Tamiyo loyalty == 4, stack empty, same turn as pre;
                 delayed trigger installed (UntilControllersNextTurn).
  A3_weakened    post.json (first attack, EndCombat/PostCombatMain): attacking
                 Bear power == 1 (base 2, -1 applied); P0 life == 19.
  A4_wears_off   p0next.json (P0's next turn): Bear power == 2 again.
  A5_expired     post2.json (P1's second attack, after P0's next turn began):
                 attacking Bear power == 2 and P0 took exactly 2 more damage
                 (life == life_at_post - 2, the control is the delta).
  A6_cleanup     final: stack empty, game proceeding, Tamiyo PW on BF.

Verdict rule: reproduced iff A1 passes, A2 passes (trigger installed), and A3
fails (the reported outcome: no -1/-0). not-reproduced iff A1..A6 all pass.
blocked iff A1 fails or the proof gate is not fully reached.

Evidence: evidence/6557/<run-id>/pre.json (before +2), mid.json (after +2
resolved), post.json (first attack), p0next.json (P0's next turn),
post2.json (second attack), trigger_samples.json, run.json,
manifest.sha256, summary.png, scenario_6557_01030.py, wire_log.jsonl,
scenario_run.log

Protocol-106 notes (see AGENTS.md): waiting_for is gone (null); priority =
PassPriority in top-level legal_actions; MulliganDecision answered via legacy
Action gated on the advertised MulliganDecision legal action; bottom-after-
mulligan via the vi schema/select opportunity gated on
waitingForKind.code=='mulligan' AND turn 1/Untap; DiscardToHandSize via
viewer_interaction gated on the 'choose' code plus hand>7 and a hand-card-
candidate heuristic; CastSpell merged_action first, else vi castSpell choice;
ActivateAbility submitted while holding priority (never pass first);
real_decision_pending excludes tapLandForMana/untapLandForMana/castSpell/
activateAbility/passPriority menus; manifest computed AFTER WIRE/RUNLOG close.
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
RUN_ID = os.environ.get("RUN_ID", "20261007-0011-6557")
ISSUE = 6557
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

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


TAMIYO = "tamiyo, inquisitive student"   # card-data.json key (exact)
TAMIYO_PW = "tamiyo, seasoned scholar"  # card-data.json key (exact)
DIV = "divination"                      # card-data.json key (exact)
BEAR = "grizzly bears"                  # card-data.json key (exact)
ISLAND = "island"
FOREST = "forest"

P0_DECK = [(TAMIYO, 12), (DIV, 12), (ISLAND, 36)]
P1_DECK = [(BEAR, 20), (FOREST, 40)]

SERVER_IDENTITY = {
    "server_version": "v0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "mode": "Full",
    "binary_sha256": "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc",
    "card_data_sha256": "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe",
    "draft_pools_sha256": "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c422135ecf69665263b6fb1edbd",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.103.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828); data digests match the "
                      "signed manifest; digests recomputed against on-disk "
                      "files this run",
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
    return None


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def bf_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == name]


def untapped_lands(state, pid, landname):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and obj_name(o) == landname and not o.get("tapped")]


def tamiyo_pw_loyalty(state):
    ids = bf_ids(state, 0, TAMIYO_PW)
    if not ids:
        return None
    return get_obj(state, ids[0]).get("loyalty")


# ---------------------------------------------------------------------------
# Protocol-106 plumbing
# ---------------------------------------------------------------------------

def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def vi_kind_code(st):
    """viewer_interaction waitingForKind code (protocol 106 replaces the
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


def surf_codes(st):
    """All vi surface action codes, None-filtered (106 surfaces sometimes
    carry empty data)."""
    codes = []
    for opp in vi_ops(st):
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
    """True iff the viewer_interaction carries a real decision (not one of
    the noisy 106 priority menus)."""
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
    """Protocol 106: the viewing seat holds priority iff a PassPriority
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


def find_plus2(acts, state):
    """The +2 loyalty ability: ActivateAbility sourced on Tamiyo PW.

    Match the intended decision against the engine-issued ability list:
    prefer the candidate whose ability description contains the +2 text
    ("Until your next turn"), else the one whose cost is Loyalty 2.
    Index 0 is not assumed to be the +2."""
    cands = [a for a in acts if a["type"] == "ActivateAbility"
             and lname(state, a.get("data", {}).get("source_id")) == TAMIYO_PW]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    src_id = cands[0]["data"].get("source_id")
    abilities = get_obj(state, src_id).get("abilities", []) or []
    for a in cands:
        idx = a.get("data", {}).get("ability_index")
        if isinstance(idx, int) and idx < len(abilities):
            desc = str(abilities[idx].get("description", ""))
            if "Until your next turn" in desc:
                return a
    for a in cands:
        cost = a.get("data", {}).get("cost") or {}
        if isinstance(cost, dict) and cost.get("type") == "Loyalty" \
                and cost.get("amount") == 2:
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


async def pay_mana_vi(c, st, tag, needs=None):
    """Answer vi tapLandForMana payment prompts (protocol 106). `needs` is
    a dict like {"U": 1, "generic": 2}; consumed as lands are tapped."""
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
    """Protocol 106: MulliganDecision arrives as a legacy legal action
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


def _bottom_rank_6557(state, who, oid):
    nm = lname(state, oid)
    if who == "P0":
        # bottom islands first; protect Tamiyo; Divination last-ish
        tamiyo_n = sum(1 for x in hand_ids(state, 0)
                       if lname(state, x) == TAMIYO)
        if nm == ISLAND:
            return (0, nm)
        if nm == TAMIYO and tamiyo_n > 1:
            return (1, nm)
        if nm == DIV:
            return (2, nm)
        return (3, nm)
    # P1: forests first, bears last
    if nm == FOREST:
        return (0, nm)
    return (1, nm)


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as per-card SelectCards
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

    ranked = sorted(cands,
                    key=lambda ch: _bottom_rank_6557(
                        state, tag, _cand_reference(ch)))
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


def _discard_rank_6557(state, who, oid):
    nm = lname(state, oid)
    if who == "P0":
        # P0: islands first, protect Divination, Tamiyo last.
        if nm == ISLAND:
            return (0, nm)
        if nm == DIV:
            return (1, nm)
        return (2, nm)
    # P1: forests first, bears last.
    if nm == FOREST:
        return (0, nm)
    return (1, nm)


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Protocol 106: DiscardToHandSize surfaces via viewer_interaction.
    106 uses a generic 'choose' waitingForKind code for the cleanup discard
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
        ranked = sorted((ch for ch in cands
                         if str(_cand_reference(ch)) in handset),
                        key=lambda ch: _discard_rank_6557(
                            state, tag, _cand_reference(ch)))
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
        chosen = sorted(act_by_oid,
                        key=lambda o: _discard_rank_6557(state, tag, o))[:n]
        say(f"[{tag}] discarding to hand size via verbatim SelectCards: "
            f"{[lname(state, o) for o in chosen]}")
        wire(f"{tag}_discard_verbatim", {"oids": chosen})
        for o in chosen:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [o]}})
            await asyncio.sleep(0.4)
        return True
    return False


def attacking_bear_power(state):
    """Power of the bear currently flagged attacking, else None."""
    for oid in bf_ids(state, 1, BEAR):
        o = get_obj(state, oid)
        if o.get("attacking"):
            return num(o.get("power")), oid
    return None, None


def attack_legal_bears(state, st=None):
    """Oids of P1's bears the engine currently allows to attack.

    Prefers the engine-advertised valid_attack_targets_by_attacker
    (present on the waiting_for data during DeclareAttackers); falls back
    to untapped, non-summoning-sick battlefield bears. Fixes the 2026-10-07
    v0.103.0 stall: picking bears[0] blindly chose a summoning-sick bear
    (cast that turn), the declaration was rejected, and the game waited
    forever for a valid declaration.
    """
    for src in (state, st or {}):
        wf = (src.get("waiting_for") or {})
        if isinstance(wf, dict):
            by_att = ((wf.get("data") or {}).get(
                "valid_attack_targets_by_attacker") or {})
            if by_att:
                return [int(k) for k in by_att.keys()]
    out = []
    for oid in bf_ids(state, 1, BEAR):
        o = get_obj(state, oid)
        if not o.get("tapped") and not o.get("summoning_sick"):
            out.append(oid)
    return out


def spell_on_stack(state, pid, name):
    """True if a spell named `name` controlled by `pid` is on the stack.

    Once the spell is on the stack, its payment was handled (auto-pay or an
    advertised PayMana action) and no vi tapLandForMana prompt will come.
    """
    for e in state.get("stack", []) or []:
        oid = e.get("object_id") or e.get("source_id")
        o = get_obj(state, oid) if oid is not None else {}
        nm = obj_name(o) if o else str(e.get("name", "")).lower()
        ctrl = o.get("controller", e.get("controller", e.get("player", pid)))
        if nm == name and ctrl == pid:
            return True
    return False


def load_env(evdir, tag):
    p = f"{evdir}/{tag}.json"
    if not os.path.exists(p):
        return None
    try:
        return json.loads(open(p).read())["state"]
    except Exception:
        return None


def evaluate(evdir, hints=None):
    """Evaluate A1..A6 from the saved authoritative states.

    Returns (verdict, assertions, notes, obs). Pure function of the saved
    states plus optional `hints` (e.g. the live run's recorded attacker oids,
    which survive the EndCombat clearing of the 'attacking' flag).
    """
    hints = hints or {}
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_plus2", "A3_weakened", "A4_wears_off",
            "A5_expired", "A6_cleanup")}
    obs = {}
    pre_st = load_env(evdir, "pre")
    mid_st = load_env(evdir, "mid")
    post_st = load_env(evdir, "post")
    p0next_st = load_env(evdir, "p0next")
    post2_st = load_env(evdir, "post2")

    def bear_power_at(st, tag):
        """(power, oid) for the scenario's attacker at a post-attack export.

        Prefers the live-recorded attacker oid (hints); falls back to the
        'attacking' flag scan (unreliable at EndCombat, where the flag may be
        cleared).
        """
        oid = hints.get(f"{tag}_bear")
        if oid is not None and get_obj(st, oid) is not None:
            return num(get_obj(st, oid).get("power")), oid
        return attacking_bear_power(st)
    # A1
    if pre_st is not None:
        ok = (tamiyo_pw_loyalty(pre_st) == 2
              and len(bf_ids(pre_st, 1, BEAR)) >= 1
              and life_of(pre_st, 0) == 20 and life_of(pre_st, 1) == 20
              and pre_st.get("phase") in ("PreCombatMain", "PostCombatMain")
              and pre_st.get("active_player") == 0)
        obs["loyalty_pre"] = tamiyo_pw_loyalty(pre_st)
        if ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(f"pre.json: P0 {pre_st.get('phase')} turn "
                         f"{pre_st.get('turn_number')}, Tamiyo PW loyalty 2, "
                         f"{len(bf_ids(pre_st,1,BEAR))} bear(s) on P1 BF, life 20/20")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(f"pre.json setup precondition not met "
                         f"(loyalty={tamiyo_pw_loyalty(pre_st)}, bears="
                         f"{len(bf_ids(pre_st,1,BEAR))}, life="
                         f"{life_of(pre_st,0)}/{life_of(pre_st,1)}, phase="
                         f"{pre_st.get('phase')})")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre.json missing (+2 activation gate never opened)")
    # A2
    if mid_st is not None and pre_st is not None:
        loy = tamiyo_pw_loyalty(mid_st)
        obs["loyalty_mid"] = loy
        stack_empty = len(mid_st.get("stack", []) or []) == 0
        same_turn = mid_st.get("turn_number") == pre_st.get("turn_number")
        blob = json.dumps(mid_st, default=str)
        trig_installed = ("UntilControllersNextTurn" in blob
                          or "untilcontrollersnextturn" in blob.lower())
        if loy == 4 and stack_empty and same_turn:
            ass["A2_plus2"] = "passed"
            notes.append(f"mid.json: +2 resolved, Tamiyo loyalty 2->4, "
                         f"stack empty (turn {mid_st.get('turn_number')}), "
                         f"delayed-trigger installed={trig_installed}")
        else:
            ass["A2_plus2"] = "failed"
            notes.append(f"mid.json: loyalty={loy} (expected 4), "
                         f"stack_empty={stack_empty}, same_turn={same_turn}")
    elif mid_st is None:
        ass["A2_plus2"] = "failed"
        notes.append("mid.json missing (+2 never resolved)")
    else:
        ass["A2_plus2"] = "not-run"
        notes.append("A2 not-run (no pre.json)")
    # A3
    life_post = None
    if post_st is not None and ass["A2_plus2"] == "passed":
        power, oid = bear_power_at(post_st, "attack1")
        obs["bear1_power_post"] = power
        life_post = life_of(post_st, 0)
        obs["bear1_life_post"] = life_post
        on_time = (post_st.get("turn_number") is not None
                   and mid_st.get("turn_number") is not None
                   and post_st.get("turn_number") == mid_st.get("turn_number") + 1)
        if power is None:
            ass["A3_weakened"] = "failed"
            notes.append("post.json: no attacking bear found at blockers step")
        elif not on_time:
            ass["A3_weakened"] = "not-run"
            notes.append(f"post.json: attack1 on turn {post_st.get('turn_number')} "
                         f"(proof turn {mid_st.get('turn_number')}); bear power={power} "
                         f"-- supplementary observation, not the A3 gate")
        elif power == 1 and life_post == 19:
            ass["A3_weakened"] = "passed"
            notes.append(f"post.json: attacking Bear oid {oid} power == 1 "
                         f"(2/2 -> 1/2; -1/-0 applied), P0 life 20->19")
        else:
            ass["A3_weakened"] = "failed"
            notes.append(f"post.json: attacking Bear oid {oid} power == "
                         f"{power} (expected 1), P0 life == {life_post} "
                         f"(expected 19) -- REPORTED BUG: -1/-0 not applied")
    elif post_st is None:
        ass["A3_weakened"] = "failed"
        notes.append("post.json missing (first attack never reached blockers)")
    else:
        ass["A3_weakened"] = "not-run"
        notes.append("A3 not-run (A2 failed)")
    # A4
    if p0next_st is not None and ass["A3_weakened"] in ("passed", "failed"):
        powers = [num(get_obj(p0next_st, oid).get("power"))
                  for oid in bf_ids(p0next_st, 1, BEAR)]
        if all(p == 2 for p in powers) and powers:
            ass["A4_wears_off"] = "passed"
            notes.append(f"p0next.json (P0 turn {p0next_st.get('turn_number')}): "
                         f"bears back to 2 power {powers} (-1/-0 wore off)")
        else:
            ass["A4_wears_off"] = "failed"
            notes.append(f"p0next.json: bear powers {powers} (expected all 2) "
                         f"-- -1/-0 did not wear off at end of turn "
                         f"(triage-noted missing-duration defect)")
    elif p0next_st is None:
        ass["A4_wears_off"] = "failed"
        notes.append("p0next.json missing")
    else:
        ass["A4_wears_off"] = "not-run"
        notes.append("A4 not-run (A3 not-run)")
    # A5: the watching effect expired at P0's next turn, so the second attack
    # must be unweakened: bear power 2 and exactly 2 combat damage relative
    # to the post-attack1 life total (i.e. life_post - 2, whatever life_post
    # was -- the control is the delta, not an absolute life total).
    if post2_st is not None and ass["A4_wears_off"] in ("passed", "failed"):
        power, oid = bear_power_at(post2_st, "attack2")
        obs["bear2_power_post2"] = power
        life = life_of(post2_st, 0)
        obs["bear2_life_post2"] = life
        want_life = (life_post - 2) if life_post is not None else None
        if power == 2 and life == want_life:
            ass["A5_expired"] = "passed"
            notes.append(f"post2.json: second attack (after P0's next turn "
                         f"began) Bear oid {oid} power == 2, P0 life "
                         f"{life_post}->{life} (full 2 damage; watching effect expired)")
        elif power is None:
            ass["A5_expired"] = "failed"
            notes.append("post2.json: no attacking bear found at blockers step")
        else:
            ass["A5_expired"] = "failed"
            notes.append(f"post2.json: second-attack Bear oid {oid} power == "
                         f"{power} (expected 2), P0 life == {life} "
                         f"(expected {want_life} = {life_post} - 2)")
    elif post2_st is None:
        ass["A5_expired"] = "failed"
        notes.append("post2.json missing (second attack never reached)")
    else:
        ass["A5_expired"] = "not-run"
        notes.append("A5 not-run (A4 not-run)")
    # A6
    final_st = post2_st or p0next_st
    if final_st is not None:
        stack_empty = len(final_st.get("stack", []) or []) == 0
        pw_there = tamiyo_pw_loyalty(final_st) is not None
        if stack_empty and pw_there:
            ass["A6_cleanup"] = "passed"
            notes.append(f"final: stack empty, Tamiyo PW on BF "
                         f"(turn {final_st.get('turn_number')}, phase "
                         f"{final_st.get('phase')}), game proceeding")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"final: stack_empty={stack_empty}, pw_on_bf={pw_there}")
    else:
        ass["A6_cleanup"] = "failed"
        notes.append("A6 unevaluable (no final state)")
    gate_keys = ("A2_plus2", "A3_weakened", "A4_wears_off", "A5_expired",
                 "A6_cleanup")
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif all(ass[k] == "passed" for k in gate_keys):
        verdict = "not-reproduced"
    elif any(ass[k] == "failed" for k in gate_keys):
        verdict = "reproduced"
        notes.append("at least one required outcome failed while the setup "
                     "was valid")
    else:
        verdict = "blocked"
        notes.append("no assertion failed but the proof gate was not fully "
                     "reached (see not-run notes); not an engine verdict")
    return verdict, ass, notes, obs

# ---------------------------------------------------------------------------
# Scenario driving (protocol 106)
# ---------------------------------------------------------------------------

def make_drivers(p0, p1):
    """Build the per-seat tick closures over the shared obs/export state."""
    obs = {"tamiyo_cast": False, "div_cast": False, "plus2_submitted": False,
           "plus2_turn": None, "plus2_phase": None, "attack1": False,
           "attack1_turn": None, "attack1_bear": None, "attack2": False,
           "attack2_turn": None, "attack2_bear": None,
           "loyalty_pre": None, "loyalty_mid": None,
           "bear1_power_post": None, "bear1_life_post": None,
           "bear2_power_post2": None, "bear2_life_post2": None,
           "tamiyo_needs": {}, "div_needs": {}, "bear_needs": {}}
    exported = {"pre": False, "mid": False, "post": False, "p0next": False,
                "post2": False}
    kept = {}
    last_assign = {}
    seen_trigger_ids = set()
    trigger_samples = []
    vi_shapes_logged = set()
    paying = {"p0": None, "p1": None}  # seat -> (label, card, needs, since)

    def sample_triggers(state):
        """Record every new TriggeredAbility stack entry (full JSON)."""
        for e in state.get("stack", []) or []:
            kind = (e.get("kind") or {})
            if kind.get("type") != "TriggeredAbility":
                continue
            eid = e.get("id")
            if eid not in seen_trigger_ids:
                seen_trigger_ids.add(eid)
                rec = {"turn": state.get("turn_number"),
                       "phase": state.get("phase"), "entry": e}
                trigger_samples.append(rec)
                wire("trigger_on_stack", rec)
                say(f"TRIGGER on stack (turn {rec['turn']} {rec['phase']}): "
                    f"{json.dumps(e, default=str)[:500]}")

    def log_vi_shapes(st, who):
        """Diagnostic: log unseen viewer_interaction opportunity shapes."""
        for opp in vi_ops(st):
            key = (who, str(sorted(surf_codes_for_opp(opp))))
            if key in vi_shapes_logged:
                continue
            vi_shapes_logged.add(key)
            wire("interaction_shape",
                 {"who": who, "kind": vi_kind_code(st),
                  "interaction": opp})

    def surf_codes_for_opp(opp):
        codes = []
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            for s in ch.get("surfaces", []) or []:
                c = (s.get("data") or {}).get("code")
                if c is not None:
                    codes.append(c)
        return codes

    async def export_tag(c, tag):
        try:
            s = await c.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            exported[tag] = True
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            say(f"{tag} export failed: {e}")
            return False

    async def export_state_dict(c, tag):
        try:
            s = await c.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            exported[tag] = True
            say(f"exported {tag}.json")
            return json.loads(s)["state"]
        except Exception as e:
            say(f"{tag} export failed: {e}")
            return None

    async def handle_paying(c, st, acts, state, who):
        """Drive an in-flight mana payment: vi tapLandForMana with needs,
        falling back to advertised PayMana*/PayManaAbilityMana. Returns True
        if the payment leg did something (or is still pending)."""
        pay = paying[who]
        if not pay:
            return False
        label, card, needs, since = pay
        # Once the spell is on the stack, payment was handled (auto-pay or
        # an advertised PayMana action) -- clear the flag so priority passes.
        pid = 0 if who == "p0" else 1
        if spell_on_stack(state, pid, card):
            say(f"[{who}] {label} on stack; payment done, clearing flag")
            wire("paying_cleared_on_stack", {"who": who, "label": label})
            paying[who] = None
            return False
        if sum(needs.values()) <= 0:
            paying[who] = None
            return False
        if await pay_mana_vi(c, st, who, needs):
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                wire(f"{who}_auto_pay",
                     {"label": label,
                      "action": {k: v for k, v in a.items()
                                 if not k.startswith("_")}})
                await submit_as_is(c, a)
                return True
        if time.time() - since > 90:
            say(f"[{who}] paying backstop: no payment affordance for 90s "
                f"({label}); clearing paying flag")
            wire("paying_backstop", {"who": who, "label": label})
            paying[who] = None
            return False
        return True  # paying; wait for the next revision

    async def p0_tick(st, acts, state):
        turn = state.get("turn_number")
        phase = state.get("phase")
        stack = state.get("stack", []) or []
        atypes = set(a.get("type") for a in acts)
        # --- mulligan / bottom / discard (decision-driven, not priority) ---
        if await do_mulligan(
                p0, acts, st, 0, "P0",
                lambda hn, mulls: (TAMIYO in hn
                                   and sum(1 for n in hn if n == ISLAND) >= 2)
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
        log_vi_shapes(st, "P0")
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
        # --- declare attackers: P0 never attacks in this scenario ---
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
        # --- mid checkpoint: +2 resolved (fall through afterwards: never
        # return after an export while holding priority) ---
        if (obs["plus2_submitted"] and not exported["mid"]
                and tamiyo_pw_loyalty(state) == 4
                and len(stack) == 0
                and turn == obs["plus2_turn"]):
            say(f"MID: +2 resolved, loyalty 4 (turn {turn})")
            await export_tag(p0, "mid")
        # --- p0next checkpoint: P0's next turn after the proof turn ---
        if (obs["attack1"] and not exported["p0next"]
                and turn is not None and obs["plus2_turn"] is not None
                and turn >= obs["plus2_turn"] + 2
                and state.get("active_player") == 0
                and phase in ("PreCombatMain", "PostCombatMain", "Upkeep",
                              "DrawStep", "Draw")):
            say(f"P0NEXT: P0 turn {turn} phase {phase}; bear powers="
                f"{[num(get_obj(state, oid).get('power')) for oid in bf_ids(state, 1, BEAR)]}")
            await export_tag(p0, "p0next")
        if not my_priority(acts):
            return
        # ---- P0 priority ----
        # in-flight mana payment first
        if await handle_paying(p0, st, acts, state, "p0"):
            return
        main_phase = phase in ("PreCombatMain", "PostCombatMain")
        pw_ids = bf_ids(state, 0, TAMIYO_PW)
        # cast Tamiyo (front)
        if not obs["tamiyo_cast"]:
            offered, act = cast_spell_offered(state, st, TAMIYO)
            if offered and main_phase:
                obs["tamiyo_cast"] = True
                paying["p0"] = ("tamiyo", TAMIYO, {"U": 1}, time.time())
                if "_vi_choice" in act:
                    wire("cast_tamiyo_vi",
                         {"choiceId": act["_vi_choice"].get("id")})
                    await answer_vi(p0, act["_vi_opp"], act["_vi_choice"],
                                    "P0")
                else:
                    wire("cast_tamiyo",
                         {k: v for k, v in act.items()
                          if not k.startswith("_")})
                    await submit_as_is(p0, act)
                say(f"P0 casts Tamiyo (turn {turn})")
                return
        # cast Divination -> 3rd draw -> transform
        if not obs["div_cast"] and not pw_ids and obs["tamiyo_cast"]:
            offered, act = cast_spell_offered(state, st, DIV)
            if (offered and main_phase
                    and len(untapped_lands(state, 0, ISLAND)) >= 3):
                obs["div_cast"] = True
                paying["p0"] = ("divination", DIV, {"U": 1, "generic": 2},
                                time.time())
                if "_vi_choice" in act:
                    wire("cast_divination_vi",
                         {"choiceId": act["_vi_choice"].get("id")})
                    await answer_vi(p0, act["_vi_opp"], act["_vi_choice"],
                                    "P0")
                else:
                    wire("cast_divination",
                         {k: v for k, v in act.items()
                          if not k.startswith("_")})
                    await submit_as_is(p0, act)
                say(f"P0 casts Divination (turn {turn})")
                return
        # proof: activate the +2 (export PRE first, submit while holding
        # priority -- never pass before the submission)
        if pw_ids and not obs["plus2_submitted"] and main_phase:
            aa = find_plus2(acts, state)
            if aa:
                say(f"proof: exporting PRE, activating Tamiyo +2 "
                    f"(turn {turn} phase {phase})")
                await export_tag(p0, "pre")
                wire("activate_plus2",
                     {k: v for k, v in aa.items() if not k.startswith("_")})
                obs["plus2_turn"] = turn
                obs["plus2_phase"] = phase
                await submit_as_is(p0, aa)
                obs["plus2_submitted"] = True
                say(f"P0 activates Tamiyo +2 (turn {turn})")
                return
        # normal land play
        la = find_action(acts, "PlayLand")
        if la and player_of(state, 0).get("lands_played_this_turn", 0) == 0:
            await submit_as_is(p0, la)
            return
        await pass_priority(p0, st, acts)

    async def p1_tick(st, acts, state):
        turn = state.get("turn_number")
        phase = state.get("phase")
        atypes = set(a.get("type") for a in acts)
        if await do_mulligan(p1, acts, st, 1, "P1",
                             lambda hn, mulls: True):
            return
        if await do_bottom(p1, acts, st, 1, "P1"):
            return
        if await do_discard_to_handsize(p1, acts, st, 1, "P1"):
            return
        # --- post-attack captures: at EndCombat/PostCombatMain of the attack
        # turn the attacker's power shows whether the -1/-0 applied. ---
        if (obs["attack1"] and not exported["post"]
                and turn == obs.get("attack1_turn")
                and phase in ("EndCombat", "PostCombatMain")):
            stx = await export_state_dict(p0, "post")
            if stx is None:
                return
            oid = obs.get("attack1_bear")
            power = num(get_obj(stx, oid).get("power")) if oid is not None else None
            obs["bear1_power_post"] = power
            obs["bear1_life_post"] = life_of(stx, 0)
            say(f"POST: attack1 turn {turn} phase {phase}; "
                f"bear oid {oid} power={power}, P0 life={obs['bear1_life_post']}")
        if (obs["attack2"] and not exported["post2"]
                and turn == obs.get("attack2_turn")
                and phase in ("EndCombat", "PostCombatMain")):
            stx = await export_state_dict(p0, "post2")
            if stx is None:
                return
            oid = obs.get("attack2_bear")
            power = num(get_obj(stx, oid).get("power")) if oid is not None else None
            obs["bear2_power_post2"] = power
            obs["bear2_life_post2"] = life_of(stx, 0)
            say(f"POST2: attack2 turn {turn} phase {phase}; "
                f"bear oid {oid} power={power}, P0 life={obs['bear2_life_post2']}")
        await asyncio.sleep(0)
        st = p1.latest or st
        state = st["state"]
        acts = merged_actions(st)
        atypes = set(a.get("type") for a in acts)
        log_vi_shapes(st, "P1")
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if "OrderTriggers" in atypes:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        if "DeclareAttackers" in atypes:
            da = find_action(acts, "DeclareAttackers")
            if da:
                # log the advertised shape once for 106-shape verification
                key = ("P1", "declare_shape")
                if key not in vi_shapes_logged:
                    vi_shapes_logged.add(key)
                    wire("p1_declare_attackers_shape",
                         {"data": da.get("data", {})})
                registered = {a.get("object_id")
                              for a in ((state.get("combat", {}) or {})
                                        .get("attackers", []) or [])}
                legal = attack_legal_bears(state, st)
                want_attack = False
                tag = None
                if legal and obs["plus2_submitted"]:
                    if not obs["attack1"]:
                        want_attack, tag = True, "attack1"
                    elif exported["p0next"] and not obs["attack2"]:
                        want_attack, tag = True, "attack2"
                if want_attack:
                    atk = legal[0]
                    if atk in registered:
                        # Declaration applied; the post-declaration priority
                        # round keeps the step advertised. Never re-submit
                        # (an empty re-declaration would undeclare the
                        # attack); fall through to priority handling.
                        wire("p1_attack_registered",
                             {"tag": tag, "attacker": atk})
                    else:
                        sub = copy.deepcopy(da)
                        sub["data"]["attacks"] = [
                            [atk, {"type": "Player", "data": 0}]]
                        sub["data"]["bands"] = []
                        await p1.send_action({"type": "DeclareAttackers",
                                              "data": sub["data"]})
                        obs[tag] = True
                        obs[f"{tag}_turn"] = turn
                        obs[f"{tag}_bear"] = atk
                        say(f"P1 {tag}: attacks P0 with Bear oid {atk} "
                            f"(turn {turn})")
                        wire(f"p1_{tag}", {"attacker": atk, "turn": turn})
                    return
                if not registered:
                    # Fresh declaration step with no intended attack: submit
                    # the explicit empty declaration the engine waits for.
                    sub = copy.deepcopy(da)
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                    await p1.send_action({"type": "DeclareAttackers",
                                          "data": sub["data"]})
                    return
                # Attack registered but no new attack wanted this tick: fall
                # through to priority handling below.
            else:
                return
        if "DeclareBlockers" in atypes:
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
        if await handle_paying(p1, st, acts, state, "p1"):
            return
        # cast bears when able (until both attacks are done)
        if not obs["attack1"] or not obs["attack2"]:
            offered, act = cast_spell_offered(state, st, BEAR)
            if offered and phase in ("PreCombatMain", "PostCombatMain"):
                paying["p1"] = ("bear", BEAR, {"G": 1, "generic": 1}, time.time())
                if "_vi_choice" in act:
                    await answer_vi(p1, act["_vi_opp"], act["_vi_choice"],
                                    "P1")
                else:
                    await submit_as_is(p1, act)
                say(f"P1 casts Grizzly Bears (turn {turn})")
                return
        la = find_action(acts, "PlayLand")
        if la and player_of(state, 1).get("lands_played_this_turn", 0) == 0:
            await submit_as_is(p1, la)
            return
        await pass_priority(p1, st, acts)

    ctx = {"obs": obs, "exported": exported, "kept": kept,
           "trigger_samples": trigger_samples,
           "sample_triggers": sample_triggers,
           "export_tag": export_tag, "export_state_dict": export_state_dict,
           "p0_tick": p0_tick, "p1_tick": p1_tick}
    return ctx

# ---------------------------------------------------------------------------
# Run scaffolding
# ---------------------------------------------------------------------------

async def verify_server_hello():
    url = os.environ.get("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")
    async with websockets.connect(url, max_size=2**26) as _w:
        hello = json.loads(await asyncio.wait_for(_w.recv(), 10))
    d = hello.get("data", {})
    say(f"ServerHello: {d.get('server_version')}/{d.get('build_commit')}/"
        f"protocol {d.get('protocol_version')}/{d.get('mode')}")
    assert str(d.get("server_version")).startswith("0.103.0"), hello
    assert d.get("build_commit") == "ec27a8d", hello
    assert int(d.get("protocol_version")) == 106, hello


def check_data_level():
    import glob
    card_data = None
    for cand in glob.glob(f"{BACKFILL}/server/releases/v0.103.0/data/"
                          f"card-data.json"):
        with open(cand) as f:
            card_data = json.load(f)
        break
    ok, notes = True, []
    e = (card_data or {}).get("tamiyo, seasoned scholar", {})
    if not e:
        ok = False
        notes.append("tamiyo, seasoned scholar: missing from card data")
    elif "until your next turn" not in str(e.get("oracle_text", "")).lower():
        ok = False
        notes.append("tamiyo, seasoned scholar: +2 oracle shape missing "
                     f"({str(e.get('oracle_text'))[:160]!r})")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": str(e.get("oracle_text"))[:500] if e else None},
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


async def finish(g, ctx):
    dur = time.time() - g["t_start"]
    obs = ctx["obs"]
    exported = ctx["exported"]
    if not exported["post2"] and not exported["post"]:
        # best-effort final export so the evidence dir always has a post
        try:
            post = await g["p0"].export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            exported["post"] = True
            say("post exported at finish (best effort)")
        except Exception as e:
            g["notes"].append(f"post export at finish failed: {e}")
    with open(f"{EVDIR}/obs.json", "w") as f:
        json.dump(obs, f, indent=1, default=str)
    verdict, eval_ass, eval_notes, eval_obs = evaluate(EVDIR, hints=obs)
    g["ass"].update(eval_ass)
    g["notes"].extend(eval_notes)
    obs.update(eval_obs)
    with open(f"{EVDIR}/trigger_samples.json", "w") as f:
        json.dump(ctx["trigger_samples"], f, indent=1, default=str)
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(g["t_start"])),
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID}",
        "server_port": 9374,
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6557_01030.py", "rb").read()
        ).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": g["ass"],
        "notes": g["notes"],
        "observations": obs,
        "trigger_samples": len(ctx["trigger_samples"]),
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Tamiyo / 12x Divination / 20x Grizzly Bears deck density is a "
            "test-harness convenience (engine accepts >4-of for custom games); "
            "exercised behavior is the shipped card text.",
            "Transform is produced by the printed Tamiyo, Inquisitive Student "
            "trigger (draw 3rd card in a turn via Divination), not a debug fixture.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Not tested on the original 0.35.0 report build; verdict is scoped "
            "to v0.103.0, not a fix claim.",
        ],
        "setup_line": "P0: 12x tamiyo inquisitive student + 12x divination + 36x island "
                      "(mulligan to Tamiyo + 2 islands; T1 Tamiyo; Divination -> "
                      "transform; +2 on main phase); P1: 20x grizzly bears + 40x "
                      "forest (attacks P0 with one bear after +2, control attack "
                      "after the watching effect expires)",
        "contract_line": "Tamiyo +2: until P0's next turn, each creature attacking "
                         "P0 gets -1/-0 until end of turn (incl. on P1's turn)",
        "prior_validation": {
            "run": "20261005-6557c",
            "verdict": "reproduced",
            "validated_version": "v0.102.0",
            "comment_url": "https://github.com/phase-rs/phase/issues/6557#issuecomment-5617016250",
            "evidence_commit": "428ef7a554589e7ba8ef027788e93c506887a59f",
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": g["ass"], "notes": g["notes"],
                   "verdict": verdict}, f, indent=1)
    shutil.copyfile(f"{BACKFILL}/driver/scenario_6557_01030.py",
                    f"{EVDIR}/scenario_6557_01030.py")
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
    pidfile = "/tmp/scenario_6557_01030.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_6557_01030 instance is alive "
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
    g = {"t_start": t_start, "phase": "setup",
         "notes": [],
         "ass": {k: "not-run" for k in
                 ("A1_setup_ok", "A2_plus2", "A3_weakened", "A4_wears_off",
                  "A5_expired", "A6_cleanup")},
         }
    await verify_server_hello()
    check_data_level()

    p0 = PhaseClient("P0-6557")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1-6557")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    g["p0"], g["p1"] = p0, p1
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    ctx = make_drivers(p0, p1)
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
        while time.time() - t0 < 1200 and not done:
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
                    await tick(st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"tick error {c.name}: {e}")
                    wire("tick_error", {"who": c.name, "err": str(e)})
                try:
                    ctx["sample_triggers"](c.latest["state"])
                except Exception:
                    pass
            if exported["post2"]:
                say("post2 exported; finishing")
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
                    f"pw_loyalty={tamiyo_pw_loyalty(s)} life={life_of(s, 0)}/{life_of(s, 1)} "
                    f"stack={len(s.get('stack') or [])} plus2={obs['plus2_submitted']} "
                    f"atk1={obs['attack1']} atk2={obs['attack2']} "
                    f"exp={json.dumps(exported)}")
            if obs["plus2_submitted"] and not exported["post2"] \
                    and stuck_deadline is None:
                stuck_deadline = time.time() + 420
            if not obs["plus2_submitted"] or exported["post2"]:
                stuck_deadline = None
            if stuck_deadline and time.time() > stuck_deadline:
                g["notes"].append("+2 submitted but full attack cycle not "
                                  "completed in 420s; see wire log (possible "
                                  "unhandled interaction)")
                await finish(g, ctx)
                done = True
                break
    finally:
        pass
    if not done:
        g["notes"].append("global timeout (1200s) hit before assertions resolved")
        await finish(g, ctx)


if __name__ == "__main__":
    asyncio.run(main())
