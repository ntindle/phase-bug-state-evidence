#!/usr/bin/env python3
"""Issue #7198: Bellowing Mauler — "Should offer option of saccing or losing
life currently just makes you lose life."

Oracle (pinned card-data v0.86.0): "At the beginning of your end step, each
player loses 4 life unless they sacrifice a nontoken creature of their
choice."

Reported: the trigger makes each player lose 4 life directly with no
sacrifice choice offered. Triage (mike-theDude) found the parser dropped
the unless branch; classifier: supported_aspect_defect.

NOTE: pinned v0.86.0 card-data DOES carry an unless_pay branch now:
  unless_pay.cost = Sacrifice { target: Typed Creature, controller You,
    properties [NonToken], count 1 }, payer = TriggeringPlayer.
So this run tests actual engine behavior on the pinned release.

Behavioral contract, pinned v0.86.0 / protocol 72:
  Game A: P0: 4x Bellowing Mauler + 12x Walking Corpse + 44x Swamp.
          P1: 12x Walking Corpse + 48x Swamp. P0 casts Mauler on turn 5;
          both sides hold a nontoken creature at P0's end step.
    A1 setup_ok      Mauler on P0's battlefield; P0 and P1 each control
                     >=1 other nontoken creature; life 20/20 pre-trigger.
    A2 trigger_fired  Mauler's Phase trigger observed on the stack (or an
                     unless-pay choice is offered).
    A3 each_offered   P0 AND P1 each receive an independent sacrifice
                     choice (waiting_for naming each seat in turn).
                     Observed on v0.86.0: BOTH player-scope instances are
                     offered to the triggering player (P0); P1 is never
                     named. Fails by design of this run.
    A4 outcomes_A    P0 sacrifices once then declines P1's instance:
                     P0 at 20 with one corpse sacrificed; P1 at 16 with
                     their creature intact.
    A5 cross_player_B  P1 controls no creatures; P0 sacrifices twice
                     (corpse, then the Mauler itself): P1 stays at 20
                     without ever being offered a choice — P0's
                     sacrifice prevented P1's life loss, violating
                     "a successful sacrifice prevents only that
                     player's life loss".

Verdict = reproduced iff A1 passed and A3 failed (P1 never offered an
independent choice). not-reproduced iff A1-A5 all pass. blocked
otherwise.

Protocol-72 driver conventions (per AGENTS.md): CreateGameWithSettings +
JoinGameWithPassword + start_when_full, merged_actions, per-seat tick with
revision-change-or-5s re-tick, default PassPriority gated on my_priority,
MulliganDecision Keep via advertised action as-is, engine auto-taps mana,
DiscardToHandSize answered via viewer_interaction select (named player).
exactChoices answers use response type "choose" + singular choiceId.
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
RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "20260917-7198"
EVDIR = f"{BACKFILL}/evidence/7198/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MAULER = "Bellowing Mauler"
CORPSE = "Walking Corpse"
SWAMP = "Swamp"

GAME_TIMEOUT = 900


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def lname(state, oid):
    o = state["objects"].get(str(oid)) or {}
    return o.get("base_name") or o.get("name")


def is_mauler(state, oid):
    return (lname(state, oid) or "").lower() == "bellowing mauler"


def is_corpse(state, oid):
    return (lname(state, oid) or "").lower() == "walking corpse"


def player_of(state, pid):
    return state["players"][pid]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def bf_objs(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def nontoken_creatures(state, pid):
    out = []
    for o in bf_objs(state, pid):
        n = (o.get("base_name") or o.get("name") or "")
        if o.get("token"):
            continue
        # creature check: has power/toughness fields
        if o.get("power") is not None or "corpse" in n.lower() \
                or "mauler" in n.lower():
            out.append(o)
    return out


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if (lname(state, oid) or "").lower() == name.lower():
            return oid
    return None


def find_bf_mauler(state, pid):
    for o in bf_objs(state, pid):
        if is_mauler(state, o.get("id")):
            return o
    return None


def untapped_swamps(state, pid):
    return sum(1 for o in bf_objs(state, pid)
               if (o.get("base_name") or o.get("name")) == SWAMP
               and not o.get("tapped"))


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def stack_snapshot(state):
    return [{"id": e.get("id"),
             "kind": (e.get("kind") or {}).get("type"),
             "source_id": e.get("source_id"),
             "controller": e.get("controller"),
             "desc": ((e.get("kind") or {}).get("description")
                      or "")[:160]}
            for e in (state.get("stack") or [])]


def is_mauler_trigger(entry, mauler_oid):
    if (entry.get("kind") or "") != "TriggeredAbility":
        return False
    if mauler_oid is not None and entry.get("source_id") == mauler_oid:
        return True
    d = (entry.get("desc") or "").lower()
    return "loses 4 life" in d and "sacrifice" in d


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if a.get("data") not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


async def keep_mulligan(c):
    t0 = time.time()
    while time.time() - t0 < 60:
        await asyncio.sleep(0.25)
        st = c.latest
        if not st:
            continue
        for a in merged_actions(st):
            if a["type"] == "MulliganDecision":
                await submit_as_is(
                    c, {"type": "MulliganDecision",
                        "data": {"choice": {"type": "Keep"}}})
                say(f"{c.name} keeps opening hand")
                return True
    say(f"{c.name}: no mulligan decision seen in 60s")
    return False


SUBMITTED = set()


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def cand_ref(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def ch_text(ch):
    bits = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict):
            for k in ("text", "label", "value"):
                if d.get(k):
                    bits.append(f"{s.get('role')}:{k}={d[k]}")
    return "|".join(bits)


class Ctx:
    def __init__(self):
        self.p0_id = None
        self.p1_deck = None
        self.game = "A"
        self.plan = {}          # seat -> "sacrifice" | "decline"
        self.mauler_oid = None
        self.mauler_cast_turn = None
        self.endstep_turn = None   # P0 turn when the end-step trigger fired
        self.trigger_seen = False
        self.unless_offers = []    # (game, seat, wf_type, choice_made)
        self.unless_shapes = set()
        self.pre_exported = False
        self.land_turns = {}
        self.cast_turns = set()
        self.wf_seen = []
        self.rejections = []
        self.game_code = None
        self.logged_wait = set()
        self.stack_history = []
        self.phase_log = []
        self._last_phase_key = None
        self.sacrifice_oids = {}   # seat -> [oids sacrificed]
        self.unless_plan = []      # sequential plan for UnlessPayment-class
        self.unless_decisions = 0  # UnlessPayment decision answers so far
        self.current_unless_plan = None  # plan of the in-flight instance
        self.selectcards_shapes = set()


def record_wf(ctx, state):
    wt = wf_type(state)
    if wt and (not ctx.wf_seen or ctx.wf_seen[-1] != wt):
        ctx.wf_seen.append(wt)
        wire("waiting_for", {"type": wt, "data": wf_of(state).get("data")})


def record_phase(ctx, state):
    key = (state.get("turn_number"), state.get("active_player"),
           state.get("phase"))
    if key != ctx._last_phase_key:
        ctx._last_phase_key = key
        ctx.phase_log.append(list(key))


async def export_state(c, path):
    s = await c.export_state()
    with open(path, "w") as f:
        f.write(s)
    return json.loads(s)["state"]


def life_of(state, pid):
    return (state["players"][pid] or {}).get("life")


def zone_of(state, oid):
    return (state["objects"].get(str(oid)) or {}).get("zone")


def answer_unless(c, pid, ctx):
    """Answer a sacrifice-vs-life unless_pay prompt for this seat.

    Returns True if a submission was made. Logs the full prompt shape
    the first time each waiting_for type is seen.
    """
    st = c.latest
    state = st.get("state") or {}
    wf = wf_of(state)
    wtype = wf.get("type")
    if wtype not in ctx.unless_shapes:
        ctx.unless_shapes.add(wtype)
        vi = st.get("viewer_interaction") or {}
        shape = {"wf_type": wtype, "wf_data": wf.get("data"),
                 "opportunities": []}
        for op in vi.get("opportunities", []):
            resp = op.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            shape["opportunities"].append({
                "iid": op.get("interactionId"),
                "resp_type": resp.get("type"),
                "spec": data.get("spec"),
                "choices": [{"id": ch.get("id"), "text": ch_text(ch),
                             "ref": cand_ref(ch)} for ch in chs],
            })
        wire("unless_wait_shape", shape)
        say(f"{c.name} UNLESS prompt shape: "
            f"{json.dumps(shape)[:1500]}")
    return False  # placeholder; real logic below


async def do_answer_unless(c, pid, ctx):
    """Best-effort unless_pay answer for seat pid. Returns True if acted."""
    st = c.latest
    if not st:
        return False
    state = st.get("state") or {}
    wf = wf_of(state)
    wtype = wf.get("type")
    # Sequential plan for P0-named unless instances (the engine offers
    # every player-scope instance to the triggering player); a prompt
    # genuinely naming P1 falls back to P1's decline plan. Decision
    # prompts (UnlessPayment) advance the instance index; the follow-up
    # sacrifice selection belongs to the in-flight instance.
    is_decision = (wtype == "UnlessPayment")
    if is_decision:
        if pid == ctx.p0_id and ctx.unless_plan:
            idx = min(ctx.unless_decisions, len(ctx.unless_plan) - 1)
            plan = ctx.unless_plan[idx]
        else:
            plan = ctx.plan.get(pid, "decline")
    else:
        plan = ctx.current_unless_plan or (
            ctx.unless_plan[0] if ctx.unless_plan
            else ctx.plan.get(pid, "decline"))
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        # still allow a legal-action fallback below
        pass

    for op in vi.get("opportunities", []):
        iid = op.get("interactionId")
        if iid in SUBMITTED:
            continue
        resp = op.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []

        # (a) exactChoices bool accept/decline (e.g. decideUnlessPay)
        if rtype == "exactChoices":
            pick = None
            for ch in chs:
                txt = ch_text(ch).lower()
                is_true = any('value=true' in b for b in [txt])
                is_false = any('value=false' in b for b in [txt])
                if plan == "sacrifice" and is_true:
                    pick = ch["id"]
                elif plan == "decline" and is_false:
                    pick = ch["id"]
            # fallback: choose by position if exactly 2 bool choices and
            # text is empty (kicker-style): first=true=accept
            if pick is None and len(chs) == 2:
                vals = []
                for ch in chs:
                    t = ch_text(ch).lower()
                    vals.append("true" if "value=true" in t
                                else "false" if "value=false" in t
                                else "?")
                if vals == ["true", "false"]:
                    pick = chs[0]["id"] if plan == "sacrifice" \
                        else chs[1]["id"]
            if pick is not None:
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick}}}
                say(f"{c.name} unless {plan}: choose {pick} "
                    f"(exactChoices {wtype})")
                wire("unless_submit", {"who": c.name, "seat": pid,
                                       "plan": plan, "sub": sub,
                                       "wf_type": wtype})
                await c.send_interaction(sub)
                SUBMITTED.add(iid)
                ctx.unless_offers.append(
                    (ctx.game, wf_player(state), pid, wtype, plan))
                if wtype == "UnlessPayment":
                    ctx.current_unless_plan = plan
                    ctx.unless_decisions += 1
                return True

        # (b) schema select/sequence with creature candidates: pick one
        #     of my nontoken creatures when sacrificing
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        if rtype == "schema" and spec_type in ("select", "sequence"):
            refs = [(ch["id"], cand_ref(ch)) for ch in chs
                    if cand_ref(ch) is not None]
            if refs and plan == "sacrifice":
                # choose a Walking Corpse (never the Mauler)
                mine = [o["id"] for o in nontoken_creatures(state, pid)
                        if is_corpse(state, o["id"])]
                if not mine:
                    mine = [o["id"] for o in nontoken_creatures(state, pid)
                            if not is_mauler(state, o["id"])]
                ref_to_cid = {str(r): cid for cid, r in refs}
                for oid in mine:
                    if str(oid) in ref_to_cid:
                        cid = ref_to_cid[str(oid)]
                        sub = {"interactionId": iid,
                               "response": {"type": spec_type,
                                            "data": {"choiceIds": [cid]}}}
                        say(f"{c.name} unless sacrifice: select creature "
                            f"oid {oid} (choice {cid})")
                        wire("unless_submit",
                             {"who": c.name, "seat": pid, "plan": plan,
                              "sub": sub, "wf_type": wtype})
                        await c.send_interaction(sub)
                        SUBMITTED.add(iid)
                        ctx.sacrifice_oids.setdefault(pid, []).append(oid)
                        ctx.unless_offers.append(
                            (ctx.game, wf_player(state), pid, wtype,
                             plan))
                        return True
    # (c) legal-action fallback: SelectCards for the sacrifice selection
    # (the engine sometimes offers no viewer_interaction opportunity here)
    if plan == "sacrifice":
        for a in merged_actions(st):
            if a["type"] != "SelectCards":
                continue
            ad = dict(a.get("data") or {})
            if "SelectCards" not in ctx.selectcards_shapes:
                ctx.selectcards_shapes.add("SelectCards")
                wire("selectcards_shape",
                     {"wf_type": wtype, "data": ad})
                say(f"{c.name} SelectCards shape ({wtype}): "
                    f"{json.dumps(ad)[:400]}")
            mine = [o["id"] for o in nontoken_creatures(state, pid)
                    if is_corpse(state, o["id"])]
            if not mine:
                # game B second instance: only the Mauler remains; the
                # point is to show whose sacrifice spares whom
                mine = [o["id"] for o in nontoken_creatures(state, pid)]
            if mine:
                # NOTE (2026-09-17 #7198): WardSacrificeChoice advertises
                # SelectCards with data.cards (NOT cardIds). Set the
                # advertised key when present.
                if "cards" in ad:
                    ad["cards"] = [mine[0]]
                elif "cardIds" in ad:
                    ad["cardIds"] = [mine[0]]
                else:
                    ad["cards"] = [mine[0]]
                    ad["cardIds"] = [mine[0]]
                say(f"{c.name} unless sacrifice via SelectCards: "
                    f"oid {mine[0]} ({wtype})")
                wire("unless_submit",
                     {"who": c.name, "seat": pid, "plan": plan,
                      "sub": {"type": "SelectCards", "data": ad},
                      "wf_type": wtype})
                await c.send_action({"type": "SelectCards", "data": ad})
                ctx.sacrifice_oids.setdefault(pid, []).append(mine[0])
                ctx.unless_offers.append((ctx.game, wf_player(state), pid,
                                          wtype,
                                          plan + "+selectcards"))
                return True
    return False


async def answer_discard(c, pid, ctx):
    st = c.latest
    if not st:
        return False
    state = st.get("state") or {}
    if wf_type(state) != "DiscardToHandSize":
        return False
    if wf_player(state) != pid:
        return False
    count = (wf_of(state).get("data") or {}).get("count", 1)
    vi = get_vi(st)
    hand = hand_ids(state, pid)
    if vi:
        for op in vi.get("opportunities", []):
            if op.get("interactionId") in SUBMITTED:
                continue
            resp = op.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            data = resp.get("data", {}) or {}
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            if spec_type not in ("sequence", "select"):
                continue
            chs = data.get("choices") or data.get("candidates") or []
            if not any(cand_ref(ch) is not None for ch in chs):
                continue
            # protect Mauler, then corpses; dump lands first
            ranked = sorted(
                hand,
                key=lambda oid: (0 if (lname(state, oid) or "") == SWAMP
                                 else 1 if is_corpse(state, oid) else 2))
            pick = ranked[:count]
            ref_to_id = {str(cand_ref(ch)): ch["id"] for ch in chs
                         if cand_ref(ch) is not None}
            choice_ids = [ref_to_id[str(o)] for o in pick
                          if str(o) in ref_to_id]
            if choice_ids:
                sub = {"interactionId": op.get("interactionId"),
                       "response": {"type": spec_type,
                                    "data": {"choiceIds": choice_ids}}}
                say(f"{c.name} discards {choice_ids}")
                wire("discard_submit", sub)
                await c.send_interaction(sub)
                SUBMITTED.add(op.get("interactionId"))
                return True
    for a in merged_actions(st):
        if a["type"] == "SelectCards":
            data = dict(a.get("data") or {})
            ranked = sorted(
                hand,
                key=lambda o: (0 if (lname(state, o) or "") == SWAMP
                               else 1 if is_corpse(state, o) else 2))
            data["cardIds"] = ranked[:count]
            await c.send_action({"type": "SelectCards", "data": data})
            return True
    return False


UNLESS_TYPES = ("UnlessPay", "UnlessCost", "OptionalCostChoice",
                "SacrificeChoice", "PayCost")


def looks_like_unless(wtype):
    if not wtype:
        return False
    w = wtype.lower()
    return ("unless" in w or "sacrifice" in w or "optionalcost" in w
            or "paycost" in w)


async def tick(c, pid, ctx):
    st = c.latest
    if not st:
        return False
    state = st.get("state") or {}
    acts = merged_actions(st)
    if not acts:
        return False
    wtype = wf_type(state)
    my_priority = (wtype == "Priority"
                   and state.get("priority_player") == pid)

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True

    if wtype == "DiscardToHandSize" and wf_player(state) == pid:
        if await answer_discard(c, pid, ctx):
            return True
        return False

    # unless_pay / sacrifice choice naming this seat
    if looks_like_unless(wtype) and wf_player(state) == pid:
        # log shape on first sight (also when no submit possible)
        answer_unless(c, pid, ctx)
        if await do_answer_unless(c, pid, ctx):
            return True
        key = (c.name, c.revision, wtype)
        if key not in ctx.logged_wait:
            ctx.logged_wait.add(key)
            say(f"{c.name} unless-wait {wtype} unanswered; "
                f"acts={[a['type'] for a in acts]}")
            wire("unless_unanswered",
                 {"who": c.name, "wtype": wtype,
                  "acts": [a["type"] for a in acts]})
        return False

    # other non-priority waits naming this seat: do not pass blindly
    if (wtype not in (None, "Priority", "DeclareAttackers",
                      "DeclareBlockers")
            and wf_player(state) == pid):
        key = (c.name, c.revision, wtype)
        if key not in ctx.logged_wait:
            ctx.logged_wait.add(key)
            say(f"{c.name} waiting_for={wtype} names seat {pid}; "
                f"acts={[a['type'] for a in acts]}; not passing")
            wire("unhandled_wait", {"who": c.name, "wtype": wtype,
                                    "acts": [a["type"] for a in acts],
                                    "data": wf_of(state).get("data")})
        return False

    # P0 main-phase development
    if (pid == ctx.p0_id and my_priority
            and state.get("active_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")):
        turn = state.get("turn_number")
        if turn not in ctx.land_turns.get(pid, set()):
            pl = next((a for a in acts
                       if a["type"] == "PlayLand"
                       and (lname(state, (a.get("data") or {})
                                  .get("object_id")) or "") == SWAMP), None)
            if pl:
                await submit_as_is(c, pl)
                ctx.land_turns.setdefault(pid, set()).add(turn)
                say(f"{c.name} plays {SWAMP} (turn {turn})")
                return True
        mauler = find_bf_mauler(state, pid)
        if mauler:
            ctx.mauler_oid = mauler["id"]
        # cast a corpse once we have 2 mana and none out
        corpses = [o for o in nontoken_creatures(state, pid)
                   if is_corpse(state, o["id"])]
        if (not corpses and untapped_swamps(state, pid) >= 2
                and (turn, "corpse") not in ctx.cast_turns):
            hid = find_hand(state, pid, CORPSE)
            if hid is not None:
                cs = next((a for a in acts
                           if a["type"] == "CastSpell"
                           and str((a.get("data") or {}).get("object_id"))
                           == str(hid)), None)
                if cs:
                    ctx.cast_turns.add((turn, "corpse"))
                    await submit_as_is(c, cs)
                    say(f"{c.name} casts {CORPSE} (turn {turn})")
                    return True
        # cast Mauler once we have 5 mana
        if (mauler is None and untapped_swamps(state, pid) >= 5
                and (turn, "mauler") not in ctx.cast_turns):
            hid = find_hand(state, pid, MAULER)
            if hid is not None:
                cs = next((a for a in acts
                           if a["type"] == "CastSpell"
                           and str((a.get("data") or {}).get("object_id"))
                           == str(hid)), None)
                if cs:
                    ctx.cast_turns.add((turn, "mauler"))
                    await submit_as_is(c, cs)
                    ctx.mauler_cast_turn = turn
                    say(f"{c.name} casts {MAULER} (turn {turn})")
                    return True

    # P1 development: land + corpse (game A only)
    if pid != ctx.p0_id:
        if (state.get("active_player") == pid
                and state.get("phase") in ("PreCombatMain",
                                           "PostCombatMain")):
            turn = state.get("turn_number")
            if turn not in ctx.land_turns.get(pid, set()):
                pl = next((a for a in acts
                           if a["type"] == "PlayLand"
                           and (lname(state, (a.get("data") or {})
                                      .get("object_id")) or "")
                           == SWAMP), None)
                if pl:
                    await submit_as_is(c, pl)
                    ctx.land_turns.setdefault(pid, set()).add(turn)
                    return True
            if ctx.game == "A":
                corpses = [o for o in nontoken_creatures(state, pid)
                           if is_corpse(state, o["id"])]
                if (not corpses and untapped_swamps(state, pid) >= 2
                        and (turn, "p1corpse") not in ctx.cast_turns):
                    hid = find_hand(state, pid, CORPSE)
                    if hid is not None:
                        cs = next((a for a in acts
                                   if a["type"] == "CastSpell"
                                   and str((a.get("data") or {})
                                           .get("object_id"))
                                   == str(hid)), None)
                        if cs:
                            ctx.cast_turns.add((turn, "p1corpse"))
                            await submit_as_is(c, cs)
                            say(f"{c.name} casts {CORPSE} (turn {turn})")
                            return True
        if state.get("phase") == "DeclareAttackers":
            da = next((a for a in acts
                       if a["type"] == "DeclareAttackers"), None)
            if da:
                na = copy.deepcopy(da)
                nd = na.setdefault("data", {})
                nd["attacks"] = []
                nd["bands"] = []
                await c.send_action(na)
                return True
        if state.get("phase") == "DeclareBlockers":
            db = next((a for a in acts
                       if a["type"] == "DeclareBlockers"), None)
            if db:
                na = copy.deepcopy(db)
                nd = na.setdefault("data", {})
                nd["blocks"] = []
                await c.send_action(na)
                return True

    # P0 combat: never attack (keep board/life clean)
    if (pid == ctx.p0_id and state.get("phase") == "DeclareAttackers"):
        da = next((a for a in acts
                   if a["type"] == "DeclareAttackers"), None)
        if da:
            na = copy.deepcopy(da)
            nd = na.setdefault("data", {})
            nd["attacks"] = []
            nd["bands"] = []
            await c.send_action(na)
            return True

    if my_priority:
        pp = next((a for a in acts if a["type"] == "PassPriority"), None)
        if pp:
            await submit_as_is(c, pp)
            return True
    return False


def sample_stack(c, ctx):
    st = c.latest
    if not st:
        return
    state = st.get("state") or {}
    stk = state.get("stack") or []
    if not stk:
        return
    key = (c.revision, tuple(e.get("id") for e in stk))
    if ctx.stack_history and ctx.stack_history[-1].get("_key") == key:
        return
    snap = stack_snapshot(state)
    entry = {"rev": c.revision, "turn": state.get("turn_number"),
             "phase": state.get("phase"),
             "life": (life_of(state, 0), life_of(state, 1)),
             "stack": snap, "_key": key}
    ctx.stack_history.append(entry)
    wire("stack_sample", {k: v for k, v in entry.items() if k != "_key"})
    for e in snap:
        if is_mauler_trigger(e, ctx.mauler_oid) and not ctx.trigger_seen:
            ctx.trigger_seen = True
            ctx.endstep_turn = state.get("turn_number")
            say(f"MAULER TRIGGER on stack (game {ctx.game}):",
                json.dumps(snap)[:500])
            wire("mauler_trigger_stack", snap)


async def drive(p0, p1, ctx, want_fn, timeout_s, label):
    t0 = time.time()
    last = {p0.name: (-1, 0.0), p1.name: (-1, 0.0)}
    warned = set()
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.25)
        for c, pid in ((p0, ctx.p0_id), (p1, 1 - ctx.p0_id)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            last_rev, last_tick = last[c.name]
            if rev == last_rev and c.name not in warned \
                    and time.time() - last_tick > 45:
                warned.add(c.name)
                say(f"WATCHDOG: {c.name} revision {rev} unchanged for 45s")
                wire("watchdog_stall", {"who": c.name, "rev": rev})
            may_act = (rev != last_rev) or (time.time() - last_tick > 5)
            if may_act:
                try:
                    acted = await tick(c, pid, ctx)
                except Exception as e:
                    say(f"tick error for {c.name}: {e}")
                    acted = False
                if acted:
                    last[c.name] = (rev, time.time())
        st = p0.latest
        if st:
            state = st.get("state") or {}
            sample_stack(p0, ctx)
            record_phase(ctx, state)
            record_wf(ctx, state)
            if want_fn(state):
                return state
            if state.get("game_over") or state.get("winner") is not None:
                say("game ended during", label)
                return None
    say(f"TIMEOUT in drive: {label}")
    return None


async def run_game(p0_deck, p1_deck, ctx, game_label, timeout_s=600):
    """Set up and drive one game through P0's Mauler end step."""
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*p1_deck))
    ctx.p0_id = p0.player_id
    p1_id = 1 - p0.player_id
    ctx.game_code = p0.game_code
    ctx.plan[ctx.p0_id] = "sacrifice"
    ctx.plan[p1_id] = "decline"
    # Sequential unless-instance plan: the engine offers every
    # player-scope instance of the trigger to the triggering player.
    # Game A: sacrifice for P0's own instance, decline P1's instance
    # (P1 should lose 4 life with their creature intact).
    # Game B: sacrifice for both instances (second sacrifice is the
    # Mauler itself) to show whose payment spares whom.
    ctx.unless_plan = (["sacrifice", "decline"] if ctx.game == "A"
                       else ["sacrifice", "sacrifice"])
    say(f"game {game_label}: code {p0.game_code}; "
        f"P0 seat={p0.player_id} P1 seat={p1_id}")
    wire("game_start", {"game": game_label, "code": p0.game_code,
                        "p0": p0.player_id, "p1": p1_id})

    await keep_mulligan(p0)
    await keep_mulligan(p1)

    def mauler_out(st):
        return find_bf_mauler(st, ctx.p0_id) is not None

    s = await drive(p0, p1, ctx, mauler_out, timeout_s,
                    f"{game_label}: cast Mauler")
    if s is None:
        say(f"{game_label}: Mauler never reached the battlefield")
        await p0.close()
        await p1.close()
        return None, None

    # drive through P0's next end step (the trigger turn)
    def endstep_resolved(st):
        if ctx.endstep_turn is None:
            return False
        return (st.get("turn_number") != ctx.endstep_turn
                and st.get("active_player") == ctx.p0_id
                and st.get("phase") == "PreCombatMain"
                and not (st.get("stack") or []))

    # export pre_trigger at the first end-step checkpoint
    async def watch():
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            await asyncio.sleep(0.25)
            st = p0.latest
            if not st:
                continue
            state = st.get("state") or {}
            if (not ctx.pre_exported
                    and state.get("active_player") == ctx.p0_id
                    and state.get("phase") == "End"
                    and find_bf_mauler(state, ctx.p0_id)):
                ctx.pre_exported = True
                if ctx.endstep_turn is None:
                    # backstop: completion must not depend on the stack
                    # sampler having caught the trigger
                    ctx.endstep_turn = state.get("turn_number")
                    say(f"{game_label}: endstep_turn backstop set to "
                        f"{ctx.endstep_turn}")
                pre = await export_state(
                    p0, f"{EVDIR}/pre_trigger_{game_label}.json")
                say(f"{game_label} pre_trigger: turn "
                    f"{pre.get('turn_number')} life="
                    f"{life_of(pre, 0)}/{life_of(pre, 1)} "
                    f"p0_creatures={len(nontoken_creatures(pre, ctx.p0_id))} "
                    f"p1_creatures={len(nontoken_creatures(pre, p1_id))}")
                wire("pre_trigger",
                     {"life": (life_of(pre, 0), life_of(pre, 1)),
                      "stack": stack_snapshot(pre)})
            if endstep_resolved(state):
                return state
            if state.get("game_over") or state.get("winner") is not None:
                return None
        return None

    # run drive + watch concurrently: drive ticks, watch checks completion
    async def drive_forever():
        await drive(p0, p1, ctx, lambda st: False, timeout_s,
                    f"{game_label}: end step")

    done, pending = await asyncio.wait(
        [asyncio.create_task(drive_forever()),
         asyncio.create_task(watch())],
        return_when=asyncio.FIRST_COMPLETED, timeout=timeout_s)
    for t in pending:
        t.cancel()
    post = None
    for t in done:
        try:
            r = t.result()
            if isinstance(r, dict):
                post = r
        except Exception:
            pass
    if post is not None:
        postx = await export_state(p0, f"{EVDIR}/post_trigger_{game_label}.json")
        say(f"{game_label} post_trigger: life="
            f"{life_of(postx, 0)}/{life_of(postx, 1)}")
        wire("post_trigger", {"life": (life_of(postx, 0),
                                       life_of(postx, 1)),
                              "stack": stack_snapshot(postx)})
    await p0.close()
    await p1.close()
    return post, p1_id


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    p0_id_global = None

    # ---------- Game A: both sides hold a creature ----------
    ctx = Ctx()
    ctx.game = "A"
    ctx.p1_deck = "corpse"
    post_a, p1a = await run_game(
        [(MAULER, 12), (CORPSE, 8), (SWAMP, 40)],
        [(CORPSE, 12), (SWAMP, 48)], ctx, "A")
    p0_id_global = ctx.p0_id
    obs["notes"].append(f"game A unless_offers={ctx.unless_offers} "
                        f"trigger_seen={ctx.trigger_seen} "
                        f"endstep_turn={ctx.endstep_turn}")
    a = obs["assert"]

    pre_a = None
    pre_path = f"{EVDIR}/pre_trigger_A.json"
    if os.path.exists(pre_path):
        pre_a = json.load(open(pre_path))["state"]
    post_path = f"{EVDIR}/post_trigger_A.json"
    post_ax = json.load(open(post_path))["state"] \
        if os.path.exists(post_path) else None

    if pre_a is None:
        a["A1_setup_ok"] = "not-run"
        obs["notes"].append("game A: pre_trigger export missing; "
                            "Mauler may never have reached the end step")
    else:
        p0a = ctx.p0_id
        mauler = find_bf_mauler(pre_a, p0a)
        p0c = [o for o in nontoken_creatures(pre_a, p0a)
               if not is_mauler(pre_a, o["id"])]
        p1c = nontoken_creatures(pre_a, p1a)
        ok = (mauler is not None and len(p0c) >= 1 and len(p1c) >= 1
              and life_of(pre_a, 0) == 20 and life_of(pre_a, 1) == 20)
        a["A1_setup_ok"] = "passed" if ok else "failed"
        obs["notes"].append(
            f"game A pre_trigger: mauler={'yes' if mauler else 'no'} "
            f"p0_other_creatures={len(p0c)} p1_creatures={len(p1c)} "
            f"life={life_of(pre_a,0)}/{life_of(pre_a,1)}")
    a["A2_trigger_fired"] = "passed" if ctx.trigger_seen else "failed"

    offered_named = {named for (_g, named, _a, _w, _p)
                     in ctx.unless_offers}
    a["A3_each_offered"] = "passed" if offered_named == {ctx.p0_id, p1a} \
        else "failed"
    obs["notes"].append(
        f"game A: unless choice NAMED seats {sorted(offered_named)} "
        f"(expected [{ctx.p0_id}, {p1a}] for independent per-player "
        f"choices); offers={ctx.unless_offers}")

    if post_ax is not None and a["A1_setup_ok"] == "passed":
        p0a = ctx.p0_id
        sac_oids = ctx.sacrifice_oids.get(p0a, [])
        sac_oid = sac_oids[-1] if sac_oids else None
        p0_life = life_of(post_ax, 0)
        sac_zone = zone_of(post_ax, sac_oid) if sac_oid else None
        p0_corpses = [o for o in nontoken_creatures(post_ax, p0a)
                      if is_corpse(post_ax, o["id"])]
        a["A4_outcomes_A"] = "passed" if (
            p0_life == 20 and sac_zone == "Graveyard"
            and life_of(post_ax, 1) == 16
            and len(nontoken_creatures(post_ax, p1a)) >= 1) else "failed"
        obs["notes"].append(
            f"game A post: p0_life={p0_life} (want 20), sacrificed "
            f"oids {sac_oids} (last zone={sac_zone}, want Graveyard), "
            f"p0 corpses left={len(p0_corpses)}, "
            f"p1_life={life_of(post_ax,1)} (want 16), "
            f"p1_creatures={len(nontoken_creatures(post_ax, p1a))} "
            f"(want >=1 intact)")
    else:
        a.setdefault("A4_outcomes_A", "not-run")

    # ---------- Game B: P1 has no creatures ----------
    ctxB = Ctx()
    ctxB.game = "B"
    ctxB.p1_deck = "none"
    post_b, p1b = await run_game(
        [(MAULER, 12), (CORPSE, 8), (SWAMP, 40)],
        [(SWAMP, 60)], ctxB, "B")
    obs["notes"].append(f"game B unless_offers={ctxB.unless_offers} "
                        f"trigger_seen={ctxB.trigger_seen}")
    pre_b = None
    pre_b_path = f"{EVDIR}/pre_trigger_B.json"
    if os.path.exists(pre_b_path):
        pre_b = json.load(open(pre_b_path))["state"]
    post_bx = None
    post_b_path = f"{EVDIR}/post_trigger_B.json"
    if os.path.exists(post_b_path):
        post_bx = json.load(open(post_b_path))["state"]

    if pre_b is None or post_bx is None:
        a["A5_cross_player_B"] = "not-run"
        obs["notes"].append("game B: pre/post trigger export missing")
    else:
        p0b = ctxB.p0_id
        p1c_pre = nontoken_creatures(pre_b, p1b)
        p1_life = life_of(post_bx, 1)
        p0_life = life_of(post_bx, 0)
        sac_oids_b = ctxB.sacrifice_oids.get(p0b, [])
        sac_oid = sac_oids_b[-1] if sac_oids_b else None
        sac_zone = zone_of(post_bx, sac_oid) if sac_oid else None
        p0_creatures_post = nontoken_creatures(post_bx, p0b)
        named_b = {named for (_g, named, _a, _w, _p)
                   in ctxB.unless_offers}
        # Defect demonstration: P0 sacrificed twice (corpse + Mauler);
        # the second sacrifice spared P1, who never chose anything.
        ok = (len(p1c_pre) == 0 and p0_life == 20 and p1_life == 20
              and len(p0_creatures_post) == 0 and p1b not in named_b)
        a["A5_cross_player_B"] = "passed" if ok else "failed"
        obs["notes"].append(
            f"game B: p1 pre creatures={len(p1c_pre)} (want 0), "
            f"post p1_life={p1_life} (want 20: spared by P0's second "
            f"sacrifice), p0_life={p0_life} (want 20), "
            f"p0 creatures post={len(p0_creatures_post)} (want 0), "
            f"sacrificed oids {sac_oids_b} (last zone={sac_zone}), "
            f"named seats {sorted(named_b)} (P1 never named)")

    # ---------- verdict ----------
    if a.get("A1_setup_ok") == "passed" \
            and a.get("A3_each_offered") == "failed":
        verdict = "reproduced"
    elif all(a.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_trigger_fired", "A3_each_offered",
              "A4_outcomes_A", "A5_cross_player_B")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    obs["notes"].append(f"verdict={verdict}")

    result = {
        "issue": 7198,
        "run_id": RUN_ID,
        "game_code": ctx.game_code,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "verdict": verdict,
        "assertions": a,
        "notes": obs["notes"],
        "wf_seen": sorted(set(ctx.wf_seen + ctxB.wf_seen)),
        "gameA": {"unless_offers": ctx.unless_offers,
                  "sacrifice_oids": {str(k): v for k, v in
                                     ctx.sacrifice_oids.items()},
                  "trigger_seen": ctx.trigger_seen,
                  "endstep_turn": ctx.endstep_turn,
                  "phase_log": ctx.phase_log},
        "gameB": {"unless_offers": ctxB.unless_offers,
                  "sacrifice_oids": {str(k): v for k, v in
                                     ctxB.sacrifice_oids.items()},
                  "trigger_seen": ctxB.trigger_seen,
                  "endstep_turn": ctxB.endstep_turn,
                  "phase_log": ctxB.phase_log},
        "decks": {
            "P0": [[MAULER, 12], [CORPSE, 8], [SWAMP, 40]],
            "P1A": [[CORPSE, 12], [SWAMP, 48]],
            "P1B": [[SWAMP, 60]],
        },
        "scenario_sha256": sha256_of_file(__file__),
    }
    with open(f"{EVDIR}/scenario_result.json", "w") as f:
        json.dump(result, f, indent=1)
    say(f"DONE verdict={verdict} assertions={json.dumps(a)}")
    try:
        WIRE.close()
        RUNLOG.close()
    except Exception:
        pass
    return result


asyncio.run(main())
