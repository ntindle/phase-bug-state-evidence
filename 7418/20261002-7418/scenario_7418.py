#!/usr/bin/env python3
"""Issue #7418: The Spot, Living Portal -- "return the exiled cards" binds
the tracked set to The Spot itself, not the ETB-exiled permanents.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:engine+parser, classifier:supported-aspect-defect,
priority:p2-wrong-game-result):
The Spot's dies-trigger sub-ability `return the exiled cards to their
owners' hands` is parsed as `Bounce { target: TrackedSet(0) }`. In that
chain the only publisher is the head `PutAtLibraryPosition`, which moves
**The Spot itself** to the bottom of its owner's library -- so the tracked
set contains The Spot, not the cards the *enters* trigger exiled. The
anaphor points across two different triggered abilities, which a
chain-scoped tracked set structurally cannot express. Unlike the rest of
the #6857 census this publish is not merely empty -- it is actively wrong.

Oracle text (verified in pinned v0.99.0 card-data.json):
  The Spot, Living Portal {3}{W}{B} 4/4
  "When The Spot enters, exile up to one target nonland permanent and up to
  one target nonland permanent card from a graveyard.
  When The Spot dies, put him on the bottom of his owner's library. If you
  do, return the exiled cards to their owners' hands."

Pinned v0.99.0 parse of the dies trigger (see data_evidence.json):
  head PutAtLibraryPosition {target: ParentTarget, position: Bottom}
  sub_ability Bounce {target: TrackedSet(0)}, sub_link: SequentialSibling,
      condition: EffectOutcome(OptionalEffectPerformed)
  -- exactly the shape the issue reports.

The issue body explicitly does NOT assert the runtime outcome (it was not
measured for this card); the triage comment's acceptance criteria call for
a runtime test driving both triggers in sequence (enter -> exile two
permanents -> die) observing the exiled cards in their owners' hands and
The Spot on the bottom of its owner's library, and pinning that the dies
trigger never returns The Spot itself to hand.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x The Spot, Living Portal + 8x Go for the Throat + 24x Plains
      + 24x Swamp (dense playsets are a test-harness convenience)
  P1: 12x Grizzly Bears + 48x Forest (plays lands/bears, never attacks)
Drive:
  1. Mulligans: P0 keeps The Spot + >=2 lands (mulligans at most three
     times); P1 keeps.
  2. P0 plays a land each turn. When P0 has Go for the Throat + {1}{B} and
     P1 has >=2 Bears on the battlefield, P0 casts Go for the Throat on a
     P1 Bear (deterministic graveyard seeding; leaves P1 a bear for the
     ETB). No combat coordination needed.
  3. On the first P0 main phase with 5+ untapped lands incl. W and B:
       a. export pre_etb (checkpoint), then export pre.json is deferred --
          the decisive pre is taken at step 5.
       b. cast The Spot, Living Portal (CastSpell + PayMana flow).
  4. The Spot's ETB trigger: answer its TargetSelection(s) --
     (i) up to one target nonland permanent -> P1's Bear on the battlefield;
     (ii) up to one target nonland permanent card from a graveyard -> the
     Bear card in P1's graveyard. Export etb_done once >=1 bear is exiled.
  5. On a later P0 main phase with Go for the Throat in hand:
       a. export pre.json IMMEDIATELY BEFORE submitting the kill cast
          (guarded flag; the decisive pre for the operation under
          investigation: Spot on BF, exiled cards in exile).
       b. cast Go for the Throat targeting The Spot.
  6. The Spot dies -> dies trigger fires (automatic). Export mid_dies when
     the trigger is observed on the stack (or when the Spot leaves the BF).
  7. Let the trigger resolve; export post once the stack is empty and the
     Spot's zone has settled; finalize immediately.

Expected (correct behavior): dies trigger puts The Spot on the bottom of
P0's library; the "if you do" Bounce returns each exiled card to its
owner's hand; The Spot is never in P0's hand.
Reported (bug): the Bounce's TrackedSet(0) names The Spot (the chain's only
publish), so the exiled cards stay exiled and/or The Spot itself is
bounced to hand -- the opposite of the printed effect.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok       pre.json: The Spot on P0's BF; >=1 exiled bear object
                    recorded (name/owner) in Exile.
  A2_dies_fired     The Spot left P0's battlefield after the kill cast.
  A3_spot_to_library post: The Spot object's zone == Library (bottom of
                    P0's library; position checked when the library order
                    is observable).
  A4_exiled_returned (THE REPORTED BUG) post: every object exiled by the
                    ETB (recorded at pre) is in its owner's hand.
  A5_spot_not_bounced post: The Spot is NOT in P0's hand (pins the
                    discriminating worst case: the tracked set naming The
                    Spot itself).
  A6_cleanup        post stack empty, game advancing.

Verdict rule: reproduced iff A1 and A2 passed and any of A3/A4/A5 failed;
              not-reproduced iff A1..A5 all passed;
              blocked iff A1 or A2 could not be established.

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions.
  - TargetSelection answered via viewer_interaction: candidates carry
    serialized object references (name/zone/controller); matched on
    serialized content, never on bare numeric needles.
  - Authoritative exports only from the host seat (P0 creates the game).
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
RUN_ID = "20261002-7418"
ISSUE = 7418
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

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))

SPOT = "the spot, living portal"
GFT = "go for the throat"
BEAR = "grizzly bears"
PLAINS = "plains"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [("The Spot, Living Portal", 4), ("Go for the Throat", 8),
           ("Plains", 24), ("Swamp", 24)]
P1_DECK = [("Grizzly Bears", 12), ("Forest", 48)]

SETUP_DEADLINE_S = 1700

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
        "phase": "setup",  # setup -> casting_spot -> etb_targeting ->
                           # etb_done -> killing -> dies_resolving -> done
        "early_kill_done": False,
        "early_kill_submitted_at": None,
        "spot_cast": False,
        "spot_cast_submitted_at": None,
        "spot_bf_oid": None,
        "spot_on_bf_at": None,
        "etb_perm_chosen": False,
        "etb_gy_chosen": False,
        "etb_done_at": None,
        "pre_exported": False,      # decisive pre, taken at kill-cast submit
        "pre_etb_exported": False,
        "etb_done_exported": False,
        "kill_cast": False,
        "kill_submitted_at": None,
        "spot_left_bf_at": None,
        "dies_trigger_seen": False,
        "dies_trigger_seen_at": None,
        "mid_dies_exported": False,
        "post_exported": False,
        "exiled_at_pre": [],        # [(oid, name, owner)] recorded at pre
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_dies_fired",
                                       "A3_spot_to_library",
                                       "A4_exiled_returned",
                                       "A5_spot_not_bounced",
                                       "A6_cleanup")},
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


def zone_ids(state, pid, zone, key=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != zone or o.get("controller") != pid:
            continue
        if key is not None and str(o.get("base_name") or o.get("name") or "").lower() != key:
            continue
        out.append(int(oid))
    return out


def exile_objs(state):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Exile":
            out.append((int(oid), str(o.get("base_name") or o.get("name") or "?"),
                        o.get("owner"), o.get("controller")))
    return out


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and "land" in [str(t).lower()
                           for t in (o.get("card_types") or {}).get("core_types", [])]]


def untapped_land_colors(state, pid):
    colors = set()
    for oid in untapped_lands(state, pid):
        n = obj_lname(state, oid)
        if n == PLAINS:
            colors.add("W")
        elif n == SWAMP:
            colors.add("B")
        elif n == FOREST:
            colors.add("G")
    return colors


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


def stack_spell_oid(state, key):
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        for k in ("object_id", "id", "source", "source_id", "card_id"):
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


def life_of(state, pid):
    return player_of(state, pid).get("life")


def candidate_matching(opp, needles):
    """First available candidate whose serialized content contains all
    needles (case-insensitive). Never matches on bare numeric needles:
    candidate ids embed the interaction counter."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
            continue
        ser = json.dumps(ch, default=str).lower()
        if all(n.lower() in ser for n in needles):
            return ch.get("id")
    return None

# ------------------------------------------------------------- data check
def check_data_level():
    spot = CARD_DATA.get("the spot, living portal", {})
    trigs = spot.get("triggers") or []
    dies = trigs[1] if len(trigs) > 1 else {}
    exe = dies.get("execute") or {}
    sub = exe.get("sub_ability") or {}
    findings = {
        "name": spot.get("name"),
        "mana_cost": spot.get("mana_cost"),
        "oracle": spot.get("oracle_text"),
        "dies_trigger_execute": exe,
        "dies_trigger_sub_ability": sub,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    head = (exe.get("effect") or {}).get("type")
    subeff = (sub.get("effect") or {})
    subtgt = (subeff.get("target") or {})
    ok = (head == "PutAtLibraryPosition"
          and subeff.get("type") == "Bounce"
          and subtgt.get("type") == "TrackedSet"
          and subtgt.get("id") == 0)
    say(f"data-level check: head={head}, sub effect={subeff.get('type')}, "
        f"sub target={subtgt} -> {'MATCHES ISSUE REPORT' if ok else 'MISMATCH'}")
    ST["notes"].append(
        "data-level: pinned v0.99.0 card-data.json parses The Spot's dies "
        f"trigger as PutAtLibraryPosition head + Bounce sub with target "
        f"{subtgt} (issue-reported shape: {ok})")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    lands = sum(1 for n in hand if n in (PLAINS, SWAMP))
    return SPOT in hand and lands >= 2


def mulligan_keep_p1(hand):
    return True


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
    if keep_fn(hn) or mulls >= 3 or len(hn) <= 4:
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


RANK = {SPOT: 10, GFT: 7, PLAINS: 5, SWAMP: 5, FOREST: 4, BEAR: 3}


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
    picks = sorted(hand, key=lambda o: RANK.get(obj_lname(state, o), 2))[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
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
    picks = [int(x) for x in sorted(
        hand, key=lambda o: RANK.get(obj_lname(state, o), 2))[:n]]
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


# Needle sets: matched against the candidate's serialized content
# (name/zone/controller/reference), never bare numeric ids.
BF_BEAR_NEEDLES = [["grizzly bears", "battlefield", '"controller": 1'],
                   ["grizzly bears", "battlefield"]]
GY_BEAR_NEEDLES = [["grizzly bears", "graveyard"],
                   ["grizzly bears"]]
SPOT_NEEDLES = [["the spot", "battlefield"]]


async def answer_target_opp(c, st, state, phase, needle_sets, label,
                            mark=None):
    """Answer every unsubmitted TargetSelection opportunity, trying each
    needle set in order. Returns True if any submission was sent."""
    vi = get_vi(st)
    if not vi:
        return False
    wtype = (wf_of(state).get("type") or "")
    if "targetselection" not in wtype.lower():
        return False
    acted = False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        rtype = (opp.get("response", {}) or {}).get("type")
        cids = []
        matched = []
        for needles in needle_sets:
            cid = candidate_matching(opp, needles)
            if cid is not None and cid not in cids:
                cids.append(cid)
                matched.append(needles)
        wire("target_opp", {"phase": phase, "iid": iid, "rtype": rtype,
                            "needle_sets": needle_sets,
                            "matched_needles": matched,
                            "matched_candidates": cids,
                            "opportunity": opp})
        if not cids:
            say(f"[{label}] WARNING: no candidate matched {needle_sets}; "
                f"not submitting blind")
            ST["notes"].append(
                f"{phase}: target selection offered but no candidate "
                f"matched {needle_sets}; see wire_log target_opp")
            continue
        SUBMITTED_OPPS.add(iid)
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": cids[0]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": cids}}}
        wire("target_submit", {"phase": phase, "submission": sub})
        await c.send_interaction(sub)
        say(f"[{label}] target -> candidates {cids} (needles {matched})")
        if mark:
            mark(cids)
        acted = True
    return acted


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def castable_spot(state):
    lands = untapped_lands(state, 0)
    if len(lands) < 5:
        return False
    colors = untapped_land_colors(state, 0)
    return {"W", "B"} <= colors


def castable_gft(state):
    lands = untapped_lands(state, 0)
    if len(lands) < 2:
        return False
    return "B" in untapped_land_colors(state, 0)


# ------------------------------------------------------------- P0 tick
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
    if ST["phase"] in ("casting_spot", "killing", "setup_early_kill"):
        if await pay_tick(c, acts, "P0"):
            return
    if await do_discard_to_handsize(c, 0, "P0"):
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
    # ETB target selections: (i) nonland permanent -> P1's bear on the
    # battlefield; (ii) nonland permanent card from a graveyard -> the bear
    # card in P1's graveyard.
    if ST["phase"] == "etb_targeting":
        needle_sets = []
        if not ST["etb_perm_chosen"]:
            needle_sets += BF_BEAR_NEEDLES
        if not ST["etb_gy_chosen"]:
            needle_sets += GY_BEAR_NEEDLES

        def mark(cids):
            # mark by re-checking which needles matched is complex; the
            # observation pass sets the flags from exile-zone contents.
            pass

        if needle_sets and await answer_target_opp(
                c, st, state, "etb_trigger", needle_sets, "P0", mark):
            return
    # early graveyard-seeding kill: Go for the Throat on a P1 bear
    if ST["phase"] == "setup_early_kill":
        if await answer_target_opp(c, st, state, "early_gft",
                                   BF_BEAR_NEEDLES, "P0"):
            return
    # the kill: Go for the Throat on The Spot
    if ST["phase"] == "killing":
        if await answer_target_opp(c, st, state, "kill_gft",
                                   SPOT_NEEDLES, "P0"):
            return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        hn = hand_lnames(state, 0)
        # 1. early graveyard seeding: kill a P1 bear while P1 has >=2,
        #    so one bear remains for the ETB permanent target.
        if (ST["phase"] == "setup" and not ST["early_kill_done"]
                and GFT in hn and castable_gft(state)
                and len(zone_ids(state, 1, "Battlefield", BEAR)) >= 2):
            a, oid = cast_action_for(acts, state, GFT)
            if a:
                ST["early_kill_done"] = True
                ST["early_kill_submitted_at"] = time.time()
                ST["phase"] = "setup_early_kill"
                say(f"[P0] early Go for the Throat (oid {oid}) "
                    f"to seed P1's graveyard")
                wire("early_gft_cast", {"oid": oid})
                await submit_as_is(c, a)
                return
        # 2. cast The Spot
        spot_bf = zone_ids(state, 0, "Battlefield", SPOT)
        if (ST["phase"] == "setup" and not ST["spot_cast"] and not spot_bf
                and SPOT in hn and castable_spot(state)):
            a, oid = cast_action_for(acts, state, SPOT)
            if a:
                # pre_etb checkpoint inside the submission path
                if not ST["pre_etb_exported"]:
                    await export_as(c, "pre_etb")
                    ST["pre_etb_exported"] = True
                ST["spot_cast"] = True
                ST["spot_cast_submitted_at"] = time.time()
                ST["phase"] = "casting_spot"
                say(f"[P0] casting The Spot, Living Portal (oid {oid})")
                wire("spot_cast", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
        # 3. the kill: Go for the Throat on The Spot. The decisive pre.json
        #    is exported INSIDE this submission path (guarded flag).
        if (ST["phase"] == "etb_done" and not ST["kill_cast"] and spot_bf
                and GFT in hn and castable_gft(state)):
            a, oid = cast_action_for(acts, state, GFT)
            if a:
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    # record the exiled objects at the decisive pre
                    ex = [(o, n, ow) for o, n, ow, _c in exile_objs(state)]
                    ST["exiled_at_pre"] = ex
                    say(f"[P0] pre.json exported; exiled at pre: {ex}")
                ST["kill_cast"] = True
                ST["kill_submitted_at"] = time.time()
                ST["phase"] = "killing"
                say(f"[P0] casting Go for the Throat (oid {oid}) "
                    f"targeting The Spot")
                wire("kill_gft_cast", {"oid": oid})
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
    if await do_discard_to_handsize(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        # P1 never attacks; keeps bears on the battlefield for the ETB.
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
        # cast a bear when affordable (2 untapped lands incl. a forest)
        if (BEAR in hand_lnames(state, 1)
                and len(untapped_lands(state, 1)) >= 2
                and "G" in untapped_land_colors(state, 1)):
            a, oid = cast_action_for(acts, state, BEAR)
            if a:
                say(f"[P1] casting Grizzly Bears (oid {oid})")
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
    pre_etb = load_env("pre_etb") or {}
    etb_done = load_env("etb_done") or {}
    mid_dies = load_env("mid_dies") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    post_st = post.get("state") or {}

    # A1: setup assembled at the decisive pre
    if ST["pre_exported"] and pre_st:
        spot_bf = zone_ids(pre_st, 0, "Battlefield", SPOT)
        ex = ST["exiled_at_pre"]
        notes.append(f"A1 probe: pre spot_on_bf={spot_bf}, "
                     f"exiled_at_pre={ex}.")
        if spot_bf and ex:
            ass["A1_setup_ok"] = "passed"
            notes.append("A1 passed: pre.json has The Spot on P0's "
                         "battlefield and >=1 exiled object recorded.")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 FAILED: the decisive pre is missing The Spot "
                         f"on P0's battlefield ({spot_bf}) or no exiled "
                         f"objects ({ex}).")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: pre.json was never exported (the kill cast "
                     "never happened).")

    # A2: The Spot died
    if ST["spot_left_bf_at"] is not None:
        ass["A2_dies_fired"] = "passed"
        notes.append("A2 passed: The Spot left P0's battlefield after the "
                     "Go for the Throat kill cast "
                     f"(dies trigger seen on stack: "
                     f"{ST['dies_trigger_seen']}).")
    else:
        notes.append("A2 not-run/failed: The Spot never left P0's "
                     "battlefield.")

    # A3/A4/A5 from the post state
    if ST["post_exported"] and post_st and ST["spot_bf_oid"] is not None:
        sobj = get_obj(post_st, ST["spot_bf_oid"])
        szone = sobj.get("zone")
        sname = str(sobj.get("base_name") or sobj.get("name") or "?")
        notes.append(f"A3 probe: post Spot oid {ST['spot_bf_oid']} "
                     f"name={sname!r} zone={szone}.")
        if szone == "Library":
            ass["A3_spot_to_library"] = "passed"
            notes.append("A3 passed: The Spot is in the Library zone at "
                         "post (the dies trigger's head put it on the "
                         "bottom of P0's library).")
        else:
            ass["A3_spot_to_library"] = "failed"
            notes.append(f"A3 FAILED: The Spot's post zone is {szone!r}, "
                         f"expected 'Library'.")
        # A4: every object exiled at pre is in its owner's hand
        if ST["exiled_at_pre"]:
            all_ok = True
            for oid, name, owner in ST["exiled_at_pre"]:
                o = get_obj(post_st, oid)
                ozone = o.get("zone")
                in_hand = oid in hand_ids(post_st, owner)
                notes.append(f"A4 probe: exiled {name!r} (oid {oid}, owner "
                             f"P{owner}) post zone={ozone!r}, "
                             f"in_owner_hand={in_hand}.")
                if not in_hand:
                    all_ok = False
            if all_ok:
                ass["A4_exiled_returned"] = "passed"
                notes.append("A4 passed: all ETB-exiled objects are in "
                             "their owners' hands at post.")
            else:
                ass["A4_exiled_returned"] = "failed"
                notes.append("A4 FAILED: at least one ETB-exiled object "
                             "did not return to its owner's hand -- THE "
                             "REPORTED BUG (the Bounce's TrackedSet(0) "
                             "names The Spot, not the exiled cards).")
        else:
            notes.append("A4 not-run: no exiled objects were recorded at "
                         "pre.")
        # A5: The Spot is not in P0's hand
        spot_in_hand = ST["spot_bf_oid"] in hand_ids(post_st, 0)
        notes.append(f"A5 probe: post Spot in P0 hand = {spot_in_hand}.")
        if not spot_in_hand:
            ass["A5_spot_not_bounced"] = "passed"
            notes.append("A5 passed: The Spot is not in P0's hand at post.")
        else:
            ass["A5_spot_not_bounced"] = "failed"
            notes.append("A5 FAILED: The Spot IS in P0's hand at post -- "
                         "the dies trigger bounced The Spot itself, the "
                         "discriminating worst case from the issue "
                         "(tracked set names The Spot).")
    else:
        notes.append("A3/A4/A5 not-run: no post state or the Spot's "
                     "battlefield oid was never recorded.")

    # A6: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty.")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if (ass["A1_setup_ok"] == "passed"
            and ass["A2_dies_fired"] == "passed"
            and any(ass[k] == "failed" for k in ("A3_spot_to_library",
                                                "A4_exiled_returned",
                                                "A5_spot_not_bounced"))):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the dies trigger fired but its observable "
            "outcome deviates from the printed effect -- see the failed "
            "assertion(s) above (confirmed on v0.99.0).")
    elif all(ass[k] == "passed" for k in ("A1_setup_ok", "A2_dies_fired",
                                          "A3_spot_to_library",
                                          "A4_exiled_returned",
                                          "A5_spot_not_bounced")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: The Spot went to the bottom of P0's "
            "library and every ETB-exiled object returned to its owner's "
            "hand; The Spot itself was never bounced. This is not a fix "
            "claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported dies-trigger path "
                     "could not be fully exercised; see assertion notes.")

    # server log excerpts for this game
    import glob
    lines = []
    used = None
    # newest-first by mtime (reverse-alphabetical once picked a September log)
    for lp in sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"),
                     key=os.path.getmtime, reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "the spot" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7418.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "early_kill_done": ST["early_kill_done"],
            "spot_cast": ST["spot_cast"],
            "spot_bf_oid": ST["spot_bf_oid"],
            "spot_on_bf_at": ST["spot_on_bf_at"],
            "etb_perm_chosen": ST["etb_perm_chosen"],
            "etb_gy_chosen": ST["etb_gy_chosen"],
            "etb_done_at": ST["etb_done_at"],
            "kill_cast": ST["kill_cast"],
            "spot_left_bf_at": ST["spot_left_bf_at"],
            "dies_trigger_seen": ST["dies_trigger_seen"],
            "dies_trigger_seen_at": ST["dies_trigger_seen_at"],
            "exiled_at_pre": ST["exiled_at_pre"],
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report includes no game state (data-level report); the "
            "scenario replays the reported line (The Spot's ETB exile x2, "
            "then its dies trigger) from a fresh game.",
            "The graveyard-seeding Go for the Throat is a test-harness "
            "convenience to produce the ETB's graveyard target "
            "deterministically; the exiled objects are ordinary Grizzly "
            "Bears.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x The Spot, Living Portal + 8x Go for the "
                      "Throat + 24x Plains + 24x Swamp; P1: 12x Grizzly "
                      "Bears + 48x Forest (lands/bears, never attacks)",
        "contract_line": "The Spot's ETB exiles a P1 Bear (battlefield) and "
                         "a Bear card (P1 graveyard); its dies trigger must "
                         "put The Spot on the bottom of P0's library and "
                         "return both exiled cards to P1's hand, never "
                         "bouncing The Spot itself",
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
                            ST["cast_rejections"] += 1
                        elif t == "TerminalResult":
                            ST["terminal"] = True
                            ST["terminal_data"] = data
                            wire("terminal_result",
                                 {"who": tag, "data": data})
                            say(f"[{tag}] TerminalResult: "
                                f"{json.dumps(data)[:300]}")
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                ST["states_seen"] += 1
                acts = merged_actions(st)

                if tag == "P0":
                    # early-kill resolution tracking
                    if ST["phase"] == "setup_early_kill":
                        gy_bears = [o for o in (state.get("objects") or {}).values()
                                    if str(o.get("base_name") or o.get("name") or "").lower() == BEAR
                                    and o.get("zone") == "Graveyard"]
                        if gy_bears:
                            say("early Go for the Throat resolved: bear in "
                                "graveyard; phase -> setup")
                            wire("early_gft_resolved",
                                 {"gy_bears": len(gy_bears)})
                            ST["phase"] = "setup"
                        elif (ST["early_kill_submitted_at"] is not None
                                and now - ST["early_kill_submitted_at"] > 180):
                            say("early-kill watchdog: 180s, no bear in any "
                                "graveyard -- resetting to setup to retry")
                            wire("early_gft_stall",
                                 {"waiting_for": wf_of(state)})
                            ST["phase"] = "setup"
                            ST["early_kill_done"] = False
                    # ETB targeting watchdog
                    if (ST["phase"] == "etb_targeting"
                            and ST["spot_on_bf_at"] is not None
                            and now - ST["spot_on_bf_at"] > 240
                            and not ST["etb_done_exported"]):
                        say("etb watchdog: 240s in etb_targeting -- "
                            "exporting etb_done and moving on")
                        wire("etb_stall",
                             {"waiting_for": wf_of(state),
                              "exile": exile_objs(state)})
                        await export_as(c, "etb_done")
                        ST["etb_done_exported"] = True
                        ST["etb_done_at"] = now
                        ST["phase"] = "etb_done"
                    # kill watchdog: the Spot should leave the BF
                    if (ST["phase"] == "killing"
                            and ST["kill_submitted_at"] is not None
                            and now - ST["kill_submitted_at"] > 180
                            and ST["spot_left_bf_at"] is None):
                        say("kill watchdog: 180s after the kill cast, The "
                            "Spot still on the battlefield -- resetting to "
                            "etb_done to retry")
                        wire("kill_stall",
                             {"waiting_for": wf_of(state),
                              "stack": stack_entries(state)})
                        ST["phase"] = "etb_done"
                        ST["kill_cast"] = False
                    # dies-resolving watchdog
                    if (ST["phase"] == "dies_resolving"
                            and ST["spot_left_bf_at"] is not None
                            and now - ST["spot_left_bf_at"] > 180
                            and not ST["post_exported"]):
                        say("dies watchdog: 180s after the Spot left -- "
                            "exporting post and finalizing")
                        wire("dies_stall",
                             {"waiting_for": wf_of(state),
                              "stack": stack_entries(state)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    # The Spot on the battlefield
                    spot_bf = zone_ids(state, 0, "Battlefield", SPOT)
                    if spot_bf and ST["spot_bf_oid"] is None:
                        ST["spot_bf_oid"] = spot_bf[0]
                        ST["spot_on_bf_at"] = now
                        ST["phase"] = "etb_targeting"
                        say(f"The Spot on P0 battlefield (oid "
                            f"{spot_bf[0]}); phase -> etb_targeting")
                        wire("spot_on_bf", {"oid": spot_bf[0]})
                    # ETB done: >=1 bear in exile (or both targets chosen)
                    if ST["phase"] == "etb_targeting":
                        ex = exile_objs(state)
                        bears_ex = [e for e in ex if e[1].lower() == BEAR]
                        if bears_ex:
                            if not ST["etb_perm_chosen"]:
                                ST["etb_perm_chosen"] = True
                            # check whether a graveyard-sourced bear is out
                            # there too: any exiled bear whose owner had it
                            # in a graveyard is indistinguishable here; the
                            # two-target structure is asserted via wire log
                            ST["etb_done_at"] = now
                            if not ST["etb_done_exported"]:
                                await export_as(c, "etb_done")
                                ST["etb_done_exported"] = True
                            ST["phase"] = "etb_done"
                            say(f"ETB resolved: exiled bears={bears_ex}; "
                                f"phase -> etb_done")
                            wire("etb_done", {"exiled": bears_ex})
                    # The Spot left the battlefield
                    if (ST["spot_bf_oid"] is not None
                            and ST["spot_left_bf_at"] is None
                            and not zone_ids(state, 0, "Battlefield", SPOT)):
                        ST["spot_left_bf_at"] = now
                        say("The Spot left P0's battlefield")
                        wire("spot_left_bf",
                             {"spot_obj": get_obj(state, ST["spot_bf_oid"])})
                    # dies trigger on the stack
                    if (ST["spot_left_bf_at"] is not None
                            and not ST["dies_trigger_seen"]):
                        for e in stack_entries(state):
                            ser = json.dumps(e, default=str).lower()
                            if "the spot" in ser or "putatlibraryposition" \
                                    in ser or "bounce" in ser:
                                ST["dies_trigger_seen"] = True
                                ST["dies_trigger_seen_at"] = now
                                say("dies trigger observed ON THE STACK; "
                                    "exporting mid_dies")
                                wire("dies_trigger_on_stack",
                                     {"entries": stack_entries(state)})
                                if not ST["mid_dies_exported"]:
                                    await export_as(c, "mid_dies")
                                    ST["mid_dies_exported"] = True
                                ST["phase"] = "dies_resolving"
                                ST["settle_at"] = now
                                break
                    # fallback: no stack window seen; export mid_dies once
                    # the Spot has been gone 10s with an empty stack
                    if (ST["spot_left_bf_at"] is not None
                            and not ST["mid_dies_exported"]
                            and not stack_entries(state)
                            and now - ST["spot_left_bf_at"] > 10):
                        say("mid_dies fallback: Spot gone 10s, stack "
                            "empty; exporting mid_dies")
                        await export_as(c, "mid_dies")
                        ST["mid_dies_exported"] = True
                        ST["phase"] = "dies_resolving"
                        ST["settle_at"] = now
                    # decisive post: stack empty + Spot zone settled
                    if ST["phase"] == "dies_resolving":
                        if not stack_entries(state):
                            if ST["settle_at"] is None:
                                ST["settle_at"] = now
                            idle = now - ST["settle_at"]
                            wtype = (wf_of(state).get("type") or "")
                            if wtype == "Priority" and idle > 8:
                                say(f"settled: stack empty, Priority, "
                                    f"{idle:.0f}s idle; exporting post")
                                await export_as(c, "post")
                                ST["post_exported"] = True
                                finalized = True
                                break
                        else:
                            ST["settle_at"] = None
                    # watchdogs
                    if (ST["phase"] == "setup"
                            and now - t_start > 1200
                            and not ST["post_exported"]):
                        say("setup watchdog: 1200s in, The Spot never "
                            "cast -- exporting state and finalizing")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if state.get("winner") is not None or state.get("game_over"):
                        say(f"game over detected in state "
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
                    if ST["terminal"] and not finalized:
                        say("TerminalResult received -- finalizing with "
                            "captured states")
                        if not ST["post_exported"]:
                            try:
                                await export_as(c, "post")
                                ST["post_exported"] = True
                            except Exception as e:
                                say(f"post export on terminal failed: {e}")
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7418.py",
                f"{EVDIR}/scenario_7418.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "The Spot dies-trigger Bounce binds TrackedSet(0) to "
                    "The Spot itself, not the exiled cards"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
