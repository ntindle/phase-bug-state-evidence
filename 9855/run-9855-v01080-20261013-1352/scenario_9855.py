#!/usr/bin/env python3
"""Issue #9855: Personal Decoy -- the leaves-the-battlefield exile
replacement and "You can't be attacked" are dropped with no unimplemented
marker.

Oracle (Personal Decoy): "Personal Decoy enters with a number of loyalty
counters on it equal to your life total. If it would leave the battlefield,
exile it instead of putting it anywhere else. You can't be attacked.
[+1]: You gain 1 life. [-4]: Draw a card."

Report (phase-rs/phase#9855, opened 2026-10-12, open,
bug/area:parser/priority:p2-wrong-game-result): read from parse output --
only the loyalty counters and loyalty abilities are present; the two
sentences are swallowed without a warning, so the card reads as supported.
Where: line splitting for the multi-sentence permanent-static paragraph in
parser/oracle.rs.

Behavioral contract (per playbook: test the reported OUTCOME; this is a
parser-subsystem report, so the decisive contract is parse-level, with
game-level confirmation of the wrong-game-result):
  Parse (decisive): in the pinned v0.108.0 card-data.json, Personal Decoy
      must carry (a) a leaves-the-battlefield exile replacement and (b) a
      static preventing attacks against its controller -- or the card must
      carry an unimplemented marker for the dropped sentences. Bug: the
      parsed entry has only the two loyalty activated abilities plus the
      ETB loyalty-counter replacement; the exile replacement and the
      can't-be-attacked static are absent, and no Unimplemented marker or
      parse warning covers them.
  Game (confirming):
      - with the Decoy on the battlefield under P0, P1's declare-attackers
        opportunity must NOT offer P0 as an attack target ("You can't be
        attacked"). Under the bug, P0 is offered.
      - P1 casts Hero's Downfall targeting the Decoy; the Decoy must be
        exiled ("If it would leave the battlefield, exile it instead").
        Under the bug, it goes to the graveyard.

Assertions:
  A1 setup_ok            game created, both seats joined, P0 cast Personal
                         Decoy and it is on the battlefield with loyalty
                         counters equal to P0's life total
  A2 exile_replacement   FAILED = the reported bug: no LTB-exile replacement
                         in the parsed entry; passed = present
  A3 cant_be_attacked     FAILED = the reported bug: no can't-be-attacked
                         static in the parsed entry; passed = present
  A4 no_unimplemented     FAILED = the reported bug: no Unimplemented marker
                         or parse warning covers the dropped sentences (the
                         card reads as supported); passed = a marker exists
  A5 oracle_intact        passed = the pinned oracle text still contains both
                         sentences (the drop is in the lower, not the data)
  A6 attack_target_offer  FAILED = P1's declare-attackers opportunity offered
                         P0 (the Decoy's controller) as an attack target
                         while the Decoy was on the battlefield; passed = P0
                         not offered; not-run = the opportunity was never
                         observed with the Decoy out and an able attacker
  A7 destroy_exiles       FAILED = the destroyed Decoy went to the graveyard
                         instead of exile; passed = exiled; not-run = the
                         destroy leg never completed

Verdict:
  reproduced      A2 failed (no LTB-exile replacement -- the reported
                  outcome), corroborated by A3/A4 and the game legs
  not-reproduced  A2 passed (the replacement is present)
  blocked         parse evidence unreadable or the fixture never reached

Driver conventions (verified scenario_9854_01080.py, protocol 134):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - gate on waiting_for (viewer_interaction)
  - exact card-data names; export response data.state is a JSON string parsed once
  - MulliganDecision via legacy Action (Keep); priority-gated passes;
    sleep(0) yield before leg evaluation; vi candidate references are strings
    (normalize with str() before oid comparisons)
  - real_decision_pending filters None surface codes (9845 lesson)
  - genuine card-choice candidates have NO action surface (9842 lesson)
  - runtime state objects use "card_types" (plural); card-data.json uses
    "card_type" (singular) (9847 lesson)
  - DeclareAttackers vi is a schema/relations opportunity: attacker
    candidates (role attacker) with edges to attackTarget candidates
    (role attackTarget, type player/seat or object reference)
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
log = logging.getLogger("scenario9855")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9855-v01080-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9855
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.108.0",
    "build_commit": "b8fa1d3",
    "protocol_version": 134,
    "mode": "Full",
    "server_binary_sha256": "83a3d30919ea7d56b98054048566d105116523abb2b88e5e4478c40ecb00c1f7",
    "card_data_sha256": "2f65a5829b509ad69a32110ff9a1361363e7f97f24c41c6a53d4ae6850e8e813",
    "draft_pools_sha256": "8c9945e3c7d511ebb1ca9ff37bee5b1f58144c1c4b5d6bed52f81d6d050c7297",
    "signature_verified": True,
    "signature_note": ("v0.108.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.108.0 (published "
               "2026-10-12T21:10:34Z) == pinned release dir; ServerHello "
               "0.108.0/b8fa1d3/protocol 134 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "v0.108.0 server on 127.0.0.1:9374 (backfill-owned, "
               "--single-user --no-data-download, verified release; reused "
               "by this run); scenario games created via "
               "CreateGameWithSettings; evidence under evidence/9855/" + RUN_ID),
}


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(b):
    return hashlib.sha256(b).hexdigest()


for _f, _k in (("server/releases/v0.108.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.108.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.108.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against the ledger-pinned v0.108.0 digests",
      flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.108.0/data/card-data.json"))


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
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_names(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_names(state, pid):
    return [obj_lname(state, o) for o in bf_oids(state, pid)]


def named_oids_anywhere(state, lname):
    return [oid for oid, o in (state.get("objects") or {}).items()
            if str(o.get("base_name") or o.get("name") or "").lower() == lname]


def obj_zone(state, oid):
    return get_obj(state, oid).get("zone")


def is_land(o):
    return "Land" in (((o.get("card_types") or {}).get("core_types")) or [])


def is_creature(o):
    return "Creature" in (((o.get("card_types") or {}).get("core_types")) or [])


DECOY = "personal decoy"
BEARS = "grizzly bears"
DOWNFALL = "hero's downfall"
PLAINS = "plains"
ISLAND = "island"
FOREST = "forest"
SWAMP = "swamp"


def decoy_oids(state, pid=0):
    return [o for o in bf_oids(state, pid)
            if obj_lname(state, o) == DECOY]


def counters_named(state, oid, needle):
    """Best-effort read of counters whose name contains `needle`
    (case-insensitive); returns (total, raw)."""
    o = get_obj(state, oid)
    raw = o.get("counters")
    total = 0
    if isinstance(raw, dict):
        for k, v in raw.items():
            if needle in str(k).lower():
                try:
                    total += int(v)
                except (TypeError, ValueError):
                    total += 1
    elif isinstance(raw, list):
        for c in raw:
            nm = c.get("name") if isinstance(c, dict) else str(c)
            if needle in str(nm).lower():
                try:
                    total += int(c.get("count", 1)) if isinstance(c, dict) else 1
                except (TypeError, ValueError):
                    total += 1
    return total, raw


def stack_spell_oid(state, lname):
    for e in state.get("stack") or []:
        oid = e.get("object_id", e.get("id", ""))
        if obj_lname(state, oid) == lname:
            return str(oid)
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
            # the DeclareAttackers relations schema is observed, not answered
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
    assert str(ver).startswith("0.108.0"), f"unexpected version {ver}"
    assert int(proto) == 134, f"unexpected protocol {proto}"
    assert str(build) == "b8fa1d3", f"unexpected build {build}"


# ------------------------------------------------------- parse-level evidence

def _blob_has_exile_replacement(entry):
    """True if any parsed replacement actually exiles (effect-level check).

    NOTE: the description text of a replacement may quote the whole oracle
    paragraph (e.g. the ETB loyalty-counter replacement's description
    contains "If it would leave the battlefield, exile it instead"), so
    text matching is WRONG here -- only an Exile-typed effect counts."""
    for r in entry.get("replacements") or []:
        exe = r.get("execute") or {}
        eff = exe.get("effect") or {}
        if eff.get("type") == "Exile":
            return True, r
    return False, None


def _blob_has_cant_be_attacked(entry):
    """True if any parsed static/ability prevents attacks on the controller."""
    blob = json.dumps(entry.get("static_abilities") or []).lower()
    if "attack" in blob and ("can't" in blob or "cannot" in blob or "cant" in blob):
        return True
    for a in entry.get("abilities") or []:
        ab = json.dumps(a).lower()
        if "attack" in ab and ("can't" in ab or "cannot" in ab):
            return True
    return False


def check_parse():
    """Decisive parse-level check for #9855. Never aborts: the missing
    lowerings ARE the reported outcome."""
    entry = CARD_DATA.get(DECOY, {})
    oracle = entry.get("oracle_text") or ""
    blob = json.dumps(entry)
    has_exile_repl, exile_repl = _blob_has_exile_replacement(entry)
    has_cba = _blob_has_cant_be_attacked(entry)
    has_unimpl = "Unimplemented" in blob
    has_parse_warnings = bool(entry.get("parse_warnings"))
    ev = {
        "oracle": oracle,
        "n_abilities": len(entry.get("abilities") or []),
        "ability_kinds": [a.get("kind") for a in entry.get("abilities") or []],
        "n_replacements": len(entry.get("replacements") or []),
        "replacement_effects": [(r.get("event"),
                                 ((r.get("execute") or {}).get("effect") or {}).get("type"))
                                for r in entry.get("replacements") or []],
        "n_static_abilities": len(entry.get("static_abilities") or []),
        "n_triggers": len(entry.get("triggers") or []),
        "has_ltb_exile_replacement": has_exile_repl,
        "has_cant_be_attacked_static": has_cba,
        "has_unimplemented_marker": has_unimpl,
        "has_parse_warnings": has_parse_warnings,
        "oracle_has_exile_sentence": "If it would leave the battlefield, exile it instead" in oracle,
        "oracle_has_cba_sentence": "You can't be attacked" in oracle,
    }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump({"issue": ISSUE, "card": DECOY, "oracle_text": oracle,
                   "abilities": entry.get("abilities"),
                   "replacements": entry.get("replacements"),
                   "static_abilities": entry.get("static_abilities"),
                   "triggers": entry.get("triggers"),
                   "metadata": entry.get("metadata"),
                   "analysis": ev}, f, indent=1, default=str)
    say(f"parse-level: ltb_exile_replacement={has_exile_repl} "
        f"cant_be_attacked={has_cba} unimplemented_marker={has_unimpl} "
        f"parse_warnings={has_parse_warnings}")
    wire("parse_level", ev)
    return ev


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
        picks = cands[:1]
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
        # answer via the choice id (choose response)
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


def untapped_named_lands(state, pid, lname):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == lname
               and not get_obj(state, o).get("tapped"))


def untapped_land_total(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def untapped_colored_lands(state, pid):
    """Count untapped lands by color identity (best-effort via name)."""
    out = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0}
    cmap = {"plains": "W", "island": "U", "swamp": "B",
            "mountain": "R", "forest": "G"}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = cmap.get(obj_lname(state, o))
            if c:
                out[c] += 1
    return out


# --------------------------------------------- declare-attackers observation

def attack_targets_offered(st, state):
    """Inspect the DeclareAttackers vi relations opportunity. Returns
    (attacker_refs, target_descs) where target_descs are dicts like
    {"kind": "player", "seat": 0} or {"kind": "object", "ref": oid,
    "name": lname}. Returns (None, None) when no relations opportunity is
    present."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data", {}) or {}
        spec = data.get("spec", {}) or {}
        if spec.get("type") != "relations":
            continue
        attackers, targets = [], []
        for ch in data.get("candidates", []) or []:
            role, ref, name, seat = None, None, None, None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if not isinstance(d, dict):
                    continue
                if d.get("role") in ("attacker", "attackTarget"):
                    role = d.get("role")
                if d.get("reference") is not None:
                    ref = str(d.get("reference"))
                if d.get("name"):
                    name = str(d.get("name"))
                if d.get("seat") is not None:
                    seat = d.get("seat")
            if role == "attacker":
                attackers.append({"id": ch.get("id"), "ref": ref, "name": name})
            elif role == "attackTarget":
                if seat is not None:
                    targets.append({"kind": "player", "seat": seat})
                else:
                    targets.append({"kind": "object", "ref": ref,
                                    "name": name or
                                    (obj_lname(state, ref) if ref else None)})
        return attackers, targets, opp
    return None, None, None


# ------------------------------------------------------- target selection

def target_opportunity(st):
    """First vi opportunity that looks like a target selection: schema
    select/sequence with candidates, or exactChoices with candidate/target
    codes (passPriority menus excluded)."""
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


def pick_decoy_target(state, opp, decoy_oids):
    """Prefer the Personal Decoy candidate (by reference or name)."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    decoy_set = {str(x) for x in decoy_oids}
    for ch in cands:
        if _cand_ref(ch) in decoy_set:
            return ch
    for ch in cands:
        if "personal decoy" in choice_text(ch).lower():
            return ch
        if _cand_ref(ch) and obj_lname(state, _cand_ref(ch)) == DECOY:
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
    say(f"[{tag}] submitting target: id={cid} text={choice_text(ch)[:80]}")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid, cid


# ------------------------------------------------------------------- ticks

async def common_tick_head(c, acts, st, pid, tag):
    if await do_mulligan(c, acts, st, pid, tag):
        return True
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return True
    atypes = set(a.get("type") for a in acts)
    if "DeclareBlockers" in atypes:
        return True  # P0/P1 never block in this scenario; hold
    if await pay_tick(c, acts):
        return True
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return True
    await asyncio.sleep(0)  # yield before leg evaluation (race fix)
    return None


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

    # --- A6 fallback: the engine offers no DeclareAttackers action when P1
    # has no able attackers, so the observation above may never fire. If the
    # Decoy has been out for 3+ full turns with no P1 creature on the
    # battlefield, record A6 as not-run and let the A7 leg proceed.
    if ctx.get("decoy_oid") and not ctx.get("a6_recorded") \
            and state.get("turn_number", 0) >= ctx.get("decoy_entry_turn", 0) + 6:
        has_creature = any(is_creature(get_obj(state, o))
                           for o in bf_oids(state, 1))
        if not has_creature:
            ctx["a6_recorded"] = True
            ctx["a6_had_attacker"] = False
            ctx["a6_attackers"] = []
            ctx["a6_targets"] = []
            say(f"[{tag}] A6 fallback: Decoy out since turn "
                f"{ctx.get('decoy_entry_turn')}, P1 still has no creature "
                f"(turn {state.get('turn_number')}) -- not-run")
            wire("a6_fallback_notrun",
                 {"decoy_entry_turn": ctx.get("decoy_entry_turn"),
                  "turn": state.get("turn_number")})

    # --- A6: observe declare-attackers targets while the Decoy is out
    if "DeclareAttackers" in atypes and ctx.get("decoy_oid") \
            and not ctx.get("a6_recorded"):
        attackers, targets, opp = attack_targets_offered(st, state)
        if attackers is not None:
            ctx["a6_recorded"] = True
            ctx["a6_attackers"] = attackers
            ctx["a6_targets"] = targets
            ctx["a6_rev"] = c.revision
            with open(f"{EVDIR}/attack_offering.json", "w") as f:
                json.dump({"rev": c.revision,
                           "decoy_oid": ctx.get("decoy_oid"),
                           "attackers": attackers, "targets": targets,
                           "opportunity": opp}, f, indent=1, default=str)
            p0_offered = any(t.get("kind") == "player" and
                             str(t.get("seat")) == "0" for t in targets)
            ctx["a6_p0_offered"] = p0_offered
            ctx["a6_had_attacker"] = len(attackers) > 0
            say(f"[{tag}] A6 declare-attackers observed: attackers="
                f"{[(a.get('name'), a.get('ref')) for a in attackers]} "
                f"targets={targets} p0_offered={p0_offered}")
            wire("attack_offering", {"rev": c.revision, "attackers": attackers,
                                     "targets": targets,
                                     "p0_offered": p0_offered})
        # declare no attackers either way (observation only), then continue
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return

    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return

    # --- A7: cast Hero's Downfall; the Decoy is chosen via the engine's
    # vi target-selection opportunity AFTER the cast (CR 601.2c).
    # DO NOT inject "targets" into the CastSpell action: the engine
    # advertises CastSpell with targets: [] and IGNORES submitted targets
    # (engine.rs: GameAction::CastSpell { .. }); worse, a string oid in
    # "targets" breaks Action deserialization so the cast is silently
    # dropped (probe 2026-10-13: cast with targets:["21"] never left the
    # hand, engine stayed at WaitingFor::Priority). Submit as advertised.
    # Only after the A6 observation so the attack leg runs first.
    if my_main(state, 1) and ctx.get("decoy_oid") and ctx.get("a6_recorded") \
            and not ctx.get("downfall_cast"):
        hn = hand_names(state, 1)
        cols = untapped_colored_lands(state, 1)
        if DOWNFALL in hn and cols["B"] >= 2 and untapped_land_total(state, 1) >= 3:
            a, oid = cast_action_for(acts, state, DOWNFALL)
            if a is not None:
                ctx["downfall_cast"] = True
                ctx["downfall_submitted"] = True
                ctx["downfall_submitted_at"] = time.time()
                ST["mana_needs"][tag] = {"B": 2, "generic": 1}
                say(f"[{tag}] casting Hero's Downfall (oid {oid}); Decoy "
                    f"target (oid {ctx['decoy_oid']}) to be chosen at the "
                    f"engine's target-selection prompt")
                wire("cast_downfall", {"oid": oid,
                                       "target_oid": ctx["decoy_oid"],
                                       "cast_action_data": a.get("data")})
                await submit_as_is(c, a)
                return True

    # --- answer the Downfall target selection with the Decoy
    if ctx.get("downfall_submitted") and not ctx.get("downfall_targeted"):
        opp, rtype, stype = target_opportunity(st)
        if opp is not None:
            decoys = decoy_oids(state, 0)
            pick = pick_decoy_target(state, opp, decoys)
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            cands = data.get("candidates") or data.get("choices") or []
            wire("downfall_target_seen",
                 {"rev": c.revision, "rtype": rtype, "stype": stype,
                  "candidates": [{"text": choice_text(ch)[:80],
                                  "ref": _cand_ref(ch),
                                  "ref_name": obj_lname(state, _cand_ref(ch))
                                  if _cand_ref(ch) else None}
                                 for ch in cands]})
            if pick is not None:
                ctx["downfall_targeted"] = True
                say(f"[{tag}] targeting the Decoy with Hero's Downfall")
                await submit_target(c, opp, rtype, stype, pick, tag)
                return True
            key = (tag, "downfall-target-miss", c.revision)
            if key not in SUBMITTED_OPPS:
                SUBMITTED_OPPS.add(key)
                say(f"[{tag}] downfall target prompt has no Decoy candidate "
                    f"-- recorded, not answered")
            return True
        # No recognizable target opportunity: log what IS offered
        # (rate-limited) instead of spinning silently, and give up after
        # 180s so the attempt can finish with A7 not-run.
        key = (tag, "downfall-no-target-opp", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("downfall_no_target_opp",
                 {"rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] downfall submitted but no target opportunity "
                f"recognized; vi={_opp_summary(st)}")
        if time.time() - ctx.get("downfall_submitted_at", 0) > 180:
            ctx["a7_giveup"] = True
            ctx["a7_giveup_reason"] = (
                "target-selection opportunity never appeared after "
                "CastSpell submission; engine stayed at priority")
            wire("downfall_stall_giveup",
                 {"rev": c.revision, "opps": _opp_summary(st)})
            say(f"[{tag}] A7 GIVEUP: {ctx['a7_giveup_reason']}")
        return True

    # --- cast bears whenever affordable (attack-leg fodder)
    if my_main(state, 1) and not ctx.get("downfall_cast"):
        hn = hand_names(state, 1)
        cols = untapped_colored_lands(state, 1)
        if BEARS in hn and cols["G"] >= 1 and untapped_land_total(state, 1) >= 2:
            a, oid = cast_action_for(acts, state, BEARS)
            if a is not None:
                ST["mana_needs"][tag] = {"G": 1, "generic": 1}
                say(f"[{tag}] casting Grizzly Bears (oid {oid})")
                wire("cast_bears", {"oid": oid})
                await submit_as_is(c, a)
                return True

    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
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
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return

    # track the Decoy entering the battlefield
    if not ctx.get("decoy_oid"):
        ds = decoy_oids(state, 0)
        if ds:
            ctx["decoy_oid"] = ds[0]
            ctx["decoy_entry_turn"] = state.get("turn_number", 0)
            n, raw = counters_named(state, ds[0], "loyalty")
            ctx["decoy_loyalty"] = n
            ctx["decoy_loyalty_raw"] = raw
            ctx["p0_life_at_entry"] = player_life(state, 0)
            say(f"[{tag}] Personal Decoy on battlefield (oid {ds[0]}), "
                f"loyalty counters={n} (raw={raw}), P0 life="
                f"{ctx['p0_life_at_entry']}")
            wire("decoy_entered", {"oid": ds[0], "loyalty": n,
                                   "loyalty_raw": raw,
                                   "p0_life": ctx["p0_life_at_entry"]})

    # track the TARGETED Decoy's zone after the Downfall resolves.
    # Must use ctx["decoy_oid"] (the specific Decoy that was targeted),
    # not the first Decoy found anywhere: the deck runs 4x Decoy and
    # named_oids_anywhere can return a different copy (e.g. one already
    # in exile), which misattributes the zone (caught 2026-10-13: oid 7
    # in Exile recorded instead of the targeted oid 10).
    if ctx.get("downfall_targeted") and not ctx.get("a7_recorded"):
        oid = ctx.get("decoy_oid")
        if oid:
            z = obj_zone(state, oid)
            # wait until it actually leaves the battlefield
            if z in ("Graveyard", "Exile"):
                ctx["a7_recorded"] = True
                ctx["a7_zone"] = z
                ctx["a7_oid"] = oid
                say(f"[{tag}] A7: targeted Decoy (oid {oid}) zone after "
                    f"destroy = {z}")
                wire("decoy_post_destroy", {"oid": oid, "zone": z})
                post = json.loads(await c.export_state())
                with open(f"{EVDIR}/post_destroy.json", "w") as f:
                    json.dump(post, f, indent=1)
                wire("post_destroy_export", {})

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return
        hn = hand_names(state, 0)
        cols = untapped_colored_lands(state, 0)
        if not ctx.get("decoy_cast") and DECOY in hn \
                and cols["W"] >= 1 and cols["U"] >= 1 \
                and untapped_land_total(state, 0) >= 7:
            a, oid = cast_action_for(acts, state, DECOY)
            if a is not None:
                ctx["decoy_cast"] = True
                ST["mana_needs"][tag] = {"W": 1, "U": 1, "generic": 5}
                say(f"[{tag}] casting Personal Decoy (oid {oid})")
                wire("cast_decoy", {"oid": oid,
                                    "cast_action_data": a.get("data")})
                await submit_as_is(c, a)
                return True

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

async def run_attempt(attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    tag = f"9855-{attempt}"
    ST["mana_needs"][tag] = {}
    ST["mana_needs"][f"P1{tag}"] = {}
    ctx = {}

    p0 = PhaseClient(f"P0{tag}")
    await p0.connect()
    await p0.create(deck((PLAINS.capitalize(), 28), (ISLAND.capitalize(), 28),
                         ("Personal Decoy", 4)))
    p1 = PhaseClient(f"P1{tag}")
    await p1.connect()
    # Dense Downfall count (12x): the A7 leg needs P1 to draw Hero's
    # Downfall reliably; 4x was too fragile (13:42 run: P1 never drew one
    # by turn 41). 4x Bears is enough for the A6 attack leg.
    await p1.join(p0.game_code, deck((FOREST.capitalize(), 24),
                                     (SWAMP.capitalize(), 24),
                                     ("Grizzly Bears", 4),
                                     ("Hero's Downfall", 12)))
    say(f"game {p0.game_code} attempt={attempt}; "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_start", {"attempt": attempt, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    obs = {"attempt": attempt, "assert": {}, "notes": [],
           "game_code": p0.game_code}
    obs["assert"]["A1_setup_ok"] = "not-run"

    # fast-abort: the fixture needs Personal Decoy in hand early; redeal a
    # fresh game when the opening 7 lacks it.
    for _ in range(80):
        await asyncio.sleep(0.25)
        if p0.latest and p0.latest.get("state", {}).get("turn_number", 0) >= 1:
            break
    st = p0.latest
    if st is None or DECOY not in hand_names(st["state"], 0):
        say(f"[{tag}] fast abort: opening hand lacks Personal Decoy "
            f"({hand_names(st['state'], 0) if st else 'no state'})")
        wire("fast_abort", {"reason": "no decoy in opening hand",
                            "hand": hand_names(st["state"], 0) if st else []})
        obs["notes"].append("fast abort: opening hand lacked Personal Decoy; "
                            "fresh game redealt")
        obs["assert"]["A6_attack_target_offer"] = "not-run"
        obs["assert"]["A7_destroy_exiles"] = "not-run"
        obs["fast_aborted"] = True
        await p0.close()
        await p1.close()
        return obs

    t0 = time.time()
    last_diag = 0
    settle_ticks = 0
    while time.time() - t0 < 780:
        await asyncio.sleep(0.25)
        await p1_tick(p1, f"P1{tag}", ctx)
        await p0_tick(p0, tag, ctx)
        await asyncio.sleep(0)
        if ctx.get("a7_giveup"):
            say(f"[{tag}] breaking attempt loop on A7 giveup: "
                f"{ctx.get('a7_giveup_reason')}")
            obs["a7_giveup"] = True
            obs["a7_giveup_reason"] = ctx.get("a7_giveup_reason")
            break
        # Runaway-game safeguard: the clean path finishes by turn ~15; if
        # the game drifts far beyond that without A7 completing, stop this
        # attempt instead of playing a 100-turn game (seen 2026-10-13).
        st_now = p0.latest
        if st_now and st_now["state"].get("turn_number", 0) > 40 \
                and not ctx.get("a7_recorded"):
            say(f"[{tag}] turn {st_now['state'].get('turn_number')} exceeded "
                f"without A7; abandoning attempt")
            obs["a7_giveup"] = True
            obs["a7_giveup_reason"] = (
                f"game reached turn {st_now['state'].get('turn_number')} "
                f"without the destroy leg completing")
            break
        if time.time() - last_diag > 30:
            last_diag = time.time()
            for c, pidx in ((p0, 0), (p1, 1)):
                st = c.latest
                if not st:
                    say(f"[diag] {c.name}: NO STATE rev={c.revision}")
                    continue
                s = st["state"]
                acts = merged_actions(st)
                say(f"[diag] {c.name}: rev={c.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} active={s.get('active_player')} "
                    f"stack={len(s.get('stack') or [])} n_acts={len(acts)} "
                    f"decoy_oid={ctx.get('decoy_oid')} a6={ctx.get('a6_recorded')} "
                    f"downfall={ctx.get('downfall_targeted')} a7={ctx.get('a7_recorded')} "
                    f"p0bf={bf_names(s, 0)[:5]} p1bf={bf_names(s, 1)[:5]}")
        if ctx.get("a7_recorded"):
            st = p0.latest
            quiet = st and not decision_pending(st) \
                and not (st["state"].get("stack") or [])
            if quiet:
                settle_ticks += 1
            else:
                settle_ticks = 0
            if settle_ticks >= 8:
                break
        if not ctx.get("decoy_cast") and time.time() - t0 > 600:
            say(f"[{tag}] Decoy never cast >600s; capturing state and bailing")
            break

    st = p0.latest
    state = st["state"] if st else {}
    # A1: Decoy was cast and entered with loyalty == P0 life (entry-time
    # values; the final state may no longer have it on the battlefield
    # because the A7 leg destroys it).
    if ctx.get("decoy_oid"):
        n = ctx.get("decoy_loyalty")
        life = ctx.get("p0_life_at_entry")
        if n is not None and life is not None and n == life:
            obs["assert"]["A1_setup_ok"] = "passed"
            obs["notes"].append(
                f"A1 passed: Personal Decoy (oid {ctx['decoy_oid']}) entered "
                f"with {n} loyalty counters == P0 life {life} at entry "
                f"(the ETB replacement parsed and works)")
        else:
            obs["assert"]["A1_setup_ok"] = "failed"
            obs["notes"].append(
                f"A1 FAILED: decoy_oid={ctx.get('decoy_oid')} "
                f"loyalty_at_entry={n} p0_life_at_entry={life}")
    else:
        obs["assert"]["A1_setup_ok"] = "not-run"
        obs["notes"].append("A1 not-run: Personal Decoy never reached the "
                            "battlefield")

    # A6: was P0 offered as an attack target while the Decoy was out?
    if ctx.get("a6_recorded"):
        if not ctx.get("a6_had_attacker"):
            obs["assert"]["A6_attack_target_offer"] = "not-run"
            obs["notes"].append(
                "A6 not-run: declare-attackers was observed with the Decoy "
                "on the battlefield but P1 had no able attacker "
                f"(targets offered: {ctx.get('a6_targets')})")
        elif ctx.get("a6_p0_offered"):
            obs["assert"]["A6_attack_target_offer"] = "failed"
            obs["notes"].append(
                "A6 FAILED (bug behavior): P1's declare-attackers opportunity "
                f"(rev {ctx.get('a6_rev')}) offered P0 -- the Decoy's "
                f"controller -- as an attack target while Personal Decoy "
                f"(oid {ctx.get('decoy_oid')}) was on the battlefield; "
                f"attackers={ctx.get('a6_attackers')} targets={ctx.get('a6_targets')}. "
                "Per the oracle ('You can't be attacked'), P0 must not be a "
                "legal attack target.")
        else:
            obs["assert"]["A6_attack_target_offer"] = "passed"
            obs["notes"].append(
                "A6 passed: P0 was NOT offered as an attack target while the "
                f"Decoy was on the battlefield (targets={ctx.get('a6_targets')})")
    else:
        obs["assert"]["A6_attack_target_offer"] = "not-run"
        obs["notes"].append("A6 not-run: declare-attackers with the Decoy on "
                            "the battlefield was never observed")

    # A7: where did the destroyed Decoy go?
    if ctx.get("a7_recorded"):
        z = ctx.get("a7_zone")
        if z == "Exile":
            obs["assert"]["A7_destroy_exiles"] = "passed"
            obs["notes"].append(
                "A7 passed: Hero's Downfall destroyed the Decoy and it went "
                "to Exile, per 'If it would leave the battlefield, exile it "
                "instead'")
        else:
            obs["assert"]["A7_destroy_exiles"] = "failed"
            obs["notes"].append(
                f"A7 FAILED (bug behavior): the destroyed Decoy went to "
                f"{z} instead of Exile (oid {ctx.get('a7_oid')})")
    else:
        obs["assert"]["A7_destroy_exiles"] = "not-run"
        note = ("A7 not-run: the destroy leg never completed "
                f"(downfall_submitted={ctx.get('downfall_submitted')}, "
                f"downfall_targeted={ctx.get('downfall_targeted')})")
        if ctx.get("a7_giveup"):
            note += f"; giveup: {ctx.get('a7_giveup_reason')}"
        obs["notes"].append(note)

    for fn in ("attack_offering.json", "post_destroy.json"):
        try:
            with open(f"{EVDIR}/{fn}") as f:
                json.load(f)
            obs["notes"].append(f"{fn} captured and parses")
        except (OSError, ValueError):
            obs["notes"].append(f"{fn} missing/unparseable")

    await p0.close()
    await p1.close()
    return obs


async def _main():
    t0 = time.time()
    await verify_server_hello()
    parse_ev = check_parse()
    obs = None
    for attempt in range(6):
        obs = await run_attempt(attempt)
        a1 = obs["assert"].get("A1_setup_ok")
        a7 = obs["assert"].get("A7_destroy_exiles")
        if a1 in ("passed", "failed") and a7 in ("passed", "failed"):
            break
        if obs.get("fast_aborted"):
            say(f"[attempt {attempt}] fast-aborted (no Decoy in opener); redealing")
            continue
        if obs.get("a7_giveup"):
            say(f"[attempt {attempt}] A7 giveup ({obs.get('a7_giveup_reason')}); "
                f"not retrying blind")
            break
        say(f"[attempt {attempt}] incomplete (A1={a1}, A7={a7}); "
            f"{'retrying' if attempt < 5 else 'giving up'}")

    ass = dict(obs["assert"])
    # A2 (decisive, parse-level): "failed" means the reported bug is present.
    if parse_ev["has_ltb_exile_replacement"]:
        ass["A2_exile_replacement"] = "passed"
    else:
        ass["A2_exile_replacement"] = "failed"
    # A3: the can't-be-attacked static
    ass["A3_cant_be_attacked"] = ("passed" if parse_ev["has_cant_be_attacked_static"]
                                  else "failed")
    # A4: no unimplemented marker covering the dropped sentences
    if parse_ev["has_unimplemented_marker"] or parse_ev["has_parse_warnings"]:
        ass["A4_no_unimplemented"] = "passed"
    else:
        ass["A4_no_unimplemented"] = "failed"
    # A5: the oracle text itself is intact in the pinned data
    ass["A5_oracle_intact"] = ("passed"
                               if (parse_ev["oracle_has_exile_sentence"]
                                   and parse_ev["oracle_has_cba_sentence"])
                               else "failed")
    notes = list(obs["notes"])
    notes.append(
        f"parse: ltb_exile_replacement={parse_ev['has_ltb_exile_replacement']} "
        f"cant_be_attacked={parse_ev['has_cant_be_attacked_static']} "
        f"unimplemented_marker={parse_ev['has_unimplemented_marker']} "
        f"parse_warnings={parse_ev['has_parse_warnings']} "
        f"n_replacements={parse_ev['n_replacements']} "
        f"replacement_effects={parse_ev['replacement_effects']} "
        f"n_static_abilities={parse_ev['n_static_abilities']}")
    notes.append("protocol-134 driver: CreateGameWithSettings/JoinGameWithPassword/"
                 "start_when_full; MulliganDecision via legacy Action (Keep); "
                 "DiscardToHandSize via vi select; mana via vi tapLandForMana; "
                 "casts via advertised legal actions; priority-gated passes; "
                 "sleep(0) yield before leg evaluation; DeclareAttackers "
                 "observed via the vi schema/relations opportunity (observation "
                 "only -- empty attacks declared); Hero's Downfall target "
                 "answered via vi target selection preferring the Decoy.")

    a2 = ass.get("A2_exile_replacement")
    if a2 == "failed":
        verdict = "reproduced"
    elif a2 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    notes.append("verdict logic: reproduced = the pinned card-data.json entry "
                 "for Personal Decoy carries no leaves-the-battlefield exile "
                 "replacement -- the reported outcome -- corroborated by the "
                 "missing can't-be-attacked static, the absence of any "
                 "unimplemented marker, and the game legs where they "
                 "completed. not-reproduced = the replacement is present.")

    scenario_src = open(__file__, "rb").read()
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9855",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": ("backfill-owned v0.108.0 server on 127.0.0.1:9374 "
                           "(--single-user --no-data-download, verified "
                           "release; reused by this run); evidence under "
                           "evidence/9855/" + RUN_ID),
        "driver": {"protocol_advertised": 134, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_bytes(scenario_src),
        "decks": {
            "P0": [["Plains", 28], ["Island", 28], ["Personal Decoy", 4]],
            "P1": [["Forest", 24], ["Swamp", 24], ["Grizzly Bears", 4],
                   ["Hero's Downfall", 12]],
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "parse_evidence": parse_ev,
        "result": ("A1 %s; A2 %s; A3 %s; A4 %s; A5 %s; A6 %s; A7 %s"
                   % (ass.get("A1_setup_ok"), a2,
                      ass.get("A3_cant_be_attacked"),
                      ass.get("A4_no_unimplemented"),
                      ass.get("A5_oracle_intact"),
                      ass.get("A6_attack_target_offer"),
                      ass.get("A7_destroy_exiles"))),
        "scope": ("Personal Decoy LTB-exile replacement + can't-be-attacked "
                  "static: parse-level presence in the pinned v0.108.0 "
                  "card-data.json (decisive), plus two in-game legs "
                  "(declare-attackers target offering; Hero's Downfall "
                  "destroy zone check); two-player game; native human seats, "
                  "no AI"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "The attack leg observes the engine's advertised attack targets "
            "rather than submitting an attack; the destroy leg requires P1 "
            "to draw and cast Hero's Downfall with the Decoy on the "
            "battlefield -- missing preconditions mark those legs not-run.",
            "Counter reads are a best-effort parse of the runtime object's "
            "counters payload; the verbatim payload is preserved in "
            "wire_log.jsonl under decoy_entered.",
        ],
        "setup_line": ("P0 28x Plains + 28x Island + 4x Personal Decoy "
                       "({5}{W}{U} Planeswalker -- Duck); P1 24x Forest + "
                       "24x Swamp + 4x Grizzly Bears ({1}{G} 2/2) + 12x Hero's "
                       "Downfall ({1}{B}{B} destroy target creature or "
                       "planeswalker)"),
        "contract_line": ("Parse (decisive): Personal Decoy must carry a "
                          "leaves-the-battlefield exile replacement and a "
                          "can't-be-attacked static, or an unimplemented "
                          "marker. Game (confirming): P1's declare-attackers "
                          "must not offer P0 as a target while the Decoy is "
                          "out; a destroyed Decoy must go to exile, not the "
                          "graveyard."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_9855.py", "w") as f:
        f.write(scenario_src.decode())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes, "verdict": verdict}, f, indent=1)
    render_summary(run, f"{EVDIR}/summary.png")
    wire("verdict", {"verdict": verdict, "assertions": ass})
    say(f"VERDICT: {verdict}")
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha256_of_bytes(open(f"{EVDIR}/{fn}", "rb").read()) + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 1100
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #9855 - Personal Decoy: LTB-exile replacement +",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "\"You can't be attacked\" dropped, no unimplemented marker",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server {si['server_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Decoy cast, loyalty == P0 life",
        "A2_exile_replacement": "A2 [bug] LTB-exile replacement missing from parse",
        "A3_cant_be_attacked": "A3 [bug] can't-be-attacked static missing from parse",
        "A4_no_unimplemented": "A4 [bug] no unimplemented marker (reads as supported)",
        "A5_oracle_intact": "A5 oracle text intact in pinned data",
        "A6_attack_target_offer": "A6 P0 offered as attack target while Decoy out",
        "A7_destroy_exiles": "A7 destroyed Decoy exiled (not graveyard)",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:16]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9855/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def main():
    pidfile = "/tmp/scenario_9855.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_9855 instance is alive "
                             f"(pid {old}); refusing")
        except (ProcessLookupError, ValueError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        if os.path.exists(pidfile):
            os.remove(pidfile)


if __name__ == "__main__":
    asyncio.run(main())
