#!/usr/bin/env python3
"""Issue #9861: Rootweaver Druid -- the "put one onto the battlefield under
your control and the rest under their own" split uses the wrong seats.

Oracle: "When this creature enters, each opponent may search their library
for up to three basic land cards. They each put one of those cards onto
the battlefield tapped under your control and the rest onto the battlefield
tapped under their control. Then each player who searched their library
this way shuffles."

Reported: the lands end up under the wrong seats when the Druid is under
another player's control (caster-control case). The issue points at
`search_library` with a per-player controller override under
`player_scope`.

Parsed card data (v0.109.0) shows the trigger chain: SearchLibrary (UpTo 3
basic lands, player_scope Opponent, optional) -> ChangeZone Library->BF,
target Any, enters_under "You" (the ONE under your control) -> ChangeZone,
target TrackedSet 0, enters_under "You" (the REST) -> Shuffle (target
Controller, player_scope PerformedActionThisWay/SearchedLibrary).

Behavioral contract -- two legs:
  Leg A (baseline, caster == controller): 2-player game. P0 casts
    Rootweaver Druid; P1 (the only opponent) searches for 3 Forests.
    Expected: 1 Forest under P0 tapped, 2 under P1 tapped.
  Leg B (caster != controller): 4-player game. P0 casts Rootweaver Druid;
    P1 answers with Aethersnatch ("Gain control of target spell"), so the
    Druid enters under P1's control. Opponents P0, P2, P3 each search for
    3 Forests. Expected per searcher: 1 Forest under P1 tapped, 2 under
    that searcher tapped.

Assertions (per leg):
  A1 setup_ok            PRE has the Druid in P0's hand and >=3 basics in
                         each searcher's library; the Druid enters under
                         the expected controller (A: P0, B: P1) and its
                         ETB trigger fires.
  A2 searches_offered    Each expected opponent got the may-search prompt,
                         accepted, and found >=1 basic land (found =
                         PRE-library oids on the BF in POST).
  A3 one_under_druid     Per searcher, exactly 1 found land has controller
                         == Druid controller. FAILED = wrong-seat split.
  A4 rest_under_searcher Per searcher, all other found lands have
                         controller == that searcher.
  A5 cleanup            POST stack empty, Druid still on BF, all found
                         lands tapped, no Druid prompt pending.

Verdict rule (pre-registered):
  - not-reproduced: both legs A1-A5 passed.
  - reproduced: any leg has A1 passed + trigger resolved + (A2 failed
    [trigger resolved but no search happened -- the effect does nothing]
    OR A3/A4 failed [wrong-seat split]).
  - blocked: anything else (setup never completed; Leg B's Aethersnatch
    never transferred control so the caster-control case was untestable
    AND Leg A passed clean; prompts never offered, etc.).

Prompt handling: the may-search optional is ACCEPTED when its text
mentions "search"; the Aethersnatch "choose new targets" optional is
DECLINED (Druid has no targets). Search selections pick 3 library basics;
the split pick (if the engine prompts for it) picks 1 of the found lands
for the Druid controller. If the engine auto-applies the split, the
driver only observes. Aethersnatch targets the Druid stack spell via the
engine's vi TargetSelection (never inject targets into CastSpell).

Revision 02 (2026-10-14): attempt 1 (run-...-0311) stalled in both legs
on the may-search optional: the engine's decideOptionalEffect choices
carry boolean surface values, NOT text (blob "false | false | true |
true"), so the text-keyword classifier refused to answer. Fix: classify
optionals by game context -- Druid on the BF => may-search => ACCEPT;
Snatch on the stack with Druid not on the BF => choose-new-targets =>
DECLINE; text keywords only as fallback. Attempt 1 also showed Leg B's
turn gate was wrong: turn_number counts individual player turns, not
rounds, so turn>=7 fired while P1 had only 3 Islands and Snatch was
unaffordable -- the Druid resolved under P0. Fix: Leg B gates on P1's
untapped Islands (>=6, +1 per attempt).
Attempt 2 (run-...-0335) proved Leg A reproduces (all 3 found lands
under P0, expected 1 under P0 + 2 under P1) but abandoned Leg B attempt
1 at turn 26: P1's 6th Island arrived on turn 26 and the turn cap (25)
cut off P0's next turn. Fix: leg-aware turn cap (A: 25, B: 45).

Conventions (from scenario_9860_02.py, protocol 140):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - Export response data.state is a JSON string, parsed once.
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
log = logging.getLogger("scenario9861")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-9861-v01090-" + time.strftime("%Y%m%d-%H%M"))
ISSUE = 9861
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
               "evidence under evidence/9861/" + RUN_ID),
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


DRUID = "rootweaver druid"
SNATCH = "aethersnatch"
FOREST = "forest"
ISLAND = "island"
BASIC_LANDS = (FOREST, ISLAND, "plains", "mountain", "swamp")


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


def druid_pending(st):
    """True if a Rootweaver Druid resolution prompt is pending on this view.

    Classification mirrors answer_druid_opp: the may-search optional, or
    a pure-candidate prompt whose candidates are basic lands (search
    selection / split pick) or a Stack-zone Druid spell (Aethersnatch
    target). Used by the POST-settle detector so ordinary post-resolution
    decisions (draw/discard, priority) do not mask it.
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
            ref = _cand_ref(ch)
            if ref is None:
                continue
            o = get_obj(state, ref)
            if obj_lname(state, ref) in BASIC_LANDS:
                return True
            if o.get("zone") == "Stack" \
                    and obj_lname(state, ref) == DRUID:
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
        # the Druid / Aethersnatch while setup is incomplete.
        picks = [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) == FOREST] or \
                [ch for ch in cands
                 if obj_lname(state, _cand_ref(ch)) == ISLAND] or cands[:1]
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

# ------------------------------------------------- Rootweaver Druid handling

RECORDED_OPPS = set()
RETRIED_OPPS = set()


def _spec_max(data, default):
    """Advertised max picks for a schema prompt, else `default`.

    The spec lives at data.spec.data.{min,max} (e.g. sequence min=1 max=1
    on the Raiding Party Plains prompt in #9860). A submission over max
    is silently ignored by the engine, so every schema submission must
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


def dump_druid_opp(state, opp, kind, tag, ctx):
    n = ctx.get("druid_prompts_seen", 0) + 1
    ctx["druid_prompts_seen"] = n
    with open(f"{EVDIR}/druid_opp_{ctx.get('leg')}_{n}.json", "w") as f:
        json.dump({"rev": None, "kind": kind, "tag": tag,
                   "opportunity": opp, "text_blob": opp_text_blob(opp)},
                  f, indent=1, default=str)
    wire("druid_opp", {"n": n, "kind": kind, "tag": tag,
                       "blob": opp_text_blob(opp)[:400]})
    say(f"[{tag}] Druid prompt #{n} kind={kind}: "
        f"blob={opp_text_blob(opp)[:220]}")


def _classify_land_candidates(state, items):
    """Pure candidates that reference basic-land objects -> list of dicts."""
    out = []
    for ch in items:
        if not _is_pure_candidate(ch):
            continue
        ref = _cand_ref(ch)
        if ref is None:
            continue
        name = obj_lname(state, ref)
        if name not in BASIC_LANDS:
            continue
        o = get_obj(state, ref)
        out.append({"id": ch.get("id"), "ref": ref, "name": name,
                    "zone": o.get("zone"), "owner": o.get("owner"),
                    "controller": o.get("controller")})
    return out


async def _submit_land_picks(c, opp, rtype, data, take, tag):
    if rtype == "schema":
        spec = (data.get("spec", {}) or {}).get("type") or "select"
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": spec,
                            "data": {"choiceIds": [x["id"] for x in take]}}}
    else:
        assert len(take) == 1
        sub = {"interactionId": opp.get("interactionId"),
               "response": {"type": "choose",
                            "data": {"choiceId": take[0]["id"]}}}
    await interact_as(c, sub, tag)


async def answer_druid_opp(c, st, pid, tag, ctx):
    """Answer one Rootweaver Druid prompt for this client, if showing.

    - decideOptionalEffect: ACCEPT when the text mentions "search"
      (the may-search offer); DECLINE when it mentions "target"
      (Aethersnatch's optional "choose new targets"; Druid has none).
    - Schema select/sequence or exactChoices with basic-land candidates:
      Library lands owned by this seat = the search selection (pick up
      to the advertised max, 3); lands owned by a searching opponent
      whose split is still open = the split pick (pick 1 for the Druid
      controller). A Library-basics prompt for another searcher with no
      picks yet is answered for that searcher (the engine may offer the
      search on the Druid controller's view).
    Returns True if something was submitted or the prompt was recorded
    (so the tick does not fall through to a priority pass while a
    decision is pending).
    """
    state = st["state"]
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in RECORDED_OPPS:
            continue
        if iid in SUBMITTED_OPPS:
            # Still pending after a submission: the engine may have
            # silently ignored it (#9860: over-max sequence). Allow
            # exactly ONE re-answer with the (now spec-capped) payload,
            # but only if the engine recorded nothing yet (progress
            # selected == 0).
            if iid in RETRIED_OPPS:
                continue
            prog = opp.get("progress", {}) or {}
            if int(prog.get("selected", 0) or 0) > 0:
                continue
            RETRIED_OPPS.add(iid)
            say(f"[{tag}] Druid opp {str(iid)[:16]} still pending after "
                f"a submission; retrying once (spec-capped payload)")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        # --- optional prompts. The engine's decideOptionalEffect choices
        # carry boolean surface values, NOT text ("false | false | true |
        # true"), so the prompt text cannot tell may-search from
        # choose-new-targets. Classify by game context instead: once the
        # Druid is on the battlefield (live state, not just ctx -- the
        # tracking block runs after this handler in the same tick), an
        # optional is the may-search offer -> ACCEPT. While Aethersnatch
        # is on the stack and the Druid has not entered, an optional is
        # the "choose new targets" offer -> DECLINE (Druid has no
        # targets). Text keywords are only a fallback.
        if rtype == "exactChoices" and find_optional_opp(st) is opp:
            blob = opp_text_blob(opp).lower()
            entered_now = any(bf_named(state, ctrl, DRUID)
                              for ctrl in range(ctx["players"]))
            dump_druid_opp(state, opp, "optional", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            if entered_now or ctx.get("druid_entered"):
                ctx.setdefault("search_accepted", set()).add(pid)
                say(f"[{tag}] may-search optional (Druid on BF): ACCEPT")
                await answer_optional(c, opp, True, tag)
                return True
            if ctx.get("snatch_cast"):
                say(f"[{tag}] choose-new-targets optional (Snatch on "
                    f"stack, Druid not on BF): DECLINE")
                await answer_optional(c, opp, False, tag)
                return True
            if "search" in blob:
                ctx.setdefault("search_accepted", set()).add(pid)
                say(f"[{tag}] may-search optional (text fallback): ACCEPT")
                await answer_optional(c, opp, True, tag)
                return True
            if "target" in blob:
                say(f"[{tag}] choose-new-targets optional (text "
                    f"fallback): DECLINE")
                await answer_optional(c, opp, False, tag)
                return True
            RECORDED_OPPS.add(iid)
            say(f"[{tag}] optional prompt with unrecognized context/text "
                f"-- recorded, not answered: blob={blob[:200]}")
            wire("druid_optional_unanswered",
                 {"iid": str(iid)[:12], "blob": blob[:400]})
            return True
        # --- land-candidate prompts (search selection / split pick).
        lands = _classify_land_candidates(state, items)
        if not lands:
            continue
        # Search selection: Library basics owned by this seat.
        mine = [x for x in lands
                if x["zone"] == "Library"
                and str(x["owner"]) == str(pid)]
        if mine:
            picked = ctx.setdefault("search_picks", {}).setdefault(pid, [])
            remaining = [x for x in mine if x["ref"] not in picked]
            if remaining:
                dump_druid_opp(state, opp, "search", tag, ctx)
                SUBMITTED_OPPS.add(iid)
                if rtype == "schema":
                    cap = _spec_max(data, 3)
                    take = remaining[:cap]
                    for x in take:
                        picked.append(x["ref"])
                    say(f"[{tag}] search picks (schema, {len(take)}): "
                        f"{[x['ref'] for x in take]}; total={picked}")
                else:
                    take = remaining[:1]
                    picked.append(take[0]["ref"])
                    say(f"[{tag}] search pick (single): {take[0]['ref']}; "
                        f"total={picked}")
                wire("druid_search_pick", {"who": tag, "picked": list(picked)})
                await _submit_land_picks(c, opp, rtype, data, take, tag)
                return True
            done = next((ch for ch in items if _is_done_choice(ch)), None)
            if done is not None:
                SUBMITTED_OPPS.add(iid)
                say(f"[{tag}] search prompt: all 3 already picked; "
                    f"answering done")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": done.get("id")}}}, tag)
                return True
            RECORDED_OPPS.add(iid)
            say(f"[{tag}] search prompt: no remaining Library basics and "
                f"no done choice -- recorded, not answered")
            return True
        # Fallback search: Library basics owned by a searching opponent
        # with no picks yet (the engine may offer the search on the
        # Druid controller's view). Answer for that searcher.
        for owner_s in ctx.get("searchers", []):
            if owner_s == pid:
                continue
            if ctx.get("search_picks", {}).get(owner_s):
                continue
            cands = [x for x in lands if x["zone"] == "Library"
                     and str(x["owner"]) == str(owner_s)]
            if not cands:
                continue
            picked = ctx.setdefault("search_picks", {}).setdefault(owner_s, [])
            remaining = [x for x in cands if x["ref"] not in picked]
            if not remaining:
                continue
            dump_druid_opp(state, opp, "search-for-other", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            if rtype == "schema":
                take = remaining[:_spec_max(data, 3)]
            else:
                take = remaining[:1]
            for x in take:
                picked.append(x["ref"])
            say(f"[{tag}] search picks for searcher {owner_s} "
                f"({len(take)}): {[x['ref'] for x in take]}; total={picked}")
            wire("druid_search_pick", {"who": f"for-{owner_s}",
                                       "picked": list(picked)})
            await _submit_land_picks(c, opp, rtype, data, take, tag)
            return True
        # Split pick: lands owned by a searching opponent whose split is
        # still open (candidates are the found lands; pick 1 for the
        # Druid controller).
        for owner_s in ctx.get("searchers", []):
            if owner_s == pid:
                continue
            found = ctx.get("search_picks", {}).get(owner_s, [])
            if not found or owner_s in ctx.get("split_picks", {}):
                continue
            cands = [x for x in lands if x["ref"] in found]
            if not cands:
                continue
            dump_druid_opp(state, opp, "split", tag, ctx)
            SUBMITTED_OPPS.add(iid)
            x = cands[0]
            ctx.setdefault("split_picks", {})[owner_s] = x["ref"]
            say(f"[{tag}] split pick for searcher {owner_s}: {x['ref']} "
                f"-> Druid controller")
            wire("druid_split_pick", {"searcher": owner_s, "one_oid": x["ref"]})
            await _submit_land_picks(c, opp, rtype, data, [x], tag)
            return True
        # Land candidates we cannot classify: record, do not guess.
        RECORDED_OPPS.add(iid)
        say(f"[{tag}] land-candidate prompt did not match search/split "
            f"-- recorded, not answered; "
            f"cands={[(x['ref'], x['name'], x['zone'], x['owner']) for x in lands]}")
        wire("druid_land_unanswered",
             {"iid": str(iid)[:12],
              "cands": [(x['ref'], x['name'], x['zone'], x['owner']) for x in lands]})
        return True
    return False


def find_snatch_target_opp(st):
    """Vi opportunity whose pure candidates include the Druid stack spell."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        for ch in items:
            if not _is_pure_candidate(ch):
                continue
            ref = _cand_ref(ch)
            if ref is None:
                continue
            o = get_obj(st["state"], ref)
            if o.get("zone") == "Stack" and obj_lname(st["state"], ref) == DRUID:
                return opp, ch
    return None, None


async def answer_snatch_target(c, st, tag, ctx):
    """Answer Aethersnatch's TargetSelection with the Druid stack spell."""
    opp, ch = find_snatch_target_opp(st)
    if opp is None:
        return False
    iid = opp.get("interactionId")
    if iid in SUBMITTED_OPPS:
        if iid in RETRIED_OPPS:
            return False
        prog = opp.get("progress", {}) or {}
        if int(prog.get("selected", 0) or 0) > 0:
            return False
        RETRIED_OPPS.add(iid)
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    dump_druid_opp(st["state"], opp, "snatch-target", tag, ctx)
    SUBMITTED_OPPS.add(iid)
    if rtype == "schema":
        spec = (data.get("spec", {}) or {}).get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": spec,
                            "data": {"choiceIds": [ch.get("id")]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": ch.get("id")}}}
    ctx["snatch_targeted"] = True
    say(f"[{tag}] Aethersnatch targets the Druid stack spell "
        f"(ref {_cand_ref(ch)})")
    wire("snatch_target", {"ref": _cand_ref(ch)})
    await interact_as(c, sub, tag)
    return True

LEG_CONFIG = {
    # leg: players, expected Druid controller, searchers, cast gate.
    # Leg A gates on P0's turn_number (>=3: P0's 3rd turn, 3 Forests).
    # Leg B gates on P1's untapped Islands (>=6: Aethersnatch mana) --
    # turn_number counts individual player turns, NOT rounds, so a turn
    # gate cannot express "P1 has 6 lands" (attempt 1 used turn>=7 and
    # P0 cast while P1 had only 3 Islands, so Snatch was unaffordable).
    "A": {"players": 2, "druid_controller": 0, "searchers": [1],
          "cast_turn": 3, "island_gate": None},
    "B": {"players": 4, "druid_controller": 1, "searchers": [0, 2, 3],
          "cast_turn": None, "island_gate": 6},
}


def cast_gate_ok(ctx, state):
    if ctx["leg"] == "A":
        return int(state.get("turn_number", 0)) >= ctx["cast_turn"]
    return untapped_of_name(state, 1, ISLAND) >= ctx["island_gate"]


def new_ctx(leg, attempt):
    cfg = LEG_CONFIG[leg]
    return {
        "leg": leg,
        "attempt": attempt,
        "players": cfg["players"],
        "druid_controller_expected": cfg["druid_controller"],
        "searchers": list(cfg["searchers"]),
        "cast_turn": (cfg["cast_turn"] + (attempt - 1) * 2
                      if cfg["cast_turn"] is not None else None),
        "island_gate": (cfg["island_gate"] + (attempt - 1)
                        if cfg["island_gate"] is not None else None),
        "druid_cast": False,
        "druid_oid": None,
        "druid_controller_actual": None,
        "druid_entered": False,
        "snatch_cast": False,
        "snatch_targeted": False,
        "search_picks": {},
        "split_picks": {},
        "search_accepted": set(),
        "druid_prompts_seen": 0,
        "pre_exported": False,
        "post_exported": False,
        "settle_ticks": 0,
    }


def druid_spell_on_stack(state, pid):
    """True if a Rootweaver Druid spell controlled by `pid` is on the stack."""
    for e in stack_entries(state):
        ctrl = e.get("controller") if isinstance(e, dict) else None
        if str(ctrl) == str(pid) \
                and stack_entry_name(state, e).lower() == DRUID:
            return True
    return False


async def try_cast(c, state, acts, tag, name, needs, pid=0):
    """Cast `name` for seat `pid` if in hand and affordable; set mana needs."""
    if name not in hand_names(state, pid):
        return False
    a, oid = cast_action_for(acts, state, name)
    if a is None:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid, "needs": needs})
    await submit_as_is(c, a)
    return True


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

    # --- Aethersnatch (Leg B, P1): target the Druid stack spell.
    if ctx["leg"] == "B" and pid == 1 and ctx.get("snatch_cast") \
            and not ctx.get("snatch_targeted"):
        if await answer_snatch_target(c, st, tag, ctx):
            return

    # --- Druid resolution prompts (any seat).
    if ctx.get("druid_cast"):
        if await answer_druid_opp(c, st, pid, tag, ctx):
            return

    # --- P1 casts Aethersnatch in response to the Druid spell (Leg B).
    if ctx["leg"] == "B" and pid == 1 and not ctx.get("snatch_cast") \
            and druid_spell_on_stack(state, 0) \
            and SNATCH in hand_names(state, 1) \
            and untapped_of_name(state, 1, ISLAND) >= 6:
        if await try_cast(c, state, acts, tag, SNATCH,
                          {"U": 2, "generic": 4}, pid=1):
            ctx["snatch_cast"] = True
            return

    # --- P0 casts the Druid once the cast gate is reached.
    if pid == 0 and not ctx.get("druid_cast") and p0_main(state, 0) \
            and cast_gate_ok(ctx, state):
        if DRUID not in hand_names(state, 0):
            # No Druid yet: keep developing (land) and wait.
            if await play_named_land(c, state, 0, acts, tag, FOREST):
                return
        else:
            if not ctx.get("pre_exported"):
                pre = json.loads(await c.export_state())
                with open(f"{EVDIR}/pre_{ctx['leg']}.json", "w") as f:
                    json.dump(pre, f, indent=1)
                ctx["pre_exported"] = True
                wire("pre_export", {"leg": ctx["leg"]})
                say(f"[{tag}] PRE_{ctx['leg']} exported; casting Druid "
                    f"(cast gate: turn>={ctx['cast_turn']} / "
                    f"islands>={ctx['island_gate']})")
            if untapped_of_name(state, 0, FOREST) >= 3:
                if await try_cast(c, state, acts, tag, DRUID,
                                  {"G": 1, "generic": 2}, pid=0):
                    ctx["druid_cast"] = True
                    ctx["druid_cast_at"] = time.time()
                    return
            # Cast not submitted this tick: fall through so this seat
            # can still pass priority / play a land instead of stalling.

    # --- Setup land plays (before the Druid is cast).
    if not ctx.get("druid_cast") and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and int(state.get("active_player", -1)) == pid \
            and not (state.get("stack") or []):
        if pid == 0:
            if await play_named_land(c, state, 0, acts, tag, FOREST):
                return
        elif ctx["leg"] == "B" and pid == 1:
            if await play_named_land(c, state, 1, acts, tag, ISLAND):
                return
        elif ctx["leg"] == "A" and pid == 1:
            if await play_named_land(c, state, 1, acts, tag, FOREST):
                return
        # P2/P3 in Leg B never play lands (keep libraries full).

    # Track the Druid entering the battlefield (any seat's view).
    if ctx.get("druid_cast") and not ctx.get("druid_entered"):
        for ctrl in range(ctx["players"]):
            oids = bf_named(state, ctrl, DRUID)
            if oids:
                ctx["druid_entered"] = True
                ctx["druid_oid"] = oids[0]
                ctx["druid_controller_actual"] = ctrl
                say(f"[{tag}] Druid entered under P{ctrl} (oid {oids[0]}); "
                    f"expected P{ctx['druid_controller_expected']}")
                wire("druid_entered", {"oid": oids[0], "controller": ctrl})
                break

    if decision_pending(st):
        key = (tag, "prompt", c.revision)
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("prompt", {"who": tag, "rev": c.revision,
                            "opps": _opp_summary(st)})
            say(f"[{tag}] prompt: {_opp_summary(st)} -- recorded, "
                f"not answered")
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision

# ------------------------------------------------------------------- run

LEG_DECKS = {
    "A": [
        deck((DRUID, 25), (FOREST, 35)),
        deck((FOREST, 60)),
    ],
    "B": [
        deck((DRUID, 25), (FOREST, 35)),
        deck((SNATCH, 12), (ISLAND, 48)),
        deck((FOREST, 60)),
        deck((FOREST, 60)),
    ],
}


async def run_leg(leg, attempt):
    global PASSED_REV
    PASSED_REV = {}
    MULLS.clear()
    SUBMITTED_OPPS.clear()
    LAND_PLAYED_TURN.clear()
    RECORDED_OPPS.clear()
    RETRIED_OPPS.clear()
    ctx = new_ctx(leg, attempt)
    tags = []
    clients = []
    for pid in range(ctx["players"]):
        tag = f"P{pid}{leg}{attempt}"
        tags.append(tag)
        ST["mana_needs"][tag] = {}
    clients.append(PhaseClient(f"P0{leg}{attempt}"))
    await clients[0].connect()
    await clients[0].create(LEG_DECKS[leg][0],
                             player_count=ctx["players"])
    for pid in range(1, ctx["players"]):
        c = PhaseClient(f"P{pid}{leg}{attempt}")
        await c.connect()
        await c.join(clients[0].game_code, LEG_DECKS[leg][pid])
        clients.append(c)
    say(f"leg {leg} attempt={attempt} game {clients[0].game_code}; seats="
        f"{[c.player_id for c in clients]}")
    wire("game_start", {"leg": leg, "attempt": attempt,
                        "game_code": clients[0].game_code,
                        "seats": [c.player_id for c in clients]})
    obs = {"leg": leg, "attempt": attempt, "assert": {}, "notes": [],
           "game_code": clients[0].game_code}
    for k in ("A1_setup_ok", "A2_searches_offered",
              "A3_one_under_druid", "A4_rest_under_searcher", "A5_cleanup"):
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
        # Leg B needs P1 to reach 6+ Islands (P1's 6th turn) before P0
        # can cast, so its turn cap is higher than Leg A's.
        turn_cap = 45 if leg == "B" else 25
        if st_now and st_now["state"].get("turn_number", 0) > turn_cap \
                and not ctx.get("druid_cast"):
            say(f"[leg {leg}] turn {st_now['state'].get('turn_number')} "
                f"exceeded without casting the Druid; abandoning leg")
            obs["notes"].append(
                "abandoned: game reached turn "
                f"{st_now['state'].get('turn_number')} without casting "
                "the Druid (setup never completed)")
            break
        if ctx.get("druid_cast") \
                and time.time() - ctx.get("druid_cast_at", 0) > 300 \
                and not ctx.get("post_exported"):
            say(f"[leg {leg}] Druid cast 300s ago, resolution never "
                f"completed; abandoning leg")
            obs["notes"].append(
                "abandoned: Druid cast but its ETB resolution never "
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
                    f"druid_cast={ctx.get('druid_cast')} "
                    f"entered={ctx.get('druid_entered')} "
                    f"ctrl={ctx.get('druid_controller_actual')} "
                    f"snatch={ctx.get('snatch_cast')}/"
                    f"{ctx.get('snatch_targeted')} "
                    f"picks={ctx.get('search_picks')} "
                    f"split={ctx.get('split_picks')}")
        # --- completion: export POST once the ETB resolution has
        # observably settled: Druid on BF, stack empty on every view,
        # no Druid prompt pending on any view, for 3 consecutive ticks.
        if ctx.get("druid_entered") and not ctx.get("post_exported"):
            latests = [c.latest for c in clients]
            if all(x is not None for x in latests) \
                    and all(not (x["state"].get("stack") or [])
                            for x in latests) \
                    and not any(druid_pending(x) for x in latests):
                ctx["settle_ticks"] += 1
                if ctx["settle_ticks"] >= 3:
                    say(f"[leg {leg}] ETB resolution settled; exporting POST")
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


def evaluate_leg(obs, ctx):
    """Score A1-A5 for one leg from PRE/POST + ctx."""
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
    obs["ctx"] = {"druid_controller_actual": ctx.get("druid_controller_actual"),
                  "druid_controller_expected": ctx.get("druid_controller_expected"),
                  "druid_oid": ctx.get("druid_oid"),
                  "snatch_cast": ctx.get("snatch_cast"),
                  "snatch_targeted": ctx.get("snatch_targeted"),
                  "search_picks": {str(k): v for k, v in ctx.get("search_picks", {}).items()},
                  "split_picks": {str(k): v for k, v in ctx.get("split_picks", {}).items()},
                  "search_accepted": sorted(ctx.get("search_accepted", set())),
                  "druid_prompts_seen": ctx.get("druid_prompts_seen", 0)}

    # ---- A1: setup + Druid under the expected controller.
    a1 = "not-run"
    pre_lib = {}
    if pre_state:
        druid_in_hand = DRUID in hand_names(pre_state, 0)
        libs_ok = True
        for s in ctx["searchers"]:
            lib_oids = [str(x) for x in
                        (player_of(pre_state, s).get("library") or [])]
            pre_lib[s] = lib_oids
            n_basic = sum(1 for oid in lib_oids
                          if obj_lname(pre_state, oid) in BASIC_LANDS)
            if n_basic < 3:
                libs_ok = False
                obs["notes"].append(
                    f"A1 leg {leg}: searcher P{s} has only {n_basic} basics "
                    f"in PRE library")
        if druid_in_hand and libs_ok:
            a1 = "passed"
            obs["notes"].append(
                f"A1 leg {leg} passed (setup): Druid in P0 hand in PRE; "
                f"each searcher has >=3 basics in PRE library")
        else:
            a1 = "failed"
            obs["notes"].append(
                f"A1 leg {leg} FAILED: druid_in_hand={druid_in_hand} "
                f"libs_ok={libs_ok}")
    else:
        obs["notes"].append(f"A1 leg {leg} not-run: no PRE export")
    obs["assert"]["A1_setup_ok"] = a1
    if ctx.get("druid_entered"):
        if ctx.get("druid_controller_actual") != ctx.get("druid_controller_expected"):
            obs["assert"]["A1_setup_ok"] = "failed"
            obs["notes"].append(
                f"A1 leg {leg} FAILED (controller): Druid entered under "
                f"P{ctx.get('druid_controller_actual')}, expected "
                f"P{ctx.get('druid_controller_expected')} "
                f"(snatch_cast={ctx.get('snatch_cast')}, "
                f"snatch_targeted={ctx.get('snatch_targeted')})")
        else:
            obs["notes"].append(
                f"leg {leg}: Druid entered under expected "
                f"P{ctx.get('druid_controller_actual')}")
    elif ctx.get("druid_cast"):
        obs["notes"].append(
            f"leg {leg}: Druid cast but never observed on the BF")

    # ---- Found lands: PRE-library oids on the BF in POST.
    found = {}
    if pre_state and post_state:
        for s in ctx["searchers"]:
            found[s] = [oid for oid in pre_lib.get(s, [])
                        if obj_zone(post_state, oid) == "Battlefield"]
        obs["found"] = {str(k): v for k, v in found.items()}
        obs["found_controllers"] = {
            str(s): {str(get_obj(post_state, oid).get("controller")): None
                     for oid in oids} for s, oids in found.items()}
        obs["found_detail"] = {
            str(s): [(oid, str(get_obj(post_state, oid).get("controller")),
                      bool(get_obj(post_state, oid).get("tapped")))
                     for oid in oids] for s, oids in found.items()}
    else:
        for s in ctx["searchers"]:
            found[s] = []
        obs["notes"].append(
            f"leg {leg}: found-land computation skipped (missing "
            f"PRE/POST)")

    # ---- A2: searches offered (each searcher found >=1 land).
    if pre_state and post_state and ctx.get("druid_entered"):
        if all(len(found.get(s, [])) >= 1 for s in ctx["searchers"]):
            obs["assert"]["A2_searches_offered"] = "passed"
            obs["notes"].append(
                f"A2 leg {leg} passed: each searcher found "
                f"{ {str(s): len(found[s]) for s in ctx['searchers']} } "
                f"land(s)")
        else:
            obs["assert"]["A2_searches_offered"] = "failed"
            obs["notes"].append(
                f"A2 leg {leg} FAILED: found counts "
                f"{ {str(s): len(found[s]) for s in ctx['searchers']} } "
                f"(trigger resolved but a search produced nothing -- "
                f"the effect does nothing for that seat)")
    elif not ctx.get("druid_entered"):
        obs["assert"]["A2_searches_offered"] = "not-run"
        obs["notes"].append(f"A2 leg {leg} not-run: Druid never entered")
    else:
        obs["assert"]["A2_searches_offered"] = "not-run"
        obs["notes"].append(f"A2 leg {leg} not-run: missing PRE/POST")

    # ---- A3/A4: the split.
    if pre_state and post_state and ctx.get("druid_entered") \
            and obs["assert"]["A2_searches_offered"] == "passed":
        dctrl = ctx.get("druid_controller_actual")
        a3_ok = True
        a4_ok = True
        for s in ctx["searchers"]:
            oids = found.get(s, [])
            n_druid = sum(1 for oid in oids
                          if str(get_obj(post_state, oid).get("controller")) == str(dctrl))
            n_self = sum(1 for oid in oids
                         if str(get_obj(post_state, oid).get("controller")) == str(s))
            if n_druid != 1:
                a3_ok = False
                obs["notes"].append(
                    f"A3 leg {leg} FAILED for searcher P{s}: {n_druid} of "
                    f"{len(oids)} found lands under Druid controller "
                    f"P{dctrl} (expected exactly 1); detail="
                    f"{obs['found_detail'][str(s)]}")
            if n_self != len(oids) - 1:
                a4_ok = False
                obs["notes"].append(
                    f"A4 leg {leg} FAILED for searcher P{s}: {n_self} of "
                    f"{len(oids)} found lands under P{s} (expected "
                    f"{len(oids) - 1}); detail={obs['found_detail'][str(s)]}")
        obs["assert"]["A3_one_under_druid"] = "passed" if a3_ok else "failed"
        obs["assert"]["A4_rest_under_searcher"] = "passed" if a4_ok else "failed"
        if a3_ok and a4_ok:
            obs["notes"].append(
                f"A3/A4 leg {leg} passed: every searcher has exactly 1 "
                f"found land under P{dctrl} and the rest under themselves")
    else:
        for k in ("A3_one_under_druid", "A4_rest_under_searcher"):
            obs["assert"][k] = "not-run"
        obs["notes"].append(
            f"A3/A4 leg {leg} not-run: A2 not passed or Druid never entered")

    # ---- A5: cleanup.
    if post_state and ctx.get("druid_entered"):
        stack_empty = not (post_state.get("stack") or [])
        druid_bf = bool(bf_named(post_state,
                                 ctx.get("druid_controller_actual"), DRUID))
        all_tapped = all(
            bool(get_obj(post_state, oid).get("tapped"))
            for s in ctx["searchers"] for oid in found.get(s, []))
        if stack_empty and druid_bf and all_tapped:
            obs["assert"]["A5_cleanup"] = "passed"
            obs["notes"].append(
                f"A5 leg {leg} passed: POST stack empty, Druid on BF, "
                f"all found lands tapped")
        else:
            obs["assert"]["A5_cleanup"] = "failed"
            obs["notes"].append(
                f"A5 leg {leg} FAILED: stack_empty={stack_empty} "
                f"druid_on_bf={druid_bf} all_found_tapped={all_tapped}")
    else:
        obs["assert"]["A5_cleanup"] = "not-run"
        obs["notes"].append(f"A5 leg {leg} not-run: no POST or no Druid entry")

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
    img = Image.new("RGB", (1000, 760), (18, 18, 22))
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), "phase-rs/phase #9861 -- Rootweaver Druid split uses wrong seats", font=font_t, fill=(255, 255, 255))
    y += 40
    d.text((24, y), "v0.109.0 (932e389, protocol 140) | Leg A: 2P caster==controller; Leg B: 4P Aethersnatch", font=font, fill=(180, 180, 190))
    y += 28
    d.text((24, y), f"Run: {RUN_ID}", font=font, fill=(180, 180, 190))
    y += 40
    verdict = str(combined.get("verdict", "blocked")).upper().replace("_", "-")
    if verdict not in ("REPRODUCED", "NOT-REPRODUCED", "BLOCKED"):
        verdict = "BLOCKED / NOT-RUN"
    color = (255, 90, 90) if verdict == "REPRODUCED" else ((90, 255, 140) if verdict == "NOT-REPRODUCED" else (255, 200, 60))
    d.text((24, y), f"Verdict: {verdict}", font=font_b, fill=color)
    y += 40
    for leg in ("A", "B"):
        lo = combined.get("legs", {}).get(leg, {})
        a = lo.get("assert", {})
        d.text((24, y), f"Leg {leg} (game {lo.get('game_code')}):", font=font_b, fill=(220, 220, 230))
        y += 28
        for k in ("A1_setup_ok", "A2_searches_offered",
                  "A3_one_under_druid", "A4_rest_under_searcher", "A5_cleanup"):
            v = a.get(k, "not-run")
            vc = (90, 255, 140) if v == "passed" else ((255, 90, 90) if v == "failed" else (255, 200, 60))
            d.text((48, y), f"{k}: {v}", font=font_b, fill=vc)
            y += 28
        detail = lo.get("found_detail", {})
        d.text((48, y), f"Found lands (oid, controller, tapped): {detail}", font=font, fill=(220, 220, 230))
        y += 28
        d.text((48, y), f"Druid controller actual/expected: {lo.get('ctx', {}).get('druid_controller_actual')}/{lo.get('ctx', {}).get('druid_controller_expected')}  snatch={lo.get('ctx', {}).get('snatch_cast')}/{lo.get('ctx', {}).get('snatch_targeted')}", font=font, fill=(220, 220, 230))
        y += 34
    y += 6
    d.text((24, y), "Expected (oracle): per searching opponent, 1 found land under the", font=font, fill=(220, 220, 230))
    y += 26
    d.text((24, y), "Druid controller (tapped) and the rest under that opponent (tapped).", font=font, fill=(220, 220, 230))
    img.save(f"{EVDIR}/summary.png")
    say("summary.png rendered from saved assertions")


async def main():
    await verify_server_hello()
    t_start = time.time()
    legs = {}
    for leg in ("A", "B"):
        obs, ctx = await run_leg(leg, 1)
        if obs["assert"].get("A1_setup_ok") in ("failed", "not-run") \
                and not ctx.get("druid_cast"):
            say(f"leg {leg} attempt 1 never cast the Druid; running attempt 2")
            obs, ctx = await run_leg(leg, 2)
        legs[leg] = obs

    # ---- combined verdict per the pre-registered rule.
    def leg_reproduced(lo):
        a = lo["assert"]
        return a.get("A1_setup_ok") == "passed" and (
            a.get("A2_searches_offered") == "failed"
            or a.get("A3_one_under_druid") == "failed"
            or a.get("A4_rest_under_searcher") == "failed")

    def leg_clean(lo):
        a = lo["assert"]
        return all(a.get(k) == "passed" for k in
                   ("A1_setup_ok", "A2_searches_offered",
                    "A3_one_under_druid", "A4_rest_under_searcher",
                    "A5_cleanup"))

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
            "rootweaver druid": (CARD_DATA.get("rootweaver druid") or {}).get("oracle_text"),
            "aethersnatch": (CARD_DATA.get("aethersnatch") or {}).get("oracle_text"),
            "forest": (CARD_DATA.get("forest") or {}).get("oracle_text"),
            "island": (CARD_DATA.get("island") or {}).get("oracle_text"),
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
            + f"; found={lo.get('found_detail')}; "
            + f"druid_ctrl={lo.get('ctx', {}).get('druid_controller_actual')} "
            + f"(expected {lo.get('ctx', {}).get('druid_controller_expected')}); "
            + f"snatch={lo.get('ctx', {}).get('snatch_cast')}/"
            + f"{lo.get('ctx', {}).get('snatch_targeted')}")
    run = {
        "issue": ISSUE,
        "issue_url": "https://github.com/phase-rs/phase/issues/9861",
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_start)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(time.time() - t_start, 1),
        "server_identity": SERVER_IDENTITY,
        "server_run_dir": "backfill-owned v0.109.0 server on 127.0.0.1:9374 "
                          "(--single-user --no-data-download, verified release); "
                          f"evidence under evidence/9861/{RUN_ID}",
        "driver": {"protocol_advertised": 140, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "leg_A_P0": [["Rootweaver Druid", 25], ["Forest", 35]],
            "leg_A_P1": [["Forest", 60]],
            "leg_B_P0": [["Rootweaver Druid", 25], ["Forest", 35]],
            "leg_B_P1": [["Aethersnatch", 12], ["Island", 48]],
            "leg_B_P2": [["Forest", 60]],
            "leg_B_P3": [["Forest", 60]],
        },
        "assertions": {f"leg_{leg}_{k}": v
                       for leg, lo in legs.items()
                       for k, v in lo["assert"].items()},
        "notes": [f"leg {leg}: {n}" for leg, lo in legs.items()
                  for n in lo["notes"]],
        "verdict": verdict,
        "result": " | ".join(result_lines),
        "scope": ("Rootweaver Druid ETB 'each opponent may search for up to "
                  "three basic lands; one under your control, rest under "
                  "theirs' split: Leg A 2-player caster==controller "
                  "baseline; Leg B 4-player with Aethersnatch moving the "
                  "Druid spell to P1 (caster != controller). Native "
                  "engine, human-client seats."),
        "limitations": [
            "Browser UI not exercised; native engine via human-client seats.",
            "States are authoritative exports, restorable only via full game replay (the phase-server has no standalone state-import path).",
            "25x/12x deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Verdict rule: not-reproduced = both legs A1-A5 passed. reproduced = any leg has A1 passed + trigger resolved + (A2 failed [no search effect] or A3/A4 failed [wrong-seat split]). blocked = setup never completed, Leg B Aethersnatch never transferred control while Leg A passed clean, or prompts never offered.",
        ],
        "setup_line": ("Leg A: P0 25x Rootweaver Druid + 35x Forest; P1 60x Forest. "
                       "Leg B: P0 25x Rootweaver Druid + 35x Forest; P1 12x Aethersnatch + 48x Island; P2/P3 60x Forest. "
                       "P0 casts Druid (Leg A turn >=3, Leg B turn >=7); Leg B P1 Aethersnatches it."),
        "contract_line": ("Per searching opponent: 1 found land under the Druid controller (tapped), rest under that opponent (tapped). "
                          "Bug: lands end up under the wrong seats when the Druid is under another player's control."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    with open(f"{EVDIR}/card_entries.json", "w") as f:
        json.dump({k: CARD_DATA.get(k) for k in
                   ("rootweaver druid", "aethersnatch", "forest", "island")},
                  f, indent=1, default=str)
    with open(f"{EVDIR}/scenario_9861.py", "w") as f:
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
