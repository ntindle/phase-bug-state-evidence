#!/usr/bin/env python3
"""Issue #6868 (revalidation on v0.98.0 / protocol 94): Jared Carthalion -3
doesn't add counters to his own Kavu.

Oracle (pinned v0.98.0 card-data): "[-3]: Choose up to two target creatures.
For each of them, put a number of +1/+1 counters on it equal to the number
of colors it is."
Pinned data: the -3 has multi_target {min: 0, max: 2} and its counter child
is effect=Unimplemented("unparsed_quantity",
"put a number of +1/+1 counters on it equal to the number of colors it is")
(sub_link SequentialSibling). Triage classifier: two-target selection is
supported but the color-counted counter child is explicitly Unimplemented
(unsupported_aspect).

The 2026-09-11 v0.80.0 run (20260911-6868b, protocol 69) found REPRODUCED:
P0 cast Jared (WUBRG), +1 -> loyalty 6 and a 3/3 all-colors Kavu token;
next P0 turn the -3 offered "up to two" targets as SEQUENTIAL per-target
prompts (schema spec max=1 per prompt) - Kavu answered first, then P1's
Grizzly Bears. Both accepted, loyalty 6->3, stack emptied - but ZERO
counters on either target (Kavu stayed 3/3, Bears stayed 2/2).

Plan (two human seats, native engine, v0.98.0 / protocol 94):
  SETUP  - land drops; P1 casts Grizzly Bears; P0 casts Jared Carthalion
           (gated on 5 distinct untapped colors).
  PLUS1  - P0 activates Jared +1 (ability_index 0) -> loyalty 6, a 3/3
           all-colors Kavu token is created.
  MINUS3 - next P0 turn: export pre_activate.json, activate -3
           (ability_index 1), answer the up-to-two TargetSelection with
           Kavu (5 colors) + P1 Bears (1 color), one target per prompt
           (both at once if a single prompt advertises max>=2). Wait for
           resolution; export post_activate.json + post.json.

Behavioral contract:
  A1 setup_ok       pre_activate.json: Jared BF, Kavu token BF (all 5
                    colors), P1 Bears BF
  A2 minus3_offered wire evidence: ActivateAbility ability_index 1
                    advertised on Jared
  A3 target_resolve both targets answered, no rejections; Jared loyalty
                    6 -> 3, stack empty post-resolution
  A4 kavu_counters  post: Kavu carries 5 +1/+1 counters (3/3 -> 8/8)
  A5 bears_counters  post: P1 Bears carries 1 +1/+1 counter (2/2 -> 3/3)
  A6 cleanup         game continues; no stuck prompt

Verdict = reproduced iff A1..A3 pass and (A4 or A5 fail: the reported gap);
not-reproduced iff A4 and A5 pass. Never "fixed".

Protocol-94 driver (v0.98.0, per scenario_6866_0980): HELLO advertises 94
(exact match enforced); MulliganDecision {"choice":{"type":"Keep"}} gated on
the seat's pending Declare; BottomCards/DiscardToHandSize via single
SelectCards {"cards":[...]}; CastSpell/ActivateAbility via advertised
actions; PayMana* via pay_tick; DeclareAttackers/Blockers (empty) via
relations-schema interaction; TargetSelection via viewer_interaction
schema/exactChoices; interaction-rejection guard + stale-client watchdog;
PassPriority only when the seat genuinely holds Priority.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-6868"
EVID_ISSUE = "6868"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
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

for _f, _k in (
        ("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.98.0/data/draft-pools.json",
         "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

# card-data excerpt for the -3 parse (static; written to evidence)
_cd = json.load(open(f"{BACKFILL}/server/releases/v0.98.0/data/card-data.json"))
_j = _cd["jared carthalion"]
with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
    json.dump({"name": _j["name"],
               "oracle_text": _j["oracle_text"],
               "minus3_ability": _j["abilities"][1]}, f, indent=2)
say("wrote carddata_excerpt.json (minus3 parse: Unimplemented counter child)")

JARED = "Jared Carthalion"
KAVU = "Kavu"
BEAR = "Grizzly Bears"
PLAINS, ISLAND, SWAMP, MOUNTAIN, FOREST = (
    "Plains", "Island", "Swamp", "Mountain", "Forest")
LANDS = (PLAINS, ISLAND, SWAMP, MOUNTAIN, FOREST)

P0_DECK = [(JARED, 8), (PLAINS, 10), (ISLAND, 10), (SWAMP, 10),
           (MOUNTAIN, 10), (FOREST, 10)]
P1_DECK = [(BEAR, 8), (FOREST, 12), (PLAINS, 10), (ISLAND, 8),
           (SWAMP, 8), (MOUNTAIN, 8)]

ST = {}
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
ACT = {}
TGT = {}
PLUS1_TURN = {}
OBS = {}
C0 = None
_WATCH = {}
_DISCARD_REV = {}


def reset_globals():
    global C0
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False,
               "game_code": None})
    MULLS.clear()
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    ACT.clear()
    TGT.clear()
    TGT.update({"minus3": None})
    PLUS1_TURN.clear()
    PLUS1_TURN.update({"turn": None})
    OBS.clear()
    _DISCARD_REV.clear()
    _WATCH.clear()
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


def distinct_untapped_colors(state, pid):
    return {name for name in LANDS if untapped_of(state, pid, name) >= 1}


def loyalty_of(o):
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    return None


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


def jared_oids(state):
    return [str(oid) for oid, o in bf(state, 0) if oname(o) == JARED]


def kavu_oid(state):
    for oid, o in state["objects"].items():
        if (oname(o) == KAVU and o.get("zone") == "Battlefield"
                and o.get("controller") == 0):
            return str(oid)
    return None


def p1_bear_oid(state):
    for oid, o in state["objects"].items():
        if (oname(o) == BEAR and o.get("zone") == "Battlefield"
                and o.get("controller") == 1):
            return str(oid)
    return None


def colors_of(o):
    c = o.get("colors") or o.get("color") or []
    if isinstance(c, str):
        return [c]
    if isinstance(c, dict):
        return [k for k, v in c.items() if v]
    return list(c)


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def activate_options(acts, state, want_index):
    """Jared's ActivateAbility options (by ability_index)."""
    srcs = set(jared_oids(state))
    out = []
    for a in acts:
        if a["type"] != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", d.get("object_id", "")))
        if src in srcs:
            out.append((d.get("ability_index"), a))
    if want_index is not None:
        return [a for i, a in out if i == want_index]
    return [a for _, a in out]


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


# ------------------------------------------------------- protocol-94 ticks

async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"]})
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
            wire("rejected", {"who": c.name, "type": t, "data": data})
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


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


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
    if MULLS.get((tag, "kept")):
        return False
    mull_count = MULLS.get((tag, "mulls"), 0)
    choice = "Keep" if (keep_fn(state) or mull_count >= 2) else "Mulligan"
    say(f"[{tag}] mulligan -> {choice} (prior mulligans: {mull_count})")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    if choice == "Mulligan":
        MULLS[(tag, "mulls")] = mull_count + 1
    else:
        MULLS[(tag, "kept")] = True
    wire("mulligan", {"who": tag, "decision": choice})
    return True


def rank_p0(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == JARED:
            return 3        # never bottom/discard Jared
        if nm in LANDS:
            return 1        # lands are expendable here
        return 0
    return rank


def rank_p1(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == BEAR:
            return 3
        if nm in LANDS:
            return 1
        return 0
    return rank


async def do_bottom(c, pid, tag, rank_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    if (tag, "bottomed") in MULLS:
        return False
    n = (pend.get("phase") or {}).get("count") or 1
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=rank_fn(state))[:n]]
    say(f"[{tag}] bottoming {n}: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


async def do_discard(c, pid, tag, rank_fn):
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
    say(f"[{tag}] discarding to hand size: "
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


async def answer_declare_attackers_empty(c, pid, tag):
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
    wire("declare_attackers_empty", {"who": tag})
    say(f"[{tag}] declares empty attackers")
    await send_interaction(c, sub)
    SUBMITTED.add(iid)
    return True

def spec_max_of(opp):
    """Best-effort max targets advertised by a schema target prompt."""
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


async def answer_minus3_prompt(c, state, st):
    """Answer P0's -3 TargetSelection: Kavu (5 colors) then P1 Bears.
    Handles both a single prompt advertising max>=2 (submit both choiceIds
    at once) and sequential per-target prompts (max=1: one choiceId per
    prompt, Kavu first). Only engine-advertised choiceIds are used."""
    if TGT.get("minus3"):
        return False
    if ST["stage"] != "MINUS3" or not ACT.get("minus3"):
        return False
    wf = wf_of(state)
    if wf.get("type") not in ("TargetSelection", "TriggerTargetSelection"):
        return False
    if str(wf_player(state)) != "0":
        return False
    want_refs = [r for r in (kavu_oid(state), p1_bear_oid(state)) if r]
    if len(want_refs) < 2:
        return False
    answered = OBS.setdefault("minus3_answered", [])
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
        if not any(x["zone"] == "Battlefield" for x in cands):
            continue
        present = {x["ref"] for x in cands}
        remaining = [r for r in want_refs
                     if r in present and r not in answered]
        if not remaining:
            continue
        if ("minus3_prompt", iid) not in SHAPES:
            SHAPES.add(("minus3_prompt", iid))
            wire("minus3_target_prompt",
                 {"rtype": resp.get("type"),
                  "spec_max": spec_max_of(opp),
                  "candidates": cands, "opportunity": opp})
            say(f"[P0] -3 target prompt (iid={iid}): "
                f"{[(x['name'], x['ref'], x['controller']) for x in cands]}")
        rtype = resp.get("type")
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        smax = spec_max_of(opp)
        if rtype == "schema" and spec_type in ("sequence", "select"):
            if smax is not None and smax >= 2 and len(remaining) >= 2:
                ids = [next(x["choice_id"] for x in cands if x["ref"] == r)
                       for r in remaining[:2]]
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": ids}}
                chosen = remaining[:2]
            else:
                want = next(x for x in cands if x["ref"] == remaining[0])
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": [want["choice_id"]]}}
                chosen = [remaining[0]]
        elif rtype == "exactChoices":
            want = next(x for x in cands if x["ref"] == remaining[0])
            resp_out = {"type": "choose",
                        "data": {"choiceId": want["choice_id"]}}
            chosen = [remaining[0]]
        else:
            say(f"[P0] -3: unexpected prompt shape {rtype}/{spec_type}")
            wire("minus3_unexpected_shape",
                 {"rtype": rtype, "spec_type": spec_type,
                  "candidates": cands})
            continue
        for r in chosen:
            nm = next(x["name"] for x in cands if x["ref"] == r)
            OBS.setdefault("target_submissions", []).append(
                {"ref": r, "name": nm})
        await send_interaction(c, {"interactionId": iid,
                                   "response": resp_out})
        SUBMITTED.add(iid)
        answered.extend(chosen)
        say(f"[P0] -3: targeted "
            f"{[next(x['name'] for x in cands if x['ref'] == r) for r in chosen]} "
            f"[{len(answered)}/2]")
        if len(answered) >= 2:
            TGT["minus3"] = {"refs": want_refs, "at": time.time()}
            say("[P0] -3: both targets answered")
        return True
    return False


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    tag = "P0" if is_p0 else "P1"
    keep_fn = (lambda s: sum(1 for o in hand_oids(s, pid)
                             if oname(s["objects"][o]) in LANDS) >= 2
               and any(oname(s["objects"][o]) == JARED
                       for o in hand_oids(s, pid))) \
        if is_p0 else \
        (lambda s: sum(1 for o in hand_oids(s, pid)
                       if oname(s["objects"][o]) in LANDS) >= 2
         and any(oname(s["objects"][o]) == BEAR
                 for o in hand_oids(s, pid)))
    if await do_mulligan(c, pid, tag, keep_fn):
        return True
    if await do_bottom(c, pid, tag, rank_p0 if is_p0 else rank_p1):
        return True
    if await do_discard(c, pid, tag, rank_p0 if is_p0 else rank_p1):
        return True
    if await pay_tick(c):
        return True
    if await answer_declare_attackers_empty(c, pid, tag):
        return True
    if is_p0:
        if await answer_minus3_prompt(c, state, st):
            return True
    # never pass while this seat has a decision pending
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    if wt0 in ("OptionalCostChoice", "TargetSelection",
               "TriggerTargetSelection", "ManaPayment", "ChooseXValue",
               "DiscardChoice", "ChooseLegend") \
            and str(wplayer) == str(pid):
        return False
    if is_p0 and state.get("active_player") == 0 \
            and state.get("phase") in ("PreCombatMain", "PostCombatMain",
                                       "Main"):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if ST["stage"] == "SETUP":
            if await p0_setup_cast(c, pid, state, acts):
                return True
        elif ST["stage"] == "PLUS1":
            if await p0_plus1(c, pid, state, acts):
                return True
        elif ST["stage"] == "MINUS3":
            if await p0_minus3(c, pid, state, acts):
                return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False


async def p0_land_drop(c, pid, state, acts):
    have = {oname(o) for _, o in bf(state, pid)}
    order = [l for l in LANDS if l not in have] + [l for l in LANDS
                                                  if l in have]
    for name in order:
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def p0_setup_cast(c, pid, state, acts):
    # Jared (needs W U B R G); gate on none-on-BF (legend)
    if not jared_oids(state):
        oid = find_hand(state, pid, JARED)
        a = castspell_advertised(acts, oid)
        if a and len(distinct_untapped_colors(state, pid)) >= 5:
            await submit_as_is(c, a)
            say("P0 casts Jared Carthalion")
            return True
    return False


async def p0_plus1(c, pid, state, acts):
    if ACT.get("plus1"):
        return False
    opts = activate_options(acts, state, 0)
    if ("plus1_opts", len(opts)) not in SHAPES:
        SHAPES.add(("plus1_opts", len(opts)))
        wire("plus1_options", {"count": len(opts),
                              "all_action_types":
                              sorted({a["type"] for a in acts})})
        say(f"[P0] Jared +1 options: {len(opts)}")
    if not opts:
        return False
    ACT["plus1"] = {"at": time.time(), "turn": state.get("turn_number")}
    PLUS1_TURN["turn"] = state.get("turn_number")
    await submit_as_is(c, opts[0])
    say(f"[P0] activates Jared +1 (ability_index 0) "
        f"turn={state.get('turn_number')}")
    wire("jared_plus1", {"turn": state.get("turn_number")})
    return True


async def p0_minus3(c, pid, state, acts):
    if ACT.get("minus3") or TGT.get("minus3"):
        return False
    # one loyalty activation per permanent per turn: wait for a fresh turn
    if PLUS1_TURN.get("turn") is not None and \
            state.get("turn_number") == PLUS1_TURN["turn"]:
        return False
    if not kavu_oid(state) or not p1_bear_oid(state):
        return False
    opts = activate_options(acts, state, 1)
    if ("minus3_opts", len(opts)) not in SHAPES:
        SHAPES.add(("minus3_opts", len(opts)))
        wire("minus3_options", {"count": len(opts),
                               "all_action_types":
                               sorted({a["type"] for a in acts})})
        say(f"[P0] Jared -3 options: {len(opts)}")
    if not opts:
        return False
    OBS["minus3_offered"] = True
    await export_now("pre_activate.json")
    ACT["minus3"] = {"at": time.time(), "turn": state.get("turn_number")}
    await submit_as_is(c, opts[0])
    say(f"[P0] activates Jared -3 (ability_index 1) "
        f"turn={state.get('turn_number')}")
    wire("jared_minus3", {"turn": state.get("turn_number")})
    return True


async def p1_step(c, pid, state, acts):
    # land drop preferring Forest (Bears), then cast Bears
    if state.get("active_player") == pid and state.get("phase") in (
            "PreCombatMain", "PostCombatMain", "Main"):
        order = [FOREST] + [l for l in LANDS if l != FOREST]
        for name in order:
            lid = find_hand(state, pid, name)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True
                break
        if not p1_bear_oid(state):
            oid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, oid)
            if a and untapped_of(state, pid, FOREST) >= 1 \
                    and sum(1 for _, o in bf(state, pid)
                            if not o.get("tapped")) >= 2:
                await submit_as_is(c, a)
                say("P1 casts Grizzly Bears")
                return True
    return False

# ------------------------------------------------------- main loop

async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    ST["game_code"] = p0.game_code
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 1800
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            if not c.latest:
                continue
            watch(c)
            rej = drain_rejections(c)
            if rej:
                if LAST_SUBMIT["iid"] in SUBMITTED:
                    SUBMITTED.discard(LAST_SUBMIT["iid"])
                    LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        if wf_of(state).get("type") == "GameOver" and not ST["stop"]:
            ST["retry"] = True
            ST["stop"] = True
            obs["notes"].append("game over before sequence completed; retry")
            say("game over -> retrying with new game")

        # --- stage transitions
        if ST["stage"] == "SETUP" and jared_oids(state):
            ST["stage"] = "PLUS1"
            say("=== stage -> PLUS1 ===")
        if ST["stage"] == "PLUS1" and ACT.get("plus1") and kavu_oid(state):
            ST["stage"] = "MINUS3"
            say(f"=== Kavu token on BF; stage -> MINUS3 "
                f"(turn {state.get('turn_number')}) ===")
            wire("kavu_created",
                 {"oid": kavu_oid(state),
                  "obj": state["objects"].get(kavu_oid(state), {})})

        # --- resolution watch after -3 targets answered
        if TGT.get("minus3") and not ST["stop"]:
            zids = jared_oids(state)
            loy = loyalty_of(state["objects"][zids[0]]) if zids else None
            stack_empty = not state.get("stack")
            if loy == 3 and stack_empty:
                await asyncio.sleep(1.5)
                st2 = p0.latest
                state2 = st2["state"] if st2 else state
                if not state2.get("stack"):
                    ko = state2["objects"].get(kavu_oid(state2) or "", {})
                    bo = state2["objects"].get(
                        p1_bear_oid(state2) or "", {})
                    OBS["kavu_post"] = ko
                    OBS["bear_post"] = bo
                    wire("post_resolution_objects",
                         {"kavu": ko, "bear": bo})
                    await export_now("post_activate.json")
                    try:
                        await export_now("post.json")
                    except Exception as e:
                        say(f"final post export failed: {e}")
                    ST["stop"] = True
                    say("=== DONE: -3 resolved ===")
            elif time.time() - TGT["minus3"]["at"] > 180:
                say("targets answered but no resolution after 180s; "
                    "exporting post anyway")
                wire("resolution_timeout", {})
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # --- stuck watch: activation submitted but prompt never answered
        if ST["stage"] == "MINUS3" and ACT.get("minus3") \
                and not TGT.get("minus3") \
                and time.time() - ACT["minus3"]["at"] > 180 \
                and not ST["stop"]:
            say("target prompt never answered after 180s; exporting post")
            wire("target_timeout", {})
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True

    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    try:
        await export_now("post.json")
    except Exception as e:
        obs["notes"].append(f"post.json export failed: {e!r}")

    await p0.close()
    await p1.close()
    return obs, True


# ------------------------------------------------------- evaluation

def _norm(s):
    return "".join(ch for ch in str(s).upper() if ch.isalnum())


def _match_p1p1_entry(e):
    if isinstance(e, dict):
        t = _norm(e.get("type", "")) + _norm(e.get("kind", "")) + \
            _norm(e.get("name", ""))
        if "P1P1" in t:
            try:
                return int(e.get("count", e.get("amount", 1)))
            except Exception:
                return 1
        return 0
    if isinstance(e, str) and "P1P1" in _norm(e):
        return 1
    return 0


def plus1p1_counters(o):
    """Best-effort +1/+1 counter count; prefers an explicit scalar field,
    else scans counter lists. Returns (count, detail)."""
    v = o.get("p1p1_counters")
    if isinstance(v, (int, float)):
        return int(v), [("p1p1_counters", int(v))]
    out = []
    for key in ("counters", "counter_list"):
        v = o.get(key)
        if isinstance(v, list):
            for e in v:
                n = _match_p1p1_entry(e)
                if n:
                    out.append((key, n))
    return sum(n for _, n in out), out


def load_state_env(path):
    with open(f"{EVDIR}/{path}") as f:
        return json.load(f)["state"]


def evaluate():
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    try:
        pre = load_state_env("pre_activate.json")
        ko = pre["objects"].get(kavu_oid(pre) or "", {})
        ncolors = len(set(colors_of(ko)))
        ok = bool(jared_oids(pre)) and bool(kavu_oid(pre)) \
            and ncolors == 5 and bool(p1_bear_oid(pre))
        A["A1_setup_ok"] = "passed" if ok else "failed"
        obs["notes"].append(
            f"A1: jared={bool(jared_oids(pre))} kavu={kavu_oid(pre)} "
            f"colors={colors_of(ko)} p1bear={p1_bear_oid(pre)} "
            f"kavu_keys={sorted(ko.keys())}")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"A1 eval error: {e!r}")

    A["A2_minus3_offered"] = ("passed"
                              if OBS.get("minus3_offered") else "failed")
    obs["notes"].append(f"A2: minus3 offered={OBS.get('minus3_offered')}")

    try:
        tgt_ok = TGT.get("minus3") is not None
        rejs = []
        for line in open(f"{EVDIR}/wire_log.jsonl"):
            d = json.loads(line)
            if d["event"] == "rejected":
                rejs.append(d)
        post = load_state_env("post_activate.json")
        zids = jared_oids(post)
        loy = loyalty_of(post["objects"][zids[0]]) if zids else None
        stack_empty = not post.get("stack")
        ok = tgt_ok and not rejs and loy == 3 and stack_empty
        A["A3_target_resolve"] = "passed" if ok else "failed"
        obs["notes"].append(
            f"A3: answered={tgt_ok} submissions={OBS.get('target_submissions')} "
            f"rejections={len(rejs)} loyalty={loy} stack_empty={stack_empty}")
    except Exception as e:
        A["A3_target_resolve"] = "not-run"
        obs["notes"].append(f"A3 eval error: {e!r}")

    def eval_counters(key, oid_fn, expect, base_pt):
        try:
            post = load_state_env("post_activate.json")
            o = post["objects"].get(oid_fn(post) or "", {})
            n, detail = plus1p1_counters(o)
            pt = (o.get("power"), o.get("toughness"))
            obs["notes"].append(
                f"{key}: counters={n} detail={detail} power/toughness={pt} "
                f"(expected {expect}; base {base_pt}) "
                f"counter_fields={ {k: o.get(k) for k in ('counters', 'counter_list', 'p1p1_counters') if k in o} }")
            if n == expect:
                A[key] = "passed"
            elif n == 0 and pt == base_pt:
                A[key] = "failed"
            elif n == 0:
                A[key] = "not-run"
                obs["notes"].append(
                    f"{key}: AMBIGUOUS - no counter entries but P/T changed "
                    f"to {pt}; manual review required")
            else:
                A[key] = "failed"
                obs["notes"].append(f"{key}: unexpected count {n}")
        except Exception as e:
            A[key] = "not-run"
            obs["notes"].append(f"{key} eval error: {e!r}")

    eval_counters("A4_kavu_counters", kavu_oid, 5, (3, 3))
    eval_counters("A5_bears_counters", p1_bear_oid, 1, (2, 2))

    try:
        final = load_state_env("post.json")
        stack_empty = not final.get("stack")
        game_over = wf_of(final).get("type") == "GameOver"
        A["A6_cleanup"] = ("passed" if stack_empty and not game_over
                           else "failed")
        obs["notes"].append(f"A6: stack_empty={stack_empty} "
                            f"game_over={game_over}")
    except Exception as e:
        A["A6_cleanup"] = "not-run"
        obs["notes"].append(f"A6 eval error: {e!r}")

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"targets: {TGT}")
    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "targets": {k: v for k, v in TGT.items()}}, f, indent=2)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"notes": obs["notes"], "observations": OBS,
                   "wf_sequence": WF_SEEN,
                   "targets": {k: v for k, v in TGT.items()}}, f, indent=2,
                  default=str)

    if A.get("A1_setup_ok") == "passed" \
            and A.get("A2_minus3_offered") == "passed" \
            and A.get("A3_target_resolve") == "passed" \
            and (A.get("A4_kavu_counters") == "failed"
                 or A.get("A5_bears_counters") == "failed"):
        verdict = "reproduced"
    elif A.get("A4_kavu_counters") == "passed" \
            and A.get("A5_bears_counters") == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    say(f"verdict: {verdict}")
    return obs


async def main():
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = ({"assert": {},
                          "notes": [f"attempt {n} crash: {e!r}"]}), False
        if done:
            break
        say(f"attempt {n} did not complete; starting a new game")
    else:
        obs["notes"].append("all attempts exhausted without completing")

    eval_obs = evaluate()
    obs["assert"] = eval_obs["assert"]
    obs["notes"] += eval_obs["notes"]
    obs["verdict"] = eval_obs["verdict"]

    run = {
        "issue": 6868,
        "run_id": RUN_ID,
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": {
            "version": SERVER_IDENTITY["validated_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
        },
        "game_code": ST.get("game_code"),
        "verdict": obs["verdict"],
        "assertions": obs["assert"],
        "evidence_comment_id": 5640479052,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2)
    say("wrote run.json")
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps({**obs.get("assert", {}),
                      "verdict": obs.get("verdict")}, indent=2))
