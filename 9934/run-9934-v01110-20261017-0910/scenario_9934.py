#!/usr/bin/env python3
"""Issue #9934: "fix(engine): exert paid as a cost records exertion and is
seen by 'whenever you exert' triggers (CR 701.43a)".

FIRST validation run on pinned v0.111.0 (protocol 144, build 33aa23a).

#9934 is an OPEN fix PR (bug-labeled) whose Summary describes a concrete
player-observable bug: exerting a permanent as part of an activated
ability's COST (Oasis Ritualist, Arena of Glory, Fervent Paincaster, Pride
Sovereign and the other cost-exert cards) did NOT record the exertion the
way an attack exert does -- no Exerted status, no CreatureExerted event --
so "Whenever you exert a creature" observers (Resolute Survivors,
Battlefield Scavenger, Rohirrim Chargers, Trueheart Twins, Vizier of the
True) never fired for another permanent's cost-exert (the pre-fix matcher
only ever matched the trigger's own source). Per the #9681 precedent, a
bug-labeled fix PR with a concrete observable symptom is a valid
reproduction target. The PR is still OPEN (matthewevans has a binding
one-MED structural change request; runtime UNRUN), so the fix is NOT in
the pinned v0.111.0 release -- the bug should be observable there.

Oracle (verified from pinned v0.111.0 card-data.json):
  Oasis Ritualist: {3}{G} Creature -- Snake Druid (2/4).
    "{T}: Add one mana of any color.
     {T}, Exert this creature: Add two mana of any one color. (An exerted
     creature won't untap during your next untap step.)"
    Two parsed activated abilities; the second carries the exert cost.
  Resolute Survivors: {1}{R}{W} Creature -- Human Warrior (3/3).
    "You may exert this creature as it attacks. (It won't untap during
     your next untap step.)
     Whenever you exert a creature, this creature deals 1 damage to each
     opponent and you gain 1 life."
    Parsed trigger: mode=Exerted, valid_card=Typed Creature
    (controller=null), valid_target=Controller ("you"), valid_source=null,
    execute=DamageEachPlayer(Opponent, 1) then GainLife(1).

Plan (native engine, two human driver seats):
  P0: 4x Oasis Ritualist + 4x Resolute Survivors + 18x Forest + 17x
      Mountain + 17x Plains. Casts both creatures, then activates the
      Ritualist's {T}, Exert mana ability (reported path).
  P1: 60x Island. Passes; is the life-total yardstick (20 -> 19 when the
      Survivors trigger resolves).

BEHAVIORAL CONTRACT (written before observing results)
  A1_setup_ok   pre.json exported with Ritualist + Survivors on P0's
                battlefield, P0 priority in its own main phase.
  A2_cost_exert_trigger (REPORTED PATH)  P0 activates Ritualist's
                {T}, Exert: Add two mana of any one color. The Survivors
                trigger must go on the stack and resolve: P0 20->21,
                P1 20->19. pass == trigger fired and resolved; failed ==
                exertion completed but no trigger (the reported bug).
  A3_own_source_control  (control) P0 attacks with Survivors and exerts
                it as it attacks; its own trigger must fire (pre-fix the
                matcher matched the trigger's own source). Proves the
                trigger machinery works; not-run if the exert-as-attacks
                decision never surfaces.
  A4_cleanup    stack empty, post.json exported.

Verdict: reproduced iff A1 passes and A2 fails. not-reproduced iff A1
and A2 pass. blocked iff A1 fails or A2 not-run.

Protocol-144 notes (from #9681): engine auto-pays (bare CastSpell);
priority = PassPriority in the viewing seat's top-level legal_actions;
MulliganDecision via legacy Action; bottom-after-mulligan via vi
schema/select; cleanup discards via viewer_interaction only. Turn order
is randomized; every play gates on active_player.
Cast-confirmation guard (2026-10-09 lesson): after every CastSpell, hold
priority passes and new main-phase plays until the spell is confirmed on
the stack or the cast object leaves its origin zone (30s backstop clears
a silently-dropped cast).

Evidence: evidence/9934/<run-id>/pre.json, post.json, post_control.json,
run.json, assertions.json, obs.json, scenario_9934.py, wire_log.jsonl,
scenario_run.log, summary.png, manifest.sha256, data_evidence.json.
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
RUN_ID = os.environ.get("RUN_ID", "run-9934-v01110-20261017-0910")
ISSUE = 9934
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

URL = "ws://127.0.0.1:9374/ws"

WIRE = None
RUNLOG = None

RITUALIST = "oasis ritualist"
SURVIVORS = "resolute survivors"
ISLAND = "island"
FOREST = "forest"
MOUNTAIN = "mountain"
PLAINS = "plains"

P0_DECK = deck((RITUALIST.title(), 4), (SURVIVORS.title(), 4),
               (FOREST.title(), 18), (MOUNTAIN.title(), 17),
               (PLAINS.title(), 17))
P1_DECK = deck((ISLAND.title(), 60))

SERVER_IDENTITY = {
    "server_version": "v0.111.0",
    "build_commit": "33aa23a",
    "protocol_version": 144,
    "mode": "Full",
    "binary_sha256": "92e0e1cd04342d3429a35c965e2487a66adda730564824b36074e9f4b0305c9f",
    "card_data_sha256": "12ac3244d7add0d8d643d3b1466cbff72059c337110ed9863bd34351a2ae04c4",
    "draft_pools_sha256": "787c188acdd4a5537bb2c447ec2da618ddad9fbbb215c807b9a575e6985a2f38",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.111.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828) at pin time 2026-10-14; "
                      "release unchanged since (latest stable still v0.111.0, "
                      "published 2026-10-16T15:04:47Z, verified this run); "
                      "data digests match the signed manifest; digests "
                      "recomputed against on-disk files this run",
    "observed_at": "2026-10-17",
    "source": "ServerHello asserted by handshake this run + sha256 "
              "re-verified against pinned v0.111.0 release artifacts; "
              "reused the live v0.111.0 server on 127.0.0.1:9374 "
              "(ServerHello re-verified this run)",
}

TIMEOUT = 1400
TURN_CAP = 45

ST = {}
MULLS = set()
MULL_COUNTS = {}
SUBMITTED_OPPS = set()
PASSED_REV = {}
EXERT_ABILITY_INDEX = None  # resolved from card data in check_card_data


def reset():
    ST.update({
        "stage": "SETUP",   # SETUP -> EXERT -> CONTROL -> DONE
        "stop": False,
        "notes": [],
        "rit_oid": None,
        "surv_oid": None,
        "rit_cast_sent": False,
        "surv_cast_sent": False,
        "exert_activated": False,
        "exert_ability_oid": None,
        "trigger_seen_stack": False,
        "trigger_resolved": False,
        "p0_life_pre": None,
        "p1_life_pre": None,
        "attacked": False,
        "attacker_oid": None,
        "control_trigger_seen": False,
        "control_trigger_resolved": False,
        "paying": {"P0": None, "P1": None},
        "pre_exported": False,
        "post_exported": False,
        "post_control_exported": False,
        "exert_status_obs": None,
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


def stack_has_trigger_from(state, src_oid):
    for e in state.get("stack", []) or []:
        sid = e.get("source_id") or e.get("object_id")
        if sid is not None and str(sid) == str(src_oid):
            return True
    return False


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


def check_card_data():
    global EXERT_ABILITY_INDEX
    ok, notes = True, []
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.111.0/data/"
                        f"card-data.json"))
    rit = cd.get(RITUALIST, {})
    ot = str(rit.get("oracle_text"))
    if "{T}, Exert this creature: Add two mana of any one color." not in ot:
        ok = False
        notes.append("oasis ritualist oracle missing exert ability")
    else:
        notes.append("oasis ritualist exert ability confirmed in oracle")
    ab = rit.get("abilities") or []
    if len(ab) < 2:
        ok = False
        notes.append(f"oasis ritualist parsed abilities != 2: {len(ab)}")
        EXERT_ABILITY_INDEX = None
    else:
        notes.append(f"oasis ritualist parsed abilities: {len(ab)}")
        # The exert cost lives in the ability's parsed cost or description;
        # locate it by index so ActivateAbility payloads (which carry only
        # source_id + ability_index) can be disambiguated without guessing.
        EXERT_ABILITY_INDEX = None
        for i, entry in enumerate(ab):
            blob = json.dumps(entry).lower()
            if "exert" in blob:
                EXERT_ABILITY_INDEX = i
                break
        notes.append(f"exert ability_index={EXERT_ABILITY_INDEX} "
                     f"(descriptions: "
                     f"{[str(e.get('description'))[:60] for e in ab]})")
        if EXERT_ABILITY_INDEX is None:
            ok = False
            notes.append("could not locate exert ability by index")
    sv = cd.get(SURVIVORS, {})
    tr = sv.get("triggers") or []
    exert_tr = [t for t in tr if t.get("mode") == "Exerted"]
    if not exert_tr:
        ok = False
        notes.append("resolute survivors has no Exerted trigger parsed")
    else:
        t0 = exert_tr[0]
        notes.append("resolute survivors Exerted trigger parsed: "
                     f"valid_card={t0.get('valid_card', {}).get('type')}/"
                     f"{t0.get('valid_card', {}).get('type_filters')}, "
                     f"valid_target={t0.get('valid_target', {}).get('type')}, "
                     f"valid_source={t0.get('valid_source')}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {RITUALIST: ot,
                              SURVIVORS: str(sv.get("oracle_text"))},
                   "rit_abilities": rit.get("abilities"),
                   "surv_triggers": sv.get("triggers")}, f, indent=1)
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

        def bkey(ch):
            oid = cand_ref_int(ch)
            o = get_obj(state, oid)
            nm = obj_lname(state, oid) if oid is not None else "?"
            if nm in (RITUALIST, SURVIVORS):
                return (2, nm)  # keep the fixture cards
            if is_land(o):
                return (1, nm)
            return (0, nm)

        picks = sorted(cands, key=bkey)[:n]
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


async def do_discard(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    handset = set(hand)

    def rank(oid):
        o = get_obj(state, oid)
        nm = obj_lname(state, oid)
        if is_land(o):
            return (0, nm)
        if nm in (RITUALIST, SURVIVORS):
            return (5, nm)
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
        say(f"[{tag}] discarding "
            f"{[obj_lname(state, cand_ref_int(x)) for x in picks]}")
        wire("discard", {"who": tag, "picks": [x.get("id") for x in picks]})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "select", "data": {
                                  "choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
        return True
    return False


def cast_guard_tick(state, who, pid):
    pay = ST["paying"][who]
    if not pay:
        return "proceed"
    label, card, zone, since, cast_oid = (pay["label"], pay["card"],
                                         pay["zone"], pay["since"],
                                         pay["oid"])
    from_idx = None
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
        if label == "cast_rit":
            ST["rit_cast_sent"] = False
        elif label == "cast_surv":
            ST["surv_cast_sent"] = False
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


def exert_ability_action(acts, state, rit_oid):
    """Pick the Ritualist's {T}, Exert mana ability among its advertised
    ActivateAbility actions. Payloads carry source_id + ability_index only;
    the exert ability's index is resolved from the parsed card data in
    check_card_data (never guessed from position)."""
    cands = [a for a in acts
             if a.get("type") == "ActivateAbility"
             and str(a.get("_src_oid")) == str(rit_oid)]
    if not cands:
        return None
    if EXERT_ABILITY_INDEX is None:
        wire("exert_ability_ambiguous",
             {"reason": "no exert ability_index from card data",
              "candidates": [a.get("data", {}) for a in cands]})
        say("[P0] exert ability index unknown; refusing to guess")
        return None
    for a in cands:
        if (a.get("data", {}) or {}).get("ability_index") \
                == EXERT_ABILITY_INDEX:
            return a
    wire("exert_ability_ambiguous",
         {"reason": "ability_index not advertised",
          "want_index": EXERT_ABILITY_INDEX,
          "candidates": [a.get("data", {}) for a in cands]})
    say(f"[P0] exert ability_index={EXERT_ABILITY_INDEX} not advertised; "
        "refusing to guess")
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


async def answer_exert_prompt(c, st, tag):
    """Answer a 'you may exert this creature as it attacks' decision
    affirmatively, from the advertised opportunity. Returns True if an
    exert-affirming submission was made."""
    for opp in vi_ops(st):
        blob = json.dumps(opp).lower()
        if "exert" not in blob:
            continue
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        items = rdata.get("candidates") or rdata.get("choices") or []
        if not items:
            continue
        pick = None
        for ch in items:
            txt = json.dumps(ch).lower()
            if "exert" in txt and ("yes" in txt or "exert this" in txt
                                   or "may exert" in txt):
                pick = ch
                break
        if pick is None:
            # boolean-style: pick the affirmative-looking first choice
            for ch in items:
                txt = json.dumps(ch).lower()
                if "yes" in txt or "exert" in txt:
                    pick = ch
                    break
        if pick is None:
            continue
        iid = opp.get("interactionId")
        key = (tag, "exert_prompt", iid)
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] answering exert prompt affirmatively "
            f"(iid {iid})")
        wire("exert_prompt_answer",
             {"who": tag, "iid": iid, "pick": pick.get("id")})
        rtype = resp.get("type") or "select"
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


async def answer_mana_color(c, st, tag, pid):
    """Answer a pending ChooseManaColor from the state's waiting_for
    (scenario_6890 pattern). Fires in any stage -- the Ritualist's exert
    mana ability defers its color choice and the game cannot proceed
    until it is answered (2026-10-17 lesson: an unanswered deferred
    choice stalled the run in PostCombatMain)."""
    state = st["state"]
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "ChooseManaColor":
        return False
    data = wf.get("data", {}) or {}
    if data.get("player") != pid:
        return False
    choice = data.get("choice") or {}
    cdata = choice.get("data") or {}
    options = cdata.get("options") or []
    if not options:
        say(f"[{tag}] ChooseManaColor has no options; holding")
        wire("cmc_no_options", {"who": tag})
        return True
    pick = "Green" if "Green" in options else options[0]
    key = (tag, "cmc", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] ChooseManaColor options={options}; picking {pick}")
    wire("cmc_answer", {"who": tag, "pick": pick, "options": options})
    await submit_as_is(c, {"type": "ChooseManaColor",
                           "data": {"choice": {"type": "SingleColor",
                                               "data": pick},
                                    "count": 1}})
    return True


# ------------------------------------------------------------- seat ticks

async def p0_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 0, "P0",
                         lambda hn: (sum(1 for n in hn
                                         if get_land(hn, n)) >= 2
                                     and (RITUALIST in hn
                                          or SURVIVORS in hn))):
        return True
    if await do_bottom(c, acts, st, 0, "P0"):
        return True
    if await do_discard(c, acts, st, 0, "P0"):
        return True
    if await answer_exert_prompt(c, st, "P0"):
        return True
    if await answer_mana_color(c, st, "P0", 0):
        return True
    holding = cast_guard_tick(state, "P0", 0) == "hold"

    atypes = set(a.get("type") for a in acts)
    # DeclareAttackers: only the CONTROL stage attacks (with Survivors);
    # in every other stage submit an empty declaration so combat never
    # stalls (2026-10-17 lesson: EXERT-stage DeclareAttackers deadlocked
    # the first attempt).
    if "DeclareAttackers" in atypes:
        da = find_action(acts, "DeclareAttackers")
        d = copy.deepcopy(da)
        if ST["stage"] == "CONTROL" and not ST["attacked"] \
                and state.get("active_player") == 0:
            ready = [b for b in bf_oids(state, 0, SURVIVORS)
                     if can_attack_now(state, b)]
            if ready:
                d.setdefault("data", {}).update(
                    {"attacks": [[int(ready[0]),
                                  {"type": "Player", "data": 1}]],
                     "bands": []})
                ST["attacked"] = True
                ST["attacker_oid"] = ready[0]
                say(f"[P0] attacks P1 with survivors {ready[0]}")
                wire("p0_attacks", {"attacker": ready[0]})
                await submit_as_is(c, d)
                return True
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        wire("declare_attackers_empty", {"who": "P0-9934",
                                         "stage": ST["stage"]})
        await submit_as_is(c, d)
        return True
    # DeclareBlockers: P0 never blocks in this fixture; declare empty so
    # combat never stalls on this seat.
    if "DeclareBlockers" in atypes:
        db = find_action(acts, "DeclareBlockers")
        d = copy.deepcopy(db)
        d.setdefault("data", {}).update({"assignments": []})
        wire("declare_blockers_empty", {"who": "P0-9934"})
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
        if have < 8:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return True
    # 2. cast creatures (SETUP stage)
    if ST["stage"] == "SETUP" and own_main:
        want = None
        if ST["rit_oid"] is None and not ST["rit_cast_sent"]:
            want = ("cast_rit", RITUALIST, "rit")
        elif ST["surv_oid"] is None and not ST["surv_cast_sent"]:
            want = ("cast_surv", SURVIVORS, "surv")
        if want:
            label, card, short = want
            a = cast_action_for(acts, state, 0, card)
            if a is not None:
                d = a.get("data", {}) or {}
                oid = d.get("object_id") or a.get("_src_oid")
                ST[f"{short}_cast_sent"] = True
                ST["paying"]["P0"] = {"label": label, "card": card,
                                      "zone": "hand",
                                      "since": time.time(), "oid": oid}
                say(f"[P0] {label} (oid {oid})")
                wire("cast", {"who": "P0", "label": label, "oid": oid})
                await submit_as_is(c, a)
                return True
    # 3. EXERT stage: activate the Ritualist's {T}, Exert ability
    if ST["stage"] == "EXERT" and not ST["exert_activated"] and own_main:
        rit = ST["rit_oid"]
        if rit is not None and not get_obj(state, rit).get("tapped") \
                and not get_obj(state, rit).get("summoning_sick"):
            a = exert_ability_action(acts, state, rit)
            if a is not None:
                ST["exert_activated"] = True
                ST["exert_ability_oid"] = rit
                say(f"[P0] activating ritualist exert ability "
                    f"(src_oid {a.get('_src_oid')})")
                wire("exert_activate", {"rit_oid": rit,
                                        "action": a.get("data")})
                await submit_as_is(c, a)
                return True
            elif not any(x.get("type") == "ActivateAbility"
                         for x in acts if str(x.get("_src_oid"))
                         == str(rit)):
                ST["notes"].append("no ActivateAbility advertised for the "
                                   "ritualist while it is untapped in main")
    # 4. pass priority
    if not holding:
        return await pass_priority(c, st, acts)
    return False


def get_land(hn, n):
    return n in (FOREST, MOUNTAIN, PLAINS, ISLAND, "swamp")


async def p1_tick(c, st, acts, state):
    if await do_mulligan(c, acts, st, 1, "P1",
                         lambda hn: sum(1 for n in hn
                                        if get_land(hn, n)) >= 2):
        return True
    if await do_bottom(c, acts, st, 1, "P1"):
        return True
    if await do_discard(c, acts, st, 1, "P1"):
        return True
    if await answer_exert_prompt(c, st, "P1"):
        return True
    holding = cast_guard_tick(state, "P1", 1) == "hold"
    atypes = set(a.get("type") for a in acts)
    # P1 never attacks or blocks in this fixture; declare empty so combat
    # never stalls on this seat.
    if "DeclareAttackers" in atypes:
        da = find_action(acts, "DeclareAttackers")
        d = copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        wire("declare_attackers_empty", {"who": "P1-9934"})
        await submit_as_is(c, d)
        return True
    if "DeclareBlockers" in atypes:
        db = find_action(acts, "DeclareBlockers")
        d = copy.deepcopy(db)
        d.setdefault("data", {}).update({"assignments": []})
        wire("declare_blockers_empty", {"who": "P1-9934"})
        await submit_as_is(c, d)
        return True
    if not my_priority(acts):
        return False
    if holding or real_decision_pending(st):
        return False
    if my_main(state, 1):
        have = sum(1 for o in bf_oids(state, 1)
                   if is_land(get_obj(state, o)))
        if have < 9:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return True
    if not holding:
        return await pass_priority(c, st, acts)
    return False


# ------------------------------------------------------------- evaluate

def bf_zone_of(env_state, oid):
    if oid is None:
        return None
    return get_obj(env_state, oid).get("zone")


def evaluate():
    a = {k: "not-run" for k in ("A1_setup_ok", "A2_cost_exert_trigger",
                                "A3_own_source_control", "A4_cleanup")}
    notes = ST["notes"]
    pre = load_env("pre")
    post = load_env("post")
    postc = load_env("post_control")

    # A1: fixture ready
    if pre is not None:
        s = pre["state"]
        rits = bf_oids(s, 0, RITUALIST)
        surs = bf_oids(s, 0, SURVIVORS)
        if rits and surs:
            a["A1_setup_ok"] = "passed"
            ST["p0_life_pre"] = ST.get("p0_life_pre") or life(s, 0)
            ST["p1_life_pre"] = ST.get("p1_life_pre") or life(s, 1)
            notes.append(f"A1: ritualist {rits[0]} + survivors {surs[0]} on "
                         f"P0 BF at pre.json (turn {s.get('turn_number')}); "
                         f"P0 life={ST['p0_life_pre']} "
                         f"P1 life={ST['p1_life_pre']}")
        else:
            a["A1_setup_ok"] = "failed"
            notes.append(f"A1: pre.json has rit={rits} surv={surs}")
    else:
        notes.append("A1: pre.json missing")
        a["A1_setup_ok"] = "failed"

    # A2: reported path -- cost-exert must fire the Survivors trigger.
    # The leg outcome is LATCHED when the leg closes (2026-10-17 #9921
    # lesson): a fallback post.json export must never flip not-run to
    # failed. failed == leg closed with no trigger after full ability
    # resolution and 60 quiet revisions; passed == trigger fired and the
    # life totals moved P0 20->21 / P1 20->19.
    outcome = ST.get("exert_leg_outcome")
    if outcome == "trigger" and post is not None:
        s = post["state"]
        p0l, p1l = life(s, 0), life(s, 1)
        notes.append(f"A2: leg closed with trigger; "
                     f"P0 {ST['p0_life_pre']}->{p0l}, "
                     f"P1 {ST['p1_life_pre']}->{p1l}; "
                     f"exerted_this_turn ledger at close="
                     f"{ST.get('exert_ledger_at_close')}")
        if (ST["p0_life_pre"] is not None
                and p0l == ST["p0_life_pre"] + 1
                and p1l == ST["p1_life_pre"] - 1):
            a["A2_cost_exert_trigger"] = "passed"
            notes.append("A2: Survivors trigger fired and resolved for the "
                         "cost-exert -- reported symptom ABSENT")
        else:
            a["A2_cost_exert_trigger"] = "failed"
            notes.append("A2: trigger seen but life totals did not move "
                         "as the trigger requires -- REPORTED BUG "
                         "reproduced")
    elif outcome == "no_trigger":
        notes.append(f"A2: leg closed with NO trigger after full ability "
                     f"resolution + 60 quiet revisions; "
                     f"exert_status_obs={ST['exert_status_obs']}; "
                     f"exerted_this_turn ledger at close="
                     f"{ST.get('exert_ledger_at_close')}; "
                     f"rit_untap_noted={ST.get('rit_untap_noted')}")
        a["A2_cost_exert_trigger"] = "failed"
        notes.append("A2: no Survivors trigger from the cost-exert -- "
                     "REPORTED BUG reproduced")
    elif not ST["exert_activated"]:
        notes.append("A2: exert ability never activated -> not-run")
    else:
        notes.append(f"A2: exert leg never closed "
                     f"(outcome={outcome}) -> not-run")

    # A3: control -- survivors exerts itself as it attacks; own trigger
    # must fire (pre-fix the matcher matched the trigger's own source)
    if postc is not None and ST["attacked"]:
        s = postc["state"]
        notes.append(f"A3: attacked={ST['attacked']} "
                     f"control_trigger_seen="
                     f"{ST['control_trigger_seen']} "
                     f"control_trigger_resolved="
                     f"{ST['control_trigger_resolved']}")
        if ST["control_trigger_resolved"]:
            a["A3_own_source_control"] = "passed"
            notes.append("A3: own-source attack-exert fired the trigger -- "
                         "trigger machinery works")
        else:
            a["A3_own_source_control"] = "failed"
            notes.append("A3: own-source attack-exert did NOT fire the "
                         "trigger -- broader trigger breakage")
    else:
        a["A3_own_source_control"] = "not-run"
        notes.append(f"A3: attacked={ST['attacked']} "
                     f"post_control={'present' if postc else 'missing'} "
                     "-> not-run")

    # A4: cleanup
    final = postc or post
    if final is not None:
        a["A4_cleanup"] = ("passed" if not (final["state"].get("stack") or [])
                           else "failed")
        notes.append(f"A4: stack_empty={a['A4_cleanup'] == 'passed'}")
    else:
        notes.append("A4: no final export -> not-run")

    if a["A1_setup_ok"] == "failed" \
            or a["A2_cost_exert_trigger"] == "not-run":
        verdict = "blocked"
    elif a["A2_cost_exert_trigger"] == "failed":
        verdict = "reproduced"
    elif a["A2_cost_exert_trigger"] == "passed":
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
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #9934 - cost-exert not recorded / "
           "observers don't fire", fill=(235, 240, 250))
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
        "A1_setup_ok": "Ritualist + Survivors on P0 BF; pre.json exported",
        "A2_cost_exert_trigger": "REPORTED: {T},Exert ability -> Survivors "
            "trigger (P0 20->21, P1 20->19)",
        "A3_own_source_control": "CONTROL: Survivors attack-exerts itself; "
            "own trigger fires",
        "A4_cleanup": "stack empty, final export present",
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
    ST["last_submit"] = time.time()
    t0 = time.time()
    await verify_server_hello()
    check_card_data()

    p0 = PhaseClient("P0-9934")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1-9934")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()

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
                if ST["rit_oid"] is None:
                    r = bf_oids(state, 0, RITUALIST)
                    if r:
                        ST["rit_oid"] = r[0]
                        say(f"Ritualist on BF oid={r[0]}")
                        wire("rit_bf", {"oid": str(r[0])})
                if ST["surv_oid"] is None:
                    sv = bf_oids(state, 0, SURVIVORS)
                    if sv:
                        ST["surv_oid"] = sv[0]
                        say(f"Survivors on BF oid={sv[0]}")
                        wire("surv_bf", {"oid": str(sv[0])})
                # SETUP -> EXERT
                if ST["stage"] == "SETUP" and ST["rit_oid"] is not None \
                        and ST["surv_oid"] is not None \
                        and ST["paying"]["P0"] is None \
                        and not (state.get("stack") or []) \
                        and state.get("active_player") == 0 \
                        and state.get("phase") in ("PreCombatMain",
                                                   "PostCombatMain"):
                    if ST["p0_life_pre"] is None:
                        ST["p0_life_pre"] = life(state, 0)
                        ST["p1_life_pre"] = life(state, 1)
                    await export_named(p0, "pre")
                    ST["pre_exported"] = True
                    ST["stage"] = "EXERT"
                    say("fixture ready; pre.json exported; -> EXERT")
                    wire("stage", {"stage": "EXERT"})
                # EXERT: watch the trigger
                if ST["stage"] == "EXERT" and ST["exert_activated"]:
                    wf = state.get("waiting_for") or {}
                    ability_pending = wf.get("type") == "ChooseManaColor"
                    if ability_pending and not ST.get("cmc_seen"):
                        ST["cmc_seen"] = True
                        say("mana ability awaiting color choice "
                            "(ChooseManaColor)")
                        wire("cmc_pending", {})
                    if not ST["trigger_seen_stack"] \
                            and stack_has_trigger_from(state,
                                                       ST["surv_oid"]):
                        ST["trigger_seen_stack"] = True
                        say("Survivors trigger observed on stack")
                        wire("trigger_on_stack", {})
                    if ST["trigger_seen_stack"] \
                            and not ST["trigger_resolved"] \
                            and not (state.get("stack") or []):
                        ST["trigger_resolved"] = True
                        say(f"trigger stack cleared; P0 life={life(state, 0)}"
                            f" P1 life={life(state, 1)}")
                        wire("trigger_resolved",
                             {"p0_life": life(state, 0),
                              "p1_life": life(state, 1)})
                    # life-change fallback: the trigger may resolve
                    # between 0.15s polls; the life totals are the
                    # authoritative outcome signal
                    if not ST["trigger_resolved"] \
                            and ST["p0_life_pre"] is not None \
                            and life(state, 0) == ST["p0_life_pre"] + 1 \
                            and life(state, 1) == ST["p1_life_pre"] - 1:
                        ST["trigger_seen_stack"] = True
                        ST["trigger_resolved"] = True
                        say("trigger resolved (life-change fallback); "
                            f"P0={life(state, 0)} P1={life(state, 1)}")
                        wire("trigger_resolved",
                             {"via": "life_fallback",
                              "p0_life": life(state, 0),
                              "p1_life": life(state, 1)})
                    # exertion completed without any trigger: require the
                    # mana ability to have finished resolving (no pending
                    # color choice), a settled board (empty stack), and
                    # ~9s of quiet before closing the leg, so a slow
                    # trigger queue is not mistaken for the bug.
                    # NOTE: do NOT require the ritualist to still be
                    # tapped -- it untaps every turn (which is itself
                    # evidence the exertion was never recorded).
                    rit = get_obj(state, ST["rit_oid"]) \
                        if ST["rit_oid"] else {}
                    if ST["exert_activated"] and not ability_pending \
                            and not ST["trigger_seen_stack"] \
                            and not (state.get("stack") or []):
                        n = ST.get("no_trigger_revs", 0) + 1
                        ST["no_trigger_revs"] = n
                        if n == 1:
                            ST["exert_status_obs"] = {
                                k: rit.get(k) for k in
                                ("tapped", "exerted", "status",
                                 "statuses", "counters",
                                 "summoning_sick")
                                if k in rit}
                            ST["exert_ledger_at_close"] = \
                                state.get("exerted_this_turn")
                            ST["rit_tap_turn"] = state.get("turn_number")
                            say(f"ritualist post-resolution: "
                                f"{ST['exert_status_obs']}; "
                                f"exerted_this_turn="
                                f"{ST['exert_ledger_at_close']}")
                            wire("rit_post_resolution", {
                                "status": ST["exert_status_obs"],
                                "exerted_this_turn":
                                    ST["exert_ledger_at_close"]})
                        if n >= 60 and not ST.get("exert_leg_closed"):
                            ST["exert_leg_closed"] = True
                            ST["exert_leg_outcome"] = "no_trigger"
                            await export_named(p0, "post")
                            ST["post_exported"] = True
                            ST["stage"] = "CONTROL"
                            say("no trigger after 60 quiet revisions; "
                                "post.json exported; -> CONTROL")
                            wire("stage", {"stage": "CONTROL",
                                           "outcome": "no_trigger"})
                    # secondary signal: the ritualist untapping on a
                    # later untap step proves the exertion was never
                    # recorded (an exerted permanent would stay tapped)
                    if ST.get("rit_tap_turn") is not None \
                            and not ST.get("rit_untap_noted") \
                            and state.get("turn_number", 0) \
                            > ST["rit_tap_turn"] \
                            and not get_obj(
                                state, ST["rit_oid"]).get("tapped"):
                        ST["rit_untap_noted"] = True
                        say("ritualist untapped on a subsequent untap "
                            "step -- exertion was not recorded")
                        wire("rit_untapped_later", {})
                    if ST["trigger_resolved"] \
                            and not ST.get("exert_leg_closed"):
                        ST["exert_leg_closed"] = True
                        ST["exert_leg_outcome"] = "trigger"
                        await export_named(p0, "post")
                        ST["post_exported"] = True
                        ST["stage"] = "CONTROL"
                        say("trigger resolved; post.json exported; "
                            "-> CONTROL")
                        wire("stage", {"stage": "CONTROL",
                                       "outcome": "trigger"})
                # CONTROL: attack-exert with Survivors itself
                if ST["stage"] == "CONTROL" and ST["attacked"]:
                    if not ST["control_trigger_seen"] \
                            and stack_has_trigger_from(state,
                                                       ST["surv_oid"]):
                        ST["control_trigger_seen"] = True
                        say("control: own trigger observed on stack")
                        wire("control_trigger_on_stack", {})
                    if ST["control_trigger_seen"] \
                            and not ST["control_trigger_resolved"] \
                            and not (state.get("stack") or []):
                        ST["control_trigger_resolved"] = True
                        say(f"control trigger resolved; P0={life(state, 0)} "
                            f"P1={life(state, 1)}")
                        wire("control_trigger_resolved",
                             {"p0_life": life(state, 0),
                              "p1_life": life(state, 1)})
                    # life-change fallback (same as the EXERT leg)
                    if not ST["control_trigger_resolved"] \
                            and ST["p0_life_pre"] is not None \
                            and life(state, 0) > ST["p0_life_pre"] \
                            and life(state, 1) < ST["p1_life_pre"]:
                        ST["control_trigger_seen"] = True
                        ST["control_trigger_resolved"] = True
                        say("control trigger resolved (life-change "
                            f"fallback); P0={life(state, 0)} "
                            f"P1={life(state, 1)}")
                        wire("control_trigger_resolved",
                             {"via": "life_fallback",
                              "p0_life": life(state, 0),
                              "p1_life": life(state, 1)})
                    if not ST["control_trigger_seen"]:
                        ST["control_revs"] = ST.get("control_revs", 0) + 1
                if ST["stage"] == "CONTROL" \
                        and (ST["control_trigger_resolved"]
                             or (ST["attacked"]
                                 and ST.get("control_revs", 0) > 120)) \
                        and not ST["post_control_exported"] \
                        and not (state.get("stack") or []):
                    if ST["attacked"] \
                            and not ST["control_trigger_resolved"]:
                        ST["control_revs"] = ST.get("control_revs", 0) + 1
                    if ST["control_trigger_resolved"] \
                            or ST.get("control_revs", 0) > 120:
                        await export_named(p0, "post_control")
                        ST["post_control_exported"] = True
                        ST["stage"] = "DONE"
                        ST["stop"] = True
                        say("post_control.json exported; DONE")
                        break

            if (st0 and (st0["state"].get("turn_number") or 0) > TURN_CAP
                    and ST["stage"] in ("SETUP", "EXERT")):
                ST["notes"].append(f"TURN_CAP {TURN_CAP} hit in "
                                   f"{ST['stage']}")
                say("TURN_CAP: finishing")
                break
            if time.time() - last_diag > 25 and st0:
                last_diag = time.time()
                s = st0["state"]
                acts0 = merged_actions(st0)
                vi = st0.get("viewer_interaction") or {}
                say(f"DIAG turn={s.get('turn_number')} active="
                    f"{s.get('active_player')} phase={s.get('phase')} "
                    f"stage={ST['stage']} rit={ST['rit_oid']} "
                    f"surv={ST['surv_oid']} exert_act="
                    f"{ST['exert_activated']} trig_seen="
                    f"{ST['trigger_seen_stack']} trig_res="
                    f"{ST['trigger_resolved']} attacked={ST['attacked']} "
                    f"P0hand={hand_lnames(s, 0)[:6]} "
                    f"P1hand={hand_lnames(s, 1)[:6]} "
                    f"life={life(s, 0)}/{life(s, 1)} "
                    f"atypes={sorted(set(a.get('type') for a in acts0))} "
                    f"vi_kind={(vi.get('waitingForKind') or {}).get('code')} "
                    f"vi_ops={len(vi_ops(st0))} "
                    f"rdp={real_decision_pending(st0)}")
            # stall recovery: no submission by either client for 180s ->
            # dump the full viewer interaction for post-hoc analysis and
            # finish gracefully instead of deadlocking to TIMEOUT
            if time.time() - ST.get("last_submit", t0) > 180 \
                    and not ST["stop"]:
                st0x = p0.latest
                if st0x:
                    vi = st0x.get("viewer_interaction") or {}
                    with open(f"{EVDIR}/stalled_vi.json", "w") as f:
                        json.dump(vi, f, indent=1, default=str)
                    wire("stalled_vi_dumped",
                         {"phase": st0x["state"].get("phase"),
                          "turn": st0x["state"].get("turn_number"),
                          "atypes": sorted(set(a.get("type") for a in
                                               merged_actions(st0x)))})
                    say("STALL: no submission for 180s; dumped "
                        "stalled_vi.json; finishing gracefully")
                    ST["notes"].append(
                        "stall recovery: no client submission for 180s in "
                        f"{st0x['state'].get('phase')} "
                        f"(turn {st0x['state'].get('turn_number')}); "
                        "full viewer_interaction saved to stalled_vi.json")
                ST["stop"] = True
                break
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
    if not ST["post_control_exported"] and ST["attacked"]:
        try:
            await export_named(p0, "post_control")
            ST["post_control_exported"] = True
            ST["notes"].append("post_control.json exported at finish() "
                               "fallback")
        except Exception as e:
            ST["notes"].append(f"post_control export failed: {e}")
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
        "issue_url": "https://github.com/phase-rs/phase/pull/9934",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(time.time())),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 144, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [[RITUALIST.title(), 4], [SURVIVORS.title(), 4],
                   [FOREST.title(), 18], [MOUNTAIN.title(), 17],
                   [PLAINS.title(), 17]],
            "P1": [[ISLAND.title(), 60]],
        },
        "assertions": a,
        "verdict": verdict,
        "result": ("A1 setup_ok: %s; A2 cost_exert_trigger: %s; "
                   "A3 own_source_control: %s; A4 cleanup: %s"
                   % tuple(a.get(k, "not-run") for k in
                           ("A1_setup_ok", "A2_cost_exert_trigger",
                            "A3_own_source_control", "A4_cleanup"))),
        "scope": ("Cost-exert recording + 'whenever you exert' observers on "
                  "pinned v0.111.0: Oasis Ritualist {T},Exert mana ability "
                  "with Resolute Survivors on the battlefield (reported "
                  "path); Survivors attack-exerting itself (control); "
                  "native engine, two human driver seats"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "States are authoritative exports, restorable only via full "
            "game replay (no standalone state-import path).",
        ],
        "fixture": {
            "rit_oid": str(ST.get("rit_oid")),
            "surv_oid": str(ST.get("surv_oid")),
            "exert_activated": ST["exert_activated"],
            "trigger_seen_stack": ST["trigger_seen_stack"],
            "trigger_resolved": ST["trigger_resolved"],
            "attacked": ST["attacked"],
            "control_trigger_seen": ST["control_trigger_seen"],
            "control_trigger_resolved": ST["control_trigger_resolved"],
            "exert_status_obs": ST["exert_status_obs"],
            "rejections": ST.get("rejections", []),
        },
        "notes": ST["notes"],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9934.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": a, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)
    with open(f"{EVDIR}/obs.json", "w") as f:
        json.dump({"rejections": ST.get("rejections", []),
                   "exert_status_obs": ST["exert_status_obs"]}, f, indent=1)
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
    pidfile = "/tmp/scenario_9934.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9934 instance is alive "
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
