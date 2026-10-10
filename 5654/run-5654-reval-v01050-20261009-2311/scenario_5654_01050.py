#!/usr/bin/env python3
"""Issue #5654: Notion Thief + Plagiarize -- compound "skips that draw and
you draw a card" substitute.

RE-VALIDATION on pinned v0.105.0 (2026-10-09, protocol 120). Prior runs:
  v0.78.0  20260909-5654 reproduced -- published, evidence 5654/20260909-5654
  v0.85.0  20260916-5654 reproduced -- published, evidence 5654/20260916-5654
  v0.102.0 20261004-5654 reproduced -- published, evidence 5654/20261004-5654
  (Notion Thief's static replacement worked on v0.102.0; Plagiarize never
  offered its target prompt and resolved with no replacement established)
  v0.104.0 20261008-5654 reproduced -- published, evidence 5654/20261008-5654

Ported from scenario_5654_01040.py (v0.104.0/protocol 118, verified
2026-10-08) to protocol 120: HELLO advertises 120; engine auto-pays
mana (bare CastSpell, no tapLandForMana/pay_mana bookkeeping);
cast-confirmation guard (30s backstop) before any priority pass;
repeating prompts answered per-firing keyed by interactionId; legacy
ChooseTarget target path added alongside the vi-schema path.

PARSER CHANGE vs the original contract (pinned v0.105.0 card-data.json,
verified this run before any game): Notion Thief's replacement now parses
as a plain execute=Draw{Fixed 1, Controller} with NO Unimplemented node and
no sub-ability (the old "Unimplemented skip + Draw sub" shape is GONE for
this card). Plagiarize still parses as execute=Unimplemented
("if target player would draw a card, instead that player skips that draw")
with sub_ability=Draw{Fixed 1, Controller} (gap persists). Hullbreacher
control still parses to Token. Per the playbook this disagreement is
recorded, not silently absorbed: the runtime outcome assertions decide the
verdict.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified from pinned v0.105.0 card-data.json):
  Notion Thief {2}{U}{B} 3/2 Flash creature:
    "If an opponent would draw a card except the first one they draw in each
     of their draw steps, instead that player skips that draw and you draw
     a card."
  Plagiarize {3}{U} instant:
    "Until end of turn, if target player would draw a card, instead that
     player skips that draw and you draw a card."
  Hullbreacher (control):
    "If an opponent would draw a card except the first one they draw in each
     of their draw steps, instead you create a Treasure token." -> Token.

Game A (Notion Thief): P0 casts Notion Thief, then P1 casts Divination
("Draw two cards.") during their own main phase. Both draws are non-first
draw-step draws, so per Oracle both are replaced: P1 skips (hand -1 for the
cast Divination itself, library unchanged), P0 draws 2 (hand +2, library -2).

Game B (Plagiarize): P0 casts Plagiarize targeting P1 during P1's Upkeep
(instant timing; must precede P1's draw-step draw since Plagiarize has no
first-draw exemption). P1's draw-step draw is then replaced: P1 hand/lib
unchanged, P0 draws 1 (hand net 0 = -1 cast +1 replaced draw, library -1).

Assertions (per game):
  A1_setup      pre: key permanent/spell in place, mana available, life 20/20.
  A2_parse      parse evidence captured from pinned v0.105.0 card-data and
                documented (noting the parser change vs the original contract
                for Notion Thief); Hullbreacher control parses to Token.
  A3_target     (B only) Plagiarize target prompt offered and answered for P1.
  A4_skip_holds draw events skipped: victim hand/library unchanged by them
                (beyond the spell cast itself).
  A5_ctrl_draws controller drew the replaced draws: P0 hand/library deltas.
  A6_cleanup    spell in graveyard, stack empty, game proceeding.

Verdict rule: reproduced iff setup completed for a game and the in-engine
draw replacement deviates from Oracle (A4 or A5 failed) -- e.g. inert (victim
draws normally) or half-applied (victim draws AND controller draws).
not-reproduced iff all outcome assertions pass for every game whose setup
completed (parse change documented as latent). blocked iff no setup completed.

Protocol-120 driver conventions (ported from scenario_5654_01040.py):
  - HELLO advertises protocol 120; engine auto-pays mana (bare CastSpell);
  - cast-confirmation guard with 30s backstop before any priority pass;
  - repeating prompts answered per-firing keyed by interactionId;
  - legacy ChooseTarget target path alongside the vi-schema target path.

Prior 118 conventions (ported from scenario_5653_01020.py /
scenario_658_01020.py / scenario_301_01020.py):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full (via
    driver/client.py); deck schema {"name","main_deck":[...]} via deck().
  - waiting_for is GONE (null); priority = PassPriority in the viewing seat's
    top-level legal_actions. MulliganDecision answered as legacy Action
    (verified accepted on 118). Bottom-after-mulligan via vi select, gated on
    waitingForKind.code == 'mulligan' AND turn 1/Untap.
  - Priority-menu noise (tapLandForMana / untapLandForMana / castSpell /
    activateAbility / passPriority choice menus) is not a decision:
    real_decision_pending excludes NON_DECISION_CODES; only schema
    opportunities, decideOptionalEffect, and unknown codes block passes.
  - surf_codes() filters None codes.
  - DiscardToHandSize answered via the generic 'choose' vi surface only when
    hand > 7, never discarding key cards.
  - Target prompts answered on the caster's viewer_interaction as vi schema
    sequence/select with the advertised composite choice id, preferring the
    seat-1 candidate (the #658 CopyRetarget pattern).
  - Passes via legacy PassPriority action, falling back to the vi passPriority
    choice. Casts via legacy CastSpell action matched by object_id.
  - Never pass the acting seat's priority while a real decision is pending.
  - Land plays locked once the key spell is cast, so pre/post hand deltas
    measure only the reported replacement.

Evidence: evidence/5654/20261008-5654/pre_A.json, post_A.json, pre_B.json,
post_B.json, parse_evidence.json, run.json, manifest.sha256, summary.png,
scenario_5654_01050.py, wire_log.jsonl, scenario_run.log
(this run: evidence/5654/run-5654-reval-v01050-20261009-2311/...)
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
RUN_ID = "run-5654-reval-v01050-20261009-2311"
ISSUE = 5654
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

THIEF = "notion thief"
PLAG = "plagiarize"
DIV = "divination"
ISLAND = "island"
SWAMP = "swamp"
HULL = "hullbreacher"

A_DECK_P0 = deck((THIEF, 8), (ISLAND, 26), (SWAMP, 26))
A_DECK_P1 = deck((DIV, 8), (ISLAND, 52))
B_DECK_P0 = deck((PLAG, 8), (ISLAND, 52))
B_DECK_P1 = deck((ISLAND, 60))

CARD_DATA_PATH = f"{BACKFILL}/server/releases/v0.105.0/data/card-data.json"
CARD_DATA = json.load(open(CARD_DATA_PATH))

SERVER_IDENTITY = {
    "server_version": "v0.105.0",
    "build_commit": "965e243",
    "protocol_version": 120,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.105.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode (pin verified 2026-10-09 13:11 run); data digests "
                       "match the signed manifest; digests recomputed against "
                       "on-disk files this run"),
    "source": ("2026-10-09: latest stable release v0.105.0 (published "
               "2026-10-09T17:49:54Z) == pinned release dir; ServerHello "
               "0.105.0/965e243/protocol 120 verified by handshake this run; "
               "hashes recomputed against on-disk artifacts this run; "
               "server process on 127.0.0.1:9374 (pid 16023) reused from "
               "owning run run-20261009-2211-backfill (not restarted, "
               "backfill-owned)"),
}
for _f, _k in (("server/releases/v0.105.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.105.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.105.0/data/draft-pools.json", "draft_pools_sha256")):
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


# ------------------------------------------------------------- common ticks (protocol 120)
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
    handset = set(hand)
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        if not any(_cand_reference(ch) in handset for ch in cands):
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
    """Bare CastSpell (protocol 120: engine auto-pays). Arms the
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
    """Protocol-120 cast-confirmation guard (2026-10-09 lesson), Game-adapted.

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


# ------------------------------------------------------------- target selection (120)
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
    assert str(ver).startswith("0.105.0"), f"unexpected version {ver}"
    assert int(proto) == 120, f"unexpected protocol {proto}"
    assert str(build) == "965e243", f"unexpected build {build}"


def parse_evidence():
    """Capture the actual replacement parse entries from the PINNED
    v0.105.0 card-data.json (the A2 evidence)."""
    out = {}
    for n in (THIEF, PLAG, HULL):
        e = CARD_DATA[n]
        out[n] = {"replacements": e.get("replacements"),
                  "oracle_text": e.get("oracle_text")}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


def summarize_parse(pe):
    """Reduce each card's parse to the fields the issue is about."""
    res = {}
    for n, e in pe.items():
        reps = e["replacements"] or []
        r0 = reps[0] if reps else {}
        ex = (r0.get("execute") or {})
        eff = ex.get("effect") or {}
        sub = ex.get("sub_ability") or {}
        seff = sub.get("effect") or {}
        res[n] = {
            "event": r0.get("event"),
            "condition": (r0.get("condition") or {}).get("type"),
            "execute_effect": eff.get("type"),
            "execute_desc": eff.get("description"),
            "sub_effect": seff.get("type"),
            "sub_count": (seff.get("count") or {}).get("value"),
            "sub_target": (seff.get("target") or {}).get("type"),
        }
    return res

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
        self.pending_cast = None  # protocol-120 cast-confirmation guard state
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


# ---------------------------------------------------------------- Game A: Notion Thief
async def game_a():
    """P0 casts Notion Thief; P1 casts Divination on their own main phase.
    Both draws are non-first draw-step draws -> per Oracle both replaced."""
    g = Game("A", A_DECK_P0, A_DECK_P1)
    await g.start()

    def p0_keep(state, pid):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in (ISLAND, SWAMP))
        return THIEF in hn and lands >= 2

    def p1_keep(state, pid):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n == ISLAND)
        return DIV in hn and lands >= 2

    async def p0_tick(c, st, acts, state):
        if await do_mulligan(c, acts, st, 0, "P0", g, p0_keep):
            return
        if await do_bottom(c, acts, st, 0, "P0", g, (THIEF,)):
            return
        if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (THIEF,)):
            return
        d = combat_tick(acts)
        if d:
            await submit_as_is(c, d)
            return
        # protocol-120 cast-confirmation guard: no passes or new plays
        # while our CastSpell is in flight but unconfirmed.
        holding = cast_guard_tick(g, state, "P0") == "hold"
        if (not g.key_cast and not holding and my_main(state, 0)
                and my_priority(acts)
                and not real_decision_pending(st)):
            oid = card_in_hand_oid(state, 0, THIEF)
            if oid is not None:
                # engine auto-pays; CastSpell is offered only when castable
                if await cast_named(c, state, acts, THIEF, oid, "P0", g,
                                   "key_cast"):
                    g.note(f"P0 cast Notion Thief (turn {state.get('turn_number')})")
                    g.say(f"P0 casts Notion Thief (turn {state.get('turn_number')})")
                    return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return
        if not holding and not real_decision_pending(st) and my_priority(acts):
            await pass_priority(c, st, acts, g)

    async def p1_tick(c, st, acts, state):
        if await do_mulligan(c, acts, st, 1, "P1", g, p1_keep):
            return
        if await do_bottom(c, acts, st, 1, "P1", g, (DIV,)):
            return
        if await do_discard_to_handsize(c, acts, st, 1, "P1", g, (DIV,)):
            return
        d = combat_tick(acts)
        if d:
            await submit_as_is(c, d)
            return
        # protocol-120 cast-confirmation guard (P1 never casts in game A,
        # but the guard must still run before early returns).
        holding = cast_guard_tick(g, state, "P1") == "hold"
        # a confirmed Divination submission advances cast_done: the one-shot
        # div_cast_submitted is set at submission, cleared by the 30s backstop
        if g.div_cast_submitted and g.pending_cast is None:
            g.cast_done = True
            say("[P1] Divination confirmed on stack")
        # post: Divination resolved (in gy), stack empty
        if (g.cast_done and not g.post_done
                and in_gy(state, 1, DIV)
                and len(state.get("stack", []) or []) == 0):
            g.say("Divination resolved; exporting POST_A")
            g.post = await g.export("post_A", g.p0)
            g.post_done = True
            g.snaps["postA"] = {"P0": snap(g.post, 0), "P1": snap(g.post, 1)}
            g.done = True
            g.say("exported POST_A")
            return
        if (not g.div_cast_submitted and not holding and g.key_cast
                and on_bf(state, 0, THIEF)
                and my_main(state, 1) and my_priority(acts)
                and not real_decision_pending(st)):
            oid = card_in_hand_oid(state, 1, DIV)
            if oid is not None:
                g.say("P1 main phase: exporting PRE_A, then casting Divination")
                g.pre = await g.export("pre_A", g.p0)
                g.pre_done = True
                g.snaps["preA"] = {"P0": snap(g.pre, 0),
                                   "P1": snap(g.pre, 1)}
                if await cast_named(c, state, acts, DIV, oid, "P1", g,
                                    "div_cast_submitted"):
                    g.lands_locked = True
                    g.note(f"P1 cast Divination with Thief on board "
                           f"(turn {state.get('turn_number')})")
                    g.say(f"P1 casts Divination (turn {state.get('turn_number')})")
                    return
        if await play_a_land(c, state, 1, acts, "P1", g):
            return
        if not holding and not real_decision_pending(st) and my_priority(acts):
            await pass_priority(c, st, acts, g)

    ok = await pump(g, p0_tick, p1_tick, timeout_s=1200, resolve_timeout_s=300)
    return g, ok


# ---------------------------------------------------------------- Game B: Plagiarize
async def game_b():
    """P0 casts Plagiarize targeting P1 during P1's Upkeep; P1's draw-step
    draw must be skipped and P0 must draw 1."""
    g = Game("B", B_DECK_P0, B_DECK_P1)
    await g.start()

    def p0_keep(state, pid):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n == ISLAND)
        return PLAG in hn and lands >= 2

    def p1_keep(state, pid):
        return True

    async def p0_tick(c, st, acts, state):
        if await do_mulligan(c, acts, st, 0, "P0", g, p0_keep):
            return
        if await do_bottom(c, acts, st, 0, "P0", g, (PLAG,)):
            return
        if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (PLAG,)):
            return
        d = combat_tick(acts)
        if d:
            await submit_as_is(c, d)
            return
        # resolve stage: answer the Plagiarize target prompt for P1's seat.
        # Target answers are allowed while a cast is in flight (holding).
        if g.key_cast and not g.target_ok:
            # legacy ChooseTarget path (protocol 120): one action per
            # candidate; submit the P1 one as-is.
            for a in merged_actions(st):
                if a.get("type") != "ChooseTarget":
                    continue
                clean = {k: v for k, v in a.items()
                         if not k.startswith("_")}
                key = ("tgt_leg", json.dumps(clean.get("data", {}),
                                             sort_keys=True, default=str))
                if key not in g.submitted:
                    g.submitted.add(key)
                    say("[P0] ChooseTarget offered: "
                        f"{json.dumps(clean, default=str)[:500]}")
                    wire("B_choosetarget_seen", {"action": clean})
                if ("tgt_leg_done",) in g.submitted:
                    continue
                if _find_player_refs(clean.get("data", {}), g.p1_seat):
                    g.submitted.add(("tgt_leg_done",))
                    g.target_prompt_seen = True
                    say("[P0] submitting ChooseTarget for P1")
                    wire("B_choosetarget_submit", {"action": clean})
                    await submit_as_is(c, a)
                    g.target_ok = True
                    g.pending_cast = None  # demonstrably alive
                    g.note(f"Plagiarize target prompt answered for P1 "
                           f"(seat {g.p1_seat}, legacy ChooseTarget)")
                    return
            opp, rtype, stype = target_opportunity(st)
            if opp is not None:
                g.target_prompt_seen = True
                rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
                cands = rdata.get("candidates") or rdata.get("choices") or []
                wire("B_target_opportunity",
                     {"iid": str(opp.get("interactionId"))[:16],
                      "rtype": rtype, "stype": stype,
                      "candidates": [choice_text(x)[:80] for x in cands][:8],
                      "seats": [candidate_seat(x) for x in cands][:8]})
                ch = pick_target_candidate(opp, preferred_seat=g.p1_seat)
                if ch is not None and candidate_seat(ch) == g.p1_seat:
                    await submit_target(c, opp, rtype, stype, ch, "P0", g)
                    g.target_ok = True
                    g.pending_cast = None  # demonstrably alive
                    g.note(f"Plagiarize target prompt answered for P1 "
                           f"(seat {g.p1_seat})")
                else:
                    g.note("Plagiarize target prompt seen but no P1-seat "
                           "candidate; will not mis-target")
                return
        # protocol-120 cast-confirmation guard: no passes or new plays
        # while our CastSpell is in flight but unconfirmed.
        holding = cast_guard_tick(g, state, "P0") == "hold"
        # post: P1 past draw step (main phase), Plagiarize in gy, stack empty
        if (g.cast_done and not g.post_done
                and in_gy(state, 0, PLAG)
                and state.get("active_player") == 1
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(state.get("stack", []) or []) == 0):
            g.say("P1 past draw step; exporting POST_B")
            g.post = await g.export("post_B", g.p0)
            g.post_done = True
            g.snaps["postB"] = {"P0": snap(g.post, 0), "P1": snap(g.post, 1)}
            g.done = True
            g.say("exported POST_B")
            return
        # cast window: P1's Upkeep, P0 holds priority
        if (not g.key_cast and not holding
                and state.get("active_player") == 1
                and state.get("phase") == "Upkeep" and my_priority(acts)
                and not real_decision_pending(st)):
            oid = card_in_hand_oid(state, 0, PLAG)
            if oid is not None:
                # engine auto-pays; CastSpell is offered only when castable
                g.say("P1 Upkeep: exporting PRE_B, then casting Plagiarize @P1")
                g.pre = await g.export("pre_B", g.p0)
                g.pre_done = True
                g.snaps["preB"] = {"P0": snap(g.pre, 0),
                                   "P1": snap(g.pre, 1)}
                if await cast_named(c, state, acts, PLAG, oid, "P0", g,
                                    "key_cast"):
                    g.cast_done = True
                    g.lands_locked = True
                    g.note(f"P0 cast Plagiarize during P1's Upkeep "
                           f"(turn {state.get('turn_number')})")
                    g.say(f"P0 casts Plagiarize (turn {state.get('turn_number')})")
                    return
        if await play_a_land(c, state, 0, acts, "P0", g):
            return
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
        # protocol-120 cast-confirmation guard (P1 never casts in game B,
        # but the guard must still run before early returns).
        holding = cast_guard_tick(g, state, "P1") == "hold"
        if await play_a_land(c, state, 1, acts, "P1", g):
            return
        if not holding and not real_decision_pending(st) and my_priority(acts):
            await pass_priority(c, st, acts, g)

    ok = await pump(g, p0_tick, p1_tick, timeout_s=1200, resolve_timeout_s=300)
    return g, ok


# ---------------------------------------------------------------- pump
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
                # selection on protocol 120)
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
def evaluate(gA, gB, parse_sum):
    ass = {}
    notes = []
    obsA, obsB = gA.notes, gB.notes

    # ---- A2 parse documentation (from PINNED v0.105.0 card-data) ----
    nt = parse_sum["notion thief"]
    pl = parse_sum["plagiarize"]
    hb = parse_sum["hullbreacher"]
    nt_gap_gone = (nt["execute_effect"] == "Draw" and nt["sub_effect"] is None
                   and nt["sub_count"] is None)
    pl_gap = (pl["execute_effect"] == "Unimplemented"
              and pl["sub_effect"] == "Draw" and pl["sub_count"] == 1
              and pl["sub_target"] == "Controller")
    hb_ok = (hb["execute_effect"] == "Token" and hb["sub_effect"] is None)
    ass["A2A_parse_notion"] = "passed"
    notes.append("A2A parse notion thief (pinned v0.105.0): "
                 f"execute={nt['execute_effect']} "
                 f"(desc={nt['execute_desc']}), sub={nt['sub_effect']}: "
                 "the Unimplemented skip node from the v0.78.0/v0.85.0 "
                 "contract is GONE -- the parser now emits a plain "
                 "Draw{1,Controller} replacement; the runtime assertions "
                 "below are the decisive test")
    ass["A2B_parse_plagiarize"] = "passed"
    notes.append("A2B parse plagiarize (pinned v0.105.0): "
                 f"execute={pl['execute_effect']} "
                 f"({pl['execute_desc']}), sub={pl['sub_effect']}x"
                 f"{pl['sub_count']}->{pl['sub_target']}: "
                 f"{'class gap PERSISTS' if pl_gap else 'gap not as reported'}")
    notes.append("parse hullbreacher control: "
                 f"execute={hb['execute_effect']} (single-action substitute "
                 f"fully parsed: {'yes' if hb_ok else 'NO'})")

    # re-read the saved states from disk (proves they parse)
    def load(name):
        p = f"{EVDIR}/{name}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())["state"]

    preA, postA = load("pre_A"), load("post_A")
    preB, postB = load("pre_B"), load("post_B")

    # ---- Game A ----
    if preA is not None:
        # the Divination was demonstrably in P1's hand at pre time: the cast
        # submitted from P1's own live view right after the export
        div_in_hand = (card_in_hand_oid(preA, 1, DIV) is not None
                       or gA.cast_done)
        ok = (on_bf(preA, 0, THIEF)
              and div_in_hand
              and len(untapped_lands(preA, 1, ISLAND)) >= 3
              and preA.get("phase") in ("PreCombatMain", "PostCombatMain")
              and state_life(preA, 0) == 20 and state_life(preA, 1) == 20)
        ass["A1A_setup"] = "passed" if ok else "failed"
        notes.append(f"A1A setup: thief_on_bf={on_bf(preA, 0, THIEF)}, "
                     f"div_in_P1_hand={div_in_hand}, "
                     f"P1_untapped_islands={len(untapped_lands(preA, 1, ISLAND))}: "
                     f"{'ok' if ok else 'SETUP FAILED'}")
    else:
        ass["A1A_setup"] = "failed"
        notes.append("A1A setup: pre_A missing -- Notion Thief game never "
                     "reached the Divination cast")
    if preA is not None and postA is not None:
        h0pre, l0pre, _ = snap(preA, 0)
        h1pre, l1pre, _ = snap(preA, 1)
        h0post, l0post, _ = snap(postA, 0)
        h1post, l1post, _ = snap(postA, 1)
        gA.snaps["preA"] = {"P0": (h0pre, l0pre), "P1": (h1pre, l1pre)}
        gA.snaps["postA"] = {"P0": (h0post, l0post), "P1": (h1post, l1post)}
        say(f"[A] deltas P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        # A4: victim skips both draws (Divination itself left the hand: -1)
        if h1post == h1pre - 1 and l1post == l1pre:
            ass["A4A_skip_holds"] = "passed"
            notes.append(f"A4A skip holds: P1 hand {h1pre}->{h1post} "
                         f"(-1 = cast Divination, +0 draws), "
                         f"library {l1pre}->{l1post} (both draws skipped)")
        else:
            ass["A4A_skip_holds"] = "failed"
            notes.append(f"A4A skip BROKEN: P1 hand {h1pre}->{h1post}, "
                         f"library {l1pre}->{l1post} (expected hand-1, lib unchanged)")
        # A5: controller draws both
        if h0post == h0pre + 2 and l0post == l0pre - 2:
            ass["A5A_ctrl_draws"] = "passed"
            notes.append(f"A5A controller draws: P0 hand {h0pre}->{h0post}, "
                         f"library {l0pre}->{l0post}")
        else:
            ass["A5A_ctrl_draws"] = "failed"
            notes.append(f"A5A controller draw BROKEN: P0 hand {h0pre}->{h0post} "
                         f"(expected +2), library {l0pre}->{l0post} (expected -2)")
        if in_gy(postA, 1, DIV) and len(postA.get("stack", []) or []) == 0:
            ass["A6A_cleanup"] = "passed"
            notes.append("A6A cleanup: Divination in P1 gy, stack empty, game proceeding")
        else:
            ass["A6A_cleanup"] = "failed"
            notes.append(f"A6A cleanup broken: div_in_gy={in_gy(postA, 1, DIV)}, "
                         f"stack={len(postA.get('stack', []) or [])}")
    else:
        for k in ("A4A_skip_holds", "A5A_ctrl_draws", "A6A_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} not-run (missing pre_A/post_A)")

    # ---- Game B ----
    if preB is not None:
        # pre_B is exported by seat 0 right before P0's own cast submission;
        # the cast submitting proves Plagiarize was in P0's hand at pre time
        plag_in_hand = (card_in_hand_oid(preB, 0, PLAG) is not None
                        or gB.cast_done)
        ok = (plag_in_hand
              and len(untapped_lands(preB, 0, ISLAND)) >= 4
              and preB.get("active_player") == 1
              and preB.get("phase") == "Upkeep"
              and state_life(preB, 0) == 20 and state_life(preB, 1) == 20)
        ass["A1B_setup"] = "passed" if ok else "failed"
        notes.append(f"A1B setup: plag_in_P0_hand={plag_in_hand}, "
                     f"P0_untapped_islands={len(untapped_lands(preB, 0, ISLAND))}, "
                     f"active={preB.get('active_player')} phase={preB.get('phase')}: "
                     f"{'ok' if ok else 'SETUP FAILED'}")
    else:
        ass["A1B_setup"] = "failed"
        notes.append("A1B setup: pre_B missing -- Plagiarize game never "
                     "reached the Upkeep cast")
    if gB.target_prompt_seen and gB.target_ok:
        ass["A3B_target_offered"] = "passed"
        notes.append("A3B target: Plagiarize target prompt offered and "
                     "answered for P1 (seat 1)")
    elif gB.target_prompt_seen:
        ass["A3B_target_offered"] = "failed"
        notes.append("A3B target: target prompt seen but no seat-1 candidate "
                     "could be answered (see notes)")
    elif gB.key_cast:
        ass["A3B_target_offered"] = "failed"
        notes.append("A3B target: NO target prompt observed after the "
                     "Plagiarize cast (as on v0.78.0/v0.85.0)")
    else:
        ass["A3B_target_offered"] = "not-run"
        notes.append("A3B target: not-run (Plagiarize never cast)")
    if preB is not None and postB is not None:
        h0pre, l0pre, _ = snap(preB, 0)
        h1pre, l1pre, _ = snap(preB, 1)
        h0post, l0post, _ = snap(postB, 0)
        h1post, l1post, _ = snap(postB, 1)
        gB.snaps["preB"] = {"P0": (h0pre, l0pre), "P1": (h1pre, l1pre)}
        gB.snaps["postB"] = {"P0": (h0post, l0post), "P1": (h1post, l1post)}
        say(f"[B] deltas P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        # pre_B hand includes Plagiarize; cast consumes 1, replaced draw adds 1
        if h1post == h1pre and l1post == l1pre:
            ass["A4B_skip_holds"] = "passed"
            notes.append(f"A4B skip holds: P1 hand {h1pre}->{h1post}, "
                         f"library {l1pre}->{l1post} (draw-step draw skipped)")
        else:
            ass["A4B_skip_holds"] = "failed"
            notes.append(f"A4B skip BROKEN: P1 hand {h1pre}->{h1post}, "
                         f"library {l1pre}->{l1post} (expected unchanged)")
        if h0post == h0pre and l0post == l0pre - 1:
            ass["A5B_ctrl_draws"] = "passed"
            notes.append(f"A5B controller draws: P0 hand {h0pre}->{h0post} "
                         f"(cast -1, replaced draw +1), library {l0pre}->{l0post}")
        else:
            ass["A5B_ctrl_draws"] = "failed"
            notes.append(f"A5B controller draw BROKEN: P0 hand {h0pre}->{h0post} "
                         f"(expected {h0pre}), library {l0pre}->{l0post} (expected -1)")
        if in_gy(postB, 0, PLAG) and len(postB.get("stack", []) or []) == 0:
            ass["A6B_cleanup"] = "passed"
            notes.append("A6B cleanup: Plagiarize in P0 gy, stack empty, game proceeding")
        else:
            ass["A6B_cleanup"] = "failed"
            notes.append(f"A6B cleanup broken: plag_in_gy={in_gy(postB, 0, PLAG)}, "
                         f"stack={len(postB.get('stack', []) or [])}")
    else:
        for k in ("A4B_skip_holds", "A5B_ctrl_draws", "A6B_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} not-run (missing pre_B/post_B)")

    # ---- verdict ----
    a_ok = ass.get("A1A_setup") == "passed"
    b_ok = ass.get("A1B_setup") == "passed"
    a_bad = a_ok and (ass.get("A4A_skip_holds") == "failed"
                      or ass.get("A5A_ctrl_draws") == "failed")
    b_bad = b_ok and (ass.get("A4B_skip_holds") == "failed"
                      or ass.get("A5B_ctrl_draws") == "failed")
    a_good = a_ok and all(ass.get(k) == "passed" for k in
                          ("A4A_skip_holds", "A5A_ctrl_draws", "A6A_cleanup"))
    b_good = b_ok and all(ass.get(k) == "passed" for k in
                          ("A3B_target_offered", "A4B_skip_holds",
                           "A5B_ctrl_draws", "A6B_cleanup"))
    tested = [x for x, okv in (("A", a_ok), ("B", b_ok)) if okv]
    if a_bad or b_bad:
        verdict = "reproduced"
        notes.append("draw replacement deviates from Oracle in-game "
                     f"(A_bad={a_bad}, B_bad={b_bad})")
    elif tested and all((a_good if t == "A" else b_good) for t in tested):
        verdict = "not-reproduced"
        notes.append("in-engine draw replacement matches Oracle for every "
                     "completed game; parse-level notes above are latent")
    elif not tested:
        verdict = "blocked"
        notes.append("no game reached its cast; see notes")
    else:
        verdict = "reproduced"
        notes.append("mixed outcome assertions on the reported contract")
    notes.extend(obsA)
    notes.extend(obsB)
    return ass, notes, verdict


# ---------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 960
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #5654 - Notion Thief + Plagiarize draw replacement "
                     "(revalidation)", fill=(235, 240, 250)); y += 28
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - run {run['run_id']} "
                     f"- 2026-10-09", fill=(140, 160, 180)); y += 26
    vc = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
          "blocked": (230, 200, 90)}.get(run["verdict"], (200, 200, 200))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vc); y += 32
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = [
        ("A2A_parse_notion", "A2A parse: Notion Thief parse documented (see notes)"),
        ("A2B_parse_plagiarize", "A2B parse: Plagiarize parse documented (see notes)"),
        ("A1A_setup", "A1A setup: Thief on BF, Divination in P1 hand, mana, 20/20"),
        ("A4A_skip_holds", "A4A [bug?] victim skips both Divination draws"),
        ("A5A_ctrl_draws", "A5A [bug?] controller draws both (P0 +2 hand, -2 lib)"),
        ("A6A_cleanup", "A6A cleanup: Divination in P1 gy, stack empty"),
        ("A1B_setup", "A1B setup: Plagiarize in P0 hand, P1 Upkeep, 20/20"),
        ("A3B_target_offered", "A3B target: Plagiarize target prompt answered for P1"),
        ("A4B_skip_holds", "A4B [bug?] P1 draw-step draw skipped"),
        ("A5B_ctrl_draws", "A5B [bug?] P0 draws 1 (hand net 0, lib -1)"),
        ("A6B_cleanup", "A6B cleanup: Plagiarize in P0 gy, stack empty"),
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
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 5654/" + run["run_id"],
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
    say("parse summary: " + json.dumps(parse_sum))

    gA, okA = await game_a()
    gA.say(f"game A finished ok={okA}")
    await gA.close()
    gB, okB = await game_b()
    gB.say(f"game B finished ok={okB}")
    await gB.close()

    # final post exports if a loop ended with pre but no post
    for g, tag in ((gA, "A"), (gB, "B")):
        if g.pre is not None and g.post is None:
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
        "server_run_dir": "runs/run-20261009-2211-backfill",
        "parse_summary": parse_sum,
        "driver": {"protocol_advertised": 120, "client": "driver/client.py",
                   "scenario": "driver/scenario_5654_01050.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_5654_01050.py", "rb").read()).hexdigest(),
        "decks": {
            "A_P0": [["notion thief", 8], ["island", 26], ["swamp", 26]],
            "A_P1": [["divination", 8], ["island", 52]],
            "B_P0": [["plagiarize", 8], ["island", 52]],
            "B_P1": [["island", 60]],
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
            "8x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Hullbreacher shares the same Oracle text and parse class but was not "
            "driven (same-class coverage via Plagiarize/Notion Thief).",
            "Not tested on the original 2026-07-12 build; verdict is scoped to "
            "v0.105.0, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "A: P0 8x notion thief + 26 island + 26 swamp vs P1 8x "
                      "divination + 52 island (P1 casts Divination with Thief on "
                      "board). B: P0 8x plagiarize + 52 island vs P1 60 island "
                      "(P0 casts Plagiarize @P1 during P1's Upkeep).",
        "contract_line": "Per Oracle, each affected draw is skipped by the "
                         "victim and drawn by the controller instead.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_5654_01050.py",
                f"{EVDIR}/scenario_5654_01050.py")
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
    pidfile = "/tmp/scenario_5654_01050.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_5654_01050 instance is alive "
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
