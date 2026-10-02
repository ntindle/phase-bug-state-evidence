#!/usr/bin/env python3
"""Issue #7380: Pre-War Formalwear doesn't attach to the returned creature.

Reported (discord 2026-08-13, sync-filed, status:needs-repro, area:engine,
classifier:supported-aspect-defect): "doesn't attach to the returned creature."

Oracle text (verified from pinned v0.99.0 card-data.json, key
'pre-war formalwear'):
  {2}{W} Artifact - Equipment
  "When this Equipment enters, return target creature card with mana value 3
  or less from your graveyard to the battlefield and attach this Equipment
  to it.
  Equipped creature gets +2/+2 and has vigilance.
  Equip {3}"

The 2026-08-15 triage (mike-theDude) ruled out the parser: the ETB trigger
lowers to ChangeZone(graveyard -> battlefield, target: MV<=3 creature you
control in graveyard) with an Attach{target: parent target} child. The
suspected mechanism is the zone-change identity problem: the Attach step
resolves against the graveyard card's pre-move identity instead of the
battlefield permanent it became.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Pre-War Formalwear + 4x Savannah Lions + 52x Plains
  P1: 4x Lightning Bolt + 56x Mountain
Drive:
  1. P0 T1: Plains, cast Savannah Lions ({W}).
  2. P1 T1: Mountain, cast Lightning Bolt targeting the lion (2/1 dies).
  3. P0: plays a Plains each turn; on a main phase with >=3 untapped Plains,
     Formalwear in hand, the lion card in P0's graveyard, and an empty stack:
       a. export pre (scope: setup assembled)
       b. cast Pre-War Formalwear ({2}{W})
       c. export cast (Formalwear spell on the stack)
       d. Formalwear resolves and enters; ETB trigger goes on the stack.
       e. answer the trigger's TargetSelection with the graveyard lion card
          (the only MV<=3 creature card in P0's graveyard)
       f. export trigger (trigger on stack, target chosen -- the real pre
          for the operation under investigation)
       g. pass priority; the trigger resolves.
  4. export post after the stack settles; finalize.

Expected (correct behavior): the lion returns to the battlefield with
Pre-War Formalwear attached to it; the lion becomes 4/3 with vigilance.
Reported (bug): the creature returns but the Equipment stays unattached.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok      pre exported: Formalwear in P0 hand, Savannah Lions card
                   in P0 graveyard, >=3 untapped Plains, life 20/20.
  A2_cast_ok       Formalwear cast with no rejections, seen on the stack,
                   then on P0's battlefield.
  A3_target_ok     ETB trigger seen on the stack; its TargetSelection was
                   answered with the graveyard lion card (or the engine
                   resolved the target without a prompt); no rejections.
  A4_attach        (THE REPORTED BUG) post state: the lion object is on P0's
                   battlefield AND Pre-War Formalwear references the lion's
                   object id as its attached/equipped creature. If the lion
                   is back on the battlefield but Formalwear is unattached,
                   the reported bug reproduces.
  A5_static_bonus  lion is 4/3 with vigilance (the +2/+2 static functioning).
  A6_cleanup       post stack empty, game advancing.

Verdict rule: reproduced iff A1..A3 passed and A4 failed (the lion returned
              but Formalwear did not attach);
              not-reproduced iff A4 passed (attached, A5 corroborates);
              blocked iff A1 failed (the setup line never assembled).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions.
  - Target selection via viewer_interaction: schema/sequence or
    exactChoices; candidates matched on serialized content.
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
RUN_ID = "20261002-7380b"
ISSUE = 7380
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

FW = "pre-war formalwear"
LION = "savannah lions"
PLAINS = "plains"
BOLT = "lightning bolt"
MOUNTAIN = "mountain"

P0_DECK = [("Pre-War Formalwear", 4), ("Savannah Lions", 4), ("Plains", 52)]
P1_DECK = [("Lightning Bolt", 4), ("Mountain", 56)]

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
        "phase": "setup",  # setup -> casting_fw -> targeting -> resolving -> done
        "lion_cast": False,
        "bolt_cast": False,
        "fw_cast_submitted": False,
        "fw_cast_submitted_at": None,
        "fw_cast_rejections": 0,
        "fw_seen_on_stack": False,
        "fw_stack_oid": None,
        "fw_bf_oid": None,
        "target_offered": False,
        "target_chosen": False,
        "target_candidate_id": None,
        "target_rejections": 0,
        "trigger_seen": False,
        "trigger_seen_at": None,
        "pre_exported": False,
        "cast_exported": False,
        "trigger_exported": False,
        "post_exported": False,
        "settle_at": None,
        "states_seen": 0,
        "pre_life": None,
        "pre_hand_n": None,
        "lion_gy_oid": None,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_cast_ok",
                                       "A3_target_ok", "A4_attach",
                                       "A5_static_bonus", "A6_cleanup")},
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


def untapped_ids(state, pid, key):
    return [i for i in zone_ids(state, pid, "Battlefield", key)
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


def stack_spell_oid(state, key):
    """Find a stack entry whose resolved object is the named card (spell
    entries often carry only an object id, no card name)."""
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


def n_candidates(opp):
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    return len(data.get("choices") or data.get("candidates") or [])

# ------------------------------------------------------------- data check
def check_data_level():
    """A0 (informational): card data parses as the report/triage expect."""
    fw = CARD_DATA.get("pre-war formalwear", {})
    lion = CARD_DATA.get("savannah lions", {})
    findings = {
        "fw_name": fw.get("name"),
        "fw_mana_cost": fw.get("mana_cost"),
        "fw_oracle": fw.get("oracle_text"),
        "lion_name": lion.get("name"),
        "lion_mana_cost": lion.get("mana_cost"),
        "lion_oracle": lion.get("oracle_text"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    ok = (
        fw.get("mana_cost") == {"type": "Cost", "shards": ["White"], "generic": 2}
        and "attach this Equipment to it" in (fw.get("oracle_text") or "")
        and lion.get("mana_cost") == {"type": "Cost", "shards": ["White"], "generic": 0}
    )
    say(f"data-level check: {'OK' if ok else 'MISMATCH'}")
    ST["notes"].append(f"data-level: Pre-War Formalwear {{2}}{{W}} / "
                       f"Savannah Lions {{W}} parse as expected: {ok}")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    return PLAINS in hand and LION in hand and FW in hand


def mulligan_keep_p1(hand):
    return MOUNTAIN in hand and BOLT in hand


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
    rank = {FW: 5, LION: 5, BOLT: 5, PLAINS: 4, MOUNTAIN: 4}
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
    rank = {PLAINS: 0, MOUNTAIN: 0, FW: 1, BOLT: 1, LION: 1}
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


async def answer_target_opp(c, st, state, phase, needle_sets, label):
    """Answer a TargetSelection opportunity whose candidates match any of
    the needle sets (each a list of strings; all must appear,
    case-insensitive). Returns True if a submission was sent."""
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
        SUBMITTED_OPPS.add(iid)
        rtype = (opp.get("response", {}) or {}).get("type")
        cid = None
        matched_needles = None
        for needles in needle_sets:
            cid = candidate_matching(opp, needles)
            if cid is not None:
                matched_needles = needles
                break
        wire("target_opp", {"phase": phase, "iid": iid, "rtype": rtype,
                            "needle_sets": needle_sets,
                            "matched_needles": matched_needles,
                            "matched_candidate": cid,
                            "n_choices": n_candidates(opp),
                            "opportunity": opp})
        if cid is None:
            say(f"[{label}] WARNING: no candidate matched {needle_sets}; "
                f"not submitting blind")
            ST["notes"].append(
                f"{phase}: target selection offered but no candidate "
                f"matched {needle_sets}; see wire_log target_opp")
            continue
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
        ST["target_candidate_id"] = cid
        ST["target_chosen"] = True
        say(f"[{label}] target -> candidate {cid} (needles {matched_needles})")
        acted = True
    return acted


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
    if ST["phase"] == "casting_fw":
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
    # answer the ETB trigger's target selection (P0's opportunity)
    if ST["phase"] in ("targeting", "resolving") and not ST["target_chosen"]:
        gy = zone_ids(state, 0, "Graveyard", LION)
        needle_sets = [[str(gy[0])], [LION]] if gy else [[LION]]
        if await answer_target_opp(c, st, state, "fw_trigger",
                                   needle_sets, "P0"):
            return
    if not my_priority(state, 0):
        return
    if ST["phase"] not in ("setup", "casting_fw", "targeting", "resolving"):
        await pass_priority(c, st, acts)
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        # 1) T1: cast Savannah Lions
        if (not ST["lion_cast"] and LION in hand_lnames(state, 0)
                and untapped_ids(state, 0, PLAINS)):
            a, oid = cast_action_for(acts, state, LION)
            if a:
                ST["lion_cast"] = True
                say(f"[P0] casting Savannah Lions (oid {oid})")
                wire("cast_lion", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
        # 2) cast Pre-War Formalwear once the lion is in the graveyard
        gy = zone_ids(state, 0, "Graveyard", LION)
        if (not ST["fw_cast_submitted"] and FW in hand_lnames(state, 0)
                and gy and len(untapped_ids(state, 0, PLAINS)) >= 3
                and not stack_entries(state)):
            a, oid = cast_action_for(acts, state, FW)
            if a:
                ST["fw_cast_submitted"] = True
                ST["fw_cast_submitted_at"] = time.time()
                ST["lion_gy_oid"] = gy[0]
                p0p = player_of(state, 0)
                p1p = player_of(state, 1)
                ST["pre_life"] = (p0p.get("life"), p1p.get("life"))
                ST["pre_hand_n"] = len(hand_ids(state, 0))
                await export_as(c, "pre")
                ST["pre_exported"] = True
                ST["phase"] = "casting_fw"
                say(f"[P0] pre exported: Formalwear in hand, lion "
                    f"(oid {ST['lion_gy_oid']}) in P0 graveyard, "
                    f"{len(untapped_ids(state, 0, PLAINS))} untapped "
                    f"Plains, life {ST['pre_life']}")
                wire("cast_fw", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
            wire("fw_cast_missing",
                 {"hand": hand_lnames(state, 0),
                  "act_types": sorted({a["type"] for a in acts})})
        # 3) land drop
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
    if ST["phase"] == "bolting":
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
    # answer the Bolt target selection: the lion on P0's battlefield.
    # Match the OBJECT candidate precisely (name + battlefield zone);
    # player candidates carry seats, and a bare object-id needle matches
    # every candidate id (they all embed the interaction counter).
    if ST["phase"] == "bolting" and not ST["target_chosen"]:
        lion_bf = zone_ids(state, 0, "Battlefield", LION)
        if lion_bf:
            if await answer_target_opp(
                    c, st, state, "bolt",
                    [[LION, "battlefield"],
                     [f'"reference": "{lion_bf[0]}"']], "P1"):
                ST["phase"] = "setup"
                return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1:
        # bolt the lion as soon as it is on P0's battlefield
        lion_bf = zone_ids(state, 0, "Battlefield", LION)
        if (not ST["bolt_cast"] and BOLT in hand_lnames(state, 1)
                and lion_bf and untapped_ids(state, 1, MOUNTAIN)):
            a, oid = cast_action_for(acts, state, BOLT)
            if a:
                ST["bolt_cast"] = True
                ST["phase"] = "bolting"
                ST["target_chosen"] = False
                say(f"[P1] casting Lightning Bolt (oid {oid}) at lion "
                    f"(bf oid {lion_bf[0]})")
                wire("cast_bolt", {"oid": oid, "lion_bf_oid": lion_bf[0]})
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)

# ------------------------------------------------------------- finalize
def references(obj, oid):
    """True if any value in the object dict references the object id."""
    needle = str(oid)
    for k, v in (obj or {}).items():
        if needle == str(v) or needle in str(v):
            return True, k
    return False, None


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
    trigger = load_env("trigger") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    cast_st = cast.get("state") or {}
    trigger_st = trigger.get("state") or {}
    post_st = post.get("state") or {}

    # A1: setup assembled
    if ST["pre_exported"] and pre_st:
        fw_hand = FW in hand_lnames(pre_st, 0)
        gy = zone_ids(pre_st, 0, "Graveyard", LION)
        plains_n = len(untapped_ids(pre_st, 0, PLAINS))
        life_ok = ST["pre_life"] == (20, 20)
        if fw_hand and gy and plains_n >= 3 and life_ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(
                f"A1 passed: pre exported with Pre-War Formalwear in P0 "
                f"hand, Savannah Lions (oid {gy[0]}) in P0 graveyard, "
                f"{plains_n} untapped Plains, life 20/20.")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(
                f"A1 FAILED: fw_in_hand={fw_hand}, lion_gy={gy}, "
                f"untapped_plains={plains_n}, pre_life={ST['pre_life']}.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: pre state was never exported (Formalwear "
                     "was never cast with the lion in the graveyard).")

    # A2: Formalwear cast cleanly and entered
    if ST["cast_exported"] and cast_st:
        notes.append(
            f"A2 context: fw_seen_on_stack={ST['fw_seen_on_stack']}, "
            f"cast_rejections={ST['fw_cast_rejections']}, "
            f"fw_bf_oid={ST['fw_bf_oid']}.")
        if (ST["fw_seen_on_stack"] and ST["fw_cast_rejections"] == 0
                and ST["fw_bf_oid"] is not None):
            ass["A2_cast_ok"] = "passed"
            notes.append(
                f"A2 passed: Formalwear cast with no rejections, seen on "
                f"the stack (oid {ST['fw_stack_oid']}), then on P0's "
                f"battlefield (oid {ST['fw_bf_oid']}).")
        else:
            ass["A2_cast_ok"] = "failed"
            notes.append("A2 FAILED: the Formalwear cast path did not "
                         "complete cleanly (see A2 context).")
    elif ST["fw_cast_submitted"]:
        ass["A2_cast_ok"] = "failed"
        notes.append("A2 FAILED: Formalwear cast was submitted but the "
                     "cast export is missing.")
    else:
        notes.append("A2 not-run: Formalwear was never cast.")

    # A3: ETB trigger fired and its target was chosen
    if ST["trigger_exported"] and trigger_st:
        notes.append(
            f"A3 context: trigger_seen={ST['trigger_seen']}, "
            f"target_offered={ST['target_offered']}, "
            f"target_chosen={ST['target_chosen']} "
            f"(candidate {ST['target_candidate_id']}), "
            f"target_rejections={ST['target_rejections']}.")
        if (ST["trigger_seen"] and ST["target_chosen"]
                and ST["target_rejections"] == 0):
            ass["A3_target_ok"] = "passed"
            notes.append(
                f"A3 passed: the ETB trigger went on the stack and its "
                f"target selection was answered with the graveyard lion "
                f"card (candidate {ST['target_candidate_id']}), no "
                f"rejections.")
        elif ST["trigger_seen"] and not ST["target_offered"]:
            ass["A3_target_ok"] = "passed"
            notes.append(
                "A3 passed (engine auto-targeted): the ETB trigger went "
                "on the stack and resolved its target without offering a "
                "TargetSelection prompt (only one legal target existed).")
        else:
            ass["A3_target_ok"] = "failed"
            notes.append("A3 FAILED: the trigger/target path did not "
                         "complete cleanly (see A3 context).")
    elif ST["trigger_seen"]:
        ass["A3_target_ok"] = "failed"
        notes.append("A3 FAILED: trigger seen but the trigger export is "
                     "missing.")
    else:
        notes.append("A3 not-run: the ETB trigger was never observed.")

    # A4: THE REPORTED BUG -- is Formalwear attached to the returned lion?
    if ST["post_exported"] and post_st:
        lion_bf = zone_ids(post_st, 0, "Battlefield", LION)
        fw_bf = zone_ids(post_st, 0, "Battlefield", FW)
        fw_obj = get_obj(post_st, fw_bf[0]) if fw_bf else {}
        attach_keys = {k: v for k, v in fw_obj.items()
                       if "attach" in k.lower() or "equip" in k.lower()}
        wire("post_attach_probe",
             {"lion_bf": lion_bf, "fw_bf": fw_bf,
              "fw_attach_fields": attach_keys,
              "fw_keys": sorted(fw_obj.keys())})
        notes.append(
            f"A4 probe: post lion on P0 BF={lion_bf}; Formalwear on P0 "
            f"BF={fw_bf}; Formalwear attach/equip fields={attach_keys}.")
        attached = False
        attached_via = None
        if lion_bf and fw_bf:
            ok, via = references(fw_obj, lion_bf[0])
            attached, attached_via = ok, via
            if not attached:
                lion_obj = get_obj(post_st, lion_bf[0])
                ok2, via2 = references(lion_obj, fw_bf[0])
                notes.append(
                    f"A4 reverse probe: lion object references Formalwear: "
                    f"{ok2} via {via2}; lion keys="
                    f"{sorted(lion_obj.keys())}")
                attached, attached_via = ok2, f"lion->{via2}"
        if attached and lion_bf:
            ass["A4_attach"] = "passed"
            notes.append(
                f"A4 passed: the returned lion (oid {lion_bf[0]}) is on "
                f"P0's battlefield and Pre-War Formalwear (oid "
                f"{fw_bf[0]}) references it via {attached_via} -- the "
                f"Equipment attached.")
        elif lion_bf and fw_bf:
            ass["A4_attach"] = "failed"
            notes.append(
                f"A4 FAILED: the lion returned to P0's battlefield (oid "
                f"{lion_bf[0]}) but Pre-War Formalwear (oid {fw_bf[0]}) "
                f"does NOT reference it (attach/equip fields "
                f"{attach_keys}) -- the reported bug reproduces: the "
                f"Equipment stayed unattached.")
        else:
            ass["A4_attach"] = "failed"
            notes.append(
                f"A4 FAILED: unexpected post shape (lion_bf={lion_bf}, "
                f"fw_bf={fw_bf}); the trigger resolution did not produce "
                f"the expected board.")
    else:
        notes.append("A4 not-run: post state missing.")

    # A5: static bonus corroboration
    if (ST["post_exported"] and post_st and ass["A4_attach"] == "passed"):
        lion_bf = zone_ids(post_st, 0, "Battlefield", LION)
        lion_obj = get_obj(post_st, lion_bf[0]) if lion_bf else {}
        pt = (lion_obj.get("power"), lion_obj.get("toughness"))
        kw = lion_obj.get("keywords") or lion_obj.get("abilities") or []
        kw_ser = json.dumps(kw, default=str).lower()
        if pt == (4, 3) and "vigilance" in kw_ser:
            ass["A5_static_bonus"] = "passed"
            notes.append(
                f"A5 passed: equipped lion is 4/3 with vigilance "
                f"(power/toughness={pt}).")
        else:
            ass["A5_static_bonus"] = "failed"
            notes.append(
                f"A5 FAILED: lion power/toughness={pt}, vigilance in "
                f"keywords={('vigilance' in kw_ser)} -- the +2/+2 static "
                f"is not functioning as expected.")
    elif ass["A4_attach"] == "passed":
        notes.append("A5 not-run: post state missing.")
    else:
        notes.append("A5 not-run: Formalwear did not attach.")

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
            and ass["A2_cast_ok"] == "passed"
            and ass["A3_target_ok"] == "passed"
            and ass["A4_attach"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Pre-War Formalwear's ETB trigger returned "
            "Savannah Lions to P0's battlefield, but the Equipment did not "
            "attach to it -- exactly the reported failure (confirmed on "
            "v0.99.0).")
    elif ass["A4_attach"] == "passed":
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the returned lion entered with "
            "Pre-War Formalwear attached. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported trigger path could not "
                     "be fully exercised; see assertion notes.")

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
               if "formalwear" in l.lower() or "savannah" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7380.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "lion_cast": ST["lion_cast"],
            "bolt_cast": ST["bolt_cast"],
            "fw_bf_oid": ST["fw_bf_oid"],
            "lion_gy_oid": ST["lion_gy_oid"],
            "target_offered": ST["target_offered"],
            "target_chosen": ST["target_chosen"],
            "target_candidate_id": ST["target_candidate_id"],
            "target_rejections": ST["target_rejections"],
            "trigger_seen": ST["trigger_seen"],
            "checkpoints": {
                "pre": {"life": ST["pre_life"], "hand_n": ST["pre_hand_n"]},
            },
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report's attached Discord game state was not imported (the "
            "prebuilt server has no standalone state-restore facility); the "
            "scenario replays the reported line (cast Formalwear with an "
            "MV<=3 creature card in the graveyard) from a fresh game.",
            "Only Savannah Lions (MV 1) was used as the returned creature; "
            "other MV<=3 targets were not exercised.",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
        "setup_line": "P0: 4x Pre-War Formalwear + 4x Savannah Lions + "
                      "52x Plains; P1: 4x Lightning Bolt + 56x Mountain",
        "contract_line": "Cast Pre-War Formalwear with Savannah Lions in "
                         "the graveyard -> the lion must return to the "
                         "battlefield with the Equipment attached (4/3, "
                         "vigilance)",
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
                            if ST["phase"] == "casting_fw":
                                ST["fw_cast_rejections"] += 1
                            elif ST["phase"] in ("targeting", "resolving") \
                                    and tag == "P0":
                                ST["target_rejections"] += 1
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                ST["states_seen"] += 1
                acts = merged_actions(st)

                if tag == "P0":
                    # Formalwear cast detection: spell on the stack
                    if ST["phase"] == "casting_fw" and not ST["fw_seen_on_stack"]:
                        foid = stack_spell_oid(state, FW)
                        if foid is not None:
                            ST["fw_seen_on_stack"] = True
                            ST["fw_stack_oid"] = foid
                            wire("fw_on_stack",
                                 {"oid": foid,
                                  "entries": stack_entries(state)})
                            say(f"Formalwear on stack (oid {foid}); "
                                f"exporting cast")
                            await export_as(c, "cast")
                            ST["cast_exported"] = True
                    # Formalwear entered: on P0's battlefield
                    if ST["fw_bf_oid"] is None:
                        fwb = zone_ids(state, 0, "Battlefield", FW)
                        if fwb:
                            ST["fw_bf_oid"] = fwb[0]
                            if ST["phase"] == "casting_fw":
                                ST["phase"] = "targeting"
                            wire("fw_entered", {"bf_oid": fwb[0]})
                            say(f"Formalwear entered the battlefield "
                                f"(oid {fwb[0]}); phase -> targeting")
                    # trigger detection
                    if ST["phase"] in ("targeting", "resolving"):
                        hits = [e for e in stack_entries(state)
                                if "formalwear" in json.dumps(e, default=str).lower()]
                        if hits and not ST["trigger_seen"]:
                            ST["trigger_seen"] = True
                            ST["trigger_seen_at"] = now
                            wire("trigger_on_stack", {"entries": stack_entries(state)})
                            say("Formalwear ETB trigger observed ON THE STACK")
                        wtype = (wf_of(state).get("type") or "")
                        if "targetselection" in wtype.lower():
                            ST["target_offered"] = True
                    # trigger export: target chosen + trigger still on stack
                    if (ST["phase"] == "targeting" and not ST["trigger_exported"]
                            and ST["trigger_seen"] and ST["target_chosen"]):
                        await export_as(c, "trigger")
                        ST["trigger_exported"] = True
                        ST["phase"] = "resolving"
                        ST["settle_at"] = now
                        say("trigger exported (target chosen); phase -> resolving")
                    # settle detection: stack empty + Priority + idle
                    if ST["phase"] == "resolving" and ST["trigger_exported"]:
                        if not stack_entries(state):
                            if ST["settle_at"] is None:
                                ST["settle_at"] = now
                            idle = now - ST["settle_at"]
                            wtype = (wf_of(state).get("type") or "")
                            if wtype == "Priority" and idle > SETTLE_IDLE_S:
                                say(f"settled: stack empty, Priority, "
                                    f"{idle:.0f}s idle; exporting post")
                                await export_as(c, "post")
                                ST["post_exported"] = True
                                finalized = True
                                break
                        else:
                            ST["settle_at"] = None
                    # watchdogs: stuck states
                    if (ST["fw_cast_submitted"] and not ST["cast_exported"]
                            and ST.get("fw_cast_submitted_at")
                            and now - ST["fw_cast_submitted_at"] > 180
                            and not ST["post_exported"]):
                        say("fw-cast watchdog: 180s after submission, cast "
                            "never completed -- exporting stuck state")
                        wire("fw_cast_stuck",
                             {"waiting_for": wf_of(state),
                              "fw_seen_on_stack": ST["fw_seen_on_stack"]})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "targeting" and ST["trigger_seen"]
                            and not ST["trigger_exported"]
                            and ST.get("trigger_seen_at")
                            and now - ST["trigger_seen_at"] > 120
                            and not ST["post_exported"]):
                        say("target watchdog: 120s after trigger seen, "
                            "target never resolved -- exporting stuck state")
                        wire("target_stuck",
                             {"waiting_for": wf_of(state),
                              "target_offered": ST["target_offered"],
                              "target_chosen": ST["target_chosen"]})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    # setup-stall watchdog: the line never assembled
                    # (e.g. P0 never drew Formalwear) -- don't spin the
                    # full deadline; record blocked.
                    if (ST["phase"] == "setup"
                            and not ST["fw_cast_submitted"]
                            and now - t_start > 900
                            and not ST["post_exported"]):
                        say("setup watchdog: 900s in, Formalwear never "
                            "cast -- exporting state and finalizing")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    # game-over detection: finalize instead of spinning
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7380.py",
                f"{EVDIR}/scenario_7380.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Pre-War Formalwear doesn't attach to the returned creature"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
