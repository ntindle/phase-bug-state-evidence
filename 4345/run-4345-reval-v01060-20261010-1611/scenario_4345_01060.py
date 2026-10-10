#!/usr/bin/env python3
"""Issue #4345: Stuck decision: CombatTaxPayment.

RE-VALIDATION on pinned v0.106.0 (2026-10-10, protocol 126). Prior runs:
  v0.78.0  20260909-4345  blocked (generic CombatTaxPayment attempt; all
           assertions passed; report has no testable premise) -- maintained
           comment 5606460566 on the issue.
  v0.101.0 / v0.102.0 / v0.103.0 re-checks (2026-10-04/05/06): comment-only,
           no new engine run (no testable premise; issue still open,
           status:needs-repro, maintainer's 2026-07-19 questions unanswered).
  v0.104.0  run-4345-reval-v01040-20261008-2130 (2026-10-08, protocol 118):
           fresh generic-attempt engine run; pay leg P1 20->18, decline leg
           no damage, every tax wait offered an available payCombatTax
           choice; no softlock signature; verdict stays blocked (no testable
           premise).
  v0.106.0  run-4345-reval-v01060-20261010-1611 (2026-10-10, protocol 126):
           fresh generic-attempt engine run on the current pin; verdict stays
           blocked (no testable premise); maintained comment 5606460566
           extended with the v0.106.0 result.
  This run re-validates the generic attempt on the current pin and extends
  the maintained comment with the fresh result.

Report (github issue, build v0.6.0 399601b, 2026-06-26):
  "What happened" - EMPTY (no description at all).
  Diagnostic: Waiting for: CombatTaxPayment | Stuck players: 0
  Maintainer (2026-07-19, mike-theDude): asked for the attackers, the card
  imposing the combat tax, available mana, and stall timing (before choosing
  to pay / while paying / after declining); a saved game state would make it
  actionable. NO reply from the reporter. No attachment.

There is NO testable premise: no board state, no tax card, no mana
situation, no indication of which branch (pay vs decline) stalled. Per
PLAYBOOK step 2, a native engine test of the reported bug would require
inventing the premise. This run is therefore a blocked ATTEMPT: drive a
fresh native-engine game through the CombatTaxPayment decision - once
paying the tax (Ghostly Prison {2} per attacker), once declining to pay -
and record whether the decision ever leaves the acting player with no
available submission (the reported softlock signature: decision waiting,
no actionable submission, Stuck players: 0).

Oracle (verified from pinned v0.106.0 card-data.json, see
parse_evidence.json): Ghostly Prison -- "Creatures can't attack you unless
their controller pays {2} for each creature they control that's attacking
you." Static CantAttack / UnlessPay {2} / PerAffectedCreature.

Protocol-126 shapes (mechanically carried over from the verified
protocol-120 run; interaction conventions unchanged from the 120 run;
ServerHello pin below enforces v0.106.0/29e0db3/protocol 126. Original
probe 2026-10-08 + engine source
phase-src-v0.82.0/crates/engine/src/game/interaction.rs):
  - DeclareAttackers: viewer_interaction waitingForKind.code == "relations",
    schema response with attacker->defender edges; a legacy DeclareAttackers
    action also exists and is submitted (as in scenario_7161_01040.py).
  - CombatTaxPayment: waitingForKind.code == "choose", exactChoices whose
    choices carry an action surface code "payCombatTax" plus a value surface
    {"role": "accept", "value": true/false}. Answered with
    {"type":"choose","data":{"choiceId":...}}.
  - Routine priority actions arrive as a "choose" exactChoices menu with
    action codes passPriority / playLand / castSpell and object surfaces
    naming the card; answered the same way. Legacy actions are a fallback.
  - Casts use engine auto-payment (payment_mode Auto); the driver never taps
    lands for mana (cf. 2026-10-07 double-payment note).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing)
  P0: 12x Grizzly Bears + 48x Forest (mulligan to Bear + 2 lands). P1: 8x
  Ghostly Prison + 52x Plains (mulligan to Prison + 3 Plains, cast Prison,
  never attacks, never blocks). P0 attacks P1 with exactly one ready Bear
  only when >=2 Forests are untapped (tax {2} payable, so both accept=true
  and accept=false are offered).
  Leg 1 (pay): first taxed attack -> answer payCombatTax accept=true ->
  engine auto-pays {2} -> unblocked Bear deals 2 (P1 20 -> 18).
  Leg 2 (decline): next taxed attack -> answer accept=false -> no damage,
  game proceeds past the tax decision.
  Exports: pre.json (first taxed DeclareAttackers, before declaring),
  mid_tax.json (first CombatTaxPayment wait, before answering),
  tax_decision_dump.json (full viewer_interaction of the first tax wait),
  post.json (after the decline leg resolves).

  Assertions:
    A1_setup_ok:      pre: P1 Ghostly Prison on BF; P0 Bear able to attack;
                      P1 life 20.
    A2_tax_prompted:  >=1 CombatTaxPayment wait observed for P0 (the acting
                      player), each offering >=1 available payCombatTax
                      choice.
    A3_pay_completes: pay-mode tax answered accept=true; P1 life 20 -> 18
                      from the unblocked Bear; game left the tax decision.
    A4_decline_resolves: decline-mode tax answered accept=false; no damage
                      from the declined attack (P1 life unchanged); game
                      advanced past the tax decision.
    A5_no_softlock:   no CombatTaxPayment wait lacked an available submission
                      for the acting player; the watchdog never fired; no
                      advertised tax answer was rejected.
    A6_cleanup:       post.json: stack empty, no tax decision pending, game
                      proceeding.

  Verdict rule: the reported bug has no testable premise (missing setup
  information that materially changes the test), so the verdict is
  `blocked` regardless of the generic attempt outcome. The generic attempt
  becomes `reproduced` only if the softlock signature itself is observed (a
  CombatTaxPayment wait with no available submission for the acting player,
  the decision orphaned after an answer, or an advertised tax answer
  rejected); it can never be `not-reproduced` because the reported path was
  never identified.
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
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 4345
RUN_ID = "run-4345-reval-v01060-20261010-1611"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEAR = "grizzly bears"
FOREST = "forest"
PLAINS = "plains"
PRISON = "ghostly prison"

P0_DECK = [(BEAR, 12), (FOREST, 48)]
P1_DECK = [(PRISON, 8), (PLAINS, 52)]

SERVER_IDENTITY = {
    "server_version": "0.106.0",
    "build_commit": "29e0db3",
    "protocol_version": 126,
    "mode": "Full",
    "binary_sha256": "5d31aeec7387c22b5af84f7befa83c14f785d8505c0f9102f4fddac743d1d47a",
    "card_data_sha256": "59d60da9833ff7cd464fca2c93f4d6b49d74a8ad0d04092055634442c70cbc41",
    "draft_pools_sha256": "fad736f3ae13b3fb2f0908319eae98af501e36cc30fbe2e84ca6684319384c86",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-10-10",
    "source": "ServerHello observed at run start + minisign verified binary/ "
              "data manifest (repo-pinned SERVER_ARTIFACT_PUBLIC_KEY, keynum "
              "436711b6a2d36828) + data files matched signed manifest; shared "
              "pinned v0.106.0 server on 127.0.0.1:9374 reused (not started "
              "by this run; owned by run-1488-reval-v01060-20261010-1441, PID 2105)",
}

GLOBAL_TIMEOUT = 1500
TAX_WATCHDOG_S = 120


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "kind": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


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


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def on_bf(state, pid, name):
    return any(obj_lname(state, o) == name for o in bf_oids(state, pid))


def untapped_lands(state, pid, name):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == name and not get_obj(state, o).get("tapped")]


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
            return False
    return True


def ready_bears(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == BEAR and can_attack_now(state, o)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def card_in_hand_oid(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
    return None


# ------------------------------------------------------------- vi choice helpers
def choice_available(ch):
    st = ch.get("status", {}) or {}
    return st.get("type") in (None, "available")


def action_code_of(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            c = (s.get("data", {}) or {}).get("code")
            if c:
                return c
    return None


def object_name_of(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "object":
            n = (s.get("data", {}) or {}).get("name")
            if n:
                return str(n).lower()
    return None


def accept_value_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        if s.get("type") == "value" and str(d.get("role", "")).lower() == "accept":
            v = d.get("value")
            # wire carries booleans as the strings "true"/"false"
            if isinstance(v, str):
                lv = v.lower()
                if lv == "true":
                    return True
                if lv == "false":
                    return False
                return None
            return v if isinstance(v, bool) else None
    return None


def tax_opportunity(st):
    """Return (opp, pay_choice, decline_choice, n_available) when this seat's
    viewer_interaction carries a CombatTaxPayment decision (vi kind "choose",
    exactChoices with payCombatTax action codes)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        choices = (resp.get("data", {}) or {}).get("choices") or []
        pay = decline = None
        n_avail = 0
        is_tax = False
        for ch in choices:
            if action_code_of(ch) != "payCombatTax":
                continue
            is_tax = True
            if choice_available(ch):
                n_avail += 1
            acc = accept_value_of(ch)
            if acc is True and pay is None:
                pay = ch
            elif acc is False and decline is None:
                decline = ch
        if is_tax:
            return opp, pay, decline, n_avail
    return None, None, None, 0


def menu_opportunity(st):
    """Return the routine priority-action menu opportunity (vi kind "choose",
    exactChoices WITHOUT any payCombatTax choice), or None."""
    if vi_kind_code(st) != "choose":
        return None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        choices = (resp.get("data", {}) or {}).get("choices") or []
        if any(action_code_of(ch) == "payCombatTax" for ch in choices):
            continue
        return opp
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


async def choose_via(c, opp, ch, tag, why):
    iid = opp.get("interactionId") or opp.get("id")
    say(f"[{tag}] vi choose ({why}): choice={ch.get('id')} "
        f"action={action_code_of(ch)} obj={object_name_of(ch)}")
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "choose",
                                       "data": {"choiceId": ch.get("id")}}}, tag)


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, acts, st, pid, tag, g, keep_fn):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in g.mulls:
        return False
    g.mulls.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    mull_count = sum(1 for k in g.mulls if k[0] == tag and k[1] == "mull")
    if keep_fn(state, pid) or mull_count >= 3 or len(hn) <= 4:
        say(f"[{tag}] keep {len(hn)}: {hn}")
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    else:
        say(f"[{tag}] mulligan #{mull_count + 1} ({len(hn)}: {hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, g, avoid_names):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
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
        key = (tag, "bottom", str(iid))
        if key in g.submitted:
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
            .get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    ref = d.get("reference")
                    break
            nm = obj_lname(state, ref) if ref is not None else "?"
            if nm in avoid_names:
                return (2, str(ref))
            if ref is not None and is_land(get_obj(state, ref)):
                return (0, str(ref))
            return (1, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch.get("id") for ch in ranked[:n]]
        g.submitted.add(key)
        say(f"[{tag}] bottoming {n} via vi")
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def do_discard_to_handsize(c, acts, st, pid, tag, g, avoid_names):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(str(h) for h in hand)
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        def _ref(ch):
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    return d.get("reference")
            return None
        if not any(str(_ref(ch)) in handset for ch in cands):
            continue
        key = (tag, "handsize", str(c.revision), str(opp.get("interactionId")))
        if key in g.submitted:
            return False

        def rank(ch):
            nm = obj_lname(state, _ref(ch))
            return 2 if nm in avoid_names else (
                0 if nm in (FOREST, PLAINS) else 1)

        ranked = sorted(cands, key=rank)
        picks = [ch.get("id") for ch in ranked[:n]]
        g.submitted.add(key)
        say(f"[{tag}] discarding {n} to hand size")
        rtype = "choose" if n == 1 else "select"
        rdata_sub = {"choiceId": picks[0]} if n == 1 else {"choiceIds": picks}
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": rtype, "data": rdata_sub}}, tag)
        return True
    return False


def cast_action_for(acts, state, oid):
    for a in acts:
        if a["type"] != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


async def play_a_land_legacy(c, state, pid, acts, tag, g, pref):
    if g.land_played_turn.get(pid) == state.get("turn_number"):
        return False
    lod = card_in_hand_oid(state, pid, pref)
    if lod is None:
        return False
    for a in acts:
        if a["type"] != "PlayLand":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(lod):
                    await submit_as_is(c, a)
                    g.land_played_turn[pid] = state.get("turn_number")
                    say(f"[{tag}] plays {pref} (legacy)")
                    return True
            except (TypeError, ValueError):
                continue
    return False


async def pass_priority_legacy(c, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def do_priority_menu(c, st, acts, state, pid, tag, g, cast_name, land_name):
    """Answer the routine 'choose' action menu: cast key spell, else play
    land, else pass. Returns True if something was submitted."""
    opp = menu_opportunity(st)
    if opp is None:
        return False
    choices = ((opp.get("response", {}) or {}).get("data", {}) or {}) \
        .get("choices") or []
    avail = [ch for ch in choices if choice_available(ch)]
    # 1. cast the key spell on our main phase
    if my_main(state, pid):
        for ch in avail:
            if action_code_of(ch) == "castSpell" \
                    and object_name_of(ch) == cast_name:
                await choose_via(c, opp, ch, tag, f"cast {cast_name}")
                return True
    # 2. play a land
    if g.land_played_turn.get(pid) != state.get("turn_number"):
        for ch in avail:
            if action_code_of(ch) == "playLand" \
                    and object_name_of(ch) == land_name:
                g.land_played_turn[pid] = state.get("turn_number")
                await choose_via(c, opp, ch, tag, f"play {land_name}")
                return True
    # 3. pass
    for ch in avail:
        if action_code_of(ch) == "passPriority":
            await choose_via(c, opp, ch, tag, "pass")
            return True
    return False


async def do_priority(c, st, acts, state, pid, tag, g, cast_name, land_name):
    """Priority handling: vi menu first, legacy actions as fallback."""
    if await do_priority_menu(c, st, acts, state, pid, tag, g,
                              cast_name, land_name):
        return True
    if my_main(state, pid):
        cid = card_in_hand_oid(state, pid, cast_name)
        a = cast_action_for(acts, state, cid) if cid else None
        if a is not None:
            say(f"[{tag}] casts {cast_name} (legacy)")
            wire("cast", {"who": tag, "card": cast_name})
            await submit_as_is(c, a)
            return True
    if await play_a_land_legacy(c, state, pid, acts, tag, g, land_name):
        return True
    if await pass_priority_legacy(c, acts):
        return True
    return False


# ------------------------------------------------------------- tax decision
async def handle_tax(c, st, acts, state, g):
    """Record + answer a CombatTaxPayment decision for P0 (the attacker).

    Returns True if the decision was handled (answered or recorded as
    unanswerable). P1 never answers: the tax is the attacker's decision.
    """
    opp, pay_ch, decline_ch, n_avail = tax_opportunity(st)
    if opp is None:
        return False
    mode = "decline" if g.decline_mode else "pay"
    rec = {
        "turn": state.get("turn_number"),
        "phase": state.get("phase"),
        "who": "P0",
        "mode": mode,
        "n_choices_avail": n_avail,
        "pay_offered": pay_ch is not None and choice_available(pay_ch),
        "decline_offered": decline_ch is not None and choice_available(decline_ch),
    }
    g.tax_waits.append(rec)
    wire("tax_wait", {"rec": rec,
                      "opportunity": opp,
                      "legal_action_types": sorted({a.get("type") for a in acts})})
    say(f"[P0] CombatTaxPayment wait (mode={mode}): avail={n_avail} "
        f"pay_offered={rec['pay_offered']} decline_offered={rec['decline_offered']}")
    if g.first_tax_at is None:
        g.first_tax_at = time.time()
    if not g.tax_dumped:
        g.tax_dumped = True
        with open(f"{EVDIR}/tax_decision_dump.json", "w") as f:
            json.dump({"turn": rec["turn"], "phase": rec["phase"],
                       "vi_kind_code": vi_kind_code(st),
                       "viewer_interaction": st.get("viewer_interaction")},
                      f, indent=1, default=str)
        say("[P0] dumped tax_decision_dump.json")
        try:
            mid = await c.export_state()
            with open(f"{EVDIR}/mid_tax.json", "w") as f:
                f.write(mid)
            say("[P0] exported mid_tax.json (first tax wait, pre-answer)")
        except Exception as e:
            g.note(f"mid_tax export failed: {e}")
    # export pre.json at the first tax wait too (first taxed attackers step)
    if g.pre is None:
        try:
            g.pre = await g.export("pre", g.p0)
            g.life_pre = life_of(g.pre, 1)
            say(f"[P0] exported pre.json (first taxed DeclareAttackers; "
                f"P1 life {g.life_pre})")
        except Exception as e:
            g.note(f"pre export failed: {e}")
    want_pay = not g.decline_mode
    ch = pay_ch if want_pay else decline_ch
    key = ("tax", str(opp.get("interactionId")),
           "pay" if want_pay else "decline", str(c.revision))
    if ch is not None and choice_available(ch) and key not in g.submitted:
        g.submitted.add(key)
        await choose_via(c, opp, ch, "P0", f"tax {mode}")
        g.last_tax_answer = {"at": time.time(), "mode": mode,
                             "turn": rec["turn"]}
        return True
    # fallback: legacy PayCombatTax action
    for a in acts:
        if a.get("type") == "PayCombatTax" \
                and (a.get("data", {}) or {}).get("accept") is want_pay:
            akey = ("tax-legacy", str(c.revision), mode)
            if akey in g.submitted:
                return True
            g.submitted.add(akey)
            say(f"[P0] submits legacy PayCombatTax accept={want_pay}")
            wire("tax_legacy_submit", {"mode": mode, "action": a})
            await submit_as_is(c, a)
            g.last_tax_answer = {"at": time.time(), "mode": mode,
                                 "turn": rec["turn"]}
            return True
    # no actionable submission for the acting player: THE reported signature
    if n_avail == 0:
        g.stall_observed = True
        g.note(f"SOFTLOCK SIGNATURE: CombatTaxPayment wait (turn {rec['turn']}, "
               f"mode={mode}) offered NO available submission to P0")
        say("[P0] SOFTLOCK SIGNATURE: tax wait with no available submission")
        try:
            stall = await c.export_state()
            with open(f"{EVDIR}/mid_stall.json", "w") as f:
                f.write(stall)
        except Exception as e:
            g.note(f"mid_stall export failed: {e}")
    else:
        say(f"[P0] tax wait: wanted branch not offered "
            f"(pay={rec['pay_offered']} decline={rec['decline_offered']}); waiting")
    return True


# ------------------------------------------------------------- declare attackers
async def handle_relations(c, st, acts, state, g):
    """Answer the DeclareAttackers 'relations' opportunity for P0 with exactly
    one ready Bear when the tax test calls for it; otherwise declare empty."""
    if vi_kind_code(st) != "relations":
        return False
    da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
    if da is None:
        return False
    key = ("declare", str(c.revision))
    if key in g.submitted:
        return False
    prison = on_bf(state, 1, PRISON)
    ready = ready_bears(state, 0)
    mana_ok = len(untapped_lands(state, 0, FOREST)) >= 2
    want_attack = False
    if prison and ready and mana_ok:
        if not g.decline_mode and not g.pay_declared:
            want_attack = True
            g.pay_declared = True
        elif g.decline_mode and not g.decline_declared:
            want_attack = True
            g.decline_declared = True
    sub = copy.deepcopy(da)
    if want_attack:
        sub.setdefault("data", {})["attacks"] = [
            [ready[0], {"type": "Player", "data": g.p1.player_id}]]
        sub["data"]["bands"] = []
        say(f"[P0] declares attack: Bear {ready[0]} -> P1 "
            f"(mode={'decline' if g.decline_mode else 'pay'})")
    else:
        sub.setdefault("data", {}).update({"attacks": [], "bands": []})
        say(f"[P0] declares no attackers (prison={prison} ready={len(ready)} "
            f"mana_ok={mana_ok} decline_mode={g.decline_mode})")
    wire("declare_attackers", {"want_attack": want_attack,
                              "data": sub.get("data", {})})
    g.submitted.add(key)
    await submit_as_is(c, {"type": "DeclareAttackers", "data": sub.get("data", {})})
    return True


# ------------------------------------------------------------- game
class Game:
    def __init__(self):
        self.p0 = self.p1 = None
        self.mulls = set()
        self.submitted = set()
        self.land_played_turn = {}
        self.notes = []
        self.stage = "setup"
        self.decline_mode = False
        self.pay_declared = False
        self.decline_declared = False
        self.tax_waits = []
        self.tax_dumped = False
        self.first_tax_at = None
        self.last_tax_answer = None
        self.stall_observed = False
        self.rejections = []
        self.life_pre = None
        self.life_after_pay = None
        self.life_post = None
        self.pay_attack_turn = None
        self.decline_turn = None
        self.pre = None
        self.post = None
        self.done = False

    def say(self, *a):
        say(" ".join(str(x) for x in a))

    def note(self, m):
        self.notes.append(m)

    async def start(self):
        self.p0 = PhaseClient("P0")
        await self.p0.connect()
        await self.p0.create(deck(*P0_DECK))
        self.p1 = PhaseClient("P1")
        await self.p1.connect()
        await self.p1.join(self.p0.game_code, deck(*P1_DECK))
        self.say(f"game {self.p0.game_code} P0seat={self.p0.player_id} "
                 f"P1seat={self.p1.player_id}")
        wire("game_start", { "game_code": self.p0.game_code,
                             "p0_seat": self.p0.player_id,
                             "p1_seat": self.p1.player_id,
                             "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    async def export(self, name, client):
        env = await client.export_state()
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(env)
        return json.loads(env)["state"]

    async def close(self):
        for c in (self.p0, self.p1):
            try:
                await c.close()
            except Exception:
                pass


async def drain_rejections(c, g):
    while c.rejections:
        r = c.rejections.pop(0)
        g.rejections.append({"who": c.name, "type": r["type"],
                             "data": r["data"]})
        wire("rejection", {"who": c.name, "type": r["type"], "data": r["data"]})
        say(f"[{c.name}] {r['type']}: {json.dumps(r['data'])[:220]}")
        # a rejected advertised tax answer is itself a failure signal
        if not g.stall_observed:
            g.note(f"rejection on {c.name}: {r['type']} "
                   f"{json.dumps(r['data'])[:200]}")


async def p0_tick(c, st, acts, state, g):
    await drain_rejections(c, g)
    if await do_mulligan(c, acts, st, 0, "P0", g,
                         lambda s, p: BEAR in hand_lnames(s, p)
                         and sum(1 for n in hand_lnames(s, p)
                                 if n == FOREST) >= 2):
        return
    if await do_bottom(c, acts, st, 0, "P0", g, (BEAR,)):
        return
    if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (BEAR,)):
        return
    # tax decision has top priority for the attacker
    if await handle_tax(c, st, acts, state, g):
        return
    if await handle_relations(c, st, acts, state, g):
        return
    # P0 never blocks (P1 never attacks), but answer if asked
    for a in acts:
        if a.get("type") == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, {"type": "DeclareBlockers",
                                   "data": sub["data"]})
            return
    # routine priority menu / legacy actions
    if await do_priority(c, st, acts, state, 0, "P0", g, BEAR, FOREST):
        return


async def p1_tick(c, st, acts, state, g):
    await drain_rejections(c, g)
    if await do_mulligan(c, acts, st, 1, "P1", g,
                         lambda s, p: PRISON in hand_lnames(s, p)
                         and sum(1 for n in hand_lnames(s, p)
                                 if n == PLAINS) >= 3):
        return
    if await do_bottom(c, acts, st, 1, "P1", g, (PRISON,)):
        return
    if await do_discard_to_handsize(c, acts, st, 1, "P1", g, (PRISON,)):
        return
    # P1 never answers the tax decision (attacker's decision); if a tax
    # opportunity is somehow submittable here, record and stay passive.
    opp, _, _, _ = tax_opportunity(st)
    if opp is not None:
        wire("p1_tax_visible_passive",
             {"interactionId": opp.get("interactionId")})
        return
    # declare no attackers / no blockers
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, {"type": "DeclareAttackers",
                                   "data": sub["data"]})
            return
        if a.get("type") == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, {"type": "DeclareBlockers",
                                   "data": sub["data"]})
            return
    if vi_kind_code(st) == "relations":
        return  # P1 has no attackers; wait it out
    # cast Prison on own main, else land, else pass
    if await do_priority(c, st, acts, state, 1, "P1", g, PRISON, PLAINS):
        return


# ------------------------------------------------------------- pump
async def pump(g, timeout_s):
    t0 = time.time()
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.15)
        for c, is_p0 in ((g.p0, True), (g.p1, False)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[c.player_id] = time.time()
            else:
                if time.time() - last_change[c.player_id] > 90:
                    s0 = st["state"]
                    la = [a.get("type") for a in
                          (st.get("legal_actions") or [])][:8]
                    g.say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                          f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                          f"legal={la} vikind={vi_kind_code(st)!r}")
                    last_change[c.player_id] = time.time()
                holds_prio = any(
                    a.get("type") == "PassPriority"
                    for a in (st.get("legal_actions") or []))
                if not (holds_prio
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                state = st["state"]
                acts = merged_actions(st)
                if is_p0:
                    await p0_tick(c, st, acts, state, g)
                else:
                    await p1_tick(c, st, acts, state, g)
            except Exception as e:
                g.say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        # ---- stage transitions (observed from P0's seat) ----
        s0 = g.p0.latest["state"] if g.p0.latest else {}
        p0st = g.p0.latest
        tax_now = tax_opportunity(p0st)[0] is not None if p0st else False
        if g.stall_observed:
            g.say("stall observed; finishing")
            g.done = True
        # pay leg -> decline leg: pay answered, tax gone, P1 took 2
        if (not g.decline_mode and g.last_tax_answer
                and g.last_tax_answer["mode"] == "pay" and not tax_now):
            lp = life_of(s0, 1)
            if g.life_pre is not None and lp == g.life_pre - 2:
                g.life_after_pay = lp
                g.pay_attack_turn = g.last_tax_answer["turn"]
                g.decline_mode = True
                g.note(f"pay leg resolved: P1 life {g.life_pre} -> {lp}; "
                       f"switching to decline mode")
                say("switched to DECLINE mode")
        # decline leg -> done: decline answered, tax gone, game advanced
        if (g.decline_mode and g.last_tax_answer
                and g.last_tax_answer["mode"] == "decline" and not tax_now
                and g.decline_turn is None):
            g.decline_turn = s0.get("turn_number")
            g.note(f"decline-mode tax resolved (turn {g.decline_turn})")
        if g.decline_turn is not None and g.post is None and g.p0.latest:
            adv = (s0.get("turn_number") or 0) > g.decline_turn or (
                s0.get("phase") in ("PostCombatMain", "End", "Cleanup")
                and s0.get("turn_number") == g.decline_turn)
            if adv:
                try:
                    g.post = await g.export("post", g.p0)
                    g.life_post = life_of(g.post, 1)
                    say(f"exported post.json (P1 life {g.life_post})")
                except Exception as e:
                    g.note(f"post export failed: {e}")
                g.done = True
        # tax watchdog: wanted branch never offered / never answerable
        if g.first_tax_at is not None and g.last_tax_answer is None \
                and not g.stall_observed:
            if time.time() - g.first_tax_at > TAX_WATCHDOG_S and tax_now:
                g.stall_observed = True
                g.note(f"tax watchdog: CombatTaxPayment wait unanswered for "
                       f">{TAX_WATCHDOG_S}s (wanted branch never actionable)")
                say("TAX WATCHDOG fired; finishing")
                try:
                    stall = await g.p0.export_state()
                    with open(f"{EVDIR}/mid_stall.json", "w") as f:
                        f.write(stall)
                except Exception as e:
                    g.note(f"mid_stall export failed: {e}")
                g.done = True
        # boredom exit: no tax decision deep into the game
        if not g.tax_waits and (s0.get("turn_number") or 0) >= 30:
            g.note("boredom exit: turn >= 30 with no CombatTaxPayment wait "
                   "(Prison likely never resolved)")
            say("boredom exit; finishing")
            g.done = True
        if g.done:
            g.say("done flag set; finishing")
            return True
        if time.time() - last_diag > 60 and g.p0.latest:
            last_diag = time.time()
            s = g.p0.latest["state"]
            g.say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                  f"phase={s.get('phase')} decline_mode={g.decline_mode} "
                  f"P0bears={len(ready_bears(s, 0))} "
                  f"P1prison={on_bf(s, 1, PRISON)} life1={life_of(s, 1)} "
                  f"taxwaits={len(g.tax_waits)} vikind={vi_kind_code(g.p0.latest)!r}")
    g.note(f"global timeout ({timeout_s}s) hit")
    return False


# ------------------------------------------------------------- server + parse
async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
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
    assert str(ver).startswith("0.106.0"), f"unexpected version {ver}"
    assert int(proto) == 126, f"unexpected protocol {proto}"
    assert str(build) == "29e0db3", f"unexpected build {build}"


def parse_evidence():
    """Capture Ghostly Prison's actual parse from pinned v0.106.0 data."""
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.106.0/data/"
                        "card-data.json"))
    e = cd[PRISON]
    out = {PRISON: {"name": e.get("name"),
                    "oracle_text": e.get("oracle_text"),
                    "static_abilities": e.get("static_abilities")}}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(g):
    ass = {}
    notes = []
    pre, post = g.pre, g.post
    if pre is not None:
        g.life_pre = life_of(pre, 1)
        prison = on_bf(pre, 1, PRISON)
        bears = ready_bears(pre, 0)
        if prison and bears and g.life_pre == 20:
            ass["A1_setup_ok"] = "passed"
            notes.append(f"pre: P1 Prison on BF, {len(bears)} ready Bear(s), "
                         f"P1 life {g.life_pre}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(f"pre incomplete: prison={prison} "
                         f"ready_bears={len(bears)} life1={g.life_pre}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre.json was never exported (no taxed attackers step)")
    p0_waits = [w for w in g.tax_waits if w["who"] == "P0"]
    if p0_waits and all(w["n_choices_avail"] >= 1 for w in p0_waits):
        ass["A2_tax_prompted"] = "passed"
        notes.append(f"{len(p0_waits)} CombatTaxPayment wait(s) for P0; every "
                     f"wait offered >=1 available payCombatTax choice")
    elif p0_waits:
        ass["A2_tax_prompted"] = "failed"
        notes.append(f"{len(p0_waits)} tax wait(s) but some offered no "
                     f"available choice (softlock signature)")
        g.stall_observed = True
    else:
        ass["A2_tax_prompted"] = "failed"
        notes.append("no CombatTaxPayment wait was ever observed")
    pay_waits = [w for w in p0_waits if w["mode"] == "pay"]
    if (g.life_after_pay is not None and g.life_pre is not None
            and g.life_after_pay == g.life_pre - 2 and pay_waits
            and pay_waits[0]["pay_offered"]):
        ass["A3_pay_completes"] = "passed"
        notes.append(f"pay leg: accept=true answered, P1 life {g.life_pre} -> "
                     f"{g.life_after_pay} (unblocked Bear, tax {{2}} paid)")
    else:
        ass["A3_pay_completes"] = "failed"
        notes.append(f"pay leg incomplete: life_pre={g.life_pre} "
                     f"life_after_pay={g.life_after_pay} "
                     f"pay_waits={len(pay_waits)}")
    decline_waits = [w for w in p0_waits if w["mode"] == "decline"]
    if (g.decline_turn is not None and decline_waits
            and decline_waits[0]["decline_offered"]
            and g.life_post == g.life_after_pay):
        ass["A4_decline_resolves"] = "passed"
        notes.append(f"decline leg: accept=false answered, no damage "
                     f"(P1 life {g.life_post}), game advanced past the tax "
                     f"wait (decline turn {g.decline_turn})")
    else:
        ass["A4_decline_resolves"] = "failed"
        notes.append(f"decline leg incomplete: decline_waits={len(decline_waits)} "
                     f"decline_turn={g.decline_turn} life_post={g.life_post} "
                     f"life_after_pay={g.life_after_pay}")
    tax_rej = [r for r in g.rejections
               if "paycombattax" in json.dumps(r.get("data", {})).lower()
               or "combattax" in json.dumps(r.get("data", {})).lower()]
    if g.tax_waits and not g.stall_observed and not tax_rej:
        ass["A5_no_softlock"] = "passed"
        notes.append(f"{len(g.tax_waits)} CombatTaxPayment wait(s); the acting "
                     f"player had an available submission every time; no "
                     f"tax answer rejected")
    else:
        ass["A5_no_softlock"] = "failed"
        notes.append(f"softlock signals: stall_observed={g.stall_observed} "
                     f"tax_rejections={len(tax_rej)}")
    if post is not None:
        slen = len(post.get("stack", []) or [])
        if slen == 0 and not g.stall_observed:
            ass["A6_cleanup"] = "passed"
            notes.append(f"post.json: stack empty, game proceeding "
                         f"(turn={post.get('turn_number')}, "
                         f"phase={post.get('phase')}, "
                         f"active=P{post.get('active_player')})")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"post.json stack={slen}, "
                         f"stall_observed={g.stall_observed}")
    else:
        ass["A6_cleanup"] = "failed"
        notes.append("post.json was never exported")
    if g.stall_observed:
        verdict = "reproduced"
        notes.append("verdict reproduced: CombatTaxPayment softlock signature "
                     "observed on v0.106.0 (a related failure on the generic "
                     "attempt, not the reporter's v0.6.0 board)")
    else:
        verdict = "blocked"
        notes.append("verdict blocked: report has no testable premise (no "
                     "board, tax card, mana situation, or branch; reporter "
                     "never answered the maintainer's 2026-07-19 questions). "
                     "Generic CombatTaxPayment path exercised on v0.106.0.")
    result = ("CombatTaxPayment decision surfaced and resolved on v0.106.0 "
              "with an available submission every time; no softlock."
              if verdict == "blocked" else
              "CombatTaxPayment softlock signature observed on v0.106.0.")
    return ass, notes, verdict, result


# ------------------------------------------------------------- summary render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 780
    BG = (18, 20, 26)
    PANEL = (26, 30, 38)
    TEXT = (235, 238, 245)
    DIM = (150, 160, 175)
    ACCENT = (110, 180, 255)
    GREEN = (110, 220, 140)
    RED = (240, 120, 120)
    YELLOW = (240, 200, 110)

    def load(p):
        with open(p) as f:
            return json.load(f)

    def oname(o):
        return o.get("card_name") or o.get("base_name") or o.get("name") or "?"

    def summarize_state(env):
        s = env["state"]
        objs = s.get("objects", {})
        lines = []
        for p in s.get("players", []):
            pid = p.get("id")
            hand = [oname(objs.get(str(o), {})) for o in p.get("hand", [])]
            bf = [o for o in objs.values()
                  if o.get("zone") == "Battlefield"
                  and str(o.get("controller")) == str(pid)]
            from collections import Counter
            bfc = Counter(oname(o) for o in bf)
            lines.append(
                f"P{pid} life {p.get('life')} | hand({len(hand)}): "
                + (", ".join(hand[:8]) + ("..." if len(hand) > 8 else "")))
            lines.append(
                "    battlefield: "
                + (", ".join(f"{k}x{v}" for k, v in sorted(bfc.items())) or "empty")
                + f" | library {len(p.get('library', []))}")
        return (f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
                f"active P{s.get('active_player')} | "
                f"stack {len(s.get('stack', []) or [])}"), lines

    evdir = os.path.dirname(out_path)
    pre = load(os.path.join(evdir, "pre.json"))
    post = load(os.path.join(evdir, "post.json")) if os.path.exists(
        os.path.join(evdir, "post.json")) else None
    srv = run["server"]
    ob = run.get("observations", {})

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), f"phase-rs/phase #4345 - Stuck decision: CombatTaxPayment",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol "
           f"{srv['protocol_version']}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']}", fill=DIM)
    y += 30
    d.text((24, y), "Setup: " + run.get("setup_line", ""), fill=TEXT)
    y += 24
    d.text((24, y), "Contract: " + run.get("contract_line", "")[:150], fill=DIM)
    y += 34

    panels = [("PRE (first taxed DeclareAttackers; P1 Prison out, Bear ready)", pre)]
    if post is not None:
        panels.append(("POST (after pay-mode + decline-mode taxed combats)", post))
    for tag, env in panels:
        d.rectangle([16, y, W - 16, y + 150], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), tag, fill=YELLOW)
        hdr, lines = summarize_state(env)
        d.text((28, y + 32), hdr, fill=TEXT)
        yy = y + 58
        for pl in lines[:4]:
            d.text((28, yy), pl[:150], fill=TEXT)
            yy += 22
        y += 162

    d.rectangle([16, y, W - 16, y + 150], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    yy = y + 34
    for k, v in run["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        yy += 22
    y += 162

    tw = ob.get("tax_waits", [])
    d.text((24, y),
           f"CombatTaxPayment waits: {len(tw)} | "
           f"pay offered+answered: {ob.get('pay_answered')} | "
           f"decline offered+answered: {ob.get('decline_answered')} | "
           f"stall: {ob.get('stall_observed')} | "
           f"P1 life {ob.get('life_pre')} -> {ob.get('life_after_pay')} -> "
           f"{ob.get('life_post')}", fill=DIM)
    y += 24
    d.text((24, y),
           ("Limitations: " + "; ".join(run.get("limitations", [])))[:150],
           fill=DIM)
    y += 24
    d.text((24, y), "sha manifest: manifest.sha256 | evidence: "
                    "ntindle/phase-bug-state-evidence 4345/<run-id>/", fill=DIM)
    img.save(out_path)
    print("wrote", out_path)



# ------------------------------------------------------------- server excerpts
def capture_server_excerpts(game_codes):
    """Grep the live server's log for this run's game codes plus any
    CastSpell 'action applied' lines (the silent-cast-drop diagnosis hook
    from the protocol-118/120 cast-confirmation guard lesson) and write
    server_excerpts.txt into the evidence dir. Called before the manifest
    is computed so the file is covered by manifest.sha256."""
    srv_log = os.path.expanduser(
        "~/workspace/dev/phase-backfill/runs/run-1488-reval-v01060-20261010-1441/server.log")
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


# ------------------------------------------------------------- main
async def _main():
    t_start = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
    say(f"starting issue #{ISSUE} run {RUN_ID}")
    await verify_server_hello()
    pe = parse_evidence()
    say("parse_evidence.json written (Ghostly Prison parse captured)")

    g = Game()
    await g.start()
    ok = await pump(g, GLOBAL_TIMEOUT)
    g.say(f"game finished ok={ok} decline_mode={g.decline_mode}")
    await g.close()

    if g.pre is not None and g.post is None:
        try:
            g.post = await g.export("post", g.p0)
            g.life_post = life_of(g.post, 1)
            g.note("post exported at loop end")
        except Exception as e:
            g.note(f"post final export failed: {e}")

    ass, notes, verdict, result = evaluate(g)
    dur = time.time() - t_start
    pay_waits = [w for w in g.tax_waits if w["mode"] == "pay"]
    decline_waits = [w for w in g.tax_waits if w["mode"] == "decline"]
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 126, "client": "driver/client.py",
                   "scenario": "driver/scenario_4345_01060.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_4345_01060.py", "rb").read()
        ).hexdigest(),
        "decks": {"P0": [list(x) for x in P0_DECK],
                  "P1": [list(x) for x in P1_DECK]},
        "observations": {
            "tax_waits": g.tax_waits,
            "n_tax_waits": len(g.tax_waits),
            "pay_answered": any(w["mode"] == "pay" for w in pay_waits),
            "decline_answered": any(w["mode"] == "decline" for w in decline_waits),
            "stall_observed": g.stall_observed,
            "rejections": g.rejections,
            "life_pre": g.life_pre,
            "life_after_pay": g.life_after_pay,
            "life_post": g.life_post,
            "pay_attack_turn": g.pay_attack_turn,
            "decline_turn": g.decline_turn,
            "first_tax_wait_s": (round(g.first_tax_at - t_start, 1)
                                 if g.first_tax_at else None),
        },
        "assertions": ass,
        "notes": notes + g.notes,
        "verdict": verdict,
        "result": result,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Grizzly Bears / 8x Ghostly Prison deck density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "The report names no tax card; Ghostly Prison ({2} per attacker) is "
            "the driver's chosen representative.",
            "Not tested on the original 2026-06-26 build v0.6.0; verdict is scoped "
            "to v0.106.0, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Shared pinned server on 127.0.0.1:9374 reused (ServerHello "
            "verified at run start); not an isolated per-run server process.",
        ],
        "setup_line": "P0: 12x Grizzly Bears + 48x Forest (mulligan to Bear+2 "
                      "lands, cast Bears, attack P1); P1: 8x Ghostly Prison + "
                      "52x Plains (mulligan to Prison+3 Plains, cast Prison, "
                      "never attacks/blocks)",
        "contract_line": "CombatTaxPayment surfaces when attacking a Prison "
                         "controller; paying {2} lets the attack proceed; "
                         "declining does not strand the game; every wait "
                         "offers an available submission",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(f"{BACKFILL}/driver/scenario_4345_01060.py",
                f"{EVDIR}/scenario_4345_01060.py")
    # server excerpts BEFORE logs close (say() writes to RUNLOG) and before
    # the manifest so the file is covered by it
    _codes = []
    try:
        _codes = [g.p0.game_code]
    except Exception:
        pass
    capture_server_excerpts(_codes)
    try:
        render_summary(run, f"{EVDIR}/summary.png")
        say("rendered summary.png")
    except Exception as e:
        say(f"summary render failed: {e}")
        notes.append(f"summary render failed: {e}")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            mf.write(f"{h}  {fn}\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


async def main():
    await _main()


if __name__ == "__main__":
    asyncio.run(main())
