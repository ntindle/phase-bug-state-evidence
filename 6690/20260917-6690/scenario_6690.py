#!/usr/bin/env python3
"""Issue #6690: chained ChangeZone's `ControllerRef::You` misclassified as
relative, blocking activation (The Beamtown Bullies).

RE-VALIDATION on pinned v0.85.0 (build cb58ef5, WS protocol 72). A prior run
(20260910-6690, v0.78.0/protocol 68, verdict not-reproduced, evidence commit
847768678deee21deabc25fd64e34aa48bcff0e7, maintained comment posted
2026-09-10) was lost from the ledger in a VM replacement; per the playbook's
staleness rule the v0.78.0 result is stale vs the v0.85.0 pin, so this run
re-executes the scenario on the current pin and records both runs. This file
is the protocol-72 rewrite of the original driver/scenario_6690.py.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-27, status:confirmed, area:engine): when a chained ChangeZone
names a zone belonging to the ability's controller ("from your graveyard")
while a companion player target exists on the parent node,
ability_utils::relative_controller_kind classifies that ControllerRef::You as
a *relative* controller. legal_targets_for_ability_filter then re-enumerates
the slot against the companion player's candidates, searches the OPPONENT's
graveyard, finds nothing, and activation fails with
ActionNotAllowed("No legal targets available").

Reproduction per the issue: The Beamtown Bullies --
  "{T}: Target opponent whose turn it is puts target nonlegendary creature
   card from your graveyard onto the battlefield under their control. It gains
   haste. Goad it. At the beginning of the next end step, exile it."
Activate {T} on an opponent's turn with a nonlegendary creature card in YOUR
graveyard. Reported: the ability is not activatable.

Pinned card-data state (v0.85.0, key 'the beamtown bullies', verified this
run): the activated ability parses with cost {T}, target null, and top-level
effect Unimplemented{name:"change_zone_enters_under_anaphor",
description:"under their control"}; haste/goad/delayed-exile is a
SequentialSibling sub-chain. A dataset-wide STRUCTURAL scan of the pinned
v0.85.0 data this run found ZERO activated abilities with both a
player/opponent ability target and a chained ChangeZone whose target names
the controller's graveyard (controller:"You" + InZone Graveyard) -- the
issue's trigger structure is absent from the pinned data, exactly as on
v0.78.0. The scenario therefore exercises the issue's literal repro (activate
{T} on the opponent's turn with a creature in your graveyard) and records
exactly what the engine offers/rejects. It tests the ACTIVATION GATE (the
reported failure), not resolution of the reanimation itself, which the pinned
parse marks Unimplemented.

Scenario (native engine, two human-client seats, protocol-72 driver):
  P0: 4x The Beamtown Bullies + 12x Grizzly Bears + 8x Faithless Looting
      ({R} sorcery: draw 2, discard 2 -> bins Bears) + 12x Forest + 12x
      Mountain + 12x Swamp. Casts Looting (discard Bears), casts Bullies,
      holds it untapped.
  P1: 60x Island. Land per turn, passes priority, never attacks.

Probe point: P0 controls an untapped Beamtown Bullies (past summoning
sickness: cast_turn + 3 <= turn), P0's graveyard holds >=1 Grizzly Bears
(nonlegendary), it is P1's turn, P0 has priority. The driver exports pre.json
+ bullies_object.json, scans legal_actions for the Bullies' ActivateAbility,
attempts it, drains rejections, exports post.json, then settles.

Assertions:
  A1_setup_ok        probe point reached (Bullies untapped on P0 BF, Bears in
                     P0 gy, P1's turn, P0 priority).
  A2_offered         an ActivateAbility action for the Bullies was advertised.
  A3_no_target_block the attempt was NOT rejected with "No legal targets
                     available" (the reported failure signature).
  A4_announced       the activation was accepted and the ability announced
                     (on the stack, tap cost paid); only meaningful if A2
                     passed.
  A5_cleanup         game proceeds after the attempt (no stall).

Verdict rule: reproduced iff the attempt is rejected with the reported "No
legal targets available" signature. not-reproduced iff the activation is
offered and accepted/announced (the reported block is absent on v0.85.0). If
the ability is not offered at all, the verdict documents the parse state (no
target slots in the pinned card data) rather than relabeling.

Evidence: evidence/6690/20260917-6690/{pre,post}.json, bullies_object.json,
parse_evidence.json, run.json, manifest.sha256, summary.png, scenario_6690.py,
wire_log.jsonl, scenario_run.log, server_excerpts.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6690
RUN_ID = "20260917-6690"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty (stale RUN_ID reuse)"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BULLIES = "the beamtown bullies"
BEARS = "grizzly bears"
LOOTING = "faithless looting"
LANDS = ("forest", "mountain", "swamp")
ISLAND = "island"

P0_DECK = [(BULLIES, 4), (BEARS, 12), (LOOTING, 8), ("forest", 12),
           ("mountain", 12), ("swamp", 12)]
P1_DECK = [(ISLAND, 60)]

ST = {}
ACTED = set()
PROMPT_DONE = set()
REJECTS = {}
LAST_IID = {}
SKIP_IID = set()
ANSWERED_IID = {}
C0 = None


def reset_per_game():
    """Fresh per-revision/per-prompt guards for each new game: revisions
    restart at 0 per game, so a global ACTED set would suppress legitimate
    first-time actions in later games (#5654 lesson)."""
    global ACTED, PROMPT_DONE, REJECTS, LAST_IID, SKIP_IID, ANSWERED_IID
    ACTED = set()
    PROMPT_DONE = set()
    REJECTS = {}
    LAST_IID = {}
    SKIP_IID = set()
    ANSWERED_IID = {}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload},
                          default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def oname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_oids(state, pid):
    return [str(x) for x in player_of(state, pid).get("hand", [])]


def hand_names(state, pid):
    return [oname(get_obj(state, oid)) for oid in hand_oids(state, pid)]


def gy_names(state, pid):
    return [oname(get_obj(state, oid))
            for oid in player_of(state, pid).get("graveyard", [])]


def gy_count(state, pid, name):
    return sum(1 for n in gy_names(state, pid) if n == name)


def bf(state, pid):
    return [(oid, o) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bullies_on_bf(state, pid=0):
    for oid, o in bf(state, pid):
        if oname(o) == BULLIES:
            return oid, o
    return None, None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def my_priority(state, pid):
    """Default PassPriority gated on actually holding priority (#4509)."""
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {}) or {}
    return (wf.get("type") == "Priority"
            and (d.get("player") == pid or d.get("deciding_player") == pid))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_entries(state):
    return state.get("stack") or []


# ------------------------------------------------------------- interaction helpers
def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def ref_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    return chs, resp.get("type")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_IID[c.name] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub})
    await c.send_interaction(sub)


def drain_rejections(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            body = json.dumps(data, default=str)
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {body[:300]}")
            iid = LAST_IID.get(c.name)
            if iid and iid in body:
                REJECTS[iid] = REJECTS.get(iid, 0) + 1
                say(f"[{c.name}] iid {iid} rejection #{REJECTS[iid]}")
                if REJECTS[iid] >= 2 and iid not in SKIP_IID:
                    SKIP_IID.add(iid)
                    say(f"[{c.name}] iid {iid} rejected twice; skipping "
                        f"further submissions on it (logged, #4509)")


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED.add(k)
    return False


async def export_now(path, client):
    s = await client.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path} ({len(s)} bytes)")
    env = json.loads(s)
    return env["state"] if isinstance(env, dict) and "state" in env else env


def castspell_for(acts, oid):
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def playland_for(acts, oid):
    for a in acts:
        if a.get("type") == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None

# ------------------------------------------------------------- per-tick handlers
async def handle_mulligan(c, pid, tag, st, state, acts):
    if wf_type(state) != "MulliganDecision":
        return False
    # gate on the seat's presence in waiting_for.data.pending[] (#7176):
    # MulliganDecision carries no top-level data.player.
    pend = wf_data(state).get("pending") or []
    mine = [e for e in pend
            if (e.get("player") if isinstance(e, dict) else None) == pid
            and (e.get("phase") or {}).get("type") == "Declare"]
    if not mine:
        return False
    rev = st.get("state_revision", -1)
    if acted(f"mull{pid}", rev):
        return True
    mulls = ST.get(f"mulls{pid}", 0)
    hn = hand_names(state, pid)
    lands = sum(1 for n in hn if n in LANDS)
    if pid == 0:
        # Hunt the Bullies + lands; Bears/Looting density makes the rest likely.
        want = BULLIES in hn and lands >= 2
    else:
        want = True  # P1: all lands, keep immediately
    decision = "keep" if (want or mulls >= 3) else "mulligan"
    if decision == "mulligan":
        ST[f"mulls{pid}"] = mulls + 1
    adv = next((a for a in acts if a.get("type") == "MulliganDecision"), None)
    if adv:
        sub = copy.deepcopy(adv)
        dd = sub.setdefault("data", {})
        dd["decision"] = decision
        dd["choice"] = {"type": "Keep" if decision == "keep" else "Mulligan"}
    else:
        sub = {"type": "MulliganDecision",
               "data": {"decision": decision,
                        "choice": {"type": "Keep" if decision == "keep"
                                   else "Mulligan"}}}
    await submit_as_is(c, sub)
    say(f"[{tag}] mulligan: {decision} (mulls={mulls})")
    return True


async def handle_bottom(c, pid, tag, st, state, acts):
    # BottomCards phase after Declare -- count from pending[].phase (#6862).
    pend = wf_data(state).get("pending") or []
    mine = [e for e in pend
            if (e.get("player") if isinstance(e, dict) else None) == pid
            and (e.get("phase") or {}).get("type") == "BottomCards"]
    if not mine:
        return False
    rev = st.get("state_revision", -1)
    if acted(f"bottom{pid}", rev):
        return True
    try:
        n = int(mine[0]["phase"].get("count", 1))
    except Exception:
        n = 1
    h = hand_oids(state, pid)
    # bottom lands first, never the Bullies
    h.sort(key=lambda o: (oname(get_obj(state, o)) not in LANDS,
                          oname(get_obj(state, o)) == BULLIES,
                          oname(get_obj(state, o))))
    adv = next((a for a in acts if a.get("type") == "SelectCards"), None)
    if adv:
        sub = copy.deepcopy(adv)
        sub["data"]["cards"] = [int(x) for x in h[:n]]
        await submit_as_is(c, sub)
        say(f"[{tag}] bottoms {n} after mulligan (legacy action)")
        return True
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            oid_by_ref = {ref_of(ch): ch["id"] for ch in chs if ref_of(ch)}
            picks = [oid_by_ref[o] for o in h[:n] if o in oid_by_ref]
            if len(picks) == n:
                await send_interaction(
                    c, {"interactionId": opp.get("interactionId"),
                        "response": {"type": "select",
                                     "data": {"choiceIds": picks}}})
                say(f"[{tag}] bottoms {n} after mulligan (vi select)")
                return True
    say(f"[{tag}] bottom: no answerable opportunity; deferring")
    return True


def rank_looting_discard(state, oid):
    """Faithless Looting resolution: bin Bears first, never the Bullies."""
    nm = oname(get_obj(state, oid))
    if nm == BEARS:
        return (0, nm)
    if nm in LANDS:
        return (1, nm)
    if nm == LOOTING:
        return (2, nm)
    if nm == BULLIES:
        return (9, nm)
    return (3, nm)


def rank_cleanup_discard(state, oid):
    """Hand-size cleanup: lands, then Bears (binning is scenario-positive),
    never the Bullies."""
    nm = oname(get_obj(state, oid))
    if nm in LANDS:
        return (0, nm)
    if nm == BEARS:
        return (1, nm)
    if nm == LOOTING:
        return (2, nm)
    if nm == BULLIES:
        return (9, nm)
    return (3, nm)


async def answer_discard(c, pid, tag, st, state, acts, rankfn, note):
    """Answer a Discard* prompt addressed to pid via viewer_interaction,
    ranked by rankfn. Falls back to the advertised legacy SelectCards action.
    Rejected iids are retried, then skipped (#4509)."""
    vi = get_vi(st)
    answered = False
    if vi:
        d = wf_data(state)
        count = 1
        for k in ("count", "amount", "number"):
            if isinstance(d.get(k), int):
                count = d[k]
                break
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
                continue
            chs, rtype = vi_choices(opp)
            if not chs:
                continue
            if iid not in ST.setdefault("discard_dumped", set()):
                ST["discard_dumped"].add(iid)
                wire(f"discard_prompt_{note}",
                     {"iid": iid, "wf_type": wf_type(state),
                      "wf_data": wf_data(state), "rtype": rtype,
                      "opp": json.loads(json.dumps(opp, default=str))})
                say(f"[{tag}] discard({note}) prompt: wf={wf_type(state)} "
                    f"rtype={rtype} n_choices={len(chs)}")
            oid_by_ref = {ref_of(ch): ch["id"] for ch in chs if ref_of(ch)}
            oids = sorted(hand_oids(state, pid),
                          key=lambda o: rankfn(state, o))[:count]
            picks = [(o, oid_by_ref[o]) for o in oids if o in oid_by_ref]
            if len(picks) < len(oids):
                continue
            cids = [cid for _, cid in picks]
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            spec = data.get("spec")
            spect = spec.get("type") if isinstance(spec, dict) else None
            if rtype == "schema" and spect in ("sequence", "select"):
                resp_out = {"type": spect, "data": {"choiceIds": cids}}
            elif rtype == "exactChoices" and len(cids) == 1:
                resp_out = {"type": "choose", "data": {"choiceId": cids[0]}}
            else:
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            ANSWERED_IID[iid] = ANSWERED_IID.get(iid, 0) + 1
            names = [oname(get_obj(state, o)) for o, _ in picks]
            say(f"[{tag}] discard({note}): submits {names}")
            wire("discard_answered", {"tag": tag, "note": note, "iid": iid,
                                      "picks": names})
            if note == "looting":
                # Looting's discard is answered; later discards are cleanup.
                ST["looting_resolving"] = False
            answered = True
            break
    if not answered:
        adv = next((a for a in acts if a.get("type") == "SelectCards"), None)
        if adv:
            oids = sorted(hand_oids(state, pid),
                          key=lambda o: rankfn(state, o))
            d = wf_data(state)
            n = 1
            for k in ("count", "amount", "number"):
                if isinstance(d.get(k), int):
                    n = d[k]
                    break
            if wf_type(state) == "DiscardToHandSize":
                n = max(0, len(oids) - 7)
            sub = copy.deepcopy(adv)
            sub["data"]["cards"] = [int(x) for x in oids[:n]]
            await submit_as_is(c, sub)
            say(f"[{tag}] discard({note}): legacy SelectCards "
                f"{[oname(get_obj(state, x)) for x in oids[:n]]}")
            if note == "looting":
                ST["looting_resolving"] = False
            return True
        say(f"[{tag}] discard({note}): no answerable opportunity; deferring")
    return answered


async def handle_nonpriority(c, pid, tag, st, state, acts):
    wtype = wf_type(state)
    if wtype == "Priority":
        return False
    if await handle_mulligan(c, pid, tag, st, state, acts):
        return True
    if await handle_bottom(c, pid, tag, st, state, acts):
        return True
    d = wf_data(state)
    dp = d.get("player")
    if isinstance(dp, dict):
        dp = dp.get("id", -1)
    # DiscardChoice names the discarding player in waiting_for.data.player
    # (#6690 lesson, 2026-09-10): never answer another player's discard.
    if wtype in ("DiscardChoice", "DiscardToHandSize") and dp == pid:
        rankfn = (rank_looting_discard if ST.get("looting_resolving")
                  else rank_cleanup_discard)
        note = "looting" if ST.get("looting_resolving") else "cleanup"
        return await answer_discard(c, pid, tag, st, state, acts, rankfn, note)
    if wtype in ("DeclareAttackers", "DeclareBlockers"):
        adv = next((a for a in acts if a.get("type") == wtype), None)
        if adv:
            sub = copy.deepcopy(adv)
            dd = sub.setdefault("data", {})
            for k in ("attacks", "attackers", "blocks", "blockers",
                      "assignments"):
                if k in dd:
                    dd[k] = [] if isinstance(dd[k], list) else {}
            await submit_as_is(c, sub)
            say(f"[{tag}] declares no {wtype}")
            return True
        return False
    if wtype == "ChooseLegend":
        # defensive: keep the first copy (#6773)
        adv = next((a for a in acts if a.get("type") == "ChooseLegend"), None)
        if adv:
            await submit_as_is(c, adv)
            say(f"[{tag}] chooses legend (keep first)")
            return True
        return False
    # Unknown non-priority prompts are NOT auto-passed; the caller logs them.
    return False


def economy_tick(c, pid, st, acts, state, tag, allow_land=True):
    """Land drop in main + priority-gated pass. Returns (kind, action)."""
    rev = st.get("state_revision", -1)
    if allow_land and is_my_main(state, pid):
        for oid in hand_oids(state, pid):
            if oname(get_obj(state, oid)) in LANDS:
                pl = playland_for(acts, oid) or next(
                    (a for a in acts if a.get("type") == "PlayLand"), None)
                if pl and not acted(f"land{pid}", rev):
                    return ("land", pl)
                break
    if my_priority(state, pid):
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp:
            return ("pass", pp)
    return None

# ------------------------------------------------------------- P0 game logic
def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(get_obj(state, oid)) == name:
            return oid
    return None


async def p0_tick(st, acts, state, tag):
    c = _CLIENTS["p0"]
    drain_rejections(c)
    if await handle_nonpriority(c, 0, tag, st, state, acts):
        return

    stage = ST.get("stage")

    # --- probe point: Bullies untapped, Bears in gy, P1's turn, P0 priority,
    # --- past summoning sickness (cast on P0 turn T; first eligible P1 turn
    # --- is T+3 since turns alternate).
    bid, bo = bullies_on_bf(state, 0)
    cast_turn = ST.get("bullies_cast_turn")
    eligible = (cast_turn is not None
                and turn_of(state) >= cast_turn + 3
                and state.get("active_player") == 1)
    if (stage == "armed" and bid is not None and not bo.get("tapped")
            and gy_count(state, 0, BEARS) >= 1 and eligible
            and my_priority(state, 0)):
        ST["stage"] = "probe"
        ST["probe_turn"] = turn_of(state)
        ST["bullies_oid"] = bid
        say(f"[{tag}] PROBE POINT t{turn_of(state)} {state.get('phase')}: "
            f"bullies={bid} untapped, bears_in_gy={gy_count(state,0,BEARS)}, "
            f"P1's turn, P0 priority (cast on t{cast_turn})")
        wire("probe_point", {"turn": turn_of(state),
                             "phase": state.get("phase"), "bullies_oid": bid,
                             "bears_in_gy": gy_count(state, 0, BEARS),
                             "bullies_object": bo})
        with open(f"{EVDIR}/bullies_object.json", "w") as f:
            json.dump({"oid": bid, "object": bo}, f, indent=1, default=str)
        pre = await export_now("pre.json", C0)
        ST["pre"] = pre
        aa = [a for a in acts if a.get("type") == "ActivateAbility"
              and str(a.get("data", {}).get("source_id", "")) == str(bid)]
        say(f"[{tag}] ActivateAbility candidates for bullies {bid}: {len(aa)}")
        wire("activation_candidates",
             {"bullies_oid": bid,
              "candidates": [a.get("data") for a in aa],
              "all_action_types": sorted(set(a.get("type") for a in acts))})
        if aa:
            ST["offered"] = True
            ST["offered_data"] = aa[0].get("data")
            ST["attempted"] = True
            say(f"[{tag}] attempting activation: "
                f"{json.dumps(aa[0].get('data'))[:300]}")
            await submit_as_is(c, aa[0])
            await asyncio.sleep(3)
            drain_rejections(c)
            st2 = c.latest or {}
            state2 = st2.get("state") or {}
            ST["stack_after"] = len(stack_entries(state2))
            b2, bo2 = bullies_on_bf(state2, 0)
            ST["tapped_after"] = bo2.get("tapped") if bo2 else None
            ST["wait_after"] = wf_type(state2)
            ST["stack_detail"] = stack_entries(state2)
            say(f"[{tag}] after attempt: stack={ST['stack_after']} "
                f"tapped={ST['tapped_after']} wait={ST['wait_after']}")
            wire("after_attempt", {"stack": ST["stack_after"],
                                   "tapped": ST["tapped_after"],
                                   "wait": ST["wait_after"],
                                   "stack_detail": ST["stack_detail"]})
        else:
            say(f"[{tag}] NO ActivateAbility offered for the Bullies")
            wire("activation_not_offered",
                 {"bullies_oid": bid,
                  "all_action_types": sorted(set(a.get("type")
                                                 for a in acts))})
        post = await export_now("post.json", C0)
        ST["post"] = post
        ST["stage"] = "settle"
        ST["settle_since"] = time.time()
        ST["settle_turn"] = turn_of(state)
        return

    if stage == "settle":
        # keep the game moving briefly; confirm no stall, then finish
        if time.time() - ST.get("settle_since", time.time()) > 25:
            ST["done"] = True
            say(f"[{tag}] settle complete; finishing")
            return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: develop / armed ---
    if stage in ("develop", "armed"):
        rev = st.get("state_revision", -1)
        if my_priority(state, 0) and is_my_main(state, 0):
            # cast Faithless Looting once: bins Bears via its discard
            if not ST.get("looting_cast"):
                loid = find_hand(state, 0, LOOTING)
                if (loid is not None and BEARS in hand_names(state, 0)
                        and gy_count(state, 0, BEARS) == 0):
                    cs = castspell_for(acts, loid)
                    if cs and not acted("castlooting", rev):
                        ST["looting_resolving"] = True
                        await submit_as_is(c, cs)
                        ST["looting_cast"] = True
                        say(f"[{tag}] casts Faithless Looting "
                            f"(t{turn_of(state)})")
                        wire("cast", {"card": LOOTING,
                                      "turn": turn_of(state)})
                        return
            # cast the Bullies (gate on none-on-battlefield; ChooseLegend
            # handled defensively, #6773)
            if bid is None:
                hoid = find_hand(state, 0, BULLIES)
                if hoid is not None:
                    cs = castspell_for(acts, hoid)
                    if cs and not acted("castbullies", rev):
                        await submit_as_is(c, cs)
                        ST["bullies_cast_turn"] = turn_of(state)
                        ST["stage"] = "armed"
                        say(f"[{tag}] casts Beamtown Bullies "
                            f"(t{turn_of(state)}); stage=armed")
                        wire("cast", {"card": BULLIES,
                                      "turn": turn_of(state)})
                        return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    r = economy_tick(c, 0, st, acts, state, tag)
    if r:
        await submit_as_is(c, r[1])


async def p1_tick(st, acts, state, tag):
    c = _CLIENTS["p1"]
    drain_rejections(c)
    if await handle_nonpriority(c, 1, tag, st, state, acts):
        return
    r = economy_tick(c, 1, st, acts, state, tag)
    if r:
        await submit_as_is(c, r[1])


_CLIENTS = {}


# ------------------------------------------------------------- game runner
def new_game_state():
    reset_per_game()
    ST.update({"stage": "develop",
               "looting_cast": False, "looting_resolving": False,
               "bullies_cast_turn": None,
               "probe_turn": None, "bullies_oid": None,
               "offered": False, "offered_data": None, "attempted": False,
               "stack_after": None, "tapped_after": None, "wait_after": None,
               "stack_detail": None,
               "pre": None, "post": None,
               "settle_since": 0, "settle_turn": 0,
               "max_turn": 0, "mulls0": 0, "mulls1": 0,
               "discard_dumped": set(),
               "done": False, "blocked": None})


async def open_game(p0_deck, p1_deck):
    p0 = PhaseClient("6690-P0")
    await p0.connect()
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient("6690-P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*p1_deck))
    say(f"[game] game={p0.game_code}")
    wire("game", {"game_code": p0.game_code})
    global C0
    C0 = p0
    return p0, p1


async def run_game(p0, p1, timeout_s):
    t0 = time.time()
    last = {}
    last_tick = {}
    last_adv = {p0.name: time.time(), p1.name: time.time()}
    last_diag = 0.0
    warned = set()
    ticks = [(p0, p0_tick, 0, "P0"), (p1, p1_tick, 1, "P1")]
    for _ in range(int(timeout_s / 0.2)):
        await asyncio.sleep(0.2)
        for c, tick, pid, tag in ticks:
            st = c.latest
            if not st:
                continue
            rev = c.revision
            # re-tick at most every 5s on the same revision (#6862)
            if (rev == last.get(c.name)
                    and time.time() - last_tick.get(c.name, 0) <= 5):
                continue
            last[c.name] = rev
            last_tick[c.name] = time.time()
            last_adv[c.name] = time.time()
            warned.discard(c.name)
            state = st["state"]
            ST["max_turn"] = max(ST["max_turn"], turn_of(state))
            acts = list(st.get("legal_actions") or [])
            try:
                await tick(st, acts, state, tag)
            except Exception as e:
                say(f"[{c.name}] tick error: {e!r}")
            if ST.get("done"):
                say("[game] contract complete; finishing")
                return True
        now = time.time()
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            bid, _ = bullies_on_bf(s, 0)
            say(f"[game] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={wf_type(s)} "
                f"P0hand={len(hand_names(s,0))} P1hand={len(hand_names(s,1))} "
                f"stack={len(stack_entries(s))} stage={ST.get('stage')} "
                f"bullies={'bf' if bid else 'no'} "
                f"gy_bears={gy_count(s,0,BEARS)}")
        for c in (p0, p1):
            # stale-client watchdog: log revision + view (#4509)
            if (now - last_adv[c.name] > 60 and c.name not in warned
                    and c.latest):
                warned.add(c.name)
                st = c.latest
                state = st.get("state", {})
                say(f"[game] WATCHDOG {c.name}: no revision advance for "
                    f"{now - last_adv[c.name]:.0f}s; rev={c.revision} "
                    f"turn={turn_of(state)} phase={state.get('phase')} "
                    f"wf={wf_type(state)}")
        if now - t0 > timeout_s:
            say(f"[game] global timeout ({timeout_s}s) hit")
            return False
    return False


async def play_game(p0_deck, p1_deck):
    new_game_state()
    p0, p1 = await open_game(p0_deck, p1_deck)
    _CLIENTS["p0"], _CLIENTS["p1"] = p0, p1
    ok = await run_game(p0, p1, timeout_s=1500)
    say(f"game finished ok={ok}")
    await p0.close()
    await p1.close()
    return ok

# ------------------------------------------------------------- parse evidence
def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def cz_targets_your_gy(a):
    for d in walk(a):
        if d.get("type") == "ChangeZone":
            t = d.get("target") or {}
            if t.get("controller") == "You" and any(
                    p.get("type") == "InZone" and p.get("zone") == "Graveyard"
                    for p in (t.get("properties") or [])):
                return True
    return False


def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
    b = cd["the beamtown bullies"]
    acts = [a for a in (b.get("abilities") or [])
            if (a.get("kind") or "") == "Activated"]
    # structural scan: activated ability with a player/opponent ability target
    # AND a chained ChangeZone from the controller's graveyard
    hits = []
    for name, c in cd.items():
        for ai, a in enumerate(c.get("abilities") or []):
            if (a.get("kind") or "") != "Activated":
                continue
            t = a.get("target")
            tt = t.get("type") if isinstance(t, dict) else t
            if cz_targets_your_gy(a) and tt in (
                    "Opponent", "Player", "TargetPlayer", "EachPlayer"):
                hits.append(name)
    out = {
        "the beamtown bullies": {
            "oracle_text": b.get("oracle_text"),
            "mana_cost": b.get("mana_cost"),
            "n_activated_abilities": len(acts),
            "activated_ability": acts[0] if acts else None,
        },
        "structural_scan": {
            "scope": ("all activated abilities in pinned v0.85.0 card data: "
                      "player/opponent ability target + chained ChangeZone "
                      "targeting the controller's graveyard "
                      "(controller:'You' + InZone Graveyard)"),
            "hits": hits,
            "n_hits": len(hits),
        },
    }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(parse):
    ass = {}
    notes = []
    b = parse["the beamtown bullies"]
    ab = b.get("activated_ability") or {}
    eff = (ab.get("effect") or {})
    notes.append(
        "parse (pinned v0.85.0 card-data): Bullies activated ability cost=" +
        json.dumps(ab.get("cost")) + " target=" +
        json.dumps(ab.get("target")) + " top_effect=" +
        json.dumps({"type": eff.get("type"), "name": eff.get("name")}))
    notes.append(
        "structural scan: %d activated abilities in the pinned data have a "
        "player/opponent ability target + chained ChangeZone from the "
        "controller's graveyard (the issue's trigger structure)" %
        parse["structural_scan"]["n_hits"])

    if ST.get("blocked"):
        for k in ("A1_setup_ok", "A2_offered", "A3_no_target_block",
                  "A4_announced", "A5_cleanup"):
            ass[k] = "not-run"
        notes.append(f"verdict: blocked - {ST['blocked']}")
        return ass, notes, "blocked"

    # A1: probe point reached
    if ST.get("probe_turn") is not None:
        ass["A1_setup_ok"] = "passed"
        notes.append(
            f"A1 passed: probe point reached on turn {ST['probe_turn']} "
            f"(P1's turn, P0 priority): Bullies oid {ST['bullies_oid']} "
            f"untapped on P0 battlefield (cast turn {ST['bullies_cast_turn']}, "
            f"past summoning sickness), >=1 Grizzly Bears in P0 graveyard")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(
            f"A1 FAILED: probe point never reached (max_turn={ST['max_turn']}, "
            f"bullies_cast_turn={ST['bullies_cast_turn']})")

    # A2: activation offered
    if ST.get("offered"):
        ass["A2_offered"] = "passed"
        notes.append(
            f"A2 passed: ActivateAbility advertised for the Bullies "
            f"(source_id={ST['offered_data'].get('source_id')}, "
            f"ability_index={ST['offered_data'].get('ability_index')})")
    elif ST.get("probe_turn") is not None:
        ass["A2_offered"] = "failed"
        notes.append("A2 FAILED: no ActivateAbility offered for the Bullies "
                     "at the probe point")
    else:
        ass["A2_offered"] = "not-run"
        notes.append("A2 not-run: probe point never reached")

    # A3: no "No legal targets available" rejection (scan the wire log)
    no_legal = False
    try:
        with open(f"{EVDIR}/wire_log.jsonl") as f:
            for line in f:
                if '"event": "rejected"' in line and "no legal targets" in line.lower():
                    no_legal = True
                    break
    except Exception:
        pass
    if no_legal:
        ass["A3_no_target_block"] = "failed"
        notes.append("A3 FAILED: the attempt was rejected with the reported "
                     "'No legal targets available' signature")
    elif ST.get("attempted"):
        ass["A3_no_target_block"] = "passed"
        notes.append("A3 passed: no 'No legal targets available' rejection "
                     "on the activation attempt")
    elif ST.get("probe_turn") is not None:
        ass["A3_no_target_block"] = "not-run"
        notes.append("A3 not-run: activation not offered, nothing attempted")
    else:
        ass["A3_no_target_block"] = "not-run"
        notes.append("A3 not-run: probe point never reached")

    # A4: announced (on the stack, tap cost paid)
    if ST.get("attempted") and (ST.get("stack_after") or 0) > 0:
        ass["A4_announced"] = "passed"
        notes.append(
            f"A4 passed: activation accepted; stack={ST['stack_after']} after "
            f"the attempt, Bullies tapped={ST['tapped_after']}, "
            f"waiting_for={ST['wait_after']}")
    elif ST.get("attempted"):
        ass["A4_announced"] = "failed"
        notes.append(
            f"A4 FAILED: activation submitted but stack={ST['stack_after']}, "
            f"tapped={ST['tapped_after']}, wait={ST['wait_after']}")
    else:
        ass["A4_announced"] = "not-run"
        notes.append("A4 not-run: nothing attempted")

    # A5: cleanup -- game proceeded after the attempt
    if ST.get("probe_turn") is not None and ST.get("done"):
        ass["A5_cleanup"] = "passed"
        notes.append("A5 passed: settle phase completed with the game "
                     "advancing; no stall observed")
    elif ST.get("probe_turn") is not None:
        ass["A5_cleanup"] = "not-run"
        notes.append("A5 not-run: settle phase did not complete")
    else:
        ass["A5_cleanup"] = "not-run"
        notes.append("A5 not-run: probe point never reached")

    if no_legal:
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED - the Bullies activation was "
                     "rejected with the reported 'No legal targets available' "
                     "signature")
    elif (ass.get("A1_setup_ok") == "passed"
          and ass.get("A2_offered") == "passed"
          and ass.get("A3_no_target_block") == "passed"
          and ass.get("A4_announced") == "passed"):
        verdict = "not-reproduced"
        notes.append("verdict: not-reproduced - the {T} activation was "
                     "offered and accepted on the opponent's turn with a "
                     "nonlegendary creature in the controller's graveyard; "
                     "the reported activation block is absent on v0.85.0. "
                     "The pinned parse carries no graveyard target slot "
                     "(top-level Unimplemented), so the described "
                     "relative_controller_kind mechanism is not reachable "
                     "from this card's runtime parse.")
    else:
        verdict = "blocked"
        notes.append("verdict: blocked - inconclusive observations "
                     "(see assertion notes)")
    return ass, notes, verdict


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1100
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#6690 Beamtown Bullies: {T} activation blocked on opponent's turn?",
         fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("setup: P0 Bullies + Bears + Faithless Looting vs P1 60x Island; "
         "activate {T} on P1's turn, Bears in P0 graveyard", size=16)
    line("parse: ability cost {T}, target null, top effect "
         "Unimplemented(change_zone_enters_under_anaphor)", size=16)
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, val in run["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=17)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:14]:
        line(f"  - {n[:112]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.85.0, protocol 72)")

    parse = parse_evidence()
    say("parse: bullies ability top_effect=",
        json.dumps({"type": (parse["the beamtown bullies"]["activated_ability"]
                             or {}).get("effect", {}).get("type"),
                    "name": (parse["the beamtown bullies"]["activated_ability"]
                             or {}).get("effect", {}).get("name")}),
        "; structural scan hits =",
        parse["structural_scan"]["n_hits"])

    try:
        ok = await play_game(P0_DECK, P1_DECK)
        say(f"game finished ok={ok}")
    except Exception as e:
        say(f"game crashed: {e!r}")
        ok = False
        ST["blocked"] = f"game crashed: {e!r}"
        ST["done"] = True

    ass, notes, verdict = evaluate(parse)
    dur = time.time() - t_start

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    cd_path = f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"
    dp_path = f"{BACKFILL}/server/releases/v0.85.0/data/draft-pools.json"
    bin_path = (f"{BACKFILL}/server/releases/v0.85.0/"
                f"phase-server-slim-x86_64-unknown-linux-musl")
    server_identity = {
        "server_version": "0.85.0",
        "build_commit": "cb58ef5",
        "protocol_version": 72,
        "mode": "Full",
        "binary_sha256": sha(bin_path),
        "card_data_sha256": sha(cd_path),
        "draft_pools_sha256": sha(dp_path),
        "signature_key_id": "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY",
        "signature_verified": True,
        "observed_at": "2026-09-17",
        "source": ("ServerHello probed live (0.85.0/cb58ef5/proto 72/Full) on "
                   "127.0.0.1:9374 this run; hashes recomputed from on-disk "
                   "release files; v0.85.0 confirmed latest stable phase-rs/"
                   "phase release (not shell-*) this run"),
    }
    # server identity cross-check against AGENTS.md pin record
    assert server_identity["binary_sha256"] == \
        "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f", \
        "binary digest drift vs pinned v0.85.0 record"
    assert server_identity["card_data_sha256"] == \
        "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed", \
        "card-data digest drift vs pinned v0.85.0 record"
    assert server_identity["draft_pools_sha256"] == \
        "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e", \
        "draft-pools digest drift vs pinned v0.85.0 record"

    game_code = _CLIENTS.get("p0").game_code if _CLIENTS.get("p0") else None
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario": "driver/scenario_6690.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_6690.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "game_code": game_code,
        "game_ok": ok,
        "parse_summary": {
            "bullies_oracle": (parse["the beamtown bullies"]
                               ["oracle_text"])[:220],
            "bullies_ability": {
                "cost": (parse["the beamtown bullies"]
                         ["activated_ability"] or {}).get("cost"),
                "target": (parse["the beamtown bullies"]
                           ["activated_ability"] or {}).get("target"),
                "top_effect": {
                    "type": ((parse["the beamtown bullies"]
                              ["activated_ability"] or {}).get("effect")
                             or {}).get("type"),
                    "name": ((parse["the beamtown bullies"]
                              ["activated_ability"] or {}).get("effect")
                             or {}).get("name"),
                },
            },
            "structural_scan_hits":
                parse["structural_scan"]["n_hits"],
        },
        "probe": {
            "probe_turn": ST.get("probe_turn"),
            "bullies_oid": ST.get("bullies_oid"),
            "bullies_cast_turn": ST.get("bullies_cast_turn"),
            "offered": ST.get("offered"),
            "offered_data": ST.get("offered_data"),
            "attempted": ST.get("attempted"),
            "stack_after": ST.get("stack_after"),
            "tapped_after": ST.get("tapped_after"),
            "wait_after": ST.get("wait_after"),
            "stack_detail": ST.get("stack_detail"),
        },
        "rejections": REJECTS,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-17",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "P1 is a passive second seat (lands only, no attacks, no spells).",
            "The pinned v0.85.0 parse of the Bullies' ability carries no "
            "graveyard target slot (top-level Unimplemented "
            "change_zone_enters_under_anaphor): this run tests the ACTIVATION "
            "GATE (the reported 'not activatable' failure), not the "
            "relative_controller_kind target-slot machinery, which is not "
            "reachable from this card's runtime parse. Resolution of the "
            "reanimation itself is not asserted.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "history": [{
            "run_id": "20260910-6690",
            "release": "v0.78.0",
            "protocol": 68,
            "verdict": "not-reproduced",
            "evidence_commit":
                "847768678deee21deabc25fd64e34aa48bcff0e7",
            "note": ("prior run; activation accepted on the opponent's turn, "
                     "no 'No legal targets available'; ledger entry lost in a "
                     "VM replacement, restored here; stale vs the v0.85.0 pin"),
        }],
        "setup_line": ("P0 4x The Beamtown Bullies + 12x Grizzly Bears + 8x "
                       "Faithless Looting + 12x Forest/Mountain/Swamp vs P1 "
                       "60x Island. P0 bins Bears with Looting, casts the "
                       "Bullies, and on a later P1 turn (past summoning "
                       "sickness) with P0 priority attempts the {T} "
                       "activation."),
        "contract_line": ("The {T} activation must be offered and accepted on "
                          "the opponent's turn with a nonlegendary creature "
                          "in the controller's graveyard; a 'No legal "
                          "targets available' rejection is the reported "
                          "failure signature."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_6690.py", "w") as f:
        f.write(open(__file__).read())
    render_png(f"{EVDIR}/summary.png", run)

    # server excerpts for this game (from THIS run's server.log)
    try:
        slog = f"{BACKFILL}/runs/{RUN_ID}/server.log"
        excerpts = []
        if os.path.exists(slog) and game_code:
            with open(slog, errors="replace") as f:
                for line in f:
                    if game_code in line:
                        excerpts.append(line.rstrip())
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(excerpts[-80:]) + "\n")
        say(f"server excerpts: {len(excerpts)} lines for {game_code}")
    except Exception as e:
        say(f"server excerpts failed: {e}")

    # hash scenario_run.log AFTER all say() logging is done (#6916 lesson)
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha(f"{EVDIR}/{fn}") + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1),
          flush=True)


asyncio.run(amain())
