#!/usr/bin/env python3
"""Issue #7191: Acorn Catapult -- Squirrel token goes to the Catapult's
controller instead of the damaged permanent's controller / damaged player.

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified from pinned v0.85.0 card-data.json):
  Acorn Catapult {4} artifact:
    "{1}, {T}: This artifact deals 1 damage to any target. That permanent's
     controller or that player creates a 1/1 green Squirrel creature token."

Reported (Discord, status:confirmed; triage + classifier unsupported_aspect):
  The 1 damage is dealt correctly, but the Squirrel token is created under the
  control of Acorn Catapult's owner instead of the damaged permanent's
  controller (or the damaged player). In pinned card-data the token clause
  parses to effect.type == "Unimplemented" (name "unbound_subject") after the
  supported DealDamage node; triage notes the engine falls back to the source
  controller as the token's controller.

Acceptance criteria (from triage):
  (1) Damaging a permanent gives its controller the token.
  (2) Damaging a player gives that player the token.
  (3) The Catapult controller receives it only when they are also the damaged
      recipient.

Game A (reported case): P0 casts Acorn Catapult, activates it targeting P1's
Grizzly Bears (2/2, survives the 1 damage). Expected: P1 controls the Squirrel
after resolution.

Game B: P0 activates Acorn Catapult targeting P1 (the player). Expected: P1
controls the Squirrel after resolution.

Game C (control): P0 activates Acorn Catapult targeting P0's own Grizzly
Bears. Expected: P0 controls the Squirrel (Catapult controller IS the damaged
recipient) -- passes under both correct and fallback behavior.

Assertions per game G in {A,B,C}:
  G1_setup     pre: Catapult on P0 battlefield (untapped), intended target in
               place, mana available, life 20/20.
  G2_resolved  post: ability resolved (Catapult tapped, no ability entry on the
               stack, damage/life delta observed).
  G3_token     >=1 Squirrel token on the battlefield post-resolution.
  G4_recipient Squirrel token(s) controlled by the damaged permanent's
               controller (A/C) or the damaged player (B).

Verdict rule:
  reproduced  -- setup reached in >=1 game AND (a token was created for the
                 Catapult controller instead of the damaged recipient in A or B
                 [the reported failure], OR no token was created at all while
                 the ability resolved [clearly identified related failure of
                 the same unbound-subject clause]).
  not-reproduced -- setup reached and G4 passes in every completed game.
  blocked -- no game reached its activation setup.

Evidence: evidence/7191/<run-id>/pre_{A,B,C}.json, post_{A,B,C}.json,
parse_evidence.json, run.json, manifest.sha256, summary.png,
scenario_7191_085.py, wire_log.jsonl, scenario_run.log
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
ISSUE = 7191
RUN_ID = "20260916-7191"
SERVER_RUN_ID = RUN_ID
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CATAPULT = "Acorn Catapult"
BEAR = "Grizzly Bears"
ISLAND = "Island"
FOREST = "Forest"
LANDS = (ISLAND, FOREST, "Swamp", "Plains", "Mountain")

A_P0_DECK = [(CATAPULT, 8), (ISLAND, 52)]
A_P1_DECK = [(BEAR, 12), (FOREST, 48)]
B_P0_DECK = [(CATAPULT, 8), (ISLAND, 52)]
B_P1_DECK = [(FOREST, 60)]
C_P0_DECK = [(CATAPULT, 8), (BEAR, 8), (ISLAND, 26), (FOREST, 18)]
C_P1_DECK = [(FOREST, 60)]

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


def lib_size(state, pid):
    return len(state["players"][pid]["library"])


def life(state, pid):
    return state["players"][pid]["life"]


def hand_oids(state, pid):
    return [str(x) for x in state["players"][pid]["hand"]]


def gy_oids(state, pid):
    return [str(x) for x in state["players"][pid]["graveyard"]]


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


def bf_find(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def untapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) in LANDS]


def squirrels(state):
    """(oid, obj) for every Squirrel-token-looking object on the battlefield."""
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") != "Battlefield":
            continue
        nm = oname(o)
        subs = []
        ct = o.get("card_type") or {}
        if isinstance(ct, dict):
            subs = ct.get("subtypes") or []
        kw = " ".join(str(x) for x in (o.get("keywords") or [])).lower()
        if ("squirrel" in nm.lower()
                or any("squirrel" in str(s).lower() for s in subs)
                or "squirrel" in kw):
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


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_entries(state):
    return state.get("stack") or []


def stack_has_ability(state, name_sub):
    for e in stack_entries(state):
        o = e if isinstance(e, dict) else get_obj(state, e)
        if name_sub.lower() in oname(o).lower():
            return True
        sid = (e.get("source_id") if isinstance(e, dict) else None)
        if sid is not None and str(sid) == str(ST.get("catapult_oid")):
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


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("seat") is not None:
            return d.get("seat")
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
        if nm in LANDS:
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
    # #4509 lesson: do not mark answered until the submission is accepted.
    # Caller marks PROMPT_DONE only when this returns True.
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
    adv = next((a for a in acts if a.get("type") == "PassPriority"), None)
    if adv:
        await submit_as_is(c, adv)
        return True
    return False


def economy_tick(c, pid, st, acts, state, tag):
    """Land drop in main + priority-gated pass. Returns (kind, action) or None."""
    rev = st.get("state_revision", -1)
    if is_my_main(state, pid):
        pl = next((a for a in acts if a.get("type") == "PlayLand"), None)
        if pl and not acted(f"land{pid}", rev):
            return ("land", pl)
    if my_priority(state, pid):
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp:
            return ("pass", pp)
    return None


async def answer_catapult_target(c, pid, tag, st, state, acts):
    """Answer the Catapult activation's TargetSelection: one choice per prompt
    (CR 601.2c, #7179 lesson). ST['target_spec'] is ('oid', oid) for a
    permanent or ('player', seat) for a player."""
    wtype = wf_type(state)
    if wtype not in ("TargetSelection", "TriggerTargetSelection"):
        return False
    d = wf_data(state)
    dp = d.get("player")
    if isinstance(dp, dict):
        dp = dp.get("id", -1)
    if dp != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    spec = ST.get("target_spec")
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in PROMPT_DONE or iid in SKIP_IID:
            continue
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        pick = None
        if spec and spec[0] == "oid":
            for ch in chs:
                if ref_of(ch) == str(spec[1]):
                    pick = ch
                    break
        elif spec and spec[0] == "player":
            for ch in chs:
                if seat_of(ch) == spec[1] and ref_of(ch) is None:
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    if seat_of(ch) == spec[1]:
                        pick = ch
                        break
        if pick is None:
            continue
        wire("target_answer", {"tag": tag, "wtype": wtype, "iid": iid,
                              "spec": spec, "choice": pick["id"]})
        await send_interaction(c, {"interactionId": iid,
                                  "response": {"type": "sequence",
                                               "data": {"choiceIds": [pick["id"]]}}})
        PROMPT_DONE.add(iid)
        ST.setdefault("targets_answered", []).append(
            {"game": ST["game"], "wtype": wtype, "iid": iid,
             "spec": spec, "choice": pick["id"]})
        ST["target_answered"] = True
        say(f"[{tag}] answers {wtype} -> {spec}")
        return True
    say(f"[{tag}] {wtype}: no matching candidate for {spec}")
    return False


async def maybe_activate(c, tag, st, state, acts):
    """Submit the advertised ActivateAbility for the Catapult as-is; record
    the catapult oid and export PRE once."""
    if ST.get("activated"):
        return False
    cat = bf_find(state, 0, CATAPULT)
    if cat is None:
        return False
    o = get_obj(state, cat)
    if o.get("tapped"):
        return False
    if not is_my_main(state, 0):
        return False
    if ST.get("target_spec") is None:
        # bear legs: don't activate before the intended target exists
        return False
    if len(untapped_lands(state, 0)) < 1:
        return False
    act = next((a for a in acts
                if a.get("type") == "ActivateAbility"
                and str((a.get("data") or {}).get("source_id")) == str(cat)),
               None)
    if act is None:
        return False
    ST["catapult_oid"] = str(cat)
    say(f"[{tag}] exporting PRE_{ST['game']}, then activating Catapult")
    ST[f"pre{ST['game']}"] = await export_now(f"pre_{ST['game']}.json", C0)
    wire("activate_catapult", act)
    await submit_as_is(c, act)
    ST["activated"] = True
    say(f"[{tag}] activated Acorn Catapult targeting {ST['target_spec']}")
    return True


def check_resolved(state):
    """True once the activated ability has left the stack after targeting."""
    if not (ST.get("activated") and ST.get("target_answered")):
        return False
    if stack_has_ability(state, CATAPULT):
        return False
    cat = bf_find(state, 0, CATAPULT)
    if cat is None:
        return False
    if not get_obj(state, cat).get("tapped"):
        return False
    return True


# ------------------------------------------------------------- game runner
def new_game_state(game, target_spec):
    reset_per_game()
    ST.update({"game": game, "target_spec": target_spec,
               "activated": False, "target_answered": False,
               "catapult_oid": None, "pre" + game: None, "post" + game: None,
               "targets_answered": ST.get("targets_answered", []),
               "max_turn": 0, "states_seen": 0,
               "mulls0": 0, "mulls1": 0, "want_keep": [None, None],
               "bear_turn": None, "cast_turn": None, "stack_seen": False})


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


async def run_game(p0, p1, tick0, tick1, done_pred, timeout_s):
    t0 = time.time()
    last = {}
    last_tick = {}
    last_adv = {p0.name: time.time(), p1.name: time.time()}
    last_diag = 0.0
    warned = set()
    for i in range(int(timeout_s / 0.2)):
        await asyncio.sleep(0.2)
        for c, tick, pid in ((p0, tick0, 0), (p1, tick1, 1)):
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
            ST["states_seen"] += 1
            state = st["state"]
            ST["max_turn"] = max(ST["max_turn"], turn_of(state))
            acts = list(st.get("legal_actions") or [])
            try:
                await tick(st, acts, state)
            except Exception as e:
                say(f"[{c.name}] tick error: {e!r}")
            if done_pred():
                say(f"[{ST['game']}] contract complete; finishing game")
                return True
        now = time.time()
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            say(f"[{ST['game']}] DIAG turn={turn_of(s)} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={wf_type(s)} "
                f"P0hand={len(hand_names(s,0))} P1hand={len(hand_names(s,1))} "
                f"stack={len(stack_entries(s))} sq={len(squirrels(s))}")
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


# ------------------------------------------------------------- shared tick logic
async def pay_as_is(c, acts, tag):
    for a in acts:
        if a.get("type") in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
            await submit_as_is(c, a)
            say(f"[{tag}] mana payment submitted as-is")
            return True
    return False


def p0_keep_catapult(state, pid, need_bear=False):
    hn = hand_names(state, pid)
    lands = sum(1 for n in hn if n in LANDS)
    ok = CATAPULT in hn and lands >= 2
    if need_bear:
        ok = ok and BEAR in hn
    return ok


def p1_keep_bear(state, pid):
    hn = hand_names(state, pid)
    lands = sum(1 for n in hn if n == FOREST)
    return BEAR in hn and lands >= 1


async def p0_tick_shared(st, acts, state, tag, cast_bear=False):
    drain_rejections(p0_client())
    if await answer_catapult_target(p0_client(), 0, tag, st, state, acts):
        return
    if await handle_nonpriority(p0_client(), 0, tag, st, state, acts,
                               protect=(CATAPULT, BEAR)):
        return
    if ST.get("activated") and ST.get(f"post{ST['game']}") is None:
        if check_resolved(state):
            say(f"[{tag}] ability resolved; exporting POST_{ST['game']}")
            ST[f"post{ST['game']}"] = await export_now(f"post_{ST['game']}.json", C0)
            return
        if stack_has_ability(state, CATAPULT) and not ST["stack_seen"]:
            ST["stack_seen"] = True
            say(f"[{tag}] ability on stack")
    await pay_as_is(p0_client(), acts, tag)
    if not my_priority(state, 0):
        return
    if cast_bear:
        b = find_hand(state, 0, BEAR)
        ul = untapped_lands(state, 0)
        if (b is not None and bf_find(state, 0, BEAR) is None
                and is_my_main(state, 0) and len(ul) >= 2
                and sum(1 for x in ul if oname(get_obj(state, x)) == FOREST) >= 1):
            cast = castspell_advertised(acts, b)
            if cast:
                ST["bear_turn"] = turn_of(state)
                await submit_as_is(p0_client(), cast)
                say(f"[{tag}] casts {BEAR} (turn {ST['bear_turn']})")
                return
    if not ST.get("activated"):
        cp = find_hand(state, 0, CATAPULT)
        ul = untapped_lands(state, 0)
        if (cp is not None and bf_find(state, 0, CATAPULT) is None
                and is_my_main(state, 0) and len(ul) >= 4):
            cast = castspell_advertised(acts, cp)
            if cast:
                ST["cast_turn"] = turn_of(state)
                await submit_as_is(p0_client(), cast)
                say(f"[{tag}] casts {CATAPULT} (turn {ST['cast_turn']})")
                return
        if await maybe_activate(p0_client(), tag, st, state, acts):
            return
    r = economy_tick(p0_client(), 0, st, acts, state, tag)
    if r:
        await submit_as_is(p0_client(), r[1])


async def p1_tick_shared(st, acts, state, tag):
    drain_rejections(p1_client())
    if await handle_nonpriority(p1_client(), 1, tag, st, state, acts,
                               protect=(BEAR,)):
        return
    await pay_as_is(p1_client(), acts, tag)
    if not my_priority(state, 1):
        return
    b = find_hand(state, 1, BEAR)
    if (b is not None and is_my_main(state, 1)):
        ul = untapped_lands(state, 1)
        greens = sum(1 for x in ul if oname(get_obj(state, x)) == FOREST)
        if len(ul) >= 2 and greens >= 1:
            cast = castspell_advertised(acts, b)
            if cast:
                await submit_as_is(p1_client(), cast)
                say(f"[{tag}] casts {BEAR}")
                return
    r = economy_tick(p1_client(), 1, st, acts, state, tag)
    if r:
        await submit_as_is(p1_client(), r[1])


_CLIENTS = {}


def p0_client():
    return _CLIENTS["p0"]


def p1_client():
    return _CLIENTS["p1"]


async def play_game(game, target_kind, cast_bear_p0=False):
    """target_kind: ('bear', seat) or ('player', seat)."""
    new_game_state(game, None)
    p0, p1 = await open_game(game, {"A": A_P0_DECK, "B": B_P0_DECK,
                                   "C": C_P0_DECK}[game],
                            {"A": A_P1_DECK, "B": B_P1_DECK,
                             "C": C_P1_DECK}[game])
    _CLIENTS["p0"], _CLIENTS["p1"] = p0, p1

    def want_p0(state, pid):
        return p0_keep_catapult(state, pid, need_bear=(game == "C"))

    def want_p1(state, pid):
        if game == "B":
            return True
        return p1_keep_bear(state, pid)

    ST["want_keep"] = [want_p0, want_p1]

    # resolve the concrete target once the board is set (target_spec may need
    # an oid, which only exists after the bear hits the battlefield)
    async def resolve_target():
        t0 = time.time()
        while time.time() - t0 < 600:
            await asyncio.sleep(0.5)
            st = p0.latest
            if not st:
                continue
            s = st["state"]
            if target_kind[0] == "player":
                return ("player", target_kind[1])
            seat = target_kind[1]
            oid = bf_find(s, seat, BEAR)
            if oid is not None:
                return ("oid", oid)
        return None

    async def p0_tick(st, acts, state):
        await p0_tick_shared(st, acts, state, f"{game}-P0",
                             cast_bear=cast_bear_p0)

    async def p1_tick(st, acts, state):
        await p1_tick_shared(st, acts, state, f"{game}-P1")

    # background target resolution: set target_spec as soon as the bear lands
    async def watcher():
        spec = await resolve_target()
        if spec and ST.get("game") == game and not ST.get("target_spec"):
            ST["target_spec"] = spec
            say(f"[{game}] target resolved: {spec}")

    if target_kind[0] == "player":
        ST["target_spec"] = ("player", target_kind[1])
    else:
        ST["target_spec"] = None
        asyncio.ensure_future(watcher())

    ok = await run_game(p0, p1, p0_tick, p1_tick,
                        done_pred=lambda: ST.get(f"post{game}") is not None,
                        timeout_s=1500)
    await p0.close()
    await p1.close()
    return ok


# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
    e = cd["acorn catapult"]
    ab = (e.get("abilities") or [])[0]
    eff = ab.get("effect") or {}
    sub = ab.get("sub_ability") or {}
    seff = sub.get("effect") or {}
    out = {"acorn catapult": {
        "oracle_text": e.get("oracle_text"),
        "activated_effect": eff.get("type"),
        "activated_amount": (eff.get("amount") or {}).get("value"),
        "activated_target": (eff.get("target") or {}).get("type"),
        "activated_cost": [c.get("type") for c in
                           ((ab.get("cost") or {}).get("costs") or [])],
        "sub_effect": seff.get("type"),
        "sub_name": seff.get("name"),
        "sub_description": seff.get("description"),
    }}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


# ------------------------------------------------------------- evaluation
def bear_damage_info(state, seat):
    oid = bf_find(state, seat, BEAR)
    if oid is None:
        return None, "bear not on battlefield"
    o = get_obj(state, oid)
    info = {"tapped": o.get("tapped")}
    for k in ("damage", "damage_marked", "marked_damage"):
        if k in o:
            info[k] = o[k]
    pt = o.get("power"), o.get("toughness")
    info["pt"] = pt
    return oid, info


def evaluate(parse, snaps):
    """snaps: {game: (pre, post)}. Returns (assertions, notes, verdict)."""
    ass = {}
    notes = []
    expected = {"A": 1, "B": 1, "C": 0}  # token controller per Oracle
    desc = {"A": "P1's Grizzly Bears (damaged permanent's controller = P1)",
            "B": "P1 the player (damaged player = P1)",
            "C": "P0's own Grizzly Bears (damaged recipient = P0, control)"}

    pe = parse["acorn catapult"]
    gap = (pe["sub_effect"] == "Unimplemented"
           and pe["sub_name"] == "unbound_subject")
    notes.append(f"parse (pinned v0.85.0 card-data): activated_effect="
                 f"{pe['activated_effect']} target={pe['activated_target']} "
                 f"cost={pe['activated_cost']}; token clause sub_effect="
                 f"{pe['sub_effect']} ({pe['sub_name']}: "
                 f"{pe['sub_description'][:80] if pe['sub_description'] else ''}): "
                 f"{'UNBOUND-SUBJECT GAP PRESENT as reported' if gap else 'gap not as reported'}")

    setup_reached = False
    for g in ("A", "B", "C"):
        pre, post = snaps[g]
        if pre is None:
            ass[f"{g}1_setup"] = "failed"
            notes.append(f"{g}1_setup failed: never reached the activation "
                         f"setup ({desc[g]})")
            for k in (f"{g}2_resolved", f"{g}3_token", f"{g}4_recipient"):
                ass[k] = "not-run"
                notes.append(f"{k} not-run (missing pre/post)")
            continue
        cat = bf_find(pre, 0, CATAPULT)
        cat_ok = cat is not None and not get_obj(pre, cat).get("tapped")
        if g in ("A", "C"):
            tgt = bf_find(pre, 1 if g == "A" else 0, BEAR)
            tgt_ok = tgt is not None
        else:
            tgt_ok = True
        life_ok = life(pre, 0) == 20 and life(pre, 1) == 20
        ok = cat_ok and tgt_ok and life_ok
        ass[f"{g}1_setup"] = "passed" if ok else "failed"
        notes.append(f"{g}1_setup: catapult_on_bf_untapped={cat_ok}, "
                     f"target_present={tgt_ok}, life={life(pre,0)}/{life(pre,1)}: "
                     f"{'ok' if ok else 'SETUP FAILED'}")
        if not ok:
            for k in (f"{g}2_resolved", f"{g}3_token", f"{g}4_recipient"):
                ass[k] = "not-run"
                notes.append(f"{k} not-run (setup failed)")
            continue
        setup_reached = True
        if post is None:
            ass[f"{g}2_resolved"] = "failed"
            notes.append(f"{g}2_resolved failed: ability never resolved "
                         f"(no post state)")
            for k in (f"{g}3_token", f"{g}4_recipient"):
                ass[k] = "not-run"
                notes.append(f"{k} not-run (no post state)")
            continue
        catp = bf_find(post, 0, CATAPULT)
        tapped = catp is not None and bool(get_obj(post, catp).get("tapped"))
        no_stack = len(stack_entries(post)) == 0
        if g in ("A", "C"):
            _, dinfo = bear_damage_info(post, 1 if g == "A" else 0)
            dmg_ev = f"bear_info={dinfo}"
        else:
            dmg_ev = f"P1 life {life(pre,1)}->{life(post,1)}"
        res_ok = tapped and no_stack
        ass[f"{g}2_resolved"] = "passed" if res_ok else "failed"
        notes.append(f"{g}2_resolved: catapult_tapped={tapped}, "
                     f"stack_empty={no_stack}, {dmg_ev}: "
                     f"{'ok' if res_ok else 'NOT RESOLVED'}")

        sq = squirrels(post)
        ctrls = {}
        for oid, o in sq:
            ctrls[o.get("controller")] = ctrls.get(o.get("controller"), 0) + 1
        say(f"[{g}] post squirrels: {[(oid, o.get('controller'), oname(o)) for oid, o in sq]}")
        if sq:
            ass[f"{g}3_token"] = "passed"
            notes.append(f"{g}3_token: {len(sq)} Squirrel token(s) on "
                         f"battlefield, controllers={ctrls}")
        else:
            ass[f"{g}3_token"] = "failed"
            notes.append(f"{g}3_token FAILED: no Squirrel token on the "
                         f"battlefield after resolution")
        if sq:
            exp = expected[g]
            if set(ctrls.keys()) == {exp}:
                ass[f"{g}4_recipient"] = "passed"
                notes.append(f"{g}4_recipient: all Squirrel tokens controlled "
                             f"by seat {exp} as Oracle requires ({desc[g]})")
            else:
                ass[f"{g}4_recipient"] = "failed"
                notes.append(f"{g}4_recipient FAILED: Squirrel tokens "
                             f"controlled by {ctrls}, expected seat {exp} "
                             f"({desc[g]})")
        else:
            ass[f"{g}4_recipient"] = "not-run"
            notes.append(f"{g}4_recipient not-run (no token created)")

    a4 = ass.get("A4_recipient"); b4 = ass.get("B4_recipient")
    a3 = ass.get("A3_token"); b3 = ass.get("B3_token")
    misassigned = a4 == "failed" or b4 == "failed"
    no_token = setup_reached and ((a3 == "failed" and b3 in ("failed", "not-run"))
                                  or (b3 == "failed" and a3 in ("failed", "not-run")))
    # token went to the Catapult controller (seat 0) instead of the damaged
    # recipient -- the exact reported failure
    reported_shape = False
    for g in ("A", "B"):
        pre, post = snaps[g]
        if post is None:
            continue
        ctrls = {}
        for oid, o in squirrels(post):
            ctrls[o.get("controller")] = ctrls.get(o.get("controller"), 0) + 1
        if ctrls.get(0) and not ctrls.get(expected[g]):
            reported_shape = True
            notes.append(f"{g}: token(s) went to the Catapult controller "
                         f"(seat 0) instead of seat {expected[g]} -- the "
                         f"reported failure shape")
    if not setup_reached:
        verdict = "blocked"
        notes.append("verdict: blocked - no game reached its activation setup")
    elif reported_shape or misassigned:
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED - Squirrel token created for the "
                     "Catapult controller instead of the damaged recipient")
    elif no_token:
        verdict = "reproduced"
        notes.append("verdict: REPRODUCED (related failure) - the Squirrel "
                     "clause produced no token at all (unbound-subject gap)")
    elif a4 == "passed" and b4 == "passed":
        verdict = "not-reproduced"
        notes.append("verdict: not-reproduced - tokens went to the damaged "
                     "recipient in every completed leg")
    else:
        verdict = "blocked"
        notes.append("verdict: blocked - inconclusive token observations")
    return ass, notes, verdict


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1160
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#7191 Acorn Catapult: Squirrel token misassigned to Catapult "
         "controller", fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, val in run["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=17)
    y += 6
    line("parse (pinned v0.85.0 card-data):", fill=(160, 200, 255))
    pe = run["parse_summary"]["acorn catapult"]
    line(f"  activated: {pe['activated_effect']} target={pe['activated_target']} "
         f"cost={pe['activated_cost']}", size=15)
    line(f"  token clause: {pe['sub_effect']} ({pe['sub_name']})", size=15)
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
    say("parse:", json.dumps(parse))

    snaps = {}
    results = {}
    for game, tkind, bear_p0 in (("A", ("bear", 1), False),
                                ("B", ("player", 1), False),
                                ("C", ("bear", 0), True)):
        try:
            ok = await play_game(game, tkind, cast_bear_p0=bear_p0)
            say(f"game {game} finished ok={ok}")
        except Exception as e:
            say(f"game {game} crashed: {e!r}")
            ok = False
        results[game] = ok
        snaps[game] = (ST.get(f"pre{game}"), ST.get(f"post{game}"))

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
        "source": "minisign verification against repo-pinned key at pin time; "
                  "hashes recomputed from on-disk release files this run; "
                  "v0.85.0 confirmed latest stable release (not shell-*)",
    }
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{SERVER_RUN_ID}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario": "driver/scenario_7191_085.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_7191_085.py"),
        "decks": {"A_P0": A_P0_DECK, "A_P1": A_P1_DECK,
                  "B_P0": B_P0_DECK, "B_P1": B_P1_DECK,
                  "C_P0": C_P0_DECK, "C_P1": C_P1_DECK},
        "games": results,
        "parse_summary": parse,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-16",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Game C (own creature) cannot distinguish the fallback from correct "
            "behavior; it validates the harness token detection only.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "A: P0 8x Acorn Catapult + 52 Island vs P1 12x Grizzly "
                      "Bears + 48 Forest (P0 activates targeting P1's bear). "
                      "B: same P0 deck vs P1 60 Forest (P0 activates targeting "
                      "P1). C: P0 8x Catapult + 8x Bears + 26 Island + 18 Forest "
                      "vs P1 60 Forest (P0 activates targeting own bear).",
        "contract_line": "Per Oracle, the damaged permanent's controller (A/C) "
                         "or the damaged player (B) creates the 1/1 Squirrel. "
                         "Reported defect: the token goes to the Catapult's "
                         "controller instead.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7191_085.py", "w") as f:
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
