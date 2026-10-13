#!/usr/bin/env python3
"""Issue #9847: Teferi, Temporal Pilgrim -12: "Target opponent chooses a
permanent they control" is read as the controller's permanent.

Oracle: "-12: Target opponent chooses a permanent they control and returns
it to its owner's hand. Then they shuffle each nonland permanent they
control into its owner's library."

The reporter's data read (confirmed against the pinned v0.108.0 card-data
this run): the -12 lowers as
  TargetOnly Typed{Permanent, controller: "You"}
  -> Bounce ParentTarget
  -> ChangeZone Library of Typed{Permanent, NonLand, controller: "You"}
  -> Shuffle ParentTargetOwner
i.e. the declared opponent is dropped entirely and both the chosen
permanent and the shuffled group bind to the ACTIVATOR's permanents.
Same parser family as #9600 ("they control" after a declared player).

Behavioral contract (per playbook: test the reported OUTCOME):
  P0 ramps to 5 mana, casts Teferi, Temporal Pilgrim (loyalty 4), then each
  turn activates [0] (draw -> the "whenever you draw" trigger adds a loyalty
  counter; net +2 loyalty per P0 turn: draw step + [0]) until loyalty >= 12,
  then activates -12 at sorcery speed.
  P1 builds a board of Aegis Turtles as the contrast group.
  Expected (correct): P1 chooses one of its permanents -> bounced; then each
      P1 nonland permanent is shuffled into its owner's library. P0's board
      (turtles + Teferi) is untouched.
  Bug (per the issue): the activator's own permanents are bounced and
      shuffled away; P1 is never asked; P1's board is untouched.

Assertions:
  A1 setup_ok        game created, both seats joined
  A2 fixture_ok      Teferi cast and seen on P0 battlefield; -12 activated;
                     pre-ult export captured; P0 had >=2 turtles pre-ult;
                     P1 had >=1 turtle pre-ult
  A3 pre_ult_export  pre_ult.json captured and parses
  A4 bug_outcome     post-resolution: P0 turtles == 0 (bounced+shuffled away)
                     AND P1 turtles == pre-ult count (untouched)
  A5 correct_outcome post-resolution: P1 turtles == 0 AND P0 turtles ==
                     pre-ult count (Teferi excluded from turtle counts)
  A6 opp_never_asked no real decision was ever offered to P1 during the ult
                     window (priority menus don't count)
  A7 data_shape      the -12 card-data lower matches the reported wrong shape

Verdict:
  reproduced      A2 passed and A4 passed
  not-reproduced  A2 passed and A5 passed
  blocked         fixture or ult never reached

Driver conventions (verified scenario_9846.py, protocol 134):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (viewer_interaction)
  - exact card-data names; export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; vi candidate references are strings
    (normalize with str() before oid comparisons)
  - real_decision_pending filters None surface codes (9845 lesson)
  - genuine card-choice candidates have NO action surface (9842 lesson)
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario9847")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9847-20261013-054205")
ISSUE = 9847
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.108.0",
    "build_commit": "b8fa1d3",
    "protocol_version": 134,
    "mode": "Full",
    "server_binary_sha256": "83a3d30919ea7d56b98054048566d105116523abb2b88e5e4478c40ecb00c1f7",
    "card_data_sha256": "2f65a5829b509ad69a32110ff9a1361363e7f97f24c41c6a53d4ae6850e8e813",
    "draft_pools_sha256": "8c9945e3c7d511ebb1ca9ff37bee5b1f58144c1c4b5d6bed52f81d6d050c7297",
    "signature_verified": True,
    "signature_note": ("v0.108.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.108.0 (published "
               "2026-10-12T21:10:34Z) == pinned release dir; ServerHello "
               "0.108.0/b8fa1d3/protocol 134 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.108.0 server on 127.0.0.1:9374 already running from the "
               "05:13 run; scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9847/" + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


for _f, _k in (("server/releases/v0.108.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.108.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.108.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against the ledger-pinned v0.108.0 digests",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.108.0/data/card-data.json"))


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


say("server identity hashes recomputed against on-disk pinned artifacts")

ST = {"mana_needs": {}}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}


# ---------------------------------------------------------------- state helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_names(state, pid):
    return [obj_lname(state, o) for o in bf_oids(state, pid)]


def is_land(o):
    # NOTE: runtime state objects use "card_types" (plural); card-data.json
    # uses "card_type" (singular). This helper reads runtime state.
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def turtle_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == "aegis turtle"]


def teferi_oid(state, pid):
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == "teferi, temporal pilgrim":
            return o
    return None


def loyalty_of(state, oid):
    """Best-effort loyalty counter read; None when the shape is unknown."""
    o = get_obj(state, oid)
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    ctrs = o.get("counters")
    if isinstance(ctrs, dict):
        for k, v in ctrs.items():
            if "loyal" in str(k).lower():
                if isinstance(v, (int, float)):
                    return int(v)
                if isinstance(v, dict) and isinstance(v.get("count"), (int, float)):
                    return int(v["count"])
    for k, v in o.items():
        if "loyal" in str(k).lower() and isinstance(v, (int, float)):
            return int(v)
    return None


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def stack_raw(state):
    return [{"name": obj_lname(state, e.get("object_id", e.get("id", ""))),
             "kind": (e.get("kind") or {}).get("type") if isinstance(e.get("kind"), dict) else e.get("kind"),
             "controller": e.get("controller")}
            for e in state.get("stack") or []]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    """Verbatim from the verified scenario_301_01080.py (None codes filtered)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def decision_pending(st):
    return real_decision_pending(st)


# ---------------------------------------------------------------- interaction primitives

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


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
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
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.108.0"), f"unexpected version {ver}"
    assert int(proto) == 134, f"unexpected protocol {proto}"
    assert str(build) == "b8fa1d3", f"unexpected build {build}"


def check_data_level():
    """Record the parser shapes this scenario depends on: Teferi's -12 must
    be the reported-wrong lower (TargetOnly Typed{Permanent, controller You}
    -> Bounce ParentTarget -> ChangeZone Library of
    Typed{Permanent, NonLand, controller You})."""
    t = CARD_DATA.get("teferi, temporal pilgrim", {})
    abs_ = t.get("abilities") or []
    ult = abs_[2] if len(abs_) > 2 else {}
    notes = []
    ok = True
    eff = ult.get("effect") or {}
    tgt = eff.get("target") or {}
    if not (eff.get("type") == "TargetOnly" and tgt.get("type") == "Typed"
            and "Permanent" in (tgt.get("type_filters") or [])
            and tgt.get("controller") == "You"):
        ok = False
        notes.append(f"-12 top effect is not TargetOnly Permanent{{controller You}}: "
                     f"{json.dumps(eff)[:300]}")
    sub1 = (ult.get("sub_ability") or {})
    e1 = (sub1.get("effect") or {})
    if not (e1.get("type") == "Bounce"
            and (e1.get("target") or {}).get("type") == "ParentTarget"):
        ok = False
        notes.append(f"-12 first sub-effect is not Bounce ParentTarget: "
                     f"{json.dumps(e1)[:200]}")
    sub2 = (sub1.get("sub_ability") or {})
    e2 = (sub2.get("effect") or {})
    t2 = e2.get("target") or {}
    tf = t2.get("type_filters") or []
    has_nonland = any(isinstance(x, dict) and x.get("Non") == "Land" for x in tf)
    if not (e2.get("type") == "ChangeZone" and e2.get("destination") == "Library"
            and t2.get("type") == "Typed" and "Permanent" in tf
            and has_nonland and t2.get("controller") == "You"):
        ok = False
        notes.append(f"-12 ChangeZone is not Library of Permanent^NonLand"
                     f"{{controller You}}: {json.dumps(e2)[:300]}")
    cost = ult.get("cost") or {}
    if not (cost.get("type") == "Loyalty" and cost.get("amount") == -12):
        ok = False
        notes.append(f"-12 cost is not Loyalty -12: {json.dumps(cost)[:120]}")
    if str(t.get("loyalty")) != "4":
        ok = False
        notes.append(f"starting loyalty is {t.get('loyalty')!r}, not 4")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle_text": t.get("oracle_text"),
                   "minus12_ability": ult}, f, indent=1, default=str)
    say(f"data-level check: ok={ok} notes={notes}")
    wire("data_level", {"ok": ok, "notes": notes})
    assert ok, f"data-level check failed: {notes}"


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = hand_names(state, pid)
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_ref(ch):
    """Card oid reference for a candidate: top-level or nested in a
    surface's data (protocol-134 discard/select prompts nest it)."""
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    if len(hand_ids(state, pid)) <= 7:
        return False
    handset = set(hand_ids(state, pid))
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if _cand_ref(ch) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "handsize", iid)
        if key in SUBMITTED_OPPS:
            return False
        SUBMITTED_OPPS.add(key)
        picks = cands[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:40] for x in picks]}")
        wire("handsize_discard", {"who": tag, "iid": iid})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
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
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
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
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[(tag,)] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
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


def teferi_ability_actions(acts, tef_oid):
    """ActivateAbility actions sourced from Teferi, matched by
    (source_id, ability_index) -- the engine advertises only currently
    activatable loyalty abilities (index 0=[0], 1=[-2], 2=[-12]; the -12
    appears once loyalty >= 12). The action data carries no cost info, so
    index matching replaces the earlier cost-amount parse. Deduped: the
    same ability appears in both legal_actions and legal_actions_by_object.
    """
    out = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            src = int(d.get("source_id"))
            idx = int(d.get("ability_index"))
        except (TypeError, ValueError):
            continue
        if src != int(tef_oid):
            continue
        out.append({"action": a, "index": idx,
                    "amount": {0: 0, 1: -2, 2: -12}.get(idx)})
    seen, uniq = set(), []
    for x in out:
        if x["index"] not in seen:
            seen.add(x["index"])
            uniq.append(x)
    return uniq


def candidate_owner_seat(state, ch):
    """Controller seat of a permanent candidate (best effort)."""
    ref = None
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            ref = d.get("reference")
            break
    if ref is None:
        ref = ch.get("reference")
    if ref is None:
        return None
    o = get_obj(state, ref)
    try:
        return int(o.get("controller"))
    except (TypeError, ValueError):
        return None


def target_opportunity(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            stype = spec.get("type")
            if stype in ("select", "sequence") and data.get("candidates"):
                return opp, "schema", stype
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(c for c in surf_codes(ch) if c)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


# ---------------------------------------------------------------- legs

TEFERI = "teferi, temporal pilgrim"
TURTLE = "aegis turtle"


async def handle_ult_target(c, st, ctx, tag):
    """The -12's TargetOnly step: per the bug the engine offers the
    ACTIVATOR's permanents as candidates (the opponent is never asked).
    Documented decision: record candidate owners, then pick a turtle (any
    legal choice leads to the same ChangeZone outcome). Any other surprise
    prompt is logged and left pending."""
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    key = (tag, "ult-target", str(iid))
    if key in SUBMITTED_OPPS:
        return False
    state = st["state"]
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or []
    recs = []
    for ch in cands:
        ref = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                ref = str(d.get("reference"))
                break
        nm = obj_lname(state, ref) if ref else choice_text(ch)[:40]
        recs.append({"name": nm, "owner_seat": candidate_owner_seat(state, ch),
                     "surface_ref": ref})
    ctx["ult_target_candidates"] = recs
    wire("ult_target_prompt", {"who": tag, "rtype": rtype, "stype": stype,
                               "candidates": recs})
    say(f"[{tag}] -12 target prompt: {len(recs)} candidates, "
        f"owner seats={sorted({r['owner_seat'] for r in recs if r['owner_seat'] is not None})}")
    pick = next((ch for ch, r in zip(cands, recs) if r["name"] == TURTLE), None)
    if pick is None and cands:
        pick = cands[0]
    if pick is None:
        say(f"[{tag}] WARNING: -12 target prompt with no candidates; leaving pending")
        return False
    SUBMITTED_OPPS.add(key)
    ctx["ult_target_answered"] = True
    await answer_vi(c, opp, pick, tag)
    return True


def loyalty_estimate(state, ctx):
    """Arithmetic loyalty: 4 at cast + 1 per P0 main phase observed with
    Teferi out (draw-step trigger resolved by then) + 1 per fully-resolved
    [0] activation. Cross-checked against the state's counter read."""
    return 4 + len(ctx["mains_with_teferi"]) + ctx["zero_done"]


async def p1_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_discard_to_handsize(c, acts, st, 1, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    # A6: record whether P1 is ever offered a REAL decision during the ult
    # window (priority menus don't count).
    if ctx.get("ult_activated") and not ctx.get("post_exported"):
        if real_decision_pending(st):
            ctx["p1_real_decision_seen"] = True
            wire("p1_real_decision", {"who": tag, "rev": c.revision})
            say(f"[{tag}] NOTE: P1 offered a real decision during ult window")
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
        if TURTLE in hand_names(state, 1) and untapped_land_count(state, 1) >= 1:
            a, oid = cast_action_for(acts, state, TURTLE)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1}
                say(f"[{tag}] casting {TURTLE} (oid {oid})")
                wire("cast_turtle_p1", {"oid": oid})
                await submit_as_is(c, a)
                return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, tag):
        return True
    if "DeclareAttackers" in atypes:
        import copy as _copy
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        return True
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    tn = state.get("turn_number")
    tef = teferi_oid(state, 0)
    if tef is not None and not ctx.get("teferi_seen"):
        ctx["teferi_seen"] = True
        ctx["teferi_cast_turn"] = tn
        say(f"[{tag}] Teferi on P0 battlefield (oid {tef})")
        wire("teferi_seen", {"oid": tef,
                             "object_keys": sorted(get_obj(state, tef).keys())})
        wire("teferi_object", {"object": get_obj(state, tef)})

    # A [0] activation fully resolved once the stack is empty again
    # (the draw trigger is automatic; an empty stack means it resolved).
    if ctx.get("zero_pending") and not (state.get("stack") or []):
        ctx["zero_pending"] = False
        ctx["zero_done"] += 1
        say(f"[{tag}] [0] cycle resolved (zero_done={ctx['zero_done']})")

    if ctx.get("ult_activated") and not ctx.get("ult_target_answered"):
        if await handle_ult_target(c, st, ctx, tag):
            return True

    if my_main(state, 0):
        tn = state.get("turn_number")
        if tef is not None and tn not in ctx["mains_with_teferi"]:
            # This turn's draw-step loyalty trigger has resolved by now
            # (stack is empty at main phase) -- except on the cast turn,
            # whose draw step preceded the cast.
            if tn != ctx.get("teferi_cast_turn"):
                ctx["mains_with_teferi"].add(tn)
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_names(state, 0)
        if tef is None and TEFERI in hn and untapped_land_count(state, 0) >= 5:
            a, oid = cast_action_for(acts, state, TEFERI)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 2, "generic": 3}
                say(f"[{tag}] casting Teferi (oid {oid})")
                wire("cast_teferi", {"oid": oid})
                await submit_as_is(c, a)
                return True
        if tef is None and TURTLE in hn and untapped_land_count(state, 0) >= 1 \
                and TEFERI not in hn:
            # ramp turtles only while still digging for Teferi
            a, oid = cast_action_for(acts, state, TURTLE)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1}
                say(f"[{tag}] casting {TURTLE} (oid {oid})")
                wire("cast_turtle_p0", {"oid": oid})
                await submit_as_is(c, a)
                return True
        if tef is not None and not ctx.get("ult_activated"):
            loy_est = loyalty_estimate(state, ctx)
            loy_read = loyalty_of(state, tef)
            p0_t = len(turtle_oids(state, 0))
            p1_t = len(turtle_oids(state, 1))
            wire("loyalty_check", {"turn": tn, "est": loy_est, "read": loy_read,
                                   "p0_turtles": p0_t, "p1_turtles": p1_t})
            abs_ = teferi_ability_actions(acts, tef)
            wire("teferi_abilities", {"turn": tn,
                                     "abilities": [{"index": x["index"],
"amount": x["amount"]} for x in abs_]})
            # Raw dump (once per turn): any ActivateAbility-shaped action at
            # all, and any vi choice carrying an activateAbility surface.
            dkey = (tag, "abdump", tn)
            if dkey not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(dkey)
                raw_acts = [a for a in acts if "ctivat" in (a.get("type") or "")]
                wire("ability_raw_dump", {
                    "turn": tn,
                    "n_raw_activate": len(raw_acts),
                    "raw": [json.dumps({"type": a.get("type"),
                                        "src": str(a.get("_src_oid")),
                                        "data": a.get("data", {})},
                                       default=str)[:500] for a in raw_acts],
                })
                via = []
                for opp in vi_ops(st):
                    odata = (opp.get("response", {}) or {}).get("data", {}) or {}
                    for ch in odata.get("choices") or []:
                        codes = [s.get("data", {}).get("code")
                                 for s in ch.get("surfaces", []) or []
                                 if s.get("type") == "action"]
                        if "activateAbility" in codes:
                            via.append(json.dumps(
                                {"iid": str(opp.get("interactionId"))[:12],
                                 "choice_id": ch.get("id"),
                                 "text": choice_text(ch)[:80],
                                 "surfaces": ch.get("surfaces")},
                                default=str)[:600])
                wire("vi_activate_dump", {"turn": tn, "n": len(via), "choices": via})
                say(f"[{tag}] ability dump: raw_activate={len(raw_acts)} vi_activate={len(via)}")
            if loy_est >= 12 and (loy_read is None or loy_read >= 12) \
                    and p0_t >= 2 and p1_t >= 1:
                ult = next((x for x in abs_ if x["index"] == 2), None)
                if ult is None:
                    say(f"[{tag}] WARNING: loyalty {loy_est}/{loy_read} but no "
                        f"-12 ability advertised; abilities={[(x['amount']) for x in abs_]}")
                    wire("ult_missing", {"abilities": [x["index"] for x in abs_]})
                else:
                    say(f"[{tag}] pre-ult export (loyalty est={loy_est} read={loy_read})")
                    pre = json.loads(await c.export_state())
                    with open(f"{EVDIR}/pre_ult.json", "w") as f:
                        json.dump(pre, f, indent=1)
                    wire("pre_ult_export", {"who": tag})
                    ctx["pre_ult"] = {
                        "p0_turtles": p0_t, "p1_turtles": p1_t,
                        "loyalty_est": loy_est, "loyalty_read": loy_read,
                        "turn": tn,
                    }
                    ctx["ult_activated"] = True
                    say(f"[{tag}] activating -12 (loyalty {loy_est})")
                    wire("activate_ult", {"loyalty_est": loy_est})
                    await submit_as_is(c, ult["action"])
                    return True
            elif loy_est < 12 or (loy_read is not None and loy_read < 12):
                if tn not in ctx["loyalty_used_turns"]:
                    zero = next((x for x in abs_ if x["index"] == 0), None)
                    if zero is None:
                        wire("zero_missing", {"turn": tn,
                                              "abilities": [x["index"] for x in abs_]})
                    else:
                        ctx["loyalty_used_turns"].add(tn)
                        ctx["zero_pending"] = True
                        say(f"[{tag}] activating [0] (loyalty est={loy_est} read={loy_read})")
                        wire("activate_zero", {"loyalty_est": loy_est})
                        await submit_as_is(c, zero["action"])
                        return True
            if tef is not None and TURTLE in hn \
                    and untapped_land_count(state, 0) >= 1 and p0_t < 4:
                a, oid = cast_action_for(acts, state, TURTLE)
                if a is not None:
                    ST["mana_needs"][tag] = {"U": 1}
                    say(f"[{tag}] casting {TURTLE} (oid {oid})")
                    wire("cast_turtle_p0", {"oid": oid})
                    await submit_as_is(c, a)
                    return True
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


def _opp_summary(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        out.append({"iid": str(opp.get("interactionId"))[:8],
                    "rtype": resp.get("type"),
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c)})
    return out


async def settle_until(cond, p0, p1, tag0, tag1, ctx, timeout, label, poll=0.25):
    t0 = time.time()
    it = 0
    last_diag = 0
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await p1_tick(p1, tag1, ctx)
        await p0_tick(p0, tag0, ctx)
        await asyncio.sleep(0)
        it += 1
        if time.time() - last_diag > 20:
            last_diag = time.time()
            for c, pidx in ((p0, 0), (p1, 1)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"act_types={sorted({a.get('type') for a in acts})} "
                    f"decision_pending={decision_pending(st)} "
                    f"my_priority={my_priority(acts)} "
                    f"p0bf={bf_names(s, 0)[:8]} p1bf={bf_names(s, 1)[:8]}")
                if decision_pending(st):
                    say(f"[diag] {c.name} pending opps: {_opp_summary(st)}")
                    wire("diag_pending", {"who": c.name, "rev": c.revision,
                                          "opps": _opp_summary(st)})
                    for a in acts:
                        if a.get("type") == "SelectCards":
                            wire("selectcards_action_shape", {
                                "who": c.name,
                                "data": a.get("data", {}),
                                "src_oid": str(a.get("_src_oid")),
                            })
                            break
                    for opp in vi_ops(st):
                        iid = str(opp.get("interactionId"))
                        dk = ("oppdump", iid)
                        if dk in SUBMITTED_OPPS:
                            continue
                        SUBMITTED_OPPS.add(dk)
                        resp = opp.get("response", {}) or {}
                        rdata = resp.get("data", {}) or {}
                        cands = rdata.get("candidates") or []
                        first = cands[0] if cands else {}
                        wire("opp_full_shape", {
                            "who": c.name, "iid": iid[:12],
                            "rtype": resp.get("type"),
                            "spec": (rdata.get("spec") or {}),
                            "n_candidates": len(cands),
                            "first_candidate_keys": sorted(first.keys())
                            if isinstance(first, dict) else type(first).__name__,
                            "first_candidate_json": json.dumps(first, default=str)[:600],
                            "top_level_keys": sorted(rdata.keys()),
                        })
        st = p0.latest
        if st and cond(st["state"], ctx):
            return True
    say(f"[{tag0}] settle timeout: {label}")
    wire("settle_timeout", {"label": label})
    return False


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9847-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {"teferi_seen": False, "teferi_cast_turn": None,
           "mains_with_teferi": set(), "zero_done": 0, "zero_pending": False,
           "loyalty_used_turns": set(), "ult_activated": False,
           "ult_target_answered": False, "ult_target_candidates": None,
           "p1_real_decision_seen": False, "pre_ult": None,
           "post_exported": False}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Island", 52), ("Teferi, Temporal Pilgrim", 4),
                         ("Aegis Turtle", 4)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 52), ("Aegis Turtle", 8)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                                and p1.player_id is not None) else "failed"

    def ult_window_reached(s, c):
        return c.get("ult_activated") or (s.get("turn_number") or 0) > 46
    ok = await settle_until(ult_window_reached, p0, p1, tag, f"P1{tag}", ctx,
                            timeout=900, label="ult window")
    st = p0.latest
    state = st["state"] if st else {}
    pre = ctx.get("pre_ult") or {}
    obs["assert"]["A2_fixture_ok"] = "passed" if (
        ctx.get("teferi_seen") and ctx.get("ult_activated")
        and pre.get("p0_turtles", 0) >= 2 and pre.get("p1_turtles", 0) >= 1
    ) else "failed"
    try:
        with open(f"{EVDIR}/pre_ult.json") as f:
            json.load(f)
        obs["assert"]["A3_pre_ult_export"] = "passed"
    except (OSError, ValueError):
        obs["assert"]["A3_pre_ult_export"] = "failed"
    if not ctx.get("ult_activated"):
        obs["notes"].append(
            f"ult window never reached: teferi_seen={ctx.get('teferi_seen')}, "
            f"turn={state.get('turn_number')}, "
            f"P0 BF={bf_names(state, 0)}, P0 hand={hand_names(state, 0)}, "
            f"loyalty_est={loyalty_estimate(state, ctx)}")
        for k in ("A4_bug_outcome", "A5_correct_outcome", "A6_opp_never_asked",
                  "A7_data_shape"):
            obs["assert"][k] = "not-run"
        await p0.close(); await p1.close()
        return obs

    def ult_resolved(s, c):
        return c.get("ult_activated") and not (s.get("stack") or [])
    ok = await settle_until(ult_resolved, p0, p1, tag, f"P1{tag}", ctx,
                            timeout=300, label="ult resolution")
    # let post-resolution priority rounds settle, then export
    for _ in range(6):
        await asyncio.sleep(0.5)
        await p1_tick(p1, f"P1{tag}", ctx)
        await p0_tick(p0, tag, ctx)
    say(f"[{tag}] post-ult export")
    post = json.loads(await p0.export_state())
    with open(f"{EVDIR}/post_ult.json", "w") as f:
        json.dump(post, f, indent=1)
    wire("post_ult_export", {"who": tag})
    ctx["post_exported"] = True
    pstate = post.get("state", post)
    p0_t_post = len(turtle_oids(pstate, 0))
    p1_t_post = len(turtle_oids(pstate, 1))
    tef_post = teferi_oid(pstate, 0)
    obs["post_counts"] = {"p0_turtles": p0_t_post, "p1_turtles": p1_t_post,
                          "teferi_present": tef_post is not None,
                          "p0_hand_size": len(hand_ids(pstate, 0)),
                          "p1_hand_size": len(hand_ids(pstate, 1))}
    obs["pre_counts"] = {"p0_turtles": pre.get("p0_turtles"),
                         "p1_turtles": pre.get("p1_turtles")}
    obs["ult_target_candidates"] = ctx.get("ult_target_candidates")
    obs["p1_real_decision_seen"] = ctx.get("p1_real_decision_seen")
    say(f"[{tag}] pre: P0 turtles={pre.get('p0_turtles')} P1 turtles={pre.get('p1_turtles')}; "
        f"post: P0 turtles={p0_t_post} P1 turtles={p1_t_post} teferi_present={tef_post is not None}")
    wire("counts", {"pre": obs["pre_counts"], "post": obs["post_counts"]})

    obs["assert"]["A4_bug_outcome"] = (
        "passed" if (p0_t_post == 0 and p1_t_post == pre.get("p1_turtles")) else "failed")
    obs["assert"]["A5_correct_outcome"] = (
        "passed" if (p1_t_post == 0 and p0_t_post == pre.get("p0_turtles")) else "failed")
    obs["assert"]["A6_opp_never_asked"] = (
        "passed" if not ctx.get("p1_real_decision_seen") else "failed")
    if (p0_t_post, p1_t_post) not in ((0, pre.get("p1_turtles")),
                                      (pre.get("p0_turtles"), 0)):
        obs["notes"].append(
            f"ambiguous board outcome: pre P0={pre.get('p0_turtles')} P1={pre.get('p1_turtles')} "
            f"post P0={p0_t_post} P1={p1_t_post}; teferi_present_post={tef_post is not None}; "
            f"see wire log")
    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    check_data_level()
    obs = None
    for attempt in range(3):
        obs = await run_attempt(attempt)
        a2 = obs["assert"].get("A2_fixture_ok")
        if a2 == "passed":
            break
        say(f"[attempt {attempt}] incomplete (A2={a2}); retrying"
            if attempt < 2 else f"[attempt {attempt}] incomplete; giving up")

    ass = dict(obs["assert"])
    ass["A7_data_shape"] = "passed"  # check_data_level() asserts before any run
    notes = list(obs["notes"])
    notes.append(f"pre-ult counts: {obs.get('pre_counts')}; "
                 f"post-ult counts: {obs.get('post_counts')}")
    notes.append(f"ult target prompt candidates: {obs.get('ult_target_candidates')}")
    notes.append(f"P1 offered a real decision during ult window: "
                 f"{obs.get('p1_real_decision_seen')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; loyalty abilities via "
                 "ActivateAbility matched on Loyalty cost amount ([0] to ramp, "
                 "-12 at >=12 loyalty); priority-gated passes; sleep(0) yield "
                 "before leg evaluation; loyalty tracked arithmetically "
                 "(4 + draw-step triggers + resolved [0] draws) and "
                 "cross-checked against the state's counter read.")

    a2, a4, a5 = ass.get("A2_fixture_ok"), ass.get("A4_bug_outcome"), ass.get("A5_correct_outcome")
    if a2 == "passed" and a4 == "passed":
        verdict = "reproduced"
    elif a2 == "passed" and a5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = fixture complete and P0's own "
                 "turtles were bounced/shuffled away (0 remain) while P1's "
                 "turtles were untouched. not-reproduced = fixture complete "
                 "and P1's turtles were bounced/shuffled away (0 remain) "
                 "while P0's were untouched.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9847",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("runs/run-9846-20261013-051252 (v0.108.0 server on "
                           "127.0.0.1:9374 started 05:13 by the #9846 run; "
                           "evidence under evidence/9847/" + RUN_ID + ")"),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Island", 52], ["Teferi, Temporal Pilgrim", 4],
                   ["Aegis Turtle", 4]],
            "P1": [["Island", 52], ["Aegis Turtle", 8]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; A6 %s; A7 %s; "
                   "pre P0/P1 turtles=%s/%s post P0/P1 turtles=%s/%s"
                   % (ass.get("A1_setup_ok"), a2, ass.get("A3_pre_ult_export"),
                      a4, a5, ass.get("A6_opp_never_asked"),
                      ass.get("A7_data_shape"),
                      (obs.get("pre_counts") or {}).get("p0_turtles"),
                      (obs.get("pre_counts") or {}).get("p1_turtles"),
                      (obs.get("post_counts") or {}).get("p0_turtles"),
                      (obs.get("post_counts") or {}).get("p1_turtles"))),
        "scope": ("Teferi, Temporal Pilgrim -12 ability: which player's "
                  "permanents are bounced/shuffled and whether the opponent "
                  "is asked; two-player game; native human seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "Loyalty tracked arithmetically (4 + resolved draw-step triggers "
            "+ resolved [0] draws) and cross-checked against the state's "
            "counter read when available; see wire log loyalty_check entries.",
            "The -12's TargetOnly permanent choice is answered by the driver "
            "(documented decision: prefer a turtle); the decisive assertion "
            "is the resolution outcome (board counts), not the prompt.",
        ],
        "setup_line": ("P0 52x Island + 4x Teferi, Temporal Pilgrim ({3}{U}{U}, "
                       "loyalty 4) + 4x Aegis Turtle ({U} 0/5); P1 52x Island "
                       "+ 8x Aegis Turtle"),
        "contract_line": ("Cast Teferi, ramp loyalty to >=12 via [0] "
                          "(draw-step + [0] draws each trigger the loyalty "
                          "counter), then activate -12 at sorcery speed. "
                          "Expected (correct): target opponent P1 chooses a "
                          "permanent -> bounced; P1's nonland permanents "
                          "shuffled into library. Bug: the activator P0's own "
                          "permanents are bounced/shuffled; P1 never asked."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9847.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read()) + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), 'Issue #9847 - Teferi, Temporal Pilgrim -12 hits the wrong player',
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_fixture_ok": "A2 Teferi cast; -12 activated; P0>=2/P1>=1 turtles pre-ult",
        "A3_pre_ult_export": "A3 pre-ult state captured",
        "A4_bug_outcome": "A4 [bug] P0's own turtles bounced/shuffled; P1 untouched",
        "A5_correct_outcome": "A5 [correct] P1's turtles bounced/shuffled; P0 untouched",
        "A6_opp_never_asked": "A6 opponent (P1) never offered a real decision",
        "A7_data_shape": "A7 card-data -12 lower matches the reported wrong shape",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:15]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9847/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9847.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9847 instance is alive "
                             f"(pid {old}); refusing")
        except (ProcessLookupError, ValueError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        if os.path.exists(pidfile):
            os.remove(pidfile)


if __name__ == "__main__":
    asyncio.run(main())
