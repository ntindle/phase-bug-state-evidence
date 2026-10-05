#!/usr/bin/env python3
"""Issue #7180 revalidation on v0.102.0 (protocol 106).

"Apex Altisaur should have a 'skip' on its triggered ability -- Since
[[Apex Altisaur]] fights 'up to one' creature, choosing no target should be
an option."

Oracle (pinned card-data.json): "When this creature enters, it fights up to
one target creature you don't control. Enrage -- Whenever this creature is
dealt damage, it fights up to one target creature you don't control."

Contract:
  A1 setup_ok            both clients attached, game started
  A2 zero_option_etb     ETB fight target selection offers a zero-target/skip
                         option (schema min=0, or a textual skip/decline/none
                         choice)
  A3 etb_resolution      forced/accepted ETB fight resolves: chosen bear in
                         P1 graveyard, Altisaur on battlefield (not-run when
                         the skip branch is taken instead)
  A4 enrage_triggered    after Altisaur is dealt damage, an enrage fight
                         target selection is offered
  A5 zero_option_enrage  enrage target selection offers a zero-target/skip
                         option
  A6 enrage_resolution   enrage fight resolves: bear in P1 graveyard,
                         Altisaur alive on battlefield

Branch plan: at the ETB selection, if a skip is offered the driver TAKES it
(the reported branch); enrage is then triggered manually by bolting P0's own
Altisaur. If no skip is offered, the driver takes the forced path (bear
target) and enrage follows from fight damage. Either way the enrage leg
answers with a bear target so fight resolution is verified.

Verdict: reproduced iff the game is driven to completion and at least one of
A2/A5 shows no skip offered; not-reproduced iff A2 and A5 pass with A3/A6 (as
applicable) passing; blocked otherwise.

Protocol-106 conventions (from the verified scenario_301_01020.py):
  - priority = PassPriority in the viewing seat's top-level legal_actions;
    decisions surface via viewer_interaction; waiting_for is gone.
  - MulliganDecision via legacy Action; bottom/discard via vi schema/select.
  - 106 priority menus (tapLandForMana/castSpell/activateAbility choice menus)
    are NOT decisions (NON_DECISION_CODES); they must not block passes.
  - playLand handled explicitly with is_land(); ExportAuthoritativeState
    returns a JSON string in data.state, parsed once.
  - Export-only checkpoints fall through; never return after an export while
    holding priority. Re-tick backstop: re-tick a priority-holding client
    with no revision change for > 5s.
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
log = logging.getLogger("scenario7180_106")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261005-7180"
ISSUE = 7180
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.102.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828); data digests match the "
                       "signed manifest; digests recomputed against on-disk "
                       "artifacts this run"),
    "source": ("2026-10-05: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "shared pinned server on 127.0.0.1:9374 reused (verified "
               "healthy by this run's own handshake)"),
}

for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

ALTISAUR = "apex altisaur"
BOLT = "lightning bolt"
BEAR = "grizzly bears"
FOREST = "forest"

P0_DECK = [(ALTISAUR, 12), (BOLT, 8), (FOREST, 30), ("mountain", 10)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]


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


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def gy_by_name(state, pid, lname):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)
            and obj_lname(state, oid) == lname]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped"))


def untapped_forests(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == "forest"
               and not get_obj(state, o).get("tapped"))


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
        rdata = {"choiceIds": [cid]}
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": rdata}}
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
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"


def check_data_level():
    notes = []
    ok = True
    alt = CARD_DATA.get("apex altisaur", {})
    if "fights up to one target" not in str(alt.get("oracle_text", "")).lower():
        ok = False
        notes.append("apex altisaur oracle missing 'fights up to one target'")
    bear = CARD_DATA.get("grizzly bears", {})
    if str(bear.get("power", {}).get("value")) != "2":
        ok = False
        notes.append("grizzly bears not 2 power")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": {"apex altisaur": str(alt.get("oracle_text"))[:300],
                              "grizzly bears power/toughness":
                              f"{bear.get('power', {}).get('value')}/"
                              f"{bear.get('toughness', {}).get('value')}"}}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok

# ---------------------------------------------------------------- mulligan / bottom / discard

async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in surf_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn, "iid": iid})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_bottom(c, acts, st, pid, tag):
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
        if nm in (ALTISAUR, BEAR):
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds": [ch.get("id") for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    code = vi_kind_code(st)
    if "discard" not in code.lower():
        found = False
        handset = set(hand)
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            rdata = resp.get("data", {}) or {}
            spec = rdata.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            cands = rdata.get("candidates") or []
            if any(_cand_reference(ch) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        if nm in (ALTISAUR, BEAR, BOLT):
            return (5, nm)
        if is_land(get_obj(state, o)):
            return (0, nm)
        return (2, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


# ---------------------------------------------------------------- mana / priority / land

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


# ---------------------------------------------------------------- target selection + skip analysis

def target_opportunity(st):
    """First vi opportunity that looks like a target selection: schema
    select/sequence with candidates, or exactChoices with candidate/target
    codes (passPriority / decideOptionalEffect menus excluded)."""
    for opp in vi_ops(st):
        if opp.get("interactionId") in SUBMITTED_OPPS:
            continue
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
                    and ("decideOptionalEffect" not in codes) \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def analyze_skip_option(state, opp):
    """Inspect a fight target-selection opportunity for a zero-target/skip
    option. Returns (skip_offered, info dict)."""
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    info = {"rtype": rtype, "skip_offered": False, "signals": []}
    cands = data.get("candidates") or data.get("choices") or []
    info["n_candidates"] = len(cands)
    if rtype == "schema":
        spec = data.get("spec", {}) or {}
        info["spec_type"] = spec.get("type")
        con = ((spec.get("data") or {}).get("constraint", {}) or {}).get("data", {}) or {}
        info["constraint"] = con
        if con.get("min") == 0:
            info["skip_offered"] = True
            info["signals"].append("schema constraint min=0")
    for ch in cands:
        txt = choice_text(ch).lower()
        if any(w in txt for w in ("skip", "no target", "decline", "none", "pass", "zero")):
            info["skip_offered"] = True
            info["signals"].append(f"textual skip choice: {txt[:60]!r}")
        codes = set(c for c in surf_codes(ch) if c)
        if codes & {"skip", "noTarget", "declineTarget"}:
            info["skip_offered"] = True
            info["signals"].append(f"skip code in {sorted(codes)}")
    refs = []
    for ch in cands:
        r = _cand_reference(ch)
        refs.append({"choice_id": ch.get("id"), "ref": r,
                     "name": obj_lname(state, r) if r is not None else None,
                     "text": choice_text(ch)[:60]})
    info["candidates"] = refs
    return info["skip_offered"], info


def pick_bear_target(state, opp):
    """Pick a candidate referencing a battlefield creature controlled by P1
    (a bear)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        r = _cand_reference(ch)
        if r is None:
            continue
        o = get_obj(state, r)
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller", -1)) == "1" \
                and obj_lname(state, r) == BEAR:
            return ch
    # fallback: any battlefield creature not controlled by P0
    for ch in cands:
        r = _cand_reference(ch)
        if r is None:
            continue
        o = get_obj(state, r)
        if o.get("zone") == "Battlefield" \
                and str(o.get("controller", -1)) != "0":
            return ch
    return None


def find_skip_choice(opp):
    """If the opportunity carries a textual skip/decline choice, return it."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    for ch in data.get("candidates") or data.get("choices") or []:
        txt = choice_text(ch).lower()
        if any(w in txt for w in ("skip", "no target", "decline", "none")):
            return ch
    return None


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting target: id={cid} kind={sub['response']['type']}")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    SUBMITTED_OPPS.add(iid)
    await interact_as(c, sub, tag)
    return iid, cid


async def submit_skip(c, opp, rtype, spec_type, tag):
    """Take the zero-target branch of a fight trigger target selection."""
    iid = opp.get("interactionId")
    ch = find_skip_choice(opp)
    if ch is not None:
        say(f"[{tag}] taking textual skip choice: {choice_text(ch)[:60]!r}")
        wire("skip_submission", {"who": tag, "via": "textual_choice"})
        SUBMITTED_OPPS.add(iid)
        await answer_vi(c, opp, ch, tag)
        return iid
    if rtype == "schema":
        say(f"[{tag}] taking schema empty selection (min=0)")
        wire("skip_submission", {"who": tag, "via": "empty_choiceIds"})
        SUBMITTED_OPPS.add(iid)
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": spec_type,
                                           "data": {"choiceIds": []}}}, tag)
        return iid
    say(f"[{tag}] WARNING: skip offered but no submittable form found")
    return None

# ---------------------------------------------------------------- ticks

async def generic_tick(c, g, tag, pid):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
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
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return
    if my_main(state, pid):
        if await play_a_land(c, state, pid, acts, tag):
            return
        if pid == 1:
            # P1: cast bears while it can, up to 5 on the battlefield.
            if len(bf_by_name(state, 1, BEAR)) < 5 \
                    and BEAR in [obj_lname(state, o) for o in hand_ids(state, 1)] \
                    and untapped_forests(state, 1) >= 1 \
                    and untapped_lands(state, 1) >= 2:
                a, oid = cast_action_for(acts, state, BEAR)
                if a is not None:
                    ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                    say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                    wire("cast_bear", {"oid": oid})
                    await submit_as_is(c, a)
                    return True
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick_ramp(c, g, tag):
    """Ramp P0 to 9 mana, then cast Apex Altisaur once P1 has >=2 bears out.
    Returns ('ALTISAUR_BF', oid) once it is on the battlefield."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
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

    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    bf = bf_by_name(state, 0, ALTISAUR)
    if bf:
        return ("ALTISAUR_BF", bf[0])
    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        on_stack = any(obj_lname(state, oid) == ALTISAUR
                       for oid, o in (state.get("objects") or {}).items()
                       if o.get("zone") == "Stack")
        if not on_stack \
                and ALTISAUR in [obj_lname(state, o) for o in hand_ids(state, 0)] \
                and untapped_lands(state, 0) >= 9 \
                and untapped_forests(state, 0) >= 2 \
                and len(bf_by_name(state, 1, BEAR)) >= 2:
            a, oid = cast_action_for(acts, state, ALTISAUR)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 2, "generic": 7}
                say(f"[{tag}] casting Apex Altisaur (oid {oid})")
                wire("cast_altisaur", {"oid": oid})
                await submit_as_is(c, a)
                return True
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def settle(p0, p1, g, ramp_tag, cond, timeout, label, poll=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(poll)
        await generic_tick(p1, g, f"P1{ramp_tag}", 1)
        await generic_tick(p0, g, ramp_tag, 0)
        await asyncio.sleep(0)
        st = p0.latest
        if st and cond(st["state"]):
            return st["state"]
    say(f"TIMEOUT in settle: {label}")
    return None


# ---------------------------------------------------------------- exports

async def export_named(c, name):
    try:
        raw = await c.export_state()
        env = json.loads(raw)
        assert "state" in env, "envelope missing 'state'"
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(raw)
        say(f"exported {name}.json (turn={env['state'].get('turn_number')})")
        return env["state"]
    except Exception as e:
        say(f"export {name} FAILED: {e!r}")
        return None


def save_prompt_detail(name, state, st):
    try:
        detail = {
            "name": name,
            "captured_at": time.time(),
            "turn_number": state.get("turn_number"),
            "phase": state.get("phase"),
            "viewer_interaction": st.get("viewer_interaction"),
            "legal_actions": st.get("legal_actions"),
            "legal_actions_by_object": st.get("legal_actions_by_object"),
            "stack": state.get("stack"),
        }
        with open(f"{EVDIR}/{name}_detail.json", "w") as f:
            json.dump(detail, f, indent=1, default=str)
        say(f"saved {name}_detail.json")
    except Exception as e:
        say(f"prompt detail {name} FAILED: {e!r}")


# ---------------------------------------------------------------- game runner

def altisaur_bf(state):
    return bf_by_name(state, 0, ALTISAUR)


async def run_game():
    global PASSED_REV
    obs = {"assert": {}, "notes": [], "events": []}
    ramp_tag = "7180"
    ST["mana_needs"][ramp_tag] = {}
    ST["mana_needs"][f"P1{ramp_tag}"] = {}

    p0 = PhaseClient(f"P0{ramp_tag}")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    p1 = PhaseClient(f"P1{ramp_tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"game_code": p0.game_code})
    obs["assert"]["A1_setup_ok"] = "passed" if (p0.player_id is not None
                                               and p1.player_id is not None) else "failed"
    if obs["assert"]["A1_setup_ok"] != "passed":
        await p0.close()
        await p1.close()
        return obs

    # ---- ramp until Apex Altisaur is on the battlefield
    t_ramp = time.time()
    last_rev, last_change, last_tick_at = {}, {0: time.time(), 1: time.time()}, {}
    altisaur_id = None
    last_diag = time.time()
    try:
        while time.time() - t_ramp < 1500:
            await asyncio.sleep(0.15)
            for c, is_p0 in ((p0, True), (p1, False)):
                st = c.latest
                if not st:
                    continue
                if c.revision != last_rev.get(c.name):
                    last_rev[c.name] = c.revision
                    last_change[c.player_id] = time.time()
                else:
                    if not (my_priority(top_acts(st))
                            and time.time() - last_tick_at.get(c.name, 0) > 5):
                        continue
                last_tick_at[c.name] = time.time()
                try:
                    if is_p0:
                        r = await p0_tick_ramp(c, {"mode": "ramp"}, ramp_tag)
                        if isinstance(r, tuple) and r[0] == "ALTISAUR_BF":
                            altisaur_id = r[1]
                            break
                    else:
                        await generic_tick(c, {"mode": "ramp"}, f"P1{ramp_tag}", 1)
                except Exception as e:
                    say(f"[{ramp_tag}] tick error {c.name}: {type(e).__name__}: {e}")
            if altisaur_id:
                break
            if state_turn(p0) and state_turn(p0) > 40:
                obs["notes"].append("turn cap 40 hit during ramp")
                break
            if time.time() - last_diag > 30:
                last_diag = time.time()
                diag(p0, p1, ramp_tag)
    except Exception as e:
        obs["notes"].append(f"ramp loop exception: {e}")
    if not altisaur_id:
        for k in ("A2_zero_option_etb", "A3_etb_resolution", "A4_enrage_triggered",
                  "A5_zero_option_enrage", "A6_enrage_resolution"):
            obs["assert"][k] = "not-run"
        obs["notes"].append("never got Apex Altisaur onto the battlefield")
        await p0.close()
        await p1.close()
        return obs
    say(f"Apex Altisaur on battlefield (oid {altisaur_id})")
    obs["events"].append({"t": "altisaur_bf", "oid": altisaur_id})

    # ---- ETB leg: wait for the fight target selection
    etb = await handle_trigger_leg(p0, p1, ramp_tag, "etb", altisaur_id, obs,
                                   take_skip_if_offered=True)
    if etb is None:
        for k in ("A2_zero_option_etb", "A3_etb_resolution", "A4_enrage_triggered",
                  "A5_zero_option_enrage", "A6_enrage_resolution"):
            obs["assert"].setdefault(k, "not-run")
        obs["notes"].append("ETB target selection never appeared")
        await p0.close()
        await p1.close()
        return obs
    skip_taken, bear1_gy = etb

    if skip_taken:
        # Reported branch taken at ETB: trigger resolved with no target.
        # Enrage must be triggered manually: bolt P0's own Altisaur.
        obs["assert"]["A3_etb_resolution"] = "not-run"
        obs["notes"].append("ETB skip taken; no ETB fight to resolve")
        ok = await bolt_own_altisaur(p0, p1, ramp_tag, altisaur_id, obs)
        if not ok:
            for k in ("A4_enrage_triggered", "A5_zero_option_enrage", "A6_enrage_resolution"):
                obs["assert"].setdefault(k, "not-run")
            obs["notes"].append("could not bolt own Altisaur for the enrage leg")
            await p0.close()
            await p1.close()
            return obs
    else:
        # Forced ETB fight path: bear1 must be in P1's graveyard now.
        s = await settle(p0, p1, {"m": 1}, ramp_tag,
                         lambda s: len(gy_by_name(s, 1, BEAR)) >= 1, 180,
                         "ETB fight resolution")
        a3 = s is not None and len(gy_by_name(s, 1, BEAR)) >= 1 \
            and len(altisaur_bf(s)) == 1
        obs["assert"]["A3_etb_resolution"] = "passed" if a3 else "failed"
        obs["notes"].append(f"ETB fight: bear1 gy={len(gy_by_name(s, 1, BEAR)) if s else '?'} "
                            f"altisaur_bf={len(altisaur_bf(s)) if s else '?'}")
        if s is not None:
            await export_named(p0, "post_etb")
        if not a3:
            for k in ("A4_enrage_triggered", "A5_zero_option_enrage", "A6_enrage_resolution"):
                obs["assert"].setdefault(k, "not-run")
            await p0.close()
            await p1.close()
            return obs

    # ---- enrage leg
    enr = await handle_trigger_leg(p0, p1, ramp_tag, "enrage", altisaur_id, obs,
                                   take_skip_if_offered=False)
    if enr is None:
        for k in ("A4_enrage_triggered", "A5_zero_option_enrage", "A6_enrage_resolution"):
            obs["assert"].setdefault(k, "not-run")
        obs["notes"].append("enrage target selection never appeared")
        await p0.close()
        await p1.close()
        return obs
    obs["assert"]["A4_enrage_triggered"] = "passed"
    _, _ = enr
    n_bears_before = len(gy_by_name(p0.latest["state"], 1, BEAR))
    s = await settle(p0, p1, {"m": 1}, ramp_tag,
                     lambda s: len(gy_by_name(s, 1, BEAR)) > n_bears_before, 180,
                     "enrage fight resolution")
    a6 = s is not None and len(gy_by_name(s, 1, BEAR)) > n_bears_before \
        and len(altisaur_bf(s)) == 1
    obs["assert"]["A6_enrage_resolution"] = "passed" if a6 else "failed"
    obs["notes"].append(f"enrage fight: bears gy {n_bears_before}->"
                        f"{len(gy_by_name(s, 1, BEAR)) if s else '?'} "
                        f"altisaur_bf={len(altisaur_bf(s)) if s else '?'}")
    if s is not None:
        await export_named(p0, "post")

    await p0.close()
    await p1.close()
    return obs


def state_turn(p0):
    st = p0.latest
    return st["state"].get("turn_number") if st else None


def diag(p0, p1, ramp_tag):
    for c, pid in ((p0, 0), (p1, 1)):
        st = c.latest
        if not st:
            say(f"[{ramp_tag}] DIAG {c.name}: no state yet")
            continue
        s = st["state"]
        acts = top_acts(st)
        say(f"[{ramp_tag}] DIAG {c.name}: rev={c.revision} turn={s.get('turn_number')} "
            f"phase={s.get('phase')} act={s.get('active_player')} "
            f"acts={[a.get('type') for a in acts][:8]} vikind={vi_kind_code(st)!r} "
            f"real_decision={real_decision_pending(st)} my_prio={my_priority(acts)} "
            f"bears_bf={len(bf_by_name(s, 1, BEAR))} altisaur_bf={len(bf_by_name(s, 0, ALTISAUR))}")


async def handle_trigger_leg(p0, p1, ramp_tag, leg, altisaur_id, obs, take_skip_if_offered):
    """Wait for the next fight target selection on P0, record the prompt,
    analyze the skip option, answer it, and return (skip_taken, bear_oid).
    Returns None if no selection appears."""
    t0 = time.time()
    seen_diag = 0
    while time.time() - t0 < 180:
        await asyncio.sleep(0.25)
        await generic_tick(p1, {"m": 1}, f"P1{ramp_tag}", 1)
        await generic_tick(p0, {"m": 1}, ramp_tag, 0)
        await asyncio.sleep(0)
        st = p0.latest
        if not st:
            continue
        opp, rtype, spec_type = target_opportunity(st)
        if opp is None:
            if seen_diag % 40 == 0:
                diag(p0, p1, ramp_tag)
            seen_diag += 1
            continue
        state = st["state"]
        say(f"[{leg}] target selection appeared: iid={opp.get('interactionId')} "
            f"rtype={rtype} spec={spec_type}")
        wire(f"{leg}_prompt", {"iid": opp.get("interactionId"), "rtype": rtype,
                               "response": opp.get("response")})
        skip_offered, info = analyze_skip_option(state, opp)
        obs["events"].append({f"{leg}_skip_offered": skip_offered, "info": info})
        key = f"A2_zero_option_etb" if leg == "etb" else "A5_zero_option_enrage"
        obs["assert"][key] = "passed" if skip_offered else "failed"
        say(f"[{leg}] skip offered: {skip_offered} signals={info['signals']} "
            f"n_candidates={info['n_candidates']}")

        await export_named(p0, f"pre_{leg}")
        save_prompt_detail(f"{leg}_prompt", state, st)
        with open(f"{EVDIR}/{leg}_prompt.json", "w") as f:
            json.dump({"leg": leg, "interaction_id": opp.get("interactionId"),
                       "skip_offered": skip_offered, "analysis": info,
                       "raw_response": opp.get("response")}, f, indent=1, default=str)

        if skip_offered and take_skip_if_offered:
            iid = await submit_skip(p0, opp, rtype, spec_type, f"{leg}-skip")
            if iid is None:
                obs["notes"].append(f"{leg}: skip offered but not submittable; "
                                    "falling back to bear target")
                ch = pick_bear_target(state, opp)
                if ch is None:
                    obs["notes"].append(f"{leg}: no bear candidate available")
                    return None
                await submit_target(p0, opp, rtype, spec_type, ch, leg)
                return (False, _cand_reference(ch))
            # settle until the trigger clears with no fight
            s = await settle(p0, p1, {"m": 1}, ramp_tag,
                             lambda _s: target_opportunity(p0.latest or {}) is None
                             and not real_decision_pending(p0.latest or {}),
                             60, f"{leg} skip settle")
            obs["notes"].append(f"{leg}: skip taken; bears_bf="
                                f"{len(bf_by_name(p0.latest['state'], 1, BEAR))}")
            return (True, None)

        ch = pick_bear_target(state, opp)
        if ch is None:
            obs["notes"].append(f"{leg}: no bear candidate available in selection")
            return None
        await submit_target(p0, opp, rtype, spec_type, ch, leg)
        return (False, _cand_reference(ch))
    say(f"[{leg}] TIMEOUT waiting for target selection")
    return None


async def bolt_own_altisaur(p0, p1, ramp_tag, altisaur_id, obs):
    """Cast Lightning Bolt targeting P0's own Altisaur to trigger enrage."""
    say("[bolt] casting Lightning Bolt at own Apex Altisaur")
    t0 = time.time()
    submitted = False
    bolt_targeted = False
    while time.time() - t0 < 300:
        await asyncio.sleep(0.2)
        await generic_tick(p1, {"m": 1}, f"P1{ramp_tag}", 1)
        if not submitted:
            st = p0.latest
            if not st:
                continue
            state = st["state"]
            acts = merged_actions(st)
            # answer bolt's own target selection if it appears
            opp, rtype, spec_type = target_opportunity(st)
            if opp is not None and not bolt_targeted:
                ch = None
                resp = opp.get("response", {}) or {}
                data = resp.get("data", {}) or {}
                for c in data.get("candidates") or data.get("choices") or []:
                    if str(_cand_reference(c)) == str(altisaur_id):
                        ch = c
                        break
                if ch is not None:
                    await submit_target(p0, opp, rtype, spec_type, ch, "bolt")
                    bolt_targeted = True
                    continue
            if my_main(state, 0):
                if BOLT in [obj_lname(state, o) for o in hand_ids(state, 0)]:
                    a, oid = cast_action_for(acts, state, BOLT)
                    if a is not None:
                        ST["mana_needs"][ramp_tag] = {"R": 1}
                        say(f"[bolt] submitting cast (oid {oid})")
                        await submit_as_is(p0, a)
                        submitted = True
                        continue
            needs = ST["mana_needs"].get(ramp_tag, {})
            if sum(needs.values()) > 0:
                if await pay_mana_vi(p0, st, ramp_tag, needs):
                    continue
            if real_decision_pending(st):
                continue
            if my_priority(acts):
                await pass_priority(p0, st, acts)
        else:
            await generic_tick(p0, {"m": 1}, ramp_tag, 0)
            st = p0.latest
            if st and bolt_targeted:
                # bolt resolved once damage is marked on the altisaur, or
                # once the enrage target selection is already pending
                o = get_obj(st["state"], altisaur_id)
                opp2, _, _ = target_opportunity(st)
                if o.get("damage") not in (None, 0) or opp2 is not None:
                    say(f"[bolt] resolved; altisaur damage={o.get('damage')}")
                    return True
    say("[bolt] TIMEOUT")
    return False

# ---------------------------------------------------------------- verdict / finalize

def compute_verdict(obs):
    a = obs["assert"]
    if a.get("A1_setup_ok") != "passed" or a.get("A3_etb_resolution") == "failed":
        return "blocked"
    etb_skip = a.get("A2_zero_option_etb")
    enr_skip = a.get("A5_zero_option_enrage")
    if etb_skip == "failed" or enr_skip == "failed":
        # Forced fight with no zero-target option: the reported bug.
        return "reproduced"
    if etb_skip == "passed" and enr_skip == "passed":
        return "not-reproduced"
    return "blocked"


def render_png(obs, verdict):
    from PIL import Image, ImageDraw
    W, H = 1100, 720
    img = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 84], fill=(30, 34, 46))
    d.text((24, 18), "phase-rs/phase #7180 — Apex Altisaur 'up to one' fight skip",
           fill=(235, 235, 240))
    d.text((24, 48), "v0.102.0 (e17f6fd) · protocol 106 · 2026-10-05 · scenario_7180_01020.py",
           fill=(150, 160, 180))
    vc = {"reproduced": (220, 90, 90), "not-reproduced": (110, 200, 130),
          "blocked": (220, 180, 90)}[verdict]
    d.text((880, 30), verdict.upper(), fill=vc)
    y = 110
    d.text((24, y), "Assertions (reported outcome: a zero-target/skip option "
                    "for 'up to one' fight triggers):", fill=(200, 205, 215))
    y += 30
    labels = {
        "A1_setup_ok": "A1 game setup (2 clients, Altisaur castable)",
        "A2_zero_option_etb": "A2 ETB trigger offers zero-target/skip",
        "A3_etb_resolution": "A3 ETB fight resolves (bear -> gy, Altisaur on bf)",
        "A4_enrage_triggered": "A4 enrage target selection offered after damage",
        "A5_zero_option_enrage": "A5 enrage trigger offers zero-target/skip",
        "A6_enrage_resolution": "A6 enrage fight resolves (bear -> gy, Altisaur alive)",
    }
    for k, lab in labels.items():
        v = obs["assert"].get(k, "not-run")
        col = {"passed": (110, 200, 130), "failed": (220, 90, 90),
               "not-run": (150, 150, 160)}.get(v, (150, 150, 160))
        d.text((40, y), f"{lab}:", fill=(210, 215, 225))
        d.text((760, y), v, fill=col)
        y += 28
    y += 8
    d.text((24, y), "Trigger prompt evidence:", fill=(200, 205, 215))
    y += 28
    for ev in obs.get("events", []):
        if "etb_skip_offered" in ev:
            d.text((40, y), f"ETB skip offered: {ev['etb_skip_offered']} "
                            f"signals={ev['info'].get('signals')}",
                   fill=(170, 175, 190))
            y += 24
        if "enrage_skip_offered" in ev:
            d.text((40, y), f"enrage skip offered: {ev['enrage_skip_offered']} "
                            f"signals={ev['info'].get('signals')}",
                   fill=(170, 175, 190))
            y += 24
    y += 4
    d.text((24, y), "Notes:", fill=(200, 205, 215))
    y += 26
    for n in obs.get("notes", [])[:6]:
        d.text((40, y), str(n)[:130], fill=(150, 155, 170))
        y += 22
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7180/20261005-7180",
           fill=(120, 125, 140))
    img.save(f"{EVDIR}/summary.png")
    say("rendered summary.png")


def write_manifest():
    import hashlib as hl
    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(os.path.join(EVDIR, f))
                   and f != "manifest.sha256")
    lines = []
    for fn in files:
        h = hl.sha256(open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 for {len(files)} files")
    return files


async def main():
    await verify_server_hello()
    data_ok = check_data_level()
    obs = await run_game()
    verdict = compute_verdict(obs)
    say(f"VERDICT: {verdict} assertions={json.dumps(obs['assert'])}")

    scenario_sha = hashlib.sha256(
        open(__file__, "rb").read()).hexdigest()
    run_json = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": SERVER_IDENTITY,
        "decks": {"P0": [list(x) for x in P0_DECK],
                  "P1": [list(x) for x in P1_DECK]},
        "scenario_file": "scenario_7180_01020.py",
        "scenario_sha256": scenario_sha,
        "data_level_ok": data_ok,
        "assertions": obs["assert"],
        "notes": obs["notes"],
        "events": obs["events"],
        "verdict": verdict,
        "scope": ("Apex Altisaur ETB + enrage fight triggers: zero-target/skip "
                  "option presence; forced-fight resolution; native engine, "
                  "two human-client seats"),
        "limitations": ["Browser/UI not exercised", "AI seats not used"],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_json, f, indent=1, default=str)
    # copy the exact driver revision into the evidence dir
    with open(__file__, "rb") as src, \
            open(f"{EVDIR}/scenario_7180_01020.py", "wb") as dst:
        dst.write(src.read())
    render_png(obs, verdict)
    # Close logs before hashing: the run log grows with every say(), so the
    # manifest must be computed after the final write.
    WIRE.close()
    RUNLOG.close()
    files = write_manifest()

    # validate: every JSON parses, hashes match, PNG readable
    problems = []
    for fn in files:
        p = os.path.join(EVDIR, fn)
        if fn.endswith(".json"):
            try:
                json.load(open(p))
            except Exception as e:
                problems.append(f"{fn}: {e}")
    try:
        from PIL import Image as I
        I.open(f"{EVDIR}/summary.png").verify()
    except Exception as e:
        problems.append(f"summary.png: {e}")
    import subprocess as sp
    r = sp.run(["sha256sum", "-c", "manifest.sha256"], cwd=EVDIR,
               capture_output=True, text=True)
    if r.returncode != 0:
        problems.append(f"manifest mismatch: {r.stdout[-500:]}")
    ok = not problems
    print(f"evidence validation: {'OK' if ok else 'FAILED'} "
          f"({len(files)} files)")
    for pr in problems:
        print(f"VALIDATE FAIL {pr}")
    print(json.dumps({"verdict": verdict, "assertions": obs["assert"],
                      "valid": ok}))


if __name__ == "__main__":
    asyncio.run(main())
