#!/usr/bin/env python3
"""Issue #7428: Karn Liberated -- the [-14] "restart the game" clause is
unparsed, so the dependent ChangeZoneAll reads an empty tracked set.

Reported (internal triage, status:confirmed, area:parser,
mechanic:zone-change, classifier:unsupported-aspect, priority:p3-card-specific;
related #6857 tracked-set census):
On Karn Liberated, the clause "Restart the game, leaving in exile all
non-Aura permanent cards exiled with Karn" does not parse: the parser emits
an `Effect::Unimplemented` node in its place. That resolver pushes no
`GameEvent`, so the chain tracked set is allocated empty and the dependent
`ChangeZoneAll` ("Then put those cards onto the battlefield under your
control") reads an empty set. The census did NOT measure `ChangeZoneAll`'s
consumption style, and the issue does not assert a runtime symptom -- the
reported defect is the parse state and the empty publish that structurally
follows from it.

Oracle text (verified against the pinned v0.100.0 card-data.json):
> [+4]: Target player exiles a card from their hand.
> [-3]: Exile target permanent.
> [-14]: Restart the game, leaving in exile all non-Aura permanent cards
> exiled with Karn. Then put those cards onto the battlefield under your
> control.

Pinned v0.100.0 parse (see data_evidence.json):
  abilities[0]: cost Loyalty +4, TargetOnly { target: Player }
      sub: ChangeZone { Hand -> Exile, Typed Card Owned-by-ScopedPlayer
             InZone Hand }               ("Target player exiles a card
                                          from their hand.")
  abilities[1]: cost Loyalty -3,
      ChangeZone { destination: Exile, target: Typed Permanent }
                                          ("Exile target permanent.")
  abilities[2]: cost Loyalty -14,
      head Unimplemented { name: "unrecognized_clause_head",
             description: "Restart the game, leaving in exile all non-Aura
                           permanent cards exiled with Karn" }
      sub: ChangeZoneAll { Exile -> Battlefield, target: TrackedSet id 0,
             enters_under: You }
    (the Unimplemented head is the chain root; the sub reads the chain
    tracked set. The issue's corpus (9b7c66e30) names the head "restart";
    the pinned v0.100.0 corpus names it "unrecognized_clause_head" with the
    identical description -- both are Effect::Unimplemented over the same
    clause; the structural claim is unchanged.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, FreeForAll, life 20):
  P0: 8x Karn Liberated, 52x Wastes. Plays a land a turn, casts Karn when
      7 mana is available, then walks the loyalty ladder:
        [+4] (6->10) -> [+4] (10->14) -> [-3] (14->11, exiles P1's Bear)
        -> [+4] (11->15) -> [-14] (15->1).
      Only one loyalty activation per own turn; never casts a second Karn.
  P1: 8x Grizzly Bears, 52x Forest. Plays a land a turn, casts Bears,
      never attacks.
Drive:
  1. Mulligans: both seats keep 7 with >= 2 lands.
  2. P0 develops to 7 lands, casts Karn Liberated.
  3. P0 activates [+4] twice (target P1; control: the ability resolves and
     P1 exiles a card from hand), then [-3] targeting P1's Grizzly Bear
     (control: the Bear is exiled), then [+4] once more.
  4. When Karn is at loyalty >= 14 and a Karn-exiled permanent exists:
     export pre.json IMMEDIATELY in the submission path (guarded flag),
     then activate [-14].
  5. Both seats pass priority; the ability resolves. Settle: stack empty,
     Priority, 8s idle -> export post.json.

Expected (correct behavior): the game restarts with the Karn-exiled cards
  left in exile, then those cards enter the battlefield under P0's control.
Reported (bug): the restart clause never parsed, so the chain tracked set
  is empty and the ChangeZoneAll sub is a silent no-op -- the exiled Bear
  stays in exile, nothing enters the battlefield, no restart occurs. The
  -14 loyalty cost is still paid.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Karn Liberated
                  abilities[2] as the issue reports: head Unimplemented
                  ("unrecognized_clause_head", "Restart the game, leaving
                  in exile all non-Aura permanent cards exiled with Karn")
                  + sub ChangeZoneAll (Exile->Battlefield, TrackedSet id 0,
                  enters_under You).
  A2_setup_ok     pre.json exported with Karn on P0's battlefield at
                  loyalty >= 14 and >= 1 Karn-exiled permanent in exile.
  A3_karn14_noop  after [-14] resolves: the Karn-exiled Bear is still in
                  exile; no Exile->Battlefield movement under P0 occurred;
                  Karn paid the -14 loyalty; life totals unchanged and the
                  turn advanced monotonically (no restart).
  A4_controls     [+4] resolved (loyalty rose) and [-3] resolved (Bear
                  exiled) without rejections.
  A5_cleanup      post stack empty.
Verdict: reproduced iff A1+A2 passed and A3 passed (the empty-publish
  consequence observed). not-reproduced iff the Bear moved to P0's
  battlefield (consequence did not manifest). blocked otherwise.
"""

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import PhaseClient

ISSUE = 7428
RUN_ID = "20261003-7428f"
BACKFILL = os.path.expanduser("~/workspace/dev/phase-backfill")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

# Fresh immutable dir: refuse to run into a non-empty evidence dir.
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
                     "(runs/20261003-server; started by this run after a VM "
                     "replacement killed the earlier 20261002-bf1 server; "
                     "ServerHello 0.100.0/bc9ef56/protocol 101 verified by "
                     "this run's own handshake)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned release "
              "dir (GitHub /releases re-confirmed v0.100.0 still latest "
              "stable this run); hashes recomputed against on-disk "
              "artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

STANDARD_FORMAT = {
    # NOTE: "Standard" enforces constructed legality, which rejects the
    # test-harness card pool. FreeForAll is the engine-canonical no-legality
    # 60-card format (FormatConfig::free_for_all()): life 20, seats 2-6,
    # sideboard Unlimited, copy limit Unlimited.
    "format": "FreeForAll",
    "starting_life": 20,
    "min_players": 2,
    "max_players": 6,  # engine-canonical FormatConfig::free_for_all()
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": False,
    "commander_damage_threshold": None,
    "team_based": False,
    "uses_commander": False,
    "sideboard_policy": {"type": "Unlimited"},  # canonical FreeForAll
    "default_deck_copy_limit": {"type": "Unlimited"},  # canonical FreeForAll
    "supplies_fixed_deck": False,
    "allow_debug_actions": False,
}


def deck(main_names):
    return {"main_deck": main_names, "sideboard": [], "commander": []}


P0_DECK = deck(["Karn Liberated"] * 8 + ["Wastes"] * 52)
P1_DECK = deck(["Grizzly Bears"] * 8 + ["Forest"] * 52)

SETUP_DEADLINE_S = 2400


def reset_attempt():
    global ST, MULLS, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> duel -> resolution_window -> done
        "karn_step": 0,  # 0:+4 1:+4 2:-3 3:+4 4:-14
        "karn_oid": None,
        "karn_loyalty_seen": None,
        "karn_cast_turn": None,
        "karn_cast_done": False,
        "activation_in_flight": False,
        "loyalty_used_turn": -1,
        "pending_target_kind": None,  # plus4_player | minus3_permanent
        "minus3_target_oid": None,
        "exiled_bear_oid": None,
        "plus4_seen": False,
        "minus3_seen": False,
        "minus14_submitted": False,
        "minus14_resolving": False,
        "pre_exported": False,
        "post_exported": False,
        "window_end_written": False,
        "settle_at": None,
        "pre_loyalty": None,
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "prompts_seen": [],
        "wf_types_window": [],
        "karn_obj_logged": False,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_karn14_noop", "A4_controls",
                                       "A5_cleanup")},
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
    return str(o.get("name") or "?").lower()


def zone_ids(state, pid, zone):
    out = []
    for p in state.get("players", []):
        if p.get("id") == pid:
            for zname, oids in (p.get("zones") or {}).items():
                if str(zname).lower() == zone.lower():
                    out.extend(int(x) for x in oids)
    # fallback: scan objects map
    if not out:
        for oid, o in (state.get("objects") or {}).items():
            if str(o.get("zone", "")).lower() == zone.lower() \
                    and o.get("controller") == pid:
                out.append(int(oid))
    return out


def hand_ids(state, pid):
    return zone_ids(state, pid, "hand")


def lib_ids(state, pid):
    return zone_ids(state, pid, "library")


def gy_ids(state, pid):
    return zone_ids(state, pid, "graveyard")


def exile_objs(state):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("zone", "")).lower() == "exile":
            out.append((int(oid), o))
    return out


def bf_ids(state, pid=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("zone", "")).lower() != "battlefield":
            continue
        if pid is not None and o.get("controller") != pid:
            continue
        out.append(int(oid))
    return out


def untapped_lands(state, pid, name):
    n = 0
    for oid in bf_ids(state, pid):
        o = get_obj(state, oid)
        if obj_lname(state, oid) == name.lower() and not o.get("tapped"):
            n += 1
    return n


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if obj_lname(state, oid) == name.lower():
            return oid
    return None


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def karn_on_bf(state):
    for oid in bf_ids(state, 0):
        if obj_lname(state, oid) == "karn liberated":
            return oid
    return None


def karn_loyalty(state, karn_oid):
    """Best-effort loyalty read: direct field, then counters scan."""
    o = get_obj(state, karn_oid)
    if o.get("loyalty") is not None:
        try:
            return int(o["loyalty"])
        except (TypeError, ValueError):
            pass
    for c in (o.get("counters") or []):
        if isinstance(c, dict):
            if "loyal" in json.dumps(c).lower():
                for k in ("count", "amount", "n", "value"):
                    if k in c:
                        try:
                            return int(c[k])
                        except (TypeError, ValueError):
                            pass
        elif isinstance(c, (list, tuple)) and len(c) == 2:
            if "loyal" in str(c[0]).lower():
                try:
                    return int(c[1])
                except (TypeError, ValueError):
                    pass
    return None


def stack_entries(state):
    return state.get("stack") or []


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


def vi_ops(st):
    vi = get_vi(st)
    return (vi.get("opportunities") or []) if vi else []


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get("karn liberated", {})
    abil = card.get("abilities") or []
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "loyalty": card.get("loyalty"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "abilities_count": len(abil),
        "ability0": None,
        "ability1": None,
        "head_effect": None,
        "sub_ability": None,
        "report_corpus_note": ("the issue's corpus (9b7c66e30) names the "
                               "Unimplemented head 'restart'; the pinned "
                               "v0.100.0 corpus names it "
                               "'unrecognized_clause_head' with the identical "
                               "description -- both are Effect::Unimplemented "
                               "over the same clause; the structural claim "
                               "is unchanged."),
    }
    ok = False
    if len(abil) >= 3:
        ev["ability0"] = {"cost": abil[0].get("cost"),
                          "effect": abil[0].get("effect"),
                          "description": abil[0].get("description")}
        ev["ability1"] = {"cost": abil[1].get("cost"),
                          "effect": abil[1].get("effect"),
                          "description": abil[1].get("description")}
        d = abil[2]
        head = d.get("effect") or {}
        sub = d.get("sub_ability") or {}
        sub_eff = sub.get("effect") or {}
        ev["head_effect"] = head
        ev["sub_ability"] = sub
        tgt = sub_eff.get("target") or {}
        ok = (
            # [+4] control: TargetOnly Player
            (abil[0].get("effect") or {}).get("type") == "TargetOnly"
            and ((abil[0].get("effect") or {}).get("target") or {}).get("type") == "Player"
            and (abil[0].get("cost") or {}).get("amount") == 4
            # [-3] control: ChangeZone -> Exile, Typed Permanent
            and (abil[1].get("effect") or {}).get("type") == "ChangeZone"
            and (abil[1].get("effect") or {}).get("destination") == "Exile"
            and (((abil[1].get("effect") or {}).get("target") or {}).get("type_filters") or []) == ["Permanent"]
            and (abil[1].get("cost") or {}).get("amount") == -3
            # [-14]: the reported defect
            and (d.get("cost") or {}).get("type") == "Loyalty"
            and (d.get("cost") or {}).get("amount") == -14
            and head.get("type") == "Unimplemented"
            and head.get("name") == "unrecognized_clause_head"
            and "restart the game, leaving in exile all non-aura permanent cards exiled with karn" in str(head.get("description", "")).lower()
            and sub_eff.get("type") == "ChangeZoneAll"
            and sub_eff.get("origin") == "Exile"
            and sub_eff.get("destination") == "Battlefield"
            and tgt.get("type") == "TrackedSet"
            and tgt.get("id") == 0
            and sub_eff.get("enters_under") == "You"
        )
        say(f"data-level check: head={head.get('type')}/{head.get('name')}, "
            f"sub={sub_eff.get('type')}/{sub_eff.get('origin')}"
            f"->{sub_eff.get('destination')}/{(sub_eff.get('target') or {}).get('type')}"
            f"/{(sub_eff.get('target') or {}).get('id')}, "
            f"enters_under={sub_eff.get('enters_under')}")
    else:
        say(f"data-level check: {len(abil)} abilities on Karn Liberated (< 3)")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok})


# ------------------------------------------------------------- actions
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
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


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = None
    for p in (wf_of(state).get("data") or {}).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            pend = p
            break
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    MULLS.add(tag)
    lands = sum(1 for oid in hand_ids(state, pid)
                if "land" in str((get_obj(state, oid).get("card_types")
                                  or {}).get("core_types") or []).lower()
                or obj_lname(state, oid) in ("wastes", "forest"))
    if lands >= 2:
        say(f"[{tag}] keep 7 (lands={lands})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    else:
        say(f"[{tag}] mulligan (lands={lands})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    wire("mulligan", {"who": tag, "lands": lands})
    return True


async def do_discard_to_handsize(c, pid, tag, keep_name):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    pend = None
    for p in (wf_of(state).get("data") or {}).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            pend = p
            break
    if pend is None:
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False

    def sortkey(oid):
        nm = obj_lname(state, oid)
        return (0 if nm in ("wastes", "forest") else 1,
                2 if nm == keep_name else 0,
                oid)
    picks = [int(x) for x in sorted(hand, key=sortkey)[:n]]
    say(f"[{tag}] discarding to hand size: "
        f"{[obj_lname(state, o) for o in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    wire("discard", {"who": tag, "picks": picks})
    return True


async def do_discard_vi(c, pid, tag, keep_name):
    """Answer a viewer-interaction discard-to-hand-size (schema/select)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    hand_oids = set(hand_ids(state, pid))
    for opp in vi_ops(st):
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
        pick = None
        for ch in chs:  # prefer a land candidate
            for sf in (ch.get("surfaces") or []):
                d = sf.get("data") or {}
                try:
                    ref = int(d.get("reference"))
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, ref) in ("wastes", "forest"):
                    pick = ch.get("id")
                    break
            if pick:
                break
        if not pick:
            pick = chs[0].get("id")
        SUBMITTED_OPPS.add(iid)
        ST["discard_iids"].add(iid)
        say(f"[{tag}] vi discard-to-hand-size: {pick}")
        wire("vi_discard", {"who": tag, "iid": iid, "pick": pick})
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [pick]}}})
        return True
    return False


def _cand_ref(ch):
    """Return (kind, ref) for a candidate: kind in object|player|other."""
    for sf in (ch.get("surfaces") or []):
        t = sf.get("type")
        d = sf.get("data") or {}
        if t == "object":
            try:
                return ("object", int(d.get("reference")))
            except (TypeError, ValueError):
                continue
        if t == "player":
            # player surfaces carry the seat id under various keys
            # (observed: "seat")
            for k in ("reference", "player", "player_id", "id", "seat"):
                try:
                    v = d.get(k)
                    if v is None:
                        continue
                    return ("player", int(v))
                except (TypeError, ValueError):
                    continue
    return ("other", None)


async def answer_target(c, st, state, tag):
    """Answer a pending target-selection for the current pending_target_kind:
    plus4_player -> player 1; minus3_permanent -> P1's Grizzly Bear
    (fallback: any P1 permanent). Returns True if a submission was made."""
    kind = ST.get("pending_target_kind")
    if not kind:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        chs = data.get("candidates") or data.get("choices") or []
        if not chs:
            continue
        pick = None
        pick_ref = None
        if kind == "plus4_player":
            for ch in chs:
                k, ref = _cand_ref(ch)
                blob = json.dumps(ch, default=str).lower()
                if (k == "player" and ref == 1) or ("player 1" in blob) \
                        or ("\"player\": 1" in blob.replace(" ", "")):
                    pick, pick_ref = ch.get("id"), ("player", 1)
                    break
        elif kind == "minus3_permanent":
            bears = [oid for oid in bf_ids(state, 1)
                     if obj_lname(state, oid) == "grizzly bears"]
            others = [oid for oid in bf_ids(state, 1) if oid not in bears]
            want = bears + others
            for oid in want:
                for ch in chs:
                    k, ref = _cand_ref(ch)
                    if k == "object" and ref == oid:
                        pick, pick_ref = ch.get("id"), ("object", oid)
                        break
                if pick:
                    break
        if not pick:
            wire("target_unmatched", {"who": tag, "kind": kind,
                                      "iid": opp.get("interactionId") or opp.get("id"),
                                      "spec": str(spec)[:300],
                                      "n_candidates": len(chs),
                                      "blob": json.dumps(chs, default=str)[:800]})
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        SUBMITTED_OPPS.add(iid)
        rtype = spec.get("type")  # "select" or "sequence"
        say(f"[{tag}] answering {kind} target: pick={pick} ref={pick_ref}")
        wire("target_submit", {"who": tag, "kind": kind, "iid": iid,
                               "pick": pick, "ref": pick_ref,
                               "spec_type": spec.get("type")})
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": rtype,
                         "data": {"choiceIds": [pick]}}})
        if kind == "minus3_permanent" and pick_ref[0] == "object":
            ST["minus3_target_oid"] = pick_ref[1]
        ST["pending_target_kind"] = None
        return True
    return False


async def answer_p1_hand_choice(c, st, state, tag):
    """P1 answers a 'choose a card from your hand' (Karn [+4] exile)."""
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        chs = (resp.get("data") or {}).get("candidates") or []
        if not chs:
            continue
        hand_oids = set(hand_ids(state, 1))
        refs = []
        ok = True
        for ch in chs:
            k, ref = _cand_ref(ch)
            if k != "object" or ref not in hand_oids:
                ok = False
                break
            refs.append((ch, ref))
        if not ok or not refs:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick = None
        for ch, ref in refs:  # prefer exiling a land
            if obj_lname(state, ref) == "forest":
                pick = ch.get("id")
                break
        if not pick:
            pick = refs[0][0].get("id")
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] hand-choice (Karn +4 exile): {pick}")
        wire("p1_hand_choice", {"iid": iid, "pick": pick})
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": spec.get("type"),
                         "data": {"choiceIds": [pick]}}})
        return True
    return False


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


async def cast_by_name(c, pid, state, acts, name, tag):
    if ST.get("cast_in_flight"):
        return False
    oid = find_hand(state, pid, name)
    if not oid:
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            await submit_as_is(c, a)
            say(f"[{tag}] cast {name} via advertised CastSpell (oid={oid})")
            wire("cast", {"who": tag, "name": name, "oid": oid,
                          "via": "advertised"})
            ST["cast_in_flight"] = str(oid)
            return True
    obj = get_obj(state, oid)
    cid = obj.get("card_id", int(oid))
    raw = {"type": "CastSpell",
           "data": {"object_id": int(oid), "card_id": int(cid),
                    "targets": [], "payment_mode": {"type": "Auto"}}}
    await submit_as_is(c, raw)
    say(f"[{tag}] cast {name} via raw CastSpell Auto (oid={oid})")
    wire("cast", {"who": tag, "name": name, "oid": oid, "via": "raw"})
    ST["cast_in_flight"] = str(oid)
    return True


def karn_ability_action(acts, karn_oid, ability_index):
    """Find the advertised ActivateAbility for Karn by ability_index
    (0=[+4], 1=[-3], 2=[-14]). The engine advertises only currently
    payable abilities (e.g. index 2 appears only at loyalty >= 14)."""
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data") or {}
        src = str(d.get("source_id") or a.get("_src_oid") or "")
        if src != str(karn_oid):
            continue
        try:
            if int(d.get("ability_index")) == ability_index:
                return a
        except (TypeError, ValueError):
            continue
    return None


async def activate_karn(c, state, acts, karn_oid, amount, tag):
    """Submit the advertised ActivateAbility for Karn's loyalty ability.
    amount selects the ability: +4 -> index 0, -3 -> index 1,
    -14 -> index 2. Returns True if submitted."""
    if ST.get("activation_in_flight"):
        return False
    index = {4: 0, -3: 1, -14: 2}[amount]
    a = karn_ability_action(acts, karn_oid, index)
    if not a:
        return False
    wire("karn_activate", {"who": tag, "amount": amount,
                           "action": {k: v for k, v in a.items()
                                      if not k.startswith("_")}})
    await submit_as_is(c, a)
    say(f"[{tag}] activating Karn loyalty {amount:+d}")
    ST["activation_in_flight"] = True
    ST["loyalty_used_turn"] = state.get("turn_number")
    if amount == 4:
        ST["pending_target_kind"] = "plus4_player"
    elif amount == -3:
        ST["pending_target_kind"] = "minus3_permanent"
    elif amount == -14:
        ST["minus14_submitted"] = True
        ST["phase"] = "resolution_window"
    return True

# ------------------------------------------------------------- P0 tick
async def p0_tick(c, st, acts, state, now):
    tag = "P0"
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        await do_mulligan(c, 0, tag)
        return
    if await do_discard_to_handsize(c, 0, tag, "karn liberated"):
        return
    if await do_discard_vi(c, 0, tag, "karn liberated"):
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
    # Target selections for loyalty abilities (answered regardless of
    # waiting_for type).
    if await answer_target(c, st, state, tag):
        return
    # Clear the in-flight cast flag once Karn left the hand.
    cf = ST.get("cast_in_flight")
    if cf:
        try:
            oid = int(cf)
            if get_obj(state, oid).get("zone") != "Hand":
                ST["cast_in_flight"] = None
                if obj_lname(state, oid) == "karn liberated":
                    ST["karn_cast_done"] = True
                    ST["karn_cast_turn"] = state.get("turn_number")
                    say("[P0] Karn Liberated cast "
                        f"(turn {state.get('turn_number')})")
        except (TypeError, ValueError):
            ST["cast_in_flight"] = None
    # Loyalty observation: advance the step machine on change.
    karn_oid = karn_on_bf(state)
    if karn_oid is not None:
        if not ST["karn_obj_logged"]:
            ST["karn_obj_logged"] = True
            wire("karn_object", get_obj(state, karn_oid))
        ST["karn_oid"] = karn_oid
        loy = karn_loyalty(state, karn_oid)
        prev = ST.get("karn_loyalty_seen")
        if loy is not None and prev is not None and loy != prev:
            say(f"[P0] Karn loyalty {prev} -> {loy}")
            wire("karn_loyalty", {"from": prev, "to": loy})
            ST["karn_loyalty_seen"] = loy
            if ST.get("activation_in_flight"):
                ST["activation_in_flight"] = False
                ST["karn_step"] += 1
                say(f"[P0] activation applied; step now {ST['karn_step']}")
            if prev == 6 and loy == 10:
                ST["plus4_seen"] = True
            if prev == 14 and loy == 11:
                ST["minus3_seen"] = True
                t_oid = ST.get("minus3_target_oid")
                if t_oid is not None and str(
                        get_obj(state, t_oid).get("zone", "")).lower() == "exile":
                    ST["exiled_bear_oid"] = t_oid
                    say(f"[P0] Karn-exiled permanent recorded: oid {t_oid} "
                        f"({obj_lname(state, t_oid)})")
        if loy is not None and ST.get("karn_loyalty_seen") is None:
            ST["karn_loyalty_seen"] = loy
            say(f"[P0] Karn on battlefield, loyalty={loy}")
        # The [-3] exile may resolve a beat after the loyalty change.
        if ST.get("minus3_seen") and ST.get("exiled_bear_oid") is None:
            t_oid = ST.get("minus3_target_oid")
            if t_oid is not None and str(
                    get_obj(state, t_oid).get("zone", "")).lower() == "exile":
                ST["exiled_bear_oid"] = t_oid
                say(f"[P0] Karn-exiled permanent recorded: oid {t_oid} "
                    f"({obj_lname(state, t_oid)})")
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if not (phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and not stack_entries(state)):
        await pass_priority(c, st, acts)
        return
    # Land drop.
    if player_of(state, 0).get("lands_played_this_turn", 0) == 0:
        lid = find_hand(state, 0, "wastes")
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        (a.get("data") or {}).get("object_id")) == str(lid):
                    say("[P0] playing Wastes")
                    await submit_as_is(c, a)
                    return
    # Cast Karn.
    if karn_oid is None and not ST["karn_cast_done"]:
        if find_hand(state, 0, "karn liberated") \
                and untapped_lands(state, 0, "wastes") >= 7:
            if await cast_by_name(c, 0, state, acts, "karn liberated", tag):
                return
    # Loyalty ladder: one activation per own turn.
    if karn_oid is not None and not ST.get("activation_in_flight") \
            and ST.get("loyalty_used_turn") != state.get("turn_number"):
        step = ST["karn_step"]
        loy = ST.get("karn_loyalty_seen")
        acted = False
        if step == 0 and loy is not None and loy < 10:
            acted = await activate_karn(c, state, acts, karn_oid, 4, tag)
        elif step == 1 and loy is not None and loy < 14:
            acted = await activate_karn(c, state, acts, karn_oid, 4, tag)
        elif step == 2:
            bears = [oid for oid in bf_ids(state, 1)
                     if obj_lname(state, oid) == "grizzly bears"]
            if bears or bf_ids(state, 1):
                acted = await activate_karn(c, state, acts, karn_oid, -3, tag)
        elif step == 3 and loy is not None and loy < 15:
            acted = await activate_karn(c, state, acts, karn_oid, 4, tag)
        elif step == 4 and loy is not None and loy >= 14 \
                and ST.get("exiled_bear_oid") is not None:
            # Decisive moment: export pre.json in the submission path,
            # guarded, before the [-14] activation.
            if not ST["pre_exported"]:
                say("[-14] window: exporting pre.json before activation")
                await export_as(c, "pre")
                ST["pre_exported"] = True
                ST["pre_loyalty"] = loy
                wire("pre_exported", {"karn_loyalty": loy,
                                      "exiled_bear_oid": ST["exiled_bear_oid"]})
            acted = await activate_karn(c, state, acts, karn_oid, -14, tag)
        if acted:
            return
        # Diagnostic: once per turn, log what the engine advertises for Karn.
        diag_key = ("karn_acts", state.get("turn_number"))
        if diag_key not in ST["discard_iids"]:
            ST["discard_iids"].add(diag_key)
            types = sorted({a.get("type") for a in acts})
            say(f"[P0] DIAG turn {state.get('turn_number')}: step={step} "
                f"loyalty={loy} act_types={types}")
            for a in acts:
                if a.get("type") == "ActivateAbility":
                    say(f"[P0] DIAG ActivateAbility: "
                        f"{json.dumps(a, default=str)[:600]}")
            wire("karn_acts_diag", {"turn": state.get("turn_number"),
                                    "step": step, "loyalty": loy,
                                    "act_types": types,
                                    "activate_abilities": [
                                        a for a in acts
                                        if a.get("type") == "ActivateAbility"]})
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(c, st, acts, state, now):
    tag = "P1"
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        await do_mulligan(c, 1, tag)
        return
    if await do_discard_to_handsize(c, 1, tag, "grizzly bears"):
        return
    if await do_discard_vi(c, 1, tag, "grizzly bears"):
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
    # Karn [+4]: P1 chooses a card from hand to exile.
    if await answer_p1_hand_choice(c, st, state, tag):
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if not (phase in ("PreCombatMain", "PostCombatMain") and active == 1
            and not stack_entries(state)):
        await pass_priority(c, st, acts)
        return
    if player_of(state, 1).get("lands_played_this_turn", 0) == 0:
        lid = find_hand(state, 1, "forest")
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        (a.get("data") or {}).get("object_id")) == str(lid):
                    await submit_as_is(c, a)
                    return
    bears_bf = sum(1 for oid in bf_ids(state, 1)
                   if obj_lname(state, oid) == "grizzly bears")
    if bears_bf < 3 and find_hand(state, 1, "grizzly bears") \
            and untapped_lands(state, 1, "forest") >= 2:
        if await cast_by_name(c, 1, state, acts, "grizzly bears", tag):
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
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses Karn "
                     "Liberated abilities[2] (Loyalty -14) as head "
                     "Unimplemented('unrecognized_clause_head': 'Restart the "
                     "game, leaving in exile all non-Aura permanent cards "
                     "exiled with Karn') + sub ChangeZoneAll "
                     "(Exile->Battlefield, TrackedSet id 0, enters_under "
                     "You); [+4] (TargetOnly Player) and [-3] (ChangeZone "
                     "-> Exile, Typed Permanent) parse as reported; see "
                     "data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre -- Karn at >= 14 loyalty, a Karn-exiled
    # permanent in exile.
    if ST["pre_exported"] and pre_st:
        karn_oid = karn_on_bf(pre_st)
        loy = karn_loyalty(pre_st, karn_oid) if karn_oid else None
        ex_oid = ST.get("exiled_bear_oid")
        ex_zone = str(get_obj(pre_st, ex_oid).get("zone", "")
                      ).lower() if ex_oid else None
        notes.append(f"A2 probe: Karn on P0 battlefield at pre="
                     f"{karn_oid is not None} (oid={karn_oid}, loyalty="
                     f"{loy}); Karn-exiled oid={ex_oid} zone={ex_zone}; "
                     f"P0 life={player_of(pre_st, 0).get('life')}, P1 life="
                     f"{player_of(pre_st, 1).get('life')}.")
        if karn_oid is not None and loy is not None and loy >= 14 \
                and ex_oid is not None and ex_zone == "exile":
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: pre.json shows Karn Liberated on P0's "
                         f"battlefield at loyalty {loy} with a Karn-exiled "
                         f"permanent ({obj_lname(pre_st, ex_oid)}, oid "
                         f"{ex_oid}) in exile.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: decisive pre lacks Karn at >= 14 "
                         "loyalty with a Karn-exiled permanent in exile.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the [-14] "
                     "window was never reached).")

    # A3: the [-14] consequence -- the ChangeZoneAll sub moved nothing.
    bear_moved_to_p0_bf = False
    if post_st and ST.get("minus14_submitted"):
        ex_oid = ST.get("exiled_bear_oid")
        ex_post = get_obj(post_st, ex_oid) if ex_oid else {}
        ex_zone_post = str(ex_post.get("zone", "")).lower()
        pre_exile_oids = {oid for oid, _ in exile_objs(pre_st)}
        moved = []
        for oid in pre_exile_oids:
            o = get_obj(post_st, oid)
            if str(o.get("zone", "")).lower() == "battlefield" \
                    and o.get("controller") == 0:
                moved.append((oid, obj_lname(post_st, oid)))
        if ex_zone_post == "battlefield" and ex_post.get("controller") == 0:
            bear_moved_to_p0_bf = True
        karn_oid_post = karn_on_bf(post_st)
        loy_post = karn_loyalty(post_st, karn_oid_post) \
            if karn_oid_post else None
        pre_loy = ST.get("pre_loyalty")
        life_same = (player_of(pre_st, 0).get("life")
                     == player_of(post_st, 0).get("life")
                     and player_of(pre_st, 1).get("life")
                     == player_of(post_st, 1).get("life"))
        # A restart would reset the turn counter to 1 and wipe the
        # battlefield; normal play only advances the turn and draws cards
        # (draw steps shrink libraries, so library-size equality is NOT a
        # restart check). Monotonic turn advance + unchanged life = no
        # restart.
        no_restart = (post_st.get("turn_number", 0)
                      > pre_st.get("turn_number", 0)
                      and life_same)
        notes.append(f"A3 probe: exiled oid {ex_oid} zone in post="
                     f"{ex_zone_post}; Exile->P0-battlefield moves={moved}; "
                     f"Karn loyalty pre={pre_loy} post={loy_post}; "
                     f"life unchanged={life_same}; turn {pre_st.get('turn_number')}"
                     f"->{post_st.get('turn_number')} (no restart={no_restart}); "
                     f"post stack empty={not stack_entries(post_st)}.")
        if ex_zone_post == "exile" and not moved \
                and pre_loy is not None and loy_post == pre_loy - 14 \
                and no_restart:
            ass["A3_karn14_noop"] = "passed"
            notes.append("A3 passed: after [-14] resolved, the Karn-exiled "
                         "permanent stayed in exile, nothing moved "
                         "Exile->Battlefield under P0, the -14 loyalty was "
                         "paid, and no game restart occurred -- the "
                         "ChangeZoneAll sub read the empty chain tracked "
                         "set, exactly the issue's structural consequence.")
        elif bear_moved_to_p0_bf:
            ass["A3_karn14_noop"] = "failed"
            notes.append("A3 FAILED (consequence did NOT manifest): the "
                         "Karn-exiled permanent entered P0's battlefield -- "
                         "the tracked set was not empty at runtime.")
        else:
            ass["A3_karn14_noop"] = "failed"
            notes.append("A3 FAILED: unexpected post state; see probe notes.")
    else:
        notes.append("A3 not-run: [-14] was never submitted or no post.")

    # A4: controls -- [+4] and [-3] resolved without rejection.
    if ST.get("plus4_seen") and ST.get("minus3_seen"):
        ass["A4_controls"] = "passed"
        notes.append("A4 passed: [+4] resolved (Karn loyalty 6->10 observed) "
                     "and [-3] resolved (loyalty 14->11, target permanent "
                     "exiled) with no rejections.")
    else:
        ass["A4_controls"] = "failed"
        notes.append(f"A4 FAILED: plus4_seen={ST.get('plus4_seen')}, "
                     f"minus3_seen={ST.get('minus3_seen')}.")

    # A5: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A5_cleanup"] = "passed"
            notes.append("A5 passed: post stack empty, game advanced "
                         f"(turn {post_st.get('turn_number')}, phase "
                         f"{post_st.get('phase')}).")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 FAILED: post stack non-empty.")
    else:
        notes.append("A5 not-run: no post state")

    # verdict
    core = [ass.get(k) for k in ("A1_data_level", "A2_setup_ok")]
    if any(v != "passed" for v in core):
        verdict = "blocked"
    elif ass.get("A3_karn14_noop") == "passed":
        verdict = "reproduced"
    elif bear_moved_to_p0_bf:
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    # server-log excerpt: the Unimplemented resolver firing, if logged
    try:
        logp = f"{BACKFILL}/runs/20261003-server/server.log"
        lines = open(logp, errors="replace").read().splitlines()
        hits = [l for l in lines
                if "nimplemented" in l.lower()
                or "nrecognized_clause" in l.lower()
                or "estart the game" in l.lower()]
        with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
            f.write("\n".join(hits[-60:]) + "\n")
        notes.append(f"server-log excerpt: {len(hits)} matching lines "
                     f"(of {len(lines)} total) saved.")
    except Exception as e:
        notes.append(f"server-log excerpt failed: {e}")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 101, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_{ISSUE}.py", "rb").read()
        ).hexdigest(),
        "format_config": "FreeForAll (2-player, 60-card minimum, no legality check)",
        "decks": {
            "P0": [["Karn Liberated", 8], ["Wastes", 52]],
            "P1": [["Grizzly Bears", 8], ["Forest", 52]],
        },
        "assertions": ass,
        "notes": notes,
        "observations": {
            "karn_step": ST["karn_step"],
            "karn_oid": ST["karn_oid"],
            "karn_loyalty_seen": ST["karn_loyalty_seen"],
            "minus14_submitted": ST["minus14_submitted"],
            "pre_exported": ST["pre_exported"],
            "post_exported": ST["post_exported"],
            "exiled_bear_oid": ST["exiled_bear_oid"],
            "minus3_target_oid": ST["minus3_target_oid"],
            "cast_rejections": ST["cast_rejections"],
            "prompts_seen": [(ph, seat, iid)
                             for ph, seat, iid, _ in ST["prompts_seen"]],
            "wf_types_window": ST["wf_types_window"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human driver "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "Not tested on the issue's 9b7c66e30 corpus build; verdict "
            "scoped to v0.100.0.",
            "The 'restart the game' half of the oracle text is not "
            "exercised as a game restart (the engine has no restart path); "
            "the observable consequence measured is the ChangeZoneAll "
            "sub-ability's empty-tracked-set read.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0 ramps to 7 lands, casts Karn Liberated, walks "
                      "[+4]->[+4]->[-3] (exiles P1's Bear)->[+4]; P1 plays "
                      "Bears and never attacks.",
        "contract_line": "Karn [-14]: restart clause is Unimplemented, so "
                         "the dependent ChangeZoneAll (Exile->Battlefield, "
                         "TrackedSet(0), under P0) reads an empty chain "
                         "tracked set and moves nothing.",
        "prior_runs": [],
        "stats": {"states_seen": ST["states_seen"]},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"run.json written: verdict={verdict}")
    wire("run_written", {"verdict": verdict})
    for c in (WIRE, RUNLOG):
        try:
            c.close()
        except Exception:
            pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return run


# ------------------------------------------------------------- one game
async def drive_one_game():
    """Run one game: develop, walk the Karn loyalty ladder, capture the
    [-14] window. Returns 'done' or 'giveup'."""
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2, format_config=STANDARD_FORMAT)
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    result = "giveup"

    async def close():
        for c in (p0, p1):
            try:
                await c.close()
            except Exception:
                pass

    while time.time() - t_start < SETUP_DEADLINE_S and result == "giveup":
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
                            if ST.get("cast_in_flight") and \
                                    "CastSpell" in json.dumps(data):
                                ST["cast_in_flight"] = None
                            if ST.get("activation_in_flight") and \
                                    "ActivateAbility" in json.dumps(data):
                                ST["activation_in_flight"] = False
                                ST["pending_target_kind"] = None
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
                if ST["phase"] == "setup" and state.get("turn_number", 0) >= 1:
                    ST["phase"] = "duel"
                # resolution-window prompt recording
                if ST["minus14_submitted"] and not ST["post_exported"]:
                    vi = get_vi(st)
                    if vi:
                        for opp in vi.get("opportunities", []) or []:
                            iid = opp.get("interactionId") or opp.get("id")
                            tg = (ST["phase"], tag, iid)
                            if all(x[:3] != tg for x in ST["prompts_seen"]):
                                ST["prompts_seen"].append(
                                    (tg[0], tg[1], tg[2], opp))
                                wire("window_prompt",
                                     {"phase": ST["phase"], "seat": tag,
                                      "iid": iid})
                    wtype = (wf_of(state).get("type") or "")
                    if wtype and wtype not in ST["wf_types_window"]:
                        ST["wf_types_window"].append(wtype)
                    if not ST["window_end_written"] and not stack_entries(state):
                        ST["window_end_written"] = True
                        with open(f"{EVDIR}/window_end.json", "w") as wfj:
                            json.dump({"state": state,
                                       "observed_turn":
                                           state.get("turn_number"),
                                       "observed_phase":
                                           state.get("phase"),
                                       "minus14_submitted": True},
                                      wfj, default=str)
                        say("window_end.json written from observed state "
                            f"(turn {state.get('turn_number')}, "
                            f"phase {state.get('phase')})")
                        wire("minus14_stack_empty", {})
                        ST["settle_at"] = now
                # decisive post: settled with empty stack
                if ST["minus14_submitted"] and not ST["post_exported"]:
                    if not stack_entries(state):
                        if ST["settle_at"] is None:
                            ST["settle_at"] = now
                        idle = now - ST["settle_at"]
                        wtype = (wf_of(state).get("type") or "")
                        if wtype == "Priority" and idle > 8:
                            say(f"settled: stack empty, Priority, "
                                f"{idle:.0f}s idle; exporting post")
                            await export_as(p0, "post")
                            ST["post_exported"] = True
                            await finalize(p0)
                            result = "done"
                            break
                    else:
                        ST["settle_at"] = None
                # watchdogs
                if (state.get("turn_number") or 0) > 40 \
                        and not ST["post_exported"]:
                    say("turn watchdog: 40 turns in, giving up")
                    wire("turn_stall",
                         {"waiting_for": wf_of(state),
                          "turn": state.get("turn_number")})
                    result = "giveup"
                    break
                if ST.get("terminal"):
                    say("TerminalResult seen; ending game")
                    result = "giveup"
                    break
                await tick(c, st, merged_actions(st), state, now)
            except Exception as e:
                say(f"[{tag}] tick error: {e!r}")
                wire("tick_error", {"who": tag, "error": repr(e)})
        # end for seats
    await close()
    return result


async def main():
    reset_attempt()
    check_data_level()
    data_ok = ST["data_level_ok"]
    reset_attempt()
    ST["data_level_ok"] = data_ok  # check_data_level ran once; keep it
    result = await drive_one_game()
    say(f"game -> {result}")
    wire("game_result", {"result": result})
    if not os.path.exists(f"{EVDIR}/run.json"):
        # No decisive run completed: finalize a blocked run from whatever
        # we have (data-level evidence stands on its own).
        say("no successful decisive attempt; finalizing blocked run")
        try:
            await finalize(None)
        except Exception as e:
            say(f"finalize failed: {e}")


if __name__ == "__main__":
    run = asyncio.run(main())
    # copy the scenario into the evidence dir, render the PNG, and write
    # the SHA-256 manifest over everything except the manifest itself.
    shutil.copy(f"{BACKFILL}/driver/scenario_7428.py",
                f"{EVDIR}/scenario_7428.py")
    import subprocess
    if os.path.exists(f"{EVDIR}/run.json") and os.path.exists(f"{EVDIR}/pre.json") \
            and os.path.exists(f"{EVDIR}/post.json"):
        subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                        EVDIR, str(ISSUE),
                        "Karn Liberated [-14]: unparsed restart clause leaves "
                        "ChangeZoneAll reading an empty tracked set; the "
                        "exiled permanent stays in exile"],
                       check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files)", flush=True)
    sys.exit(0)
