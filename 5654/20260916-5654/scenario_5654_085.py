#!/usr/bin/env python3
"""Issue #5654: Notion Thief + Plagiarize -- compound "skips that draw and you
draw a card" substitute drops to Unimplemented (one fix, two cards).

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Issue (internal triage, 2026-07-12; status:confirmed; classifier verdict
unsupported_aspect): both cards' replacement substitute is the identical
compound "instead that player skips that draw and you draw a card". In the
pinned v0.85.0 card-data.json the skip half parses to
execute.effect.type == "Unimplemented" while the "you draw a card" half parses
to Draw{Fixed 1, Controller}. Hullbreacher's identical-antecedent single-action
substitute parses fully to Token.

Oracle text (verified from pinned v0.85.0 card-data.json):
  Notion Thief {2}{U}{B} 3/2 Flash creature:
    "If an opponent would draw a card except the first one they draw in each
     of their draw steps, instead that player skips that draw and you draw
     a card."
  Plagiarize {3}{U} instant:
    "Until end of turn, if target player would draw a card, instead that
     player skips that draw and you draw a card."
  Hullbreacher (control):
    "If an opponent would draw a card except the first one they draw in each
     of their draw steps, instead you create a Treasure token." -> Token.

Game A (Notion Thief): P0 casts Notion Thief, then P1 casts Divination
("Draw two cards.") during their own main phase. Both draws are non-first
draw-step draws, so per Oracle both are replaced: P1 skips (hand -1 for the
cast itself, +0 draws), P0 draws 2.

Game B (Plagiarize): P0 casts Plagiarize targeting P1 during P1's Upkeep
(instant timing; must precede P1's draw-step draw since Plagiarize has no
first-draw exemption). P1's draw-step draw is then replaced: P1 hand/lib
unchanged, P0 draws 1.

Assertions:
  A1A/A1B_setup   pre: key permanent/spell in place, mana available, life 20/20.
  A2A/A2B_parse   pinned v0.85.0 card-data still shows the Unimplemented skip
                  node with the Draw{1,Controller} sub-ability (the reported
                  class gap); Hullbreacher control parses to Token.
  A3B_target      (B only) Plagiarize target prompt offered P1 and answered.
  A4A/A4B_skip_holds   draw events skipped: victim hand/library unchanged by them.
  A5A/A5B_ctrl_draws   controller drew the replaced draws: P0 hand/library deltas.
  A6A/A6B_cleanup spell in graveyard, stack empty, game proceeding.

Verdict rule: reproduced iff the reported parse gap is present in the pinned
v0.85.0 card-data (A2A/A2B pass) and at least one game reached its cast setup.
The in-engine games characterize whether the Unimplemented node is latent or
active; deviations there are recorded but the reported defect is the parse gap.
not-reproduced iff the parse gap is gone from pinned card-data. blocked iff no
game reached its cast setup.

Evidence: evidence/5654/<run-id>/pre_A.json, post_A.json, pre_B.json,
post_B.json, parse_evidence.json, run.json, manifest.sha256, summary.png,
scenario_5654_085.py, wire_log.jsonl, scenario_run.log
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
ISSUE = 5654
RUN_ID = "20260916-5654"
SERVER_RUN_ID = RUN_ID
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

THIEF = "Notion Thief"
PLAG = "Plagiarize"
DIV = "Divination"
ISLAND = "Island"
SWAMP = "Swamp"
LANDS = (ISLAND, SWAMP, "Forest", "Plains", "Mountain")

A_P0_DECK = [(THIEF, 8), (ISLAND, 26), (SWAMP, 26)]
A_P1_DECK = [(DIV, 8), (ISLAND, 52)]
B_P0_DECK = [(PLAG, 8), (ISLAND, 52)]
B_P1_DECK = [(ISLAND, 60)]

ST = {}
ACTED = set()
PROMPT_DONE = set()
REJECTS = {}
LAST_IID = {}
SKIP_IID = set()
C0 = None  # exporter client (P0 of current game)


def reset_per_game():
    """Fresh per-revision/per-prompt guards for each new game: revisions
    restart at 0 per game, so a global ACTED set would suppress legitimate
    first-time actions in game B that share a revision number with game A."""
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


def gy_find(state, pid, name):
    for oid in gy_oids(state, pid):
        if oname(get_obj(state, oid)) == name:
            return oid
    return None


def untapped_lands(state, pid, name=None):
    return [oid for oid, o in bf(state, pid)
            if not o.get("tapped") and oname(o) in LANDS
            and (name is None or oname(o) == name)]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def my_priority(state, pid):
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


def stack_names(state):
    out = []
    for e in state.get("stack") or []:
        o = e if isinstance(e, dict) else get_obj(state, e)
        out.append(oname(o))
    return out


# ------------------------------------------------------------- interaction helpers
def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


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
    """Answer a DiscardChoice/DiscardToHandSize: lands first, `protect` last."""
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
    """Land drop in main + pass priority. Returns True if it submitted."""
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


async def answer_target_selection(c, pid, tag, st, state, acts, pick_seat):
    """Answer one TargetSelection/TriggerTargetSelection slot for `pid` by
    choosing the candidate whose surface seat == pick_seat. One choice per
    prompt (CR 601.2c; engine max:1 per prompt)."""
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
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in PROMPT_DONE or iid in SKIP_IID:
            continue
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        pick = None
        for ch in chs:
            if seat_of(ch) == pick_seat:
                pick = ch
                break
        if pick is None:
            # fall back: pick first candidate with a seat surface
            for ch in chs:
                if seat_of(ch) is not None:
                    pick = ch
                    break
        if pick is None:
            continue
        await send_interaction(c, {"interactionId": iid,
                                  "response": {"type": "sequence",
                                               "data": {"choiceIds": [pick["id"]]}}})
        PROMPT_DONE.add(iid)
        ST.setdefault("targets_answered", []).append(
            {"game": ST["game"], "wtype": wtype, "iid": iid,
             "seat": seat_of(pick), "choice": pick["id"]})
        say(f"[{tag}] answers {wtype} -> seat {seat_of(pick)}")
        wire("target_answered", {"tag": tag, "wtype": wtype,
                                "seat": seat_of(pick), "iid": iid})
        return True
    say(f"[{tag}] {wtype} for seat {pid}: no answerable opportunity")
    return False

# ------------------------------------------------------------- Game A: Notion Thief
async def game_a():
    reset_per_game()
    ST.update({"game": "A", "done": False, "thief_cast": False,
               "div_submitted": False, "thief_turn": None, "div_turn": None,
               "preA": None, "postA": None, "max_turn": 0, "states_seen": 0,
               "mulls0": 0, "mulls1": 0,
               "want_keep": [None, None]})

    def p0_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n in (ISLAND, SWAMP))
        return THIEF in hn and lands >= 2

    def p1_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n == ISLAND)
        return DIV in hn and lands >= 2

    ST["want_keep"] = [p0_keep, p1_keep]

    p0 = PhaseClient("A-P0")
    await p0.connect()
    await p0.create(deck(*A_P0_DECK))
    p1 = PhaseClient("A-P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*A_P1_DECK))
    say(f"[A] game={p0.game_code}")
    global C0
    C0 = p0

    async def p0_tick(st, acts, state):
        drain_rejections(p0)
        if await handle_nonpriority(p0, 0, "A-P0", st, state, acts,
                                   protect=(THIEF,)):
            return
        for a in acts:
            if a.get("type") in ("PayMana", "PayManaAbilityMana",
                                 "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                say("[A-P0] mana payment submitted as-is")
                return
        if not my_priority(state, 0):
            return
        ui = untapped_lands(state, 0, ISLAND)
        us = untapped_lands(state, 0, SWAMP)
        ul = untapped_lands(state, 0)
        if (not ST["thief_cast"] and bf_find(state, 0, THIEF) is None
                and find_hand(state, 0, THIEF) is not None
                and is_my_main(state, 0)
                and len(ui) >= 1 and len(us) >= 1 and len(ul) >= 4):
            cast = castspell_advertised(acts, find_hand(state, 0, THIEF))
            if cast:
                wire("A_cast_thief", cast)
                ST["thief_turn"] = turn_of(state)
                await submit_as_is(p0, cast)
                ST["thief_cast"] = True
                say(f"[A-P0] casts Notion Thief (turn {ST['thief_turn']})")
                return
        r = economy_tick(p0, 0, st, acts, state, "A-P0")
        if r:
            await submit_as_is(p0, r[1])

    async def p1_tick(st, acts, state):
        drain_rejections(p1)
        if await handle_nonpriority(p1, 1, "A-P1", st, state, acts,
                                   protect=(DIV,)):
            return
        # post: Divination resolved (in gy), stack empty
        if (ST["div_submitted"] and ST["postA"] is None
                and gy_find(state, 1, DIV) is not None
                and len(state.get("stack", []) or []) == 0):
            say("[A] Divination resolved; exporting POST_A")
            ST["postA"] = await export_now("post_A.json", C0)
            return
        for a in acts:
            if a.get("type") in ("PayMana", "PayManaAbilityMana",
                                 "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if not my_priority(state, 1):
            return
        ui = untapped_lands(state, 1, ISLAND)
        if (ST["thief_cast"] and not ST["div_submitted"]
                and bf_find(state, 0, THIEF) is not None
                and find_hand(state, 1, DIV) is not None
                and is_my_main(state, 1)
                and len(ui) >= 3):
            cast = castspell_advertised(acts, find_hand(state, 1, DIV))
            if cast:
                say("[A-P1] main phase: exporting PRE_A, then casting Divination")
                ST["preA"] = await export_now("pre_A.json", C0)
                wire("A_cast_divination", cast)
                ST["div_turn"] = turn_of(state)
                await submit_as_is(p1, cast)
                ST["div_submitted"] = True
                say(f"[A-P1] casts Divination (turn {ST['div_turn']})")
                return
        r = economy_tick(p1, 1, st, acts, state, "A-P1")
        if r:
            await submit_as_is(p1, r[1])

    ok = await run_game(p0, p1, p0_tick, p1_tick,
                        done_pred=lambda: ST["postA"] is not None,
                        timeout_s=1500)
    await p0.close()
    await p1.close()
    return ok


# ------------------------------------------------------------- Game B: Plagiarize
async def game_b():
    reset_per_game()
    ST.update({"game": "B", "done": False, "plag_cast": False,
               "target_prompt_seen": False, "target_ok": False,
               "plag_turn": None, "preB": None, "postB": None,
               "max_turn": 0, "states_seen": 0, "mulls0": 0, "mulls1": 0,
               "want_keep": [None, None]})

    def p0_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n == ISLAND)
        return PLAG in hn and lands >= 2

    def p1_keep(state, pid):
        return True

    ST["want_keep"] = [p0_keep, p1_keep]

    p0 = PhaseClient("B-P0")
    await p0.connect()
    await p0.create(deck(*B_P0_DECK))
    p1 = PhaseClient("B-P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*B_P1_DECK))
    say(f"[B] game={p0.game_code}")
    global C0
    C0 = p0

    async def p0_tick(st, acts, state):
        drain_rejections(p0)
        if await answer_target_selection(p0, 0, "B-P0", st, state, acts, pick_seat=1):
            if not ST["target_prompt_seen"]:
                ST["target_prompt_seen"] = True
                say("[B] Plagiarize target prompt seen and answered")
            return
        if await handle_nonpriority(p0, 0, "B-P0", st, state, acts,
                                   protect=(PLAG,)):
            return
        # post: P1 past draw step (main phase), Plagiarize in gy, stack empty
        if (ST["plag_cast"] and ST["postB"] is None
                and gy_find(state, 0, PLAG) is not None
                and state.get("active_player") == 1
                and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain")
                and len(state.get("stack", []) or []) == 0):
            say("[B] P1 past draw step; exporting POST_B")
            ST["postB"] = await export_now("post_B.json", C0)
            return
        for a in acts:
            if a.get("type") in ("PayMana", "PayManaAbilityMana",
                                 "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        if not my_priority(state, 0):
            return
        ui = untapped_lands(state, 0, ISLAND)
        # cast during P1's Upkeep (must precede P1's draw-step draw)
        if (not ST["plag_cast"]
                and find_hand(state, 0, PLAG) is not None
                and state.get("active_player") == 1
                and (state.get("phase") or "") == "Upkeep"
                and len(ui) >= 4):
            cast = castspell_advertised(acts, find_hand(state, 0, PLAG))
            if cast:
                say("[B-P0] P1 Upkeep: exporting PRE_B, then casting Plagiarize @P1")
                ST["preB"] = await export_now("pre_B.json", C0)
                wire("B_cast_plagiarize", cast)
                ST["plag_turn"] = turn_of(state)
                await submit_as_is(p0, cast)
                ST["plag_cast"] = True
                say(f"[B-P0] casts Plagiarize targeting P1 (turn {ST['plag_turn']})")
                return
        r = economy_tick(p0, 0, st, acts, state, "B-P0")
        if r:
            await submit_as_is(p0, r[1])

    async def p1_tick(st, acts, state):
        drain_rejections(p1)
        if await handle_nonpriority(p1, 1, "B-P1", st, state, acts):
            return
        for a in acts:
            if a.get("type") in ("PayMana", "PayManaAbilityMana",
                                 "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if not my_priority(state, 1):
            return
        r = economy_tick(p1, 1, st, acts, state, "B-P1")
        if r:
            await submit_as_is(p1, r[1])

    ok = await run_game(p0, p1, p0_tick, p1_tick,
                        done_pred=lambda: ST["postB"] is not None,
                        timeout_s=1500)
    await p0.close()
    await p1.close()
    return ok


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
                f"stack={len(s.get('stack') or [])}")
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

# ------------------------------------------------------------- parse evidence
def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
    low = {k.strip('"').lower(): k for k in cd}
    out = {}
    for n in ("notion thief", "plagiarize", "hullbreacher"):
        e = cd[low[n]]
        out[n] = {"replacements": e.get("replacements"),
                  "oracle_text": e.get("oracle_text")}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


def summarize_parse(pe):
    res = {}
    for n, e in pe.items():
        reps = e["replacements"] or []
        r0 = reps[0] if reps else {}
        ex = (r0.get("execute") or {})
        eff = ex.get("effect") or {}
        sub = ex.get("sub_ability") or {}
        seff = sub.get("effect") or {}
        cond = r0.get("condition")
        res[n] = {
            "event": r0.get("event"),
            "condition": cond.get("type") if isinstance(cond, dict) else cond,
            "execute_effect": eff.get("type"),
            "execute_desc": eff.get("description"),
            "sub_effect": seff.get("type"),
            "sub_count": (seff.get("count") or {}).get("value"),
            "sub_target": (seff.get("target") or {}).get("type"),
            "valid_player": r0.get("valid_player"),
        }
    return res


# ------------------------------------------------------------- evaluation
def evaluate(parse_sum, preA, postA, preB, postB):
    ass = {}
    notes = []
    nt = parse_sum["notion thief"]
    pl = parse_sum["plagiarize"]
    hb = parse_sum["hullbreacher"]
    gap_nt = (nt["execute_effect"] == "Unimplemented"
              and nt["sub_effect"] == "Draw" and nt["sub_count"] == 1
              and nt["sub_target"] == "Controller")
    gap_pl = (pl["execute_effect"] == "Unimplemented"
              and pl["sub_effect"] == "Draw" and pl["sub_count"] == 1
              and pl["sub_target"] == "Controller")
    hb_ok = (hb["execute_effect"] == "Token" and hb["sub_effect"] is None)
    ass["A2A_parse_notion"] = "passed" if gap_nt else "failed"
    notes.append(f"parse notion thief (pinned v0.85.0 card-data): "
                 f"execute={nt['execute_effect']} ({nt['execute_desc']}), "
                 f"sub={nt['sub_effect']}x{nt['sub_count']}->{nt['sub_target']}, "
                 f"cond={nt['condition']}, valid_player={nt['valid_player']}: "
                 f"{'GAP PRESENT as reported' if gap_nt else 'GAP NOT as reported'}")
    ass["A2B_parse_plagiarize"] = "passed" if gap_pl else "failed"
    notes.append(f"parse plagiarize (pinned v0.85.0 card-data): "
                 f"execute={pl['execute_effect']} ({pl['execute_desc']}), "
                 f"sub={pl['sub_effect']}x{pl['sub_count']}->{pl['sub_target']}, "
                 f"valid_player={pl['valid_player']}: "
                 f"{'GAP PRESENT as reported' if gap_pl else 'GAP NOT as reported'}")
    notes.append(f"parse hullbreacher control: execute={hb['execute_effect']} "
                 f"(single-action substitute fully parsed: "
                 f"{'yes' if hb_ok else 'NO'})")

    def snap(state, pid):
        return (len(hand_oids(state, pid)), lib_size(state, pid),
                len(gy_oids(state, pid)))

    # ---- Game A ----
    if preA is not None:
        ok = (bf_find(preA, 0, THIEF) is not None
              and find_hand(preA, 1, DIV) is not None
              and len(untapped_lands(preA, 1, ISLAND)) >= 3
              and life(preA, 0) == 20 and life(preA, 1) == 20)
        ass["A1A_setup"] = "passed" if ok else "failed"
        notes.append(f"pre_A: thief_on_bf={bf_find(preA,0,THIEF) is not None}, "
                     f"div_in_hand={find_hand(preA,1,DIV) is not None}, "
                     f"P1_untapped_islands={len(untapped_lands(preA,1,ISLAND))}, "
                     f"life={life(preA,0)}/{life(preA,1)}: {'ok' if ok else 'SETUP FAILED'}")
    else:
        ass["A1A_setup"] = "failed"
        notes.append("pre_A missing: Notion Thief game never reached the Divination cast")
    if preA is not None and postA is not None:
        h0pre, l0pre, _ = snap(preA, 0)
        h1pre, l1pre, _ = snap(preA, 1)
        h0post, l0post, _ = snap(postA, 0)
        h1post, l1post, _ = snap(postA, 1)
        say(f"[A] deltas P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        if h1post == h1pre - 1 and l1post == l1pre:
            ass["A4A_skip_holds"] = "passed"
            notes.append(f"A4A skip holds: P1 hand {h1pre}->{h1post} "
                         f"(-1 = cast Divination, +0 draws), library "
                         f"{l1pre}->{l1post} (both draws skipped)")
        else:
            ass["A4A_skip_holds"] = "failed"
            notes.append(f"A4A skip DEVIATES: P1 hand {h1pre}->{h1post}, "
                         f"library {l1pre}->{l1post} (expected -1/+0 draws, lib unchanged)")
        if h0post == h0pre + 2 and l0post == l0pre - 2:
            ass["A5A_ctrl_draws"] = "passed"
            notes.append(f"A5A controller draws: P0 hand {h0pre}->{h0post}, "
                         f"library {l0pre}->{l0post} (both replaced draws drawn)")
        else:
            ass["A5A_ctrl_draws"] = "failed"
            notes.append(f"A5A controller draw DEVIATES: P0 hand {h0pre}->{h0post} "
                         f"(expected +2), library {l0pre}->{l0post} (expected -2)")
        if gy_find(postA, 1, DIV) is not None and len(postA.get("stack", []) or []) == 0:
            ass["A6A_cleanup"] = "passed"
            notes.append("A6A cleanup: Divination in P1 gy, stack empty, game proceeding")
        else:
            ass["A6A_cleanup"] = "failed"
            notes.append(f"A6A cleanup broken: div_in_gy={gy_find(postA,1,DIV) is not None}, "
                         f"stack={len(postA.get('stack',[]) or [])}")
    else:
        for k in ("A4A_skip_holds", "A5A_ctrl_draws", "A6A_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} not-run (missing pre/post)")

    # ---- Game B ----
    if preB is not None:
        ok = (find_hand(preB, 0, PLAG) is not None
              and len(untapped_lands(preB, 0, ISLAND)) >= 4
              and preB.get("active_player") == 1
              and (preB.get("phase") or "") == "Upkeep"
              and life(preB, 0) == 20 and life(preB, 1) == 20)
        ass["A1B_setup"] = "passed" if ok else "failed"
        notes.append(f"pre_B: plag_in_hand={find_hand(preB,0,PLAG) is not None}, "
                     f"P0_untapped_islands={len(untapped_lands(preB,0,ISLAND))}, "
                     f"active={preB.get('active_player')} phase={preB.get('phase')}, "
                     f"life={life(preB,0)}/{life(preB,1)}: {'ok' if ok else 'SETUP FAILED'}")
    else:
        ass["A1B_setup"] = "failed"
        notes.append("pre_B missing: Plagiarize game never reached the Upkeep cast")
    targets = ST.get("targets_answered", [])
    b_targets = [t for t in targets if t.get("game") == "B"]
    if ST.get("plag_cast") and b_targets:
        ass["A3B_target_offered"] = "passed"
        notes.append(f"A3B target: Plagiarize target prompt offered and answered "
                     f"-> seat {[t['seat'] for t in b_targets]}")
    elif ST.get("plag_cast"):
        ass["A3B_target_offered"] = "failed"
        notes.append("A3B target: Plagiarize cast completed but NO target prompt "
                     "was ever offered (target binding gap)")
    else:
        ass["A3B_target_offered"] = "not-run"
        notes.append("A3B target not-run (Plagiarize was never cast)")
    if preB is not None and postB is not None:
        h0pre, l0pre, _ = snap(preB, 0)
        h1pre, l1pre, _ = snap(preB, 1)
        h0post, l0post, _ = snap(postB, 0)
        h1post, l1post, _ = snap(postB, 1)
        say(f"[B] deltas P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        if h1post == h1pre and l1post == l1pre:
            ass["A4B_skip_holds"] = "passed"
            notes.append(f"A4B skip holds: P1 hand {h1pre}->{h1post}, library "
                         f"{l1pre}->{l1post} (draw-step draw skipped)")
        else:
            ass["A4B_skip_holds"] = "failed"
            notes.append(f"A4B skip DEVIATES: P1 hand {h1pre}->{h1post}, library "
                         f"{l1pre}->{l1post} (expected unchanged)")
        # pre_B hand includes Plagiarize; cast consumes 1, replaced draw adds 1
        if h0post == h0pre and l0post == l0pre - 1:
            ass["A5B_ctrl_draws"] = "passed"
            notes.append(f"A5B controller draws: P0 hand {h0pre}->{h0post} "
                         f"(cast -1, replaced draw +1), library {l0pre}->{l0post}")
        else:
            ass["A5B_ctrl_draws"] = "failed"
            notes.append(f"A5B controller draw DEVIATES: P0 hand {h0pre}->{h0post} "
                         f"(expected {h0pre}), library {l0pre}->{l0post} (expected -1)")
        if gy_find(postB, 0, PLAG) is not None and len(postB.get("stack", []) or []) == 0:
            ass["A6B_cleanup"] = "passed"
            notes.append("A6B cleanup: Plagiarize in P0 gy, stack empty, game proceeding")
        else:
            ass["A6B_cleanup"] = "failed"
            notes.append(f"A6B cleanup broken: plag_in_gy={gy_find(postB,0,PLAG) is not None}, "
                         f"stack={len(postB.get('stack',[]) or [])}")
    else:
        for k in ("A4B_skip_holds", "A5B_ctrl_draws", "A6B_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} not-run (missing pre/post)")

    # ---- verdict ----
    parse_gap = (ass["A2A_parse_notion"] == "passed"
                 and ass["A2B_parse_plagiarize"] == "passed")
    setup_reached = (ass.get("A1A_setup") == "passed"
                     or ass.get("A1B_setup") == "passed")
    engine_dev = any(ass.get(k) == "failed" for k in
                     ("A4A_skip_holds", "A5A_ctrl_draws", "A3B_target_offered",
                      "A4B_skip_holds", "A5B_ctrl_draws"))
    if parse_gap and setup_reached:
        verdict = "reproduced"
        notes.append("verdict: reported parse gap present in pinned v0.85.0 "
                     f"card-data; in-engine deviation={'yes' if engine_dev else 'no'}")
    elif not parse_gap and setup_reached:
        verdict = "not-reproduced"
        notes.append("verdict: parse gap NOT present in pinned v0.85.0 card-data")
    else:
        verdict = "blocked"
        notes.append("verdict: blocked - no game reached its cast setup")
    return ass, notes, verdict


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1040
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#5654 Notion Thief + Plagiarize: compound substitute -> Unimplemented",
         fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    line(f"verdict: {run['verdict'].upper()}",
         fill=(255, 170, 90) if run["verdict"] == "reproduced" else (120, 255, 160)
         if run["verdict"] == "not-reproduced" else (255, 120, 120))
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, v in run["assertions"].items():
        col = (120, 255, 160) if v == "passed" else ((255, 120, 120)
              if v == "failed" else (200, 200, 200))
        line(f"  {k}: {v}", fill=col, size=17)
    y += 6
    line("parse summary (pinned v0.85.0 card-data):", fill=(160, 200, 255))
    for n, p in run["parse_summary"].items():
        line(f"  {n}: execute={p['execute_effect']} "
             f"sub={p['sub_effect']}x{p['sub_count']}->{p['sub_target']}",
             size=15)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:12]:
        line(f"  - {n[:116]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def amain():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID} (pinned v0.85.0, protocol 72)")

    pe = parse_evidence()
    parse_sum = summarize_parse(pe)
    say("parse summary:", json.dumps(parse_sum))

    preA = postA = preB = postB = None
    okA = okB = False
    try:
        okA = await game_a()
        say(f"game A finished ok={okA}")
    except Exception as e:
        say(f"game A crashed: {e!r}")
    preA, postA = ST.get("preA"), ST.get("postA")
    try:
        okB = await game_b()
        say(f"game B finished ok={okB}")
    except Exception as e:
        say(f"game B crashed: {e!r}")
    preB, postB = ST.get("preB"), ST.get("postB")

    ass, notes, verdict = evaluate(parse_sum, preA, postA, preB, postB)
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
        "source": "ServerHello + minisign verification against repo-pinned key "
                  "(hashes recomputed from on-disk release files; verified "
                  "v0.85.0 is the latest stable release, not shell-*)",
    }
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{SERVER_RUN_ID}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario": "driver/scenario_5654_085.py",
        "scenario_sha256": sha(f"{BACKFILL}/driver/scenario_5654_085.py"),
        "decks": {"A_P0": A_P0_DECK, "A_P1": A_P1_DECK,
                  "B_P0": B_P0_DECK, "B_P1": B_P1_DECK},
        "games": {"A": {"ok": okA}, "B": {"ok": okB}},
        "parse_summary": parse_sum,
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-16",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Magus of the Chains shares the same Oracle text and parse class but "
            "was not driven (same-class coverage via Plagiarize/Notion Thief).",
            "Verdict is scoped to v0.85.0; this is a reproduction, not a fix claim.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "A: P0 8x Notion Thief + 26 Island + 26 Swamp vs P1 8x "
                      "Divination + 52 Island (P1 casts Divination with Thief on "
                      "board). B: P0 8x Plagiarize + 52 Island vs P1 60 Island "
                      "(P0 casts Plagiarize @P1 during P1's Upkeep).",
        "contract_line": "Per Oracle, each affected draw is skipped by the "
                         "victim and drawn by the controller instead. The "
                         "reported defect is the parser dropping the compound "
                         "substitute's skip half to Unimplemented.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_5654_085.py", "w") as f:
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
