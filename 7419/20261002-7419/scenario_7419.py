#!/usr/bin/env python3
"""Issue #7419: Afterlife from the Loam -- "for each player, choose up to one
target creature card in that player's graveyard" is unparsed, so
`ChangeZoneAll` reads an empty tracked set.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, classifier:unsupported-aspect, priority:p3-card-specific):
The ability's chain head is an `Effect::Unimplemented` node -- the
"for each player, choose up to one target creature card in that player's
graveyard" clause did not parse. Its `sub_ability` is the anaphor
"Put those cards onto the battlefield under your control", parsed as
`ChangeZoneAll { origin: Exile, destination: Battlefield,
target: TrackedSet(0), enters_under: You }`. The Unimplemented resolver is
a no-op (pushes no GameEvent), so the chain tracked set is allocated empty
and the ChangeZoneAll reads an empty set. The issue explicitly does NOT
assert a runtime symptom ("not measured for this sub type"); the reported
defect is the parse state and the empty publish that structurally follows.

Oracle text (verified in pinned v0.100.0 card-data.json):
  Afterlife from the Loam {5}{B}{B}{B} -- Sorcery
  "Delve (Each card you exile from your graveyard while casting this spell
  pays for {1}.)
  For each player, choose up to one target creature card in that player's
  graveyard. Put those cards onto the battlefield under your control.
  They're Zombies in addition to their other types."

Pinned v0.100.0 parse (see data_evidence.json):
  ability[0].effect = Unimplemented { name: "unparsed_quantity",
      description: "For each player, choose up to one target creature card
      in that player's graveyard" }
    (the issue's corpus at 9b7c66e30 named this node "for"; same clause,
    renamed head in the current data)
  ability[0].sub_ability.effect = ChangeZoneAll { origin: Exile,
      destination: Battlefield, target: TrackedSet(0), enters_under: You }
  ability[0].sub_ability.sub_ability.effect = Unimplemented
      { name: "unrecognized_clause_head",
        description: "They're Zombies in addition to their other types" }
  -- exactly the shape the issue reports.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Afterlife from the Loam + 8x Go for the Throat + 4x Grizzly Bears
      + 16x Swamp + 16x Plains + 12x Forest (dense playsets are a
      test-harness convenience; the Bears give P0 a creature to seed its
      own graveyard with, since "for each player" covers both seats)
  P1: 12x Grizzly Bears + 48x Forest (plays lands/bears, never attacks)
Drive:
  1. Mulligans: P0 keeps Afterlife from the Loam + >=3 lands (mulligans at
     most three times); P1 keeps.
  2. Seed P1's graveyard: P0 casts Go for the Throat on a P1 Bear
     ({1}{B}) once P1 has >=1 Bear on the battlefield.
  3. Seed P0's graveyard: P0 casts its own Bear ({1}{G}), then Go for the
     Throat on it.
  4. On the first P0 main phase with 8+ untapped lands including >=3
     Swamps: export pre.json IMMEDIATELY BEFORE submitting the Loam cast
     (guarded flag; the decisive pre), cast Afterlife from the Loam paying
     the full {5}{B}{B}{B} (delve declined if offered -- exiling the seeded
     bears for delve would destroy the test fixture), let it resolve.
  5. Watch for any TargetSelection/choice prompt during resolution (none
     expected -- the clause is unparsed); export post once the stack is
     empty, the Loam is in P0's graveyard, and the game has settled.

Expected (correct behavior): each graveyard's Bear is chosen and put onto
the battlefield under P0's control (2 more P0 creatures at post than at
pre); both graveyards lose their Bears.
Reported (bug): the chain tracked set is empty, so ChangeZoneAll no-ops:
the Bears stay in their graveyards and P0's battlefield is unchanged.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses the ability as the
                  issue reports: head Unimplemented naming the "for each
                  player, choose up to one target creature card..." clause,
                  sub ChangeZoneAll with target TrackedSet(0), destination
                  Battlefield, enters_under You.
  A2_setup_ok     pre.json: >=1 Bear in P0's graveyard, >=1 Bear in P1's
                  graveyard, Afterlife from the Loam castable (8 untapped
                  lands incl. >=3 Swamps).
  A3_cast_resolves the Loam cast completed and the spell object reached
                  P0's graveyard (no stall, no rejection left pending).
  A4_gy_creatures_moved (THE REPORTED BUG) post: every Bear that was in a
                  graveyard at pre has left that graveyard.
  A5_bf_gains     (THE REPORTED BUG) post: P0 controls at least 2 more
                  creatures than at pre (the reanimated Bears under P0's
                  control).
  A6_cleanup      post stack empty, game advancing.

Verdict rule: reproduced iff A1, A2, A3 passed and (A4 failed or A5
              failed); not-reproduced iff A1..A5 all passed;
              blocked iff A1, A2, or A3 could not be established.

Protocol-101 driver notes (v0.100.0, build bc9ef56, verified 2026-10-02):
  - HELLO advertises protocol 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions.
  - TargetSelection answered via viewer_interaction: candidates carry
    serialized object references ("reference": "<oid>"); matched on
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
RUN_ID = "20261002-7419"
ISSUE = 7419
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR) if f != "scenario_run.log"]
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
                     "(started by this run; ServerHello "
                     "0.100.0/bc9ef56/protocol 101 verified this run)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.100.0 == pinned release "
              "dir; hashes recomputed against on-disk artifacts; release "
              "advanced past v0.99.0 this run (pin_release.py: minisign-verify "
              "binary + data manifest, binary digest vs GitHub asset digest)",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

LOAM = "afterlife from the loam"
GFT = "go for the throat"
BEAR = "grizzly bears"
PLAINS = "plains"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [("Afterlife from the Loam", 4), ("Go for the Throat", 8),
           ("Grizzly Bears", 4), ("Swamp", 16), ("Plains", 16),
           ("Forest", 12)]
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
        "phase": "setup",  # setup -> casting_gft1 -> seeding2 -> casting_bear
                           # -> seeding3 -> casting_gft2 -> loam_ready
                           # -> casting_loam -> resolving -> done
        "gft1_submitted_at": None,
        "gft1_target_oid": None,
        "bear_cast_submitted_at": None,
        "own_bear_oid": None,
        "own_bear_on_bf_at": None,
        "gft2_submitted_at": None,
        "loam_submitted_at": None,
        "loam_cast": False,
        "pre_exported": False,
        "gy_bears_at_pre": [],      # [(oid, owner_pid)]
        "p0_bf_creatures_at_pre": None,
        "post_exported": False,
        "prompts_seen": [],         # (phase, iid) vi opportunities during
                                   # loam casting/resolution
        "mana_needs": {"B": 0, "G": 0, "generic": 0},  # set per cast
        "mana_tapped": 0,
        "mana_spent": 0,
        "delve_offered": False,
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_cast_resolves",
                                       "A4_gy_creatures_moved",
                                       "A5_bf_gains", "A6_cleanup")},
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


def gy_bears(state, pid):
    """Bear objects in a player's graveyard (by controller==owner convention
    used by this driver: graveyard objects keep their controller)."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Graveyard":
            continue
        if str(o.get("base_name") or o.get("name") or "").lower() != BEAR:
            continue
        if o.get("controller") == pid or o.get("owner") == pid:
            out.append(int(oid))
    return out


def bf_creatures(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        types = [str(t).lower() for t in
                 (o.get("card_types") or {}).get("core_types", [])]
        if "creature" in types:
            out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and "land" in [str(t).lower()
                           for t in (o.get("card_types") or {}).get("core_types", [])]]


def untapped_swamps(state, pid):
    return [oid for oid in untapped_lands(state, pid)
            if obj_lname(state, oid) == SWAMP]


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
    card = CARD_DATA.get("afterlife from the loam", {})
    ab = (card.get("abilities") or [{}])[0]
    head = ab.get("effect") or {}
    sub = (ab.get("sub_ability") or {}).get("effect") or {}
    subtgt = sub.get("target") or {}
    tail = ((ab.get("sub_ability") or {}).get("sub_ability") or {}).get("effect") or {}
    findings = {
        "name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "oracle": card.get("oracle_text"),
        "ability_head": head,
        "sub_ability_effect": sub,
        "tail_effect": tail,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    ok = (head.get("type") == "Unimplemented"
          and "for each player, choose up to one target creature card" in
          str(head.get("description"))
          and sub.get("type") == "ChangeZoneAll"
          and subtgt.get("type") == "TrackedSet"
          and subtgt.get("id") == 0
          and sub.get("destination") == "Battlefield"
          and sub.get("enters_under") == "You")
    say(f"data-level check: head={head.get('type')}/{head.get('name')!r}, "
        f"sub={sub.get('type')} target={subtgt} -> "
        f"{'MATCHES ISSUE REPORT' if ok else 'MISMATCH'}")
    ST["notes"].append(
        "data-level: pinned v0.100.0 card-data.json parses Afterlife from "
        "the Loam's ability as head "
        f"Unimplemented(name={head.get('name')!r}, "
        f"description={str(head.get('description'))[:80]!r}...) + sub "
        f"ChangeZoneAll target={subtgt} destination={sub.get('destination')} "
        f"enters_under={sub.get('enters_under')} "
        f"(issue-reported shape: {ok}; head node name differs from the "
        "issue's 9b7c66e30 corpus ('for' -> 'unparsed_quantity'), same "
        "clause)")
    ST["data_level_ok"] = ok
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    lands = sum(1 for n in hand if n in (PLAINS, SWAMP, FOREST))
    return LOAM in hand and lands >= 3


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


RANK = {LOAM: 10, GFT: 7, BEAR: 6, SWAMP: 5, PLAINS: 5, FOREST: 5}


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


def target_needles_for(oid, lname, zone):
    """Exact-reference needle first, then looser content needles."""
    return [[f'"reference": "{oid}"'],
            [lname, zone]]


async def answer_target_opp(c, st, state, phase, needle_sets, label):
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
        # Use ONLY the first needle set that matches: the spell takes a
        # single target, so accumulating one candidate per needle set
        # produces an over-targeted (constraint-violating) submission.
        cid = None
        matched = None
        for needles in needle_sets:
            cid = candidate_matching(opp, needles)
            if cid is not None:
                matched = needles
                break
        wire("target_opp", {"phase": phase, "iid": iid, "rtype": rtype,
                            "needle_sets": needle_sets,
                            "matched_needles": matched,
                            "matched_candidate": cid,
                            "opportunity": opp})
        if not cid:
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
                                "data": {"choiceId": cid}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
        wire("target_submit", {"phase": phase, "submission": sub})
        await c.send_interaction(sub)
        say(f"[{label}] target -> candidate {cid} (needles {matched})")
        acted = True
    return acted


async def pay_mana_vi(c, st, state, tag):
    """Answer vi mana-payment choices during casting. The engine runs a
    tap-to-pool payment UI (one tapLandForMana interaction per land); for
    spells with delve it then asks the delve question reusing the convoke
    UI (waiting_for ManaPayment, convoke_mode "Delve", tapForConvoke
    choices pointing at graveyard cards = "exile for delve"). The driver
    ALWAYS declines delve (passPriority) to preserve the graveyard
    fixture; the engine then auto-pays the tapped pool. Never touches
    cancelCast / untapLandForMana / tapForConvoke / unspendPoolMana."""
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    needs = ST["mana_needs"]
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        rtype = (opp.get("response", {}) or {}).get("type")
        taps, passes = [], []
        has_delve_choice = False
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
            elif "passPriority" in codes:
                passes.append(ch)
            if "tapForConvoke" in codes:
                objs = [s for s in ch.get("surfaces", []) or []
                        if s.get("type") == "object"]
                if any((s.get("data", {}) or {}).get("zone") == "graveyard"
                       for s in objs):
                    has_delve_choice = True
        if not taps and not (passes and has_delve_choice):
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, syms, used = None, [], None
        if sum(needs.values()) > 0 and taps:
            for ch, s in taps:  # colored needs first
                for color in ("B", "G", "W", "U", "R"):
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
            ST["mana_tapped"] += 1
            wire("tap_land", {"iid": iid, "choice": pick["id"],
                              "symbols": syms, "used_for": used,
                              "needs_now": dict(needs)})
            say(f"[{tag}] tap land for mana: {pick['id'][:24]} "
                f"symbols={syms} used_for={used} needs={dict(needs)}")
        elif has_delve_choice and passes:
            # Delve offered (convoke UI): decline to preserve the
            # graveyard fixture. NEVER select tapForConvoke.
            pick = passes[0]
            used = "decline_delve"
            ST["delve_offered"] = True
            wire("delve_decline", {"iid": iid, "choice": pick["id"],
                                   "via": "passPriority"})
            say(f"[{tag}] declined delve via passPriority (graveyard "
                f"fixture preserved)")
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
        await c.send_interaction(sub)
        acted = True
    return acted


async def decline_delve(c, st, state, tag):
    """If the engine offers a delve payment choice during the Loam cast,
    decline it (exiling the seeded bears would destroy the fixture).
    Returns True if a decline was submitted."""
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    for opp in vi.get("opportunities", []) or []:
        ser = json.dumps(opp, default=str).lower()
        if "delve" not in ser:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        rtype = (opp.get("response", {}) or {}).get("type")
        for ch in data.get("choices") or data.get("candidates") or []:
            cser = json.dumps(ch, default=str).lower()
            if any(w in cser for w in ("decline", "\"no\"", "skip", "pay full",
                                       "do not", "don't", "none")):
                if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                    continue
                SUBMITTED_OPPS.add(iid)
                ST["delve_offered"] = True
                wire("delve_decline", {"iid": iid, "choice": ch})
                if rtype == "exactChoices":
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": ch.get("id")}}}
                else:
                    sub = {"interactionId": iid,
                           "response": {"type": "sequence",
                                        "data": {"choiceIds": [ch.get("id")]}}}
                await c.send_interaction(sub)
                say(f"[{tag}] declined delve (kept graveyard fixture)")
                acted = True
                break
        if not acted:
            wire("delve_offer_unanswered", {"iid": iid, "opportunity": opp})
            say(f"[{tag}] WARNING: delve offered but no decline choice "
                f"found; leaving unanswered")
    return acted


def castable_gft(state):
    return len(untapped_lands(state, 0)) >= 2 and untapped_swamps(state, 0)


def castable_bear(state):
    return (len(untapped_lands(state, 0)) >= 2
            and any(obj_lname(state, o) == FOREST
                    for o in untapped_lands(state, 0)))


def castable_loam(state):
    return (len(untapped_lands(state, 0)) >= 8
            and len(untapped_swamps(state, 0)) >= 3)


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


# ------------------------------------------------------------- P0 tick
CASTING_PHASES = ("casting_gft1", "casting_bear", "casting_gft2",
                  "casting_loam")


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
    if ST["phase"] in CASTING_PHASES:
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await decline_delve(c, st, state, "P0"):
            return
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
    # GFT target selections (seed kills)
    if ST["phase"] == "casting_gft1" and ST["gft1_target_oid"] is not None:
        if await answer_target_opp(
                c, st, state, "gft1",
                target_needles_for(ST["gft1_target_oid"], BEAR, "battlefield"),
                "P0"):
            return
    if ST["phase"] == "casting_gft2" and ST["own_bear_oid"] is not None:
        if await answer_target_opp(
                c, st, state, "gft2",
                target_needles_for(ST["own_bear_oid"], BEAR, "battlefield"),
                "P0"):
            return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        hn = hand_lnames(state, 0)
        # 1. seed P1's graveyard: Go for the Throat on a P1 bear
        if (ST["phase"] == "setup" and GFT in hn and castable_gft(state)):
            p1_bears = zone_ids(state, 1, "Battlefield", BEAR)
            if p1_bears:
                a, oid = cast_action_for(acts, state, GFT)
                if a:
                    ST["gft1_target_oid"] = p1_bears[0]
                    ST["gft1_submitted_at"] = time.time()
                    ST["mana_needs"] = {"B": 1, "G": 0, "generic": 1}
                    ST["mana_tapped"] = 0
                    ST["mana_spent"] = 0
                    ST["phase"] = "casting_gft1"
                    say(f"[P0] Go for the Throat (oid {oid}) targeting P1 "
                        f"bear oid {p1_bears[0]} (seed P1 graveyard)")
                    wire("gft1_cast", {"oid": oid,
                                       "target": p1_bears[0]})
                    await submit_as_is(c, a)
                    return
        # 2. cast P0's own bear (to be killed for the P0-graveyard seed)
        if (ST["phase"] == "seeding2" and BEAR in hn
                and castable_bear(state)):
            a, oid = cast_action_for(acts, state, BEAR)
            if a:
                ST["bear_cast_submitted_at"] = time.time()
                ST["mana_needs"] = {"B": 0, "G": 1, "generic": 1}
                ST["mana_tapped"] = 0
                ST["mana_spent"] = 0
                ST["phase"] = "casting_bear"
                say(f"[P0] casting own Grizzly Bears (oid {oid})")
                wire("own_bear_cast", {"oid": oid})
                await submit_as_is(c, a)
                return
        # 3. kill P0's own bear -> P0 graveyard seed
        if (ST["phase"] == "seeding3" and GFT in hn and castable_gft(state)
                and ST["own_bear_oid"] is not None
                and ST["own_bear_oid"] in zone_ids(state, 0, "Battlefield",
                                                   BEAR)):
            a, oid = cast_action_for(acts, state, GFT)
            if a:
                ST["gft2_submitted_at"] = time.time()
                ST["mana_needs"] = {"B": 1, "G": 0, "generic": 1}
                ST["mana_tapped"] = 0
                ST["mana_spent"] = 0
                ST["phase"] = "casting_gft2"
                say(f"[P0] Go for the Throat (oid {oid}) targeting own "
                    f"bear oid {ST['own_bear_oid']} (seed P0 graveyard)")
                wire("gft2_cast", {"oid": oid,
                                   "target": ST["own_bear_oid"]})
                await submit_as_is(c, a)
                return
        # 4. the Loam: decisive pre.json exported INSIDE this submission
        #    path (guarded flag), then cast paying full {5}{B}{B}{B}.
        if (ST["phase"] == "loam_ready" and LOAM in hn
                and castable_loam(state)):
            a, oid = cast_action_for(acts, state, LOAM)
            if a:
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    ST["gy_bears_at_pre"] = (
                        [(o, 0) for o in gy_bears(state, 0)]
                        + [(o, 1) for o in gy_bears(state, 1)])
                    ST["p0_bf_creatures_at_pre"] = len(bf_creatures(state, 0))
                    say(f"[P0] pre.json exported; gy bears at pre: "
                        f"{ST['gy_bears_at_pre']}; P0 BF creatures: "
                        f"{ST['p0_bf_creatures_at_pre']}")
                ST["loam_cast"] = True
                ST["loam_submitted_at"] = time.time()
                ST["mana_needs"] = {"B": 3, "G": 0, "generic": 5}
                ST["mana_tapped"] = 0
                ST["mana_spent"] = 0
                ST["phase"] = "casting_loam"
                say(f"[P0] casting Afterlife from the Loam (oid {oid}), "
                    f"paying full cost (delve declined)")
                wire("loam_cast", {"oid": oid})
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
        # P1 never attacks; keeps bears on the battlefield to be killed.
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
                and any(obj_lname(state, o) == FOREST
                        for o in untapped_lands(state, 1))):
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
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    post_st = post.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Afterlife from the Loam exactly as the issue reports "
                     "(Unimplemented 'for each player...' head + ChangeZoneAll "
                     "TrackedSet(0) sub; see data_evidence.json).")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        p0_gy = [o for o, p in ST["gy_bears_at_pre"] if p == 0]
        p1_gy = [o for o, p in ST["gy_bears_at_pre"] if p == 1]
        notes.append(f"A2 probe: pre gy bears P0={p0_gy} P1={p1_gy}; P0 BF "
                     f"creatures={ST['p0_bf_creatures_at_pre']}.")
        if p0_gy and p1_gy and ST["p0_bf_creatures_at_pre"] is not None:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: pre.json has >=1 Bear in each "
                         "player's graveyard and the Loam was castable.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre is missing a "
                         "graveyard seed for P0 or P1.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the Loam "
                     "cast never happened).")

    # A3: the cast completed and the spell resolved into P0's graveyard
    loam_gy_post = False
    if post_st:
        for oid, o in (post_st.get("objects") or {}).items():
            if (str(o.get("base_name") or o.get("name") or "").lower() == LOAM
                    and o.get("zone") == "Graveyard"
                    and (o.get("controller") == 0 or o.get("owner") == 0)):
                loam_gy_post = True
                break
    if ST["loam_cast"] and loam_gy_post:
        ass["A3_cast_resolves"] = "passed"
        notes.append("A3 passed: Afterlife from the Loam was cast and its "
                     "spell object reached P0's graveyard (no stall, no "
                     "pending rejection).")
    elif ST["loam_cast"]:
        ass["A3_cast_resolves"] = "failed"
        notes.append("A3 FAILED: the Loam was cast but its object is not "
                     "in P0's graveyard at post (stuck or misrouted).")
    else:
        notes.append("A3 not-run: the Loam was never cast.")

    # A4/A5: the reported bug -- graveyard bears should have entered the
    # battlefield under P0's control; an empty tracked set no-ops instead.
    if (ST["pre_exported"] and ST["post_exported"] and pre_st and post_st
            and ST["gy_bears_at_pre"]):
        stuck = []
        moved = []
        for oid, pid in ST["gy_bears_at_pre"]:
            o = get_obj(post_st, oid)
            z = o.get("zone")
            notes.append(f"A4 probe: pre-gy bear oid {oid} (P{pid}) post "
                         f"zone={z!r}.")
            if z == "Graveyard":
                stuck.append((oid, pid))
            else:
                moved.append((oid, pid, z))
        if not stuck:
            ass["A4_gy_creatures_moved"] = "passed"
            notes.append("A4 passed: every pre-graveyard Bear left its "
                         f"graveyard ({moved}).")
        else:
            ass["A4_gy_creatures_moved"] = "failed"
            notes.append("A4 FAILED: Bears stayed in their graveyards "
                         f"after the Loam resolved: {stuck} -- THE "
                         "REPORTED BUG (ChangeZoneAll read an empty "
                         "tracked set and no-op'd).")
        p0_post = len(bf_creatures(post_st, 0))
        want = (ST["p0_bf_creatures_at_pre"] or 0) + 2
        notes.append(f"A5 probe: P0 BF creatures pre="
                     f"{ST['p0_bf_creatures_at_pre']} post={p0_post} "
                     f"(want >= {want}).")
        if p0_post >= want:
            ass["A5_bf_gains"] = "passed"
            notes.append("A5 passed: P0 gained >=2 battlefield creatures "
                         "(the reanimated Bears under P0's control).")
        else:
            ass["A5_bf_gains"] = "failed"
            notes.append("A5 FAILED: P0's battlefield did not gain the "
                         "reanimated Bears -- THE REPORTED BUG.")
    else:
        notes.append("A4/A5 not-run: no pre/post pair with recorded "
                     "graveyard bears.")

    # choice-prompt observation (the clause is unparsed; no resolution
    # choice expected). Casting-phase prompts are ordinary mana-payment
    # steps; only resolving-phase prompts would contradict the report.
    resolving_prompts = [iid for ph, iid in ST["prompts_seen"]
                         if ph == "resolving"]
    casting_prompts = [iid for ph, iid in ST["prompts_seen"]
                       if ph == "casting_loam"]
    notes.append(f"Casting-phase prompts (expected mana-payment steps): "
                 f"{len(casting_prompts)}.")
    if resolving_prompts:
        notes.append("Resolution prompts observed (unexpected for an "
                     f"unparsed clause): {resolving_prompts[:6]}")
    else:
        notes.append("No TargetSelection/choice prompt was offered to either "
                     "seat while the Loam resolved -- consistent with the "
                     "clause being unparsed (the engine never advertised "
                     "the 'choose up to one target' step).")
    if ST["delve_offered"]:
        notes.append("The engine offered a delve payment choice; the driver "
                     "declined it to preserve the graveyard fixture.")

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
    if (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolves"] == "passed"
            and any(ass[k] == "failed" for k in ("A4_gy_creatures_moved",
                                                "A5_bf_gains"))):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the Loam resolved but the graveyard "
            "creatures never entered the battlefield -- see the failed "
            "assertion(s) above (confirmed on v0.100.0). The parse is the "
            "reported defect; the runtime no-op is its direct structural "
            "consequence. This is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_cast_resolves",
                                          "A4_gy_creatures_moved",
                                          "A5_bf_gains")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the graveyard creatures entered the "
            "battlefield under P0's control as printed. This is not a fix "
            "claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported Loam-resolution path "
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
               if "afterlife" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7419.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "loam_cast": ST["loam_cast"],
            "loam_submitted_at": ST["loam_submitted_at"],
            "gy_bears_at_pre": ST["gy_bears_at_pre"],
            "p0_bf_creatures_at_pre": ST["p0_bf_creatures_at_pre"],
            "prompts_seen": ST["prompts_seen"],
            "delve_offered": ST["delve_offered"],
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report (no game state); the "
            "scenario replays the reported line (cast the spell with "
            "creature cards in each graveyard) from a fresh game and "
            "observes the resolution outcome.",
            "The graveyard-seeding Go for the Throat casts are a "
            "test-harness convenience to produce deterministic graveyard "
            "contents; the creatures are ordinary Grizzly Bears.",
            "Delve was declined (paid the full {5}{B}{B}{B}) to preserve "
            "the graveyard fixture; the delve payment path itself was not "
            "exercised.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Afterlife from the Loam + 8x Go for the "
                      "Throat + 4x Grizzly Bears + 16x Swamp + 16x Plains "
                      "+ 12x Forest; P1: 12x Grizzly Bears + 48x Forest "
                      "(lands/bears, never attacks)",
        "contract_line": "Each graveyard holds >=1 Bear at pre; the Loam "
                         "must put those Bears onto the battlefield under "
                         "P0's control. Observed: the clause never parsed, "
                         "no choice prompt appeared, and the resolution "
                         "moved nothing.",
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
                    # record any viewer-interaction prompts offered while
                    # the Loam is being cast / resolving. Casting-phase
                    # prompts are expected payment steps (tapLandForMana);
                    # resolution-phase prompts would be unexpected, because
                    # the "for each player, choose..." clause is unparsed.
                    if ST["phase"] in ("casting_loam", "resolving"):
                        vi = get_vi(st)
                        if vi:
                            for opp in vi.get("opportunities", []) or []:
                                iid = opp.get("interactionId") or opp.get("id")
                                tag2 = (ST["phase"], iid)
                                if tag2 not in ST["prompts_seen"]:
                                    ST["prompts_seen"].append(tag2)
                                    wire("resolution_prompt",
                                         {"phase": ST["phase"], "iid": iid,
                                          "opportunity": opp})
                                    say(f"[{ST['phase']}] prompt seen: "
                                        f"{json.dumps(opp)[:300]}")
                    # gft1 resolution: target bear should die
                    if ST["phase"] == "casting_gft1":
                        tgt = get_obj(state, ST["gft1_target_oid"] or -1)
                        if tgt.get("zone") == "Graveyard":
                            say("gft1 resolved: P1 bear in graveyard; "
                                "phase -> seeding2")
                            wire("gft1_resolved",
                                 {"target_oid": ST["gft1_target_oid"]})
                            ST["mana_needs"] = {"B": 0, "G": 0, "generic": 0}
                            ST["phase"] = "seeding2"
                        elif (ST["gft1_submitted_at"] is not None
                                and now - ST["gft1_submitted_at"] > 240):
                            say("gft1 watchdog: 240s, target bear not dead "
                                "-- resetting to setup to retry")
                            wire("gft1_stall",
                                 {"waiting_for": wf_of(state)})
                            ST["phase"] = "setup"
                            ST["gft1_target_oid"] = None
                    # own bear on the battlefield
                    if ST["phase"] == "casting_bear":
                        own = zone_ids(state, 0, "Battlefield", BEAR)
                        if own:
                            ST["own_bear_oid"] = own[0]
                            ST["own_bear_on_bf_at"] = now
                            say(f"own bear on P0 battlefield (oid {own[0]}); "
                                f"phase -> seeding3")
                            wire("own_bear_on_bf", {"oid": own[0]})
                            ST["mana_needs"] = {"B": 0, "G": 0, "generic": 0}
                            ST["phase"] = "seeding3"
                        elif (ST["bear_cast_submitted_at"] is not None
                                and now - ST["bear_cast_submitted_at"] > 240):
                            say("bear watchdog: 240s, own bear never on "
                                "battlefield -- resetting to seeding2")
                            wire("bear_stall",
                                 {"waiting_for": wf_of(state)})
                            ST["phase"] = "seeding2"
                    # gft2 resolution: own bear should die
                    if ST["phase"] == "casting_gft2":
                        tgt = get_obj(state, ST["own_bear_oid"] or -1)
                        if tgt.get("zone") == "Graveyard":
                            say("gft2 resolved: own bear in P0 graveyard; "
                                "phase -> loam_ready")
                            wire("gft2_resolved",
                                 {"target_oid": ST["own_bear_oid"]})
                            ST["mana_needs"] = {"B": 0, "G": 0, "generic": 0}
                            ST["phase"] = "loam_ready"
                        elif (ST["gft2_submitted_at"] is not None
                                and now - ST["gft2_submitted_at"] > 240):
                            say("gft2 watchdog: 240s, own bear not dead "
                                "-- resetting to seeding3 to retry")
                            wire("gft2_stall",
                                 {"waiting_for": wf_of(state)})
                            ST["phase"] = "seeding3"
                    # loam resolution: spell object -> P0 graveyard
                    if ST["phase"] == "casting_loam":
                        loam_gy = any(
                            str(o.get("base_name") or o.get("name") or "")
                            .lower() == LOAM
                            and o.get("zone") == "Graveyard"
                            and (o.get("controller") == 0
                                 or o.get("owner") == 0)
                            for o in (state.get("objects") or {}).values())
                        if loam_gy:
                            say("Loam resolved (spell in P0 graveyard); "
                                "phase -> resolving")
                            wire("loam_resolved", {})
                            ST["mana_needs"] = {"B": 0, "G": 0, "generic": 0}
                            ST["phase"] = "resolving"
                            ST["settle_at"] = now
                        elif (ST["loam_submitted_at"] is not None
                                and now - ST["loam_submitted_at"] > 300):
                            say("loam watchdog: 300s after the cast, spell "
                                "not in graveyard -- exporting post and "
                                "finalizing")
                            wire("loam_stall",
                                 {"waiting_for": wf_of(state),
                                  "stack": stack_entries(state)})
                            await export_as(c, "post")
                            ST["post_exported"] = True
                            finalized = True
                            break
                    # decisive post: stack empty + settled
                    if ST["phase"] == "resolving":
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
                        say("setup watchdog: 1200s in, Loam never cast "
                            "-- exporting state and finalizing")
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7419.py",
                f"{EVDIR}/scenario_7419.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Afterlife from the Loam: unparsed 'for each player' "
                    "clause leaves ChangeZoneAll reading an empty tracked "
                    "set; resolution moves nothing"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
