#!/usr/bin/env python3
"""Issue #6772: Ultimecia, Omnipotent does not trigger the extra turn when transformed.

Reported (Discord): "[[Ultimecia, Omnipotent]] does not trigger the extra turn
when transformed".

Oracle (pinned v0.79.0 card-data.json):
  Ultimecia, Time Sorceress ({3}{U}{B}, 4/5):
    "Whenever Ultimecia enters or attacks, surveil 2.
     At the beginning of your end step, you may pay {4}{U}{U}{B}{B} and exile
     eight cards from your graveyard. If you do, transform Ultimecia."
  Ultimecia, Omnipotent (7/7, menace):
    "Time Compression - When this creature transforms into Ultimecia,
     Omnipotent, take an extra turn after this one."

Pinned parse: back face carries a supported Transformed trigger with an
ExtraTurn child for its controller (mode=Transformed, effect=ExtraTurn).
Triage acceptance criteria: transforming into the Omnipotent face triggers
exactly once; the controller receives one extra turn immediately after the
current turn; entering already transformed does not falsely count.

Behavioral contract (single game, two human-client seats, v0.79.0/proto 69):
  RAMP   - P0 plays a land per tick (color-balanced Island/Swamp), keeps
           mulligan, casts Otherworldly Gaze to mill (surveil 3).
  CAST   - P0 casts Ultimecia, Time Sorceress when affordable ({3}{U}{B});
           its enters-surveil mills 2 more. No attacks (no combat needed).
  READY  - when P0 graveyard >= 8 and 8 untapped lands (4 Island + 4 Swamp
           sources) are available, P0 stops casting Opts and passes to the
           end step.
  ACCEPT - at P0's End phase, when the optional transform prompt is pending
           for P0: export pre_transform FIRST, then accept (pay).
  PAY    - mana is auto-tapped by the engine; exile exactly 8 cards from
           P0's graveyard at the selection prompt; transform resolves.
  WATCH  - record (turn_number, active_player) across the turn boundary.
           Extra turn expected: turn T+1 active_player == 0.
           Then turn T+2 active_player == 1 (exactly one extra turn).
  STOP   - after P0's extra turn reaches its main phase and P1's following
           turn begins: export post_extra_turn, stop.

  A1 setup_ok            pre_transform: P0 End phase, Time Sorceress on P0
                         battlefield, P0 gy >= 8, >= 4 untapped Islands and
                         >= 4 untapped Swamps.
  A2 transform_resolved   post: Ultimecia, Omnipotent on P0 battlefield.
  A3 extra_turn_next      the turn after T (T+1) has active_player == 0.
  A4 extra_turn_taken     P0 reached a main phase on turn T+1.
  A5 single_extra         turn T+2 has active_player == 1.
  A6 cleanup              post_extra_turn: stack empty.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes, A2 passes, and A3 fails (transform
          happened but no extra turn followed).
Verdict = not-reproduced iff A1..A6 all pass.
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
RUN_ID = "20260911-6772"
EVID_ISSUE = "6772"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


SPELL = "Ultimecia, Time Sorceress"
BACKFACE = "Ultimecia, Omnipotent"
GAZE = "Otherworldly Gaze"
ISLAND = "Island"
SWAMP = "Swamp"

P0_DECK = deck((SPELL, 12), (GAZE, 12), (ISLAND, 18), (SWAMP, 18))
P1_DECK = deck((ISLAND, 60))

ST = {"cast_done": False, "pre_exported": False,
      "accepted": False, "transform_turn": None, "post_exported": False,
      "stop": False, "stall_since": None, "extra_main_seen": False}
WF_SEEN = []
TURN_SEQ = []  # (turn_number, active_player) transitions observed


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def gy(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def bf_type(state, pid, key):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == key.lower()]


def untapped_bf(state, pid, key):
    return [oid for oid in bf_type(state, pid, key)
            if not objs(state)[oid].get("tapped")]


def life(state, pid):
    players = state.get("players", [])
    if isinstance(players, dict):
        pp = players.get(str(pid), players.get(pid)) or {}
        return pp.get("life")
    for p in players or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p.get("life")
    return None


def lib_count(state, pid):
    players = state.get("players", [])
    if isinstance(players, dict):
        pp = players.get(str(pid), players.get(pid)) or {}
        return len(pp.get("library", []) or [])
    for p in players or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return len(p.get("library", []) or [])
    return None


def find_hand(state, pid, name):
    for oid in hand(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def vi_opps(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


def record_turn(state):
    t = (state.get("turn_number"), state.get("active_player"))
    if not TURN_SEQ or TURN_SEQ[-1] != t:
        TURN_SEQ.append(t)
        wire("turn", {"turn": t[0], "active": t[1],
                      "phase": state.get("phase")})


def submit_select(c, iid, choice_ids):
    return c.send_interaction({"interactionId": iid,
                               "response": {"type": "select",
                                            "data": {"choiceIds": choice_ids}}})


def submit_choose(c, iid, choice_id):
    return c.send_interaction({"interactionId": iid,
                               "response": {"type": "choose",
                                            "data": {"choiceId": choice_id}}})


async def answer_surveil(c, pid, mill_all=True):
    """SurveilChoice: schema 'select' where the SELECTED candidates stay on
    top of the library and the rest go to the graveyard (verified 2026-09-11:
    selecting all milled 0). So mill_all submits an empty selection."""
    answered = False
    for opp in vi_opps(c):
        iid = opp.get("interactionId")
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates", []) or []
        ids = [ch.get("id") for ch in cands if ch.get("id")]
        if not ids:
            continue
        pick = [] if mill_all else ids
        wire("surveil_answer", {"who": c.name, "iid": iid, "mill_all": mill_all,
                               "picked": pick})
        await submit_select(c, iid, pick)
        say(f"[{c.name}] surveil -> {'mill ' + str(len(ids)) if mill_all else 'keep on top'}")
        answered = True
    return answered


async def answer_optional_transform(c, pid, accept=True):
    """Answer the end-step 'you may pay ... if you do, transform'.
    Handles exactChoices (choose the affirmative/negative) and
    schema/kicker-style decideOptionalCost surfaces."""
    for opp in vi_opps(c):
        iid = opp.get("interactionId")
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        wire("optional_opp", {"who": c.name, "iid": iid, "accept": accept,
                             "opp": opp})
        if rtype == "exactChoices":
            def choice_val(ch):
                for s in ch.get("surfaces", []) or []:
                    dd = s.get("data") or {}
                    if s.get("type") == "value" and "value" in dd:
                        return str(dd.get("value")).lower(), str(dd.get("role") or "")
                return None, None
            best = None
            choices = data.get("choices", []) or []
            want = "true" if accept else "false"
            for ch in choices:
                v, role = choice_val(ch)
                if v == want and role in ("accept", "pay", "decision", ""):
                    best = ch.get("choiceId") or ch.get("id")
                    break
            if best is None:
                # substring fallback
                for ch in choices:
                    txt = json.dumps(ch, default=str).lower()
                    cid = ch.get("choiceId") or ch.get("id")
                    if accept:
                        if ('"pay", "true"' in txt or '"value": "true"' in txt
                                or '"yes"' in txt) and '"false"' not in txt:
                            best = cid
                            break
                    else:
                        if '"false"' in txt or '"decline"' in txt:
                            best = cid
                            break
            if best is None and choices:
                ch = choices[0 if accept else -1]
                best = ch.get("choiceId") or ch.get("id")
            if best:
                say(f"[{c.name}] optional transform -> {'ACCEPT' if accept else 'DECLINE'} ({best})")
                wire("optional_answer", {"iid": iid, "choice": best,
                                        "accept": accept})
                await submit_choose(c, iid, best)
                return True
            say(f"[{c.name}] optional transform: no {'affirmative' if accept else 'negative'} choice found")
            return False
        if rtype == "schema":
            cands = data.get("candidates", []) or []
            for ch in cands:
                for s in ch.get("surfaces", []) or []:
                    dd = s.get("data") or {}
                    if dd.get("code") == "decideOptionalCost":
                        cid = ch.get("id")
                        say(f"[{c.name}] optional transform -> decideOptionalCost "
                            f"{'accept' if accept else 'decline'}")
                        wire("optional_answer", {"iid": iid, "choice": cid,
                                                "style": "decideOptionalCost",
                                                "accept": accept})
                        await submit_choose(c, iid, cid)
                        return True
    say(f"[{c.name}] optional transform: no answerable opportunity")
    return False


def find_exile_prompt(c):
    """Return ('schema', opp, ids) or ('action', action) for an exile-8 style
    selection prompt, else None."""
    for opp in vi_opps(c):
        iid = opp.get("interactionId")
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        cands = (resp.get("data") or {}).get("candidates", []) or []
        ids = [ch.get("id") for ch in cands if ch.get("id")]
        if len(ids) >= 8:
            return ("schema", opp, ids[:8])
    for a in (c.latest.get("legal_actions") or []):
        if a.get("type") == "SelectCards":
            return ("action", a, None)
    return None


async def answer_exile_eight(c, pid, state):
    """Exile exactly 8 cards from P0's graveyard. Returns True if a prompt was
    answered, 'wait' if the prompt is present but gy<8, False if no prompt."""
    found = find_exile_prompt(c)
    if not found:
        return False
    g = gy(state, pid)
    if len(g) < 8:
        wire("exile8_waiting_gy", {"gy": len(g)})
        return "wait"
    kind, target, ids = found
    if kind == "schema":
        iid = target.get("interactionId")
        wire("exile8_answer", {"who": c.name, "iid": iid, "picked": ids})
        await submit_select(c, iid, ids)
        say(f"[{c.name}] exile 8 from graveyard (schema select)")
        return True
    picks = [int(x) for x in g[:8]]
    wire("exile8_answer", {"who": c.name, "style": "SelectCards",
                          "picks": picks})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    say(f"[{c.name}] exile 8 from graveyard (SelectCards action)")
    return True


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            wire("action_submit", {"who": "P0", "action": "MulliganDecision/Keep"})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P0] keeps")
            return True

    # cleanup discard: prefer extra Ultimecias, then Gazes; keep lands
    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)

        def rank(oid):
            nm = lname(objs(state)[oid])
            if nm == SPELL.lower():
                return 0
            if nm == GAZE.lower():
                return 1
            return 2
        picks = sorted(oids, key=rank)[:n]
        if picks:
            wire("action_submit", {"who": "P0", "action": "SelectCards/discard",
                                  "picks": picks})
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)} to hand size")
            return True
        return False

    # surveil (enters-trigger and Gaze): mill until gy>=8, then keep on top
    if wtype == "SurveilChoice" and wplayer == 0:
        if await answer_surveil(c, pid, mill_all=len(gy(state, pid)) < 8):
            return True
        wire("surveil_noopportunity", {})
        return False

    # optional end-step transform: only ACCEPT when fully ready (so the
    # pre-export captures a meaningful pre-state). DECLINE on earlier end
    # steps; the trigger fires again next turn. Never pass while it pends.
    if wtype in ("OptionalEffectChoice", "OptionalCostChoice") and wplayer == 0:
        ready = transform_ready(state)
        if ready and not ST["pre_exported"]:
            wire("hold_optional", {"wtype": wtype,
                                  "note": "pre export pending in main loop"})
            return False
        if ready and not ST["accepted"]:
            if await answer_optional_transform(c, pid, accept=True):
                ST["accepted"] = True
                return True
            return False
        if not ready and not ST["accepted"]:
            if await answer_optional_transform(c, pid, accept=False):
                wire("optional_decline", {"turn": state.get("turn_number"),
                                         "gy": len(gy(state, pid))})
                say(f"[{c.name}] declined transform (not ready yet)")
                return True
            return False
        return False

    # mana payment (auto-tap confirmations) go through before exile handling
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    # exile-8 selection after accepting: only engage on a real prompt
    if ST["accepted"] and wplayer == 0 and wtype not in ("Priority", None):
        r = await answer_exile_eight(c, pid, state)
        if r == "wait":
            return False
        if r:
            return True
        wire("accepted_hold", {"wtype": wtype})
        return False

    if wtype == "OrderTriggers":
        oa = find_action(acts, "OrderTriggers")
        if oa:
            wire("action_submit", {"who": "P0", "action": "OrderTriggers/asis"})
            await c.send_action(oa)
            return True

    # never pass priority while a P0 decision is pending
    if wtype in ("OptionalCostChoice", "OptionalEffectChoice", "TargetSelection",
                 "ManaPayment", "ChooseXValue", "SurveilChoice",
                 "ScryChoice") and wplayer == 0:
        wire("hold_priority", {"wtype": wtype})
        return False

    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True

    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True

    if is_my_main(state, pid):
        # land drop every tick: color-balanced
        n_isle = len(bf_type(state, pid, ISLAND))
        n_swamp = len(bf_type(state, pid, SWAMP))
        order = (ISLAND, SWAMP) if n_isle <= n_swamp else (SWAMP, ISLAND)
        for ln in order:
            hid = find_hand(state, pid, ln)
            for a in acts:
                if a["type"] == "PlayLand" and hid \
                        and str(a.get("data", {}).get("object_id")) == hid:
                    wire("action_submit", {"who": "P0", "action": f"PlayLand/{ln}"})
                    await c.send_action(a)
                    return True
        # cast Ultimecia when affordable (only one needed)
        if not ST["cast_done"]:
            mid = find_hand(state, pid, SPELL)
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == mid:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Ultimecia",
                                          "object_id": mid})
                    await c.send_action(a)
                    ST["cast_done"] = True
                    say("[P0] casts Ultimecia, Time Sorceress")
                    return True
        # cast Gazes to mill until ready for the end step
        if not ST["accepted"] and not transform_ready(state):
            oid = find_hand(state, pid, GAZE)
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == oid:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Gaze",
                                          "object_id": oid})
                    await c.send_action(a)
                    return True

    for a in acts:
        if a["type"] == "PassPriority":
            wire("action_submit", {"who": "P0", "action": "PassPriority"})
            await c.send_action(a)
            return True
    return False


def transform_ready(state):
    """8+ cards in P0 gy and 8 untapped lands with 4U+4B sources."""
    if len(gy(state, 0)) < 8:
        return False
    if len(untapped_bf(state, 0, ISLAND)) < 4:
        return False
    if len(untapped_bf(state, 0, SWAMP)) < 4:
        return False
    return bool(bf_type(state, 0, SPELL))


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            return True
    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        picks = hand(state, pid)[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            return True
        return False
    if wtype == "OrderTriggers":
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    da = find_action(acts, "DeclareAttackers")
    if da:
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True
    if is_my_main(state, pid):
        hid = find_hand(state, pid, ISLAND)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                await c.send_action(a)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if wtype not in ("Priority", None) and wplayer == 1:
        wire("p1_hold", {"wtype": wtype})
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------------ main

def has_backface(state):
    for o in objs(state).values():
        if o.get("zone") == "Battlefield" and o.get("controller") == 0:
            nm = lname(o)
            if "omnipotent" in nm:
                return True
            pw = (o.get("power") or {})
            tu = (o.get("toughness") or {})
            pv = pw.get("value") if isinstance(pw, dict) else pw
            tv = tu.get("value") if isinstance(tu, dict) else tu
            if pv == 7 and tv == 7 and "time sorceress" not in nm:
                return True
    return False


def has_frontface(state):
    return bool(bf_type(state, 0, SPELL))


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    try:
        await p0.create(P0_DECK)
    except Exception as e:
        say(f"game creation failed: {e}")
        A["A1_setup_ok"] = "blocked"
        obs["notes"].append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": A, "notes": obs["notes"],
                       "wf_sequence": WF_SEEN}, f, indent=2)
        await p0.close()
        return obs
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    global C0
    C0 = p0

    last_rev = {}
    TIMEOUT = 1500
    accept_t = None
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        for c, pid, tick in ((p0, p0.player_id, tick_p0),
                             (p1, p1.player_id, tick_p1)):
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if await tick(c, pid):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)
        record_turn(state)

        wf = state.get("waiting_for") or {}
        wtype = wf.get("type")
        wplayer = (wf.get("data") or {}).get("player")
        turn = state.get("turn_number")
        phase = state.get("phase") or ""
        active = state.get("active_player")

        # PRE: optional transform prompt pending for P0 at its End phase,
        # only once fully ready (gy>=8, mana available)
        if not ST["pre_exported"] and wtype in ("OptionalEffectChoice",
                                               "OptionalCostChoice") \
                and wplayer == 0 and phase == "End" and active == 0 \
                and transform_ready(state):
            pre = await do_export("pre_transform.json")
            ST["pre_exported"] = True
            ST["transform_turn"] = pre.get("turn_number")
            wire("pre_transform", {"turn": ST["transform_turn"],
                                  "gy": len(gy(pre, 0)),
                                  "islands": len(untapped_bf(pre, 0, ISLAND)),
                                  "swamps": len(untapped_bf(pre, 0, SWAMP)),
                                  "frontface_bf": has_frontface(pre)})
            say(f"[pre] exported turn {ST['transform_turn']} "
                f"gy={len(gy(pre, 0))}")

        if ST["accepted"] and accept_t is None:
            accept_t = time.time()

        # transform observed?
        if ST["accepted"] and not ST.get("transform_observed") \
                and has_backface(state):
            ST["transform_observed"] = True
            wire("transform_observed", {"turn": turn, "phase": phase})
            say(f"[observed] Ultimecia, Omnipotent on battlefield (turn {turn})")

        # stall watchdog: accepted but no transform within 240s
        if accept_t and not ST.get("transform_observed") \
                and time.time() - accept_t > 240:
            await do_export("mid_stall.json")
            obs["notes"].append("stall: accepted transform but no Omnipotent "
                                "face observed within 240s")
            say("[stall] watchdog fired")
            ST["stop"] = True

        T = ST["transform_turn"]
        if T is not None:
            # extra-turn main phase observation
            if turn == T + 1 and active == 0 \
                    and phase in ("PreCombatMain", "PostCombatMain"):
                if not ST["extra_main_seen"]:
                    ST["extra_main_seen"] = True
                    wire("extra_turn_main", {"turn": turn, "phase": phase})
                    say(f"[observed] P0 main phase on turn {turn} (=T+1)")
            # post export once P1's turn after the extra turn begins
            if turn == T + 2 and not ST["post_exported"]:
                await asyncio.sleep(1.0)
                post = await do_export("post_extra_turn.json")
                ST["post_exported"] = True
                wire("post_extra_turn", {"turn": post.get("turn_number"),
                                        "active": post.get("active_player"),
                                        "phase": post.get("phase"),
                                        "backface_bf": has_backface(post),
                                        "stack_empty": not (post.get("stack") or [])})
                say(f"[post] exported turn {post.get('turn_number')} "
                    f"active={post.get('active_player')}")
                ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def load_env(p):
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)

    def env_state(p):
        try:
            return load_env(p)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre_transform.json")
    post = env_state("post_extra_turn.json")
    T = ST["transform_turn"]

    # A1
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_transform.json never exported")
    else:
        ok_phase = pre.get("active_player") == 0 and pre.get("phase") == "End"
        ok_face = has_frontface(pre)
        ok_gy = len(gy(pre, 0)) >= 8
        ok_mana = len(untapped_bf(pre, 0, ISLAND)) >= 4 \
            and len(untapped_bf(pre, 0, SWAMP)) >= 4
        obs["notes"].append(
            f"pre_transform: end_phase={ok_phase} frontface_bf={ok_face} "
            f"gy={len(gy(pre, 0))} (need>=8) islands={len(untapped_bf(pre, 0, ISLAND))} "
            f"swamps={len(untapped_bf(pre, 0, SWAMP))} (need>=4 each) "
            f"turn={pre.get('turn_number')}")
        A["A1_setup_ok"] = "passed" if (ok_phase and ok_face and ok_gy
                                       and ok_mana) else "failed"

    # A2
    A["A2_transform_resolved"] = "passed" if ST.get("transform_observed") else "failed"
    obs["notes"].append(f"Omnipotent face observed on battlefield: "
                        f"{bool(ST.get('transform_observed'))}")

    # turn-sequence based assertions
    def active_of(target_turn):
        for tn, ap in TURN_SEQ:
            if tn == target_turn:
                return ap
        return None

    # A3
    if T is None:
        A["A3_extra_turn_next"] = "not-run"
        obs["notes"].append("transform turn unknown; A3 not-run")
    else:
        a1 = active_of(T + 1)
        obs["notes"].append(f"turn sequence: {TURN_SEQ}; active(T+1)={a1}")
        A["A3_extra_turn_next"] = "passed" if a1 == 0 else "failed"

    # A4
    A["A4_extra_turn_taken"] = "passed" if ST["extra_main_seen"] else "failed"
    obs["notes"].append(f"P0 main phase observed on turn T+1: "
                        f"{ST['extra_main_seen']}")

    # A5
    if T is None:
        A["A5_single_extra"] = "not-run"
        obs["notes"].append("transform turn unknown; A5 not-run")
    else:
        a2 = active_of(T + 2)
        A["A5_single_extra"] = "passed" if a2 == 1 else "failed"
        obs["notes"].append(f"active(T+2)={a2} (expect 1)")

    # A6
    if post is not None:
        empty = not (post.get("stack") or [])
        obs["notes"].append(f"post_extra_turn: turn={post.get('turn_number')} "
                            f"active={post.get('active_player')} "
                            f"phase={post.get('phase')} stack_empty={empty}")
        A["A6_cleanup"] = "passed" if empty else "failed"
    else:
        A["A6_cleanup"] = "not-run"
        obs["notes"].append("post_extra_turn.json missing; A6 not-run")

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)
    say("Turn sequence:", TURN_SEQ)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN, "turn_sequence": TURN_SEQ,
                   "transform_turn": T}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        if A.get("A2_transform_resolved") == "passed" \
                and A.get("A3_extra_turn_next") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6772,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": {"Ultimecia, Time Sorceress": 12, "Otherworldly Gaze": 12,
                   "Island": 18, "Swamp": 18},
            "P1": {"Island": 60},
        },
        "verdict": verdict,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", verdict)
    with open(f"{EVDIR}/scenario_6772.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
