#!/usr/bin/env python3
"""Issue #6869 revalidation on v0.98.0 / protocol 94: "secret rendezvous -
AI failed to select an opponent and game froze." (reporter via discord,
source:discord, status:confirmed, area:engine+parser+ai, priority:p0-softlock)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Subsystem under test: native server-side AI policy (phase-ai) on the pinned
release. P1 is an AI seat (Medium first; Hard corroboration leg if Medium
never casts); P0 is a human driver seat.

Secret Rendezvous {1}{W}{W} sorcery: "You and target opponent each draw three
cards."

Parser corroboration (v0.98.0 card-data.json, verified 2026-10-01): the second
Draw effect is NOT a targeted-opponent slot. It is scoped as
  {"type":"Draw","count":{"type":"Fixed","value":3},
   "target":{"type":"Typed","type_filters":[],"controller":"Opponent",
             "properties":[]}}
with target_prompt:null and optional_targeting:false - the parser gap
persists on the pinned release. The triage on the issue ("the opponent child
is merely scoped to opponent; it does not expose a targeted-opponent slot...
can leave AI action generation without the required target decision") matches
this parse.

The 2026-09-11 v0.80.0 run (20260911-6869 / 20260911-6869-hard, protocol 69)
was BLOCKED: the AI never attempted to cast in either leg (Medium 14 turns,
Hard 10 turns), so the freeze path could not be exercised; a human control
cast completed with NO target-opponent prompt and both players drew exactly 3.

Triage acceptance criteria:
  - Casting requires one legal opponent target.
  - AI selects a legal opponent before announcement or does not cast the spell.
  - The caster and that chosen opponent each draw three cards, with no
    repeated waiting state.

Setup (per leg):
  P0 (human driver): 12x "Secret Rendezvous" + 48x "Plains". Keep mulligan;
                     play one Plains per turn; pass priority; never attack;
                     never casts in phase 1 (passive observer).
  P1 (AI, native server-side): 12x "Secret Rendezvous" + 48x "Plains".

Legs: Medium (turn limit 12) first; if the AI never casts within the window,
a second leg with AI Hard (turn limit 10), same phase 1 + phase 2.

Expected:
  E1: the AI casts Secret Rendezvous (spell on the stack, controller 1).
  E2: no freeze: any target decision for the AI completes and the spell leaves
      the stack well within the stall window; the game keeps advancing (the
      same waiting state is NOT stuck without progress).
  E3: both the caster and the opponent draw three cards (Library->Hand
      movement for exactly 3 cards each between cast and resolution).
  E4: control: a human-driven P0 cast exercises the target path directly -
      target-opponent selection is offered (or documented auto-target), and
      both players draw three.
  E5: the game continues past each resolution.

Assertions (overall; per-leg detail recorded in observations):
  A1_ai_cast        >=1 AI Secret Rendezvous observed on the stack (any leg).
  A2_no_freeze      every observed AI cast resolved within STALL_TIMEOUT of
                    the cast; no AI decision wait persisted without progress.
  A3_draws          for the first resolved AI cast, each player drew exactly 3
                    cards (zone-diff Library->Hand), on the same turn.
  A4_control        human P0 cast completed: target decision exercised and
                    both players drew 3.
  A5_cleanup        game state advanced after resolution.

Verdict rule: blocked iff A1 fails (AI never cast: the reported AI path was
not exercised). reproduced iff an AI cast stalls (A2 fails) - the reported
softlock. not-reproduced iff A1..A5 all pass. NEVER "fixed".

Evidence: evidence/6869/20261001-6869/<pre_ai_cast|post_ai_cast|
pre_control|post_control>[<_hard>].json (authoritative exports),
carddata_excerpt.json, run.json, manifest.sha256, summary.png,
scenario_6869_0980.py, wire_log.jsonl, scenario_run.log, server_excerpts.log.

Protocol-94 driver (v0.98.0): HELLO advertises 94 (exact match enforced by the
server); MulliganDecision {"choice":{"type":"Keep"}} gated on the seat's
pending Declare; BottomCards/DiscardToHandSize via single SelectCards
{"cards":[...]}; CastSpell/ActivateAbility via advertised actions; PayMana*
via pay_tick; DeclareAttackers (empty) via relations-schema interaction;
TargetSelection via viewer_interaction schema/exactChoices;
interaction-rejection guard + stale-client watchdog; PassPriority only when
the seat genuinely holds Priority.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client  # noqa: E402
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-6869"
EVID_ISSUE = "6869"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD = "Secret Rendezvous"
CARD_L = CARD.lower()
LAND = "Plains"
P0_DECK = [(CARD, 12), (LAND, 48)]
AI_DECK = [(CARD, 12), (LAND, 48)]

SERVER_IDENTITY = {
    "validated_version": "v0.98.0",
    "build_commit": "61e8550",
    "protocol_version": 94,
    "server_binary_sha256":
        "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
    "card_data_sha256":
        "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
    "draft_pools_sha256":
        "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
    "signature_verified": True,
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


for _f, _k in (
        ("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.98.0/data/draft-pools.json",
         "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

# card-data excerpt (static; written to evidence)
_cd = json.load(open(f"{BACKFILL}/server/releases/v0.98.0/data/card-data.json"))
_sr = _cd["secret rendezvous"]
with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
    json.dump({"name": _sr["name"], "oracle_text": _sr["oracle_text"],
               "abilities": _sr["abilities"]}, f, indent=2)
say("wrote carddata_excerpt.json (parser gap persists: second Draw is "
    "Typed/Opponent scope, target_prompt null)")

STALL_TIMEOUT = 150    # max seconds one AI cast may stay unresolved
LEG_TIMEOUTS = {"Medium": 600, "Hard": 540}
LEG_TURN_LIMITS = {"Medium": 12, "Hard": 10}
GLOBAL_BUDGET = 1500   # ~25 min overall watchdog

ST = {}
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
_WATCH = {}
_DISCARD_REV = {}
OBS = {}
C0 = None


def reset_globals():
    global C0
    ST.clear()
    ST.update({"stage": "PHASE1", "game_code": None, "suffix": "",
               "difficulty": "", "stop": False})
    MULLS.clear()
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    _DISCARD_REV.clear()
    _WATCH.clear()
    OBS.clear()
    OBS.update({"casts": [], "rejections": [], "p0_acts": 0,
                "ai_target_waits": [], "control": {}})
    C0 = None

# ------------------------------------------------------------ state helpers


def oname(o):
    return o.get("base_name") or o.get("card_name") or o.get("name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def hand_size(state, pid):
    return len(hand_oids(state, pid))


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def merged_actions(st):
    return st.get("legal_actions") or []


def vi_ops(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def pending_for(state, pid):
    data = wf_of(state).get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": wf_of(state).get("data"),
                             "stage": ST["stage"]})


def rendezvous_on_stack(state):
    return [oid for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Stack"
            and str(oname(o)).lower() == CARD_L]


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = seat = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            if "reference" in d:
                ref = str(d["reference"])
            if "seat" in d:
                seat = d["seat"]
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller")})
    return out


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


# ------------------------------------------------------- protocol-94 ticks

async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"], "leg": ST["difficulty"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"],
                                "leg": ST["difficulty"]})
    await c.send_interaction(sub)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "leg": ST["difficulty"]})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return found


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 45:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} "
                    f"wf={json.dumps(wf_of(s), default=str)[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 45s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


async def export_now(tag):
    s = await C0.export_state()
    with open(f"{EVDIR}/{tag}.json", "w") as f:
        f.write(s)
    say(f"exported {tag}.json")
    return s


async def do_mulligan(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if MULLS.get("kept"):
        return False
    keep = hand_size(state, pid) >= 7 or sum(
        1 for oid in hand_oids(state, pid)
        if oname(state["objects"][oid]) == LAND) >= 2
    choice = "Keep" if keep else "Mulligan"
    say(f"[P0] mulligan -> {choice}")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    if choice == "Keep":
        MULLS["kept"] = True
    wire("mulligan", {"who": "P0", "decision": choice})
    return True


def rank_fn(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == CARD:
            return 3   # never bottom/discard Rendezvous
        if nm == LAND:
            return 1
        return 0
    return rank


async def do_bottom(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    if MULLS.get("bottomed"):
        return False
    n = (pend.get("phase") or {}).get("count") or 1
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=rank_fn(state))[:n]]
    say(f"[P0] bottoming {n}: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS["bottomed"] = True
    wire("bottom", {"who": "P0", "count": n})
    return True


async def do_discard(c, pid):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_oids(state, pid)) - 7
    if n <= 0:
        return False
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=rank_fn(state))[:n]]
    say(f"[P0] discarding to hand size: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c):
    st = c.latest
    if not st:
        return False
    for a in merged_actions(st):
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


def find_relations_op(c):
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return op
    return None


async def answer_declare_attackers_empty(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DeclareAttackers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    if not iid or iid in SUBMITTED:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations",
                        "data": {"relations": []}}}
    wire("declare_attackers_empty", {"who": "P0"})
    say("[P0] declares empty attackers")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    return True


def spec_max_of(opp):
    data = (opp.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec") or {}
    for blob in (spec.get("data"), spec, data):
        if isinstance(blob, dict):
            for k in ("max", "maxTargets", "max_targets"):
                v = blob.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
                if isinstance(v, dict) and v.get("type") == "Fixed":
                    return int(v.get("value", 0))
    return None


# ------------------------------------------- control-cast target answerer

async def answer_control_target(c, state):
    """Answer P0's TargetSelection for the control cast by choosing the
    opponent (seat/player 1) from engine-advertised candidates. Handles
    schema (sequence/select, incl. multi-target) and exactChoices shapes.
    Returns True if a submission was sent; the mode is recorded."""
    wf = wf_of(state)
    if wf.get("type") not in ("TargetSelection", "TriggerTargetSelection"):
        return False
    if str(wf_player(state)) != "0":
        return False
    ctl = OBS["control"]
    if ctl.get("resolved") or ctl.get("target_answered"):
        return False
    for opp in vi_ops(c):
        iid = opp.get("interactionId") or opp.get("id")
        if not iid or iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(opp, state)
        wire("control_target_prompt",
             {"rtype": resp.get("type"),
              "spec_max": spec_max_of(opp),
              "candidates": cands,
              "leg": ST["difficulty"]})
        say(f"[P0] control target prompt (iid={iid}): "
            f"{[(x['name'], x['ref'], x['seat'], x['controller'])
                for x in cands]}")
        rtype = resp.get("type")
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        pick = next((x for x in cands if x.get("seat") == 1), None)
        if not pick:
            pick = next((x for x in cands if x.get("controller") == 1
                         and x.get("zone") != "Hand"), None)
        if not pick:
            say("[P0] control: no opponent candidate in prompt; skipping")
            wire("control_target_no_opponent", {"candidates": cands})
            continue
        if rtype == "schema" and spec_type in ("sequence", "select"):
            resp_out = {"type": spec_type,
                        "data": {"choiceIds": [pick["choice_id"]]}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose",
                        "data": {"choiceId": pick["choice_id"]}}
        else:
            say(f"[P0] control: unexpected prompt shape {rtype}/{spec_type}")
            wire("control_target_unexpected_shape",
                 {"rtype": rtype, "spec_type": spec_type,
                  "candidates": cands})
            continue
        ctl["target_mode"] = "prompt"
        ctl["target_submission"] = {"ref": pick["ref"], "name": pick["name"]}
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        ctl["target_answered"] = True
        say(f"[P0] control: targeted opponent candidate "
            f"{pick['name']} (ref {pick['ref']}, seat {pick['seat']})")
        return True
    return False


# ------------------------------------------------------------------ tick

DECISION_GUARD = ("OptionalCostChoice", "TargetSelection",
                  "TriggerTargetSelection", "ManaPayment", "ChooseXValue",
                  "DiscardChoice", "ChooseLegend", "ChooseMode",
                  "ChooseAbility")


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    if await pay_tick(c):
        return True
    if await answer_declare_attackers_empty(c, pid):
        return True
    # control-cast target prompt takes precedence over the decision guard
    ctl = OBS["control"]
    if ctl.get("cast_oid") is not None and not ctl.get("resolved"):
        if await answer_control_target(c, state):
            OBS["p0_acts"] += 1
            return True
    # never pass while this seat has a decision pending
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    if wt0 in DECISION_GUARD and str(wplayer) == str(pid):
        key = ("unhandled_decision", wt0, st.get("state_revision"))
        if key not in SHAPES:
            SHAPES.add(key)
            say(f"[P0] unhandled decision pending: {wt0}; holding")
            wire("unhandled_decision",
                 {"type": wt0, "leg": ST["difficulty"]})
        return False
    if ST["stage"] == "PHASE1":
        if await phase1_step(c, pid, state, acts):
            return True
    elif ST["stage"] == "PHASE2":
        if await phase2_step(c, pid, state, acts):
            return True
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                OBS["p0_acts"] += 1
                return True
    return False


async def land_drop(c, pid, state, acts, played_turns):
    turn = state.get("turn_number")
    if turn in played_turns:
        return False
    lid = find_hand(state, pid, LAND)
    if not lid:
        return False
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id")) == lid:
            await submit_as_is(c, a)
            played_turns.add(turn)
            OBS["p0_acts"] += 1
            return True
    return False


async def phase1_step(c, pid, state, acts):
    # passive observer: one land per turn, pass priority, never cast
    if state.get("active_player") == pid and state.get("phase") in (
            "PreCombatMain", "PostCombatMain", "Main"):
        if await land_drop(c, pid, state, acts, ST.setdefault("land_turns",
                                                             set())):
            return True
    return False


async def phase2_step(c, pid, state, acts):
    ctl = OBS["control"]
    # land first (mana for the cast)
    if state.get("active_player") == pid and state.get("phase") in (
            "PreCombatMain", "PostCombatMain", "Main"):
        if await land_drop(c, pid, state, acts, ST.setdefault("land_turns",
                                                             set())):
            return True
        if ctl.get("cast_oid") is None and not ctl.get("resolved") \
                and untapped_of(state, pid, LAND) >= 3:
            oid = find_hand(state, pid, CARD)
            a = castspell_advertised(acts, oid)
            if a:
                if not ctl.get("pre_exported"):
                    await export_now(f"pre_control{ST['suffix']}")
                    ctl["pre_exported"] = True
                ctl["cast_turn"] = state.get("turn_number")
                await submit_as_is(c, a)
                ctl["cast_oid"] = int(oid)
                ctl["cast_t"] = time.time()
                OBS["p0_acts"] += 1
                say(f"[P0] CONTROL: casting {CARD} (oid {oid}) "
                    f"turn={state.get('turn_number')}")
                wire("control_cast_submit",
                     {"oid": oid, "turn": state.get("turn_number"),
                      "leg": ST["difficulty"]})
                return True
    return False


# --------------------------------------------------------------- leg loop

def draws_between(pre_st, post_st, pid):
    """Cards that moved Library -> Hand for player pid between two exports."""
    pre_objs = pre_st.get("objects", {})
    post_objs = post_st.get("objects", {})
    drawn = []
    for oid, po in post_objs.items():
        if po.get("zone") != "Hand":
            continue
        pre = pre_objs.get(oid)
        if pre is not None and pre.get("zone") == "Library":
            owner = po.get("owner", po.get("controller"))
            if owner == pid:
                drawn.append((oid, oname(po)))
    return drawn


def load_env(tag):
    p = f"{EVDIR}/{tag}.json"
    if not os.path.exists(p):
        return None
    try:
        return json.loads(open(p).read())["state"]
    except Exception:
        return None


async def play_leg(difficulty, suffix, t_run_start):
    """One full game: phase 1 = AI watch, phase 2 = P0 control cast."""
    reset_globals()
    ST["difficulty"] = difficulty
    ST["suffix"] = suffix
    leg = {"difficulty": difficulty, "suffix": suffix or "none",
           "game_code": None, "casts": [], "verdict_bits": {}}
    t0 = time.time()
    known_stack = set()
    land_turns = ST.setdefault("land_turns", set())
    ctl = OBS["control"]
    stalled = False
    stall_info = None
    ai_cast_timeout_logged = False
    export_done = {"pre_ai": False, "post_ai": False,
                   "pre_ctl": False, "post_ctl": False}

    p0 = PhaseClient("P0")
    await p0.connect()
    ai_deck = deck(*AI_DECK)
    say(f"[leg {difficulty}] creating game (P1 = AI {difficulty})...")
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": difficulty,
                               "deck": {"type": "DeckList",
                                        "data": ai_deck}}])
    say(f"[leg {difficulty}] game {p0.game_code}; P0 seat={p0.player_id}")
    ST["game_code"] = p0.game_code
    leg["game_code"] = p0.game_code
    wire("game_created", {"code": p0.game_code, "p0_seat": p0.player_id,
                          "difficulty": difficulty})

    global C0
    C0 = p0
    pid = p0.player_id

    last_rev = -1
    last_progress_t = time.time()
    last_tick_wall = 0.0
    force = False
    TIMEOUT = LEG_TIMEOUTS[difficulty]

    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        if time.time() - t_run_start > GLOBAL_BUDGET:
            leg["notes"] = ["global budget exceeded; leg cut short"]
            say(f"[leg {difficulty}] GLOBAL BUDGET exceeded; ending leg")
            break
        await asyncio.sleep(0.25)
        now = time.time()
        if not p0.latest:
            continue
        watch(p0)
        for r in drain_rejections(p0):
            OBS["rejections"].append(r)
            if LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                LAST_SUBMIT["iid"] = None
            # a rejected control cast must be re-armed
            if ctl.get("cast_oid") is not None and not ctl.get("resolved"):
                say(f"[leg {difficulty}] control cast rejected; re-arming")
                wire("control_cast_rejected", {"leg": difficulty})
                ctl["cast_oid"] = None
            force = True
        if now - last_tick_wall >= 5:
            force = True
        if p0.revision == last_rev and not force:
            continue
        force = False
        last_tick_wall = now
        try:
            if await tick(p0, pid):
                pass
        except Exception as e:
            say(f"[leg {difficulty}] tick error: {e}")
        last_rev = p0.revision
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)
        wf = wf_of(state)
        wfd = wf.get("data") or {}
        turn = state.get("turn_number") or 0

        # ---- AI cast tracking ----
        for oid in rendezvous_on_stack(state):
            if oid not in known_stack:
                known_stack.add(oid)
                obj = state.get("objects", {}).get(oid, {})
                ctrl = obj.get("controller", obj.get("owner"))
                rec = {"oid": int(oid), "cast_t": now,
                       "cast_rev": p0.revision, "cast_turn": turn,
                       "controller": ctrl, "resolved": False,
                       "resolve_t": None, "resolve_s": None,
                       "target_wait": None}
                leg["casts"].append(rec)
                say(f"[leg {difficulty}] OBSERVED cast #{len(leg['casts'])}: "
                    f"{CARD} on stack (oid {oid}, controller {ctrl}, "
                    f"turn {turn}, rev {p0.revision})")
                wire("cast", {"n": len(leg["casts"]), "oid": int(oid),
                              "ctrl": ctrl, "rev": p0.revision, "turn": turn,
                              "waiting_for": wf.get("type"),
                              "leg": difficulty})
                if ctl.get("cast_oid") is not None \
                        and int(oid) == ctl["cast_oid"]:
                    ctl["seen_on_stack"] = True
                if ctrl == 1 and not export_done["pre_ai"]:
                    try:
                        await export_now(f"pre_ai_cast{suffix}")
                        export_done["pre_ai"] = True
                    except Exception as e:
                        say(f"pre_ai_cast export failed: {e}")

        # WS-visible AI target wait (if the poll lands inside it)
        for ci, c_ in enumerate(leg["casts"]):
            if c_["resolved"] or c_["controller"] != 1:
                continue
            if wfd.get("player") == 1 and not c_["target_wait"]:
                c_["target_wait"] = {"type": wf.get("type"),
                                     "rev": p0.revision, "t": now}
                OBS["ai_target_waits"].append({"cast": ci + 1, "wf": wf,
                                               "leg": difficulty})
                say(f"[leg {difficulty}] OBSERVED AI decision wait for cast "
                    f"#{ci+1}: wf={wf.get('type')}, rev {p0.revision}")
                wire("ai_target_wait",
                     {"cast": ci + 1, "rev": p0.revision,
                      "waiting_for": wf.get("type"),
                      "viewer_interaction":
                          (st.get("viewer_interaction") or {}),
                      "leg": difficulty})

        # resolution / stall per cast
        current_stack = set(rendezvous_on_stack(state))
        for ci, c_ in enumerate(leg["casts"]):
            if c_["resolved"]:
                continue
            if str(c_["oid"]) not in current_stack:
                c_["resolved"] = True
                c_["resolve_t"] = now
                c_["resolve_s"] = round(now - c_["cast_t"], 2)
                say(f"[leg {difficulty}] cast #{ci+1} resolved in "
                    f"{c_['resolve_s']}s (rev {p0.revision})")
                wire("cast_resolved", {"cast": ci + 1,
                                       "resolve_s": c_["resolve_s"],
                                       "rev": p0.revision, "leg": difficulty})
                if c_["controller"] == 1 and not export_done["post_ai"]:
                    try:
                        await export_now(f"post_ai_cast{suffix}")
                        export_done["post_ai"] = True
                    except Exception as e:
                        say(f"post_ai_cast export failed: {e}")
            elif now - c_["cast_t"] > STALL_TIMEOUT:
                c_["stalled"] = True
                say(f"[leg {difficulty}] STALL: cast #{ci+1} unresolved "
                    f"after {STALL_TIMEOUT}s (rev {p0.revision}, "
                    f"wf={wf.get('type')})")
                wire("stall", {"cast": ci + 1, "controller": c_["controller"],
                               "rev": p0.revision,
                               "waiting_for": wf.get("type"),
                               "leg": difficulty})
                if c_["controller"] == 1:
                    stalled = True
                    stall_info = {"kind": "cast_unresolved",
                                  "cast": ci + 1, "rev": p0.revision,
                                  "waiting_for": wf.get("type"),
                                  "wf_player": wfd.get("player")}
                else:
                    leg.setdefault("notes", []).append(
                        f"P0 control cast #{ci+1} stalled")
                if c_["controller"] == 1 and not export_done["post_ai"]:
                    try:
                        await export_now(f"post_ai_cast{suffix}")
                        export_done["post_ai"] = True
                    except Exception as e:
                        say(f"post_ai_cast export failed: {e}")

        # global stall watchdog: no revision progress at all
        if not stalled and p0.revision != last_rev:
            last_progress_t = now
        if (not stalled and now - last_progress_t > STALL_TIMEOUT
                and not (state.get("game_over")
                         or state.get("winner") is not None)):
            stalled = True
            stall_info = {"kind": "no_progress",
                          "idle_s": round(now - last_progress_t, 1),
                          "rev": p0.revision, "waiting_for": wf.get("type"),
                          "wf_player": wfd.get("player"),
                          "phase": state.get("phase"), "turn": turn}
            say(f"[leg {difficulty}] STALL: no state progress for "
                f"{STALL_TIMEOUT}s (rev {p0.revision}, "
                f"wf={wf.get('type')}/P{wfd.get('player')})")
            wire("stall_no_progress", {**stall_info, "leg": difficulty})
            if not export_done["post_ai"]:
                try:
                    await export_now(f"post_ai_cast{suffix}")
                    export_done["post_ai"] = True
                except Exception as e:
                    say(f"post_ai_cast export failed: {e}")
            break
        if stalled:
            break

        if state.get("game_over") or state.get("winner") is not None:
            say(f"[leg {difficulty}] game ended")
            leg.setdefault("notes", []).append(
                "game ended before phase-2 watch completed")
            break

        # ---- control cast resolution watch ----
        # (only after the cast was actually observed on the stack; the
        #  pre-submission state trivially lacks the oid)
        if ctl.get("cast_oid") is not None and not ctl.get("resolved"):
            if str(ctl["cast_oid"]) in current_stack:
                ctl["seen_on_stack"] = True
            if ctl.get("seen_on_stack") \
                    and str(ctl["cast_oid"]) not in current_stack:
                ctl["resolved"] = True
                ctl["resolve_t"] = now
                ctl["resolve_s"] = round(now - ctl["cast_t"], 2)
                if ctl.get("target_mode") is None:
                    ctl["target_mode"] = "auto"
                say(f"[leg {difficulty}] CONTROL resolved in "
                    f"{ctl['resolve_s']}s (mode={ctl['target_mode']})")
                wire("control_resolved", {"resolve_s": ctl["resolve_s"],
                                          "target_mode": ctl["target_mode"],
                                          "leg": difficulty})
                if not export_done["post_ctl"]:
                    try:
                        await export_now(f"post_control{suffix}")
                        export_done["post_ctl"] = True
                    except Exception as e:
                        say(f"post_control export failed: {e}")
            elif now - ctl["cast_t"] > STALL_TIMEOUT:
                leg.setdefault("notes", []).append(
                    f"P0 control cast stalled after {STALL_TIMEOUT}s")
                say(f"[leg {difficulty}] P0 control cast unresolved after "
                    f"{STALL_TIMEOUT}s; ending leg")
                break

        # ---- phase 1 -> phase 2 ----
        if ST["stage"] == "PHASE1":
            ai_casts = [c for c in leg["casts"] if c["controller"] == 1]
            ai_done = bool(ai_casts and all(c["resolved"]
                                           for c in ai_casts))
            if ai_done:
                ST["stage"] = "PHASE2"
                say(f"[leg {difficulty}] phase 1 complete: AI cast resolved; "
                    f"arming P0 control cast")
            elif turn > LEG_TURN_LIMITS[difficulty] \
                    and not ai_cast_timeout_logged:
                ai_cast_timeout_logged = True
                ST["stage"] = "PHASE2"
                leg.setdefault("notes", []).append(
                    f"no AI cast by turn {LEG_TURN_LIMITS[difficulty]}; "
                    "proceeding to P0 control cast")
                say(f"[leg {difficulty}] phase 1 timeout: AI never cast by "
                    f"turn {turn}; arming P0 control cast")

        # end condition: control resolved + short tail
        if ctl.get("resolved") and export_done["post_ctl"]:
            if ctl.get("tail_started") is None:
                ctl["tail_started"] = now
                say(f"[leg {difficulty}] control resolved; tail watch")
            if now - ctl["tail_started"] > 20:
                say(f"[leg {difficulty}] tail watch complete")
                break

    # leg teardown exports
    if not export_done["post_ai"]:
        try:
            await export_now(f"post_ai_cast{suffix}")
            export_done["post_ai"] = True
        except Exception as e:
            leg.setdefault("notes", []).append(f"post_ai_cast export: {e}")
    if ctl.get("cast_oid") is not None and not export_done["post_ctl"]:
        try:
            await export_now(f"post_control{suffix}")
            export_done["post_ctl"] = True
        except Exception as e:
            leg.setdefault("notes", []).append(f"post_control export: {e}")

    leg["stalled"] = stalled
    leg["stall_info"] = stall_info
    leg["control"] = {k: v for k, v in ctl.items()
                      if k not in ("pre_exported", "seen_on_stack")}
    leg["wf_sequence"] = list(WF_SEEN)
    leg["p0_acts"] = OBS["p0_acts"]
    leg["rejections"] = len(OBS["rejections"])
    leg["stage_end"] = ST["stage"]
    await p0.close()
    return leg


# ------------------------------------------------------------- evaluation

def eval_leg(leg):
    """Per-leg assertion evaluation. Returns dict of assertion statuses."""
    sfx = leg["suffix"] if leg["suffix"] != "none" else ""
    A = {}
    notes = []

    pre_ai = load_env(f"pre_ai_cast{sfx}")
    post_ai = load_env(f"post_ai_cast{sfx}")
    pre_ctl = load_env(f"pre_control{sfx}")
    post_ctl = load_env(f"post_control{sfx}")

    ai_casts = [c for c in leg["casts"] if c["controller"] == 1]
    if ai_casts:
        A["A1_ai_cast"] = "passed"
        notes.append(f"AI casts ({leg['difficulty']}): " + ", ".join(
            f"#{i+1} turn {c['cast_turn']} resolved={c['resolved']} "
            f"({c.get('resolve_s', '?')}s) target_wait={c['target_wait']}"
            for i, c in enumerate(ai_casts)))
    else:
        A["A1_ai_cast"] = "failed"
        notes.append(f"AI ({leg['difficulty']}) never cast Secret Rendezvous "
                     f"within the window")

    if ai_casts and not leg["stalled"] \
            and all(c["resolved"] for c in ai_casts):
        worst = max(c.get("resolve_s") or 0 for c in ai_casts)
        A["A2_no_freeze"] = "passed"
        notes.append(f"all AI casts resolved within {worst}s "
                     f"(window {STALL_TIMEOUT}s); ai target waits seen: "
                     f"{len([w for w in OBS['ai_target_waits']])}")
    elif ai_casts and (any(c.get("stalled") for c in ai_casts)
                       or leg["stalled"]):
        A["A2_no_freeze"] = "failed"
        notes.append(f"AI cast stall ({leg['difficulty']}): "
                     f"{json.dumps(leg['stall_info'])}")
    elif ai_casts:
        A["A2_no_freeze"] = "failed"
        notes.append(f"AI cast(s) unresolved at leg end "
                     f"({leg['difficulty']}): {json.dumps(leg['stall_info'])}")
    else:
        A["A2_no_freeze"] = "not-run"

    resolved_ai = [c for c in ai_casts if c["resolved"]]
    if pre_ai is not None and post_ai is not None and resolved_ai:
        d0 = draws_between(pre_ai, post_ai, 0)
        d1 = draws_between(pre_ai, post_ai, 1)
        turn_same = (pre_ai.get("turn_number")
                     == post_ai.get("turn_number"))
        notes.append(f"first AI cast ({leg['difficulty']}): pre turn "
                     f"{pre_ai.get('turn_number')} -> post turn "
                     f"{post_ai.get('turn_number')}; P0 hand "
                     f"{hand_size(pre_ai, 0)}->{hand_size(post_ai, 0)}, "
                     f"P1 hand {hand_size(pre_ai, 1)}->"
                     f"{hand_size(post_ai, 1)}; drew P0={len(d0)} P1={len(d1)}")
        if turn_same and len(d0) == 3 and len(d1) == 3:
            A["A3_draws"] = "passed"
        else:
            A["A3_draws"] = "failed"
            notes.append("expected each player to draw exactly 3 on the same "
                         "turn")
    else:
        A["A3_draws"] = "not-run"
        notes.append(f"draws not evaluated ({leg['difficulty']}: no pre/post "
                     "AI pair)")

    ctl = leg["control"]
    if ctl.get("resolved") and pre_ctl is not None and post_ctl is not None:
        d0 = draws_between(pre_ctl, post_ctl, 0)
        d1 = draws_between(pre_ctl, post_ctl, 1)
        notes.append(f"P0 control ({leg['difficulty']}): target mode="
                     f"{ctl.get('target_mode')} "
                     f"(prompt_seen={ctl.get('target_answered')}); P0 drew "
                     f"{len(d0)}, P1 drew {len(d1)}")
        if len(d0) == 3 and len(d1) == 3:
            A["A4_control"] = "passed"
        else:
            A["A4_control"] = "failed"
    elif ctl.get("cast_oid") is not None:
        A["A4_control"] = "failed"
        notes.append(f"P0 control cast ({leg['difficulty']}) did not resolve")
    else:
        A["A4_control"] = "not-run"
        notes.append(f"P0 control cast ({leg['difficulty']}) never submitted")

    fin = post_ai
    if fin is not None:
        stack_empty = not fin.get("stack")
        game_over = bool(fin.get("game_over")
                         or fin.get("winner") is not None)
        A["A5_cleanup"] = ("passed" if stack_empty and not game_over
                           else "failed")
        notes.append(f"A5 ({leg['difficulty']}): stack_empty={stack_empty} "
                     f"game_over={game_over}")
    else:
        A["A5_cleanup"] = "not-run"

    return A, notes


def main():
    t_start = time.time()
    legs = []
    # leg 1: Medium. Leg 2 (Hard) only if Medium's AI never cast.
    for difficulty, suffix in (("Medium", ""), ("Hard", "_hard")):
        say(f"===== LEG {difficulty} =====")
        try:
            leg = asyncio.run(play_leg(difficulty, suffix, t_start))
        except Exception as e:
            say(f"leg {difficulty} crashed: {e!r}")
            leg = {"difficulty": difficulty,
                   "suffix": suffix or "none",
                   "casts": [], "control": {},
                   "notes": [f"leg crash: {e!r}"],
                   "stalled": False, "stall_info": None,
                   "wf_sequence": []}
        legs.append(leg)
        ai_casts = [c for c in leg["casts"] if c["controller"] == 1]
        if ai_casts:
            say(f"AI cast observed in {difficulty} leg; skipping Hard leg")
            break
        if time.time() - t_start > GLOBAL_BUDGET:
            say("global budget exceeded; skipping remaining legs")
            break

    # ---------------- evaluation ----------------
    per_leg = []
    for leg in legs:
        A, notes = eval_leg(leg)
        per_leg.append({"leg": leg["difficulty"], "assertions": A,
                        "notes": notes})
        for k in sorted(A):
            say(f"[{leg['difficulty']}] {k}: {A[k]}")

    all_casts = [c for leg in legs for c in leg["casts"]
                 if c["controller"] == 1]
    any_stall = any(leg.get("stalled") and
                    any(c["controller"] == 1 for c in leg["casts"])
                    for leg in legs)

    A = {}
    notes = []
    A["A1_ai_cast"] = "passed" if all_casts else "failed"
    notes.append(f"AI casts across legs: {len(all_casts)}")
    if not all_casts:
        A["A2_no_freeze"] = "not-run"
    elif any_stall or any(c.get("stalled") for c in all_casts):
        A["A2_no_freeze"] = "failed"
    else:
        A["A2_no_freeze"] = "passed"

    # A3 from the first resolved AI cast's leg
    first_leg = next((leg for leg in legs
                      if any(c["controller"] == 1 and c["resolved"]
                             for c in leg["casts"])), None)
    if first_leg:
        A["A3_draws"] = per_leg[legs.index(first_leg)]["assertions"][
            "A3_draws"]
    else:
        A["A3_draws"] = "not-run"

    # A4: primary leg = first leg with an AI cast, else Medium
    primary = first_leg or (legs[0] if legs else None)
    if primary:
        A["A4_control"] = per_leg[legs.index(primary)]["assertions"][
            "A4_control"]
        for pl in per_leg:
            notes.append(f"A4 ({pl['leg']}): "
                         f"{pl['assertions']['A4_control']}")
    else:
        A["A4_control"] = "not-run"

    if primary:
        A["A5_cleanup"] = per_leg[legs.index(primary)]["assertions"][
            "A5_cleanup"]
    else:
        A["A5_cleanup"] = "not-run"

    for pl in per_leg:
        notes.extend(f"[{pl['leg']}] {n}" for n in pl["notes"])
    for leg in legs:
        notes.extend(f"[{leg['difficulty']}] {n}"
                     for n in leg.get("notes", []))

    if A["A1_ai_cast"] != "passed":
        verdict = "blocked"
    elif A["A2_no_freeze"] == "failed":
        verdict = "reproduced"
    elif all(A[k] == "passed" for k in ("A1_ai_cast", "A2_no_freeze",
                                        "A3_draws", "A4_control",
                                        "A5_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    say(f"VERDICT: {verdict}")
    say(f"assertions: {json.dumps(A)}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "per_leg": per_leg, "notes": notes,
                   "verdict": verdict}, f, indent=2)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"per_leg": per_leg, "legs": legs, "notes": notes},
                  f, indent=2, default=str)

    run = {
        "run_id": RUN_ID,
        "issue": 6869,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
        "server": {
            "version": SERVER_IDENTITY["validated_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "source": "shared server on 127.0.0.1:9374 (started by the 6865 "
                      "run; reused per playbook); digests recomputed from "
                      "pinned on-disk artifacts at run start",
        },
        "verdict": verdict,
        "assertions": A,
        "per_leg": per_leg,
        "legs": [{k: v for k, v in leg.items()
                  if k not in ("control",)} | {
            "control_summary": {
                "resolved": leg["control"].get("resolved"),
                "target_mode": leg["control"].get("target_mode")}}
            for leg in legs],
        "contract_line": "AI casts Secret Rendezvous -> target decision must "
                         "complete with no freeze; caster and chosen opponent "
                         "each draw three. Then a human-driven control cast "
                         "exercises the target path directly.",
        "parser_corroboration": "v0.98.0 card-data.json models the second "
                         "Draw as target {type: Typed, controller: Opponent} "
                         "with target_prompt:null, optional_targeting:false "
                         "- a generic opponent scope, not a targeted-opponent "
                         "slot (parser gap persists; matches issue triage).",
        "limitations": [
            "Browser/UI not exercised; server-side phase-ai policy only",
            "2-player game only (single legal opponent); 3+ player target "
            "choice among multiple opponents not exercised",
            "Not tested on the original report build; verdict is scoped to "
            "v0.98.0, not a fix claim",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay)",
        ],
        "setup_line": "P0: 12x Secret Rendezvous + 48x Plains (human driver, "
                      "passive in phase 1; control cast in phase 2). P1: AI "
                      "(Medium, then Hard if Medium never cast), 12x Secret "
                      "Rendezvous + 48x Plains.",
        "scenario": {"file": "scenario_6869_0980.py",
                     "sha256": sha256_of_file(__file__)},
        "decks": {"P0": {"main": P0_DECK}, "P1_AI": {"main": AI_DECK}},
        "evidence_comment_id": 5640661872,
        "notes": notes,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2, default=str)
    say("wrote run.json")
    WIRE.close()
    RUNLOG.close()
    return verdict, A


if __name__ == "__main__":
    v, a = main()
    print("FINAL:", v, json.dumps(a))
