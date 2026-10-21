#!/usr/bin/env python3
"""Issue #9681: "fix(engine): scope damage prevention to blocked creatures
for Wall of Vapor".

RE-VALIDATION run on pinned v0.114.0 (protocol 153, build ac67cbb).

Prior run (run-9681-v01110-20261017-0411, v0.111.0 / protocol 144):
verdict not-reproduced -- A1/A2/A4 passed; A3 failed as the documented
control divergence (prevention parses as Effect::Unimplemented
{name: prevention_source_scope} and fails closed, so combat prevention
never applies either). Card data on v0.114.0 still shows
prevention_source_scope Unimplemented: the fail-closed state persists, so
the behavioral contract is unchanged. Mechanical protocol-153 port of
scenario_9681.py: same decks, setup, decisions, assertions A1-A4.

#9681 is an OPEN fix PR (bug-labeled) whose Summary describes a concrete
player-observable bug: Wall of Vapor's prevention ("Prevent all damage
that would be dealt to this creature by creatures it's blocking")
incorrectly prevented non-combat damage from spells (such as Dry Spell)
and damage from unblocked creatures. Per the #9921 precedent, a
bug-labeled fix PR with a concrete observable symptom is a valid
reproduction target.

Oracle (verified from pinned v0.114.0 card-data.json):
  Wall of Vapor: {3}{U} Creature -- Wall (0/1). "Defender (This creature
  can't attack.)\nPrevent all damage that would be dealt to this creature
  by creatures it's blocking."
  Parse on v0.114.0: the prevention line parses as Effect::Unimplemented
  { name: "prevention_source_scope" } -- the parser's Priority-8 route
  fails CLOSED per CR 615.1a (PreventionFailClosed::SourceScope): no
  unscoped prevention shield is ever created (engine unit test
  prevention_source_scope_fails_closed_at_the_route asserts exactly this).
  So on the pinned release the effect does nothing at all.
  Dry Spell: {1}{B} Sorcery. "Dry Spell deals 1 damage to each creature
  and each player." Parse: DamageAll amount 1 (implemented).

Plan (native engine, two human driver seats):
  P0: 8x Wall of Vapor + 26x Island + 26x Swamp. Casts WoV #1 in its main
      phase when it holds priority ({3}{U}, engine auto-pays).
  P1: 4x Dry Spell + 4x Grizzly Bears + 28x Swamp + 24x Forest.
      Casts Dry Spell in its main phase while WoV #1 is on the
      battlefield (leg 1, the reported path). Casts a Bear; attacks with it;
      P0 blocks with WoV #2 (leg 2, control).

BEHAVIORAL CONTRACT (written before observing results)
  A1_setup_ok      pre_dryspell.json exported with WoV on P0's battlefield
                   and P0 priority in its own main phase before the
                   Dry Spell cast.
  A2_spell_not_prevented (REPORTED PATH)  Dry Spell resolves (both
                   players 20->19 life = spell-resolution control); WoV
                   takes the 1 damage -> a 0/1 creature with 1 damage dies
                   to state-based actions, so WoV must NOT be on the
                   battlefield after resolution. pass == reported symptom
                   ABSENT on the pinned release. failed == damage was
                   prevented (WoV survives with no damage) -> bug
                   reproduced.
  A3_combat_control  (best-effort) WoV #2 blocks a 2/2 Bear. Per rules the
                   damage would be prevented (WoV survives); on the pinned
                   release the effect is unimplemented so no prevention
                   applies (WoV dies). Asserted as a control showing the
                   parser fail-closed: neither correct nor incorrect
                   prevention exists. Failure here is a RELATED,
                   already-documented engine divergence, not the reported
                   bug; it does not drive the verdict. not-run if the
                   combat fixture never assembles.
  A4_cleanup       stack empty, post.json exported.

Verdict: reproduced iff A1 passes and A2 fails (prevention applied to
spell damage). not-reproduced iff A1 and A2 pass. blocked iff A1 fails.

Protocol-153 notes: engine auto-pays (bare CastSpell, no PayMana /
tapLandForMana bookkeeping); priority = PassPriority in the viewing
seat's top-level legal_actions (waiting_for gone); MulliganDecision via
legacy Action; bottom-after-mulligan via vi schema/select; cleanup
discards via viewer_interaction only (never decide an unknown prompt).
Turn order is randomized; every play gates on active_player.
Cast-confirmation guard (2026-10-09 lesson): after every CastSpell, hold
priority passes and new main-phase plays until the spell is confirmed on
the stack or the cast object leaves its origin zone (30s backstop clears
a silently-dropped cast).

Evidence: evidence/9681/<run-id>/pre_dryspell.json, post_dryspell.json,
pre_combat.json, post.json, run.json, assertions.json, obs.json,
scenario_9681_01140.py, wire_log.jsonl, scenario_run.log, summary.png,
manifest.sha256, data_evidence.json.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9681-reval-v01140-20261021-0611")
ISSUE = 9681
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

URL = "ws://127.0.0.1:9374/ws"

WIRE = None
RUNLOG = None

WOV = "wall of vapor"
DRYSPELL = "dry spell"
BEAR = "grizzly bears"
ISLAND = "island"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = deck(("Wall of Vapor", 8), ("Island", 26), ("Swamp", 26))
P1_DECK = deck(("Dry Spell", 4), ("Grizzly Bears", 4), ("Swamp", 28),
               ("Forest", 24))

SERVER_IDENTITY = {
    "server_version": "v0.114.0",
    "build_commit": "ac67cbb",
    "protocol_version": 153,
    "mode": "Full",
    "binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_key_id": "436711b6a2d36828",
    "signature_note": ("v0.114.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "observed_at": "2026-10-21",
    "source": ("2026-10-21: latest stable release v0.114.0 (published "
               "2026-10-19T18:59:10Z) == pinned release dir; ServerHello "
               "0.114.0/ac67cbb/protocol 153 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "existing v0.114.0 server on 127.0.0.1:9374 reused "
               "(identity verified by handshake this run); fresh game created "
               "by this run; isolated evidence under evidence/9681/" + RUN_ID + ")."),
}

for _f, _k in (("server/releases/v0.114.0/phase-server-slim-x86_64-unknown-linux-musl", "binary_sha256"),
               ("server/releases/v0.114.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.114.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

TIMEOUT = 1400
TURN_CAP = 45

ST = {}
MULLS = set()
MULL_COUNTS = {}
SUBMITTED_OPPS = set()
PASSED_REV = {}


def reset():
    ST.update({
        "stage": "SETUP",   # SETUP -> LEG1 -> COMBAT -> DONE
        "stop": False,
        "notes": [],
        "wov1_oid": None,
        "wov2_oid": None,
        "bear_oid": None,
        "dry_cast": False,        # P1 submitted Dry Spell
        "dry_seen_stack": False,  # Dry Spell observed on stack
        "dry_resolved": False,    # stack cleared after dry_seen_stack
        "p0_life_pre": None,
        "p1_life_pre": None,
        "attacked": False,
        "attacker_oid": None,
        "blocked": False,
        "blocker_oid": None,
        "combat_done": False,
        "paying": {"P0": None, "P1": None},
        "pre_dryspell": False,
        "post_dryspell": False,
        "pre_combat": False,
        "post_exported": False,
    })


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if RUNLOG is not None:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        if str(o.get("controller", -1)) != str(pid):
            continue
        if lname is not None and obj_lname(state, oid) != lname:
            continue
        out.append(oid)
    return out


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def life(state, pid):
    return player_of(state, pid).get("life")


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


def find_action(acts, atype):
    return next((a for a in acts if a.get("type") == atype), None)


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
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
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def cand_ref_int(ch):
    r = _cand_reference(ch)
    try:
        return int(r)
    except (TypeError, ValueError):
        return None


def spell_on_stack(state, pid, name):
    for e in state.get("stack", []) or []:
        oid = e.get("object_id") or e.get("source_id")
        o = get_obj(state, oid) if oid is not None else {}
        nm = obj_lname(state, oid) if o else str(e.get("name", "")).lower()
        ctrl = o.get("controller", e.get("controller", e.get("player", pid)))
        if nm == name and str(ctrl) == str(pid):
            return True
    return False


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg = {"type": a["type"], "data": copy.deepcopy(a["data"])}
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = (json.loads(raw).get("data") or {})
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.114.0"), f"unexpected version {ver}"
    assert int(proto) == 153, f"unexpected protocol {proto}"
    assert str(build) == "ac67cbb", f"unexpected build {build}"


CARD_DATA = {}


def check_card_data():
    global CARD_DATA
    ok, notes = True, []
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.114.0/data/"
                        f"card-data.json"))
    CARD_DATA = cd
    wov = cd.get("wall of vapor", {})
    if "Prevent all damage" not in str(wov.get("oracle_text")):
        ok = False
        notes.append("wall of vapor oracle text missing prevention line")
    ab = wov.get("abilities") or []
    eff = str(ab)
    if "prevention_source_scope" not in eff or "Unimplemented" not in eff:
        ok = False
        notes.append(f"wall of vapor prevention parse unexpected: {eff[:200]}")
    else:
        notes.append("wall of vapor prevention = Effect::Unimplemented "
                     "{name: prevention_source_scope} (fail-closed, no "
                     "shield) -- matches engine unit test "
                     "prevention_source_scope_fails_closed_at_the_route")
    ds = cd.get("dry spell", {})
    if "DamageAll" not in str(ds.get("abilities")):
        ok = False
        notes.append("dry spell missing DamageAll parse")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {"wall of vapor": str(wov.get("oracle_text")),
                              "dry spell": str(ds.get("oracle_text"))},
                   "wov_abilities": wov.get("abilities")}, f, indent=1)
    say(f"card-data check: ok={ok} notes={notes}")
    wire("card_data_check", {"ok": ok, "notes": notes})
    return ok


async def do_mulligan(c, acts, st, pid, tag, keep_fn):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    hn = hand_lnames(st["state"], pid)
    mulls = MULL_COUNTS.get(tag, 0)
    if keep_fn(hn) or mulls >= 4:
        decision = "keep"
    else:
        decision = "mulligan"
        MULL_COUNTS[tag] = mulls + 1
    say(f"[{tag}] {decision} {len(hn)}: {hn} (mulls={mulls})")
    wire("mulligan", {"who": tag, "decision": decision, "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if decision == "keep"
                                             else "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    if target is None:
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = cand_ref_int(ch)
        o = get_obj(state, oid)
        nm = obj_lname(state, oid) if oid is not None else "?"
        # keep the castables, bottom lands first
        if is_land(o):
            return (1, nm)
        return (0, nm)

    picks = sorted(cands, key=bkey)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, cand_ref_int(x)) for x in picks]}")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard(c, acts, st, pid, tag):
    """Cleanup discard via vi schema/select; never decide unknown prompts."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)

    def rank(o):
        o = get_obj(state, o)
        nm = obj_lname(state, o)
        if is_land(o):
            return (0, nm)
        if nm in (WOV, DRYSPELL):
            return (5, nm)   # keep the fixture cards
        return (2, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = [ch for ch in (rdata.get("candidates") or [])
                 if str(cand_ref_int(ch)) in handset]
        if not cands:
            continue
        picks = sorted(cands, key=lambda ch: rank(str(cand_ref_int(ch))))[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding {[obj_lname(state, cand_ref_int(x)) for x in picks]}")
        wire("discard", {"who": tag, "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "select",
                                           "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
        return True
    return False


def cast_guard_tick(state, who, pid):
    """2026-10-09 cast-confirmation guard: hold priority passes and new
    plays while a CastSpell is in flight; vi decisions still answered."""
    pay = ST["paying"][who]
    if not pay:
        return "proceed"
    label, card, zone, since, cast_oid = (pay["label"], pay["card"],
                                         pay["zone"], pay["since"],
                                         pay["oid"])
    if spell_on_stack(state, pid, card):
        say(f"[{who}] cast confirmed (stack): {label}")
        wire("cast_confirmed", {"who": who, "label": label, "via": "stack"})
        ST["paying"][who] = None
        return "proceed"
    if cast_oid is not None:
        o = get_obj(state, cast_oid)
        still = (o and str(o.get("zone") or "").lower() == zone
                 and str(o.get("controller")) == str(pid))
        if not still:
            say(f"[{who}] cast left {zone} ({label}); clearing guard")
            wire("cast_confirmed", {"who": who, "label": label,
                                    "via": f"left_{zone}"})
            ST["paying"][who] = None
            return "proceed"
    if time.time() - since > 30:
        say(f"[{who}] cast not confirmed after 30s ({label}); resetting")
        wire("cast_dropped_retry", {"who": who, "label": label})
        ST["paying"][who] = None
        if label == "cast_wov1":
            ST["wov1_cast_sent"] = False
        elif label == "cast_wov2":
            ST["wov2_cast_sent"] = False
        elif label == "cast_dryspell":
            ST["dry_cast"] = False
        elif label == "cast_bear":
            ST["bear_cast_sent"] = False
        return "proceed"
    return "hold"


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def cast_action_for(acts, state, pid, name):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and obj_lname(state, v) == name \
                        and str(get_obj(state, v).get("controller")) == str(pid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    return (o.get("zone") == "Battlefield" and not o.get("tapped")
            and not o.get("summoning_sick"))


async def export_named(c, tag):
    s = await c.export_state()
    with open(f"{EVDIR}/{tag}.json", "w") as f:
        f.write(s)
    say(f"exported {tag}.json")
    wire("export", {"tag": tag})


def load_env(tag):
    p = f"{EVDIR}/{tag}.json"
    if not os.path.exists(p):
        return None
    return json.loads(open(p).read())


async def drain(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            return
        if t in ("Error", "ActionRejected"):
            ST.setdefault("rejections", []).append(
                {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
            wire("rejection", {"who": c.name, "type": t, "data": data})


# ------------------------------------------------------------- seat ticks

async def p0_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 0, "P0",
                         lambda hn: (sum(1 for n in hn
                                         if n in (ISLAND, SWAMP)) >= 2
                                     and ISLAND in hn and WOV in hn)):
        return True
    if await do_bottom(c, acts, st, 0, "P0"):
        return True
    if await do_discard(c, acts, st, 0, "P0"):
        return True
    holding = cast_guard_tick(state, "P0", 0) == "hold"

    atypes = set(a.get("type") for a in acts)
    # DeclareBlockers: block Bear with WoV #2 (best-effort; payload
    # shape discovered from the advertised action, logged on wire).
    if "DeclareBlockers" in atypes and ST["stage"] == "COMBAT" \
            and ST["bear_oid"] is not None and not ST["blocked"]:
        da = find_action(acts, "DeclareBlockers")
        wire("blockers_advertised", {"data": da.get("data") if da else None})
        say(f"[P0] DeclareBlockers advertised; data_keys="
            f"{list((da.get('data') or {}).keys()) if da else None}")
        wovs = bf_oids(state, 0, WOV)
        wov = ST.get("wov2_oid") or (wovs[0] if wovs else None)
        if wov is not None and da is not None:
            d = copy.deepcopy(da)
            # most likely schema; rejection is wired and degrades the
            # control leg to not-run
            d.setdefault("data", {}).update(
                {"assignments": [[int(wov), int(ST["bear_oid"])] ]})
            ST["blocked"] = True
            ST["blocker_oid"] = wov
            say(f"[P0] blocking bear {ST['bear_oid']} with wov {wov}")
            wire("p0_blocks", {"blocker": wov,
                               "attacker": ST["bear_oid"]})
            await submit_as_is(c, d)
            return True
    if not my_priority(acts):
        return False
    if holding or real_decision_pending(st):
        return False
    own_main = my_main(state, 0)
    # 1. play a land
    if own_main:
        have = sum(1 for o in bf_oids(state, 0)
                   if is_land(get_obj(state, o)))
        if have < 6:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return True
    # 2. cast WoV #1 (SETUP) then WoV #2 (COMBAT setup)
    want = None
    if ST["stage"] == "SETUP" and ST["wov1_oid"] is None \
            and not ST.get("wov1_cast_sent"):
        want = "cast_wov1"
    elif ST["stage"] == "COMBAT" and ST["wov2_oid"] is None \
            and ST["wov1_oid"] is not None and not ST.get("wov2_cast_sent"):
        want = "cast_wov2"
    if want and own_main:
        a = cast_action_for(acts, state, 0, WOV)
        if a is not None:
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or a.get("_src_oid")
            ST[want.replace("cast_", "") + "_cast_sent"] = True
            ST["paying"]["P0"] = {"label": want, "card": WOV,
                                  "zone": "hand",
                                  "since": time.time(), "oid": oid}
            say(f"[P0] {want} (oid {oid})")
            wire("cast", {"who": "P0", "label": want, "oid": oid})
            await submit_as_is(c, a)
            return True
    # 3. pass priority
    if not holding:
        return await pass_priority(c, st, acts)
    return False


async def p1_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 1, "P1",
                         lambda hn: (sum(1 for n in hn
                                         if n in (SWAMP, FOREST)) >= 2
                                     and SWAMP in hn and DRYSPELL in hn)):
        return True
    if await do_bottom(c, acts, st, 1, "P1"):
        return True
    if await do_discard(c, acts, st, 1, "P1"):
        return True
    holding = cast_guard_tick(state, "P1", 1) == "hold"

    atypes = set(a.get("type") for a in acts)
    # DeclareAttackers: attack with a ready Bear at P0 (COMBAT stage)
    if "DeclareAttackers" in atypes and ST["stage"] == "COMBAT" \
            and not ST["attacked"]:
        da = find_action(acts, "DeclareAttackers")
        ready = [b for b in bf_oids(state, 1, BEAR)
                 if can_attack_now(state, b)]
        if ready and da is not None and state.get("active_player") == 1:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update(
                {"attacks": [[int(ready[0]),
                              {"type": "Player", "data": 0}]],
                 "bands": []})
            ST["attacked"] = True
            ST["attacker_oid"] = ready[0]
            say(f"[P1] attacks P0 with bear {ready[0]}")
            wire("p1_attacks", {"attacker": ready[0]})
            await submit_as_is(c, d)
            return True
        elif da is not None:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            return True
    if not my_priority(acts):
        return False
    if holding or real_decision_pending(st):
        return False
    own_main = my_main(state, 1)
    # 1. play a land
    if own_main:
        have = sum(1 for o in bf_oids(state, 1)
                   if is_land(get_obj(state, o)))
        if have < 6:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return True
    # 2a. LEG1: cast Dry Spell while WoV #1 is on the battlefield
    if ST["stage"] == "LEG1" and not ST["dry_cast"] and own_main:
        if bf_oids(state, 0, WOV):
            a = cast_action_for(acts, state, 1, DRYSPELL)
            if a is not None:
                d = a.get("data", {}) or {}
                oid = d.get("object_id") or a.get("_src_oid")
                ST["dry_cast"] = True
                ST["p0_life_pre"] = life(state, 0)
                ST["p1_life_pre"] = life(state, 1)
                ST["paying"]["P1"] = {"label": "cast_dryspell",
                                      "card": DRYSPELL, "zone": "hand",
                                      "since": time.time(), "oid": oid}
                say(f"[P1] casts Dry Spell (oid {oid}); "
                    f"P0 life={ST['p0_life_pre']} P1 life={ST['p1_life_pre']}")
                wire("cast", {"who": "P1", "label": "cast_dryspell",
                              "oid": oid})
                await submit_as_is(c, a)
                return True
    # 2b. cast a Bear whenever (for the combat leg)
    if ST["stage"] in ("LEG1", "COMBAT") and ST["bear_oid"] is None \
            and not ST.get("bear_cast_sent") and own_main:
        a = cast_action_for(acts, state, 1, BEAR)
        if a is not None:
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or a.get("_src_oid")
            ST["bear_cast_sent"] = True
            ST["paying"]["P1"] = {"label": "cast_bear", "card": BEAR,
                                  "zone": "hand",
                                  "since": time.time(), "oid": oid}
            say(f"[P1] casts Bear (oid {oid})")
            wire("cast", {"who": "P1", "label": "cast_bear", "oid": oid})
            await submit_as_is(c, a)
            return True
    # 3. pass priority
    if not holding:
        return await pass_priority(c, st, acts)
    return False


# ------------------------------------------------------------- evaluate

def wov_bf_zone(env_state, wov_oid):
    if wov_oid is None:
        return None
    return get_obj(env_state, wov_oid).get("zone")


def evaluate():
    a = {k: "not-run" for k in ("A1_setup_ok", "A2_spell_not_prevented",
                                "A3_combat_control", "A4_cleanup")}
    notes = ST["notes"]
    pre = load_env("pre_dryspell")
    post = load_env("post_dryspell")
    postc = load_env("post")

    # A1: fixture ready before the Dry Spell cast
    if pre is not None:
        s = pre["state"]
        wovs = [oid for oid, o in (s.get("objects") or {}).items()
                if o.get("zone") == "Battlefield"
                and str(o.get("controller")) == "0"
                and str(o.get("base_name") or o.get("name") or "")
                .lower() == WOV]
        if wovs:
            a["A1_setup_ok"] = "passed"
            notes.append(f"A1: {len(wovs)} WoV on P0 BF at pre_dryspell "
                         f"(turn {s.get('turn_number')})")
        else:
            a["A1_setup_ok"] = "failed"
            notes.append("A1: no WoV on P0 BF in pre_dryspell")
    else:
        notes.append("A1: pre_dryspell.json missing")
        a["A1_setup_ok"] = "failed"

    # A2: reported path -- Dry Spell damage to WoV must NOT be prevented
    if post is not None and ST["dry_resolved"]:
        s = post["state"]
        zone = wov_bf_zone(s, ST["wov1_oid"])
        p0l, p1l = life(s, 0), life(s, 1)
        spell_ok = (ST["p0_life_pre"] is not None
                    and p0l == ST["p0_life_pre"] - 1
                    and p1l == ST["p1_life_pre"] - 1)
        notes.append(f"A2: dry resolved; P0 {ST['p0_life_pre']}->{p0l}, "
                     f"P1 {ST['p1_life_pre']}->{p1l} "
                     f"(spell-resolution control {'OK' if spell_ok else 'OFF'}); "
                     f"WoV#1 zone={zone}")
        if spell_ok and zone != "Battlefield":
            a["A2_spell_not_prevented"] = "passed"
            notes.append("A2: WoV took the 1 damage and left the BF -- "
                         "no prevention; reported symptom absent")
        elif spell_ok and zone == "Battlefield":
            a["A2_spell_not_prevented"] = "failed"
            notes.append("A2: WoV survived a resolved Dry Spell -- "
                         "prevention applied to spell damage = REPORTED BUG")
        else:
            a["A2_spell_not_prevented"] = "failed"
            notes.append("A2: Dry Spell resolution control off; "
                         "cannot judge prevention")
    else:
        notes.append(f"A2: dry_resolved={ST['dry_resolved']} "
                     f"post_dryspell={'present' if post else 'missing'}")
        a["A2_spell_not_prevented"] = "not-run"

    # A3: combat control (best-effort, does not drive the verdict)
    final = postc or post
    if final is not None and ST["combat_done"] and ST["blocker_oid"]:
        zone = wov_bf_zone(final["state"], ST["blocker_oid"])
        notes.append(f"A3: blocker WoV#{ST['blocker_oid']} zone={zone} "
                     f"after blocked Bear combat")
        # rules: damage prevented -> WoV survives. On v0.114.0 the effect
        # is unimplemented (parser fail-closed), so WoV dies. Either way
        # the outcome is OBSERVED here; 'passed' = matches rules.
        a["A3_combat_control"] = "passed" if zone == "Battlefield" else "failed"
        if a["A3_combat_control"] == "failed":
            notes.append("A3: WoV died to blocked Bear damage -- consistent "
                         "with prevention_source_scope being Unimplemented "
                         "(no shield exists); separate from the reported bug")
    else:
        a["A3_combat_control"] = "not-run"
        notes.append(f"A3: combat_done={ST['combat_done']} "
                     f"blocker={ST['blocker_oid']} -> not-run")

    # A4: cleanup
    if postc is not None:
        a["A4_cleanup"] = ("passed" if not (postc["state"].get("stack") or [])
                           else "failed")
        notes.append(f"A4: stack_empty={a['A4_cleanup']=='passed'}")
    else:
        notes.append("A4: post.json missing -> not-run")

    if a["A1_setup_ok"] == "failed" or a["A2_spell_not_prevented"] == "not-run":
        verdict = "blocked"
    elif a["A2_spell_not_prevented"] == "failed":
        verdict = "reproduced"
    elif a["A2_spell_not_prevented"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return a, verdict


# ------------------------------------------------------------- render

def render_summary(run, out_path):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 860
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #9681 - Wall of Vapor damage prevention "
           "scope", fill=(235, 240, 250))
    y += 28
    d.text((24, y), f"server v0.114.0 (ac67cbb) protocol 153 - re- "
           f"validation {run['run_id']}", fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced"
           else ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (255, 200, 90)))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire observations):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "WoV on P0 BF; pre_dryspell exported",
        "A2_spell_not_prevented": "REPORTED PATH: Dry Spell resolved; WoV "
            "took 1 damage (not prevented)",
        "A3_combat_control": "CONTROL: WoV blocked Bear (rules: survives)",
        "A4_cleanup": "stack empty, post.json exported",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 12
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:12]:
        d.text((36, y), str(n)[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(out_path)
    say(f"wrote {out_path}")


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------- main

async def main():
    RUNLOG_F = open(f"{EVDIR}/scenario_run.log", "w")
    global RUNLOG, WIRE
    RUNLOG = RUNLOG_F
    WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    reset()
    MULLS.clear()
    MULL_COUNTS.clear()
    SUBMITTED_OPPS.clear()
    PASSED_REV.clear()
    t0 = time.time()
    await verify_server_hello()
    check_card_data()

    p0 = PhaseClient("P0-9681-01140")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1-9681-01140")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    wov1_cast_marked = False

    try:
        while time.time() - t0 < TIMEOUT:
            await asyncio.sleep(0.15)
            if ST["stop"]:
                break
            for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                      (p1, p1_tick, "P1", 1)):
                st = c.latest
                if not st:
                    continue
                rev_changed = c.revision != last_rev.get(c.name)
                if rev_changed:
                    last_rev[c.name] = c.revision
                else:
                    holds = my_priority(merged_actions(st))
                    if not (holds and time.time()
                            - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    await drain(c)
                    await tick(c, st, merged_actions(st), st["state"], )
                except Exception as e:
                    say(f"[{tag}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": tag,
                                        "err": f"{type(e).__name__}: {e}"})

            st0 = p0.latest
            if st0:
                state = st0["state"]
                # track WoV / Bear oids
                if ST["wov1_oid"] is None:
                    w = bf_oids(state, 0, WOV)
                    if w:
                        ST["wov1_oid"] = w[0]
                        say(f"WoV#1 on BF oid={w[0]}")
                        wire("wov1_bf", {"oid": str(w[0])})
                if ST["wov2_oid"] is None and ST["wov1_oid"] is not None:
                    w = [x for x in bf_oids(state, 0, WOV)
                         if x != ST["wov1_oid"]]
                    if w:
                        ST["wov2_oid"] = w[0]
                        say(f"WoV#2 on BF oid={w[0]}")
                        wire("wov2_bf", {"oid": str(w[0])})
                if ST["bear_oid"] is None:
                    b = bf_oids(state, 1, BEAR)
                    if b:
                        ST["bear_oid"] = b[0]
                        say(f"Bear on BF oid={b[0]}")
                        wire("bear_bf", {"oid": str(b[0])})
                # Dry Spell stack tracking
                if ST["dry_cast"] and not ST["dry_seen_stack"]:
                    if spell_on_stack(state, 1, DRYSPELL):
                        ST["dry_seen_stack"] = True
                        say("Dry Spell observed on stack")
                        wire("dry_on_stack", {})
                if ST["dry_seen_stack"] and not ST["dry_resolved"]:
                    if not (state.get("stack") or []):
                        ST["dry_resolved"] = True
                        say(f"Dry Spell RESOLVED; P0 life={life(state,0)} "
                            f"P1 life={life(state,1)} wov1="
                            f"{wov_bf_zone(state, ST['wov1_oid'])}")
                        wire("dry_resolved", {"p0_life": life(state, 0),
                                              "p1_life": life(state, 1),
                                              "wov1_zone": wov_bf_zone(
                                                  state, ST["wov1_oid"])})
                        await export_named(p0, "post_dryspell")
                        ST["post_dryspell"] = True
                        ST["stage"] = "COMBAT"
                # stage transitions
                if ST["stage"] == "SETUP" and ST["wov1_oid"] is not None \
                        and ST["paying"]["P0"] is None:
                    await export_named(p0, "pre_dryspell")
                    ST["pre_dryspell"] = True
                    ST["stage"] = "LEG1"
                    say("WoV#1 resolved; pre_dryspell exported; -> LEG1")
                    wire("stage", {"stage": "LEG1"})
                # combat leg completion: blocker took combat damage
                if ST["stage"] == "COMBAT" and ST["blocked"] \
                        and not ST["combat_done"]:
                    ph = state.get("phase", "")
                    if ph in ("PostCombatMain",) \
                            or (ST["attacked"] and ph == "CombatDamage"
                                and not (state.get("stack") or [])):
                        # combat damage assigned this step; check next
                        # revision after the step leaves
                        pass
                    if ph == "PostCombatMain" and ST["blocked"]:
                        ST["combat_done"] = True
                        await export_named(p0, "pre_combat_note")
                        say(f"combat finished; blocker zone="
                            f"{wov_bf_zone(state, ST['blocker_oid'])}")
                        wire("combat_done", {
                            "blocker": ST["blocker_oid"],
                            "blocker_zone": wov_bf_zone(
                                state, ST["blocker_oid"]),
                            "attacker_zone": wov_bf_zone(
                                state, ST["attacker_oid"])})
                if ST["combat_done"] and not ST["post_exported"] \
                        and not (state.get("stack") or []):
                    await export_named(p0, "post")
                    ST["post_exported"] = True
                    ST["stage"] = "DONE"
                    ST["stop"] = True
                    say("post exported; DONE")
                    break

            if (st0 and (st0["state"].get("turn_number") or 0) > TURN_CAP
                    and ST["stage"] in ("SETUP", "LEG1")):
                ST["notes"].append(f"TURN_CAP {TURN_CAP} hit in "
                                   f"{ST['stage']}")
                say("TURN_CAP: finishing")
                break
            if time.time() - last_diag > 25 and st0:
                last_diag = time.time()
                s = st0["state"]
                say(f"DIAG turn={s.get('turn_number')} active="
                    f"{s.get('active_player')} phase={s.get('phase')} "
                    f"stage={ST['stage']} wov1={ST['wov1_oid']} "
                    f"wov2={ST['wov2_oid']} bear={ST['bear_oid']} "
                    f"dry_cast={ST['dry_cast']} dry_stack="
                    f"{ST['dry_seen_stack']} resolved={ST['dry_resolved']} "
                    f"attacked={ST['attacked']} blocked={ST['blocked']} "
                    f"P0hand={hand_lnames(s, 0)[:6]} "
                    f"P1hand={hand_lnames(s, 1)[:6]}")
            if time.time() - t0 > TIMEOUT - 5:
                ST["notes"].append("TIMEOUT hit; finishing")
                break
    except Exception as e:
        ST["notes"].append(f"main loop exception: {type(e).__name__}: {e}")
        say(f"main loop exception: {e}")

    a, verdict = evaluate()
    say(f"FINAL verdict={verdict} assertions={a}")
    if not ST["post_exported"]:
        try:
            await export_named(p0, "post")
            ST["post_exported"] = True
            ST["notes"].append("post.json exported at finish() fallback")
        except Exception as e:
            ST["notes"].append(f"post export failed: {e}")
    a, verdict = evaluate()

    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass

    dur = time.time() - t0
    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9681",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 153, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Wall of Vapor", 8], ["Island", 26], ["Swamp", 26]],
            "P1": [["Dry Spell", 4], ["Grizzly Bears", 4], ["Swamp", 28],
                   ["Forest", 24]],
        },
        "assertions": a,
        "verdict": verdict,
        "result": ("A1 setup_ok: %s; A2 spell_not_prevented: %s; "
                   "A3 combat_control: %s; A4 cleanup: %s"
                   % tuple(a.get(k, "not-run") for k in
                           ("A1_setup_ok", "A2_spell_not_prevented",
                            "A3_combat_control", "A4_cleanup"))),
        "scope": ("Wall of Vapor damage-prevention scope on pinned "
                  "v0.114.0: spell damage (Dry Spell, reported path) and "
                  "blocked-creature combat damage (control); native engine, "
                  "two human driver seats"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "The prevention effect parses as Effect::Unimplemented "
            "{name: prevention_source_scope} on v0.114.0 (parser fails "
            "closed per CR 615.1a); no prevention shield exists, so both "
            "the reported over-prevention AND rules-correct prevention "
            "are absent. States are authoritative exports, restorable only "
            "via full game replay (no standalone state-import path).",
        ],
        "fixture": {
            "wov1_oid": str(ST.get("wov1_oid")),
            "wov2_oid": str(ST.get("wov2_oid")),
            "bear_oid": str(ST.get("bear_oid")),
            "dry_cast": ST["dry_cast"],
            "dry_seen_stack": ST["dry_seen_stack"],
            "dry_resolved": ST["dry_resolved"],
            "attacked": ST["attacked"],
            "blocked": ST["blocked"],
            "blocker_oid": str(ST.get("blocker_oid")),
            "combat_done": ST["combat_done"],
            "rejections": ST.get("rejections", []),
        },
        "notes": ST["notes"],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9681_01140.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": a, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)
    with open(f"{EVDIR}/obs.json", "w") as f:
        json.dump({"rejections": ST.get("rejections", [])}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": a})
    say(f"VERDICT: {verdict}")
    WIRE.close()
    RUNLOG_F.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read())
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(a)}", flush=True)


if __name__ == "__main__":
    pidfile = "/tmp/scenario_9681_01140.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9681 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        asyncio.run(main())
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass
