#!/usr/bin/env python3
"""Issue #10063: "[parser] Scampire activation drops required sticker recipient
restriction".

FIRST validation run on pinned v0.111.0 (protocol 144, build 33aa23a).

#10063 is an OPEN bug issue (not a PR). The report's own confirmed evidence is
data-level: the parsed target of Scampire's activated ability is
`Typed` Creature with only `InZone(Graveyard)` + `Owned(You)` properties --
the printed "with a sticker on it" restriction has no predicate. The reporter
explicitly did NOT run a runtime activation probe ("permitting an unstickered
creature is the expected consequence, rather than a measured runtime result").
This run performs that missing runtime probe end-to-end on the pinned release.

Oracle (verified from pinned v0.111.0 card-data.json, see data_evidence.json):
  Scampire: {2}{B} Creature -- Vampire Employee (1/3).
    "When Scampire enters, you get {TK}, then you may put a sticker on a
     creature card in your graveyard.
     {3}{B}: Return target creature card with a sticker on it from your
     graveyard to the battlefield. That creature gains haste. Exile it at the
     beginning of the next end step. Activate only as a sorcery."
  Parsed: exactly 1 Activated ability (ability_index 0); its target is
  Typed/Creature with properties [InZone(Graveyard), Owned(You)] -- NO sticker
  predicate. Corroborating source read (local phase-main checkout, commit
  3adc37af -- NOT the pinned release build): the engine models stickers on
  objects (GameObject.stickers, object_has_sticker_kind) but the FilterProp
  vocabulary used by Typed targets has no HasSticker variant, so the parser
  has nowhere to emit the restriction.

Plan (native engine, two human driver seats):
  P0: 4x Scampire + 8x Walking Corpse + 48x Swamp (+1 sticker sheet so the
      ETB PutSticker has candidates). Discards 2 corpses via cleanup, casts
      Scampire, accepts the ETB sticker on ONE corpse (control: engine
      demonstrably distinguishes stickered vs unstickered objects), then in a
      later main phase activates Scampire's {3}{B} ability.
  P1: 60x Island. Passes; is the priority-passing partner.

BEHAVIORAL CONTRACT (written before observing results)
  A1_setup_ok     pre.json exported with Scampire on P0's battlefield, >=2
                  corpses in P0's graveyard (ideally one stickered via the
                  ETB + one unstickered, distinguished via the objects'
                  stickers field), P0 priority in its own main phase with
                  >=4 untapped Swamps.
  A2_correct_targeting (REPORTED PATH)  The TargetSelection opportunity for
                  Scampire's activation must NOT advertise the UNSTICKERED
                  corpse. passed == not advertised (printed restriction
                  enforced); failed == advertised (reported bug).
  A3_correct_resolution (REPORTED PATH)  Targeting the unstickered corpse must
                  NOT return it to the battlefield. passed == stays in
                  graveyard / ability fizzles; failed == returns to BF with
                  haste (reported bug, end-to-end).
  A4_stickered_control  The stickered corpse IS advertised (control: the
                  target surface works and the engine knows which card is
                  stickered; the filter just ignores the distinction).
                  not-run if the ETB sticker never lands.
  A5_cleanup      stack empty, post.json exported.

Verdict: reproduced iff A1 passes and A2 fails and A3 fails.
not-reproduced iff A1 passes and A2 passes (restriction enforced at the
targeting layer; resolution probe unnecessary). blocked iff A1 fails or the
activation never completes.

Protocol-144 notes (from #9934/#9921): engine auto-pays (bare CastSpell);
priority = PassPriority in the viewing seat's top-level legal_actions;
MulliganDecision via legacy Action; bottom-after-mulligan via vi
schema/select; cleanup discards via viewer_interaction only. Turn order is
randomized; every play gates on active_player. Latch verdict-relevant
observations on first sight (never overwrite).
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
RUN_ID = os.environ.get("RUN_ID", "run-10063-v01110-20261017-0811")
ISSUE = 10063
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

URL = "ws://127.0.0.1:9374/ws"

WIRE = None
RUNLOG = None

SCAMPIRE = "scampire"
CORPSE = "walking corpse"
SWAMP = "swamp"
ISLAND = "island"

P0_DECK = deck((SCAMPIRE.title(), 4), (CORPSE.title(), 8),
               (SWAMP.title(), 48))
P0_DECK["sticker_sheets"] = ["Playable Delusionary Hydra"]
P1_DECK = deck((ISLAND.title(), 60))

TIMEOUT = 1500

ST = {}
MULLS = set()
MULL_COUNTS = {}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
SCAMPIRE_ABILITY_INDEX = None  # resolved from card data in check_card_data


def reset():
    ST.update({
        "stage": "SETUP",   # SETUP -> PRE -> ACTIVATE -> RESOLVE -> DONE
        "stop": False,
        "notes": [],
        "scam_oid": None,
        "scam_cast_sent": False,
        "etb_pending": False,
        "etb_resolved": False,
        "stickered_oid": None,
        "unstickered_oid": None,
        "advertised": None,          # latched: {oid: name} at activation
        "advertised_latched": False,
        "activated": False,          # ActivateAbility submitted (await target)
        "target_submitted": False,
        "target_oid": None,
        "resolve_latched": None,     # "returned_bf" | "stayed_gy"
        "resolve_quiet": 0,
        "paying": {"P0": None, "P1": None},
        "pre_exported": False,
        "post_exported": False,
        "last_submit": time.time(),
        "rejections": [],
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


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def gy_oids(state, pid, lname=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Graveyard":
            continue
        if str(o.get("owner", o.get("controller", -1))) != str(pid):
            continue
        if lname is not None and obj_lname(state, oid) != lname:
            continue
        out.append(oid)
    return out


def is_stickered(state, oid):
    o = get_obj(state, oid)
    st = o.get("stickers")
    return bool(st)


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def life(state, pid):
    return player_of(state, pid).get("life")


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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg = {"type": a["type"], "data": copy.deepcopy(a["data"])}
    wire("action_submit", {"who": c.name, "action": msg})
    ST["last_submit"] = time.time()
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    ST["last_submit"] = time.time()
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
    assert str(ver).startswith("0.111.0"), f"unexpected version {ver}"
    assert int(proto) == 144, f"unexpected protocol {proto}"
    assert str(build) == "33aa23a", f"unexpected build {build}"
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


def check_card_data():
    global SCAMPIRE_ABILITY_INDEX
    ok, notes = True, []
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.111.0/data/"
                        f"card-data.json"))
    sc = cd.get(SCAMPIRE, {})
    ot = str(sc.get("oracle_text"))
    if "Return target creature card with a sticker on it" not in ot:
        ok = False
        notes.append("scampire oracle missing the sticker clause")
    else:
        notes.append("scampire oracle sticker clause confirmed")
    ab = sc.get("abilities") or []
    act = [e for e in ab if e.get("kind") == "Activated"]
    notes.append(f"scampire parsed abilities: total={len(ab)} "
                 f"activated={len(act)}")
    if len(act) != 1:
        ok = False
        notes.append("expected exactly 1 parsed Activated ability")
        SCAMPIRE_ABILITY_INDEX = None
    else:
        for i, e in enumerate(ab):
            if e.get("kind") == "Activated":
                SCAMPIRE_ABILITY_INDEX = i
        tgt = act[0].get("effect", {}).get("target", {})
        props = [p.get("type") for p in tgt.get("properties", [])]
        notes.append(f"activated ability_index={SCAMPIRE_ABILITY_INDEX}; "
                     f"target type={tgt.get('type')} "
                     f"filters={tgt.get('type_filters')} props={props}")
        if any("ticker" in str(p).lower() or "sticker" in str(p).lower()
               for p in tgt.get("properties", [])):
            notes.append("UNEXPECTED: a sticker property IS present")
        else:
            notes.append("CONFIRMED: no sticker predicate in the parsed "
                         "target properties (matches the issue report)")
        if tgt.get("type") != "Typed" or props != ["InZone", "Owned"]:
            notes.append(f"target shape differs from report: {tgt}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {SCAMPIRE: ot},
                   "scampire_abilities": sc.get("abilities"),
                   "scampire_triggers": sc.get("triggers")}, f, indent=1)
    say(f"card-data check: ok={ok}")
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


def _bottom_rank(hand_names, idx):
    """Rank hand cards for bottoming: extras of each kind first."""
    nm = hand_names[idx]
    counts = {}
    rank_of_extra = {SCAMPIRE: 0, CORPSE: 1, SWAMP: 2, ISLAND: 3}
    keep_n = {SCAMPIRE: 1, CORPSE: 2, SWAMP: 4, ISLAND: 4}
    seen = {}
    order = []
    for i, n in enumerate(hand_names):
        seen[n] = seen.get(n, 0) + 1
        if seen[n] > keep_n.get(n, 7):
            order.append((rank_of_extra.get(n, 5), n, i))
    return order


async def do_bottom(c, acts, st, pid, tag):
    vi = st.get("viewer_interaction") or {}
    if (vi.get("waitingForKind") or {}).get("code") != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    if not any(a.get("type") == "SelectCards" for a in acts):
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
        cands = [ch for ch in (rdata.get("candidates") or [])
                 if str(cand_ref_int(ch)) in handset]
        if not cands:
            continue
        iid = opp.get("interactionId")
        key = (tag, "bottom", iid)
        if key in SUBMITTED_OPPS:
            return False
        con = ((spec.get("data", {}) or {}).get("constraint", {})
               or {}).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False
        hn = hand_lnames(state, pid)
        extras = _bottom_rank(hn, 0)
        extra_idx = {i for _, _, i in extras}
        ranked = []
        for pos, ch in enumerate(cands):
            oid = cand_ref_int(ch)
            try:
                hpos = hand_ids(state, pid).index(str(oid))
            except ValueError:
                hpos = 999
            ranked.append((0 if hpos in extra_idx else 1, pos, ch))
        ranked.sort(key=lambda t: (t[0], t[1]))
        picks = [t[2] for t in ranked[:n]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoming "
            f"{[obj_lname(state, cand_ref_int(x)) for x in picks]}")
        wire("bottom", {"who": tag, "iid": iid,
                        "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


async def do_discard_p0(c, acts, st, tag):
    """Cleanup discard for P0: corpses first while the graveyard needs them,
    then lands; never the last Scampire."""
    state = st["state"]
    hand = hand_ids(state, pid=0)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)
    need_gy = len(gy_oids(state, 0, CORPSE)) < 2

    def rank(oid):
        nm = obj_lname(state, oid)
        o = get_obj(state, oid)
        if nm == CORPSE and need_gy:
            return (0, nm)
        if is_land(o):
            return (1, nm)
        if nm == CORPSE:
            return (2, nm)
        if nm == SCAMPIRE:
            return (5, nm)
        return (3, nm)

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
        picks = sorted(cands,
                       key=lambda ch: rank(str(cand_ref_int(ch))))[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding "
            f"{[obj_lname(state, cand_ref_int(x)) for x in picks]}")
        wire("discard", {"who": tag, "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


async def answer_sticker(c, st, tag):
    """Affirmative handler for the Scampire ETB sticker prompts. Answers the
    optional 'you may' affirmatively, picks the first offered sticker, and
    targets the first P0 graveyard corpse. Scoped strictly to opportunities
    whose text mentions stickers; never touches unrelated prompts."""
    state = st["state"]
    gy_corpses = set(gy_oids(state, 0, CORPSE))
    for opp in vi_ops(st):
        blob = json.dumps(opp).lower()
        if "sticker" not in blob:
            continue
        iid = opp.get("interactionId")
        key = (tag, "sticker", iid)
        if key in SUBMITTED_OPPS:
            return True
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        rdata = resp.get("data", {}) or {}
        items = rdata.get("candidates") or rdata.get("choices") or []
        target_picks = [ch for ch in items
                        if str(cand_ref_int(ch)) in gy_corpses]
        if target_picks:
            pick = target_picks[0]
            oid = cand_ref_int(pick)
            say(f"[{tag}] sticker target: corpse oid {oid}")
            wire("sticker_target", {"who": tag, "iid": iid, "oid": oid})
        elif items:
            pick = items[0]
            say(f"[{tag}] sticker choice: {json.dumps(pick)[:160]}")
            wire("sticker_pick", {"who": tag, "iid": iid,
                                  "pick": str(pick.get('id'))})
        else:
            # boolean-style optional: pick the affirmative-looking choice
            txt = json.dumps(opp).lower()
            say(f"[{tag}] sticker boolean prompt; blob: {txt[:200]}")
            wire("sticker_bool", {"who": tag, "iid": iid})
            return False  # do not guess; hold and log
        SUBMITTED_OPPS.add(key)
        if rtype == "schema":
            await interact_as(c, {"interactionId": iid,
                                  "response": {
                                      "type": "select",
                                      "data": {"choiceIds": [pick.get("id")]}}},
                              tag)
        else:
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": rtype, "data": {
                                      "choiceIds": [pick.get("id")]}}}, tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def cast_guard_tick(state, who, pid):
    pay = ST["paying"][who]
    if not pay:
        return "proceed"
    label, card, zone, since, cast_oid = (pay["label"], pay["card"],
                                         pay["zone"], pay["since"],
                                         pay["oid"])
    for e in state.get("stack", []) or []:
        oid = e.get("object_id") or e.get("source_id")
        o = get_obj(state, oid) if oid is not None else {}
        nm = obj_lname(state, oid) if o else str(e.get("name", "")).lower()
        ctrl = o.get("controller", e.get("controller", e.get("player", pid)))
        if nm == card and str(ctrl) == str(pid):
            say(f"[{who}] cast confirmed (stack): {label}")
            wire("cast_confirmed", {"who": who, "label": label,
                                    "via": "stack"})
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
        if label == "cast_scam":
            ST["scam_cast_sent"] = False
        return "proceed"
    return "hold"


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


def scampire_activate_action(acts, state, scam_oid):
    cands = [a for a in acts
             if a.get("type") == "ActivateAbility"
             and str(a.get("_src_oid")) == str(scam_oid)]
    if not cands:
        return None
    if SCAMPIRE_ABILITY_INDEX is None:
        say("[P0] scampire ability index unknown; refusing to guess")
        return None
    for a in cands:
        if (a.get("data", {}) or {}).get("ability_index") \
                == SCAMPIRE_ABILITY_INDEX:
            return a
    wire("activate_ambiguous",
         {"reason": "ability_index not advertised",
          "want_index": SCAMPIRE_ABILITY_INDEX,
          "candidates": [a.get("data", {}) for a in cands]})
    say(f"[P0] ability_index={SCAMPIRE_ABILITY_INDEX} not advertised; "
        "refusing to guess")
    return None


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
            ST["rejections"].append({"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
            wire("rejection", {"who": c.name, "type": t, "data": data})

# ------------------------------------------------------------- seat ticks

async def answer_target_selection(c, st, tag, pid):
    """Answer a pending TriggerTargetSelection or TargetSelection from
    state.waiting_for directly (neither is reliably surfaced via
    viewer_interaction). Submits the legacy ChooseTarget action.

    - TriggerTargetSelection (ETB PutSticker): pick the first P0 graveyard
      corpse (we WANT the sticker for the A4 control).
    - TargetSelection with pending_cast (the Scampire activation): latch the
      advertised legal targets (A2) and pick the UNSTICKERED corpse (the
      reported path)."""
    state = st["state"]
    wf = state.get("waiting_for") or {}
    wft = wf.get("type")
    if wft not in ("TriggerTargetSelection", "TargetSelection"):
        return False
    data = wf.get("data", {}) or {}
    if data.get("player") != pid:
        return False
    sel = data.get("selection", {}) or {}
    legal = sel.get("current_legal_targets") or []
    is_activation = wft == "TargetSelection" and "pending_cast" in data
    gy = set(gy_oids(state, pid, CORPSE))

    def legal_oids():
        return [t["Object"] for t in legal
                if isinstance(t, dict) and "Object" in t]

    if is_activation and not ST["advertised_latched"]:
        adv = {str(oid): obj_lname(state, oid) for oid in legal_oids()}
        ST["advertised"] = adv
        ST["advertised_latched"] = True
        say(f"[{tag}] advertised targets latched: {adv}; "
            f"stickered={ST['stickered_oid']} "
            f"unstickered={ST['unstickered_oid']}")
        wire("advertised_latched", {"advertised": adv,
                                    "stickered": ST["stickered_oid"],
                                    "unstickered": ST["unstickered_oid"]})

    pick_oid = None
    if is_activation:
        want = ST.get("unstickered_oid")
        for oid in legal_oids():
            if str(oid) == str(want):
                pick_oid = oid
                break
        if pick_oid is None:
            # Unstickered corpse not advertised: the printed restriction is
            # enforced at the targeting layer (A2 passes). Do not submit a
            # target; leave the activation pending and let evaluate() close
            # out the run.
            if not ST.get("a2_pass_noted"):
                ST["a2_pass_noted"] = True
                ST["notes"].append(
                    f"unstickered corpse {want} NOT among advertised "
                    f"targets {legal_oids()} -- printed restriction "
                    f"enforced; not submitting a target")
                wire("target_not_advertised",
                     {"want": want, "legal": legal_oids()})
            return False
    else:
        for oid in legal_oids():
            if str(oid) in gy:
                pick_oid = oid
                break
        if pick_oid is None and legal_oids():
            pick_oid = legal_oids()[0]

    src = data.get("source_id") or (data.get("pending_cast") or {}).get(
        "object_id")
    key = (tag, "choose_target", wft, str(src), str(sel.get("current_slot")))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    tgt = {"Object": int(pick_oid)} if pick_oid is not None else None
    say(f"[{tag}] {wft} (src {src}): choosing target {tgt} "
        f"from legal={legal_oids()}")
    wire("choose_target", {"who": tag, "kind": wft, "target": tgt,
                           "legal": legal_oids()})
    await submit_as_is(c, {"type": "ChooseTarget",
                           "data": {"target": tgt}})
    if is_activation:
        ST["target_submitted"] = True
        ST["target_oid"] = pick_oid
    return True


async def answer_optional_effect(c, st, tag, pid):
    """Answer a pending OptionalEffectChoice from state.waiting_for by
    accepting (we want the ETB sticker for the A4 control). Submits the
    legacy DecideOptionalEffect action."""
    state = st["state"]
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "OptionalEffectChoice":
        return False
    data = wf.get("data", {}) or {}
    if data.get("player") != pid:
        return False
    key = (tag, "optional_effect", str(data.get("source_id")))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] OptionalEffectChoice (src {data.get('source_id')}): "
        f"accepting -- {str(data.get('description'))[:80]}")
    wire("optional_effect", {"who": tag, "accept": True,
                             "description": data.get("description")})
    await submit_as_is(c, {"type": "DecideOptionalEffect",
                           "data": {"accept": True}})
    return True


async def answer_mana_payment(c, st, tag, pid):
    """Pay the Scampire activation's {3}{B} mana cost. Handles both the
    waiting_for form (PayManaAbilityMana et al.) and the legal_actions
    form."""
    state = st["state"]
    wf = state.get("waiting_for") or {}
    wft = wf.get("type")
    data = wf.get("data", {}) or {}
    pending_pay = (wft in ("PayManaAbilityMana", "ManaAbilityManaPayment",
                           "ManaPayment", "PayCost")
                   and data.get("player") == pid)
    if pending_pay:
        key = (tag, "mana_pay_wf", wft)
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] {wft}: paying 4x Black for the activation")
        wire("mana_pay", {"who": tag, "kind": wft,
                          "payment": ["Black"] * 4})
        await submit_as_is(c, {"type": "PayManaAbilityMana",
                               "data": {"payment": ["Black"] * 4}})
        return True
    return False


async def answer_choose_branch(c, st, tag, pid):
    """Answer a pending ChooseOneOfBranch (the sticker picker) by taking
    branch 0."""
    state = st["state"]
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "ChooseOneOfBranch":
        return False
    data = wf.get("data", {}) or {}
    if data.get("player") != pid:
        return False
    branches = data.get("branches") or []
    if not branches:
        return False
    key = (tag, "choose_branch", str(data.get("source_id")))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    desc = (data.get("branch_descriptions") or ["?"])[0]
    say(f"[{tag}] ChooseOneOfBranch: picking branch 0 of {len(branches)} "
        f"({str(desc)[:60]})")
    wire("choose_branch", {"who": tag, "index": 0, "of": len(branches)})
    await submit_as_is(c, {"type": "ChooseBranch", "data": {"index": 0}})
    return True


async def p0_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 0, "P0",
                         lambda hn: hn.count(SWAMP) >= 2
                         and SCAMPIRE in hn):
        return True
    if await do_bottom(c, acts, st, 0, "P0"):
        return True
    if await do_discard_p0(c, acts, st, "P0"):
        return True
    if await answer_target_selection(c, st, "P0", 0):
        return True
    if await answer_optional_effect(c, st, "P0", 0):
        return True
    if await answer_choose_branch(c, st, "P0", 0):
        return True
    if await answer_mana_payment(c, st, "P0", 0):
        return True
    if await answer_sticker(c, st, "P0"):
        return True
    if await pay_tick(c, acts):
        return True
    # (activation target selection is handled pre-gate by
    # answer_target_selection via state.waiting_for + ChooseTarget)

    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = find_action(acts, "DeclareAttackers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        wire("declare_attackers_empty", {"who": "P0-10063"})
        await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        db = find_action(acts, "DeclareBlockers")
        d = copy.deepcopy(db)
        d.setdefault("data", {}).update({"assignments": []})
        wire("declare_blockers_empty", {"who": "P0-10063"})
        await submit_as_is(c, d)
        return True
    if not my_priority(acts):
        return False
    holding = cast_guard_tick(state, "P0", 0) == "hold"
    if holding or real_decision_pending(st):
        return False
    own_main = my_main(state, 0)
    hand = hand_lnames(state, 0)
    lands_bf = sum(1 for o in bf_oids(state, 0)
                   if is_land(get_obj(state, o)))
    corpses_gy = gy_oids(state, 0, CORPSE)
    need_gy = len(corpses_gy) < 2

    # 1. land play: skip the drop while we still need graveyard corpses and
    #    holding one with a full hand forces the cleanup discard.
    if own_main:
        turn = state.get("turn_number")
        if need_gy and CORPSE in hand and len(hand) >= 8:
            wire("skip_land_for_discard", {"who": "P0-10063", "turn": turn,
                                           "hand": len(hand)})
        elif lands_bf < 5 and SWAMP in hand \
                and LAND_PLAYED_TURN.get("P0") != turn:
            for a in acts:
                if a.get("type") == "PlayLand":
                    LAND_PLAYED_TURN["P0"] = turn
                    say(f"[P0] playing land (bf had {lands_bf})")
                    wire("play_land", {"who": "P0-10063"})
                    await submit_as_is(c, a)
                    return True
    # 2. cast Scampire once 2 corpses are down and we have 3 lands
    if ST["stage"] == "SETUP" and own_main \
            and not need_gy and ST["scam_oid"] is None \
            and not ST["scam_cast_sent"] and lands_bf >= 3:
        a = cast_action_for(acts, state, 0, SCAMPIRE)
        if a is not None:
            d = a.get("data", {}) or {}
            oid = d.get("object_id") or a.get("_src_oid")
            ST["scam_cast_sent"] = True
            ST["paying"]["P0"] = {"label": "cast_scam", "card": SCAMPIRE,
                                  "zone": "hand",
                                  "since": time.time(), "oid": oid}
            say(f"[P0] casting scampire (oid {oid})")
            wire("cast", {"who": "P0", "label": "cast_scam", "oid": oid})
            await submit_as_is(c, a)
            return True
    # 3. ACTIVATE stage: fire Scampire's {3}{B} ability (once)
    if ST["stage"] == "ACTIVATE" and own_main and not ST.get("activated") \
            and not ST["target_submitted"]:
        scam = ST["scam_oid"]
        a = scampire_activate_action(acts, state, scam)
        if a is not None:
            untapped = sum(1 for o in bf_oids(state, 0)
                           if is_land(get_obj(state, o))
                           and not get_obj(state, o).get("tapped"))
            ST["activated"] = True
            say(f"[P0] activating scampire (untapped lands={untapped})")
            wire("scam_activate", {"scam_oid": scam,
                                   "action": a.get("data")})
            await submit_as_is(c, a)
            return True
    # 4. pass priority
    if not holding:
        return await pass_priority(c, st, acts)
    return False


async def do_discard_p1(c, acts, st, tag):
    """P1 cleanup discard (all-island deck; discard anything). Without this
    the game stalls at P1's DiscardToHandSize (observed turn 15, first
    attempt)."""
    state = st["state"]
    hand = hand_ids(state, pid=1)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)
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
        picks = cands[:n]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding "
            f"{[obj_lname(state, cand_ref_int(x)) for x in picks]}")
        wire("discard", {"who": tag, "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


async def p1_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 1, "P1",
                         lambda hn: hn.count(ISLAND) >= 2):
        return True
    if await do_bottom(c, acts, st, 1, "P1"):
        return True
    if await do_discard_p1(c, acts, st, "P1"):
        return True
    if await pay_tick(c, acts):
        return True
    holding = cast_guard_tick(state, "P1", 1) == "hold"
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = find_action(acts, "DeclareAttackers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        wire("declare_attackers_empty", {"who": "P1-10063"})
        await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        db = find_action(acts, "DeclareBlockers")
        d = copy.deepcopy(db)
        d.setdefault("data", {}).update({"assignments": []})
        wire("declare_blockers_empty", {"who": "P1-10063"})
        await submit_as_is(c, d)
        return True
    if not my_priority(acts):
        return False
    if holding or real_decision_pending(st):
        return False
    if my_main(state, 1):
        have = sum(1 for o in bf_oids(state, 1)
                   if is_land(get_obj(state, o)))
        turn = state.get("turn_number")
        if have < 6 and LAND_PLAYED_TURN.get("P1") != turn:
            for a in acts:
                if a.get("type") == "PlayLand":
                    LAND_PLAYED_TURN["P1"] = turn
                    await submit_as_is(c, a)
                    return True
    if not holding:
        return await pass_priority(c, st, acts)
    return False

# ------------------------------------------------------------- evaluate

def evaluate():
    a = {k: "not-run" for k in ("A1_setup_ok", "A2_correct_targeting",
                                "A3_correct_resolution",
                                "A4_stickered_control", "A5_cleanup")}
    notes = ST["notes"]
    pre = load_env("pre")
    post = load_env("post")

    # A1: fixture ready
    if pre is not None:
        s = pre["state"]
        scs = bf_oids(s, 0, SCAMPIRE)
        corpses = gy_oids(s, 0, CORPSE)
        stick = [o for o in corpses if is_stickered(s, o)]
        unst = [o for o in corpses if not is_stickered(s, o)]
        notes.append(f"A1: pre.json turn={s.get('turn_number')} "
                     f"scampire_bf={scs} corpses_gy={corpses} "
                     f"stickered={stick} unstickered={unst}")
        if scs and len(corpses) >= 2:
            a["A1_setup_ok"] = "passed"
            if not stick:
                notes.append("A1: no stickered corpse in pre.json -- the "
                             "ETB sticker did not land; A4 will be not-run")
        else:
            a["A1_setup_ok"] = "failed"
            notes.append("A1: fixture incomplete")
    else:
        notes.append("A1: pre.json missing")
        a["A1_setup_ok"] = "failed"

    # A2: the unstickered corpse must NOT be advertised (printed restriction)
    adv = ST.get("advertised") or {}
    unst = ST.get("unstickered_oid")
    if ST.get("advertised_latched"):
        if unst is not None and str(unst) in {str(k) for k in adv}:
            a["A2_correct_targeting"] = "failed"
            notes.append(f"A2: UNSTICKERED corpse {unst} ADVERTISED as a "
                         f"legal target {adv} -- REPORTED BUG reproduced "
                         f"(target filter ignores the sticker restriction)")
        else:
            a["A2_correct_targeting"] = "passed"
            notes.append(f"A2: unstickered corpse {unst} not advertised "
                         f"{adv} -- printed restriction enforced")
    else:
        notes.append("A2: target opportunity never observed -> not-run")

    # A3: targeting the unstickered corpse must NOT return it to the BF.
    # failed == the corpse reached the battlefield (reported bug,
    # end-to-end). passed == the ability was observed on the stack and
    # resolved with the corpse still in the graveyard. not-run == the
    # ability never completed (e.g. stuck pending).
    res = ST.get("resolve_latched")
    if res == "returned_bf":
        a["A3_correct_resolution"] = "failed"
        notes.append(f"A3: unstickered corpse {ST['target_oid']} RETURNED to "
                     f"the battlefield via Scampire's ability -- REPORTED "
                     f"BUG reproduced end-to-end")
    elif res == "stayed_gy":
        a["A3_correct_resolution"] = "passed"
        notes.append("A3: activated ability resolved with the unstickered "
                     "corpse still in the graveyard -- restriction enforced "
                     "at resolution despite the advertised target")
    else:
        notes.append(f"A3: resolve outcome never latched ({res}); "
                     f"target_submitted={ST['target_submitted']} -> not-run")

    # A4: control -- the stickered corpse IS advertised/accepted
    stick = ST.get("stickered_oid")
    if stick is None:
        notes.append("A4: no stickered corpse (ETB sticker did not land) "
                     "-> not-run")
    elif ST.get("advertised_latched"):
        if str(stick) in {str(k) for k in adv}:
            a["A4_stickered_control"] = "passed"
            notes.append(f"A4: stickered corpse {stick} advertised -- target "
                         f"surface works; the filter just ignores the "
                         f"sticker distinction")
        else:
            a["A4_stickered_control"] = "failed"
            notes.append(f"A4: stickered corpse {stick} NOT advertised "
                         f"{adv} -- target surface itself broken")
    else:
        notes.append("A4: no advertised set -> not-run")

    # A5: cleanup
    final = post
    if final is not None:
        a["A5_cleanup"] = ("passed" if not (final["state"].get("stack") or [])
                           else "failed")
        notes.append(f"A5: stack_empty={a['A5_cleanup'] == 'passed'}")
    else:
        notes.append("A5: post.json missing -> not-run")

    if a["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: fixture not established")
    elif a["A2_correct_targeting"] == "failed" \
            and a["A3_correct_resolution"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: unstickered corpse advertised AND "
                     "returned to the battlefield")
    elif a["A2_correct_targeting"] == "passed":
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: unstickered corpse not "
                     "advertised -- printed restriction enforced at the "
                     "targeting layer")
    else:
        verdict = "blocked"
        notes.append(f"verdict=blocked: A2={a['A2_correct_targeting']} "
                     f"A3={a['A3_correct_resolution']}")
    return a, verdict


# ------------------------------------------------------------- render

def render_summary(run, out_path):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #10063 - Scampire activation drops the "
           "'with a sticker on it' target restriction", fill=(235, 240, 250))
    y += 28
    d.text((24, y), f"server v0.111.0 (33aa23a) protocol 144 - first "
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
        "A1_setup_ok": "Scampire on BF; stickered + unstickered corpse in "
            "P0 GY; pre.json exported",
        "A2_correct_targeting": "REPORTED: unstickered corpse must NOT be "
            "an advertised target",
        "A3_correct_resolution": "REPORTED: unstickered corpse must NOT "
            "return to the battlefield",
        "A4_stickered_control": "CONTROL: stickered corpse IS advertised "
            "(surface works)",
        "A5_cleanup": "stack empty, post.json exported",
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
    for n in run["notes"][:13]:
        d.text((36, y), str(n)[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(out_path)
    say(f"wrote {out_path}")


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
    LAND_PLAYED_TURN.clear()
    ST["last_submit"] = time.time()
    t0 = time.time()

    hello = await verify_server_hello()
    # re-verify pinned artifact digests against on-disk files this run
    rel = f"{BACKFILL}/server/releases/v0.111.0"
    digests = {
        "binary_sha256": sha256_file(f"{rel}/phase-server-slim-x86_64-"
                                     f"unknown-linux-musl"),
        "card_data_sha256": sha256_file(f"{rel}/data/card-data.json"),
        "draft_pools_sha256": sha256_file(f"{rel}/data/draft-pools.json"),
    }
    say(f"digests this run: {digests}")
    wire("digests", digests)
    assert digests["binary_sha256"] == \
        "92e0e1cd04342d3429a35c965e2487a66adda730564824b36074e9f4b0305c9f", \
        "binary digest drifted from the pinned v0.111.0 artifact"
    assert digests["card_data_sha256"] == \
        "12ac3244d7add0d8d643d3b1466cbff72059c337110ed9863bd34351a2ae04c4", \
        "card-data digest drifted from the pinned v0.111.0 artifact"
    assert digests["draft_pools_sha256"] == \
        "787c188acdd4a5537bb2c447ec2da618ddad9fbbb215c807b9a575e6985a2f38", \
        "draft-pools digest drifted from the pinned v0.111.0 artifact"
    check_card_data()

    p0 = PhaseClient("P0-10063")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1-10063")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    last_rev = {}
    last_tick_at = {}

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
                    await tick(c, st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"[{tag}] tick error: {type(e).__name__}: {e}")
                    wire("tick_error", {"who": tag,
                                        "err": f"{type(e).__name__}: {e}"})

            st0 = p0.latest
            if st0:
                state = st0["state"]
                # track Scampire hitting the battlefield
                if ST["scam_oid"] is None:
                    scs = bf_oids(state, 0, SCAMPIRE)
                    if scs:
                        ST["scam_oid"] = scs[0]
                        ST["etb_pending"] = True
                        say(f"Scampire on BF oid={scs[0]}; ETB pending")
                        wire("scam_bf", {"oid": str(scs[0])})
                # ETB sticker resolution: latch once a stickered corpse is
                # observed, or (if the prompt flow never stickers anything)
                # after the trigger is gone, no sticker prompt is pending,
                # and the board has been quiet for a while. The trigger's
                # prompts appear while the stack is EMPTY, so stack-emptiness
                # alone must not close this out.
                if ST["etb_pending"] and not ST["etb_resolved"]:
                    corpses = gy_oids(state, 0, CORPSE)
                    stick = [o for o in corpses if is_stickered(state, o)]
                    sticker_opp = any(
                        "sticker" in json.dumps(opp).lower()
                        for opp in vi_ops(st0))
                    stack_busy = bool(state.get("stack"))
                    if stick:
                        ST["etb_resolved"] = True
                        ST["stickered_oid"] = stick[0]
                        rest = [o for o in corpses if o != stick[0]]
                        ST["unstickered_oid"] = rest[0] if rest else None
                        say(f"ETB sticker applied: stickered="
                            f"{ST['stickered_oid']} unstickered="
                            f"{ST['unstickered_oid']}")
                        wire("etb_stickered",
                             {"stickered": ST["stickered_oid"],
                              "unstickered": ST["unstickered_oid"]})
                        await export_named(p0, "etb")
                    elif not stack_busy and not sticker_opp:
                        n = ST.get("etb_quiet", 0) + 1
                        ST["etb_quiet"] = n
                        if n >= 40 and not ST.get("etb_nostick_noted"):
                            ST["etb_nostick_noted"] = True
                            ST["etb_resolved"] = True
                            if corpses:
                                ST["unstickered_oid"] = corpses[0]
                            ST["notes"].append(
                                "ETB flow finished with no stickered "
                                "corpse after 40 quiet revisions; the "
                                "reported-path probe proceeds with an "
                                "unstickered corpse, A4 control will be "
                                "not-run")
                            wire("etb_no_sticker", {})
                    else:
                        ST["etb_quiet"] = 0
                # SETUP -> PRE: fixture complete, export pre.json. Requires
                # Scampire on the BF, >=2 corpses in P0's GY (one designated
                # target), and 4 untapped Swamps. The stickered/unstickered
                # distinction is a control (A4); the reported path runs even
                # if the ETB sticker never lands.
                if ST["stage"] == "SETUP" and ST["etb_resolved"] \
                        and ST["scam_oid"] is not None \
                        and ST["unstickered_oid"] is not None \
                        and len(gy_oids(state, 0, CORPSE)) >= 2 \
                        and my_main(state, 0) \
                        and ST["paying"]["P0"] is None:
                    untapped = sum(1 for o in bf_oids(state, 0)
                                   if is_land(get_obj(state, o))
                                   and not get_obj(state, o).get("tapped"))
                    if untapped >= 4:
                        await export_named(p0, "pre")
                        ST["pre_exported"] = True
                        ST["stage"] = "ACTIVATE"
                        say("fixture ready; pre.json exported; -> ACTIVATE")
                        wire("stage", {"stage": "ACTIVATE"})
                # RESOLVE: watch the targeted corpse's zone. Latch on first
                # observation (never overwrite).
                # - "returned_bf": the corpse hits the battlefield (definitive
                #   end-to-end bug confirmation).
                # - "stayed_gy": the activated ability is observed ON the
                #   stack and then leaves it (resolved) while the corpse is
                #   still in the graveyard. The pending_cast must have
                #   cleared first; a stuck pending_cast (as in attempt 5)
                #   must NOT latch stayed_gy.
                if ST["target_submitted"] and ST["resolve_latched"] is None:
                    tgt = ST["target_oid"]
                    o = get_obj(state, tgt)
                    z = o.get("zone")
                    if z == "Battlefield":
                        ST["resolve_latched"] = "returned_bf"
                        kw = o.get("keywords")
                        say(f"unstickered corpse {tgt} is on the "
                            f"BATTLEFIELD (keywords={kw}) -- bug confirmed")
                        wire("resolve_latched",
                             {"outcome": "returned_bf", "keywords": kw})
                        await export_named(p0, "post")
                        ST["post_exported"] = True
                        ST["stage"] = "DONE"
                        ST["stop"] = True
                    else:
                        wf = state.get("waiting_for") or {}
                        pending = (wf.get("data") or {}).get("pending_cast")
                        scam_ab_on_stack = any(
                            str(e.get("source_id")) == str(ST["scam_oid"])
                            and "trigger" not in json.dumps(
                                e.get("kind", {})).lower()
                            for e in state.get("stack", []) or [])
                        if scam_ab_on_stack and not ST.get("ability_on_stack"):
                            ST["ability_on_stack"] = True
                            say("scampire activated ability observed on "
                                "the stack")
                            wire("ability_on_stack", {})
                        elif ST.get("ability_on_stack") \
                                and not scam_ab_on_stack \
                                and pending is None:
                            # Ability left the stack: it resolved.
                            if z == "Graveyard":
                                ST["resolve_latched"] = "stayed_gy"
                                say("ability resolved; corpse stayed in "
                                    "graveyard")
                            else:
                                ST["resolve_latched"] = "returned_bf"
                                say(f"ability resolved; corpse now in {z}")
                            wire("resolve_latched",
                                 {"outcome": ST["resolve_latched"]})
                            await export_named(p0, "post")
                            ST["post_exported"] = True
                            ST["stage"] = "DONE"
                            ST["stop"] = True
            # waiting_for visibility: log whenever it changes so a stall on
            # an unhandled prompt type is diagnosable from the run log
            if st0:
                wf = st0["state"].get("waiting_for") or {}
                wft = wf.get("type")
                if wft and wft != ST.get("last_wf_logged"):
                    ST["last_wf_logged"] = wft
                    say(f"waiting_for: {wft} "
                        f"player={(wf.get('data') or {}).get('player')}")
                    wire("waiting_for", {"type": wft})
            # stall watchdog: no submission for 240s -> note and stop
            if time.time() - ST["last_submit"] > 240 and not ST["stop"]:
                ST["notes"].append("stall watchdog: 240s without a "
                                   f"submission at stage {ST['stage']}")
                wire("stall", {"stage": ST["stage"]})
                say("STALL: 240s without submission; ending run")
                break
    finally:
        try:
            await p0.close()
        except Exception:
            pass
        try:
            await p1.close()
        except Exception:
            pass

    assertions, verdict = evaluate()
    say(f"verdict: {verdict}")
    for k, v in assertions.items():
        say(f"  {k}: {v}")
    run = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server_identity": dict(hello, **digests,
                                signature_key_id="436711b6a2d36828",
                                signature_verified=True,
                                signature_note="v0.111.0 binary + release "
                                "manifest minisign-verified against the "
                                "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY at "
                                "pin time 2026-10-14; latest stable still "
                                "v0.111.0; digests re-verified this run; "
                                "reused the live backfill v0.111.0 server "
                                "on 127.0.0.1:9374 (ServerHello re-verified "
                                "this run)"),
        "scenario": "driver/scenario_10063.py",
        "assertions": assertions,
        "verdict": verdict,
        "notes": ST["notes"],
        "rejections": ST["rejections"],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(assertions, f, indent=1)
    import shutil
    shutil.copy(__file__, f"{EVDIR}/scenario_10063.py")
    render_summary(run, f"{EVDIR}/summary.png")
    # manifest
    files = sorted(f for f in os.listdir(EVDIR)
                   if f != "manifest.sha256"
                   and os.path.isfile(f"{EVDIR}/{f}"))
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            mf.write(f"{sha256_file(f'{EVDIR}/{fn}')}  {fn}\n")
    say(f"evidence complete: {EVDIR} ({len(files)} files)")
    RUNLOG_F.close()
    WIRE.close()


if __name__ == "__main__":
    asyncio.run(main())
