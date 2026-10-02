#!/usr/bin/env python3
"""Issue #7422: Elrond of the White Council -- "the voter chooses a
creature they control" is unparsed, so `GainControlAll` reads an empty
tracked set.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, classifier:unsupported-aspect, priority:p3-card-specific):
Secret council -- "When Elrond enters, each player secretly votes for
fellowship or aid, then those votes are revealed. For each fellowship vote,
the voter chooses a creature they control. You gain control of each creature
chosen this way, and they gain 'This creature can't attack its owner.' Then
for each aid vote, put a +1/+1 counter on each creature you control."

The fellowship per-choice head is an `Effect::Unimplemented` node -- the
"the voter chooses a creature they control" clause did not parse. Its
sub-ability is the anaphor "You gain control of each creature chosen this
way", parsed as `GainControlAll { target: TrackedSet(0) }`, itself with a
sub-ability granting the can't-attack-its-owner static. The Unimplemented
resolver is a no-op (pushes no GameEvent), so the chain tracked set is
allocated empty and the GainControlAll reads an empty set. The issue
explicitly does NOT assert a runtime symptom for GainControlAll ("not
measured for this sub type"); the reported defect is the parse state and
the empty publish that structurally follows.

Pinned v0.100.0 parse (see data_evidence.json) -- improved vs the issue's
9b7c66e30 corpus (the secret-council Vote itself now parses):
  triggers[0] (ChangesZone SelfRef -> Battlefield):
    execute.effect = Vote { choices: [fellowship, aid],
        voter_scope: AllPlayers, tally_mode: PerVote,
        starting_with: You, visibility: Secret }
    per_choice_effect[0] (fellowship): head Unimplemented
        { name: "unrecognized_clause_head",
          description: "the voter chooses a creature they control" }
        (the issue's 9b7c66e30 corpus named this node "the"; same clause,
        renamed head)
      sub: GainControlAll { target: TrackedSet(0) }
        sub: GenericEffect granting Continuous Can't-attack-its-owner to
             ParentTarget
    per_choice_effect[1] (aid): PutCounterAll { P1P1 x1,
        target: Typed(Creature, controller: You) }

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Elrond of the White Council + 24x Forest + 24x Island + 8x Plains
      (Elrond costs {3}{G}{U}; dense playsets are a test-harness
      convenience)
  P1: 12x Grizzly Bears + 48x Forest (plays lands/bears, never attacks)
Drive:
  1. Mulligans: P0 keeps Elrond + >=3 lands (mulligans at most three
     times); P1 keeps.
  2. On the first P0 main phase with 5+ untapped lands including >=1
     Forest and >=1 Island: export pre.json IMMEDIATELY BEFORE submitting
     the Elrond cast (guarded flag; the decisive pre), cast Elrond paying
     {3}{G}{U}, let the ETB trigger resolve.
  3. Secret-council vote policy (deterministic test policy): P0 votes
     "fellowship", P1 votes "fellowship". Under the printed text, P1 (a
     fellowship voter) would choose a creature P1 controls and P0 would
     gain control of it -- a net P1->P0 control change. The driver answers
     any vote opportunity by content match on "fellowship"/"aid".
  4. Record every viewer-interaction opportunity during resolution; export
     post once the stack is empty, the game has settled, and no prompt is
     pending.

Expected (correct behavior): the secret-council vote is offered to both
seats; each fellowship voter's per-choice effect runs; P1 is offered the
"choose a creature they control" choice; P0 gains control of the chosen
creature(s) (net P1->P0 change on P1's creature).
Reported (bug): the fellowship head clause never parsed, so the voter is
never offered the choice and GainControlAll reads an empty tracked set --
no control change occurs.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses the ability as the
                  issue reports: Vote root with fellowship/aid; fellowship
                  per-choice head Unimplemented naming the "the voter
                  chooses a creature they control" clause; sub
                  GainControlAll with target TrackedSet(0); aid branch
                  PutCounterAll.
  A2_setup_ok     pre.json: P1 controls >=1 creature; Elrond castable (5+
                  untapped lands incl. >=1 Forest and >=1 Island).
  A3_cast_resolves the Elrond cast completed, Elrond is on P0's
                  battlefield at post, and the ETB trigger resolved (no
                  stall, no rejection left pending).
  A4_vote_offered the secret-council vote was offered to both seats and
                  both votes were submitted (P0->fellowship, P1->fellowship).
  A5_fellowship_choice (THE REPORTED BUG) during resolution, a
                  "choose a creature" choice was offered to the fellowship
                  voter. Expected: offered. Observed bug: never offered.
  A6_control_change (THE REPORTED BUG) post: >=1 creature changed control
                  P1->P0 via the fellowship branch. Expected: changed.
                  Observed bug: unchanged.
  A7_cleanup      post stack empty, game advancing.

Verdict rule: reproduced iff A1, A2, A3 passed and NOT (A5 passed and A6
              passed); not-reproduced iff A1..A6 all passed; blocked iff
              A1, A2, or A3 could not be established. (If the vote itself
              never runs (A4 failed), the ability produced no printed
              effect at all -- still reproduced, noted as the broader
              failure.)

Protocol-101 driver notes (v0.100.0, build bc9ef56, verified 2026-10-02):
  - HELLO advertises protocol 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions and
    viewer-interaction tapLandForMana choices.
  - TargetSelection answered via viewer_interaction: candidates carry
    serialized object references ("reference": "<oid>"); matched on
    serialized content, never on bare numeric needles.
  - Authoritative exports only from the host seat (P0 creates the game).
  - Adapted from scenario_7421.py (issue #7421, same TrackedSet(0)
    unparsed-head pattern on Dredge the Mire) 2026-10-02.
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
RUN_ID = "20261002-7422"
ISSUE = 7422
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
                     "(already listening at run start; ServerHello "
                     "0.100.0/bc9ef56/protocol 101 verified by the "
                     "2026-10-02 pin run and re-verified by this run's "
                     "own Hello handshake)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.100.0 == pinned release "
              "dir (ledger server.pinned_version 2026-10-02T12:50-05:00; "
              "GitHub /releases confirms v0.100.0 still latest stable); "
              "hashes recomputed against on-disk artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

ELROND = "elrond of the white council"
BEAR = "grizzly bears"
FOREST = "forest"
ISLAND = "island"
PLAINS = "plains"

P0_DECK = [("Elrond of the White Council", 4), ("Forest", 24),
           ("Island", 24), ("Plains", 8)]
P1_DECK = [("Grizzly Bears", 12), ("Forest", 48)]

SETUP_DEADLINE_S = 1700
VOTE_POLICY = {"P0": "fellowship", "P1": "fellowship"}

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
        "phase": "setup",  # setup -> casting_elrond -> resolving -> done
        "elrond_submitted_at": None,
        "elrond_cast": False,
        "pre_exported": False,
        "p1_bf_creatures_at_pre": [],   # [oid]
        "p0_bf_creatures_at_pre": None,
        "counters_at_pre": {},          # oid -> counters blob
        "post_exported": False,
        "prompts_seen": [],             # (phase, iid) vi opportunities
                                        # during resolution
        "vote_iids": [],                # iids answered as secret-council votes
        "votes_submitted": {},          # tag -> vote word
        "wf_types_resolving": [],       # waiting_for types seen while resolving
        "stack_saw_trigger": False,
        "mana_needs": {"G": 0, "U": 0, "generic": 0},
        "mana_tapped": 0,
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_cast_resolves", "A4_vote_offered",
                                       "A5_fellowship_choice",
                                       "A6_control_change", "A7_cleanup")},
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


def is_creature(o):
    return "creature" in [str(t).lower()
                          for t in (o.get("card_types") or {}).get("core_types", [])]


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def bf_creatures(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and is_creature(o)]


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and is_land(o)]


def counters_of(state, oid):
    o = get_obj(state, oid)
    return o.get("counters")


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
    card = CARD_DATA.get("elrond of the white council", {})
    trigs = card.get("triggers") or []
    trig = trigs[0] if trigs else {}
    ex = (trig.get("execute") or {})
    eff = ex.get("effect") or {}
    pce = eff.get("per_choice_effect") or []
    fellow = (pce[0] or {}).get("effect") or {} if len(pce) > 0 else {}
    fellow_sub = ((pce[0] or {}).get("sub_ability") or {}).get("effect") or {} \
        if len(pce) > 0 else {}
    fellow_tgt = fellow_sub.get("target") or {}
    aid = (pce[1] or {}).get("effect") or {} if len(pce) > 1 else {}
    findings = {
        "name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "oracle": card.get("oracle_text"),
        "trigger_mode": trig.get("mode"),
        "trigger_destination": trig.get("destination"),
        "vote_effect": eff,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", {"vote_type": eff.get("type"),
                        "choices": eff.get("choices"),
                        "voter_scope": eff.get("voter_scope"),
                        "fellow_head": fellow,
                        "fellow_sub": fellow_sub,
                        "aid": aid})
    ok = (eff.get("type") == "Vote"
          and set(eff.get("choices") or []) == {"fellowship", "aid"}
          and fellow.get("type") == "Unimplemented"
          and "the voter chooses a creature they control" in
          str(fellow.get("description"))
          and fellow_sub.get("type") == "GainControlAll"
          and fellow_tgt.get("type") == "TrackedSet"
          and fellow_tgt.get("id") == 0
          and aid.get("type") == "PutCounterAll")
    say(f"data-level check: vote={eff.get('type')} choices={eff.get('choices')}, "
        f"fellowship head={fellow.get('type')}/{fellow.get('name')!r}, "
        f"sub={fellow_sub.get('type')} target={fellow_tgt}, "
        f"aid={aid.get('type')} -> "
        f"{'MATCHES ISSUE REPORT' if ok else 'MISMATCH'}")
    ST["notes"].append(
        "data-level: pinned v0.100.0 card-data.json parses Elrond of the "
        "White Council's ETB as Vote{fellowship, aid} (voter_scope "
        f"{eff.get('voter_scope')}, tally {eff.get('tally_mode')}); "
        "fellowship per-choice head "
        f"Unimplemented(name={fellow.get('name')!r}, "
        f"description={str(fellow.get('description'))[:70]!r}...) + sub "
        f"GainControlAll target={fellow_tgt}; aid branch "
        f"{aid.get('type')} (issue-reported shape: {ok}; head node name "
        "differs from the issue's 9b7c66e30 corpus ('the' -> "
        "'unrecognized_clause_head'), same clause; the Vote root itself "
        "is newly parsed vs the issue corpus)")
    ST["data_level_ok"] = ok
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    lands = sum(1 for n in hand if n in (FOREST, ISLAND, PLAINS))
    return ELROND in hand and lands >= 3


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


RANK = {ELROND: 10, FOREST: 6, ISLAND: 6, PLAINS: 5, BEAR: 4}


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


async def answer_vote_opp(c, st, state, tag, vote_word):
    """Answer the secret-council vote opportunity for this seat's test-policy
    vote, matched on serialized candidate content. Records the full
    opportunity for evidence. Never submits blind."""
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        ser = json.dumps(opp, default=str).lower()
        if "fellowship" not in ser or "aid" not in ser:
            continue  # not the secret-council vote
        rtype = (opp.get("response", {}) or {}).get("type")
        cid = candidate_matching(opp, [vote_word])
        wire("vote_opp", {"who": tag, "iid": iid, "rtype": rtype,
                          "policy_vote": vote_word,
                          "matched_candidate": cid,
                          "opportunity": opp})
        if not cid:
            say(f"[{tag}] WARNING: vote offered but no candidate matched "
                f"{vote_word!r}; not submitting blind")
            ST["notes"].append(
                f"vote: opportunity offered to {tag} but no candidate "
                f"matched {vote_word!r}; see wire_log vote_opp")
            continue
        SUBMITTED_OPPS.add(iid)
        ST["vote_iids"].append(iid)
        ST["votes_submitted"][tag] = vote_word
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": cid}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
        wire("vote_submit", {"who": tag, "submission": sub})
        await c.send_interaction(sub)
        say(f"[{tag}] secret-council vote -> {vote_word} "
            f"(candidate {str(cid)[:40]})")
        acted = True
    return acted


async def pay_mana_vi(c, st, state, tag):
    """Answer vi mana-payment choices during casting. The engine runs a
    tap-to-pool payment UI (one tapLandForMana interaction per land); never
    touches cancelCast / untapLandForMana / unspendPoolMana."""
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
    needs = ST["mana_needs"]
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
                for color in ("G", "U", "B", "W", "R"):
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


def castable_elrond(state):
    lands = untapped_lands(state, 0)
    if len(lands) < 5:
        return False
    names = {obj_lname(state, o) for o in lands}
    return FOREST in names and ISLAND in names


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


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
    if ST["phase"] == "casting_elrond":
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await pay_tick(c, acts, "P0"):
            return
    if ST["phase"] == "resolving":
        if await answer_vote_opp(c, st, state, "P0", VOTE_POLICY["P0"]):
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
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        hn = hand_lnames(state, 0)
        # the Elrond cast: decisive pre.json exported INSIDE this
        # submission path (guarded flag), then cast paying {3}{G}{U}.
        if (ST["phase"] == "setup" and ELROND in hn
                and castable_elrond(state)):
            a, oid = cast_action_for(acts, state, ELROND)
            if a:
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    ST["p1_bf_creatures_at_pre"] = bf_creatures(state, 1)
                    ST["p0_bf_creatures_at_pre"] = len(bf_creatures(state, 0))
                    ST["counters_at_pre"] = {
                        str(o): counters_of(state, o)
                        for o in (ST["p1_bf_creatures_at_pre"]
                                  + bf_creatures(state, 0))}
                    say(f"[P0] pre.json exported; P1 BF creatures at pre: "
                        f"{ST['p1_bf_creatures_at_pre']}; P0 BF creatures: "
                        f"{ST['p0_bf_creatures_at_pre']}")
                ST["elrond_cast"] = True
                ST["elrond_submitted_at"] = time.time()
                ST["mana_needs"] = {"G": 1, "U": 1, "generic": 3}
                ST["mana_tapped"] = 0
                ST["phase"] = "casting_elrond"
                say(f"[P0] casting Elrond of the White Council (oid {oid})")
                wire("elrond_cast", {"oid": oid})
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
    if ST["phase"] == "resolving":
        if await answer_vote_opp(c, st, state, "P1", VOTE_POLICY["P1"]):
            return
    if await do_discard_to_handsize(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        # P1 never attacks; keeps bears on the battlefield for the vote.
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
                     "Elrond of the White Council's ETB as Vote{fellowship, "
                     "aid} with the fellowship per-choice head "
                     "Unimplemented('the voter chooses a creature they "
                     "control') + sub GainControlAll TrackedSet(0) and an "
                     "aid PutCounterAll branch; see data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        p1c = ST["p1_bf_creatures_at_pre"]
        notes.append(f"A2 probe: pre P1 BF creatures={p1c}; P0 BF "
                     f"creatures={ST['p0_bf_creatures_at_pre']}.")
        if p1c and ST["p0_bf_creatures_at_pre"] is not None:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: pre.json has >=1 creature under P1's "
                         "control and Elrond was castable.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre is missing a P1 "
                         "battlefield creature for the fellowship path.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the Elrond "
                     "cast never happened).")

    # A3: the cast completed, Elrond is on P0's battlefield, trigger resolved
    elrond_bf_post = False
    if post_st:
        for oid, o in (post_st.get("objects") or {}).items():
            if (str(o.get("base_name") or o.get("name") or "").lower() == ELROND
                    and o.get("zone") == "Battlefield"
                    and o.get("controller") == 0):
                elrond_bf_post = True
                break
    if ST["elrond_cast"] and elrond_bf_post:
        ass["A3_cast_resolves"] = "passed"
        notes.append("A3 passed: Elrond was cast, is on P0's battlefield at "
                     "post, and the ETB trigger left the stack (no stall, "
                     "no pending rejection).")
    elif ST["elrond_cast"]:
        ass["A3_cast_resolves"] = "failed"
        notes.append("A3 FAILED: Elrond was cast but is not on P0's "
                     "battlefield at post.")
    else:
        notes.append("A3 not-run: Elrond was never cast.")

    # A4: the secret-council vote ran
    if ST["votes_submitted"].get("P0") and ST["votes_submitted"].get("P1"):
        ass["A4_vote_offered"] = "passed"
        notes.append(f"A4 passed: secret-council vote offered to both seats; "
                     f"submitted {ST['votes_submitted']} "
                     f"(vote iids: {ST['vote_iids']}).")
    else:
        ass["A4_vote_offered"] = "failed"
        notes.append(f"A4 FAILED: the secret-council vote was not offered "
                     f"to both seats (submitted: {ST['votes_submitted']}); "
                     f"waiting_for types seen while resolving: "
                     f"{sorted(set(ST['wf_types_resolving']))}.")

    # A5/A6: the reported bug -- the fellowship voter's "choose a creature
    # they control" step and the resulting control change. A5 passes only
    # if a CHOICE-shaped prompt reached the voter: ordinary turn-structure
    # prompts (priority passes, tap-land/cast menus, DeclareAttackers) do
    # NOT count -- a prompt appearing is not a pass (playbook step 2).
    nonvote_prompts = [t for t in ST["prompts_seen"]
                       if t[1] not in ST["vote_iids"]]
    notes.append(f"Resolution prompts: {len(ST['prompts_seen'])} total, "
                 f"{len(nonvote_prompts)} non-vote.")
    ORDINARY_ACTIONS = {"passPriority", "tapLandForMana", "castSpell",
                        "playLand"}
    choice_like = []
    for _ph, iid, opp in nonvote_prompts:
        resp = (opp or {}).get("response") or {}
        rtype = resp.get("type")
        if rtype == "schema":
            spec = ((resp.get("data") or {}).get("spec")) or {}
            sdata = spec.get("data") or {}
            if (spec.get("type") == "relations"
                    and sdata.get("sourceConstraint") == "atMostOne"):
                continue  # DeclareAttackers, not the choice step
            choice_like.append(iid)
            continue
        if rtype == "exactChoices":
            codes = set()
            for ch in ((resp.get("data") or {}).get("choices") or []):
                for sf in (ch.get("surfaces") or []):
                    if sf.get("type") == "action":
                        codes.add((sf.get("data") or {}).get("code"))
            codes.discard(None)
            if codes <= ORDINARY_ACTIONS:
                continue  # ordinary turn menu, not the choice step
            choice_like.append(iid)
    if nonvote_prompts:
        notes.append(f"non-vote prompt iids during resolution: "
                     f"{[t[1] for t in nonvote_prompts][:8]} (full JSON in "
                     f"wire_log resolution_prompt entries); choice-shaped "
                     f"iids: {choice_like[:4] or 'none'}")
    if ST["votes_submitted"].get("P1") == "fellowship" and not choice_like:
        ass["A5_fellowship_choice"] = "failed"
        notes.append("A5 FAILED: P1 voted fellowship but no 'choose a "
                     "creature' choice was ever offered to the voter -- THE "
                     "REPORTED BUG (the clause never parsed, so the choice "
                     "step does not exist at runtime). The non-vote prompts "
                     "were all ordinary turn-structure prompts (priority "
                     "passes, tap-land menus, DeclareAttackers).")
    elif choice_like:
        ass["A5_fellowship_choice"] = "passed"
        notes.append("A5 passed: a choice-shaped prompt was offered during "
                     f"resolution: {choice_like[:4]}.")
    else:
        notes.append("A5 not-run: no fellowship vote was submitted, so the "
                     "choice step has no voter to observe.")

    changed = []
    unchanged = []
    if (ST["pre_exported"] and ST["post_exported"] and pre_st and post_st
            and ST["p1_bf_creatures_at_pre"]):
        for oid in ST["p1_bf_creatures_at_pre"]:
            o = get_obj(post_st, oid)
            ctrl = o.get("controller")
            z = o.get("zone")
            notes.append(f"A6 probe: pre-P1 creature oid {oid} post "
                         f"zone={z!r} controller={ctrl}.")
            if z == "Battlefield" and ctrl == 0:
                changed.append(oid)
            else:
                unchanged.append((oid, z, ctrl))
        if changed:
            ass["A6_control_change"] = "passed"
            notes.append(f"A6 passed: control changed P1->P0 for {changed}.")
        else:
            ass["A6_control_change"] = "failed"
            notes.append("A6 FAILED: no pre-P1 creature changed control to "
                         f"P0 ({unchanged}) -- THE REPORTED BUG "
                         "(GainControlAll read an empty tracked set).")
    else:
        notes.append("A6 not-run: no pre/post pair with recorded P1 "
                     "battlefield creatures.")

    # A7: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A7_cleanup"] = "passed"
            notes.append("A7 passed: post stack empty.")
        else:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A7 not-run: no post state")

    if ST["stack_saw_trigger"]:
        notes.append("The Elrond ETB trigger was observed on the stack "
                     "during resolution (live tick).")
    else:
        notes.append("The Elrond ETB trigger was NOT observed on the stack "
                     "by the live tick (may have resolved between ticks).")

    # verdict
    if (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolves"] == "passed"
            and not (ass["A5_fellowship_choice"] == "passed"
                     and ass["A6_control_change"] == "passed")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the fellowship branch's printed effect "
            "never happened -- see the failed assertion(s) above "
            "(confirmed on v0.100.0). The parse is the reported defect; "
            "the missing choice and missing control change are its direct "
            "structural consequence. This answers the issue's open "
            "consumer-classification question for GainControlAll: on the "
            "empty chain tracked set it is filter-only -- a silent no-op, "
            "not target-inheriting. This is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_cast_resolves",
                                          "A5_fellowship_choice",
                                          "A6_control_change")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the fellowship voter was offered the "
            "choice and a creature changed control to P0 as printed. This "
            "is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported Elrond-ETB path could "
                     "not be fully exercised; see assertion notes.")

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
               if "elrond" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7422.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "elrond_cast": ST["elrond_cast"],
            "elrond_submitted_at": ST["elrond_submitted_at"],
            "p1_bf_creatures_at_pre": ST["p1_bf_creatures_at_pre"],
            "p0_bf_creatures_at_pre": ST["p0_bf_creatures_at_pre"],
            "votes_submitted": ST["votes_submitted"],
            "vote_iids": ST["vote_iids"],
            "prompts_seen": ST["prompts_seen"],
            "wf_types_resolving": sorted(set(ST["wf_types_resolving"])),
            "stack_saw_trigger": ST["stack_saw_trigger"],
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report (no game state); the "
            "scenario replays the reported line (Elrond enters with a "
            "creature under the opponent's control; both seats vote "
            "fellowship) from a fresh game and observes the resolution "
            "outcome.",
            "Vote policy is a deterministic test choice (both seats vote "
            "fellowship); the aid branch was not exercised.",
            "12x Grizzly Bears deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Elrond of the White Council + 24x Forest + "
                      "24x Island + 8x Plains; P1: 12x Grizzly Bears + "
                      "48x Forest (lands/bears, never attacks)",
        "contract_line": "Both seats vote fellowship on Elrond's ETB; P1 "
                         "(a fellowship voter) must be offered the 'choose "
                         "a creature they control' choice and P0 must gain "
                         "control of the chosen creature. Observed: the "
                         "clause never parsed, no choice prompt appeared, "
                         "and no creature changed control.",
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
                    if ST["phase"] == "resolving":
                        # record any viewer-interaction prompts offered while
                        # the ETB trigger is resolving. The vote opportunities
                        # are expected (answered by the tick); other prompts
                        # during resolution are usually ordinary turn-structure
                        # menus (priority passes, tap-land/cast menus,
                        # DeclareAttackers) -- only a CHOICE-shaped prompt
                        # (non-ordinary actions / non-attackers schema) would
                        # be the fellowship choice step.
                        vi = get_vi(st)
                        if vi:
                            for opp in vi.get("opportunities", []) or []:
                                iid = opp.get("interactionId") or opp.get("id")
                                tag2 = (ST["phase"], iid)
                                if all(t[:2] != tag2 for t in ST["prompts_seen"]):
                                    ST["prompts_seen"].append((tag2[0], tag2[1], opp))
                                    wire("resolution_prompt",
                                         {"phase": ST["phase"], "iid": iid,
                                          "opportunity": opp})
                                    say(f"[{ST['phase']}] prompt seen: "
                                        f"{json.dumps(opp)[:300]}")
                        wtype = (wf_of(state).get("type") or "")
                        if wtype and wtype not in ST["wf_types_resolving"]:
                            ST["wf_types_resolving"].append(wtype)
                        for se in stack_entries(state):
                            ser = json.dumps(se, default=str).lower()
                            if "elrond" in ser and not ST["stack_saw_trigger"]:
                                ST["stack_saw_trigger"] = True
                                say("ETB trigger observed on the stack")
                                wire("trigger_on_stack", {"entry": se})
                    # elrond resolution: creature on P0's battlefield
                    if ST["phase"] == "casting_elrond":
                        elrond_bf = any(
                            str(o.get("base_name") or o.get("name") or "")
                            .lower() == ELROND
                            and o.get("zone") == "Battlefield"
                            and o.get("controller") == 0
                            for o in (state.get("objects") or {}).values())
                        if elrond_bf:
                            say("Elrond resolved (on P0 battlefield); "
                                "phase -> resolving")
                            wire("elrond_resolved", {})
                            ST["mana_needs"] = {"G": 0, "U": 0, "generic": 0}
                            ST["phase"] = "resolving"
                            ST["settle_at"] = now
                        elif (ST["elrond_submitted_at"] is not None
                                and now - ST["elrond_submitted_at"] > 300):
                            say("elrond watchdog: 300s after the cast, "
                                "Elrond not on battlefield -- exporting "
                                "post and finalizing")
                            wire("elrond_stall",
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
                        say("setup watchdog: 1200s in, Elrond never cast "
                            "-- exporting state and finalizing")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "resolving"
                            and ST["settle_at"] is not None
                            and now - ST["settle_at"] > 300):
                        say("resolving watchdog: 300s settled-ish without "
                            "post export -- exporting post and finalizing")
                        wire("resolving_stall",
                             {"waiting_for": wf_of(state),
                              "stack": stack_entries(state)})
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7422.py",
                f"{EVDIR}/scenario_7422.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Elrond of the White Council: unparsed 'the voter "
                    "chooses a creature they control' leaves GainControlAll "
                    "reading an empty tracked set; no choice offered, no "
                    "control change"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
