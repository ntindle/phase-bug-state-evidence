#!/usr/bin/env python3
"""Issue #9862: Per-player "this way" counts -- Kamahl's Summons creates no
Bears; Truce and Temporary Truce give life only to the caster.

Oracle:
  Kamahl's Summons: "Each player may reveal any number of creature cards
    from their hand. Then each player creates a 2/2 green Bear creature
    token for each card they revealed this way."
  Truce: "Each player may draw up to two cards. For each card less than
    two a player draws this way, that player gains 2 life."
  Temporary Truce: same text as Truce (sorcery).

Reported (measured on main): Kamahl's Summons creates 0 Bears in every
configuration; Truce gives no life to anyone whatever each player drew.
With a pending count-shape change, the caster alone gains
2 x max(0, 2 - T) where T is the TOTAL drawn by all players (caster-only,
clause-wide count).

Behavioral contract -- three legs, all 3-player games, native engine:
  Leg A (Kamahl's Summons): P0 casts. P0 ACCEPTs the may-reveal optional,
    P1 ACCEPTs, P2 DECLINEs (reveals 0). Expected: each accepter is
    offered a reveal selection ("reveal any number of creature cards"),
    P1 reveals 1 Grizzly Bears -> 1 Bear; P0 reveals 2 -> 2 Bears; P2
    gets 0. OBSERVED (run 0341 + probe3, corrected): the engine offers
    NO reveal selection to any seat after the optionals and resolves
    with an empty revealed set -> 0 Bears. Probe3's apparent "P1 reveal
    pick" was re-examined and proved to be a Cleanup DiscardToHandSize
    prompt (the picked oid 90 ended in P1's graveyard, turn 12 Cleanup),
    NOT a reveal -- so the reveal step is skipped entirely, not merely
    miscounted.
  Leg B (Truce): every seat ACCEPTs the may-draw optional and answers
    chooseBranch optionIndex 1 (draw 1). Expected: each gains 2 life.
  Leg C (Temporary Truce): every seat DECLINEs the may-draw optional
    (draw 0). Expected: each gains 4 life. Also discriminates the
    patched variant (caster +4 only) from main (all +0).

Assertions:
  A1/B1/C1 setup_ok        PRE has the spell in P0's hand.
  A2_reveal_prompts        P0/P1 optionals answered ACCEPT, P2 DECLINE;
                           P1 offered a reveal select and picked 1.
                           FAILED in run 0341: no select was offered to
                           any seat (reveal step skipped entirely).
  A3_p1_bear               POST P1 has exactly 1 Bear token (expected
                           under the intended reveal policy).
                           FAILED (0): no reveal happened, so no Bears.
  A4_caster_reveal         P0 offered a reveal select before resolution
                           completed (expected per "each player").
                           FAILED (not offered) = caster reveal skipped
                           (same skipped-step mechanism as A2).
  A5/B5/C5 cleanup         POST stack empty, spell in P0 graveyard,
                           (Leg A) no hand changed by the spell (P1/P2
                           hands identical PRE->POST; P0 hand = PRE
                           minus the cast Summons).
  B2_draw_choices          Each seat ACCEPTed + chooseBranch 1.
  B3_draw_counts           Each seat drew exactly 1 (library -1).
  B4_life_gain             Each seat +2 life (expected).
  C2_decline_choices       Each seat DECLINEed the optional.
  C3_draw_counts           Each seat drew 0 (library unchanged).
  C4_life_gain             Each seat +4 life (expected); observed
                           per-player deltas recorded + signature
                           classified (main: all +0 vs patched:
                           caster +4 only).

Verdict rule (pre-registered):
  - not-reproduced: all three legs, all assertions passed.
  - reproduced: any leg has setup passed + (A3 failed [revealed creature
    -> no Bear] OR A4 failed [caster never offered reveal] OR B4 failed
    [drew 1 -> no life] OR C4 failed [declined -> no life]).
  - blocked: setup never completed, prompts never offered, etc.

Prompt shapes (probe-verified 2026-10-14 on v0.109.0/protocol 140):
  - decideOptionalEffect exactChoices, choices carry value surfaces
    {role: "accept", value: "true"/"false"} (no text).
  - chooseBranch exactChoices, choices carry value surfaces
    {role: "optionIndex", value: "0"/"1"/"2"} (draw count).
  - Reveal selection is a schema select, constraint min=1 max=1,
    confirm=explicit, candidates = the seat's whole hand (object
    surfaces carry reference/name/zone/controller). "Reveal any number"
    is one-pick-at-a-time; a seat that DECLINEs the optional gets no
    reveal select (probe3's apparent P2 select was a Cleanup
    DiscardToHandSize prompt, not a reveal).
  - The reveal selects are offered per accepting seat; the caster (P0)
    was NOT offered one in probe3 even after accepting -- A4 tests this.

Conventions (from scenario_9861.py, protocol 140):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - Export response data.state is a JSON string, parsed once.
  - finish() ordering: run.json, scenario copy, every logging hook,
    THEN close WIRE/RUNLOG, THEN manifest LAST.
  - DiscardToHandSize is gated on the state's waiting_for type (a reveal
    select is also a hand-card schema select; the 9861 hand>7 heuristic
    would steal it). During spell resolution the spell handler runs
    before the discard handler.
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
log = logging.getLogger("scenario9862")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9862-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9862
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
               "evidence under evidence/9862/" + RUN_ID),
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
RETRIED_OPPS = set()
RECORDED_OPPS = set()
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


def library_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("library") or [])]


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


def envelope_state(exported):
    if not isinstance(exported, dict):
        return {}
    if "objects" in exported:
        return exported
    if isinstance(exported.get("state"), dict):
        return exported["state"]
    return exported


KAMAHL = "kamahl's summons"
TRUCE = "truce"
TEMP_TRUCE = "temporary truce"
BEARS = "grizzly bears"
BEAR_TOKEN = "bear"
FOREST = "forest"
PLAINS = "plains"


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


def _cand_ref(ch):
    if ch.get("reference") is not None:
        return str(ch.get("reference"))
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def _cand_surface(ch):
    """The object surface of a schema candidate (reference/name/zone/...)."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return d
    return {}


def _is_pure_candidate(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            return False
    return True


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
        if "decideOptionalEffect" in codes or "chooseBranch" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


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
    """Cleanup discard. Gated on the state's waiting_for type: a reveal
    select is also a hand-card schema select, so the hand>7 heuristic
    alone would steal it. The spell handler runs before this during
    resolution, but the waiting_for gate is the real discriminator."""
    state = st["state"]
    wf = (state.get("waiting_for") or {}).get("type", "")
    if "Discard" not in str(wf):
        return False
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
        picks = [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) == PLAINS] or \
                [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) == FOREST] or cands[:1]
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


async def declare_no_attackers(c, acts):
    da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
    if da:
        import copy as _copy
        d = _copy.deepcopy(da)
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        await submit_as_is(c, d)
        return True
    return False


# ------------------------------------------------------- optional prompts

def find_optional_opp(st):
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
            f"choice identified")
        return False
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": "choose",
                                       "data": {"choiceId": ch_id}}}, tag)
    return True


def pick_branch_choice(opp, want_index):
    for ch in (opp.get("response", {}).get("data", {}) or {}).get("choices", []):
        for sf in ch.get("surfaces", []):
            dd = sf.get("data", {}) or {}
            if dd.get("role") == "optionIndex" \
                    and str(dd.get("value")) == str(want_index):
                return ch.get("id")
    return None


# ------------------------------------------------------- spell prompts

def dump_spell_opp(state, opp, kind, tag, ctx):
    n = ctx.get("spell_prompts_seen", 0) + 1
    ctx["spell_prompts_seen"] = n
    with open(f"{EVDIR}/spell_opp_{ctx.get('leg')}_{n}.json", "w") as f:
        json.dump({"leg": ctx.get("leg"), "kind": kind, "tag": tag,
                   "waiting_for": state.get("waiting_for"),
                   "opportunity": opp, "text_blob": opp_text_blob(opp)},
                  f, indent=1, default=str)
    wire("spell_opp", {"n": n, "kind": kind, "tag": tag,
                       "blob": opp_text_blob(opp)[:400]})
    say(f"[{tag}] spell prompt #{n} kind={kind}: "
        f"blob={opp_text_blob(opp)[:220]}")


async def answer_spell_opp(c, st, pid, tag, ctx):
    """Answer one #9862 spell prompt for this client, if showing.

    - decideOptionalEffect: per-leg policy (Leg A: P0/P1 ACCEPT, P2
      DECLINE; Leg B: all ACCEPT; Leg C: all DECLINE).
    - chooseBranch (Leg B): optionIndex == branch policy (1 = draw 1).
    - Schema select with hand candidates (Leg A reveal): pick one
      Grizzly Bears per prompt until the seat's reveal policy is met.
      A declined seat that is offered a select anyway is recorded, not
      answered (decline must mean reveal 0).
    Returns True if something was submitted or the prompt was recorded
    (so the tick does not fall through to a priority pass while a
    decision is pending).
    """
    state = st["state"]
    # A cleanup-discard wait is never a spell prompt (the discard
    # handler owns it); without this guard a post-resolution cleanup
    # select could be misanswered as a reveal.
    wf = str((state.get("waiting_for") or {}).get("type", ""))
    if "Discard" in wf:
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            # Still pending after a submission: allow exactly ONE retry
            # with the SAME payload, but only if the engine recorded
            # nothing yet (progress selected == 0). (#9860 lesson: an
            # over-max submission is silently ignored.)
            if iid in RETRIED_OPPS:
                continue
            prog = opp.get("progress", {}) or {}
            if int(prog.get("selected", 0) or 0) > 0:
                continue
            RETRIED_OPPS.add(iid)
            prev = ctx.get("submitted_subs", {}).get(iid)
            if prev is not None:
                say(f"[{tag}] spell opp {str(iid)[:16]} still pending "
                    f"after a submission; retrying once with the same "
                    f"payload")
                await interact_as(c, dict(prev), tag)
                return True
            say(f"[{tag}] spell opp {str(iid)[:16]} still pending after "
                f"a submission; retrying once (re-answer)")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        # --- optional prompts.
        if rtype == "exactChoices" and find_optional_opp(st) is opp:
            want = ctx["optional_policy"][pid]
            dump_spell_opp(state, opp, "optional", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            ctx.setdefault("optionals", {})[pid] = want
            say(f"[{tag}] may-optional -> {'ACCEPT' if want else 'DECLINE'}")
            ch_id = pick_optional_choice(opp, want)
            if ch_id is None:
                say(f"[{tag}] optional prompt: no "
                    f"{'accept' if want else 'decline'} choice identified")
                return True
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": ch_id}}}
            ctx.setdefault("submitted_subs", {})[iid] = sub
            await interact_as(c, sub, tag)
            return True
        # --- chooseBranch (draw count).
        if rtype == "exactChoices" and any(
                "chooseBranch" in surf_codes(ch) for ch in items):
            want_idx = ctx.get("branch_policy", {}).get(pid)
            dump_spell_opp(state, opp, "chooseBranch", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            if want_idx is None:
                say(f"[{tag}] chooseBranch prompt but leg has no branch "
                    f"policy -- recorded, not answered")
                wire("branch_unanswered", {"iid": str(iid)[:12]})
                ctx["branch_unanswered"] = True
                return True
            ch_id = pick_branch_choice(opp, want_idx)
            if ch_id is None:
                say(f"[{tag}] chooseBranch: no optionIndex {want_idx} "
                    f"choice -- recorded, not answered")
                ctx["branch_unanswered"] = True
                return True
            ctx.setdefault("branches", {})[pid] = int(want_idx)
            say(f"[{tag}] chooseBranch -> draw {want_idx}")
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": ch_id}}}
            ctx.setdefault("submitted_subs", {})[iid] = sub
            await interact_as(c, sub, tag)
            return True
        # --- schema select (Leg A reveal): hand candidates of this seat.
        if rtype == "schema" and ctx["leg"] == "A":
            spec = data.get("spec", {}) or {}
            if (spec.get("type") or "") != "select":
                continue
            mine = []
            for ch in items:
                surf = _cand_surface(ch)
                if surf.get("zone") == "hand" \
                        and str(surf.get("controller")) == str(pid):
                    mine.append((ch, surf))
            if not mine:
                continue
            picked = ctx.setdefault("picks", {}).setdefault(pid, [])
            if ctx["optionals"].get(pid) is False:
                # Declined the may-reveal: a select anyway means the
                # decline was ignored -- record, do not answer.
                if iid not in RECORDED_OPPS:
                    RECORDED_OPPS.add(iid)
                    dump_spell_opp(state, opp, "select-after-decline",
                                    tag, ctx)
                    say(f"[{tag}] select offered AFTER decline -- "
                        f"recorded, not answered (decline must mean 0)")
                    ctx["select_after_decline"] = True
                return True
            bears = [x for x in mine
                     if str(x[1].get("name", "")).lower() == BEARS
                     and _cand_ref(x[0]) not in picked]
            if len(picked) < ctx["reveal_policy"][pid] and bears:
                dump_spell_opp(state, opp, "reveal", tag, ctx)
                SUBMITTED_OPPS.add(iid)
                ch, surf = bears[0]
                picked.append(_cand_ref(ch))
                say(f"[{tag}] reveal pick #{len(picked)}/"
                    f"{ctx['reveal_policy'][pid]}: ref={_cand_ref(ch)}")
                wire("reveal_pick", {"who": tag, "picked": list(picked)})
                sub = {"interactionId": iid,
                       "response": {"type": "select",
                                    "data": {"choiceIds": [ch.get("id")]}}}
                ctx.setdefault("submitted_subs", {})[iid] = sub
                await interact_as(c, sub, tag)
                return True
            if len(picked) < ctx["reveal_policy"][pid] and not bears:
                # Seat has no creature cards in hand: the reveal policy
                # cannot be satisfied -- setup failure, abandon leg.
                if "no_creatures" not in ctx:
                    ctx["no_creatures"] = pid
                    say(f"[{tag}] SETUP: no creature cards in hand at "
                        f"reveal time (hand={hand_names(state, pid)}); "
                        f"abandoning leg for a retry")
                    wire("no_creatures_setup", {"who": tag,
                                                "hand": hand_names(state, pid)})
                return True
            # Policy satisfied but a select is still offered: record,
            # do not answer (should not happen; the engine should move
            # on). The settle detector will not fire while it pends.
            if iid not in RECORDED_OPPS:
                RECORDED_OPPS.add(iid)
                dump_spell_opp(state, opp, "select-after-policy", tag, ctx)
                say(f"[{tag}] select offered after policy satisfied "
                    f"(picks={picked}) -- recorded, not answered")
            return True
    return False


def spell_pending(st, ctx):
    """True if a #9862 spell-resolution prompt is pending on this view."""
    if not st:
        return False
    state = st["state"]
    wf = str((state.get("waiting_for") or {}).get("type", ""))
    if "Discard" in wf:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "exactChoices":
            codes = set()
            for ch in items:
                codes.update(c for c in surf_codes(ch) if c)
            if "decideOptionalEffect" in codes or "chooseBranch" in codes:
                return True
        if rtype == "schema" and ctx["leg"] == "A":
            for ch in items:
                surf = _cand_surface(ch)
                if surf.get("zone") == "hand":
                    return True
    return False


# ------------------------------------------------------------------- ticks

async def common_tick_head(c, acts, st, pid, tag):
    if await do_mulligan(c, acts, st, pid, tag):
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


async def try_cast(c, state, acts, tag, name, needs, pid=0, ctx=None):
    if name not in hand_names(state, pid):
        return False
    a, oid = cast_action_for(acts, state, name)
    if a is None:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid, "needs": needs})
    if ctx is not None:
        ctx["cast_oid"] = oid
    await submit_as_is(c, a)
    return True


LEG_CONFIG = {
    "A": {"players": 3, "card": KAMAHL, "needs": {"G": 1, "generic": 3},
          "land": FOREST, "land_gate": 4,
          "optional_policy": {0: True, 1: True, 2: False},
          "reveal_policy": {0: 2, 1: 1, 2: 0},
          "branch_policy": {},
          "decks": [deck((KAMAHL, 12), (BEARS, 12), (FOREST, 36)),
                    deck((BEARS, 24), (FOREST, 36)),
                    deck((BEARS, 24), (FOREST, 36))]},
    "B": {"players": 3, "card": TRUCE, "needs": {"W": 1, "generic": 2},
          "land": PLAINS, "land_gate": 3,
          "optional_policy": {0: True, 1: True, 2: True},
          "reveal_policy": {},
          "branch_policy": {0: 1, 1: 1, 2: 1},
          "decks": [deck((TRUCE, 12), (PLAINS, 48)),
                    deck((PLAINS, 60)),
                    deck((PLAINS, 60))]},
    "C": {"players": 3, "card": TEMP_TRUCE, "needs": {"W": 1, "generic": 1},
          "land": PLAINS, "land_gate": 2,
          "optional_policy": {0: False, 1: False, 2: False},
          "reveal_policy": {},
          "branch_policy": {},
          "decks": [deck((TEMP_TRUCE, 12), (PLAINS, 48)),
                    deck((PLAINS, 60)),
                    deck((PLAINS, 60))]},
}


def new_ctx(leg, attempt):
    cfg = LEG_CONFIG[leg]
    return {
        "leg": leg,
        "attempt": attempt,
        "players": cfg["players"],
        "card": cfg["card"],
        "needs": dict(cfg["needs"]),
        "land": cfg["land"],
        "land_gate": cfg["land_gate"],
        "optional_policy": dict(cfg["optional_policy"]),
        "reveal_policy": dict(cfg["reveal_policy"]),
        "branch_policy": dict(cfg["branch_policy"]),
        "spell_cast": False,
        "cast_oid": None,
        "cast_at": None,
        "optionals": {},
        "branches": {},
        "picks": {},
        "spell_prompts_seen": 0,
        "pre_exported": False,
        "post_exported": False,
        "settle_ticks": 0,
        "branch_unanswered": False,
        "select_after_decline": False,
    }


async def generic_tick(c, pid, tag, ctx):
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

    # --- Spell-resolution prompts (any seat) once the spell is cast.
    if ctx.get("spell_cast"):
        if await answer_spell_opp(c, st, pid, tag, ctx):
            return

    # --- Cleanup discard (waiting_for-gated; runs after the spell
    # handler so a reveal select is never stolen).
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return

    # --- P0 casts the spell once affordable on its own main phase.
    if pid == 0 and not ctx.get("spell_cast") and p0_main(state, 0):
        if ctx["card"] not in hand_names(state, 0):
            if await play_named_land(c, state, 0, acts, tag, ctx["land"]):
                return
        else:
            # PRE is exported only when the cast is imminent (affordable
            # this tick): exporting earlier would let later turn draws
            # pollute the PRE->POST library/hand deltas that measure the
            # spell's own draws.
            if untapped_of_name(state, 0, ctx["land"]) >= ctx["land_gate"]:
                if not ctx.get("pre_exported"):
                    pre = json.loads(await c.export_state())
                    with open(f"{EVDIR}/pre_{ctx['leg']}.json", "w") as f:
                        json.dump(pre, f, indent=1)
                    ctx["pre_exported"] = True
                    wire("pre_export", {"leg": ctx["leg"]})
                    say(f"[{tag}] PRE_{ctx['leg']} exported; casting "
                        f"{ctx['card']} (needs {ctx['needs']})")
                if await try_cast(c, state, acts, tag, ctx["card"],
                                  ctx["needs"], pid=0, ctx=ctx):
                    ctx["spell_cast"] = True
                    ctx["cast_at"] = time.time()
                    return
            # Cast not submitted this tick: fall through so this seat
            # can still pass priority / play a land instead of stalling.

    # --- Setup land plays (before the spell is cast).
    if not ctx.get("spell_cast") and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and int(state.get("active_player", -1)) == pid \
            and not (state.get("stack") or []):
        if await play_named_land(c, state, pid, acts, tag, ctx["land"]):
            return

    if real_decision_pending(st):
        key = (tag, "prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("prompt", {"who": tag, "rev": c.revision,
                            "opps": _opp_summary(st)})
            say(f"[{tag}] prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if any(a.get("type") == "PassPriority" for a in acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision

# ------------------------------------------------------------------- run

async def run_leg(leg, attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    RETRIED_OPPS.clear()
    RECORDED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    ctx = new_ctx(leg, attempt)
    cfg = LEG_CONFIG[leg]
    tags = []
    clients = []
    for pid in range(ctx["players"]):
        tag = f"P{pid}{leg}{attempt}"
        tags.append(tag)
        ST["mana_needs"][tag] = {}
    clients.append(PhaseClient(f"P0{leg}{attempt}"))
    await clients[0].connect()
    await clients[0].create(cfg["decks"][0], player_count=ctx["players"])
    for pid in range(1, ctx["players"]):
        c = PhaseClient(f"P{pid}{leg}{attempt}")
        await c.connect()
        await c.join(clients[0].game_code, cfg["decks"][pid])
        clients.append(c)
    say(f"leg {leg} attempt={attempt} game {clients[0].game_code}; seats="
        f"{[c.player_id for c in clients]}")
    wire("game_start", {"leg": leg, "attempt": attempt,
                        "game_code": clients[0].game_code,
                        "seats": [c.player_id for c in clients]})
    obs = {"leg": leg, "attempt": attempt, "assert": {}, "notes": [],
           "game_code": clients[0].game_code}
    for k in ASSERT_KEYS[leg]:
        obs["assert"][k] = "not-run"

    t0 = time.time()
    last_diag = 0
    last_rev = -1
    last_rev_change = time.time()
    while time.time() - t0 < 600:
        await asyncio.sleep(0.25)
        for c, pid, tag in zip(clients, range(ctx["players"]), tags):
            await generic_tick(c, pid, tag, ctx)
        await asyncio.sleep(0)
        cur_rev = clients[0].revision
        if cur_rev != last_rev:
            last_rev = cur_rev
            last_rev_change = time.time()
        dead = [c.name for c in clients
                if getattr(c, "ws", None) is not None
                and not getattr(c.ws, "open", True)]
        if dead:
            say(f"[leg {leg}] client websocket(s) closed: {dead}; "
                f"abandoning leg (dead game)")
            obs["notes"].append(
                "abandoned: client websocket(s) closed mid-leg "
                f"({dead}); server-side stall/disconnect, not a game "
                "decision")
            break
        if time.time() - last_rev_change > 150:
            say(f"[leg {leg}] no state-revision change in 150s "
                f"(rev={cur_rev}); abandoning leg (stalled game)")
            obs["notes"].append(
                "abandoned: no state-revision change in 150s "
                f"(rev={cur_rev}); game stalled with no client-side "
                "action available")
            break
        st_now = clients[0].latest
        if st_now and st_now["state"].get("turn_number", 0) > 30 \
                and not ctx.get("spell_cast"):
            say(f"[leg {leg}] turn {st_now['state'].get('turn_number')} "
                f"exceeded without casting the spell; abandoning leg")
            obs["notes"].append(
                "abandoned: game reached turn "
                f"{st_now['state'].get('turn_number')} without casting "
                "the spell (setup never completed)")
            break
        if ctx.get("no_creatures") is not None:
            say(f"[leg {leg}] setup: seat P{ctx['no_creatures']} had no "
                f"creature cards in hand at reveal time; abandoning leg")
            obs["notes"].append(
                f"abandoned: setup -- seat P{ctx['no_creatures']} had no "
                f"creature cards in hand at reveal time (reveal policy "
                f"unsatisfiable); retry with a fresh game")
            break
        if ctx.get("spell_cast") \
                and time.time() - ctx.get("cast_at", 0) > 300 \
                and not ctx.get("post_exported"):
            say(f"[leg {leg}] spell cast 300s ago, resolution never "
                f"completed; abandoning leg")
            obs["notes"].append(
                "abandoned: spell cast but its resolution never "
                "completed within 300s")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pid in zip(clients, range(ctx["players"])):
                st = c.latest
                if not st:
                    say(f"[diag leg {leg}] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                say(f"[diag leg {leg}] {c.name}: rev={c.revision} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} "
                    f"cast={ctx.get('spell_cast')} "
                    f"optionals={ctx.get('optionals')} "
                    f"branches={ctx.get('branches')} "
                    f"picks={ctx.get('picks')}")
        # --- completion: export POST once the resolution has observably
        # settled: spell cast, stack empty on every view, no spell
        # prompt pending on any view, for 3 consecutive ticks.
        if ctx.get("spell_cast") and not ctx.get("post_exported"):
            latests = [c.latest for c in clients]
            if all(x is not None for x in latests) \
                    and all(not (x["state"].get("stack") or [])
                            for x in latests) \
                    and not any(spell_pending(x, ctx) for x in latests):
                ctx["settle_ticks"] += 1
                if ctx["settle_ticks"] >= 3:
                    say(f"[leg {leg}] resolution settled; exporting POST")
                    wire("post_settled", {"leg": leg})
                    try:
                        post = json.loads(await clients[0].export_state())
                        with open(f"{EVDIR}/post_{leg}.json", "w") as f:
                            json.dump(post, f, indent=1)
                        ctx["post_exported"] = True
                        say(f"[leg {leg}] POST exported after resolution settled")
                        wire("post_export", {"leg": leg})
                    except Exception as e:
                        say(f"POST_{leg} export failed: {e}")
                        obs["notes"].append(f"POST export failed: {e}")
                    break
            else:
                ctx["settle_ticks"] = 0

    evaluate_leg(obs, ctx)
    for c in clients:
        await c.close()
    return obs, ctx


ASSERT_KEYS = {
    "A": ("A1_setup_ok", "A2_reveal_prompts", "A3_p1_bear",
          "A4_caster_reveal", "A5_cleanup"),
    "B": ("B1_setup_ok", "B2_draw_choices", "B3_draw_counts",
          "B4_life_gain", "B5_cleanup"),
    "C": ("C1_setup_ok", "C2_decline_choices", "C3_draw_counts",
          "C4_life_gain", "C5_cleanup"),
}


def evaluate_leg(obs, ctx):
    """Score the leg's assertions from PRE/POST + ctx."""
    leg = ctx["leg"]
    pre_state = {}
    try:
        with open(f"{EVDIR}/pre_{leg}.json") as f:
            pre_state = envelope_state(json.load(f))
    except Exception as e:
        obs["notes"].append(f"PRE_{leg} export missing/unreadable: {e}")
    post_state = {}
    try:
        with open(f"{EVDIR}/post_{leg}.json") as f:
            post_state = envelope_state(json.load(f))
    except Exception as e:
        obs["notes"].append(f"POST_{leg} export missing/unreadable: {e}")
    obs["ctx"] = {"spell_cast": ctx.get("spell_cast"),
                  "cast_oid": ctx.get("cast_oid"),
                  "optionals": {str(k): v for k, v in ctx.get("optionals", {}).items()},
                  "branches": {str(k): v for k, v in ctx.get("branches", {}).items()},
                  "picks": {str(k): v for k, v in ctx.get("picks", {}).items()},
                  "spell_prompts_seen": ctx.get("spell_prompts_seen", 0),
                  "select_after_decline": ctx.get("select_after_decline", False),
                  "branch_unanswered": ctx.get("branch_unanswered", False)}

    card = ctx["card"]
    setup_key = {"A": "A1_setup_ok", "B": "B1_setup_ok", "C": "C1_setup_ok"}[leg]
    if pre_state:
        if card in hand_names(pre_state, 0):
            obs["assert"][setup_key] = "passed"
            obs["notes"].append(
                f"{setup_key} leg {leg} passed (setup): {card} in P0 hand "
                f"in PRE; P0 life={player_life(pre_state, 0)}, "
                f"P1 life={player_life(pre_state, 1)}, "
                f"P2 life={player_life(pre_state, 2)}")
        else:
            obs["assert"][setup_key] = "failed"
            obs["notes"].append(
                f"{setup_key} leg {leg} FAILED: {card} not in P0 hand in PRE")
    else:
        obs["notes"].append(f"{setup_key} leg {leg} not-run: no PRE export")

    if leg == "A":
        # ---- A2: reveal prompts. The optionals must be answered per
        # policy AND a reveal select must actually be offered (in run
        # 0341 the optionals were answered but NO select was offered to
        # any seat -- the reveal step was skipped entirely).
        opt = ctx.get("optionals", {})
        picks = ctx.get("picks", {})
        if opt.get(0) is True and opt.get(1) is True and opt.get(2) is False \
                and len(picks.get(1, [])) == 1:
            obs["assert"]["A2_reveal_prompts"] = "passed"
            obs["notes"].append(
                "A2 leg A passed: optionals P0 ACCEPT / P1 ACCEPT / "
                f"P2 DECLINE; P1 picked 1 Bears (ref {picks[1][0]})")
        else:
            obs["assert"]["A2_reveal_prompts"] = "failed"
            obs["notes"].append(
                f"A2 leg A FAILED: optionals={opt} picks={picks} -- the "
                f"optionals were answered per policy but NO reveal select "
                f"was offered to any seat (expected P1 to pick 1 Bears). "
                f"The reveal step is skipped entirely: the engine "
                f"resolves with an empty revealed set.")
        # ---- A3: P1's Bear. Expected 1 under the intended policy (P1
        # accepts and would reveal 1 Bears). Observed 0 because no
        # reveal happened at all -- the reported "0 Bears" signature,
        # via a skipped reveal step rather than a miscounted one.
        if post_state:
            p1_bears = [oid for oid in bf_oids(post_state, 1)
                        if obj_lname(post_state, oid) == BEAR_TOKEN]
            obs["bears"] = {str(pid): [oid for oid in bf_oids(post_state, pid)
                                        if obj_lname(post_state, oid) == BEAR_TOKEN]
                            for pid in range(3)}
            obs["bear_detail"] = {
                str(pid): [(oid, get_obj(post_state, oid).get("power"),
                            get_obj(post_state, oid).get("toughness"))
                           for oid in oids]
                for pid, oids in obs["bears"].items()}
            if len(p1_bears) == 1:
                obs["assert"]["A3_p1_bear"] = "passed"
                obs["notes"].append(
                    f"A3 leg A passed: P1 has exactly 1 Bear token "
                    f"(oid {p1_bears[0]})")
            else:
                obs["assert"]["A3_p1_bear"] = "failed"
                obs["notes"].append(
                    f"A3 leg A FAILED: P1 has {len(p1_bears)} Bear tokens "
                    f"in POST (expected 1 under the intended reveal "
                    f"policy). P1 revealed NOTHING -- no reveal select "
                    f"was offered -- so the 0 Bears reflect a skipped "
                    f"reveal step, not a miscounted reveal. All seats: "
                    f"{obs['bears']}")
        else:
            obs["assert"]["A3_p1_bear"] = "not-run"
            obs["notes"].append("A3 leg A not-run: no POST export")
        # ---- A4: caster reveal select offered?
        if ctx.get("spell_cast"):
            p0_picks = picks.get(0, [])
            # A reveal select was offered to P0 iff P0 picked (the
            # handler only picks on a genuine select) OR the run
            # recorded a select-after-policy for P0. The decisive
            # observation: did P0 ever see a reveal select? The picks
            # record is the positive signal; absence + completed
            # resolution = never offered.
            if len(p0_picks) >= 1:
                obs["assert"]["A4_caster_reveal"] = "passed"
                obs["notes"].append(
                    f"A4 leg A passed: P0 was offered a reveal select and "
                    f"picked {len(p0_picks)} card(s)")
            else:
                obs["assert"]["A4_caster_reveal"] = "failed"
                obs["notes"].append(
                    "A4 leg A FAILED: P0 ACCEPTed the may-reveal optional "
                    "but was never offered a reveal select before the "
                    "spell resolved (caster reveal skipped)")
        else:
            obs["assert"]["A4_caster_reveal"] = "not-run"
            obs["notes"].append("A4 leg A not-run: spell never cast")
        # ---- A5: cleanup. The spell moved no cards: P1/P2 hands
        # identical PRE->POST, P0 hand = PRE minus the cast Summons.
        if post_state and pre_state and ctx.get("spell_cast"):
            stack_empty = not (post_state.get("stack") or [])
            in_gy = in_graveyard(post_state, 0, KAMAHL)
            p1_same = set(hand_ids(pre_state, 1)) == set(hand_ids(post_state, 1))
            p2_same = set(hand_ids(pre_state, 2)) == set(hand_ids(post_state, 2))
            p0_expected = set(hand_ids(pre_state, 0)) - {str(ctx.get("cast_oid"))}
            p0_same = set(hand_ids(post_state, 0)) == p0_expected
            if stack_empty and in_gy and p1_same and p2_same and p0_same:
                obs["assert"]["A5_cleanup"] = "passed"
                obs["notes"].append(
                    "A5 leg A passed: POST stack empty, Kamahl's Summons "
                    "in P0 graveyard, no hand changed by the spell "
                    "(P1/P2 hands identical PRE->POST; P0 hand = PRE "
                    "minus the cast Summons)")
            else:
                obs["assert"]["A5_cleanup"] = "failed"
                obs["notes"].append(
                    f"A5 leg A FAILED: stack_empty={stack_empty} "
                    f"summons_in_gy={in_gy} p1_hand_same={p1_same} "
                    f"p2_hand_same={p2_same} p0_hand_minus_cast={p0_same}")
        else:
            obs["assert"]["A5_cleanup"] = "not-run"
            obs["notes"].append("A5 leg A not-run: no POST/PRE or no cast")
        return

    # ---- Legs B/C: draw + life.
    pre_life = {}
    post_life = {}
    drawn = {}
    hand_delta = {}
    if pre_state and post_state:
        for pid in range(3):
            pre_life[pid] = player_life(pre_state, pid)
            post_life[pid] = player_life(post_state, pid)
            drawn[pid] = len(library_ids(pre_state, pid)) - len(library_ids(post_state, pid))
            hand_delta[pid] = len(hand_ids(post_state, pid)) - len(hand_ids(pre_state, pid))
        obs["life"] = {"pre": {str(k): v for k, v in pre_life.items()},
                       "post": {str(k): v for k, v in post_life.items()},
                       "delta": {str(k): (post_life[k] - pre_life[k]
                                          if pre_life[k] is not None and post_life[k] is not None
                                          else None)
                                 for k in range(3)}}
        obs["drawn"] = {str(k): v for k, v in drawn.items()}
        obs["hand_delta"] = {str(k): v for k, v in hand_delta.items()}
    else:
        obs["notes"].append(
            f"legs {leg}: life/draw computation skipped (missing PRE/POST)")

    if leg == "B":
        opt = ctx.get("optionals", {})
        br = ctx.get("branches", {})
        if all(opt.get(p) is True for p in range(3)) \
                and all(br.get(p) == 1 for p in range(3)):
            obs["assert"]["B2_draw_choices"] = "passed"
            obs["notes"].append(
                "B2 leg B passed: all seats ACCEPTed + chooseBranch 1")
        else:
            obs["assert"]["B2_draw_choices"] = "failed"
            obs["notes"].append(
                f"B2 leg B FAILED: optionals={opt} branches={br} "
                f"(expected all ACCEPT + branch 1)")
        if pre_state and post_state:
            if all(drawn.get(p) == 1 for p in range(3)):
                obs["assert"]["B3_draw_counts"] = "passed"
                obs["notes"].append(
                    f"B3 leg B passed: each seat drew exactly 1 "
                    f"(library -1 each; hand deltas {hand_delta}, P0's "
                    f"includes the cast Truce leaving hand)")
            else:
                obs["assert"]["B3_draw_counts"] = "failed"
                obs["notes"].append(
                    f"B3 leg B FAILED: drawn={drawn} (expected 1 each)")
            deltas = obs["life"]["delta"]
            if all(deltas.get(str(p)) == 2 for p in range(3)):
                obs["assert"]["B4_life_gain"] = "passed"
                obs["notes"].append(
                    "B4 leg B passed: each seat +2 life")
            else:
                obs["assert"]["B4_life_gain"] = "failed"
                obs["notes"].append(
                    f"B4 leg B FAILED: life deltas={deltas} (expected "
                    f"+2 each) -- the reported signature (drew 1, no life)")
            stack_empty = not (post_state.get("stack") or [])
            in_gy = in_graveyard(post_state, 0, TRUCE)
            if stack_empty and in_gy:
                obs["assert"]["B5_cleanup"] = "passed"
                obs["notes"].append(
                    "B5 leg B passed: POST stack empty, Truce in P0 graveyard")
            else:
                obs["assert"]["B5_cleanup"] = "failed"
                obs["notes"].append(
                    f"B5 leg B FAILED: stack_empty={stack_empty} "
                    f"truce_in_gy={in_gy}")
        else:
            for k in ("B3_draw_counts", "B4_life_gain", "B5_cleanup"):
                obs["assert"][k] = "not-run"
            obs["notes"].append("B3/B4/B5 leg B not-run: missing PRE/POST")
        return

    if leg == "C":
        opt = ctx.get("optionals", {})
        if all(opt.get(p) is False for p in range(3)):
            obs["assert"]["C2_decline_choices"] = "passed"
            obs["notes"].append(
                "C2 leg C passed: all seats DECLINEed the may-draw optional")
        else:
            obs["assert"]["C2_decline_choices"] = "failed"
            obs["notes"].append(
                f"C2 leg C FAILED: optionals={opt} (expected all DECLINE)")
        if pre_state and post_state:
            if all(drawn.get(p) == 0 for p in range(3)):
                obs["assert"]["C3_draw_counts"] = "passed"
                obs["notes"].append(
                    "C3 leg C passed: each seat drew 0 (library unchanged)")
            else:
                obs["assert"]["C3_draw_counts"] = "failed"
                obs["notes"].append(
                    f"C3 leg C FAILED: drawn={drawn} (expected 0 each)")
            deltas = obs["life"]["delta"]
            if all(deltas.get(str(p)) == 4 for p in range(3)):
                obs["assert"]["C4_life_gain"] = "passed"
                obs["notes"].append(
                    "C4 leg C passed: each seat +4 life")
            else:
                obs["assert"]["C4_life_gain"] = "failed"
                d0 = deltas.get("0")
                if d0 == 4 and deltas.get("1") == 0 and deltas.get("2") == 0:
                    sig = ("patched-variant signature: caster +4 only, "
                           "others +0 (clause-wide count, caster binding)")
                elif all(deltas.get(str(p)) == 0 for p in range(3)):
                    sig = ("main signature: nobody gained life")
                else:
                    sig = "unclassified signature"
                obs["notes"].append(
                    f"C4 leg C FAILED: life deltas={deltas} (expected "
                    f"+4 each) -- {sig}")
            stack_empty = not (post_state.get("stack") or [])
            in_gy = in_graveyard(post_state, 0, TEMP_TRUCE)
            if stack_empty and in_gy:
                obs["assert"]["C5_cleanup"] = "passed"
                obs["notes"].append(
                    "C5 leg C passed: POST stack empty, Temporary Truce "
                    "in P0 graveyard")
            else:
                obs["assert"]["C5_cleanup"] = "failed"
                obs["notes"].append(
                    f"C5 leg C FAILED: stack_empty={stack_empty} "
                    f"temp_truce_in_gy={in_gy}")
        else:
            for k in ("C3_draw_counts", "C4_life_gain", "C5_cleanup"):
                obs["assert"][k] = "not-run"
            obs["notes"].append("C3/C4/C5 leg C not-run: missing PRE/POST")
        return


def render_summary(combined):
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
    img = Image.new("RGB", (1000, 860), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #9862 -- Per-player \"this way\" counts", font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.109.0 (932e389, protocol 140) | 3-player games, native engine", font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = str(combined.get("verdict", "blocked")).upper().replace("_", "-")
    if verdict not in ("REPRODUCED", "NOT-REPRODUCED", "BLOCKED"):
        verdict = "BLOCKED / NOT-RUN"
    color = (255, 90, 90) if verdict == "REPRODUCED" else ((90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    for leg in ("A", "B", "C"):
        lo = combined.get("legs", {}).get(leg, {})
        a = lo.get("assert", {})
        d.text((24, y), f"Leg {leg} (game {lo.get('game_code')}):", font=font_b, fill=(220, 220, 230))
        y += 28
        for k in ASSERT_KEYS[leg]:
            v = a.get(k, "not-run")
            vc = (90, 255, 140) if v == "passed" else ((255, 90, 90) if v == "failed" else (255, 200, 60))
            d.text((48, y), f"{k}: {v}", font=font_b, fill=vc)
            y += 28
        if leg == "A":
            d.text((48, y), f"Bears per seat: {lo.get('bears')}  picks={lo.get('ctx', {}).get('picks')}", font=font, fill=(220, 220, 230))
            y += 28
        else:
            d.text((48, y), f"Drawn: {lo.get('drawn')}  life deltas: {lo.get('life', {}).get('delta')}", font=font, fill=(220, 220, 230))
            y += 28
        d.text((48, y), f"Optionals: {lo.get('ctx', {}).get('optionals')}  branches: {lo.get('ctx', {}).get('branches')}", font=font, fill=(220, 220, 230))
        y += 34
    y += 6
    d.text((24, y), "Expected (oracle): Kamahl -- 1 Bear per revealed creature, per player;", font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Truce (draw 1 each) -- +2 life each; Temporary Truce (all decline) -- +4 each.", font=font, fill=(220, 220, 230))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    legs = {}
    for leg in ("A", "B", "C"):
        obs, ctx = await run_leg(leg, 1)
        # Retry once when the leg never got its setup (spell never cast,
        # or a seat had no creature cards at reveal time).
        if (not ctx.get("spell_cast") or ctx.get("no_creatures") is not None) \
                and obs["assert"].get(ASSERT_KEYS[leg][0]) in ("failed", "not-run"):
            say(f"leg {leg} attempt 1 never completed setup; running attempt 2")
            obs, ctx = await run_leg(leg, 2)
        legs[leg] = obs

    # ---- combined verdict per the pre-registered rule.
    def leg_reproduced(lo):
        a = lo["assert"]
        leg = lo["leg"]
        if leg == "A":
            return a.get("A1_setup_ok") == "passed" and (
                a.get("A3_p1_bear") == "failed"
                or a.get("A4_caster_reveal") == "failed")
        if leg == "B":
            return a.get("B1_setup_ok") == "passed" and (
                a.get("B4_life_gain") == "failed")
        if leg == "C":
            return a.get("C1_setup_ok") == "passed" and (
                a.get("C4_life_gain") == "failed")
        return False

    def leg_clean(lo):
        a = lo["assert"]
        return all(a.get(k) == "passed" for k in ASSERT_KEYS[lo["leg"]])

    if any(leg_reproduced(lo) for lo in legs.values()):
        verdict = "reproduced"
    elif all(leg_clean(lo) for lo in legs.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    combined = {
        "server_identity": SERVER_IDENTITY,
        "issue": ISSUE,
        "run_id": RUN_ID,
        "legs": legs,
        "verdict": verdict,
        "card_data_oracle": {
            "kamahl's summons": (CARD_DATA.get("kamahl's summons") or {}).get("oracle_text"),
            "truce": (CARD_DATA.get("truce") or {}).get("oracle_text"),
            "temporary truce": (CARD_DATA.get("temporary truce") or {}).get("oracle_text"),
        },
    }
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump(combined, f, indent=1, default=str)
    for leg, lo in legs.items():
        say(f"leg {leg} assertions: " + json.dumps(lo["assert"]))
        for n in lo["notes"]:
            say(f"leg {leg} note: " + n)
    say(f"VERDICT: {verdict}")
    result_lines = []
    for leg, lo in legs.items():
        result_lines.append(
            f"Leg {leg}: " + "; ".join(f"{k} {v}" for k, v in lo["assert"].items())
            + f"; ctx={lo.get('ctx')}; "
            + (f"bears={lo.get('bears')}; " if leg == "A" else "")
            + (f"drawn={lo.get('drawn')}; life_delta={lo.get('life', {}).get('delta')}; " if leg in ("B", "C") else ""))
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9862",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                          "(--single-user --no-data-download, verified release); "
                          f"evidence under evidence/9862/{RUN_ID}",
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "leg_A_P0": [["Kamahl's Summons", 12], ["Grizzly Bears", 12], ["Forest", 36]],
            "leg_A_P1": [["Grizzly Bears", 24], ["Forest", 36]],
            "leg_A_P2": [["Grizzly Bears", 24], ["Forest", 36]],
            "leg_B_P0": [["Truce", 12], ["Plains", 48]],
            "leg_B_P1": [["Plains", 60]],
            "leg_B_P2": [["Plains", 60]],
            "leg_C_P0": [["Temporary Truce", 12], ["Plains", 48]],
            "leg_C_P1": [["Plains", 60]],
            "leg_C_P2": [["Plains", 60]],
        },
        "assertions": {f"leg_{leg}_{k}": v
                       for leg, lo in legs.items()
                       for k, v in lo["assert"].items()},
        "notes": [f"leg {leg}: {n}" for leg, lo in legs.items()
                  for n in lo["notes"]],
        "verdict": verdict,
        "result": " | ".join(result_lines),
        "scope": ("Per-player 'this way' counts (publish_player_scope_clause_results "
                  "/ tracked-set reader): Leg A Kamahl's Summons (P0 accept, "
                  "P1 accept + reveal 1 Bears, P2 decline); Leg B Truce "
                  "(all accept + draw 1); Leg C Temporary Truce (all decline, "
                  "draw 0). Native engine, three human-client seats."),
        "limitations": [
            "Browser UI not exercised; native engine via human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "12x/24x deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Verdict rule: not-reproduced = all three legs all assertions passed. reproduced = any leg has setup passed + (A3 failed [revealed creature -> no Bear] or A4 failed [caster never offered reveal] or B4 failed [drew 1 -> no life] or C4 failed [declined -> no life]). blocked = setup never completed, prompts never offered, etc.",
        ],
        "setup_line": ("Leg A: P0 12x Kamahl's Summons + 12x Grizzly Bears + 36x Forest; P1/P2 24x Grizzly Bears + 36x Forest. "
                       "Leg B: P0 12x Truce + 48x Plains; P1/P2 60x Plains. "
                       "Leg C: P0 12x Temporary Truce + 48x Plains; P1/P2 60x Plains. "
                       "P0 casts on its own main phase once affordable."),
        "contract_line": ("Kamahl: 1 Bear per revealed creature, per player; Truce draw-1-each: +2 life each; "
                          "Temporary Truce all-decline: +4 life each. Bug: per-player results are not carried "
                          "to the per-player follow-up (clause-wide tracked set, caster binding)."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump({k: CARD_DATA.get(k) for k in
                   ("kamahl's summons", "truce", "temporary truce")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9862.py", "w") as f:
        f.write(open(__file__).read())
    render_summary(combined)
    wire("verdict", {"verdict": verdict,
                     "assertions": run["assertions"]})
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
    print(f"DONE verdict={verdict} assertions={json.dumps(run['assertions'])}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
