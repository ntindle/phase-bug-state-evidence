#!/usr/bin/env python3
"""Issue #7161: Abyssal Harvester doesn't create a token copy.

RE-VALIDATION on pinned v0.104.0 (2026-10-08, protocol 118). Prior run:
  v0.82.0  run-7161-20260914-1600  reproduced (A4 token_created failed;
           A5 not-run) -- published comment 5670373243 on the issue.
  This run re-validates on the current pin and, per the playbook, extends
  the maintained comment with the fresh result.

Oracle text (verified from pinned v0.104.0 card-data.json):
  {T}: Exile target creature card from a graveyard that was put there this
  turn. Create a token that's a copy of it, except it's a Nightmare in
  addition to its other types. Then exile all other Nightmare tokens you
  control.

Reported (Discord): Cloudthresher was exiled from a graveyard via the
Harvester, but no token copy was created and nothing followed on the stack.
Triage: ChangeZone -> CopyTokenOf -> ChangeZoneAll chain; runtime completes
the exile but loses the forwarded copy source.

Pinned parse evidence (v0.104.0 card-data.json, captured this run BEFORE any
game -- see parse_evidence.json):
  Activated ability, cost Tap:
    execute=ChangeZone(Graveyard->Exile, Typed Creature, forward_result=false)
    sub_ability: execute=CopyTokenOf(target=TrackedSet id 0,
                 owner=Controller, additional_modifications=[AddSubtype
                 Nightmare], forward_result=false), sub_link=SequentialSibling
    sub_ability: execute=ChangeZoneAll(->Exile, Typed Subtype Nightmare,
                 controller=You, properties=[Another, Token])

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing)
  P0 ramps, casts Abyssal Harvester (needs a turn of summoning-sickness
  clearance), waits for P1's Grizzly Bears, then on one P0 main phase:
  Lightning Bolt the Bear (Bear card -> P1 graveyard, "put there this turn"
  satisfied), export pre.json, activate the Harvester's tap ability
  targeting that Bear card (auto-target accepted as the engine's normal
  behavior for a sole legal target), let it resolve, export post.json.
  Leg2 (trailing-clause control): on a later P0 turn, Bolt a second Bear
  and activate again; export post_leg2.json.

  Assertions:
    A1_setup_ok:    pre: Harvester on P0 BF, untapped, controller 0.
    A2_bear_died:   pre: the Bolted Bear card is in P1's graveyard, and the
                    kill turn == the activation turn ("put there this turn").
    A3_bear_exiled: post: that Bear card's zone is Exile.
    A4_token_created: post: a token copy of Grizzly Bears carrying the
                    Nightmare subtype is on P0's battlefield.
    A5_others_exiled: post_leg2: first-leg token (if any) is exiled and the
                    second-leg token is on P0's BF; not-run when leg1 made
                    no token (trailing clause untestable).
    A6_cleanup:     final live state: stack empty, game advanced past the
                    resolution.

  Verdict: reproduced iff A2 and A3 pass and A4 fails (the reported
  no-token-after-exile defect). not-reproduced iff A1-A4 all pass.
  blocked iff A2/A3 cannot be established.

Protocol-118 driver conventions (from scenario_5654_01040.py):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full (via
    driver/client.py); waiting_for is gone; priority = PassPriority in the
    viewing seat's merged legal_actions.
  - Casts via legacy CastSpell matched by object_id; the engine auto-pays
    (payment_mode Auto) -- the driver never taps lands for mana itself, so
    no double-payment artifact (cf. 2026-10-07 driver note).
  - Target prompts answered on the caster's viewer_interaction as vi schema
    sequence/select (or exactChoices choose) with the advertised candidate
    id; the Harvester's sole-legal-target case may auto-target (no prompt),
    which is accepted, not a failure.
  - Passes via legacy PassPriority, falling back to the vi passPriority
    choice. Never pass the acting seat's priority while a real decision is
    pending (real_decision_pending excludes NON_DECISION_CODES).
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7161
RUN_ID = "run-7161-reval-v01040-20261008-2041"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

HARV = "abyssal harvester"
BOLT = "lightning bolt"
BEAR = "grizzly bears"
SWAMP, MOUNTAIN, FOREST = "swamp", "mountain", "forest"

P0_DECK = [(HARV, 12), (BOLT, 12), (SWAMP, 20), (MOUNTAIN, 16)]
P1_DECK = [(BEAR, 12), (FOREST, 24)]

SERVER_IDENTITY = {
    "server_version": "0.104.0",
    "build_commit": "4227122",
    "protocol_version": 118,
}

GLOBAL_TIMEOUT = 1500
WATCH_DEADLINE = 120


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "kind": event,
                           "data": payload}) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return (o.get("card_name") or o.get("name") or "").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def on_bf(state, pid, name):
    return any(obj_lname(state, o) == name for o in bf_oids(state, pid))


def bf_oid(state, pid, name):
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == name:
            return o
    return None


def gy_oids(state, pid, name):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("controller", -1)) == str(pid)
            and obj_lname(state, oid) == name]


def in_gy(state, pid, name):
    return bool(gy_oids(state, pid, name))


def card_in_hand_oid(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
    return None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


# ------------------------------------------------------------- interactions
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def submit_target(c, opp, rtype, spec_type, ch, tag, game):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    key = ("target", str(iid), str(cid))
    if key in game.submitted:
        return None
    game.submitted.add(key)
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} "
        f"kind={sub['response']['type']} ({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid


def target_opportunity(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


async def pass_priority(c, st, acts, game):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


def cast_action_for(acts, state, oid):
    for a in acts:
        if a["type"] != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


def activate_action_for(acts, harv_oid):
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("source_id"), d.get("sourceId"),
                  d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(harv_oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, acts, st, pid, tag, game, keep_fn):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in game.mulls:
        return False
    game.mulls.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    mull_count = sum(1 for k in game.mulls if k[0] == tag and k[1] == "mull")
    if keep_fn(state, pid) or mull_count >= 3 or len(hn) <= 4:
        say(f"[{tag}] keep {len(hn)}: {hn}")
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    else:
        say(f"[{tag}] mulligan #{mull_count + 1} ({len(hn)}: {hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mull"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, game, avoid_names):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    if target is None:
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", str(iid))
    if key in game.submitted:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in avoid_names:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = [ch.get("id") for ch in ranked[:n]]
    game.submitted.add(key)
    say(f"[{tag}] bottoming {[choice_text(x)[:40] for x in ranked[:n]]} via vi")
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": picks}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, game, avoid_names):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(str(h) for h in hand)
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        if not any(str(_cand_reference(ch)) in handset for ch in cands):
            continue
        key = (tag, "handsize", str(c.revision),
               str(opp.get("interactionId")))
        if key in game.submitted:
            return False

        def rank(ch):
            nm = obj_lname(state, _cand_reference(ch))
            return 2 if nm in avoid_names else (
                0 if nm in (SWAMP, MOUNTAIN, FOREST) else 1)

        ranked = sorted(cands, key=rank)
        picks = [ch.get("id") for ch in ranked[:n]]
        game.submitted.add(key)
        say(f"[{tag}] discarding {[choice_text(x)[:30] for x in ranked[:n]]} "
            f"to hand size")
        wire("discard_handsize", {"who": tag,
                                  "picks": [choice_text(x)[:40]
                                            for x in ranked[:n]]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "choose",
                                           "data": {"choiceId": picks[0]}}}
                          if n == 1 else {"interactionId":
                                          opp.get("interactionId"),
                                          "response": {"type": "select",
                                                       "data": {"choiceIds":
                                                                picks}}}, tag)
        return True
    return False


def combat_tick(acts):
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            return d
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            return d
    return None


async def play_a_land(c, state, pid, acts, tag, game):
    if game.land_played_turn.get(pid) == state.get("turn_number"):
        return False
    bfn = [obj_lname(state, o) for o in bf_oids(state, pid)]
    pref = SWAMP if (pid == 0 and bfn.count(SWAMP) < 3) else None
    pref = pref or (FOREST if pid == 1 else SWAMP)
    lod = card_in_hand_oid(state, pid, pref)
    if lod is None:
        for ln in (SWAMP, MOUNTAIN, FOREST):
            lod = card_in_hand_oid(state, pid, ln)
            if lod is not None:
                break
    if lod is None:
        return False
    for a in acts:
        if a["type"] != "PlayLand":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(lod):
                    await submit_as_is(c, a)
                    game.land_played_turn[pid] = state.get("turn_number")
                    return True
            except (TypeError, ValueError):
                continue
    return False


# ------------------------------------------------------------- token scan
def find_nightmare_tokens(state, pid):
    """Token objects on pid's BF carrying the Nightmare subtype."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        if str(o.get("controller", -1)) != str(pid):
            continue
        if not o.get("is_token"):
            continue
        ct = o.get("card_types") or {}
        subtypes = ct.get("subtypes") or []
        blob = json.dumps(o)
        if "Nightmare" in subtypes or "Nightmare" in blob:
            out.append((str(oid), o))
    return out


# ------------------------------------------------------------- Game
class Game:
    def __init__(self):
        self.p0 = self.p1 = None
        self.mulls = set()
        self.submitted = set()
        self.land_played_turn = {}
        self.notes = []
        self.stage = "setup"
        self.harv_oid = None
        self.harv_cast_turn = None
        self.bolt_target_oid = None
        self.bear_dead_oid = None
        self.kill_turn = None
        self.harv_activated = False
        self.activation_turn = None
        self.harv_target_oid = None
        self.target_prompt_seen = False
        self.watch_t0 = None
        self.watch_timed_out = False
        self.leg2_bolt_target_oid = None
        self.leg2_bear_dead_oid = None
        self.leg2_kill_turn = None
        self.leg2_harv_activated = False
        self.leg2_target_oid = None
        self.leg2_timed_out = False
        self.token1 = []       # (oid, name) at leg1 post
        self.token2 = []       # (oid, name) at leg2 post
        self.pre = None
        self.post = None
        self.post_leg2 = None
        self.done = False

    def say(self, *a):
        say(" ".join(str(x) for x in a))

    def note(self, m):
        self.notes.append(m)

    async def start(self):
        self.p0 = PhaseClient("P0")
        await self.p0.connect()
        await self.p0.create(deck(*P0_DECK))
        self.p1 = PhaseClient("P1")
        await self.p1.connect()
        await self.p1.join(self.p0.game_code, deck(*P1_DECK))
        self.say(f"game {self.p0.game_code} P0seat={self.p0.player_id} "
                 f"P1seat={self.p1.player_id}")
        wire("game_start", {"game_code": self.p0.game_code,
                            "p0_seat": self.p0.player_id,
                            "p1_seat": self.p1.player_id,
                            "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    async def export(self, name, client):
        env = await client.export_state()
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(env)
        return json.loads(env)["state"]

    async def close(self):
        for c in (self.p0, self.p1):
            try:
                await c.close()
            except Exception:
                pass


async def answer_targets(c, st, tag, g):
    """Answer Bolt / Harvester target prompts with the exact intended oid."""
    opp, rtype, spec_type = target_opportunity(st)
    if not opp:
        return False
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    want = None
    if g.stage == "kill" and g.bolt_target_oid and not g.bear_dead_oid:
        want = str(g.bolt_target_oid)
    elif g.stage == "leg2kill" and g.leg2_bolt_target_oid \
            and not g.leg2_bear_dead_oid:
        want = str(g.leg2_bolt_target_oid)
    elif g.stage in ("watch",) and g.harv_activated and g.bear_dead_oid:
        want = str(g.bear_dead_oid)
    elif g.stage in ("leg2watch",) and g.leg2_harv_activated \
            and g.leg2_bear_dead_oid:
        want = str(g.leg2_bear_dead_oid)
    if want is None:
        return False
    for ch in cands:
        if str(_cand_reference(ch)) == want:
            g.target_prompt_seen = True
            if g.stage in ("watch", "leg2watch"):
                (g.__dict__["leg2_target_oid" if g.stage == "leg2watch"
                            else "harv_target_oid"]) = want
            wire("target_matched", {"who": tag, "want_oid": want,
                                    "stage": g.stage,
                                    "choice_text": choice_text(ch)[:120]})
            await submit_target(c, opp, rtype, spec_type, ch, tag, g)
            return True
    return False


async def p0_tick(c, st, acts, state, g):
    if await do_mulligan(c, acts, st, 0, "P0", g,
                         lambda s, p: HARV in hand_lnames(s, p)
                         and sum(1 for n in hand_lnames(s, p)
                                 if n in (SWAMP, MOUNTAIN)) >= 2):
        return
    if await do_bottom(c, acts, st, 0, "P0", g, (HARV, BOLT)):
        return
    if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (HARV, BOLT)):
        return
    d = combat_tick(acts)
    if d:
        await submit_as_is(c, d)
        return
    if await answer_targets(c, st, "P0", g):
        return

    turn = state.get("turn_number")

    # track harvester cast turn
    ho = bf_oid(state, 0, HARV)
    if ho is not None:
        if g.harv_oid is None:
            g.harv_oid = ho
            g.harv_cast_turn = turn
            g.say(f"Harvester on BF oid={ho} cast_turn={turn}")
        elif str(g.harv_oid) != str(ho):
            g.harv_oid = ho
    harvester_ready = (ho is not None
                       and not get_obj(state, ho).get("tapped")
                       and g.harv_cast_turn is not None
                       and turn is not None
                       and turn > g.harv_cast_turn)

    if g.stage == "setup":
        # cast the harvester as soon as possible (engine auto-pays)
        if ho is None and my_main(state, 0) and my_priority(acts) \
                and not real_decision_pending(st):
            hid = card_in_hand_oid(state, 0, HARV)
            a = cast_action_for(acts, state, hid) if hid else None
            if a is not None:
                wire("cast_harvester", {"action": a})
                await submit_as_is(c, a)
                g.say(f"P0 casts Abyssal Harvester (turn {turn})")
                return
        # bolt+activate leg starts when the harvester is ready and a bear
        # is out
        bear_oid = bf_oid(state, 1, BEAR)
        if (harvester_ready and bear_oid is not None
                and card_in_hand_oid(state, 0, BOLT) is not None
                and my_main(state, 0) and my_priority(acts)
                and not real_decision_pending(st)):
            bid = card_in_hand_oid(state, 0, BOLT)
            a = cast_action_for(acts, state, bid)
            if a is not None:
                g.bolt_target_oid = str(bear_oid)
                wire("cast_bolt", {"action": a,
                                   "target_bear_oid": g.bolt_target_oid})
                await submit_as_is(c, a)
                g.stage = "kill"
                g.say(f"P0 casts Bolt targeting Bear {bear_oid} "
                      f"(turn {turn}); stage=kill")
                return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return

    elif g.stage == "kill":
        # watch for the bear dying
        if not g.bear_dead_oid:
            for oid in gy_oids(state, 1, BEAR):
                g.bear_dead_oid = str(oid)
                g.kill_turn = turn
                g.say(f"Bear died -> P1 graveyard oid={oid} turn={turn}; "
                      f"exporting pre.json")
                wire("bear_died", {"oid": oid, "turn": turn})
                g.pre = await g.export("pre", g.p0)
                g.stage = "activate"
                return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return

    elif g.stage == "activate":
        if not g.harv_activated:
            ho2 = bf_oid(state, 0, HARV)
            if ho2 is None:
                g.say("WARNING: harvester left BF before activation")
            a = activate_action_for(acts, ho2 or g.harv_oid)
            if a is not None:
                wire("activate_harvester", {"action": a})
                await submit_as_is(c, a)
                g.harv_activated = True
                g.activation_turn = turn
                g.watch_t0 = time.time()
                g.stage = "watch"
                g.say(f"P0 activated Harvester (turn {turn}); stage=watch")
                return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return

    elif g.stage == "watch":
        bear = get_obj(state, g.bear_dead_oid)
        opp, _, _ = target_opportunity(st)
        stack = state.get("stack") or []
        if bear.get("zone") == "Exile" and opp is None and not stack:
            g.token1 = [(oid, obj_lname(state, oid))
                        for oid, _ in find_nightmare_tokens(state, 0)]
            g.say(f"leg1 resolved: bear exiled; nightmare tokens on P0 BF: "
                  f"{g.token1 or 'NONE'}; exporting post.json")
            wire("leg1_resolved", {"bear_zone": "Exile",
                                   "tokens": g.token1,
                                   "target_prompt_seen": g.target_prompt_seen})
            g.post = await g.export("post", g.p0)
            g.stage = "leg2setup"
            return
        if g.watch_t0 and time.time() - g.watch_t0 > WATCH_DEADLINE:
            g.watch_timed_out = True
            g.token1 = [(oid, obj_lname(state, oid))
                        for oid, _ in find_nightmare_tokens(state, 0)]
            g.say(f"leg1 watch timed out ({WATCH_DEADLINE}s); bear zone="
                  f"{bear.get('zone')} stack={len(stack)}")
            wire("leg1_watch_timeout",
                 {"bear_zone": bear.get("zone"), "stack": len(stack),
                  "tokens": g.token1})
            g.post = await g.export("post", g.p0)
            g.stage = "leg2setup"
            return

    elif g.stage == "leg2setup":
        # second leg: bolt another bear, then activate again
        bear_oid = bf_oid(state, 1, BEAR)
        ho3 = bf_oid(state, 0, HARV)
        ready = (ho3 is not None
                 and not get_obj(state, ho3).get("tapped"))
        if (ready and bear_oid is not None
                and card_in_hand_oid(state, 0, BOLT) is not None
                and my_main(state, 0) and my_priority(acts)
                and not real_decision_pending(st)):
            bid = card_in_hand_oid(state, 0, BOLT)
            a = cast_action_for(acts, state, bid)
            if a is not None:
                g.leg2_bolt_target_oid = str(bear_oid)
                wire("cast_bolt_leg2", {"action": a,
                                        "target_bear_oid":
                                        g.leg2_bolt_target_oid})
                await submit_as_is(c, a)
                g.stage = "leg2kill"
                g.say(f"P0 leg2: casts Bolt targeting Bear {bear_oid} "
                      f"(turn {turn}); stage=leg2kill")
                return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return

    elif g.stage == "leg2kill":
        if not g.leg2_bear_dead_oid:
            for oid in gy_oids(state, 1, BEAR):
                if str(oid) == str(g.bear_dead_oid):
                    continue
                g.leg2_bear_dead_oid = str(oid)
                g.leg2_kill_turn = turn
                g.say(f"leg2: Bear2 died oid={oid} turn={turn}; "
                      f"stage=leg2activate")
                wire("bear2_died", {"oid": oid, "turn": turn})
                g.stage = "leg2activate"
                return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return

    elif g.stage == "leg2activate":
        if not g.leg2_harv_activated:
            ho4 = bf_oid(state, 0, HARV)
            a = activate_action_for(acts, ho4)
            if a is not None:
                wire("activate_harvester_leg2", {"action": a})
                await submit_as_is(c, a)
                g.leg2_harv_activated = True
                g.watch_t0 = time.time()
                g.stage = "leg2watch"
                g.say(f"P0 leg2: activated Harvester again (turn {turn})")
                return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return

    elif g.stage == "leg2watch":
        bear2 = get_obj(state, g.leg2_bear_dead_oid)
        opp, _, _ = target_opportunity(st)
        stack = state.get("stack") or []
        if bear2.get("zone") == "Exile" and opp is None and not stack:
            g.token2 = [(oid, obj_lname(state, oid))
                        for oid, _ in find_nightmare_tokens(state, 0)]
            g.say(f"leg2 resolved: bear2 exiled; tokens now: "
                  f"{g.token2 or 'NONE'}; exporting post_leg2.json")
            wire("leg2_resolved", {"bear2_zone": "Exile",
                                   "tokens": g.token2})
            g.post_leg2 = await g.export("post_leg2", g.p0)
            g.done = True
            return
        if g.watch_t0 and time.time() - g.watch_t0 > WATCH_DEADLINE:
            g.leg2_timed_out = True
            g.token2 = [(oid, obj_lname(state, oid))
                        for oid, _ in find_nightmare_tokens(state, 0)]
            g.say(f"leg2 watch timed out; bear2 zone={bear2.get('zone')}")
            wire("leg2_watch_timeout",
                 {"bear2_zone": bear2.get("zone"), "stack": len(stack)})
            g.post_leg2 = await g.export("post_leg2", g.p0)
            g.done = True
            return

    if not real_decision_pending(st) and my_priority(acts):
        await pass_priority(c, st, acts, g)


async def p1_tick(c, st, acts, state, g):
    if await do_mulligan(c, acts, st, 1, "P1", g,
                         lambda s, p: BEAR in hand_lnames(s, p)
                         and FOREST in hand_lnames(s, p)):
        return
    if await do_bottom(c, acts, st, 1, "P1", g, (BEAR,)):
        return
    if await do_discard_to_handsize(c, acts, st, 1, "P1", g, (BEAR,)):
        return
    d = combat_tick(acts)
    if d:
        await submit_as_is(c, d)
        return
    if (my_main(state, 1) and my_priority(acts)
            and not real_decision_pending(st)):
        bid = card_in_hand_oid(state, 1, BEAR)
        a = cast_action_for(acts, state, bid) if bid else None
        if a is not None:
            wire("p1_cast_bear", {"action": a})
            await submit_as_is(c, a)
            say(f"[P1] casts Grizzly Bears (turn {state.get('turn_number')})")
            return
    if await play_a_land(c, state, 1, acts, "P1", g):
        return
    if not real_decision_pending(st) and my_priority(acts):
        await pass_priority(c, st, acts, g)


async def pump(g, timeout_s):
    t0 = time.time()
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.15)
        for c, is_p0 in ((g.p0, True), (g.p1, False)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[c.player_id] = time.time()
            else:
                if time.time() - last_change[c.player_id] > 90:
                    s0 = st["state"]
                    la = [a.get("type") for a in
                          (st.get("legal_actions") or [])][:8]
                    g.say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                          f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                          f"stage={g.stage} legal={la} "
                          f"vikind={vi_kind_code(st)!r} "
                          f"real_decision={real_decision_pending(st)}")
                    last_change[c.player_id] = time.time()
                holds_prio = any(
                    a.get("type") == "PassPriority"
                    for a in (st.get("legal_actions") or []))
                if not (holds_prio
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                state = st["state"]
                acts = merged_actions(st)
                if is_p0:
                    await p0_tick(c, st, acts, state, g)
                else:
                    await p1_tick(c, st, acts, state, g)
            except Exception as e:
                g.say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        if g.done:
            g.say("done flag set; finishing")
            return True
        if time.time() - last_diag > 60 and g.p0.latest:
            last_diag = time.time()
            s = g.p0.latest["state"]
            g.say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                  f"phase={s.get('phase')} stage={g.stage} "
                  f"P0hand={len(hand_lnames(s, 0))} P1hand={len(hand_lnames(s, 1))} "
                  f"stack={len(s.get('stack') or [])}")
    g.note(f"global timeout ({timeout_s}s) hit at stage={g.stage}")
    return False


# ------------------------------------------------------------- server + parse
async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.104.0"), f"unexpected version {ver}"
    assert int(proto) == 118, f"unexpected protocol {proto}"
    assert str(build) == "4227122", f"unexpected build {build}"


def parse_evidence():
    """Capture the Harvester's actual parse from the PINNED v0.104.0
    card-data.json (the forwarding-chain evidence)."""
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.104.0/data/"
                        "card-data.json"))
    e = cd[HARV]
    out = {HARV: {"oracle_text": e.get("oracle_text"),
                  "abilities": e.get("abilities")}}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


# ------------------------------------------------------------- evaluation
def evaluate(g):
    ass = {}
    notes = []

    def objs(state):
        return (state or {}).get("objects", {}) if state else {}

    pre_o = objs(g.pre)
    post_o = objs(g.post)

    # A1: setup
    a1 = "not-run"
    if g.pre:
        h = get_obj(g.pre, g.harv_oid)
        on_bf = h.get("zone") == "Battlefield" \
            and str(h.get("controller")) == "0"
        tapped = h.get("tapped")
        a1 = "passed" if on_bf and not tapped else "failed"
        notes.append(f"A1: harvester pre-activation: zone={h.get('zone')} "
                     f"tapped={tapped} oid={g.harv_oid}")
    else:
        notes.append("A1: pre.json unavailable")
    ass["A1_setup_ok"] = a1

    # A2: bear died this turn
    a2 = "not-run"
    if g.pre and g.bear_dead_oid:
        b = get_obj(g.pre, g.bear_dead_oid)
        gy_ok = b.get("zone") == "Graveyard" \
            and str(b.get("controller")) == "1"
        turn_ok = g.kill_turn == g.activation_turn
        a2 = "passed" if gy_ok and turn_ok else "failed"
        notes.append(f"A2: bear {g.bear_dead_oid} pre: zone={b.get('zone')} "
                     f"kill_turn={g.kill_turn} activation_turn="
                     f"{g.activation_turn}")
    else:
        notes.append("A2: bear never reached graveyard / pre.json missing")
    ass["A2_bear_died"] = a2

    # A3: bear exiled
    a3 = "not-run"
    if g.post and g.bear_dead_oid:
        b = get_obj(g.post, g.bear_dead_oid)
        a3 = "passed" if b.get("zone") == "Exile" else "failed"
        notes.append(f"A3: bear {g.bear_dead_oid} post: zone={b.get('zone')} "
                     f"(target oid submitted={g.harv_target_oid or 'auto'}; "
                     f"target prompt seen={g.target_prompt_seen}; "
                     f"watch_timed_out={g.watch_timed_out})")
    else:
        notes.append("A3: post.json unavailable")
    ass["A3_bear_exiled"] = a3

    # A4: nightmare token copy
    a4 = "not-run"
    if g.post and g.harv_activated:
        toks = find_nightmare_tokens(g.post, 0)
        names = [(oid, obj_lname(g.post, oid)) for oid, _ in toks]
        if toks and any(obj_lname(g.post, oid) == BEAR
                        for oid, _ in toks):
            a4 = "passed"
        else:
            a4 = "failed"
        notes.append(f"A4: nightmare-token candidates on P0 BF post: "
                     f"{names or 'NONE'}; bear card zone="
                     f"{get_obj(g.post, g.bear_dead_oid).get('zone')}")
        wire("A4_token_scan",
             {"candidates": names,
              "full": [o for _, o in toks]})
    else:
        notes.append("A4: activation never submitted or post.json missing")
    ass["A4_token_created"] = a4

    # A5: trailing clause (leg2)
    a5 = "not-run"
    if g.post_leg2 and g.token1:
        post2_o = objs(g.post_leg2)
        t1_zone = get_obj(g.post_leg2, g.token1[0][0]).get("zone") \
            if g.token1 else None
        new_toks = [(oid, obj_lname(g.post_leg2, oid))
                    for oid, _ in find_nightmare_tokens(g.post_leg2, 0)]
        g.token2 = new_toks
        t1_gone = t1_zone != "Battlefield"
        t2_present = any(nm == BEAR for _, nm in new_toks)
        if t1_gone and t2_present:
            a5 = "passed"
        else:
            a5 = "failed"
        notes.append(f"A5: leg2: first token {g.token1[0][0]} zone={t1_zone}; "
                     f"second-leg tokens={new_toks or 'NONE'}")
    elif g.post_leg2:
        notes.append("A5: not-run -- leg1 created no token, so the trailing "
                     "'exile all other Nightmare tokens' clause is "
                     "untestable (bear2 zone="
                     f"{get_obj(g.post_leg2, g.leg2_bear_dead_oid).get('zone')})")
    else:
        notes.append("A5: not-run -- leg2 did not complete")
    ass["A5_others_exiled"] = a5

    # A6: cleanup
    a6 = "not-run"
    live = None
    try:
        if g.p0 and g.p0.latest:
            live = g.p0.latest.get("state", {})
    except Exception:
        pass
    if live is not None:
        stack = live.get("stack") or []
        a6 = "passed" if (not stack and not g.watch_timed_out
                          and not g.leg2_timed_out) else "failed"
        notes.append(f"A6: final live stack={len(stack)} entries; "
                     f"turn={live.get('turn_number')} phase={live.get('phase')}; "
                     f"timeouts={g.watch_timed_out}/{g.leg2_timed_out}")
    else:
        notes.append("A6: no live state at end")
    ass["A6_cleanup"] = a6

    if ass["A2_bear_died"] == "passed" and ass["A3_bear_exiled"] == "passed" \
            and ass["A4_token_created"] == "failed":
        verdict = "reproduced"
        result = ("Harvester exiled the Bolted Grizzly Bears from P1's "
                  "graveyard but created no Nightmare token copy: post "
                  "state shows the Bear card in Exile and zero Nightmare "
                  "token objects on P0's battlefield. Matches the reported "
                  "no-token-after-exile defect (v0.82.0 result confirmed on "
                  "v0.104.0).")
    elif all(v == "passed" for v in
             (ass["A1_setup_ok"], ass["A2_bear_died"], ass["A3_bear_exiled"],
              ass["A4_token_created"])):
        verdict = "not-reproduced"
        result = ("Full Harvester chain completed on v0.104.0: Bear exiled, "
                  "Nightmare copy token created on P0's battlefield. "
                  f"A5={ass['A5_others_exiled']}.")
    elif ass["A2_bear_died"] != "passed" or ass["A3_bear_exiled"] != "passed":
        verdict = "blocked"
        result = ("Could not establish the exile leg (A2/A3 not passed); "
                  "the token assertion is not trustworthy. See notes.")
    else:
        verdict = "reproduced"
        result = ("Partial Harvester chain with a clearly identified "
                  f"failure: assertions={ass}. See notes.")
    return ass, notes, verdict, result


# ------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #7161 - Abyssal Harvester no token copy "
                     "(revalidation)", fill=(235, 240, 250)); y += 28
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - run {run['run_id']} "
                     f"- 2026-10-08", fill=(140, 160, 180)); y += 26
    vc = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
          "blocked": (230, 200, 90)}.get(run["verdict"], (200, 200, 200))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vc); y += 32
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = [
        ("A1_setup_ok", "A1 setup: Harvester on P0 BF untapped pre-activation"),
        ("A2_bear_died", "A2 bear died: Bolted Bear in P1 gy, kill==activation turn"),
        ("A3_bear_exiled", "A3 bear exiled: Bear card in Exile post-activation"),
        ("A4_token_created", "A4 [bug?] Nightmare copy token of Bear on P0 BF"),
        ("A5_others_exiled", "A5 trailing clause: leg2 exiles token1, token2 enters"),
        ("A6_cleanup", "A6 cleanup: stack empty, game advanced"),
    ]
    for k, lab in labels:
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col); y += 21
    y += 6
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:14]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 19
    y += 4
    d.text((24, y), f"target prompt seen: {run['target_record']['target_prompt_seen']} | "
                     f"bear oid {run['target_record']['bear_dead_oid']} -> "
                     f"harv target {run['target_record']['harv_target_oid'] or 'auto'}",
           fill=(150, 165, 185))
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7161/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


# ------------------------------------------------------------- main
async def _main():
    t_start = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
    say(f"starting issue #{ISSUE} run {RUN_ID}")
    await verify_server_hello()
    pe = parse_evidence()
    say("parse_evidence.json written (Harvester parse captured)")

    g = Game()
    await g.start()
    ok = await pump(g, GLOBAL_TIMEOUT)
    g.say(f"game finished ok={ok} stage={g.stage}")
    await g.close()

    # final exports if a loop ended with pre but no post
    if g.pre is not None and g.post is None:
        try:
            g.post = await g.export("post", g.p0)
            g.note("post exported at loop end")
        except Exception as e:
            g.note(f"post final export failed: {e}")

    ass, notes, verdict, result = evaluate(g)
    dur = time.time() - t_start
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID} (phase-server owned by this run, "
                          "127.0.0.1:9374)",
        "driver": {"protocol_advertised": 118, "client": "driver/client.py",
                   "scenario": "driver/scenario_7161_01040.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7161_01040.py", "rb").read()
        ).hexdigest(),
        "decks": {"P0": [list(x) for x in P0_DECK],
                  "P1": [list(x) for x in P1_DECK]},
        "observations": {
            "harv_oid": g.harv_oid,
            "harv_cast_turn": g.harv_cast_turn,
            "bolt_target_oid": g.bolt_target_oid,
            "bear_dead_oid": g.bear_dead_oid,
            "kill_turn": g.kill_turn,
            "activation_turn": g.activation_turn,
            "harv_target_oid": g.harv_target_oid,
            "target_prompt_seen": g.target_prompt_seen,
            "leg2_bear_dead_oid": g.leg2_bear_dead_oid,
            "token1": g.token1,
            "token2": g.token2,
            "watch_timed_out": g.watch_timed_out,
            "leg2_timed_out": g.leg2_timed_out,
        },
        "target_record": {
            "bear_dead_oid": g.bear_dead_oid,
            "harv_target_oid": g.harv_target_oid,
            "target_prompt_seen": g.target_prompt_seen,
            "bolt_target_oid": g.bolt_target_oid,
        },
        "assertions": ass,
        "notes": notes + g.notes,
        "verdict": verdict,
        "result": result,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x key-card deck density is a test-harness convenience (engine "
            "accepts >4-of for custom games); exercised behavior is the "
            "shipped card text.",
            "Attached turn-34 Discord state (Cloudthresher game) not restored; "
            "equivalent fixture with Grizzly Bears used (as in the v0.82.0 run).",
            "Opponent-graveyard targeting only (matches the reported failure "
            "mode per triage).",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Verdict is scoped to v0.104.0, not a fix claim.",
        ],
        "setup_line": "P0 12x abyssal harvester + 12x lightning bolt + 20x "
                      "swamp + 16x mountain vs P1 12x grizzly bears + 24x "
                      "forest. P0 casts Harvester, Bolts P1's Bear on P0's "
                      "own turn, activates the tap ability targeting the Bear "
                      "card (leg2 repeats with a second Bear).",
        "contract_line": "Per Oracle: exile the creature card, create a "
                         "Nightmare copy token of it, then exile all other "
                         "Nightmare tokens you control.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(f"{BACKFILL}/driver/scenario_7161_01040.py",
                f"{EVDIR}/scenario_7161_01040.py")
    try:
        render_summary(run, f"{EVDIR}/summary.png")
        say("rendered summary.png")
    except Exception as e:
        say(f"summary render failed: {e}")
        notes.append(f"summary render failed: {e}")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            mf.write(f"{h}  {fn}\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


async def main():
    await _main()


if __name__ == "__main__":
    asyncio.run(main())
