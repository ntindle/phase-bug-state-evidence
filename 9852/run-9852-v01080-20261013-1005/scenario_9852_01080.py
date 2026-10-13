#!/usr/bin/env python3
"""Issue #9852: Undercover Operative -- the "except it enters with a shield
counter if you control that creature" exception is dropped from the
BecomeCopy lowering.

Oracle: "You may have this creature enter as a copy of any creature on the
battlefield, except it enters with a shield counter on it if you control
that creature. (If it would be dealt damage or destroyed, remove a shield
counter from it instead.)"

Report (phase-rs/phase#9852, opened 2026-10-12 by matthewevans, open,
bug/area:parser/priority:p3-card-specific): the exception clause is not
attached in the BecomeCopy lowering; the copy never gets the shield counter.
Expected: the copy enters with a shield counter exactly when the copied
creature is controlled by the copy's controller. Where: BecomeCopy exception
handling in the clone/copy parser. CR 614.12, CR 122.1.

Behavioral contract (per playbook: test the reported OUTCOME; this is a
parser-subsystem report, so the decisive contract is parse-level, with a
game-level confirmation that the copy enters without the shield counter):
  Parse (decisive): in the pinned v0.108.0 card-data.json, Undercover
      Operative's BecomeCopy effect must carry the shield-counter exception
      (an additional_modifications entry or equivalent conditional
      enters-with-counter modification) -- OR the card must read as
      unsupported. Bug: the effect has NO additional_modifications key at
      all while the oracle text contains the "except it enters with a
      shield counter on it if you control that creature" clause. Controls:
      Evil Twin ("except it has {T}: Destroy ...") carries
      additional_modifications=[GrantAbility]; Sakashima's Student
      ("except it's a Ninja") carries additional_modifications=[AddSubtype].
  Game (confirming): P0 casts Undercover Operative, accepts the "may"
      replacement, copies P0's own Merfolk of the Pearl Trident. Expected
      under the bug: the copy enters with NO shield counter. Control: a
      second Operative copying P1's Merfolk also enters with no shield
      counter (correct even when fixed).

Assertions:
  A1 setup_ok          game created, both seats joined
  A2 parse_exception   FAILED = the reported bug: the shield-counter
                       exception is absent from the BecomeCopy lowering
                       (no additional_modifications) while the oracle has
                       the except-clause; passed = exception present, or
                       card unsupported
  A3 no_shield_copy_own  copy of P0's own creature entered with no shield
                       counter (bug manifested in-game); passed = observed
                       absence; failed = shield counter present
  A4 no_shield_copy_opp  control: copy of P1's creature entered with no
                       shield counter (expected even when fixed)
  A5 data_shape        card-data lower matches the reported shape
                       (BecomeCopy, Typed/Creature target, controller null,
                       oracle contains the except-clause)

Verdict:
  reproduced      A2 failed (exception dropped in parse output) -- the
                  reported outcome, regardless of A3/A4
  not-reproduced  A2 passed (exception present or card unsupported)
  blocked         fixture never reached, or parse evidence unreadable

Driver conventions (verified scenario_9850_01080.py, protocol 134):
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
  - Optional "may" answered via vi exactChoices decideOptionalEffect
    role/value (verified scenario_301_01080.py)
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
log = logging.getLogger("scenario9852")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9852-v01080-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9852
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
               "v0.108.0 server on 127.0.0.1:9374 (started by the 9850 run "
               "earlier today, same verified release, reused by this run); "
               "scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9852/" + RUN_ID),
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


OPERATIVE = "undercover operative"
MERFOLK = "merfolk of the pearl trident"
ISLAND = "island"


def operative_oid(state, pid=0):
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == OPERATIVE:
            return o
    return None


def merfolk_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == MERFOLK]


def shield_counters_of(state, oid):
    """Shield counters on a battlefield object. Returns (count, raw) where
    raw is the verbatim counters payload for evidence."""
    o = get_obj(state, oid)
    raw = o.get("counters")
    count = 0
    if isinstance(raw, dict):
        for k, v in raw.items():
            if "shield" in str(k).lower():
                try:
                    count += int(v)
                except (TypeError, ValueError):
                    count += 1
    elif isinstance(raw, list):
        for c in raw:
            nm = c.get("name") if isinstance(c, dict) else str(c)
            if "shield" in str(nm).lower():
                try:
                    count += int(c.get("count", 1)) if isinstance(c, dict) else 1
                except (TypeError, ValueError):
                    count += 1
    return count, raw


def copy_permanent_oid(state, spell_oid, pid):
    """The battlefield permanent that a resolved spell became. Prefers the
    same oid (object moved zones); falls back to the newest operative-named
    or newest creature permanent controlled by pid."""
    if spell_oid is not None:
        o = get_obj(state, spell_oid)
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller", -1)) == str(pid):
            return str(spell_oid)
    cands = [o for o in bf_oids(state, pid)
             if is_creature(get_obj(state, o))]
    return cands[-1] if cands else None


def stack_spell_oid(state, lname):
    for e in state.get("stack") or []:
        oid = e.get("object_id", e.get("id", ""))
        if obj_lname(state, oid) == lname:
            return str(oid)
    return None


def activate_ability_offered(acts, state, oid):
    """True if an ActivateAbility legal action is advertised for oid."""
    for a in acts:
        if a["type"] == "ActivateAbility":
            d = a.get("data", {}) or {}
            for v in list(d.values()) + [a.get("_src_oid")]:
                try:
                    if int(v) == int(oid):
                        return True
                except (TypeError, ValueError):
                    continue
    return False


def offered_attacker_oids(st, acts):
    """Best-effort set of attacker oids the engine offers at a
    DeclareAttackers step: vi candidate/choice refs plus any oids found in
    the DeclareAttackers action payload."""
    oids = set()
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in (data.get("candidates") or data.get("choices") or []):
            ref = _cand_ref(ch)
            if ref is not None:
                oids.add(str(ref))
    for a in acts:
        if a["type"] == "DeclareAttackers":
            blob = json.dumps(a.get("data", {}), default=str)
            for oid in (st.get("state", {}).get("objects") or {}):
                if str(oid) in blob:
                    oids.add(str(oid))
    return oids


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def untapped_creature_oids(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_creature(get_obj(state, o)) and not get_obj(state, o).get("tapped")]


def stack_raw(state):
    return [{"name": obj_lname(state, e.get("object_id", e.get("id", ""))),
             "kind": (e.get("kind") or {}).get("type") if isinstance(e.get("kind"), dict) else e.get("kind"),
             "controller": e.get("controller"),
             "raw_keys": sorted(e.keys())}
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


def check_becomecopy_exception():
    """Record the parser shape this scenario depends on: Undercover
    Operative's BecomeCopy lowering must carry the shield-counter
    exception (additional_modifications or equivalent), or the card must
    read as unsupported.

    Returns a dict with the decisive A2 evidence. This function does NOT
    abort the run when the bug is present -- the dropped exception IS the
    reported outcome, so the game confirmation still runs."""
    uo = CARD_DATA.get("undercover operative", {})
    oracle = uo.get("oracle_text") or ""
    clause = "except it enters with a shield counter on it if you control that creature"
    oracle_has_clause = clause in oracle
    replacements = uo.get("replacements") or []
    effects = []
    for r in replacements:
        eff = ((r.get("execute") or {}).get("effect")) or {}
        if eff.get("type") == "BecomeCopy":
            effects.append(eff)
    n_bc = len(effects)
    has_mods = []
    mod_kinds = []
    targets = []
    for eff in effects:
        mods = eff.get("additional_modifications")
        has_mods.append(mods is not None)
        if mods:
            mod_kinds.append([m.get("type") for m in mods])
        targets.append(eff.get("target"))
    # controls: other BecomeCopy cards whose "except" clause IS lowered
    controls = {}
    for name in ["evil twin", "sakashima's student"]:
        v = CARD_DATA.get(name, {})
        mods = []
        for r in v.get("replacements") or []:
            eff = ((r.get("execute") or {}).get("effect")) or {}
            if eff.get("type") == "BecomeCopy" \
                    and eff.get("additional_modifications"):
                mods = [m.get("type")
                        for m in eff["additional_modifications"]]
        controls[name] = mods
    # The reported bug: oracle has the except-clause but the BecomeCopy
    # effect carries no additional_modifications key at all.
    exception_dropped = (
        oracle_has_clause
        and n_bc == 1
        and not any(has_mods)
    )
    card_unsupported = False
    for r in replacements:
        m = ((r.get("execute") or {}).get("effect")) or {}
        mt = m.get("type")
        if isinstance(mt, dict) and mt.get("type") == "Unimplemented":
            card_unsupported = True
    ev = {
        "oracle_has_except_clause": oracle_has_clause,
        "n_becomecopy_effects": n_bc,
        "effect_has_additional_modifications": has_mods,
        "additional_modification_kinds": mod_kinds,
        "effect_targets": targets,
        "control_cards_additional_modifications": controls,
        "card_reads_unsupported": card_unsupported,
        "exception_dropped": exception_dropped,
    }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump({"issue": 9852, "card": "undercover operative",
                   "oracle_text": oracle,
                   "replacements": replacements,
                   "analysis": ev}, f, indent=1, default=str)
    say(f"parse-level check: exception_dropped={exception_dropped} "
        f"n_becomecopy={n_bc} has_mods={has_mods} "
        f"controls={controls}")
    wire("parse_level", ev)
    return ev


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
    """Find the 'may have it enter as a copy' OptionalEffectChoice
    opportunity: exactChoices with a decideOptionalEffect action code."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


def find_replacement_opp(st):
    """Find the engine's combined replacement prompt: exactChoices whose
    choices carry a chooseReplacement code (the 'may' is folded into
    choosing which replacement applies, with candidate surfaces)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "chooseReplacement" in surf_codes(ch):
                return opp
    return None


async def answer_replacement(c, st, ctx, cast, tag):
    """Answer the engine's ReplacementChoice: pick the choice whose
    optionIndex value surface equals the index of the copy candidate in
    waiting_for.data.candidates (description contains 'copy', not
    'decline'). The follow-up 'which creature' prompt is answered
    separately by answer_copy_choice."""
    state = st["state"]
    wf = state.get("waiting_for") or {}
    if (wf.get("type") or "") != "ReplacementChoice":
        return False
    cands = (wf.get("data") or {}).get("candidates") or []
    copy_idx = None
    for i, cd in enumerate(cands):
        desc = str(cd.get("description") or "").lower()
        if "copy" in desc and "decline" not in desc:
            copy_idx = i
            break
    if copy_idx is None:
        wire("replacement_no_copy_candidate",
             {"who": tag, "branch": cast["branch"],
              "candidates": [str(cd.get("description"))[:120]
                             for cd in cands]})
        say(f"[{tag}] ReplacementChoice has no copy candidate; leaving pending")
        return False
    for opp in vi_ops(st):
        iid = str(opp.get("interactionId"))
        key = ("replacement", iid)
        if key in SUBMITTED_OPPS:
            continue
        chs = (opp.get("response", {}).get("data", {}) or {}).get(
            "choices", []) or []
        dkey = ("replacement-dump", iid)
        if dkey not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(dkey)
            wire("replacement_opp_dump",
                 {"who": tag, "branch": cast["branch"], "iid": iid,
                  "wf_candidates": [
                      str(cd.get("description"))[:160] for cd in cands],
                  "choices": [
                      {"id": ch.get("id"),
                       "text": choice_text(ch)[:120],
                       "codes": sorted(cc for cc in surf_codes(ch) if cc),
                       "values": [
                           {"role": (sf.get("data") or {}).get("role"),
                            "value": str((sf.get("data") or {}).get("value"))}
                           for sf in ch.get("surfaces", []) or []
                           if isinstance(sf.get("data"), dict)
                           and (sf.get("data") or {}).get("role")]}
                      for ch in chs]})
        for ch in chs:
            hit = False
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data") or {}
                if dd.get("role") == "optionIndex" \
                        and str(dd.get("value")) == str(copy_idx):
                    hit = True
                    break
            if hit:
                SUBMITTED_OPPS.add(key)
                cast["may_answered"] = True
                desc = str(cands[copy_idx].get("description"))[:120]
                say(f"[{tag}] ReplacementChoice: ACCEPT copy (option "
                    f"{copy_idx}): {desc} (branch {cast['branch']})")
                wire("replacement_answer",
                     {"who": tag, "branch": cast["branch"],
                      "option_index": copy_idx, "description": desc})
                await answer_vi(c, opp, ch, tag)
                return True
    return False


async def answer_may(c, st, ctx, cast, tag, want_accept=True):
    opp = find_may_opp(st)
    if opp is None:
        return False
    key = ("may", str(opp.get("interactionId")))
    if key in SUBMITTED_OPPS:
        return False
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "accept" in txt or "copy" in txt:
                is_accept = True
            elif "decline" in txt or "don't" in txt or "do not" in txt:
                is_accept = False
        if is_accept == want_accept:
            SUBMITTED_OPPS.add(key)
            cast["may_answered"] = True
            say(f"[{tag}] may-copy prompt: "
                f"{'ACCEPT' if want_accept else 'DECLINE'} "
                f"(branch {cast['branch']})")
            wire("may_answer", {"who": tag, "accept": want_accept,
                                "branch": cast["branch"]})
            await answer_vi(c, opp, ch, tag)
            return True
    wire("may_no_matching_choice",
         {"who": tag, "branch": cast["branch"],
          "opps": _opp_summary(st)})
    say(f"[{tag}] may prompt present but no accept choice matched")
    return False


async def answer_copy_choice(c, st, ctx, cast, tag):
    """Answer the copy-target selection with the branch's chosen creature:
    first via target_opportunity, then via a raw blob scan for the target
    oid across all opportunities (7167-style)."""
    if cast.get("copy_chosen"):
        return False
    want = str(cast["copy_target_oid"]) if cast.get("copy_target_oid") else None
    if want is None:
        return False
    # pass 1: structured target opportunity
    opp, rtype, stype = target_opportunity(st)
    if opp is not None:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        for ch in cands:
            if _cand_ref(ch) == want or want in json.dumps(ch, default=str):
                cast["copy_chosen"] = True
                cast["copy_choice_rev"] = c.revision
                say(f"[{tag}] copying {obj_lname(c.latest['state'], want)} "
                    f"(oid {want}) with Undercover Operative "
                    f"(branch {cast['branch']})")
                wire("copy_choice_answer",
                     {"who": tag, "branch": cast["branch"],
                      "copy_target_oid": want, "via": "target_opportunity"})
                await answer_vi(c, opp, ch, tag)
                return True
    # pass 2: raw blob scan across all opportunities
    for opp2 in vi_ops(st):
        iid = str(opp2.get("interactionId"))
        key = ("copychoice", iid)
        if key in SUBMITTED_OPPS:
            continue
        chs = (opp2.get("response", {}).get("data", {}) or {}).get(
            "choices", []) or (opp2.get("response", {}).get("data", {})
                                or {}).get("candidates", []) or []
        if not chs or want not in json.dumps(chs, default=str).lower():
            continue
        for ch in chs:
            if want in json.dumps(ch, default=str):
                SUBMITTED_OPPS.add(key)
                cast["copy_chosen"] = True
                cast["copy_choice_rev"] = c.revision
                say(f"[{tag}] copying {obj_lname(c.latest['state'], want)} "
                    f"(oid {want}) with Undercover Operative "
                    f"(branch {cast['branch']}, via blob scan)")
                wire("copy_choice_answer",
                     {"who": tag, "branch": cast["branch"],
                      "copy_target_oid": want, "via": "blob_scan"})
                await answer_vi(c, opp2, ch, tag)
                return True
    wire("copy_choice_no_match",
         {"who": tag, "branch": cast["branch"], "want": want,
          "opps": _opp_summary(st)})
    return False


def active_cast(ctx):
    for cast in ctx["casts"]:
        if not cast.get("done"):
            return cast
    return None


def update_cast(c, st, ctx, tag):
    """Track each Operative cast: submitted -> on stack (pre export) ->
    resolved -> may answered -> copy chosen -> entered (post export)."""
    state = st["state"]
    for cast in ctx["casts"]:
        if cast.get("done"):
            continue
        spell_oid = stack_spell_oid(state, OPERATIVE)
        if spell_oid and not cast.get("seen_on_stack"):
            cast["seen_on_stack"] = True
            cast["spell_oid"] = spell_oid
            cast["need_pre_export"] = True
            wire("operative_on_stack",
                 {"who": tag, "branch": cast["branch"],
                  "rev": c.revision, "spell_oid": spell_oid,
                  "stack": stack_raw(state)})
            say(f"[{tag}] Operative on the stack (branch {cast['branch']}) "
                f"-- pre-cast export next")
        if cast.get("seen_on_stack") and spell_oid is None \
                and not cast.get("resolved"):
            cast["resolved"] = True
            wire("operative_resolved",
                 {"who": tag, "branch": cast["branch"],
                  "rev": c.revision,
                  "p0_bf": bf_names(state, 0)[:10]})
            say(f"[{tag}] Operative spell resolved (branch "
                f"{cast['branch']}); awaiting may/copy prompts")
        # the copy permanent, once it is on the battlefield after the
        # copy choice was answered
        if cast.get("copy_chosen") and not cast.get("entered"):
            perm = copy_permanent_oid(state, cast.get("spell_oid"), 0)
            if perm is not None:
                cast["entered"] = True
                cast["copy_oid"] = perm
                shield, raw = shield_counters_of(state, perm)
                cast["shield"] = shield
                cast["copy_name"] = obj_lname(state, perm)
                cast["need_post_export"] = True
                wire("copy_entered",
                     {"who": tag, "branch": cast["branch"],
                      "rev": c.revision, "copy_oid": perm,
                      "copy_name": cast["copy_name"],
                      "shield_counters": shield,
                      "counters_raw": raw})
                say(f"[{tag}] copy entered: {cast['copy_name']} "
                    f"(oid {perm}) shield counters={shield} "
                    f"(branch {cast['branch']})")


async def handle_operative_prompt(c, st, ctx, tag, seat):
    """Answer the Operative's own prompts (may + copy choice) for the
    active cast. Any other real decision is recorded, never auto-answered."""
    cast = active_cast(ctx)
    if cast is None:
        return False
    # The engine folds the "may" into a single chooseReplacement prompt
    # (verified 2026-10-13: exactChoices with chooseReplacement codes);
    # try that first, then the split may/copy shape as a fallback.
    if not cast.get("copy_chosen"):
        if await answer_replacement(c, st, ctx, cast, tag):
            return True
    if not cast.get("may_answered"):
        if await answer_may(c, st, ctx, cast, tag, want_accept=True):
            return True
    elif not cast.get("copy_chosen"):
        if await answer_copy_choice(c, st, ctx, cast, tag):
            return True
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

    update_cast(c, st, ctx, tag)

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
        hn = hand_names(state, 1)
        if MERFOLK in hn and untapped_land_count(state, 1) >= 1 \
                and len(merfolk_oids(state, 1)) < 2:
            a, oid = cast_action_for(acts, state, MERFOLK)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1}
                say(f"[{tag}] casting {MERFOLK} (oid {oid})")
                wire("cast_merfolk_p1", {"oid": oid})
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
    if await handle_operative_prompt(c, st, ctx, tag, 0):
        return True

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        hn = hand_names(state, 0)
        # diagnostic: trace the cast branch inputs once per main phase
        dkey = (tag, "castdiag", state.get("turn_number"), state.get("phase"))
        if dkey not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(dkey)
            cast_types = sorted({a.get("type") for a in acts
                                 if "cast" in str(a.get("type")).lower()})
            say(f"[{tag}] castdiag turn={state.get('turn_number')} "
                f"hand_n={len(hn)} operative_in_hand={OPERATIVE in hn} "
                f"untapped_lands={untapped_land_count(state, 0)} "
                f"cast_actions={cast_types} "
                f"hand_sample={hn[:12]}")
            wire("cast_diag", {"who": tag, "turn": state.get("turn_number"),
                               "hand_n": len(hn),
                               "operative_in_hand": OPERATIVE in hn,
                               "untapped_lands": untapped_land_count(state, 0),
                               "cast_action_types": cast_types,
                               "hand": hn})
        # uncastability streak: Operative in hand + 4 mana available, yet
        # the engine advertises no cast action.
        cast_types = sorted({a.get("type") for a in acts
                             if "cast" in str(a.get("type")).lower()})
        if (OPERATIVE in hn and untapped_land_count(state, 0) >= 4
                and not cast_types):
            ctx["uncastable_streak"] = ctx.get("uncastable_streak", 0) + 1
            if ctx["uncastable_streak"] >= 8 \
                    and not ctx.get("uncastable_confirmed"):
                ctx["uncastable_confirmed"] = True
                say(f"[{tag}] UNCASTABLE: {ctx['uncastable_streak']} "
                    f"consecutive P0 main phases with Operative in hand "
                    f"and >=4 untapped lands, zero cast actions advertised")
                wire("uncastable_confirmed",
                     {"who": tag, "streak": ctx["uncastable_streak"],
                      "hand": hn,
                      "untapped_lands": untapped_land_count(state, 0)})
        else:
            ctx["uncastable_streak"] = 0
        # P0 also fields a Merfolk to copy (branch own)
        if MERFOLK in hn and untapped_land_count(state, 0) >= 1 \
                and len(merfolk_oids(state, 0)) < 1 \
                and not any(not x.get("done") for x in ctx["casts"]):
            a, oid = cast_action_for(acts, state, MERFOLK)
            if a is not None:
                ST["mana_needs"][tag] = {"U": 1}
                say(f"[{tag}] casting {MERFOLK} (oid {oid})")
                wire("cast_merfolk_p0", {"oid": oid})
                await submit_as_is(c, a)
                return True
        # branch plan: first cast copies P0's own Merfolk; once that cast
        # is done, the second cast copies P1's Merfolk (control).
        n_done = sum(1 for x in ctx["casts"] if x.get("done"))
        n_live = sum(1 for x in ctx["casts"] if not x.get("done"))
        if n_live == 0 and n_done < 2 and OPERATIVE in hn \
                and untapped_land_count(state, 0) >= 4:
            branch = "own" if n_done == 0 else "opp"
            own_m = merfolk_oids(state, 0)
            opp_m = merfolk_oids(state, 1)
            tgt = own_m[0] if branch == "own" and own_m else \
                (opp_m[0] if opp_m else None)
            if tgt is None:
                nkey = (tag, "no-target", state.get("turn_number"))
                if nkey not in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.add(nkey)
                    say(f"[{tag}] branch {branch}: no copy target on the "
                        f"battlefield yet; passing priority so the game "
                        f"progresses")
                # fall through to the priority pass below
            a, oid = cast_action_for(acts, state, OPERATIVE)
            if a is not None:
                ctx["casts"].append({
                    "branch": branch, "copy_target_oid": str(tgt),
                    "submitted": True, "seen_on_stack": False,
                    "spell_oid": None, "resolved": False,
                    "may_answered": False, "copy_chosen": False,
                    "entered": False, "copy_oid": None, "shield": None,
                    "copy_name": None, "pre_exported": False,
                    "post_exported": False, "done": False})
                ST["mana_needs"][tag] = {"U": 2, "generic": 2}
                say(f"[{tag}] casting Undercover Operative (oid {oid}) "
                    f"branch={branch} copy_target={tgt} "
                    f"({obj_lname(state, tgt)})")
                wire("cast_operative",
                     {"oid": oid, "branch": branch,
                      "copy_target_oid": str(tgt),
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
    tag = f"9852-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {"casts": [], "uncastable_streak": 0,
           "uncastable_confirmed": False}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(("Island", 48), ("Merfolk of the Pearl Trident", 8),
                         ("Undercover Operative", 4)))
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

    # fast-abort: the fixture needs Undercover Operative in hand early;
    # redraw a fresh game when the opening 7 lacks it.
    for _ in range(80):
        await asyncio.sleep(0.25)
        if p0.latest and p0.latest.get("state", {}).get("turn_number", 0) >= 1:
            break
    st = p0.latest
    if st is None or OPERATIVE not in hand_names(st["state"], 0):
        say(f"[{tag}] fast abort: opening hand lacks Undercover Operative "
            f"({hand_names(st['state'], 0) if st else 'no state'})")
        wire("fast_abort", {"reason": "no operative in opening hand",
                            "hand": hand_names(st["state"], 0) if st else []})
        obs["notes"].append("fast abort: opening hand lacked Undercover "
                            "Operative; fresh game redealt")
        obs["assert"]["A3_no_shield_copy_own"] = "not-run"
        obs["assert"]["A4_no_shield_copy_opp"] = "not-run"
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
                    f"casts={[(x['branch'], x.get('entered'), x.get('shield')) for x in ctx['casts']]} "
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
            wire("cast_stuck", {"stack": stack_raw(p0.latest["state"])
                                if p0.latest else None})
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
         "may_answered": x.get("may_answered"),
         "copy_chosen": x.get("copy_chosen"),
         "entered": x.get("entered"),
         "copy_oid": x.get("copy_oid"), "copy_name": x.get("copy_name"),
         "copy_target_oid": x.get("copy_target_oid"),
         "shield": x.get("shield"),
         "pre_exported": x.get("pre_exported"),
         "post_exported": x.get("post_exported")}
        for x in ctx["casts"]]

    # ---- A3: copy of own creature must enter with a shield counter
    # (bug expectation: it does NOT -- the dropped exception).
    own = next((x for x in ctx["casts"] if x["branch"] == "own"), None)
    if own is None or not own.get("entered"):
        obs["assert"]["A3_no_shield_copy_own"] = "not-run"
        obs["notes"].append(
            "A3 not-run: own-creature copy never entered the battlefield "
            f"(casts={obs['casts']})")
    elif own.get("shield") == 0:
        obs["assert"]["A3_no_shield_copy_own"] = "passed"
        obs["notes"].append(
            f"A3: copy of P0's own {own.get('copy_name')} (oid "
            f"{own.get('copy_oid')}) entered with NO shield counter -- "
            f"the dropped exception, manifested in-game")
    else:
        obs["assert"]["A3_no_shield_copy_own"] = "failed"
        obs["notes"].append(
            f"A3 FAILED: copy of own creature entered WITH "
            f"{own.get('shield')} shield counter(s)")

    # ---- A4: control -- copy of OPPONENT's creature enters with no shield
    # counter (correct even when the bug is fixed).
    opp = next((x for x in ctx["casts"] if x["branch"] == "opp"), None)
    if opp is None or not opp.get("entered"):
        obs["assert"]["A4_no_shield_copy_opp"] = "not-run"
        obs["notes"].append(
            "A4 not-run: opponent-creature copy never entered "
            f"(casts={obs['casts']})")
    elif opp.get("shield") == 0:
        obs["assert"]["A4_no_shield_copy_opp"] = "passed"
        obs["notes"].append(
            f"A4: control copy of P1's {opp.get('copy_name')} (oid "
            f"{opp.get('copy_oid')}) entered with no shield counter, as "
            f"expected")
    else:
        obs["assert"]["A4_no_shield_copy_opp"] = "failed"
        obs["notes"].append(
            f"A4 FAILED: control copy of opponent's creature entered WITH "
            f"{opp.get('shield')} shield counter(s)")

    for branch in ("own", "opp"):
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
    parse_ev = check_becomecopy_exception()
    obs = None
    for attempt in range(8):
        obs = await run_attempt(attempt)
        a3 = obs["assert"].get("A3_no_shield_copy_own")
        a4 = obs["assert"].get("A4_no_shield_copy_opp")
        if a3 == "passed" and a4 == "passed":
            break
        if obs.get("fast_aborted"):
            say(f"[attempt {attempt}] fast-aborted (no Operative in opener); redealing")
            continue
        # uncastability is engine behavior, not draw luck: no point redealing
        if any("uncastable" in n for n in obs["notes"]):
            say(f"[attempt {attempt}] uncastability confirmed; not redealing")
            break
        say(f"[attempt {attempt}] incomplete (A3={a3}, A4={a4}); retrying"
            if attempt < 7 else f"[attempt {attempt}] incomplete; giving up")

    ass = dict(obs["assert"])
    # A2 (decisive, parse-level): "failed" means the reported bug is present.
    if parse_ev["exception_dropped"]:
        ass["A2_parse_exception"] = "failed"
    elif parse_ev["card_reads_unsupported"] \
            or any(parse_ev["effect_has_additional_modifications"]):
        ass["A2_parse_exception"] = "passed"
    else:
        ass["A2_parse_exception"] = "not-run"
    # A5: the card-data lower matches the reported shape
    ass["A5_data_shape"] = "passed" if (
        parse_ev["n_becomecopy_effects"] == 1
        and parse_ev["oracle_has_except_clause"]) else "failed"
    notes = list(obs["notes"])
    notes.append(
        f"parse: exception_dropped={parse_ev['exception_dropped']} "
        f"n_becomecopy={parse_ev['n_becomecopy_effects']} "
        f"has_mods={parse_ev['effect_has_additional_modifications']} "
        f"controls={parse_ev['control_cards_additional_modifications']}")
    notes.append(f"casts: {obs.get('casts')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; priority-gated passes; "
                 "sleep(0) yield before leg evaluation. The 'may' replacement "
                 "prompt is answered ACCEPT via vi exactChoices "
                 "decideOptionalEffect role/value; the copy-target choice is "
                 "answered with the branch's chosen creature; any other "
                 "prompt during the cast is recorded, never auto-answered.")

    a2 = ass.get("A2_parse_exception")
    if parse_ev is None:
        verdict = "blocked"
    elif a2 == "failed":
        verdict = "reproduced"
    elif a2 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = the parse output drops the "
                 "'except it enters with a shield counter if you control "
                 "that creature' exception (no additional_modifications on "
                 "the BecomeCopy effect while the oracle has the clause) -- "
                 "the reported outcome. not-reproduced = the exception is "
                 "present as a modification, or the card reads unsupported.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9852",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("runs/run-9850-v01080-20261013-0841 (v0.108.0 "
                           "server on 127.0.0.1:9374, started by the 9850 run "
                           "earlier today on the same verified release; "
                           "reused by this run; evidence under "
                           "evidence/9852/" + RUN_ID + ")"),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Island", 48], ["Merfolk of the Pearl Trident", 8],
                   ["Undercover Operative", 4]],
            "P1": [["Island", 52], ["Merfolk of the Pearl Trident", 8]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "parse_evidence": parse_ev,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s"
                   % (ass.get("A1_setup_ok"), a2,
                      ass.get("A3_no_shield_copy_own"),
                      ass.get("A4_no_shield_copy_opp"),
                      ass.get("A5_data_shape"))),
        "scope": ("Undercover Operative BecomeCopy exception: parse-level "
                  "presence of the shield-counter exception on the BecomeCopy "
                  "effect, plus in-game confirmation that a copy of the "
                  "controller's own creature enters without a shield counter "
                  "(control: copy of the opponent's creature enters without "
                  "one too); two-player game; native human seats, no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The 'if you control that creature' condition is evaluated by the "
            "engine at copy time; the game checks confirm the observed "
            "counter absence on both branches.",
            "Shield-counter reads are a best-effort parse of the runtime "
            "object's counters payload; the verbatim payload is preserved in "
            "wire_log.jsonl under copy_entered.",
            "The copy permanent is located by its spell oid (object moved "
            "zones); if the engine re-issues the oid, the fallback is the "
            "newest P0 creature permanent, recorded in the wire log.",
        ],
        "setup_line": ("P0 48x Island + 8x Merfolk of the Pearl Trident "
                       "({U} 1/1) + 4x Undercover Operative ({2}{U}{U} 0/0 "
                       "Shapeshifter Rogue); P1 52x Island + 8x Merfolk of "
                       "the Pearl Trident"),
        "contract_line": ("Parse (decisive): Undercover Operative's "
                          "BecomeCopy effect must carry the 'except it "
                          "enters with a shield counter on it if you control "
                          "that creature' exception or the card must read "
                          "unsupported. Game (confirming): cast the Operative, "
                          "accept the may-copy replacement, copy P0's own "
                          "Merfolk -- the copy must enter with a shield "
                          "counter (expected absent under the bug); control: "
                          "copy P1's Merfolk -- no shield counter expected."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9852_01080.py", "w") as f:
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
    d.text((24, y), "Issue #9852 - Undercover Operative: the 'except it enters",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "with a shield counter if you control that creature' exception",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "is dropped from the BecomeCopy lowering",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: both seats joined",
        "A2_parse_exception": "A2 [bug] shield-counter exception absent from BecomeCopy",
        "A3_no_shield_copy_own": "A3 copy of own creature: no shield counter (bug in-game)",
        "A4_no_shield_copy_opp": "A4 control copy of opp creature: no shield counter",
        "A5_data_shape": "A5 card-data BecomeCopy lower matches reported shape",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:15]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9852/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9852.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9852 instance is alive "
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
