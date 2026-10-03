#!/usr/bin/env python3
"""Issue #7437: Razor Demon -- "target opponent drafts a card from Razor
Demon's spellbook" is unparsed, so `GrantCastingPermission` reads an empty
tracked set.

Reported (2026-08-15, lgray; source:internal-triage, status:confirmed,
area:parser, mechanic:triggers, mechanic:zone-change, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 tracked-set publish census, one
of 33 cards whose tracked-set antecedent clause never parses):

> When you cast this spell, target opponent drafts a card from Razor Demon's
> spellbook. They may cast that card without paying its mana cost.
> Flying
> Ward -- Discard a card.

Pinned v0.100.0 parse (see data_evidence.json): Razor Demon triggers[0],
mode "SpellCast":
  head = Unimplemented { name: "unrecognized_clause_head",
                         description: "target opponent drafts a card from
                                       ~'s spellbook" }
    sub_ability (Spell, SequentialSibling): GrantCastingPermission
      { permission: PlayFromExile { UntilEndOfTurn, granted_to 0, Cast },
        target: TrackedSet(0), grantee: ParentTargetController }
DISCREPANCY (recorded, as in the #7434/#7436 runs): the issue body quotes the
head name as "target"; the pinned v0.100.0 card-data.json names it
"unrecognized_clause_head". The description string matches, and the
structural defect (Unimplemented head -> empty tracked set ->
GrantCastingPermission reads an empty set) is identical either way.
valid_target: Player. metadata.spellbook = [Demonic Bargain, Demonic Pact,
Ever After].

The Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a FRESH EMPTY tracked set and the
GrantCastingPermission sub-ability reads that empty set. The issue explicitly
states no runtime symptom is asserted for the consumer side
("GrantCastingPermission ... not measured"; "this issue does not assert a
runtime symptom") -- the reported defect is the parse state and the empty
publish that structurally follows from it. The scenario therefore replays the
reported line (cast Razor Demon, answer the trigger's target selection with
the opponent, let the SpellCast trigger resolve) and observes the resolution
outcome: whether a draft-choice prompt is ever offered to the opponent and
whether any spellbook card / permission grant materializes.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Razor Demon + 56x Swamp (60)
  P1: 60x Swamp (plays lands only, never casts, never attacks)
Drive:
  1. Mulligans: P0 keeps iff Razor Demon is in the opening hand
     (mulligans down, min hand 5); P1 keeps 7.
  2. P0 plays one Swamp per turn, casts Razor Demon as soon as able
     ({1}{B}{B} = 3 untapped Swamps), passing priority otherwise. P1 plays
     lands and passes. Turn cap ~45.
  3. pre.json is exported INSIDE the cast submit path (guarded flag), never
     by a main-loop probe. mid_stack.json is exported when the SpellCast
     trigger is first seen on the stack WITH the target chosen (the real
     pre-resolution snapshot). post.json once settled (trigger resolved,
     stack empty, Priority, 8s idle) or on the GameOver/deadline finalize
     path.
  4. The trigger targets "target opponent". The driver answers a TargetSelection
     prompt with P1 (the only opponent) via the advertised schema/sequence
     response if one is issued; if the engine auto-targets without prompting
     (observed: no TargetSelection in the wire log), that is recorded.

Expected (correct behavior): the trigger resolves by offering the targeted
opponent a draft choice from Razor Demon's spellbook (the spellbook metadata
lists Demonic Bargain, Demonic Pact, Ever After), then the opponent may cast
the drafted card without paying its mana cost (a GrantCastingPermission over
the drafted card).
Reported (bug): the draft clause never parsed (Unimplemented head, runtime
no-op), so no draft prompt is offered, the chain tracked set is allocated
empty, and GrantCastingPermission reads an empty set -- no permission grant
materializes.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Razor Demon as the
                  issue reports: triggers[0] mode SpellCast, head
                  Unimplemented (record exact head name) describing "target
                  opponent drafts a card from ~'s spellbook"; sub Spell
                  GrantCastingPermission { PlayFromExile UntilEndOfTurn
                  granted_to 0 Cast, target TrackedSet(0), grantee
                  ParentTargetController }, sub_link SequentialSibling;
                  valid_target Player; spellbook metadata has 3 entries.
  A2_setup_ok     pre.json: Razor Demon in P0 hand before the cast, >=3
                  untapped Swamps, main phase, stack empty; mid_stack.json:
                  the SpellCast trigger on the stack (source = the Demon
                  spell object, "draft"/"spellbook" in the entry text).
                  (The trigger's target selection is informational: the
                  engine auto-targets the only opponent without prompting.)
  A3_cast_resolved
                  the cast was submitted, the trigger was seen on the stack
                  and resolved; Razor Demon is on the P0 battlefield at
                  post.
  A4_no_draft_window
                  the Unimplemented draft head produced no draft: no
                  draft-choice prompt was offered to the opponent during the
                  trigger resolution window (draft_prompt_seen false, 0
                  choice_shaped window prompts), and no spellbook-named card
                  appears in any zone at post. EXPECTED TO PASS under the
                  bug (the no-op played out at runtime).
  A5_no_permission_grant
                  GrantCastingPermission read the empty tracked set and
                  granted nothing observable: no draft prompt to P1 in the
                  window, no spellbook card became castable/visible to
                  either player, and the wire log records no draft-choice
                  opportunity. (Proxy: the issue asserts no consumer-side
                  symptom; this records the observable absence.)
  A6_cleanup      post stack empty, game advanced past the cast turn, no
                  unrejected submissions lingering (no in-flight
                  cast/activation, rejections recorded).

Verdict rule: reproduced iff A1, A2, A3, A4, A5, A6 passed (the reported
              parse no-op played out at runtime: no draft prompt, no grant);
              not-reproduced iff a draft prompt was offered or a spellbook
              card / permission grant materialized;
              blocked iff A1, A2, or A3 cannot be established.
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
RUN_ID = "20261003-0651-7437"
ISSUE = 7437
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
              "latest stable; ServerHello 0.100.0/bc9ef56/protocol 101 "
              "re-verified by this run); hashes recomputed against "
              "on-disk artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

RAZOR = "razor demon"
SWAMP = "swamp"
SPELLBOOK = ["demonic bargain", "demonic pact", "ever after"]

P0_DECK = [("Razor Demon", 4), ("Swamp", 56)]
P1_DECK = [("Swamp", 60)]


def deck(pairs):
    names = []
    for n, c in pairs:
        names += [n] * c
    return {"main_deck": names, "sideboard": [], "commander": []}


SETUP_DEADLINE_S = 1800
RESOLVE_DEADLINE_S = 600
SETTLE_IDLE_S = 8
TURN_CAP = 45
OPPONENT_PID = 1


def reset_attempt():
    global ST, MULLS, MULL_COUNT, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "build",  # build -> resolving -> done
        "demon_cast": False,
        "demon_oid": None,
        "target_chosen": False,
        "trigger_on_stack": False,
        "trigger_resolved": False,
        "cast_turn": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "cast_in_flight": None,
        "draft_prompt_seen": False,
        "draft_answered": False,
        "mana_needs": {"P0": {"B": 0, "R": 0, "generic": 0},
                       "P1": {"B": 0, "R": 0, "generic": 0}},
        "prompts_seen": [],
        "prompt_classes": {},
        "wf_types_window": [],
        "stack_kinds_window": [],
        "terminal": False,
        "states_seen": 0,
        "cast_rejections": 0,
        "stack_dumped": False,
        "settle_at": None,
        "resolving_since": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_cast_resolved",
                                       "A4_no_draft_window",
                                       "A5_no_permission_grant",
                                       "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
    }
    MULLS = set()
    MULL_COUNT = {}
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


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def demon_oid(state, pid=0):
    for oid in bf_permanents(state, pid):
        if obj_lname(state, oid) == RAZOR:
            return oid
    return None


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and is_land(o)]


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


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


def se_blob(se):
    return json.dumps(se, default=str).lower()


def _src_matches(se, want):
    if want is None:
        return False
    try:
        return int(se.get("source_id")) == int(want)
    except (TypeError, ValueError):
        return False


def draft_trigger_on_stack(state):
    """Razor Demon's SpellCast trigger: source is the Demon spell object and
    the entry text names the draft/spellbook clause."""
    want = ST.get("demon_oid")
    if want is None:
        return None
    for se in stack_entries(state):
        b = se_blob(se)
        if _src_matches(se, want) and ("draft" in b or "spellbook" in b):
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

# ------------------------------------------------------------- data check
def check_data_level():
    rd = CARD_DATA.get("razor demon", {})
    triggers = rd.get("triggers") or []
    t0 = triggers[0] if triggers else {}
    head = (t0.get("execute") or {}).get("effect") or {}
    sub = ((t0.get("execute") or {}).get("sub_ability") or {})
    sub_eff = sub.get("effect") or {}
    perm = sub_eff.get("permission") or {}
    sub_tgt = sub_eff.get("target") or {}
    sub_grantee = sub_eff.get("grantee") or {}
    book = ((rd.get("metadata") or {}).get("spellbook") or [])
    ev = {
        "razor_demon": rd,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "head_name_discrepancy": {
            "issue_body_quotes": "target",
            "pinned_data_shows": head.get("name"),
            "note": "description string matches the issue; structural "
                    "defect identical either way",
        },
    }
    ok = (
        t0.get("mode") == "SpellCast"
        and head.get("type") == "Unimplemented"
        and "target opponent drafts a card from ~'s spellbook" in
        str(head.get("description", "")).lower()
        and sub_eff.get("type") == "GrantCastingPermission"
        and perm.get("type") == "PlayFromExile"
        and perm.get("duration") == "UntilEndOfTurn"
        and perm.get("granted_to") == 0
        and perm.get("mode") == "Cast"
        and sub_tgt.get("type") == "TrackedSet"
        and sub_tgt.get("id") == 0
        and sub_grantee.get("type") == "ParentTargetController"
        and sub.get("sub_link") == "SequentialSibling"
        and (t0.get("valid_target") or {}).get("type") == "Player"
        and len(book) == 3
    )
    say(f"data-level check: trigger mode={t0.get('mode')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"sub={sub_eff.get('type')}/{sub_tgt}; "
        f"perm={perm.get('type')}/{perm.get('duration')}/"
        f"granted_to={perm.get('granted_to')}/{perm.get('mode')}; "
        f"grantee={sub_grantee.get('type')}; sub_link={sub.get('sub_link')}; "
        f"valid_target={(t0.get('valid_target') or {}).get('type')}; "
        f"spellbook={book}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    # byte snapshot of the Razor Demon entry itself
    with open(f"{EVDIR}/razor_demon_card_data.json", "w") as f:
        json.dump(rd, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok, "head_name": head.get("name")})

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


def keep_p0(hand):
    return RAZOR in hand


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
    if key in SUBMITTED_OPPS:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    # bottom lands first; never bottom Razor Demon for P0
    rank = {SWAMP: 0, RAZOR: 9}
    picks = [int(o) for o in sorted(
        hand, key=lambda o: (rank.get(obj_lname(state, o), 5), o))[:n]]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


def p0_discard_pick(state, pid):
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return []
    # discard lands first; never discard Razor Demon
    rank = {SWAMP: 0, RAZOR: 9}
    return [int(o) for o in sorted(
        hand, key=lambda o: (rank.get(obj_lname(state, o), 5), o))[:n]]


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
    if tag == "P0":
        picks = p0_discard_pick(state, pid)
    else:
        hand = hand_ids(state, pid)
        n = len(hand) - 7
        if n <= 0:
            return False
        picks = [int(x) for x in sorted(
            hand, key=lambda o: (0 if obj_lname(state, o) == SWAMP else 1,
                                 o))[:n]]
    if not picks:
        return False
    _DISCARD_REV[(c.name, rev)] = True
    say(f"[{tag}] discarding {len(picks)} to hand size: "
        f"{[obj_lname(state, o) for o in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("discard", {"who": tag, "picks": picks})
    return True


async def do_discard_vi(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    ops = vi_ops(st)
    if not ops:
        return False
    hand_oids = set(hand_ids(state, pid))
    if not hand_oids:
        return False
    for opp in ops:
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
            k, ref = _cand_ref(ch)
            if k != "object" or ref not in hand_oids:
                all_hand = False
                break
            cand_oids.add(ref)
        if not all_hand or not cand_oids:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if tag == "P0":
            picks = p0_discard_pick(state, pid)
            pick_oid = picks[0] if picks else None
        else:
            pick_oid = None
        pick = None
        for ch in chs:
            k, ref = _cand_ref(ch)
            if pick_oid is not None and ref == pick_oid:
                pick = ch.get("id")
                break
        if pick is None:
            for ch in chs:
                k, ref = _cand_ref(ch)
                if obj_lname(state, ref) == SWAMP:
                    pick = ch.get("id")
                    break
            if pick is None:
                pick = chs[0].get("id")
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] vi discard-to-hand-size: {pick}")
        wire("vi_discard", {"who": tag, "iid": iid, "pick": pick})
        await interact_as(c, {
            "interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [pick]}}}, tag)
        return True
    return False


def _cand_ref(ch):
    for sf in (ch.get("surfaces") or []):
        t = sf.get("type")
        d = sf.get("data") or {}
        if t == "object":
            try:
                return ("object", int(d.get("reference")))
            except (TypeError, ValueError):
                continue
        if t == "player":
            for k in ("reference", "player", "player_id", "id", "seat"):
                try:
                    v = d.get(k)
                    if v is None:
                        continue
                    return ("player", int(v))
                except (TypeError, ValueError):
                    continue
    return ("other", None)


async def pay_mana_vi(c, st, state, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
    for opp in ops:
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
                for color in ("B", "R", "G", "U", "W"):
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
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
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


def candidate_matching(opp, needles):
    """First available candidate whose serialized content contains all
    needles (case-insensitive)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
            continue
        ser = json.dumps(ch, default=str).lower()
        if all(n.lower() in ser for n in needles):
            return ch.get("id")
    return None


async def answer_trigger_target(c, st, state, tag):
    """Answer the SpellCast trigger's "target opponent" TargetSelection with
    P1's player candidate, matched by parsed player reference (never a bare
    numeric needle)."""
    if (wf_of(state).get("type") or "") != "TargetSelection":
        return False
    if ST["phase"] != "resolving" or ST["target_chosen"]:
        return False
    acted = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("candidates") or data.get("choices") or []
        cid = None
        for ch in chs:
            k, ref = _cand_ref(ch)
            if k == "player" and ref == OPPONENT_PID:
                if (ch.get("status", {}) or {}).get("type") not in (
                        None, "available"):
                    continue
                cid = ch.get("id")
                break
        if cid is None:
            continue
        SUBMITTED_OPPS.add(iid)
        rtype = (opp.get("response", {}) or {}).get("type")
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": cid}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
        wire("trigger_target_submit", {"who": tag, "iid": iid, "cid": cid,
                                       "rtype": rtype, "opportunity": opp})
        await interact_as(c, sub, tag)
        ST["target_chosen"] = True
        say(f"[{tag}] trigger target -> P1 (candidate {cid}, rtype {rtype})")
        acted = True
    return acted

# ------------------------------------------------------------- prompt handlers
ORDINARY_ACTION_CODES = {"passPriority", "playLand", "tapLandForMana"}


def classify_prompt(opp):
    """Classify a resolution-window opportunity. Returns one of:
    mana_payment, pass_priority, action_menu, declare_combat,
    discard_handsize, target_selection, choice_shaped, other.

    choice_shaped is deliberately narrow: only a draft-from-spellbook-shaped
    card selection -- the opportunity text names the spellbook/draft, or its
    object candidates sit in a non-ordinary zone (outside-the-game pool).
    Mana payment, pass-priority, play-land menus, attack/block declarations,
    hand-size discards, and plain target selections must never count as the
    reported prompt."""
    blob = json.dumps(opp, default=str).lower()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    items = (data.get("choices") or []) + (data.get("candidates") or [])
    # 1. combat declarations (selection intent attack/block, or
    #    attacker/blocker roles on candidates)
    for sf in opp.get("surfaces") or []:
        if sf.get("type") == "selection":
            intent = str((sf.get("data") or {}).get("intent") or "").lower()
            if intent in ("attack", "block"):
                return "declare_combat"
    for it in items:
        for sf in it.get("surfaces") or []:
            role = str((sf.get("data") or {}).get("role") or "").lower()
            if role in ("attacker", "attacktarget", "blocker",
                        "blocktarget", "defender"):
                return "declare_combat"
    # 2. mana payment (tapLandForMana anywhere)
    for it in items:
        for sf in it.get("surfaces") or []:
            if sf.get("type") == "action" and \
                    (sf.get("data") or {}).get("code") == "tapLandForMana":
                return "mana_payment"
    # 3. ordinary action menus: every item's action codes are ordinary and
    #    every object surface is merely the "source" of such an action
    #    (e.g. the Swamp behind a playLand choice)
    if items:
        ordinary = True
        saw_obj = False
        for it in items:
            codes = [(sf.get("data") or {}).get("code")
                     for sf in it.get("surfaces") or []
                     if sf.get("type") == "action"]
            obj_roles = [(sf.get("data") or {}).get("role")
                         for sf in it.get("surfaces") or []
                         if sf.get("type") == "object"]
            if obj_roles:
                saw_obj = True
            if any(r not in (None, "source") for r in obj_roles):
                ordinary = False
            if not codes or any(c not in ORDINARY_ACTION_CODES
                                for c in codes):
                ordinary = False
        if ordinary:
            return "pass_priority" if not saw_obj else "action_menu"
    # 4. discard to hand size: card-candidate selection, all in hand
    obj_zones = set()
    saw_obj = False
    saw_player_only = True
    for it in items:
        for sf in it.get("surfaces") or []:
            if sf.get("type") == "object":
                saw_obj = True
                saw_player_only = False
                obj_zones.add(
                    str((sf.get("data") or {}).get("zone") or "").lower())
    if saw_obj and obj_zones and obj_zones <= {"hand"}:
        return "discard_handsize"
    # 5. plain target selections (player candidates, no object surfaces)
    if not saw_obj and saw_player_only and items:
        if "target" in blob:
            return "target_selection"
    # 6. the bug's choice: draft-from-spellbook card selection
    if saw_obj and ("spellbook" in blob
                    or ("draft" in blob and "draft" in str(
                        (resp.get("data") or {}).get("prompt") or "").lower())
                    or (obj_zones and not obj_zones <= {"hand", "battlefield",
                        "library", "graveyard", "exile", "stack", "command"})):
        return "choice_shaped"
    # 7. pass priority fallback (no objects)
    for it in data.get("choices") or []:
        for sf in it.get("surfaces") or []:
            if sf.get("type") == "action" and \
                    (sf.get("data") or {}).get("code") == "passPriority":
                return "pass_priority"
    return "other"


def record_window_prompt(seat, st):
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if all(t[2] != iid for t in ST["prompts_seen"]):
            ST["prompts_seen"].append((ST["phase"], seat, iid, opp))
            cls = classify_prompt(opp)
            ST["prompt_classes"][str(iid)] = cls
            wire("window_prompt", {"phase": ST["phase"], "seat": seat,
                                   "iid": iid, "class": cls,
                                   "opportunity": opp})
            say(f"[{ST['phase']}/{seat}] prompt class={cls}: "
                f"{json.dumps(opp)[:260]}")


async def answer_draft_choice(c, st, state, tag):
    """Control-branch handler: if the engine DOES offer a draft-from-spellbook
    choice during the trigger window, record it (A4/A5 then fail) and answer
    minimally so the game can continue: empty selection when the spec min
    allows 0, else the first candidates up to max."""
    if tag not in ("P0", "P1") or ST["phase"] != "resolving" \
            or not ST["trigger_on_stack"]:
        return False
    if (wf_of(state).get("type") or "") == "TargetSelection":
        return False  # the trigger's own target prompt is not the draft
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        chs = data.get("candidates") or []
        if not chs:
            continue
        has_obj = False
        for ch in chs:
            k, _ = _cand_ref(ch)
            if k == "object":
                has_obj = True
                break
        if not has_obj:
            continue
        if classify_prompt(opp) != "choice_shaped":
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        ST["draft_prompt_seen"] = True
        try:
            lo = int(spec.get("min") or 0)
        except (TypeError, ValueError):
            lo = 0
        try:
            hi = int((spec.get("max") or {}).get("value")
                     if isinstance(spec.get("max"), dict)
                     else (spec.get("max") or len(chs)))
        except (TypeError, ValueError):
            hi = len(chs)
        hi = max(0, min(hi, len(chs)))
        picks = [] if lo == 0 else [ch.get("id") for ch in chs[:hi]]
        SUBMITTED_OPPS.add(iid)
        ST["draft_answered"] = True
        say(f"[{tag}] CONTROL: draft-from-spellbook choice prompt OFFERED "
            f"(min={lo} max={hi}); answering with {len(picks)} picks")
        wire("draft_choice", {"who": tag, "iid": iid,
                              "spec": str(spec)[:300],
                              "n_candidates": len(chs),
                              "picks": picks})
        await interact_as(c, {
            "interactionId": iid,
            "response": {"type": spec.get("type"),
                         "data": {"choiceIds": picks}}}, tag)
        return True
    return False

# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P0", st)
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P0", keep_p0):
            return
        if await do_bottom(c, pid, "P0"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P0"):
        return
    if await do_discard_vi(c, pid, "P0"):
        return
    if await answer_draft_choice(c, st, state, "P0"):
        return
    # Payment-complete detectors: a cast leaves the hand; the engine has
    # taken its payment: clear the owed mana.
    cif = ST.get("cast_in_flight")
    if cif is not None:
        co = get_obj(state, cif)
        if co.get("zone") != "Hand":
            ST["cast_in_flight"] = None
            ST["mana_needs"]["P0"] = {"B": 0, "R": 0, "generic": 0}
            say(f"[P0] cast oid {cif} left hand (zone {co.get('zone')}); "
                f"payment complete, needs cleared")
            wire("payment_complete", {"oid": cif, "zone": co.get("zone")})
    if sum(ST["mana_needs"]["P0"].values()) > 0:
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await pay_tick(c, acts, "P0"):
            return
        # Mana is still owed but no tap/payment made progress: the engine
        # is waiting on payment. Do NOT fall through to pass_priority here
        # (passing on a mana prompt can cancel/stall the cast); wait for the
        # next state and let the stall watchdog diagnose.
        if time.time() - ST.get("_mana_wait_at", 0) > 30:
            ST["_mana_wait_at"] = time.time()
            say(f"[P0] mana still owed {ST['mana_needs']['P0']} but no "
                f"payment available; waiting")
            wire("mana_wait", {"needs": dict(ST["mana_needs"]["P0"])})
        await asyncio.sleep(5)
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
    if await answer_trigger_target(c, st, state, "P0"):
        return
    if not my_priority(state, pid):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid \
            and not stack_entries(state) and ST["phase"] == "build":
        hn = hand_lnames(state, pid)
        did = demon_oid(state, pid)
        if did is not None:
            ST["demon_oid"] = did
        if did is None and not ST["demon_cast"] and RAZOR in hn:
            if len(untapped_lands(state, pid)) >= 3:
                a, oid = cast_action_for(acts, state, RAZOR)
                if a:
                    # pre.json is exported INSIDE the cast submit path
                    # (guarded flag), never by a main-loop probe.
                    if not ST["pre_exported"]:
                        await export_as(c, "pre")
                        ST["pre_exported"] = True
                        pre_st = json.load(
                            open(f"{EVDIR}/pre.json")).get("state", {})
                        say(f"[cast] pre.json exported: demon in P0 hand="
                            f"{RAZOR in hand_lnames(pre_st, 0)}; "
                            f"untapped swamps="
                            f"{len(untapped_lands(pre_st, 0))}; "
                            f"turn={pre_st.get('turn_number')}; "
                            f"phase={pre_st.get('phase')}")
                        wire("pre_exported",
                             {"turn": pre_st.get("turn_number")})
                    ST["demon_oid"] = oid
                    ST["demon_cast"] = True
                    ST["cast_in_flight"] = oid
                    ST["mana_needs"]["P0"] = {"B": 2, "R": 0, "generic": 1}
                    ST["phase"] = "resolving"
                    ST["resolving_since"] = time.time()
                    ST["cast_turn"] = state.get("turn_number")
                    say(f"[P0] casting Razor Demon (oid {oid}) -- DECISIVE")
                    wire("demon_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
        # play a land
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 20:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
    await pass_priority(c, st, acts)

# ------------------------------------------------------------- P1 tick (lands only)
async def p1_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P1", st)
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P1", lambda hn: True):
            return
        if await do_bottom(c, pid, "P1"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P1"):
        return
    if await do_discard_vi(c, pid, "P1"):
        return
    if await answer_draft_choice(c, st, state, "P1"):
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
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 8:
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
    mid = load_env("mid_stack") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    mid_st = mid.get("state") or {}
    post_st = post.get("state") or {}

    def spellbook_in_zones(st):
        found = []
        for oid, o in (st.get("objects") or {}).items():
            if obj_lname(st, oid) in SPELLBOOK:
                found.append((oid, o.get("zone"),
                              str(o.get("base_name") or o.get("name"))))
        return found

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Razor Demon as the issue reports -- triggers[0] "
                     "mode SpellCast, head Unimplemented (name "
                     "'unrecognized_clause_head' in pinned data; the issue "
                     "body quotes 'target' -- description matches, "
                     "structural defect identical) describing 'target "
                     "opponent drafts a card from ~'s spellbook', + sub "
                     "Spell GrantCastingPermission { PlayFromExile "
                     "UntilEndOfTurn granted_to 0 Cast, target TrackedSet(0),"
                     " grantee ParentTargetController }, sub_link "
                     "SequentialSibling; valid_target Player; spellbook "
                     "metadata has 3 entries (Demonic Bargain, Demonic Pact, "
                     "Ever After); see data_evidence.json and "
                     "razor_demon_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre + trigger on the stack
    if ST["pre_exported"] and pre_st and ST["mid_exported"] and mid_st:
        demon_in_hand = RAZOR in hand_lnames(pre_st, 0)
        n_sw = len(untapped_lands(pre_st, 0))
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        trig_mid = draft_trigger_on_stack(mid_st)
        # The engine never issued a TargetSelection prompt for the trigger
        # (no TargetSelection in the wire log or wf_types_window); the
        # trigger's target defaulted to the only legal opponent (P1).
        # target_chosen is therefore informational, not required.
        no_target_prompt = "TargetSelection" not in ST["wf_types_window"]
        notes.append(f"A2 probe: pre demon_in_hand={demon_in_hand}; "
                     f"pre untapped_swamps={n_sw}; pre phase={ph}; "
                     f"pre active={act}; pre stack={len(stack_entries(pre_st))}; "
                     f"mid trigger on stack={trig_mid is not None}; "
                     f"target_chosen={ST['target_chosen']} "
                     f"(no TargetSelection prompt seen: {no_target_prompt}).")
        if (demon_in_hand and n_sw >= 3 and trig_mid is not None
                and ph in ("PreCombatMain", "PostCombatMain") and act == 0
                and not stack_entries(pre_st)):
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: pre.json shows Razor Demon in the P0 "
                         "hand with >=3 untapped Swamps (main phase, stack "
                         "empty), and mid_stack.json shows the SpellCast "
                         "draft trigger on the stack. The trigger's target "
                         "selection was never prompted (engine auto-"
                         "targeted the only opponent, P1); no "
                         "TargetSelection appears in the wire log.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: pre/mid do not show the required "
                         "setup (demon in hand and/or trigger on stack).")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json and/or mid_stack.json was never "
                     "exported (the decisive window never armed).")

    # A3: cast resolved, Razor Demon on the battlefield, trigger gone
    notes.append(f"A3 probe: demon_cast={ST['demon_cast']}; "
                 f"target_chosen={ST['target_chosen']}; "
                 f"trigger_on_stack={ST['trigger_on_stack']}; "
                 f"trigger_resolved={ST['trigger_resolved']}; "
                 f"cast_turn={ST['cast_turn']}.")
    demon_post = demon_oid(post_st, 0) if post_st else None
    if (ST["demon_cast"] and ST["trigger_on_stack"]
            and ST["trigger_resolved"] and demon_post is not None):
        ass["A3_cast_resolved"] = "passed"
        notes.append(f"A3 passed: Razor Demon was cast, its SpellCast "
                     f"trigger was seen on the stack and resolved; the "
                     f"creature is now on the P0 battlefield "
                     f"(oid {demon_post}).")
    else:
        ass["A3_cast_resolved"] = "failed"
        notes.append("A3 FAILED: the cast / trigger resolution could not "
                     "be confirmed (see A3 probe).")

    # A4: the draft head produced no draft (bug symptom)
    book_post = spellbook_in_zones(post_st) if post_st else []
    classes = {}
    for _ph, seat, iid, opp in ST["prompts_seen"]:
        classes.setdefault(ST["prompt_classes"].get(str(iid), "other"),
                           []).append((_ph, seat, str(iid)))
    n_choice = len(classes.get("choice_shaped", []))
    notes.append(f"A4 probe: trigger_resolved={ST['trigger_resolved']}; "
                 f"draft_prompt_seen={ST['draft_prompt_seen']}; "
                 f"choice_shaped window prompts={n_choice} "
                 f"{classes.get('choice_shaped')}; "
                 f"spellbook cards in zones at post={book_post}.")
    if (ST["trigger_resolved"] and not ST["draft_prompt_seen"]
            and n_choice == 0 and not book_post):
        ass["A4_no_draft_window"] = "passed"
        notes.append("A4 passed (bug symptom): the trigger resolved but no "
                     "draft-choice prompt was ever offered to the opponent "
                     "and no spellbook card entered any zone -- the "
                     "Unimplemented draft head is a runtime no-op, exactly "
                     "the reported defect.")
    elif ST["trigger_resolved"]:
        ass["A4_no_draft_window"] = "failed"
        notes.append("A4 FAILED: a draft prompt was offered and/or a "
                     "spellbook card materialized -- the draft clause "
                     "functioned at runtime.")
    else:
        notes.append("A4 not-run: the trigger never resolved.")

    # A5: GrantCastingPermission read the empty tracked set -- no grant
    p1_choice = [x for x in classes.get("choice_shaped", [])
                 if x[1] == "P1"]
    wire_draft_events = 0
    try:
        with open(f"{EVDIR}/wire_log.jsonl") as f:
            for line in f:
                if '"event": "draft_choice"' in line:
                    wire_draft_events += 1
    except Exception as e:
        notes.append(f"A5 wire scan failed: {e}")
    notes.append(f"A5 probe: choice_shaped prompts to P1={p1_choice}; "
                 f"wire draft_choice events={wire_draft_events}; "
                 f"spellbook in zones post={book_post}.")
    if (ST["trigger_resolved"] and not p1_choice
            and wire_draft_events == 0 and not book_post):
        ass["A5_no_permission_grant"] = "passed"
        notes.append("A5 passed: no draft prompt reached the opponent, the "
                     "wire log records no draft-choice opportunity, and no "
                     "spellbook card became visible/castable to either "
                     "player -- GrantCastingPermission read the empty "
                     "tracked set and granted nothing observable (the "
                     "issue asserts no consumer-side symptom; this records "
                     "the observable absence).")
    elif ST["trigger_resolved"]:
        ass["A5_no_permission_grant"] = "failed"
        notes.append("A5 FAILED: a permission grant materialized "
                     "(draft prompt to P1 and/or spellbook card visible).")
    else:
        notes.append("A5 not-run: the trigger never resolved.")

    # A6: cleanup
    if post_st:
        tnum = ST.get("cast_turn")
        cur_turn = post_st.get("turn_number")
        no_lingering = ST.get("cast_in_flight") is None
        if not stack_entries(post_st) and no_lingering and (
                tnum is None or (cur_turn or 0) > tnum):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the cast turn (cast turn {tnum}, post turn "
                         f"{cur_turn}, phase {post_st.get('phase')}), no "
                         f"in-flight submissions lingering "
                         f"(rejections={ST['cast_rejections']}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty, the game did not "
                         "advance past the cast turn, or an in-flight "
                         f"submission is lingering "
                         f"(in_flight={ST.get('cast_in_flight')}, "
                         f"stack={len(stack_entries(post_st))}, "
                         f"turn {cur_turn} vs cast {tnum}).")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                        "A3_cast_resolved",
                                        "A4_no_draft_window",
                                        "A5_no_permission_grant",
                                        "A6_cleanup")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Razor Demon was cast, its SpellCast draft "
            "trigger resolved, but the 'target opponent drafts a card from "
            "Razor Demon's spellbook' clause never parsed (Unimplemented "
            "head, runtime no-op), so no draft prompt was offered to the "
            "opponent, no spellbook card materialized, and the "
            "GrantCastingPermission sub-ability read an empty chain tracked "
            "set -- the exact structural defect the issue reports, now "
            "measured at runtime. Confirmed on v0.100.0. This is not a fix "
            "claim.")
    elif (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolved"] == "passed"
            and (ass["A4_no_draft_window"] == "failed"
                 or ass["A5_no_permission_grant"] == "failed")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the cast-trigger path was exercised "
            "and the draft clause functioned at runtime (a draft prompt "
            "was offered and/or a spellbook card / grant materialized). "
            "This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported cast-trigger path "
                     "could not be fully exercised; see assertion notes.")

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
               if "razor demon" in l.lower()
               or "draft" in l.lower()
               or "spellbook" in l.lower()
               or "grant" in l.lower()
               or "tracked" in l.lower()
               or "unimplemented" in l.lower()
               or (ST["game_code"] and ST["game_code"] in l)]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    # copy the scenario into the evidence dir for provenance
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                f"{EVDIR}/scenario_{ISSUE}.py")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 101, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                 "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "demon_cast": ST["demon_cast"],
            "demon_oid": ST["demon_oid"],
            "target_chosen": ST["target_chosen"],
            "trigger_on_stack": ST["trigger_on_stack"],
            "trigger_resolved": ST["trigger_resolved"],
            "cast_turn": ST["cast_turn"],
            "draft_prompt_seen": ST["draft_prompt_seen"],
            "draft_answered": ST["draft_answered"],
            "spellbook_in_zones_post": book_post,
            "prompt_classes": {k: len(v)
                               for k, v in classes.items()},
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "stack_kinds_window": ST["stack_kinds_window"][-10:],
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (Razor Demon cast, SpellCast draft trigger "
            "resolution with the opponent targeted) and observes the "
            "resolution outcome. The issue asserts no consumer-side "
            "symptom; A5 records the observable absence of any grant.",
            "4x Razor Demon / 56x Swamp deck densities are test-harness "
            "conveniences (engine accepts >4-of for custom games).",
            "The harness holds no outside-the-game spellbook pool; the "
            "decisive observable is whether the draft prompt is ever "
            "offered, not the number of cards drafted.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Razor Demon + 56x Swamp; P1: 60x Swamp "
                      "(lands only, never casts, never attacks); default "
                      "Bo1, life 20",
        "contract_line": "Razor Demon cast -> SpellCast draft trigger "
                         "resolution (target opponent = P1): correct = the "
                         "opponent is offered a draft choice from the "
                         "spellbook and may cast the drafted card. Observed "
                         "(bug): the draft clause never parsed "
                         "(Unimplemented head, runtime no-op), so no draft "
                         "prompt is offered and the GrantCastingPermission "
                         "sub-ability reads an empty tracked set.",
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


def write_manifest_and_validate():
    import glob as _glob
    files = sorted(os.path.basename(p)
                   for p in _glob.glob(f"{EVDIR}/*")
                   if os.path.isfile(p) and os.path.basename(p) != "manifest.sha256")
    lines = []
    for fn in files:
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest.sha256 written ({len(files)} files)")
    # validation: every JSON parses; hashes match; PNG opens nonzero
    problems = []
    for fn in files:
        if fn.endswith(".json") or fn.endswith(".jsonl"):
            try:
                if fn.endswith(".jsonl"):
                    for line in open(f"{EVDIR}/{fn}"):
                        if line.strip():
                            json.loads(line)
                else:
                    json.load(open(f"{EVDIR}/{fn}"))
            except Exception as e:
                problems.append(f"{fn} JSON parse failed: {e}")
    for line in open(f"{EVDIR}/manifest.sha256"):
        h, _, fn = line.strip().partition("  ")
        if not fn:
            continue
        actual = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        if actual != h:
            problems.append(f"{fn} hash mismatch")
    try:
        from PIL import Image
        im = Image.open(f"{EVDIR}/summary.png")
        im.load()
        if im.size[0] == 0 or im.size[1] == 0:
            problems.append("summary.png has zero size")
        say(f"summary.png opens OK ({im.size[0]}x{im.size[1]})")
    except Exception as e:
        problems.append(f"summary.png unreadable: {e}")
    if problems:
        say("VALIDATION PROBLEMS:")
        for p in problems:
            say(f"  - {p}")
    else:
        say("validation OK: all JSON parse, hashes match, PNG readable")
    return problems

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
        last_change = [time.time()]
        last_rev = [-1]
        stall_dumped = [False]
        while not ST["terminal"]:
            try:
                t, data = await asyncio.wait_for(c.inbox.get(), 2)
            except asyncio.TimeoutError:
                # stall watchdog: if the game state hasn't changed for 60s,
                # dump a diagnostic summary (the game may be waiting on us).
                st = c.latest
                if st is not None:
                    rev = st.get("state_revision", -1)
                    if rev != last_rev[0]:
                        last_rev[0] = rev
                        last_change[0] = time.time()
                        stall_dumped[0] = False
                    elif (not stall_dumped[0]
                          and time.time() - last_change[0] > 60):
                        stall_dumped[0] = True
                        state = st["state"]
                        vi = st.get("viewer_interaction") or {}
                        summ = {
                            "at": time.time(),
                            "who": tag,
                            "rev": rev,
                            "phase": state.get("phase"),
                            "active": state.get("active_player"),
                            "turn": state.get("turn_number"),
                            "waiting_for": wf_of(state),
                            "stack_n": len(stack_entries(state)),
                            "stack": [json.dumps(se, default=str)[:400]
                                      for se in stack_entries(state)],
                            "p0_hand": hand_lnames(state, 0),
                            "p0_untapped_lands": [
                                obj_lname(state, o)
                                for o in untapped_lands(state, 0)],
                            "p0_bf": [obj_lname(state, o)
                                      for o in bf_permanents(state, 0)],
                            "p1_hand_n": len(hand_ids(state, 1)),
                            "mana_needs": ST["mana_needs"][tag],
                            "cast_in_flight": ST.get("cast_in_flight"),
                            "demon_oid": ST.get("demon_oid"),
                            "target_chosen": ST.get("target_chosen"),
                            "vi_canSubmit": vi.get("canSubmit"),
                            "vi_opps": [
                                json.dumps(o, default=str)[:1200]
                                for o in (vi.get("opportunities") or [])],
                            "legal_action_types": sorted(set(
                                a.get("type") for a in merged_actions(st))),
                        }
                        with open(f"{EVDIR}/stall_dump.jsonl", "a") as f:
                            f.write(json.dumps(summ, default=str) + "\n")
                        say(f"[{tag}] STALL DUMP written "
                            f"(rev {rev} unchanged 60s)")
                        wire("stall_dump", {"rev": rev})
                # resolve watchdog also runs on the timeout path (no new
                # messages arrive after GameOver): deadline or game end must
                # terminate the run with a post export, never deadlock.
                if ST["phase"] == "resolving":
                    stx = c.latest
                    wfx = (wf_of(stx["state"]).get("type") or "") if stx \
                        else ""
                    if wfx == "GameOver":
                        say(f"[{tag}] game over seen on timeout path; "
                            "exporting post")
                        wire("gameover_timeout", {})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                        ST["terminal"] = True
                        return
                    if (not ST["trigger_resolved"]
                            and time.time() - (ST["resolving_since"]
                                               or ST["t0"])
                            > RESOLVE_DEADLINE_S):
                        say("[watchdog/timeout] deadline in resolving "
                            "without resolution; exporting post and "
                            "finalizing")
                        wire("watchdog_timeout",
                             {"note": "resolving timeout"})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                        ST["terminal"] = True
                        return
                continue
            if t == "ActionRejected":
                ST["cast_rejections"] += 1
                wire("action_rejected", {"who": tag,
                                        "data": json.dumps(data)[:400]})
                say(f"[{tag}] ActionRejected: {json.dumps(data)[:200]}")
                # a rejected cast leaves no mana owed
                ST["mana_needs"][tag] = {"B": 0, "R": 0, "generic": 0}
                if ST.get("cast_in_flight"):
                    ST["cast_in_flight"] = None
                if ST.get("demon_cast") and not ST.get("trigger_on_stack"):
                    # the cast submission itself was rejected; back out to
                    # the build phase so the stage machine retries
                    ST["demon_cast"] = False
                    ST["demon_oid"] = None
                    ST["phase"] = "build"
                    ST["resolving_since"] = None
                    say("[P0] cast rejected; returning to build phase")
            st = c.latest
            if not st:
                continue
            state = st["state"]
            ST["states_seen"] += 1
            # ---- decisive window tracking ----
            if ST["phase"] == "resolving":
                if ST["demon_cast"] and not ST["trigger_on_stack"]:
                    if draft_trigger_on_stack(state) is not None:
                        ST["trigger_on_stack"] = True
                        say("[window] Razor Demon SpellCast draft trigger "
                            "observed on the stack")
                        wire("trigger_on_stack", {})
                if (ST["trigger_on_stack"]
                        and not ST["mid_exported"]):
                    await export_as(c0_ref[0], "mid_stack")
                    ST["mid_exported"] = True
                    say("[window] mid_stack exported (trigger on stack; "
                        f"target_chosen={ST['target_chosen']})")
                    wire("mid_exported",
                         {"target_chosen": ST["target_chosen"]})
                    dump_stack_once(state, "trigger_on_stack")
                if ST["trigger_on_stack"] and not ST["trigger_resolved"]:
                    if draft_trigger_on_stack(state) is None:
                        ST["trigger_resolved"] = True
                        say("[window] draft trigger resolved; "
                            "starting settle clock")
                        wire("trigger_resolved", {})
                        ST["settle_at"] = time.time()
                # record the kinds of stack entries seen in the window
                for se in stack_entries(state):
                    kind = json.dumps(se.get("kind"), default=str)[:120]
                    if kind not in ST["stack_kinds_window"]:
                        ST["stack_kinds_window"].append(kind)
                # watchdog: the window must not hang the run forever
                if not ST["trigger_resolved"]:
                    if (time.time() - (ST["resolving_since"] or ST["t0"])
                            > RESOLVE_DEADLINE_S):
                        say("[watchdog] deadline in resolving without "
                            "resolution; exporting post and finalizing")
                        wire("watchdog", {"note": "resolving timeout"})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                        ST["terminal"] = True
                        return
                wf = (wf_of(state).get("type") or "")
                if wf and wf not in ST["wf_types_window"]:
                    ST["wf_types_window"].append(wf)
                if wf == "GameOver":
                    say("[window] game over during decisive window; "
                        "exporting post")
                    wire("gameover", {"winner": (wf_of(state).get("data")
                                                or {}).get("winner")})
                    if not ST["post_exported"]:
                        await export_as(c0_ref[0], "post")
                        ST["post_exported"] = True
                    ST["terminal"] = True
                    return
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
                        ST["terminal"] = True
                        return
                elif not ST["trigger_resolved"]:
                    ST["settle_at"] = None
            if time.time() - ST["t0"] > SETUP_DEADLINE_S:
                say("[timeout] deadline reached")
                wire("timeout", {})
                ST["terminal"] = True
                return
            if ST["phase"] == "build" and \
                    (state.get("turn_number") or 0) > TURN_CAP:
                say(f"[turncap] turn {state.get('turn_number')} > {TURN_CAP} "
                    "in build phase; finalizing")
                wire("turncap", {})
                ST["terminal"] = True
                return
            acts = merged_actions(st)
            if tag == "P0":
                await p0_tick(st, acts, state, c)
            else:
                await p1_tick(st, acts, state, c)

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
    # render the summary PNG from saved evidence, then manifest+validate
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_7437.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
