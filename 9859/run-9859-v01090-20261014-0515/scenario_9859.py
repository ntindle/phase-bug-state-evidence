#!/usr/bin/env python3
"""Issue #9859: Tempt with Mayhem -- the controller copies the spell once,
not "once plus an additional time for each opponent who copied".

Behavioral contract (4-player Free-For-All, P0 = Tempt controller):
  - P1 casts Lightning Bolt targeting P3 (once P0 holds 3+ untapped
    Mountains, so P0 can pay for Tempt). P0 casts Tempt with Mayhem
    targeting that Bolt in P0's natural priority window during the
    pass-around (P1 -> P2 -> P3 -> P0), before the Bolt resolves.
  - NOTE (2026-10-13, attempt-1 lesson): P0 casting the Bolt itself does
    NOT work -- after P0's Bolt completes (target + payment), priority
    passes straight to P1; P0 never gets a hold-priority window to cast
    Tempt, and the Bolt resolves. The responder design above avoids the
    race: P0's Tempt window arrives through the normal pass-around.
  - Each opponent (P1, P2, P3) is offered the may-copy: driver ACCEPTS for
    all three. Each opponent is offered the may-retarget for their copy:
    driver DECLINES (copies keep the original target P3).
  - P0's copies: driver ACCEPTS each may-retarget and answers P2.
  - Expected (oracle): P0 makes 1 + 3 = 4 copies. Stack after Tempt
    resolves: 1 original Bolt (P1's) + 3 opponent copies + 4 P0 copies = 8.
    P3 takes 3*(1+3) = 12 -> life 8. P2 takes 3*4 = 12 -> life 8.
    P1, P0 stay at 20.
  - Buggy (reported): P0 makes exactly 1 copy regardless. P2 takes 3 -> 17.

Assertions:
  A1 setup_ok            Bolt cast (target P3), Tempt cast (target Bolt),
                         Tempt resolved (in P0 graveyard).
  A2 opponents_copied    Stack checkpoint: P1 controls 2 Bolt stack objects
                         (the ORIGINAL + P1's copy), P2/P3 each exactly 1
                         (their copy). I.e. opponent copies: P1 = count-1,
                         P2 = count, P3 = count; each == 1.
  A3 controller_copies   DECISIVE. Stack checkpoint: P0 controls N copies
                         (the original Bolt is P1's, not P0's); N == 4
                         expected. FAILED = bug.
  A4 damage_correct      Post-final lives equal the ORACLE outcome:
                         [20, 20, 8, 8] (P2 takes 3x4 P0 copies, P3 takes
                         3x4 original+opponent copies).
  A5 bug_signature       Post-final lives equal the REPORTED-BUG outcome:
                         [20, 20, 17, 8] (P0 made exactly 1 copy: P2 takes
                         3, P3 still takes 12). A5 passing with A1 passed
                         and all 3 opponent copies accepted is decisive
                         for `reproduced` even if the stack checkpoint
                         counting fails to observe the copies.

Conventions (from scenario_9858.py, protocol 140):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - Never inject targets into CastSpell; answer the engine's vi
    TargetSelection opportunity afterwards (CR 601.2c).
  - decideOptionalEffect accept/decline via the choice whose surface
    carries role=="accept" value true/false, else text fallback.
  - Export response data.state is a JSON string, parsed once.
  - Stack entries in snapshots/exports carry NO name field; resolve via
    state.objects[source_id] (stack_entry_name). Liveness guards: break an
    attempt on closed client websockets or 180s without a revision change.
  - Target menus may MIX a pure target candidate with priority-menu action
    entries (Tempt's target opp: exactChoices {candidate, passPriority,
    untapLandForMana}); accept the menu for its pure candidate(s) only.
  - Tempt's cast completes via a passPriority payment-confirm menu (no
    target opp is ever offered -- the engine auto-targets the only legal
    target) and its may-copy/retarget prompts run as a post-resolution
    continuation with Tempt already in the graveyard.
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
log = logging.getLogger("scenario9859")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9859-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9859
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.109.0",
    "build_commit": "932e389",
    "protocol_version": 140,
    "mode": "Full",
    "server_binary_sha256": "b2522f3657873456f1a5d17c2c97842e2ea2c6833afcbb7bc492696ff89142d5",
    "card_data_sha256": "9089f823081843e8ad700ce362a448a3ba165d377a23465c86355e8ed07e5225",
    "draft_pools_sha256": "2543f1bdfb3e4c2469ff3193b91a936d3ddc6bde99c6f5d19926cae4743c7309",
    "signature_verified": True,
    "signature_note": ("v0.109.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.109.0 (published "
               "2026-10-13T18:43:00Z) == pinned release dir; ServerHello "
               "0.109.0/932e389/protocol 140 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.109.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release); "
               "scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9859/" + RUN_ID),
}

FORMAT_FFA = {
    "format": "FreeForAll",
    "starting_life": 20,
    "min_players": 2,
    "max_players": 6,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": False,
    "commander_damage_threshold": None,
    "range_of_influence": None,
    "team_based": False,
    "archenemy_player": None,
    "uses_commander": False,
    "sideboard_policy": {"type": "Unlimited"},
    "default_deck_copy_limit": {"type": "Unlimited"},
    "supplies_fixed_deck": False,
    "allow_debug_actions": False,
    "custom_rules": None,
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


for _f, _k in (("server/releases/v0.109.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.109.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.109.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against the pinned v0.109.0 digests",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.109.0/data/card-data.json"))


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


def player_life(state, pid):
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal", "life_total_value"):
        if p.get(k) is not None:
            try:
                return int(p.get(k))
            except (TypeError, ValueError):
                pass
    return None


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def is_land(o):
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def stack_entries(state):
    """Stack as a list of object dicts (entries may be oids or objects)."""
    out = []
    for e in state.get("stack") or []:
        if isinstance(e, dict):
            out.append(e)
        else:
            out.append(get_obj(state, e))
    return out


def stack_entry_name(state, e):
    """Name of a stack entry. Snapshot/export stack entries are lightweight
    ({id, source_id, controller, kind}) with NO base_name/name field --
    the name must be resolved via state.objects[source_id] (2026-10-13
    probe: a Bolt on the stack showed id=63/source_id=63/controller=1 and
    no name; the pre-fix on_stack/stack_bolts therefore never matched)."""
    if isinstance(e, dict):
        for k in ("base_name", "name"):
            if e.get(k):
                return str(e.get(k))
        for k in ("source_id", "id", "oid"):
            if e.get(k) is not None:
                o = get_obj(state, e.get(k))
                for kk in ("base_name", "name"):
                    if o.get(kk):
                        return str(o.get(kk))
    elif isinstance(e, (int, str)):
        o = get_obj(state, e)
        for kk in ("base_name", "name"):
            if o.get(kk):
                return str(o.get(kk))
    return "?"


def stack_bolts(state):
    """Lightning Bolt objects on the stack, grouped by controller."""
    counts = {}
    oids = []
    for e in stack_entries(state):
        if stack_entry_name(state, e).lower() == "lightning bolt":
            try:
                ctrl = int(e.get("controller")) if isinstance(e, dict) else None
            except (TypeError, ValueError):
                ctrl = None
            if ctrl is None and not isinstance(e, dict):
                ctrl = int(get_obj(state, e).get("controller", -1)) \
                    if get_obj(state, e).get("controller") is not None else None
            counts[ctrl] = counts.get(ctrl, 0) + 1
            oids.append(str((e.get("id") or e.get("oid") or e) if isinstance(e, dict) else e))
    return counts, oids


def in_graveyard(state, pid, name):
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() == name \
                and o.get("zone") == "Graveyard" \
                and str(o.get("controller", o.get("owner", -1))) == str(pid):
            return True
    return False


def on_stack(state, pid, name):
    for e in stack_entries(state):
        ctrl = None
        if isinstance(e, dict):
            ctrl = e.get("controller")
        else:
            ctrl = get_obj(state, e).get("controller")
        if stack_entry_name(state, e).lower() == name \
                and str(ctrl) == str(pid):
            return True
    return False


BOLT = "lightning bolt"
TEMPT = "tempt with mayhem"
MOUNTAIN = "mountain"


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
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            spec = (data.get("spec", {}) or {}).get("type")
            if spec == "relations":
                continue
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


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def _opp_summary(st):
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        out.append({"iid": str(opp.get("interactionId"))[:12],
                    "rtype": resp.get("type"),
                    "stype": (resp.get("data", {}) or {}).get("spec", {}).get("type")
                    if isinstance((resp.get("data", {}) or {}).get("spec"), dict) else None,
                    "n_items": len(items),
                    "codes": sorted(c for c in codes if c)})
    return out


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] is not None:
        msg["data"] = a["data"]
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
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.109.0"), f"unexpected version {ver}"
    assert int(proto) == 140, f"unexpected protocol {proto}"
    assert str(build) == "932e389", f"unexpected build {build}"


# ------------------------------------------------------------- action helpers

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
        # Prefer discarding a Mountain so Bolt/Tempt are never dumped.
        picks = [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) == MOUNTAIN] or cands[:1]
        picks = picks[:1]
        say(f"[{tag}] discarding to hand size: "
            f"{[choice_text(x)[:40] for x in picks]}")
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
                manas = [s for s in ch.get("surfaces", []) or []]
                syms = []
                for m in manas:
                    if m.get("type") == "mana":
                        syms = (m.get("data") or {}).get("symbols", []) or []
                        break
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
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
        await interact_as(c, sub, tag)
        return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action({"type": "PassPriority"})
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
    lands = [o for o in hand_ids(state, pid)
             if is_land(get_obj(state, o))]
    for o in lands:
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


def untapped_land_total(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def untapped_mountains(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == MOUNTAIN
               and not get_obj(state, o).get("tapped"))


def p0_main(state, pid=0, empty_stack=True):
    if state.get("phase") not in ("PreCombatMain", "PostCombatMain"):
        return False
    if int(state.get("active_player", -1)) != pid:
        return False
    if empty_stack and (state.get("stack") or []):
        return False
    return True


# ------------------------------------------------------- target selection

def _is_pure_candidate(ch):
    """True if the choice carries NO action surface. Per the #9842 lesson:
    a genuine card-choice candidate must have no action surface at all;
    priority-menu entries (playLand, castSpell, tapLandForMana,
    passPriority, ...) all carry action surfaces. A target-selection menu
    may MIX a pure target candidate with priority-menu action entries
    (observed on Tempt's target opp: exactChoices, 2 items, codes
    {candidate, passPriority, untapLandForMana}) -- the menu must not be
    rejected for containing passPriority; the pure candidate is the target."""
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            return False
    return True


def target_opportunity(st):
    """First vi opportunity that looks like a target selection: schema
    select/sequence with candidates, or exactChoices with a pure-candidate
    choice (passPriority menus and decideOptionalEffect excluded)."""
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
            if chs and "decideOptionalEffect" not in codes \
                    and any(_is_pure_candidate(ch) for ch in chs):
                return opp, "exactChoices", "choose"
    return None, None, None


def classify_target_candidates(state, opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    out = []
    for ch in cands:
        if not _is_pure_candidate(ch):
            continue  # priority-menu action entry in a mixed menu
        ref = _cand_ref(ch)
        obj = get_obj(state, ref) if ref else {}
        ctrl = obj.get("controller")
        try:
            ctrl = int(ctrl) if ctrl is not None else None
        except (TypeError, ValueError):
            ctrl = None
        out.append({"id": ch.get("id"), "ref": ref,
                    "name": obj_lname(state, ref) if ref else choice_text(ch),
                    "controller": ctrl,
                    "zone": obj.get("zone"),
                    "text": choice_text(ch)[:80]})
    return out


def classify_player_candidates(state, opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    out = []
    for ch in cands:
        if not _is_pure_candidate(ch):
            continue  # priority-menu action entry in a mixed menu
        seat = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            for k in ("seat", "player", "index"):
                if d.get(k) is not None:
                    try:
                        seat = int(d.get(k))
                    except (TypeError, ValueError):
                        seat = None
                    if seat is not None:
                        break
            if seat is not None:
                break
            if d.get("role") == "player" and d.get("reference") is not None:
                try:
                    seat = int(d.get("reference"))
                except (TypeError, ValueError):
                    seat = None
        if seat is not None:
            out.append({"id": ch.get("id"), "seat": seat,
                        "text": choice_text(ch)[:80]})
    return out


async def submit_target(c, opp, rtype, spec_type, chs, tag):
    iid = opp.get("interactionId")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [ch["id"] for ch in chs]}}}
    else:
        assert len(chs) == 1
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": chs[0]["id"]}}}
    say(f"[{tag}] submitting target(s): "
        f"{[(ch.get('name'), ch.get('controller'), ch.get('seat')) for ch in chs]}")
    wire("target_submission", {"who": tag, "submission": sub,
                               "targets": chs})
    await interact_as(c, sub, tag)
    return iid


# ------------------------------------------------------- optional prompts

def find_optional_opp(st):
    """Find the decideOptionalEffect opportunity (exactChoices)."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data", {}) or {}).get("choices", []):
            if "decideOptionalEffect" in surf_codes(ch):
                return opp
    return None


def opp_text_blob(opp):
    """All visible text in an opportunity, for copy-vs-retarget classification."""
    parts = []
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    for k in ("prompt", "description", "title", "message"):
        if data.get(k):
            parts.append(str(data.get(k)))
    for ch in data.get("choices") or data.get("candidates") or []:
        parts.append(choice_text(ch))
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                for k in ("description", "prompt", "name", "value"):
                    if d.get(k) is not None:
                        parts.append(str(d.get(k)))
    return " | ".join(parts)


def pick_optional_choice(opp, want_accept):
    """Return the choice id matching want_accept, or None."""
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        is_accept = None
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "accept":
                is_accept = str(dd.get("value")).lower() == "true"
        if is_accept is None:
            txt = choice_text(ch).lower()
            if "accept" in txt or "copy" in txt or "yes" in txt \
                    or "choose new targets" in txt or "retarget" in txt:
                is_accept = True
            elif "decline" in txt or "don't" in txt or "do not" in txt \
                    or "no " in txt or txt.strip() == "no":
                is_accept = False
        if is_accept == want_accept:
            return ch.get("id")
    return None


async def answer_optional(c, opp, want_accept, tag):
    ch_id = pick_optional_choice(opp, want_accept)
    if ch_id is None:
        say(f"[{tag}] optional prompt: no {'accept' if want_accept else 'decline'} "
            f"choice identified; choices="
            f"{[choice_text(ch) for ch in (opp.get('response', {}).get('data', {}) or {}).get('choices', [])]}")
        return False
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": "choose",
                                       "data": {"choiceId": ch_id}}}, tag)
    return True


# ------------------------------------------------------------------- ticks

async def common_tick_head(c, acts, st, pid, tag):
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareBlockers" in atypes:
        return True  # never block in this scenario; hold
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    return None


async def declare_no_attackers(c, acts):
    da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
    if da:
        import copy as _copy
        d = _copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    return False


def tempt_resolving(ctx, state):
    # NOTE: the engine runs Tempt's may-copy/retarget prompts as a
    # post-resolution continuation (Tempt already in the graveyard while
    # the prompts are pending), so this must NOT require Tempt to still
    # be on the stack.
    return ctx.get("tempt_cast") and ctx.get("tempt_targets_done")


def note_tempt_auto_target(ctx, state, tag):
    """If Tempt resolved without the driver ever submitting targets, the
    engine auto-targeted (the only legal target was P1's Bolt) and is
    running the copy prompts as a post-resolution continuation. Mark
    targets done so the may-copy/retarget prompt branches fire."""
    if ctx.get("tempt_cast") and not ctx.get("tempt_targets_done") \
            and tempt_resolved(ctx, state):
        ctx["tempt_targets_done"] = True
        ctx["tempt_auto_targeted"] = True
        say(f"[{tag}] Tempt resolved without driver-submitted targets; "
            f"engine auto-targeted (only legal target was P1's Bolt)")
        wire("tempt_auto_targeted", {})


def tempt_resolved(ctx, state):
    return ctx.get("tempt_cast") and in_graveyard(state, 0, TEMPT)


async def p0_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)

    r = await common_tick_head(c, acts, st, 0, tag)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    if "DeclareAttackers" in atypes:
        await declare_no_attackers(c, acts)
        return

    # --- P0 prompts during Tempt resolution (L4 retarget for P0's copies).
    # P0 is not in the Opponent scope, so any decideOptionalEffect here is
    # a retarget decision: ACCEPT (unless its text says "copy", which
    # would be a scoping anomaly -- decline + flag).
    note_tempt_auto_target(ctx, state, tag)
    if tempt_resolving(ctx, state):
        opp = find_optional_opp(st)
        if opp is not None:
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                blob = opp_text_blob(opp)
                n = ctx.get("p0_retarget_decisions", 0) + 1
                ctx["p0_retarget_decisions"] = n
                with open(f"{EVDIR}/p0_optional_opp_{n}.json", "w") as f:
                    json.dump({"rev": c.revision, "opportunity": opp,
                               "text_blob": blob}, f, indent=1, default=str)
                say(f"[{tag}] P0 optional opp #{n} during Tempt resolution: "
                    f"blob={blob[:220]}")
                wire("p0_optional_opp", {"n": n, "blob": blob[:400]})
                low = blob.lower()
                if "copy" in low and "target" not in low:
                    ctx["p0_copy_prompt_seen"] = True
                    say(f"[{tag}] P0 got a COPY prompt (scoping anomaly); "
                        f"declining so L3 is the only source of P0 copies")
                    wire("p0_copy_prompt_anomaly", {"blob": blob[:400]})
                    await answer_optional(c, opp, False, tag)
                else:
                    await answer_optional(c, opp, True, tag)
                return True
        topp, rtype, stype = target_opportunity(st)
        if topp is not None:
            iid = topp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                players = classify_player_candidates(state, topp)
                cls = classify_target_candidates(state, topp)
                n = ctx.get("p0_retarget_targets", 0) + 1
                ctx["p0_retarget_targets"] = n
                with open(f"{EVDIR}/p0_retarget_opp_{n}.json", "w") as f:
                    json.dump({"rev": c.revision, "rtype": rtype,
                               "spec_type": stype, "opportunity": topp,
                               "player_classified": players,
                               "object_classified": cls},
                              f, indent=1, default=str)
                say(f"[{tag}] P0 retarget opp #{n}: rtype={rtype} "
                    f"stype={stype} players={players}")
                wire("p0_retarget_opp", {"n": n, "rtype": rtype,
                                         "stype": stype, "players": players,
                                         "objects": cls})
                pick = next((p for p in players if p["seat"] == 2), None)
                if pick is None:
                    say(f"[{tag}] P0 retarget opp has no P2 player "
                        f"candidate -- recorded, not answered")
                    wire("p0_retarget_no_p2", {"n": n, "players": players,
                                               "objects": cls})
                    return True
                await submit_target(c, topp, rtype, stype,
                                    [{"id": pick["id"], "name": "P2",
                                      "controller": 2, "seat": 2}], tag)
                return True

    # --- Tempt target selection (after CastSpell, CR 601.2c): the Bolt.
    if ctx.get("tempt_cast") and not ctx.get("tempt_targets_done"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                cls = classify_target_candidates(state, opp)
                with open(f"{EVDIR}/tempt_target_opp.json", "w") as f:
                    json.dump({"rev": c.revision, "rtype": rtype,
                               "spec_type": stype, "opportunity": opp,
                               "classified": cls}, f, indent=1, default=str)
                say(f"[{tag}] Tempt target opp: rtype={rtype} stype={stype} "
                    f"candidates={cls}")
                wire("tempt_target_opp", {"rtype": rtype, "stype": stype,
                                          "classified": cls})
                bolts = [x for x in cls if x["name"] == BOLT
                         and x["zone"] == "Stack"]
                if bolts:
                    ctx["tempt_targets_done"] = True
                    ctx["tempt_target_oid"] = bolts[0]["ref"]
                    say(f"[{tag}] Tempt targeting Bolt (oid "
                        f"{bolts[0]['ref']})")
                    await submit_target(c, opp, rtype, stype, [bolts[0]], tag)
                    return True
                say(f"[{tag}] Tempt target opp has no Bolt-on-stack "
                    f"candidate -- recorded, not answered")
                wire("tempt_target_no_bolt", {"classified": cls})
                return True
        # No target opportunity recognized: the engine may be waiting on
        # P0 to confirm mana payment -- the post-tap menu offers
        # passPriority + untapLandForMana, no target candidate. Pass once
        # per menu iid to advance the cast to target selection.
        for _opp in vi_ops(st):
            _iid = _opp.get("interactionId")
            if ("tempt-pass", _iid) in SUBMITTED_OPPS:
                continue
            _data = (_opp.get("response", {}) or {}).get("data", {}) or {}
            for _ch in _data.get("choices") or []:
                _codes = [s.get("data", {}).get("code")
                          for s in _ch.get("surfaces", []) or []
                          if s.get("type") == "action"]
                if "passPriority" in _codes and _ch.get("status", {}) \
                        .get("type") in (None, "available"):
                    SUBMITTED_OPPS.add(("tempt-pass", _iid))
                    say(f"[{tag}] Tempt-target wait: passing priority on "
                        f"menu {_iid} to advance the cast")
                    wire("tempt_pass_to_advance", {"iid": _iid})
                    await interact_as(c, {"interactionId": _iid,
                                          "response": {"type": "choose",
                                                       "data": {"choiceId": _ch.get("id")}}}, tag)
                    return True
        key = (tag, "tempt-no-target-opp", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("tempt_no_target_opp",
                 {"rev": c.revision, "opps": _opp_summary(st)})
            # Dump the FULL opportunity: the menu shape decides the
            # target-candidate discriminator (exactChoices target menus mix
            # the candidate with priority-menu action entries).
            with open(f"{EVDIR}/tempt_target_unrecognized.json", "w") as f:
                json.dump({"rev": c.revision,
                           "opportunities": vi_ops(st)}, f, indent=1, default=str)
            detail = []
            for _opp in vi_ops(st):
                _data = (_opp.get("response", {}) or {}).get("data", {}) or {}
                for _ch in _data.get("choices") or _data.get("candidates") or []:
                    detail.append({"id": str(_ch.get("id")),
                                   "surfaces": [[s.get("type"),
                                                (s.get("data") or {}).get("code")
                                                if isinstance(s.get("data"), dict) else None]
                                               for s in _ch.get("surfaces", []) or []]})
            say(f"[{tag}] Tempt cast but no target opportunity recognized; "
                f"vi={_opp_summary(st)} detail={detail}")
        return True

    # --- Cast Tempt with Mayhem targeting P1's Bolt on the stack.
    # Fires in P0's natural priority window during the pass-around
    # (P1 -> P2 -> P3 -> P0) after P1 casts Bolt. No p0_main
    # requirement: it is P1's turn. No bolt_targets_done requirement:
    # P1's tick answers the Bolt target selection.
    if ctx.get("bolt_cast") and not ctx.get("tempt_cast") \
            and on_stack(state, 1, BOLT) and my_priority(acts):
        hn = hand_names(state, 0)
        if TEMPT in hn and untapped_mountains(state, 0) >= 3:
            a, oid = cast_action_for(acts, state, TEMPT)
            if a is not None:
                ctx["tempt_cast"] = True
                ctx["tempt_cast_at"] = time.time()
                ST["mana_needs"][tag] = {"R": 2, "generic": 1}
                pre = json.loads(await c.export_state())
                with open(f"{EVDIR}/pre_tempt.json", "w") as f:
                    json.dump(pre, f, indent=1)
                wire("pre_tempt_export", {})
                say(f"[{tag}] casting Tempt with Mayhem (oid {oid}) "
                    f"targeting P1's Bolt on the stack")
                wire("cast_tempt", {"oid": oid,
                                    "cast_action_data": a.get("data")})
                await submit_as_is(c, a)
                return True
            key = (tag, "tempt-not-advertised", c.revision)
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] Tempt in hand + mana + Bolt on stack + "
                    f"priority, but CastSpell for Tempt NOT advertised")
                wire("tempt_not_advertised",
                     {"hand": hn,
                      "untapped_mountains": untapped_mountains(state, 0)})

    # --- Stack checkpoint: Tempt resolved, P0 holds priority, export
    # BEFORE passing so the Bolt copies are all still on the stack.
    # PEAK tracking (2026-10-13): the checkpoint must NOT fire on the
    # first Tempt-resolved tick -- the engine creates the copies over
    # the next few revisions as the copy prompts are answered, so an
    # immediate checkpoint sees only the original Bolt. Instead record
    # the maximum Bolt-stack snapshot observed from Tempt-resolution
    # until the stack empties (the stack only grows during copy
    # creation, then shrinks as Bolts resolve), and finalize when the
    # stack empties. The peak is the full copy set.
    if tempt_resolved(ctx, state) and not ctx.get("stack_counted"):
        counts, oids = stack_bolts(state)
        total = sum(counts.values())
        # Diagnostic: stack non-empty but zero Bolts counted => the
        # counting (name resolution) is missing entries; dump the raw
        # stack + resolved objects once so the miss is auditable.
        if (state.get("stack") or []) and total == 0 \
                and not ctx.get("stack_unresolved_dumped"):
            ctx["stack_unresolved_dumped"] = True
            with open(f"{EVDIR}/stack_unresolved_rev{c.revision}.json", "w") as f:
                json.dump({"rev": c.revision,
                           "raw_stack": state.get("stack"),
                           "resolved": [get_obj(state, (e.get("source_id") or e.get("id"))
                                                if isinstance(e, dict) else e)
                                        for e in (state.get("stack") or [])]},
                          f, indent=1, default=str)
            say(f"[{tag}] stack non-empty but 0 Bolts counted -- dumped raw stack to stack_unresolved_rev{c.revision}.json")
        if total > ctx.get("stack_peak_total", -1):
            ctx["stack_peak_total"] = total
            ctx["stack_checkpoint_counts"] = counts
            ctx["stack_checkpoint_oids"] = oids
            ctx["stack_checkpoint_rev"] = c.revision
            post = json.loads(await c.export_state())
            with open(f"{EVDIR}/stack_after_tempt.json", "w") as f:
                json.dump(post, f, indent=1)
            wire("stack_checkpoint_peak", {"counts": counts, "oids": oids})
            say(f"[{tag}] STACK CHECKPOINT (peak) after Tempt resolved: "
                f"Bolt stack objects by controller = {counts} (oids={oids})")
        if not (state.get("stack") or []) \
                and ctx.get("stack_checkpoint_counts") is not None:
            ctx["stack_counted"] = True

    # --- play a land on P0's main phase
    if p0_main(state, 0, empty_stack=True):
        if await play_a_land(c, state, 0, acts, tag):
            return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def other_tick(c, pid, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    r = await common_tick_head(c, acts, st, pid, tag)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    if "DeclareAttackers" in atypes:
        await declare_no_attackers(c, acts)
        return

    # --- Opponent prompts during Tempt resolution.
    # Per opponent, in chain order: 1st decideOptionalEffect = may-copy
    # (ACCEPT), 2nd = may-retarget for their copy (DECLINE = keep P3).
    # If retarget arrives as a direct target prompt instead, answer P3.
    note_tempt_auto_target(ctx, state, tag)
    if tempt_resolving(ctx, state):
        opp = find_optional_opp(st)
        if opp is not None:
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                blob = opp_text_blob(opp)
                stage = ctx.setdefault("opp_stage", {}).get(pid, "copy")
                n = ctx.get(f"opp_optional_{pid}", 0) + 1
                ctx[f"opp_optional_{pid}"] = n
                with open(f"{EVDIR}/opp{pid}_optional_opp_{n}.json", "w") as f:
                    json.dump({"rev": c.revision, "opportunity": opp,
                               "text_blob": blob, "stage": stage},
                              f, indent=1, default=str)
                say(f"[{tag}] opponent optional opp #{n} (stage={stage}): "
                    f"blob={blob[:220]}")
                wire("opp_optional_opp", {"pid": pid, "n": n,
                                          "stage": stage, "blob": blob[:400]})
                if stage == "copy":
                    ctx.setdefault("opp_stage", {})[pid] = "retarget"
                    ctx.setdefault("opp_copy_accepted", {})[pid] = True
                    await answer_optional(c, opp, True, tag)
                else:
                    ctx.setdefault("opp_stage", {})[pid] = "done"
                    ctx.setdefault("opp_retarget_declined", {})[pid] = True
                    await answer_optional(c, opp, False, tag)
                return True
        topp, rtype, stype = target_opportunity(st)
        if topp is not None:
            iid = topp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                players = classify_player_candidates(state, topp)
                cls = classify_target_candidates(state, topp)
                n = ctx.get(f"opp_target_{pid}", 0) + 1
                ctx[f"opp_target_{pid}"] = n
                with open(f"{EVDIR}/opp{pid}_target_opp_{n}.json", "w") as f:
                    json.dump({"rev": c.revision, "rtype": rtype,
                               "spec_type": stype, "opportunity": topp,
                               "player_classified": players,
                               "object_classified": cls},
                              f, indent=1, default=str)
                say(f"[{tag}] opponent target opp #{n}: rtype={rtype} "
                    f"stype={stype} players={players}")
                wire("opp_target_opp", {"pid": pid, "n": n, "rtype": rtype,
                                        "stype": stype, "players": players,
                                        "objects": cls})
                pick = next((p for p in players if p["seat"] == 3), None)
                if pick is None:
                    say(f"[{tag}] opponent target opp has no P3 player "
                        f"candidate -- recorded, not answered")
                    wire("opp_target_no_p3", {"pid": pid, "players": players,
                                              "objects": cls})
                    return True
                ctx.setdefault("opp_retarget_done", {})[pid] = True
                await submit_target(c, topp, rtype, stype,
                                    [{"id": pick["id"], "name": "P3",
                                      "controller": 3, "seat": 3}], tag)
                return True

    # --- P1: Bolt target selection (after CastSpell, CR 601.2c): P3.
    if pid == 1 and ctx.get("bolt_cast") and not ctx.get("bolt_targets_done"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            iid = opp.get("interactionId")
            if iid not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(iid)
                players = classify_player_candidates(state, opp)
                cls = classify_target_candidates(state, opp)
                with open(f"{EVDIR}/bolt_target_opp.json", "w") as f:
                    json.dump({"rev": c.revision, "rtype": rtype,
                               "spec_type": stype, "opportunity": opp,
                               "player_classified": players,
                               "object_classified": cls},
                              f, indent=1, default=str)
                say(f"[{tag}] Bolt target opp: rtype={rtype} stype={stype} "
                    f"players={players}")
                wire("bolt_target_opp", {"rtype": rtype, "stype": stype,
                                         "players": players, "objects": cls})
                pick = next((p for p in players if p["seat"] == 3), None)
                if pick is None:
                    say(f"[{tag}] Bolt target opp has no P3 player "
                        f"candidate -- recorded, not answered")
                    wire("bolt_target_no_p3", {"players": players,
                                               "objects": cls})
                    return True
                ctx["bolt_targets_done"] = True
                ctx["bolt_target_seat"] = 3
                await submit_target(c, opp, rtype, stype,
                                    [{"id": pick["id"], "name": "P3",
                                      "controller": 3, "seat": 3}], tag)
                return True
        key = (tag, "bolt-no-target-opp", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("bolt_no_target_opp",
                 {"rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] Bolt cast but no target opportunity recognized; "
                f"vi={_opp_summary(st)}")
        return True

    # --- P1: cast the single Lightning Bolt (target P3) once P0 can pay
    # for Tempt (3+ untapped Mountains) and the game is far enough along
    # that P0 very likely holds Tempt (turn >= 14: P0 has seen 11 cards).
    # Single-Bolt gate: if P0 misses the response window, the attempt
    # ends (retry in the next attempt) so no resolved Bolt pollutes the
    # life-total math.
    if pid == 1 and not ctx.get("bolt_cast") \
            and not ctx.get("stack_counted") \
            and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and int(state.get("active_player", -1)) == 1 \
            and not (state.get("stack") or []) \
            and int(state.get("turn_number", 0)) >= 14 \
            and untapped_mountains(state, 0) >= 3:
        hn = hand_names(state, 1)
        if BOLT in hn and untapped_mountains(state, 1) >= 1:
            a, oid = cast_action_for(acts, state, BOLT)
            if a is not None:
                ctx["bolt_cast"] = True
                ctx["bolt_cast_at"] = time.time()
                ctx["bolt_oid"] = oid
                ctx["bolt_caster"] = 1
                ST["mana_needs"][tag] = {"R": 1}
                say(f"[{tag}] P1 casting Lightning Bolt (oid {oid}) "
                    f"targeting P3; P0 to answer with Tempt")
                wire("cast_bolt", {"oid": oid})
                await submit_as_is(c, a)
                return True

    # --- play a land on their own main phase
    if state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and int(state.get("active_player", -1)) == pid \
            and not (state.get("stack") or []):
        if await play_a_land(c, state, pid, acts, tag):
            return

    if decision_pending(st):
        key = (tag, "other-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("other_prompt", {"who": tag, "rev": c.revision,
                                  "opps": _opp_summary(st)})
            say(f"[{tag}] other prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------------- run

async def create_ffa_game(c):
    """Create a 4-player Free-For-All game via CreateGameWithSettings."""
    await c.ws.send(json.dumps({
        "type": "CreateGameWithSettings",
        "data": {
            "deck": deck((MOUNTAIN.capitalize(), 35),
                         ("Tempt with Mayhem", 25)),
            "display_name": c.name,
            "public": False,
            "password": None,
            "timer_seconds": None,
            "player_count": 4,
            "match_config": {"match_type": "Bo1"},
            "ai_seats": [],
            "format_config": FORMAT_FFA,
            "room_name": None,
            "host_peer_id": None,
            "draft_metadata": None,
            "start_when_full": True,
            "ranked": False,
        },
    }))
    for _ in range(50):
        t, data = await asyncio.wait_for(c.inbox.get(), 10)
        if t == "SessionAttached":
            return data
    raise RuntimeError("no SessionAttached")


async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9859-{attempt}"
    ST["mana_needs"][tag] = {}
    for i in range(1, 4):
        ST["mana_needs"][f"P{i}{tag}"] = {}
    ctx = {}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await create_ffa_game(p0)
    clients = [p0]
    join_decks = {
        1: deck((MOUNTAIN.capitalize(), 40), ("Lightning Bolt", 20)),
        2: deck((MOUNTAIN.capitalize(), 60)),
        3: deck((MOUNTAIN.capitalize(), 60)),
    }
    for i in range(1, 4):
        c = PhaseClient(f"P{i}{tag}")
        await c.connect()
        await c.join(p0.game_code, join_decks[i])
        clients.append(c)
        say(f"joined P{i} seat={c.player_id}")
    say(f"game {p0.game_code} attempt={attempt}; seats="
        f"{[c.player_id for c in clients]}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "seats": [c.player_id for c in clients]})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    for k in ("A1_setup_ok", "A2_opponents_copied", "A3_controller_copies",
              "A4_damage_correct", "A5_bug_signature"):
        obs["assert"][k] = "not-run"

    t0 = time.time()
    last_diag = 0
    last_rev = -1
    last_rev_change = time.time()
    settle_ticks = 0
    player_keys_logged = False
    while time.time() - t0 < 780:
        await asyncio.sleep(0.25)
        await p0_tick(p0, tag, ctx)
        for c, pid in zip(clients[1:], (1, 2, 3)):
            await other_tick(c, pid, f"P{pid}{tag}", ctx)
        await asyncio.sleep(0)
        # --- liveness guards (2026-10-13: the prior run's attempt 2 died
        # when the server took 49.3s on one P2 PassPriority at turn-5 Draw
        # and all four client sockets ping-timed-out; the driver then
        # looped ~8 min on the dead game at rev=128. Break the attempt
        # instead of burning the run.)
        cur_rev = p0.revision
        if cur_rev != last_rev:
            last_rev = cur_rev
            last_rev_change = time.time()
        dead = [c.name for c in clients
                if getattr(c, "ws", None) is not None
                and not getattr(c.ws, "open", True)]
        if dead:
            say(f"[{tag}] client websocket(s) closed: {dead}; "
                f"abandoning attempt (dead game)")
            obs["notes"].append(
                "abandoned: client websocket(s) closed mid-attempt "
                f"({dead}); server-side stall/disconnect, not a game "
                "decision -- retry in the next attempt")
            break
        if time.time() - last_rev_change > 180:
            say(f"[{tag}] no state-revision change in 180s "
                f"(rev={cur_rev}); abandoning attempt (stalled game)")
            obs["notes"].append(
                "abandoned: no state-revision change in 180s "
                f"(rev={cur_rev}); game stalled with no client-side "
                "action available -- retry in the next attempt")
            break
        st_now = p0.latest
        if st_now and not player_keys_logged:
            player_keys_logged = True
            p0p = player_of(st_now["state"], 0)
            say(f"[diag] P0 player keys: {sorted(p0p.keys())} "
                f"life-ish={ {k: p0p.get(k) for k in ('life', 'life_total', 'lifeTotal')} }")
        if st_now and st_now["state"].get("turn_number", 0) > 40 \
                and not ctx.get("bolt_cast"):
            say(f"[{tag}] turn {st_now['state'].get('turn_number')} exceeded "
                f"without the Bolt cast; abandoning attempt")
            obs["notes"].append(
                f"abandoned: game reached turn "
                f"{st_now['state'].get('turn_number')} without P1 casting "
                f"Lightning Bolt (P1 never held Bolt with P0 on 3+ "
                f"untapped Mountains)")
            break
        if ctx.get("bolt_cast") and not ctx.get("tempt_cast") \
                and not on_stack(st_now["state"] if st_now else {}, 1, BOLT) \
                and not ((st_now["state"] if st_now else {}).get("stack") or []) \
                and time.time() - ctx.get("bolt_cast_at", 0) > 5:
            say(f"[{tag}] P1's Bolt resolved without P0 casting Tempt "
                f"(Tempt likely not in P0's hand); single-Bolt gate, "
                f"abandoning attempt")
            obs["notes"].append(
                "abandoned: P1's Lightning Bolt resolved without P0 "
                "casting Tempt with Mayhem in response (Tempt likely not "
                "in P0's hand); single-Bolt gate -- retry in next attempt "
                "so no resolved Bolt pollutes the life-total math")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pid in zip(clients, (0, 1, 2, 3)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"bolt={ctx.get('bolt_cast')} tempt={ctx.get('tempt_cast')} "
                    f"stack_counted={ctx.get('stack_counted')} "
                    f"lives={[player_life(s, p) for p in (0, 1, 2, 3)]}")
        if ctx.get("stack_counted"):
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break
        if ctx.get("tempt_cast") and not ctx.get("tempt_targets_done") \
                and time.time() - ctx.get("tempt_cast_at", 0) > 240:
            say(f"[{tag}] Tempt cast 240s ago, target selection never "
                f"completed; abandoning attempt")
            obs["notes"].append(
                "abandoned: Tempt with Mayhem cast but its target selection "
                "never completed within 240s")
            break
        if ctx.get("tempt_targets_done") \
                and not ctx.get("stack_counted") \
                and time.time() - ctx.get("tempt_cast_at", 0) > 300:
            say(f"[{tag}] Tempt targeted 300s ago, resolution never "
                f"completed; abandoning attempt")
            obs["notes"].append(
                "abandoned: Tempt with Mayhem targeted but its resolution "
                "(opponent may-copy / retarget prompts) never completed "
                "within 300s")
            break

    st = p0.latest
    state = st["state"] if st else {}
    # ---- A1
    if ctx.get("stack_counted") or tempt_resolved(ctx, state):
        obs["assert"]["A1_setup_ok"] = "passed"
        _tgt = ("engine auto-targeted" if ctx.get("tempt_auto_targeted")
                else f"oid {ctx.get('tempt_target_oid')}")
        obs["notes"].append(
            "A1 passed: 4-player Free-For-All game; P1 cast Lightning Bolt "
            f"(target P3, oid {ctx.get('bolt_oid')}), P0 answered with "
            f"Tempt with Mayhem (targeting that Bolt, {_tgt}); "
            "Tempt resolved (in P0 graveyard)")
    elif ctx.get("tempt_cast"):
        obs["assert"]["A1_setup_ok"] = "failed"
        obs["notes"].append(
            "A1 FAILED: Tempt with Mayhem was cast but never resolved")
    elif ctx.get("bolt_cast"):
        obs["assert"]["A1_setup_ok"] = "failed"
        obs["notes"].append(
            "A1 FAILED: P1's Lightning Bolt was cast but P0 never answered "
            "with Tempt with Mayhem (Tempt likely not in P0's hand)")
    else:
        obs["assert"]["A1_setup_ok"] = "not-run"
        obs["notes"].append("A1 not-run: Lightning Bolt never cast")
    # ---- A2 (opponent copies; P1 also controls the original Bolt)
    counts = ctx.get("stack_checkpoint_counts") or {}
    if ctx.get("stack_counted"):
        opp_copies = {1: (counts.get(1) or 0) - 1,
                      2: counts.get(2) or 0,
                      3: counts.get(3) or 0}
        ok = all(v == 1 for v in opp_copies.values())
        if ok:
            obs["assert"]["A2_opponents_copied"] = "passed"
            obs["notes"].append(
                "A2 passed: stack checkpoint after Tempt resolved shows "
                "each opponent controlling exactly 1 Bolt copy "
                f"(P1 count 2 = original + copy; counts={counts})")
        else:
            obs["assert"]["A2_opponents_copied"] = "failed"
            obs["notes"].append(
                "A2 FAILED: stack checkpoint after Tempt resolved shows "
                f"opponent Bolt-copy counts != 1 each: {opp_copies} "
                f"(raw stack counts={counts}; copy prompts accepted: "
                f"{ctx.get('opp_copy_accepted')})")
    else:
        obs["assert"]["A2_opponents_copied"] = "not-run"
        obs["notes"].append("A2 not-run: stack checkpoint never taken")
    # ---- A3 (decisive)
    n_copies = None
    if ctx.get("stack_counted"):
        # P0's stack count IS its copy count: the original Bolt is P1's.
        n_copies = counts.get(0) or 0
        obs["n_controller_copies"] = n_copies
        if n_copies == 4:
            obs["assert"]["A3_controller_copies"] = "passed"
            obs["notes"].append(
                "A3 passed: P0 controls 4 copies on the stack "
                f"after Tempt resolved (1 + 3 opponents who copied): {counts}")
        else:
            obs["assert"]["A3_controller_copies"] = "failed"
            obs["notes"].append(
                "A3 FAILED (reported bug): P0 controls "
                f"{n_copies} copies on the stack after Tempt resolved "
                f"(expected 4 = 1 + 3 opponents who copied): {counts}. "
                "The controller's copy count does not grow by one per "
                "opponent who copied.")
    else:
        obs["assert"]["A3_controller_copies"] = "not-run"
        obs["notes"].append("A3 not-run: stack checkpoint never taken")
    # ---- A4 (oracle damage, NOT derived from the measured copy count,
    # so a counting failure cannot make A4 pass circularly)
    lives = [player_life(state, p) for p in (0, 1, 2, 3)]
    obs["final_lives"] = lives
    if ctx.get("stack_counted") and not (state.get("stack") or []):
        exp = [20, 20, 8, 8]
        if lives == exp:
            obs["assert"]["A4_damage_correct"] = "passed"
            obs["notes"].append(
                f"A4 passed: final lives {lives} match the oracle outcome "
                f"{exp} (P2 took 3 x 4 P0 copies, P3 took 3 x 4 "
                "original+opponent copies)")
        else:
            obs["assert"]["A4_damage_correct"] = "failed"
            obs["notes"].append(
                f"A4 FAILED: final lives {lives} do not match the oracle "
                f"outcome {exp} (P2 should take 3 x 4 P0 copies, P3 12)")
    else:
        obs["assert"]["A4_damage_correct"] = "not-run"
        obs["notes"].append(
            f"A4 not-run: stack not empty or checkpoint missing "
            f"(lives={lives}, stack={len(state.get('stack') or [])})")
    # ---- A5 (reported-bug signature: P0 made exactly 1 copy)
    if ctx.get("stack_counted") and not (state.get("stack") or []):
        bug = [20, 20, 17, 8]
        if lives == bug:
            obs["assert"]["A5_bug_signature"] = "passed"
            obs["notes"].append(
                f"A5 passed (reported-bug signature): final lives {lives} "
                "== [20, 20, 17, 8]: P2 took exactly 3 (exactly one P0 "
                "copy reached P2) while P3 took 12 (original + 3 "
                "opponent copies). With all 3 opponent may-copy prompts "
                "accepted and P0's retarget answered to P2, this is the "
                "reported bug even if the stack checkpoint counting "
                "failed to observe the copies.")
        else:
            obs["assert"]["A5_bug_signature"] = "failed"
            obs["notes"].append(
                f"A5 failed: final lives {lives} != reported-bug "
                "signature [20, 20, 17, 8]")

    # final state export
    try:
        final = json.loads(await p0.export_state())
        with open(f"{EVDIR}/post_final.json", "w") as f:
            json.dump(final, f, indent=1)
    except Exception as e:
        say(f"final export failed: {e}")

    for c in clients:
        await c.close()
    return obs, ctx


# ------------------------------------------------------------------- render

def render_summary(obs, ctx):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        font_b = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_t = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
        font_b = font
        font_t = font
    img = Image.new("RGB", (1000, 700), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #9859 -- Tempt with Mayhem: controller copies once, not 1+N", font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.109.0 (932e389, protocol 140) | Free-For-All, 4 players | Bolt -> Tempt", font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID} | game {obs.get('game_code')}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = str(obs.get("verdict", "blocked")).upper().replace("_", "-")
    if verdict == "NOT-REPRODUCED":
        verdict = "NOT-REPRODUCED"
    elif verdict not in ("REPRODUCED", "BLOCKED"):
        verdict = "BLOCKED / NOT-RUN"
    color = (255, 90, 90) if verdict == "REPRODUCED" else ((90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    for k in ("A1_setup_ok", "A2_opponents_copied", "A3_controller_copies",
              "A4_damage_correct", "A5_bug_signature"):
        v = obs["assert"].get(k, "not-run")
        vc = (90, 255, 140) if v == "passed" else ((255, 90, 90) if v == "failed" else (255, 200, 60))
        d.text((24, y), f"{k}: {v}", font=font_b, fill=vc)
        y += 30
    y += 10
    d.text((24, y), "Expected (oracle): with 3 opponents copying, the controller makes", font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "1 + 3 = 4 copies: P2 takes 12 (life 8), P3 takes 12 (life 8).", font=font, fill=(220, 220, 230))
    y += 26
    counts = ctx.get("stack_checkpoint_counts") or {}
    if ctx.get("stack_counted"):
        d.text((24, y), f"Observed stack after Tempt resolved (Bolt objects by controller): {counts}", font=font, fill=(255, 160, 160))
        y += 26
        d.text((24, y), f"Final lives P0/P1/P2/P3: {obs.get('final_lives')}", font=font, fill=(255, 160, 160))
    else:
        d.text((24, y), "Observed: stack checkpoint never taken; Tempt resolution not completed.", font=font, fill=(255, 200, 60))
    y += 40
    d.text((24, y), "Parse note: the controller's second CopySpell carries no count --", font=font, fill=(160, 160, 170))
    y += 26
    d.text((24, y), "the \"once plus an additional time per opponent who copied\" multiplicity", font=font, fill=(160, 160, 170))
    y += 26
    d.text((24, y), "is not represented in the parsed effect chain.", font=font, fill=(160, 160, 170))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_attempt(1)
    if obs["assert"].get("A3_controller_copies") == "not-run" \
            and obs["assert"].get("A1_setup_ok") in ("failed", "not-run"):
        say("attempt 1 did not reach the Tempt resolution; running attempt 2")
        obs, ctx = await run_attempt(2)
    obs["server_identity"] = SERVER_IDENTITY
    obs["issue"] = ISSUE
    obs["run_id"] = RUN_ID
    obs["format_config"] = FORMAT_FFA
    obs["card_data_oracle"] = {
        "tempt with mayhem": (CARD_DATA.get("tempt with mayhem") or {}).get("oracle_text"),
        "lightning bolt": (CARD_DATA.get("lightning bolt") or {}).get("oracle_text"),
        "mountain": (CARD_DATA.get("mountain") or {}).get("oracle_text"),
    }
    a = obs["assert"]
    all_opp_accepted = bool(ctx.get("opp_copy_accepted")) and all(
        ctx.get("opp_copy_accepted", {}).get(p) for p in (1, 2, 3))
    # Damage (lives) is ground truth and outranks the stack checkpoint:
    # if the oracle damage is observed, the behavior is correct even if
    # the checkpoint counting disagrees (driver counting issue, noted).
    if a.get("A1_setup_ok") == "passed" and all_opp_accepted \
            and a.get("A4_damage_correct") == "passed":
        verdict = "not-reproduced"
    elif a.get("A1_setup_ok") == "passed" and all_opp_accepted and (
            a.get("A3_controller_copies") == "failed"
            or a.get("A5_bug_signature") == "passed"):
        # A3 failed = checkpoint saw P0 copies != 4. A5 passed = the
        # damage outcome is exactly the reported-bug signature (P0 made
        # exactly 1 copy); decisive even if checkpoint counting failed.
        verdict = "reproduced"
    elif a.get("A2_opponents_copied") == "passed" and a.get("A3_controller_copies") == "passed" \
            and a.get("A4_damage_correct") == "failed":
        # Copy counts right but damage wrong: a clearly identified
        # related failure (playbook: reproduced).
        verdict = "reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    obs["n_controller_copies"] = obs.get("n_controller_copies")
    obs["stack_checkpoint_counts"] = ctx.get("stack_checkpoint_counts")
    obs["final_lives"] = obs.get("final_lives")
    obs["p0_retarget_decisions"] = ctx.get("p0_retarget_decisions")
    obs["p0_retarget_targets"] = ctx.get("p0_retarget_targets")
    obs["p0_copy_prompt_seen"] = ctx.get("p0_copy_prompt_seen")
    obs["tempt_auto_targeted"] = ctx.get("tempt_auto_targeted")
    obs["opp_copy_accepted"] = ctx.get("opp_copy_accepted")
    obs["opp_retarget_declined"] = ctx.get("opp_retarget_declined")
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(obs, f, indent=1, default=str)
    say("assertions: " + json.dumps(obs["assert"]))
    for n in obs["notes"]:
        say("note: " + n)
    say(f"VERDICT: {verdict}")
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9859",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                          "(--single-user --no-data-download, verified release); "
                          f"evidence under evidence/9859/{RUN_ID}",
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Mountain", 35], ["Tempt with Mayhem", 25]],
            "P1": [["Mountain", 40], ["Lightning Bolt", 20]],
            "P2": [["Mountain", 60]],
            "P3": [["Mountain", 60]],
        },
        "assertions": dict(a),
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; controller_copies=%s; "
                   "stack_counts=%s; final_lives=%s"
                   % (a.get("A1_setup_ok"), a.get("A2_opponents_copied"),
                      a.get("A3_controller_copies"),
                      a.get("A4_damage_correct"),
                      a.get("A5_bug_signature"),
                      obs.get("n_controller_copies"),
                      ctx.get("stack_checkpoint_counts"),
                      obs.get("final_lives"))),
        "scope": ("Tempt with Mayhem tempting-offer copy count (1 + number of "
                  "opponents who copied) in a real 4-player Free-For-All "
                  "game; native engine, four human-client seats; target "
                  "spell Lightning Bolt"),
        "limitations": [
            "Browser UI not exercised; native engine via four human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "25x/20x deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Verdict rule: not-reproduced = A1 passed, all 3 opponent may-copy prompts accepted, and A4 passed (oracle damage [20,20,8,8] observed -- damage is ground truth and outranks the checkpoint). reproduced = A1 passed + all opponents accepted + (A3 failed, i.e. checkpoint saw P0 copies != 4, or A5 passed, i.e. final lives exactly the reported-bug signature [20,20,17,8]), or A3 passed but A4 failed (related damage failure). blocked = Tempt resolution never observed.",
        ],
        "setup_line": ("P0 35x Mountain + 25x Tempt with Mayhem; P1 40x Mountain + 20x Lightning Bolt; P2/P3 60x Mountain. FFA 4-player, starting life 20."),
        "contract_line": ("P1 casts Lightning Bolt targeting P3 (once P0 holds 3+ untapped Mountains); P0 answers with Tempt with Mayhem targeting that Bolt. All 3 opponents accept the may-copy (decline retarget, copies keep P3); P0 accepts retarget for its copies and answers P2. Expected: P0 makes 4 copies (1+3); P2 life 8, P3 life 8. Bug: P0 makes 1 copy; P2 life 17."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump({k: CARD_DATA.get(k) for k in
                   ("tempt with mayhem", "lightning bolt", "mountain")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9859.py", "w") as f:
        f.write(open(__file__).read())
    render_summary(obs, ctx)
    wire("verdict", {"verdict": verdict, "assertions": dict(a)})
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        with open(f"{EVDIR}/{fn}", "rb") as f:
            lines.append(hashlib.sha256(f.read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(dict(a))}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
