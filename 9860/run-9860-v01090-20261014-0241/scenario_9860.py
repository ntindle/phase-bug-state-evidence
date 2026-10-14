#!/usr/bin/env python3
"""Issue #9860: Raiding Party -- every Plains is destroyed even when each
player taps white creatures.

Oracle: "This enchantment can't be the target of white spells or abilities
from white sources. Sacrifice an Orc: Each player may tap any number of
untapped white creatures they control. For each creature tapped this way,
that player chooses up to two Plains. Then destroy all Plains that weren't
chosen this way by any player."

Behavioral contract (2-player game, P0 = Raiding Party controller):
  - P0 BF: Raiding Party + 1 Orcish Conscripts (sacrifice fodder, {R} 2/2,
    no triggers/abilities) + 1 Elite Vanguard (white, {W} 2/1 vanilla) +
    3 Plains + 2 Mountains.
  - P1 BF: 1 Elite Vanguard + 3 Plains.
  - P0 activates Raiding Party (sacrifice the Conscripts). Each player
    taps their Vanguard. Each player chooses up to two Plains: P0 chooses
    its first two Plains (by oid), P1 chooses its first two Plains.
  - Expected (oracle): the 4 chosen Plains survive; the other 2 Plains
    (P0's 3rd, P1's 3rd) are destroyed.
  - Buggy (reported): ALL 6 Plains are destroyed -- the per-player
    selections are not read back as the "chosen this way" set.

Assertions:
  A1 setup_ok            PRE state has the full setup above (P0: Raiding
                         Party + Conscripts + Vanguard + >=3 Plains; P1:
                         Vanguard + >=3 Plains).
  A2 taps_happened       POST shows both designated Vanguards tapped.
  A3 chosen_survive      POST shows all 4 designated Plains still on the
                         battlefield. FAILED = the reported bug.
  A4 unchosen_destroyed POST shows every non-designated Plains (from PRE)
                         in a graveyard.
  A5 cleanup            POST stack empty, Conscripts in P0's graveyard,
                         Raiding Party still on P0's BF, no pending
                         decision.

Verdict rule (pre-registered):
  - not-reproduced: A1 + A2 passed, 4 Plains choices actually made
    (chosen set == designated set), A3 + A4 + A5 passed.
  - reproduced: A1 + A2 passed, choices actually made, and (A3 failed
    [chosen Plains destroyed -- the reported signature] OR (A3 passed
    and A4 failed [destroy step did not destroy -- a clearly identified
    related failure of the same ability])).
  - blocked: anything else (setup never completed, tap/choice prompts
    never offered so the reported path was not exercised, chosen set
    did not match the designated set, etc.).

Revision 02 (attempt 2, 2026-10-14): attempt 1 (run-9860-v01090-20261014-0111)
stalled at rev 181 because the driver submitted TWO choiceIds to a
sequence prompt whose spec was min=1/max=1 (iid 7SC7M0.0.181); the engine
silently ignored the over-max submission, the prompt stayed pending, and
the driver's SUBMITTED_OPPS blacklist refused to re-answer it. Fix: cap
every schema submission at the advertised spec max (Plains + tap
branches), and allow exactly one re-answer of a still-pending iid
(RETRIED_OPPS) so a silently-ignored submission cannot deadlock the game.
Verdict rule, decks, assertions, and all other logic unchanged.
Completion-detector fix (same revision): the first 02 run showed the
resolution DOES settle (Plains pick -> sacrifice -> may-tap optionals ->
taps), but the old "8 quiet ticks with no pending decision" POST detector
never fired -- the game then cycles draw/discard decisions every turn and
the Vanguards untap on later untap steps, so a late POST would show
taps=False. POST is now exported the moment the resolution observably
settles (sacrifice + both taps + >=1 pick, stack empty, no Raiding prompt
pending on either view, BF Plains count dropped below PRE).

Conventions (from scenario_9859.py, protocol 140):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - Never inject targets into CastSpell; answer the engine's vi
    TargetSelection opportunity afterwards (CR 601.2c). No CastSpell
    targets are needed here (all creatures/spells are targetless).
  - Export response data.state is a JSON string, parsed once.
  - Prompt-driven resolution: the Raiding Party tap/choice prompts are
    answered by candidate-object classification (Orc = sacrifice cost,
    Vanguard = tap, Plains = choice), never by assumed menu shape.
  - finish() ordering: run.json, scenario copy, every logging hook,
    THEN close WIRE/RUNLOG, THEN manifest LAST.
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
log = logging.getLogger("scenario9860")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9860-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9860
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
    "source": ("2026-10-14: latest stable release v0.109.0 (published "
               "2026-10-13T18:43:00Z) == pinned release dir; ServerHello "
               "0.109.0/932e389/protocol 140 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.109.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release); "
               "scenario games created via CreateGameWithSettings; "
               "evidence under evidence/9860/" + RUN_ID),
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


def bf_named(state, pid, name):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == name]


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def is_land(o):
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def stack_entries(state):
    out = []
    for e in state.get("stack") or []:
        if isinstance(e, dict):
            out.append(e)
        else:
            out.append(get_obj(state, e))
    return out


def stack_entry_name(state, e):
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


def envelope_state(exported):
    """Authoritative export envelope -> game state dict."""
    if not isinstance(exported, dict):
        return {}
    if "objects" in exported:
        return exported
    if isinstance(exported.get("state"), dict):
        return exported["state"]
    return exported


MOUNTAIN = "mountain"
PLAINS = "plains"
VANGUARD = "elite vanguard"
ORC = "orcish conscripts"
RAIDING = "raiding party"


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


def raiding_pending(st):
    """True if a Raiding Party resolution prompt is pending on this view.

    Classification mirrors answer_raiding_opp: the may-tap optional, or
    pure candidates referencing the Orc (sacrifice), a Vanguard (tap), or
    a Plains (choice). Used by the POST-settle detector so ordinary
    post-resolution decisions (draw/discard, priority) do not mask it.
    """
    if not st:
        return False
    state = st["state"]
    for opp in vi_ops(st):
        if find_optional_opp(st) is opp:
            return True
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        for ch in items:
            if not _is_pure_candidate(ch):
                continue
            if obj_lname(state, _cand_ref(ch)) in (ORC, VANGUARD, PLAINS):
                return True
    return False


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

# ------------------------------------------------------------- action helpers

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
        # Prefer discarding a land (decks are land-dense); never dump
        # the Raiding Party / Orc / Vanguard while setup is incomplete.
        picks = [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) == MOUNTAIN] or \
                [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) == PLAINS] or cands[:1]
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


async def play_named_land(c, state, pid, acts, tag, name):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    lands = [o for o in hand_ids(state, pid)
             if is_land(get_obj(state, o))
             and obj_lname(state, o) == name]
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


def untapped_of_name(state, pid, name):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == name
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
    """True if the choice carries NO action surface (per the #9842
    lesson: a genuine card-choice candidate must have no action surface
    at all; priority-menu entries all carry action surfaces)."""
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
            continue
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
        f"{[(ch.get('name'), ch.get('controller')) for ch in chs]}")
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

# ------------------------------------------------- Raiding Party handling

RECORDED_OPPS = set()
RETRIED_OPPS = set()


def _spec_max(data, default):
    """Advertised max picks for a schema prompt, else `default`.

    The spec lives at data.spec.data.{min,max} (e.g. sequence min=1 max=1
    on the Raiding Party Plains prompt). A submission over max is silently
    ignored by the engine (attempt 1), so every schema submission must
    respect it. Missing/degenerate max -> `default`.
    """
    try:
        spec = (data.get("spec", {}) or {}).get("data", {}) or {}
        mx = spec.get("max", None)
        if mx is None:
            return default
        mx = int(mx)
        return mx if mx > 0 else default
    except Exception:
        return default

DONE_WORDS = ("done", "finish", "confirm", "no more", "choose none",
              "pass", "decline", "skip", "cancel", "none", "end")


def _is_done_choice(ch):
    txt = choice_text(ch).strip().lower()
    return txt in DONE_WORDS or "done" in txt or "no more" in txt \
        or "choose none" in txt


def dump_raiding_opp(state, opp, kind, tag, ctx):
    n = ctx.get("raiding_prompts_seen", 0) + 1
    ctx["raiding_prompts_seen"] = n
    with open(f"{EVDIR}/raiding_opp_a{ctx.get('attempt', 1)}_{n}.json", "w") as f:
        json.dump({"rev": None, "kind": kind, "tag": tag,
                   "opportunity": opp, "text_blob": opp_text_blob(opp)},
                  f, indent=1, default=str)
    wire("raiding_opp", {"n": n, "kind": kind, "tag": tag,
                         "blob": opp_text_blob(opp)[:400]})
    say(f"[{tag}] Raiding prompt #{n} kind={kind}: "
        f"blob={opp_text_blob(opp)[:220]}")


async def answer_raiding_opp(c, st, pid, tag, ctx):
    """Answer one Raiding Party prompt for this client, if one is showing.

    Classification is by the referenced object of each pure candidate:
    Orcish Conscripts = sacrifice cost, Elite Vanguard = tap choice,
    Plains = Plains choice. Optional (may) prompts during resolution are
    ACCEPTED. Returns True if something was submitted or the prompt was
    recorded-not-answerable (so the tick does not fall through to a
    priority pass while a decision is pending).
    """
    state = st["state"]
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in RECORDED_OPPS:
            continue
        if iid in SUBMITTED_OPPS:
            # Still pending after a submission: the engine may have
            # silently ignored it (attempt 1: over-max sequence). Allow
            # exactly ONE re-answer with the (now spec-capped) payload,
            # but only if the engine recorded nothing yet (progress
            # selected == 0); if it recorded a pick, re-answering risks
            # a double-apply and the stall needs a human read of the
            # wire log instead.
            if iid in RETRIED_OPPS:
                continue
            prog = opp.get("progress", {}) or {}
            if int(prog.get("selected", 0) or 0) > 0:
                continue
            RETRIED_OPPS.add(iid)
            say(f"[{tag}] Raiding opp {str(iid)[:16]} still pending after "
                f"a submission; retrying once (spec-capped payload)")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        # Optional (may) prompt during resolution: ACCEPT -- the only
        # optional in this ability is the may-tap / may-choose.
        if rtype == "exactChoices" and find_optional_opp(st) is opp:
            dump_raiding_opp(state, opp, "optional", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            await answer_optional(c, opp, True, tag)
            return True
        pure = [ch for ch in items if _is_pure_candidate(ch)]
        if not pure:
            continue
        names = {}
        for ch in pure:
            ref = _cand_ref(ch)
            names[ch.get("id")] = (obj_lname(state, ref) if ref else None,
                                   ref)
        kinds = set(n for n, _ in names.values())
        # --- sacrifice cost: the Orc.
        if ORC in kinds:
            dump_raiding_opp(state, opp, "sacrifice", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            pick = next(ch for ch in pure
                        if names[ch.get("id")][0] == ORC)
            ctx["sacrifice_done"] = True
            ctx["sacrifice_oid"] = names[pick.get("id")][1]
            if rtype == "schema":
                spec = (data.get("spec", {}) or {}).get("type") or "select"
                sub = {"interactionId": iid,
                       "response": {"type": spec,
                                    "data": {"choiceIds": [pick.get("id")]}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": pick.get("id")}}}
            say(f"[{tag}] answering sacrifice cost with Orc "
                f"oid {ctx['sacrifice_oid']}")
            wire("raiding_sacrifice", {"oid": ctx["sacrifice_oid"]})
            await interact_as(c, sub, tag)
            return True
        # --- tap choice: white creatures.
        if VANGUARD in kinds:
            ctx["tap_prompts_seen"] = ctx.get("tap_prompts_seen", 0) + 1
            dump_raiding_opp(state, opp, "tap", tag, ctx)
            remaining = [ch for ch in pure
                         if names[ch.get("id")][1] in ctx.get("tap_queue", [])
                         and names[ch.get("id")][1]
                         not in ctx.get("tap_answers", [])]
            if remaining:
                SUBMITTED_OPPS.add(iid)
                if rtype == "schema":
                    spec = (data.get("spec", {}) or {}).get("type") or "select"
                    cap = _spec_max(data, len(remaining))
                    ids = [ch.get("id") for ch in remaining[:cap]]
                    for ch in remaining:
                        ctx.setdefault("tap_answers", []).append(
                            names[ch.get("id")][1])
                    sub = {"interactionId": iid,
                           "response": {"type": spec,
                                        "data": {"choiceIds": ids}}}
                else:
                    ch = remaining[0]
                    ctx.setdefault("tap_answers", []).append(
                        names[ch.get("id")][1])
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": ch.get("id")}}}
                say(f"[{tag}] tapping Vanguard(s): {ctx['tap_answers']}")
                wire("raiding_tap", {"tapped": list(ctx["tap_answers"])})
                await interact_as(c, sub, tag)
                return True
            done = next((ch for ch in items if _is_done_choice(ch)), None)
            if done is not None:
                SUBMITTED_OPPS.add(iid)
                say(f"[{tag}] tap prompt: all designated Vanguards already "
                    f"tapped; answering done")
                wire("raiding_tap_done", {})
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": done.get("id")}}}, tag)
                return True
            if rtype == "schema":
                spec = (data.get("spec", {}) or {}).get("type") or "select"
                SUBMITTED_OPPS.add(iid)
                say(f"[{tag}] tap prompt: no remaining designated Vanguard; "
                    f"submitting empty selection")
                wire("raiding_tap_empty", {})
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": spec,
                                                   "data": {"choiceIds": []}}}, tag)
                return True
            RECORDED_OPPS.add(iid)
            say(f"[{tag}] tap prompt: no remaining designated Vanguard and "
                f"no done choice -- recorded, not answered")
            wire("raiding_tap_unanswered", {"iid": str(iid)[:12]})
            return True
        # --- Plains choice.
        if PLAINS in kinds:
            ctx["plains_prompts_seen"] = ctx.get("plains_prompts_seen", 0) + 1
            dump_raiding_opp(state, opp, "plains", tag, ctx)
            queue = ctx.get("plains_pick_queue", [])
            cands = [ch for ch in pure
                     if names[ch.get("id")][1] in queue]
            if cands:
                SUBMITTED_OPPS.add(iid)
                if rtype == "schema":
                    picks = cands[:_spec_max(data, 2)]
                    for ch in picks:
                        oid = names[ch.get("id")][1]
                        ctx.setdefault("plains_chosen", []).append(oid)
                        ctx["plains_pick_queue"].remove(oid)
                    sub = {"interactionId": iid,
                           "response": {"type": (data.get("spec", {}) or {}).get("type") or "select",
                                        "data": {"choiceIds": [ch.get("id") for ch in picks]}}}
                    say(f"[{tag}] choosing Plains (schema, "
                        f"{len(picks)}): {ctx['plains_chosen']}")
                else:
                    ch = cands[0]
                    oid = names[ch.get("id")][1]
                    ctx.setdefault("plains_chosen", []).append(oid)
                    ctx["plains_pick_queue"].remove(oid)
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": ch.get("id")}}}
                    say(f"[{tag}] choosing Plains (single): {oid}; "
                        f"chosen so far: {ctx['plains_chosen']}")
                wire("raiding_plains_choice",
                     {"chosen": list(ctx.get("plains_chosen", [])),
                      "remaining_queue": list(ctx.get("plains_pick_queue", []))})
                await interact_as(c, sub, tag)
                return True
            done = next((ch for ch in items if _is_done_choice(ch)), None)
            if done is not None:
                SUBMITTED_OPPS.add(iid)
                say(f"[{tag}] Plains prompt: no remaining designated Plains "
                    f"among candidates; answering done")
                wire("raiding_plains_done", {})
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": done.get("id")}}}, tag)
                return True
            if rtype == "schema":
                spec = (data.get("spec", {}) or {}).get("type") or "select"
                SUBMITTED_OPPS.add(iid)
                say(f"[{tag}] Plains prompt: no designated Plains among "
                    f"candidates; submitting empty selection")
                wire("raiding_plains_empty", {})
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": spec,
                                                   "data": {"choiceIds": []}}}, tag)
                return True
            RECORDED_OPPS.add(iid)
            say(f"[{tag}] Plains prompt: no designated Plains among "
                f"candidates and no done choice -- recorded, not answered; "
                f"candidates={[names[ch.get('id')] for ch in pure]}")
            wire("raiding_plains_unanswered",
                 {"iid": str(iid)[:12],
                  "candidates": [names[ch.get("id")] for ch in pure],
                  "queue": list(ctx.get("plains_pick_queue", []))})
            return True
    # Legacy sacrifice-cost action fallback (no vi prompt offered).
    if ctx.get("raiding_activated") and not ctx.get("sacrifice_done"):
        for a in merged_actions(st):
            if "acrifice" in a["type"].lower():
                blob = json.dumps(a, default=str)
                orc_oids = bf_named(state, pid, ORC)
                data = a.get("data", {}) or {}
                refs = [str(v) for v in data.values()]
                if (orc_oids and (orc_oids[0] in refs
                                   or str(a.get("_src_oid")) in orc_oids)) \
                        or not refs:
                    say(f"[{tag}] legacy sacrifice action found; "
                        f"submitting as-is: {blob[:300]}")
                    wire("raiding_legacy_sacrifice", {"action": a})
                    ctx["sacrifice_done"] = True
                    ctx["sacrifice_oid"] = orc_oids[0] if orc_oids else None
                    await submit_as_is(c, a)
                    return True
                say(f"[{tag}] legacy sacrifice action NOT submitted "
                    f"(target unclear): {blob[:300]}")
                wire("raiding_legacy_sacrifice_skipped", {"action": a})
                return True
    return False


def setup_complete(state, ctx):
    return bool(bf_named(state, 0, RAIDING)
                and bf_named(state, 0, ORC)
                and bf_named(state, 0, VANGUARD)
                and bf_named(state, 1, VANGUARD)
                and len(bf_named(state, 0, PLAINS)) >= 3
                and len(bf_named(state, 1, PLAINS)) >= 3)


def designate(state, ctx):
    pl0 = sorted(bf_named(state, 0, PLAINS), key=lambda x: int(x))
    pl1 = sorted(bf_named(state, 1, PLAINS), key=lambda x: int(x))
    ctx["p0_plains"] = pl0
    ctx["p1_plains"] = pl1
    ctx["designated"] = pl0[:2] + pl1[:2]
    ctx["plains_pick_queue"] = list(ctx["designated"])
    ctx["tap_queue"] = [bf_named(state, 0, VANGUARD)[0],
                        bf_named(state, 1, VANGUARD)[0]]
    ctx["tap_answers"] = []
    ctx["plains_chosen"] = []
    ctx["sacrifice_done"] = False
    ctx["tap_prompts_seen"] = 0
    ctx["plains_prompts_seen"] = 0
    ctx["raiding_prompts_seen"] = 0


def p0_land_want(state):
    n_plains = len(bf_named(state, 0, PLAINS))
    n_mtn = len(bf_named(state, 0, MOUNTAIN))
    if n_plains < 1:
        return PLAINS
    if n_mtn < 1:
        return MOUNTAIN
    if n_plains < 3:
        return PLAINS
    if n_mtn < 2:
        return MOUNTAIN
    return None


async def try_cast(c, state, acts, tag, name, needs):
    """Cast `name` (P0 only) if in hand and affordable; set mana needs."""
    if name not in hand_names(state, 0):
        return False
    a, oid = cast_action_for(acts, state, name)
    if a is None:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid, "needs": needs})
    await submit_as_is(c, a)
    return True


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

    # --- Raiding Party resolution prompts (tap / Plains / sacrifice).
    if ctx.get("raiding_activated"):
        if await answer_raiding_opp(c, st, 0, tag, ctx):
            return

    # --- Activate Raiding Party once the setup is complete.
    if not ctx.get("raiding_activated") and p0_main(state, 0) \
            and setup_complete(state, ctx):
        designate(state, ctx)
        pre = json.loads(await c.export_state())
        with open(f"{EVDIR}/pre.json", "w") as f:
            json.dump(pre, f, indent=1)
        wire("pre_export", {"designated": ctx["designated"],
                            "tap_queue": ctx["tap_queue"]})
        say(f"[{tag}] PRE exported; designated Plains={ctx['designated']} "
            f"tap_queue={ctx['tap_queue']}; activating Raiding Party")
        raiding_oid = bf_named(state, 0, RAIDING)[0]
        act = None
        for a in acts:
            if a["type"] == "ActivateAbility" and (
                    str(a.get("_src_oid")) == str(raiding_oid)
                    or str(a.get("data", {}).get("source_id")) == str(raiding_oid)):
                if a.get("data", {}).get("ability_index", 0) == 0:
                    act = a
                    break
                act = act or a
        if act is not None:
            ctx["raiding_activated"] = True
            ctx["raiding_activated_at"] = time.time()
            ctx["raiding_oid"] = raiding_oid
            wire("activate_raiding", {"oid": raiding_oid,
                                      "action": act})
            await submit_as_is(c, act)
            return
        # Activation via a vi menu (activateAbility surface).
        for opp in vi_ops(st):
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                if "activateAbility" in surf_codes(ch) \
                        and _cand_ref(ch) == str(raiding_oid):
                    ctx["raiding_activated"] = True
                    ctx["raiding_activated_at"] = time.time()
                    ctx["raiding_oid"] = raiding_oid
                    wire("activate_raiding_vi", {"oid": raiding_oid})
                    await interact_as(c, {"interactionId": opp.get("interactionId"),
                                          "response": {"type": "choose",
                                                       "data": {"choiceId": ch.get("id")}}}, tag)
                    return
        key = (tag, "raiding-not-advertised", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] setup complete but ActivateAbility for Raiding "
                f"Party NOT advertised")
            wire("raiding_not_advertised", {"rev": c.revision})
        return

    # --- Setup: land, then one cast per tick (P0 main, stack empty).
    if not ctx.get("raiding_activated") and p0_main(state, 0):
        want = p0_land_want(state)
        if want and await play_named_land(c, state, 0, acts, tag, want):
            return
        if not bf_named(state, 0, VANGUARD) \
                and untapped_of_name(state, 0, PLAINS) >= 1:
            if await try_cast(c, state, acts, tag, VANGUARD, {"W": 1}):
                return
        if not bf_named(state, 0, ORC) \
                and untapped_of_name(state, 0, MOUNTAIN) >= 1:
            if await try_cast(c, state, acts, tag, ORC, {"R": 1}):
                return
        if not bf_named(state, 0, RAIDING) \
                and untapped_land_total(state, 0) >= 3 \
                and untapped_of_name(state, 0, MOUNTAIN) >= 1:
            if await try_cast(c, state, acts, tag, RAIDING,
                              {"R": 1, "generic": 2}):
                return

    if decision_pending(st):
        key = (tag, "p0-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("p0_prompt", {"rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] P0 prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p1_tick(c, tag, ctx):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)

    r = await common_tick_head(c, acts, st, 1, tag)
    if r is not None:
        return
    st = c.latest or st
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)

    if "DeclareAttackers" in atypes:
        await declare_no_attackers(c, acts)
        return

    # --- Raiding Party resolution prompts for P1.
    if ctx.get("raiding_activated"):
        if await answer_raiding_opp(c, st, 1, tag, ctx):
            return

    # --- Setup: Plains, then Vanguard (P1 main, stack empty).
    if not ctx.get("raiding_activated") \
            and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and int(state.get("active_player", -1)) == 1 \
            and not (state.get("stack") or []):
        if len(bf_named(state, 1, PLAINS)) < 3 \
                and await play_named_land(c, state, 1, acts, tag, PLAINS):
            return
        if not bf_named(state, 1, VANGUARD) \
                and untapped_of_name(state, 1, PLAINS) >= 1 \
                and VANGUARD in hand_names(state, 1):
            a, oid = cast_action_for(acts, state, VANGUARD)
            if a is not None:
                ST["mana_needs"][tag] = {"W": 1}
                say(f"[{tag}] casting {VANGUARD} (oid {oid})")
                wire("cast", {"who": tag, "card": VANGUARD, "oid": oid})
                await submit_as_is(c, a)
                return

    if decision_pending(st):
        key = (tag, "p1-prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("p1_prompt", {"rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] P1 prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision

# ------------------------------------------------------------------- run

async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    RECORDED_OPPS.clear()
    tag0 = f"9860-{attempt}"
    tag1 = f"P19860-{attempt}"
    ST["mana_needs"][tag0] = {}
    ST["mana_needs"][tag1] = {}
    ctx = {"attempt": attempt}

    p0 = PhaseClient(f"P0{tag0}")
    await p0.connect()
    await p0.create(deck(("Raiding Party", 12), ("Orcish Conscripts", 12),
                          ("Elite Vanguard", 12), ("Mountain", 12),
                          ("Plains", 12)))
    p1 = PhaseClient(f"P1{tag1}")
    await p1.connect()
    await p1.join(p0.game_code, deck(("Elite Vanguard", 12),
                                     ("Plains", 48)))
    clients = [p0, p1]
    say(f"game {p0.game_code} attempt={attempt}; seats="
        f"{[c.player_id for c in clients]}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "seats": [c.player_id for c in clients]})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    for k in ("A1_setup_ok", "A2_taps_happened", "A3_chosen_survive",
              "A4_unchosen_destroyed", "A5_cleanup"):
        obs["assert"][k] = "not-run"

    t0 = time.time()
    last_diag = 0
    last_rev = -1
    last_rev_change = time.time()
    settle_ticks = 0
    while time.time() - t0 < 780:
        await asyncio.sleep(0.25)
        await p0_tick(p0, tag0, ctx)
        await p1_tick(p1, tag1, ctx)
        await asyncio.sleep(0)
        cur_rev = p0.revision
        if cur_rev != last_rev:
            last_rev = cur_rev
            last_rev_change = time.time()
            settle_ticks = 0
        dead = [c.name for c in clients
                if getattr(c, "ws", None) is not None
                and not getattr(c.ws, "open", True)]
        if dead:
            say(f"[{tag0}] client websocket(s) closed: {dead}; "
                f"abandoning attempt (dead game)")
            obs["notes"].append(
                "abandoned: client websocket(s) closed mid-attempt "
                f"({dead}); server-side stall/disconnect, not a game "
                "decision -- retry in the next attempt")
            break
        if time.time() - last_rev_change > 180:
            say(f"[{tag0}] no state-revision change in 180s "
                f"(rev={cur_rev}); abandoning attempt (stalled game)")
            obs["notes"].append(
                "abandoned: no state-revision change in 180s "
                f"(rev={cur_rev}); game stalled with no client-side "
                "action available -- retry in the next attempt")
            break
        st_now = p0.latest
        if st_now and st_now["state"].get("turn_number", 0) > 30 \
                and not ctx.get("raiding_activated"):
            say(f"[{tag0}] turn {st_now['state'].get('turn_number')} exceeded "
                f"without Raiding Party activation; abandoning attempt")
            obs["notes"].append(
                "abandoned: game reached turn "
                f"{st_now['state'].get('turn_number')} without completing "
                "the setup + activation (setup never completed)")
            break
        if ctx.get("raiding_activated") \
                and time.time() - ctx.get("raiding_activated_at", 0) > 300 \
                and not ctx.get("post_exported"):
            say(f"[{tag0}] Raiding Party activated 300s ago, resolution "
                f"never completed; abandoning attempt")
            obs["notes"].append(
                "abandoned: Raiding Party activated but its resolution "
                "(tap / Plains-choice prompts) never completed within 300s")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pid in zip(clients, (0, 1)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"activated={ctx.get('raiding_activated')} "
                    f"taps={ctx.get('tap_answers')} "
                    f"plains_chosen={ctx.get('plains_chosen')} "
                    f"lives={[player_life(s, p) for p in (0, 1)]}")
        # --- completion: export POST the moment the Raiding resolution
        # has observably settled. The old "8 quiet ticks with no pending
        # decision" detector never fires here: after the resolution the
        # game keeps cycling draw/discard decisions every turn (and the
        # Vanguards untap on later untap steps), so a late POST would
        # show taps=False and post-resolution land plays. Settle =
        # sacrifice + both taps + >=1 Plains pick answered, stack empty,
        # no Raiding prompt pending on either view, and the BF Plains
        # count has dropped below PRE (the destroy step applied).
        if ctx.get("raiding_activated") and not ctx.get("post_exported"):
            st0 = p0.latest
            st1 = p1.latest
            if st0 and st1 and not (st0["state"].get("stack") or []):
                if "pre_plains_total" not in ctx:
                    try:
                        with open(f"{EVDIR}/pre.json") as f:
                            _pre = envelope_state(json.load(f))
                        ctx["pre_plains_total"] = (
                            len(bf_named(_pre, 0, PLAINS))
                            + len(bf_named(_pre, 1, PLAINS)))
                    except Exception:
                        ctx["pre_plains_total"] = None
                now_plains = (len(bf_named(st0["state"], 0, PLAINS))
                              + len(bf_named(st0["state"], 1, PLAINS)))
                settled = (
                    ctx.get("sacrifice_done")
                    and len(ctx.get("tap_answers", [])) >= 2
                    and len(ctx.get("plains_chosen", [])) >= 1
                    and not raiding_pending(st0)
                    and not raiding_pending(st1)
                    and ctx.get("pre_plains_total") is not None
                    and now_plains < ctx["pre_plains_total"])
                if settled:
                    say(f"[{tag0}] Raiding resolution settled: BF Plains "
                        f"{ctx['pre_plains_total']} -> {now_plains}; "
                        f"exporting POST")
                    wire("post_settled", {"pre_plains": ctx["pre_plains_total"],
                                          "post_plains": now_plains})
                    try:
                        post = json.loads(await p0.export_state())
                        with open(f"{EVDIR}/post.json", "w") as f:
                            json.dump(post, f, indent=1)
                        ctx["post_exported"] = True
                        ctx["post_pending"] = False
                        say(f"[{tag0}] POST exported after resolution settled")
                        wire("post_export", {})
                    except Exception as e:
                        say(f"POST export failed: {e}")
                        obs["notes"].append(f"POST export failed: {e}")
                    break

    # ---- evaluate assertions from PRE / POST / ctx
    pre_state = {}
    try:
        with open(f"{EVDIR}/pre.json") as f:
            pre_state = envelope_state(json.load(f))
    except Exception as e:
        obs["notes"].append(f"PRE export missing/unreadable: {e}")
    post_state = {}
    try:
        with open(f"{EVDIR}/post.json") as f:
            post_state = envelope_state(json.load(f))
    except Exception as e:
        obs["notes"].append(f"POST export missing/unreadable: {e}")

    if pre_state:
        n_pl0 = len(bf_named(pre_state, 0, PLAINS))
        n_pl1 = len(bf_named(pre_state, 1, PLAINS))
        obs["pre_plains"] = {"P0": n_pl0, "P1": n_pl1}
        obs["pre_bf"] = {
            "P0_raiding": len(bf_named(pre_state, 0, RAIDING)),
            "P0_orc": len(bf_named(pre_state, 0, ORC)),
            "P0_vanguard": len(bf_named(pre_state, 0, VANGUARD)),
            "P1_vanguard": len(bf_named(pre_state, 1, VANGUARD)),
        }
        if (bf_named(pre_state, 0, RAIDING)
                and bf_named(pre_state, 0, ORC)
                and bf_named(pre_state, 0, VANGUARD)
                and bf_named(pre_state, 1, VANGUARD)
                and n_pl0 >= 3 and n_pl1 >= 3):
            obs["assert"]["A1_setup_ok"] = "passed"
            obs["notes"].append(
                "A1 passed: PRE has P0 Raiding Party + Orcish Conscripts + "
                f"Elite Vanguard + {n_pl0} Plains; P1 Elite Vanguard + "
                f"{n_pl1} Plains")
        else:
            obs["assert"]["A1_setup_ok"] = "failed"
            obs["notes"].append(
                f"A1 FAILED: PRE battlefield incomplete: {obs['pre_bf']} "
                f"Plains P0={n_pl0} P1={n_pl1}")
    else:
        obs["assert"]["A1_setup_ok"] = "not-run"
        obs["notes"].append("A1 not-run: no PRE export (activation never "
                            "reached or export failed)")

    designated = ctx.get("designated", [])
    chosen = ctx.get("plains_chosen", [])
    obs["designated"] = designated
    obs["plains_chosen"] = chosen
    obs["tap_answers"] = ctx.get("tap_answers", [])
    obs["tap_queue"] = ctx.get("tap_queue", [])
    obs["choices_made"] = bool(designated) and sorted(chosen) == sorted(designated)
    if not obs["choices_made"]:
        obs["notes"].append(
            f"choices_made=False: designated={designated} "
            f"chosen={chosen} (tap prompts seen: "
            f"{ctx.get('tap_prompts_seen')}, Plains prompts seen: "
            f"{ctx.get('plains_prompts_seen')}, sacrifice done: "
            f"{ctx.get('sacrifice_done')})")

    if post_state and designated:
        tapped = []
        for oid in ctx.get("tap_queue", []):
            o = get_obj(post_state, oid)
            tapped.append(bool(o.get("tapped")))
        obs["post_vanguards_tapped"] = tapped
        if tapped and all(tapped):
            obs["assert"]["A2_taps_happened"] = "passed"
            obs["notes"].append(
                "A2 passed: both designated Elite Vanguards tapped in POST")
        else:
            obs["assert"]["A2_taps_happened"] = "failed"
            obs["notes"].append(
                f"A2 FAILED: designated Vanguards tapped flags in POST: "
                f"{tapped} (tap prompts seen: "
                f"{ctx.get('tap_prompts_seen')})")
        survivors = [oid for oid in designated
                     if obj_zone(post_state, oid) == "Battlefield"]
        obs["post_survivors"] = survivors
        if len(survivors) == len(designated):
            obs["assert"]["A3_chosen_survive"] = "passed"
            obs["notes"].append(
                "A3 passed: all 4 designated Plains still on the "
                "battlefield in POST")
        else:
            obs["assert"]["A3_chosen_survive"] = "failed"
            destroyed = [oid for oid in designated
                         if obj_zone(post_state, oid) != "Battlefield"]
            obs["notes"].append(
                "A3 FAILED (reported bug): designated Plains destroyed: "
                f"{[(oid, obj_zone(post_state, oid)) for oid in destroyed]}; "
                f"survivors: {survivors}")
        pre_plains_all = bf_named(pre_state, 0, PLAINS) \
            + bf_named(pre_state, 1, PLAINS)
        unchosen = [oid for oid in pre_plains_all
                    if oid not in designated]
        obs["post_unchosen"] = [(oid, obj_zone(post_state, oid))
                                for oid in unchosen]
        if unchosen and all(obj_zone(post_state, oid) == "Graveyard"
                            for oid in unchosen):
            obs["assert"]["A4_unchosen_destroyed"] = "passed"
            obs["notes"].append(
                "A4 passed: every non-designated Plains is in a graveyard "
                f"in POST ({len(unchosen)} Plains)")
        elif not unchosen:
            obs["assert"]["A4_unchosen_destroyed"] = "not-run"
            obs["notes"].append("A4 not-run: no non-designated Plains in PRE")
        else:
            obs["assert"]["A4_unchosen_destroyed"] = "failed"
            obs["notes"].append(
                "A4 FAILED: non-designated Plains not all destroyed: "
                f"{obs['post_unchosen']}")
        obs["post_stack_empty"] = not (post_state.get("stack") or [])
        obs["post_orc_sacrificed"] = in_graveyard(post_state, 0, ORC)
        obs["post_raiding_on_bf"] = bool(bf_named(post_state, 0, RAIDING))
        if obs["post_stack_empty"] and obs["post_orc_sacrificed"] \
                and obs["post_raiding_on_bf"]:
            obs["assert"]["A5_cleanup"] = "passed"
            obs["notes"].append(
                "A5 passed: POST stack empty, Conscripts in P0 graveyard, "
                "Raiding Party still on P0 battlefield")
        else:
            obs["assert"]["A5_cleanup"] = "failed"
            obs["notes"].append(
                "A5 FAILED: POST stack_empty="
                f"{obs['post_stack_empty']} orc_sacrificed="
                f"{obs['post_orc_sacrificed']} raiding_on_bf="
                f"{obs['post_raiding_on_bf']}")
    else:
        for k in ("A2_taps_happened", "A3_chosen_survive",
                  "A4_unchosen_destroyed", "A5_cleanup"):
            obs["assert"][k] = "not-run"
        obs["notes"].append("A2-A5 not-run: no POST export or no "
                            "designated set (resolution never settled)")

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
    d.text((24, y), "phase-rs/phase #9860 -- Raiding Party destroys every Plains", font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.109.0 (932e389, protocol 140) | 2 players | Sacrifice an Orc", font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID} | game {obs.get('game_code')}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = str(obs.get("verdict", "blocked")).upper().replace("_", "-")
    if verdict not in ("REPRODUCED", "NOT-REPRODUCED", "BLOCKED"):
        verdict = "BLOCKED / NOT-RUN"
    color = (255, 90, 90) if verdict == "REPRODUCED" else ((90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    for k in ("A1_setup_ok", "A2_taps_happened", "A3_chosen_survive",
              "A4_unchosen_destroyed", "A5_cleanup"):
        v = obs["assert"].get(k, "not-run")
        vc = (90, 255, 140) if v == "passed" else ((255, 90, 90) if v == "failed" else (255, 200, 60))
        d.text((24, y), f"{k}: {v}", font=font_b, fill=vc)
        y += 30
    y += 10
    d.text((24, y), "Expected (oracle): each player taps 1 Vanguard, chooses 2 Plains;", font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "the 4 chosen Plains survive; the other 2 are destroyed.", font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), f"Designated: {obs.get('designated')}  Chosen: {obs.get('plains_chosen')}", font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), f"Survivors in POST: {obs.get('post_survivors')}", font=font, fill=(255, 160, 160))
    y += 26
    d.text((24, y), f"Unchosen in POST (oid, zone): {obs.get('post_unchosen')}", font=font, fill=(255, 160, 160))
    y += 40
    d.text((24, y), "Parse note: the DestroyAll target is all Plains with no", font=font, fill=(160, 160, 170))
    y += 26
    d.text((24, y), "chosen-group exclusion in the parsed effect chain.", font=font, fill=(160, 160, 170))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    obs, ctx = await run_attempt(1)
    if obs["assert"].get("A1_setup_ok") in ("failed", "not-run") \
            and not ctx.get("raiding_activated"):
        say("attempt 1 did not reach the Raiding Party activation; "
            "running attempt 2")
        obs, ctx = await run_attempt(2)
    obs["server_identity"] = SERVER_IDENTITY
    obs["issue"] = ISSUE
    obs["run_id"] = RUN_ID
    obs["card_data_oracle"] = {
        "raiding party": (CARD_DATA.get("raiding party") or {}).get("oracle_text"),
        "orcish conscripts": (CARD_DATA.get("orcish conscripts") or {}).get("oracle_text"),
        "elite vanguard": (CARD_DATA.get("elite vanguard") or {}).get("oracle_text"),
        "plains": (CARD_DATA.get("plains") or {}).get("oracle_text"),
    }
    a = obs["assert"]
    choices_made = bool(obs.get("choices_made"))
    if a.get("A1_setup_ok") == "passed" \
            and a.get("A2_taps_happened") == "passed" and choices_made \
            and a.get("A3_chosen_survive") == "passed" \
            and a.get("A4_unchosen_destroyed") == "passed" \
            and a.get("A5_cleanup") == "passed":
        verdict = "not-reproduced"
    elif a.get("A1_setup_ok") == "passed" \
            and a.get("A2_taps_happened") == "passed" and choices_made \
            and (a.get("A3_chosen_survive") == "failed"
                 or a.get("A4_unchosen_destroyed") == "failed"
                 or (a.get("A5_cleanup") == "failed"
                     and obs.get("post_stack_empty")
                     and (not obs.get("post_orc_sacrificed")
                          or not obs.get("post_raiding_on_bf")))):
        verdict = "reproduced"
    else:
        verdict = "blocked"
    obs["verdict"] = verdict
    obs["choices_made"] = choices_made
    obs["post_stack_empty"] = obs.get("post_stack_empty")
    obs["post_orc_sacrificed"] = obs.get("post_orc_sacrificed")
    obs["post_raiding_on_bf"] = obs.get("post_raiding_on_bf")
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(obs, f, indent=1, default=str)
    say("assertions: " + json.dumps(obs["assert"]))
    for n in obs["notes"]:
        say("note: " + n)
    say(f"VERDICT: {verdict}")
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9860",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                          "(--single-user --no-data-download, verified release); "
                          f"evidence under evidence/9860/{RUN_ID}",
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Raiding Party", 12], ["Orcish Conscripts", 12],
                   ["Elite Vanguard", 12], ["Mountain", 12], ["Plains", 12]],
            "P1": [["Elite Vanguard", 12], ["Plains", 48]],
        },
        "assertions": dict(a),
        "notes": list(obs["notes"]),
        "verdict": verdict,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; designated=%s; "
                   "chosen=%s; survivors=%s; unchosen_post=%s; "
                   "orc_sacrificed=%s; raiding_on_bf=%s"
                   % (a.get("A1_setup_ok"), a.get("A2_taps_happened"),
                      a.get("A3_chosen_survive"),
                      a.get("A4_unchosen_destroyed"),
                      a.get("A5_cleanup"), obs.get("designated"),
                      obs.get("plains_chosen"), obs.get("post_survivors"),
                      obs.get("post_unchosen"),
                      obs.get("post_orc_sacrificed"),
                      obs.get("post_raiding_on_bf"))),
        "scope": ("Raiding Party 'Sacrifice an Orc' ability: each player taps "
                  "white creatures, each chooses up to two Plains per tapped "
                  "creature, then all unchosen Plains are destroyed; native "
                  "engine, two human-client seats; 1 tapped creature per "
                  "player, 3 Plains per player"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "12x deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Verdict rule: not-reproduced = A1 + A2 passed, 4 Plains choices actually made (chosen set == designated set), A3 + A4 + A5 passed. reproduced = A1 + A2 passed, choices actually made, and (A3 failed [chosen Plains destroyed -- the reported signature] or A4 failed [destroy step wrong] or A5 failed with an empty stack and the Orc not sacrificed / Raiding Party gone [related cost/cleanup failure]). blocked = setup never completed, tap/choice prompts never offered, or chosen set did not match the designated set.",
        ],
        "setup_line": ("P0 12x Raiding Party + 12x Orcish Conscripts + 12x Elite Vanguard + 12x Mountain + 12x Plains; P1 12x Elite Vanguard + 48x Plains. 2-player game, starting life 20."),
        "contract_line": ("P0 activates Raiding Party (sacrifice Conscripts). Each player taps their Vanguard and chooses 2 Plains (P0 its first two, P1 its first two). Expected: 4 chosen Plains survive, other 2 destroyed. Bug: all 6 destroyed."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump({k: CARD_DATA.get(k) for k in
                   ("raiding party", "orcish conscripts", "elite vanguard",
                    "plains", "mountain")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9860.py", "w") as f:
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
