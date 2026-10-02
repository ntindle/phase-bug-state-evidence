#!/usr/bin/env python3
"""Issue #7369: Brass Infiniscope isn't triggering on X spells.

CLEAN RE-RUN (b): the first attempt (20261002-7369, game EMZSAL)
demonstrated the full bug line (Banefire X=2 cast and resolved, P1
20->18, P0 stayed 20, no payout) but the driver died before exporting
the cast/post states: its stack scan searched entry text for the card
name, which spell entries don't carry, so banefire_seen_on_stack never
tripped and the run never finalized. This revision resolves stack
entries through the object table and gates discards on prompt
ownership. The 20261002-7369 dir is kept as-is, not published.

Reported (discord 2026-08-13, sync-filed, status:confirmed,
area:engine/area:parser, classifier:supported-aspect-defect): "The mana
ability works, but nothing happens regarding card draw or life gain from
[[Brass Infiniscope]]'s other abilities."

Oracle text (verified from pinned v0.99.0 card-data.json):
  brass infiniscope: {4} Artifact
  {T}: Add {C}{C}. When you next cast a spell with {X} in its mana cost
  this turn, you draw a card and gain half X life, rounded down.

Pinned card-data state (v0.99.0, key 'brass infiniscope', verified this
run): the activated ability parses as Mana {C}x2 (is_mana_ability=true)
with a Spell sub_ability CreateDelayedTrigger whose condition is
WhenNextEvent -> trigger mode SpellCast, valid_card Typed with property
HasXInManaCost, valid_target Controller, execute null. The trigger's
effect is Draw 1 (target Controller) with a SequentialSibling sub_ability
GainLife amount DivideRounded(Ref Variable "X", 2, Down). Note: the
2026-08-15 triage (mike-theDude) read the then-current coverage data as
an UNQUALIFIED "when next event this turn" (event type and spell filter
not lowered); the pinned v0.99.0 data DOES carry SpellCast +
HasXInManaCost. The empirical test below decides whether the trigger
fires and pays out on the current pin.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Brass Infiniscope + 4x Banefire + 52x Mountain
  P1: 60x Island (plays a land, passes priority, never attacks)
Drive:
  P0 plays a Mountain each turn and casts Brass Infiniscope as soon as
  {4} is payable (turn 4). On a later main phase with Banefire in hand:
    1. export pre (scope untapped on BF, Banefire in hand, 20/20)
    2. activate the scope's {T}: Add {C}{C} (mana ability, no stack)
    3. export activated (pool check + delayed_triggers record)
    4. cast Banefire, announce X=2 ({2}{R} paid from {C}{C}+{R}), target P1
    5. export cast (spell on stack, X value, chosen target)
    6. pass priority; the delayed trigger should fire on the SpellCast
       event, go on the stack, and resolve: P0 draws a card and gains
       half of X=2 = 1 life (20 -> 21)
    7. Banefire resolves: P1 takes 2 (20 -> 18)
    8. export post; finalize
Expected (correct behavior): after the X spell is cast, P0 draws 1 card
and gains 1 life.
Reported (bug): no draw, no life gain.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok        pre exported: scope untapped on P0 BF, Banefire in P0
                     hand, both players at 20 life.
  A2_activation_ok   the {T} activation was accepted: scope tapped, P0 mana
                     pool gained >=2 colorless. (delayed_triggers content is
                     recorded as an observation, not asserted -- its engine
                     representation is logged raw.)
  A3_cast_ok         Banefire was cast with X=2 announced and P1 chosen as
                     the target (no rejections on the cast path).
  A4_trigger_pays    (THE REPORTED BUG) after the cast, the scope's delayed
                     trigger fired AND P0 drew a card AND P0 gained 1 life.
                     Sub-flags: trigger_seen_on_stack, draw_observed,
                     life_gain_observed.
  A5_spell_resolves  Banefire resolved: P1 life 20 -> 18.
  A6_cleanup         post stack empty, game advancing.

Verdict rule: reproduced iff A1..A3 passed and A4 failed -- the X spell
              was cast but no draw and/or no life gain followed, exactly
              the reported failure;
              not-reproduced iff A4 passed (draw + 1 life observed);
              blocked iff A1 failed (setup never assembled).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - ActivateAbility via advertised action (source_id/ability_index).
  - X announcement: schema/number opportunity ->
    {"type":"number","data":{"value":N}}.
  - Target selection via viewer_interaction: schema/sequence or
    exactChoices; player candidates carry surfaces[].data.seat.
  - Authoritative exports only from the host seat (P0 creates the game).
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-7369b"
ISSUE = 7369
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
# the shell redirect creates scenario_run.log before we start; ignore it
leftovers = [f for f in os.listdir(EVDIR) if f != "scenario_run.log"]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
    "server_run_id": "shared pinned v0.99.0 server on 127.0.0.1:9374 "
                     "(already listening per task body; ServerHello "
                     "0.99.0/d919616/protocol 98 verified this run; "
                     "not restarted by this run)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.99.0 == pinned release "
              "dir; hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))

SCOPE = "brass infiniscope"
BANEFIRE = "banefire"
MOUNTAIN = "mountain"
ISLAND = "island"
X_VALUE = 2  # Banefire X=2 -> {2}{R}; expected life gain = half of 2 = 1

P0_DECK = [("Brass Infiniscope", 4), ("Banefire", 4), ("Mountain", 52)]
P1_DECK = [("Island", 60)]

SETUP_DEADLINE_S = 1500
SETTLE_IDLE_S = 20

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()
SUBMITTED_OPPS = set()
_DISCARD_REV = {}


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> activating -> casting -> resolving -> done
        "scope_cast": False,
        "scope_oid": None,
        "activated": False,
        "activation_at": None,
        "cast_submitted": False,
        "x_value": None,
        "target_chosen": False,
        "target_candidate_id": None,
        "target_rejections": 0,
        "cast_rejections": 0,
        "trigger_seen": False,
        "trigger_seen_at": None,
        "delayed_triggers_at_activation": None,
        "pool_at_activation": None,
        "pre_exported": False,
        "activated_exported": False,
        "cast_exported": False,
        "trigger_exported": False,
        "post_exported": False,
        "settle_at": None,
        "states_seen": 0,
        "trigger_observations": 0,
        "pre_life": None,
        "pre_hand_n": None,
        "pre_drawn": None,
        "cast_life0": None,
        "cast_hand0": None,
        "cast_drawn0": None,
        "cast_life1": None,
        "post_life0": None,
        "post_life1": None,
        "post_hand0": None,
        "post_drawn0": None,
        "draw_observed": False,
        "life_gain_observed": False,
        "p1_damaged": False,
        "banefire_seen_on_stack": False,
        "banefire_stack_oid": None,
        "player_keys_logged": False,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_activation_ok",
                                       "A3_cast_ok", "A4_trigger_pays",
                                       "A5_spell_resolves", "A6_cleanup")},
        "notes": [],
    })


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
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


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower() == key]


def untapped_ids(state, pid, key):
    return [i for i in bf_ids(state, pid, key)
            if not get_obj(state, i).get("tapped")]


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and "land" in [str(t).lower()
                           for t in (o.get("card_types") or {}).get("core_types", [])]]


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


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


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def stack_entries(state):
    return state.get("stack") or []


def stack_mentions(state, needle):
    return [e for e in stack_entries(state)
            if needle in json.dumps(e, default=str).lower()]


def stack_spell_oid(state, key):
    """Find a stack entry whose resolved object is the named card.

    Spell stack entries often carry only an object id (no card name in
    the entry itself), so resolve through the object table. Returns the
    object id or None."""
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        for k in ("object_id", "id", "source", "source_id", "card_id"):
            v = e.get(k)
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == key:
                return iv
        # nested kind payloads
        for v in e.values():
            if isinstance(v, dict):
                for k in ("object_id", "id", "source", "source_id"):
                    try:
                        iv = int(v.get(k))
                    except (TypeError, ValueError):
                        continue
                    if obj_lname(state, iv) == key:
                        return iv
    return None


def stack_has_trigger_from(state, key):
    """True if any stack entry references the named source card (trigger
    entries usually name their source in the serialized entry)."""
    if stack_mentions(state, key):
        return True
    return stack_spell_oid(state, key) is not None


def colorless_in_pool(state, pid):
    """Tolerant count of colorless mana in the pool; logs the raw shape."""
    mp = player_of(state, pid).get("mana_pool") or {}
    entries = mp.get("mana") or []
    total = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        ser = json.dumps(e).lower()
        amt = e.get("amount", e.get("count", 1))
        try:
            amt = int(amt)
        except (TypeError, ValueError):
            amt = 1
        if "colorless" in ser:
            total += amt
    return total, entries


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


def choice_text(ch):
    bits = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data", {}) or {}
        for k in ("text", "label", "code", "name"):
            if d.get(k):
                bits.append(str(d[k]))
    if ch.get("text"):
        bits.append(str(ch["text"]))
    return " | ".join(bits)


# ------------------------------------------------------------- data check
def check_data_level():
    """A0 (informational): card data parses as the report/triage expect."""
    sc = CARD_DATA.get("brass infiniscope", {})
    bf = CARD_DATA.get("banefire", {})
    trig = None
    try:
        trig = sc["abilities"][0]["sub_ability"]["effect"]
    except (KeyError, IndexError, TypeError):
        trig = None
    findings = {
        "scope_name": sc.get("name"),
        "scope_mana_cost": sc.get("mana_cost"),
        "scope_oracle": sc.get("oracle_text"),
        "scope_mana_ability": (sc.get("abilities") or [{}])[0].get("is_mana_ability"),
        "scope_delayed_trigger_effect": trig,
        "banefire_cost": bf.get("mana_cost"),
        "banefire_oracle": bf.get("oracle_text"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    ok = (
        sc.get("mana_cost") == {"type": "Cost", "shards": [], "generic": 4}
        and (trig or {}).get("type") == "CreateDelayedTrigger"
        and bf.get("mana_cost") == {"type": "Cost", "shards": ["X", "Red"],
                                    "generic": 0}
    )
    say(f"data-level check: {'OK' if ok else 'MISMATCH'}")
    ST["notes"].append(f"data-level: card data parses as expected: {ok}")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def do_mulligan(c, pid, tag, want):
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
    hn = hand_lnames(state, pid)
    mkey = (tag, "mulligan")
    mulls = MULL_COUNT.get(mkey, 0)
    if want is None or want in hn or mulls >= 2 or len(hn) <= 5:
        MULLS.add(tag)
        say(f"[{tag}] keep {len(hn)} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep"})
    else:
        MULL_COUNT[mkey] = mulls + 1
        say(f"[{tag}] mulligan #{mulls + 1} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") not in ("MulliganDecision", "BottomCards"):
        return False
    pend = pending_for(state, pid)
    if not pend:
        return False
    phase = (pend.get("phase") or {}).get("type")
    if phase not in ("BottomCards", "Bottom"):
        return False
    key = (tag, "bottom", str((pend.get("phase") or {}).get("count")))
    if key in SUBMITTED:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    # keep scope/banefire first, bottom mountains
    rank = {SCOPE: 5, BANEFIRE: 4, MOUNTAIN: 0, ISLAND: 0}
    picks = sorted(hand, key=lambda o: rank.get(obj_lname(state, o), 2))[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


async def do_discard(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    # only answer our own discard prompt (a cross-player submission is
    # silently ignored and would loop forever)
    pend = pending_for(state, pid)
    if pend is None:
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    rank = {MOUNTAIN: 0, ISLAND: 0, BANEFIRE: 1, SCOPE: 5}
    picks = [int(x) for x in sorted(
        hand, key=lambda o: rank.get(obj_lname(state, o), 2))[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


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
                    await c.send_interaction({
                        "interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}})
                    return True
    return False


def seat1_candidate_id(opp):
    """Find the candidate id for the opponent player (seat 1)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        for sf in ch.get("surfaces", []) or []:
            dd = sf.get("data", {}) or {}
            if (dd.get("role") in ("candidate", "target")
                    and str(dd.get("seat")) == "1"):
                return ch.get("id")
    # fallback: any surface mentioning player 1
    for ch in chs:
        ser = json.dumps(ch, default=str)
        if re.search(r'"player"\s*:\s*1', ser):
            return ch.get("id")
    return None


async def answer_cast_opps(c, st, state):
    """Answer the X-choice and target-selection opportunities that follow
    the Banefire CastSpell submission. Returns True if anything submitted."""
    if ST["phase"] != "casting":
        return False
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    wtype = (wf_of(state).get("type") or "")
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        rtype = (opp.get("response", {}) or {}).get("type")
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        spec = data.get("spec") or {}
        stype = spec.get("type") if isinstance(spec, dict) else None
        # X choice: schema/number
        if (rtype == "schema" and stype == "number"
                and ST["x_value"] is None):
            SUBMITTED_OPPS.add(iid)
            sub = {"interactionId": iid,
                   "response": {"type": "number",
                                "data": {"value": X_VALUE}}}
            wire("x_choice_submit", {"submission": sub, "opportunity": opp})
            await c.send_interaction(sub)
            ST["x_value"] = X_VALUE
            say(f"[P0] Banefire X-choice -> X={X_VALUE}")
            acted = True
            continue
        # target selection for Banefire ("any target"): pick P1 (seat 1)
        if ("targetselection" in wtype.lower()
                and not ST["target_chosen"]):
            SUBMITTED_OPPS.add(iid)
            cid = seat1_candidate_id(opp)
            wire("target_opp", {"iid": iid, "rtype": rtype,
                                "seat1_candidate": cid,
                                "n_choices": len(data.get("choices")
                                                 or data.get("candidates")
                                                 or []),
                                "opportunity": opp})
            if cid is None:
                say("[P0] WARNING: no seat-1 candidate found; "
                    "not submitting a blind target")
                ST["notes"].append(
                    "target selection offered but no seat-1 candidate "
                    "identified; see wire_log target_opp")
                continue
            if rtype == "exactChoices":
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": cid}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": "sequence",
                                    "data": {"choiceIds": [cid]}}}
            wire("target_submit", {"submission": sub})
            await c.send_interaction(sub)
            ST["target_chosen"] = True
            ST["target_candidate_id"] = cid
            say(f"[P0] Banefire target -> P1 (candidate {cid})")
            acted = True
            continue
        # anything else during casting: log once, never touch (priority
        # menus live here too)
        key = ("castopp", iid)
        if key not in SUBMITTED:
            SUBMITTED.add(key)
            wire("cast_phase_other_opp",
                 {"rtype": rtype, "spec_type": stype,
                  "waiting_for": wtype,
                  "choice_texts": [choice_text(ch)[:60]
                                   for ch in data.get("choices") or []]})
    return acted


async def activate_scope(c, st, acts, state):
    """Submit ActivateAbility for the untapped Brass Infiniscope."""
    wid = (untapped_ids(state, 0, SCOPE) or [None])[0]
    if wid is None:
        return False
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        ser = json.dumps(a, default=str)
        if str(wid) in ser or str(a.get("_src_oid")) == str(wid):
            say(f"[P0] activating Brass Infiniscope oid {wid}: {a['type']}")
            wire("scope_activate", {"scope_oid": wid, "action": a})
            await submit_as_is(c, a)
            ST["activated"] = True
            ST["activation_at"] = time.time()
            ST["phase"] = "activating"
            return True
    wire("scope_activate_missing",
         {"scope_oid": wid,
          "act_types": sorted({a["type"] for a in acts})})
    return False


# ------------------------------------------------------------- exports
async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 0, "P0", SCOPE):
            return
        if await do_bottom(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    # during the cast sequence: mana payment, X choice, targets first
    if ST["phase"] == "casting":
        if await pay_tick(c, acts, "P0"):
            return
        if await answer_cast_opps(c, st, state):
            return
        if await do_discard(c, 0, "P0"):
            return
    if await do_discard(c, 0, "P0"):
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 0):
        return
    if ST["phase"] not in ("setup", "activating"):
        await pass_priority(c, st, acts)
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        # 1) cast the scope as soon as {4} is payable
        if (not ST["scope_cast"] and SCOPE in hand_lnames(state, 0)
                and len(untapped_lands(state, 0)) >= 4):
            a, oid = cast_action_for(acts, state, SCOPE)
            if a:
                ST["scope_cast"] = True
                say(f"[P0] casting Brass Infiniscope (oid {oid})")
                wire("cast_scope", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
        # 2) activate the scope once Banefire is in hand (stack must be
        #    empty so the trigger window is clean)
        if (ST["scope_cast"] and not ST["activated"]
                and BANEFIRE in hand_lnames(state, 0)
                and untapped_ids(state, 0, SCOPE)
                and not stack_entries(state)):
            if not ST["pre_exported"]:
                p0p = player_of(state, 0)
                p1p = player_of(state, 1)
                ST["pre_life"] = (p0p.get("life"), p1p.get("life"))
                ST["pre_hand_n"] = len(hand_ids(state, 0))
                ST["pre_drawn"] = p0p.get("cards_drawn_this_turn")
                await export_as(c, "pre")
                ST["pre_exported"] = True
                ST["scope_oid"] = untapped_ids(state, 0, SCOPE)[0]
                say(f"[P0] pre exported: scope oid {ST['scope_oid']} "
                    f"untapped, Banefire in hand, life {ST['pre_life']}, "
                    f"hand {ST['pre_hand_n']}")
            if await activate_scope(c, st, acts, state):
                return
        # 3) after activation: cast Banefire with X=2
        if (ST["activated"] and not ST["cast_submitted"]
                and BANEFIRE in hand_lnames(state, 0)):
            a, oid = cast_action_for(acts, state, BANEFIRE)
            if a:
                ST["cast_submitted"] = True
                ST["cast_submitted_at"] = time.time()
                ST["phase"] = "casting"
                say(f"[P0] casting Banefire (oid {oid}) with X={X_VALUE}")
                wire("cast_banefire", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
            wire("banefire_cast_missing",
                 {"hand": hand_lnames(state, 0),
                  "act_types": sorted({a["type"] for a in acts})})
        # 4) land drop
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 1, "P1", None):
            return
        if await do_bottom(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
        return
    if await pay_tick(c, acts, "P1"):
        return
    if await do_discard(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1:
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
    activated = load_env("activated") or {}
    cast = load_env("cast") or {}
    trigger = load_env("trigger") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    activated_st = activated.get("state") or {}
    cast_st = cast.get("state") or {}
    post_st = post.get("state") or {}

    # A1: setup assembled
    if ST["pre_exported"] and pre_st:
        scope_untapped = untapped_ids(pre_st, 0, SCOPE)
        banefire_hand = BANEFIRE in hand_lnames(pre_st, 0)
        life_ok = ST["pre_life"] == (20, 20)
        if scope_untapped and banefire_hand and life_ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(
                f"A1 passed: pre exported with Brass Infiniscope untapped "
                f"on P0 BF (oid {ST['scope_oid']}), Banefire in P0 hand "
                f"({ST['pre_hand_n']} cards), life 20/20.")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(
                f"A1 FAILED: scope_untapped={scope_untapped}, "
                f"banefire_in_hand={banefire_hand}, pre_life={ST['pre_life']}.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: pre state was never exported (the scope "
                     "was never activated with Banefire in hand).")

    # A2: activation produced {C}{C}
    if ST["activated_exported"] and activated_st:
        pool_n, pool_raw = colorless_in_pool(activated_st, 0)
        scope_tapped = (ST["scope_oid"] is not None
                        and get_obj(activated_st, ST["scope_oid"]).get("tapped"))
        dt = activated_st.get("delayed_triggers")
        if pool_n >= 2 and scope_tapped:
            ass["A2_activation_ok"] = "passed"
            notes.append(
                f"A2 passed: scope tapped after activation, P0 mana pool "
                f"holds {pool_n} colorless (raw: "
                f"{json.dumps(pool_raw, default=str)[:200]}).")
        else:
            ass["A2_activation_ok"] = "failed"
            notes.append(
                f"A2 FAILED: pool colorless={pool_n} (raw "
                f"{json.dumps(pool_raw, default=str)[:200]}), "
                f"scope_tapped={scope_tapped}.")
        notes.append(
            "A2 observation (not asserted): state.delayed_triggers right "
            f"after activation = {json.dumps(dt, default=str)[:600]}")
        wire("delayed_triggers_at_activation", {"value": dt})
    elif ST["activated"]:
        ass["A2_activation_ok"] = "failed"
        notes.append("A2 FAILED: activation submitted but the activated "
                     "export is missing.")
    else:
        notes.append("A2 not-run: the scope was never activated.")

    # A3: Banefire cast with X=2, P1 targeted, no rejections
    if ST["cast_exported"] and cast_st:
        p0p = player_of(cast_st, 0)
        notes.append(
            f"A3 context: x_value announced={ST['x_value']}, "
            f"target_chosen={ST['target_chosen']} "
            f"(candidate {ST['target_candidate_id']}), "
            f"cast_rejections={ST['cast_rejections']}, "
            f"target_rejections={ST['target_rejections']}, "
            f"banefire_seen_on_stack={ST['banefire_seen_on_stack']}.")
        if (ST["x_value"] == X_VALUE and ST["target_chosen"]
                and ST["cast_rejections"] == 0
                and ST["target_rejections"] == 0
                and ST["banefire_seen_on_stack"]):
            ass["A3_cast_ok"] = "passed"
            notes.append(
                f"A3 passed: Banefire cast with X={X_VALUE} announced, P1 "
                f"chosen as target, no rejections on the cast path.")
        else:
            ass["A3_cast_ok"] = "failed"
            notes.append("A3 FAILED: the cast path did not complete cleanly "
                         "(see A3 context).")
    elif ST["cast_submitted"]:
        ass["A3_cast_ok"] = "failed"
        notes.append("A3 FAILED: Banefire cast was submitted but the cast "
                     "export is missing.")
    else:
        notes.append("A3 not-run: Banefire was never cast.")

    # A4: the reported bug -- draw + life gain after the X spell
    if (ass["A3_cast_ok"] == "passed" and ST["post_exported"] and post_st):
        p0p = player_of(post_st, 0)
        p1p = player_of(post_st, 1)
        ST["post_life0"] = p0p.get("life")
        ST["post_life1"] = p1p.get("life")
        ST["post_hand0"] = len(hand_ids(post_st, 0))
        ST["post_drawn0"] = p0p.get("cards_drawn_this_turn")
        # draw: hand grew by exactly 1 between cast and post (no other
        # draws happen in the window), corroborated by cards_drawn_this_turn
        hand_delta = None
        if ST["cast_hand0"] is not None:
            hand_delta = ST["post_hand0"] - ST["cast_hand0"]
        drawn_delta = None
        if ST["cast_drawn0"] is not None and ST["post_drawn0"] is not None:
            drawn_delta = ST["post_drawn0"] - ST["cast_drawn0"]
        ST["draw_observed"] = (hand_delta == 1) or (drawn_delta == 1)
        # life: half of X=2 rounded down = 1
        life_delta = None
        if ST["cast_life0"] is not None and ST["post_life0"] is not None:
            life_delta = ST["post_life0"] - ST["cast_life0"]
        ST["life_gain_observed"] = (life_delta == 1)
        notes.append(
            f"A4 measurements: trigger_seen_on_stack={ST['trigger_seen']}, "
            f"hand cast->{ST['cast_hand0']} post->{ST['post_hand0']} "
            f"(delta {hand_delta}), cards_drawn delta {drawn_delta}, "
            f"P0 life cast->{ST['cast_life0']} post->{ST['post_life0']} "
            f"(delta {life_delta}); expected draw +1 and life +1 "
            f"(half of X={X_VALUE}, rounded down).")
        if ST["draw_observed"] and ST["life_gain_observed"]:
            ass["A4_trigger_pays"] = "passed"
            notes.append("A4 passed: after the X spell, P0 drew a card and "
                         "gained 1 life -- the scope's delayed trigger paid "
                         "out.")
        else:
            ass["A4_trigger_pays"] = "failed"
            missing = []
            if not ST["draw_observed"]:
                missing.append("no card drawn")
            if not ST["life_gain_observed"]:
                missing.append("no life gained")
            notes.append(
                f"A4 FAILED: {'; '.join(missing)} after casting the X "
                f"spell -- the reported bug reproduces (trigger on stack: "
                f"{ST['trigger_seen']}).")
    elif ass["A3_cast_ok"] == "passed":
        notes.append("A4 not-run: post state missing.")
    else:
        notes.append("A4 not-run: the cast never completed.")

    # A5: Banefire resolved for 2 to P1
    if ST["post_exported"] and post_st and ST["cast_life1"] is not None:
        ST["p1_damaged"] = (ST["post_life1"] == ST["cast_life1"] - X_VALUE)
        if ST["p1_damaged"]:
            ass["A5_spell_resolves"] = "passed"
            notes.append(
                f"A5 passed: P1 life {ST['cast_life1']} -> "
                f"{ST['post_life1']} (Banefire dealt {X_VALUE}).")
        else:
            ass["A5_spell_resolves"] = "failed"
            notes.append(
                f"A5 FAILED: P1 life {ST['cast_life1']} -> "
                f"{ST['post_life1']}, expected -{X_VALUE}.")
    else:
        notes.append("A5 not-run: life checkpoints incomplete.")

    # A6: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game continues.")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A6 not-run: no post state")

    if (ass["A1_setup_ok"] == "passed"
            and ass["A2_activation_ok"] == "passed"
            and ass["A3_cast_ok"] == "passed"
            and ass["A4_trigger_pays"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Banefire (X=2) was cast after tapping "
            "Brass Infiniscope for {C}{C}, but P0 drew no card and gained "
            "no life -- exactly the reported failure (confirmed on "
            "v0.99.0).")
    elif ass["A4_trigger_pays"] == "passed":
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the scope's delayed trigger paid out "
            "(draw + half-X life) after the X spell. This is not a fix "
            "claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported cast path could not be "
                     "fully exercised; see assertion notes.")

    # server log excerpts for this game
    import glob
    lines = []
    used = None
    for lp in sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"), reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "infiniscope" in l.lower() or "banefire" in l.lower()
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
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7369b.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "scope_oid": ST["scope_oid"],
            "activated": ST["activated"],
            "x_value": ST["x_value"],
            "target_chosen": ST["target_chosen"],
            "target_candidate_id": ST["target_candidate_id"],
            "cast_rejections": ST["cast_rejections"],
            "target_rejections": ST["target_rejections"],
            "trigger_seen": ST["trigger_seen"],
            "delayed_triggers_at_activation": ST["delayed_triggers_at_activation"],
            "draw_observed": ST["draw_observed"],
            "life_gain_observed": ST["life_gain_observed"],
            "p1_damaged": ST["p1_damaged"],
            "checkpoints": {
                "pre": {"life": ST["pre_life"], "hand_n": ST["pre_hand_n"],
                        "drawn": ST["pre_drawn"]},
                "cast": {"life0": ST["cast_life0"], "life1": ST["cast_life1"],
                         "hand0": ST["cast_hand0"], "drawn0": ST["cast_drawn0"]},
                "post": {"life0": ST["post_life0"], "life1": ST["post_life1"],
                         "hand0": ST["post_hand0"], "drawn0": ST["post_drawn0"]},
            },
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report's attached Discord game state was not imported (the "
            "prebuilt server has no standalone state-restore facility); the "
            "scenario replays the reported line (tap scope, cast X spell) "
            "from a fresh game.",
            "X=2 was used (expected life gain 1); other X values were not "
            "exercised.",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
        "setup_line": "P0: 4x Brass Infiniscope + 4x Banefire + 52x "
                      "Mountain; P1: 60x Island",
        "contract_line": "Tap Brass Infiniscope for {C}{C}, then cast "
                         "Banefire with X=2 -> P0 must draw a card and gain "
                         "1 life (half of X, rounded down)",
        "prior_runs": [],
        "stats": {"states_seen": ST["states_seen"],
                  "trigger_observations": ST["trigger_observations"]},
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
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return run


# ------------------------------------------------------------- main
async def main():
    reset()
    check_data_level()

    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    finalized = False

    while time.time() - t_start < SETUP_DEADLINE_S and not finalized:
        await asyncio.sleep(0.15)
        now = time.time()
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            same_rev = (c.revision == last.get(tag))
            stale = now - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = c.revision
            last_tick_at[tag] = now
            # ---- observation pass
            try:
                try:
                    while True:
                        t, data = c.inbox.get_nowait()
                        if t in ("ActionRejected", "Error"):
                            say(f"[{tag}] {t}: {json.dumps(data)[:300]}")
                            wire("rejection", {"who": tag, "type": t,
                                               "data": data})
                            if ST["phase"] == "casting":
                                if not ST["target_chosen"]:
                                    ST["cast_rejections"] += 1
                                else:
                                    ST["target_rejections"] += 1
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                ST["states_seen"] += 1
                if tag == "P0" and not ST["player_keys_logged"]:
                    ST["player_keys_logged"] = True
                    wire("player_keys",
                         {"p0_keys": sorted(player_of(state, 0).keys())})
                acts = merged_actions(st)

                if tag == "P0":
                    # activation detection: scope tapped + pool check ->
                    # export "activated"
                    if (ST["phase"] == "activating" and not ST["activated_exported"]
                            and ST["scope_oid"] is not None
                            and get_obj(state, ST["scope_oid"]).get("tapped")):
                        pool_n, pool_raw = colorless_in_pool(state, 0)
                        ST["pool_at_activation"] = pool_raw
                        ST["delayed_triggers_at_activation"] = \
                            state.get("delayed_triggers")
                        wire("activated_snapshot",
                             {"pool": pool_raw,
                              "delayed_triggers": state.get("delayed_triggers")})
                        await export_as(c, "activated")
                        ST["activated_exported"] = True
                        say(f"activated exported: pool colorless={pool_n}, "
                            f"delayed_triggers="
                            f"{json.dumps(state.get('delayed_triggers'), default=str)[:200]}")
                    # cast detection: Banefire on the stack (resolve the
                    # entry's object id -- spell entries often carry no
                    # card name in the entry itself)
                    if ST["phase"] == "casting" and not ST["banefire_seen_on_stack"]:
                        boid = stack_spell_oid(state, BANEFIRE)
                        if boid is not None:
                            ST["banefire_seen_on_stack"] = True
                            ST["banefire_stack_oid"] = boid
                            bobj = get_obj(state, boid)
                            wire("banefire_on_stack",
                                 {"oid": boid, "object": bobj,
                                  "entries": stack_entries(state)})
                            say(f"Banefire on stack (oid {boid}, "
                                f"{len(stack_entries(state))} entries); "
                                f"object keys: {sorted(bobj.keys())}")
                    # cast export: X announced + target chosen + spell seen
                    if (ST["phase"] == "casting" and not ST["cast_exported"]
                            and ST["x_value"] is not None
                            and ST["target_chosen"]
                            and ST["banefire_seen_on_stack"]):
                        p0p = player_of(state, 0)
                        p1p = player_of(state, 1)
                        ST["cast_life0"] = p0p.get("life")
                        ST["cast_life1"] = p1p.get("life")
                        ST["cast_hand0"] = len(hand_ids(state, 0))
                        ST["cast_drawn0"] = p0p.get("cards_drawn_this_turn")
                        await export_as(c, "cast")
                        ST["cast_exported"] = True
                        ST["phase"] = "resolving"
                        ST["settle_at"] = now
                        say(f"cast exported: P0 life {ST['cast_life0']} hand "
                            f"{ST['cast_hand0']} drawn {ST['cast_drawn0']}; "
                            f"P1 life {ST['cast_life1']}; entering resolving")
                    # trigger detection during resolving
                    if ST["phase"] == "resolving":
                        hits = stack_mentions(state, "infiniscope")
                        seen = bool(hits) or stack_has_trigger_from(state, SCOPE)
                        if seen and not ST["trigger_seen"]:
                            ST["trigger_seen"] = True
                            ST["trigger_seen_at"] = now
                            ST["trigger_observations"] += 1
                            wire("trigger_on_stack", {"entries": stack_entries(state)})
                            say("Infiniscope delayed trigger observed ON THE STACK")
                            await export_as(c, "trigger")
                            ST["trigger_exported"] = True
                        elif seen:
                            ST["trigger_observations"] += 1
                    # settle detection: stack empty + Priority + idle
                    if ST["phase"] == "resolving" and ST["cast_exported"]:
                        if not stack_entries(state):
                            if ST["settle_at"] is None:
                                ST["settle_at"] = now
                            idle = now - ST["settle_at"]
                            wtype = wf_of(state).get("type")
                            if wtype == "Priority" and idle > SETTLE_IDLE_S:
                                say(f"settled: stack empty, Priority, "
                                    f"{idle:.0f}s idle; exporting post")
                                await export_as(c, "post")
                                ST["post_exported"] = True
                                finalized = True
                                break
                        else:
                            ST["settle_at"] = None
                    # cast watchdog: 180s after submission with no cast
                    # export -> stuck state
                    if (ST["cast_submitted"] and not ST["cast_exported"]
                            and ST.get("cast_submitted_at")
                            and now - ST["cast_submitted_at"] > 180
                            and not ST["post_exported"]):
                        say("cast watchdog: 180s after submission, cast "
                            "never completed -- exporting stuck state")
                        wire("cast_stuck",
                             {"waiting_for": wf_of(state),
                              "x_value": ST["x_value"],
                              "target_chosen": ST["target_chosen"],
                              "banefire_on_stack": ST["banefire_seen_on_stack"]})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
            except Exception as e:
                say(f"[{tag}] observation error: {e}")
            # ---- action pass
            try:
                acts = merged_actions(st)
                await tick(st, acts, st["state"], c)
            except Exception as e:
                say(f"[{tag}] tick error: {e}")
    say("finalizing")
    run = await finalize(p0)
    await p0.close()
    await p1.close()
    return run


if __name__ == "__main__":
    run = asyncio.run(main())
    # copy the scenario into the evidence dir, render the PNG, and write
    # the SHA-256 manifest over everything except the manifest itself.
    shutil.copy(f"{BACKFILL}/driver/scenario_7369b.py",
                f"{EVDIR}/scenario_7369b.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Brass Infiniscope isn't triggering on X spells"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
