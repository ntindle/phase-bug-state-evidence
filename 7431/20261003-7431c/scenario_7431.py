#!/usr/bin/env python3
"""Issue #7431: Mist of Stagnation -- "chooses a permanent for each card in
their graveyard" is unparsed, so the Untap sub-ability reads an empty
tracked set.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, mechanic:triggers, classifier:unsupported-aspect,
priority:p3-card-specific; related #6857 tracked-set census):
Mist of Stagnation reads:
> Permanents don't untap during their controllers' untap steps.
> At the beginning of each player's upkeep, that player chooses a
> permanent for each card in their graveyard, then untaps those permanents.
The parser emits an `Effect::Unimplemented` node as the chain head (root)
for "choose a permanent for each card in their graveyard"; its sub_ability
is the anaphor -- "then untaps those permanents" -- a `SetTapState`
(Untap) targeting the chain tracked set (`TrackedSet(0)`). The
Unimplemented resolver is a no-op (pushes no GameEvent), so the publish
authority allocates a fresh EMPTY tracked set; SetTapState is a
filter-only consumer (reads the tracked set directly, never
ability.targets), so the untap no-ops. The report asserts the parse state
and its direct structural consequence; no runtime reproduction was run.

Pinned v0.100.0 parse (see data_evidence.json): triggers[0] (Phase/Upkeep):
  execute.kind = Spell,
  head = Unimplemented { name: "unparsed_verb_arguments",
                         description: "choose a permanent for each card in
                                       their graveyard" }
    sub_ability (Spell): SetTapState { target: TrackedSet(0),
                                       scope: Single, state: Untap }
  static_abilities[0]: CantUntap { affected: Typed(Permanent) } --
  the "don't untap" clause parsed fine.
The issue's corpus (9b7c66e30) named the head 'choose'; the pinned corpus
names it 'unparsed_verb_arguments' with the identical description -- both
are Effect::Unimplemented over the same clause; the structural claim is
unchanged.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)

Driver notes (v0.100.0, protocol 101):
  - mana_needs is PER-SEAT (7431 fix): the first attempt shared one global
    dict, so P0 tapped its lands for P1's Grizzly Bears (19 cross-seat taps,
    P1 tapped nothing) and P1 never discarded -- the window could never arm.
    P1 now taps its own lands.
  - P1 casts at most 3 bears: enough tapped lands for the window, then the
    hand clogs and cleanup discards fill the graveyard.
  - P1 stops playing lands at 6 on the battlefield (7431 fix 2): attempt 2
    played 1 land/turn against 1 draw/turn, so the hand sat at ~7 forever,
    no discards ever happened, the window never armed, and P0 decked
    itself on turn ~50 (P1 won by game_rules).
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Mist of Stagnation + 24x Island + 32x Forest (60)
  P1: 8x Grizzly Bears + 24x Forest + 28x Island (60; plays lands/bears,
      never attacks)
Drive:
  1. Mulligans: both seats keep 7.
  2. Build: both play a land per turn; P0 casts Mist of Stagnation
     ({3}{U}{U}, tap-to-pool vi mana payment) once 2U+3 mana is
     available; P1 casts Grizzly Bears ({1}{G}) when affordable so its
     lands are tapped. Cleanup discards fill P1's graveyard.
  3. Arm: at the first P1 Upkeep with Mist of Stagnation on P0's
     battlefield, the upkeep trigger on the stack, >=1 card in P1's
     graveyard and >=1 tapped P1 permanent: export pre.json INSIDE the
     detection path (guarded flag) -- pre records graveyard count N and
     the tapped-permanent population.
  4. Resolution window: record every viewer-interaction opportunity
     offered to ANY seat from the trigger firing through resolution;
     answer a genuine permanent-choice prompt if one appears (defensive:
     choose tapped permanents first, up to N); otherwise pass priority.
  5. Export post.json once settled (stack empty, Priority, 8s idle).

Expected (correct behavior): the upkeep trigger prompts P1 to choose one
permanent for each of the N cards in their graveyard, then untaps those
permanents -- exactly N permanents go tapped->untapped in the window.
Reported (bug): the antecedent clause never parsed (Unimplemented head,
a runtime no-op), so no choice prompt is offered and the SetTapState
sub-ability reads an empty tracked set -- 0 permanents untap.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Mist of
                  Stagnation as the issue reports: triggers[0]
                  Phase/Upkeep, head Unimplemented naming the
                  "choose a permanent for each card in their graveyard"
                  clause; sub SetTapState target TrackedSet(0) state
                  Untap; static CantUntap for Permanents present.
  A2_setup_ok     pre.json: Mist of Stagnation on P0's battlefield;
                  P1 graveyard count N >= 1; >= 1 tapped P1 permanent;
                  phase Upkeep, active player P1.
  A3_trigger_fired the upkeep trigger was observed on the stack at P1's
                  upkeep (stack entry naming the trigger/choice clause).
  A4_choice_prompt a genuine choice opportunity for the head clause
                  ("choose a permanent ...") was advertised to the
                  choosing player. EXPECTED TO FAIL under the bug
                  (no such prompt exists: the head is unparsed). A mere
                  prompt appearing is not a pass -- the prompt must be
                  choice-shaped (select-schema with permanent candidates
                  or exactChoices naming permanent choice); see the 7422
                  lesson.
  A5_untap        count of P1 permanents tapped at pre and untapped at
                  post. Passes iff > 0 (correct behavior untaps exactly
                  the N chosen). EXPECTED TO FAIL (0) under the bug --
                  the empty-tracked-set no-op the issue predicts.
  A6_cleanup      post stack empty, game advanced past the trigger turn.

Verdict rule: reproduced iff A1, A2, A3 passed and A5 failed (0 untaps);
              not-reproduced iff A1..A5 all passed;
              blocked iff A1, A2, or A3 could not be established.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-7431c"
ISSUE = 7431
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
            if f not in ("scenario_run.log", "driver_stdout.log")]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.100.0",
    "build_commit": "bc9ef56",
    "protocol_version": 101,
    "server_binary_sha256": "261550905a3d569731c9bd66b2b12a0fa878400fefaa9cc36f3ad4e1a3d8adda",
    "card_data_sha256": "57e086e700ee0bd81002327e89d339356c4bb6c26d9e1ca9c010474c0b8c291c",
    "draft_pools_sha256": "961c5397d834ca92b2168035be386844339573024df72dbc78370b934ed75770",
    "signature_verified": True,
    "server_run_id": "backfill-owned v0.100.0 server on 127.0.0.1:9374 "
                     "(shared process; ServerHello 0.100.0/bc9ef56/"
                     "protocol 101 re-verified by this run's own Hello "
                     "handshake)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned "
              "release dir (GitHub /releases re-confirmed v0.100.0 still "
              "latest stable at 01:45 CDT 2026-10-03); hashes recomputed "
              "against on-disk artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

MIST = "mist of stagnation"
BEAR = "grizzly bears"
ISLAND, FOREST = "island", "forest"
BASICS = {"swamp", "mountain", "forest", "island", "plains"}

P0_DECK = [("Mist of Stagnation", 4), ("Island", 24), ("Forest", 32)]
P1_DECK = [("Grizzly Bears", 8), ("Forest", 24), ("Island", 28)]


def deck(pairs):
    names = []
    for n, c in pairs:
        names += [n] * c
    return {"main_deck": names, "sideboard": [], "commander": []}


SETUP_DEADLINE_S = 2400
SETTLE_IDLE_S = 8


def reset_attempt():
    global ST, MULLS, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "build",  # build -> resolving -> done
        "mist_cast": False,
        "mist_cast_turn": None,
        "mist_cast_oid": None,
        "mist_on_bf": False,
        "trigger_fired": False,
        "trigger_resolved": False,
        "trigger_turn": None,
        "pre_exported": False,
        "post_exported": False,
        "gy_n_at_pre": None,
        "tapped_at_pre": None,
        "untapped_window": None,
        "bears_cast_p1": 0,
        "mana_needs": {"P0": {"U": 0, "generic": 0},
                       "P1": {"G": 0, "generic": 0}},
        "prompts_seen": [],
        "choice_answered": False,
        "wf_types_window": [],
        "terminal": False,
        "states_seen": 0,
        "cast_rejections": 0,
        "stack_dumped": False,
        "settle_at": None,
        "last_rev": -1,
        "last_rev_at": 0,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_trigger_fired", "A4_choice_prompt",
                                       "A5_untap", "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
        "discard_iids": set(),
    }
    MULLS = set()
    SUBMITTED_OPPS = set()
    _DISCARD_REV = {}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE.closed:
        return
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()

# ------------------------------------------------------------- state utils
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def is_creature(o):
    return "creature" in [str(t).lower()
                          for t in (o.get("card_types") or {}).get("core_types", [])]


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def tapped_permanents(state, pid):
    return [oid for oid in bf_permanents(state, pid)
            if get_obj(state, oid).get("tapped")]


def gy_count(state, pid):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == "Graveyard" and o.get("owner") == pid)


def mist_on_bf(state):
    return any(obj_lname(state, oid) == MIST for oid in bf_permanents(state, 0))


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and is_land(o)]


def mana_color_pool(state, pid):
    """Untapped basic lands by color name for pid."""
    pool = {"B": 0, "R": 0, "G": 0, "U": 0, "W": 0}
    for oid in untapped_lands(state, pid):
        nm = obj_lname(state, oid)
        if nm == "swamp":
            pool["B"] += 1
        elif nm == "mountain":
            pool["R"] += 1
        elif nm == "forest":
            pool["G"] += 1
        elif nm == "island":
            pool["U"] += 1
        elif nm == "plains":
            pool["W"] += 1
    return pool


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def stack_entries(state):
    return state.get("stack") or []


def mist_trigger_on_stack(state):
    """Match the Mist of Stagnation upkeep trigger on the stack."""
    for se in stack_entries(state):
        blob = json.dumps(se, default=str).lower()
        if "choose a permanent" in blob or "mist of stagnation" in blob:
            return se
    return None


def dump_stack_once(state, why):
    if ST.get("stack_dumped"):
        return
    ST["stack_dumped"] = True
    with open(f"{EVDIR}/stack_dump.json", "w") as f:
        json.dump({"why": why, "stack": stack_entries(state)}, f, indent=1,
                  default=str)
    say(f"[window] stack dump written ({why}); "
        f"{len(stack_entries(state))} entries")
    wire("stack_dump", {"why": why,
                        "n": len(stack_entries(state))})

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get(MIST, {})
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_type": card.get("card_type"),
        "mana_cost": card.get("mana_cost"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "triggers": card.get("triggers"),
        "static_abilities": card.get("static_abilities"),
        "report_corpus_note": ("the issue's corpus (9b7c66e30) names the "
                               "Unimplemented head 'choose'; the pinned "
                               "v0.100.0 corpus names it "
                               "'unparsed_verb_arguments' with the identical "
                               "description -- both are Effect::Unimplemented "
                               "over the same clause; the structural claim "
                               "is unchanged."),
    }
    ok = False
    trigs = card.get("triggers") or []
    if trigs:
        t = trigs[0]
        exe = t.get("execute") or {}
        head = exe.get("effect") or {}
        sub = exe.get("sub_ability") or {}
        sub_eff = sub.get("effect") or {}
        statics = card.get("static_abilities") or []
        cant_untap = any(s.get("mode") == "CantUntap" for s in statics)
        ok = (t.get("phase") == "Upkeep"
              and exe.get("kind") == "Spell"
              and head.get("type") == "Unimplemented"
              and "choose a permanent for each card in their graveyard" in
              str(head.get("description", "")).lower()
              and sub.get("kind") == "Spell"
              and sub_eff.get("type") == "SetTapState"
              and (sub_eff.get("target") or {}).get("type") == "TrackedSet"
              and (sub_eff.get("target") or {}).get("id") == 0
              and (sub_eff.get("state") or {}).get("type") == "Untap"
              and cant_untap
              and (card.get("mana_cost") or {}).get("generic") == 3
              and set((card.get("mana_cost") or {}).get("shards", []))
              == {"Blue"})
        say(f"data-level check: trigger phase={t.get('phase')}; head="
            f"{head.get('type')}/{head.get('name')}/"
            f"{head.get('description')!r}; sub={sub_eff.get('type')}/"
            f"target={sub_eff.get('target')}/state={sub_eff.get('state')}; "
            f"CantUntap static={cant_untap}; "
            f"mana={json.dumps(card.get('mana_cost'))}")
    else:
        say("data-level check: NO triggers found in pinned card data")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok})

# ------------------------------------------------------------- actions
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


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    MULLS.add(tag)
    say(f"[{tag}] keep 7")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_discard_to_handsize(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    pend = pending_for(state, pid)
    if pend is None:
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    # never discard the combo pieces first: basics, then bears, then MoS
    order = sorted(hand, key=lambda o: (
        0 if obj_lname(state, o) in BASICS
        else (1 if obj_lname(state, o) == BEAR else 2), o))
    picks = [int(x) for x in order[:n]]
    say(f"[{tag}] discarding {n} to hand size")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def do_discard_vi(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    vi = get_vi(st)
    if not vi:
        return False
    hand_oids = set(hand_ids(state, pid))
    if not hand_oids:
        return False
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        chs = (resp.get("data") or {}).get("candidates") or []
        if not chs:
            continue
        cand_oids = set()
        all_hand = True
        for ch in chs:
            for sf in (ch.get("surfaces") or []):
                if sf.get("type") == "object":
                    d = sf.get("data") or {}
                    if d.get("zone") != "hand":
                        all_hand = False
                        break
                    try:
                        cand_oids.add(int(d.get("reference")))
                    except (TypeError, ValueError):
                        pass
            if not all_hand:
                break
        if not all_hand or not cand_oids or not cand_oids <= hand_oids:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick = chs[0].get("id")
        SUBMITTED_OPPS.add(iid)
        ST["discard_iids"].add(iid)
        say(f"[{tag}] vi discard-to-hand-size: {pick}")
        wire("vi_discard", {"who": tag, "iid": iid, "pick": pick})
        await interact_as(c, {
            "interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [pick]}}}, tag)
        return True
    return False


async def pay_mana_vi(c, st, state, tag):
    """Answer vi mana-payment choices during casting (7423 pattern)."""
    vi = get_vi(st)
    if not vi:
        return False
    needs = ST["mana_needs"][tag]
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        rtype = (opp.get("response", {}) or {}).get("type")
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, syms, used = None, [], None
        if sum(needs.values()) > 0 and taps:
            for ch, s in taps:  # colored needs first
                for color in ("B", "G", "U", "W", "R"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, syms, used = ch, s, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, syms, used = taps[0][0], taps[0][1], "generic"
            if pick is None:
                continue  # no choice makes progress; do not tap blindly
            needs[used] -= 1
            wire("tap_land", {"iid": iid, "choice": pick["id"],
                              "symbols": syms, "used_for": used,
                              "needs_now": dict(needs)})
            say(f"[{tag}] tap land for mana: {pick['id'][:24]} "
                f"symbols={syms} used_for={used} needs={dict(needs)}")
        else:
            continue
        SUBMITTED_OPPS.add(iid)
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick["id"]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [pick["id"]]}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pay_tick(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "passPriority" in codes and ch.get("status", {}) \
                        .get("type") == "available":
                    iid = opp.get("interactionId") or opp.get("id")
                    await interact_as(c, {
                        "interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}},
                        tag)
                    return True
    return False


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


def record_window_prompt(seat, st):
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if all(t[2] != iid for t in ST["prompts_seen"]):
            ST["prompts_seen"].append((ST["phase"], seat, iid, opp))
            wire("window_prompt", {"phase": ST["phase"], "seat": seat,
                                   "iid": iid, "opportunity": opp})
            say(f"[{ST['phase']}/{seat}] prompt: "
                f"{json.dumps(opp)[:260]}")


def is_permanent_choice_opp(state, opp, n):
    """True iff this opportunity is a choice-shaped permanent selection
    (the head clause's missing choice) -- NOT a mere prompt appearing.
    Per the 7422 lesson: a prompt appearing is not a pass."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    data = resp.get("data") or {}
    blob = json.dumps(opp).lower()
    if rtype == "schema":
        spec = data.get("spec") or {}
        if spec.get("type") != "select":
            return False
        cands = data.get("candidates") or []
        if not cands:
            return False
        # candidates must reference battlefield permanents
        n_perm = 0
        for ch in cands:
            for sf in (ch.get("surfaces") or []):
                if sf.get("type") == "object":
                    d = sf.get("data") or {}
                    if d.get("zone") == "Battlefield":
                        n_perm += 1
                        break
        return n_perm > 0 and ("permanent" in blob or "choose" in blob)
    if rtype == "exactChoices":
        choices = data.get("choices") or []
        return bool(choices) and "permanent" in blob and "choose" in blob
    return False


async def answer_mist_choice(c, pid, tag):
    """Defensive: if the engine DOES offer the head-clause permanent
    choice, answer it (tapped permanents first, up to N). Under the bug
    this handler never fires."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    vi = get_vi(st)
    if not vi:
        return False
    n = ST.get("gy_n_at_pre") or 0
    if n <= 0:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if not is_permanent_choice_opp(state, opp, n):
            continue
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        cands = data.get("candidates") or []
        tapped = set(tapped_permanents(state, pid))
        perm_cands = []
        for ch in cands:
            ref = None
            for sf in (ch.get("surfaces") or []):
                if sf.get("type") == "object":
                    d = sf.get("data") or {}
                    if d.get("zone") == "Battlefield":
                        try:
                            ref = int(d.get("reference"))
                        except (TypeError, ValueError):
                            ref = None
            if ref is not None:
                perm_cands.append((ch.get("id"), ref))
        perm_cands.sort(key=lambda t: (0 if t[1] in tapped else 1, t[1]))
        picks = [cid for cid, _ in perm_cands[:max(n, 1)]]
        if not picks:
            continue
        SUBMITTED_OPPS.add(iid)
        ST["choice_answered"] = True
        say(f"[{tag}] answering permanent-choice with {len(picks)} picks "
            f"(n={n}): {[p[:16] for p in picks]}")
        wire("mist_choice_answer", {"who": tag, "iid": iid,
                                    "picks": picks, "n": n})
        if resp.get("type") == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": picks[0]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "select",
                                "data": {"choiceIds": picks}}}
        await interact_as(c, sub, tag)
        return True
    return False

# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P0", st)
        if await answer_mist_choice(c, pid, "P0"):
            return
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P0"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P0"):
        return
    if await do_discard_vi(c, pid, "P0"):
        return
    if sum(ST["mana_needs"]["P0"].values()) > 0:
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await pay_tick(c, acts, "P0"):
            return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, pid):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid \
            and not stack_entries(state) and ST["phase"] == "build":
        hn = hand_lnames(state, pid)
        # Cast Mist of Stagnation once 2U+3 mana is available.
        if not ST["mist_cast"] and MIST in hn:
            pool = mana_color_pool(state, pid)
            lands = len(untapped_lands(state, pid))
            if pool["U"] >= 2 and lands >= 5:
                a, oid = cast_action_for(acts, state, MIST)
                if a:
                    ST["mist_cast"] = True
                    ST["mist_cast_oid"] = oid
                    ST["mist_cast_turn"] = state.get("turn_number")
                    ST["mana_needs"]["P0"] = {"U": 2, "generic": 3}
                    say(f"[P0] casting Mist of Stagnation (oid {oid})")
                    wire("mist_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick (build only, never attack)
async def p1_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P1", st)
        if await answer_mist_choice(c, pid, "P1"):
            return
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P1"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P1"):
        return
    if await do_discard_vi(c, pid, "P1"):
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, pid):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid \
            and not stack_entries(state) and ST["phase"] == "build":
        hn = hand_lnames(state, pid)
        if (BEAR in hn and len(untapped_lands(state, pid)) >= 2
                and ST["bears_cast_p1"] < 3):
            a, oid = cast_action_for(acts, state, BEAR)
            if a:
                ST["bears_cast_p1"] += 1
                ST["mana_needs"]["P1"] = {"G": 1, "generic": 1}
                say(f"[P1] casting Grizzly Bears (oid {oid})")
                wire("bear_cast", {"oid": oid, "who": "P1"})
                await submit_as_is(c, a)
                return
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 6:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- finalize
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]

    def load_env(name):
        try:
            return json.load(open(f"{EVDIR}/{name}.json"))
        except Exception as e:
            notes.append(f"{name}.json load failed: {e}")
            return None

    pre = load_env("pre") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    post_st = post.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Mist of Stagnation as the issue reports -- "
                     "triggers[0] Phase/Upkeep, head Unimplemented "
                     "('unparsed_verb_arguments': 'choose a permanent for "
                     "each card in their graveyard') + sub Spell "
                     "SetTapState { target: TrackedSet(0), scope: Single, "
                     "state: Untap }; CantUntap static for Permanents "
                     "present; mana {3}{U}{U}; see data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        mon = mist_on_bf(pre_st)
        n = gy_count(pre_st, 1)
        tapped = tapped_permanents(pre_st, 1)
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        notes.append(f"A2 probe: pre MoS on P0 BF={mon}; P1 gy={n}; "
                     f"P1 tapped permanents={len(tapped)}; phase={ph}; "
                     f"active={act}; P1 life={[p.get('life') for p in pre_st.get('players', [])]}.")
        if mon and n >= 1 and len(tapped) >= 1 and ph == "Upkeep" \
                and act == 1:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: Mist of Stagnation on P0's battlefield "
                         f"at P1's upkeep with {n} card(s) in P1's "
                         f"graveyard and {len(tapped)} tapped P1 "
                         "permanent(s).")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre does not show the "
                         "required setup.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the upkeep "
                     "trigger window never armed).")

    # A3: the upkeep trigger fired
    if ST["trigger_fired"]:
        ass["A3_trigger_fired"] = "passed"
        notes.append("A3 passed: the Mist of Stagnation upkeep trigger was "
                     "observed on the stack at P1's upkeep (see "
                     "stack_dump.json); trigger resolved="
                     f"{ST.get('trigger_resolved')}.")
    else:
        ass["A3_trigger_fired"] = "failed"
        notes.append("A3 FAILED: the upkeep trigger was never observed on "
                     "the stack.")

    # A4: was a genuine permanent-choice prompt offered?
    # (choice-shaped only -- a prompt appearing is not a pass, 7422 lesson)
    choice_iids = []
    for p in ST["prompts_seen"]:
        opp = p[3]
        try:
            if is_permanent_choice_opp(pre_st, opp, ST.get("gy_n_at_pre") or 1):
                choice_iids.append(p[2])
        except Exception as e:
            notes.append(f"A4 classifier error on prompt {p[2]}: {e}")
    notes.append(f"A4 probe: {len(ST['prompts_seen'])} total prompts in "
                 f"window; choice-shaped permanent prompts="
                 f"{choice_iids or 'none'}; wf types in window="
                 f"{ST['wf_types_window']}; choice_answered="
                 f"{ST.get('choice_answered')}.")
    if choice_iids:
        ass["A4_choice_prompt"] = "passed"
        notes.append("A4 passed: a choice-shaped permanent-selection "
                     "prompt was advertised for the head clause.")
    elif ST["trigger_fired"] and ST["trigger_resolved"]:
        ass["A4_choice_prompt"] = "failed"
        notes.append("A4 FAILED: the trigger fired and resolved but NO "
                     "choice-shaped permanent-selection prompt was ever "
                     "offered -- THE REPORTED BUG: the 'choose a permanent "
                     "for each card in their graveyard' clause never "
                     "parsed (Unimplemented head, a runtime no-op), so "
                     "there is no choice to offer.")
    else:
        notes.append("A4 not-run: the trigger window never completed.")

    # A5: did any permanents untap in the window?
    unt = ST.get("untapped_window")
    n = ST.get("gy_n_at_pre")
    tapped_n = ST.get("tapped_at_pre")
    notes.append(f"A5 probe: P1 gy at pre n={n}; tapped P1 permanents at "
                 f"pre={tapped_n}; tapped->untapped in window={unt}.")
    if unt is not None and ST.get("trigger_resolved"):
        if unt > 0:
            ass["A5_untap"] = "passed"
            notes.append(f"A5 passed: {unt} P1 permanent(s) untapped "
                         "during the trigger window.")
        else:
            ass["A5_untap"] = "failed"
            notes.append("A5 FAILED: 0 P1 permanents untapped during the "
                         "trigger window -- THE REPORTED BUG: the "
                         "Unimplemented head pushed no GameEvent, the "
                         "publish authority allocated an empty tracked "
                         "set, and the filter-only SetTapState consumer "
                         "no-op'd on it. 'Then untaps those permanents' "
                         "untapped nothing.")
    else:
        notes.append("A5 not-run: the resolution window never completed.")

    # A6: cleanup
    if post_st:
        tnum = ST.get("trigger_turn")
        cur_turn = post_st.get("turn_number")
        if not stack_entries(post_st) and (
                tnum is None or (cur_turn or 0) > tnum):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the trigger turn (trigger turn {tnum}, post "
                         f"turn {cur_turn}, phase {post_st.get('phase')}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty or the game did "
                         "not advance past the trigger turn.")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_trigger_fired"] == "passed"
            and ass["A5_untap"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the Mist of Stagnation upkeep trigger "
            f"fired at P1's upkeep with {n} card(s) in P1's graveyard and "
            f"{tapped_n} tapped P1 permanent(s), and resolved -- but no "
            "choice prompt was offered and 0 permanents untapped. The "
            "'choose a permanent for each card in their graveyard' clause "
            "never parsed (Unimplemented head, a runtime no-op), so the "
            "chain tracked set was allocated empty and the filter-only "
            "SetTapState consumer no-op'd -- the exact structural defect "
            "the issue reports. Confirmed on v0.100.0. This is not a fix "
            "claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_trigger_fired",
                                          "A4_choice_prompt", "A5_untap")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the upkeep trigger prompted the "
            "permanent choice and untapped the chosen permanents. This is "
            "not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported trigger path could not "
                     "be fully exercised; see assertion notes.")

    # server log excerpts for this game (newest-first by mtime)
    import glob
    lines = []
    used = None
    for lp in sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"),
                     key=os.path.getmtime, reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "mist of stagnation" in l.lower()
               or "unimplemented" in l.lower()
               or "tracked" in l.lower()
               or (ST["game_code"] and ST["game_code"] in l)]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 101, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7431.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "mist_cast": ST["mist_cast"],
            "mist_cast_turn": ST["mist_cast_turn"],
            "mist_cast_oid": ST["mist_cast_oid"],
            "trigger_fired": ST["trigger_fired"],
            "trigger_resolved": ST["trigger_resolved"],
            "trigger_turn": ST["trigger_turn"],
            "gy_n_at_pre": ST["gy_n_at_pre"],
            "tapped_at_pre": ST["tapped_at_pre"],
            "untapped_window": ST["untapped_window"],
            "bears_cast_p1": ST["bears_cast_p1"],
            "choice_answered": ST["choice_answered"],
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (Mist of Stagnation on the battlefield at a "
            "player's upkeep with cards in that player's graveyard and "
            "tapped permanents) and observes the resolution outcome. The "
            "consumer-side outcome was unmeasured in the report; this run "
            "measures it.",
            "4x Mist of Stagnation / 8x Grizzly Bears deck densities are "
            "test-harness conveniences (engine accepts >4-of for custom "
            "games).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Mist of Stagnation + 24x Island + 32x Forest; "
                      "P1: 8x Grizzly Bears + 24x Forest + 28x Island "
                      "(lands/bears, never attacks); default Bo1, life 20",
        "contract_line": "Mist of Stagnation on the battlefield at P1's "
                         "upkeep with N>=1 cards in P1's graveyard and "
                         "tapped P1 permanents: correct = P1 chooses one "
                         "permanent per graveyard card, then untaps those. "
                         "Observed (bug): the 'choose a permanent for each "
                         "card in their graveyard' clause never parsed "
                         "(Unimplemented head, runtime no-op), so no choice "
                         "is offered and the SetTapState sub-ability reads "
                         "an empty tracked set -- 0 permanents untap.",
        "prior_runs": [],
        "stats": {"states_seen": ST["states_seen"]},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    return verdict


# ------------------------------------------------------------- main loop
async def run_game():
    reset_attempt()
    check_data_level()
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    att = await p0.create(deck(P0_DECK), player_count=2)
    ST["game_code"] = att.get("game_code")
    await p1.join(ST["game_code"], deck(P1_DECK))
    say(f"game created: code={ST['game_code']}")
    wire("game_created", {"game_code": ST["game_code"]})

    async def pump(c, tag):
        while not ST["terminal"]:
            try:
                t, data = await asyncio.wait_for(c.inbox.get(), 2)
            except asyncio.TimeoutError:
                tick_timeout(c)
                continue
            st = c.latest
            if not st:
                continue
            state = st["state"]
            ST["states_seen"] += 1
            # arm the window: first P1 upkeep with MoS on BF, trigger on
            # the stack, >=1 gy card and >=1 tapped P1 permanent
            if ST["phase"] == "build" and mist_on_bf(state):
                if not ST["mist_on_bf"]:
       
                    ST["mist_on_bf"] = True
                    say("[build] Mist of Stagnation on P0 battlefield")
                    wire("mist_on_bf", {})
                if (state.get("phase") == "Upkeep"
                        and state.get("active_player") == 1):
                    se = mist_trigger_on_stack(state)
                    n = gy_count(state, 1)
                    tapped = tapped_permanents(state, 1)
                    if se is not None and n >= 1 and len(tapped) >= 1:
                        # export pre INSIDE the arming path (guarded)
                        await export_as(c0_ref[0], "pre")
                        ST["pre_exported"] = True
                        ST["gy_n_at_pre"] = n
                        ST["tapped_at_pre"] = tapped
                        ST["trigger_fired"] = True
                        ST["trigger_turn"] = state.get("turn_number")
                        ST["phase"] = "resolving"
                        say(f"[arm] pre.json exported at P1 upkeep: gy={n}, "
                            f"tapped P1 permanents={len(tapped)}, turn="
                            f"{ST['trigger_turn']}; trigger on stack")
                        wire("pre_exported", {"gy_n": n,
                                              "tapped_n": len(tapped)})
                        wire("trigger_fired", {})
                        dump_stack_once(state, "trigger_seen")
            # track the trigger on the stack
            if ST["phase"] == "resolving" and not ST["trigger_resolved"]:
                if mist_trigger_on_stack(state) is None and ST["trigger_fired"]:
                    ST["trigger_resolved"] = True
                    say("[window] Mist of Stagnation trigger resolved; "
                        "starting settle clock")
                    wire("trigger_resolved", {})
                    ST["settle_at"] = time.time()
            # watchdog: the window must not hang the run forever
            if ST["phase"] == "resolving" and not ST["trigger_resolved"]:
                if time.time() - ST["t0"] > SETUP_DEADLINE_S:
                    say("[watchdog] deadline in resolving without "
                        "resolution; exporting post and finalizing")
                    wire("watchdog", {"note": "resolving timeout"})
                    if not ST["post_exported"]:
                        await export_as(c0_ref[0], "post")
                        ST["post_exported"] = True
                        compute_untap()
                    ST["terminal"] = True
                    return
            if ST["phase"] == "resolving":
                wf = (wf_of(state).get("type") or "")
                if wf and wf not in ST["wf_types_window"]:
                    ST["wf_types_window"].append(wf)
                if (ST["trigger_resolved"] and not stack_entries(state)
                        and wf == "Priority"):
                    if ST["settle_at"] is None:
                        ST["settle_at"] = time.time()
                    elif time.time() - ST["settle_at"] >= SETTLE_IDLE_S:
                        say("[window] settle clock done; exporting post")
                        wire("post_export", {})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                            compute_untap()
                        ST["terminal"] = True
                        return
                elif not ST["trigger_resolved"]:
                    ST["settle_at"] = None
            if time.time() - ST["t0"] > SETUP_DEADLINE_S:
                say("[timeout] deadline reached")
                wire("timeout", {})
                ST["terminal"] = True
                return
            acts = merged_actions(st)
            if tag == "P0":
                await p0_tick(st, acts, state, c)
            else:
                await p1_tick(st, acts, state, c)

    def tick_timeout(c):
        # on idle ticks, nothing to do: the pump loop re-reads state
        pass

    def compute_untap():
        post = json.load(open(f"{EVDIR}/post.json")).get("state", {})
        tapped_pre = ST.get("tapped_at_pre") or []
        unt = 0
        unt_names = []
        for oid in tapped_pre:
            o = (post.get("objects") or {}).get(str(oid), {})
            if (o.get("zone") == "Battlefield"
                    and not o.get("tapped")):
                unt += 1
                unt_names.append(obj_lname(post, oid))
        ST["untapped_window"] = unt
        say(f"[untap] tapped at pre={len(tapped_pre)}; untapped in "
            f"window={unt} ({unt_names})")
        wire("untap", {"tapped_at_pre": len(tapped_pre),
                       "untapped_window": unt,
                       "untapped_names": unt_names})

    c0_ref = [p0]
    await asyncio.gather(pump(p0, "P0"), pump(p1, "P1"))
    for c in (p0, p1):
        try:
            await c.ws.close()
        except Exception:
            pass


async def main():
    try:
        await run_game()
    except Exception as e:
        say(f"FATAL: {e!r}")
        wire("fatal", {"error": repr(e)})
    verdict = await finalize(None)
    say(f"FINAL VERDICT: {verdict}")
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"]},
                     indent=1))


if __name__ == "__main__":
    asyncio.run(main())
