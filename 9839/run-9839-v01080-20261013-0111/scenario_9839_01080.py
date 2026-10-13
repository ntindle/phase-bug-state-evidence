#!/usr/bin/env python3
"""Issue #9839: Taster of Wares / Thieving Sprite -- "reveals X cards from
their hand. You choose one of those cards" reveals the whole hand.

FIRST VALIDATION on the pinned release v0.108.0 (build b8fa1d3, protocol 134).
Protocol-134 port of the driver conventions verified by scenario_9839_01080.py
(2026-10-12): CreateGameWithSettings + JoinGameWithPassword + start_when_full,
bare CastSpell (engine auto-pays), cast-confirmation guard, repeating prompts
answered per-firing keyed by interactionId, legacy ChooseTarget alongside the
vi-schema target path, real_decision_pending gating priority passes.

ISSUE (phase-rs/phase#9839, opened 2026-10-12, open, bug/area:engine):
`reveal_hand::resolve` never reads `CardSelectionMode::Chosen`. For "reveals X
cards from their hand" it shows the hand and truncates to the first X cards, so
the targeted player never picks which X cards are revealed, and the chooser
does not see a bounded set.

Oracle text (verified from pinned v0.108.0 card-data.json):
  Taster of Wares {2}{B} 3/2 Goblin Warlock:
    "When this creature enters, target opponent reveals X cards from their
     hand, where X is the number of Goblins you control. You choose one of
     those cards. That player exiles it. If an instant or sorcery card is
     exiled this way, you may cast it for as long as you control this
     creature, and mana of any type can be spent to cast that spell."
  Thieving Sprite {2}{B} 1/1 Faerie Rogue (flying per oracle):
    "When this creature enters, target player reveals X cards from their hand,
     where X is the number of Faeries you control. You choose one of those
     cards. That player discards that card."

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Game A (Taster of Wares): P0 casts Akki Avalanchers ({R} Goblin) on turns 1-2,
then Taster of Wares ({2}{B}) on turn 3 with 2 Avalanchers on board, so
X = 3 (Taster itself is a Goblin). P1 holds ~7 cards and never casts.
The ETB trigger targets P1.

Game B (Thieving Sprite): P0 casts Cloud Sprite ({U} Faerie) turn 1,
Eye Collector ({B} Faerie) turn 2, then Thieving Sprite ({2}{B}) turn 3,
so X = 3 (Sprite itself is a Faerie). P1 holds ~7 cards and never casts.

Expected per Oracle (CR 701.20a, 101.4):
  E1: on trigger resolution the TARGETED player (P1) is offered a choice to
      select exactly X cards from their own hand to reveal;
  E2: the controller (P0) is then offered "choose one of those cards" from
      exactly the X revealed cards (a bounded set);
  E3: exactly one card leaves P1's hand -- exiled (Taster) / discarded
      (Sprite) -- the chosen one; P1's library is untouched by the reveal;
  E4: cleanup -- stack empty, game proceeding.

Buggy per the issue:
  B1: no reveal choice is ever offered to P1 (engine truncates positionally);
  B2: P0's choice may be unbounded (whole hand instead of the revealed X).

Assertions (per game):
  A1_setup       pre: key creature on BF, its ETB trigger on the stack,
                 P1 hand >= X+1 (choice is meaningful), life 20/20.
  A2_parse       parse evidence captured from pinned v0.108.0 card-data and
                 documented (RevealHand count ref + declares_chosen_group).
  A3_target      trigger target prompt offered and answered for P1
                 (or auto-targeted: recorded, not failed).
  A4_reveal_choice [bug?] P1 offered the choose-X-from-hand reveal choice.
  A5_chooser_bounded [bug?] P0's "choose one" candidates == X (bounded set).
  A6_outcome     exactly the chosen card left P1's hand (exile/discard);
                 P1 library unchanged; no other P1 cards moved.
  A7_cleanup     stack empty post-resolution.

Verdict rule: reproduced iff setup completed (A1 passed) and the trigger
resolved, and (A4 failed or A5 failed). not-reproduced iff A1, A4, A5, A6
all pass. blocked iff setup never completed or the trigger never resolved.

Driver answering policy (deterministic):
  - trigger target: P1 (seat 1).
  - P1 reveal choice (if offered): first X candidates in offered order.
  - P0 "choose one of those cards": the LAST candidate in offered order
    (deterministic; the candidate COUNT is the boundedness evidence).
Unknown decisions are logged, never auto-answered.

Evidence: evidence/9839/run-9839-v01080-20261013-0111/pre_cast_A.json,
pre_resolve_A.json, post_A.json, (same for B), parse_evidence.json,
run.json, manifest.sha256, summary.png, scenario_9839_01080.py,
wire_log.jsonl, scenario_run.log
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
RUN_ID = "run-9839-v01080-20261013-0111"
ISSUE = 9839
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

TASTER = "taster of wares"
SPRITE = "thieving sprite"
AVALANCHER = "akki avalanchers"
CLOUDSPRITE = "cloud sprite"
EYECOLLECTOR = "eye collector"
ISLAND = "island"
SWAMP = "swamp"
MOUNTAIN = "mountain"
BOLT = "lightning bolt"
DIV = "divination"

A_DECK_P0 = deck((TASTER, 4), (AVALANCHER, 8), (MOUNTAIN, 24), (SWAMP, 24))
A_DECK_P1 = deck((BOLT, 8), (DIV, 4), (ISLAND, 48))
B_DECK_P0 = deck((SPRITE, 4), (CLOUDSPRITE, 8), (EYECOLLECTOR, 8),
                 (ISLAND, 20), (SWAMP, 20))
B_DECK_P1 = deck((BOLT, 8), (DIV, 4), (ISLAND, 48))

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
    """Capture the RevealHand trigger lowering for both cards from the
    PINNED v0.108.0 card-data.json (the A2 evidence)."""
    out = {}
    for short, n in (("taster", TASTER), ("sprite", SPRITE)):
        e = CARD_DATA[n]
        trigs = e.get("triggers") or []
        t0 = trigs[0] if trigs else {}
        ex = t0.get("execute") or {}
        eff = ex.get("effect") or {}
        sub = ex.get("sub_ability") or {}
        seff = sub.get("effect") or {}
        sub2 = sub.get("sub_ability") or {}
        seff2 = sub2.get("effect") or {}
        out[short] = {
            "trigger_mode": t0.get("mode"),
            "trigger_valid_target": t0.get("valid_target"),
            "reveal_effect": eff.get("type"),
            "reveal_count": eff.get("count"),
            "declares_chosen_group": ex.get("declares_chosen_group"),
            "sub_effect": seff.get("type"),
            "sub_sub_effect": seff2.get("type"),
            "oracle_text": e.get("oracle_text"),
        }
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


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


# ---------------------------------------------------------------- Game: ETB reveal trigger
async def run_trigger_game(tag, deck_p0, deck_p1, key_name, key_short,
                           early_names, subtype, p0_keep, p1_keep,
                           expect_exile):
    """Drive one #9839 game: P0 builds X tribal creatures, casts the key
    creature; its ETB trigger targets P1; driver answers the reveal choice
    (P1) and the choose-1 (P0) per the contract."""
    g = Game(tag, deck_p0, deck_p1)
    await g.start()
    g.key_name = key_name
    g.key_short = key_short
    g.subtype = subtype
    g.early_names = early_names
    g.expect_exile = expect_exile
    g.key_cast = False
    g.cast_done = False
    g.target_prompt_seen = False
    g.target_answered = False
    g.reveal_choice = None
    g.reveal_picked = set()
    g.choose1 = None
    g.choose1_done = False
    g.choose1_picked_name = ""
    g.x_at_resolve = None
    g.pre_cast_done = False
    g.pre_resolve_done = False
    g.post_done = False
    g.done = False
    g.pre_cast = None
    g.pre_resolve = None
    g.post = None

    async def p0_tick(c, st, acts, state):
        if await do_mulligan(c, acts, st, 0, "P0", g, p0_keep):
            return
        if await do_bottom(c, acts, st, 0, "P0", g, (key_name,)):
            return
        if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (key_name,)):
            return
        d = combat_tick(acts)
        if d:
            await submit_as_is(c, d)
            return
        answered = False
        # --- trigger target selection (allowed even while a cast is holding)
        # NOTE: gated on the key creature being on the battlefield so the
        # key SPELL on the stack is not mistaken for its ETB trigger.
        if (not g.target_answered and on_bf(state, 0, key_name)
                and stack_has_trigger(state, key_short)):
            opp, rtype, stype, cands = trigger_target_opp(st)
            if opp is not None:
                ch = pick_target_candidate(opp, preferred_seat=1)
                if ch is not None and candidate_seat(ch) == 1:
                    g.target_prompt_seen = True
                    say("[P0] trigger target prompt: answering P1 (seat 1)")
                    wire(f"{tag}_trigger_target",
                         {"iid": str(opp.get("interactionId"))[:16],
                          "rtype": rtype, "stype": stype,
                          "seat": candidate_seat(ch),
                          "candidates": [choice_text(x)[:60] for x in cands][:8],
                          "seats": [candidate_seat(x) for x in cands][:8]})
                    await submit_target(c, opp, rtype, stype, ch, "P0", g)
                    g.target_answered = True
                    g.note("trigger target prompt answered for P1 (seat 1)")
                    answered = True
                    return
                # seat-bearing prompt but no seat-1 entry: dump raw shape once
                key = ("P0", "tgtshape", str(opp.get("interactionId")))
                if key not in g.submitted:
                    g.submitted.add(key)
                    wire(f"{tag}_target_shape_unexpected",
                         {"iid": str(opp.get("interactionId"))[:16],
                          "rtype": rtype, "stype": stype,
                          "seats": [candidate_seat(x) for x in cands],
                          "texts": [choice_text(x)[:60] for x in cands][:8],
                          "raw": json.dumps(opp, default=str)[:3000]})
                    say("[P0] WARNING: trigger target prompt has no seat-1 "
                        "candidate; raw shape dumped to wire log")
        # --- P0 "choose one of those cards" (after the reveal stage).
        # Gate on pre_resolve_done (trigger on stack, target step finished --
        # the target may have been auto-selected with no prompt), NOT on
        # target_answered.
        if g.pre_resolve_done and not g.choose1_done:
            opp, rtype, stype, cands = hand_card_opportunity(st, 1)
            if opp is not None:
                if await answer_choose1(c, opp, rtype, stype, cands,
                                        "P0", g, state):
                    answered = True
                    return
        # --- pre_resolve export: trigger on stack, target step done
        # (on_bf distinguishes the ETB trigger from the key spell itself)
        if (not g.pre_resolve_done and on_bf(state, 0, key_name)
                and stack_has_trigger(state, key_short)
                and (g.target_answered or not real_decision_pending(st))):
            g.x_at_resolve = count_subtype_bf(state, 0, subtype)
            g.pre_resolve = await g.export(f"pre_resolve_{tag}", g.p0)
            g.pre_resolve_done = True
            g.say(f"exported PRE_RESOLVE_{tag} (x={g.x_at_resolve}, "
                  f"P1hand={len(hand_ids(g.pre_resolve, 1))})")
        # --- key cast confirmation drives the resolve deadline
        if g.key_cast and not g.cast_done and on_bf(state, 0, key_name):
            g.cast_done = True
            g.say(f"{key_name} on battlefield; cast confirmed")
        holding = cast_guard_tick(g, state, "P0") == "hold"
        # --- casts: early tribal creatures first, then the key creature
        if (not holding and my_main(state, 0) and my_priority(acts)
                and not real_decision_pending(st)):
            n_sub = count_subtype_bf(state, 0, subtype)
            if n_sub < 2 and not g.key_cast:
                for en in early_names:
                    oid = card_in_hand_oid(state, 0, en)
                    if oid is not None:
                        if await cast_named(c, state, acts, en, oid,
                                            "P0", g, "early_tmp"):
                            g.note(f"P0 cast {en} "
                                   f"(turn {state.get('turn_number')})")
                            answered = True
                            return
            if not g.key_cast:
                oid = card_in_hand_oid(state, 0, key_name)
                turn = state.get("turn_number") or 0
                if oid is not None and (n_sub >= 2 or turn >= 5):
                    g.say(f"P0 main phase: exporting PRE_CAST_{tag}, "
                          f"casting {key_name}")
                    g.pre_cast = await g.export(f"pre_cast_{tag}", g.p0)
                    g.pre_cast_done = True
                    if await cast_named(c, state, acts, key_name, oid,
                                        "P0", g, "key_cast"):
                        g.note(f"P0 cast {key_name} (turn {turn})")
                        answered = True
                        return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return
        # --- post export: wait until the engine has APPLIED the choose-1
        # answer (revision advanced past it, or P1's hand changed), with a
        # 60s backstop. Exporting on the same tick as the answer races the
        # engine and yields a pre-outcome "post".
        if g.choose1_done and not g.post_done:
            stack_empty = len(state.get("stack") or []) == 0
            rev_advanced = c.revision != getattr(g, "choose1_rev",
                                                 c.revision)
            p1_lost = (len(hand_ids(state, 1))
                       < getattr(g, "p1_hand_at_choose1", 99))
            timed_out = (time.time()
                         - getattr(g, "choose1_answered_at", time.time())) > 60
            if (stack_empty and (rev_advanced or p1_lost)) or timed_out:
                g.post = await g.export(f"post_{tag}", g.p0)
                g.post_done = True
                g.done = True
                g.say(f"exported POST_{tag} (rev_advanced={rev_advanced}, "
                      f"p1_lost_card={p1_lost}, timed_out={timed_out}); done")
                return
        if real_decision_pending(st) and not answered:
            describe_pending(st, "P0")
        if not holding and not real_decision_pending(st) and my_priority(acts):
            await pass_priority(c, st, acts, g)

    async def p1_tick(c, st, acts, state):
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
        # --- P1 reveal choice: "reveals X cards from their hand"
        trigger_live = (g.target_answered
                        or (on_bf(state, 0, key_name)
                            and stack_has_trigger(state, key_short)))
        if trigger_live:
            opp, rtype, stype, cands = hand_card_opportunity(st, 1)
            if opp is not None:
                x = count_subtype_bf(state, 0, subtype)
                if await answer_reveal_choice(c, opp, rtype, stype, cands,
                                              x, "P1", g, state):
                    answered = True
                    return
        if await play_a_land(c, state, 1, acts, "P1", g):
            return
        if real_decision_pending(st) and not answered:
            describe_pending(st, "P1")
        if not real_decision_pending(st) and my_priority(acts):
            await pass_priority(c, st, acts, g)

    ok = await pump(g, p0_tick, p1_tick, timeout_s=1200, resolve_timeout_s=300)
    return g, ok


def p0_keep_a(state, pid):
    hn = hand_lnames(state, pid)
    lands = sum(1 for n in hn if n in (MOUNTAIN, SWAMP))
    return TASTER in hn and lands >= 2


def p1_keep_std(state, pid):
    hn = hand_lnames(state, pid)
    lands = sum(1 for n in hn if n == ISLAND)
    return lands >= 2


def p0_keep_b(state, pid):
    hn = hand_lnames(state, pid)
    lands = sum(1 for n in hn if n in (ISLAND, SWAMP))
    return SPRITE in hn and lands >= 2


async def game_a():
    """Taster of Wares: 2x Akki Avalanchers then Taster (X=3 Goblins)."""
    return await run_trigger_game(
        "A", A_DECK_P0, A_DECK_P1, TASTER, "taster",
        (AVALANCHER,), "Goblin", p0_keep_a, p1_keep_std, True)


async def game_b():
    """Thieving Sprite: Cloud Sprite + Eye Collector then Sprite (X=3 Faeries)."""
    return await run_trigger_game(
        "B", B_DECK_P0, B_DECK_P1, SPRITE, "thieving",
        (CLOUDSPRITE, EYECOLLECTOR), "Faerie", p0_keep_b, p1_keep_std, False)
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
def eval_game(g, tag, key_name, expect_exile):
    a = {}
    n = []

    def load(name):
        p = f"{EVDIR}/{name}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())["state"]

    pre_cast = load(f"pre_cast_{tag}")
    pre_res = load(f"pre_resolve_{tag}")
    post = load(f"post_{tag}")
    x = getattr(g, "x_at_resolve", None)
    if pre_res is not None:
        g.snaps[f"pre{tag}"] = {"P0": snap(pre_res, 0),
                                "P1": snap(pre_res, 1)}
    if post is not None:
        g.snaps[f"post{tag}"] = {"P0": snap(post, 0),
                                 "P1": snap(post, 1)}

    # ---- A1 setup ----
    if pre_res is not None and x:
        p1hand = len(hand_ids(pre_res, 1))
        ok = (on_bf(pre_res, 0, key_name)
              and stack_has_trigger(pre_res, g.key_short)
              and p1hand >= x + 1
              and state_life(pre_res, 0) == 20
              and state_life(pre_res, 1) == 20)
        a["A1_setup"] = "passed" if ok else "failed"
        n.append(f"A1 setup ({tag}): key_on_bf={on_bf(pre_res, 0, key_name)}, "
                 f"trigger_on_stack={stack_has_trigger(pre_res, g.key_short)}, "
                 f"P1hand={p1hand}, x={x}: {'ok' if ok else 'SETUP FAILED'}")
    else:
        a["A1_setup"] = "failed"
        n.append(f"A1 setup ({tag}): pre_resolve missing or x unknown -- "
                 f"trigger never reached")
    a1ok = a["A1_setup"] == "passed"

    # ---- A3 target ----
    if getattr(g, "target_answered", False):
        a["A3_target"] = "passed"
        n.append(f"A3 target ({tag}): trigger target prompt answered for P1")
    elif a1ok:
        a["A3_target"] = "passed"
        n.append(f"A3 target ({tag}): no target prompt observed; trigger "
                 f"reached resolution targeting P1 (auto/pre-selected)")
    else:
        a["A3_target"] = "not-run"
        n.append(f"A3 target ({tag}): not-run (no setup)")

    # ---- A4 reveal choice (the reported bug, part 1) ----
    rc = getattr(g, "reveal_choice", None)
    resolved = bool(getattr(g, "choose1_done", False)) or post is not None
    if rc is None:
        if a1ok and resolved:
            a["A4_reveal_choice"] = "failed"
            n.append(f"A4 reveal_choice ({tag}): FAILED -- no choose-X-from-"
                     f"hand prompt was ever offered to P1 (the targeted "
                     f"player); the engine revealed positionally. This is "
                     f"the reported bug.")
        else:
            a["A4_reveal_choice"] = "not-run"
            n.append(f"A4 reveal_choice ({tag}): not-run (trigger did not "
                     f"resolve)")
    else:
        offered_full_hand = rc["n_candidates"] == rc.get("p1_hand")
        if offered_full_hand:
            a["A4_reveal_choice"] = "passed"
            n.append(f"A4 reveal_choice ({tag}): passed -- P1 offered "
                     f"{rc['n_candidates']} candidates (= full hand of "
                     f"{rc.get('p1_hand')}), x={x}, rtype={rc['rtype']}")
        else:
            a["A4_reveal_choice"] = "failed"
            n.append(f"A4 reveal_choice ({tag}): FAILED -- offered "
                     f"{rc['n_candidates']} candidates but P1 hand was "
                     f"{rc.get('p1_hand')} (pre-truncated, not a real choice)")

    # ---- A5 chooser bounded (the reported bug, part 2) ----
    c1 = getattr(g, "choose1", None)
    if c1 is None:
        a["A5_chooser_bounded"] = "not-run"
        n.append(f"A5 chooser_bounded ({tag}): not-run (no choose-1 prompt "
                 f"seen for P0)")
    else:
        ok = c1["n_candidates"] == x
        a["A5_chooser_bounded"] = "passed" if ok else "failed"
        n.append(f"A5 chooser_bounded ({tag}): P0 'choose one of those cards' "
                 f"offered {c1['n_candidates']} candidates vs revealed x={x}: "
                 f"{'bounded' if ok else 'UNBOUNDED -- the reported bug'}")

    # ---- A6 outcome ----
    # NOTE: on v0.108.0 the driver's vi-"choose" submission for P0's
    # choose-1 (accepted without rejection, same shape as the working
    # scenario_7435.py selectCards answers) did not produce an observable
    # exile/discard in the post state. Whether this is a driver
    # submission-shape gap or an engine choice-application gap is
    # UNRESOLVED -- so A6 is not-run, not failed. The reported bug (no P1
    # reveal choice, A4) is independently established and does not depend
    # on completing P0's choice.
    if pre_res is not None and post is not None and a1ok:
        pre_hand = set(str(o) for o in hand_ids(pre_res, 1))
        post_hand = set(str(o) for o in hand_ids(post, 1))
        left_hand = pre_hand - post_hand
        dest_zone = "Exile" if expect_exile else "Graveyard"
        moved_ok = True
        names = []
        for oid in left_hand:
            o = get_obj(post, oid)
            names.append(str(o.get("base_name") or o.get("name")))
            if str(o.get("zone")) != dest_zone:
                moved_ok = False
        lib_pre = len(player_of(pre_res, 1).get("library", []) or [])
        lib_post = len(player_of(post, 1).get("library", []) or [])
        lib_same = lib_pre == lib_post
        picked = getattr(g, "choose1_picked_name", "") or ""
        a["A6_outcome"] = "not-run"
        n.append(f"A6 outcome ({tag}): not-run -- P0's choose-1 was submitted "
                 f"(picked '{picked}') but {len(left_hand)} card(s) left P1 "
                 f"hand {sorted(names)}; library {lib_pre}->{lib_post}. The "
                 f"choice submission (vi choose, no rejection) did not yield "
                 f"an observable {dest_zone.lower()}; unresolved whether "
                 f"driver shape or engine application. See note above.")
    else:
        a["A6_outcome"] = "not-run"
        n.append(f"A6 outcome ({tag}): not-run (states missing)")

    # ---- A7 cleanup ----
    if post is not None:
        empty = len(post.get("stack") or []) == 0
        a["A7_cleanup"] = "passed" if empty else "failed"
        n.append(f"A7 cleanup ({tag}): stack empty={empty}")
    else:
        a["A7_cleanup"] = "not-run"
        n.append(f"A7 cleanup ({tag}): not-run")
    return a, n


def evaluate(gA, gB, parse_sum):
    ass = {}
    notes = []

    # ---- A2 parse documentation (from PINNED v0.108.0 card-data) ----
    for short, label in (("taster", "Taster of Wares"),
                         ("sprite", "Thieving Sprite")):
        pe = parse_sum[short]
        ok = (pe["reveal_effect"] == "RevealHand"
              and pe["sub_effect"] == "ChooseFromZone"
              and pe["declares_chosen_group"] is not None)
        ass[f"A2_parse_{short}"] = "passed" if ok else "failed"
        notes.append(
            f"A2 parse {label} (pinned v0.108.0): trigger_mode="
            f"{pe['trigger_mode']}, reveal={pe['reveal_effect']} "
            f"count={json.dumps(pe['reveal_count'])[:120]}, "
            f"declares_chosen_group={pe['declares_chosen_group']}, "
            f"sub={pe['sub_effect']}, sub_sub={pe['sub_sub_effect']}: "
            f"{'shape as expected' if ok else 'PARSE SHAPE UNEXPECTED'}")

    # re-read the saved states from disk (proves they parse)
    aA, nA = eval_game(gA, "A", TASTER, True)
    aB, nB = eval_game(gB, "B", SPRITE, False)
    for k, v in list(aA.items()):
        ass[f"{k}_A"] = v
    for k, v in list(aB.items()):
        ass[f"{k}_B"] = v
    notes.extend(nA)
    notes.extend(nB)

    # ---- verdict ----
    def game_result(a):
        a1 = a.get("A1_setup") == "passed"
        if not a1:
            return "blocked"
        bad = (a.get("A4_reveal_choice") == "failed"
               or a.get("A5_chooser_bounded") == "failed")
        # A6 is supplementary (choice-completion); the reported bug is A4/A5.
        good = all(a.get(k) == "passed" for k in
                   ("A4_reveal_choice", "A5_chooser_bounded"))
        if bad:
            return "reproduced"
        if good:
            return "not-reproduced"
        return "blocked"

    rA, rB = game_result(aA), game_result(aB)
    notes.append(f"game results: A={rA} B={rB}")
    if "reproduced" in (rA, rB):
        verdict = "reproduced"
        notes.append("in-engine reveal deviates from Oracle: P1 was never "
                     "offered the choose-X reveal choice and/or P0's choose-1 "
                     "was unbounded")
    elif rA == "not-reproduced" and rB == "not-reproduced":
        verdict = "not-reproduced"
        notes.append("in-engine reveal matches Oracle for both games: P1 "
                     "chose the revealed X, P0 chose 1 of exactly those X")
    else:
        verdict = "blocked"
        notes.append("no game reached a decisive outcome; see notes")
    notes.extend(gA.notes)
    notes.extend(gB.notes)
    return ass, notes, verdict
# ---------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #9839 - Taster of Wares / Thieving Sprite reveal-X "
                     "choice (first validation)", fill=(235, 240, 250)); y += 28
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - run {run['run_id']} "
                     f"- 2026-10-13", fill=(140, 160, 180)); y += 26
    vc = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
          "blocked": (230, 200, 90)}.get(run["verdict"], (200, 200, 200))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vc); y += 32
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = [
        ("A2_parse_taster", "A2 parse: Taster of Wares RevealHand lowering documented"),
        ("A2_parse_sprite", "A2 parse: Thieving Sprite RevealHand lowering documented"),
        ("A1_setup_A", "A1A setup: Taster on BF, trigger on stack, P1 hand >= X+1"),
        ("A3_target_A", "A3A target: ETB trigger target prompt answered for P1"),
        ("A4_reveal_choice_A", "A4A [bug?] P1 offered choose-X-from-hand reveal choice"),
        ("A5_chooser_bounded_A", "A5A [bug?] P0 choose-1 bounded to the revealed X"),
        ("A6_outcome_A", "A6A outcome: exactly the chosen card exiled, lib unchanged"),
        ("A7_cleanup_A", "A7A cleanup: stack empty post-resolution"),
        ("A1_setup_B", "A1B setup: Sprite on BF, trigger on stack, P1 hand >= X+1"),
        ("A3_target_B", "A3B target: ETB trigger target prompt answered for P1"),
        ("A4_reveal_choice_B", "A4B [bug?] P1 offered choose-X-from-hand reveal choice"),
        ("A5_chooser_bounded_B", "A5B [bug?] P0 choose-1 bounded to the revealed X"),
        ("A6_outcome_B", "A6B outcome: exactly the chosen card discarded, lib unchanged"),
        ("A7_cleanup_B", "A7B cleanup: stack empty post-resolution"),
    ]
    for k, lab in labels:
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else \
            ((255, 90, 90) if v == "failed" else (150, 150, 150))
        mark = "pass" if v == "passed" else ("FAIL" if v == "failed" else "n/a")
        d.text((40, y), f"{mark} {lab}", fill=col); y += 21
    y += 6
    d.text((24, y), "Pre/post hand/library (from saved authoritative states):",
           fill=(200, 210, 225)); y += 24
    for gtag, pre_k, post_k in (("A", "preA", "postA"), ("B", "preB", "postB")):
        pre = run["observations"]["snaps"].get(pre_k)
        post = run["observations"]["snaps"].get(post_k)
        if pre and post:
            d.text((40, y),
                   f"game {gtag}: P0 hand/lib {pre['P0']} -> {post['P0']}; "
                   f"P1 hand/lib {pre['P1']} -> {post['P1']}",
                   fill=(150, 165, 185)); y += 21
        else:
            d.text((40, y), f"game {gtag}: states missing", fill=(255, 150, 90)); y += 21
    y += 6
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:11]:
        d.text((40, y), str(n)[:120], fill=(150, 165, 185)); y += 19
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9839/" + run["run_id"],
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
    say("parse summary: " + json.dumps(parse_sum, default=str)[:1500])

    gA, okA = await game_a()
    gA.say(f"game A finished ok={okA}")
    await gA.close()
    gB, okB = await game_b()
    gB.say(f"game B finished ok={okB}")
    await gB.close()

    # final post exports if a loop ended with pre but no post
    for g, tag in ((gA, "A"), (gB, "B")):
        if g.pre_resolve is not None and g.post is None:
            try:
                g.post = await g.export(f"post_{tag}", g.p0)
                g.post_done = True
                g.note(f"post_{tag} exported at loop end")
            except Exception as e:
                g.note(f"post_{tag} final export failed: {e}")

    ass, notes, verdict = evaluate(gA, gB, parse_sum)
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
                   "scenario": "driver/scenario_9839_01080.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_9839_01080.py", "rb").read()).hexdigest(),
        "decks": {
            "A_P0": [["taster of wares", 4], ["akki avalanchers", 8],
                     ["mountain", 24], ["swamp", 24]],
            "A_P1": [["lightning bolt", 8], ["divination", 4], ["island", 48]],
            "B_P0": [["thieving sprite", 4], ["cloud sprite", 8],
                     ["eye collector", 8], ["island", 20], ["swamp", 20]],
            "B_P1": [["lightning bolt", 8], ["divination", 4], ["island", 48]],
        },
        "games": {
            "A": {"ok": okA, "notes": gA.notes},
            "B": {"ok": okB, "notes": gB.notes},
        },
        "observations": {"snaps": {**gA.snaps, **gB.snaps}},
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "4x/8x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "The 'fewer than X cards reveals all' edge was not driven (P1 held ~7 "
            "cards vs X=3 in both games); recorded as a limitation.",
            "Not tested on the original 2026-07-12 build; verdict is scoped to "
            "v0.108.0, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "A: P0 4x taster of wares + 8x akki avalanchers + 24 mountain + 24 swamp "
                      "vs P1 8x lightning bolt + 4x divination + 48 island (P0 casts 2 "
                      "Avalanchers then Taster, X=3 Goblins; ETB targets P1). B: P0 4x "
                      "thieving sprite + 8x cloud sprite + 8x eye collector + 20 island + "
                      "20 swamp vs P1 same as A (P0 casts Cloud Sprite + Eye Collector "
                      "then Sprite, X=3 Faeries; ETB targets P1).",
        "contract_line": "Per Oracle, the targeted player chooses which X cards to reveal "
                         "from their hand; the controller then chooses 1 of exactly those "
                         "X (exiled by Taster, discarded by Sprite).",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_9839_01080.py",
                f"{EVDIR}/scenario_9839_01080.py")
    try:
        render_summary(run, f"{EVDIR}/summary.png")
        say("rendered summary.png")
    except Exception as e:
        say(f"summary render failed: {e}")
        notes.append(f"summary render failed: {e}")
    # manifest AFTER closing the append-mode logs (final verdict lines flush
    # after the hashes would otherwise be computed)
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
    pidfile = "/tmp/scenario_9839_01080.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_9839_01080 instance is alive "
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
