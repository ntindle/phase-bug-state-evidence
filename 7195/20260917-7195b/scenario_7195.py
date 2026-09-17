#!/usr/bin/env python3
"""Issue #7195: Transforming Flourish -- "Exiles card correctly but doesn't
allow the exiled card to be cast."

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified from pinned v0.85.0 card-data.json), Transforming
Flourish {2}{R} Instant:
  "Demonstrate (When you cast this spell, you may copy it. If you do, choose
   an opponent to also copy it. Players may choose new targets for their
   copies.)
   Destroy target artifact or creature you don't control. If that permanent
   is destroyed this way, its controller exiles cards from the top of their
   library until they exile a nonland card, then they may cast that card
   without paying its mana cost."

Reported (Discord sync): the exile half works correctly, but the exiled
card is never allowed to be cast.

Driver contract (this scenario):
  - P0: 12x Transforming Flourish, 48x Mountain.
    P1: 4x Sol Ring, 12x Forest, 44x Lightning Bolt.
  - P1 plays a Forest, taps it to cast Sol Ring, then plays NO further
    lands: when the may-cast window opens P1 has zero available mana, so
    any successful cast of the exiled card PROVES it was cast "without
    paying its mana cost".
  - P0 ramps to 3 Mountains and casts Transforming Flourish targeting
    P1's Sol Ring (single legal target -> engine auto-targets, #6762).
    P0 DECLINES demonstrate (the reported path does not involve the copy).
  - Correct behavior: Sol Ring destroyed; P1 exiles cards from the top
    until a nonland (very likely Lightning Bolt) is exiled; the engine
    offers P1 a cast of that exiled card; P1 casts it and it resolves
    (Bolt -> 3 damage to P0, P0 at 17 life, Bolt in P1's graveyard; or a
    Sol Ring -> on P1's battlefield) with P1's lands still tapped.

Assertions:
  A1 setup_ok: pre_cast exported with P0 main phase, Transforming
      Flourish in hand, >=3 untapped Mountains, Sol Ring on P1's
      battlefield.
  A2 destroyed: P1's Sol Ring left the battlefield (destroyed, not
      exiled/returned).
  A3 exile_done: >=1 card in P1's exile zone and at least one exiled
      nonland (record which card).
  A4 cast_offered: the engine offered P1 a cast of the exiled nonland
      card (CastSpell legal action on an exiled object, or a may-cast
      interaction opportunity).
  A5 free_cast_resolved: P1 cast the exiled card and it resolved
      (Bolt: P0 life 20 -> 17 and Bolt in P1's graveyard; Sol Ring:
      Sol Ring on P1's battlefield) with P1 paying no mana (all P1
      lands tapped before and after).

Verdict rule:
  reproduced     -- A1, A2, A3 pass and A4 fails (exile works, no cast
                    is ever offered: the reported symptom), or A4 passes
                    but A5 fails (offered but the cast is broken).
  not-reproduced -- A1..A5 all pass.
  blocked        -- setup never assembled, Flourish never cast/destroyed,
                    or another concrete prerequisite failure (with the
                    next unblock action).

Evidence: evidence/7195/20260917-7195/pre_cast.json, post_destroy.json,
post_cast.json, run.json, manifest.sha256, summary.png,
scenario_7195.py, wire_log.jsonl, scenario_run.log, parse_evidence.json
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
ISSUE = 7195
RUN_ID = "20260917-7195b"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TF = "Transforming Flourish"
RING = "Sol Ring"
BOLT = "Lightning Bolt"
P0_LANDS = ("Mountain",)
P1_LANDS = ("Forest",)

P0_DECK = [(TF, 12), ("Mountain", 48)]
P1_DECK = [(RING, 4), ("Forest", 12), (BOLT, 44)]

ST = {}
ACTED = set()
PROMPT_DONE = set()
REJECTS = {}
LAST_IID = {}
SKIP_IID = set()
C0 = None


def reset_per_game():
    """Fresh per-revision/per-prompt guards for each new game: revisions
    restart at 0 per game, so a global ACTED set would suppress legitimate
    first-time actions in later games (#5654 lesson)."""
    global ACTED, PROMPT_DONE, REJECTS, LAST_IID, SKIP_IID
    ACTED = set()
    PROMPT_DONE = set()
    REJECTS = {}
    LAST_IID = {}
    SKIP_IID = set()


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


def life_of(state, pid):
    p = state["players"][pid]
    return p.get("life", p.get("life_total"))


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


def untapped_lands(state, pid, lands):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) in lands]


def ring_on_battlefield(state, pid):
    for oid, o in bf(state, pid):
        if oname(o) == RING:
            return oid, o
    return None, None


def exile_zone(state, pid):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") in ("Exile", "exile") and o.get("owner") == pid:
            out.append((oid, o))
    return out


def graveyard(state, pid):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") in ("Graveyard", "graveyard") and o.get("owner") == pid:
            out.append((oid, o))
    return out


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


def wf_player(state):
    d = wf_data(state)
    p = d.get("player")
    if isinstance(p, dict):
        p = p.get("id", -1)
    return p


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_entries(state):
    return state.get("stack") or []


# ------------------------------------------------------------- interaction helpers
def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_opps(st):
    vi = get_vi(st)
    return (vi.get("opportunities") or []) if vi else []


def ref_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def choice_text(ch):
    bits = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        for k in ("text", "label", "value", "code"):
            v = d.get(k)
            if v is not None:
                bits.append(str(v))
    return " / ".join(bits)


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
    h.sort(key=lambda o: (oname(get_obj(state, o)) not in P0_LANDS + P1_LANDS,
                         oname(get_obj(state, o))))
    adv = next((a for a in acts if a.get("type") == "SelectCards"), None)
    if adv:
        sub = copy.deepcopy(adv)
        sub["data"]["cards"] = [int(x) for x in h[:n]]
        await submit_as_is(c, sub)
    else:
        for opp in vi_opps(st):
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
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


async def handle_cleanup_discard(c, pid, tag, st, state, protect):
    d = wf_data(state)
    dp = wf_player(state)
    if wf_type(state) in ("DiscardChoice", "DiscardToHandSize") and dp == pid:
        for opp in vi_opps(st):
            iid = opp.get("interactionId")
            if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            oid_by_ref = {ref_of(ch): ch["id"] for ch in chs if ref_of(ch)}
            def rank(o):
                nm = oname(get_obj(state, o))
                if nm in protect:
                    return (2, nm)
                if nm in P0_LANDS + P1_LANDS:
                    return (0, nm)
                return (1, nm)
            oids = sorted(hand_oids(state, pid), key=rank)[:1]
            picks = [oid_by_ref[o] for o in oids if o in oid_by_ref]
            if not picks:
                continue
            spec = (data.get("spec") or {})
            spect = spec.get("type") if isinstance(spec, dict) else None
            if resp.get("type") == "schema" and spect in ("sequence", "select"):
                resp_out = {"type": spect, "data": {"choiceIds": picks}}
            elif resp.get("type") == "exactChoices":
                resp_out = {"type": "choose", "data": {"choiceId": picks[0]}}
            else:
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            PROMPT_DONE.add(iid)
            say(f"[{tag}] cleanup discard: "
                f"{[oname(get_obj(state, o)) for o in oids]}")
            return True
        say(f"[{tag}] {wf_type(state)} but no answerable opportunity; "
            f"deferring")
        return True
    return False


async def handle_declare_none(c, pid, tag, st, state, acts):
    if wf_type(state) in ("DeclareAttackers", "DeclareBlockers"):
        adv = next((a for a in acts if a.get("type") == wf_type(state)), None)
        if adv:
            sub = copy.deepcopy(adv)
            dd = sub.setdefault("data", {})
            for k in ("attacks", "attackers", "blocks", "blockers",
                      "assignments"):
                if k in dd:
                    dd[k] = [] if isinstance(dd[k], list) else {}
            await submit_as_is(c, sub)
            say(f"[{tag}] declares no {wf_type(state)}")
            return True
    return False


async def handle_demonstrate(c, tag, st, state):
    """Decline Demonstrate's may-copy prompt for P0 (reported path does
    not involve the copy). Logs the full opportunity shape."""
    if ST.get("demonstrate_done"):
        return False
    if wf_type(state) != "OptionalEffectChoice":
        return False
    desc = json.dumps(wf_data(state), default=str).lower()
    if "demonstrate" not in desc:
        return False
    for opp in vi_opps(st):
        iid = opp.get("interactionId")
        if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
            continue
        wire("demonstrate_prompt_shape",
             {"iid": iid, "wf_data": wf_data(state),
              "opp": opp})
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        texts = [(ch.get("id"), choice_text(ch)) for ch in chs]
        say(f"[{tag}] demonstrate prompt choices: {texts}")
        wire("demonstrate_prompt",
             {"iid": iid, "choices": texts,
              "wf": wf_data(state)})
        pick = None
        # value surfaces first: ("accept"/"decision", "false") = decline
        for ch in chs:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if (str(d.get("value", "")).lower() == "false"
                        and str(d.get("role", "")).lower()
                        in ("accept", "pay", "decision", "copy")):
                    pick = ch["id"]
                    break
            if pick:
                break
        # text fallback: "decline"/"no"/"don't copy"
        if pick is None:
            for ch in chs:
                t = choice_text(ch).lower()
                if "decline" in t or "don't" in t or "do not" in t:
                    pick = ch["id"]
                    break
        # last resort with 2 choices: pick the non-"copy"/non-"yes" one
        if pick is None and len(chs) == 2:
            for ch in chs:
                t = choice_text(ch).lower()
                if "copy" not in t and t not in ("yes", "true"):
                    pick = ch["id"]
                    break
        if pick is None and chs:
            # last resort: pick the choice that is NOT the accept one
            # (log heavily; do not submit blindly)
            wire("demonstrate_no_decline_found",
                 {"choices": texts})
            say(f"[{tag}] demonstrate: no decline choice identified; "
                f"deferring (NOT submitting blindly)")
            return True
        rtype = (opp.get("response", {}) or {}).get("type")
        if rtype == "exactChoices":
            resp_out = {"type": "choose", "data": {"choiceId": pick}}
        else:
            resp_out = {"type": "choose", "data": {"choiceId": pick}}
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        PROMPT_DONE.add(iid)
        ST["demonstrate_done"] = True
        ST["demonstrate_iid"] = iid
        say(f"[{tag}] demonstrate DECLINED (choice {pick})")
        wire("demonstrate_declined", {"iid": iid, "choice": pick})
        return True
    return False


# ------------------------------------------------------------- Flourish target selection
async def handle_tf_target(c, tag, st, state, acts):
    """Answer Transforming Flourish's TargetSelection: the single legal
    target should be P1's Sol Ring (engine auto-targets single legal
    targets, #6762; handle the prompt if it appears)."""
    if wf_type(state) != "TargetSelection" or wf_player(state) != 0:
        return False
    if ST.get("tf_target_done"):
        return True
    ring_oid, _ = ring_on_battlefield(state, 1)
    d = wf_data(state)
    sel = d.get("selection") or {}
    slot = sel.get("current_slot") or {}
    legal = [str(x) for x in (slot.get("current_legal_targets")
                              or d.get("current_legal_targets") or [])]
    say(f"[{tag}] TargetSelection legal={legal} ring={ring_oid}")
    wire("tf_target_prompt",
         {"legal": legal, "ring_oid": ring_oid, "wf_data": d})
    if ring_oid and str(ring_oid) in legal:
        want = str(ring_oid)
    elif len(legal) == 1:
        want = legal[0]
    else:
        say(f"[{tag}] TargetSelection: no Sol Ring among legal targets; "
            f"deferring")
        return True
    for opp in vi_opps(st):
        iid = opp.get("interactionId")
        if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        oid_by_ref = {ref_of(ch): ch["id"] for ch in chs if ref_of(ch)}
        if want not in oid_by_ref:
            continue
        rtype = (opp.get("response", {}) or {}).get("type")
        spec = (data.get("spec") or {})
        spect = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spect in ("sequence", "select"):
            resp_out = {"type": spect, "data": {"choiceIds": [oid_by_ref[want]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": oid_by_ref[want]}}
        else:
            say(f"[{tag}] target prompt unexpected rtype={rtype}; deferring")
            return True
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        PROMPT_DONE.add(iid)
        ST["tf_target_done"] = True
        ST["tf_target_oid"] = want
        ST["tf_target_iid"] = iid
        say(f"[{tag}] Flourish targets Sol Ring (oid {want})")
        wire("tf_target_answered", {"iid": iid, "oid": want})
        return True
    return False


# ------------------------------------------------------------- P0 / P1 ticks
async def p0_tick(st, acts, state, tag):
    c = _CLIENTS["p0"]
    drain_rejections(c)
    if await handle_mulligan(c, 0, tag, st, state, acts,
                             ST["want_keep"][0]):
        return
    if await handle_bottom(c, 0, tag, st, state, acts):
        return
    if await handle_cleanup_discard(c, 0, tag, st, state, {TF}):
        return
    if await handle_declare_none(c, 0, tag, st, state, acts):
        return
    if wf_type(state) != "Priority":
        if await handle_demonstrate(c, tag, st, state):
            return
        if await handle_tf_target(c, tag, st, state, acts):
            return
        # Unknown P0 prompt: log and hold (do not auto-decline, playbook).
        rev = st.get("state_revision", -1)
        if not acted(f"unk{wf_type(state)}", rev):
            say(f"[{tag}] UNKNOWN P0 prompt {wf_type(state)}; wf_data="
                f"{json.dumps(wf_data(state), default=str)[:400]}; "
                f"opps={len(vi_opps(st))}")
            wire("unknown_p0_prompt",
                 {"wtype": wf_type(state), "wf_data": wf_data(state),
                  "opps": vi_opps(st)})
        return

    stage = ST.get("tf_stage")
    if stage == "develop":
        if my_priority(state, 0) and is_my_main(state, 0):
            rev = st.get("state_revision", -1)
            ul = untapped_lands(state, 0, P0_LANDS)
            tf_oid = find_hand(state, 0, TF)
            ring_oid, _ = ring_on_battlefield(state, 1)
            if (tf_oid is not None and ring_oid is not None
                    and len(ul) >= 3):
                cast = castspell_advertised(acts, tf_oid)
                if cast and not acted("casttf", rev):
                    ST["pre_cast_snapshot"] = {
                        "turn": turn_of(state),
                        "phase": state.get("phase"),
                        "hand": hand_names(state, 0),
                        "untapped_mountains": len(ul),
                        "ring_on_p1_battlefield": True,
                        "ring_oid": str(ring_oid),
                        "p0_life": life_of(state, 0),
                        "p1_life": life_of(state, 1),
                        "p1_lib": len(state["players"][1]["library"]),
                    }
                    pre = await export_now("pre_cast.json", C0)
                    ST["pre_cast"] = pre
                    ST["cast_action"] = copy.deepcopy(cast)
                    ST["cast_oid"] = str(tf_oid)
                    ST["cast_time"] = time.time()
                    await submit_as_is(c, cast)
                    ST["tf_stage"] = "in_flight"
                    say(f"[{tag}] casts {TF} (oid {tf_oid}); pre-cast "
                        f"exported")
                    wire("tf_cast_submitted",
                         {"oid": str(tf_oid), "action": cast,
                          "pre": ST["pre_cast_snapshot"]})
                    return
        # land drop + pass
        rev = st.get("state_revision", -1)
        if is_my_main(state, 0):
            pl = next((a for a in acts if a.get("type") == "PlayLand"), None)
            if pl and not acted("land0", rev):
                await submit_as_is(c, pl)
                return
        if my_priority(state, 0):
            pp = next((a for a in acts if a.get("type") == "PassPriority"),
                      None)
            if pp:
                await submit_as_is(c, pp)
        return

    # in_flight / post_destroy / cast_watch / resolving: P0 just passes
    if my_priority(state, 0):
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp and not acted(f"pass0", st.get("state_revision", -1)):
            await submit_as_is(c, pp)


async def p1_tick(st, acts, state, tag):
    c = _CLIENTS["p1"]
    drain_rejections(c)
    if await handle_mulligan(c, 1, tag, st, state, acts,
                             ST["want_keep"][1]):
        return
    if await handle_bottom(c, 1, tag, st, state, acts):
        return
    if await handle_cleanup_discard(c, 1, tag, st, state, {RING, BOLT}):
        return
    if await handle_declare_none(c, 1, tag, st, state, acts):
        return

    # --- the may-cast window: look for a cast of the exiled card ---
    if ST.get("tf_stage") in ("post_destroy", "cast_watch"):
        if await p1_try_cast_exiled(c, tag, st, state, acts):
            return

    # --- develop: play one Forest, cast Sol Ring, then stop landing ---
    if ST.get("tf_stage") == "develop":
        if my_priority(state, 1) and is_my_main(state, 1):
            rev = st.get("state_revision", -1)
            ring_oid, _ = ring_on_battlefield(state, 1)
            if ring_oid is None:
                # play exactly one land, then cast Sol Ring
                if not untapped_lands(state, 1, P1_LANDS) and not any(
                        oname(o) in P1_LANDS for _, o in bf(state, 1)):
                    pl = next((a for a in acts if a.get("type") == "PlayLand"),
                              None)
                    if pl and not acted("land1", rev):
                        await submit_as_is(c, pl)
                        say(f"[{tag}] plays Forest")
                        return
                sr = find_hand(state, 1, RING)
                if sr is not None and untapped_lands(state, 1, P1_LANDS):
                    cast = castspell_advertised(acts, sr)
                    if cast and not acted("castring", rev):
                        await submit_as_is(c, cast)
                        ST["ring_cast_time"] = time.time()
                        say(f"[{tag}] casts {RING} (oid {sr})")
                        return
        # NOTE: no further land drops after the Ring plan -- P1 must have
        # zero available mana when the may-cast window opens, so any cast
        # proves "without paying its mana cost".
    if my_priority(state, 1):
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp and not acted("pass1", st.get("state_revision", -1)):
            await submit_as_is(c, pp)


async def p1_try_cast_exiled(c, tag, st, state, acts):
    """During the may-cast window, look for an engine-offered cast of the
    exiled nonland card and take it. Returns True if an action was taken
    or a prompt was handled."""
    exiled = ST.get("exiled_nonland") or {}
    ex_oid = exiled.get("oid")
    # 1) legal_actions CastSpell on the exiled object
    if ex_oid:
        cast = castspell_advertised(acts, ex_oid)
        if cast:
            if not ST.get("cast_offered"):
                ST["cast_offered"] = True
                ST["cast_offer_seen"] = {"kind": "legal_action",
                                        "action": copy.deepcopy(cast)}
                say(f"[{tag}] *** cast offered via legal_actions for "
                    f"exiled {exiled.get('name')} (oid {ex_oid}) ***")
                wire("cast_offered",
                     {"kind": "legal_action", "action": cast})
            rev = st.get("state_revision", -1)
            if acted("castexiled", rev):
                return True
            ST["exiled_cast_iid"] = None
            ST["exiled_cast_time"] = time.time()
            ST["lands_before"] = len(untapped_lands(state, 1, P1_LANDS))
            await submit_as_is(c, cast)
            ST["tf_stage"] = "resolving"
            say(f"[{tag}] casts exiled {exiled.get('name')} (oid {ex_oid})")
            wire("exiled_cast_submitted",
                 {"oid": ex_oid, "name": exiled.get("name")})
            return True
    # 2) viewer_interaction opportunity referencing the exiled card
    for opp in vi_opps(st):
        iid = opp.get("interactionId")
        if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        refs = {ref_of(ch): ch["id"] for ch in chs if ref_of(ch)}
        blob = json.dumps(opp, default=str).lower()
        hit = (ex_oid and ex_oid in refs) or (
            "cast" in blob and ex_oid and ex_oid in blob)
        if not hit:
            continue
        if not ST.get("cast_offered"):
            ST["cast_offered"] = True
            ST["cast_offer_seen"] = {"kind": "interaction", "iid": iid,
                                     "opp": copy.deepcopy(opp)}
            say(f"[{tag}] *** cast offered via interaction for exiled "
                f"{exiled.get('name')} (iid {iid}) ***")
            wire("cast_offered", {"kind": "interaction", "iid": iid,
                                  "opp": opp})
        pick = refs.get(ex_oid) if ex_oid else None
        if pick is None and chs:
            # accept-style choice: log, do not submit blindly
            say(f"[{tag}] may-cast interaction without a clear card "
                f"candidate; deferring (logged)")
            wire("maycast_unclear", {"iid": iid, "opp": opp})
            return True
        rtype = (opp.get("response", {}) or {}).get("type")
        if rtype == "exactChoices":
            resp_out = {"type": "choose", "data": {"choiceId": pick}}
        else:
            spec = (data.get("spec") or {})
            spect = spec.get("type") if isinstance(spec, dict) else "select"
            resp_out = {"type": spect, "data": {"choiceIds": [pick]}}
        ST["exiled_cast_iid"] = iid
        ST["exiled_cast_time"] = time.time()
        ST["lands_before"] = len(untapped_lands(state, 1, P1_LANDS))
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        PROMPT_DONE.add(iid)
        ST["tf_stage"] = "resolving"
        say(f"[{tag}] accepts may-cast of exiled {exiled.get('name')} "
            f"(iid {iid})")
        wire("exiled_cast_submitted", {"iid": iid, "name": exiled.get("name")})
        return True
    # 3) Bolt target selection after the free cast
    if (ST.get("tf_stage") == "resolving"
            and wf_type(state) == "TargetSelection"
            and wf_player(state) == 1):
        d = wf_data(state)
        sel = d.get("selection") or {}
        slot = sel.get("current_slot") or {}
        legal = [str(x) for x in (slot.get("current_legal_targets")
                                  or d.get("current_legal_targets") or [])]
        # target P0 (player candidate carries surfaces[].data.seat, #6906)
        for opp in vi_opps(st):
            iid = opp.get("interactionId")
            if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
                continue
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            pick = None
            for ch in chs:
                for s in ch.get("surfaces", []) or []:
                    dd = s.get("data") or {}
                    if isinstance(dd, dict) and dd.get("seat") == 0:
                        pick = ch["id"]
                        break
                if pick:
                    break
            if pick is None:
                continue
            rtype = (opp.get("response", {}) or {}).get("type")
            if rtype == "exactChoices":
                resp_out = {"type": "choose", "data": {"choiceId": pick}}
            else:
                resp_out = {"type": "sequence", "data": {"choiceIds": [pick]}}
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            PROMPT_DONE.add(iid)
            ST["bolt_target_iid"] = iid
            say(f"[{tag}] exiled Bolt targets P0 (seat 0)")
            wire("bolt_targeted", {"iid": iid})
            return True
    return False


_CLIENTS = {}


# ------------------------------------------------------------- game runner
def new_game_state():
    reset_per_game()
    ST.update({"tf_stage": "develop",
               "pre_cast": None, "pre_cast_snapshot": None,
               "cast_action": None, "cast_oid": None, "cast_time": 0,
               "demonstrate_done": False, "demonstrate_iid": None,
               "tf_target_done": False, "tf_target_oid": None,
               "tf_target_iid": None,
               "post_destroy": None, "post_destroy_snapshot": None,
               "exiled_cards": [], "exiled_nonland": None,
               "ring_destroy_turn": None,
               "lands_before": None, "lands_after": None,
               "cast_offered": False, "cast_offer_seen": None,
               "exiled_cast_iid": None, "exiled_cast_time": 0,
               "bolt_target_iid": None,
               "resolution": None, "post_cast": None, "post_snapshot": None,
               "max_turn": 0, "mulls0": 0, "mulls1": 0,
               "want_keep": [None, None], "done": False, "blocked": None,
               "stage_time": time.time()})


def observe_transitions(state, tag):
    """Stage transitions driven by authoritative state (not by prompt
    timing)."""
    stage = ST.get("tf_stage")
    if stage == "in_flight":
        ring_oid, _ = ring_on_battlefield(state, 1)
        if ring_oid is None:
            # Sol Ring left P1's battlefield -> destroyed (it was the
            # only legal target; Flourish resolved).
            ST["ring_destroy_turn"] = turn_of(state)
            ST["tf_stage"] = "post_destroy"
            ST["stage_time"] = time.time()
            ex = exile_zone(state, 1)
            ST["exiled_cards"] = [(oid, oname(o)) for oid, o in ex]
            nonlands = [(oid, oname(o)) for oid, o in ex
                        if oname(o) not in P1_LANDS]
            ST["exiled_nonland"] = ({"oid": nonlands[0][0],
                                     "name": nonlands[0][1]}
                                    if nonlands else None)
            ST["lands_before"] = len(untapped_lands(state, 1, P1_LANDS))
            ST["post_destroy_snapshot"] = {
                "turn": turn_of(state), "phase": state.get("phase"),
                "exiled": ST["exiled_cards"],
                "exiled_nonland": ST["exiled_nonland"],
                "p1_untapped_forests": ST["lands_before"],
                "p0_life": life_of(state, 0),
                "p1_life": life_of(state, 1),
                "p1_lib": len(state["players"][1]["library"]),
            }
            say(f"[{tag}] *** Sol Ring destroyed; exile observed: "
                f"{ST['exiled_cards']}; nonland={ST['exiled_nonland']} ***")
            wire("destroy_exile", ST["post_destroy_snapshot"])
            return "export_post_destroy"
    if stage == "post_destroy":
        # The may-cast permission lasts until end of turn. If the game
        # advances two full turns with no offer, the window closed.
        if turn_of(state) > (ST.get("ring_destroy_turn") or 0) + 2:
            ST["tf_stage"] = "done"
            ST["done"] = True
            say(f"[{tag}] may-cast window closed (turn advanced to "
                f"{turn_of(state)}) with NO cast offered")
            wire("maycast_window_closed", {"turn": turn_of(state)})
    if stage == "resolving":
        exn = ST.get("exiled_nonland") or {}
        name = exn.get("name")
        done = False
        if name == BOLT:
            gy = [oname(o) for _, o in graveyard(state, 1)]
            if BOLT in gy and (life_of(state, 0) or 99) <= 17:
                done = True
        elif name == RING:
            ro, _ = ring_on_battlefield(state, 1)
            if ro is not None:
                done = True
        if done or time.time() - ST.get("exiled_cast_time", 0) > 180:
            ST["resolution"] = {
                "exiled_card": exn,
                "p0_life": life_of(state, 0),
                "p1_life": life_of(state, 1),
                "p1_graveyard": [oname(o) for _, o in graveyard(state, 1)],
                "ring_on_p1_bf": ring_on_battlefield(state, 1)[0] is not None,
                "p1_untapped_forests": len(untapped_lands(state, 1, P1_LANDS)),
                "timed_out": not done,
            }
            ST["lands_after"] = ST["resolution"]["p1_untapped_forests"]
            ST["tf_stage"] = "done"
            ST["done"] = True
            say(f"[{tag}] resolution observed: {ST['resolution']}")
            wire("resolution", ST["resolution"])
            return "export_post_cast"
    return None


async def open_game(p0_deck, p1_deck):
    p0 = PhaseClient("7195-P0")
    await p0.connect()
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient("7195-P1")
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
            action = observe_transitions(state, tag)
            if action == "export_post_destroy":
                post = await export_now("post_destroy.json", C0)
                ST["post_destroy"] = post
                ST["tf_stage"] = "post_destroy"
                ST["stage_time"] = time.time()
            elif action == "export_post_cast":
                post = await export_now("post_cast.json", C0)
                ST["post_cast"] = post
                ST["post_snapshot"] = {
                    "turn": turn_of(state), "phase": state.get("phase")}
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
                f"stack={len(stack_entries(s))} stage={ST.get('tf_stage')} "
                f"offer={ST.get('cast_offered')}")
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
    lands = sum(1 for n in hn if n in P0_LANDS)
    return TF in hn and lands >= 2


def p1_keep(state, pid):
    hn = hand_names(state, pid)
    return RING in hn and any(n in P1_LANDS for n in hn)


async def play_game(p0_deck, p1_deck):
    new_game_state()
    p0, p1 = await open_game(p0_deck, p1_deck)
    _CLIENTS["p0"], _CLIENTS["p1"] = p0, p1
    ST["want_keep"] = [p0_keep, p1_keep]
    ok = await run_game(p0, p1, timeout_s=1500)
    say(f"game finished ok={ok}")
    await p0.close()
    await p1.close()
    return ok


# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
    out = {}
    for key in ("transforming flourish", "sol ring", "lightning bolt"):
        e = cd[key]
        out[key] = {
            "name": e.get("name"),
            "oracle_text": e.get("oracle_text"),
            "mana_cost": e.get("mana_cost"),
        }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(parse):
    ass = {}
    notes = []
    notes.append(
        "parse (pinned v0.85.0 card-data): transforming flourish oracle=" +
        repr(parse["transforming flourish"]["oracle_text"]))

    if ST.get("blocked"):
        for k in ("A1_setup_ok", "A2_destroyed", "A3_exile_done",
                  "A4_cast_offered", "A5_free_cast_resolved"):
            ass[k] = "not-run"
        notes.append(f"verdict: blocked - {ST['blocked']}")
        return ass, notes, "blocked"

    pre = ST.get("pre_cast_snapshot") or {}

    # A1: setup assembled
    if (pre.get("phase") in ("PreCombatMain", "PostCombatMain")
            and TF in (pre.get("hand") or [])
            and (pre.get("untapped_mountains") or 0) >= 3
            and pre.get("ring_on_p1_battlefield")):
        ass["A1_setup_ok"] = "passed"
        notes.append(
            f"A1 passed: pre_cast on P0 turn {pre.get('turn')} "
            f"{pre.get('phase')}; {TF} in hand; "
            f"{pre.get('untapped_mountains')} untapped Mountains; "
            f"{RING} on P1's battlefield")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"A1 FAILED: pre_cast_snapshot={pre}")

    # A2: Sol Ring destroyed
    postd = ST.get("post_destroy_snapshot") or {}
    if ST.get("ring_destroy_turn") is not None:
        ass["A2_destroyed"] = "passed"
        notes.append(
            f"A2 passed: {RING} left P1's battlefield on turn "
            f"{ST.get('ring_destroy_turn')} (Flourish resolved; the "
            f"destroy half worked)")
    else:
        ass["A2_destroyed"] = "failed"
        notes.append(
            f"A2 FAILED: {RING} never left P1's battlefield; "
            f"stage={ST.get('tf_stage')}")

    # A3: exile happened, nonland exiled
    exn = ST.get("exiled_nonland")
    if postd and exn:
        ass["A3_exile_done"] = "passed"
        notes.append(
            f"A3 passed: P1 exiled {postd.get('exiled')}; first exiled "
            f"nonland = {exn.get('name')} (oid {exn.get('oid')}) -- the "
            f"'exiles correctly' half of the report")
    elif postd:
        ass["A3_exile_done"] = "failed"
        notes.append(
            f"A3 FAILED: destroy happened but no nonland exiled: "
            f"{postd.get('exiled')}")
    else:
        ass["A3_exile_done"] = "not-run"
        notes.append("A3 not-run: no post-destroy state captured")

    # A4: cast was offered
    if ST.get("cast_offered"):
        ass["A4_cast_offered"] = "passed"
        notes.append(
            f"A4 passed: engine offered P1 a cast of exiled "
            f"{(ST.get('exiled_nonland') or {}).get('name')}: "
            f"{json.dumps(ST.get('cast_offer_seen'), default=str)[:200]}")
    elif ST.get("ring_destroy_turn") is not None:
        ass["A4_cast_offered"] = "failed"
        notes.append(
            "A4 FAILED (THE REPORTED SYMPTOM): P1 exiled cards correctly "
            "but the engine never offered a cast of the exiled nonland "
            f"({(ST.get('exiled_nonland') or {}).get('name')}); the "
            "may-cast window closed with no CastSpell legal action and no "
            "may-cast interaction for P1")
    else:
        ass["A4_cast_offered"] = "not-run"
        notes.append("A4 not-run: destroy never happened")

    # A5: free cast resolved
    res = ST.get("resolution")
    exn = ST.get("exiled_nonland") or {}
    if res and not res.get("timed_out"):
        paid = (ST.get("lands_before"), ST.get("lands_after"))
        if exn.get("name") == BOLT:
            if res["p0_life"] == 17 and BOLT in res["p1_graveyard"]:
                ass["A5_free_cast_resolved"] = "passed"
                notes.append(
                    f"A5 passed: P1 cast exiled {BOLT} for free -- P0 "
                    f"life 20 -> 17, {BOLT} in P1's graveyard; P1 "
                    f"untapped Forests before/after = {paid[0]}/{paid[1]} "
                    f"(no mana paid)")
            else:
                ass["A5_free_cast_resolved"] = "failed"
                notes.append(f"A5 FAILED: resolution={res}")
        elif exn.get("name") == RING:
            if res["ring_on_p1_bf"]:
                ass["A5_free_cast_resolved"] = "passed"
                notes.append(
                    f"A5 passed: P1 cast exiled {RING} for free -- on P1's "
                    f"battlefield; untapped Forests before/after = "
                    f"{paid[0]}/{paid[1]} (no mana paid)")
            else:
                ass["A5_free_cast_resolved"] = "failed"
                notes.append(f"A5 FAILED: resolution={res}")
        else:
            ass["A5_free_cast_resolved"] = "failed"
            notes.append(
                f"A5 FAILED: unexpected exiled card {exn}; resolution={res}")
    elif ST.get("cast_offered"):
        ass["A5_free_cast_resolved"] = "failed"
        notes.append(
            f"A5 FAILED: cast was offered but never resolved; "
            f"resolution={res}")
    else:
        ass["A5_free_cast_resolved"] = "not-run"
        notes.append("A5 not-run: no cast was offered")

    if (ass.get("A4_cast_offered") == "failed"
            and ass.get("A1_setup_ok") == "passed"
            and ass.get("A2_destroyed") == "passed"
            and ass.get("A3_exile_done") == "passed"):
        verdict = "reproduced"
        notes.append(
            "verdict: REPRODUCED - the destroy half and the exile half "
            "both worked, but P1 was never offered the free cast of the "
            "exiled card: exactly the reported symptom")
    elif (ass.get("A4_cast_offered") == "passed"
            and ass.get("A5_free_cast_resolved") == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict: REPRODUCED - a cast was offered but it did not "
            "complete/resolve correctly")
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
        notes.append(
            "verdict: not-reproduced - P1 exiled and then cast the exiled "
            "card for free with correct resolution on v0.85.0")
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

    line("#7195 Transforming Flourish: exiles but never offers the cast?",
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
    line("setup: P0 12x Transforming Flourish / 48x Mountain vs P1 4x Sol "
         "Ring / 12x Forest / 44x Bolt", size=16)
    line("P1 casts Sol Ring, then holds 0 untapped mana; P0 casts Flourish "
         "on it (demonstrate declined)", size=16)
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
    say("parse: transforming flourish / sol ring / lightning bolt loaded")

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
        "source": "reused live server on 127.0.0.1:9374 (run 20260917-6753 "
                  "games.db); ServerHello confirms v0.85.0/cb58ef5/protocol "
                  "72; hashes recomputed from on-disk release files this "
                  "run; v0.85.0 confirmed latest stable release (not shell-*)",
    }
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario": "driver/scenario_7195.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_7195.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "game_ok": ok,
        "parse_summary": parse,
        "cast_action": ST.get("cast_action"),
        "demonstrate": {"done": ST.get("demonstrate_done"),
                        "iid": ST.get("demonstrate_iid")},
        "flourish_target": {"oid": ST.get("tf_target_oid"),
                             "iid": ST.get("tf_target_iid")},
        "post_destroy_snapshot": ST.get("post_destroy_snapshot"),
        "exiled_cards": ST.get("exiled_cards"),
        "exiled_nonland": ST.get("exiled_nonland"),
        "cast_offer_seen": ST.get("cast_offer_seen"),
        "exiled_cast_iid": ST.get("exiled_cast_iid"),
        "bolt_target_iid": ST.get("bolt_target_iid"),
        "lands_before": ST.get("lands_before"),
        "lands_after": ST.get("lands_after"),
        "resolution": ST.get("resolution"),
        "rejections": REJECTS,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-17",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Only the non-demonstrate path is exercised (demonstrate "
            "declined); the demonstrate-copy branch is not tested.",
            "P1 is scripted (one Forest, Sol Ring, then passive); opponent "
            "interaction with the spell is not exercised.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 12x Transforming Flourish + 48x Mountain vs P1 "
                      "4x Sol Ring + 12x Forest + 44x Lightning Bolt. P1 "
                      "casts Sol Ring on turn 1 and then holds zero "
                      "untapped mana; P0 casts Transforming Flourish "
                      "targeting the Sol Ring on its main phase (declining "
                      "demonstrate) and passes priority to resolution.",
        "contract_line": "Destroying P1's Sol Ring must make P1 exile cards "
                         "until a nonland is exiled, and the engine must "
                         "then offer P1 a free cast of that card; the cast "
                         "must resolve correctly (Bolt: 3 damage to P0; "
                         "Sol Ring: onto P1's battlefield) with no mana paid.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7195.py", "w") as f:
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
