#!/usr/bin/env python3
"""Issue #7368: Battle at the Helvault doesn't go around to all players.

Reported (discord 2026-08-13, sync-filed, status:confirmed,
classifier:supported-aspect-defect): "Triggered in this game state if by
activating [[Satsuki, the Living Lore]]. When [[Battle at the Helvault]] I
or II trigger, they prompt the controlling player to exile, but if you skip
(which should be permissible, since it's 'up to one' permanent), it doesn't
continue around to give the option to exile opponent's permanents."

Triage (mike-theDude, 2026-08-15): chapters I and II are supported:true but
parse to a SINGLE 0-to-1 target slot (targets: 0-1); the per-player
iteration ("For each player, exile up to one target non-Saga, nonland
permanent that player controls") is missing. Declining the one slot ends
the chapter instead of moving on to the next player. Same missing
primitive as #7353 (Blatant Thievery).

Oracle text (verified from pinned card-data.json):
  battle at the helvault: {4}{W}{W} Enchantment -- Saga
  (As this Saga enters and after your draw step, add a lore counter.
   Sacrifice after III.)
  I, II -- For each player, exile up to one target non-Saga, nonland
           permanent that player controls until this Saga leaves the
           battlefield.
  III -- Create Avacyn, a legendary 8/8 white Angel creature token with
         flying, vigilance, and indestructible.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 8x Battle at the Helvault + 16x Elite Vanguard + 36x Plains
  P1: 16x Elite Vanguard + 44x Plains
  (dense playsets are a test-harness convenience; the engine accepts >4-of
   for custom games)
Drive: both players play a Plains every turn and cast Elite Vanguards; P0
casts Battle at the Helvault as soon as {4}{W}{W} is payable (6 untapped
Plains). The saga enters, a lore counter is added, the chapter I trigger
goes on the stack. When it resolves:
  slot 1 (candidates scoped to P0's permanents): DECLINE (zero targets --
           legal because the clause is "up to one").
Expected (correct behavior): the trigger then offers slot 2, scoped to
P1's permanents; choosing one exiles it until the saga leaves.
Reported (bug): declining slot 1 ends the chapter; no slot 2 is offered.

Note: the report's game reached the trigger via Satsuki's {T} adding a
lore counter; this run reaches the identical chapter-I trigger via the
saga's own ETB lore counter. The clause under test ("For each player,
exile up to one target ...") is the same.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok        saga on P0 battlefield with a lore counter; chapter I
                     trigger observed on the stack; both players control
                     >=1 non-Saga nonland permanent.
  A2_prompt_offered  chapter I's target-selection opportunity is advertised
                     to the controller; its candidates are P0 permanents.
  A3_decline_accepted the decline (zero targets) is accepted without a
                     rejection; the trigger does not stick on the slot.
  A4_iteration_continues (THE REPORTED BUG) after the decline, a second
                     target slot appears scoped to P1's permanents.
  A5_accept_exiles   choosing one P1 permanent exiles it (zone Exile).
  A6_cleanup         post state sane: stack empty, game advancing.

Verdict rule: reproduced iff A1..A3 passed and A4 failed -- declining ends
              the chapter instead of iterating to the next player, exactly
              the reported failure;
              not-reproduced iff A4 and A5 passed;
              blocked iff A1 failed (setup never assembled).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - Target selection via viewer_interaction opportunities: schema/sequence
    responses submit {"type":"sequence","data":{"choiceIds":[...]}};
    "up to one" declines as an empty choiceIds list.
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
RUN_ID = "20261002-7368c"
ISSUE = 7368
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
    "server_run_id": "shared pinned v0.99.0 server on 127.0.0.1:9374 "
                     "(already listening per task body; ServerHello "
                     "0.99.0/d919616/protocol 98 verified this run; "
                     "games-db lives under runs/20261002-7366b; not "
                     "restarted by this run)",
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

HELVAULT = "battle at the helvault"
VANGUARD = "elite vanguard"
PLAINS = "plains"

P0_DECK = [("Battle at the Helvault", 8), ("Elite Vanguard", 16),
           ("Plains", 36)]
P1_DECK = [("Elite Vanguard", 24), ("Plains", 36)]

SETUP_DEADLINE_S = 1500
ITERATION_WAIT_S = 120

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
        "phase": "setup",  # setup -> trigger -> slots -> done
        "saga_cast": False,
        "saga_oid": None,
        "trigger_seen": False,
        "trigger_seen_at": None,
        "slots": [],            # slot records in observation order
        "slot_ids_seen": set(),
        "decline_submitted": False,
        "decline_at": None,
        "decline_rejections": 0,
        "awaiting_iteration": False,
        "iteration_continued": None,
        "end_ticks": 0,
        "accept_oid": None,     # P1 permanent chosen in slot >= 1
        "accept_submitted": False,
        "accepted_exiled": False,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "player_keys_logged": False,
        "saga_keys_logged": False,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_prompt_offered",
                                       "A3_decline_accepted",
                                       "A4_iteration_continues",
                                       "A5_accept_exiles", "A6_cleanup")},
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


def nonland_nonsaga_permanents(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        ct = o.get("card_types") or {}
        core = [str(t).lower() for t in ct.get("core_types", [])]
        sub = [str(t).lower() for t in ct.get("subtypes", [])]
        if "land" in core or "saga" in sub:
            continue
        out.append(int(oid))
    return out


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


def helvault_trigger_on_stack(state):
    """True if a Battle at the Helvault chapter trigger is on the stack."""
    for e in stack_entries(state):
        ser = json.dumps(e, default=str).lower()
        if "helvault" in ser:
            return True
    return False


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
    hv = CARD_DATA.get("battle at the helvault", {})
    vg = CARD_DATA.get("elite vanguard", {})
    findings = {
        "helvault_name": hv.get("name"),
        "helvault_mana_cost": hv.get("mana_cost"),
        "helvault_types": hv.get("card_type"),
        "helvault_oracle": hv.get("oracle_text"),
        "vanguard_cost": vg.get("mana_cost"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    ok = (
        hv.get("mana_cost") == {"type": "Cost",
                                "shards": ["White", "White"], "generic": 4}
        and "Saga" in ((hv.get("card_type") or {}).get("subtypes") or [])
        and "For each player, exile up to one target" in (hv.get("oracle_text") or "")
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
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    # keep saga/vanguard first, bottom plains
    rank = {HELVAULT: 5, VANGUARD: 4, PLAINS: 0}
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
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    n = len(hand) - 7
    if n <= 0:
        return False
    # discard plains first; never the saga
    rank = {PLAINS: 0, VANGUARD: 1, HELVAULT: 5}
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


# ------------------------------------------------------------- slot handling
def bf_oid_set(state):
    return {int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"}


def extract_candidates(opp, state):
    """Pull candidate list from a target-selection opportunity.

    Returns [{candidate_id, oids, controllers, text}]. oids are battlefield
    object ids referenced by the candidate (intersected with actual BF oids
    so interaction ids / counters don't leak in)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    raw = data.get("choices") or data.get("candidates") or []
    bf = bf_oid_set(state)
    out = []
    for ch in raw:
        cid = ch.get("id")
        ints = set()
        for tok in __import__("re").findall(r"-?\d+", json.dumps(ch, default=str)):
            try:
                v = int(tok)
            except ValueError:
                continue
            if v in bf:
                ints.add(v)
        ctrls = sorted({get_obj(state, o).get("controller") for o in ints})
        out.append({"candidate_id": cid, "oids": sorted(ints),
                    "controllers": ctrls, "text": choice_text(ch)[:160]})
    return out


def is_target_slot_opp(opp, state):
    """Strict: an opportunity is a chapter target slot only if the engine
    is in a target-selection wait (observed: TriggerTargetSelection).
    Priority menus (exactChoices with passPriority/castSpell codes) and
    discard prompts are NEVER slots -- loose matching polluted the
    20261002-7368 run (priority menus 'declined'/'accepted') and the
    'helvault in menu text' clause misfired in 20261002-7368b (a castSpell
    choice for the second saga in hand matched)."""
    wtype = (wf_of(state).get("type") or "")
    return "targetselection" in wtype.lower()


async def decline_slot(c, opp, slot):
    """Decline an up-to-one target slot per the advertised schema."""
    rtype = (opp.get("response", {}) or {}).get("type")
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    iid = opp.get("interactionId") or opp.get("id")
    if rtype == "exactChoices":
        for ch in data.get("choices") or []:
            t = choice_text(ch).lower()
            if any(k in t for k in ("skip", "decline", "none", "no target",
                                    "no targets", "pass")):
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": ch.get("id")}}}
                wire("slot_decline_submit",
                     {"slot": slot["index"], "via": "explicit_choice",
                      "choice": choice_text(ch)[:120], "submission": sub})
                await c.send_interaction(sub)
                slot["decision"] = "declined_explicit_choice"
                slot["submission"] = sub
                return True
        wire("slot_decline_no_choice",
             {"slot": slot["index"],
              "choices": [choice_text(ch)[:80]
                          for ch in data.get("choices") or []]})
        slot["decision"] = "decline_unavailable_no_explicit_choice"
        return False
    spec = data.get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else None
    if stype == "sequence" or rtype in ("schema", "TargetSelection"):
        sub = {"interactionId": iid,
               "response": {"type": "sequence", "data": {"choiceIds": []}}}
        wire("slot_decline_submit",
             {"slot": slot["index"], "via": "empty_sequence",
              "submission": sub})
        await c.send_interaction(sub)
        slot["decision"] = "declined_empty_sequence"
        slot["submission"] = sub
        return True
    wire("slot_decline_unknown_schema",
         {"slot": slot["index"], "rtype": rtype, "spec": spec})
    slot["decision"] = "decline_unavailable_unknown_schema"
    return False


async def accept_slot(c, opp, slot, state):
    """Accept a slot by choosing one candidate (prefer a P1 permanent)."""
    cands = slot["candidates"]
    pick = next((x for x in cands if 1 in x["controllers"]), None)
    pick = pick or (cands[0] if cands else None)
    if pick is None:
        wire("slot_accept_no_candidates", {"slot": slot["index"]})
        slot["decision"] = "accept_unavailable_no_candidates"
        return False
    rtype = (opp.get("response", {}) or {}).get("type")
    iid = opp.get("interactionId") or opp.get("id")
    if rtype == "exactChoices":
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick["candidate_id"]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": [pick["candidate_id"]]}}}
    wire("slot_accept_submit",
         {"slot": slot["index"], "pick": pick, "submission": sub})
    await c.send_interaction(sub)
    slot["decision"] = "accepted"
    slot["submission"] = sub
    ST["accept_oid"] = (pick["oids"] or [None])[0]
    ST["accept_submitted"] = True
    say(f"[slot {slot['index']}] accepted: chose candidate "
        f"{pick['candidate_id']} (oids {pick['oids']}, "
        f"controllers {pick['controllers']})")
    return True


async def maybe_answer_slot(c, tag, st, acts, state):
    """Answer a fresh chapter-target slot if one is advertised to us.

    Policy: slot index 0 -> decline (zero targets, legal "up to one");
    slot index >= 1 -> accept one candidate (prefer P1's). Returns True if
    a submission was made (or the slot was recorded as unanswerable)."""
    if ST["phase"] not in ("trigger", "slots"):
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in ST["slot_ids_seen"]:
            continue
        if not is_target_slot_opp(opp, state):
            # Not a chapter slot (e.g. a priority menu or discard prompt
            # observed while the trigger is on the stack): record for
            # inspection, never act on it.
            key = ("oppwired", iid)
            if key not in SUBMITTED:
                SUBMITTED.add(key)
                wire("trigger_phase_opp",
                     {"advertised_to": tag,
                      "waiting_for": wf_of(state).get("type"),
                      "rtype": (opp.get("response") or {}).get("type"),
                      "n_choices": len(((opp.get("response") or {}).get("data")
                                        or {}).get("choices") or []),
                      "choice_texts": [choice_text(ch)[:60]
                                       for ch in (((opp.get("response") or {})
                                                   .get("data") or {})
                                                  .get("choices") or [])][:8]})
            continue
        # fresh slot: record BEFORE submitting (cross-client guard)
        ST["slot_ids_seen"].add(iid)
        cands = extract_candidates(opp, state)
        slot = {
            "index": len(ST["slots"]),
            "interaction_id": iid,
            "advertised_to": tag,
            "waiting_for": wf_of(state),
            "candidates": cands,
            "candidate_summary": {
                "n": len(cands),
                "controllers": sorted({ct for x in cands
                                       for ct in x["controllers"]}),
            },
            "decision": None,
            "submission": None,
        }
        ST["slots"].append(slot)
        wire("slot_observed", {"slot_index": slot["index"],
                               "advertised_to": tag,
                               "waiting_for": slot["waiting_for"],
                               "candidates": cands,
                               "opportunity": opp})
        say(f"[slot {slot['index']}] advertised to {tag}: "
            f"{len(cands)} candidates, controllers="
            f"{slot['candidate_summary']['controllers']}; "
            f"waiting_for={slot['waiting_for'].get('type')}")
        if ST["phase"] == "trigger":
            ST["phase"] = "slots"
        if slot["index"] == 0:
            ok = await decline_slot(c, opp, slot)
            if ok:
                ST["decline_submitted"] = True
                ST["decline_at"] = time.time()
                ST["awaiting_iteration"] = True
                ST["end_ticks"] = 0
                say(f"[slot 0] DECLINED (decision={slot['decision']}); "
                    f"awaiting iteration to the next player")
            else:
                say(f"[slot 0] could not decline: {slot['decision']}")
            return True
        else:
            # A later slot: is it the iteration (P1-scoped) or a new
            # chapter trigger (P0-scoped again)?
            ctrls = slot["candidate_summary"]["controllers"]
            if ctrls == [0] and helvault_trigger_on_stack(state):
                say(f"[slot {slot['index']}] P0-scoped slot while the "
                    f"chapter trigger is back on the stack -- looks like a "
                    f"NEW chapter trigger (chapter II), not iteration; "
                    f"recording and declining to keep the game moving")
                wire("slot_new_chapter_trigger", {"slot": slot})
                await decline_slot(c, opp, slot)
                ST["notes"].append(
                    f"slot {slot['index']}: P0-scoped prompt arrived with a "
                    f"fresh chapter trigger on the stack (chapter II); not "
                    f"counted as iteration")
                return True
            if ST["iteration_continued"] is None:
                ST["iteration_continued"] = True
                say(f"[slot {slot['index']}] ITERATION CONTINUED -- a "
                    f"second slot is offered after the decline")
            await accept_slot(c, opp, slot, state)
            return True
    return False


# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    if await maybe_answer_slot(c, "P0", st, acts, state):
        return
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 0, "P0", HELVAULT):
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
    if ST["phase"] != "setup":
        # trigger/slots in flight: only answer slots and pass priority
        await pass_priority(c, st, acts)
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        ul = untapped_lands(state, 0)
        # 1) cast the saga as soon as {4}{W}{W} is payable
        if not ST["saga_cast"] and HELVAULT in hand_lnames(state, 0) \
                and len(ul) >= 6:
            a, oid = cast_action_for(acts, state, HELVAULT)
            if a:
                ST["saga_cast"] = True
                ST["saga_oid"] = oid
                ST["saga_cast_at"] = time.time()
                say(f"[P0] casting Battle at the Helvault (oid {oid})")
                wire("cast_saga", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
        # 2) cast vanguards: spend freely before turn 5; from turn 5 keep
        #    {4}{W}{W} open once the saga is in hand
        if VANGUARD in hand_lnames(state, 0):
            reserve = 6 if (HELVAULT in hand_lnames(state, 0)
                            and (state.get("turn_number") or 0) >= 5) else 0
            if len(ul) >= 1 and len(ul) - 1 >= reserve:
                a, _oid = cast_action_for(acts, state, VANGUARD)
                if a:
                    say("[P0] casting Elite Vanguard")
                    wire("cast_vanguard", {"action": a["type"]})
                    await submit_as_is(c, a)
                    return
                wire("vanguard_cast_missing",
                     {"hand": hand_lnames(state, 0),
                      "untapped_lands": len(ul),
                      "act_types": sorted({a["type"] for a in acts})})
        # 3) land drop: Plains only (deck is all Plains)
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    if await maybe_answer_slot(c, "P1", st, acts, state):
        return
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
        if VANGUARD in hand_lnames(state, 1) and untapped_lands(state, 1):
            a, _oid = cast_action_for(acts, state, VANGUARD)
            if a:
                say("[P1] casting Elite Vanguard")
                await submit_as_is(c, a)
                return
            wire("p1_vanguard_cast_missing",
                 {"hand": hand_lnames(state, 1),
                  "untapped_lands": len(untapped_lands(state, 1)),
                  "act_types": sorted({a["type"] for a in acts})})
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)

# ------------------------------------------------------------- exports
async def export_pre(c):
    env = await c.export_state()
    with open(f"{EVDIR}/pre.json", "w") as f:
        f.write(env)
    ST["pre_exported"] = True
    say("exported pre.json (chapter trigger on stack / first slot seen)")


async def export_mid(c):
    env = await c.export_state()
    with open(f"{EVDIR}/mid_decline.json", "w") as f:
        f.write(env)
    ST["mid_exported"] = True
    say("exported mid_decline.json (after the slot-0 decline)")


async def export_post(c):
    env = await c.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(env)
    ST["post_exported"] = True
    say("exported post.json")


def saga_lore_info(state, saga_oid):
    o = get_obj(state, saga_oid)
    ser = json.dumps(o, default=str)
    info = {"keys": sorted(o.keys()), "has_lore_mention": "lore" in ser.lower()}
    for k in o.keys():
        if "counter" in str(k).lower():
            info["counters_field"] = {k: o[k]}
    return info


# ------------------------------------------------------------- finalization
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]
    pre = post = None
    for name in ("pre", "post"):
        try:
            v = json.load(open(f"{EVDIR}/{name}.json"))
        except Exception as e:
            notes.append(f"{name} load failed: {e}")
            v = None
        if name == "pre":
            pre = v
        else:
            post = v
    pre_st = (pre or {}).get("state") or {}
    post_st = (post or {}).get("state") or {}
    slots = ST["slots"]

    # A1: setup assembled
    lore = saga_lore_info(pre_st, ST["saga_oid"]) if pre_st else {}
    if (ST["trigger_seen"] and ST["pre_exported"]
            and ST.get("pre_p0_elig") and ST.get("pre_p1_elig")):
        ass["A1_setup_ok"] = "passed"
        notes.append(
            "A1 passed: Battle at the Helvault on P0 battlefield "
            f"(saga keys: {lore.get('keys')}, lore mention: "
            f"{lore.get('has_lore_mention')}); chapter I trigger observed "
            f"on the stack; eligible permanents P0={ST['pre_p0_elig']}, "
            f"P1={ST['pre_p1_elig']}.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append(
            f"A1 FAILED: trigger_seen={ST['trigger_seen']}, "
            f"pre_exported={ST['pre_exported']}, "
            f"pre_p0_elig={ST.get('pre_p0_elig')}, "
            f"pre_p1_elig={ST.get('pre_p1_elig')}.")

    # A2: prompt offered
    if slots:
        s0 = slots[0]
        ctrls = s0["candidate_summary"]["controllers"]
        ass["A2_prompt_offered"] = "passed"
        notes.append(
            f"A2 passed: chapter I target slot advertised to "
            f"{s0['advertised_to']} with {s0['candidate_summary']['n']} "
            f"candidates controlled by {ctrls} "
            f"(waiting_for={s0['waiting_for'].get('type')}).")
        if ctrls != [0]:
            notes.append(
                "A2 structure note: slot-0 candidates were NOT exclusively "
                f"P0-scoped (controllers={ctrls}); expected the first slot "
                "to cover the controller's own permanents.")
    else:
        ass["A2_prompt_offered"] = "failed"
        notes.append("A2 FAILED: no target-selection slot was ever offered "
                     "for the chapter I trigger.")

    # A3: decline accepted
    if (ST["decline_submitted"] and ST["decline_rejections"] == 0
            and (len(slots) > 1 or ST.get("moved_past_slot0"))):
        ass["A3_decline_accepted"] = "passed"
        notes.append(
            f"A3 passed: the slot-0 decline (decision="
            f"{slots[0]['decision']}) was accepted with no rejections; "
            f"the game moved past the slot.")
    elif ST["decline_submitted"] and ST["decline_rejections"] > 0:
        ass["A3_decline_accepted"] = "failed"
        notes.append(
            f"A3 FAILED: the decline submission was rejected "
            f"({ST['decline_rejections']} rejections); the reported decline "
            f"path could not be exercised.")
    elif slots and not ST["decline_submitted"]:
        ass["A3_decline_accepted"] = "failed"
        notes.append(
            f"A3 FAILED: slot 0 could not be declined "
            f"(decision={slots[0]['decision']}).")
    else:
        notes.append("A3 not-run: no slot was offered to decline.")

    # A4: iteration continued (the reported bug)
    if ST["iteration_continued"] is True:
        ass["A4_iteration_continues"] = "passed"
        s1 = slots[1]
        notes.append(
            f"A4 passed: after the decline, slot 1 was offered "
            f"(advertised to {s1['advertised_to']}, candidates controlled "
            f"by {s1['candidate_summary']['controllers']}) -- the trigger "
            f"iterated to the next player.")
    elif ST["iteration_continued"] is False:
        ass["A4_iteration_continues"] = "failed"
        notes.append(
            "A4 FAILED: after the slot-0 decline, the chapter ended with "
            "no second slot offered -- the reported bug reproduces: "
            "declining ends the chapter instead of continuing around to "
            "the next player.")
    else:
        notes.append("A4 not-run: the decline path never completed.")

    # A5: accept exiles
    if ST["accepted_exiled"]:
        ass["A5_accept_exiles"] = "passed"
        notes.append(
            f"A5 passed: the chosen P1 permanent (oid {ST['accept_oid']}) "
            f"is in Exile at post.")
    elif ST["accept_submitted"]:
        ass["A5_accept_exiles"] = "failed"
        notes.append(
            f"A5 FAILED: accepted oid {ST['accept_oid']} but it is not in "
            f"exile at post (zone="
            f"{get_obj(post_st, ST['accept_oid']).get('zone')}).")
    else:
        notes.append("A5 not-run: no accept leg ran "
                     "(expected when A4 failed).")

    # A6: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game continues.")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append(f"A6 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A6 not-run: no post state")

    if (ass["A1_setup_ok"] == "passed"
            and ass["A2_prompt_offered"] == "passed"
            and ass["A3_decline_accepted"] == "passed"
            and ass["A4_iteration_continues"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Battle at the Helvault chapter I offered "
            "one up-to-one target slot; declining it ended the chapter "
            "instead of iterating to the next player -- exactly the "
            "reported failure (confirmed on v0.99.0).")
    elif ass["A4_iteration_continues"] == "passed" \
            and ass["A5_accept_exiles"] == "passed":
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: declining slot 0 continued the "
            "iteration to P1's permanents and the chosen permanent was "
            "exiled. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported decline path could not "
                     "be fully exercised; see assertion notes.")

    # persist slot records
    with open(f"{EVDIR}/slots.json", "w") as f:
        json.dump(ST["slots"], f, indent=1, default=str)

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
               if "helvault" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7368c.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "saga_oid": ST["saga_oid"],
            "trigger_seen": ST["trigger_seen"],
            "n_slots": len(slots),
            "slot_decisions": [s["decision"] for s in slots],
            "decline_submitted": ST["decline_submitted"],
            "decline_rejections": ST["decline_rejections"],
            "iteration_continued": ST["iteration_continued"],
            "accept_oid": ST["accept_oid"],
            "accepted_exiled": ST["accepted_exiled"],
            "pre_p0_elig": ST.get("pre_p0_elig"),
            "pre_p1_elig": ST.get("pre_p1_elig"),
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets (8x/16x) are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The chapter trigger was reached via the saga's own ETB lore "
            "counter, not via Satsuki, the Living Lore's {T} ability as in "
            "the report; the clause under test is identical.",
            "The 'exiled until this Saga leaves the battlefield' return was "
            "not exercised (the saga was not removed).",
            "Chapter II was not driven (run finalizes after chapter I).",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
        "setup_line": "P0: 8x Battle at the Helvault + 16x Elite Vanguard + "
                      "36x Plains; P1: 16x Elite Vanguard + 44x Plains",
        "contract_line": "Battle at the Helvault chapter I: decline the "
                         "controller's up-to-one slot -> the trigger must "
                         "still offer a slot for the next player's "
                         "permanents",
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
                            if ST["decline_submitted"] and not ST.get("moved_past_slot0") \
                                    and len(ST["slots"]) == 1:
                                ST["decline_rejections"] += 1
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                if tag == "P0" and not ST["player_keys_logged"]:
                    ST["player_keys_logged"] = True
                    wire("player_keys",
                         {"p0_keys": sorted(player_of(state, 0).keys())})
                acts = merged_actions(st)

                if tag == "P0":
                    # trigger detection
                    if helvault_trigger_on_stack(state) and not ST["trigger_seen"]:
                        ST["trigger_seen"] = True
                        ST["trigger_seen_at"] = now
                        ST["phase"] = "trigger"
                        say(f"chapter I trigger on stack at turn "
                            f"{state.get('turn_number')}")
                        wire("trigger_seen",
                             {"stack": stack_entries(state),
                              "saga_lore": saga_lore_info(state, ST["saga_oid"])})
                        if not ST["saga_keys_logged"]:
                            ST["saga_keys_logged"] = True
                            wire("saga_object",
                                 {"oid": ST["saga_oid"],
                                  "obj": get_obj(state, ST["saga_oid"])})
                    # while the chapter trigger is on the stack, record the
                    # advertised action surface once per revision (catches a
                    # legacy target action if the engine uses one)
                    if helvault_trigger_on_stack(state):
                        key = ("trigacts", c.revision)
                        if key not in SUBMITTED:
                            SUBMITTED.add(key)
                            wire("trigger_acts",
                                 {"rev": c.revision,
                                  "waiting_for": wf_of(state).get("type"),
                                  "act_types": sorted({a["type"]
                                                       for a in acts})})
                    # pre export: first trigger sighting or first slot
                    if not ST["pre_exported"] and (ST["trigger_seen"] or ST["slots"]):
                        ST["pre_p0_elig"] = nonland_nonsaga_permanents(state, 0)
                        ST["pre_p1_elig"] = nonland_nonsaga_permanents(state, 1)
                        await export_pre(c)
                    # mid export: shortly after the decline
                    if ST["decline_submitted"] and not ST["mid_exported"]:
                        await export_mid(c)
                    if ST["accept_submitted"] and ST.get("accept_at") is None:
                        ST["accept_at"] = now
                    # accept leg: chosen permanent exiled?
                    if ST["accept_submitted"] and ST["accept_oid"] is not None:
                        if get_obj(state, ST["accept_oid"]).get("zone") == "Exile":
                            if not ST["accepted_exiled"]:
                                ST["accepted_exiled"] = True
                                say(f"accepted oid {ST['accept_oid']} is now "
                                    f"in Exile")
                    # chapter settle detection
                    if ST["phase"] in ("trigger", "slots") and ST["trigger_seen"]:
                        if helvault_trigger_on_stack(state):
                            ST["settle_at"] = None
                        else:
                            if ST["settle_at"] is None:
                                ST["settle_at"] = now
                            idle = now - ST["settle_at"]
                            if wf_of(state).get("type") == "Priority" and idle > 20:
                                if not ST["slots"]:
                                    say("chapter trigger left the stack with "
                                        "NO slot ever offered")
                                elif ST["awaiting_iteration"] and ST["iteration_continued"] is None:
                                    ST["iteration_continued"] = False
                                    ST["moved_past_slot0"] = True
                                    say("chapter ended after the decline: no "
                                        "second slot was offered (the "
                                        "reported bug)")
                                if ST["accept_submitted"] and ST["accepted_exiled"] \
                                        and not stack_entries(state):
                                    say("accept leg complete: exile verified, "
                                        "stack empty")
                                if not ST["post_exported"]:
                                    await export_post(c)
                                finalized = True
                                break
                    # accept-leg watchdog
                    if ST["accept_submitted"] and not ST["accepted_exiled"] \
                            and ST.get("accept_at") \
                            and now - ST["accept_at"] > ITERATION_WAIT_S:
                        say("accept watchdog: chosen permanent never reached "
                            "exile in 120s -- exporting stuck state")
                        wire("accept_stuck",
                             {"accept_oid": ST["accept_oid"],
                              "zone": get_obj(state, ST["accept_oid"]).get("zone"),
                              "waiting_for": wf_of(state)})
                        if not ST["post_exported"]:
                            await export_post(c)
                        finalized = True
                        break
                    # decline watchdog: slot answered but game never moves on
                    if ST["decline_submitted"] and not ST.get("moved_past_slot0") \
                            and len(ST["slots"]) == 1 and ST.get("decline_at") \
                            and now - ST["decline_at"] > ITERATION_WAIT_S \
                            and not ST["post_exported"]:
                        say("decline watchdog: 120s after the decline the "
                            "game has not moved past slot 0 -- exporting "
                            "stuck state")
                        wire("decline_stuck",
                             {"waiting_for": wf_of(state),
                              "trigger_on_stack": helvault_trigger_on_stack(state)})
                        await export_post(c)
                        finalized = True
                        break
                    # saga-cast watchdog: cast but no trigger within 180s
                    if ST["saga_cast"] and not ST["trigger_seen"] \
                            and ST.get("saga_cast_at") \
                            and now - ST["saga_cast_at"] > 180 \
                            and not ST["post_exported"]:
                        say("saga watchdog: 180s after cast, no chapter "
                            "trigger seen -- exporting stuck state")
                        wire("saga_stuck",
                             {"saga_oid": ST["saga_oid"],
                              "zone": get_obj(state, ST["saga_oid"]).get("zone"),
                              "waiting_for": wf_of(state)})
                        await export_post(c)
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7368c.py",
                f"{EVDIR}/scenario_7368c.py")
    sys.exit(0)
