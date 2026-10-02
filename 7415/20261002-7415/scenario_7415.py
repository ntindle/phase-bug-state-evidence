#!/usr/bin/env python3
"""Issue #7415: Duneblast - "choose up to one creature" is unparsed, so
`Destroy` reads an empty tracked set.

Reported (internal-triage 2026-08-15, confirmed, area:parser,
classifier:unsupported-aspect, related #6857): the census found the
"choose up to one creature" clause unparsed (Effect::Unimplemented), so
`Destroy` in the same chain reads an empty tracked set.

Oracle text (verified from pinned v0.99.0 card-data.json, key 'duneblast'):
  {4}{W}{B}{G} Sorcery
  "Choose up to one creature. Destroy the rest."
Ruling (2014-09-20): "You decide which creature to spare as Duneblast
resolves. ... If you don't choose a creature, then all creatures will be
destroyed."

IMPORTANT: the pinned v0.99.0 card-data actually parses Duneblast
correctly: effect ChooseObjectsIntoTrackedSet (chooser Controller, filter
Creature, min 0, max 1) with sub_ability DestroyAll (Typed Creature,
Not(InTrackedSet id 0)). The issue's "measured parse state" was against
an older corpus (card-data generated at 9b7c66e30; coverage preview).
This run therefore tests the RUNTIME behavior on the pinned v0.99.0
release: does choosing a creature actually spare it, and does choosing
none destroy everything?

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing)
------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Duneblast + 4x Savannah Lions + 4x Llanowar Elves
      + 4x Deathrite Shaman + 12x Plains + 16x Forest + 16x Swamp
  P1: 4x Savannah Lions + 4x Llanowar Elves + 4x Deathrite Shaman
      + 16x Plains + 16x Forest + 16x Swamp
Drive:
  1. Both sides mulligan/land-drop/cast cheap creatures to build boards.
  2. Branch A (the reported choice path): on a P0 main phase with
     Duneblast in hand, >=7 untapped lands incl. W+B+G sources, >=3
     creatures total on the battlefields, and an empty stack:
       a. export pre (setup assembled)
       b. cast Duneblast
       c. export cast (Duneblast spell on the stack)
       d. pass priority; Duneblast begins resolving
       e. when the "choose up to one creature" choice is offered to P0,
          export choose1 (pre-decision), then submit the spare choice:
          a P0 Savannah Lions on the battlefield (else any P0 creature)
       f. settle; export post1
  3. Branch B (choose none, the control): if P0 holds a second Duneblast
     and can pay again, cast it, export cast2, and at its resolution
     choose NONE; settle; export post.
Expected (correct behavior): branch A leaves exactly the spared
creature on the battlefield and destroys every other creature
(Duneblast in P0's graveyard); branch B destroys all remaining
creatures.
Reported (bug, if it reproduces at runtime): the Destroy reads an
empty tracked set, so the wrong set of creatures is destroyed (per the
census analysis: nothing destroyed, or the spare logic inverted).

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok    pre exported: Duneblast in P0 hand, >=3 creatures on the
                 battlefields, >=7 untapped lands with W+B+G sources,
                 life 20/20.
  A2_cast_ok     Duneblast #1 cast with no rejections, seen on the stack,
                 resolved.
  A3_choice_ok   the "choose up to one" choice was offered to P0 and the
                 spare choice was submitted without rejection.
  A4_spare       (THE REPORTED PATH) post1: the chosen spared creature is
                 still on the battlefield; every other battlefield
                 creature from choose1 is in a graveyard; Duneblast is in
                 P0's graveyard; stack empty.
  A5_choose_none (CONTROL) post: no creatures on any battlefield after
                 branch B (the previously spared creature destroyed);
                 Duneblast #2 in P0's graveyard.
  A6_cleanup     post stack empty, game advancing.

Verdict rule: reproduced iff A1..A3 passed and A4 failed (wrong
              destruction outcome); not-reproduced iff A4 and A5 passed;
              blocked iff A1 failed (the setup line never assembled).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02): as in
scenario_7380 (protocol 98; CreateGameWithSettings + JoinGameWithPassword
+ start_when_full; MulliganDecision/SelectCards/PassPriority actions;
target/choice selection via viewer_interaction exactChoices/sequence).
The "choose up to one" opportunity structure is unknown in advance;
the scenario logs the full opportunity JSON before submitting.
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-7415"
ISSUE = 7415
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
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

for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))

DB = "duneblast"
LION = "savannah lions"
ELVES = "llanowar elves"
SHAMAN = "deathrite shaman"
PLAINS = "plains"
FOREST = "forest"
SWAMP = "swamp"
CREATURES = (LION, ELVES, SHAMAN)

P0_DECK = [("Duneblast", 4), ("Savannah Lions", 4), ("Llanowar Elves", 4),
           ("Deathrite Shaman", 4), ("Plains", 12), ("Forest", 16),
           ("Swamp", 16)]
P1_DECK = [("Savannah Lions", 4), ("Llanowar Elves", 4),
           ("Deathrite Shaman", 4), ("Plains", 16), ("Forest", 16),
           ("Swamp", 16)]

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
        "phase": "setup",  # setup -> casting_db1 -> choice1 -> resolving1
                           # -> casting_db2 -> choice2 -> resolving2 -> done
        "db1_submitted": False,
        "db1_submitted_at": None,
        "db1_seen_on_stack": False,
        "db1_stack_oid": None,
        "db1_cast_rejections": 0,
        "db1_resolved": False,
        "choice1_offered": False,
        "choice1_submitted": False,
        "choice1_exported": False,
        "choice1_rejections": 0,
        "choice1_opportunity": None,
        "spare_oid": None,
        "spare_name": None,
        "choose1_creatures": None,
        "post1_exported": False,
        "db2_submitted": False,
        "db2_seen_on_stack": False,
        "db2_cast_rejections": 0,
        "choice2_offered": False,
        "choice2_submitted": False,
        "choice2_how": None,
        "choice2_rejections": 0,
        "post_exported": False,
        "settle_at": None,
        "states_seen": 0,
        "pre_life": None,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_cast_ok",
                                       "A3_choice_ok", "A4_spare",
                                       "A5_choose_none", "A6_cleanup")},
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


def zone_ids(state, pid, zone, key=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != zone or o.get("controller") != pid:
            continue
        if key is not None and str(o.get("base_name") or o.get("name") or "").lower() != key:
            continue
        out.append(int(oid))
    return out


def battlefield_creatures(state):
    """All creature oids on any battlefield."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        ctypes = (o.get("card_types") or {}).get("core_types", []) or []
        if any(str(t).lower() == "creature" for t in ctypes):
            out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and "land" in [str(t).lower()
                           for t in (o.get("card_types") or {}).get("core_types", [])]]


def has_colors(state, pid, keys):
    lands = untapped_lands(state, pid)
    names = [obj_lname(state, i) for i in lands]
    return all(k in names for k in keys)


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


def stack_spell_oid(state, key):
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        for k in ("object_id", "id", "source", "source_id"):
            try:
                iv = int(e.get(k))
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == key:
                return iv
    return None


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


def opp_candidates(opp):
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    return data.get("choices") or data.get("candidates") or []


def candidate_matching(opp, needles):
    for ch in opp_candidates(opp):
        if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
            continue
        ser = json.dumps(ch, default=str).lower()
        if all(n.lower() in ser for n in needles):
            return ch.get("id")
    return None


def check_data_level():
    """A0 (informational): card data parses as the triage asserts on
    the pinned release."""
    db = CARD_DATA.get("duneblast", {})
    findings = {
        "db_name": db.get("name"),
        "db_mana_cost": db.get("mana_cost"),
        "db_oracle": db.get("oracle_text"),
        "db_abilities": db.get("abilities"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    eff = ((db.get("abilities") or [{}])[0].get("effect") or {})
    sub = (((db.get("abilities") or [{}])[0].get("sub_ability") or {})
           .get("effect") or {})
    ok = (eff.get("type") == "ChooseObjectsIntoTrackedSet"
          and eff.get("max") == 1 and eff.get("min") == 0
          and sub.get("type") == "DestroyAll"
          and json.dumps(sub.get("target")).find("InTrackedSet") >= 0)
    say(f"data-level check: {'OK (clause parsed as ChooseObjectsIntoTrackedSet(min 0,max 1)->DestroyAll excl. InTrackedSet) -- the issue premise (unparsed Effect::Unimplemented head) does NOT hold in the pinned v0.99.0 data; this run tests runtime behavior against the current parse' if ok else 'MISMATCH (issue premise holds in pinned data)'}")
    ST["notes"].append(
        f"data-level: Duneblast effect parses as "
        f"{eff.get('type')}(min={eff.get('min')},max={eff.get('max')})->"
        f"{sub.get('type')}(target={'Not(InTrackedSet)' if 'InTrackedSet' in json.dumps(sub.get('target')) else 'other'}): parse_correct={ok}. "
        f"On ok=True the census premise does not hold in pinned v0.99.0 "
        f"card-data, so the run asserts runtime behavior, not the parse.")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    return hand.count(PLAINS) + hand.count(FOREST) + hand.count(SWAMP) >= 2


def mulligan_keep_p1(hand):
    return hand.count(PLAINS) + hand.count(FOREST) + hand.count(SWAMP) >= 2


async def do_mulligan(c, pid, tag, keep_fn):
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
    if keep_fn(hn) or mulls >= 2 or len(hn) <= 5:
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
    rank = {DB: 5, LION: 4, ELVES: 4, SHAMAN: 4, PLAINS: 3, FOREST: 3, SWAMP: 3}
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
    pend = pending_for(state, pid)
    if pend is None:
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    rank = {PLAINS: 0, FOREST: 0, SWAMP: 0, LION: 1, ELVES: 1, SHAMAN: 1, DB: 2}
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


async def submit_choice_candidate(c, opp, cid, label, branch):
    """Submit a single candidate through the opportunity's advertised
    response schema (exactChoices -> choose; else sequence)."""
    iid = opp.get("interactionId") or opp.get("id")
    rtype = (opp.get("response", {}) or {}).get("type")
    if rtype == "exactChoices":
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": cid}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": [cid]}}}
    wire("choice_submit", {"branch": branch, "submission": sub})
    await c.send_interaction(sub)
    say(f"[P0] {label} branch {branch}: chose candidate {cid} "
        f"(rtype {rtype})")


async def submit_choice_none(c, opp, label, branch):
    """Choose 'none' for the up-to-one choice. Prefer an explicit
    none/decline candidate; fall back to an empty sequence if the schema
    admits one. Returns how the choice was submitted."""
    for needles in (["none"], ["decline"], ["skip"], ["no creature"]):
        cid = candidate_matching(opp, needles)
        if cid is not None:
            await submit_choice_candidate(c, opp, cid, label, branch)
            return f"candidate({cid})"
    rtype = (opp.get("response", {}) or {}).get("type")
    iid = opp.get("interactionId") or opp.get("id")
    sub = {"interactionId": iid,
           "response": {"type": "sequence", "data": {"choiceIds": []}}}
    wire("choice_submit_none_fallback", {"branch": branch, "submission": sub,
                                         "rtype": rtype})
    await c.send_interaction(sub)
    say(f"[P0] {label} branch {branch}: submitted empty choiceIds "
        f"(rtype {rtype})")
    return "empty_sequence"


async def handle_duneblast_choice(c, st, state, branch, acts):
    """Look for P0's 'choose up to one creature' opportunity during
    Duneblast resolution and answer it. Branch A: spare a P0 creature.
    Branch B: choose none. Returns True if a submission was sent."""
    vi = get_vi(st)
    acted = False
    wtype = (wf_of(state).get("type") or "")
    # The choice may not list P0 in waiting_for.pending in the format
    # pending_for expects; gate on the ChooseObjectsSelection waiting
    # type plus a submittable opportunity instead.
    if not (vi and wtype == "ChooseObjectsSelection"):
        return False
    if vi and wtype == "ChooseObjectsSelection":
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId") or opp.get("id")
            if iid in SUBMITTED_OPPS:
                continue
            nc = len(opp_candidates(opp))
            wire("choice_opp", {"branch": branch, "iid": iid, "rtype":
                                (opp.get("response", {}) or {}).get("type"),
                                "n_choices": nc,
                                "waiting_for": wf_of(state).get("type"),
                                "opportunity": opp})
            say(f"[P0] choice opportunity (branch {branch}): iid {iid}, "
                f"type {(opp.get('response', {}) or {}).get('type')}, "
                f"{nc} candidates, wf={wf_of(state).get('type')}")
            ST[f"choice{branch}_offered"] = True
            if branch == "1":
                # prefer a P0 Savannah Lions candidate, then any P0
                # creature. Candidates serialize as
                # {"name": ..., "controller": <seat>, "reference": "<oid>"}.
                # P0 is seat 0 -- a bare name needle matched P1's lion
                # once, which spared the wrong side's creature.
                spare = candidate_matching(opp, [LION, '"controller": 0'])
                if spare is None:
                    p0_creatures = [x for x in zone_ids(state, 0, "Battlefield")
                                    if obj_lname(state, x) in CREATURES]
                    if p0_creatures:
                        nm = obj_lname(state, p0_creatures[0])
                        spare = candidate_matching(opp, [nm, '"controller": 0'])
                if spare is None:
                    say(f"[P0] WARNING branch 1: no candidate matched a P0 "
                        f"creature; not submitting blind")
                    ST["notes"].append("choice1: offered but no candidate "
                                       "matched a P0 creature; see wire_log")
                    continue
                SUBMITTED_OPPS.add(iid)
                ST["choice1_opportunity"] = opp
                # record the actual object being spared from the chosen
                # candidate's reference field (exact, not a name guess)
                chosens = None
                for ch in opp_candidates(opp):
                    if ch.get("id") == spare:
                        chosens = ch
                        break
                ref = None
                if chosens:
                    ser_ch = json.dumps(chosens, default=str)
                    import re as _re
                    m = _re.search(r'"reference":\s*"(\d+)"', ser_ch)
                    ref = int(m.group(1)) if m else None
                ST["spare_oid"] = ref
                ST["spare_name"] = obj_lname(state, ref) \
                    if ref is not None else None
                ST["choose1_creatures"] = battlefield_creatures(state)
                if not ST["choice1_exported"]:
                    await export_as(c, "choose1")
                    ST["choice1_exported"] = True
                await submit_choice_candidate(c, opp, spare, "Duneblast", "1")
                ST["choice1_submitted"] = True
                acted = True
            else:
                # branch 2: choose none
                SUBMITTED_OPPS.add(iid)
                # baseline for the control comparison: the creatures the
                # choice was offered over (candidates carry the object
                # reference), not whatever is on the board 20s later
                refs = []
                import re as _re2
                for ch in opp_candidates(opp):
                    m = _re2.search(r'"reference":\s*"(\d+)"',
                                    json.dumps(ch, default=str))
                    if m:
                        refs.append(int(m.group(1)))
                ST["choose2_creatures"] = refs or battlefield_creatures(state)
                ST["choice2_how"] = await submit_choice_none(
                    c, opp, "Duneblast", "2")
                ST["choice2_submitted"] = True
                acted = True
    # legacy path: a Choice/Choose action in legal_actions (not
    # viewer_interaction). Log it; answering blind is not allowed.
    if not acted:
        for a in acts:
            at = a.get("type", "")
            if "choos" in at.lower() or "choice" in at.lower():
                wire("legacy_choice_action", {"branch": branch,
                                              "action": a,
                                              "waiting_for": wf_of(state).get("type")})
                say(f"[P0] WARNING branch {branch}: legacy choice action "
                    f"{at} seen; not auto-answering (see wire_log)")
    return acted


async def instrument_resolution(c, st, state, branch):
    """Periodic snapshot of what the engine is waiting on while a
    Duneblast resolution is in flight (max once per 10s)."""
    now = time.time()
    key = f"instr{branch}"
    if now - ST.get(key, 0) < 10:
        return
    ST[key] = now
    vi = st.get("viewer_interaction") or {}
    opps = vi.get("opportunities") or []
    wf = wf_of(state)
    acts = merged_actions(st)
    wire("resolution_probe",
         {"branch": branch,
          "waiting_for": wf.get("type"),
          "pending_players": [str(p.get("player"))
                              for p in ((wf.get("data") or {})
                                        .get("pending", []) or [])],
          "vi_canSubmit": vi.get("canSubmit"),
          "vi_n_opportunities": len(opps),
          "vi_rtypes": [(o.get("response", {}) or {}).get("type")
                        for o in opps],
          "action_types": sorted({a.get("type") for a in acts}),
          "n_stack": len(stack_entries(state)),
          "stack": stack_entries(state)})
    say(f"[probe b{branch}] wf={wf.get('type')} vi_canSubmit="
        f"{vi.get('canSubmit')} vi_opps={len(opps)} "
        f"actions={sorted({a.get('type') for a in acts})[:8]} "
        f"stack_n={len(stack_entries(state))}")


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")

# ------------------------------------------------------------- P0 tick
def can_pay_duneblast(state):
    lands = untapped_lands(state, 0)
    return len(lands) >= 7 and has_colors(state, 0, (PLAINS, FOREST, SWAMP))


async def maybe_cast_duneblast(c, acts, state, which):
    """Cast Duneblast #1 or #2 when the contract's preconditions hold."""
    key = f"db{which}_submitted"
    if ST[key]:
        return False
    if DB not in hand_lnames(state, 0):
        return False
    if not can_pay_duneblast(state):
        return False
    if stack_entries(state):
        return False
    creatures = battlefield_creatures(state)
    if which == 1 and len(creatures) < 3:
        return False
    if which == 2 and len(creatures) < 1:
        return False
    a, oid = cast_action_for(acts, state, DB)
    if not a:
        return False
    ST[key] = True
    ST[f"db{which}_submitted_at"] = time.time()
    p0p = player_of(state, 0)
    p1p = player_of(state, 1)
    ST["pre_life"] = (p0p.get("life"), p1p.get("life"))
    await export_as(c, "pre" if which == 1 else "pre2")
    ST["phase"] = f"casting_db{which}"
    say(f"[P0] casting Duneblast #{which} (oid {oid}); "
        f"untapped lands={len(untapped_lands(state, 0))}, "
        f"creatures on board={len(creatures)}")
    wire(f"cast_db{which}", {"oid": oid, "action": a["type"]})
    await submit_as_is(c, a)
    return True


async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 0, "P0", mulligan_keep_p0):
            return
        if await do_bottom(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    if ST["phase"].startswith("casting_db"):
        if await pay_tick(c, acts, "P0"):
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
            d.setdefault("data", {}).update({"assignments": [], "orders": []})
            await submit_as_is(c, d)
        return
    # the "choose up to one creature" opportunity during resolution
    if ST["phase"] in ("casting_db1", "casting_db2", "choice1", "choice2"):
        branch = "1" if ST["phase"] in ("casting_db1", "choice1") else "2"
        await instrument_resolution(c, st, state, branch)
        if await handle_duneblast_choice(c, st, state, branch, acts):
            ST["phase"] = f"choice{branch}"
            return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        # cast Duneblast #1 then #2 (branch B after branch A settles)
        which = 1 if not ST["db1_submitted"] else 2
        if ((which == 1 and ST["phase"] == "setup")
                or (which == 2 and ST["phase"] == "resolving1_settled")):
            if await maybe_cast_duneblast(c, acts, state, which):
                return
        # cast cheap creatures to build the board
        for key in (LION, ELVES, SHAMAN):
            if key in hand_lnames(state, 0) and untapped_lands(state, 0):
                a, oid = cast_action_for(acts, state, key)
                if a:
                    say(f"[P0] casting {key} (oid {oid})")
                    await submit_as_is(c, a)
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 1, "P1", mulligan_keep_p1):
            return
        if await do_bottom(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
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
            d.setdefault("data", {}).update({"assignments": [], "orders": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1:
        for key in (LION, ELVES, SHAMAN):
            if key in hand_lnames(state, 1) \
                    and untapped_lands(state, 1):
                a, oid = cast_action_for(acts, state, key)
                if a:
                    say(f"[P1] casting {key} (oid {oid})")
                    await submit_as_is(c, a)
                    return
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
    cast = load_env("cast") or {}
    choose1 = load_env("choose1") or {}
    post1 = load_env("post1") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    cast_st = cast.get("state") or {}
    choose1_st = choose1.get("state") or {}
    post1_st = post1.get("state") or {}
    post_st = post.get("state") or {}

    # A1: setup assembled
    if pre_st:
        db_hand = DB in hand_lnames(pre_st, 0)
        creatures = [o for o in (pre_st.get("objects") or {}).values()
                     if o.get("zone") == "Battlefield"
                     and "creature" in [str(t).lower() for t in
                                        ((o.get("card_types") or {})
                                         .get("core_types", []) or [])]]
        lands_ok = can_pay_duneblast(pre_st)
        life_ok = ST["pre_life"] == (20, 20)
        if db_hand and len(creatures) >= 3 and lands_ok and life_ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(
                f"A1 passed: pre exported with Duneblast in P0 hand, "
                f"{len(creatures)} creatures on battlefields, "
                f"{len(untapped_lands(pre_st, 0))} untapped P0 lands "
                f"(W+B+G present), life 20/20.")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(
                f"A1 FAILED: db_in_hand={db_hand}, "
                f"creatures={len(creatures)}, lands_ok={lands_ok}, "
                f"pre_life={ST['pre_life']}.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: pre state was never exported.")

    # A2: Duneblast #1 cast cleanly and resolved
    if ST["db1_submitted"]:
        if (ST["db1_seen_on_stack"] and ST["db1_cast_rejections"] == 0
                and ST["db1_resolved"]):
            ass["A2_cast_ok"] = "passed"
            notes.append(
                f"A2 passed: Duneblast #1 cast with no rejections, seen on "
                f"the stack (oid {ST['db1_stack_oid']}), then resolved "
                f"(left the stack).")
        else:
            ass["A2_cast_ok"] = "failed"
            notes.append(
                f"A2 FAILED: on_stack={ST['db1_seen_on_stack']}, "
                f"rejections={ST['db1_cast_rejections']}, "
                f"resolved={ST['db1_resolved']}.")
    else:
        notes.append("A2 not-run: Duneblast #1 was never cast.")

    # A3: the "choose up to one" choice was offered and answered
    if ST["choice1_offered"]:
        if ST["choice1_submitted"] and ST["choice1_rejections"] == 0:
            ass["A3_choice_ok"] = "passed"
            notes.append(
                f"A3 passed: the 'choose up to one creature' choice was "
                f"offered to P0 and the spare choice was submitted "
                f"(spare candidate resolved to oid {ST['spare_oid']} = "
                f"{ST['spare_name']}), no rejections.")
        else:
            ass["A3_choice_ok"] = "failed"
            notes.append(
                f"A3 FAILED: offered but submission incomplete "
                f"(submitted={ST['choice1_submitted']}, "
                f"rejections={ST['choice1_rejections']}).")
    elif ST["db1_resolved"]:
        ass["A3_choice_ok"] = "failed"
        notes.append("A3 FAILED: Duneblast #1 resolved without ever "
                     "offering P0 the 'choose up to one' choice.")
    else:
        notes.append("A3 not-run: Duneblast #1 never resolved.")

    # A4: THE REPORTED PATH -- branch A outcome
    if ST["post1_exported"] and post1_st and ST.get("choose1_creatures") is not None:
        spare = ST["spare_oid"]
        before = set(ST["choose1_creatures"] or [])
        spare_bf = spare is not None and \
            get_obj(post1_st, spare).get("zone") == "Battlefield"
        others = []
        for oid in before:
            if oid == spare:
                continue
            o = get_obj(post1_st, oid)
            others.append((oid, obj_lname(post1_st, oid), o.get("zone")))
        others_dead = all(z == "Graveyard" for _, _, z in others)
        db_gy = zone_ids(post1_st, 0, "Graveyard", DB)
        stack_empty = not stack_entries(post1_st)
        wire("post1_probe", {"spare_oid": spare, "spare_bf": spare_bf,
                             "others": others, "db_gy": db_gy,
                             "stack_empty": stack_empty,
                             "choice_offered": ST["choice1_offered"]})
        notes.append(
            f"A4 probe: spare oid {spare} ({ST['spare_name']}) on "
            f"battlefield post1={spare_bf}; other creatures at resolution "
            f"(n={len(others)}): "
            f"{[(n, z) for _, n, z in others]}; Duneblast in P0 "
            f"graveyard={bool(db_gy)}; stack empty={stack_empty}; "
            f"choice_offered={ST['choice1_offered']}.")
        if ST["choice1_offered"] and spare_bf and others_dead and db_gy and stack_empty:
            ass["A4_spare"] = "passed"
            notes.append(
                f"A4 passed: exactly the spared creature "
                f"({ST['spare_name']}, oid {spare}) survived; all "
                f"{len(others)} other creatures were destroyed and "
                f"Duneblast went to P0's graveyard. The reported "
                f"failure does not reproduce on v0.99.0.")
        else:
            ass["A4_spare"] = "failed"
            if not ST["choice1_offered"]:
                notes.append(
                    f"A4 FAILED (no-choice path): Duneblast resolved "
                    f"without offering the 'choose up to one creature' "
                    f"choice; observed outcome: "
                    f"{[(n, z) for _, n, z in others]} "
                    f"(spare_oid={spare}). The Destroy consumed an "
                    f"undefined/empty tracked set instead of a choice.")
            else:
                notes.append(
                    f"A4 FAILED: wrong destruction outcome. spared on "
                    f"battlefield={spare_bf}; all others destroyed="
                    f"{others_dead}; Duneblast in graveyard={bool(db_gy)}. "
                    f"This is the reported bug.")
    else:
        notes.append("A4 not-run: post1 export or resolution baseline missing.")

    # A5: control -- branch B (choose none). The comparison baseline is
    # the candidate set the choice was offered over, NOT the final board:
    # either player may cast new creatures during the settle window.
    if ST["post_exported"] and post_st and ST.get("choose2_creatures"):
        destroyed = []
        for oid in ST["choose2_creatures"]:
            o = get_obj(post_st, oid)
            destroyed.append((oid, obj_lname(post_st, oid), o.get("zone")))
        all_dead = all(z == "Graveyard" for _, _, z in destroyed)
        db2_gy = zone_ids(post_st, 0, "Graveyard", DB)
        if (ST["choice2_submitted"] and ST["choice2_rejections"] == 0
                and all_dead and db2_gy and not stack_entries(post_st)):
            ass["A5_choose_none"] = "passed"
            notes.append(
                f"A5 passed: branch B chose none (via "
                f"{ST['choice2_how']}); all {len(destroyed)} creatures on "
                f"the board at resolution were destroyed "
                f"{[(n, z) for _, n, z in destroyed]}; Duneblast #2 in "
                f"P0's graveyard; stack empty. (Creatures cast after "
                f"resolution are not part of this comparison.)")
        else:
            ass["A5_choose_none"] = "failed"
            notes.append(
                f"A5 FAILED: post-resolution board creatures="
                f"{[(n, z) for _, n, z in destroyed]}, "
                f"choice2_submitted={ST['choice2_submitted']}, "
                f"how={ST['choice2_how']}, "
                f"rejections={ST['choice2_rejections']}, "
                f"Duneblast in graveyard={bool(db2_gy)}.")
    else:
        notes.append("A5 not-run: post export or branch-2 baseline missing.")

    # A6: cleanup
    if post_st or post1_st:
        final = post_st if post_st else post1_st
        if not stack_entries(final):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: final stack empty, game continues.")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"A6 FAILED: final stack non-empty: "
                         f"{stack_entries(final)}")
    else:
        notes.append("A6 not-run: no final state")

    if (ass["A1_setup_ok"] == "passed"
            and ass["A2_cast_ok"] == "passed"
            and ass["A4_spare"] == "failed"):
        verdict = "reproduced"
        if ass["A3_choice_ok"] == "failed":
            notes.append(
                "verdict=reproduced (no-choice variant): Duneblast "
                "resolved on v0.99.0 without ever offering the 'choose up "
                "to one creature' choice, so the Destroy consumed an "
                "undefined/empty tracked set -- the runtime shape of the "
                "reported defect.")
        else:
            notes.append(
                "verdict=reproduced: Duneblast's 'choose up to one "
                "creature' choice produced the wrong destruction outcome "
                "on v0.99.0.")
    elif (ass["A4_spare"] == "passed" and ass["A5_choose_none"] == "passed"):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: Duneblast spared exactly the chosen "
            "creature (branch A) and destroyed all remaining creatures "
            "when none was chosen (branch B) on v0.99.0. This is not a "
            "fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported choice path could not "
                     "be fully exercised; see assertion notes.")

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
               if "duneblast" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7415.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "spare_oid": ST["spare_oid"],
            "spare_name": ST["spare_name"],
            "choose1_creatures": ST["choose1_creatures"],
            "choice1_offered": ST["choice1_offered"],
            "choice1_submitted": ST["choice1_submitted"],
            "choice1_rejections": ST["choice1_rejections"],
            "choice2_how": ST["choice2_how"],
            "choice2_submitted": ST["choice2_submitted"],
            "choice2_rejections": ST["choice2_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report's parse-state evidence (Effect::Unimplemented head) "
            "was measured against an older card-data corpus; the pinned "
            "v0.99.0 card-data parses Duneblast as "
            "ChooseObjectsIntoTrackedSet -> DestroyAll, so this run tests "
            "runtime behavior against the current parse.",
            "Only the spare-one branch (P0 creature) and the choose-none "
            "control were exercised; sparing an opponent's creature was not.",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
        "setup_line": "P0: 4x Duneblast + 4x Savannah Lions + 4x Llanowar "
                      "Elves + 4x Deathrite Shaman + 12x Plains + 16x "
                      "Forest + 16x Swamp; P1: 4x each creature + 16x each "
                      "basic land",
        "contract_line": "Cast Duneblast, spare one chosen creature -> only "
                         "that creature survives; cast again choosing none "
                         "-> everything destroyed",
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
                            if ST["phase"].startswith("casting_db1"):
                                ST["db1_cast_rejections"] += 1
                            elif ST["phase"].startswith("casting_db2"):
                                ST["db2_cast_rejections"] += 1
                            elif ST["phase"] == "choice1":
                                ST["choice1_rejections"] += 1
                            elif ST["phase"] == "choice2":
                                ST["choice2_rejections"] += 1
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                ST["states_seen"] += 1
                acts = merged_actions(st)

                if tag == "P0":
                    # Duneblast #1 on the stack
                    if ST["phase"] == "casting_db1" and not ST["db1_seen_on_stack"]:
                        did = stack_spell_oid(state, DB)
                        if did is not None:
                            ST["db1_seen_on_stack"] = True
                            ST["db1_stack_oid"] = did
                            wire("db1_on_stack", {"oid": did,
                                                  "entries": stack_entries(state)})
                            say(f"Duneblast #1 on stack (oid {did}); "
                                f"exporting cast")
                            await export_as(c, "cast")
                            say("cast exported")
                    # Duneblast #1 resolved: gone from stack
                    if (ST["phase"] in ("casting_db1", "choice1")
                            and ST["db1_seen_on_stack"]
                            and not ST["db1_resolved"]):
                        did = stack_spell_oid(state, DB)
                        if did is None:
                            ST["db1_resolved"] = True
                            ST["db1_resolved_at"] = now
                            # baseline for the destruction comparison even
                            # if no choice export exists
                            ST["choose1_creatures"] = battlefield_creatures(state)
                            wire("db1_resolved", {})
                            say("Duneblast #1 resolved (left the stack)")
                    # Duneblast #2 on the stack
                    if ST["phase"] == "casting_db2" and not ST["db2_seen_on_stack"]:
                        did = stack_spell_oid(state, DB)
                        if did is not None:
                            ST["db2_seen_on_stack"] = True
                            wire("db2_on_stack", {"oid": did})
                            say(f"Duneblast #2 on stack (oid {did}); "
                                f"exporting cast2")
                            await export_as(c, "cast2")
                            say("cast2 exported")
                    # branch A settle: stack empty + Priority + idle, either
                    # after the choice was submitted or after a no-choice
                    # timeout (the engine resolved without offering it).
                    if (ST["phase"] in ("choice1", "casting_db1")
                            and ST["db1_resolved"]):
                        if (ST["choice1_submitted"]
                                or (ST.get("db1_resolved_at")
                                    and now - ST["db1_resolved_at"] > 45)):
                            if not stack_entries(state):
                                if ST["settle_at"] is None:
                                    ST["settle_at"] = now
                                idle = now - ST["settle_at"]
                                wtype = (wf_of(state).get("type") or "")
                                if wtype == "Priority" and idle > SETTLE_IDLE_S:
                                    if not ST["choice1_submitted"]:
                                        say("branch A: Duneblast resolved with "
                                            "NO choice offered in 45s -- "
                                            "recording as no-choice path")
                                        wire("no_choice_offered",
                                             {"branch": "1"})
                                    say(f"branch A settled: stack empty, "
                                        f"Priority, {idle:.0f}s idle; exporting post1")
                                    await export_as(c, "post1")
                                    ST["post1_exported"] = True
                                    ST["settle_at"] = None
                                    if ST["spare_oid"] is not None:
                                        p1env = json.load(open(f"{EVDIR}/post1.json"))
                                        p1st = p1env.get("state", {})
                                        sbf = get_obj(p1st, ST["spare_oid"]).get("zone")
                                        say(f"branch A quick probe: spare oid "
                                            f"{ST['spare_oid']} zone post1={sbf}")
                                    ST["phase"] = "resolving1_settled"
                                    wire("branchA_settled", {})
                    # branch B settle
                    if ST["phase"] in ("choice2", "casting_db2") and ST["choice2_submitted"]:
                        if not stack_entries(state):
                            if ST["settle_at"] is None:
                                ST["settle_at"] = now
                            idle = now - ST["settle_at"]
                            wtype = (wf_of(state).get("type") or "")
                            if wtype == "Priority" and idle > SETTLE_IDLE_S:
                                say(f"branch B settled; exporting post")
                                await export_as(c, "post")
                                ST["post_exported"] = True
                                finalized = True
                                break
                        else:
                            ST["settle_at"] = None
                    # watchdogs
                    if (ST["db1_submitted"] and not ST["db1_resolved"]
                            and ST.get("db1_submitted_at")
                            and now - ST["db1_submitted_at"] > 240
                            and not ST["post_exported"]
                            and not ST["post1_exported"]):
                        say("db1 watchdog: 240s after submission, never "
                            "resolved -- exporting stuck state")
                        wire("db1_stuck", {"waiting_for": wf_of(state)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if ((ST["phase"] in ("casting_db1", "choice1"))
                            and ST["db1_resolved"] and not ST["choice1_submitted"]
                            and not ST["post_exported"]):
                        pass  # choice may come after resolution begins; wait
                    if (ST["phase"] == "setup"
                            and not ST["db1_submitted"]
                            and now - t_start > 900
                            and not ST["post_exported"]):
                        say("setup watchdog: 900s in, Duneblast never "
                            "cast -- exporting state and finalizing")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if state.get("winner") is not None or state.get("game_over"):
                        say(f"game over detected "
                            f"(winner={state.get('winner')}) -- finalizing")
                        wire("game_over_state",
                             {"winner": state.get("winner"),
                              "state_keys": sorted(state.keys())})
                        if not ST["post_exported"]:
                            try:
                                await export_as(c, "post")
                                ST["post_exported"] = True
                            except Exception as e:
                                say(f"post export on game-over failed: {e}")
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7415.py",
                f"{EVDIR}/scenario_7415.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    'Duneblast: "choose up to one creature" is unparsed, so '
                    'Destroy reads an empty tracked set'],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
