"""Issue #9841: Mire's Toll / Thought Rattle -- redundant RevealChoice
acknowledgement before the "you choose one of those cards" prompt.

FIRST VALIDATION on the pinned release v0.108.0 (build b8fa1d3, protocol 134).
Protocol-134 driver conventions (verified by scenario_9839_01080.py,
2026-10-12): CreateGameWithSettings + JoinGameWithPassword + start_when_full,
bare CastSpell (engine auto-pays), cast-confirmation guard, repeating prompts
answered per-firing keyed by interactionId, legacy ChooseTarget alongside the
vi-schema target path, real_decision_pending gating priority passes.

ISSUE (phase-rs/phase#9841, opened 2026-10-12 by matthewevans, open,
bug/area:engine): when the revealed cards are already visible to the choosing
player, the engine still raises a RevealChoice acknowledgement before the
ChooseFromZoneChoice prompt, so the player clicks twice for one decision.
The outcome of the effect is correct; this is a UX defect. Reporter's
confidence: moderate, not re-measured after the latest reveal changes.
Where: reveal_hand::resolve, ChooseFromZoneChoice handling in
engine_resolution_choices. CR 701.20a.

Oracle text (verified from pinned v0.108.0 card-data.json):
  Mire's Toll {1}{B} Sorcery (pinned card-data cost: {B}):
    "Target player reveals a number of cards from their hand equal to the
     number of Swamps you control. You choose one of them. That player
     discards that card."
  Thought Rattle {U}{B} Sorcery:
    "Target opponent reveals each nonland card in their hand. You choose one
     of those cards. Exile that card. ..." (threshold/seek/lifegain clauses
     omitted; not driven)

Pinned parse (v0.108.0 card-data.json): Mire's Toll abilities[0].effect is
RevealHand; its sub-effect is ChooseFromZone count=1 (the choose clause IS
modelled -- contrast #9840's Blackmail, whose chooser is Unimplemented).
Thought Rattle abilities[0].effect is RevealHand (nonland filter).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
P0 (seat 0) holds Mire's Toll + Swamps, casts it on turn 2 with 2 Swamps on
the battlefield, targeting P1. P1 holds ~7-9 cards and never casts.

Expected per the issue's "Expected" (one selection prompt for the
controller, with the revealed cards displayed):
  E1: on resolution P1 reveals 2 cards from their hand;
  E2: P0 (the spell's controller) is offered exactly ONE prompt --
      "choose one of them" (ChooseFromZoneChoice) over exactly the 2
      revealed cards (a bounded set);
  E3: P1 discards the card P0 chose.

The reported bug: E2 is preceded by a redundant RevealChoice
acknowledgement offered to the choosing player, i.e. TWO sequential prompts
for one decision.

Assertions:
  A1_setup    pre: Mire's Toll in P0 hand, 2 Swamps on P0 battlefield at
                cast, P1 hand >= 2, life 20/20.
  A2_parse    parse evidence captured from pinned v0.108.0 card-data for
                Mire's Toll (RevealHand + ChooseFromZone) and Thought Rattle
                (RevealHand, nonland filter).
  A3_cast     the spell's cast was allowed, targeted P1, reached the stack.
  A4_reveal   on resolution P1 revealed 2 cards (= Swamps P0 controls).
  A5_no_redundant_ack  [the reported bug] P0's prompt sequence during
                resolution contained NO RevealChoice acknowledgement before
                the ChooseFromZoneChoice prompt. FAILS (= reproduced) if a
                reveal-ack opportunity was offered to the choosing player
                ahead of the choose prompt.
  A6_chooser  the CONTROLLER (P0) was offered choose-1 from exactly the 2
                revealed cards (bounded set).
  A7_outcome  P1 discarded exactly the controller-chosen card; no other
                P1 cards moved; library unchanged by the reveal.
  A8_cleanup  stack empty post-resolution.

Verdict rule: reproduced iff A1, A3, A4, A6, A7 pass and A5 fails (the
redundant ack was observed). not-reproduced iff A1, A3, A4, A5, A6, A7 all
pass (single prompt, correct outcome). blocked iff setup never completed or
resolution never reached a decisive state.

Driver answering policy (deterministic):
  - spell target: P1 (seat 1).
  - P1 reveal-N choice (if offered): first N candidates in offered order.
  - reveal ack (either seat, if offered): acknowledged generically with the
    first/only choice; the opportunity is recorded verbatim as the bug
    evidence.
  - P0 choose-1: if it is the FIRST prompt of the resolution (no prior ack),
    answer FIRST candidate; if it follows a reveal ack, answer LAST
    candidate. The picked card name is recorded and the outcome assertion
    checks exactly that card was discarded.
Unknown decisions are logged with raw opportunity JSON, never auto-answered
beyond the ack/choose policy above.

Evidence: evidence/9841/run-9841-v01080-20261013-0235/pre_cast.json,
post.json, parse_evidence.json, run.json, manifest.sha256, summary.png,
scenario_9841_01080.py, wire_log.jsonl, scenario_run.log
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
RUN_ID = "run-9841-v01080-20261013-0235"
ISSUE = 9841
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MIRES_TOLL = "mire's toll"
SWAMP = "swamp"
BOLT = "lightning bolt"
DIV = "divination"
ISLAND = "island"
P0_DECK = deck((MIRES_TOLL, 4), (SWAMP, 56))
P1_DECK = deck((BOLT, 8), (DIV, 4), (ISLAND, 48))
N_REVEAL = 2  # P0 casts with 2 Swamps on the battlefield

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
    """Capture the parsed-ability lowering for the two #9841 cards from the
    PINNED v0.108.0 card-data.json (the A2 evidence)."""
    out = {}
    for short, n in (("mires_toll", MIRES_TOLL),
                     ("thought_rattle", "thought rattle")):
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
            "effect_target": eff.get("target"),
            "effect_card_filter": eff.get("card_filter"),
            "effect_count": eff.get("count"),
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


def all_real_opportunities(st, state):
    """All non-priority-menu vi opportunities with real (non-menu) choices.

    Like hand_card_opportunity but keeps ack-shaped opportunities too (a
    reveal ack may be a single acknowledge/OK choice with no card
    candidates). The priority menu (all choices carry menu action codes) is
    excluded. Player-target prompts are excluded (handled by the target
    path). Returns [(opp, rtype, stype, real_choices)].
    """
    MENU_ACTION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                         "castSpell", "activateAbility", "mulliganDecision"}
    out = []
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        spec = rdata.get("spec", {}) or {}
        stype = spec.get("type")
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
        if real:
            out.append((opp, rtype, stype, real))
    return out


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


# ------------------------------------------------------------- #9841 game driver
class MiresTollGame(Game):
    def __init__(self):
        super().__init__("B9841", P0_DECK, P1_DECK)
        self.cast_attempted = False   # bare CastSpell submitted
        self.cast_confirmed = False   # spell seen on the stack
        self.target_answered = False  # target = P1 (seat 1)
        self.target_chosen_seat = None
        self.resolving = False        # spell on stack -> resolution window
        self.stack_was_seen = False
        self.p0_prompts = []          # prompt sequence seen by P0 (chooser)
        self.p1_prompts = []          # prompt sequence seen by P1 (revealer)
        self.p1_reveal_choice = None  # P1 offered reveal-N choice
        self.reveal_acked = {}        # seat -> ack opportunity record
        self.choose1 = None           # P0 offered controller choose-1
        self.choose1_done = False
        self.choose1_picked = ""
        self.choose1_pick_oid = None
        self.choose1_first_was_choose = False  # no ack preceded the choose
        self.swamps_at_cast = 0
        self.pre_cast = None
        self.post = None
        self.post_done = False
        self.done = False
        self.p1_hand_pre = None
        self.p1_lib_pre = None


def p0_keep(state, pid):
    hn = hand_lnames(state, pid)
    return MIRES_TOLL in hn and SWAMP in hn


def p1_keep(state, pid):
    return len(hand_lnames(state, pid)) >= 2


def stack_has_mirestoll(state):
    # object-based: stack ENTRY payloads do not reliably carry the card
    # name, but stack-zone objects do (#9841 driver note)
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("zone") or "").lower() == "stack" and \
                str(o.get("base_name") or o.get("name") or "").lower() \
                == "mire's toll":
            return True
    for entry in state.get("stack") or []:
        if "mire" in json.dumps(entry).lower():
            return True
    return False


def _opp_text_blob(opp):
    return json.dumps(opp, default=str).lower()


def looks_like_reveal_ack(opp, kind_code):
    """Heuristic: is this opportunity a reveal acknowledgement rather than
    the ChooseFromZoneChoice? Checks the waitingForKind code, choice
    surface codes/text for reveal/acknowledge markers, the selectCards
    action shape (observed 2026-10-13: the RevealChoice ack is exactChoices
    whose choices carry selectCards actions over the already-revealed
    cards, object role "selected"), and the bare single-button shape."""
    blob = _opp_text_blob(opp)
    if "reveal" in (kind_code or "").lower():
        return True
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
    cands = rdata.get("candidates") or rdata.get("choices") or []
    for ch in cands:
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "action" and \
                    (s.get("data") or {}).get("code") == "selectCards":
                return True
    for marker in ("revealchoice", "acknowledgereveal", "acknowledge_reveal",
                   "reveal_ack", "revealack"):
        if marker in blob.replace(" ", "").replace("_", ""):
            return True
    # a bare acknowledge/continue/ok/done single choice with no card
    # candidates is ack-shaped
    if len(cands) == 1:
        t = (choice_text(cands[0]) or "").lower()
        if any(w in t for w in ("acknowledge", "reveal", "continue", "ok",
                                "done", "confirm")):
            return True
    return False


def classify_prompt(opp, state, kind_code):
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
    cands = rdata.get("candidates") or rdata.get("choices") or []
    spec = rdata.get("spec", {}) or {}
    descs = []
    for ch in cands[:12]:
        nm, zone, ctrl = card_choice_desc(state, ch)
        descs.append(f"{nm}:{zone}")
    return {
        "kind_code": kind_code,
        "rtype": resp.get("type"),
        "spec_type": spec.get("type"),
        "n_candidates": len(cands),
        "candidates": descs,
        "ack_like": looks_like_reveal_ack(opp, kind_code),
        "at": time.time(),
    }


async def answer_first_choice(c, opp, tag):
    """Generic acknowledgement: submit the first available choice."""
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
    cands = rdata.get("candidates") or rdata.get("choices") or []
    avail = [ch for ch in cands
             if ch.get("status", {}).get("type") in (None, "available")]
    pick = (avail or cands)[0]
    rtype = resp.get("type")
    spec = rdata.get("spec", {}) or {}
    stype = spec.get("type")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": stype or "sequence",
                            "data": {"choiceIds": [pick.get("id")]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose",
                            "data": {"choiceId": pick.get("id")}}}
    say(f"[{tag}] ack-style answer: '{choice_text(pick)[:60]}'")
    wire("ack_answer", {"who": tag, "iid": str(iid)[:16],
                        "picked": choice_text(pick)[:80],
                        "submission": sub})
    await interact_as(c, sub, tag)


async def p0_tick(c, st, acts, state, g):
    for rej in getattr(c, "rejections", []):
        say(f"[P0] REJECTION: {json.dumps(rej, default=str)[:300]}")
        wire("b9841_rejection", {"who": "P0", "rej": rej})
    c.rejections.clear()
    # cast-not-offered backstop: Mire's Toll is cast on turn 2; if it was
    # never a legal CastSpell by turn 5, the card is unplayable -- record
    # and finish rather than stalling
    turn = state.get("turn_number") or 0
    if (not g.cast_attempted and turn >= 5 and state.get("phase") == "Untap"):
        g.say("cast never attempted by turn 5 and no CastSpell offered -- "
              "treating as uncastable; finishing")
        g.note("Mire's Toll was never offered as a castable spell by turn 5; "
               "cast_not_offered")
        g.done = True
        return
    if await do_mulligan(c, acts, st, 0, "P0", g, p0_keep):
        return
    if await do_bottom(c, acts, st, 0, "P0", g, (MIRES_TOLL,)):
        return
    if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (MIRES_TOLL,)):
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
                wire("b9841_spell_target",
                     {"iid": str(opp.get("interactionId"))[:16],
                      "rtype": rtype, "stype": stype, "seat": 1,
                      "seats": [candidate_seat(x) for x in cands][:8]})
                await submit_target(c, opp, rtype, stype, ch, "P0", g)
                g.target_answered = True
                g.target_chosen_seat = 1
                answered = True
                return
    # --- resolution prompts: record the FULL prompt sequence for the
    # choosing player. The reported bug is a RevealChoice ack ahead of the
    # ChooseFromZoneChoice; the order of opportunities is the evidence.
    if g.resolving:
        for (opp, rtype, stype, real) in all_real_opportunities(st, state):
            iid = opp.get("interactionId")
            key = ("P0", "resopp", str(iid))
            if key in g.submitted:
                continue
            cls = classify_prompt(opp, state, vi_kind_code(st))
            cls["seat"] = "P0"
            g.p0_prompts.append(cls)
            say(f"[P0] resolution prompt #{len(g.p0_prompts)}: "
                f"kind={cls['kind_code']!r} rtype={rtype} "
                f"n_cand={cls['n_candidates']} ack_like={cls['ack_like']} "
                f"[{'; '.join(cls['candidates'][:6])}]")
            wire("b9841_p0_prompt",
                 {"seq": len(g.p0_prompts), "cls": cls,
                  "iid": str(iid)[:16],
                  "raw": json.dumps(opp, default=str)[:4000]})
            g.submitted.add(key)
            if cls["ack_like"] and "P0" not in g.reveal_acked:
                g.reveal_acked["P0"] = cls
                say("[P0] answering reveal ACK (first/only choice)")
                await answer_first_choice(c, opp, "P0")
            elif not g.choose1_done:
                # the choose-1: FIRST candidate if no ack preceded it (bug
                # absent), LAST candidate if an ack did (bug present).
                # Either way the pick is recorded for the outcome assert.
                pick = real[0] if "P0" not in g.reveal_acked else real[-1]
                pnm = card_choice_desc(state, pick)[0]
                poid = _cand_reference(pick)
                g.choose1 = dict(cls)
                g.choose1["p1_hand"] = len(hand_ids(state, 1))
                g.choose1["picked"] = pnm
                g.choose1_first_was_choose = "P0" not in g.reveal_acked
                if rtype == "schema":
                    sub = {"interactionId": iid,
                           "response": {"type": stype or "sequence",
                                        "data": {"choiceIds": [pick.get("id")]}}}
                else:
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": pick.get("id")}}}
                g.choose1_done = True
                g.choose1_picked = pnm
                g.choose1_pick_oid = poid
                g.p1_hand_at_choose1 = len(hand_ids(state, 1))
                say(f"[P0] answering choose-1: picked "
                    f"{'FIRST' if g.choose1_first_was_choose else 'LAST'} "
                    f"'{pnm}'")
                wire("b9841_p0_choose1",
                     {"iid": str(iid)[:16], "picked": pnm,
                      "pick_oid": str(poid),
                      "first_was_choose": g.choose1_first_was_choose,
                      "submission": sub})
                await interact_as(c, sub, "P0")
            else:
                say("[P0] unexpected third resolution prompt; ack-answering")
                wire("b9841_p0_third_prompt", {"iid": str(iid)[:16]})
                await answer_first_choice(c, opp, "P0")
            answered = True
            return
    holding = cast_guard_tick(g, state, "P0") == "hold"
    # --- cast Mire's Toll on turn >= 2 with 2 Swamps (reveal 2 = real choice)
    if (not holding and not g.cast_attempted and (turn >= 2)
            and my_main(state, 0) and my_priority(acts)
            and not real_decision_pending(st)):
        nsw = count_subtype_bf(state, 0, "Swamp")
        oid = card_in_hand_oid(state, 0, MIRES_TOLL)
        if oid is not None and nsw >= N_REVEAL:
            g.say(f"P0 main phase turn {turn}: exporting PRE_CAST "
                  f"({nsw} swamps), casting Mire's Toll")
            g.pre_cast = await g.export("pre_cast", g.p0)
            g.p1_hand_pre = len(hand_ids(g.pre_cast, 1))
            g.p1_lib_pre = len(player_of(g.pre_cast, 1).get("library", []) or [])
            g.swamps_at_cast = nsw
            g.pre_cast_done = True
            if await cast_named(c, state, acts, MIRES_TOLL, oid, "P0", g,
                                "key_cast"):
                g.cast_attempted = True
                g.note(f"P0 cast Mire's Toll (turn {turn}, {nsw} swamps)")
                answered = True
                return
    # --- cast confirmation drives the resolution window
    if g.cast_attempted and not g.cast_confirmed:
        if stack_has_mirestoll(state):
            g.cast_confirmed = True
            g.resolving = True
            g.say("Mire's Toll on the stack; resolution window open")
    if g.resolving and stack_has_mirestoll(state):
        g.stack_was_seen = True
    if await play_a_land(c, state, 0, acts, "P0", g):
        return
    # --- post export: once Mire's Toll left the stack after being seen,
    # wait for the engine to apply the outcome (revision advanced or P1's
    # hand changed). 30s backstop.
    if g.resolving and g.stack_was_seen and not g.post_done:
        if not stack_has_mirestoll(state):
            if "resolve_seen_at" not in vars(g):
                g.resolve_seen_at = time.time()
                g.resolve_seen_rev = c.revision
                g.say("Mire's Toll left the stack; resolution observed")
            stack_empty = len(state.get("stack") or []) == 0
            rev_advanced = c.revision != g.resolve_seen_rev
            base = (getattr(g, "p1_hand_at_choose1", None)
                    or getattr(g, "p1_hand_pre", None) or 99)
            p1_lost = len(hand_ids(state, 1)) < base
            timed_out = (time.time() - g.resolve_seen_at) > 30
            chose_happened = (g.choose1 is not None
                              or "P0" in g.reveal_acked)
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
        wire("b9841_rejection", {"who": "P1", "rej": rej})
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
    # --- P1 resolution prompts: the reveal-N choice (if the engine asks the
    # revealer to pick) and any reveal ack. Recorded; answered
    # deterministically.
    if g.resolving:
        for (opp, rtype, stype, real) in all_real_opportunities(st, state):
            iid = opp.get("interactionId")
            key = ("P1", "resopp", str(iid))
            if key in g.submitted:
                continue
            cls = classify_prompt(opp, state, vi_kind_code(st))
            cls["seat"] = "P1"
            g.p1_prompts.append(cls)
            say(f"[P1] resolution prompt #{len(g.p1_prompts)}: "
                f"kind={cls['kind_code']!r} rtype={rtype} "
                f"n_cand={cls['n_candidates']} ack_like={cls['ack_like']} "
                f"[{'; '.join(cls['candidates'][:6])}]")
            wire("b9841_p1_prompt",
                 {"seq": len(g.p1_prompts), "cls": cls,
                  "iid": str(iid)[:16],
                  "raw": json.dumps(opp, default=str)[:4000]})
            g.submitted.add(key)
            if cls["ack_like"]:
                g.reveal_acked["P1"] = cls
                say("[P1] answering reveal ACK (first/only choice)")
                await answer_first_choice(c, opp, "P1")
            elif g.p1_reveal_choice is None:
                # reveal-N choice: pick the first N candidates
                n = min(N_REVEAL, len(real))
                picks = [ch.get("id") for ch in real[:n]]
                picked_names = [card_choice_desc(state, ch)[0]
                                for ch in real[:n]]
                g.p1_reveal_choice = {
                    "n_candidates": len(real),
                    "picked": picked_names,
                    "p1_hand": len(hand_ids(state, 1)),
                }
                if rtype == "schema":
                    sub = {"interactionId": iid,
                           "response": {"type": stype or "sequence",
                                        "data": {"choiceIds": picks}}}
                else:
                    # repeated single-choose firings: answer one at a time
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": picks[0]}}}
                    g.p1_reveal_choice["pending_more"] = n - 1
                say(f"[P1] answering reveal choice: picked {picked_names}")
                wire("b9841_p1_reveal_choice",
                     {"iid": str(iid)[:16], "picked": picked_names,
                      "submission": sub})
                await interact_as(c, sub, "P1")
            else:
                say("[P1] unexpected extra resolution prompt; ack-answering")
                await answer_first_choice(c, opp, "P1")
            answered = True
            return
    if await play_a_land(c, state, 1, acts, "P1", g):
        return
    if real_decision_pending(st) and not answered:
        describe_pending(st, "P1")
    if not real_decision_pending(st) and my_priority(acts):
        await pass_priority(c, st, acts, g)


async def run_game():
    g = MiresTollGame()
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
        ok = (MIRES_TOLL in hand_lnames(pre, 0)
              and getattr(g, "swamps_at_cast", 0) >= N_REVEAL
              and p1hand >= N_REVEAL
              and state_life(pre, 0) == 20
              and state_life(pre, 1) == 20)
        a["A1_setup"] = "passed" if ok else "failed"
        n.append(f"A1 setup: mires_toll_in_P0_hand="
                 f"{MIRES_TOLL in hand_lnames(pre, 0)}, "
                 f"swamps_at_cast={getattr(g, 'swamps_at_cast', 0)}, "
                 f"P1hand={p1hand}, life 20/20: "
                 f"{'ok' if ok else 'SETUP FAILED'}")
    else:
        a["A1_setup"] = "failed"
        n.append("A1 setup: pre_cast missing -- cast never attempted")
    a1ok = a["A1_setup"] == "passed"

    # ---- A3 cast (reached the stack, targeted P1?) ----
    if getattr(g, "cast_confirmed", False) or getattr(g, "stack_was_seen", False):
        tgt = getattr(g, "target_answered", False) and \
            getattr(g, "target_chosen_seat", None) == 1
        a["A3_cast"] = "passed" if tgt else "failed"
        n.append(f"A3 cast: Mire's Toll reached the stack; "
                 f"targeted P1 (seat 1): {tgt}")
    elif getattr(g, "cast_attempted", False):
        a["A3_cast"] = "failed"
        n.append("A3 cast: FAILED -- CastSpell submitted but the spell never "
                 "reached the stack.")
    else:
        a["A3_cast"] = "not-run"
        n.append("A3 cast: not-run (cast never attempted)")
    cast_ok = a["A3_cast"] == "passed"

    # ---- A4 reveal (did P1 reveal N_REVEAL cards?) ----
    # Evidence: P1 answered a reveal-N choice, and/or P0's choose-1 was
    # offered over exactly N_REVEAL cards drawn from P1's hand.
    rev_choice = getattr(g, "p1_reveal_choice", None)
    c1 = getattr(g, "choose1", None)
    pre_p1_names = ([obj_lname(pre, o) for o in hand_ids(pre, 1)]
                    if pre is not None else [])
    if cast_ok and (rev_choice is not None or c1 is not None):
        from_hand = True
        if c1 is not None:
            cand_names = [d.split(":")[0].lower()
                          for d in c1.get("candidates", [])]
            from_hand = (len(cand_names) == N_REVEAL
                         and all(nm in pre_p1_names for nm in cand_names))
        ok = from_hand and (rev_choice is not None
                            or (c1 is not None
                                and c1.get("n_candidates") == N_REVEAL))
        a["A4_reveal"] = "passed" if ok else "failed"
        n.append(f"A4 reveal: {'passed' if ok else 'FAILED'} -- "
                 f"p1_reveal_choice={rev_choice is not None}, "
                 f"choose1_n={c1.get('n_candidates') if c1 else None} "
                 f"(expected {N_REVEAL}), candidates_from_P1_hand={from_hand}")
    elif cast_ok and post is not None:
        a["A4_reveal"] = "failed"
        n.append("A4 reveal: FAILED -- resolution completed with no reveal "
                 "step observed (no P1 reveal choice, no choose-1).")
    else:
        a["A4_reveal"] = "not-run"
        n.append("A4 reveal: not-run (resolution not reached)")

    # ---- A5 no redundant ack (THE reported bug) ----
    # FAILS (= reproduced) when the choosing player (P0) was offered a
    # RevealChoice acknowledgement during the resolution window.
    ack0 = g.reveal_acked.get("P0")
    if c1 is None and ack0 is None:
        a["A5_no_redundant_ack"] = "not-run"
        n.append("A5 no_redundant_ack: not-run (no resolution prompts seen)")
    elif ack0 is not None:
        a["A5_no_redundant_ack"] = "failed"
        order = ""
        if c1 is not None:
            order = ("ack_at=%.1f choose_at=%.1f ack_first=%s"
                     % (ack0.get("at", 0), c1.get("at", 0),
                        ack0.get("at", 0) <= c1.get("at", 0)))
        n.append(f"A5 no_redundant_ack: FAILED (= bug reproduced) -- P0 was "
                 f"offered a reveal ack (kind={ack0.get('kind_code')!r}, "
                 f"n_cand={ack0.get('n_candidates')}) during resolution. "
                 f"{order} P0 prompt sequence: "
                 f"{[(p.get('kind_code'), p.get('n_candidates'), p.get('ack_like')) for p in g.p0_prompts]}")
    else:
        a["A5_no_redundant_ack"] = "passed"
        n.append(f"A5 no_redundant_ack: passed -- P0 saw exactly one "
                 f"resolution prompt (the choose-1); no reveal ack. "
                 f"P1 prompts: {len(g.p1_prompts)} "
                 f"(ack for P1: {'P1' in g.reveal_acked})")

    # ---- A6 chooser (controller choose-1, bounded to the revealed set) ----
    if c1 is not None:
        bounded = (c1.get("n_candidates") == N_REVEAL
                   and all(d.split(":")[0].lower() in pre_p1_names
                           for d in c1.get("candidates", [])))
        a["A6_chooser"] = "passed" if bounded else "failed"
        n.append(f"A6 chooser: {'passed' if bounded else 'FAILED'} -- P0 "
                 f"(controller) offered choose-1 with "
                 f"{c1.get('n_candidates')} candidates (expected {N_REVEAL} "
                 f"from P1's hand): [{'; '.join(c1.get('candidates', []))}]")
    elif cast_ok and post is not None:
        a["A6_chooser"] = "failed"
        n.append("A6 chooser: FAILED -- resolution completed with no "
                 "choose-1 prompt for the controller.")
    else:
        a["A6_chooser"] = "not-run"
        n.append("A6 chooser: not-run (resolution not reached)")

    # ---- A7 outcome (exactly the picked card discarded) ----
    from collections import Counter
    if pre is not None and post is not None and cast_ok and c1 is not None:
        picked = g.choose1_picked
        picked_l = picked.lower()
        pre_c = Counter(obj_lname(pre, o) for o in hand_ids(pre, 1))
        post_c = Counter(obj_lname(post, o) for o in hand_ids(post, 1))
        diff = pre_c - post_c
        left_names = sorted(diff.elements())
        gy_names = [obj_lname(post, o)
                    for o in player_of(post, 1).get("graveyard", [])]
        lib_pre = len(player_of(pre, 1).get("library", []) or [])
        lib_post = len(player_of(post, 1).get("library", []) or [])
        toll_gy = in_gy(post, 0, MIRES_TOLL)
        ok = (left_names == [picked_l] and picked_l in gy_names
              and lib_pre == lib_post and toll_gy)
        a["A7_outcome"] = "passed" if ok else "failed"
        n.append(f"A7 outcome: {'passed' if ok else 'FAILED'} -- P1 hand lost "
                 f"{left_names} (picked='{picked}'); in P1 gy: "
                 f"{picked_l in gy_names}; library {lib_pre}->{lib_post}; "
                 f"mires_toll in P0 gy: {toll_gy}")
    elif pre is not None and post is not None and cast_ok:
        a["A7_outcome"] = "not-run"
        n.append("A7 outcome: not-run (no choose-1 to compare against)")
    else:
        a["A7_outcome"] = "not-run"
        n.append("A7 outcome: not-run (states missing)")

    # ---- A8 cleanup ----
    if post is not None:
        empty = len(post.get("stack") or []) == 0
        a["A8_cleanup"] = "passed" if empty else "failed"
        n.append(f"A8 cleanup: stack empty={empty}")
    else:
        a["A8_cleanup"] = "not-run"
        n.append("A8 cleanup: not-run")
    return a, n


def evaluate(g, parse_sum):
    ass = {}
    notes = []

    # ---- A2 parse documentation (from PINNED v0.108.0 card-data) ----
    pe = parse_sum["mires_toll"]
    ok = (pe["effect_type"] == "RevealHand"
          and pe["sub_effect_type"] == "ChooseFromZone"
          and pe["sub_effect_count"] == 1)
    ass["A2_parse_mires_toll"] = "passed" if ok else "failed"
    notes.append(
        f"A2 parse Mire's Toll (pinned v0.108.0): ability_kind="
        f"{pe['ability_kind']}, effect={pe['effect_type']} "
        f"(target={json.dumps(pe['effect_target'])[:90]}), "
        f"sub_effect={pe['sub_effect_type']} "
        f"count={pe['sub_effect_count']}: "
        f"{'choose clause modelled' if ok else 'PARSE SHAPE UNEXPECTED'}")
    q = parse_sum["thought_rattle"]
    ass["A2_parse_thought_rattle"] = \
        "passed" if q["effect_type"] == "RevealHand" else "failed"
    notes.append(
        f"A2 parse Thought Rattle (not driven): effect={q['effect_type']} "
        f"(target={json.dumps(q['effect_target'])[:90]}, "
        f"filter={json.dumps(q['effect_card_filter'])[:60]})")

    a, n = eval_game(g)
    ass.update(a)
    notes.extend(n)

    # ---- verdict ----
    a1 = a.get("A1_setup") == "passed"
    a3 = a.get("A3_cast")
    a5 = a.get("A5_no_redundant_ack")
    if not a1:
        verdict = "blocked"
        notes.append("setup never completed; verdict blocked")
    elif a3 != "passed":
        verdict = "blocked"
        notes.append("cast never reached the stack targeted at P1; the "
                     "reported prompt sequence is untestable -- blocked")
    elif a5 == "failed":
        verdict = "reproduced"
        notes.append("reproduced: the choosing player (P0) was offered a "
                     "RevealChoice acknowledgement during resolution -- "
                     "two prompts for one decision, exactly the reported "
                     "UX defect")
    elif a5 == "passed":
        flow_ok = all(a.get(k) == "passed"
                      for k in ("A4_reveal", "A6_chooser", "A7_outcome"))
        if flow_ok:
            verdict = "not-reproduced"
            notes.append("not-reproduced: single ChooseFromZoneChoice prompt "
                         "for the controller, no redundant reveal ack, and "
                         "the outcome was correct on v0.108.0")
        else:
            verdict = "reproduced"
            notes.append("reproduced (related failure): no redundant ack, "
                         "but the reveal/choose/discard flow itself is "
                         "broken -- see A4/A6/A7")
    else:
        verdict = "blocked"
        notes.append("resolution prompts never materialized; verdict blocked")
    notes.extend(g.notes)
    return ass, notes, verdict
# ---------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #9841 - Mire's Toll redundant reveal ack "
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
        ("A1_setup", "A1 setup: Mire's Toll in P0 hand, 2 Swamps, P1 hand >= 2"),
        ("A2_parse_mires_toll", "A2 parse: Mire's Toll RevealHand + ChooseFromZone modelled"),
        ("A2_parse_thought_rattle", "A2 parse: Thought Rattle RevealHand (nonland filter)"),
        ("A3_cast", "A3 cast: Mire's Toll reached the stack targeting P1"),
        ("A4_reveal", "A4 P1 revealed 2 cards (= Swamps P0 controls)"),
        ("A5_no_redundant_ack", "A5 [the bug] NO reveal ack before choose (FAIL = reproduced)"),
        ("A6_chooser", "A6 controller (P0) chose 1 from exactly the 2 revealed"),
        ("A7_outcome", "A7 outcome: exactly the picked card discarded"),
        ("A8_cleanup", "A8 cleanup: stack empty post-resolution"),
    ]
    for k, lab in labels:
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col); y += 22
    y += 4
    obs = run["observations"]
    d.text((24, y), "Prompt sequence (choosing player P0):",
           fill=(200, 210, 225)); y += 22
    for p in obs.get("p0_prompt_sequence", [])[:4]:
        d.text((40, y), (f"#{p['seq']} kind={p['kind_code']!r} "
                         f"n_cand={p['n_candidates']} "
                         f"ack_like={p['ack_like']} "
                         f"[{' ; '.join(p['candidates'][:4])}]")[:118],
               fill=(150, 165, 185)); y += 19
    if not obs.get("p0_prompt_sequence"):
        d.text((40, y), "(no resolution prompts recorded)",
               fill=(255, 150, 90)); y += 19
    y += 4
    d.text((24, y), "Pre/post hand/library (from saved authoritative states):",
           fill=(200, 210, 225)); y += 22
    pre = obs["snaps"].get("pre")
    post = obs["snaps"].get("post")
    if pre and post:
        d.text((40, y),
               f"P0 hand/lib {pre['P0']} -> {post['P0']}; "
               f"P1 hand/lib {pre['P1']} -> {post['P1']}",
               fill=(150, 165, 185)); y += 22
    else:
        d.text((40, y), "states missing", fill=(255, 150, 90)); y += 22
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9841/" + run["run_id"],
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
    say("parse summary (mires_toll): "
        + json.dumps(parse_sum["mires_toll"], default=str)[:1200])

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
    p0_seq = [{"seq": i + 1, **p} for i, p in enumerate(g.p0_prompts)]
    p1_seq = [{"seq": i + 1, **p} for i, p in enumerate(g.p1_prompts)]
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/run-5654-reval-v01080-20261012-2341",
        "parse_summary": parse_sum,
        "driver": {"protocol_advertised": 134, "client": "driver/client.py",
                   "scenario": "driver/scenario_9841_01080.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_9841_01080.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [["mire's toll", 4], ["swamp", 56]],
            "P1": [["lightning bolt", 8], ["divination", 4], ["island", 48]],
        },
        "games": {"mires_toll": {"ok": ok, "notes": g.notes}},
        "observations": {
            "snaps": g.snaps,
            "target_answered": g.target_answered,
            "target_chosen_seat": g.target_chosen_seat,
            "swamps_at_cast": g.swamps_at_cast,
            "p0_prompt_sequence": p0_seq,
            "p1_prompt_sequence": p1_seq,
            "reveal_acked": g.reveal_acked,
            "p1_reveal_choice": g.p1_reveal_choice,
            "choose1": g.choose1,
            "choose1_picked": g.choose1_picked,
            "choose1_first_was_choose": g.choose1_first_was_choose,
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "4x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Thought Rattle was not driven (its RevealHand parse shape was "
            "documented from the pinned card data; threshold/seek clauses "
            "untested).",
            "Not tested on the original build; verdict is scoped to v0.108.0, "
            "not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 4x mire's toll + 56 swamp vs P1 8x lightning bolt + "
                      "4x divination + 48 island (P0 casts Mire's Toll "
                      "targeting P1 on turn 2 with 2 Swamps).",
        "contract_line": "Per the issue's Expected: target reveals 2 cards "
                         "(= Swamps controlled); the controller gets ONE "
                         "selection prompt (ChooseFromZoneChoice) over the "
                         "revealed cards; the target discards the chosen "
                         "card. The reported bug is a redundant RevealChoice "
                         "ack before the choose prompt.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_9841_01080.py",
                f"{EVDIR}/scenario_9841_01080.py")
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
    pidfile = "/tmp/scenario_9841_01080.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_9841_01080 instance is alive "
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
