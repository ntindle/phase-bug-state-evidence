#!/usr/bin/env python3
"""Issue #7159 re-validation on v0.110.0 (protocol 142): The Irencrag -
equip {3} missing after the become-Equipment transformation.

Reported: when you take the Irencrag's trigger option, it becomes a
legendary Equipment named Everflame, Heroes' Legacy and loses the mana
ability, but equip {3} is missing.

Prior run (2026-09-14, v0.82.0/protocol 70, run 20260914-7159):
reproduced - A1/A2/A3/A4/A5 passed; A6 equip_offered FAILED across 6
clean main-phase turns; A7 not-run; A8 passed. Published + comment
maintained.

This script is a protocol-142 port of the verified scenario_7159.py
(driver conventions from the verified protocol-142 scenario_1234_01100.py):
  - waiting_for is gone (null); prompt detection via
    viewer_interaction.waitingForKind.code + opportunities (canSubmit).
  - PlayLand/CastSpell/ActivateAbility live in legal_actions_by_object;
    merged_actions() merges them with _src_oid (plain top-level
    legal_actions hides them).
  - CastSpell submitted EXACTLY as advertised (no target injection);
    payment_mode Auto - the engine taps lands itself; never inject targets.
  - Priority = PassPriority legal action advertised to the viewing seat.
  - Cast-in-flight guard: after submitting a CastSpell, withhold our own
    PassPriority until the engine processes the cast (revision advance or
    object leaves hand), or a bare CastSpell can lose the race to our pass
    and be silently dropped (cf. #6765).
  - Optional-effect accept via vi exactChoices decideOptionalEffect action
    code + value surface (role=accept, value="true"); ReplacementChoice
    fallback via optionIndex copy candidate (cf. #7167/#9852).
  - MulliganDecision answered via legacy Action; bottoming via vi
    schema/select (waitingForKind.code == 'mulligan' at turn 1/Untap) or
    verbatim SelectCards; DiscardToHandSize via vi schema/select offering
    hand cards.

Behavioral contract (2 human seats, native engine, Bo1, protocol 142):
  P0: 12x The Irencrag, 12x Isamaru, Hound of Konda ({W} 2/2 vanilla
    legendary), 18x Plains, 18x Mountain. Land every turn; cast Irencrag
    at 2+ lands; cast Isamaru once Irencrag is on the battlefield.
  P1: 60x Forest; passive punching bag (keep mulligan, never play lands,
    never cast, no attacks, no blocks; hand-size discards lands first).
  Isamaru ETB => Irencrag trigger => optional-effect prompt => ACCEPT via
    the choice whose value surface is (role=accept, value "true")
    (decideOptionalEffect action code; never pick by text).
  A1 parse_ok:      trigger AST carries the become-Equipment mods + the
                    sub_ability RemoveAllAbilities / GrantStaticAbility
                    (+3/+3, EquippedBy) / GrantAbility equip x2 (record the
                    duplicated equip grant as a secondary parse defect).
  A2 setup_ok:      Irencrag on P0 BF; Isamaru entered; the trigger fired.
  A3 option_offered: optional-effect prompt seen; accepted with accept=true.
  A4 transform_ok:  post-resolution object named "Everflame, Heroes'
                    Legacy", still Legendary Artifact, subtype Equipment.
  A5 mana_lost:     original {T}: Add {C} gone from the object's current
                    abilities (mid.json).
  A6 equip_offered: an equip {3} activation offered for the transformed
                    object across 6 clean main-phase turns (merged
                    ActivateAbility actions AND viewer_interaction
                    activateAbility opportunities). THE REPORTED BUG -
                    expect failed if it reproduces.
  A7 equip_resolves: if A6 passes, activate it, answer target (Isamaru),
                    assert attached_to points at Isamaru and Isamaru is
                    5/5. Not-run if A6 fails.
  A8 cleanup:       stack empty; game advanced >= 2 full turns after
                    resolution.

Verdict: reproduced iff A2, A3, A4 pass and A6 fails. not-reproduced iff
A2-A8 all pass. blocked iff A2/A3 cannot be established.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402
import websockets as wsmod  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-7159-reval-v01100-20261014-1911")
ISSUE = 7159
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

IRENCRAG = "The Irencrag"
ISAMARU = "Isamaru, Hound of Konda"
EVERFLAME = "Everflame, Heroes' Legacy"
PLAINS, MOUNTAIN, FOREST = "Plains", "Mountain", "Forest"
LANDS = ("Plains", "Mountain", "Forest", "Swamp", "Island")

P0_DECK = [(IRENCRAG, 12), (ISAMARU, 12), (PLAINS, 18), (MOUNTAIN, 18)]
P1_DECK = [(FOREST, 60)]
TIMEOUT = 1500

CD_PATH = f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"

SERVER_IDENTITY = {
    "server_version": "0.110.0",
    "build_commit": "9bfd12e",
    "protocol_version": 142,
    "mode": "Full",
    "binary_sha256": "346ad6a1a8cd887bb49108fa132476bea36e42e7cfc5116fe2cbdf8ad369e3f2",
    "card_data_sha256": "c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74",
    "draft_pools_sha256": "7e45abbf0a964959192d2528c067a2a9caab04438640ab63b9ad79d2ce0e4676",
    "signature_verified": True,
    "signature_key_id": "436711b6a2d36828",
    "observed_at": "2026-10-14",
    "source": "isolated v0.110.0 single-user server on 127.0.0.1:9374 "
              "(ServerHello re-verified at run start) + verified pin "
              "(minisign-verify of binary + signed data manifest with the "
              "repo-pinned key).",
}

ST = {}
WF_SEEN = []
ACTED = {}
C0 = C1 = None
SUBMITTED_OPPS = set()
MULLS = set()
LAND_PLAYED_TURN = {}


def reset():
    ST.clear()
    ACTED.clear()
    ST.update({
        "stage": "SETUP",
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "hello_ok": False,
        "exports": {},
        "rejections": [],
        "life": {0: 20, 1: 20},
        "a1": "not-run",
        "irencrag_cast": False,
        "irencrag_cast_rev": None,
        "isamaru_cast": False,
        "isamaru_cast_rev": None,
        "isamaru_cast_turn": None,
        "trigger_on_stack_seen": False,
        "trigger_stack_record": None,
        "option_offered": False,
        "option_accepted": False,
        "pre_exported": False,
        "transformed": False,
        "transformed_oid": None,
        "transform_record": None,
        "mid_exported": False,
        "resolve_turn": None,
        "offer_scans": [],
        "equip_ability_index": None,
        "equip_descriptions": [],
        "equip_offered_seen": False,
        "equip_activated": False,
        "equip_activated_at": None,
        "equip_activated_rev": None,
        "equip_target_oid": None,
        "equip_resolved": False,
        "equip_resolved_pt": None,
        "equip_giveup": False,
        "cast_pending": None,  # (oid, rev) cast-in-flight guard
        "unexpected": [],
        "notes": [],
        "t0": time.time(),
    })
    WF_SEEN.clear()
    SUBMITTED_OPPS.clear()
    MULLS.clear()
    LAND_PLAYED_TURN.clear()


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
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "error": str(e)}) + "\n")
    WIRE.flush()


def obj_lname(state, oid):
    try:
        o = get_obj(state, oid)
    except Exception:
        return ""
    return str(o.get("card_name") or o.get("name")
               or o.get("base_name") or "").lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def hand_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Hand"
            and str(o.get("controller", -1)) == str(pid)]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == name.lower()]


def find_hand(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name.lower():
            return o
    return None


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")]


def untapped_land_names(state, pid):
    return [obj_lname(state, o) for o in untapped_lands(state, pid)]


def life(state, pid):
    for p in state.get("players", []):
        if str(p.get("id", p.get("seat", -1))) == str(pid):
            return p.get("life")
    for p in state.get("players", []):
        if p.get("life") is not None:
            pass
    return None


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


def merged_actions(st):
    """Top-level legal_actions + legal_actions_by_object (with _src_oid).
    Protocol 118+: PlayLand/CastSpell/ActivateAbility are hidden unless
    merged."""
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


def action_type_codes(ch):
    """Codes from action-type surfaces only (summary/value surfaces carry
    marker codes like "candidate" that are not decisions)."""
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if s.get("type") == "action"
            and isinstance(s.get("data"), dict)]


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


def action_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg = {"type": a["type"], "data": a["data"]}
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
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = action_codes(ch)
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def verify_server_hello():
    ws = await wsmod.connect("ws://127.0.0.1:9374/ws", max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.110.0"), f"unexpected version {ver}"
    assert int(proto) == 142, f"unexpected protocol {proto}"
    assert str(build) == "9bfd12e", f"unexpected build {build}"
    ST["server_hello"] = hello
    ST["hello_ok"] = True

# ---------------- parse check (A1) ----------------------------------------
def parse_check():
    cd = json.load(open(CD_PATH))
    t = cd["the irencrag"]
    trigs = t.get("triggers") or []
    rec = {"n_triggers": len(trigs)}
    ok_main = ok_sub = False
    equip_grants = []
    trig = trigs[0] if trigs else {}
    rec["mode"] = trig.get("mode")
    rec["optional"] = trig.get("optional")
    rec["valid_card"] = trig.get("valid_card")
    exe = (trig.get("execute") or {})
    eff = (exe.get("effect") or {})
    statics = eff.get("static_abilities") or []
    main_mods = []
    for sa in statics:
        for m in sa.get("modifications") or []:
            main_mods.append(m.get("type"))
    rec["main_effect_modifications"] = main_mods
    ok_main = all(k in main_mods for k in
                  ("SetTextName", "AddSupertype", "AddType",
                   "RemoveAllSubtypes", "AddSubtype"))
    sub = (exe.get("sub_ability") or {})
    cond = (sub.get("condition") or {})
    rec["sub_condition"] = cond
    sub_eff = (sub.get("effect") or {})
    sub_statics = sub_eff.get("static_abilities") or []
    sub_mods = []
    for sa in sub_statics:
        for m in sa.get("modifications") or []:
            sub_mods.append(m.get("type"))
            if m.get("type") == "GrantAbility":
                definition = m.get("definition") or {}
                equip_grants.append(definition.get("description"))
    rec["sub_ability_modifications"] = sub_mods
    rec["equip_grant_descriptions"] = equip_grants
    rec["duplicated_equip_grant"] = len(equip_grants) == 2
    rec["static_grant_present"] = "GrantStaticAbility" in sub_mods
    ok_sub = (cond.get("type") == "EffectOutcome"
              and cond.get("signal") == "OptionalEffectPerformed"
              and "RemoveAllAbilities" in sub_mods
              and "GrantStaticAbility" in sub_mods
              and len(equip_grants) >= 1)
    blob = json.dumps(t)
    rec["no_unimplemented_nodes"] = "Unimplemented" not in blob
    with open(f"{EVDIR}/parse_irencrag.json", "w") as f:
        json.dump(rec, f, indent=1)
    ok = ok_main and ok_sub and rec["no_unimplemented_nodes"]
    say(f"A1 parse_ok: {'passed' if ok else 'failed'} "
        f"(main={ok_main} sub={ok_sub} equip_grants={len(equip_grants)} "
        f"duplicated={rec['duplicated_equip_grant']})")
    ST["a1"] = "passed" if ok else "failed"
    ST["parse_rec"] = rec
    return ST["a1"]


# ---------------- exports --------------------------------------------------
async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    ST["exports"][path] = True
    say(f"exported {path}")
    return json.loads(s)


def load_state(path):
    try:
        with open(f"{EVDIR}/{path}") as f:
            env = json.load(f)
        return env.get("state", env)
    except Exception:
        return None


# ---------------- preamble (mulligan / bottom / discard / combat) ---------
async def do_mulligan(c, acts, st, pid, tag, keep_names):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in action_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", str(iid or c.revision))
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    nlands = sum(1 for n in hn if n in [l.lower() for l in LANDS])
    mull_count = sum(1 for k in SUBMITTED_OPPS
                     if k[0] == tag and k[1] == "mull")
    keep = (nlands >= 2) or (nlands >= 1 and any(n in hn for n in keep_names)) \
        or mull_count >= 3
    say(f"[{tag}] mulligan: {nlands} lands, hand={hn} -> "
        f"{'keep' if keep else 'redo'}")
    wire("mulligan", {"who": tag, "decision": "keep" if keep else "mulligan",
                      "hand": hn, "iid": str(iid)[:12]})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep" if keep else "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan: gated on waitingForKind.code == 'mulligan' at
    turn 1 / Untap. Prefer the vi schema/select opportunity; fall back to
    the verbatim per-card SelectCards actions."""
    state = st["state"]
    if vi_kind_code(st) != "mulligan":
        return False
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = (resp.get("data", {}) or {}).get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    con = None
    if target:
        con = (((target[2].get("data") or {}).get("constraint") or {})
               .get("data") or {})
    else:
        a0 = sel_acts[0].get("data", {}) or {}
        con = a0.get("constraint") or {}
        if isinstance(con, dict):
            con = (con.get("data") or {})
    n = int((con or {}).get("min") or (con or {}).get("max") or 1)
    if n <= 0:
        return False
    key = (tag, "bottom", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    protect = {IRENCRAG.lower(), ISAMARU.lower()} if pid == 0 else set()

    def rank_oid(o):
        nm = obj_lname(state, o)
        if nm in protect:
            return (2, str(o))
        if is_land(get_obj(state, o)):
            return (0, str(o))
        return (1, str(o))

    picks = sorted(hand_ids(state, pid), key=rank_oid)[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, o) for o in picks]}")
    if target and all(_cand_reference(ch) is not None
            for ch in target[1]):
        opp = target[0]
        id_by_ref = {}
        for ch in target[1]:
            id_by_ref[str(_cand_reference(ch))] = ch.get("id")
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "select",
                            "data": {"choiceIds": [id_by_ref[str(o)] for o in picks
                                                   if str(o) in id_by_ref]}}}
        wire("bottom", {"who": tag, "via": "vi", "picks": picks})
        await interact_as(c, sub, tag)
        return True
    wire("bottom", {"who": tag, "via": "verbatim_selectcards", "picks": picks})
    for a in sel_acts:
        cards = (a.get("data", {}) or {}).get("cards") or []
        if cards and int(cards[0]) in picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(cards[0])]}})
            await asyncio.sleep(0.4)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    protect = {IRENCRAG.lower(), ISAMARU.lower()} if pid == 0 else set()
    handset = set(str(x) for x in hand)
    found_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if any(str(_cand_reference(ch)) in handset for ch in cands):
            found_opp = (opp, cands, spec)
            break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    opp, cands, spec = found_opp
    id_by_ref = {}
    for ch in cands:
        r = _cand_reference(ch)
        if r is not None:
            id_by_ref[str(r)] = ch.get("id")

    def rank_oid(o):
        nm = obj_lname(state, o)
        if nm in protect:
            return (5, str(o))
        if is_land(get_obj(state, o)):
            return (0, str(o))
        return (2, str(o))

    picks = sorted(hand, key=rank_oid)[:max(1, n)]
    stype = spec.get("type") or "select"
    say(f"[{tag}] discarding to hand size via vi ({stype}): "
        f"{[obj_lname(state, o) for o in picks]}")
    wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": [id_by_ref[str(o)]
                                                               for o in picks
                                                               if str(o) in id_by_ref]}}},
                      tag)
    return True


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


# ---------------- optional-effect accept (A2/A3) ---------------------------
def stack_trigger_sample(state):
    """Scan the stack for the Irencrag trigger entry (discriminate by the
    ability's own description text)."""
    hits = []
    for e in state.get("stack") or []:
        kind = e.get("kind") or {}
        if (kind.get("type") or "") != "TriggeredAbility":
            continue
        ab = e.get("ability") or kind.get("ability") or {}
        desc = str(ab.get("description") or "")
        if "become a legendary equipment" in desc.lower():
            hits.append({"id": e.get("id"),
                         "source_id": e.get("source_id"),
                         "description": desc[:160]})
    return hits


def decide_optional_accept_choice(opp):
    """Pick the accept=true choice from a decideOptionalEffect-style
    exactChoices opportunity."""
    resp = opp.get("response", {}) or {}
    chs = (resp.get("data") or {}).get("choices") or []
    for ch in chs:
        if "decideOptionalEffect" not in action_codes(ch):
            continue
        for sf in ch.get("surfaces", []) or []:
            d = sf.get("data") or {}
            if (sf.get("type") == "value" and d.get("role") == "accept"
                    and str(d.get("value")).lower() == "true"):
                return ch
    return None


async def handle_optional_effect(c, st, tag):
    """Answer the Irencrag 'you may have it become...' prompt with
    accept=true (never by text). Returns True if it acted or is holding."""
    if ST["option_accepted"]:
        return False
    holding = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "optional", str(iid))
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        chs = (resp.get("data") or {}).get("choices") or []
        codes = {code for ch in chs for code in action_codes(ch)}
        if "decideOptionalEffect" not in codes:
            continue
        # pre-transform export at first sight of the prompt
        if not ST["pre_exported"]:
            await export_now("pre.json")
            ST["pre_exported"] = True
        ST["option_offered"] = True
        wire("optional_effect_offered",
             {"interaction_id": str(iid)[:12],
              "turn": st["state"].get("turn_number")})
        say(f"[{tag}] optional-effect prompt offered")
        pick = decide_optional_accept_choice(opp)
        if pick is None:
            say(f"[{tag}] no decideOptionalEffect accept=true choice found; "
                "wire-dumping")
            wire("optional_effect_no_accept",
                 {"interaction_id": str(iid)[:12],
                  "codes_seen": sorted(codes),
                  "texts": [choice_text(ch)[:80] for ch in chs]})
            SUBMITTED_OPPS.add(key)
            ST["unexpected"].append("optional_no_accept_choice")
            return True
        wire("optional_effect_opportunity", opp)
        await answer_vi(c, opp, pick, tag)
        SUBMITTED_OPPS.add(key)
        ST["option_accepted"] = True
        ST["stage"] = "TRANSFORM_WATCH"
        say(f"[{tag}] optional effect ACCEPTED (accept=true)")
        return True
    # ReplacementChoice fallback (cf. #7167/#9852): the engine folds some
    # optional "may" effects into a single ReplacementChoice prompt with
    # optionIndex value surfaces + chooseReplacement action code.
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        key = (tag, "replacement", str(iid))
        if key in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        chs = (resp.get("data") or {}).get("choices") or []
        codes = {code for ch in chs for code in action_codes(ch)}
        if "chooseReplacement" not in codes:
            continue
        idx_of, decline_idx = {}, None
        for ch in chs:
            txt = choice_text(ch).lower()
            for sf in ch.get("surfaces", []) or []:
                d = sf.get("data") or {}
                if d.get("role") == "optionIndex":
                    idx_of[str(d.get("value"))] = ch
            if "decline" in txt:
                decline_idx = ch
        pick = None
        for idx, ch in idx_of.items():
            if ch is not decline_idx and "decline" not in \
                    choice_text(ch).lower():
                pick = ch
                break
        if pick is None:
            continue
        if not ST["pre_exported"]:
            await export_now("pre.json")
            ST["pre_exported"] = True
        ST["option_offered"] = True
        wire("replacement_choice_accept", {"interaction_id": str(iid)[:12]})
        await answer_vi(c, opp, pick, tag)
        SUBMITTED_OPPS.add(key)
        ST["option_accepted"] = True
        ST["stage"] = "TRANSFORM_WATCH"
        say(f"[{tag}] ReplacementChoice ACCEPTED via optionIndex")
        return True
    # Hold only while OUR optional-effect decision is actually pending
    # (decideOptionalEffect / chooseReplacement codes). Other opportunities
    # (land plays, unrelated target selections) must not block the driver:
    # a broad "any decision" hold wedges P0's own main phases once its vi
    # starts offering playLand choices.
    holding = False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        chs = (resp.get("data") or {}).get("choices") or []
        codes = {code for ch in chs for code in action_type_codes(ch)}
        if "decideOptionalEffect" in codes or "chooseReplacement" in codes:
            holding = True
            break
    return holding


def check_transform(state):
    for oid in bf_oids(state, 0):
        if obj_lname(state, oid) == EVERFLAME.lower():
            return oid, get_obj(state, oid)
    return None, None


def attached_to_ints(o):
    found = []

    def walk(n):
        if isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
        elif isinstance(n, int) and n >= 0:
            found.append(n)
    walk(o.get("attached_to"))
    return found

# ---------------- equip offer scan + activation (A6/A7) --------------------
def equip_offers(st, oid):
    """ActivateAbility offers for the transformed object, from merged
    actions and viewer_interaction opportunities. Returns
    (offered, ability_index, descriptions, wire_detail)."""
    acts = merged_actions(st)
    descs = []
    idxs = []
    detail = {"merged": [], "vi": []}
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = str(d.get("source_id", a.get("_src_oid", "")))
        if src != str(oid):
            continue
        desc = str(d.get("description") or a.get("description") or "")
        descs.append(desc)
        idxs.append(d.get("ability_index"))
        detail["merged"].append({"ability_index": d.get("ability_index"),
                                 "description": desc[:120],
                                 "ability_tag": d.get("ability_tag")})
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "activateAbility" not in action_codes(ch):
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)
                    and s.get("data", {}).get("role") == "source"]
            if str(oid) in refs:
                txt = choice_text(ch)
                descs.append(txt)
                detail["vi"].append({"text": txt[:120],
                                     "choice_id": ch.get("id")})
    offered = any("equip" in d.lower() for d in descs)
    return offered, (idxs[0] if idxs else None), descs, detail


def cast_action_for(acts, state, name):
    for a in acts:
        if a.get("type") == "CastSpell":
            oid = a.get("data", {}).get("object_id")
            if oid is not None and obj_lname(state, oid) == name.lower():
                return a, int(oid)
        if a.get("type") == "CastSpell":
            oid = a.get("_src_oid")
            if oid is not None and obj_lname(state, oid) == name.lower():
                return a, int(oid)
    return None, None


async def p0_tick(c):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    if await do_mulligan(c, acts, st, 0, "P0",
                         [IRENCRAG.lower(), ISAMARU.lower()]):
        return True
    if await do_bottom(c, acts, st, 0, "P0"):
        return True
    if await do_discard_to_handsize(c, acts, st, 0, "P0"):
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

    # phantom-cast recovery: a CastSpell that never applied (engine dropped
    # it while our pass raced it) leaves the object in hand at a later
    # revision - clear the flag so the setup retries.
    rev = st.get("state_revision", -1)
    for key, flag in (("irencrag", "irencrag_cast"),
                      ("isamaru", "isamaru_cast")):
        if ST[flag]:
            oid = find_hand(state, 0, IRENCRAG if key == "irencrag" else ISAMARU)
            sr = ST.get(flag + "_rev")
            if oid is not None and sr is not None and rev > sr:
                still = bf_by_name(state, 0,
                                   IRENCRAG if key == "irencrag" else ISAMARU)
                if not still:
                    ST[flag] = False
                    ST[flag + "_rev"] = None
                    say(f"[P0] phantom-cast recovery: {key} still in hand "
                        f"at rev {rev} (submitted at {sr}); retrying")
                    wire("phantom_cast_recovery", {"spell": key, "rev": rev})

    # optional-effect prompt: answer or hold (never pass priority into it)
    if await handle_optional_effect(c, st, "P0"):
        return True

    # opportunistic trigger-stack sampling (A2)
    if ST["isamaru_cast"] and not ST["option_accepted"]:
        hits = stack_trigger_sample(state)
        if hits and not ST["trigger_on_stack_seen"]:
            ST["trigger_on_stack_seen"] = True
            ST["trigger_stack_record"] = hits[0]
            say(f"[P0] trigger on stack: {json.dumps(hits[0])[:200]}")
            wire("trigger_on_stack", hits[0])

    # ---- setup: land every turn, Irencrag at 2+ lands, Isamaru after ----
    if not ST["transformed"] and my_main(state, 0):
        if await play_a_land(c, state, 0, acts, "P0"):
            return True
        ul = untapped_land_names(state, 0)
        if not bf_by_name(state, 0, IRENCRAG) and not ST["irencrag_cast"]:
            ir = find_hand(state, 0, IRENCRAG)
            if ir and len(ul) >= 2:
                ca, _ = cast_action_for(acts, state, IRENCRAG)
                if ca and not acted("castirencrag", rev):
                    await submit_as_is(c, ca)
                    ST["irencrag_cast"] = True
                    ST["irencrag_cast_rev"] = rev
                    ST["cast_pending"] = (ir, rev, time.time())
                    say("[P0] casts The Irencrag (engine auto-pays)")
                    wire("cast_irencrag", {"oid": ir, "rev": rev})
                    return True
        if bf_by_name(state, 0, IRENCRAG) and not bf_by_name(state, 0, ISAMARU) \
                and not ST["isamaru_cast"]:
            isa = find_hand(state, 0, ISAMARU)
            if isa and "plains" in ul:
                ca, _ = cast_action_for(acts, state, ISAMARU)
                if ca and not acted("castisamaru", rev):
                    await submit_as_is(c, ca)
                    ST["isamaru_cast"] = True
                    ST["isamaru_cast_rev"] = rev
                    ST["isamaru_cast_turn"] = state.get("turn_number")
                    ST["cast_pending"] = (isa, rev, time.time())
                    say("[P0] casts Isamaru, Hound of Konda (engine auto-pays)")
                    wire("cast_isamaru", {"oid": isa, "rev": rev})
                    return True

    # transformation detection
    if ST["option_accepted"] and not ST["transformed"]:
        oid, o = check_transform(state)
        if oid:
            ST["transformed"] = True
            ST["transformed_oid"] = oid
            ST["resolve_turn"] = state.get("turn_number")
            ST["transform_record"] = {
                "oid": oid,
                "card_name": o.get("card_name") or o.get("name"),
                "card_types": o.get("card_types"),
                "zone": o.get("zone"),
                "controller": o.get("controller"),
                "tapped": o.get("tapped"),
            }
            say(f"[P0] TRANSFORMED: oid={oid} "
                f"{json.dumps(ST['transform_record'])[:300]}")
            wire("transformed", ST["transform_record"])
            ST["stage"] = "EQUIP_SCAN"
            await export_now("mid.json")
            ST["mid_exported"] = True

    # equip offer scanning: only in clean P0 main phases (equip is AsSorcery)
    if ST["transformed"] and my_main(state, 0) \
            and not ST["equip_offered_seen"] and not ST["equip_giveup"]:
        oid = ST["transformed_oid"]
        offered, idx, descs, detail = equip_offers(st, oid)
        if not acted(f"eqscan{oid}", rev):
            rec = {"rev": rev, "turn": state.get("turn_number"),
                   "n_activations_for_object":
                   len([a for a in acts
                        if a.get("type") == "ActivateAbility"
                        and str(a.get("data", {}).get("source_id",
                                                     a.get("_src_oid", "")))
                        == str(oid)]),
                   "ability_index": idx, "descriptions": descs,
                   "equip_offered": offered,
                   "all_action_types": sorted(atypes),
                   "detail": detail}
            ST["offer_scans"].append(rec)
            wire("equip_offer_scan", rec)
        if offered:
            ST["equip_offered_seen"] = True
            ST["equip_ability_index"] = idx
            ST["equip_descriptions"] = descs
            ST["stage"] = "EQUIP_ACTIVATE"
            say(f"[P0] EQUIP OFFERED: index={idx} descs={descs}")
            wire("equip_offered", {"ability_index": idx,
                                   "descriptions": descs, "detail": detail})
        else:
            turns = {s["turn"] for s in ST["offer_scans"]}
            if len(turns) >= 6:
                ST["equip_giveup"] = True
                ST["stage"] = "CLEANUP"
                say("[P0] equip giveup: no equip offer across "
                    f"{len(turns)} clean main-phase turns")
                wire("equip_giveup", {"turns": len(turns)})

    # activate equip (A7): submit the advertised ActivateAbility EXACTLY
    # (never inject targets; targets come via the vi TargetSelection after)
    if ST["stage"] == "EQUIP_ACTIVATE" and not ST["equip_activated"]:
        if my_main(state, 0):
            oid = ST["transformed_oid"]
            for a in acts:
                if a.get("type") != "ActivateAbility":
                    continue
                d = a.get("data", {}) or {}
                src = str(d.get("source_id", a.get("_src_oid", "")))
                if src != str(oid):
                    continue
                desc = str(d.get("description") or "")
                if "equip" not in desc.lower():
                    continue
                if not acted("equpact", rev):
                    await submit_as_is(c, a)
                    ST["equip_activated"] = True
                    ST["equip_activated_at"] = time.time()
                    ST["equip_activated_rev"] = rev
                    say("[P0] equip activated (advertised action, as-is)")
                    wire("equip_activated", {"action": a})
                    return True

    # answer the equip target selection (Isamaru)
    if ST["equip_activated"] and not ST["equip_resolved"]:
        for opp in vi_ops(st):
            iid = opp.get("interactionId")
            key = ("P0", "equiptarget", str(iid))
            if key in SUBMITTED_OPPS:
                continue
            resp = opp.get("response", {}) or {}
            chs = (resp.get("data") or {}).get("choices") or \
                (resp.get("data") or {}).get("candidates") or []
            if not chs:
                continue
            isamarus = set(str(o) for o in bf_by_name(state, 0, ISAMARU))
            pick = None
            for ch in chs:
                r = _cand_reference(ch)
                if r is not None and str(r) in isamarus:
                    pick = ch
                    break
            if pick is None:
                continue
            r = _cand_reference(pick)
            ST["equip_target_oid"] = str(r) if r is not None else None
            say(f"[P0] equip target: Isamaru oid={r}")
            wire("equip_target_pick", {"ref": str(r)})
            await answer_vi(c, opp, pick, "P0")
            SUBMITTED_OPPS.add(key)
            return True

    # equip resolution watch: attached_to points at Isamaru and Isamaru 5/5
    if ST["equip_activated"] and not ST["equip_resolved"]:
        oid = ST["transformed_oid"]
        o = get_obj(state, oid)
        att = attached_to_ints(o)
        isamarus = set(str(x) for x in bf_by_name(state, 0, ISAMARU))
        hit = [a for a in att if str(a) in isamarus]
        if hit:
            io = get_obj(state, hit[0])
            pt = (io.get("power"), io.get("toughness"))
            ST["equip_resolved"] = True
            ST["equip_resolved_pt"] = pt
            ST["stage"] = "CLEANUP"
            say(f"[P0] equip RESOLVED: attached_to={att}, Isamaru P/T={pt}")
            wire("equip_resolved", {"attached_to": att, "pt": pt})
        rej = [r for r in ST["rejections"]
               if r.get("t", 0) > (ST.get("equip_activated_at") or 0)]
        if rej and not hit:
            ST["unexpected"].append("equip_activation_rejected")
            say("[P0] equip activation rejected: "
                f"{json.dumps(rej[-1])[:300]}")
            wire("equip_activation_rejected", rej[-1])

    # trigger give-up: Isamaru cast long ago, no option prompt
    if ST["isamaru_cast"] and not ST["option_offered"]:
        t0c = ST.get("isamaru_cast_turn")
        if t0c is not None and state.get("turn_number", 0) >= t0c + 10:
            ST["done_reason"] = ("Isamaru cast at turn %s but no optional "
                                 "effect prompt by turn %s" %
                                 (t0c, state.get("turn_number")))
            say("GIVEUP: " + ST["done_reason"])
            wire("trigger_giveup", {})
            ST["stop"] = True

    # End the post-resolution observation window only once the equip
    # question is settled (offered, or 6 clean main turns with no offer).
    # Ending at resolve_turn+2 unconditionally cuts the scan short when the
    # transform resolves late (e.g. after a phantom-cast retry), leaving
    # A6 as not-run instead of failed/passed.
    equip_settled = ST["equip_offered_seen"] or ST["equip_giveup"] \
        or ST["equip_resolved"]
    if equip_settled and ST["stage"] in ("CLEANUP", "EQUIP_SCAN") \
            and ST.get("resolve_turn") is not None:
        stack = state.get("stack") or []
        if not stack and state.get("turn_number", 0) >= \
                ST["resolve_turn"] + 2:
            ST["done_reason"] = "post-resolution observation window complete"
            say("DONE: " + ST["done_reason"])
            ST["stop"] = True
    return False


async def p1_tick(c):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, "P1", [FOREST.lower()]):
        return True
    if await do_bottom(c, acts, st, 1, "P1"):
        return True
    if await do_discard_to_handsize(c, acts, st, 1, "P1"):
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
    return False

# ---------------- tick + main loop ----------------------------------------
def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage"),
                                     "t": time.time()})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


async def tick(c, pid):
    st = c.latest
    if not st or ST["stop"]:
        return False
    state = st.get("state")
    acts = merged_actions(st)
    if state is None:
        return False
    drain_rejections(c)
    kind = vi_kind_code(st)
    if kind and (not WF_SEEN or WF_SEEN[-1] != kind):
        WF_SEEN.append(kind)
        wire("waiting_for_kind", {"kind": kind, "stage": ST["stage"]})
        say(f"[{c.name}] vi waitingForKind: {kind}")
    for q in (0, 1):
        lv = life(state, q)
        if lv is not None and lv < ST["life"][q]:
            say(f"[{c.name}] P{q} life {ST['life'][q]} -> {lv} "
                f"(turn {state.get('turn_number')} phase {state.get('phase')})")
            wire("life_drop", {"player": q, "from": ST["life"][q], "to": lv,
                               "turn": state.get("turn_number"),
                               "phase": state.get("phase")})
            ST["life"][q] = lv
        elif lv is not None and lv > ST["life"][q]:
            say(f"[{c.name}] P{q} life {ST['life'][q]} -> {lv} (gain)")
            wire("life_gain", {"player": q, "from": ST["life"][q], "to": lv})
            ST["life"][q] = lv
    try:
        if pid == 0:
            did = await p0_tick(c)
        else:
            did = await p1_tick(c)
    except Exception as e:
        say(f"tick error [{c.name}]: {e}")
        wire("tick_error", {"who": c.name, "error": str(e)[:300]})
        return False
    if did:
        return True
    # priority gate, then yield before the priority-pass decision (race fix)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    # NOTE: no broad "decision pending" gate here. P0's optional-effect
    # prompt is already held by handle_optional_effect (it returns
    # holding=True instead of passing), and a broad gate misfires on
    # P1's passive land-play opportunities (playLand action codes read as
    # "decisions"), wedging the game at P1's first main phase with nobody
    # passing. (diagnosed 2026-10-14 via STALLDUMP on game Z9528Q.)
    # never pass priority while a cast we submitted is still in flight
    # (the engine must process it first, else our pass races the cast)
    cp = ST.get("cast_pending")
    if cp and pid == 0:
        coid, crev, ct0 = cp
        cur_rev = st.get("state_revision", -1)
        o = get_obj(state, coid)
        if cur_rev <= crev and o.get("zone") == "Hand" \
                and time.time() - ct0 < 30:
            return False
        ST["cast_pending"] = None
    if my_priority(acts):
        await pass_priority(c, st, acts)
        return True
    return False


# ---------------- finalize -------------------------------------------------
async def finalize():
    if ST.get("finalized"):
        return None, {}
    ST["finalized"] = True
    ST["stop"] = True
    ST["done_reason"] = ST.get("done_reason") or "flow complete"
    say("finalizing...")

    try:
        shutil.copy(__file__, f"{EVDIR}/scenario_7159_01100.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")
    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("server.log copied to evidence")
    except Exception as e:
        say(f"server.log copy failed: {e}")
        wire("server_log_copy_failed", {"error": str(e)[:200]})

    if "post.json" not in ST["exports"]:
        await export_now("post.json")

    post = load_state("post.json")
    mid = load_state("mid.json")
    pre = load_state("pre.json")
    ass, notes = {}, []

    def note(m):
        notes.append(m)
        say("NOTE:", m)

    # A1
    ass["A1_parse_ok"] = ST.get("a1", "not-run")
    rec = ST.get("parse_rec") or {}
    note(f"A1: become-Equipment mods {rec.get('main_effect_modifications')}; "
         f"sub mods {rec.get('sub_ability_modifications')}; equip grants "
         f"{rec.get('equip_grant_descriptions')} (duplicated grant: "
         f"{rec.get('duplicated_equip_grant')}) -> {ass['A1_parse_ok']}")

    # A2
    ok2 = False
    if ST["trigger_stack_record"]:
        tr = ST["trigger_stack_record"]
        note(f"A2: trigger on stack seen: source_id={tr.get('source_id')} "
             f"desc={tr.get('description')[:120]}")
        ok2 = True
    elif pre:
        hits = stack_trigger_sample(pre)
        if hits:
            ST["trigger_stack_record"] = hits[0]
            ok2 = True
            note(f"A2: trigger on stack found in pre.json: "
                 f"{json.dumps(hits[0])[:160]}")
        else:
            note("A2: trigger not on stack in live sampling or pre.json "
                 "stack (stack empties fast once the option is pending)")
    if not ok2 and ST["option_offered"]:
        ok2 = True
        note("A2: trigger fire established via the optional-effect prompt "
             "itself (the prompt is the trigger's optional decision); the "
             "stack entry resolved too fast for sampling")
    ass["A2_setup_ok"] = "passed" if ok2 else "failed"

    # A3
    if ST["option_accepted"]:
        ass["A3_option_offered"] = "passed"
    elif ST["option_offered"]:
        ass["A3_option_offered"] = "failed"
    else:
        ass["A3_option_offered"] = "not-run"
    note(f"A3: option offered={ST['option_offered']} "
         f"accepted(accept=true)={ST['option_accepted']}")

    # A4
    ok4 = False
    trec = ST.get("transform_record") or {}
    if trec:
        ct = trec.get("card_types") or {}
        subs_l = [str(s).lower() for s in (ct.get("subtypes") or [])]
        sups_l = [str(s).lower() for s in (ct.get("supertypes") or [])]
        typs_l = [str(t).lower() for t in (ct.get("core_types") or [])]
        name_ok = (trec.get("card_name") or "") == EVERFLAME
        eq_ok = "equipment" in subs_l
        leg_ok = "legendary" in sups_l
        art_ok = "artifact" in typs_l
        ok4 = name_ok and eq_ok and leg_ok and art_ok
        note(f"A4: name={trec.get('card_name')!r} card_types={ct} -> "
             f"name_ok={name_ok} legendary={leg_ok} artifact={art_ok} "
             f"equipment={eq_ok}")
    else:
        note("A4: no transformation recorded")
    ass["A4_transform_ok"] = "passed" if ok4 else "failed"

    # A5: original {T}: Add {C} must be gone from the object's current
    # `abilities` list (base_abilities retains the printed text as the
    # pre-transform snapshot; the effective list is what matters).
    if mid and ST.get("transformed_oid"):
        mo = get_obj(mid, ST["transformed_oid"])
        cur = mo.get("abilities") or []
        mana_in_cur = [a for a in cur
                       if a.get("is_mana_ability")
                       or "add {c}" in str(a.get("description") or "")
                       .lower()]
        equip_in_cur = [a for a in cur
                        if "equip" in str(a.get("description") or "")
                        .lower()]
        ok5 = not mana_in_cur
        ass["A5_mana_lost"] = "passed" if ok5 else "failed"
        note(f"A5: current abilities={len(cur)} mana_remaining="
             f"{len(mana_in_cur)} equip_granted={len(equip_in_cur)} "
             f"has_mana_ability_flag={mo.get('has_mana_ability')} "
             f"(base_abilities still lists the printed mana ability: "
             f"{len(mo.get('base_abilities') or [])})")
    else:
        ass["A5_mana_lost"] = "not-run"
        note("A5: mid.json/transformed object missing")

    # A6
    if ST["equip_offered_seen"]:
        ass["A6_equip_offered"] = "passed"
    else:
        scans = [s for s in ST["offer_scans"]]
        turns = len({s["turn"] for s in scans})
        if turns >= 6:
            ass["A6_equip_offered"] = "failed"
        else:
            ass["A6_equip_offered"] = "not-run"
        note(f"A6: equip offered={ST['equip_offered_seen']} "
             f"scans={len(scans)} clean_main_turns={turns}")
    note(f"A6: ability_index={ST.get('equip_ability_index')} "
         f"descs={ST.get('equip_descriptions')}")

    # A7
    if ST["equip_resolved"]:
        pt = ST.get("equip_resolved_pt")
        ok7 = pt is not None and pt[0] == 5 and pt[1] == 5
        ass["A7_equip_resolves"] = "passed" if ok7 else "failed"
        note(f"A7: attached to Isamaru; P/T={pt} (expected (5, 5))")
    elif ST["equip_activated"]:
        ass["A7_equip_resolves"] = "failed"
        note("A7: equip activated but never resolved")
    else:
        ass["A7_equip_resolves"] = "not-run"
        note("A7: equip never offered -> not-run")

    # A8
    ok8 = False
    if post:
        stack = post.get("stack") or []
        turn = post.get("turn_number")
        rt = ST.get("resolve_turn")
        ok8 = len(stack) == 0 and rt is not None and turn >= rt + 2
        note(f"A8: post stack depth={len(stack)} turn={turn} "
             f"resolve_turn={rt}")
    else:
        note("A8: post.json missing")
    ass["A8_cleanup"] = "passed" if ok8 else "failed"

    # verdict
    a = ass
    if a["A2_setup_ok"] == "passed" and a["A3_option_offered"] == "passed" \
            and a["A4_transform_ok"] == "passed" \
            and a["A6_equip_offered"] == "failed":
        verdict = "reproduced"
    elif all(a[k] == "passed" for k in
             ("A2_setup_ok", "A3_option_offered", "A4_transform_ok",
              "A5_mana_lost", "A6_equip_offered", "A7_equip_resolves",
              "A8_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    run = {
        "issue": ISSUE,
        "title": "The Irencrag - equip {3} missing after the "
                 "become-Equipment transformation.",
        "run_id": RUN_ID,
        "validated_at": "2026-10-14",
        "validated_version": "v0.110.0",
        "server": SERVER_IDENTITY,
        "server_hello_observed": ST.get("server_hello"),
        "protocol_version": 142,
        "scope": "The Irencrag {2} (pinned card-data AST: ChangesZone "
                 "trigger -> optional become legendary Equipment named "
                 "Everflame, Heroes' Legacy; sub_ability: RemoveAllAbilities "
                 "+ GrantStaticAbility(+3/+3, EquippedBy) + GrantAbility "
                 "equip {3} x2 (duplicated grant)). Isamaru, Hound of Konda "
                 "as the legendary ETB trigger; native engine, two "
                 "human-client seats",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "result": "; ".join(f"{k}: {v}" for k, v in ass.items()),
        "trigger_stack_record": ST.get("trigger_stack_record"),
        "transform_record": ST.get("transform_record"),
        "offer_scans": ST.get("offer_scans"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
            "Mana ability absence judged from the transformed object's "
            "effective ability list in mid.json, not from offer counts.",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
        "done_reason": ST.get("done_reason"),
        "prior_run": {
            "run_id": "20260914-7159",
            "validated_version": "v0.82.0",
            "verdict": "reproduced",
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # render summary.png via the standalone renderer (reads run.json +
    # saved states; must run before manifest)
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable,
             f"{BACKFILL}/driver/render_summary_7159.py", EVDIR],
            capture_output=True, text=True, timeout=120)
        say(r.stdout.strip())
        if r.returncode != 0:
            say("renderer failed: " + r.stderr[-500:])
            wire("renderer_failed", {"stderr": r.stderr[-500:]})
    except Exception as e:
        say(f"renderer error: {e}")
        wire("renderer_error", {"error": str(e)[:200]})

    # manifest LAST (after all logging is done) - no say()/wire() calls
    # may happen after this point or the hashed logs will drift.
    say(f"writing manifest; done: {ST.get('done_reason')}")
    RUNLOG.flush()
    WIRE.flush()
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            h = hashlib.sha256(
                open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            f.write(f"{h}  {fn}\n")
    RUNLOG.flush()
    WIRE.flush()
    return verdict, ass


# ---------------- main -----------------------------------------------------
async def main():
    reset()
    global C0, C1
    await verify_server_hello()
    parse_check()

    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=2)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "run_id": RUN_ID})

    t0 = time.time()
    last_rev_seen = {0: -1, 1: -1}
    last_rev_time = time.time()
    stall_dumped = False
    try:
        while time.time() - t0 < TIMEOUT and not ST["stop"]:
            await tick(C0, 0)
            await tick(C1, 1)
            # stall watchdog: no revision progress on either client for 90s
            cur0 = (C0.latest or {}).get("state_revision", -1)
            cur1 = (C1.latest or {}).get("state_revision", -1)
            if cur0 != last_rev_seen[0] or cur1 != last_rev_seen[1]:
                last_rev_seen = {0: cur0, 1: cur1}
                last_rev_time = time.time()
                stall_dumped = False
            elif time.time() - last_rev_time > 90 and not stall_dumped:
                stall_dumped = True
                say("STALL WATCHDOG: no revision progress for 90s; "
                    "dumping client views")
                wire("stall_watchdog", {"rev0": cur0, "rev1": cur1})
                for cc, tag in ((C0, "P0"), (C1, "P1")):
                    sst = cc.latest or {}
                    sstate = (sst.get("state") or {})
                    aats = sorted(set(a.get("type")
                                       for a in merged_actions(sst)))
                    vvi = sst.get("viewer_interaction") or {}
                    opps = []
                    for o in (vvi.get("opportunities") or [])[:4]:
                        rr = o.get("response", {}) or {}
                        cchs = (rr.get("data") or {}).get("choices") or \
                            (rr.get("data") or {}).get("candidates") or []
                        opps.append({
                            "iid": str(o.get("interactionId"))[:16],
                            "rtype": rr.get("type"),
                            "choices": [
                                {"id": str(ch.get("id"))[:16],
                                 "action_codes": action_type_codes(ch),
                                 "text": choice_text(ch)[:60]}
                                for ch in cchs]})
                    dump = {
                        "rev": sst.get("state_revision"),
                        "turn": sstate.get("turn_number"),
                        "phase": sstate.get("phase"),
                        "active": sstate.get("active_player"),
                        "stack_n": len(sstate.get("stack") or []),
                        "atypes": aats,
                        "vi_kind": (vvi.get("waitingForKind") or {})
                        .get("code"),
                        "vi_canSubmit": vvi.get("canSubmit"),
                        "vi_opps": opps,
                    }
                    say(f"[{tag}] STALLDUMP " + json.dumps(dump)[:1500])
                    wire("stall_dump", {"who": tag, "dump": dump})
            await asyncio.sleep(0.25)
    except Exception as e:
        say(f"main loop error: {e}")
        wire("main_error", {"error": str(e)[:300]})
    finally:
        if not ST.get("finalized"):
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"pre-close post export failed: {e}")
                wire("post_export_failed", {"error": str(e)[:200]})
        try:
            await C0.close()
        except Exception:
            pass
        try:
            await C1.close()
        except Exception:
            pass
    return await finalize()


if __name__ == "__main__":
    verdict, ass = asyncio.run(main())
    print(f"FINAL verdict={verdict} "
          + " ".join(f"{k}={v}" for k, v in ass.items()))
