#!/usr/bin/env python3
"""Issue #6250: Ardenn, Intrepid Archaeologist -- beginning-of-combat trigger
attaches only ONE Aura/Equipment instead of any number.

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72). Prior run
6250-20260910-071149 (v0.78.0, protocol 68) reproduced; maintained comment
https://github.com/phase-rs/phase/issues/6250#issuecomment-5614776894.

Behavioral contract:
  game A (accept): P0 controls Ardenn + holy strength (aura, attached to Ardenn
      on cast) + plate armor + colossus hammer (equipment, unattached) and
      moves to combat. Trigger fires: choose Ardenn as target permanent, accept
      the optional "you may", select attachments (the buggy prompt caps the
      selection at max=1). Expect ALL THREE attachments on Ardenn after
      resolution; the bug attaches only one.
  game B (decline control): same setup, but decline the optional trigger.
      Expect NO attachment moves: holy strength stays on Ardenn, plate armor
      and colossus hammer stay unattached.

Evidence: evidence/6250/<run-id>/pre_{A,B}.json, post_{A,B}.json, run.json,
parse_evidence.json, wire_log.jsonl, scenario_run.log, summary.png,
manifest.sha256.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

ISSUE = 6250
RUN_ID = "20260916-6250"
SERVER_RUN_ID = "6250-20260916-2141"  # fresh server started by this run
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ARDENN = "ardenn, intrepid archaeologist"
HOLY = "holy strength"
PLATE = "plate armor"
HAMMER = "colossus hammer"
ATTACHMENTS = [HOLY, PLATE, HAMMER]
LANDS = ("Plains",)

A_P0_DECK = [(ARDENN, 8), (HOLY, 8), (PLATE, 8), (HAMMER, 8), ("Plains", 28)]
A_P1_DECK = [("Mountain", 60)]
B_P0_DECK = list(A_P0_DECK)
B_P1_DECK = [("Mountain", 60)]

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
    return (o.get("card_name") or o.get("base_name") or o.get("name") or "").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def life(state, pid):
    return state["players"][pid]["life"]


def hand_oids(state, pid):
    return [str(x) for x in state["players"][pid]["hand"]]


def hand_names(state, pid):
    return [oname(get_obj(state, oid)) for oid in hand_oids(state, pid)]


def bf(state, pid):
    return [(oid, o) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_find(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def untapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) in (l.lower() for l in LANDS)]


def attached_to_oid(o):
    """Resolve which host oid an attachment is attached to, or None.
    Protocol 69+ nests it; search the object recursively (#6762 lesson)."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("attached_to", "attached_to_id", "attachedTo",
                         "attachedToId", "host_id", "host_oid"):
                    if isinstance(v, int):
                        found.append(str(v))
                    elif isinstance(v, dict) and isinstance(v.get("data"), int):
                        found.append(str(v["data"]))
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(o)
    return found[0] if found else None


def bf_attachments(state, pid):
    return {oid: o for oid, o in bf(state, pid) if oname(o) in ATTACHMENTS}


def attachments_on(state, pid, host_name):
    """Names of the attachments attached to the (first) host_name creature."""
    host_oid = bf_find(state, pid, host_name)
    if host_oid is None:
        return []
    return [oname(o) for oid, o in bf_attachments(state, pid).items()
            if attached_to_oid(o) == str(host_oid)]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def my_priority(state, pid):
    """Default PassPriority gated on actually holding priority (#4509)."""
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {}) or {}
    dp = d.get("player")
    if isinstance(dp, dict):
        dp = dp.get("id", -1)
    return (wf.get("type") == "Priority"
            and (dp == pid or d.get("deciding_player") == pid))


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


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("seat") is not None:
            return d.get("seat")
    return None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("name"):
                t = d["name"]
                break
    return str(t)


def surf_names(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict):
            if d.get("name"):
                out.append(str(d["name"]).lower())
            if d.get("seat") is not None:
                out.append(f"player-seat:{d['seat']}")
    return out


def action_codes(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            out.append(((s.get("data") or {}).get("code")) or "")
    return out


def accept_value(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "value":
            d = s.get("data") or {}
            if d.get("role") == "accept":
                return str(d.get("value")).lower() == "true"
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
    want = "keep" if (want_keep(state, pid) or mulls >= 3) else "mulligan"
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
    h.sort(key=lambda o: (oname(get_obj(state, o)) not in
                          [l.lower() for l in LANDS], oname(get_obj(state, o))))
    adv = next((a for a in acts if a.get("type") == "SelectCards"), None)
    if adv:
        sub = copy.deepcopy(adv)
        sub["data"]["cards"] = [int(x) for x in h[:n]]
        await submit_as_is(c, sub)
    else:
        return False
    say(f"[{tag}] bottoms {n} after mulligan")
    return True


async def answer_discard(c, pid, opp, tag, protect):
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = (data.get("spec") or {}).get("type") if isinstance(
        data.get("spec"), dict) else data.get("type")
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

    def rank(oid):
        nm = oname(get_obj(state, oid))
        if nm in protect:
            return (2, nm)
        if nm in [l.lower() for l in LANDS]:
            return (0, nm)
        return (1, nm)
    oids = sorted(hand_oids(state, pid), key=rank)[:count]
    picks = [(o, oid_by_ref[o]) for o in oids if o in oid_by_ref]
    if len(picks) < len(oids):
        say(f"[{tag}] discard: unmapped candidates; deferring")
        return False
    cids = [cid for _, cid in picks]
    if rtype == "schema" and spec in ("sequence", "select"):
        resp_out = {"type": spec, "data": {"choiceIds": cids}}
    elif rtype == "exactChoices":
        resp_out = {"type": "choose", "data": {"choiceId": cids[0]}}
    else:
        say(f"[{tag}] discard: unexpected rtype={rtype} spec={spec}; deferring")
        return False
    await send_interaction(c, {"interactionId": opp.get("interactionId"),
                              "response": resp_out})
    names = [oname(get_obj(state, o)) for o, _ in picks]
    say(f"[{tag}] discards {names}")
    wire("discard_answered", {"tag": tag, "picks": names})
    return True


async def handle_nonpriority(c, pid, tag, st, state, acts, protect=()):
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
                if await answer_discard(c, pid, opp, tag, protect):
                    PROMPT_DONE.add(iid)
                    return True
        say(f"[{tag}] {wtype} but no answerable opportunity; deferring")
        return True
    if wtype in ("DeclareAttackers", "DeclareBlockers"):
        adv = next((a for a in acts if a.get("type") == wtype), None)
        if adv:
            sub = copy.deepcopy(adv)
            dd = sub.setdefault("data", {})
            for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
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
    return False


async def answer_trigger_interactions(c, pid, tag, st, state, acts):
    """Answer the Ardenn trigger's prompts for P0: target selection (Ardenn),
    the attachment choice (up to the prompt's max; log the shape), and the
    optional 'you may' (decideOptionalEffect accept=game's decision)."""
    vi = get_vi(st)
    if not vi:
        return False
    g = ST["game"]
    mode = ST.get("mode")  # "accept" or "decline"
    acted_any = False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if (ANSWERED_IID.get(iid, 0) >= 3 or iid in SKIP_IID
                or iid in PROMPT_DONE):
            continue
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(action_codes(ch))
        if codes - {"decideOptionalEffect"}:
            continue  # priority menus; not for us
        # --- optional "you may" ---
        if "decideOptionalEffect" in codes:
            # During setup (attachments still being deployed) always decline:
            # the real decision happens only at the combat-stage trigger.
            real_decision = (ST.get("stage") == "combat")
            want_accept = (mode == "accept" and real_decision)
            pick = next((ch for ch in chs if accept_value(ch) is want_accept),
                        None)
            if pick is None:
                say(f"[{tag}] optional: no accept={want_accept} choice; deferring")
                continue
            obs = ST.setdefault("obs", {})
            obs.setdefault("optional_shapes", []).append(
                json.dumps([choice_text(ch) for ch in chs])[:200])
            await send_interaction(
                c, {"interactionId": iid,
                    "response": {"type": "choose",
                                 "data": {"choiceId": pick["id"]}}})
            ANSWERED_IID[iid] = ANSWERED_IID.get(iid, 0) + 1
            PROMPT_DONE.add(iid)
            if real_decision:
                obs["optional_decided"] = "accept" if want_accept else "decline"
                ST["decision_time"] = time.time()
            say(f"[{tag}] optional 'you may' -> "
                f"{'ACCEPT' if want_accept else 'DECLINE'}"
                f"{'' if real_decision else ' (setup-stage, not the verdict decision)'}")
            wire("optional_decision", {"mode": mode, "iid": iid,
                                       "stage": ST.get("stage"),
                                       "real_decision": real_decision})
            acted_any = True
            continue
        # --- classify candidates ---
        def cand_kind(ch):
            nm = choice_text(ch).lower()
            sns = surf_names(ch)
            if nm in ATTACHMENTS or any(n in ATTACHMENTS for n in sns):
                return "attachment"
            if any(s.startswith("player-seat:") for s in sns):
                return "player"
            if nm or sns:
                return "permanent"
            return "unknown"

        kinds = {cand_kind(ch) for ch in chs}
        avail = [ch for ch in chs
                 if ch.get("status", {}).get("type", "available") != "unavailable"]
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        spec_data = (spec.get("data") if isinstance(spec, dict) else {}) or {}
        obs = ST.setdefault("obs", {})
        # --- attachment selection prompt ---
        if kinds <= {"attachment", "unknown"} and avail:
            cap = spec_data.get("max")
            shape = f"{rtype}/{spec_type} max={cap} n_avail={len(avail)}"
            obs.setdefault("attachment_shapes", []).append(shape)
            say(f"[{tag}] attachment prompt: {shape} "
                f"({[choice_text(ch) for ch in avail]})")
            wire("attachment_prompt", {"iid": iid, "shape": shape,
                                       "available": [choice_text(ch) for ch in avail]})
            if isinstance(cap, int) and cap < len(avail):
                obs["cap_note"] = (f"attachment prompt caps selection at max={cap} "
                                   f"with {len(avail)} attachments available -- the "
                                   f"'any number' choice is restricted in the "
                                   f"prompt itself")
            avail = [ch for ch in avail if cand_kind(ch) == "attachment"]
            # prefer currently-unattached attachments so moves are visible
            def sort_key(ch):
                ref = ref_of(ch)
                o = get_obj(state, ref) if ref else {}
                return (0 if attached_to_oid(o) is None else 1,
                        choice_text(ch))
            avail = sorted(avail, key=sort_key)
            n_pick = (min(cap, len(avail)) if isinstance(cap, int)
                      else len(avail))
            picks = avail[:n_pick]
            ids = [ch["id"] for ch in picks]
            if rtype == "exactChoices":
                sub = {"interactionId": iid, "response":
                       {"type": "choose", "data": {"choiceId": ids[0]}}}
            else:
                sub = {"interactionId": iid, "response":
                       {"type": spec_type or "sequence",
                        "data": {"choiceIds": ids}}}
            obs.setdefault("attach_picks", []).extend(
                choice_text(ch) for ch in picks)
            say(f"[{tag}] attachment choice -> {[choice_text(ch) for ch in picks]}")
            wire("attachment_choice", {"submission": sub, "spec_max": cap})
            await send_interaction(c, sub)
            ANSWERED_IID[iid] = ANSWERED_IID.get(iid, 0) + 1
            PROMPT_DONE.add(iid)
            acted_any = True
            continue
        # --- target selection prompt (permanents and/or players) ---
        if kinds & {"permanent", "player"}:
            obs.setdefault("target_shapes", []).append(
                f"{rtype}/{spec_type} n={len(chs)}")
            ardenn_oid = bf_find(state, 0, ARDENN)
            pick = None
            for ch in chs:
                if ref_of(ch) == str(ardenn_oid):
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    if "ardenn" in choice_text(ch).lower():
                        pick = ch
                        break
            if pick is None:
                say(f"[{tag}] target prompt: no Ardenn candidate; deferring")
                continue
            await send_interaction(
                c, {"interactionId": iid,
                    "response": {"type": spec_type or "sequence",
                                 "data": {"choiceIds": [pick["id"]]}}})
            ANSWERED_IID[iid] = ANSWERED_IID.get(iid, 0) + 1
            PROMPT_DONE.add(iid)
            if not ST.get("holy_pending") and ST.get("stage") == "combat":
                obs["trigger_target"] = "ardenn"
            say(f"[{tag}] target -> {choice_text(pick)!r} "
                f"(stage={ST.get('stage')}, holy_pending={ST.get('holy_pending')})")
            wire("target_choice", {"iid": iid,
                                   "choice": choice_text(pick),
                                   "stage": ST.get("stage"),
                                   "holy_pending": ST.get("holy_pending")})
            if ST.get("holy_pending"):
                ST["holy_pending"] = False
            acted_any = True
            continue
    return acted_any


def economy_tick(c, pid, st, acts, state, tag):
    """Land drop in main + priority-gated pass. Returns action or None."""
    rev = st.get("state_revision", -1)
    if is_my_main(state, pid):
        pl = next((a for a in acts if a.get("type") == "PlayLand"), None)
        if pl and not acted(f"land{pid}", rev):
            return pl
    if my_priority(state, pid):
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp:
            return pp
    return None


# ------------------------------------------------------------- P0 game logic
async def p0_tick(st, acts, state, tag):
    c = _CLIENTS["p0"]
    drain_rejections(c)
    if await answer_trigger_interactions(c, 0, tag, st, state, acts):
        return
    if await handle_nonpriority(c, 0, tag, st, state, acts,
                               protect=(ARDENN, HOLY)):
        return

    # --- post-resolution export window: real decision made, stack empty,
    # --- past beginning of combat -> capture post state ---
    if ST.get("decision_time") and not ST.get(f"post{ST['game']}"):
        calm = len(stack_entries(state)) == 0
        past_boc = (state.get("phase") or "") != "BeginningOfCombat"
        if calm and past_boc:
            ST["calm_ticks"] = ST.get("calm_ticks", 0) + 1
        else:
            ST["calm_ticks"] = 0
        if ST["calm_ticks"] >= 2:
            say(f"[{tag}] post: optional={ST['obs'].get('optional_decided')} "
                f"on_ardenn={attachments_on(state, 0, ARDENN)} "
                f"phase={state.get('phase')} turn={turn_of(state)}")
            ST[f"post{ST['game']}"] = await export_now(f"post_{ST['game']}.json", C0)
            return
        if time.time() - ST["decision_time"] > 240:
            say(f"[{tag}] post-timeout: exporting post anyway")
            ST[f"post{ST['game']}"] = await export_now(f"post_{ST['game']}.json", C0)
            return

    if wf_type(state) != "Priority" or not my_priority(state, 0):
        return

    main_phase = is_my_main(state, 0)
    hn = hand_names(state, 0)
    lands_played = (state["players"][0].get("lands_played_this_turn")
                    or state["players"][0].get("land_plays_this_turn") or 0)
    ardenn_bf = bf_find(state, 0, ARDENN) is not None
    atts = bf_attachments(state, 0)

    # 1) cast Ardenn
    if not ardenn_bf and main_phase:
        for oid in hand_oids(state, 0):
            if oname(get_obj(state, oid)) == ARDENN:
                cast = castspell_advertised(acts, oid)
                if cast and not acted("castardenn", st.get("state_revision", -1)):
                    await submit_as_is(c, cast)
                    say(f"[{tag}] casts Ardenn (turn {turn_of(state)})")
                    return
    # 2) cast holy strength (targets Ardenn via interaction)
    if (ardenn_bf and HOLY in hn and main_phase
            and not ST.get("holy_cast")):
        for oid in hand_oids(state, 0):
            if oname(get_obj(state, oid)) == HOLY:
                cast = castspell_advertised(acts, oid)
                if cast and not acted("castholy", st.get("state_revision", -1)):
                    ST["holy_cast"] = True
                    ST["holy_pending"] = True
                    await submit_as_is(c, cast)
                    say(f"[{tag}] casts Holy Strength (targets Ardenn)")
                    wire("cast_holy", cast)
                    return
    # 3) cast plate armor
    if (ardenn_bf and PLATE in hn and main_phase
            and not ST.get("plate_cast")):
        for oid in hand_oids(state, 0):
            if oname(get_obj(state, oid)) == PLATE:
                cast = castspell_advertised(acts, oid)
                if cast and not acted("castplate", st.get("state_revision", -1)):
                    ST["plate_cast"] = True
                    await submit_as_is(c, cast)
                    say(f"[{tag}] casts Plate Armor")
                    return
    # 4) cast colossus hammer
    if (ardenn_bf and HAMMER in hn and main_phase
            and not ST.get("hammer_cast")):
        for oid in hand_oids(state, 0):
            if oname(get_obj(state, oid)) == HAMMER:
                cast = castspell_advertised(acts, oid)
                if cast and not acted("casthammer", st.get("state_revision", -1)):
                    ST["hammer_cast"] = True
                    await submit_as_is(c, cast)
                    say(f"[{tag}] casts Colossus Hammer")
                    return
    # 5) setup complete? export PRE at PreCombatMain
    if (ardenn_bf and len(atts) == 3 and ST.get("stage") == "setup"
            and main_phase and state.get("phase") == "PreCombatMain"):
        ST["stage"] = "combat"
        say(f"[{tag}] setup complete (turn {turn_of(state)}): on_ardenn="
            f"{attachments_on(state, 0, ARDENN)}; exporting PRE")
        for oid, o in atts.items():
            wire("pre_attachment_obj", {"name": oname(o), "object": o})
        ST[f"pre{ST['game']}"] = await export_now(f"pre_{ST['game']}.json", C0)
        # fall through to pass below
    # 6) land drop while setting up
    r = economy_tick(c, 0, st, acts, state, tag)
    if r:
        await submit_as_is(c, r)


async def p1_tick(st, acts, state, tag):
    c = _CLIENTS["p1"]
    drain_rejections(c)
    if await handle_nonpriority(c, 1, tag, st, state, acts, protect=()):
        return
    r = economy_tick(c, 1, st, acts, state, tag)
    if r:
        await submit_as_is(c, r)


_CLIENTS = {}


# ------------------------------------------------------------- game runner
def new_game_state(game, mode):
    reset_per_game()
    ST.update({"game": game, "mode": mode, "stage": "setup",
               "holy_cast": False, "holy_pending": False,
               "plate_cast": False, "hammer_cast": False,
               "decision_time": 0, "calm_ticks": 0,
               "obs": {}, "mulls0": 0, "mulls1": 0,
               "want_keep": [None, None],
               f"pre{game}": None, f"post{game}": None})


async def open_game(tag, p0_deck, p1_deck):
    p0 = PhaseClient(f"{tag}-P0")
    await p0.connect()
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient(f"{tag}-P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*p1_deck))
    say(f"[{tag}] game={p0.game_code}")
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
    ticks = [(p0, p0_tick, 0, f"{ST['game']}-P0"),
             (p1, p1_tick, 1, f"{ST['game']}-P1")]
    for _ in range(int(timeout_s / 0.2)):
        await asyncio.sleep(0.2)
        for c, tick, pid, tag in ticks:
            st = c.latest
            if not st:
                continue
            rev = c.revision
            if rev == last.get(c.name) and time.time() - last_tick.get(c.name, 0) <= 5:
                continue
            last[c.name] = rev
            last_tick[c.name] = time.time()
            last_adv[c.name] = time.time()
            warned.discard(c.name)
            state = st["state"]
            acts = list(st.get("legal_actions") or [])
            try:
                await tick(st, acts, state, tag)
            except Exception as e:
                say(f"[{c.name}] tick error: {e!r}")
            if ST.get(f"post{ST['game']}") is not None:
                say(f"[{ST['game']}] contract complete; finishing game")
                return True
        now = time.time()
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            say(f"[{ST['game']}] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={wf_type(s)} "
                f"P0hand={len(hand_names(s,0))} stack={len(stack_entries(s))} "
                f"stage={ST.get('stage')} obs={json.dumps(ST.get('obs'))[:200]}")
        for c in (p0, p1):
            if (now - last_adv[c.name] > 60 and c.name not in warned
                    and c.latest):
                warned.add(c.name)
                st = c.latest
                state = st.get("state", {})
                say(f"[{ST['game']}] WATCHDOG {c.name}: no revision advance for "
                    f"{now - last_adv[c.name]:.0f}s; rev={c.revision} "
                    f"turn={turn_of(state)} phase={state.get('phase')} "
                    f"wf={wf_type(state)}")
        if now - t0 > timeout_s:
            say(f"[{ST['game']}] global timeout ({timeout_s}s) hit")
            return False
    return False


def p0_keep(state, pid):
    hn = hand_names(state, pid)
    lands = sum(1 for n in hn if n in [l.lower() for l in LANDS])
    return ARDENN in hn and lands >= 2


async def play_game(game, mode, p0_deck, p1_deck):
    new_game_state(game, mode)
    p0, p1 = await open_game(game, p0_deck, p1_deck)
    _CLIENTS["p0"], _CLIENTS["p1"] = p0, p1
    ST["want_keep"] = [p0_keep, lambda state, pid: True]
    ok = await run_game(p0, p1, timeout_s=1200)
    say(f"game {game} finished ok={ok}")
    await p0.close()
    await p1.close()
    return ok


# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd_path = f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"
    cd = json.load(open(cd_path))
    e = cd["ardenn, intrepid archaeologist"]

    def filt_summary(f):
        if not isinstance(f, dict):
            return str(f)
        t = f.get("type")
        if t == "Or":
            return "Or(" + ",".join(filt_summary(x) for x in f.get("filters", [])) + ")"
        if t == "Typed":
            subs = [x.get("Subtype") for x in f.get("type_filters", [])
                    if isinstance(x, dict)]
            return f"Typed(subtypes={subs},controller={f.get('controller')})"
        if t == "SelfRef":
            return "SelfRef"
        return t

    trig = (e.get("triggers") or [])[0]
    ex = (trig.get("execute") or {}).get("effect") or {}
    out = {"ardenn, intrepid archaeologist": {
        "oracle_text": e.get("oracle_text"),
        "trigger_condition": str((trig.get("condition") or {})
                                 .get("type", trig.get("condition")))[:200],
        "effect_type": ex.get("type"),
        "effect_optional": ex.get("optional"),
        "effect_attachment": filt_summary(ex.get("attachment") or {}),
        "effect_target": filt_summary(ex.get("target") or {}),
    }}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(parse, snaps):
    """snaps: {game: (pre, post)}. Returns (assertions, notes, verdict)."""
    ass = {}
    notes = []
    pe = parse["ardenn, intrepid archaeologist"]
    notes.append(
        "parse (pinned v0.85.0 card-data): effect="
        f"{pe['effect_type']} optional={pe['effect_optional']}; "
        f"attachment={pe['effect_attachment']}; target={pe['effect_target']} "
        "-- the 'any number' choice is not reflected in the AST, matching the "
        "triage finding that the defect is at runtime/prompt level.")

    verdict = None
    setup = {}
    for g, mode in (("A", "accept"), ("B", "decline")):
        pre, post = snaps[g]
        obs = ST.get(f"obs{g}") or {}
        pre_tag = f"{g}1_setup"
        if pre is None:
            ass[pre_tag] = "failed"
            notes.append(f"{pre_tag} failed: game {g} never reached the PRE export")
            setup[g] = False
            for k in (f"{g}2_decision", f"{g}3_result", f"{g}4_cleanup"):
                ass[k] = "not-run"
            continue
        atts = bf_attachments(pre, 0)
        on_ardenn = attachments_on(pre, 0, ARDENN)
        ardenn_bf = bf_find(pre, 0, ARDENN) is not None
        ok = (ardenn_bf and len(atts) == 3
              and HOLY in on_ardenn and PLATE not in on_ardenn
              and HAMMER not in on_ardenn
              and life(pre, 0) == 20 and life(pre, 1) == 20
              and pre.get("phase") == "PreCombatMain")
        setup[g] = ok
        ass[pre_tag] = "passed" if ok else "failed"
        notes.append(f"{pre_tag} ({mode}): ardenn={ardenn_bf}, "
                     f"atts={sorted(oname(o) for o in atts.values())}, "
                     f"on_ardenn={sorted(on_ardenn)}, phase={pre.get('phase')}: "
                     f"{'ok' if ok else 'SETUP FAILED'}")
        if not ok:
            for k in (f"{g}2_decision", f"{g}3_result", f"{g}4_cleanup"):
                ass[k] = "not-run"
            continue
        # --- decision branch ---
        dec = obs.get("optional_decided")
        if dec == mode:
            ass[f"{g}2_decision"] = "passed"
            notes.append(f"{g}2_decision: optional trigger {dec}ed "
                         f"(target={obs.get('trigger_target')})")
        else:
            ass[f"{g}2_decision"] = "failed"
            notes.append(f"{g}2_decision FAILED: wanted {mode}, saw {dec}")
        shapes = "; ".join(obs.get("attachment_shapes") or [])
        if shapes:
            notes.append(f"{g}: attachment prompt shape(s): {shapes}")
        if obs.get("cap_note"):
            notes.append(f"{g}: {obs['cap_note']}")
        # --- result branch ---
        if post is None:
            ass[f"{g}3_result"] = "failed"
            ass[f"{g}4_cleanup"] = "not-run"
            notes.append(f"{g}3_result failed: no post state captured")
            continue
        on_ardenn_post = attachments_on(post, 0, ARDENN)
        moved = sorted(n for n in (PLATE, HAMMER) if n in on_ardenn_post)
        all_on = all(n in on_ardenn_post for n in ATTACHMENTS)
        if mode == "accept":
            if all_on:
                ass[f"{g}3_result"] = "passed"
                notes.append(f"{g}3_result: all 3 attachments on Ardenn "
                             f"({sorted(on_ardenn_post)}) -- any-number behavior OK")
            else:
                ass[f"{g}3_result"] = "failed"
                notes.append(f"{g}3_result FAILED: attachments on Ardenn = "
                             f"{sorted(on_ardenn_post)} (expected all of "
                             f"{ATTACHMENTS}); moved={moved} -- matches the "
                             f"reported single-attachment collapse")
        else:
            unchanged = (sorted(on_ardenn_post) == sorted(on_ardenn))
            if unchanged:
                ass[f"{g}3_result"] = "passed"
                notes.append(f"{g}3_result (decline control): no attachment "
                             f"moved ({sorted(on_ardenn_post)}); control OK")
            else:
                ass[f"{g}3_result"] = "failed"
                notes.append(f"{g}3_result FAILED (decline control): attachments "
                             f"moved anyway: {sorted(on_ardenn)} -> "
                             f"{sorted(on_ardenn_post)}")
        calm = len(stack_entries(post)) == 0
        l0, l1 = life(post, 0), life(post, 1)
        if calm and l0 == 20 and l1 == 20:
            ass[f"{g}4_cleanup"] = "passed"
            notes.append(f"{g}4_cleanup: stack empty, life 20/20 "
                         f"(phase={post.get('phase')})")
        else:
            ass[f"{g}4_cleanup"] = "failed"
            notes.append(f"{g}4_cleanup FAILED: stack_empty={calm} "
                         f"life={l0}/{l1}")

    if not setup["A"]:
        verdict = "blocked"
        notes.append("verdict: blocked - game A never reached the setup")
    elif ass.get("A2_decision") != "passed":
        verdict = "blocked"
        notes.append("verdict: blocked - game A's trigger decision branch "
                     "was not exercised")
    elif ass["A3_result"] == "passed":
        verdict = "not-reproduced"
        notes.append("verdict: not-reproduced - all 3 attachments moved on "
                     "Ardenn after accepting the trigger")
    else:
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED - accepting the trigger attached "
                     "fewer than all 3 attachments (single-attachment "
                     "collapse), matching the report")
    return ass, notes, verdict


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1240
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#6250 Ardenn, Intrepid Archaeologist: trigger attaches ONE "
         "Aura/Equipment instead of any number", fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("setup: P0 Ardenn + Holy Strength (attached on cast) + Plate Armor + "
         "Colossus Hammer (unattached)", size=16)
    line("A: accept trigger, target Ardenn | B: decline trigger (control)",
         size=16)
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, val in run["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=17)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:16]:
        line(f"  - {n[:112]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.85.0, protocol 72)")

    parse = parse_evidence()
    say("parse:", json.dumps(parse))

    snaps = {}
    results = {}
    for game, mode, p0d, p1d in (("A", "accept", A_P0_DECK, A_P1_DECK),
                                 ("B", "decline", B_P0_DECK, B_P1_DECK)):
        try:
            ok = await play_game(game, mode, p0d, p1d)
            say(f"game {game} finished ok={ok}")
        except Exception as e:
            say(f"game {game} crashed: {e!r}")
            ok = False
        results[game] = ok
        snaps[game] = (ST.get(f"pre{game}"), ST.get(f"post{game}"))
        # stash per-game obs for evaluate()
        ST[f"obs{game}"] = dict(ST.get("obs") or {})
        ST["obs"] = {}

    ass, notes, verdict = evaluate(parse, snaps)
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
        "observed_at": "2026-09-16",
        "source": "fresh isolated server on 127.0.0.1:9374 for run "
                  "6250-20260916-2141; ServerHello confirms v0.85.0/cb58ef5/"
                  "protocol 72; hashes recomputed from on-disk release files "
                  "this run; v0.85.0 confirmed latest stable release (not shell-*)",
    }
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{SERVER_RUN_ID}",
        "server_port": 9374,
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario": "driver/scenario_6250_085.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_6250_085.py"),
        "decks": {"A_P0": A_P0_DECK, "A_P1": A_P1_DECK,
                  "B_P0": B_P0_DECK, "B_P1": B_P1_DECK},
        "games": results,
        "parse_summary": parse,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-16",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x copies per card is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "No second card in the same class (e.g. Brass Squire) exercised; "
            "acceptance criterion noted for the fix, not the repro.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0: 8x Ardenn + 8x holy strength + 8x plate armor + "
                      "8x colossus hammer + 28x plains; P1: 60x mountain dummy. "
                      "Mulligan to Ardenn + lands; cast Ardenn, then holy strength "
                      "(targeting Ardenn), plate armor, colossus hammer; move to "
                      "combat with all three attachments controlled by P0.",
        "contract_line": "Game A: accept the beginning-of-combat trigger, target "
                         "Ardenn, select attachments -- expect all 3 attached to "
                         "Ardenn after resolution. Game B: decline the trigger -- "
                         "expect no attachment moves.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6250_085.py", "w") as f:
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
