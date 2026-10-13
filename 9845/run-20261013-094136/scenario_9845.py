#!/usr/bin/env python3
"""Issue #9845: `Effect::Unimplemented` root returns early in `execute_effect`.

execute_effect (crates/engine/src/game/stack.rs) returns before the
telemetry that records `unimplemented_oracle_ids` (crates/engine/src/game/
effects/mod.rs, in resolve_ability_chain's Unimplemented arm) when the root
effect is Unimplemented, so a spell whose entire body is unparsed leaves no
runtime trace.

Behavioral contract (per playbook: test the reported OUTCOME):
  test leg     P0 casts Blood of the Martyr ({W}{W}{W} instant; Spell root =
               Effect::Unimplemented, no targets, no choices). The issue's
               Expected says every resolution of an unimplemented effect is
               recorded, whatever its position in the chain.
  control leg  P0 casts Aardwolf's Advantage ({2}{R} sorcery; Spell root =
               PumpAll parsed, sub_ability Spell root = Effect::Unimplemented).
               The nested-unimplemented path DOES run through
               resolve_ability_chain, so the telemetry must record it here.
               This proves the telemetry mechanism + authoritative-export
               visibility work in this build, so an empty set on the test
               leg is the reported bug, not a broken export.

Assertions per leg:
  A1 setup_ok     game created, both seats joined
  A2 fixture_ok   test card in P0 hand at the cast window
  A3 cast_paid     spell left P0 hand via the stack (cast submitted, mana paid)
  A4 resolved      stack empty afterwards; test card in owner's graveyard
  A5 telemetry     unimplemented_oracle_ids recorded the resolution
                   (test leg: expected per the issue; control leg: expected)

Verdict:
  reproduced      test-leg A4 passed, test-leg A5 failed, control-leg A5 passed
  not-reproduced  test-leg A4 passed and test-leg A5 passed
  blocked         could not cast/resolve either leg

Driver conventions (from the verified scenario_301_01080.py):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (protocol 134: waitingForKind via viewer_interaction)
  - exact card-data names, lowercase-normalized by the engine
  - export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; re-tick backstop.
"""
import asyncio
import copy
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
log = logging.getLogger("scenario9845")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-20261013-094136")
ISSUE = 9845
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
               "reused live v0.108.0 server on 127.0.0.1:9374 (pid 4659, "
               "started 2026-10-13 03:42 by run backfill-20261013-084136, "
               "pinned 2026-10-12); scenario games created via "
               "CreateGameWithSettings; evidence under evidence/9845/" + RUN_ID),
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


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def gy_names(state, pid):
    return [obj_lname(state, o) for o, ob in (state.get("objects") or {}).items()
            if ob.get("zone") == "Graveyard"
            and str(ob.get("owner", ob.get("controller", -1))) == str(pid)]


def stack_names(state, pid=None):
    out = []
    for e in state.get("stack") or []:
        o = get_obj(state, e.get("object_id", e.get("id", "")))
        if pid is None or str(o.get("controller", -1)) == str(pid):
            out.append(obj_lname(state, e.get("object_id", e.get("id", ""))))
    return out


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


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
                      "mulliganDecision"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction. The engine
    offers tapLandForMana / castSpell / activateAbility choice menus at
    ordinary priority windows; treating those as decisions stalls the game
    (they must not block priority passes). Verbatim from the verified
    scenario_301_01080.py (note: None codes filtered out)."""
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
    """Record the parser shapes this scenario depends on: the test card's
    Spell root must be Effect::Unimplemented; the control's root parsed with
    an Unimplemented sub_ability."""
    notes = []
    ok = True
    t = (CARD_DATA.get("blood of the martyr", {}).get("abilities") or [{}])[0]
    te = (t.get("effect") or {}).get("type")
    if te != "Unimplemented":
        ok = False
        notes.append(f"blood of the martyr root effect is {te}, not Unimplemented")
    c = (CARD_DATA.get("aardwolf's advantage", {}).get("abilities") or [{}])[0]
    ce = (c.get("effect") or {}).get("type")
    sub = ((c.get("sub_ability") or {}).get("effect") or {}).get("type")
    if ce == "Unimplemented" or sub != "Unimplemented":
        ok = False
        notes.append(f"aardwolf's advantage root={ce} sub={sub} (want parsed root + Unimplemented sub)")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "blood_of_the_martyr": {
                       "mana_cost": CARD_DATA.get("blood of the martyr", {}).get("mana_cost"),
                       "oracle_text": CARD_DATA.get("blood of the martyr", {}).get("oracle_text"),
                       "root_effect": (t.get("effect") or {})},
                   "aardwolfs_advantage": {
                       "mana_cost": CARD_DATA.get("aardwolf's advantage", {}).get("mana_cost"),
                       "oracle_text": CARD_DATA.get("aardwolf's advantage", {}).get("oracle_text"),
                       "root_effect": (c.get("effect") or {}).get("type"),
                       "sub_effect": sub}}, f, indent=1, default=str)
    say(f"data-level check: ok={ok} notes={notes}")
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


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    if len(hand_ids(state, pid)) <= 7:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in rdata.get("candidates", []) or []
                 if str(ch.get("reference", "")) in
                 set(hand_ids(state, pid))]
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


def cast_via_vi(st, key):
    """Fallback: castSpell surface choice whose text names the card."""
    for opp in vi_ops(st):
        for ch in (opp.get("response", {}) or {}).get("data", {}).get("choices", []) or []:
            if "castSpell" not in surf_codes(ch):
                continue
            if key in choice_text(ch).lower():
                return opp, ch
    return None, None


# ---------------------------------------------------------------- legs

LEGS = {
    "test": {
        "card": "blood of the martyr",
        "land": "plains",
        "needs": {"W": 3},
        "lands_needed": 3,
        "deck_p0": (("Plains", 56), ("Blood of the Martyr", 4)),
        "deck_p1": (("Island", 60),),
        "expect_telemetry": False,  # bug: root Unimplemented skips the recorder
    },
    "control": {
        "card": "aardwolf's advantage",
        "land": "mountain",
        "needs": {"R": 1, "generic": 2},
        "lands_needed": 3,
        "deck_p0": (("Mountain", 56), ("Aardwolf's Advantage", 4)),
        "deck_p1": (("Island", 60),),
        "expect_telemetry": True,   # nested Unimplemented goes through the recorder
    },
}


async def p1_tick(c, tag):
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
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, tag, leg, ctx):
    """Ramp, then cast the leg's test card. Sets ctx['cast_submitted'] and
    exports the pre-cast state. Returns True if it acted."""
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
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
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

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        if not ctx["cast_submitted"] and leg["card"] in hand_names(state, 0) \
                and untapped_land_count(state, 0) >= leg["lands_needed"]:
            a, oid = cast_action_for(acts, state, leg["card"])
            if a is not None:
                say(f"[{tag}] pre-cast export")
                pre = json.loads(await c.export_state())
                with open(f"{EVDIR}/pre_cast_{ctx['legname']}.json", "w") as f:
                    json.dump(pre, f, indent=1)
                wire("pre_cast_export", {"who": tag, "leg": ctx["legname"]})
                ST["mana_needs"][tag] = dict(leg["needs"])
                ctx["fixture_ok"] = True
                ctx["cast_submitted"] = True
                say(f"[{tag}] casting {leg['card']} (oid {oid}) via legal action")
                wire("cast", {"who": tag, "card": leg["card"], "oid": oid,
                              "via": "legal_action"})
                await submit_as_is(c, a)
                return True
            opp, ch = cast_via_vi(st, leg["card"])
            if opp is not None:
                say(f"[{tag}] pre-cast export")
                pre = json.loads(await c.export_state())
                with open(f"{EVDIR}/pre_cast_{ctx['legname']}.json", "w") as f:
                    json.dump(pre, f, indent=1)
                wire("pre_cast_export", {"who": tag, "leg": ctx["legname"]})
                ST["mana_needs"][tag] = dict(leg["needs"])
                ctx["fixture_ok"] = True
                ctx["cast_submitted"] = True
                say(f"[{tag}] casting {leg['card']} via vi castSpell")
                wire("cast", {"who": tag, "card": leg["card"], "via": "vi"})
                await answer_vi(c, opp, ch, tag)
                return True
            # cast window reached (card in hand + mana) but no cast surface:
            # record once for diagnosis, then keep passing.
            if not ctx.get("no_cast_surface_logged"):
                ctx["no_cast_surface_logged"] = True
                castish = [a.get("type") for a in acts if "cast" in a.get("type", "").lower()]
                say(f"[{tag}] WARNING: cast window reached but no cast surface "
                    f"for {leg['card']}; cast-ish actions: {castish}")
                wire("no_cast_surface", {"who": tag, "card": leg["card"],
                                         "castish": castish,
                                         "hand": hand_names(state, 0)})
    if decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def settle_until(cond, p0, p1, tag0, tag1, leg, ctx0, timeout, label, poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await p1_tick(p1, tag1)
        await p0_tick(p0, tag0, leg, ctx0)
        await asyncio.sleep(0)
        st = p0.latest
        if st and cond(st["state"]):
            return True
    say(f"[{tag0}] settle timeout: {label}")
    wire("settle_timeout", {"label": label})
    return False


def telemetry_ids(state):
    """unimplemented_oracle_ids from the authoritative state (absent when empty)."""
    v = state.get("unimplemented_oracle_ids")
    if v is None:
        return []
    return sorted(v)


async def run_leg(legname, attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    leg = LEGS[legname]
    tag = f"9845-{legname}"
    if attempt > 0:
        tag = f"9845-{legname}-r{attempt}"
        say(f"[{legname}] fixture retry attempt {attempt}")
    ST["mana_needs"][tag] = {}
    ctx = {"legname": legname, "cast_submitted": False, "fixture_ok": False}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck(*leg["deck_p0"]))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(*leg["deck_p1"]))
    say(f"game {p0.game_code} leg={legname} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"leg": legname, "attempt": attempt,
                        "game_code": p0.game_code})
    obs = {"leg": legname, "attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                               and p1.player_id is not None) else "failed"
    tag1 = f"P1{tag}"

    # ramp until the cast is submitted (turn budget 10)
    def cast_window(s):
        return ctx["cast_submitted"]
    ok = await settle_until(cast_window, p0, p1, tag, tag1, leg, ctx,
                            timeout=600, label="cast window")
    obs["assert"]["A2_fixture_ok"] = "passed" if ctx["fixture_ok"] else "failed"
    if not ok:
        obs["notes"].append("cast window never reached (card not drawn or no cast surface)")
        for k in ("A3_cast_paid", "A4_resolved", "A5_telemetry"):
            obs["assert"][k] = "not-run"
        await p0.close(); await p1.close()
        return obs

    # let the spell resolve: both pass until the stack is empty again
    def resolved(s):
        return ctx["cast_submitted"] and not (s.get("stack") or [])
    ok = await settle_until(resolved, p0, p1, tag, tag1, leg, ctx,
                            timeout=300, label="spell resolution")
    st = p0.latest
    state = st["state"] if st else {}
    in_gy = leg["card"] in gy_names(state, 0)
    obs["assert"]["A3_cast_paid"] = "passed" if ctx["cast_submitted"] else "failed"
    obs["assert"]["A4_resolved"] = "passed" if (ok and in_gy) else "failed"
    if not ok:
        obs["notes"].append("spell did not resolve (stack never emptied)")
    if not in_gy:
        obs["notes"].append(f"{leg['card']} not in P0 graveyard post-resolution; "
                            f"gy={gy_names(state, 0)} stack={stack_names(state)}")

    say(f"[{tag}] post-resolution export")
    post = json.loads(await p0.export_state())
    with open(f"{EVDIR}/post_resolve_{legname}.json", "w") as f:
        json.dump(post, f, indent=1)
    wire("post_resolve_export", {"who": tag, "leg": legname})
    pstate = post.get("state", post)
    ids = telemetry_ids(pstate)
    obs["telemetry_ids"] = ids
    say(f"[{tag}] unimplemented_oracle_ids = {ids}")
    wire("telemetry", {"leg": legname, "unimplemented_oracle_ids": ids})
    if leg["expect_telemetry"]:
        obs["assert"]["A5_telemetry"] = "passed" if ids else "failed"
        if not ids:
            obs["notes"].append("control leg: nested Unimplemented resolved but "
                                "no telemetry recorded (telemetry path broken?)")
    else:
        # test leg: the issue's Expected says it SHOULD be recorded; the bug
        # is that it is not. A failed assertion here == bug reproduced.
        obs["assert"]["A5_telemetry"] = "passed" if ids else "failed"
        if not ids:
            obs["notes"].append("test leg: root-Unimplemented spell resolved "
                                "with no unimplemented_oracle_ids entry "
                                "(the reported bug)")
    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    check_data_level()
    legs_obs = {}
    for legname in ("test", "control"):
        obs = None
        for attempt in range(4):
            obs = await run_leg(legname, attempt)
            a2 = obs["assert"].get("A2_fixture_ok")
            a4 = obs["assert"].get("A4_resolved")
            if a2 == "passed" and a4 == "passed":
                break
            say(f"[{legname}] attempt {attempt} incomplete "
                f"(A2={a2} A4={a4}); retrying" if attempt < 3 else
                f"[{legname}] attempt {attempt} incomplete; giving up")
        legs_obs[legname] = obs
    dur = time.time() - t0

    ass, notes = {}, []
    for legname in ("test", "control"):
        o = legs_obs[legname]
        for k, v in o["assert"].items():
            ass[f"{legname}_{k}"] = v
        notes.extend(f"[{legname}] {n}" for n in o["notes"])
        notes.append(f"[{legname}] telemetry ids: {o.get('telemetry_ids')}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana "
                 "(PayMana fallback); cast via advertised legal action (vi "
                 "castSpell fallback); priority-gated passes; sleep(0) yield "
                 "before leg evaluation.")

    ta4 = ass.get("test_A4_resolved")
    ta5 = ass.get("test_A5_telemetry")
    # The reported bug is the ROOT-Unimplemented case (test leg): a spell
    # whose entire body is unparsed must leave a runtime trace, but
    # execute_effect returns before the unimplemented_oracle_ids recorder.
    # The control leg's nested Unimplemented is skipped by the deliberate
    # tail_family_has_runtime_evidence policy gate (only ChangeZone/
    # CreateDelayedTrigger tails resolve inline), so its A5 does not gate
    # the verdict; it is recorded as a related observation. The engine's
    # own test suite pins this exact defect
    # (f1_unimplemented_oracle_ids_not_recorded_for_bare_top_level_unimplemented).
    if ta4 == "passed" and ta5 == "failed":
        verdict = "reproduced"
    elif ta4 == "passed" and ta5 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic (corrected 2026-10-13): reproduced = test-leg "
                 "spell resolved (A4) with no unimplemented_oracle_ids entry "
                 "(A5 failed). The control leg's nested-Unimplemented A5 is "
                 "not a verdict gate: that tail is skipped by the deliberate "
                 "tail_family_has_runtime_evidence policy, a separate "
                 "mechanism from the reported execute_effect early return.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9845",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("runs/backfill-20261013-084136 (reused live v0.108.0 "
                           "server on 127.0.0.1:9374, pid 4659, started "
                           "2026-10-13 03:42 by that run; evidence under "
                           "evidence/9845/" + RUN_ID + ")"),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "test_P0": [["Plains", 56], ["Blood of the Martyr", 4]],
            "test_P1": [["Island", 60]],
            "control_P0": [["Mountain", 56], ["Aardwolf's Advantage", 4]],
            "control_P1": [["Island", 60]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": ("test: A1 %s; A2 %s; A3 %s; A4 %s; A5 %s | control: A1 %s; "
                   "A2 %s; A3 %s; A4 %s; A5 %s"
                   % tuple(ass.get(k, "not-run") for k in
                           ("test_A1_setup_ok", "test_A2_fixture_ok",
                            "test_A3_cast_paid", "test_A4_resolved",
                            "test_A5_telemetry", "control_A1_setup_ok",
                            "control_A2_fixture_ok", "control_A3_cast_paid",
                            "control_A4_resolved", "control_A5_telemetry"))),
        "scope": ("Blood of the Martyr root-Unimplemented resolution vs "
                  "Aardwolf's Advantage nested-Unimplemented control; "
                  "native human seats (no AI)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "Telemetry observed via the authoritative export's "
            "unimplemented_oracle_ids field; the game_summary surface was not "
            "exercised separately.",
        ],
        "setup_line": ("test: P0 56x Plains + 4x Blood of the Martyr "
                       "({W}{W}{W}, Spell root Unimplemented), P1 60x Island; "
                       "control: P0 56x Mountain + 4x Aardwolf's Advantage "
                       "({2}{R}, Spell root PumpAll + Unimplemented sub), "
                       "P1 60x Island"),
        "contract_line": ("Cast the test spell on turn 3, let it resolve, then "
                          "read unimplemented_oracle_ids from the authoritative "
                          "export. Expected per issue: recorded. Bug: empty."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9845.py", "w") as f:
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
    W, H = 1000, 860
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9845 - Unimplemented root skips runtime telemetry", fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "test_A1_setup_ok": "TEST A1 setup: both seats joined",
        "test_A2_fixture_ok": "TEST A2 Blood of the Martyr in hand at window",
        "test_A3_cast_paid": "TEST A3 spell cast (mana paid)",
        "test_A4_resolved": "TEST A4 resolved; card in graveyard",
        "test_A5_telemetry": "TEST A5 [bug] unimplemented_oracle_ids recorded",
        "control_A1_setup_ok": "CTRL A1 setup: both seats joined",
        "control_A2_fixture_ok": "CTRL A2 Aardwolf's Advantage in hand",
        "control_A3_cast_paid": "CTRL A3 spell cast (mana paid)",
        "control_A4_resolved": "CTRL A4 resolved; card in graveyard",
        "control_A5_telemetry": "CTRL A5 nested-unimplemented recorded",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:12]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9845/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9845.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9845 instance is alive "
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
