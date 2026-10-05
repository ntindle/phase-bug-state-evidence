#!/usr/bin/env python3
"""Issue #6764 revalidation on v0.102.0 (protocol 106): Knowledge Pool.

Oracle (verified from pinned card data):
  "Imprint - When this artifact enters, each player exiles the top three
   cards of their library. Whenever a player casts a spell from their hand,
   that player exiles it. If the player does, they may cast a spell from
   among other cards exiled with this artifact without paying its mana cost."

Reported: the free-cast card choice lets you select cards it shouldn't
(wrong scope) and the chosen card still doesn't get cast.

Driver plan (protocol 106, v0.102.0, native engine, two human-client seats):
  P0: 4x Knowledge Pool + 24x Shock + 32x Mountain (60)
  P1: 60x Island (draw-go, never blocks)
  P0 ramps to 6 mana and casts Knowledge Pool ({6}). Imprint exiles the top
  3 of each library. P0 casts Shock #1 from hand targeting P1 -> Pool
  trigger exiles it -> may-choice DECLINED (control leg). P0 casts Shock #2
  -> trigger -> may-choice ACCEPTED -> the card choice is recorded and
  asserted -> if a legal exiled Shock is offered it is chosen and cast free
  at P1.

Assertions:
  A1 setup_ok            Pool on P0 battlefield; imprint exiled exactly 6
                         (3 per library); each library -3 vs pre-cast export
  A2a decline_control    leg-1 may declined: no card choice offered, Shock #1
                         ends in Exile, game proceeds
  A2b accept_may         leg-2 may-choice offered and accepted
  A3 choice_scope        card choice offers ONLY other cards exiled with THIS
                         Pool (zone Exile); the triggering Shock #2 is not
                         offered; every exiled nonland spell other than the
                         trigger is offered
  A4 free_cast_executes  the chosen exiled Shock is cast free: resolves for
                         2 damage to P1, ends in P0's graveyard, no mana paid
  A5 cleanup             stack empty, no pending decisions, game continues

Verdict = reproduced iff A2b passed and (A3 failed or A4 failed);
          not-reproduced iff A2b, A3 and A4 all passed;
          blocked otherwise.

Protocol-106 driver notes (v0.102.0): ported harness from the verified
scenario_6760_01020.py (same run series):
  - waiting_for is gone; priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action; land plays via legacy
    PlayLand + vi playLand opportunity (answered before the decision gate).
  - CastSpell via legacy action; mana via PayManaAbilityMana/PayMana legacy
    actions + vi tapLandForMana menus driven by mana_needs.
  - Shock targets via the advertised vi target opportunity (schema
    select/sequence), seat-1 (opponent) candidate preferred.
  - may-choice via exactChoices decideOptionalEffect accept true/false.
  - The card choice is found by scanning vi opportunities for candidates
    that reference card objects (not the may-choice, not priority menus,
    not player-target prompts); full opportunity JSON is wire-logged.
  - Export-only checkpoints fall through to the priority pass; never return
    after an export while holding priority.
  - deck schema {"main_deck": [...]} via client.py deck() helper; HELLO
    advertises 106 (exact match enforced).
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
log = logging.getLogger("scenario6764_106")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261005-6764"
ISSUE = 6764
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
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-05: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "fresh isolated server on 127.0.0.1:9374 started by run "
               "20261005-1611 with run dir runs/run-20261005-1611"),
}

for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h

POOL = "Knowledge Pool"
SHOCK = "Shock"
MOUNTAIN = "Mountain"
ISLAND = "Island"


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
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


def lib_size(state, pid):
    return len(player_of(state, pid).get("library") or [])


def life(state, pid):
    return player_of(state, pid).get("life")


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def exile_oids(state):
    return {oid: o for oid, o in (state.get("objects") or {}).items()
            if str(o.get("zone") or "").lower() == "exile"}


def is_land(o):
    if not o:
        return False
    ct = (o.get("card_types") or {}).get("core_types") or []
    if "Land" in ct:
        return True
    return "Land" in str(o.get("type_line") or "") or \
        "land" in str(o.get("card_type") or "").lower()


def untapped_mountains(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if not get_obj(state, o).get("tapped")
               and obj_lname(state, o) == MOUNTAIN.lower())


def tapped_mountains(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if get_obj(state, o).get("tapped")
               and obj_lname(state, o) == MOUNTAIN.lower())


def find_hand(state, pid, lname):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == lname:
            return o
    return None


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
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
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
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability/land-play menu) in its viewer_interaction."""
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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "object":
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("reference") is not None:
                return str(d.get("reference")), d
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return str(d.get("reference")), d
    return None, None


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return False
    MULLS.add(key)
    state = st["state"]
    hn = [obj_lname(state, o) for o in hand_ids(state, pid)]
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


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
        ref, _d = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in (POOL.lower(), SHOCK.lower()):
            return (2, str(ref))
        if ref is not None and is_land(get_obj(state, ref)):
            return (0, str(ref))
        return (1, str(ref))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, _cand_reference(x)[0]) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid, "picks": [x.get("id") for x in picks]})
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
            if any(str(_cand_reference(ch)[0]) in handset for ch in cands):
                found = True
                break
        if not found:
            return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False

    def rank(o):
        nm = obj_lname(state, o)
        if nm in (POOL.lower(), SHOCK.lower()):
            return (3, nm)
        if is_land(get_obj(state, o)):
            return (1, nm)
        return (0, nm)

    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = (rdata.get("spec") or {})
        stype = spec.get("type") or "select"
        picks = [ch["id"] for ch in
                 sorted(cands, key=lambda ch: rank(_cand_reference(ch)[0]))[:max(1, n)]]
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discarding to hand size via vi ({stype})")
        wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
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


async def answer_vi_play_land(c, state, pid, acts, tag, land_name):
    """Protocol 106 advertises land plays as a vi exactChoices opportunity
    carrying a playLand action code. Answer it before the decision gate so
    the driver never stalls holding priority with only a land-play menu."""
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    for opp in vi_ops(state and c.latest):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref, _d = _cand_reference(ch)
            if ref is None or obj_lname(state, ref) != land_name.lower():
                continue
            if str(ref) not in hand_ids(state, pid):
                continue
            LAND_PLAYED_TURN[(tag,)] = turn
            say(f"[{tag}] playing land via vi playLand: {land_name}")
            wire("play_land_vi", {"who": tag, "ref": ref})
            await answer_vi(c, opp, ch, tag)
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
                    say(f"[{tag}] playing land {obj_lname(state, o)} (legacy)")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


async def my_land_or_vi_land(c, state, pid, acts, tag, land_name):
    if await play_a_land(c, state, pid, acts, tag):
        return True
    return await answer_vi_play_land(c, state, pid, acts, tag, land_name)


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


# ------------------------------------------------------------- target selection (106)

def candidate_seat(ch):
    """Player seat for a target/choice candidate (strict: only player /
    target / candidate surfaces with a seat-ish key)."""
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def target_opportunity(st):
    """First viewer_interaction opportunity that looks like a target
    selection: schema select/sequence with candidates, or exactChoices whose
    choices carry candidate/target codes (passPriority menus excluded)."""
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


def pick_target_candidate(opp, preferred_seat):
    """Prefer the candidate at preferred_seat (a player)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    best, best_score = None, -1
    for ch in cands:
        codes = set(surf_codes(ch))
        score = 0
        if candidate_seat(ch) == preferred_seat:
            score += 2
        if codes & {"candidate", "target"}:
            score += 1
        if score > best_score:
            best, best_score = ch, score
    if best is None and len(cands) == 1:
        best = cands[0]
    return best


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']} "
        f"({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid, cid


# ------------------------------------------------------------- may-choice + card choice

def find_optional_choice_opp(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


async def answer_may(c, tag, ctx, accept):
    st = c.latest
    opp = find_optional_choice_opp(st)
    if opp is None:
        return False
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "accept" in txt or "yes" in txt or "put" in txt:
                is_accept = True
            elif "decline" in txt or "no " in txt:
                is_accept = False
        if is_accept == accept:
            wire("may_choice_opportunity",
                 {"iid": opp.get("interactionId"), "accept": accept,
                  "opp": opp})
            say(f"[{tag}] P0 {'ACCEPTS' if accept else 'DECLINES'} Knowledge Pool may-choice")
            await answer_vi(c, opp, ch, tag)
            return True
    say(f"[{tag}] may-choice opportunity found but no {'accept' if accept else 'decline'} choice identified")
    return False


def classify_candidate(state, ch):
    """Return (ref, name, zone, is_card) for a choice candidate, using the
    object surface when present and the authoritative state as fallback."""
    ref, sdata = _cand_reference(ch)
    name = (sdata or {}).get("name")
    zone = (sdata or {}).get("zone")
    if ref is not None:
        o = get_obj(state, ref)
        if o:
            name = name or o.get("base_name") or o.get("name")
            zone = zone or o.get("zone")
            return ref, str(name or "?"), str(zone or "?").lower(), True
        # reference to something that is not a card object (e.g. a player)
        return ref, str(name or choice_text(ch) or "?"), str(zone or "?").lower(), False
    # no reference: fall back to matching the choice text against objects
    txt = choice_text(ch).lower()
    if txt:
        for oid, o in (state.get("objects") or {}).items():
            if str(o.get("base_name") or o.get("name") or "").lower() == txt:
                return oid, str(o.get("base_name") or o.get("name")), \
                    str(o.get("zone") or "?").lower(), True
    return None, choice_text(ch) or "?", "?", False


def find_card_choice(st):
    """Scan viewer_interaction for the Knowledge Pool free-cast card choice:
    an opportunity (not the may-choice, not a priority/mana menu) whose
    candidates reference card objects. Returns (opp, kind, classified) where
    kind is 'cards' (card candidates outside the hand -- the Pool choice or
    a mis-scoped variant of it), 'cards-handonly' (only hand-zone cards,
    i.e. a mulligan/discard prompt, not the Pool choice), or None."""
    state = st["state"]
    found = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            continue
        if codes and codes <= NON_DECISION_CODES:
            continue
        classified = [classify_candidate(state, ch) for ch in items]
        n_cards = sum(1 for _r, _n, _z, is_card in classified if is_card)
        if n_cards == 0:
            continue  # player-target prompt, not the card choice
        zones = {z for _r, _n, z, _b in classified}
        found.append((opp, zones, list(zip(items, classified))))
    if not found:
        return None, None, None
    for opp, zones, cc in found:
        if not zones <= {"hand"}:
            return opp, "cards", cc
    opp, zones, cc = found[0]
    return opp, "cards-handonly", cc

# ---------------------------------------------------------------- game ticks

async def p1_tick(c, tag):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_bottom(c, acts, st, 1, tag):
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
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if await pay_tick(c, acts):
        return
    if my_main(state, 1):
        if await my_land_or_vi_land(c, state, 1, acts, tag, ISLAND):
            return
    if await answer_vi_play_land(c, state, 1, acts, tag, ISLAND):
        return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, tag, ctx):
    """P0 driver tick: ramp, cast Pool, cast Shocks (legs 1-2), answer
    targets and may-choices, otherwise keep priority moving."""
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
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return True
    # During the free-cast window the engine must NOT ask for mana; do not
    # auto-answer any payment prompt so a mana demand surfaces as a stall.
    if not ctx.get("free_cast_window"):
        if await pay_tick(c, acts):
            return True
    needs = ST["mana_needs"].get(tag, {})
    if not ctx.get("free_cast_window") and sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    # Shock target selection (hand casts and the free cast)
    if ctx.get("shock_target_pending"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            ch = pick_target_candidate(opp, 1)
            if ch is not None and candidate_seat(ch) == 1:
                ctx["shock_target_pending"] = False
                say(f"[{tag}] submitting Shock target: P1 (seat 1)")
                await submit_target(c, opp, rtype, stype, ch, tag)
                return True
            wire("target_no_p1", {"opp": opp})
            say(f"[{tag}] target opportunity without seat-1 candidate; waiting")
            return True
    # may-choice: decline on leg 1, accept on leg 2
    if ctx.get("leg") == 1 and not ctx.get("may_done_leg1"):
        if find_optional_choice_opp(st):
            if await answer_may(c, tag, ctx, accept=False):
                ctx["may_done_leg1"] = True
                ctx["may1_at"] = time.time()
            return True
    if ctx.get("leg") == 2 and not ctx.get("may_done_leg2"):
        if find_optional_choice_opp(st):
            if await answer_may(c, tag, ctx, accept=True):
                ctx["may_done_leg2"] = True
                ctx["may2_at"] = time.time()
            return True

    # ---- priority gate, then yield before leg evaluation (race fix)
    await asyncio.sleep(0)
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)

    # clear stale mana needs once the Pool spell has resolved
    if ctx.get("pool_cast") and not ctx.get("pool_needs_cleared"):
        if bf_by_name(state, 0, POOL.lower()):
            ctx["pool_needs_cleared"] = True
            ST["mana_needs"][tag] = {}
            say(f"[{tag}] Knowledge Pool on battlefield; mana needs cleared")

    if my_main(state, 0):
        if await my_land_or_vi_land(c, state, 0, acts, tag, MOUNTAIN):
            return True
        # cast Knowledge Pool ({6} generic)
        if (not ctx.get("pool_cast")
                and find_hand(state, 0, POOL.lower()) is not None
                and state.get("phase") == "PreCombatMain"
                and untapped_mountains(state, 0) >= 6):
            a, oid = cast_action_for(acts, state, POOL.lower())
            if a is not None:
                pre_s = await c.export_state()
                with open(f"{EVDIR}/pre_pool_cast.json", "w") as f:
                    f.write(pre_s)
                ctx["pre_pool_cast"] = json.loads(pre_s)["state"]
                ST["mana_needs"][tag] = {"generic": 6}
                ctx["pool_cast"] = True
                say(f"[{tag}] casting Knowledge Pool (oid {oid})")
                wire("cast_pool", {"oid": oid})
                await submit_as_is(c, a)
                return True
        # cast Shock, leg 1 (decline control)
        if (ctx.get("imprint_done") and ctx.get("leg") == 1
                and not ctx.get("shock_cast_leg1")
                and state.get("phase") == "PreCombatMain"):
            soid = find_hand(state, 0, SHOCK.lower())
            if soid is not None and untapped_mountains(state, 0) >= 1:
                a, oid = cast_action_for(acts, state, SHOCK.lower())
                if a is not None:
                    ST["mana_needs"][tag] = {"R": 1}
                    ctx["shock_cast_leg1"] = True
                    ctx["shock1_oid"] = str(oid)
                    ctx["shock_target_pending"] = True
                    say(f"[{tag}] leg 1: casting Shock #1 (oid {oid})")
                    wire("cast_shock_leg1", {"oid": oid})
                    await submit_as_is(c, a)
                    return True
        # cast Shock, leg 2 (accept leg)
        if (ctx.get("leg1_done") and ctx.get("leg") == 2
                and not ctx.get("shock_cast_leg2")
                and state.get("phase") == "PreCombatMain"):
            soid = find_hand(state, 0, SHOCK.lower())
            if soid is not None and untapped_mountains(state, 0) >= 1:
                a, oid = cast_action_for(acts, state, SHOCK.lower())
                if a is not None:
                    ST["mana_needs"][tag] = {"R": 1}
                    ctx["shock_cast_leg2"] = True
                    ctx["shock2_oid"] = str(oid)
                    ctx["shock_target_pending"] = True
                    say(f"[{tag}] leg 2: casting Shock #2 (oid {oid})")
                    wire("cast_shock_leg2", {"oid": oid})
                    await submit_as_is(c, a)
                    return True
    if await answer_vi_play_land(c, state, 0, acts, tag, MOUNTAIN):
        return True
    if real_decision_pending(st):
        return True
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision
        return True
    return False


async def drive(p0, p1, tag, ctx, timeout_s, done_fn, diag_every=30):
    """Tick both clients until done_fn() is truthy or the timeout fires.
    Returns True iff done_fn() became truthy."""
    t0 = time.time()
    last_diag = 0.0
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.15)
        for c, is_p0 in ((p0, True), (p1, False)):
            st = c.latest
            if not st:
                continue
            try:
                if is_p0:
                    await p0_tick(c, tag, ctx)
                else:
                    await p1_tick(c, f"P1{tag}")
            except Exception as e:
                say(f"[{tag}] tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        for c in (p0, p1):
            while True:
                try:
                    t, data = c.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t in ("ActionRejected", "Error"):
                    rec = json.dumps(data, default=str)[:400]
                    ctx["rejections"].append({"who": c.name, "type": t,
                                             "at": time.time(), "data": rec})
                    wire("rejection", {"who": c.name, "type": t, "data": rec})
        try:
            if done_fn():
                return True
        except Exception as e:
            say(f"[{tag}] done_fn error: {e}")
        if time.time() - last_diag > diag_every:
            last_diag = time.time()
            st = p0.latest
            if st:
                s = st["state"]
                say(f"[{tag}] DIAG rev={p0.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} prio={my_priority(top_acts(st))} "
                    f"vikind={vi_kind_code(st)!r} "
                    f"real_decision={real_decision_pending(st)} "
                    f"pool_cast={ctx.get('pool_cast')} "
                    f"imprint_done={ctx.get('imprint_done')} leg={ctx.get('leg')}")
    return False


# ---------------------------------------------------------------- attempt runner

def new_ctx(attempt):
    return {
        "attempt": attempt,
        "assert": {},
        "notes": [],
        "rejections": [],
        "leg": 1,
    }


def export_now(state_env, name):
    with open(f"{EVDIR}/{name}", "w") as f:
        json.dump(state_env, f)
    return state_env["state"]


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    ST["mana_needs"] = {}
    ctx = new_ctx(attempt)
    tag = f"6764-a{attempt}"
    for k in ("A1_setup_ok", "A2a_decline_control", "A2b_accept_may",
              "A3_choice_scope", "A4_free_cast_executes", "A5_cleanup"):
        ctx["assert"][k] = "not-run"

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck((POOL, 4), (SHOCK, 24), (MOUNTAIN, 32)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck((ISLAND, 60)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    try:
        # ---- phase 1: ramp, cast Knowledge Pool, imprint resolves
        say(f"[{tag}] phase 1: ramp to Knowledge Pool")
        ok = await drive(p0, p1, tag, ctx, 1500,
                         lambda: bool(ctx.get("pool_cast"))
                         and p0.latest is not None
                         and bool(bf_by_name(p0.latest["state"], 0, POOL.lower()))
                         and len(exile_oids(p0.latest["state"])) >= 6)
        if not ok:
            ctx["notes"].append("phase 1 timed out: Pool never reached the "
                                "battlefield with 6 exiled cards")
            return ctx
        post_s = await p0.export_state()
        with open(f"{EVDIR}/post_imprint.json", "w") as f:
            f.write(post_s)
        post = json.loads(post_s)["state"]
        pre = ctx.get("pre_pool_cast") or {}
        ex = exile_oids(post)
        d0 = lib_size(pre, 0) - lib_size(post, 0) if pre else None
        d1 = lib_size(pre, 1) - lib_size(post, 1) if pre else None
        a1 = (bool(bf_by_name(post, 0, POOL.lower()))
              and len(ex) == 6 and d0 == 3 and d1 == 3)
        ctx["assert"]["A1_setup_ok"] = "passed" if a1 else "failed"
        say(f"[{tag}] A1: pool_on_bf={bool(bf_by_name(post, 0, POOL.lower()))} "
            f"exiled={len(ex)} lib_delta=({d0},{d1}) -> {ctx['assert']['A1_setup_ok']}")
        wire("imprint_result",
             {"exiled": sorted((o.get("base_name") or o.get("name"), str(k))
                               for k, o in ex.items()),
              "lib_delta": [d0, d1]})
        if not a1:
            ctx["notes"].append(
                f"A1 detail: exiled={len(ex)} (expect 6), lib deltas=({d0},{d1}) "
                "(expect (3,3))")
            return ctx
        ctx["imprint_done"] = True
        ctx["imprint_exile_oids"] = set(str(k) for k in ex)

        # ---- phase 2: leg 1 -- Shock #1, DECLINE the may (control)
        say(f"[{tag}] phase 2: leg 1 (decline)")
        ok = await drive(p0, p1, tag, ctx, 900,
                         lambda: bool(ctx.get("may_done_leg1")))
        if not ok:
            ctx["notes"].append("leg-1 may-choice never appeared within 900s")
            return ctx
        # decline verification window: keep driving 25s, watch for any card
        # choice (there must be none), then check Shock #1 is exiled
        say(f"[{tag}] leg 1 declined; 25s no-choice verification window")
        t_end = time.time() + 25
        saw_choice = None
        while time.time() < t_end:
            await asyncio.sleep(0.5)
            await p0_tick(p0, tag, ctx)
            await p1_tick(p1, f"P1{tag}")
            if p0.latest:
                opp, kind, _cc = find_card_choice(p0.latest)
                if opp is not None and kind == "cards" and saw_choice is None:
                    saw_choice = opp
                    wire("leg1_unexpected_card_choice", {"opp": opp})
                    say(f"[{tag}] UNEXPECTED: card choice offered after decline")
        dec_s = await p0.export_state()
        with open(f"{EVDIR}/post_decline.json", "w") as f:
            f.write(dec_s)
        dec = json.loads(dec_s)["state"]
        s1 = get_obj(dec, ctx.get("shock1_oid"))
        s1_exiled = str(s1.get("zone") or "").lower() == "exile"
        stack_empty = not (dec.get("stack") or [])
        a2a = (saw_choice is None and s1_exiled and stack_empty)
        ctx["assert"]["A2a_decline_control"] = "passed" if a2a else "failed"
        say(f"[{tag}] A2a: no_choice_after_decline={saw_choice is None} "
            f"shock1_exiled={s1_exiled} stack_empty={stack_empty} "
            f"-> {ctx['assert']['A2a_decline_control']}")
        if not a2a:
            ctx["notes"].append(
                f"A2a detail: saw_choice={saw_choice is not None}, "
                f"shock1 zone={s1.get('zone')}, stack_len={len(dec.get('stack') or [])}")
        ctx["leg1_done"] = True
        ctx["leg"] = 2

        # ---- phase 3: leg 2 -- Shock #2, ACCEPT the may, test the choice
        say(f"[{tag}] phase 3: leg 2 (accept)")
        ok = await drive(p0, p1, tag, ctx, 900,
                         lambda: bool(ctx.get("may_done_leg2")))
        if not ok:
            ctx["notes"].append("leg-2 may-choice never appeared within 900s")
            return ctx
        ctx["assert"]["A2b_accept_may"] = "passed"
        say(f"[{tag}] A2b: leg-2 may-choice offered and accepted -> passed")

        # wait for the free-cast card choice
        say(f"[{tag}] waiting for the free-cast card choice")
        choice_opp = None

        def _choice_seen():
            if p0.latest is None:
                return False
            opp, kind, _cc = find_card_choice(p0.latest)
            return opp is not None and kind == "cards"

        ok = await drive(p0, p1, tag, ctx, 180, _choice_seen)
        if not ok:
            ctx["notes"].append("no free-cast card choice appeared within 180s "
                                "of accepting the may")
            ctx["assert"]["A3_choice_scope"] = "failed"
            return ctx
        opp, kind, classified = find_card_choice(p0.latest)
        choice_opp = opp
        wire("card_choice_opportunity_full", {"opp": opp})
        pre_s = await p0.export_state()
        with open(f"{EVDIR}/pre_choice.json", "w") as f:
            f.write(pre_s)
        pre = json.loads(pre_s)["state"]
        ctx["pre_choice_turn"] = pre.get("turn_number")
        ctx["pre_choice_tapped"] = tapped_mountains(pre, 0)

        # classify the offered candidates against the authoritative export
        ex = exile_oids(pre)
        trigger_oid = str(ctx.get("shock2_oid"))
        offered = []
        for ch, (ref, name, zone, is_card) in classified:
            offered.append({"choice_id": ch.get("id"), "ref": ref,
                            "name": name, "zone": zone, "is_card": is_card})
            say(f"[{tag}]   candidate {ch.get('id')}: {name} zone={zone} "
                f"ref={ref} is_card={is_card}")
        wire("card_choice_classified", {"candidates": offered,
                                        "exile_count": len(ex),
                                        "trigger_oid": trigger_oid})
        ctx["offered"] = offered
        legal = {oid for oid, o in ex.items()
                 if str(oid) != trigger_oid and not is_land(o)}
        offered_refs = {str(c["ref"]) for c in offered if c["ref"]}
        a3a = all(c["zone"] == "exile" for c in offered) and len(offered) > 0
        a3b = trigger_oid not in offered_refs
        missing = sorted(oid for oid in legal if str(oid) not in offered_refs)
        a3c = not missing
        ctx["assert"]["A3_choice_scope"] = (
            "passed" if (a3a and a3b and a3c) else "failed")
        say(f"[{tag}] A3: all_exiled={a3a} trigger_excluded={a3b} "
            f"legal_all_offered={a3c} (legal={len(legal)}, offered={len(offered)}) "
            f"-> {ctx['assert']['A3_choice_scope']}")
        if not (a3a and a3b and a3c):
            ctx["notes"].append(
                "A3 detail: offered=" +
                json.dumps([(c["name"], c["zone"]) for c in offered]) +
                f"; legal_missing={[ (ex[m].get('base_name'), m) for m in missing ]}; "
                f"trigger_oid={trigger_oid} offered={trigger_oid in offered_refs}")
        # pick the free cast: prefer an exiled Shock, else any legal spell
        pick = next((c for c in offered
                     if c["ref"] and str(c["ref"]) in legal
                     and c["name"].lower() == SHOCK.lower()), None)
        if pick is None:
            pick = next((c for c in offered
                         if c["ref"] and str(c["ref"]) in legal), None)
        if pick is None:
            ctx["notes"].append("A3 failed with no legal spell offered: the "
                                "free cast cannot be exercised (A4 not-run)")
            ctx["assert"]["A4_free_cast_executes"] = "not-run"
            return ctx
        ch = next(ch for ch, (_r, _n, _z, _b) in classified
                  if ch.get("id") == pick["choice_id"])
        resp = opp.get("response", {}) or {}
        if resp.get("type") == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": opp.get("interactionId"),
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick["choice_id"]]}}}
        else:
            sub = {"interactionId": opp.get("interactionId"),
                   "response": {"type": "choose",
                                "data": {"choiceId": pick["choice_id"]}}}
        say(f"[{tag}] choosing free cast: {pick['name']} (ref {pick['ref']})")
        wire("free_cast_choice", {"submission": sub, "pick": pick})
        ctx["free_pick_oid"] = str(pick["ref"])
        ctx["free_pick_name"] = pick["name"]
        ctx["free_cast_window"] = True  # engine must not ask for mana
        ctx["shock_target_pending"] = True  # the free Shock needs a target
        await interact_as(p0, sub, tag)
        ctx["choice_at"] = time.time()

        # wait for the free cast to resolve: P1 takes 2, spell in graveyard
        life_before = life(pre, 1)

        def _free_resolved():
            if p0.latest is None:
                return False
            s = p0.latest["state"]
            o = get_obj(s, ctx["free_pick_oid"])
            return (life(s, 1) == life_before - 2
                    and str(o.get("zone") or "").lower() == "graveyard")

        ok = await drive(p0, p1, tag, ctx, 240, _free_resolved)
        ctx["free_cast_window"] = False
        post_s = await p0.export_state()
        with open(f"{EVDIR}/post_free_cast.json", "w") as f:
            f.write(post_s)
        post = json.loads(post_s)["state"]
        o = get_obj(post, ctx["free_pick_oid"])
        dmg_ok = life(post, 1) == life_before - 2
        gy_ok = str(o.get("zone") or "").lower() == "graveyard"
        same_turn = post.get("turn_number") == ctx.get("pre_choice_turn")
        mana_ok = (tapped_mountains(post, 0) == ctx.get("pre_choice_tapped")
                   if same_turn else None)
        a4_core = ok and dmg_ok and gy_ok
        if not a4_core:
            a4_verdict = "failed"
        elif mana_ok is False:
            a4_verdict = "failed"
        else:
            a4_verdict = "passed"
        ctx["assert"]["A4_free_cast_executes"] = a4_verdict
        if mana_ok is None:
            ctx["notes"].append("A4 mana sub-check not-run: turn advanced "
                                "between pre_choice and post_free_cast exports")
        elif mana_ok is False:
            ctx["notes"].append(
                f"A4 mana sub-check FAILED: tapped Mountains "
                f"{ctx.get('pre_choice_tapped')} -> {tapped_mountains(post, 0)} "
                "(engine may have charged mana for the 'free' cast)")
        say(f"[{tag}] A4: resolved={ok} dmg_2={dmg_ok} in_gy={gy_ok} "
            f"no_mana_paid={mana_ok} -> {ctx['assert']['A4_free_cast_executes']}")
        wire("free_cast_result",
             {"resolved": ok, "dmg_ok": dmg_ok, "gy_ok": gy_ok,
              "mana_ok": mana_ok, "p1_life": life(post, 1),
              "pick_zone": o.get("zone")})

        # ---- phase 4: cleanup -- the game must continue cleanly
        say(f"[{tag}] phase 4: cleanup")
        await drive(p0, p1, tag, ctx, 30, lambda: False)
        fin_s = await p0.export_state()
        with open(f"{EVDIR}/final.json", "w") as f:
            f.write(fin_s)
        fin = json.loads(fin_s)["state"]
        a5 = (not (fin.get("stack") or [])
              and not real_decision_pending(p0.latest))
        ctx["assert"]["A5_cleanup"] = "passed" if a5 else "failed"
        say(f"[{tag}] A5: stack_empty={not (fin.get('stack') or [])} "
            f"no_pending={not real_decision_pending(p0.latest)} "
            f"-> {ctx['assert']['A5_cleanup']}")
        ctx["finished"] = True
    finally:
        await p0.close()
        await p1.close()
    return ctx

# ---------------------------------------------------------------- main

async def main():
    await verify_server_hello()
    t0 = time.time()
    final = None
    for attempt in (1, 2, 3):
        say(f"===== attempt {attempt} =====")
        ctx = await run_attempt(attempt)
        say(f"attempt {attempt} assertions: {json.dumps(ctx['assert'])}")
        final = ctx
        # stop retrying once the accept leg was actually exercised
        if ctx["assert"].get("A2b_accept_may") == "passed":
            break
        say("attempt did not reach the leg-2 may-choice; retrying with a fresh game")
    dur = time.time() - t0
    ass = final["assert"]
    notes = final["notes"] + [
        "protocol-106 driver (v0.102.0): Knowledge Pool cast via CastSpell "
        "legacy action with {generic:6} driven through PayManaAbilityMana / "
        "PayMana legacy actions + vi tapLandForMana menus; Shock casts via "
        "CastSpell with {R:1}; Shock targets via the advertised vi target "
        "opportunity (schema select/sequence), seat-1 candidate preferred; "
        "may-choice via exactChoices decideOptionalEffect accept true/false; "
        "the free-cast card choice found by scanning vi opportunities for "
        "card-object candidates (hand-zone-only prompts excluded); free "
        "cast submitted as {type:<spec.type>, data:{choiceIds:[...]}}; a "
        "free_cast_window flag suppresses all auto mana payment between the "
        "card choice and the free cast's resolution so a mana demand would "
        "surface as a stall rather than being silently paid.",
        "P0 deck 4x Knowledge Pool + 24x Shock + 32x Mountain; P1 60x Island "
        "draw-go, never attacks or blocks (engine accepts >4-of for custom "
        "games). Leg 1 (decline) exiles Shock #1, making it part of the "
        "legal free-cast set on leg 2 alongside the 6 imprint cards.",
    ]
    a2b = ass.get("A2b_accept_may")
    a3 = ass.get("A3_choice_scope")
    a4 = ass.get("A4_free_cast_executes")
    if a2b == "passed" and (a3 == "failed" or a4 == "failed"):
        verdict = "reproduced"
    elif a2b == "passed" and a3 == "passed" and a4 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server": {
            "server_version": "0.102.0",
            "build_commit": "e17f6fd",
            "protocol_version": 106,
            "mode": "Full",
            **{k: v for k, v in SERVER_IDENTITY.items()
                if k in ("server_binary_sha256", "card_data_sha256",
                         "draft_pools_sha256", "signature_verified")},
            "observed_at": "2026-10-05",
            "source": SERVER_IDENTITY["source"],
        },
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6764_01020.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [[POOL, 4], [SHOCK, 24], [MOUNTAIN, 32]],
            "P1": [[ISLAND, 60]],
        },
        "assertions": ass,
        "notes": notes,
        "rejections": final["rejections"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "Cards exiled by a *different* Knowledge Pool or another effect "
            "were not present; the 'exiled with THIS artifact' boundary was "
            "tested against the single Pool's imprint set only.",
            "Not tested on the original 2026-07-29 build; verdict is scoped to "
            "v0.102.0, not a fix claim.",
            "Dense playsets (24x) are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
        ],
        "setup_line": "P0: 4x Knowledge Pool + 24x Shock + 32x Mountain; P1: 60x Island (draw-go)",
        "contract_line": "Pool imprints 3+3; hand-cast Shock exiled by trigger; "
                         "may-cast choice must offer only other cards exiled with "
                         "this Pool and the chosen card must cast free",
        "stats": {"states_seen": "n/a", "trigger_observations": "n/a"},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_6764_01020.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6764_01020.py").read())
    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    WIRE.close()
    RUNLOG.close()


asyncio.run(main())
