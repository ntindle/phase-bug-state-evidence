#!/usr/bin/env python3
"""Issue #7343: Fatal Grudge was not castable from hand even with basic
lands of its colors readily available.

Protocol-153 port (v0.114.0, 2026-10-20) of scenario_7343.py (verified
2026-10-01, run 20261001-7343, v0.99.0/protocol 98, REPRODUCED: across 4
main-phase observations the engine's cast gate advertised ONLY
PassPriority, no CastSpell for Fatal Grudge).

Mechanical identity port + the 2026-10-09 cast-confirmation lesson: on
protocol 118/120/126/142/147/151/153 a bare CastSpell (payment_mode Auto)
can lose a race to the driver's own PassPriority, so after every CastSpell
the driver holds (no priority pass, no new main-phase plays) until the
spell is confirmed on the stack; a 30s backstop clears a still-in-hand
cast as dropped and resets the one-shot flags so the cast block retries.

Oracle (pinned v0.114.0 card-data.json):
  Fatal Grudge {B}{R} sorcery.
  "As an additional cost to cast this spell, sacrifice a nonland
  permanent. Each opponent chooses a permanent they control that shares
  a card type with the sacrificed permanent and sacrifices it.
  Draw a card."
  Pinned parse (see data_evidence.json): additional_cost Required /
  Sacrifice nonland permanent count 1; spell ability TargetOnly Typed
  Permanent + SharesQuality CardType against CostPaidObject, per-opponent
  scope; sub_ability Draw 1 SequentialSibling. fully_parsed.

Behavioral contract (per PLAYBOOK.md step 2 -- written before observing
results; identical to the verified protocol-98 contract):
  A1 setup_ok    pre.json: P0 PreCombatMain with priority, Fatal Grudge in
                 hand, untapped Memnite on P0 BF, untapped Swamp +
                 untapped Mountain for P0, Memnite on P1 BF.
  A2 cast_offered engine advertises CastSpell for the Grudge (the gate
                 offers it despite the Required additional cost).
  A3 sac_paid    additional cost paid: a P0 Memnite in graveyard;
                 Swamp + Mountain tapped for {B}{R}.
  A4 opp_sac     P1's Memnite in graveyard (chose + sacrificed).
  A5 draw        P0 library count pre -> post decreased by exactly 1.
  A6 cleanup     stack empty, game proceeding, no stuck decisions or
                 rejections; when the cast resolved, the Grudge is in
                 P0's graveyard. (When the bug reproduces at the gate
                 (A2 failed), the Grudge was never cast, so cleanup is
                 judged on stack/game state alone.)

Verdict: reproduced iff A1 passes and A2 fails (cast never offered - the
reported "not castable"), or the additional-cost / opponent-choice flow
stalls or silently no-ops (A3/A4 fail after A2 passed).
not-reproduced iff A1-A6 all pass. blocked iff A1 fails.
NEVER "fixed".
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402
import websockets  # noqa: E402

ISSUE = 7343
BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-7343-reval-v01140-20261020-1111"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

GRUDGE = "fatal grudge"
MEMNITE = "memnite"
SWAMP = "swamp"
MOUNTAIN = "mountain"
P0_DECK = [("Fatal Grudge", 4), ("Memnite", 8), ("Swamp", 24), ("Mountain", 24)]
P1_DECK = [("Memnite", 12), ("Mountain", 48)]
LANDS = (SWAMP, MOUNTAIN)
TIMEOUT = 1500

ST = {
    "stage": "SETUP",
    "pre_exported": False, "mid_exported": False, "post_exported": False,
    "rejections": [], "stop": False,
    "grudge_oid": None, "memnite_oid": None, "p1_memnite_oid": None,
    "cast_offered": False, "cast_submitted": False,
    "sac_paid": False, "opp_choice_done": False,
    "library_pre": None, "library_post": None,
    "cast_pending": None,
    "mana_needs": {"P0": {}, "P1": {}},
    "no_offer_observations": 0,
    "cast_at": None,
    "last_diag": 0,
    "states_seen": 0,
}
MULLS = set()
SUBMITTED_OPPS = set()
PASSED_REV = {}
MULL_COUNT = {}
WF_SEEN = []


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ------------------------------------------------------------------ helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def hand_ids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def untapped_of(state, pid, lname):
    return sum(1 for o in bf_by_name(state, pid, lname)
               if not get_obj(state, o).get("tapped"))


def gy_ids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid]


def gy_names(state, pid):
    return [obj_lname(state, o) for o in gy_ids(state, pid)]


def lib_count(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return len(p.get("library", []) or [])
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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def my_priority(acts):
    """Protocol 118+: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it."""
    return any(a.get("type") == "PassPriority" for a in acts)


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})
        say(f"waiting_for: {wf} stage={ST.get('stage')}")


# ------------------------------------------------------- interaction prims

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


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


async def verify_server_hello():
    ws = await websockets.connect("ws://localhost:9374/ws", max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = json.loads(raw).get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.114.0"), f"unexpected version {ver}"
    assert int(proto) == 153, f"unexpected protocol {proto}"
    assert str(build) == "ac67cbb", f"unexpected build {build}"
    return ver, build, proto


def check_data_level():
    """Pinned v0.114.0 parse: Fatal Grudge {B}{R} sorcery; additional_cost
    Required / Sacrifice nonland permanent count 1; spell ability
    TargetOnly Typed Permanent sharing CardType with CostPaidObject
    (per-opponent scope) -> Draw 1 SequentialSibling."""
    ok, notes = True, []
    rec = json.load(open(f"{BACKFILL}/server/releases/v0.114.0/data/card-data.json"))["fatal grudge"]
    payload = {}
    try:
        assert rec.get("name") == "Fatal Grudge"
        mc = rec.get("mana_cost") or {}
        assert sorted(mc.get("shards") or []) == ["Black", "Red"], mc
        assert (rec.get("card_type") or {}).get("core_types") == ["Sorcery"]
        assert "sacrifice a nonland permanent" in (rec.get("oracle_text") or "")
        ac = rec.get("additional_cost") or {}
        assert ac.get("type") == "Required", ac.get("type")
        sac = ac.get("data") or {}
        assert sac.get("type") == "Sacrifice"
        tf = (sac.get("target") or {}).get("type_filters") or []
        assert "Permanent" in tf and {"Non": "Land"} in tf, tf
        assert sac.get("count") == 1
        ab = (rec.get("abilities") or [{}])[0]
        assert ab.get("kind") == "Spell"
        eff = ab.get("effect") or {}
        assert eff.get("type") == "TargetOnly", eff.get("type")
        tgt = eff.get("target") or {}
        assert tgt.get("type") == "Typed"
        assert "Permanent" in (tgt.get("type_filters") or [])
        props = tgt.get("properties") or []
        assert any(p.get("type") == "SharesQuality"
                   and p.get("quality") == "CardType"
                   and (p.get("reference") or {}).get("type") == "CostPaidObject"
                   for p in props), props
        assert (ab.get("player_scope") or {}).get("type") == "Opponent"
        sub = ab.get("sub_ability") or {}
        seff = sub.get("effect") or {}
        assert seff.get("type") == "Draw"
        assert ((seff.get("count") or {}).get("value")) == 1
        assert (seff.get("target") or {}).get("type") == "Controller"
        assert sub.get("sub_link") == "SequentialSibling"
        payload = {
            "mana_cost": f"{mc.get('shards')}+{mc.get('generic')} generic",
            "additional_cost": "Required Sacrifice nonland permanent x1",
            "opponent_choice": "TargetOnly Typed Permanent shares CardType "
                               "with CostPaidObject (per-opponent)",
            "sub_ability": "Draw 1 Controller, SequentialSibling",
            "oracle_text": rec.get("oracle_text"),
        }
    except AssertionError as e:
        ok = False
        notes.append(f"data-level check failed: {e}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes, "parsed": payload,
                   "scryfall_oracle_id": rec.get("scryfall_oracle_id")}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok
# --------------------------------------------------- decision ticks

def mulligan_pending_for(state, pid):
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "Declare":
            return True
    return False


async def do_mulligan(c, acts, st, pid, tag):
    """MulliganDecision via legacy Action (Keep); gated on the advertised
    MulliganDecision action + Declare phase pending for our seat."""
    state = st["state"]
    if not mulligan_pending_for(state, pid):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return True
    adv = next((a for a in acts if a.get("type") == "MulliganDecision"), None)
    if adv is None:
        return False
    MULLS.add(key)
    n_lands = sum(1 for o in hand_ids(state, pid)
                  if obj_lname(state, o) in LANDS)
    n_mulls = MULL_COUNT.get(tag, 0)
    keep_ok = n_lands >= 2 or n_mulls >= 2
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        MULL_COUNT[tag] = n_mulls + 1
    wire("mulligan", {"who": tag, "choice": choice, "lands": n_lands})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    say(f"[{tag}] mulligan -> {choice} (lands={n_lands})")
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Bottom-after-mulligan via vi schema/select, gated on
    waitingForKind.code == 'mulligan' + turn 1 / Untap. Bottoms
    non-essential cards first (keeps lands, Grudge, Memnite)."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; not answering")
        wire(f"{tag}_bottom_no_vi", {})
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
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in (GRUDGE, MEMNITE) or (oid is not None and is_land(get_obj(state, oid))):
            return (1, str(oid))
        return (0, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}},
                      tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """DiscardToHandSize via vi: hand > 7 + a schema/select opportunity
    offering hand cards. Keeps Grudge + lands; discards Memnites first."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        codes = set()
        for ch in cands:
            codes.update(x for x in surf_codes(ch) if x)
        blob = (json.dumps([choice_text(ch) for ch in cands])
                + str(opp.get("description", ""))).lower()
        if not any("discard" in (x or "").lower() for x in codes) \
                and "discard" not in blob:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "sequence"

        def rank(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else ""
            if nm == MEMNITE:
                return (0, nm)
            if nm in (GRUDGE, SWAMP, MOUNTAIN):
                return (2, nm)
            return (1, nm)

        picks = [ch["id"] for ch in sorted(cands, key=rank)[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding {len(picks)} to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
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
                await interact_as(c, {"interactionId": opp.get("interactionId"),
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def answer_declare_attackers(c, st, state, tag):
    """Declare no attackers (advertised action, vi opportunity, or blind
    fallback). Returns True if something was submitted."""
    acts = merged_actions(st)
    da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
    if da is not None:
        d = copy.deepcopy(da.get("data", {}))
        d["attacks"] = []
        d["bands"] = []
        await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        say(f"[{tag}] DeclareAttackers -> none (advertised)")
        return True
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        blob = (str(opp.get("description", "")) + " " +
                " ".join(choice_text(ch) for ch in items)).lower()
        if "attack" not in blob:
            continue
        if rtype == "schema":
            spec = data.get("spec") or {}
            stype = spec.get("type") or "select"
            await interact_as(c, {"interactionId": opp.get("interactionId"),
                                  "response": {"type": stype,
                                               "data": {"choiceIds": []}}}, tag)
            say(f"[{tag}] DeclareAttackers -> none (vi {stype})")
            return True
        best = None
        for ch in items:
            if any(k in choice_text(ch).lower()
                   for k in ("no attack", "none", "done", "declare no", "skip")):
                best = ch.get("choiceId") or ch.get("id")
                break
        if best is None and items:
            best = items[0].get("choiceId") or items[0].get("id")
        if best:
            await interact_as(c, {"interactionId": opp.get("interactionId"),
                                  "response": {"type": "choose",
                                               "data": {"choiceId": best}}}, tag)
            say(f"[{tag}] DeclareAttackers -> none (vi choose)")
            return True
    key = ("da", state.get("turn_number"), int(time.time() // 30))
    if key not in SUBMITTED_OPPS:
        SUBMITTED_OPPS.add(key)
        await c.send_action({"type": "DeclareAttackers",
                             "data": {"attacks": [], "bands": []}})
        say(f"[{tag}] DeclareAttackers -> none (blind)")
        return True
    return False


# --------------------------------------------- cast gate + cost handlers

def castspell_advertised(acts, oid):
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            return a
    return None


def vi_cast_choice(st, oid):
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in surf_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None


def vi_choice_for_oid(st, oid):
    """Find a viewer_interaction opportunity whose candidates reference oid.
    Skips castSpell/priority menus. Returns (opp, choice) or None."""
    for opp in vi_ops(st):
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        for ch in chs:
            if not isinstance(ch, dict):
                continue
            codes = surf_codes(ch)
            if "castSpell" in codes or "passPriority" in codes:
                continue
            if str(oid) in json.dumps(ch, default=str):
                return opp, ch
    return None


async def handle_castoffer(c, pid, state, st):
    """Answer the engine's CastOffer confirmation (7313 pattern)."""
    if wf_type(state) != "CastOffer":
        return False
    if str(((state.get("waiting_for") or {}).get("data") or {}).get("player")) != str(pid):
        return False
    data = (state.get("waiting_for") or {}).get("data", {}) or {}
    kind = data.get("kind") or {}
    oid = kind.get("object_id")
    vcc = vi_cast_choice(st, oid)
    if vcc:
        opp, ch = vcc
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": "choose",
                                           "data": {"choiceId": ch["id"]}}}, c.name)
        say(f"[{c.name}] answered CastOffer for oid={oid}")
        return True
    for opp in vi_ops(st):
        data2 = (opp.get("response") or {}).get("data", {}) or {}
        chs = data2.get("choices") or data2.get("candidates") or []
        if len(chs) == 1 and isinstance(chs[0], dict) and chs[0].get("id"):
            await interact_as(c, {"interactionId": opp.get("interactionId"),
                                  "response": {"type": "choose",
                                               "data": {"choiceId": chs[0]["id"]}}},
                              c.name)
            say(f"[{c.name}] answered CastOffer (single choice) for oid={oid}")
            return True
    return False


async def _answer_choice_with_oid(c, st, oid, who, flag_key, log):
    """Generic vi/legacy-action answer for a prompt referencing one oid."""
    if ST.get(flag_key):
        return False
    st_ = st
    for a in merged_actions(st_):
        blob = json.dumps(a, default=str)
        atype = (a.get("type") or "").lower()
        if ("choose" in atype or "sacrifice" in atype) and str(oid) in blob:
            wire(log + "_action", {"action": a})
            await submit_as_is(c, a)
            ST[flag_key] = True
            say(f"[{who}] {log} via advertised {a.get('type')} (oid {oid})")
            return True
    vcc = vi_choice_for_oid(st_, oid)
    if not vcc:
        return False
    opp, ch = vcc
    iid = opp.get("interactionId") or opp.get("id")
    cid = ch.get("id") or ch.get("choiceId")
    spec = ((opp.get("response") or {}).get("data") or {}).get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else None
    if not iid or cid is None:
        wire(log + "_vi_unusable", {"iid": iid, "cid": cid, "stype": stype})
        return False
    if stype == "sequence":
        sub = {"interactionId": iid,
               "response": {"type": "sequence", "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    wire(log + "_submit", {"iid": iid, "cid": cid, "stype": stype})
    await interact_as(c, sub, c.name)
    ST[flag_key] = True
    say(f"[{who}] {log} oid={oid} (stype={stype})")
    return True


async def answer_sacrifice_prompt(c, st, state):
    """Answer P0's additional-cost sacrifice prompt with the Memnite."""
    moid = ST.get("memnite_oid")
    if not moid:
        return False
    return await _answer_choice_with_oid(c, st, moid, "P0", "sac_paid",
                                         "sacrifice")


async def answer_opp_choice(c, pid, st, state):
    """Answer P1's 'choose a permanent sharing a card type' with P1's Memnite."""
    moid = ST.get("p1_memnite_oid")
    if not moid:
        moid = next(iter(bf_by_name(state, pid, MEMNITE)), None)
        if moid:
            ST["p1_memnite_oid"] = moid
        else:
            return False
    return await _answer_choice_with_oid(c, st, moid, "P1", "opp_choice_done",
                                         "oppchoice")


# ------------------------------------------------------------------ mana

async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            if "tapLandForMana" not in surf_codes(ch):
                continue
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
                for color in ("B", "R"):
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
        say(f"[{tag}] tap land for mana used_for={used} needs_left={needs}")
        wire("tap_land", {"who": tag, "used_for": used,
                         "needs_left": dict(needs or {})})
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        cid = pick.get("id")
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": opp.get("interactionId"),
                   "response": {"type": stype, "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": opp.get("interactionId"),
                   "response": {"type": "choose", "data": {"choiceId": cid}}}
        await interact_as(c, sub, tag)
        return True
    return False


# ------------------------------------------------------- cast guard (2026-10-09 lesson)

def cast_zone(state, oid):
    return str(get_obj(state, oid).get("zone") or "").lower()


async def guard_cast_pending(c, tag):
    """While a CastSpell is in flight the driver must not pass priority or
    start new main-phase plays (a bare CastSpell with payment_mode Auto can
    lose a race to the driver's own PassPriority). Returns True while the
    tick must hold. A 30s backstop clears a still-in-hand cast as dropped
    and resets the one-shot flags so the cast block retries. NEVER trust a
    cast simply because it was submitted -- confirm stack placement."""
    pend = ST.get("cast_pending")
    if not pend:
        return False
    st = c.latest
    if not st:
        return True
    state = st["state"]
    zone = cast_zone(state, pend["oid"])
    if zone in ("stack", "battlefield"):
        kind = pend["kind"]
        ST["cast_pending"] = None
        ST[kind + "_cast"] = True
        say(f"[{tag}] cast confirmed on {zone}: {pend['name']} "
            f"(oid {pend['oid']})")
        wire("cast_confirmed", {"kind": kind, "oid": pend["oid"],
                                "zone": zone})
        return False
    if zone == "hand" and time.time() - pend["since"] > 30:
        ST["cast_pending"] = None
        ST["mana_needs"][tag] = {}
        if pend["kind"] == "grudge":
            ST["stage"] = "SETUP"
            ST["cast_submitted"] = False
            # NOTE: ST["cast_offered"] stays True -- the gate DID offer it;
        say(f"[{tag}] cast NOT confirmed after 30s (still in hand): "
            f"{pend['name']} (oid {pend['oid']}); will retry")
        wire("cast_dropped_retry", {"kind": pend["kind"], "oid": pend["oid"]})
        return False
    return True

# ------------------------------------------------------------------- ticks

async def p_land_drop(c, pid, state, acts, land_pref):
    if state.get("land_played_this_turn"):
        return False
    hand = hand_ids(state, pid)
    for name in land_pref:
        for oid in hand:
            if obj_lname(state, oid) != name:
                continue
            for a in acts:
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(oid):
                    await submit_as_is(c, a)
                    say(f"[{c.name}] land drop {name}")
                    return True
    return False


async def p_cast_memnite(c, pid, state, acts, tag):
    if bf_by_name(state, pid, MEMNITE):
        return False
    if ST.get("cast_pending"):
        return False
    hand = hand_ids(state, pid)
    oid = next((o for o in hand if obj_lname(state, o) == MEMNITE), None)
    if oid is None:
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            ST["cast_pending"] = {"kind": "memnite", "oid": str(oid),
                                  "name": "Memnite", "since": time.time()}
            await submit_as_is(c, a)
            say(f"[{tag}] cast Memnite (oid={oid})")
            return True
    obj = get_obj(state, oid)
    cid = obj.get("card_id", oid)
    ST["cast_pending"] = {"kind": "memnite", "oid": str(oid),
                          "name": "Memnite", "since": time.time()}
    await c.send_action({"type": "CastSpell",
                         "data": {"object_id": int(oid), "card_id": int(cid),
                                  "targets": [], "payment_mode": {"type": "Auto"}}})
    say(f"[{tag}] cast Memnite raw (oid={oid})")
    return True


def grudge_on_stack(state):
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str).lower().replace(" ", "")
        if "fatalgrudge" in blob or str(ST.get("grudge_oid")) in blob:
            return True
    return False


async def p0_tick(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True

    wf = wf_type(state)
    wplayer = ((state.get("waiting_for") or {}).get("data") or {}).get("player")
    if wf == "DeclareAttackers" and wplayer == pid:
        await answer_declare_attackers(c, st, state, tag)
        return True

    if await handle_castoffer(c, pid, state, st):
        return True

    if ST["stage"] in ("CAST", "RESOLVING"):
        if await answer_sacrifice_prompt(c, st, state):
            return True

    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get("P0", {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True

    # cast-confirmation guard (2026-10-09 lesson): hold while a CastSpell
    # is in flight. Priority passes and new main-phase plays wait.
    holding = await guard_cast_pending(c, tag)

    # never pass while a P0 decision is pending
    if real_decision_pending(st):
        return True

    if "DeclareBlockers" in {a.get("type") for a in acts}:
        return True

    if holding:
        return True

    if ST["stage"] == "SETUP" and is_my_main(state, pid) \
            and my_priority(acts) and not (state.get("stack") or []):
        if await p_cast_memnite(c, pid, state, acts, tag):
            return True
        if await p_land_drop(c, pid, state, acts, (SWAMP, MOUNTAIN)):
            return True
        # conditions to attempt the Grudge cast
        hand = hand_ids(state, pid)
        goid = next((o for o in hand if obj_lname(state, o) == GRUDGE), None)
        moid = next(iter(bf_by_name(state, pid, MEMNITE)), None)
        p1m = next(iter(bf_by_name(state, 1, MEMNITE)), None)
        if moid and goid and p1m \
                and untapped_of(state, pid, SWAMP) >= 1 \
                and untapped_of(state, pid, MOUNTAIN) >= 1:
            ST["memnite_oid"] = moid
            ST["p1_memnite_oid"] = p1m
            if not ST["pre_exported"]:
                pre = await do_export(c, "pre.json")
                ST["pre_exported"] = True
                ST["grudge_oid"] = goid
                ST["library_pre"] = lib_count(pre, pid)
                wire("pre", {"grudge_oid": goid, "memnite_oid": moid,
                             "p1_memnite_oid": p1m,
                             "library_pre": ST["library_pre"],
                             "turn": pre.get("turn_number"),
                             "phase": pre.get("phase")})
                say(f"[pre] exported turn {pre.get('turn_number')} "
                    f"phase={pre.get('phase')}")
            await asyncio.sleep(0)  # yield before leg evaluation
            st2 = c.latest
            acts2 = merged_actions(st2) if st2 else acts
            a = castspell_advertised(acts2, goid)
            if a:
                ST["cast_offered"] = True
                wire("castspell_advertised", {"action": a})
                say(f"[P0] Fatal Grudge offered (oid={goid}) - gate OK")
                ST["mana_needs"]["P0"] = {"B": 1, "R": 1}
                ST["cast_pending"] = {"kind": "grudge", "oid": str(goid),
                                      "name": "Fatal Grudge",
                                      "since": time.time()}
                await submit_as_is(c, a)
                ST["cast_submitted"] = True
                ST["stage"] = "CAST"
                ST["cast_at"] = time.time()
                return True
            else:
                wire("no_castspell_offer", {
                    "flat_types": sorted(set(x.get("type") for x in acts2)),
                    "grudge_oid": goid,
                })
                say(f"[P0] Fatal Grudge NOT offered (oid={goid}) - BUG CANDIDATE")
                ST["no_offer_observations"] += 1
                n = ST["no_offer_observations"]
                if n >= 4 and not ST.get("post_exported"):
                    # 4 main-phase observations with no offer: capture and stop
                    await do_export(c, "post.json")
                    ST["post_exported"] = True
                    ST["stop"] = True
                    say(f"[P0] {n} main-phase observations, cast never "
                        f"offered; exported post.json, stopping")
                    return True

    if any(a.get("type") == "PassPriority" for a in acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            if await pass_priority(c, st, acts):
                PASSED_REV[c.name] = c.revision
                return True
    return False


async def p1_tick(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_bottom(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True

    wf = wf_type(state)
    wplayer = ((state.get("waiting_for") or {}).get("data") or {}).get("player")
    if wf == "DeclareAttackers" and wplayer == pid:
        await answer_declare_attackers(c, st, state, tag)
        return True

    if ST["stage"] in ("CAST", "RESOLVING"):
        if await answer_opp_choice(c, pid, st, state):
            return True

    holding = await guard_cast_pending(c, tag)
    if real_decision_pending(st):
        return True
    if "DeclareBlockers" in {a.get("type") for a in acts}:
        return True
    if holding:
        return True

    if is_my_main(state, pid) and my_priority(acts) \
            and not (state.get("stack") or []):
        if await p_cast_memnite(c, pid, state, acts, tag):
            return True
        if await p_land_drop(c, pid, state, acts, (MOUNTAIN,)):
            return True

    if any(a.get("type") == "PassPriority" for a in acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            if await pass_priority(c, st, acts):
                PASSED_REV[c.name] = c.revision
                return True
    return False
# ------------------------------------------------------------------- main

def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 800
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #7343 — Fatal Grudge cast gate (revalidation)",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} — {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: P0 PreCombatMain, Grudge in hand, Memnite+lands ready",
        "A2_cast_offered": "A2 [bug] CastSpell advertised for Fatal Grudge",
        "A3_sac_paid": "A3 additional cost paid: P0 Memnite sacc'd, {B}{R} tapped",
        "A4_opp_sac": "A4 P1 chose+sacrificed a sharing-type permanent",
        "A5_draw": "A5 P0 library decreased by exactly 1",
        "A6_cleanup": "A6 Grudge in P0 graveyard, stack empty",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:9]:
        d.text((40, y), str(n)[:122], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7343/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


async def main():
    t0 = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_cast_offered", "A3_sac_paid",
            "A4_opp_sac", "A5_draw", "A6_cleanup")}

    ver, build, proto = await verify_server_hello()
    data_ok = check_data_level()
    if not data_ok:
        notes.append("data-level check FAILED -- see data_evidence.json")

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    try:
        await p0.create(deck(*P0_DECK))
    except Exception as e:
        say(f"game creation failed: {e}")
        ass["A1_setup_ok"] = "blocked"
        notes.append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": ass, "notes": notes}, f, indent=2)
        await p0.close()
        return
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last = {}
    last_tick_at = {}
    progress_rev = -1
    progress_at = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        for c, tick, tag, pid in ((p0, p0_tick, "P0", p0.player_id),
                                  (p1, p1_tick, "P1", p1.player_id)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            # re-tick backstop: re-tick a client holding priority with no
            # revision change for > 5s
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if rev == last.get(c.name) and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, pid, tag)
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        await asyncio.sleep(0)  # yield to pump before leg evaluation
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        ST["states_seen"] += 1
        record_wf(state)
        pid0, pid1 = p0.player_id, p1.player_id

        # ---- CAST flow checkpoints ----
        if ST.get("stage") == "CAST":
            stack = state.get("stack") or []
            moid = ST.get("memnite_oid")
            sac_done = (MEMNITE in gy_names(state, pid0)
                        and moid is not None
                        and not any(str(oid) == str(moid)
                                    for oid in bf_oids(state, pid0)))
            if sac_done and not ST.get("mid_exported"):
                mid = await do_export(p0, "mid_cast.json")
                ST["mid_exported"] = True
                wire("mid_cast", {"stack_n": len(stack), "wf": wf_type(state),
                                  "grudge_on_stack": grudge_on_stack(mid)})
                say(f"mid_cast: sacrifice paid, stack={len(stack)} "
                    f"wf={wf_type(state)} grudge_on_stack={grudge_on_stack(mid)}")
                ST["stage"] = "RESOLVING"
            if not stack and ST.get("sac_paid") and not ST.get("post_exported"):
                post = await do_export(p0, "post.json")
                ST["post_exported"] = True
                ST["library_post"] = lib_count(post, pid0)
                say(f"post: stack empty after cast, "
                    f"library_post={ST['library_post']}")
                ST["stop"] = True
            if not ST.get("post_exported") and now_safe() - (ST.get("cast_at") or now_safe()) > 150:
                say("cast stalled 150s; exporting post and stopping")
                wire("cast_stalled", {"wf": wf_type(state)})
                await do_export(p0, "post.json")
                ST["post_exported"] = True
                ST["stop"] = True

        if ST.get("stage") == "RESOLVING":
            stack = state.get("stack") or []
            if not stack and not ST.get("post_exported"):
                post = await do_export(p0, "post.json")
                ST["post_exported"] = True
                ST["library_post"] = lib_count(post, pid0)
                say(f"post: resolved, library_post={ST['library_post']}")
                ST["stop"] = True
            if not ST.get("post_exported") and now_safe() - (ST.get("cast_at") or now_safe()) > 240:
                say("resolving stalled 240s; exporting post and stopping")
                await do_export(p0, "post.json")
                ST["post_exported"] = True
                ST["stop"] = True

        # generic progress watchdog: no revision advance at all for 180s
        if max(p0.revision, p1.revision) != progress_rev:
            progress_rev = max(p0.revision, p1.revision)
            progress_at = time.time()
        if time.time() - progress_at > 180:
            await do_export(p0, "mid_stall.json")
            notes.append("generic stall watchdog: no revision advance >180s")
            say("[stall] generic watchdog fired")
            ST["stop"] = True
            continue

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            say("game over")
            continue

        if time.time() - ST["last_diag"] > 30:
            ST["last_diag"] = time.time()
            hand0 = [obj_lname(state, o) for o in hand_ids(state, pid0)]
            bf0 = [obj_lname(state, o) for o in bf_oids(state, pid0)]
            bf1 = [obj_lname(state, o) for o in bf_oids(state, pid1)]
            stack = state.get("stack") or []
            say(f"DIAG turn={state.get('turn_number')} phase={state.get('phase')} "
                f"wf={wf_type(state)} stack={len(stack)} "
                f"p0hand={len(hand0)} p0mem={bf0.count(MEMNITE)} "
                f"p0grudge_hand={hand0.count(GRUDGE)} "
                f"p1mem={bf1.count(MEMNITE)} "
                f"untap_sw={untapped_of(state, pid0, SWAMP)} "
                f"untap_mo={untapped_of(state, pid0, MOUNTAIN)} "
                f"stage={ST.get('stage')} sac={ST.get('sac_paid')} "
                f"opp={ST.get('opp_choice_done')} "
                f"no_offer_n={ST.get('no_offer_observations')}")
            wire("diag", {"turn": state.get("turn_number"), "phase": state.get("phase"),
                          "wf": wf_type(state), "stack_n": len(stack),
                          "p0hand": hand0, "p0bf": bf0, "p1bf": bf1})

        if not ST.get("post_exported") and time.time() - t0 > TIMEOUT - 120:
            await do_export(p0, "post.json")
            ST["post_exported"] = True
            ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre.json")
    mid = env_state("mid_cast.json")
    post = env_state("post.json")
    pid0, pid1 = 0, 1

    # A1
    if pre is None:
        ass["A1_setup_ok"] = "failed"
        notes.append("pre.json never exported; A1 failed")
    else:
        ok_phase = pre.get("active_player") == pid0 and pre.get("phase") == "PreCombatMain"
        ok_grudge = any(obj_lname(pre, o) == GRUDGE for o in hand_ids(pre, pid0))
        ok_m0 = bool(bf_by_name(pre, pid0, MEMNITE))
        ok_m1 = bool(bf_by_name(pre, pid1, MEMNITE))
        ok_sw = untapped_of(pre, pid0, SWAMP) >= 1
        ok_mo = untapped_of(pre, pid0, MOUNTAIN) >= 1
        ass["A1_setup_ok"] = "passed" if (ok_phase and ok_grudge and ok_m0
                                          and ok_m1 and ok_sw and ok_mo) else "failed"
        notes.append(
            f"pre: main_phase={ok_phase} grudge_in_hand={ok_grudge} "
            f"p0_memnite_bf={ok_m0} p1_memnite_bf={ok_m1} "
            f"untapped_swamp={untapped_of(pre, pid0, SWAMP)} "
            f"untapped_mountain={untapped_of(pre, pid0, MOUNTAIN)} "
            f"turn={pre.get('turn_number')} library_pre={ST['library_pre']}")

    # A2
    ass["A2_cast_offered"] = "passed" if ST.get("cast_offered") else "failed"
    notes.append(f"Fatal Grudge CastSpell advertised: {ST.get('cast_offered')} "
                 f"({ST.get('no_offer_observations')} no-offer main-phase observations)")

    cost_src = mid or post
    # A3
    if not ST.get("cast_offered"):
        ass["A3_sac_paid"] = "not-run"
        notes.append("A3 not-run: cast never offered/submitted")
    elif cost_src is None:
        ass["A3_sac_paid"] = "failed"
        notes.append("A3 failed: cast offered but no mid/post state captured")
    else:
        mem_sacced = (str(ST.get("memnite_oid")) in
                      {str(o) for o in gy_ids(cost_src, pid0)}) \
            or MEMNITE in gy_names(cost_src, pid0)
        sw_tapped = sum(1 for o in bf_by_name(cost_src, pid0, SWAMP)
                        if get_obj(cost_src, o).get("tapped")) >= 1
        mo_tapped = sum(1 for o in bf_by_name(cost_src, pid0, MOUNTAIN)
                        if get_obj(cost_src, o).get("tapped")) >= 1
        grudge_gone = grudge_on_stack(cost_src) or GRUDGE in gy_names(cost_src, pid0)
        ass["A3_sac_paid"] = "passed" if (mem_sacced and sw_tapped and mo_tapped
                                         and grudge_gone) else "failed"
        notes.append(f"A3: memnite_saccd={mem_sacced} swamp_tapped={sw_tapped} "
                     f"mountain_tapped={mo_tapped} grudge_left_hand={grudge_gone}")

    # A4
    if not ST.get("cast_offered"):
        ass["A4_opp_sac"] = "not-run"
        notes.append("A4 not-run: cast never offered/submitted")
    elif post is None:
        ass["A4_opp_sac"] = "failed"
        notes.append("A4 failed: no post state captured")
    else:
        p1m_gone = (str(ST.get("p1_memnite_oid")) in
                    {str(o) for o in gy_ids(post, pid1)}) \
            or MEMNITE in gy_names(post, pid1)
        ass["A4_opp_sac"] = "passed" if p1m_gone else "failed"
        notes.append(f"A4: p1_memnite_in_gy={p1m_gone} "
                     f"opp_choice_done={ST.get('opp_choice_done')}")

    # A5
    if ST.get("library_pre") is None or ST.get("library_post") is None:
        ass["A5_draw"] = "not-run"
        notes.append(f"A5 not-run: library_pre={ST.get('library_pre')} "
                     f"library_post={ST.get('library_post')}")
    else:
        d = ST["library_pre"] - ST["library_post"]
        ass["A5_draw"] = "passed" if d == 1 else "failed"
        notes.append(f"A5: library {ST['library_pre']} -> {ST['library_post']} "
                     f"(delta={d}, expect 1)")

    # A6
    if post is None:
        ass["A6_cleanup"] = "not-run"
        notes.append("A6 not-run: no post state captured")
    elif ST.get("cast_offered"):
        grudge_gy = GRUDGE in gy_names(post, pid0)
        stack_empty = not (post.get("stack") or [])
        ass["A6_cleanup"] = "passed" if (grudge_gy and stack_empty) else "failed"
        notes.append(f"A6: grudge_in_p0_gy={grudge_gy} stack_empty={stack_empty} "
                     f"phase={post.get('phase')}")
    else:
        # cast never offered: cleanup = stack empty, game proceeding,
        # no stuck decisions or rejections (matches the v0.99.0 run)
        stack_empty = not (post.get("stack") or [])
        no_rej = len(ST.get("rejections") or []) == 0
        ass["A6_cleanup"] = "passed" if (stack_empty and no_rej) else "failed"
        notes.append(f"A6: stack_empty={stack_empty} rejections={len(ST['rejections'])} "
                     f"phase={post.get('phase')} wf={wf_type(post)} "
                     f"(cast never offered; judged on game state alone)")

    for k in sorted(ass):
        say(f"{k}: {ass[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN,
                   "no_offer_observations": ST.get("no_offer_observations"),
                   "grudge_oid": ST.get("grudge_oid"),
                   "memnite_oid": ST.get("memnite_oid"),
                   "p1_memnite_oid": ST.get("p1_memnite_oid")}, f, indent=2)

    scenario_src = open(__file__, "rb").read()

    # verdict per behavioral contract
    verdict = "blocked"
    if ass.get("A1_setup_ok") == "passed":
        if ass.get("A2_cast_offered") == "failed":
            verdict = "reproduced"
        elif ass.get("A2_cast_offered") == "passed" and \
                (ass.get("A3_sac_paid") == "failed"
                 or ass.get("A4_opp_sac") == "failed"):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
    say("verdict:", verdict)

    def sha(p):
        return sha256_of_bytes(open(p, "rb").read())

    run_meta = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/7343",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00", time.localtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00", time.localtime(time.time())),
        "server": {
            "server_version": "v0.114.0",
            "build_commit": "ac67cbb",
            "protocol_version": 153,
            "binary_sha256": sha(f"{BACKFILL}/server/releases/v0.114.0/"
                                  "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(f"{BACKFILL}/server/releases/v0.114.0/data/"
                                     "card-data.json"),
            "draft_pools_sha256": sha(f"{BACKFILL}/server/releases/v0.114.0/data/"
                                      "draft-pools.json"),
            "port": 9374,
        },
        "server_run_dir": "runs/run-6772-reval-v01140-20261020-0941 "
                          "(shared v0.114.0 server on 127.0.0.1:9374, started "
                          "2026-10-20 ~09:41 CDT by the #6772 run; evidence "
                          "under evidence/7343/" + RUN_ID + ")",
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "scenario_file": "driver/scenario_7343_01140.py",
        "decks": {"P0": dict(P0_DECK), "P1": dict(P1_DECK)},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"],
                  "rejections": len(ST["rejections"])},
        "setup_line": ("P0: 4x Fatal Grudge + 8x Memnite + 24x Swamp + 24x "
                       "Mountain; P1: 12x Memnite + 48x Mountain; native "
                       "human seats"),
        "contract_line": ("assemble P0 PreCombatMain w/ Grudge in hand + "
                          "untapped Memnite/Swamp/Mountain + P1 Memnite, "
                          "export pre, assert the cast gate offers CastSpell; "
                          "if offered, pay {B}{R}, sacrifice Memnite, P1 "
                          "chooses+sacrifices its Memnite, draw 1, Grudge to "
                          "gy (reported: never offered)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "Server binary shared with the #6772 run of this morning; no "
            "dedicated server log for this run (wire log + exports carry "
            "the evidence).",
        ],
        "driver_notes": [
            "Mechanical protocol-153 port of the verified protocol-98 "
            "scenario_7343.py (run 20261001-7343, v0.99.0). Identity port: "
            "client.py HELLO already advertises protocol 153; ServerHello "
            "asserts 0.114.0/ac67cbb/protocol 153/Full.",
            "MulliganDecision via legacy Action (Keep); bottom/SelectCards "
            "via vi schema/select gated on waitingForKind.code=='mulligan' "
            "+ turn 1/Untap; DiscardToHandSize via vi; payments via vi "
            "tapLandForMana / PayMana; CastOffer via the 7313 answer pattern; "
            "priority-gated passes; sleep(0) yield before leg evaluation; "
            "re-tick backstop for priority-holding clients.",
            "Cast-confirmation guard (2026-10-09 lesson): after every "
            "CastSpell the driver holds (no priority pass, no new main-phase "
            "plays) until the spell is confirmed on the stack/battlefield; "
            "a 30s backstop clears a still-in-hand cast as dropped and "
            "resets the one-shot flags so the cast block retries.",
            "Same behavioral contract as the verified v0.99.0 run: "
            "reproduced iff A1 passes and A2 fails, or A2 passes but the "
            "additional-cost/opponent-choice flow stalls/no-ops (A3/A4 "
            "fail).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=1)

    with open(f"{EVDIR}/scenario_7343.py", "w") as f:
        f.write(scenario_src.decode())

    run_for_png = {"run_id": RUN_ID, "verdict": verdict, "assertions": ass,
                   "notes": notes,
                   "server_identity": {"server_version": "v0.114.0",
                                       "build_commit": "ac67cbb",
                                       "protocol_version": 153}}
    render_summary(run_for_png, f"{EVDIR}/summary.png")
    say("rendered summary.png")

    # Close logs BEFORE computing the manifest so their hashes are final.
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read())
                     + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest: {len(lines)} files")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    await p0.close()
    await p1.close()


def now_safe():
    return time.time()


if __name__ == "__main__":
    asyncio.run(main())
