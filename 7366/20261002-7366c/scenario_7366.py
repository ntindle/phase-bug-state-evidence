#!/usr/bin/env python3
"""Issue #7366: Dusk // Dawn -- the Aftermath half (Dawn) cannot be played
from the graveyard because it is absent from the shipped card data entirely.

First backfill run for this issue, on v0.99.0 / protocol 98.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-08-13, classifier: unsupported_aspect,
status:confirmed): "Dawn/Dusk - Does not allow a card to be played from
graveyard". Triage (mike-theDude, 2026-08-15) clarified: after Dusk has been
cast and is in the graveyard, Dawn (Aftermath: castable only from the
graveyard, then exiled) cannot be played from the graveyard. Triage
confirmed by data inspection that the `dusk` entry in card-data.json holds
ONLY the front half (oracle text "Destroy all creatures with power 3 or
greater.", no faces/layout) and no `dawn` key exists -- a card-data
ingestion defect, likely a class covering all Aftermath split cards.

Per PLAYBOOK.md: "If a missing card or failed import is itself the reported
bug, demonstrating it is a reproduction, not a missing-prerequisite
blocker." So the scenario asserts the missing-card defect at three levels:

  data level    - the pinned v0.99.0 card-data.json has no Dawn half;
  corpus level  - how many split-card back halves are missing (class scope);
  runtime level - the engine rejects a deck containing "Dawn" as an
                  unresolvable card name, and a live game shows Dusk cast
                  and in the graveyard with no Dawn cast opportunity.

Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Dusk + 28x Swamp + 28x Plains (mixed mana: the dusk data entry
      carries a {2}{W}{W} mana_cost while its oracle text is the {2}{B}{B}
      Dusk half, so either color may be demanded -- covered both).
  P1: 60x Forest dummy (never attacks, never blocks).

Drive:
  P0 mulligans (max 2) unless the opener holds Dusk and >=2 lands.
  T1+: play a land each turn (balancing colors); once Dusk is in hand with
  >=5 lands on the battlefield (2+ swamps and 2+ plains among them), cast
  Dusk. Log the ManaPayment request verbatim (records which colors the
  engine actually demands for the Dusk half), pay it via offered PayMana
  actions, let Dusk resolve (no creatures with power 3+ exist) so it goes
  to the graveyard. Then, at P0's next main phase with priority and mana
  available, scan every legal action, viewer-interaction opportunity, and
  the full state for any mention of "dawn".

Assertions:
  A1_data_entry  the pinned card-data.json `dusk` entry contains ONLY the
                 Dusk half (oracle_text == "Destroy all creatures with power
                 3 or greater.", no faces/layout, face_index 0) and NO
                 `dawn` / `dusk // dawn` key exists. FAIL = the reported
                 data defect is gone.
  A2_corpus      all 27 back halves of face-0-only split cards (20 Aftermath
                 + 7 regular Amonkhet-block splits) are absent from the
                 data. Recorded as class scope; passes iff the corpus check
                 runs and its count/list is preserved.
  A3_deck_reject CreateGameWithSettings with a deck containing "Dawn" is
                 rejected with Error "Unresolvable card names: main:Dawn".
                 FAIL = Dawn is suddenly a resolvable card.
  A4_dusk_cast   Dusk was cast from hand in the live game (control: the
                 front half is a working card).
  A5_dusk_gy     Dusk resolved and is in P0's graveyard at post.
  A6_no_dawn     with Dusk in the graveyard, P0 holding priority and mana
                 available, no legal action / interaction opportunity /
                 state text mentions "dawn" (case-insensitive full scan).
                 FAIL = a Dawn cast path exists.
  A7_cost_logged the ManaPayment request for Dusk was observed and logged
                 verbatim (informational: records whether the engine
                 demands {2}{W}{W} per the data's mana_cost or {2}{B}{B}
                 per the true card).

Verdict rule: reproduced iff A1, A3 and A6 passed (the bug is the missing
              Dawn half; A2 is class scope, A4/A5 are controls, A7 is
              informational);
              not-reproduced iff A1 failed (Dawn half present in data) and
              A6 failed (a Dawn cast path exists);
              blocked iff A4 failed (couldn't drive the gameplay control).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
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
RUN_ID = "20261002-7366c"
ISSUE = 7366
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

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
    "server_run_id": "live v0.99.0 server on 127.0.0.1:9374 "
                     "(reused per task body: already listening with pinned release)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.99.0 == pinned release dir; "
              "hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))

DUSK = "dusk"
SWAMP = "swamp"
PLAINS = "plains"
FOREST = "forest"

P0_DECK = [("Dusk", 4), ("Swamp", 28), ("Plains", 28)]
P1_DECK = [("Forest", 60)]

# The 27 face-0-only fronts and their known back halves (Amonkhet/HOU splits).
SPLIT_BACKS = {
    "appeal": "authority", "claim": "fame", "commit": "memory",
    "consign": "oblivion", "cut": "ribbons", "destined": "lead",
    "driven": "despair", "dusk": "dawn", "failure": "comply",
    "farm": "market", "grind": "dust", "heaven": "earth",
    "indulge": "excess", "insult": "injury", "leave": "chance",
    "mouth": "feed", "never": "return", "onward": "victory",
    "prepare": "fight", "rags": "riches", "reason": "believe",
    "reduce": "rubble", "refuse": "cooperate", "road": "ruin",
    "spring": "mind", "start": "finish", "struggle": "survive",
}

SETUP_DEADLINE_S = 1500

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()
_DISCARD_REV = {}


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "dusk_cast": False,
        "dusk_oid": None,  # object id of the cast Dusk (tracked by id, not name)
        "dusk_cast_at": None,
        "dusk_on_stack": False,
        "dusk_resolved": False,
        "mana_payment_seen": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "dawn_mentions": None,
        "test_done": False,
        "ass": {k: "not-run" for k in ("A1_data_entry", "A2_corpus",
                                       "A3_deck_reject", "A4_dusk_cast",
                                       "A5_dusk_gy", "A6_no_dawn",
                                       "A7_cost_logged")},
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


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in player_of(state, pid).get("hand", [])]


def gy_lnames(state, pid):
    return [obj_lname(state, o) for o in player_of(state, pid).get("graveyard", [])]


def gy_ids(state, pid, key):
    return [int(o) for o in player_of(state, pid).get("graveyard", [])
            if obj_lname(state, o) == key]


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower() == key]


def untapped_lands(state, pid, key):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and str(o.get("base_name") or o.get("name") or "").lower() == key]


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


def cast_action_for(acts, state, key):
    """Returns (action, object_id) for a cast action of the named card."""
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


# ------------------------------------------------------------- data checks
def check_data_level():
    """A1/A2: data-level and corpus-level checks against pinned card-data."""
    dusk = CARD_DATA.get("dusk")
    findings = {
        "dusk_key_present": dusk is not None,
        "dusk_oracle_text": (dusk or {}).get("oracle_text"),
        "dusk_mana_cost": (dusk or {}).get("mana_cost"),
        "dusk_faces": (dusk or {}).get("faces"),
        "dusk_layout": (dusk or {}).get("layout"),
        "dusk_face_index": (dusk or {}).get("face_index"),
        "dawn_key_present": "dawn" in CARD_DATA,
        "dusk_dawn_key_present": "dusk // dawn" in CARD_DATA,
    }
    # corpus: every known back half must be absent
    missing_backs = sorted(b for f, b in SPLIT_BACKS.items() if b not in CARD_DATA)
    present_backs = sorted(b for f, b in SPLIT_BACKS.items() if b in CARD_DATA)
    fronts_present = sorted(f for f in SPLIT_BACKS if f in CARD_DATA)
    findings["corpus"] = {
        "fronts_checked": len(SPLIT_BACKS),
        "fronts_present": fronts_present,
        "backs_missing": missing_backs,
        "backs_present": present_backs,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)

    ass = ST["ass"]
    notes = ST["notes"]
    if (findings["dusk_key_present"]
            and findings["dusk_oracle_text"] == "Destroy all creatures with power 3 or greater."
            and findings["dusk_faces"] is None
            and not findings["dawn_key_present"]
            and not findings["dusk_dawn_key_present"]):
        ass["A1_data_entry"] = "passed"
        notes.append("A1 passed: pinned card-data.json `dusk` entry holds ONLY "
                     "the Dusk half (oracle_text is the Dusk half alone, no "
                     "faces/layout); no `dawn` or `dusk // dawn` key exists.")
    else:
        ass["A1_data_entry"] = "failed"
        notes.append(f"A1 FAILED: dusk entry = {json.dumps(findings, default=str)[:600]}")

    if findings["corpus"]["backs_present"]:
        notes.append("A2 note: some back halves ARE present: "
                     f"{findings['corpus']['backs_present']}")
    ass["A2_corpus"] = "passed"
    notes.append(f"A2 passed (class scope recorded): {len(missing_backs)}/"
                 f"{len(SPLIT_BACKS)} split-card back halves missing from "
                 f"pinned data: {', '.join(missing_backs)}")
    say(f"A1={ass['A1_data_entry']} A2={ass['A2_corpus']}: "
        f"{len(missing_backs)} missing backs")
    wire("data_level", findings)


async def check_deck_reject():
    """A3: the engine rejects a deck containing the missing Dawn half."""
    import websockets as _ws
    ass = ST["ass"]
    notes = ST["notes"]
    uri = "ws://127.0.0.1:9374/ws"
    result = {}
    try:
        async with _ws.connect(uri, max_size=200_000_000) as ws:
            await asyncio.wait_for(ws.recv(), 5)  # ServerHello
            from client import HELLO
            await ws.send(json.dumps(HELLO))
            await ws.send(json.dumps({
                "type": "CreateGameWithSettings",
                "data": {
                    "deck": deck(("Dawn", 4), ("Swamp", 56)),
                    "display_name": "reject-probe", "public": False,
                    "password": None, "timer_seconds": None,
                    "player_count": 2,
                    "match_config": {"match_type": "Bo1"},
                    "ai_seats": [], "format_config": None,
                    "room_name": None, "host_peer_id": None,
                    "draft_metadata": None, "start_when_full": True,
                    "ranked": False,
                }}))
            for _ in range(40):
                msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                t = msg.get("type")
                if t in ("Error", "SessionAttached", "GameCreated"):
                    result = {"type": t, "data": msg.get("data", {})}
                    break
    except Exception as e:
        result = {"probe_error": f"{type(e).__name__}: {e}"}
    with open(f"{EVDIR}/deck_reject.json", "w") as f:
        json.dump({"deck": ["Dawn x4", "Swamp x56"], "response": result}, f, indent=1)
    wire("deck_reject", result)
    say(f"deck-reject probe response: {json.dumps(result)[:300]}")
    if result.get("type") == "Error" and "Dawn" in json.dumps(result.get("data", {})):
        ass["A3_deck_reject"] = "passed"
        notes.append(f"A3 passed: engine rejected the Dawn deck with Error: "
                     f"{json.dumps(result['data'])[:200]}")
    else:
        ass["A3_deck_reject"] = "failed"
        notes.append(f"A3 FAILED: unexpected response {json.dumps(result)[:300]}")


# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    await c.send_action(a)


async def do_mulligan_p0(c, pid, tag):
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
    lands = [n for n in hn if n in (SWAMP, PLAINS)]
    mkey = (tag, "mulligan")
    mulls = MULL_COUNT.get(mkey, 0)
    if not (DUSK in hn and len(lands) >= 2) and len(hn) > 5 and mulls < 2:
        MULL_COUNT[mkey] = mulls + 1
        say(f"[P0] mulligan #{mulls + 1} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    else:
        MULLS.add(tag)
        say(f"[P0] keep {len(hn)} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_mulligan_keep(c, pid, tag):
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
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
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
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    # keep Dusk first, then lands
    rank = {DUSK: 0, SWAMP: 1, PLAINS: 1}
    picks = sorted(hand, key=lambda o: rank.get(obj_lname(state, o), 2),
                   reverse=True)[:n]
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
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    n = len(hand) - 7
    if n <= 0:
        return False
    # discard lands first, never Dusk
    rank = {SWAMP: 0, PLAINS: 0, DUSK: 2}
    picks = [int(x) for x in sorted(
        hand, key=lambda o: rank.get(obj_lname(state, o), 1))[:n]]
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


# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_p0(c, 0, "P0"):
            return
        if await do_bottom(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    if await pay_tick(c, acts, "P0"):
        return
    if await do_discard(c, 0, "P0"):
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return

    # ---- ManaPayment: log the FULL request verbatim (A7), then pay ----
    if "ManaPayment" in wtype:
        if ST["mana_payment_seen"] is None:
            ST["mana_payment_seen"] = wf_of(state)
            wire("mana_payment_FULL", {"waiting_for": wf_of(state),
                                       "vi": st.get("viewer_interaction")})
            say(f"[P0] ManaPayment for Dusk: "
                f"{json.dumps(wf_of(state), default=str)[:800]}")
        return True  # pay_tick above handles the actual payment

    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")

    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        in_flight = any((e.get("kind") or {}).get("type") == "Spell"
                        and e.get("controller") == 0
                        for e in stack_entries(state))
        # 1) cast Dusk once we can cover either possible cost
        if not ST["dusk_cast"] and DUSK in hand_lnames(state, 0) \
                and not in_flight:
            sw = untapped_lands(state, 0, SWAMP)
            pl = untapped_lands(state, 0, PLAINS)
            if len(sw) + len(pl) >= 5 and len(sw) >= 2 and len(pl) >= 2:
                a, dusk_oid = cast_action_for(acts, state, DUSK)
                if a:
                    say("[P0] casting Dusk "
                        f"(oid {dusk_oid}; untapped swamps={len(sw)}, "
                        f"plains={len(pl)})")
                    wire("cast_dusk", {"action": a["type"],
                                       "dusk_oid": dusk_oid,
                                       "untapped_swamps": len(sw),
                                       "untapped_plains": len(pl)})
                    await submit_as_is(c, a)
                    ST["dusk_cast"] = True
                    ST["dusk_oid"] = dusk_oid
                    ST["dusk_cast_at"] = time.time()
                    return
        # 2) land drop: balance colors
        n_sw = len(bf_ids(state, 0, SWAMP))
        n_pl = len(bf_ids(state, 0, PLAINS))
        hand = hand_lnames(state, 0)
        want = SWAMP if n_sw <= n_pl and SWAMP in hand else \
            (PLAINS if PLAINS in hand else SWAMP if SWAMP in hand else None)
        if want:
            for a in acts:
                if a["type"] == "PlayLand":
                    d = a.get("data", {})
                    # PlayLand may or may not name the land; only play if
                    # the chosen land is in hand
                    try:
                        if obj_lname(state, int(d.get("card") or d.get("object") or -1)) != want:
                            continue
                    except (TypeError, ValueError):
                        pass
                    await submit_as_is(c, a)
                    return
            # fallback: any PlayLand
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_keep(c, 1, "P1"):
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


# ------------------------------------------------------------- observation
async def export_pre(c):
    env = await c.export_state()
    with open(f"{EVDIR}/pre.json", "w") as f:
        f.write(env)
    ST["pre_exported"] = True
    say("exported pre.json")


async def export_mid(c):
    env = await c.export_state()
    with open(f"{EVDIR}/mid_cast.json", "w") as f:
        f.write(env)
    ST["mid_exported"] = True
    say("exported mid_cast.json")


async def export_post(c):
    env = await c.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(env)
    ST["post_exported"] = True
    say("exported post.json")


def note_dusk_on_stack(state):
    """Detect the cast Dusk on the stack by OBJECT ID (stack Spell entries
    reference the card by id; the name need not appear in the entry)."""
    if ST["dusk_on_stack"]:
        return
    oid = ST["dusk_oid"]
    if oid is None:
        return
    on_stack_by_zone = (get_obj(state, oid).get("zone") == "Stack")
    on_stack_by_ref = any(
        (e.get("kind") or {}).get("type") == "Spell"
        and str(e.get("source_id")) == str(oid)
        for e in stack_entries(state))
    if on_stack_by_zone or on_stack_by_ref:
        ST["dusk_on_stack"] = True
        say(f"DUSK oid {oid} observed on stack "
            f"(by_zone={on_stack_by_zone}, by_ref={on_stack_by_ref})")
        wire("dusk_on_stack", {"oid": oid, "by_zone": on_stack_by_zone,
                               "by_ref": on_stack_by_ref})


def dusk_oid_in_gy(state):
    oid = ST["dusk_oid"]
    if oid is None:
        return False
    if str(oid) in [str(o) for o in player_of(state, 0).get("graveyard", [])]:
        return True
    return get_obj(state, oid).get("zone") == "Graveyard"


def scan_for_dawn(st, state, acts):
    """Full case-insensitive scan for any mention of 'dawn' across the
    client view state, legal actions, and viewer interaction."""
    hay = json.dumps({
        "state": state,
        "legal_actions": acts,
        "viewer_interaction": st.get("viewer_interaction"),
    }, default=str).lower()
    return hay.count("dawn")


# ------------------------------------------------------------- finalization
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]
    pre = mid = post = None
    for name, var in (("pre", "pre"), ("mid_cast", "mid"), ("post", "post")):
        try:
            v = json.load(open(f"{EVDIR}/{name}.json"))
        except Exception as e:
            notes.append(f"{name} load failed: {e}")
            v = None
        if var == "pre":
            pre = v
        elif var == "mid":
            mid = v
        else:
            post = v
    pre_st = (pre or {}).get("state") or {}
    post_st = (post or {}).get("state") or {}

    # A4: Dusk was cast (control)
    if ST["dusk_cast"] or ST["dusk_on_stack"] or ST["dusk_resolved"]:
        ass["A4_dusk_cast"] = "passed"
        notes.append("A4 passed: Dusk was cast from hand in the live game "
                     "(front half is a working card).")
    else:
        ass["A4_dusk_cast"] = "failed"
        notes.append("A4 FAILED: Dusk was never cast (mana base or driver "
                     "issue); the gameplay control is moot.")

    # A5: Dusk in P0's graveyard at post
    if post_st:
        dusk_gy = gy_lnames(post_st, 0).count(DUSK) >= 1
        if dusk_gy:
            ass["A5_dusk_gy"] = "passed"
            notes.append("A5 passed: Dusk resolved and is in P0's graveyard "
                         f"at post (turn {post_st.get('turn_number')}).")
        else:
            ass["A5_dusk_gy"] = "failed"
            notes.append(f"A5 FAILED: Dusk not in P0 graveyard at post; "
                         f"gy={gy_lnames(post_st, 0)}")
    else:
        notes.append("A5 not-run: no post state")

    # A6: no Dawn mention anywhere with Dusk in the graveyard
    if ST["dawn_mentions"] is not None:
        if ST["dawn_mentions"] == 0 and ass["A5_dusk_gy"] == "passed":
            ass["A6_no_dawn"] = "passed"
            notes.append("A6 passed: with Dusk in P0's graveyard and P0 "
                         "holding priority with mana available, a full "
                         "case-insensitive scan of state + legal actions + "
                         "viewer interaction found ZERO mentions of 'dawn' "
                         "-- no cast-from-graveyard path exists.")
        elif ST["dawn_mentions"] != 0:
            ass["A6_no_dawn"] = "failed"
            notes.append(f"A6 FAILED: found {ST['dawn_mentions']} mentions of "
                         "'dawn' in the post state/actions -- a Dawn path "
                         "exists?!")
        else:
            notes.append("A6 not-run: A5 failed")
    else:
        notes.append("A6 not-run: scan never ran")

    # A7: cost observation (informational)
    if ST["mana_payment_seen"] is not None:
        ass["A7_cost_logged"] = "passed"
        notes.append("A7 passed: ManaPayment request for Dusk observed and "
                     "logged verbatim in wire_log (event mana_payment_FULL).")
    else:
        notes.append("A7 not-run: no ManaPayment observed")

    if ass["A1_data_entry"] == "passed" and ass["A3_deck_reject"] == "passed" \
            and ass["A6_no_dawn"] == "passed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: the Dawn (Aftermath) half is absent "
                     "from the pinned card data, the engine rejects it as an "
                     "unresolvable card, and a live game with Dusk in the "
                     "graveyard offers no Dawn cast path.")
    elif ass["A1_data_entry"] == "failed" and ass["A6_no_dawn"] == "failed":
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: the Dawn half is present and a "
                     "cast path exists.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: inconclusive assertion set")

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
               if "dusk" in l.lower() or "dawn" in l.lower()
               or "unresolvable" in l.lower()]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-120:]) + "\n")
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
            open(f"{BACKFILL}/driver/scenario_7366.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "dusk_cast": ST["dusk_cast"],
            "dusk_on_stack": ST["dusk_on_stack"],
            "dusk_resolved": ST["dusk_resolved"],
            "mana_payment_seen": ST["mana_payment_seen"] is not None,
            "dawn_mentions_at_post": ST["dawn_mentions"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "4x Dusk deck density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The Dawn half's true Oracle text is deliberately not quoted: it "
            "is absent from the pinned data, so there is nothing authoritative "
            "to quote from (same sourcing rule as the issue triage).",
            "A6 scans the viewer-filtered post state plus legal actions and "
            "interaction opportunities; the authoritative post export is "
            "preserved for independent inspection.",
            "States are authoritative exports, restorable only via full game "
            "replay; the data-level finding is restorable by re-reading the "
            "pinned card-data.json (sha256 in server identity).",
        ],
        "setup_line": "P0: 4x Dusk + 28x Swamp + 28x Plains; P1: 60x Forest",
        "contract_line": "Dawn (Aftermath) is absent from card data: engine "
                         "rejects it as unresolvable and no graveyard cast "
                         "path exists after Dusk resolves",
        "prior_runs": [],
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
    return verdict


# ------------------------------------------------------------- main
async def main():
    reset()
    # Phase 1+2: data-level, corpus-level, and deck-reject checks (no game).
    check_data_level()
    await check_deck_reject()

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
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                if tag == "P0":
                    note_dusk_on_stack(state)
                    # Dusk left the stack -> resolved to graveyard
                    if ST["dusk_on_stack"] and not ST["dusk_resolved"]:
                        if dusk_oid_in_gy(state):
                            ST["dusk_resolved"] = True
                            say(f"DUSK oid {ST['dusk_oid']} resolved -> "
                                f"P0 graveyard")
                            wire("dusk_resolved", {"oid": ST["dusk_oid"]})
                    # watchdog: cast submitted but Dusk never reached the
                    # stack within 180s -> export stuck state, finalize
                    if ST["dusk_cast"] and not ST["dusk_on_stack"] \
                            and not ST["post_exported"]:
                        if now - (ST["dusk_cast_at"] or now) > 180:
                            wire("cast_stuck",
                                 {"waiting_for": wf_of(state),
                                  "dusk_oid": ST["dusk_oid"],
                                  "dusk_zone": get_obj(
                                      state, ST["dusk_oid"]).get("zone")})
                            say("Dusk cast but never reached the stack in "
                                "180s -- exporting stuck state")
                            await export_post(c)
                            finalized = True
                            break
                    # pre export: about to cast (Dusk in hand, main, priority)
                    if (not ST["pre_exported"] and not ST["dusk_cast"]
                            and DUSK in hand_lnames(state, 0)
                            and my_priority(state, 0)
                            and state.get("phase") in ("PreCombatMain",
                                                       "PostCombatMain")
                            and state.get("active_player") == 0):
                        await export_pre(c)
                    # mid export: Dusk on the stack
                    if ST["dusk_on_stack"] and not ST["mid_exported"]:
                        await export_mid(c)
                    # post export: Dusk in graveyard, P0 priority, quiet
                    if ST["dusk_resolved"] and not ST["post_exported"]:
                        if my_priority(state, 0) \
                                and state.get("phase") in ("PreCombatMain",
                                                           "PostCombatMain") \
                                and not stack_entries(state):
                            acts = merged_actions(st)
                            ST["dawn_mentions"] = scan_for_dawn(st, state, acts)
                            wire("dawn_scan",
                                 {"mentions": ST["dawn_mentions"],
                                  "p0_gy": gy_lnames(state, 0)[:8]})
                            say(f"dawn scan: {ST['dawn_mentions']} mentions; "
                                f"P0 gy has dusk="
                                f"{DUSK in gy_lnames(state, 0)}")
                            await asyncio.sleep(1.0)
                            await export_post(c)
                            say(f"test complete at turn "
                                f"{state.get('turn_number')}")
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
    verdict = await finalize(p0)
    await p0.close()
    await p1.close()
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    # copy the scenario into the evidence dir for the manifest
    shutil.copy(f"{BACKFILL}/driver/scenario_7366.py",
                f"{EVDIR}/scenario_7366.py")
    sys.exit(0)
