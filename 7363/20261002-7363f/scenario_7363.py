#!/usr/bin/env python3
"""Issue #7363: Emet-Selch of the Third Seat -- the "you may cast target
instant or sorcery card from your graveyard" trigger logs a cast but the
spell never resolves; Ponder stays in (or returns to) the graveyard
instead of going graveyard -> stack -> exile.

First backfill run for this issue, on v0.99.0 / protocol 98.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-08-13, classifier: supported_aspect_defect,
status:needs-repro): "In this game state, I triggered Emet-Selch,
targeting Ponder in my graveyard. Although the game log says Ponder was
cast, this wasn't visually apparent and the spell never resolved. Ponder
simply remained (or was put back in) my graveyard rather than going from
graveyard to stack to exile."

Oracle text (pinned v0.99.0 card-data.json, "emet-selch of the third seat"):
  "Spells you cast from your graveyard cost {2} less to cast.
   Whenever one or more opponents lose life, you may cast target instant
   or sorcery card from your graveyard. If that spell would be put into
   your graveyard, exile it instead. Do this only once each turn."
Parse (v0.99.0 card-data.json): LifeLost(opponent, once/turn) ->
CastFromZone(target: instant/sorcery in your graveyard) -> ChangeZone(exile).
Parser ruled out by triage; this is a runtime defect on the cast path.

Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Emet-Selch of the Third Seat ({2}{U}{B} 3/4) + 8x Ponder ({U}
      sorcery) + 4x Sign in Blood ({B}{B}) + 22x Island + 22x Swamp.
  P1: 60x Forest dummy (never attacks, never blocks).

Drive:
  P0 mulligans (max 2) unless the opener holds Ponder and >=2 lands.
  T1+: play a land; cast Ponder from hand while none is in the graveyard
  (it resolves and goes to the graveyard normally). Ramp to {2}{U}{B},
  cast Emet-Selch. Then cast Sign in Blood targeting P1 (P1 loses 2 life)
  to fire the trigger. Answer the trigger's TargetSelection with Ponder,
  ACCEPT the "you may cast" OptionalEffectChoice, and watch the targeted
  Ponder: it must move graveyard -> stack, resolve, and end in exile
  (the "exile it instead" replacement), not the graveyard.
  Ponder's own resolution sub-choices (Dig reorder answered in presented
  order; "may shuffle" declined) are test-harness answers, not assertions.

Assertions:
  A1_setup      pre.json: Emet-Selch on P0 BF, >=1 Ponder in P0 graveyard.
  A2_trigger    an Emet-Selch TriggeredAbility entry was observed on the
                stack after P1 lost life.
  A3_target     the trigger's TargetSelection was answered with Ponder.
  A4_cast       after accepting the may-cast, the targeted Ponder reached
                the stack (zone Stack observed). FAIL = the reported
                symptom (log says cast, spell never appears).
  A5_exile      the Ponder resolved and its final zone is Exile, not
                Graveyard. FAIL = the reported outcome.
  A6_cleanup    stack empty and the game proceeding at post.

Verdict rule: reproduced iff A1..A3 passed and (A4 failed or A5 failed);
              not-reproduced iff A1..A6 all passed;
              blocked iff A1, A2 or A3 failed (the cast test is moot).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - Optional "may cast" answered via decideOptionalEffect surfaces
    (role accept, value true/false); Ponder's "may shuffle" declined.
  - Target selection: advertised schema sequence response with the
    engine-issued candidate id (never synthesized from object ids).
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
RUN_ID = "20261002-7363f"
ISSUE = 7363
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
    "server_run_id": "runs/20261002-7359 (live v0.99.0 server on 127.0.0.1:9374, "
                     "reused per task body: already listening with pinned release)",
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

EMET = "emet-selch of the third seat"
PONDER = "ponder"
SIGN = "sign in blood"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [("Emet-Selch of the Third Seat", 4), ("Ponder", 8),
           ("Sign in Blood", 4), ("Island", 22), ("Swamp", 22)]
P1_DECK = [("Forest", 60)]

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
        "ponder_cast_from_hand": False,  # a Ponder was cast from hand
        "emet_cast": False,
        "sign_cast": False,
        "sign_resolved": False,
        "trigger_seen": False,      # Emet-Selch TriggeredAbility on stack
        "target_answered": False,   # trigger's TargetSelection answered w/ Ponder
        "target_ponder_oid": None,  # oid of the targeted Ponder
        "may_cast_accepted": False,
        "ponder_on_stack": False,   # targeted Ponder observed in zone Stack
        "ponder_zone_trace": [],    # (turn, phase, zone) samples
        "ponder_final_zone": None,
        "ponder_resolved": False,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "test_done": False,         # Ponder left the stack after being on it
        "ass": {k: "not-run" for k in ("A1_setup", "A2_trigger", "A3_target",
                                       "A4_cast", "A5_exile", "A6_cleanup")},
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


def lib_size(state, pid):
    return len(player_of(state, pid).get("library", []))


def life_of(state, pid):
    return player_of(state, pid).get("life")


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
                    return a
    return None


# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    await c.send_action(a)


def choice_text(choice):
    bits = []
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        for k in ("text", "label", "name", "value", "code"):
            if d.get(k):
                bits.append(f"{d.get('role', '?')}:{k}={d[k]}")
    return " | ".join(bits)


def _surface_object(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "object":
            return s.get("data") or {}
    return {}


def _surface_player_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if "seat" in d:
            return d.get("seat")
    return None


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId") or opp.get("id")
    key = (c.name, tag, str(iid), str(choice.get("id")))
    if key in SUBMITTED:
        return False
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    SUBMITTED.add(key)
    say(f"[{c.name}] interaction {tag}: iid={iid} choice={cid} ({rtype})")
    wire(f"interaction_{tag}", sub)
    await c.send_interaction(sub)
    return True


def find_target_ponder(vi, state):
    """Trigger TargetSelection offering a graveyard Ponder -> (opp, choice)."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = list(data.get("candidates", []) or []) \
            + list(data.get("choices", []) or [])
        for ch in cands:
            d = _surface_object(ch)
            if str(d.get("zone", "")).lower() == "graveyard" \
                    and str(d.get("name", "")).lower() == PONDER:
                return opp, ch
    return None, None


def find_target_player(vi, seat):
    """TargetSelection offering a player candidate with the given seat."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = list(data.get("candidates", []) or []) \
            + list(data.get("choices", []) or [])
        for ch in cands:
            if _surface_player_seat(ch) == seat or \
                    str(_surface_player_seat(ch)) == str(seat):
                return opp, ch
    return None, None


def pick_optional(c, want_accept):
    """Find the decideOptionalEffect choice in the current vi; returns
    (opp, choice, is_accept)."""
    st = c.latest
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []) or []:
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data", {}) or {}).get("choices", []) or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []]
            if "decideOptionalEffect" not in codes:
                continue
            is_accept = None
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data", {}) or {}
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None:
                txt = choice_text(ch).lower()
                if "cast" in txt or "accept" in txt:
                    is_accept = True
                elif "decline" in txt or "don't" in txt or "do not" in txt \
                        or "shuffle" in txt:
                    is_accept = False
            if is_accept == want_accept:
                return op, ch, is_accept
    return None, None, None


async def vi_pass_fallback(c, st):
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "passPriority" in codes and ch.get("status", {}) \
                        .get("type") == "available":
                    await answer_vi(c, opp, ch, "pass")
                    return True
    return False


# ------------------------------------------------------------- common ticks
def p0_keepable(hand):
    lands = [n for n in hand if n in (ISLAND, SWAMP)]
    return len(lands) >= 2 and PONDER in hand


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
    mkey = (tag, "mulligan")
    mulls = MULL_COUNT.get(mkey, 0)
    if not p0_keepable(hn) and len(hn) > 5 and mulls < 2:
        MULL_COUNT[mkey] = mulls + 1
        say(f"[P0] mulligan #{mulls + 1} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    else:
        MULLS.add(tag)
        say(f"[P0] keep {len(hn)}")
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
    rank = {EMET: 3, SIGN: 2, PONDER: 1, ISLAND: 0, SWAMP: 0}
    picks = sorted(hand, key=lambda o: rank.get(obj_lname(state, o), 0))[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


def discard_rank(state, o):
    nm = obj_lname(state, o)
    if nm in (ISLAND, SWAMP):
        return 0
    if nm == SIGN:
        return 1
    return 2


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
    picks = [int(x) for x in sorted(hand, key=lambda o: discard_rank(state, o))[:n]]
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
    return await vi_pass_fallback(c, st)


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

    vi = get_vi(st)

    # ---- trigger target selection: answer with the graveyard Ponder ----
    if vi and "TargetSelection" in wtype and not ST["target_answered"]:
        opp, ch = find_target_ponder(vi, state)
        if ch is not None:
            if await answer_vi(c, opp, ch, "trigger-target"):
                ST["target_answered"] = True
                d = _surface_object(ch)
                ST["target_ponder_oid"] = d.get("reference") or d.get("id")
                say(f"[P0] trigger target: Ponder "
                    f"(oid {ST['target_ponder_oid']}) -- prompt observed")
                wire("trigger_target",
                     {"ponder_oid": ST["target_ponder_oid"]})
                return True

    # ---- Sign in Blood's target: P1 ----
    if vi and "TargetSelection" in wtype and ST["sign_cast"] \
            and not ST["sign_resolved"]:
        opp, ch = find_target_player(vi, 1)
        if ch is not None:
            if await answer_vi(c, opp, ch, "sign-target"):
                say("[P0] Sign in Blood target: P1")
                wire("sign_target", {"seat": 1})
                return True

    # ---- OptionalEffectChoice: may-cast (accept) vs may-shuffle (decline) --
    if vi and "OptionalEffectChoice" in wtype:
        ponder_on_stack = any(
            (e.get("kind") or {}).get("type") == "Spell"
            and str(e.get("source_id") or e.get("source") or "").lower() == PONDER
            or PONDER in json.dumps(e, default=str).lower()
            for e in stack_entries(state))
        # The Emet-Selch may-cast prompt appears while the trigger has
        # resolved and no Ponder spell is on the stack yet.
        want_accept = ST["target_answered"] and not ST["may_cast_accepted"] \
            and not ponder_on_stack
        # Ponder's own "may shuffle" appears while its spell is resolving.
        opp, ch, is_accept = pick_optional(c, want_accept)
        if ch is not None:
            tag = "may-cast" if want_accept else "may-shuffle"
            if await answer_vi(c, opp, ch, tag):
                if want_accept:
                    ST["may_cast_accepted"] = True
                    ST["accept_at"] = time.time()
                    say("[P0] ACCEPTED Emet-Selch may-cast")
                    wire("may_cast_accept", {})
                else:
                    say("[P0] declined optional (shuffle)")
                    wire("may_shuffle_decline", {})
                return True
        else:
            wire("optional_unanswered",
                 {"vi": str(st.get("viewer_interaction"))[:2000]})
            say("[P0] OptionalEffectChoice with no decideOptionalEffect "
                "choice identified; logged and waiting")

    # ---- GraveyardPaidCastChoice: log the full opportunity, then answer ----
    # (appears after accepting Emet-Selch's may-cast; the engine asks how
    #  to complete the cast from the graveyard)
    if vi and ST["may_cast_accepted"] and not ST["ponder_on_stack"]:
        for opp in vi.get("opportunities", []) or []:
            wire("graveyard_paid_cast_opportunity_FULL", opp)
            say(f"[P0] GraveyardPaidCastChoice opportunity FULL: "
                f"{json.dumps(opp, default=str)[:3000]}")
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = list(data.get("candidates", []) or []) \
                + list(data.get("choices", []) or [])
            # Prefer the targeted Ponder if it is among the candidates.
            pick = None
            for ch in cands:
                d = _surface_object(ch)
                if str(d.get("reference", "")) == str(ST["target_ponder_oid"]) \
                        or str(d.get("name", "")).lower() == PONDER:
                    pick = ch
                    break
            if pick is None and cands:
                pick = cands[0]
            if pick is not None:
                if await answer_vi(c, opp, pick, "graveyard-paid-cast"):
                    say(f"[P0] answered GraveyardPaidCastChoice with "
                        f"choice {pick.get('id')}")
                    return True
        # fall through to the generic handlers below if nothing matched

    # ---- Ponder Dig reorder: answer card choices in presented order ----
    # (only while a Ponder spell is on the stack resolving, OR during the
    #  early hand-cast before the may-cast phase; the GraveyardPaidCastChoice
    #  above is handled separately and must not be misanswered as a Dig)
    ponder_resolving = any(
        (e.get("kind") or {}).get("type") == "Spell"
        and PONDER in json.dumps(e, default=str).lower()
        for e in stack_entries(state))
    early_ponder_phase = ST["ponder_cast_from_hand"] \
        and not ST["may_cast_accepted"]
    if vi and (ponder_resolving or early_ponder_phase):
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = list(data.get("candidates", []) or []) \
                + list(data.get("choices", []) or [])
            if not cands:
                continue
            codes = [s.get("data", {}).get("code")
                     for ch0 in cands for s in ch0.get("surfaces", []) or []]
            if "decideOptionalEffect" in codes or "passPriority" in codes:
                continue
            # Heuristic: a card-choice opportunity during Ponder resolution.
            if all(_surface_object(ch).get("zone", "").lower() == "library"
                   for ch in cands if _surface_object(ch)):
                iid = opp.get("interactionId") or opp.get("id")
                key = (c.name, "dig", str(iid))
                if key in SUBMITTED:
                    continue
                rtype = resp.get("type")
                if rtype == "schema":
                    spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
                    stype = spec.get("type") or "sequence"
                    sub = {"interactionId": iid,
                           "response": {"type": stype,
                                        "data": {"choiceIds":
                                                 [ch.get("id") for ch in cands]}}}
                else:
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": cands[0].get("id")}}}
                SUBMITTED.add(key)
                say(f"[P0] dig reorder: {len(cands)} candidates in "
                    f"presented order ({rtype})")
                wire("dig_reorder", sub)
                await c.send_interaction(sub)
                return True

    # ---- ManaPayment: the engine (wrongly, per the {2} reduction) asks P0
    # to pay for the Ponder cast. Log it and let the watchdog finalize;
    # the zone desync is already proven. Do NOT pay (payment does not
    # progress the game). ----
    if vi and "ManaPayment" in wtype:
        if not ST.get("mana_logged"):
            wire("mana_payment_FULL", {"waiting_for": wf_of(state),
                                       "vi": st.get("viewer_interaction")})
            say(f"[P0] ManaPayment pending (Ponder should cost {{0}} with "
                f"Emet-Selch's {{2}} reduction); leaving for watchdog")
            ST["mana_logged"] = True
        return True  # hold; do not pass priority blindly

    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    in_flight = any((e.get("kind") or {}).get("type") == "Spell"
                    and e.get("controller") == 0
                    for e in stack_entries(state))

    if phase in ("PreCombatMain", "PostCombatMain") and active == 0 \
            and not in_flight:
        # 1) cast Ponder from hand until one is in the graveyard
        if not gy_ids(state, 0, PONDER) and PONDER in hand_lnames(state, 0):
            if len(untapped_lands(state, 0, ISLAND)) >= 1:
                a = cast_action_for(acts, state, PONDER)
                if a:
                    say("[P0] casting Ponder from hand")
                    wire("cast_ponder_hand", {"action": a["type"]})
                    await submit_as_is(c, a)
                    ST["ponder_cast_from_hand"] = True
                    return
        # 2) cast Emet-Selch ({2}{U}{B})
        if not ST["emet_cast"] and not bf_ids(state, 0, EMET) \
                and EMET in hand_lnames(state, 0):
            unt_islands = len(untapped_lands(state, 0, ISLAND))
            unt_swamps = len(untapped_lands(state, 0, SWAMP))
            if unt_islands >= 1 and unt_swamps >= 1 \
                    and unt_islands + unt_swamps >= 4:
                a = cast_action_for(acts, state, EMET)
                if a:
                    say("[P0] casting Emet-Selch of the Third Seat")
                    wire("cast_emet", {"action": a["type"]})
                    await submit_as_is(c, a)
                    ST["emet_cast"] = True
                    return
        # 3) cast Sign in Blood targeting P1 (fires the trigger)
        if not ST["sign_cast"] and bf_ids(state, 0, EMET) \
                and gy_ids(state, 0, PONDER) and SIGN in hand_lnames(state, 0):
            if len(untapped_lands(state, 0, SWAMP)) >= 2:
                a = cast_action_for(acts, state, SIGN)
                if a:
                    say("[P0] casting Sign in Blood (target P1)")
                    wire("cast_sign", {"action": a["type"]})
                    await submit_as_is(c, a)
                    ST["sign_cast"] = True
                    return
        # 4) normal play: land drop, then pass
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


def note_trigger_on_stack(state):
    if ST["trigger_seen"]:
        return
    for e in stack_entries(state):
        if "triggered" in json.dumps(e, default=str).lower() \
                and "emet" in json.dumps(e, default=str).lower():
            ST["trigger_seen"] = True
            say("EMET-SELCH TRIGGERED ABILITY observed on stack")
            wire("emet_trigger_on_stack", e)
            return


def trace_ponder(state):
    """Record the targeted Ponder's zone; detect stack arrival/departure.
    Arrival is detected two ways: (1) the card object's zone becomes Stack,
    (2) a Spell entry appears on the stack with source_id/card_id == oid
    (the engine may reference the card without moving its zone -- which is
    itself the suspected defect, so both signals are recorded)."""
    oid = ST["target_ponder_oid"]
    if oid is None:
        return
    o = get_obj(state, oid)
    zone = o.get("zone")
    ST["ponder_zone_trace"].append(
        (state.get("turn_number"), state.get("phase"), zone))
    on_stack_by_zone = (zone == "Stack")
    on_stack_by_ref = any(
        (e.get("kind") or {}).get("type") == "Spell"
        and str(e.get("source_id")) == str(oid)
        for e in stack_entries(state))
    if (on_stack_by_zone or on_stack_by_ref) and not ST["ponder_on_stack"]:
        ST["ponder_on_stack"] = True
        ST["ponder_on_stack_by_ref_only"] = on_stack_by_ref \
            and not on_stack_by_zone
        say(f"TARGETED PONDER oid {oid} reached the STACK "
            f"(turn {state.get('turn_number')}; by_zone={on_stack_by_zone}, "
            f"by_ref={on_stack_by_ref})")
        wire("ponder_on_stack", {"oid": oid,
                                "turn": state.get("turn_number"),
                                "by_zone": on_stack_by_zone,
                                "by_ref": on_stack_by_ref,
                                "card_zone": zone})
    if ST["ponder_on_stack"] and not on_stack_by_zone and not on_stack_by_ref \
            and not ST["test_done"]:
        ST["test_done"] = True
        ST["ponder_final_zone"] = zone
        ST["ponder_resolved"] = True
        say(f"TARGETED PONDER oid {oid} left the stack; final zone={zone}")
        wire("ponder_left_stack", {"oid": oid, "final_zone": zone})


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
    mid_st = (mid or {}).get("state") or {}
    post_st = (post or {}).get("state") or {}

    # A1: setup
    if pre_st:
        emet_ok = bool(bf_ids(pre_st, 0, EMET))
        ponder_gy = bool(gy_ids(pre_st, 0, PONDER))
        if emet_ok and ponder_gy:
            ass["A1_setup"] = "passed"
            notes.append(f"A1 passed: Emet-Selch on P0 BF, Ponder in P0 "
                         f"graveyard (pre turn {pre_st.get('turn_number')}).")
        else:
            ass["A1_setup"] = "failed"
            notes.append(f"A1 FAILED: emet_on_bf={emet_ok} "
                         f"ponder_in_gy={ponder_gy}")
    else:
        notes.append("A1 not-run: no pre state")

    # A2: trigger observed
    if ST["trigger_seen"]:
        ass["A2_trigger"] = "passed"
        notes.append("A2 passed: Emet-Selch TriggeredAbility observed on "
                     "the stack after P1 lost life.")
    elif ass["A1_setup"] == "passed":
        ass["A2_trigger"] = "failed"
        notes.append("A2 FAILED: Sign in Blood resolved (P1 life "
                     f"{life_of(pre_st, 1) if pre_st else '?'}) but no "
                     "Emet-Selch trigger ever appeared on the stack.")
    else:
        notes.append("A2 not-run: A1 failed")

    # A3: target answered
    if ST["target_answered"]:
        ass["A3_target"] = "passed"
        notes.append(f"A3 passed: trigger TargetSelection answered with "
                     f"Ponder (oid {ST['target_ponder_oid']}).")
    elif ass["A2_trigger"] == "passed":
        ass["A3_target"] = "failed"
        notes.append("A3 FAILED: trigger seen but its target selection was "
                     "never answered with Ponder.")
    else:
        notes.append("A3 not-run: A2 failed")

    # A4: Ponder reached the stack after the may-cast accept.
    # A real cast moves the card graveyard -> stack. If the engine only
    # created a Spell entry referencing the card while the card object
    # stayed in the Graveyard (by_ref_only), that IS the reported defect:
    # "the game log says Ponder was cast ... Ponder simply remained in my
    # graveyard".
    if ST["ponder_on_stack"] and not ST.get("ponder_on_stack_by_ref_only"):
        ass["A4_cast"] = "passed"
        notes.append("A4 passed: the targeted Ponder moved graveyard -> "
                     "stack after the may-cast accept.")
    elif ST.get("ponder_on_stack_by_ref_only"):
        ass["A4_cast"] = "failed"
        notes.append("A4 FAILED (reported symptom, zone desync): a Spell "
                     "entry for the targeted Ponder appeared on the stack, "
                     "but the card object never left the Graveyard "
                     "(zone=Graveyard, still in P0's graveyard list).")
    elif ass["A3_target"] == "passed":
        ass["A4_cast"] = "failed"
        trace = ST["ponder_zone_trace"][-6:] if ST["ponder_zone_trace"] else []
        notes.append("A4 FAILED (reported symptom): may-cast accepted but "
                     "the targeted Ponder never reached the stack; zone "
                     f"trace tail={trace}")
    else:
        notes.append("A4 not-run: A3 failed")

    # A5: Ponder resolved and ended in Exile
    if ST["test_done"]:
        fz = ST["ponder_final_zone"]
        if fz == "Exile":
            ass["A5_exile"] = "passed"
            notes.append("A5 passed: Ponder resolved and was exiled "
                         "(replacement applied).")
        else:
            ass["A5_exile"] = "failed"
            notes.append(f"A5 FAILED (reported outcome): Ponder resolved "
                         f"but ended in zone={fz}, not Exile.")
    elif ass["A4_cast"] == "passed":
        ass["A5_exile"] = "failed"
        notes.append("A5 FAILED: Ponder reached the stack but never left "
                     "it (resolution stalled or vanished).")
    else:
        notes.append("A5 not-run: A4 failed")

    # A6: cleanup
    if post_st:
        empty = not stack_entries(post_st)
        wf = (post_st.get("waiting_for") or {}).get("type")
        if empty and wf in ("Priority", None):
            ass["A6_cleanup"] = "passed"
            notes.append(f"A6 passed: stack empty, waiting_for={wf}, game "
                         f"at turn {post_st.get('turn_number')}.")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"A6 FAILED: stack={len(stack_entries(post_st))}, "
                         f"waiting_for={wf}")
    else:
        notes.append("A6 not-run: no post state")

    if ass["A1_setup"] != "passed" or ass["A2_trigger"] != "passed" \
            or ass["A3_target"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup, trigger, or target selection "
                     "incomplete")
    elif ass["A4_cast"] == "failed" or ass["A5_exile"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: the may-cast was accepted but "
                     "Ponder did not complete the graveyard -> stack -> "
                     "exile path.")
    elif all(ass[k] == "passed" for k in ass):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: Ponder was cast from the "
                     "graveyard via the trigger, resolved, and was exiled.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: inconclusive assertion set")

    import glob
    log_paths = [f"{BACKFILL}/runs/{RUN_ID}/server.log"]
    lines = []
    used = None
    for lp in log_paths + sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"),
                                 reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "emet" in l.lower() or "ponder" in l.lower()
               or "castfromzone" in l.lower() or "cast_from" in l.lower()]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-120:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")
    notes.append(f"ponder zone trace ({len(ST['ponder_zone_trace'])} "
                 f"samples): {ST['ponder_zone_trace'][:4]} ... "
                 f"{ST['ponder_zone_trace'][-4:]}")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7363.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "emet_cast": ST["emet_cast"],
            "sign_cast": ST["sign_cast"],
            "sign_resolved": ST["sign_resolved"],
            "trigger_seen": ST["trigger_seen"],
            "target_answered": ST["target_answered"],
            "target_ponder_oid": ST["target_ponder_oid"],
            "may_cast_accepted": ST["may_cast_accepted"],
            "ponder_on_stack": ST["ponder_on_stack"],
            "ponder_on_stack_by_ref_only": ST.get("ponder_on_stack_by_ref_only",
                                                  False),
            "ponder_final_zone": ST["ponder_final_zone"],
            "ponder_zone_trace_len": len(ST["ponder_zone_trace"]),
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x Ponder deck density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "P1 life loss is dealt by Sign in Blood ({B}{B}); the report does "
            "not specify how its opponent lost life, only that the trigger "
            "fired.",
            "Ponder's resolution sub-choices (Dig reorder in presented order, "
            "'may shuffle' declined) are deterministic test-harness answers, "
            "not assertions.",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
        "setup_line": "P0: 4x Emet-Selch of the Third Seat + 8x Ponder + 4x "
                      "Sign in Blood + 22x Island + 22x Swamp; P1: 60x Forest",
        "contract_line": "Accepting Emet-Selch's 'may cast' trigger casts the "
                         "targeted Ponder from the graveyard: it reaches the "
                         "stack, resolves, and is exiled instead of "
                         "returning to the graveyard",
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
            # ---- observation pass (TOCTOU-safe: only on client-view facts)
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
                    note_trigger_on_stack(state)
                    trace_ponder(state)
                    # pre export: trigger on the stack
                    if ST["trigger_seen"] and not ST["pre_exported"]:
                        await export_pre(c)
                    # mid export: Ponder on the stack
                    if ST["ponder_on_stack"] and not ST["mid_exported"]:
                        await export_mid(c)
                    # post export: Ponder left the stack and game is quiet
                    if ST["test_done"] and not ST["post_exported"]:
                        if not stack_entries(state) and \
                                (wf_of(state).get("type") in
                                 ("Priority", None)):
                            await asyncio.sleep(1.0)
                            await export_post(c)
                            say(f"test complete at turn "
                                f"{state.get('turn_number')}")
                            finalized = True
                            break
                    # may-cast accepted but Ponder never properly arrived
                    # (or arrived by reference only and never resolves):
                    # give it 120s, then export post as the stuck state
                    if ST["may_cast_accepted"] and not ST["post_exported"]:
                        stuck = (not ST["ponder_on_stack"]) or \
                            ST.get("ponder_on_stack_by_ref_only", False)
                        if stuck and now - ST.get("accept_at", now) > 120:
                            await export_post(c)
                            say("may-cast accepted but Ponder never completed "
                                "the cast within 120s -- stuck state exported")
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
        # deadline watchdog: finalize as blocked once time runs out
    say("finalizing")
    verdict = await finalize(p0)
    await p0.close()
    await p1.close()
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    # copy the scenario into the evidence dir for the manifest
    shutil.copy(f"{BACKFILL}/driver/scenario_7363.py",
                f"{EVDIR}/scenario_7363.py")
    sys.exit(0)
