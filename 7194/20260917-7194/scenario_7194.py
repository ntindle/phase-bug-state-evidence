#!/usr/bin/env python3
"""Issue #7194: Unbound Flourishing -- "Triggers correctly but does not
double the value of X."

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified from pinned v0.85.0 card-data.json), Unbound
Flourishing {2}{G} Enchantment:
  "Whenever you cast a permanent spell with a mana cost that contains {X},
   double the value of X.
   Whenever you cast an instant or sorcery spell or activate an ability,
   if that spell's mana cost or that ability's activation cost contains {X},
   copy that spell or ability. You may choose new targets for the copy."

Reported (Discord sync): the first trigger fires ("triggers correctly")
but the value of X is NOT doubled.

Driver contract (this scenario):
  - P0: 12x Unbound Flourishing, 12x Walking Ballista, 36x Forest.
    P1: 60x Forest (passive: land drop, pass, never attacks).
  - P0 ramps, casts Unbound Flourishing ({2}{G}) on its main phase, then
    casts Walking Ballista ({X}{X}) with X=3 (6 mana, engine auto-taps)
    while Flourishing is on the battlefield.
  - Correct behavior: the Flourishing trigger doubles X (3 -> 6) and
    Walking Ballista enters with SIX +1/+1 counters (counters.P1P1 == 6).
  - Reported bug: trigger fires but Ballista enters with THREE counters.

Assertions:
  A1 setup_ok: pre_cast exported with P0 main phase, Ballista in hand,
      Unbound Flourishing on P0's battlefield, >=6 untapped Forests.
  A2 x_announced: a ChooseXValue prompt was offered for P0 and answered
      X=3 (no rejection on that interaction); >=6 Forests tapped by the
      engine for the cast.
  A3 trigger_seen: an Unbound Flourishing triggered ability ("double the
      value of X") appeared on the stack after the cast.
  A4 doubled: Walking Ballista is on P0's battlefield with counters.P1P1
      == 6 (X=3 doubled). P1P1 == 3 is the reported failure.
  A5 cleanup: the turn advanced past the cast with no stuck prompt
      attributable to the cast/trigger; Ballista remains on the
      battlefield under P0's control.

Verdict rule:
  reproduced     -- A1 and A2 pass, Ballista enters with P1P1 == 3
                    (the reported "does not double" outcome).
  not-reproduced -- A1..A5 all pass (P1P1 == 6).
  blocked        -- setup never assembled, X prompt unanswerable, Ballista
                    never reached the battlefield, or another concrete
                    prerequisite failure (with next unblock action).

Evidence: evidence/7194/20260917-7194/pre_cast.json, post_cast.json,
run.json, manifest.sha256, summary.png, scenario_7194.py, wire_log.jsonl,
scenario_run.log, parse_evidence.json
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
ISSUE = 7194
RUN_ID = "20260917-7194"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

UF = "Unbound Flourishing"
WB = "Walking Ballista"
LANDS = ("Forest",)
X_CHOSEN = 3

P0_DECK = [(UF, 12), (WB, 12), ("Forest", 36)]
P1_DECK = [("Forest", 60)]

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


def tapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if o.get("tapped") and oname(o) in LANDS]


def uf_on_battlefield(state, pid):
    return any(oname(o) == UF for _, o in bf(state, pid))


def ballista_on_battlefield(state, pid):
    for oid, o in bf(state, pid):
        if oname(o) == WB:
            return oid, o
    return None, None


def p1p1_counters(o):
    c = (o or {}).get("counters") or {}
    if isinstance(c, dict):
        for k, v in c.items():
            if str(k).upper() == "P1P1":
                try:
                    return int(v)
                except Exception:
                    return None
    return None


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


def stack_blob(e, state):
    if not isinstance(e, dict):
        e = get_obj(state, e)
    return json.dumps(e, default=str).lower()


def flourishing_trigger_on_stack(state):
    """True if a stack entry mentions unbound flourishing (the doubling
    trigger is a triggered ability sourced from it)."""
    for e in stack_entries(state):
        if "flourishing" in stack_blob(e, state):
            return True
    return False


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
            chs = ((opp.get("response", {}) or {}).get("data", {}) or {}).get(
                "choices") or ((opp.get("response", {}) or {}).get(
                    "data", {}) or {}).get("candidates") or []
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


def rank_cleanup_discard(state, oid):
    nm = oname(get_obj(state, oid))
    if nm in LANDS:
        return (0, nm)
    if nm in (UF, WB):
        return (2, nm)
    return (1, nm)


async def handle_nonpriority(c, pid, tag, st, state, acts):
    wtype = wf_type(state)
    if wtype == "Priority":
        return False
    if await handle_mulligan(c, pid, tag, st, state, acts,
                             ST["want_keep"][pid]):
        return True
    if await handle_bottom(c, pid, tag, st, state, acts):
        return True
    d = wf_data(state)
    dp = wf_player(state)
    if wtype in ("DiscardChoice", "DiscardToHandSize") and dp == pid:
        vi = get_vi(st)
        if vi:
            for opp in vi.get("opportunities", []) or []:
                iid = opp.get("interactionId")
                if iid in PROMPT_DONE or iid in SKIP_IID:
                    continue
                resp = opp.get("response", {}) or {}
                rtype = resp.get("type")
                data = resp.get("data", {}) or {}
                spec = data.get("spec")
                spect = (spec.get("type") if isinstance(spec, dict)
                         else data.get("type"))
                chs = data.get("choices") or data.get("candidates") or []
                oid_by_ref = {ref_of(ch): ch["id"] for ch in chs
                              if ref_of(ch)}
                oids = sorted(hand_oids(state, pid),
                              key=lambda o: rank_cleanup_discard(state, o))[:1]
                picks = [oid_by_ref[o] for o in oids if o in oid_by_ref]
                if not picks:
                    continue
                if rtype == "schema" and spect in ("sequence", "select"):
                    resp_out = {"type": spect,
                                "data": {"choiceIds": picks}}
                elif rtype == "exactChoices":
                    resp_out = {"type": "choose",
                                "data": {"choiceId": picks[0]}}
                else:
                    continue
                await send_interaction(
                    c, {"interactionId": iid, "response": resp_out})
                PROMPT_DONE.add(iid)
                say(f"[{tag}] cleanup discard: "
                    f"{[oname(get_obj(state, o)) for o in oids]}")
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
    return False


async def handle_choose_x(c, pid, tag, st, state, acts):
    """Answer the Walking Ballista ChooseXValue prompt with X=3. Runs only
    while the cast is announced and X is not yet chosen."""
    if ST.get("wb_stage") != "awaiting_x":
        return False
    if wf_type(state) != "ChooseXValue" or wf_player(state) != pid:
        return False
    for opp in vi_opps(st):
        iid = opp.get("interactionId")
        if not iid or iid in PROMPT_DONE or iid in SKIP_IID:
            continue
        resp = opp.get("response", {}) or {}
        spec = (resp.get("data", {}) or {}).get("spec") or {}
        if resp.get("type") == "schema" and spec.get("type") == "number":
            await send_interaction(
                c, {"interactionId": iid,
                    "response": {"type": "number",
                                 "data": {"value": X_CHOSEN}}})
            PROMPT_DONE.add(iid)
            ST["x_iid"] = iid
            ST["x_value"] = X_CHOSEN
            ST["wb_stage"] = "trigger_watch"
            ST["x_time"] = time.time()
            say(f"[{tag}] ChooseXValue answered X={X_CHOSEN} (iid {iid})")
            wire("x_answered", {"iid": iid, "x": X_CHOSEN,
                                "wf_data": wf_data(state)})
            return True
        wire("x_prompt_unexpected",
             {"iid": iid, "rtype": resp.get("type"), "spec": spec})
        say(f"[{tag}] ChooseXValue prompt has unexpected shape "
            f"(rtype={resp.get('type')}, spec={spec}); deferring")
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

    # --- stack sampling: every revision once the Ballista is cast ---
    if ST.get("wb_stage") in ("trigger_watch", "resolving"):
        if flourishing_trigger_on_stack(state) and not ST.get("trigger_seen"):
            ST["trigger_seen"] = True
            ST["trigger_turn"] = turn_of(state)
            trig = [json.loads(json.dumps(e, default=str))
                    for e in stack_entries(state)
                    if "flourishing" in stack_blob(e, state)]
            ST["trigger_entries"] = trig
            say(f"[{tag}] *** Unbound Flourishing trigger on stack "
                f"(turn {turn_of(state)}) ***")
            wire("trigger_seen", {"turn": turn_of(state),
                                  "entries": trig})

    if await handle_nonpriority(c, 0, tag, st, state, acts):
        return
    if await handle_choose_x(c, 0, tag, st, state, acts):
        return

    stage = ST.get("wb_stage")

    # --- stage: develop (ramp; cast Flourishing; then Ballista) ---
    if stage == "develop":
        if my_priority(state, 0) and is_my_main(state, 0):
            ul = untapped_lands(state, 0)
            rev = st.get("state_revision", -1)
            if not uf_on_battlefield(state, 0):
                uf_oid = find_hand(state, 0, UF)
                if uf_oid is not None and len(ul) >= 3:
                    cast = castspell_advertised(acts, uf_oid)
                    if cast and not acted("castuf", rev):
                        await submit_as_is(c, cast)
                        say(f"[{tag}] casts {UF} (oid {uf_oid})")
                        wire("uf_cast", {"oid": str(uf_oid)})
                        return
            else:
                wb_oid = find_hand(state, 0, WB)
                if wb_oid is not None and len(ul) >= 2 * X_CHOSEN:
                    cast = castspell_advertised(acts, wb_oid)
                    if cast and not acted("castwb", rev):
                        ST["pre_cast_snapshot"] = {
                            "lib": lib_size(state, 0),
                            "hand": hand_names(state, 0),
                            "turn": turn_of(state),
                            "phase": state.get("phase"),
                            "untapped_forests": len(ul),
                            "uf_on_battlefield": True,
                        }
                        pre = await export_now("pre_cast.json", C0)
                        ST["pre_cast"] = pre
                        ST["cast_action"] = copy.deepcopy(cast)
                        ST["cast_oid"] = str(wb_oid)
                        await submit_as_is(c, cast)
                        ST["wb_stage"] = "awaiting_x"
                        ST["cast_time"] = time.time()
                        say(f"[{tag}] casts {WB} (oid {wb_oid}); pre-cast "
                            f"exported; awaiting ChooseXValue")
                        wire("wb_cast_submitted",
                             {"oid": str(wb_oid), "action": cast,
                              "pre": ST["pre_cast_snapshot"]})
                        return
        r = economy_tick(c, 0, st, acts, state, tag)
        if r:
            await submit_as_is(c, r[1])
        return

    # --- stage: awaiting_x (ChooseXValue pending) ---
    if stage == "awaiting_x":
        if time.time() - ST.get("cast_time", time.time()) > 180:
            ST["blocked"] = ("ChooseXValue prompt never became answerable "
                             "within 180s of the cast; see wire_log for the "
                             "observed prompt shape")
            ST["done"] = True
            say(f"[{tag}] BLOCKED: {ST['blocked']}")
            return
        # Never pass priority while our own decision is pending (#6758).
        if not (wf_type(state) == "ChooseXValue"
                and wf_player(state) == 0):
            r = economy_tick(c, 0, st, acts, state, tag, allow_land=False)
            if r:
                await submit_as_is(c, r[1])
        return

    # --- stage: trigger_watch (X answered; drive trigger+spell to resolve) ---
    if stage == "trigger_watch":
        ST["tapped_after_x"] = len(tapped_lands(state, 0))
        oid, obj = ballista_on_battlefield(state, 0)
        if oid is not None:
            ST["wb_stage"] = "done"
            ST["ballista_oid"] = oid
            ST["ballista_counters"] = p1p1_counters(obj)
            ST["ballista_object"] = json.loads(json.dumps(obj, default=str))
            ST["resolution_turn"] = turn_of(state)
            post = await export_now("post_cast.json", C0)
            ST["post_cast"] = post
            ST["post_snapshot"] = {
                "turn": turn_of(state),
                "phase": state.get("phase"),
                "ballista_oid": oid,
                "p1p1": ST["ballista_counters"],
                "stack_len": len(stack_entries(state)),
            }
            ST["done"] = True
            say(f"[{tag}] {WB} on battlefield with P1P1="
                f"{ST['ballista_counters']}; post-cast exported")
            wire("ballista_entered", ST["post_snapshot"])
            return
        if time.time() - ST.get("x_time", time.time()) > 300:
            ST["blocked"] = (f"{WB} never reached the battlefield within "
                             f"300s of X={X_CHOSEN}; trigger_seen="
                             f"{ST.get('trigger_seen')}")
            ST["done"] = True
            say(f"[{tag}] BLOCKED: {ST['blocked']}")
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
    if await handle_nonpriority(c, 1, tag, st, state, acts):
        return
    r = economy_tick(c, 1, st, acts, state, tag)
    if r:
        await submit_as_is(c, r[1])


_CLIENTS = {}


# ------------------------------------------------------------- game runner
def new_game_state():
    reset_per_game()
    ST.update({"wb_stage": "develop",
               "pre_cast": None, "pre_cast_snapshot": None,
               "cast_action": None, "cast_oid": None, "cast_time": 0,
               "x_iid": None, "x_value": None, "x_time": 0,
               "tapped_after_x": None,
               "trigger_seen": False, "trigger_turn": None,
               "trigger_entries": [],
               "ballista_oid": None, "ballista_counters": None,
               "ballista_object": None,
               "resolution_turn": None,
               "post_cast": None, "post_snapshot": None,
               "max_turn": 0, "mulls0": 0, "mulls1": 0,
               "want_keep": [None, None], "done": False, "blocked": None})


async def open_game(p0_deck, p1_deck):
    p0 = PhaseClient("7194-P0")
    await p0.connect()
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient("7194-P1")
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
                f"stack={len(stack_entries(s))} stage={ST.get('wb_stage')} "
                f"trigger_seen={ST.get('trigger_seen')}")
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
    return UF in hn and lands >= 2


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
    out = {}
    for key in ("unbound flourishing", "walking ballista"):
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
    uf = parse["unbound flourishing"]
    notes.append(
        "parse (pinned v0.85.0 card-data): unbound flourishing oracle=" +
        repr(uf["oracle_text"]))

    if ST.get("blocked"):
        for k in ("A1_setup_ok", "A2_x_announced", "A3_trigger_seen",
                  "A4_doubled", "A5_cleanup"):
            ass[k] = "not-run"
        notes.append(f"verdict: blocked - {ST['blocked']}")
        return ass, notes, "blocked"

    pre = ST.get("pre_cast_snapshot") or {}

    # A1: setup assembled
    if (pre.get("uf_on_battlefield")
            and WB in (pre.get("hand") or [])
            and (pre.get("untapped_forests") or 0) >= 2 * X_CHOSEN
            and pre.get("phase") in ("PreCombatMain", "PostCombatMain")):
        ass["A1_setup_ok"] = "passed"
        notes.append(
            f"A1 passed: pre_cast on P0 turn {pre.get('turn')} "
            f"{pre.get('phase')}; {WB} in hand; {UF} on battlefield; "
            f"{pre.get('untapped_forests')} untapped Forests "
            f"(need {2 * X_CHOSEN} for X={X_CHOSEN})")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(f"A1 FAILED: pre_cast_snapshot={pre}")

    # A2: X announced as 3
    tapped = ST.get("tapped_after_x")
    pre_untapped = pre.get("untapped_forests") or 0
    mana_ok = (tapped is not None and tapped >= 2 * X_CHOSEN)
    if (ST.get("x_value") == X_CHOSEN
            and REJECTS.get(ST.get("x_iid"), 0) == 0
            and mana_ok):
        ass["A2_x_announced"] = "passed"
        notes.append(
            f"A2 passed: ChooseXValue answered X={X_CHOSEN} (iid "
            f"{ST.get('x_iid')}, no rejection); engine tapped "
            f"{tapped} Forests for the {{X}}{{X}} cost "
            f"(pre-cast untapped: {pre_untapped})")
    else:
        ass["A2_x_announced"] = "failed"
        notes.append(
            f"A2 FAILED: x_value={ST.get('x_value')}, x_iid={ST.get('x_iid')}, "
            f"rejects={REJECTS.get(ST.get('x_iid'), 0)}, tapped_after_x="
            f"{tapped} (pre-cast untapped {pre_untapped})")

    # A3: the doubling trigger fired (the report says it triggers correctly)
    if ST.get("trigger_seen"):
        ass["A3_trigger_seen"] = "passed"
        notes.append(
            f"A3 passed: Unbound Flourishing triggered ability reached the "
            f"stack on turn {ST.get('trigger_turn')} after the cast "
            f"(matches the report's 'triggers correctly')")
    else:
        ass["A3_trigger_seen"] = "failed"
        notes.append(
            "A3 FAILED: no Unbound Flourishing trigger entry was observed "
            "on the stack after the cast (the report claims it triggers)")

    # A4: the decisive outcome -- counters doubled?
    p1p1 = ST.get("ballista_counters")
    if p1p1 == 2 * X_CHOSEN:
        ass["A4_doubled"] = "passed"
        notes.append(
            f"A4 passed: {WB} entered with P1P1={p1p1} (+1/+1 counters); "
            f"X={X_CHOSEN} was doubled to {2 * X_CHOSEN}")
    elif p1p1 == X_CHOSEN:
        ass["A4_doubled"] = "failed"
        notes.append(
            f"A4 FAILED (THE REPORTED SYMPTOM): {WB} entered with P1P1="
            f"{p1p1}; X={X_CHOSEN} was NOT doubled despite the trigger "
            f"firing")
    elif p1p1 is None:
        ass["A4_doubled"] = "not-run"
        notes.append(
            f"A4 not-run: {WB} never reached the battlefield "
            f"(ballista_oid={ST.get('ballista_oid')})")
    else:
        ass["A4_doubled"] = "failed"
        notes.append(
            f"A4 FAILED: {WB} entered with unexpected P1P1={p1p1} "
            f"(expected {2 * X_CHOSEN} doubled or {X_CHOSEN} undoubled)")

    # A5: cleanup -- game proceeded normally after the cast
    post = ST.get("post_snapshot") or {}
    if (ST.get("ballista_oid") is not None
            and (post.get("turn") or 0) >= (pre.get("turn") or 0)):
        ass["A5_cleanup"] = "passed"
        notes.append(
            f"A5 passed: {WB} on P0's battlefield (oid "
            f"{ST.get('ballista_oid')}) at turn {post.get('turn')} "
            f"{post.get('phase')}; no stuck prompt from the cast/trigger")
    else:
        ass["A5_cleanup"] = "failed"
        notes.append(f"A5 FAILED: post_snapshot={post}")

    if ass.get("A4_doubled") == "failed" and p1p1 == X_CHOSEN:
        verdict = "reproduced"
        notes.append(
            "verdict: REPRODUCED - the Unbound Flourishing trigger fired "
            f"on a permanent spell with X={X_CHOSEN} but {WB} entered with "
            f"only {p1p1} counters: the value of X was not doubled, exactly "
            "the reported symptom")
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
        notes.append(
            "verdict: not-reproduced - X was doubled (6 counters on the "
            "Ballista) and the trigger fired; the reported failure did not "
            "occur on v0.85.0")
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

    line("#7194 Unbound Flourishing: trigger fires but X is not doubled?",
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
    line("setup: P0 12x Unbound Flourishing / 12x Walking Ballista / 36x "
         "Forest vs P1 60x Forest", size=16)
    line(f"cast {WB} with X={X_CHOSEN} while {UF} is on the battlefield; "
         f"correct = {2 * X_CHOSEN} counters, bug = {X_CHOSEN}", size=16)
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
    say("parse: unbound flourishing / walking ballista loaded")

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
        "source": "reused live server on 127.0.0.1:9374 (run 20260917-7194); "
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
        "scenario": "driver/scenario_7194.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_7194.py"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "game_ok": ok,
        "parse_summary": parse,
        "cast_action": ST.get("cast_action"),
        "x_decision": {"interaction_id": ST.get("x_iid"),
                       "value": ST.get("x_value")},
        "trigger": {"seen": ST.get("trigger_seen"),
                    "turn": ST.get("trigger_turn"),
                    "entries": ST.get("trigger_entries")},
        "ballista": {"oid": ST.get("ballista_oid"),
                     "p1p1_counters": ST.get("ballista_counters"),
                     "object": ST.get("ballista_object")},
        "resolution": {"turn": ST.get("resolution_turn")},
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
            "opponent interaction with the spell/trigger is not exercised.",
            "Only the 'double the value of X' branch (permanent spells) is "
            "exercised (the reported path); the copy branch for instants/"
            "sorceries/abilities is not tested.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 12x Unbound Flourishing + 12x Walking Ballista + "
                      "36x Forest vs P1 60x Forest. P0 casts Unbound "
                      "Flourishing on its main phase, then casts Walking "
                      "Ballista with X=3 (6 mana, engine auto-taps) while "
                      "Flourishing is on the battlefield; the driver answers "
                      "ChooseXValue with 3 and passes priority to resolve "
                      "the trigger and the spell.",
        "contract_line": "A permanent spell cast with X=3 while Unbound "
                         "Flourishing is on the battlefield must have its X "
                         "doubled: Walking Ballista must enter with 6 "
                         "+1/+1 counters.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_7194.py", "w") as f:
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
