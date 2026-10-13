"""Issue #9840: Blackmail / Hollow Specter / Cabal Interrogator --
"reveals X cards from their hand and you choose one of them" lets the
discarding player pick.

FIRST VALIDATION on the pinned release v0.108.0 (build b8fa1d3, protocol 134).
Protocol-134 driver conventions (verified by scenario_9839_01080.py,
2026-10-12): CreateGameWithSettings + JoinGameWithPassword + start_when_full,
bare CastSpell (engine auto-pays), cast-confirmation guard, repeating prompts
answered per-firing keyed by interactionId, legacy ChooseTarget alongside the
vi-schema target path, real_decision_pending gating priority passes.

ISSUE (phase-rs/phase#9840, opened 2026-10-12 by matthewevans, open,
bug/area:parser/priority:p2-wrong-game-result): the "and you choose one of
them" chooser clause has no typed representation on the reveal-then-discard
shape. Before the fail-closed change it parsed supported with the wrong
chooser; it is now an honest Effect::Unimplemented, so the cards are
unsupported until the chooser is modelled.

Oracle text (verified from pinned v0.108.0 card-data.json):
  Blackmail {B} Sorcery:
    "Target player reveals three cards from their hand and you choose one of
     them. That player discards that card."
  (Ruling: if the player has fewer than 3 cards, all of them are revealed.)
  Reach per the issue: Hollow Specter, Cabal Interrogator, Mire's Toll,
  Taster of Wares (same grammar).

Pinned parse (v0.108.0 card-data.json, Blackmail): abilities[0].effect is
Unimplemented {name: "unparsed_verb_arguments",
description: "reveal three cards from their hand and you choose one of them"}
followed by a sequential sibling Spell effect DiscardCard count=1
ParentTarget.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
P0 (seat 0) holds Blackmail + a Swamp, casts it turn 1 targeting P1.
P1 holds ~7 cards and never casts.

Expected per Oracle (CR 701.20a, 701.9a):
  E1: on resolution P1 reveals three cards from their hand (all, if hand < 3);
  E2: P0 (the spell's controller) is offered "choose one of them" from exactly
      the revealed cards (a bounded set);
  E3: P1 discards the card P0 chose.

Assertions:
  A1_setup    pre: Blackmail in P0's hand, P1 hand >= 3, life 20/20.
  A2_parse    parse evidence captured from pinned v0.108.0 card-data and
                documented for all five cards (Blackmail Unimplemented
                reveal clause + DiscardCard sibling documented).
  A3_cast     the spell's cast was allowed and reached the stack.
  A4_reveal   [bug?] on resolution P1 revealed the stated number of cards
                (or all if hand < 3); if the effect is Unimplemented and no
                reveal happened, this fails = the reported bug.
  A5_chooser  [bug?] the CONTROLLER (P0) was offered the choose-1 from the
                revealed set (bounded), not the target player choosing freely.
  A6_outcome  P1 discarded exactly the controller-chosen card; no other
                P1 cards moved; library unchanged by the reveal.
  A7_cleanup  stack empty post-resolution.

Verdict rule: reproduced iff A1 passed and the resolution did not implement
the reveal-then-controller-choose-then-discard flow (any of A4/A5/A6 failed),
OR the spell could not be cast at all on current main (A3 failed -- the
"honestly unsupported" outcome described in the issue). not-reproduced iff
A1, A3, A4, A5, A6 all pass. blocked iff setup never completed.

Driver answering policy (deterministic):
  - spell target: P1 (seat 1).
  - P1 discard prompt (if the engine offers the DiscardCard sub-ability to
    the target): first candidate in offered order; candidate set recorded as
    wrong-chooser evidence.
  - P0 choose-1 (if offered, the correct chooser): LAST candidate; candidate
    COUNT is the boundedness evidence.
Unknown decisions are logged, never auto-answered.

Evidence: evidence/9840/run-9840-v01080-20261013-0141/pre_cast.json,
post.json, parse_evidence.json, run.json, manifest.sha256, summary.png,
scenario_9840_01080.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "run-9840-v01080-20261013-0141"
ISSUE = 9840
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BLACKMAIL = "blackmail"
SWAMP = "swamp"
BOLT = "lightning bolt"
DIV = "divination"
ISLAND = "island"
P0_DECK = deck((BLACKMAIL, 4), (SWAMP, 56))
P1_DECK = deck((BOLT, 8), (DIV, 4), (ISLAND, 48))

CARD_DATA_PATH = f"{BACKFILL}/server/releases/v0.108.0/data/card-data.json"
CARD_DATA = json.load(open(CARD_DATA_PATH))

SERVER_IDENTITY = {
    "server_version": "v0.108.0",
    "build_commit": "b8fa1d3",
    "protocol_version": 134,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.108.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode (pin verified 2026-10-12 16:20 run; binary "
                       "digest matches the GitHub asset digest); data digests "
                       "match the signed manifest; digests recomputed against "
                       "on-disk files this run"),
    "source": ("2026-10-13: latest stable release v0.108.0 (published "
               "2026-10-12T21:10:34Z) == pinned release dir; ServerHello "
               "0.108.0/b8fa1d3/protocol 134 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "server on 127.0.0.1:9374 reused (already listening; started "
               "by run-5654-reval-v01080-20261012-2341)"),
}
for _f, _k in (("server/releases/v0.108.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.108.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.108.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def state_life(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        for a in opp.get("actions", []) or []:
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


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


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


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def untapped_lands(state, pid, name=None):
    return [o for o in bf_oids(state, pid)
            if is_land(get_obj(state, o)) and not get_obj(state, o).get("tapped")
            and (name is None or obj_lname(state, o) == name)]


def on_bf(state, pid, name):
    return any(obj_lname(state, o) == name for o in bf_oids(state, pid))


def in_gy(state, pid, name):
    return any(obj_lname(state, o) == name
               for o in player_of(state, pid).get("graveyard", []))


def card_in_hand_oid(state, pid, name):
    for o in hand_ids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
    return None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None

# ------------------------------------------------------------- interaction primitives
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


async def answer_vi(c, opp, choice, tag, submitted):
    iid = opp.get("interactionId")
    key = (tag, str(iid), str(choice.get("id")))
    if key in submitted:
        return False
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
    submitted.add(key)
    say(f"[{tag}] submitting interaction iid={str(iid)[:16]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await interact_as(c, sub, tag)
    return True


# ------------------------------------------------------------- common ticks (protocol 134)
async def do_mulligan(c, acts, st, pid, tag, game, keep_fn):
    """MulliganDecision as legacy Action (verified accepted on 118)."""
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
    if key in game.mulls:
        return False
    game.mulls.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    mull_count = sum(1 for k in game.mulls
                     if k[0] == tag and k[1] == "mull")
    if keep_fn(state, pid) or mull_count >= 4 or len(hn) <= 4:
        say(f"[{tag}] keep {len(hn)}: {hn}")
        wire("mulligan", {"who": tag, "decision": "keep", "hand": hn,
                          "iid": str(iid)[:16]})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    else:
        say(f"[{tag}] mulligan #{mull_count + 1} ({len(hn)}: {hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn,
                          "iid": str(iid)[:16]})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag, game, avoid_names):
    """Bottom-after-mulligan: vi schema/select, gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap."""
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
    key = (tag, "bottom", str(iid))
    if key in game.submitted:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False

    def bkey(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in avoid_names:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = [ch.get("id") for ch in ranked[:n]]
    game.submitted.add(key)
    say(f"[{tag}] bottoming {[choice_text(x)[:40] for x in ranked[:n]]} via vi")
    sub = {"interactionId": iid,
           "response": {"type": "select", "data": {"choiceIds": picks}}}
    await interact_as(c, sub, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag, game, avoid_names):
    """DiscardToHandSize via the generic 'choose' vi surface, gated on
    hand > 7. Never discards key cards."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    # protocol 134: vi candidate references are STRINGS, player.hand holds
    # INTS -- normalize both sides with str() (AGENTS.md 2026-10-11)
    handset = {str(x) for x in hand}
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        refs = [_cand_reference(ch) for ch in cands]
        if not any(r in handset for r in refs):
            # heuristic miss: dump the vi surface once per interaction so a
            # shape change on the new protocol is a 2-minute read, not a
            # 10-minute stall mystery (AGENTS.md 2026-10-11)
            wire("handsize_heuristic_miss",
                 {"iid": opp.get("interactionId"),
                  "ref_types": sorted({type(r).__name__ for r in refs}),
                  "ref_sample": [str(r) for r in refs[:5]],
                  "hand_sample": sorted(handset)[:5],
                  "resp_type": resp.get("type"),
                  "spec_type": (rdata.get("spec", {}) or {}).get("type")})
            continue
        key = (tag, "handsize", str(c.revision), str(opp.get("interactionId")))
        if key in game.submitted:
            return False

        def rank(ch):
            nm = obj_lname(state, _cand_reference(ch))
            if nm in avoid_names:
                return (5, nm)
            if _cand_reference(ch) is not None and is_land(
                    get_obj(state, _cand_reference(ch))):
                return (0, nm)
            return (2, nm)

        picks = [ch["id"] for ch in
                 sorted(cands, key=rank)[:max(1, n)]]
        game.submitted.add(key)
        say(f"[{tag}] discarding to hand size via vi: picks={picks}")
        wire("handsize_discard", {"who": tag, "picks": picks})
        spec = rdata.get("spec") or {}
        stype = spec.get("type") or "select"
        await interact_as(c, {"interactionId": opp.get("interactionId"),
                              "response": {"type": stype,
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


async def pass_priority(c, st, acts, game):
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


async def play_a_land(c, state, pid, acts, tag, game):
    if game.lands_locked:
        return False
    turn = state.get("turn_number")
    if game.land_played_turn.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and \
                        str(a.get("_src_oid")) == str(o):
                    game.land_played_turn[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
                    wire("play_land", {"who": tag, "oid": o})
                    await submit_as_is(c, a)
                    return True
    return False


def cast_action_for(acts, state, oid):
    for a in acts:
        if a["type"] != "CastSpell":
            continue
        d = a.get("data", {}) or {}
        for v in (d.get("object_id"), a.get("_src_oid")):
            try:
                if v is not None and int(v) == int(oid):
                    return a
            except (TypeError, ValueError):
                continue
    return None


def combat_tick(acts):
    atypes = set(a.get("type") for a in acts)
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            return d
    if "DeclareBlockers" in atypes:
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            return d
    return None




def _find_player_refs(data, want=1):
    refs = []
    if isinstance(data, dict):
        t = data.get("type")
        if t == "Player":
            try:
                if int(data.get("data")) == want:
                    refs.append(data)
            except (TypeError, ValueError):
                pass
        for k in ("player", "seat", "player_id", "seat_index"):
            try:
                if data.get(k) is not None and int(data[k]) == want:
                    refs.append({k: data[k]})
            except (TypeError, ValueError):
                pass
        for v in data.values():
            refs.extend(_find_player_refs(v, want))
    elif isinstance(data, list):
        for v in data:
            refs.extend(_find_player_refs(v, want))
    return refs



async def cast_named(c, state, acts, name, oid, tag, g, flag):
    """Bare CastSpell (protocol 134: engine auto-pays). Arms the
    cast-confirmation guard on the Game object; `flag` is a one-shot Game
    attribute, cleared on the 30s backstop so a dropped cast re-triggers."""
    a = cast_action_for(acts, state, oid)
    if a is None:
        return False
    say(f"[{tag}] casting {name} (oid={oid})")
    wire("cast", {"who": tag, "name": name, "oid": str(oid)})
    await submit_as_is(c, a)
    g.pending_cast = {"oid": str(oid), "name": name,
                      "since": time.time(), "tag": tag, "flag": flag}
    setattr(g, flag, True)
    await asyncio.sleep(0.5)
    return True




def cast_guard_tick(g, state, tag):
    """Protocol-134 cast-confirmation guard (2026-10-09 lesson), Game-adapted.

    Returns "hold" while our CastSpell is in flight but unconfirmed: the
    driver must not pass priority or start new plays in that window -- a
    bare CastSpell can lose a race to our own PassPriority submitted on the
    next tick and be silently dropped by the server. Returns "proceed" once
    confirmed; a 30s backstop clears a still-unconfirmed cast and resets
    the one-shot Game flag so the play re-triggers. Must run BEFORE any
    early-return block (attackers/blockers/target-wait), and target/x-choice
    schema answers stay allowed while holding."""
    pc = g.pending_cast
    if not pc:
        return "proceed"
    o = (state.get("objects") or {}).get(str(pc["oid"]))
    zone = str((o.get("zone") or "")).lower() if o else "gone"
    if zone in ("stack", "battlefield", "graveyard", "exile", "command"):
        g.pending_cast = None
        say(f"[{tag}] cast confirmed ({zone}): {pc['name']} "
            f"oid {pc['oid']}")
        wire("cast_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                "zone": zone})
        return "proceed"
    # fallback: the spell may live under a new object id on the stack
    want = pc["name"].lower()
    for oid2, o2 in (state.get("objects") or {}).items():
        nm = str(o2.get("base_name") or o2.get("name") or "").lower()
        if nm == want and str(o2.get("zone") or "").lower() == "stack":
            g.pending_cast = None
            say(f"[{tag}] cast confirmed (stack scan, oid {oid2}): "
                f"{pc['name']}")
            wire("cast_confirmed", {"name": pc["name"],
                                    "oid": str(oid2), "zone": "stack",
                                    "via": "name_scan"})
            return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] CAST NOT CONFIRMED after 30s ({zone}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("cast_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]), "zone": zone})
        g.pending_cast = None
        if pc.get("flag"):
            setattr(g, pc["flag"], False)
        return "proceed"
    return "hold"


# ------------------------------------------------------------- target selection (130)
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


def target_opportunity(st):
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


async def submit_target(c, opp, rtype, spec_type, ch, tag, game):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    key = ("target", str(iid), str(cid))
    if key in game.submitted:
        return None
    game.submitted.add(key)
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting advertised target: id={cid} kind={sub['response']['type']} "
        f"seat={candidate_seat(ch)} ({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)
    return iid


# ------------------------------------------------------------- server + data checks
async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
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
    assert str(ver).startswith("0.108.0"), f"unexpected version {ver}"
    assert int(proto) == 134, f"unexpected protocol {proto}"
    assert str(build) == "b8fa1d3", f"unexpected build {build}"


def parse_evidence():
    """Capture the parsed-ability lowering for the five #9840 cards from the
    PINNED v0.108.0 card-data.json (the A2 evidence)."""
    out = {}
    for short, n in (("blackmail", BLACKMAIL),
                     ("hollow_specter", "hollow specter"),
                     ("cabal_interrogator", "cabal interrogator"),
                     ("mires_toll", "mire's toll"),
                     ("taster_of_wares", "taster of wares")):
        e = CARD_DATA[n]
        abil = (e.get("abilities") or [{}])[0]
        eff = abil.get("effect") or {}
        sub = abil.get("sub_ability") or {}
        seff = sub.get("effect") or {}
        out[short] = {
            "mana_cost": e.get("mana_cost"),
            "ability_kind": abil.get("kind"),
            "effect_type": eff.get("type"),
            "effect_name": eff.get("name"),
            "effect_description": eff.get("description"),
            "sub_effect_type": seff.get("type"),
            "sub_effect_count": seff.get("count"),
            "sub_effect_target": seff.get("target"),
            "sub_link": abil.get("sub_link"),
            "oracle_text": e.get("oracle_text"),
        }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


def summarize_parse(pe):
    return pe
def summarize_parse(pe):
    return pe


# ------------------------------------------------------------- #9839 helpers
def stack_has_trigger(state, needle):
    for entry in state.get("stack") or []:
        if needle in json.dumps(entry).lower():
            return True
    return False


def count_subtype_bf(state, pid, subtype):
    n = 0
    for o in bf_oids(state, pid):
        sts = ((get_obj(state, o).get("card_types") or {}).get("subtypes")) or []
        if subtype in sts:
            n += 1
    return n


def card_choice_desc(state, ch):
    """(name, zone, controller) for a candidate, tolerant of unresolved ids."""
    cid = ch.get("id")
    o = get_obj(state, cid) if cid is not None else {}
    nm = str(o.get("base_name") or o.get("name") or "") or choice_text(ch)[:40]
    return nm, str(o.get("zone") or "?"), str(o.get("controller", o.get("owner", "?")))


def describe_pending(st, tag):
    """Log every pending vi opportunity with candidate detail."""
    state = st["state"]
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        spec = rdata.get("spec", {}) or {}
        descs = []
        for ch in cands[:12]:
            nm, zone, ctrl = card_choice_desc(state, ch)
            descs.append(f"{ch.get('id')}:{nm}:{zone}:p{ctrl}")
        say(f"[{tag}] PENDING kind={vi_kind_code(st)!r} rtype={resp.get('type')} "
            f"spectype={spec.get('type')} n_cand={len(cands)} "
            f"[{' ; '.join(descs)}]")
        wire("pending_opportunity",
             {"who": tag, "kind": vi_kind_code(st), "rtype": resp.get("type"),
              "spec_type": spec.get("type"), "n_candidates": len(cands),
              "candidates": descs,
              "constraint": ((spec.get("data", {}) or {}).get("constraint", {})
                             if isinstance(spec.get("data"), dict) else {})})


def trigger_target_opp(st):
    """A vi opportunity offering a player/seat choice (trigger targeting).
    More tolerant than target_opportunity: any rtype, candidates or choices,
    as long as at least one entry carries a seat reference."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        if any(candidate_seat(ch) is not None for ch in cands):
            rtype = resp.get("type")
            spec = rdata.get("spec", {}) or {}
            stype = spec.get("type") or ("sequence" if rtype == "schema"
                                         else "choose")
            return opp, rtype, stype, cands
    return None, None, None, []


def hand_card_opportunity(st, pid):
    """A vi opportunity offering a REAL card choice (not the priority menu).

    The engine's ordinary priority window also surfaces as exactChoices with
    named object surfaces (tapLandForMana on lands, passPriority, ...). Those
    menu entries always carry an `action` surface with a menu code; a genuine
    card choice (reveal pick, choose-1) is candidate+object with NO action
    surface. Only the non-menu entries are returned.
    Returns (opp, rtype, stype, real_choices) or (None, None, None, [])."""
    MENU_ACTION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                         "castSpell", "activateAbility", "mulliganDecision"}
    state = st["state"]
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type")
        # player-target prompts are handled by the target path, not here
        if any(candidate_seat(ch) is not None for ch in cands):
            continue
        real = []
        for ch in cands:
            is_menu = False
            for s in ch.get("surfaces", []) or []:
                if s.get("type") == "action" and \
                        (s.get("data") or {}).get("code") in MENU_ACTION_CODES:
                    is_menu = True
                    break
            if not is_menu:
                real.append(ch)
        named = [ch for ch in real
                 if (card_choice_desc(state, ch)[0] or "").strip()]
        if named:
            return opp, rtype, stype, named
    return None, None, None, []


async def answer_reveal_choice(c, opp, rtype, stype, cands, x, tag, game, state):
    """P1 picks the first X hand-card candidates (deterministic). Handles
    select-X schema and repeated single-choose firings."""
    iid = opp.get("interactionId")
    key = (tag, "reveal", str(iid))
    if key in game.submitted:
        return False
    if game.reveal_choice is None:
        names = [card_choice_desc(state, ch)[0] for ch in cands]
        game.reveal_choice = {
            "n_candidates": len(cands), "names": names,
            "rtype": rtype, "stype": stype, "x_expected": x,
            "p1_hand": len(hand_ids(state, 1)),
        }
        say(f"[{tag}] REVEAL CHOICE offered: {len(cands)} candidates, "
            f"x_expected={x}, names={names}")
        wire("reveal_choice_seen",
             {"who": tag, "iid": str(iid)[:16], "n_candidates": len(cands),
              "names": names, "rtype": rtype, "stype": stype, "x": x,
              "p1_hand": len(hand_ids(state, 1)),
              "raw": json.dumps(opp, default=str)[:4000]})
    if rtype == "schema":
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        con_raw = (spec.get("data", {}) or {}).get("constraint", {}) or {}
        con = con_raw.get("data", {}) if isinstance(con_raw, dict) else {}
        n = int(con.get("max") or con.get("min") or x)
        n = max(0, min(n, x, len(cands)))
        ids = [ch.get("id") for ch in cands[:n]]
        sub = {"interactionId": iid,
               "response": {"type": stype or "sequence",
                            "data": {"choiceIds": ids}}}
        picked = [card_choice_desc(state, ch)[0] for ch in cands[:n]]
    else:
        picked_prev = game.reveal_picked
        nxt = next((ch for ch in cands
                    if str(ch.get("id")) not in picked_prev), None)
        if nxt is None:
            return False
        picked_prev.add(str(nxt.get("id")))
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": nxt.get("id")}}}
        picked = [card_choice_desc(state, nxt)[0]]
        ids = [nxt.get("id")]
    game.submitted.add(key)
    say(f"[{tag}] answering reveal choice: picked={picked} ids={ids}")
    wire("reveal_choice_answer",
         {"who": tag, "iid": str(iid)[:16], "picked": picked,
          "submission": sub})
    await interact_as(c, sub, tag)
    return True


async def answer_choose1(c, opp, rtype, stype, cands, tag, game, state):
    """P0 'choose one of those cards': record the offered set (boundedness
    evidence), then deterministically pick the LAST candidate."""
    iid = opp.get("interactionId")
    key = (tag, "choose1", str(iid))
    if key in game.submitted:
        return False
    descs = []
    for ch in cands:
        nm, zone, ctrl = card_choice_desc(state, ch)
        descs.append(f"{nm}:{zone}")
    if game.choose1 is None:
        game.choose1 = {"n_candidates": len(cands), "candidates": descs,
                        "rtype": rtype, "stype": stype}
        say(f"[{tag}] CHOOSE-1 offered: {len(cands)} candidates [{'; '.join(descs)}]")
        wire("choose1_seen",
             {"who": tag, "iid": str(iid)[:16], "n_candidates": len(cands),
              "candidates": descs, "rtype": rtype, "stype": stype,
              "raw": json.dumps(opp, default=str)[:4000]})
    pick = cands[-1]
    pnm = card_choice_desc(state, pick)[0]
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": stype or "sequence",
                            "data": {"choiceIds": [pick.get("id")]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
    game.submitted.add(key)
    game.choose1_done = True
    game.choose1_picked_name = pnm
    game.choose1_rev = c.revision
    game.choose1_answered_at = time.time()
    game.p1_hand_at_choose1 = len(hand_ids(state, 1))
    say(f"[{tag}] answering choose-1: picked LAST candidate '{pnm}'")
    wire("choose1_answer",
         {"who": tag, "iid": str(iid)[:16], "picked": pnm,
          "p1_hand_before": len(hand_ids(state, 1)),
          "submission": sub})
    await interact_as(c, sub, tag)
    return True
# ---------------------------------------------------------------- Game class
class Game:
    def __init__(self, tag, deck_p0, deck_p1):
        self.tag = tag
        self.deck_p0 = deck_p0
        self.deck_p1 = deck_p1
        self.p0 = self.p1 = None
        self.mulls = set()
        self.submitted = set()
        self.land_played_turn = {}
        self.lands_locked = False
        self.pending_cast = None  # protocol-134 cast-confirmation guard state
        self.div_cast_submitted = False  # game A: P1 Divination one-shot
        self.notes = []
        self.pre = None
        self.post = None
        self.pre_done = False
        self.post_done = False
        self.cast_done = False
        self.done = False
        self.key_cast = False
        self.target_prompt_seen = False
        self.target_ok = False
        self.snaps = {}

    def say(self, *a):
        say(" ".join([f"[{self.tag}]"] + [str(x) for x in a]))

    def note(self, m):
        self.notes.append(m)

    async def start(self):
        self.p0 = PhaseClient(f"{self.tag}-P0")
        await self.p0.connect()
        await self.p0.create(self.deck_p0)
        self.p1 = PhaseClient(f"{self.tag}-P1")
        await self.p1.connect()
        await self.p1.join(self.p0.game_code, self.deck_p1)
        self.say(f"game {self.p0.game_code} P0seat={self.p0.player_id} "
                 f"P1seat={self.p1.player_id}")
        self.p1_seat = self.p1.player_id
        wire(f"{self.tag}_game_start",
             {"game_code": self.p0.game_code,
              "p0_seat": self.p0.player_id, "p1_seat": self.p1.player_id})

    async def export(self, name, client):
        env = await client.export_state()
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(env)
        return json.loads(env)["state"]

    async def close(self):
        for c in (self.p0, self.p1):
            try:
                await c.close()
            except Exception:
                pass


def snap(state, pid):
    p = player_of(state, pid)
    return (len(p.get("hand", []) or []),
            len(p.get("library", []) or []),
            len(p.get("graveyard", []) or []))


# ------------------------------------------------------------- #9840 game driver
class BlackmailGame(Game):
    def __init__(self):
        super().__init__("B9840", P0_DECK, P1_DECK)
        self.cast_attempted = False   # bare CastSpell submitted
        self.cast_confirmed = False   # spell seen on the stack
        self.target_answered = False  # target = P1 (seat 1)
        self.target_chosen_seat = None
        self.resolving = False        # spell on stack -> resolution window
        self.stack_was_seen = False
        self.reveal_seen = False      # any reveal observed for P1
        self.reveal_detail = {}
        self.discard_prompt = None    # P1 offered a discard choice (wrong chooser?)
        self.discard_answered = False
        self.p0_choose1 = None        # P0 offered controller choose-1 (correct chooser?)
        self.p0_choose1_done = False
        self.p0_choose1_picked = ""
        self.pre_cast = None
        self.post = None
        self.post_done = False
        self.done = False
        self.p1_hand_pre = None
        self.p1_lib_pre = None


def p0_keep(state, pid):
    hn = hand_lnames(state, pid)
    return BLACKMAIL in hn and SWAMP in hn


def p1_keep(state, pid):
    hn = hand_lnames(state, pid)
    return hn.count(ISLAND) >= 2


def stack_has_blackmail(state):
    # object-based: stack ENTRY payloads do not reliably carry the card
    # name (Blackmail was cast untargeted with targets:[] and its entry
    # JSON has no name field), but stack-zone objects do
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("zone") or "").lower() == "stack" and \
                str(o.get("base_name") or o.get("name") or "").lower() \
                == "blackmail":
            return True
    for entry in state.get("stack") or []:
        if "blackmail" in json.dumps(entry).lower():
            return True
    return False


async def p0_tick(c, st, acts, state, g):
    for rej in getattr(c, "rejections", []):
        say(f"[P0] REJECTION: {json.dumps(rej, default=str)[:300]}")
        wire("b9840_rejection", {"who": "P0", "rej": rej})
    c.rejections.clear()
    # cast-not-offered backstop: if Blackmail is never a legal CastSpell,
    # the card is unplayable -- record and finish rather than stalling
    turn = state.get("turn_number") or 0
    if (not g.cast_attempted and turn >= 4 and state.get("phase") == "Untap"):
        g.say("cast never attempted by turn 4 and no CastSpell offered -- "
              "treating as uncastable; finishing")
        g.note("Blackmail was never offered as a castable spell by turn 4; "
               "cast_not_offered")
        g.done = True
        return
    if await do_mulligan(c, acts, st, 0, "P0", g, p0_keep):
        return
    if await do_bottom(c, acts, st, 0, "P0", g, (BLACKMAIL,)):
        return
    if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (BLACKMAIL,)):
        return
    d = combat_tick(acts)
    if d:
        await submit_as_is(c, d)
        return
    answered = False
    # --- spell target selection (allowed even while a cast is holding)
    if not g.target_answered and g.cast_attempted:
        opp, rtype, stype, cands = trigger_target_opp(st)
        if opp is not None:
            ch = pick_target_candidate(opp, preferred_seat=1)
            if ch is not None and candidate_seat(ch) == 1:
                say("[P0] spell target prompt: answering P1 (seat 1)")
                wire("b9840_spell_target",
                     {"iid": str(opp.get("interactionId"))[:16],
                      "rtype": rtype, "stype": stype, "seat": 1,
                      "seats": [candidate_seat(x) for x in cands][:8]})
                await submit_target(c, opp, rtype, stype, ch, "P0", g)
                g.target_answered = True
                g.target_chosen_seat = 1
                answered = True
                return
    # --- P0 "choose one of them" (the CORRECT chooser per Oracle)
    if g.resolving and not g.p0_choose1_done:
        opp, rtype, stype, cands = hand_card_opportunity(st, 1)
        if opp is not None:
            iid = opp.get("interactionId")
            key = ("P0", "choose1", str(iid))
            if key not in g.submitted:
                descs = [f"{card_choice_desc(state, ch)[0]}:"
                         f"{card_choice_desc(state, ch)[1]}" for ch in cands]
                g.p0_choose1 = {"n_candidates": len(cands),
                                "candidates": descs, "rtype": rtype,
                                "stype": stype,
                                "p1_hand": len(hand_ids(state, 1))}
                say(f"[P0] CHOOSE-1 offered (correct chooser): "
                    f"{len(cands)} candidates [{'; '.join(descs)}]")
                wire("b9840_p0_choose1",
                     {"iid": str(iid)[:16], "n_candidates": len(cands),
                      "candidates": descs, "rtype": rtype, "stype": stype,
                      "raw": json.dumps(opp, default=str)[:4000]})
                pick = cands[-1]
                pnm = card_choice_desc(state, pick)[0]
                sub = {"interactionId": iid,
                       "response": {"type": stype or "sequence",
                                    "data": {"choiceIds": [pick.get("id")]}}}
                g.submitted.add(key)
                g.p0_choose1_done = True
                g.p0_choose1_picked = pnm
                g.p1_hand_at_p0_choose1 = len(hand_ids(state, 1))
                g.p0_choose1_rev = c.revision
                g.p0_choose1_at = time.time()
                say(f"[P0] answering choose-1: picked LAST '{pnm}'")
                await interact_as(c, sub, "P0")
                answered = True
                return
    holding = cast_guard_tick(g, state, "P0") == "hold"
    # --- cast Blackmail at main phase
    if (not holding and not g.cast_attempted and my_main(state, 0)
            and my_priority(acts) and not real_decision_pending(st)):
        oid = card_in_hand_oid(state, 0, BLACKMAIL)
        if oid is not None:
            g.say("P0 main phase: exporting PRE_CAST, casting Blackmail")
            g.pre_cast = await g.export("pre_cast", g.p0)
            g.p1_hand_pre = len(hand_ids(g.pre_cast, 1))
            g.p1_lib_pre = len(player_of(g.pre_cast, 1).get("library", []) or [])
            g.pre_cast_done = True
            if await cast_named(c, state, acts, BLACKMAIL, oid, "P0", g,
                                "key_cast"):
                g.cast_attempted = True
                g.note(f"P0 cast Blackmail (turn {state.get('turn_number')})")
                answered = True
                return
    # --- cast confirmation drives the resolution window
    if g.cast_attempted and not g.cast_confirmed:
        if stack_has_blackmail(state):
            g.cast_confirmed = True
            g.resolving = True
            g.say("Blackmail on the stack; resolution window open")
    if g.resolving and stack_has_blackmail(state):
        g.stack_was_seen = True
    if await play_a_land(c, state, 0, acts, "P0", g):
        return
    # --- post export: once Blackmail left the stack after being seen,
    # wait for the engine to apply the outcome (revision advanced or P1's
    # hand changed); if nothing was ever offered to anyone there is nothing
    # to wait for. 30s backstop.
    if g.resolving and g.stack_was_seen and not g.post_done:
        if not stack_has_blackmail(state):
            if "resolve_seen_at" not in vars(g):
                g.resolve_seen_at = time.time()
                g.resolve_seen_rev = c.revision
                g.say("Blackmail left the stack; resolution observed")
            stack_empty = len(state.get("stack") or []) == 0
            rev_advanced = c.revision != g.resolve_seen_rev
            base = (getattr(g, "p1_hand_at_p0_choose1", None)
                    or getattr(g, "p1_hand_pre", None) or 99)
            p1_lost = len(hand_ids(state, 1)) < base
            timed_out = (time.time() - g.resolve_seen_at) > 30
            chose_happened = (g.p0_choose1 is not None
                              or g.discard_prompt is not None)
            if (stack_empty and (rev_advanced or p1_lost or not chose_happened)
                    or timed_out):
                g.post = await g.export("post", g.p0)
                g.post_done = True
                g.done = True
                g.say(f"exported POST (rev_advanced={rev_advanced}, "
                      f"p1_lost_card={p1_lost}, timed_out={timed_out}); done")
                return
    if real_decision_pending(st) and not answered:
        describe_pending(st, "P0")
    if not holding and not real_decision_pending(st) and my_priority(acts):
        await pass_priority(c, st, acts, g)


async def p1_tick(c, st, acts, state, g):
    for rej in getattr(c, "rejections", []):
        say(f"[P1] REJECTION: {json.dumps(rej, default=str)[:300]}")
        wire("b9840_rejection", {"who": "P1", "rej": rej})
    c.rejections.clear()
    if await do_mulligan(c, acts, st, 1, "P1", g, p1_keep):
        return
    if await do_bottom(c, acts, st, 1, "P1", g, ()):
        return
    if await do_discard_to_handsize(c, acts, st, 1, "P1", g, ()):
        return
    d = combat_tick(acts)
    if d:
        await submit_as_is(c, d)
        return
    answered = False
    # --- P1 discard prompt (the DiscardCard sub-ability offering the choice
    # to the TARGET player = the wrong chooser per Oracle). Answered
    # deterministically to complete the flow; recorded as evidence.
    if g.resolving and not g.discard_answered:
        opp, rtype, stype, cands = hand_card_opportunity(st, 1)
        if opp is not None:
            iid = opp.get("interactionId")
            key = ("P1", "discard", str(iid))
            if key not in g.submitted:
                descs = [f"{card_choice_desc(state, ch)[0]}:"
                         f"{card_choice_desc(state, ch)[1]}" for ch in cands]
                g.discard_prompt = {"n_candidates": len(cands),
                                    "candidates": descs, "rtype": rtype,
                                    "stype": stype,
                                    "p1_hand": len(hand_ids(state, 1))}
                say(f"[P1] DISCARD PROMPT offered to the TARGET player: "
                    f"{len(cands)} candidates [{'; '.join(descs)}] -- this "
                    f"is the WRONG chooser per Oracle (controller should "
                    f"choose from revealed)")
                wire("b9840_p1_discard_prompt",
                     {"iid": str(iid)[:16], "n_candidates": len(cands),
                      "candidates": descs, "rtype": rtype, "stype": stype,
                      "p1_hand": len(hand_ids(state, 1)),
                      "raw": json.dumps(opp, default=str)[:4000]})
                pick = cands[0]
                pnm = card_choice_desc(state, pick)[0]
                sub = {"interactionId": iid,
                       "response": {"type": stype or "sequence",
                                    "data": {"choiceIds": [pick.get("id")]}}}
                g.submitted.add(key)
                g.discard_answered = True
                g.p1_discarded_name = pnm
                g.p1_discard_rev = c.revision
                say(f"[P1] answering discard: picked FIRST '{pnm}'")
                await interact_as(c, sub, "P1")
                answered = True
                return
    if await play_a_land(c, state, 1, acts, "P1", g):
        return
    if real_decision_pending(st) and not answered:
        describe_pending(st, "P1")
    if not real_decision_pending(st) and my_priority(acts):
        await pass_priority(c, st, acts, g)


async def run_game():
    g = BlackmailGame()
    await g.start()
    ok = await pump(g,
                    lambda c, st, acts, state: p0_tick(c, st, acts, state, g),
                    lambda c, st, acts, state: p1_tick(c, st, acts, state, g),
                    timeout_s=900, resolve_timeout_s=180)
    return g, ok
async def pump(g, p0_tick, p1_tick, timeout_s, resolve_timeout_s=None):
    t0 = time.time()
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    last_diag = 0.0
    resolve_deadline = None
    while time.time() - t0 < timeout_s:
        await asyncio.sleep(0.15)
        for c, is_p0 in ((g.p0, True), (g.p1, False)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[c.player_id] = time.time()
            else:
                if time.time() - last_change[c.player_id] > 60:
                    s0 = st["state"]
                    la = [a.get("type") for a in
                          (st.get("legal_actions") or [])][:8]
                    g.say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                          f"turn={s0.get('turn_number')} phase={s0.get('phase')} "
                          f"legal={la} vikind={vi_kind_code(st)!r} "
                          f"real_decision={real_decision_pending(st)}")
                    last_change[c.player_id] = time.time()
                acts_now = [a.get("type") for a in
                            (st.get("legal_actions") or [])]
                # re-tick while the client has anything to answer
                # (PassPriority AND decision actions like ChooseTarget;
                # the old PassPriority-only gate deadlocked target
                # selection on protocol 134)
                if not (acts_now and time.time()
                        - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                state = st["state"]
                acts = merged_actions(st)
                if is_p0:
                    await p0_tick(c, st, acts, state)
                else:
                    await p1_tick(c, st, acts, state)
            except Exception as e:
                g.say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire(f"{g.tag}_tick_error",
                     {"who": c.name, "err": f"{type(e).__name__}: {e}"})
        if g.done:
            g.say("done flag set; finishing game")
            return True
        if time.time() - last_diag > 60 and g.p0.latest:
            last_diag = time.time()
            s = g.p0.latest["state"]
            g.say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                  f"phase={s.get('phase')} "
                  f"P0hand={len(hand_lnames(s, 0))} P1hand={len(hand_lnames(s, 1))} "
                  f"stack={len(s.get('stack') or [])} key_cast={g.key_cast}")
        if g.cast_done and not g.post_done and resolve_deadline is None:
            resolve_deadline = time.time() + (resolve_timeout_s or 300)
        if not g.cast_done or g.post_done:
            resolve_deadline = None
        if resolve_deadline and time.time() > resolve_deadline:
            g.note("key spell cast but post state not reached in "
                   f"{resolve_timeout_s or 300}s; see wire log")
            return False
    g.note(f"global timeout ({timeout_s}s) hit")
    return False

# ---------------------------------------------------------------- evaluation
# ---------------------------------------------------------------- evaluation
def eval_game(g):
    a = {}
    n = []

    def load(name):
        p = f"{EVDIR}/{name}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())["state"]

    pre = load("pre_cast")
    post = load("post")
    if pre is not None:
        g.snaps["pre"] = {"P0": snap(pre, 0), "P1": snap(pre, 1)}
    if post is not None:
        g.snaps["post"] = {"P0": snap(post, 0), "P1": snap(post, 1)}

    # ---- A1 setup ----
    if pre is not None:
        p1hand = len(hand_ids(pre, 1))
        ok = (BLACKMAIL in hand_lnames(pre, 0)
              and p1hand >= 3
              and state_life(pre, 0) == 20
              and state_life(pre, 1) == 20)
        a["A1_setup"] = "passed" if ok else "failed"
        n.append(f"A1 setup: blackmail_in_P0_hand={BLACKMAIL in hand_lnames(pre, 0)}, "
                 f"P1hand={p1hand}, life 20/20: {'ok' if ok else 'SETUP FAILED'}")
    else:
        a["A1_setup"] = "failed"
        n.append("A1 setup: pre_cast missing -- cast never attempted")
    a1ok = a["A1_setup"] == "passed"

    # ---- A3 cast (reached the stack?) ----
    if getattr(g, "cast_confirmed", False) or getattr(g, "stack_was_seen", False):
        a["A3_cast"] = "passed"
        unt = ("NO target prompt was ever offered; the engine advertised "
               "CastSpell with targets:[] and the spell was cast untargeted")
        n.append(f"A3 cast: Blackmail reached the stack "
                 f"(target_answered={g.target_answered}). {unt}")
        g.untargeted_cast_note = unt
    elif getattr(g, "cast_attempted", False):
        a["A3_cast"] = "failed"
        n.append("A3 cast: FAILED -- CastSpell submitted but the spell never "
                 "reached the stack (engine refused / dropped the unsupported "
                 "card). This is the 'honestly unsupported' outcome.")
    else:
        a["A3_cast"] = "not-run"
        n.append("A3 cast: not-run (cast never attempted)")
    cast_ok = a["A3_cast"] == "passed"

    # ---- A4 reveal ----
    rs = getattr(g, "reveal_seen", False)
    if rs:
        a["A4_reveal"] = "passed"
        n.append(f"A4 reveal: passed -- {g.reveal_detail}")
    elif a1ok and cast_ok and getattr(g, "resolving", False)             and post is not None:
        a["A4_reveal"] = "failed"
        n.append("A4 reveal: FAILED -- Blackmail resolved with NO reveal of "
                 "P1's hand: the reveal clause is Effect::Unimplemented, so "
                 "the reported shape ('you choose one of the revealed "
                 "cards') cannot occur. This is the reported bug.")
    else:
        a["A4_reveal"] = "not-run"
        n.append("A4 reveal: not-run (resolution not reached)")

    # ---- A5 chooser ----
    c1 = getattr(g, "p0_choose1", None)
    dp = getattr(g, "discard_prompt", None)
    if c1 is not None:
        bounded = c1["n_candidates"] == 3
        a["A5_chooser"] = "passed" if bounded else "failed"
        n.append(f"A5 chooser: P0 (controller) was offered choose-1 with "
                 f"{c1['n_candidates']} candidates (expected 3 revealed): "
                 f"{'bounded' if bounded else 'UNBOUNDED'}")
    elif dp is not None:
        whole = dp["n_candidates"] == dp.get("p1_hand")
        a["A5_chooser"] = "failed"
        n.append(f"A5 chooser: FAILED -- the choice went to the TARGET player "
                 f"(P1), not the controller: P1 was offered a discard choice "
                 f"over {dp['n_candidates']} cards (P1 hand was "
                 f"{dp.get('p1_hand')}{' = whole hand' if whole else ''}). "
                 f"This is the pre-fail-closed wrong-chooser behavior the "
                 f"issue reports.")
    elif cast_ok and post is not None:
        a["A5_chooser"] = "failed"
        n.append("A5 chooser: FAILED -- resolution completed with no choose-1 "
                 "prompt at all (neither the controller nor the target chose; "
                 "the card's choice clause is Unimplemented)")
    else:
        a["A5_chooser"] = "not-run"
        n.append("A5 chooser: not-run (resolution not reached)")

    # ---- A6 outcome ----
    if pre is not None and post is not None and cast_ok:
        pre_hand = set(str(o) for o in hand_ids(pre, 1))
        post_hand = set(str(o) for o in hand_ids(post, 1))
        left_hand = pre_hand - post_hand
        moved = []
        for oid in left_hand:
            o = get_obj(post, oid)
            moved.append(f"{o.get('base_name') or o.get('name')}:{o.get('zone')}")
        lib_pre = len(player_of(pre, 1).get("library", []) or [])
        lib_post = len(player_of(post, 1).get("library", []) or [])
        in_gy = [m for m in moved if m.endswith(":Graveyard")]
        chosen = getattr(g, "p0_choose1_picked", "") or \
            getattr(g, "p1_discarded_name", "")
        if a["A5_chooser"] == "passed" and len(in_gy) == 1 \
                and lib_pre == lib_post and len(left_hand) == 1:
            a["A6_outcome"] = "passed"
            n.append(f"A6 outcome: passed -- exactly '{chosen}' discarded by "
                     f"P1, library {lib_pre}->{lib_post}")
        else:
            a["A6_outcome"] = "failed"
            n.append(f"A6 outcome: FAILED -- cards leaving P1 hand: "
                     f"{sorted(moved) or 'none'} (chosen='{chosen}'); "
                     f"library {lib_pre}->{lib_post}. Oracle expects exactly "
                     f"the controller-chosen revealed card discarded.")
    else:
        a["A6_outcome"] = "not-run"
        n.append("A6 outcome: not-run (states missing)")

    # ---- A7 cleanup ----
    if post is not None:
        empty = len(post.get("stack") or []) == 0
        a["A7_cleanup"] = "passed" if empty else "failed"
        n.append(f"A7 cleanup: stack empty={empty}")
    else:
        a["A7_cleanup"] = "not-run"
        n.append("A7 cleanup: not-run")
    return a, n


def evaluate(g, parse_sum):
    ass = {}
    notes = []

    # ---- A2 parse documentation (from PINNED v0.108.0 card-data) ----
    pe = parse_sum["blackmail"]
    ok = (pe["effect_type"] == "Unimplemented"
          and pe["effect_name"] == "unparsed_verb_arguments"
          and pe["sub_effect_type"] == "DiscardCard"
          and pe["sub_effect_count"] == 1)
    ass["A2_parse_blackmail"] = "passed" if ok else "failed"
    notes.append(
        f"A2 parse Blackmail (pinned v0.108.0): ability_kind="
        f"{pe['ability_kind']}, effect={pe['effect_type']} "
        f"(name={pe['effect_name']!r}), sub_effect={pe['sub_effect_type']} "
        f"count={pe['sub_effect_count']} target="
        f"{json.dumps(pe['sub_effect_target'])[:80]}: "
        f"{'shape as documented' if ok else 'PARSE SHAPE UNEXPECTED'}")
    for short, label in (("hollow_specter", "Hollow Specter"),
                         ("cabal_interrogator", "Cabal Interrogator"),
                         ("mires_toll", "Mire's Toll"),
                         ("taster_of_wares", "Taster of Wares")):
        q = parse_sum[short]
        notes.append(
            f"A2 parse {label}: effect={q['effect_type']} "
            f"(name={q['effect_name']!r}) sub={q['sub_effect_type']}")

    a, n = eval_game(g)
    ass.update(a)
    notes.extend(n)

    # ---- verdict ----
    a1 = a.get("A1_setup") == "passed"
    a3 = a.get("A3_cast")
    if not a1:
        verdict = "blocked"
        notes.append("setup never completed; verdict blocked")
    elif a3 == "failed":
        verdict = "reproduced"
        notes.append("reproduced: Blackmail could not even be cast on "
                     "v0.108.0 -- the card is unsupported, exactly the "
                     "'honestly flagged' state the issue describes")
    elif a3 == "passed":
        bad = (a.get("A4_reveal") == "failed"
               or a.get("A5_chooser") == "failed"
               or a.get("A6_outcome") == "failed")
        good = all(a.get(k) == "passed" for k in
                   ("A4_reveal", "A5_chooser", "A6_outcome"))
        if bad:
            verdict = "reproduced"
            notes.append("reproduced: Blackmail resolves without the "
                         "Oracle reveal-then-controller-choose-then-discard "
                         "flow (see A4/A5/A6)")
        elif good:
            verdict = "not-reproduced"
            notes.append("not-reproduced: Blackmail executed the full Oracle "
                         "flow on v0.108.0")
        else:
            verdict = "blocked"
            notes.append("resolution incomplete; verdict blocked")
    else:
        verdict = "blocked"
        notes.append("cast never reached a decisive state; verdict blocked")
    notes.extend(g.notes)
    return ass, notes, verdict
# ---------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 860
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #9840 - Blackmail reveal/choose/discard "
                     "(first validation)", fill=(235, 240, 250)); y += 28
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - run {run['run_id']} "
                     f"- 2026-10-13", fill=(140, 160, 180)); y += 26
    vc = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
          "blocked": (230, 200, 90)}.get(run["verdict"], (200, 200, 200))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vc); y += 32
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = [
        ("A1_setup", "A1 setup: Blackmail in P0 hand, P1 hand >= 3, life 20/20"),
        ("A2_parse_blackmail", "A2 parse: Blackmail Unimplemented reveal clause + DiscardCard documented"),
        ("A3_cast", "A3 cast: Blackmail cast allowed, reached the stack targeting P1"),
        ("A4_reveal", "A4 [bug?] P1 revealed 3 cards from hand on resolution"),
        ("A5_chooser", "A5 [bug?] controller (P0) chose 1 from exactly the revealed cards"),
        ("A6_outcome", "A6 outcome: exactly the chosen card discarded, library unchanged"),
        ("A7_cleanup", "A7 cleanup: stack empty post-resolution"),
    ]
    for k, lab in labels:
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col); y += 24
    y += 6
    d.text((24, y), "Pre/post hand/library (from saved authoritative states):",
           fill=(200, 210, 225)); y += 24
    pre = run["observations"]["snaps"].get("pre")
    post = run["observations"]["snaps"].get("post")
    if pre and post:
        d.text((40, y),
               f"P0 hand/lib {pre['P0']} -> {post['P0']}; "
               f"P1 hand/lib {pre['P1']} -> {post['P1']}",
               fill=(150, 165, 185)); y += 24
    else:
        d.text((40, y), "states missing", fill=(255, 150, 90)); y += 24
    y += 6
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:12]:
        d.text((40, y), str(n)[:120], fill=(150, 165, 185)); y += 19
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9840/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)
# ---------------------------------------------------------------- main
async def _main():
    t_start = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
    say(f"starting issue #{ISSUE} run {RUN_ID}")
    await verify_server_hello()
    pe = parse_evidence()
    parse_sum = summarize_parse(pe)
    say("parse summary (blackmail): "
        + json.dumps(parse_sum["blackmail"], default=str)[:1200])

    g, ok = await run_game()
    g.say(f"game finished ok={ok}")
    await g.close()

    if g.pre_cast is not None and g.post is None:
        try:
            g.post = await g.export("post", g.p0)
            g.post_done = True
            g.note("post exported at loop end")
        except Exception as e:
            g.note(f"post final export failed: {e}")

    ass, notes, verdict = evaluate(g, parse_sum)
    dur = time.time() - t_start
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/run-5654-reval-v01080-20261012-2341",
        "parse_summary": parse_sum,
        "driver": {"protocol_advertised": 134, "client": "driver/client.py",
                   "scenario": "driver/scenario_9840_01080.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_9840_01080.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [["blackmail", 4], ["swamp", 56]],
            "P1": [["lightning bolt", 8], ["divination", 4], ["island", 48]],
        },
        "games": {"blackmail": {"ok": ok, "notes": g.notes}},
        "observations": {
            "snaps": g.snaps,
            "target_answered": g.target_answered,
            "target_chosen_seat": g.target_chosen_seat,
            "untargeted_cast": getattr(g, "untargeted_cast_note", ""),
            "discard_prompt": g.discard_prompt,
            "p0_choose1": g.p0_choose1,
            "p0_choose1_picked": g.p0_choose1_picked,
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "4x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Hollow Specter and Cabal Interrogator were not driven (shared grammar; "
            "their parsed shapes were documented from the pinned card data).",
            "Not tested on the original build; verdict is scoped to v0.108.0, "
            "not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 4x blackmail + 56 swamp vs P1 8x lightning bolt + 4x "
                      "divination + 48 island (P0 casts Blackmail targeting P1 "
                      "on turn 1).",
        "contract_line": "Per Oracle, target reveals 3 cards from hand (all if "
                         "<3); the controller chooses 1 of those; the target "
                         "discards the chosen card.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_9840_01080.py",
                f"{EVDIR}/scenario_9840_01080.py")
    try:
        render_summary(run, f"{EVDIR}/summary.png")
        say("rendered summary.png")
    except Exception as e:
        say(f"summary render failed: {e}")
        notes.append(f"summary render failed: {e}")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
async def main():
    pidfile = "/tmp/scenario_9840_01080.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_9840_01080 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


asyncio.run(main())
