#!/usr/bin/env python3
"""Issue #6557: Tamiyo, Seasoned Scholar +2 -- delayed -1/-0 trigger does not apply.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github #6557, status:confirmed): after Tamiyo, Inquisitive Student
transforms into Tamiyo, Seasoned Scholar and the +2 ("Until your next turn,
whenever a creature attacks you or a planeswalker you control, it gets -1/-0
until end of turn") is activated, the opponent's attacking creatures do not
get -1/-0.

Prior validation (ntindle, 2026-09-10, v0.78.0/build 4de7224/protocol 68,
maintained comment
https://github.com/phase-rs/phase/issues/6557#issuecomment-5617016250,
evidence commit f0aae358b25d5fedfa25864584c4dbaa808f6c43): REPRODUCED. The
delayed trigger FIRES on the attack (goes on the stack as a TriggeredAbility
carrying Pump -1/-0 and resolves), but the attacking Bear stays 2/2 and deals
the full 2 combat damage. Suspect: the nested Pump's target:SelfRef resolves
to Tamiyo (a planeswalker, no power/toughness) instead of the attacking
creature. This run re-validates on the pinned v0.85.0 (protocol 72) and
backfills the missing ledger entry.

Oracle text (verified from pinned v0.85.0 card-data.json, key
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
manifest.sha256, summary.png, scenario_6557_085.py, wire_log.jsonl,
scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260917-6557")
ISSUE = 6557
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = None
RUNLOG = None


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
    "server_version": "v0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "mode": "Full",
    "binary_sha256": "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f",
    "card_data_sha256": "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed",
    "draft_pools_sha256": "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e",
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


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


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
    await c.send_action(msg)


def mulligan_pending(state, seat):
    """True if `seat` has a pending Declare-phase MulliganDecision.

    Protocol 71+: MulliganDecision's waiting_for carries no top-level
    data.player; pending seats live in data.pending[] as
    {player, phase:{type:"Declare"}} entries. Gate the mulligan answer on the
    seat's presence in pending (a wf_player guard stalls both seats).
    """
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "MulliganDecision":
        return False
    pending = (wf.get("data") or {}).get("pending") or []
    if not pending:
        return True  # legacy shape; rely on the advertised action
    for p in pending:
        if p.get("player") == seat and (p.get("phase") or {}).get("type") == "Declare":
            return True
    return False


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


def load_env(evdir, tag):
    p = f"{evdir}/{tag}.json"
    if not os.path.exists(p):
        return None
    try:
        return json.loads(open(p).read())["state"]
    except Exception:
        return None


def attacking_bear_power(state):
    """Power of the bear currently flagged attacking, else None."""
    for oid in bf_ids(state, 1, BEAR):
        o = get_obj(state, oid)
        if o.get("attacking"):
            return num(o.get("power")), oid
    return None, None


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

async def main():
    init_logging()
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_plus2", "A3_weakened", "A4_wears_off",
            "A5_expired", "A6_cleanup")}
    p0 = PhaseClient("P0")
    # NOTE: client.connect() uses module-level URL from PHASE_WS_URL env;
    # launch with PHASE_WS_URL=ws://127.0.0.1:9375/ws
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}

    obs = {"tamiyo_cast": False, "div_cast": False, "plus2_submitted": False,
           "plus2_turn": None, "plus2_phase": None, "attack1": False,
           "attack1_turn": None, "attack1_bear": None, "attack2": False,
           "attack2_turn": None, "attack2_bear": None,
           "loyalty_pre": None, "loyalty_mid": None,
           "bear1_power_post": None, "bear1_life_post": None,
           "bear2_power_post2": None, "bear2_life_post2": None}
    exported = {"pre": False, "mid": False, "post": False, "p0next": False,
                "post2": False}
    submitted_interactions = set()
    discard_attempts = {}
    shapes_logged = set()
    last_assign = {}
    seen_trigger_ids = set()
    trigger_samples = []

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


    async def export_tag(c, tag):
        try:
            s = await c.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            exported[tag] = True
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
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
            notes.append(f"{tag} export failed: {e}")
            return None

    async def scan_interactions(st, who):
        """Protocol-72 interaction handling for this scenario.

        Only DiscardToHandSize needs an answer (schema opportunity over hand
        cards); every other opportunity shape is logged for diagnostics and
        left alone. Rejected submissions are retried (up to 3 attempts per
        interaction id) rather than marked answered, per the #4509 lesson:
        a rejected discard with a permanently-answered iid stalls the driver.
        """
        vi = st.get("viewer_interaction") or {}
        if not (vi.get("canSubmit") and (vi.get("opportunities") or [])):
            return False
        state = st.get("state", {}) or {}
        acted = False
        for opp in vi["opportunities"]:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            chs = data.get("choices") or data.get("candidates") or []
            key = (who, rtype, str(iid))
            if key not in shapes_logged:
                shapes_logged.add(key)
                wire("interaction_shape", {"who": who, "rtype": rtype,
                                           "interaction": opp})
            if iid in submitted_interactions:
                continue
            wftype = (state.get("waiting_for") or {}).get("type")
            if wftype == "DiscardToHandSize" and chs:
                if discard_attempts.get(iid, 0) >= 3:
                    continue
                spec = data.get("spec", {}) or {}
                stype = spec.get("type", "select")

                def cand_nm(cnd):
                    for s in cnd.get("surfaces", []) or []:
                        d = s.get("data", {}) or {}
                        ref = d.get("reference")
                        if ref is not None and str(d.get("role")) == "candidate":
                            return lname(state, ref)
                    return ""

                def ckey(cnd):
                    nm = cand_nm(cnd)
                    # P0: islands first, protect Divination, Tamiyo last.
                    # P1: forests first, bears last.
                    if who == "P0":
                        return (0 if nm == ISLAND else
                                1 if nm == DIV else 2, nm)
                    return (0 if nm == FOREST else 1, nm)

                pick = sorted(chs, key=ckey)[0]
                sub = {"interactionId": iid,
                       "response": {"type": stype,
                                    "data": {"choiceIds": [pick["id"]]}}}
                say(f"[{who}] discards to hand size: {cand_nm(pick)} "
                    f"(iid {iid}, attempt {discard_attempts.get(iid, 0) + 1})")
                wire("discard_submit", {"who": who, "submission": sub})
                c = p0 if who == "P0" else p1
                await c.send_interaction(sub)
                discard_attempts[iid] = discard_attempts.get(iid, 0) + 1
                acted = True
                await asyncio.sleep(0.5)
        return acted

    async def finish():
        dur = time.time() - t_start
        with open(f"{EVDIR}/obs.json", "w") as f:
            json.dump(obs, f, indent=1, default=str)
        verdict, eval_ass, eval_notes, eval_obs = evaluate(EVDIR, hints=obs)
        ass.update(eval_ass)
        notes.extend(eval_notes)
        obs.update(eval_obs)
        with open(f"{EVDIR}/trigger_samples.json", "w") as f:
            json.dump(trigger_samples, f, indent=1, default=str)
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": "runs/20260917-6557",
            "server_port": 9375,
            "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6557_085.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "trigger_samples": len(trigger_samples),
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
            ],
            "setup_line": "P0: 12x tamiyo inquisitive student + 12x divination + 36x island "
                          "(mulligan to Tamiyo + 2 islands; T1 Tamiyo; Divination -> "
                          "transform; +2 on main phase); P1: 20x grizzly bears + 40x "
                          "forest (attacks P0 with one bear after +2)",
            "contract_line": "Tamiyo +2: until P0's next turn, each creature attacking "
                             "P0 gets -1/-0 until end of turn (incl. on P1's turn)",
            "prior_validation": {
                "run": "2026-09-10-v0.78.0-6557d",
                "verdict": "reproduced",
                "comment_url": "https://github.com/phase-rs/phase/issues/6557#issuecomment-5617016250",
                "evidence_commit": "f0aae358b25d5fedfa25864584c4dbaa808f6c43",
            },
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": ass, "notes": notes,
                       "verdict": verdict}, f, indent=1)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    async def p0_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        turn = state.get("turn_number")
        phase = state.get("phase")
        stack = state.get("stack", []) or []
        # --- mulligan (gated on pending[] Declare entries) ---
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0") and mulligan_pending(state, 0):
            hn = hand_names(state, 0)
            mulls = kept.get("P0_mulls", 0)
            if (TAMIYO in hn and sum(1 for n in hn if n == ISLAND) >= 2) or mulls >= 3:
                kept["P0"] = True
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Keep"}}})
                say(f"P0 keeps (tamiyo={TAMIYO in hn}, mulls={mulls})")
            else:
                kept["P0_mulls"] = mulls + 1
                await submit_as_is(p0, {"type": "MulliganDecision",
                                        "data": {"choice": {"type": "Mulligan"}}})
                say(f"P0 mulligans #{mulls + 1}")
            return
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get("P0_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == 0:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hids = hand_ids(state, 0)
                tamiyo_n = sum(1 for x in hids if lname(state, x) == TAMIYO)

                def bottom_key(oid):
                    nm = lname(state, oid)
                    if nm == ISLAND:
                        return 0
                    if nm == TAMIYO and tamiyo_n > 1:
                        return 1
                    if nm == DIV:
                        return 2
                    return 3

                picks = sorted(hids, key=bottom_key)[:count]
                kept["P0_bottomed"] = True
                await submit_as_is(p0, {"type": "SelectCards",
                                        "data": {"cards": [int(x) for x in picks]}})
                say(f"P0 bottoms {count}")
                return
        # --- engine-advertised payments ---
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # --- trigger ordering: submit the advertised default ---
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
                return
        # --- interactions (discard-to-hand-size only) ---
        if await scan_interactions(st, "P0"):
            return
        # --- declare attackers: P0 never attacks in this scenario ---
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = json.loads(json.dumps(da.get("data", {})))
                d["attacks"] = []
                d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
                say(f"P0 declares no attackers (turn {turn})")
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = json.loads(json.dumps(da.get("data", {})))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return
        # --- mid checkpoint: +2 resolved ---
        if (obs["plus2_submitted"] and not exported["mid"]
                and tamiyo_pw_loyalty(state) == 4
                and len(stack) == 0
                and turn == obs["plus2_turn"]):
            say(f"MID: +2 resolved, loyalty 4 (turn {turn})")
            await export_tag(p0, "mid")
            return
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
            return
        if wtype == "AssignCombatDamage":
            aa = find_action(acts, "AssignCombatDamage")
            if aa and last_assign.get(0) != p0.revision:
                last_assign[0] = p0.revision
                d = json.loads(json.dumps(aa.get("data", {})))
                say("P0 AssignCombatDamage: submitting advertised default")
                wire("p0_assign_combat_damage", d)
                await p0.send_action({"type": "AssignCombatDamage", "data": d})
            return
        # Gate default PassPriority on my_priority (an out-of-turn pass is an
        # applied no-op that burns the per-revision guard and can deadlock).
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        main_phase = phase in ("PreCombatMain", "PostCombatMain")
        pw_ids = bf_ids(state, 0, TAMIYO_PW)
        # cast Tamiyo (front)
        if not obs["tamiyo_cast"]:
            ca = find_action(acts, "CastSpell", TAMIYO, state)
            if ca and main_phase:
                say(f"P0 casts Tamiyo (turn {turn})")
                wire("cast_tamiyo", ca)
                await submit_as_is(p0, ca)
                obs["tamiyo_cast"] = True
                return
        # cast Divination -> 3rd draw -> transform
        if not obs["div_cast"] and not pw_ids and obs["tamiyo_cast"]:
            ca = find_action(acts, "CastSpell", DIV, state)
            if ca and main_phase and len(untapped_lands(state, 0, ISLAND)) >= 3:
                say(f"P0 casts Divination (turn {turn})")
                wire("cast_divination", ca)
                await submit_as_is(p0, ca)
                obs["div_cast"] = True
                return
        # proof: activate the +2
        if pw_ids and not obs["plus2_submitted"] and main_phase:
            aa = find_plus2(acts, state)
            if aa:
                say(f"proof: exporting PRE, activating Tamiyo +2 "
                    f"(turn {turn} phase {phase})")
                await export_tag(p0, "pre")
                wire("activate_plus2", aa)
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
        pp = find_action(acts, "PassPriority")
        if pp:
            await submit_as_is(p0, pp)

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        turn = state.get("turn_number")
        phase = state.get("phase")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1") and mulligan_pending(state, 1):
            kept["P1"] = True
            await submit_as_is(p1, {"type": "MulliganDecision",
                                    "data": {"choice": {"type": "Keep"}}})
            say("P1 keeps opening hand")
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
            return
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
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
                return
        if await scan_interactions(st, "P1"):
            return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                bears = bf_ids(state, 1, BEAR)
                sub = json.loads(json.dumps(da))
                want_attack = False
                tag = None
                if bears and obs["plus2_submitted"]:
                    if not obs["attack1"]:
                        want_attack, tag = True, "attack1"
                    elif exported["p0next"] and not obs["attack2"]:
                        want_attack, tag = True, "attack2"
                if want_attack:
                    sub["data"]["attacks"] = [[bears[0], {"type": "Player", "data": 0}]]
                    sub["data"]["bands"] = []
                    await p1.send_action({"type": "DeclareAttackers",
                                          "data": sub["data"]})
                    obs[tag] = True
                    obs[f"{tag}_turn"] = turn
                    obs[f"{tag}_bear"] = bears[0]
                    say(f"P1 {tag}: attacks P0 with Bear oid {bears[0]} "
                        f"(turn {turn})")
                    wire(f"p1_{tag}", {"attacker": bears[0], "turn": turn})
                else:
                    sub["data"]["attacks"] = []
                    sub["data"]["bands"] = []
                    await p1.send_action({"type": "DeclareAttackers",
                                          "data": sub["data"]})
            return
        if wtype == "DeclareBlockers":
            return
        if wtype == "AssignCombatDamage":
            aa = find_action(acts, "AssignCombatDamage")
            if aa and last_assign.get(1) != p1.revision:
                last_assign[1] = p1.revision
                d = json.loads(json.dumps(aa.get("data", {})))
                say("P1 AssignCombatDamage: submitting advertised default")
                wire("p1_assign_combat_damage", d)
                await p1.send_action({"type": "AssignCombatDamage", "data": d})
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        # cast bears when able (until both attacks are done)
        if not obs["attack1"] or not obs["attack2"]:
            ca = find_action(acts, "CastSpell", BEAR, state)
            if ca and phase in ("PreCombatMain", "PostCombatMain"):
                await submit_as_is(p1, ca)
                return
        la = find_action(acts, "PlayLand")
        if la and player_of(state, 1).get("lands_played_this_turn", 0) == 0:
            await submit_as_is(p1, la)
            return
        pp = find_action(acts, "PassPriority")
        if pp:
            await submit_as_is(p1, pp)

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_deadline = None
    done = False
    while time.time() - t0 < 1200 and not done:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
            try:
                sample_triggers(c.latest["state"])
            except Exception:
                pass
        if exported["post2"]:
            say("post2 exported; finishing")
            await finish()
            done = True
            break
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_names(s, 0)[:6]} "
                f"pw_loyalty={tamiyo_pw_loyalty(s)} life={life_of(s, 0)}/{life_of(s, 1)} "
                f"stack={len(s.get('stack') or [])} plus2={obs['plus2_submitted']} "
                f"atk1={obs['attack1']} atk2={obs['attack2']} "
                f"exp={json.dumps(exported)}")
        if obs["plus2_submitted"] and not exported["post2"] and stuck_deadline is None:
            stuck_deadline = time.time() + 420
        if not obs["plus2_submitted"] or exported["post2"]:
            stuck_deadline = None
        if stuck_deadline and time.time() > stuck_deadline:
            notes.append("+2 submitted but full attack cycle not completed in "
                         "420s; see wire log (possible unhandled interaction)")
            await finish()
            done = True
            break
    if not done:
        notes.append("global timeout (1200s) hit before assertions resolved")
        await finish()


if __name__ == "__main__":
    asyncio.run(main())
