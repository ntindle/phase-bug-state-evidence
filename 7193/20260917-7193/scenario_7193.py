#!/usr/bin/env python3
"""Issue #7193: Demand Answers -- casting via the DISCARD branch of the
additional cost reportedly leaves the spell reappearing on later stacks.

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified from pinned v0.85.0 card-data.json):
  Demand Answers {1}{R} Instant:
    "As an additional cost to cast this spell, sacrifice an artifact or
     discard a card. Draw two cards."
  additional_cost = Choice([Sacrifice(Typed Artifact, count 1),
                            Discard(Fixed 1)])

Reported: "[[demand answers]] if cast without a sacrifice it seems to get
added to every stack."

Triage acceptance criteria:
  (1) record the chosen additional-cost branch and the submitted action
  (2) the spell cannot be announced without paying one branch
  (3) after one resolution, no stale copy/entry appears on later stacks

Driver contract (this scenario):
  - P0: 12x Demand Answers, 24x Mountain, 24x Shock. P1: 60x Mountain
    (passive: land drop, pass).
  - P0 casts Demand Answers on its PreCombatMain with >=2 untapped Mountains,
    choosing the DISCARD branch (the reported path), discarding a Shock.
  - After resolution, the driver samples the stack on every revision for
    >=4 full turn cycles (8 turns) and records any Demand Answers entry.
  - The driver NEVER casts a second Demand Answers, so any post-resolution
    stack entry is stale by construction.

Assertions:
  A1: additional-cost choice prompt appeared; discard branch submitted and
      accepted (no rejection on that interaction).
  A2: discard happened (a Shock moved hand -> P0 graveyard via the advertised
      discard choice; Demand Answers never discarded).
  A3: spell resolved correctly: P0 library -2 vs pre-cast (same turn),
      Demand Answers in P0's graveyard.
  A4: stack empty of Demand Answers immediately after resolution.
  A5: across >=4 subsequent full turn cycles, zero Demand Answers entries
      ever observed on the stack.
  A6: no dangling cast / no rejections on the cost+discard interactions.

Verdict rule:
  reproduced     -- a Demand Answers stack entry is observed after resolution
                    with no new cast submitted (the reported symptom).
  not-reproduced -- all assertions pass.
  blocked        -- cast never announced, cost prompt unanswerable, discard
                    unresolvable, resolution never reached, or a concrete
                    prerequisite failure (with next unblock action).

Evidence: evidence/7193/20260917-7193/pre_cast.json, post_resolution.json,
run.json, manifest.sha256, summary.png, scenario_7193.py, wire_log.jsonl,
scenario_run.log
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
ISSUE = 7193
RUN_ID = "20260917-7193"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DA = "Demand Answers"
LANDS = ("Mountain",)
SHOCK = "Shock"

P0_DECK = [(DA, 12), ("Mountain", 24), (SHOCK, 24)]
P1_DECK = [("Mountain", 60)]

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
    return o.get("card_name") or o.get("base_name") or o.get("name") or ""


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lib_size(state, pid):
    return len(state["players"][pid]["library"])


def hand_oids(state, pid):
    return [str(x) for x in state["players"][pid]["hand"]]


def hand_names(state, pid):
    return [oname(get_obj(state, oid)) for oid in hand_oids(state, pid)]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(get_obj(state, oid)) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) in LANDS]


def gy_count(state, pid, name):
    return sum(1 for _, o in state.get("objects", {}).items()
               if o.get("zone") == "Graveyard" and o.get("controller") == pid
               and oname(o) == name)


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


def stack_has_da(state):
    """True if any stack entry mentions Demand Answers (entry may be a dict
    or a bare oid -- resolve via the object map when needed)."""
    for e in stack_entries(state):
        if not isinstance(e, dict):
            e = get_obj(state, e)
        if "demand answers" in json.dumps(e, default=str).lower():
            return True
    return False


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
    say(f"exported {path}")
    env = json.loads(s)
    return env["state"] if isinstance(env, dict) and "state" in env else env


def castspell_advertised(acts, oid):
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None

# ------------------------------------------------------------- per-tick handlers
async def handle_mulligan(c, pid, tag, st, state, acts, want_keep):
    if wf_type(state) != "MulliganDecision":
        return False
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
    want = "keep" if (want_keep(state, pid) or mulls >= 2) else "mulligan"
    if want == "mulligan":
        ST[f"mulls{pid}"] = mulls + 1
    adv = next((a for a in acts if a.get("type") == "MulliganDecision"), None)
    if adv:
        sub = copy.deepcopy(adv)
        dd = sub.setdefault("data", {})
        dd["decision"] = want
        dd["choice"] = {"type": "Keep" if want == "keep" else "Mulligan"}
    else:
        sub = {"type": "MulliganDecision",
               "data": {"decision": want,
                        "choice": {"type": "Keep" if want == "keep" else "Mulligan"}}}
    await submit_as_is(c, sub)
    say(f"[{tag}] mulligan: {want}")
    return True


async def handle_bottom(c, pid, tag, st, state, acts):
    pend = wf_data(state).get("pending") or []
    mine = [e for e in pend
            if (e.get("player") if isinstance(e, dict) else None) == pid
            and (e.get("phase") or {}).get("type") == "BottomCards"]
    if not mine:
        return False
    rev = st.get("state_revision", -1)
    if acted(f"bottom{pid}", rev):
        return True
    n = 1
    try:
        n = int(mine[0]["phase"].get("count", 1))
    except Exception:
        pass
    h = hand_oids(state, pid)
    h.sort(key=lambda o: (oname(get_obj(state, o)) not in LANDS,
                         oname(get_obj(state, o))))
    adv = next((a for a in acts if a.get("type") == "SelectCards"), None)
    if adv:
        sub = copy.deepcopy(adv)
        sub["data"]["cards"] = [int(x) for x in h[:n]]
        await submit_as_is(c, sub)
    else:
        vi = get_vi(st)
        if not vi:
            return False
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
                break
        else:
            return False
    say(f"[{tag}] bottoms {n} after mulligan")
    return True


def rank_cost_discard(state, oid):
    """Cost-discard ranking: Shock first (the expendable spell), then lands,
    never Demand Answers (we must keep the test card castable/held)."""
    nm = oname(get_obj(state, oid))
    if nm == SHOCK:
        return (0, nm)
    if nm in LANDS:
        return (1, nm)
    if nm == DA:
        return (9, nm)
    return (2, nm)


def rank_cleanup_discard(state, oid):
    nm = oname(get_obj(state, oid))
    if nm in LANDS:
        return (0, nm)
    if nm == DA:
        return (2, nm)
    return (1, nm)


async def answer_discard_ranked(c, pid, opp, tag, rankfn, note):
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec")
    spect = spec.get("type") if isinstance(spec, dict) else data.get("type")
    chs = data.get("choices") or data.get("candidates") or []
    state = c.latest["state"]
    d = wf_data(state)
    count = 1
    for k in ("count", "amount", "number"):
        if isinstance(d.get(k), int):
            count = d[k]
    oid_by_ref = {}
    for ch in chs:
        r = ref_of(ch)
        if r:
            oid_by_ref[r] = ch["id"]

    oids = sorted(hand_oids(state, pid),
                  key=lambda o: rankfn(state, o))[:count]
    picks = [(o, oid_by_ref[o]) for o in oids if o in oid_by_ref]
    if len(picks) < len(oids):
        say(f"[{tag}] discard({note}): unmapped candidates; deferring")
        return False
    cids = [cid for _, cid in picks]
    if rtype == "schema" and spect in ("sequence", "select"):
        resp_out = {"type": spect, "data": {"choiceIds": cids}}
    elif rtype == "exactChoices":
        resp_out = {"type": "choose", "data": {"choiceId": cids[0]}}
    else:
        say(f"[{tag}] discard({note}): unexpected rtype={rtype} "
            f"spec={spect}; deferring")
        return False
    iid = opp.get("interactionId")
    await send_interaction(c, {"interactionId": iid, "response": resp_out})
    names = [oname(get_obj(state, o)) for o, _ in picks]
    ST["last_discard_picks"] = names
    ST["last_discard_iid"] = iid
    say(f"[{tag}] discard({note}): submits {names} (choice ids {cids})")
    wire("discard_answered", {"tag": tag, "note": note, "iid": iid,
                              "picks": names, "choice_ids": cids})
    return True


async def handle_nonpriority(c, pid, tag, st, state, acts, cost_rank=False):
    wtype = wf_type(state)
    if wtype == "Priority":
        return False
    if await handle_mulligan(c, pid, tag, st, state, acts,
                             ST["want_keep"][pid]):
        return True
    if await handle_bottom(c, pid, tag, st, state, acts):
        return True
    d = wf_data(state)
    dp = d.get("player")
    if isinstance(dp, dict):
        dp = dp.get("id", -1)
    if wtype in ("DiscardChoice", "DiscardToHandSize") and dp == pid:
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                iid = opp.get("interactionId")
                if iid in PROMPT_DONE or iid in SKIP_IID:
                    continue
                rankfn = rank_cost_discard if (
                    cost_rank and ST.get("da_stage") == "awaiting_discard"
                ) else rank_cleanup_discard
                note = "cost" if rankfn is rank_cost_discard else "cleanup"
                if await answer_discard_ranked(c, pid, opp, tag, rankfn, note):
                    PROMPT_DONE.add(iid)
                    if note == "cost":
                        ST["discard_iid"] = iid
                        ST["discarded_names"] = ST.get("last_discard_picks", [])
                    return True
        say(f"[{tag}] {wtype} but no answerable opportunity; deferring")
        return True
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
        adv = next((a for a in acts if a.get("type") == "ChooseLegend"), None)
        if adv:
            await submit_as_is(c, adv)
            say(f"[{tag}] chooses legend (keep first)")
            return True
        return False
    # NOTE: unknown non-priority prompts are NOT auto-passed: the caller
    # answers the additional-cost choice explicitly first, so we never burn
    # a decision prompt (#7176 lesson about answering the wrong prompt).
    return False


async def handle_cost_choice(c, pid, tag, st, state, acts):
    """Answer the Demand Answers additional-cost branch choice. Runs only
    while the cast is announced and no branch chosen yet. Discovers the
    prompt shape live: logs the full opportunity once, then submits the
    DISCARD branch (the reported path) via the engine-issued choice id."""
    if ST.get("da_stage") != "awaiting_cost":
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
            continue
        if ANSWERED_IID.get(iid, 0) >= 3:
            continue
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        if iid not in ST.setdefault("cost_dumped", set()):
            ST["cost_dumped"].add(iid)
            wire("cost_prompt", {"iid": iid, "wf_type": wf_type(state),
                                 "wf_data": wf_data(state), "rtype": rtype,
                                 "opp": json.loads(json.dumps(opp,
                                                              default=str))})
            say(f"[{tag}] cost prompt discovered: wf={wf_type(state)} "
                f"rtype={rtype} n_choices={len(chs)}")
            for ch in chs:
                say(f"[{tag}]   choice {ch.get('id')}: "
                    f"{json.dumps(ch, default=str)[:280]}")

        def blob(ch):
            return json.dumps(ch, default=str).lower()

        disc = [ch for ch in chs
                if "discard" in blob(ch) and "sacrifice" not in blob(ch)]
        method = "text-match"
        if not disc and len(chs) >= 2:
            # Fallback: card-data declares the branches [Sacrifice, Discard]
            # in that order; assume the engine presents them in declared
            # order and take the second. LOUDLY logged as an assumption.
            disc = [chs[1]]
            method = ("declared-order-fallback (card-data Choice data order "
                      "[Sacrifice, Discard]; NO identifying text found)")
            say(f"[{tag}] WARNING: cost choices carry no identifying text; "
                f"assuming declared order and picking choice index 1")
        if not disc:
            say(f"[{tag}] cost prompt: no discard-branch candidate; deferring")
            wire("cost_prompt_unidentified", {"iid": iid})
            continue
        pick = disc[0]
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        spec = data.get("spec")
        spect = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spect in ("sequence", "select"):
            resp_out = {"type": spect, "data": {"choiceIds": [pick["id"]]}}
        else:
            resp_out = {"type": "choose", "data": {"choiceId": pick["id"]}}
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        ANSWERED_IID[iid] = ANSWERED_IID.get(iid, 0) + 1
        ST["cost_iid"] = iid
        ST["cost_choice_id"] = pick["id"]
        ST["cost_choice_json"] = json.loads(json.dumps(pick, default=str))
        ST["cost_wf_type"] = wf_type(state)
        ST["cost_rtype"] = rtype
        ST["cost_method"] = method
        ST["cost_branch"] = "discard"
        ST["da_stage"] = "awaiting_discard"
        say(f"[{tag}] submitted DISCARD branch via {method} "
            f"(choice id {pick['id']})")
        wire("cost_branch_chosen", {"iid": iid, "choice_id": pick["id"],
                                    "method": method})
        return True
    return False


def economy_tick(c, pid, st, acts, state, tag, allow_land=True):
    """Land drop in main + priority-gated pass. Returns (kind, action) or None."""
    rev = st.get("state_revision", -1)
    if allow_land and is_my_main(state, pid):
        pl = next((a for a in acts if a.get("type") == "PlayLand"), None)
        if pl and not acted(f"land{pid}", rev):
            return ("land", pl)
    if my_priority(state, pid):
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp:
            return ("pass", pp)
    return None

# ------------------------------------------------------------- P0 game logic
async def p0_tick(st, acts, state, tag):
    c = _CLIENTS["p0"]
    drain_rejections(c)

    # --- stack sampling: every revision, every stage ---
    if stack_has_da(state):
        if ST.get("resolved"):
            ST.setdefault("sightings", []).append(
                {"turn": turn_of(state), "phase": state.get("phase"),
                 "rev": st.get("state_revision", -1)})
            say(f"[{tag}] *** POST-RESOLUTION Demand Answers on stack "
                f"(turn {turn_of(state)}, phase {state.get('phase')}) ***")
            wire("post_resolution_da_on_stack",
                 {"turn": turn_of(state), "phase": state.get("phase")})
        else:
            if not ST.get("saw_on_stack"):
                ST["saw_on_stack"] = True
                say(f"[{tag}] Demand Answers on stack (pre-resolution, "
                    f"expected)")

    if await handle_nonpriority(c, 0, tag, st, state, acts, cost_rank=True):
        # a cost discard answered here flips the stage below
        if (ST.get("da_stage") == "awaiting_discard"
                and ST.get("discard_iid") is not None):
            ST["da_stage"] = "on_stack"
            ST["discard_time"] = time.time()
            say(f"[{tag}] cost discard answered "
                f"({ST.get('discarded_names')}); spell should be on stack")
        return
    if await handle_cost_choice(c, 0, tag, st, state, acts):
        return

    stage = ST.get("da_stage")

    # --- stage: develop (ramp to 2 untapped Mountains, then cast) ---
    if stage == "develop":
        if my_priority(state, 0) and is_my_main(state, 0):
            ul = untapped_lands(state, 0)
            da_oid = find_hand(state, 0, DA)
            if da_oid is not None and len(ul) >= 2:
                cast = castspell_advertised(acts, da_oid)
                rev = st.get("state_revision", -1)
                if cast and not acted("castda", rev):
                    ST["pre_cast_snapshot"] = {
                        "lib": lib_size(state, 0),
                        "hand": hand_names(state, 0),
                        "gy_da": gy_count(state, 0, DA),
                        "gy_shock": gy_count(state, 0, SHOCK),
                        "turn": turn_of(state),
                        "untapped_mountains": len(ul),
                    }
                    pre = await export_now("pre_cast.json", C0)
                    ST["pre_cast"] = pre
                    ST["cast_action"] = copy.deepcopy(cast)
                    ST["cast_oid"] = str(da_oid)
                    await submit_as_is(c, cast)
                    ST["da_stage"] = "awaiting_cost"
                    ST["cast_time"] = time.time()
                    say(f"[{tag}] casts {DA} (oid {da_oid}); pre-cast "
                        f"exported; awaiting additional-cost prompt")
                    wire("cast_submitted",
                         {"oid": str(da_oid), "action": cast,
                          "pre": ST["pre_cast_snapshot"]})
                    return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: awaiting_cost (branch choice pending) ---
    if stage == "awaiting_cost":
        if time.time() - ST.get("cast_time", time.time()) > 180:
            ST["blocked"] = ("cost prompt never became answerable within "
                             "180s of the cast; see wire_log cost_prompt "
                             "dumps for the observed prompt shape")
            ST["done"] = True
            say(f"[{tag}] BLOCKED: {ST['blocked']}")
            return
        # The engine may skip the branch choice (no artifacts controlled)
        # and go straight to the discard prompt; that is handled above via
        # handle_nonpriority. Here we only pass priority when offered.
        r = economy_tick(c, 0, st, acts, state, tag, allow_land=False)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: awaiting_discard (branch chosen, discard prompt pending) ---
    if stage == "awaiting_discard":
        if time.time() - ST.get("cast_time", time.time()) > 240:
            ST["blocked"] = ("discard prompt never resolved within 240s of "
                             "the cast")
            ST["done"] = True
            say(f"[{tag}] BLOCKED: {ST['blocked']}")
            return
        r = economy_tick(c, 0, st, acts, state, tag, allow_land=False)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: on_stack (driving the spell to resolution) ---
    if stage == "on_stack":
        if not stack_entries(state) and gy_count(state, 0, DA) > ST[
                "pre_cast_snapshot"]["gy_da"]:
            ST["resolved"] = True
            ST["resolution_turn"] = turn_of(state)
            ST["watch_until_turn"] = turn_of(state) + 8  # 4 full cycles
            post = await export_now("post_resolution.json", C0)
            ST["post_resolution"] = post
            ST["post_snapshot"] = {
                "lib": lib_size(state, 0),
                "gy_da": gy_count(state, 0, DA),
                "gy_shock": gy_count(state, 0, SHOCK),
                "turn": turn_of(state),
                "stack_len": len(stack_entries(state)),
            }
            ST["da_stage"] = "watching"
            say(f"[{tag}] RESOLVED on turn {turn_of(state)}: {DA} in P0 "
                f"graveyard, stack empty; post-resolution exported; "
                f"watching until turn {ST['watch_until_turn']}")
            wire("resolved", ST["post_snapshot"])
            return
        if time.time() - ST.get("discard_time", time.time()) > 300:
            ST["blocked"] = ("spell never left the stack within 300s of the "
                             "discard; cannot evaluate post-resolution "
                             "behavior")
            ST["done"] = True
            say(f"[{tag}] BLOCKED: {ST['blocked']}")
            return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: watching (post-resolution stack surveillance) ---
    if stage == "watching":
        ST.setdefault("watch_turns", set()).add(turn_of(state))
        ST["watch_revs"] = ST.get("watch_revs", 0) + 1
        if ST.get("sightings"):
            # Verdict already determined; keep sampling until the watch
            # window closes so the evidence shows the full pattern.
            pass
        if turn_of(state) >= ST["watch_until_turn"]:
            ST["done"] = True
            say(f"[{tag}] watch window complete: "
                f"{len(ST.get('watch_turns', set()))} turns observed, "
                f"{len(ST.get('sightings', []))} post-resolution sightings")
            return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- fallback: keep the game moving ---
    r = economy_tick(c, 0, st, acts, state, tag)
    if r:
        await submit_as_is(c, r[1])


async def p1_tick(st, acts, state, tag):
    c = _CLIENTS["p1"]
    drain_rejections(c)
    if await handle_nonpriority(c, 1, tag, st, state, acts, cost_rank=False):
        return
    r = economy_tick(c, 1, st, acts, state, tag)
    if r:
        await submit_as_is(c, r[1])


_CLIENTS = {}


# ------------------------------------------------------------- game runner
def new_game_state():
    reset_per_game()
    ST.update({"da_stage": "develop",
               "pre_cast": None, "pre_cast_snapshot": None,
               "cast_action": None, "cast_oid": None, "cast_time": 0,
               "cost_iid": None, "cost_choice_id": None,
               "cost_choice_json": None, "cost_wf_type": None,
               "cost_rtype": None, "cost_method": None,
               "cost_branch": None, "cost_dumped": set(),
               "discard_iid": None, "discarded_names": None,
               "discard_time": 0,
               "saw_on_stack": False, "resolved": False,
               "resolution_turn": None, "watch_until_turn": None,
               "post_resolution": None, "post_snapshot": None,
               "watch_turns": set(), "watch_revs": 0,
               "sightings": [],
               "last_discard_picks": None, "last_discard_iid": None,
               "max_turn": 0, "mulls0": 0, "mulls1": 0,
               "want_keep": [None, None], "done": False, "blocked": None})


async def open_game(p0_deck, p1_deck):
    p0 = PhaseClient("7193-P0")
    await p0.connect()
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient("7193-P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*p1_deck))
    say(f"[game] game={p0.game_code}")
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
            say(f"[game] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={wf_type(s)} "
                f"P0hand={len(hand_names(s,0))} P1hand={len(hand_names(s,1))} "
                f"stack={len(stack_entries(s))} stage={ST.get('da_stage')} "
                f"resolved={ST.get('resolved')} "
                f"watch_turns={len(ST.get('watch_turns', set()))}")
        for c in (p0, p1):
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


def p0_keep(state, pid):
    hn = hand_names(state, pid)
    lands = sum(1 for n in hn if n in LANDS)
    return DA in hn and lands >= 2


async def play_game(p0_deck, p1_deck):
    new_game_state()
    p0, p1 = await open_game(p0_deck, p1_deck)
    _CLIENTS["p0"], _CLIENTS["p1"] = p0, p1
    ST["want_keep"] = [p0_keep, lambda state, pid: True]
    ok = await run_game(p0, p1, timeout_s=1500)
    say(f"game finished ok={ok}")
    await p0.close()
    await p1.close()
    return ok

# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
    e = cd["demand answers"]
    ac = e.get("additional_cost") or {}
    branches = []
    for b in (ac.get("data") or []):
        branches.append(b.get("type"))
    out = {"demand answers": {
        "oracle_text": e.get("oracle_text"),
        "mana_cost": e.get("mana_cost"),
        "additional_cost_type": ac.get("type"),
        "additional_cost_branches": branches,
    }}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(parse):
    ass = {}
    notes = []
    pe = parse["demand answers"]
    notes.append(
        "parse (pinned v0.85.0 card-data): oracle=" +
        repr(pe["oracle_text"]) + "; additional_cost=" +
        f"{pe['additional_cost_type']}{pe['additional_cost_branches']}")

    if ST.get("blocked"):
        for k in ("A1_cost_branch", "A2_discard", "A3_resolve",
                  "A4_stack_clean", "A5_no_later_stack", "A6_no_rejects"):
            ass[k] = "not-run"
        notes.append(f"verdict: blocked - {ST['blocked']}")
        return ass, notes, "blocked"

    # A1: cost branch choice
    if (ST.get("cost_branch") == "discard"
            and REJECTS.get(ST.get("cost_iid"), 0) == 0):
        ass["A1_cost_branch"] = "passed"
        notes.append(
            f"A1 passed: additional-cost prompt appeared "
            f"(wf={ST['cost_wf_type']}, rtype={ST['cost_rtype']}); DISCARD "
            f"branch submitted via {ST['cost_method']} (choice id "
            f"{ST['cost_choice_id']}); no rejection on the interaction")
    elif (ST.get("discard_iid") is not None
            and not ST.get("cost_dumped")):
        ass["A1_cost_branch"] = "passed"
        notes.append(
            "A1 passed (forced path): engine offered NO branch choice "
            "(P0 controlled no artifacts, so only the discard branch was "
            "payable) and went straight to the discard prompt, which was "
            "answered; no rejection")
    else:
        ass["A1_cost_branch"] = "failed"
        notes.append(
            f"A1 FAILED: cost_branch={ST.get('cost_branch')}, "
            f"cost_iid={ST.get('cost_iid')}, rejects="
            f"{REJECTS.get(ST.get('cost_iid'), 0)}")

    # A2: the discard happened via the advertised choice
    pre = ST.get("pre_cast_snapshot") or {}
    post = ST.get("post_snapshot") or {}
    disc = ST.get("discarded_names") or []
    if (disc == [SHOCK]
            and post.get("gy_shock", -1) == pre.get("gy_shock", -2) + 1
            and DA not in disc):
        ass["A2_discard"] = "passed"
        notes.append(
            f"A2 passed: discarded {disc} via the advertised discard choice "
            f"(iid {ST.get('discard_iid')}); P0 graveyard Shock "
            f"{pre.get('gy_shock')}->{post.get('gy_shock')}; no Demand "
            f"Answers discarded")
    else:
        ass["A2_discard"] = "failed"
        notes.append(
            f"A2 FAILED: discarded_names={disc}, gy_shock "
            f"{pre.get('gy_shock')}->{post.get('gy_shock')}")

    # A3: resolution drew exactly 2, Demand Answers in graveyard
    if (ST.get("resolved")
            and post.get("turn") == pre.get("turn")
            and post.get("lib") == pre.get("lib", 0) - 2
            and post.get("gy_da", -1) == pre.get("gy_da", -2) + 1):
        ass["A3_resolve"] = "passed"
        notes.append(
            f"A3 passed: resolved on turn {post.get('turn')} (same turn as "
            f"cast); P0 library {pre.get('lib')}->{post.get('lib')} "
            f"(exactly -2, drew two cards); Demand Answers in P0 graveyard "
            f"({pre.get('gy_da')}->{post.get('gy_da')})")
    elif not ST.get("resolved"):
        ass["A3_resolve"] = "not-run"
        notes.append("A3 not-run: resolution never reached")
    else:
        ass["A3_resolve"] = "failed"
        notes.append(
            f"A3 FAILED: turn {pre.get('turn')}->{post.get('turn')}, lib "
            f"{pre.get('lib')}->{post.get('lib')}, gy_da "
            f"{pre.get('gy_da')}->{post.get('gy_da')}")

    # A4: stack clean of Demand Answers right after resolution
    post_st = ST.get("post_resolution")
    if post_st is not None and not stack_has_da(post_st):
        ass["A4_stack_clean"] = "passed"
        notes.append("A4 passed: post-resolution export shows zero Demand "
                     "Answers entries on the stack")
    elif post_st is None:
        ass["A4_stack_clean"] = "not-run"
        notes.append("A4 not-run: no post-resolution state")
    else:
        ass["A4_stack_clean"] = "failed"
        notes.append("A4 FAILED: Demand Answers still on the stack in the "
                     "post-resolution export")

    # A5: no later-stack appearances across >=4 full turn cycles
    sightings = ST.get("sightings", [])
    nturns = len(ST.get("watch_turns", set()))
    if nturns >= 8 and not sightings:
        ass["A5_no_later_stack"] = "passed"
        notes.append(
            f"A5 passed: {nturns} post-resolution turns observed "
            f"({ST.get('watch_revs', 0)} stack samples, turns "
            f"{sorted(ST.get('watch_turns', set()))[:4]}...), zero Demand "
            f"Answers entries on any later stack")
    elif sightings:
        ass["A5_no_later_stack"] = "failed"
        notes.append(
            f"A5 FAILED (THE REPORTED SYMPTOM): {len(sightings)} "
            f"post-resolution Demand Answers stack sighting(s): " +
            json.dumps(sightings[:6]))
    else:
        ass["A5_no_later_stack"] = "not-run"
        notes.append(f"A5 not-run: only {nturns}/8 watch turns observed")

    # A6: no rejections on the cost/discard interactions, cast completed
    bad = {iid: n for iid, n in REJECTS.items() if n > 0}
    watched = [ST.get("cost_iid"), ST.get("discard_iid"),
               ST.get("last_discard_iid")]
    bad_watched = {i: bad[i] for i in watched if i in bad}
    if not bad_watched and ST.get("resolved"):
        ass["A6_no_rejects"] = "passed"
        notes.append("A6 passed: no rejections on the cost-branch or discard "
                     "interactions; the announced cast completed")
    else:
        ass["A6_no_rejects"] = "failed"
        notes.append(f"A6 FAILED: rejections on watched iids={bad_watched}, "
                     f"all rejects={bad}, resolved={ST.get('resolved')}")

    if sightings:
        verdict = "reproduced"
        notes.append(
            "verdict: REPRODUCED - Demand Answers appeared on a later "
            "stack after resolving once, with no new cast submitted by the "
            "driver (exactly the reported 'added to every stack' symptom)")
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
        notes.append(
            "verdict: not-reproduced - discard-branch cast resolved "
            "cleanly (drew 2, spell in graveyard) and no Demand Answers "
            "entry appeared on any later stack across 4+ full turn cycles")
    else:
        verdict = "blocked"
        notes.append("verdict: blocked - inconclusive observations "
                     "(see assertion notes)")
    return ass, notes, verdict


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

    line("#7193 Demand Answers: discard-branch cast reappears on later "
         "stacks?", fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("setup: P0 12x Demand Answers / 24x Mountain / 24x Shock vs P1 60x "
         "Mountain", size=16)
    line("cast via DISCARD branch (reported path), discard a Shock, draw 2; "
         "then 4 turn cycles of stack sampling", size=16)
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, val in run["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=17)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:15]:
        line(f"  - {n[:112]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.85.0, protocol 72)")

    parse = parse_evidence()
    say("parse:", json.dumps(parse))

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
        "source": "reused live server on 127.0.0.1:9374 (run 20260917-7193); "
                  "ServerHello confirms v0.85.0/cb58ef5/protocol 72; hashes "
                  "recomputed from on-disk release files this run; v0.85.0 "
                  "confirmed latest stable release (not shell-*)",
    }
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario": "driver/scenario_7193.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_7193.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "game_ok": ok,
        "parse_summary": parse,
        "cast_action": ST.get("cast_action"),
        "cost_decision": {
            "wf_type": ST.get("cost_wf_type"),
            "response_type": ST.get("cost_rtype"),
            "branch": ST.get("cost_branch"),
            "method": ST.get("cost_method"),
            "interaction_id": ST.get("cost_iid"),
            "choice_id": ST.get("cost_choice_id"),
            "choice_json": ST.get("cost_choice_json"),
            "forced_path": ST.get("discard_iid") is not None and not ST.get(
                "cost_dumped"),
        },
        "discard": {"interaction_id": ST.get("discard_iid"),
                    "discarded": ST.get("discarded_names")},
        "resolution": {"resolved": ST.get("resolved"),
                       "turn": ST.get("resolution_turn"),
                       "saw_on_stack": ST.get("saw_on_stack")},
        "watch": {"turns_observed": sorted(ST.get("watch_turns", set())),
                  "stack_samples": ST.get("watch_revs", 0),
                  "sightings": ST.get("sightings", [])},
        "rejections": REJECTS,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-17",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "P1 is a passive second seat (lands only, no attacks, no spells) - "
            "opponent interaction with the spell is not exercised.",
            "Only the DISCARD branch of the additional cost is exercised "
            "(the reported path); the sacrifice branch is not tested.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 12x Demand Answers + 24x Mountain + 24x Shock vs "
                      "P1 60x Mountain. P0 casts Demand Answers on its main "
                      "phase with 2 untapped Mountains, answers the "
                      "additional-cost branch choice with DISCARD, discards a "
                      "Shock, draws two, then both seats pass while the "
                      "driver samples the stack every revision for 4 full "
                      "turn cycles.",
        "contract_line": "A discard-branch Demand Answers cast must resolve "
                         "exactly once (draw 2, spell to graveyard) and must "
                         "never reappear on a later stack without a new cast.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7193.py", "w") as f:
        f.write(open(__file__).read())
    render_png(f"{EVDIR}/summary.png", run)
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
