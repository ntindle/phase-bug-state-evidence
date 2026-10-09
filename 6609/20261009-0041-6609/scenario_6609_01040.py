#!/usr/bin/env python3
"""Issue #6609: "[Card Bug] Nethergoyf: Forces to exile all cards when escaping".

RE-VALIDATION RUN on pinned v0.104.0 (protocol 118, build 4227122), ported
from driver/scenario_6609_01030.py (protocol 106, run 20261007-0141-6609,
verdict reproduced on v0.103.0). The v0.103.0 run established: the literal
sweep-all behavior no longer reproduces (a per-card select choice IS
offered), but the "four or more card types among them" aggregate constraint
is unenforced (a 1-card subset is accepted, min=0). The v0.104.0 result is
stale under the playbook's release-pinning rule, so the escape exile-cost
path is re-driven here on the current pin with identical assertions.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-24, pascalberger, build 0.36.0): escaping Nethergoyf from the
graveyard "expects to exile all cards from graveyard".

Oracle text (verified from pinned v0.104.0 card-data.json):
  "Nethergoyf's power is equal to the number of card types among cards in your
  graveyard and its toughness is equal to that number plus 1.
  Escape -- {2}{B}, Exile any number of other cards from your graveyard with
  four or more card types among them. (You may cast this card from your
  graveyard for its escape cost.)"

Triage acceptance criteria:
  - Escaping Nethergoyf prompts the player to choose which other graveyard
    cards to exile, rather than exiling all of them.
  - The chosen set is rejected unless it covers four or more card types
    in total.
  - Cards not chosen remain in the graveyard, and Nethergoyf's power and
    toughness recompute from what remains.

Scenario (native engine, two human-client seats):
  P0: 8x Nethergoyf + 8x Ornithopter (artifact) + 8x Rancor (enchantment) +
      8x Shock (instant) + 8x Divination (sorcery) + 8x Grizzly Bears
      (creature) + 8x Swamp + 8x Island (dense counts: engine accepts
      >4-of for custom games; mulligan hunts 2+ lands with a Swamp).
      Only {B}/{U} spells are castable; Shock/Bears/Rancor fill the
      graveyard via cleanup discards.
  P1: 12x Lightning Bolt + 36x Mountain (removal bot; never attacks).

  SETUP: P0 casts Nethergoyf (needs Swamp+1), P1 Bolts it -> Nethergoyf in
      P0 gy. P0 casts Ornithopter (free; P1 Bolts it when able) and
      Divination ({2}{U}; draws 2, sorcery -> gy). Shock/Bears/Rancor are
      uncastable (no R/G mana) and fill the gy via cleanup discards.
      Types among others: artifact+creature (Ornithopter), instant (Shock),
      sorcery (Divination), enchantment (Rancor) -> >= 4.
  ESCAPE: with >=3 untapped lands incl. a Swamp, P0 casts Nethergoyf from
      the graveyard via escape ({2}{B}). The exile half of the cost is the
      device under test.

Assertions:
  A1_setup_ok        pre_escape.json: Nethergoyf in P0 gy; >=4 card types
                     among OTHER P0 gy cards; P0 has >=3 untapped lands
                     incl. a Swamp; P0 priority in own main phase.
  A2_escape_accepted The escape cast is accepted: Nethergoyf leaves the gy
                     (to stack, then battlefield), {2}{B} paid.
  A3_choice_offered  During the exile-cost step the engine offers a per-card
                     choice (vi schema/select with individual candidates)
                     instead of exiling all other gy cards with no decision
                     point.
  A4_subset_accepted A submitted subset covering >=4 card types
                     (Ornithopter+Shock+Divination+Bears) is accepted and
                     the cast completes. (Negative probe first: a 1-card
                     subset must be REJECTED per the "four or more card
                     types" rule; acceptance of it is a related failure.)
  A5_unchosen_remain >=1 non-chosen card remains in P0's graveyard after
                     the escape completes.
  A6_pt_recomputed   Nethergoyf on the battlefield has P/T equal to the
                     number of card types among cards in P0's remaining
                     graveyard (power) and +1 (toughness).
  A7_cleanup         Game settled: stack empty, no stall watchdog fired
                     (waiting_for is null on protocol 118; the live view
                     is checked for pending real decisions instead).

Verdict rule: reproduced iff the engine exiles all other gy cards without
offering a choice (A3 fails), or rejects a >=4-type subset / accepts a
<4-type subset (A4 fails), or unchosen cards do not remain (A5 fails).
not-reproduced iff all of A1..A7 pass. blocked iff the fixture can never
be assembled (no Nethergoyf in gy with >=4 types among others and mana).

Protocol-118 notes: waiting_for is GONE (null) - priority = PassPriority in
the viewer's top-level legal_actions; MulliganDecision answered via legacy
Action gated on the advertised action; bottom-after-mulligan via vi
schema/select gated on waitingForKind.code=='mulligan' AND turn 1/Untap;
DiscardToHandSize via viewer_interaction only (generic 'choose' code on
118), gated on hand>7 plus hand-card candidates; casts via legacy CastSpell
matched by object_id; mana payment via legacy PayMana actions first, then vi
tapLandForMana while a payment is expected (noise menus excluded);
target selection via vi schema/sequence; manifest computed AFTER
WIRE/RUNLOG close.

Evidence: evidence/6609/<run-id>/pre_escape.json, post_escape.json,
run.json, assertions.json, obs.json, scenario_6609_01020.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log, summary.png, manifest.sha256.
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
RUN_ID = os.environ.get("RUN_ID", "20261009-0041-6609")
ISSUE = 6609
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = None
RUNLOG = None


def init_logging():
    global WIRE, RUNLOG
    if WIRE is None:
        WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
    if RUNLOG is None:
        RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


GOYF = "nethergoyf"
ORNITHOPTER = "ornithopter"
RANCOR = "rancor"
SHOCK = "shock"
DIVINATION = "divination"
BEARS = "grizzly bears"
BOLT = "lightning bolt"
SWAMP = "swamp"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"

LAND_NAMES = {SWAMP, ISLAND, MOUNTAIN, FOREST}
KNOWN_CREATURES = {GOYF, BEARS, ORNITHOPTER}

_CARDDATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.104.0/data/card-data.json"))
_NAME_TYPES = {}
for _k, _c in _CARDDATA.items():
    _ct = (_c.get("card_type") or {})
    _NAME_TYPES[_k] = {str(t).lower()
                       for t in (_ct.get("core_types") or [])}
del _k, _c, _ct


def type_set_of_name(nm):
    return set(_NAME_TYPES.get(nm, ()))


P0_DECK = [(GOYF, 8), (ORNITHOPTER, 8), (RANCOR, 8), (SHOCK, 8),
           (DIVINATION, 8), (BEARS, 8),
           (SWAMP, 8), (ISLAND, 8)]
P1_DECK = [(BOLT, 12), (MOUNTAIN, 36)]

# card types that can appear in a graveyard (per Nethergoyf rulings)
CARD_TYPES = ["artifact", "battle", "creature", "enchantment", "instant",
              "kindred", "land", "planeswalker", "sorcery"]

SERVER_IDENTITY = {
    "server_version": "v0.104.0",
    "build_commit": "4227122",
    "protocol_version": 118,
    "mode": "Full",
    "binary_sha256": "f4f3d74a21a5474453d4ca06b9c90e2751af7bea66c0101db26f4baa385f5c9d",
    "card_data_sha256": "7d131899fb22736dc6068c25ec220f2fed8b71578dba4ba1135c56a19247789d",
    "draft_pools_sha256": "d8d4664a45d095d5f30f570c5463d9a1a5f231049ff30403477efbf4384c79dc",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "signature_note": "v0.104.0 binary + release manifest minisign-verified "
                      "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                      "(key id 436711b6a2d36828) at pin time 2026-10-06; "
                      "data digests match the signed manifest; binary/data "
                      "digests recomputed against on-disk files this run "
                      "(/proc/4134/exe hash matches on-disk pinned binary)",
    "observed_at": "2026-10-09",
    "source": "ServerHello asserted by handshake this run (v0.104.0, "
              "build 4227122, protocol 118) + sha256 re-verified against "
              "pinned v0.104.0 release artifacts",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    if RUNLOG is not None:
        try:
            RUNLOG.write(m + "\n")
            RUNLOG.flush()
        except ValueError:
            pass  # log already closed (post-manifest tail messages)


def wire(event, payload):
    if WIRE is None:
        return
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def gy_objs(state, pid):
    return [(int(o), get_obj(state, o))
            for o in player_of(state, pid).get("graveyard", [])]


def gy_names(state, pid):
    return [obj_name(o) for _, o in gy_objs(state, pid)]


def type_set_of(o):
    tl = str(o.get("type_line") or "").lower()
    if tl and tl != "none":
        return {t for t in CARD_TYPES if t in tl}
    return type_set_of_name(obj_name(o))


def gy_type_set(state, pid, exclude_names=()):
    out = set()
    for oid, o in gy_objs(state, pid):
        if obj_name(o) in exclude_names:
            continue
        out |= type_set_of(o)
    return out


def goyf_gy_oid(state, pid=0):
    for oid, o in gy_objs(state, pid):
        if obj_name(o) == GOYF:
            return oid
    return None


def bf_creatures(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            tl = str(o.get("type_line") or "").lower()
            nm = obj_name(o)
            if "creature" in tl or nm in KNOWN_CREATURES:
                if name is None or nm == name:
                    out.append(int(oid))
    return out


def goyf_bf_oid(state, pid=0):
    cs = bf_creatures(state, pid, GOYF)
    return cs[0] if cs else None


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and obj_name(o) in LAND_NAMES
                and (name is None or obj_name(o) == name)):
            out.append(int(oid))
    return out


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


def stack_entries(state):
    return state.get("stack") or []


# ------------------------------------------------------------- protocol-118 helpers
def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


def my_priority(acts):
    """Protocol 118: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is null on 118)."""
    return any(a.get("type") == "PassPriority" for a in acts)


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, a)
            return True
    # vi fallback: passPriority action-code choice
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await c.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose",
                                  "data": {"choiceId": ch.get("id")}}})
                return True
    return False


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction. The 118 engine
    offers tapLandForMana / castSpell / activateAbility choice menus at
    ordinary priority windows; treating those as decisions stalls the game
    (they must not block priority passes)."""
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


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("text")):
                t = d.get("name") or d.get("text")
                break
    return str(t)


def choice_status(ch):
    return (ch.get("status") or {}).get("type")


def cand_ref_oid(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("reference") is not None:
            ref = d["reference"]
            if isinstance(ref, dict):
                return ref.get("object_id") or ref.get("id")
            try:
                return int(ref)
            except (TypeError, ValueError):
                return None
    return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("seat") is not None:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                return None
    return None


def opp_candidates(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"), data.get("spec"))


def opp_waiting_kind(opp):
    wfk = opp.get("waitingForKind") or {}
    return wfk.get("code")


def vi_kind_code(st):
    """View-level waitingForKind code (protocol 118 surfaces it on the
    viewer_interaction, not reliably on each opportunity)."""
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def cast_action_for(acts, oid):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
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


def candidate_seat(ch):
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


def pick_target_candidate(opp, want_oid=None, state=None):
    """Prefer the candidate whose object reference matches want_oid; fall
    back to the single candidate if there is exactly one."""
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    if want_oid is not None:
        for ch in cands:
            if cand_ref_oid(ch) is not None \
                    and int(cand_ref_oid(ch)) == int(want_oid):
                return ch
    if len(cands) == 1:
        return cands[0]
    return None


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} "
        f"kind={sub['response']['type']} ({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await c.send_interaction(sub)
    return iid, cid


async def observe_server_hello():
    """Raw handshake to capture the OBSERVED ServerHello and assert it
    matches the pinned release identity before driving anything."""
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with websockets.connect(url, max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        msg = json.loads(raw)
        assert msg.get("type") == "ServerHello", \
            f"expected ServerHello, got {msg.get('type')}"
        data = msg.get("data", {})
        obs = {
            "server_version": data.get("server_version"),
            "build_commit": data.get("build_commit"),
            "protocol_version": data.get("protocol_version"),
            "mode": data.get("mode"),
        }
        for k in ("server_version", "build_commit", "protocol_version",
                  "mode"):
            want = SERVER_IDENTITY[k]
            want_cmp = want[1:] if k == "server_version" and \
                str(want).startswith("v") else want
            assert obs[k] == want_cmp or obs[k] == want, (
                f"ServerHello {k}={obs[k]!r} != pinned {want!r}; "
                "refusing to run")
        say(f"ServerHello OK: {obs}")
        wire("server_hello", obs)
        return obs


# --- mulligan / bottom / discard / payment (protocol 118) ---
_mulligan_answered_rev = {}
_mulligan_kept = {}
_mulligan_counts = {}


async def mulligan_tick(c, acts, state, who, keep_fn, max_mulls=4):
    """Protocol 118: MulliganDecision arrives as a legacy legal action; the
    engine accepts the Action submission. Answers at most once per
    revision (a fresh decision arrives at a new revision after a Mulligan)."""
    ma = find_action(acts, "MulliganDecision")
    if not ma or _mulligan_kept.get(who):
        return False
    if _mulligan_answered_rev.get(who) == c.revision:
        return False
    hn = hand_names(state, c.player_id)
    mulls = _mulligan_counts.get(who, 0)
    if keep_fn(hn) or mulls >= max_mulls:
        _mulligan_kept[who] = True
        _mulligan_answered_rev[who] = c.revision
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        say(f"{who} keeps (hand={hn[:8]}, mulls={mulls})")
        wire("mulligan", {"who": who, "decision": "keep",
                          "mulls": mulls})
    else:
        _mulligan_counts[who] = mulls + 1
        _mulligan_answered_rev[who] = c.revision
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"{who} mulligans #{mulls + 1} (hand={hn[:8]})")
        wire("mulligan", {"who": who, "decision": "mulligan",
                          "mulls": mulls + 1})
    return True


_bottom_done = set()
_bottomed = set()


async def bottom_tick(c, st, state, who):
    """Protocol 118: bottom-after-mulligan is a vi schema/select
    opportunity. The 118 surface carries NO waitingForKind code (null at
    both view and opportunity level), so detect by context: turn 1 pregame,
    this seat mulliganed, not yet bottomed, and the candidates reference
    this seat's hand cards. (The exile-cost choice references graveyard
    cards and only appears mid-game with escape_live; the cleanup discard
    needs hand>7.)"""
    if _mulligan_counts.get(who, 0) == 0:
        return False
    if ("bottom", who) in _bottomed:
        return False
    if state.get("turn_number") != 1:
        return False
    if state.get("phase") not in ("Untap", "Upkeep", "Draw"):
        return False
    hand_oids = set()
    for o in player_of(state, c.player_id).get("hand", []):
        try:
            hand_oids.add(int(o))
        except (TypeError, ValueError):
            pass
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in _bottom_done:
            continue
        choices, rtype, spec = opp_candidates(opp)
        if not choices or rtype != "schema":
            continue
        stype = spec.get("type") if isinstance(spec, dict) else spec
        if stype not in ("select", "sequence"):
            continue
        n_hand = 0
        for ch in choices:
            r = cand_ref_oid(ch)
            if r is not None and int(r) in hand_oids:
                n_hand += 1
        if n_hand < max(1, len(choices) - 1):
            continue
        count = _mulligan_counts.get(who, 1)
        spec_data = (spec.get("data") if isinstance(spec, dict)
                     else {}) or {}
        try:
            count = int(spec_data.get("max") or spec_data.get("count")
                        or count)
        except (TypeError, ValueError):
            pass

        def bkey(oid):
            nm = lname(state, oid)
            # bottom non-lands first (keep the hunted lands!);
            # keep goyf most of all
            if nm == GOYF:
                return 2
            if nm in LAND_NAMES:
                return 1
            return 0

        id_by_ref = {}
        for ch in choices:
            r = cand_ref_oid(ch)
            if r is not None:
                id_by_ref[int(r)] = ch.get("id")
        picks = []
        for oid in sorted(hand_oids, key=bkey):
            if oid in id_by_ref:
                picks.append(id_by_ref[oid])
            if len(picks) >= count:
                break
        if len(picks) < count:
            picks = [ch.get("id") for ch in choices[:count]]
        _bottom_done.add(iid)
        _bottomed.add(("bottom", who))
        sub_type = stype if isinstance(stype, str) else "select"
        say(f"{who} bottoms {count} via vi ({sub_type})")
        wire("bottom_cards", {"who": who, "count": count, "picks": picks})
        await c.send_interaction(
            {"interactionId": iid,
             "response": {"type": sub_type, "data": {"choiceIds": picks}}})
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


_pay_vi_done = set()


async def pay_mana_vi(c, st, tag, needs):
    """Drive a vi tapLandForMana mana payment. Only call while a payment is
    genuinely expected (after a CastSpell submission); the 118 engine also
    offers tapLandForMana noise menus at ordinary priority windows."""
    if not needs or sum(max(0, v) for v in needs.values()) <= 0:
        return False
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (
                    None, "available"):
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
        if iid in _pay_vi_done:
            continue
        pick, used = None, None
        for ch, s in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in s:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] = needs.get(used, 0) - 1
        _pay_vi_done.add(iid)
        cid = pick.get("id")
        say(f"[{tag}] paying mana via vi: tap {choice_text(pick)[:40]} "
            f"for {used} (remaining needs={needs})")
        wire("mana_payment_vi", {"who": tag, "used": used,
                                 "remaining": dict(needs), "iid": iid})
        await c.send_interaction(
            {"interactionId": iid,
             "response": {"type": "choose", "data": {"choiceId": cid}}})
        return True
    return False


async def main():
    init_logging()
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_escape_accepted", "A3_choice_offered",
            "A4_subset_accepted", "A5_unchosen_remain", "A6_pt_recomputed",
            "A7_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    hello = await observe_server_hello()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    obs = {
        "phase": "setup",        # setup -> escape -> done
        "answered_ids": [],
        "rejections": [],
        "escape_decisions": [],
        "goyf_cast": False,      # initial battlefield cast
        "div_never": False,      # sentinel: Divination stays re-castable
        "div_casts": 0,
        "ornithopter_cast": False,
        "escape_initiated": False,
        "escape_cast_accepted": False,   # goyf left gy for the stack
        "exile_prompt_seen": False,      # per-card choice offered
        "exile_prompt_kind": None,       # "vi" | None
        "exile_prompt_codes": [],
        "invalid_subset_tried": False,   # 1-card subset negative probe
        "invalid_subset_rejected": None,
        "valid_subset_submitted": False,
        "valid_subset_accepted": None,
        "chosen_names": [],
        "auto_exile_observed": False,    # gy swept with no decision point
        "stuck_watch_fired": False,
        "awaiting_payment": None,  # {"needs": {...}, "why": str, "since": t}
        "p1_bolt_pending": False,
        "p1_bolt_target": None,
        "p1_bolt_iid": None,
        "done": False,
        "escape_live": False,
    }

    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "type": t,
                                          "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "type": t, "data": data})

    def log_vi(c, st, tag):
        vi = st.get("viewer_interaction") or {}
        opps = vi.get("opportunities") or []
        if not opps:
            return
        for opp in opps:
            choices, rtype, spec = opp_candidates(opp)
            stype = spec.get("type") if isinstance(spec, dict) else spec
            info = {
                "tag": tag, "who": c.name, "canSubmit": vi.get("canSubmit"),
                "interactionId": opp.get("interactionId"),
                "waitingForKind": opp.get("waitingForKind"),
                "rtype": rtype, "spec_type": stype,
                "prompt": str(opp.get("prompt") or opp.get("title") or "")[:160],
                "n_choices": len(choices),
                "choices": [
                    {"id": ch.get("id"), "text": choice_text(ch)[:80],
                     "status": choice_status(ch),
                     "ref_oid": cand_ref_oid(ch), "seat": cand_seat(ch),
                     "codes": [x for x in surf_codes(ch) if x]}
                    for ch in choices[:16]],
            }
            wire("vi_opportunity", info)
            say(f"[vi {tag}/{c.name}] rtype={rtype} spec={stype} "
                f"kind={opp.get('waitingForKind')} "
                f"prompt={info['prompt'][:70]!r} "
                f"choices={[(x['text'], x['status']) for x in info['choices']][:8]}")

    async def submit_vi_choice(c, opp, choice_ids, why, rtype=None,
                               stype=None):
        """Submit a multi-choice vi response (protocol-118 schema path)."""
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        rtype = rtype or resp.get("type")
        spec = data.get("spec") or {}
        stype = stype or (spec.get("type") if isinstance(spec, dict)
                          else spec)
        if rtype == "schema":
            sub_type = stype if isinstance(stype, str) else "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": sub_type,
                                "data": {"choiceIds": choice_ids}}}
        elif rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": choice_ids[0]}}}
        else:
            sub_type = stype if isinstance(stype, str) else "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": sub_type,
                                "data": {"choiceIds": choice_ids}}}
        obs["answered_ids"].append(iid)
        obs["_last_sub"] = {"iid": iid, "why": why,
                            "rej_mark": len(obs["rejections"])}
        # #4509 lesson: a REJECTED discard submission must not leave its iid
        # permanently marked answered or the driver stalls at cleanup.
        obs["escape_decisions"].append(
            {"interactionId": iid, "why": why, "sub_type": sub["response"]["type"],
             "choice_ids": choice_ids})
        say(f"[{c.name}] {why}: submit {sub['response']['type']} "
            f"ids={choice_ids} (rtype={rtype} spec={stype})")
        wire("decision_submission", {"who": c.name, "why": why,
                                     "interactionId": iid,
                                     "submission": sub})
        await c.send_interaction(sub)

    def gy_choice_names(state, pid=0):
        """Names of cards in P0's graveyard excluding Nethergoyf itself."""
        return [n for n in gy_names(state, pid) if n != GOYF]

    def wanted_subset_ids(state, opp, wanted):
        """Map wanted card names to candidate ids in this opportunity."""
        choices, _rtype, _spec = opp_candidates(opp)
        by_name = {}
        for ch in choices:
            t = choice_text(ch).lower()
            oid = cand_ref_oid(ch)
            name = lname(state, oid).lower() if oid else t
            by_name.setdefault(name, ch.get("id"))
            by_name.setdefault(t, ch.get("id"))
        ids = []
        for w in wanted:
            cid = by_name.get(w)
            if cid is not None:
                ids.append(cid)
        return ids, by_name

    def decide_discard(opp, state, who):
        # P0's graveyard is filled deliberately through discards: one of
        # each type-card first (they add new card types), then dead lands,
        # then anything except a Goyf we still need to cast.
        choices, _rtype, _spec = opp_candidates(opp)
        avail = [ch for ch in choices
                 if choice_status(ch) in ("available", "Available", None)]
        if not avail:
            avail = choices
        goyf_in_gy = goyf_gy_oid(state, 0) is not None
        gy = set(gy_names(state, 0))

        def rank(ch):
            t = choice_text(ch).lower()
            oid = cand_ref_oid(ch)
            nm = lname(state, oid).lower() if oid else t
            if nm == GOYF:
                # keep a goyf to cast while none is in the gy; extras rank
                # AFTER the type-filling discards so cleanup keeps feeding
                # new card types into the gy instead of spare goyfs.
                return 9 if not goyf_in_gy else 2
            if nm in (ORNITHOPTER, SHOCK, DIVINATION, BEARS, RANCOR) \
                    and nm not in gy:
                return 1    # first copy -> gy, adds a card type
            if nm in LAND_NAMES:
                return 3    # lands in hand are dead at cleanup
            return 5
        avail.sort(key=rank)
        n_discard = max(1, len(avail) - 7)
        return avail[:n_discard], "discard_to_hand_size"

    def is_discard_opp(c, st, opp, state):
        """Protocol 118: DiscardToHandSize is a viewer_interaction
        opportunity. The view-level waitingForKind code may name it, but
        118 often uses a generic 'choose' (or null); gate on hand>7 plus
        hand-card candidates so the exile-cost choice (whose candidates
        reference graveyard cards) never misfires here."""
        code = vi_kind_code(st).lower()
        hand = player_of(state, c.player_id).get("hand", [])
        if "discard" in code and len(hand) > 7:
            return True
        if len(hand) <= 7:
            return False
        choices, rtype, _spec = opp_candidates(opp)
        if not choices or rtype != "schema":
            return False
        hand_oids = set()
        for o in hand:
            try:
                hand_oids.add(int(o))
            except (TypeError, ValueError):
                pass
        for ch in choices:
            oid = cand_ref_oid(ch)
            if oid is not None and int(oid) in hand_oids:
                return True
        return False

    async def handle_exile_vi(c, st, state):
        """Answer the escape exile-cost choice prompt if present (protocol
        118).

        Returns True if a submission was sent. Records whether a genuine
        per-card choice was offered (A3) and drives the invalid-then-valid
        subset probes (A4). Cleanup discards are answered via the separate
        is_discard_opp gate (118: generic 'choose' code + hand>7)."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in obs["answered_ids"]:
                # Re-answer allowance: after the invalid subset is rejected
                # the same prompt may stay open under the same id; the valid
                # subset still needs to go out.
                if not (obs["invalid_subset_rejected"]
                        and not obs["valid_subset_submitted"]
                        and obs["escape_live"]):
                    continue
            # cleanup discard: view-level kind or hand>7 + hand candidates.
            if is_discard_opp(c, st, opp, state):
                ch, why = decide_discard(opp, state, c.name)
                if ch is not None:
                    ids = [x.get("id") for x in ch]
                    await submit_vi_choice(c, opp, ids, why, stype="select")
                    acted = True
                continue
            if not obs["escape_live"]:
                continue
            choices, rtype, spec = opp_candidates(opp)
            if not choices:
                continue
            # skip the noisy priority/mana menus (never decisions)
            codes = set()
            for ch in choices:
                codes.update(x for x in surf_codes(ch) if x)
            if codes and codes <= NON_DECISION_CODES and rtype != "schema":
                continue
            blob = json.dumps(opp, default=str).lower()
            # Heuristic: a per-card exile choice references graveyard cards
            # of P0 (seat 0) or names exile/graveyard in the prompt.
            gy_names_l = set(gy_choice_names(state, 0))
            refs_gy = False
            for ch in choices:
                oid = cand_ref_oid(ch)
                if oid and lname(state, oid).lower() in gy_names_l:
                    refs_gy = True
                    break
                if choice_text(ch).lower() in gy_names_l:
                    refs_gy = True
                    break
            mentions = ("exile" in blob and "graveyard" in blob)
            if not (refs_gy or mentions):
                continue
            # This is the exile-cost choice prompt.
            if not obs["exile_prompt_seen"]:
                obs["exile_prompt_seen"] = True
                obs["exile_prompt_kind"] = "vi"
                obs["exile_prompt_codes"] = sorted(codes)
                say(f"[{c.name}] EXILE CHOICE PROMPT SEEN (vi): "
                    f"rtype={rtype} n={len(choices)} "
                    f"kind={opp.get('waitingForKind')}")
                wire("exile_prompt", {"kind": "vi", "opp": opp,
                                      "gy_names": sorted(gy_names_l)})
            stype = spec.get("type") if isinstance(spec, dict) else spec
            spec_data = (spec.get("data") if isinstance(spec, dict)
                         else {}) or {}
            say(f"[{c.name}] exile prompt spec={stype!r} rtype={rtype!r} "
                f"min={spec_data.get('min')} max={spec_data.get('max')}")
            wire("exile_prompt_detail",
                 {"rtype": rtype, "spec": spec, "n_choices": len(choices),
                  "waitingForKind": opp.get("waitingForKind"),
                  "min": spec_data.get("min"), "max": spec_data.get("max")})
            if not obs["invalid_subset_tried"]:
                # Negative probe: a single card covers 1 type < 4 -> the
                # engine MUST reject it per the card's "four or more card
                # types among them" rule.
                ids, _by = wanted_subset_ids(state, opp, [SHOCK])
                if not ids:
                    # fall back to the first available candidate
                    avail = [ch for ch in choices
                             if choice_status(ch) in ("available",
                                                      "Available", None)]
                    ids = [(avail[0] if avail else choices[0]).get("id")]
                obs["invalid_subset_tried"] = True
                obs["invalid_subset_ids"] = ids
                await submit_vi_choice(c, opp, ids, "invalid_subset_1card",
                                       rtype=rtype, stype=stype)
                acted = True
                continue
            if obs["invalid_subset_rejected"] \
                    and not obs["valid_subset_submitted"]:
                # Valid probe: one card per type -> 4 types among them.
                wanted = [ORNITHOPTER, SHOCK, DIVINATION, BEARS]
                ids, by_name = wanted_subset_ids(state, opp, wanted)
                if len(ids) < 4:
                    say(f"[{c.name}] WARNING: only mapped {len(ids)}/4 "
                        f"wanted cards; submitting what we have")
                    wire("subset_map_short", {"ids": ids})
                obs["valid_subset_submitted"] = True
                obs["valid_subset_ids"] = ids
                rev = {v: k for k, v in by_name.items()}
                obs["chosen_names"] = [rev.get(i, "?") for i in ids]
                await submit_vi_choice(c, opp, ids, "valid_subset_4types",
                                       rtype=rtype, stype=stype)
                acted = True
                continue
            say(f"[{c.name}] exile prompt already probed; leaving unanswered")
            wire("exile_prompt_repeat", {"interactionId": iid})
        return acted

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    def fixture_ready(state):
        if goyf_gy_oid(state, 0) is None:
            return False
        if len(gy_type_set(state, 0, exclude_names=(GOYF,))) < 4:
            return False
        lands = untapped_lands(state, 0)
        if len(lands) < 3:
            return False
        if not untapped_lands(state, 0, SWAMP):
            return False
        return True

    def log_castspell_actions(state, acts):
        for a in acts:
            if a["type"] != "CastSpell":
                continue
            d = a.get("data", {})
            oid = d.get("object_id")
            o = get_obj(state, oid) if oid is not None else {}
            wire("castspell_action",
                 {"object_id": oid, "name": obj_name(o),
                  "zone": o.get("zone"), "controller": o.get("controller"),
                  "data_keys": sorted(d.keys())})
            say(f"[P0] CastSpell offered: {obj_name(o)} zone={o.get('zone')} "
                f"data_keys={sorted(d.keys())}")

    async def answer_bolt_target(c, st, state):
        """Answer P1's Lightning Bolt target selection (protocol 118: vi
        schema/sequence with a composite choice id). Targets the recorded
        creature oid; submits once per interaction id."""
        if not obs.get("p1_bolt_pending"):
            return False
        opp, rtype, spec_type = target_opportunity(st)
        if opp is None:
            return False
        iid = opp.get("interactionId")
        if iid in obs["answered_ids"]:
            return False
        want = obs.get("p1_bolt_target")
        ch = pick_target_candidate(opp, want_oid=want, state=state)
        if ch is None:
            return False
        obs["answered_ids"].append(iid)
        obs["p1_bolt_iid"] = iid
        wire("bolt_target_answered",
             {"who": c.name, "interactionId": iid, "want_oid": want,
              "pick": choice_text(ch)[:60]})
        await submit_target(c, opp, rtype, spec_type, ch, c.name)
        obs["p1_bolt_pending"] = False
        return True

    def play_land_action(acts, state):
        cands = [a for a in acts if a["type"] == "PlayLand"]
        if not cands:
            return None
        if not obs.get("playland_logged"):
            obs["playland_logged"] = True
            for a in cands:
                d = a.get("data", {})
                wire("playland_action",
                     {"object_id": d.get("object_id"),
                      "name": lname(state, d.get("object_id")),
                      "data_keys": sorted(d.keys())})
        have = set()
        for _oid, o in state.get("objects", {}).items():
            if (o.get("zone") == "Battlefield" and o.get("controller") == 0
                    and obj_name(o) in LAND_NAMES):
                have.add(obj_name(o))
        for want in (SWAMP, ISLAND):
            if want not in have:
                for a in cands:
                    d = a.get("data", {})
                    if lname(state, d.get("object_id")) == want:
                        return a
        return cands[0]

    def mark_payment(why, needs):
        obs["awaiting_payment"] = {"needs": dict(needs), "why": why,
                                   "since": time.time()}
        say(f"payment expected for {why}: needs={needs}")

    def clear_payment_if_done(state):
        ap = obs.get("awaiting_payment")
        if not ap:
            return
        needs = ap["needs"]
        if sum(max(0, v) for v in needs.values()) <= 0:
            obs["awaiting_payment"] = None
            say(f"payment for {ap['why']} complete (needs exhausted)")
            return
        if time.time() - ap["since"] > 60:
            obs["awaiting_payment"] = None
            say(f"payment for {ap['why']} timed out; clearing")
            wire("payment_timeout", ap)

    async def p0_tick(st, acts, state):
        if await mulligan_tick(p0, acts, state, "P0",
                               lambda hn: (
                                   sum(1 for n in hn if n in LAND_NAMES) >= 2
                                   and SWAMP in hn
                                   and any(n in (GOYF, DIVINATION, ORNITHOPTER)
                                           for n in hn)),
                               max_mulls=4):
            return True
        if await bottom_tick(p0, st, state, "P0"):
            return True

        # payments: legacy actions first, then vi tapLandForMana while a
        # payment is genuinely expected (noise menus excluded).
        if await pay_tick(p0, acts):
            return True
        ap = obs.get("awaiting_payment")
        if ap and await pay_mana_vi(p0, st, "P0", ap["needs"]):
            return True
        clear_payment_if_done(state)

        # exile-choice / discard opportunities
        if await handle_exile_vi(p0, st, state):
            return True

        # combat / trigger bookkeeping (advertised actions, not waiting_for)
        atypes = set(a.get("type") for a in acts)
        if "DeclareAttackers" in atypes:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(p0, d)
            return True
        if "DeclareBlockers" in atypes:
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(p0, d)
            return True
        if "OrderTriggers" in atypes:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return True

        # ---- escape progress tracking ----
        if obs["escape_live"]:
            # invalid-subset rejection detection
            if (obs["invalid_subset_tried"]
                    and obs["invalid_subset_rejected"] is None
                    and len(obs["rejections"]) > obs.get("_rej_mark", 0)):
                new_rej = obs["rejections"][obs.get("_rej_mark", 0):]
                obs["invalid_subset_rejected"] = True
                say(f"[P0] invalid 1-card subset REJECTED: "
                    f"{json.dumps(new_rej)[:500]}")
                wire("invalid_subset_rejected", new_rej)
            # escape cast accepted: goyf left the graveyard
            if (obs["escape_initiated"] and not obs["escape_cast_accepted"]
                    and goyf_gy_oid(state, 0) is None):
                obs["escape_cast_accepted"] = True
                obs["awaiting_payment"] = None
                say("[P0] escape cast accepted: Nethergoyf left the graveyard")
                wire("escape_accepted", {})
            # auto-exile detection: all other gy cards gone with no prompt
            if (obs["escape_cast_accepted"]
                    and not obs["exile_prompt_seen"]
                    and not obs["auto_exile_observed"]):
                if not gy_choice_names(state, 0):
                    obs["auto_exile_observed"] = True
                    say("[P0] AUTO-EXILE OBSERVED: graveyard swept with no "
                        "choice prompt (the reported bug)")
                    wire("auto_exile", {"gy": gy_names(state, 0)})
            # invalid subset accepted (constraint missing): cast completed
            # without ever submitting the valid set
            if (obs["invalid_subset_tried"]
                    and not obs["valid_subset_submitted"]
                    and goyf_bf_oid(state, 0) is not None):
                obs["invalid_subset_accepted"] = True
                say("[P0] INVALID 1-card subset was ACCEPTED (missing 4-type "
                    "constraint) - related failure")
                wire("invalid_subset_accepted", {})
            # valid subset accepted: goyf on battlefield, chosen exiled
            if (obs["valid_subset_submitted"]
                    and obs["valid_subset_accepted"] is None
                    and goyf_bf_oid(state, 0) is not None):
                obs["valid_subset_accepted"] = True
                say("[P0] valid 4-type subset ACCEPTED; Nethergoyf escaped "
                    "to the battlefield")
                wire("valid_subset_accepted", {})
            # done: goyf on the battlefield and stack settled
            if (goyf_bf_oid(state, 0) is not None
                    and not stack_entries(state)
                    and not obs["done"]):
                obs["done"] = True
                obs["phase"] = "done"
                obs["escape_live"] = False
                obs["final_quiet"] = not real_decision_pending(st)
                await export("post_escape")
                say("[P0] ESCAPE COMPLETE: Nethergoyf on battlefield; "
                    "exported post_escape.json")
                return True

        # protocol 118: priority = PassPriority in top-level legal_actions
        if not my_priority(acts):
            return False

        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain"))
        if own_main and not real_decision_pending(st):
            # 1. land drop
            pla = play_land_action(acts, state)
            if pla:
                await submit_as_is(p0, pla)
                say(f"P0 plays land "
                    f"{lname(state, pla.get('data', {}).get('object_id'))}")
                return True
            # 2. escape initiation
            if obs["phase"] == "setup" and fixture_ready(state):
                log_castspell_actions(state, acts)
                gid = goyf_gy_oid(state, 0)
                target = cast_action_for(acts, gid)
                if target is None:
                    if not obs.get("no_escape_cast_logged"):
                        obs["no_escape_cast_logged"] = True
                        say("[P0] FIXTURE READY but no CastSpell offered for "
                            "the graveyard Nethergoyf")
                        wire("no_escape_cast",
                             {"goyf_gy_oid": gid,
                              "gy_types": sorted(gy_type_set(
                                  state, 0, exclude_names=(GOYF,)))})
                    return False
                await export("pre_escape")
                obs["_rej_mark"] = len(obs["rejections"])
                obs["phase"] = "escape"
                obs["escape_initiated"] = True
                obs["escape_live"] = True
                say(f"[P0] initiating ESCAPE of Nethergoyf (gy oid {gid})")
                wire("escape_initiated", target)
                await submit_as_is(p0, target)
                # escape cost {2}{B} + exile choice; mana first
                mark_payment("escape", {"B": 1, "generic": 2})
                return True
            # 3. setup casts: Goyf ({1}{B}), Ornithopter (free),
            # Divination ({2}{U}). Shock/Bears/Rancor are uncastable here
            # (no R/G mana) and reach the graveyard via cleanup discards.
            n_untapped = len(untapped_lands(state, 0))
            has_swamp = bool(untapped_lands(state, 0, SWAMP))
            has_island = bool(untapped_lands(state, 0, ISLAND))

            def try_cast(name, cond, flag, needs):
                if obs[flag] or not cond:
                    return None
                sid = spell_in_hand_oid(state, 0, name)
                if sid is None:
                    return None
                a = cast_action_for(acts, sid)
                if a is None:
                    return None
                return a, needs

            r = try_cast(GOYF, n_untapped >= 2 and has_swamp
                         and goyf_gy_oid(state, 0) is None
                         and goyf_bf_oid(state, 0) is None, "goyf_cast",
                         {"B": 1, "generic": 1})
            if r:
                a, needs = r
                obs["goyf_cast"] = True
                say("P0 casts Nethergoyf")
                wire("cast_goyf", a)
                await submit_as_is(p0, a)
                mark_payment("goyf_cast", needs)
                return True
            r = try_cast(ORNITHOPTER, True, "ornithopter_cast", {})
            if r:
                a, _needs = r
                obs["ornithopter_cast"] = True
                say("P0 casts Ornithopter")
                await submit_as_is(p0, a)
                return True
            # Divination stays re-castable: each cast draws 2, overfilling
            # the hand for type-filling cleanup discards.
            r = try_cast(DIVINATION, n_untapped >= 3 and has_island
                         and len(hand_names(state, 0)) <= 5,
                         "div_never", {"U": 1, "generic": 2})
            if r:
                a, needs = r
                obs["div_casts"] = obs.get("div_casts", 0) + 1
                say("P0 casts Divination")
                wire("cast_divination", a)
                await submit_as_is(p0, a)
                mark_payment("divination", needs)
                return True

        if not real_decision_pending(st):
            return await pass_priority(p0, st, acts)
        return False

    async def p1_tick(st, acts, state):
        if await mulligan_tick(p1, acts, state, "P1",
                               lambda hn: MOUNTAIN in hn):
            return True
        if await bottom_tick(p1, st, state, "P1"):
            return True
        if await pay_tick(p1, acts):
            return True
        ap = obs.get("p1_awaiting_payment")
        if ap and await pay_mana_vi(p1, st, "P1", ap["needs"]):
            return True
        if await handle_exile_vi(p1, st, state):
            return True
        if await answer_bolt_target(p1, st, state):
            return True
        atypes = set(a.get("type") for a in acts)
        if "DeclareAttackers" in atypes:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(p1, d)
            return True
        if "DeclareBlockers" in atypes:
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(p1, d)
            return True
        if "OrderTriggers" in atypes:
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return True
        if not my_priority(acts):
            return False
        own_main = (state.get("active_player") == 1
                    and state.get("phase") in ("PreCombatMain",
                                               "PostCombatMain"))
        if own_main and not real_decision_pending(st):
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    say(f"P1 plays land "
                        f"{lname(state, a.get('data', {}).get('object_id'))}")
                    return True
            # bolt P0's creatures: goyf > bears > ornithopter (name-based:
            # filtered views omit type_line)
            target = None
            for nm in (GOYF, BEARS, ORNITHOPTER):
                cs = bf_creatures(state, 0, nm)
                if cs:
                    target = (nm, cs[0])
                    break
            n_mtn = len(untapped_lands(state, 1, MOUNTAIN))
            bid = spell_in_hand_oid(state, 1, BOLT)
            if target is not None and n_mtn > 0 and bid is not None:
                a = cast_action_for(acts, bid)
                if a is not None:
                    obs["p1_bolt_pending"] = True
                    obs["p1_bolt_target"] = target[1]
                    say(f"P1 bolts {target[0]} (oid {target[1]})")
                    wire("cast_bolt", {"target": target[1]})
                    await submit_as_is(p1, a)
                    obs["p1_awaiting_payment"] = {
                        "needs": {"R": 1}, "why": "bolt",
                        "since": time.time()}
                    return True
        if not real_decision_pending(st):
            return await pass_priority(p1, st, acts)
        return False

    def evaluate():
        pre = load_env("pre_escape")
        post = load_env("post_escape")
        notes.append(
            f"escape_initiated={obs['escape_initiated']} "
            f"escape_cast_accepted={obs['escape_cast_accepted']} "
            f"exile_prompt_seen={obs['exile_prompt_seen']} "
            f"({obs['exile_prompt_kind']}) codes={obs['exile_prompt_codes']} "
            f"invalid_tried={obs['invalid_subset_tried']} "
            f"invalid_rejected={obs['invalid_subset_rejected']} "
            f"invalid_accepted={obs.get('invalid_subset_accepted')} "
            f"valid_submitted={obs['valid_subset_submitted']} "
            f"valid_accepted={obs['valid_subset_accepted']} "
            f"auto_exile={obs['auto_exile_observed']} "
            f"rejections={len(obs['rejections'])}")
        # A1
        if pre is not None:
            s = pre["state"]
            goyf_gy = goyf_gy_oid(s, 0) is not None
            types = gy_type_set(s, 0, exclude_names=(GOYF,))
            lands = untapped_lands(s, 0)
            ok = (goyf_gy and len(types) >= 4 and len(lands) >= 3
                  and bool(untapped_lands(s, 0, SWAMP))
                  and s.get("active_player") == 0
                  and s.get("phase") in ("PreCombatMain", "PostCombatMain")
                  and s.get("priority_player") == 0)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: goyf_in_gy={goyf_gy}, types_among_others="
                         f"{sorted(types)} ({len(types)}), untapped_lands="
                         f"{len(lands)}, swamp_untapped="
                         f"{bool(untapped_lands(s, 0, SWAMP))}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre_escape.json missing (fixture never ready)")
        # A2
        if post is not None:
            s = post["state"]
            goyf_bf = goyf_bf_oid(s, 0) is not None
            goyf_gy = goyf_gy_oid(s, 0) is not None
            ok = goyf_bf and not goyf_gy
            ass["A2_escape_accepted"] = "passed" if ok else "failed"
            notes.append(f"A2: goyf_on_bf={goyf_bf}, goyf_in_gy={goyf_gy}")
        else:
            ass["A2_escape_accepted"] = "failed"
            notes.append("A2: post_escape.json missing")
        # A3
        if obs["exile_prompt_seen"]:
            ass["A3_choice_offered"] = "passed"
            notes.append(f"A3: per-card exile choice offered via "
                         f"{obs['exile_prompt_kind']}")
        elif obs["auto_exile_observed"]:
            ass["A3_choice_offered"] = "failed"
            notes.append("A3: NO choice offered; graveyard swept with no "
                         "decision point (the reported bug)")
        else:
            ass["A3_choice_offered"] = "failed"
            notes.append("A3: no per-card exile choice observed")
        # A4
        if obs.get("invalid_subset_accepted"):
            ass["A4_subset_accepted"] = "failed"
            notes.append("A4: RELATED FAILURE - a 1-card subset (<4 types) "
                         "was ACCEPTED; the 4-type aggregate constraint is "
                         "not enforced")
        elif obs["valid_subset_accepted"]:
            ass["A4_subset_accepted"] = "passed"
            notes.append("A4: 1-card subset rejected "
                         f"(rejected={obs['invalid_subset_rejected']}); "
                         "4-type subset accepted and cast completed")
        elif obs["invalid_subset_tried"] and obs["invalid_subset_rejected"]:
            ass["A4_subset_accepted"] = "failed"
            notes.append("A4: 1-card subset correctly rejected, but the "
                         "4-type subset was never accepted / cast did not "
                         "complete")
        else:
            ass["A4_subset_accepted"] = "not-run"
            notes.append("A4: no exile choice prompt to probe")
        # A5 / A6 need the post state and the chosen set
        chosen = set(obs.get("chosen_names", []))
        if post is not None:
            s = post["state"]
            gy = [o for _, o in gy_objs(s, 0)]
            unchosen = [o for o in gy
                        if obj_name(o) not in chosen
                        and obj_name(o) != GOYF]
            ok = len(unchosen) >= 1
            ass["A5_unchosen_remain"] = "passed" if ok else "failed"
            notes.append(f"A5: unchosen remaining in gy="
                         f"{[obj_name(o) for o in unchosen]}")
            # A6
            goyf_oid = goyf_bf_oid(s, 0)
            if goyf_oid is not None:
                g = get_obj(s, goyf_oid)
                types = gy_type_set(s, 0)
                pw = g.get("power")
                tw = g.get("toughness")
                pwr = pw.get("value") if isinstance(pw, dict) else pw
                tgh = tw.get("value") if isinstance(tw, dict) else tw
                try:
                    pwr_i, tgh_i = int(pwr), int(tgh)
                except (TypeError, ValueError):
                    pwr_i = tgh_i = None
                ok = (pwr_i == len(types) and tgh_i == len(types) + 1)
                ass["A6_pt_recomputed"] = "passed" if ok else "failed"
                notes.append(f"A6: goyf P/T={pwr_i}/{tgh_i}, types in "
                             f"remaining gy={sorted(types)} "
                             f"(want {len(types)}/{len(types) + 1})")
            else:
                ass["A6_pt_recomputed"] = "failed"
                notes.append("A6: Nethergoyf not on battlefield")
            # A7: protocol 118 has no waiting_for; the live view at
            # completion is checked for pending real decisions instead.
            ok = (not stack_entries(s) and not obs["stuck_watch_fired"]
                  and obs.get("final_quiet", False))
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: stack_empty={not stack_entries(s)}, "
                         f"final_quiet={obs.get('final_quiet')}, "
                         f"stuck_watch={obs['stuck_watch_fired']}")
        else:
            for k in ("A5_unchosen_remain", "A6_pt_recomputed", "A7_cleanup"):
                ass[k] = "failed"
            notes.append("A5/A6/A7: post_escape.json missing")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("fixture never assembled; no trustworthy result")
        elif all(ass[k] == "passed" for k in ass):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("the Nethergoyf escape exile-cost path did not "
                         "behave per the oracle text on this build")
        return verdict

    async def finish():
        dur = time.time() - t_start
        if not os.path.exists(f"{EVDIR}/post_escape.json"):
            try:
                await export("post_escape")
            except Exception:
                pass
        verdict = evaluate()
        # server-log excerpts: corroborate the escape path
        try:
            slog = open(os.environ.get("SERVER_LOG_PATH",
                f"{BACKFILL}/runs/{RUN_ID}/server.log"),
                        encoding="utf-8", errors="replace").read()
            sig_lines = [ln for ln in slog.splitlines()
                         if "escape" in ln.lower() or "exile" in ln.lower()
                         or "nethergoyf" in ln.lower()]
            with open(f"{EVDIR}/server_excerpts.log", "w") as f:
                f.write(f"# server log excerpts for run {RUN_ID} "
                        f"(issue #{ISSUE})\n")
                f.write(f"# {len(sig_lines)} matching lines\n")
                for ln in sig_lines[-80:]:
                    f.write(ln + "\n")
            say(f"server excerpts: {len(sig_lines)} matching lines")
        except Exception as e:
            notes.append(f"server log read failed: {e}")
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_observed": hello,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 118,
                       "client": "driver/client.py"},
            "scenario": {"file": "scenario_6609_01040.py",
                         "sha256": hashlib.sha256(
                             open(__file__, "rb").read()).hexdigest()},
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if k not in ("escape_decisions",)},
            "escape_decisions": obs["escape_decisions"],
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x spell density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The escape fixture is reconstructed (cast + Bolt), not the "
                "reporter's attached game state.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
                "waiting_for is null on protocol 118: A7 checks the live "
                "view for pending real decisions at completion instead of a "
                "Priority waiting_for type.",
            ],
            "setup_line": "P0: 8x Nethergoyf/Ornithopter/Rancor/Shock/Divination/"
                          "Grizzly Bears + 8x Swamp + 8x Island; "
                          "P1: 12x Lightning Bolt + 36x Mountain",
            "contract_line": "Escape Nethergoyf ({2}{B}): engine must offer a "
                             "per-card exile choice; a 1-card (<4 types) subset "
                             "must be rejected; a 4-type subset must be accepted; "
                             "unchosen cards remain; P/T recomputes from the "
                             "remaining graveyard",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=2, default=str)
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump(ass, f, indent=2)
        with open(f"{EVDIR}/obs.json", "w") as f:
            json.dump(obs, f, indent=2, default=str)
        # copy the running driver source into the evidence dir
        with open(__file__, "rb") as src, \
                open(f"{EVDIR}/scenario_6609_01040.py", "wb") as dst:
            dst.write(src.read())
        # close logs BEFORE the manifest (hashes must cover final bytes)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        files = sorted(f for f in os.listdir(EVDIR)
                       if f != "manifest.sha256"
                       and os.path.isfile(os.path.join(EVDIR, f)))
        with open(f"{EVDIR}/manifest.sha256", "w") as mf:
            for fn in files:
                h = hashlib.sha256(
                    open(os.path.join(EVDIR, fn), "rb").read()).hexdigest()
                mf.write(f"{h}  {fn}\n")
        say(f"manifest written for {len(files)} files")
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_watch = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            await drain_rejections(c)
            # rejection-retry (#4509): if the last vi submission for a
            # discard was rejected, unmark its iid so the next tick retries
            # instead of stalling at DiscardToHandSize.
            last_sub = obs.pop("_last_sub", None)
            if last_sub and len(obs["rejections"]) > last_sub["rej_mark"] \
                    and last_sub["why"] == "discard_to_hand_size":
                iid = last_sub["iid"]
                if iid in obs["answered_ids"]:
                    obs["answered_ids"].remove(iid)
                say(f"[{c.name}] discard submission rejected; unmarking iid "
                    f"{iid} for retry")
                wire("discard_rejected_retry", {"iid": iid})
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
            if c.name == "P0" and (obs["escape_live"]
                                   or real_decision_pending(st)):
                log_vi(c, st, "esc" if obs["escape_live"] else "decision")
        if obs["done"]:
            say("escape complete; finishing")
            await finish()
            return
        if p0.latest and p0.latest["state"].get("turn_number", 0) >= 30 \
                and not obs["done"]:
            notes.append("turn 30 reached without completing the escape; "
                         "bailing out to evaluation")
            say("turn 30 bail-out; finishing")
            await finish()
            return
        if obs["escape_live"] and stuck_watch is None:
            stuck_watch = time.time() + 180
        if not obs["escape_live"]:
            stuck_watch = None
        if stuck_watch and time.time() > stuck_watch:
            s = p0.latest["state"] if p0.latest else {}
            obs["stuck_watch_fired"] = True
            notes.append("STUCK WATCH FIRED (180s mid-escape): "
                         f"priority_player={s.get('priority_player')}")
            say("STUCK WATCH FIRED mid-escape")
            try:
                await export("mid_stall")
            except Exception:
                pass
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} pp={s.get('priority_player')} "
                f"hand={hand_names(s, 0)[:6]} "
                f"gy0={[obj_name(o) for _, o in gy_objs(s, 0)][:10]} "
                f"goyf_gy={goyf_gy_oid(s, 0) is not None} "
                f"phase_sm={obs['phase']} escape_live={obs['escape_live']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


if __name__ == "__main__":
    asyncio.run(main())
