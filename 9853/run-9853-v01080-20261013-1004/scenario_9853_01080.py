#!/usr/bin/env python3
"""Issue #9853: '"Enters with" counters gated by an "If" in a different
sentence: the condition is dropped or claimed by the wrong replacement'.

Oracle (Rescuer Sphinx): "Flying. As this creature enters, you may return
a nonland permanent you control to its owner's hand. If you do, this
creature enters with a +1/+1 counter on it."

Report (phase-rs/phase#9853, opened 2026-10-12 by matthewevans, open,
bug/area:parser/priority:p2-wrong-game-result): the "If you do" condition
does not reach the replacement -- counters are added even when you
decline the return (or the optional gate isn't carried).

Behavioral contract (per playbook: test the reported OUTCOME; this is a
parser-subsystem report, so the decisive contract is parse-level, with a
game-level confirmation of the reported wrong-game-result):
  Parse (decisive): in the pinned v0.108.0 card-data.json, Rescuer
      Sphinx's "enters with a +1/+1 counter" replacement must carry the
      "If you do" condition (or the card must read as unsupported). Bug:
      the PutCounter effect is lowered Mandatory/unconditional
      (condition: null, optional: false, mode: Mandatory) and the
      SwallowedClause parse warnings show the Optional_YouMay clause
      AND the Condition_If ("you do") clause were swallowed.
  Game (confirming): P0 casts Rescuer Sphinx while controlling a nonland
      permanent (Merfolk of the Pearl Trident). Expected under the bug:
      NO "may return" prompt is ever offered, and the Sphinx enters WITH
      a +1/+1 counter anyway (counter added despite no return having
      happened). Leg A (decline): decline the may-prompt if offered;
      assert the Sphinx enters with NO +1/+1 counter. Leg B (accept,
      control): if the prompt is offered, accept and return the Merfolk;
      assert the Sphinx enters with EXACTLY ONE +1/+1 counter.

Assertions:
  A1 setup_ok        game created, both seats joined
  A2 parse_condition FAILED = the reported bug: the "If you do" condition
                     and the optional "may return" gate are absent from
                     the lowering (PutCounter Mandatory, condition null;
                     SwallowedClause warnings for Optional_YouMay and
                     Condition_If "you do"); passed = condition present
                     (or card unsupported)
  A3 no_return_no_counter  FAILED = the reported wrong-game-result: the
                     Sphinx entered WITH a +1/+1 counter although no
                     nonland permanent was returned (decline leg); passed
                     = it entered with no counter (correct); not-run =
                     the Sphinx never entered
  A4 accept_one_counter  control: accept the return prompt, return a
                     nonland permanent; passed = exactly one +1/+1
                     counter; failed = wrong count; not-run = the return
                     prompt was never offered (the optional gate is absent
                     entirely) or the Sphinx never entered
  A5 data_shape      the card-data lower matches the reported shape
                     (one Moved-event replacement, PutCounter P1P1 x1,
                     target SelfRef)

Verdict:
  reproduced      A2 failed (condition dropped in parse output) -- the
                  reported outcome, corroborated by A3 if the game leg
                  completed
  not-reproduced  A2 passed (condition present or card unsupported)
  blocked         fixture never reached, or parse evidence unreadable

Driver conventions (verified scenario_9852_01080.py, protocol 134):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (viewer_interaction)
  - exact card-data names; export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; vi candidate references are strings
    (normalize with str() before oid comparisons)
  - real_decision_pending filters None surface codes (9845 lesson)
  - genuine card-choice candidates have NO action surface (9842 lesson)
  - runtime state objects use "card_types" (plural); card-data.json uses
    "card_type" (singular) (9847 lesson)
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario9853")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9853-v01080-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9853
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
               "v0.108.0 server on 127.0.0.1:9374 (backfill-owned, pid "
               "4038, --single-user --no-data-download, started earlier "
               "today on the same verified release; reused by this run); "
               "scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9853/" + RUN_ID),
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


def is_creature(o):
    return "Creature" in (((o.get("card_types") or {}).get("core_types")) or [])


SPHINX = "rescuer sphinx"
MERFOLK = "merfolk of the pearl trident"
ISLAND = "island"


def sphinx_oid(state, spell_oid=None, pid=0):
    if spell_oid is not None:
        o = get_obj(state, spell_oid)
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller", -1)) == str(pid):
            return str(spell_oid)
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == SPHINX:
            return o
    return None


def merfolk_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == MERFOLK]


def p1p1_counters_of(state, oid):
    """+1/+1 counters on a battlefield object. Returns (count, raw) where
    raw is the verbatim counters payload for evidence."""
    o = get_obj(state, oid)
    raw = o.get("counters")
    count = 0
    keys = ("p1p1", "+1/+1", "plusone", "plus_one", "plus1plus1", "ponepone")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if any(key in str(k).lower() for key in keys):
                try:
                    count += int(v)
                except (TypeError, ValueError):
                    count += 1
    elif isinstance(raw, list):
        for c in raw:
            nm = c.get("name") if isinstance(c, dict) else str(c)
            if any(key in str(nm).lower() for key in keys):
                try:
                    count += int(c.get("count", 1)) if isinstance(c, dict) else 1
                except (TypeError, ValueError):
                    count += 1
    return count, raw


def obj_pt(state, oid):
    o = get_obj(state, oid)
    return o.get("power"), o.get("toughness")


def stack_spell_oid(state, lname):
    for e in state.get("stack") or []:
        oid = e.get("object_id", e.get("id", ""))
        if obj_lname(state, oid) == lname:
            return str(oid)
    return None


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


def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices whose
    choices carry candidate/target codes (passPriority menus and
    decideOptionalEffect excluded)."""
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


def check_parse_condition():
    """Record the parser shape this scenario depends on: Rescuer Sphinx's
    "enters with a +1/+1 counter" replacement must carry the "If you do"
    condition and the optional "may return" gate, or the card must read as
    unsupported.

    Returns a dict with the decisive A2 evidence. This function does NOT
    abort the run when the bug is present -- the dropped condition IS the
    reported outcome, so the game confirmation still runs."""
    rs = CARD_DATA.get("rescuer sphinx", {})
    oracle = rs.get("oracle_text") or ""
    has_if_you_do = "If you do, this creature enters with a +1/+1 counter" in oracle
    has_may_return = "you may return a nonland permanent you control" in oracle
    replacements = rs.get("replacements") or []
    rep = replacements[0] if len(replacements) == 1 else None
    exec_ = (rep.get("execute") or {}) if rep else {}
    eff = exec_.get("effect") or {}
    analysis = {
        "oracle_has_if_you_do": has_if_you_do,
        "oracle_has_may_return": has_may_return,
        "n_replacements": len(replacements),
        "replacement_event": (rep or {}).get("event"),
        "replacement_mode": (rep or {}).get("mode"),
        "replacement_condition": (rep or {}).get("condition"),
        "replacement_description": (rep or {}).get("description"),
        "execute_kind": exec_.get("kind"),
        "execute_optional": exec_.get("optional"),
        "execute_condition": exec_.get("condition"),
        "execute_sub_ability": exec_.get("sub_ability"),
        "effect_type": eff.get("type"),
        "effect_counter_type": eff.get("counter_type"),
        "effect_count": eff.get("count"),
        "effect_target": eff.get("target"),
        "parse_warnings": rs.get("parse_warnings"),
    }
    swallowed = []
    for w in rs.get("parse_warnings") or []:
        swallowed.append({
            "type": w.get("type"),
            "detector": w.get("detector"),
            "description": str(w.get("description"))[:160],
            "gap": w.get("gap"),
        })
    analysis["swallowed_warnings"] = swallowed
    # The reported bug: the PutCounter is lowered Mandatory/unconditional
    # (no condition anywhere, optional: false), the "may return a nonland
    # permanent" optional effect is absent (no sub_ability), and the
    # parser's own SwallowedClause warnings show both the
    # Optional_YouMay clause and the Condition_If ("you do") clause were
    # swallowed.
    swallowed_types = {(w.get("type"), w.get("detector")) for w in
                       rs.get("parse_warnings") or []}
    condition_dropped = (
        has_if_you_do
        and len(replacements) == 1
        and eff.get("type") == "PutCounter"
        and (rep or {}).get("condition") is None
        and exec_.get("condition") is None
        and exec_.get("optional") is False
        and ("SwallowedClause", "Optional_YouMay") in swallowed_types
        and ("SwallowedClause", "Condition_If") in swallowed_types
    )
    analysis["condition_dropped"] = condition_dropped
    card_unsupported = any(
        isinstance((r.get("execute") or {}).get("effect", {}).get("type"), dict)
        and (r.get("execute") or {}).get("effect", {}).get("type", {}).get("type")
        == "Unimplemented"
        for r in replacements)
    analysis["card_reads_unsupported"] = card_unsupported
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump({"issue": 9853, "card": "rescuer sphinx",
                   "oracle_text": oracle,
                   "replacements": replacements,
                   "analysis": analysis}, f, indent=1, default=str)
    say(f"parse-level check: condition_dropped={condition_dropped} "
        f"effect={eff.get('type')} mode={(rep or {}).get('mode')} "
        f"exec_optional={exec_.get('optional')} "
        f"exec_condition={exec_.get('condition')}")
    wire("parse_level", analysis)
    return analysis


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


def find_may_opp(st):
    """Find an OptionalEffectChoice 'you may' opportunity: exactChoices
    with a decideOptionalEffect action code."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


def find_replacement_opp(st):
    """Find a ReplacementChoice prompt: exactChoices whose choices carry a
    chooseReplacement code."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "chooseReplacement" in surf_codes(ch):
                return opp
    return None


async def answer_may(c, st, cast, tag, want_accept=True):
    """Answer the Sphinx's 'you may return a nonland permanent' prompt.
    Leg A (decline branch): want_accept=False. Leg B (accept branch):
    want_accept=True."""
    opp = find_may_opp(st)
    if opp is None:
        return False
    key = ("may", cast["branch"], str(opp.get("interactionId")))
    if key in SUBMITTED_OPPS:
        return False
    cast["prompt_offered"] = True
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "accept" in txt or "return" in txt or "yes" in txt:
                is_accept = True
            elif "decline" in txt or "don't" in txt or "do not" in txt or "no" in txt:
                is_accept = False
        if is_accept == want_accept:
            SUBMITTED_OPPS.add(key)
            cast["may_answered"] = True
            cast["may_accepted"] = want_accept
            say(f"[{tag}] may-return prompt: "
                f"{'ACCEPT' if want_accept else 'DECLINE'} "
                f"(branch {cast['branch']})")
            wire("may_answer", {"who": tag, "accept": want_accept,
                                "branch": cast["branch"]})
            await answer_vi(c, opp, ch, tag)
            return True
    wire("may_no_matching_choice",
         {"who": tag, "branch": cast["branch"], "opps": _opp_summary(st)})
    say(f"[{tag}] may prompt present but no matching choice (branch {cast['branch']})")
    return False


async def answer_return_target(c, st, cast, tag):
    """Answer the accept-branch follow-up: choose which nonland permanent
    to return. Prefers the branch's recorded Merfolk."""
    if cast.get("return_target_chosen"):
        return False
    state = st["state"]
    want = str(cast.get("return_target_oid")) if cast.get("return_target_oid") else None
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return False
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    if not cands:
        return False
    cast["prompt_offered"] = True
    # legal returnables: nonland permanents P0 controls
    legal = {o for o in bf_oids(state, 0) if not is_land(get_obj(state, o))}
    pick = None
    if want is not None:
        for ch in cands:
            if _cand_ref(ch) == want:
                pick = ch
                break
    if pick is None:
        for ch in cands:
            if _cand_ref(ch) in legal:
                pick = ch
                break
    if pick is None:
        wire("return_target_no_match",
             {"who": tag, "branch": cast["branch"], "want": want,
              "legal": sorted(legal), "opps": _opp_summary(st)})
        return False
    iid = opp.get("interactionId")
    key = ("return-target", cast["branch"], str(iid))
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    cast["return_target_chosen"] = True
    cast["returned_oid"] = _cand_ref(pick)
    cast["returned_name"] = obj_lname(state, cast["returned_oid"]) \
        if cast["returned_oid"] else "?"
    say(f"[{tag}] returning {cast['returned_name']} "
        f"(oid {cast['returned_oid']}) to hand (branch {cast['branch']})")
    wire("return_target_answer",
         {"who": tag, "branch": cast["branch"],
          "returned_oid": cast["returned_oid"],
          "returned_name": cast["returned_name"]})
    await answer_vi(c, opp, pick, tag)
    return True


def active_cast(ctx):
    for cast in ctx["casts"]:
        if not cast.get("done"):
            return cast
    return None


def update_cast(c, st, ctx, tag):
    """Track each Sphinx cast: submitted -> on stack (pre export) ->
    resolved -> entered (post export, counters read)."""
    state = st["state"]
    for cast in ctx["casts"]:
        if cast.get("done"):
            continue
        spell_oid = stack_spell_oid(state, SPHINX)
        if spell_oid and not cast.get("seen_on_stack"):
            cast["seen_on_stack"] = True
            cast["spell_oid"] = spell_oid
            cast["need_pre_export"] = True
            wire("sphinx_on_stack",
                 {"who": tag, "branch": cast["branch"],
                  "rev": c.revision, "spell_oid": spell_oid})
            say(f"[{tag}] Sphinx on the stack (branch {cast['branch']}) "
                f"-- pre-cast export next")
        if cast.get("seen_on_stack") and spell_oid is None \
                and not cast.get("resolved"):
            cast["resolved"] = True
            wire("sphinx_resolved",
                 {"who": tag, "branch": cast["branch"],
                  "rev": c.revision,
                  "p0_bf": bf_names(state, 0)[:10]})
            say(f"[{tag}] Sphinx spell resolved (branch {cast['branch']}); "
                f"awaiting entry/prompts")
        if cast.get("resolved") and not cast.get("entered"):
            perm = sphinx_oid(state, cast.get("spell_oid"), 0)
            if perm is not None:
                cast["entered"] = True
                cast["sphinx_oid"] = perm
                n, raw = p1p1_counters_of(state, perm)
                cast["counters"] = n
                cast["counters_raw"] = raw
                cast["need_post_export"] = True
                pwr, tgh = obj_pt(state, perm)
                cast["power"] = str(pwr)
                cast["toughness"] = str(tgh)
                wire("sphinx_entered",
                     {"who": tag, "branch": cast["branch"],
                      "rev": c.revision, "sphinx_oid": perm,
                      "p1p1_counters": n, "counters_raw": raw,
                      "power": str(pwr), "toughness": str(tgh),
                      "prompt_offered": cast.get("prompt_offered", False),
                      "may_accepted": cast.get("may_accepted"),
                      "returned_oid": cast.get("returned_oid")})
                say(f"[{tag}] Sphinx entered (oid {perm}) P/T "
                    f"{pwr}/{tgh} +1/+1 counters={n} "
                    f"prompt_offered={cast.get('prompt_offered', False)} "
                    f"(branch {cast['branch']})")


async def handle_sphinx_prompt(c, st, ctx, tag, seat):
    """Answer the Sphinx's own entry prompts (may-return + return target)
    for the active cast. Any other real decision is recorded, never
    auto-answered."""
    cast = active_cast(ctx)
    if cast is None:
        return False
    # 1) the "you may return" prompt (expected absent under the bug)
    if not cast.get("may_answered"):
        want_accept = (cast["branch"] == "accept")
        if await answer_may(c, st, cast, tag, want_accept=want_accept):
            return True
    # 2) the accept-branch follow-up: which permanent to return
    if cast.get("may_accepted") and not cast.get("return_target_chosen"):
        if await answer_return_target(c, st, cast, tag):
            return True
    # 3) a bare target-select without a may prompt (accept-shaped entry)
    opp, rtype, stype = target_opportunity(st)
    if opp is not None and not cast.get("may_answered"):
        cast["prompt_offered"] = True
        wire("bare_target_select",
             {"who": tag, "branch": cast["branch"], "rev": c.revision,
              "opps": _opp_summary(st)})
        say(f"[{tag}] bare target-select during Sphinx entry (branch "
            f"{cast['branch']}): {_opp_summary(st)}")
        if cast["branch"] == "accept":
            if await answer_return_target(c, st, cast, tag):
                return True
        return True  # decline branch: do not answer; recorded above
    # 4) replacement-choice shaped prompt (unexpected for this card)
    ropp = find_replacement_opp(st)
    if ropp is not None:
        cast["prompt_offered"] = True
        wire("replacement_prompt",
             {"who": tag, "branch": cast["branch"], "rev": c.revision,
              "opps": _opp_summary(st)})
        say(f"[{tag}] unexpected ReplacementChoice during Sphinx entry "
            f"(branch {cast['branch']}): recorded, not answered")
    if real_decision_pending(st):
        key = (tag, "other-prompt", seat, c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt_during_cast",
                 {"who": tag, "seat": seat, "rev": c.revision,
                  "branch": cast["branch"],
                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt during cast (seat {seat}, branch "
                f"{cast['branch']}): {_opp_summary(st)} -- recorded, "
                f"not answered")
    return False


# ---------------------------------------------------------------- legs

async def common_tick_head(c, acts, st, pid, tag, ctx):
    """Shared head for both legs. Returns 'acted' (True/False) or None to
    continue with seat-specific logic."""
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
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
    return None


async def p1_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)

    r = await common_tick_head(c, acts, st, 1, tag, ctx)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
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
    r = await common_tick_head(c, acts, st, 0, tag, ctx)
    if r is not None:
        return True
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    update_cast(c, st, ctx, tag)
    # per-cast exports
    for cast in ctx["casts"]:
        if cast.pop("need_pre_export", False):
            say(f"[{tag}] pre-cast export (branch {cast['branch']})")
            pre = json.loads(await c.export_state())
            with open(f"{EVDIR}/pre_cast_{cast['branch']}.json", "w") as f:
                json.dump(pre, f, indent=1)
            wire("pre_cast_export", {"who": tag, "branch": cast["branch"]})
            cast["pre_exported"] = True
        if cast.pop("need_post_export", False):
            say(f"[{tag}] post-cast export (branch {cast['branch']})")
            post = json.loads(await c.export_state())
            with open(f"{EVDIR}/post_cast_{cast['branch']}.json", "w") as f:
                json.dump(post, f, indent=1)
            wire("post_cast_export", {"who": tag, "branch": cast["branch"]})
            cast["post_exported"] = True
            cast["done"] = True
    if await handle_sphinx_prompt(c, st, ctx, tag, 0):
        return True

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_names(state, 0)
        dkey = (tag, "castdiag", state.get("turn_number"), state.get("phase"))
        if dkey not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(dkey)
            cast_types = sorted({a.get("type") for a in acts
                                 if "cast" in str(a.get("type")).lower()})
            say(f"[{tag}] castdiag turn={state.get('turn_number')} "
                f"hand_n={len(hn)} sphinx_in_hand={SPHINX in hn} "
                f"untapped_lands={untapped_land_count(state, 0)} "
                f"cast_actions={cast_types} "
                f"p0_merfolks={len(merfolk_oids(state, 0))}")
            wire("cast_diag", {"who": tag, "turn": state.get("turn_number"),
                               "hand_n": len(hn),
                               "sphinx_in_hand": SPHINX in hn,
                               "untapped_lands": untapped_land_count(state, 0),
                               "cast_action_types": cast_types,
                               "hand": hn})
        cast_types = sorted({a.get("type") for a in acts
                             if "cast" in str(a.get("type")).lower()})
        if (SPHINX in hn and untapped_land_count(state, 0) >= 4
                and not cast_types):
            ctx["uncastable_streak"] = ctx.get("uncastable_streak", 0) + 1
            if ctx["uncastable_streak"] >= 8 \
                    and not ctx.get("uncastable_confirmed"):
                ctx["uncastable_confirmed"] = True
                say(f"[{tag}] UNCASTABLE: {ctx['uncastable_streak']} "
                    f"consecutive P0 main phases with Sphinx in hand "
                    f"and >=4 untapped lands, zero cast actions advertised")
                wire("uncastable_confirmed",
                     {"who": tag, "streak": ctx["uncastable_streak"],
                      "hand": hn,
                      "untapped_lands": untapped_land_count(state, 0)})
        else:
            ctx["uncastable_streak"] = 0
        # P0 fields a Merfolk first: it is the nonland permanent the
        # Sphinx could return (and the accept branch's return target).
        if MERFOLK in hn and untapped_land_count(state, 0) >= 1 \
                and len(merfolk_oids(state, 0)) < 2 \
                and not any(not x.get("done") for x in ctx["casts"]):
            a, oid = cast_action_for(acts, state, MERFOLK)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1}
                say(f"[{tag}] casting {MERFOLK} (oid {oid})")
                wire("cast_merfolk_p0", {"oid": oid})
                await submit_as_is(c, a)
                return True
        # branch plan: first cast = decline leg; second cast = accept leg
        # (only meaningful if the engine offers the return prompt).
        n_done = sum(1 for x in ctx["casts"] if x.get("done"))
        n_live = sum(1 for x in ctx["casts"] if not x.get("done"))
        if n_live == 0 and n_done < 2 and SPHINX in hn \
                and untapped_land_count(state, 0) >= 4 \
                and len(merfolk_oids(state, 0)) >= 1:
            branch = "decline" if n_done == 0 else "accept"
            tgt = merfolk_oids(state, 0)[0]
            a, oid = cast_action_for(acts, state, SPHINX)
            if a is not None:
                ctx["casts"].append({
                    "branch": branch,
                    "return_target_oid": str(tgt),
                    "submitted": True, "seen_on_stack": False,
                    "spell_oid": None, "resolved": False,
                    "prompt_offered": False,
                    "may_answered": False, "may_accepted": None,
                    "return_target_chosen": False,
                    "returned_oid": None, "returned_name": None,
                    "entered": False, "sphinx_oid": None,
                    "counters": None, "counters_raw": None,
                    "power": None, "toughness": None,
                    "pre_exported": False, "post_exported": False,
                    "done": False})
                ST["mana_needs"][tag] = {"U": 2, "generic": 2}
                say(f"[{tag}] casting Rescuer Sphinx (oid {oid}) "
                    f"branch={branch} return_target={tgt} "
                    f"({obj_lname(state, tgt)})")
                wire("cast_sphinx",
                     {"oid": oid, "branch": branch,
                      "return_target_oid": str(tgt),
                      "cast_action_data": a.get("data")})
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


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


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


# ---------------------------------------------------------------- run

async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9853-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {"casts": [], "uncastable_streak": 0,
           "uncastable_confirmed": False}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Island", 48), ("Merfolk of the Pearl Trident", 8),
                         ("Rescuer Sphinx", 4)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Island", 52),
                                    ("Merfolk of the Pearl Trident", 8)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                                and p1.player_id is not None) else "failed"

    # fast-abort: the fixture needs Rescuer Sphinx in hand early;
    # redraw a fresh game when the opening 7 lacks it.
    for _ in range(80):
        await asyncio.sleep(0.25)
        if p0.latest and p0.latest.get("state", {}).get("turn_number", 0) >= 1:
            break
    st = p0.latest
    if st is None or SPHINX not in hand_names(st["state"], 0):
        say(f"[{tag}] fast abort: opening hand lacks Rescuer Sphinx "
            f"({hand_names(st['state'], 0) if st else 'no state'})")
        wire("fast_abort", {"reason": "no sphinx in opening hand",
                            "hand": hand_names(st["state"], 0) if st else []})
        obs["notes"].append("fast abort: opening hand lacked Rescuer "
                            "Sphinx; fresh game redealt")
        obs["assert"]["A3_no_return_no_counter"] = "not-run"
        obs["assert"]["A4_accept_one_counter"] = "not-run"
        obs["fast_aborted"] = True
        await p0.close()
        await p1.close()
        return obs

    t0 = time.time()
    last_diag = 0
    settle_ticks = 0
    while time.time() - t0 < 900:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx)
        await p0_tick(p0, tag, ctx)
        await asyncio.sleep(0)
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
                    f"decision_pending={decision_pending(st)} "
                    f"my_priority={my_priority(acts)} "
                    f"casts={[(x['branch'], x.get('entered'), x.get('counters')) for x in ctx['casts']]} "
                    f"p0bf={bf_names(s, 0)[:6]} p1bf={bf_names(s, 1)[:6]}")
                if decision_pending(st):
                    say(f"[diag] {c.name} pending opps: {_opp_summary(st)}")
        # end condition: both planned casts done + quiet settle
        done_casts = [x for x in ctx["casts"] if x.get("done")]
        if len(done_casts) >= 2:
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break
        if len(ctx["casts"]) == 0 and time.time() - t0 > 600:
            say(f"[{tag}] no cast submitted >600s; capturing state and bailing")
            break
        if ctx.get("uncastable_confirmed"):
            say(f"[{tag}] uncastability established; ending attempt")
            break

    st = p0.latest
    state = st["state"] if st else {}
    obs["casts"] = [
        {"branch": x["branch"], "submitted": x.get("submitted"),
         "seen_on_stack": x.get("seen_on_stack"),
         "resolved": x.get("resolved"),
         "prompt_offered": x.get("prompt_offered"),
         "may_answered": x.get("may_answered"),
         "may_accepted": x.get("may_accepted"),
         "return_target_chosen": x.get("return_target_chosen"),
         "returned_oid": x.get("returned_oid"),
         "returned_name": x.get("returned_name"),
         "entered": x.get("entered"),
         "sphinx_oid": x.get("sphinx_oid"),
         "counters": x.get("counters"),
         "counters_raw": x.get("counters_raw"),
         "power": x.get("power"), "toughness": x.get("toughness"),
         "pre_exported": x.get("pre_exported"),
         "post_exported": x.get("post_exported")}
        for x in ctx["casts"]]

    # ---- A3: decline leg -- nothing was returned, so the Sphinx must
    # enter with NO +1/+1 counter. Bug expectation: it enters WITH one.
    dec = next((x for x in ctx["casts"] if x["branch"] == "decline"), None)
    if dec is None or not dec.get("entered"):
        obs["assert"]["A3_no_return_no_counter"] = "not-run"
        obs["notes"].append(
            "A3 not-run: decline-branch Sphinx never entered the battlefield "
            f"(casts={obs['casts']})")
    elif dec.get("counters") == 0:
        obs["assert"]["A3_no_return_no_counter"] = "passed"
        obs["notes"].append(
            f"A3: decline branch: the Sphinx (oid {dec.get('sphinx_oid')}) "
            f"entered with NO +1/+1 counter although nothing was returned "
            f"(prompt_offered={dec.get('prompt_offered')}) -- correct "
            f"behavior")
    else:
        obs["assert"]["A3_no_return_no_counter"] = "failed"
        obs["notes"].append(
            f"A3 FAILED: decline branch: the Sphinx (oid {dec.get('sphinx_oid')}) "
            f"entered WITH {dec.get('counters')} +1/+1 counter(s) "
            f"(counters_raw={dec.get('counters_raw')}, P/T "
            f"{dec.get('power')}/{dec.get('toughness')}) although no "
            f"nonland permanent was returned "
            f"(prompt_offered={dec.get('prompt_offered')}, "
            f"may_accepted={dec.get('may_accepted')}) -- the reported "
            f"wrong-game-result")

    # ---- A4: accept leg (control) -- return a nonland permanent; the
    # Sphinx must enter with EXACTLY ONE +1/+1 counter.
    acc = next((x for x in ctx["casts"] if x["branch"] == "accept"), None)
    if acc is None or not acc.get("entered"):
        obs["assert"]["A4_accept_one_counter"] = "not-run"
        obs["notes"].append(
            "A4 not-run: accept-branch Sphinx never entered "
            f"(casts={obs['casts']})")
    elif not acc.get("prompt_offered"):
        obs["assert"]["A4_accept_one_counter"] = "not-run"
        obs["notes"].append(
            "A4 not-run: the 'you may return a nonland permanent' prompt "
            "was never offered (the optional gate is absent entirely), so "
            "the accept branch cannot be exercised; the accept-branch Sphinx "
            f"(oid {acc.get('sphinx_oid')}) entered with "
            f"{acc.get('counters')} +1/+1 counter(s)")
    elif acc.get("may_accepted") and acc.get("returned_oid") \
            and acc.get("counters") == 1:
        obs["assert"]["A4_accept_one_counter"] = "passed"
        obs["notes"].append(
            f"A4: accept branch: returned {acc.get('returned_name')} (oid "
            f"{acc.get('returned_oid')}); the Sphinx (oid "
            f"{acc.get('sphinx_oid')}) entered with exactly one +1/+1 "
            f"counter -- correct")
    else:
        obs["assert"]["A4_accept_one_counter"] = "failed"
        obs["notes"].append(
            f"A4 FAILED: accept branch misbehaved: may_accepted="
            f"{acc.get('may_accepted')} returned={acc.get('returned_name')} "
            f"counters={acc.get('counters')}")

    for branch in ("decline", "accept"):
        for which in ("pre", "post"):
            fn = f"{EVDIR}/{which}_cast_{branch}.json"
            try:
                with open(fn) as f:
                    json.load(f)
                obs["notes"].append(f"{which}_cast_{branch}.json captured and parses")
            except (OSError, ValueError):
                obs["notes"].append(f"{which}_cast_{branch}.json missing/unparseable")

    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    parse_ev = check_parse_condition()
    obs = None
    for attempt in range(6):
        obs = await run_attempt(attempt)
        a3 = obs["assert"].get("A3_no_return_no_counter")
        a4 = obs["assert"].get("A4_accept_one_counter")
        if a3 in ("passed", "failed") and a4 in ("passed", "failed", "not-run"):
            break
        if obs.get("fast_aborted"):
            say(f"[attempt {attempt}] fast-aborted (no Sphinx in opener); redealing")
            continue
        if any("uncastable" in n for n in obs["notes"]):
            say(f"[attempt {attempt}] uncastability confirmed; not redealing")
            break
        say(f"[attempt {attempt}] incomplete (A3={a3}, A4={a4}); retrying"
            if attempt < 5 else f"[attempt {attempt}] incomplete; giving up")

    ass = dict(obs["assert"])
    # A2 (decisive, parse-level): "failed" means the reported bug is present.
    if parse_ev["condition_dropped"]:
        ass["A2_parse_condition"] = "failed"
    elif parse_ev["card_reads_unsupported"]:
        ass["A2_parse_condition"] = "passed"
    else:
        ass["A2_parse_condition"] = "not-run"
    # A5: the card-data lower matches the reported shape
    ass["A5_data_shape"] = "passed" if (
        parse_ev["n_replacements"] == 1
        and parse_ev["effect_type"] == "PutCounter"
        and parse_ev["effect_counter_type"] == "P1P1"
        and parse_ev["effect_target"] == {"type": "SelfRef"}
        and parse_ev["oracle_has_if_you_do"]) else "failed"
    notes = list(obs["notes"])
    notes.append(
        f"parse: condition_dropped={parse_ev['condition_dropped']} "
        f"effect={parse_ev['effect_type']} mode={parse_ev['replacement_mode']} "
        f"exec_optional={parse_ev['execute_optional']} "
        f"exec_condition={parse_ev['execute_condition']} "
        f"swallowed={[ (w['type'], w['detector']) for w in parse_ev['swallowed_warnings']]}")
    notes.append(f"casts: {obs.get('casts')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; priority-gated passes; "
                 "sleep(0) yield before leg evaluation. The 'may return' "
                 "prompt (exactChoices decideOptionalEffect) is answered "
                 "DECLINE on the decline branch and ACCEPT on the accept "
                 "branch (with the branch's Merfolk as the return target); "
                 "any other prompt during a cast is recorded, never "
                 "auto-answered.")

    a2 = ass.get("A2_parse_condition")
    if parse_ev is None:
        verdict = "blocked"
    elif a2 == "failed":
        verdict = "reproduced"
    elif a2 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = the parse output drops the "
                 "'If you do' condition and the optional 'you may return a "
                 "nonland permanent' gate (PutCounter lowered "
                 "Mandatory/unconditional, condition null everywhere, "
                 "SwallowedClause warnings for Optional_YouMay and "
                 "Condition_If 'you do') -- the reported outcome. "
                 "not-reproduced = the condition is carried (or the card "
                 "reads unsupported).")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9853",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.108.0 server on 127.0.0.1:9374 "
                           "(pid 4038, --single-user --no-data-download, "
                           "started earlier today on the same verified "
                           "release; reused by this run); evidence under "
                           "evidence/9853/" + RUN_ID),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Island", 48], ["Merfolk of the Pearl Trident", 8],
                   ["Rescuer Sphinx", 4]],
            "P1": [["Island", 52], ["Merfolk of the Pearl Trident", 8]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "parse_evidence": parse_ev,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s"
                   % (ass.get("A1_setup_ok"), a2,
                      ass.get("A3_no_return_no_counter"),
                      ass.get("A4_accept_one_counter"),
                      ass.get("A5_data_shape"))),
        "scope": ("Rescuer Sphinx 'enters with a +1/+1 counter' gate: "
                  "parse-level presence of the 'If you do' condition and the "
                  "optional 'may return a nonland permanent' gate on the "
                  "PutCounter replacement, plus in-game confirmation that "
                  "the Sphinx enters with a +1/+1 counter even when nothing "
                  "was returned (decline leg) and that the accept branch "
                  "cannot be exercised because the prompt is never offered; "
                  "two-player game; native human seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The accept leg (A4) can only run if the engine offers the 'you "
            "may return' prompt; under the reported bug the gate is absent, "
            "so A4 is expected to be not-run.",
            "+1/+1-counter reads are a best-effort parse of the runtime "
            "object's counters payload; the verbatim payload plus P/T are "
            "preserved in wire_log.jsonl under sphinx_entered.",
            "The Sphinx permanent is located by its spell oid (object moved "
            "zones); if the engine re-issues the oid, the fallback is the "
            "newest P0 'rescuer sphinx' permanent, recorded in the wire log.",
        ],
        "setup_line": ("P0 48x Island + 8x Merfolk of the Pearl Trident "
                       "({U} 1/1) + 4x Rescuer Sphinx ({2}{U}{U} 3/2 flyer); "
                       "P1 52x Island + 8x Merfolk of the Pearl Trident"),
        "contract_line": ("Parse (decisive): Rescuer Sphinx's PutCounter "
                          "replacement must carry the 'If you do' condition "
                          "and the optional 'may return a nonland permanent' "
                          "gate, or the card must read unsupported. Game "
                          "(confirming): cast the Sphinx while controlling a "
                          "Merfolk; no return prompt is expected under the "
                          "bug. Decline leg: the Sphinx must enter with no "
                          "+1/+1 counter when nothing was returned (bug: it "
                          "enters with one). Accept leg (control): return "
                          "the Merfolk; the Sphinx must enter with exactly "
                          "one +1/+1 counter."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9853_01080.py", "w") as f:
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
    d.text((24, y), "Issue #9853 - Rescuer Sphinx: the 'If you do' condition",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "is dropped from the 'enters with a +1/+1 counter' gate",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_parse_condition": "A2 [bug] 'If you do' condition absent from lowering",
        "A3_no_return_no_counter": "A3 decline leg: counter added without a return (bug)",
        "A4_accept_one_counter": "A4 accept leg: exactly one counter after return",
        "A5_data_shape": "A5 card-data PutCounter lower matches reported shape",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:15]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9853/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9853.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9853 instance is alive "
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
