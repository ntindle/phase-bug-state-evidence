"""Issue #9842: Vanish into Memory -- delayed-trigger discard reads 0.

FIRST VALIDATION on the pinned release v0.108.0 (build b8fa1d3, protocol 134).
Protocol-134 driver conventions (verified by scenario_9841_01080.py,
2026-10-13): CreateGameWithSettings + JoinGameWithPassword + start_when_full,
bare CastSpell (engine auto-pays), cast-confirmation guard, repeating prompts
answered per-firing keyed by interactionId, real_decision_pending gating
priority passes.

ISSUE (phase-rs/phase#9842, opened 2026-10-12, open,
bug/area:engine/priority:p3-card-specific): the delayed trigger's discard
amount `Toughness { CostPaidObject }` resolved to 0 in a planner probe, so the
"If you do, discard cards equal to that creature's toughness" clause discards
nothing when the exiled creature returns.

Oracle text (verified from pinned v0.108.0 card-data.json):
  Vanish into Memory {2}{W}{U} Sorcery:
    "Exile target creature. You draw cards equal to that creature's power.
     At the beginning of your next upkeep, return that card to the battlefield
     under its owner's control. If you do, discard cards equal to that
     creature's toughness."

Pinned parse (v0.108.0 card-data.json): ability[0] Spell: ChangeZone->Exile
(target Typed Creature); sub Draw count Ref Power{Demonstrative} target
Controller; sub CreateDelayedTrigger (AtNextPhaseForPlayer Upkeep player 0)
whose effect is ChangeZone->Battlefield target ParentTarget, whose sub is
Discard count Ref Toughness{Demonstrative} target Controller. (The issue's
probe saw CostPaidObject; the pin lowers to Demonstrative -- the executed
outcome is decisive either way and is asserted directly.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
P0 (seat 0) casts Elite Vanguard (vanilla 2/1, {W}) once a Plains is out,
then Vanish into Memory targeting it ({2}{W}{U} off 4 lands, P0's 4th turn).
Power 2 != toughness 1, so the draw clause (expect +2 cards) and the discard clause
(expect -1 card) are distinguishable.

Expected per the issue's "Expected" (after the creature returns, the spell's
controller discards cards equal to the returned creature's toughness):
  E1: on resolution the Vanguard is exiled and P0 draws 2 (= its power);
  E2: at the beginning of P0's next upkeep the delayed trigger returns the
      Vanguard to the battlefield under P0's control;
  E3: P0 then discards 1 card (= its toughness).

The reported bug: E3 discards 0 -- the discard amount resolves to 0.

Assertions:
  A1_setup    pre: Vanish into Memory in P0 hand, Elite Vanguard on P0's
                battlefield (2/1), >=4 lands (>=1 Plains, >=1 Island) at cast.
  A2_parse    parse evidence captured from pinned v0.108.0 card-data for
                Vanish into Memory (ChangeZone->Exile; Draw Power;
                CreateDelayedTrigger Upkeep; return ChangeZone->Battlefield
                ParentTarget; Discard Toughness).
  A3_cast     the spell's cast was allowed, targeted the Vanguard, reached
                the stack.
  A4_exile_draw  on resolution: Vanguard in Exile, Vanish into Memory in P0
                graveyard, P0 hand +1 net (cast -1, draw +2), library -2.
  A5_return   at P0's next upkeep the Vanguard returned to the battlefield
                under P0's control (pre_upkeep: in Exile; post: on BF).
  A6_discard  [the reported bug] P0 discarded exactly 1 card (= toughness)
                after the return: N_observed = (H_pre - H_post) + 1 (draw
                step) == 1 and graveyard delta == 1. FAILS (= reproduced)
                if N_observed == 0 (no discard prompt / no discard).
  A7_pt       the returned Vanguard is still 2/1 on the battlefield.
  A8_cleanup  stack empty post-resolution.

Verdict rule: reproduced iff A1, A3, A4, A5 pass and A6 fails with
N_observed == 0. not-reproduced iff all pass. blocked iff setup/cast/upkeep
never completed.

Discard answering policy (deterministic): any P0 discard-choice opportunity
during the upkeep window is answered with the requested count (lands first);
the count, candidates, and picks are recorded verbatim as evidence.
Unknown decisions are logged with raw opportunity JSON, never auto-answered
beyond the documented policy.

Evidence: evidence/9842/run-9842-v01080-20261013-0320/pre_cast.json,
post_cast.json, pre_upkeep.json, post_upkeep.json, parse_evidence.json,
run.json, manifest.sha256, summary.png, scenario_9842_01080.py,
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
RUN_ID = "run-9842-v01080-20261013-0320"
ISSUE = 9842
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

VIM = "vanish into memory"
VANGUARD = "elite vanguard"
PLAINS = "plains"
ISLAND = "island"
P0_DECK = deck((VIM, 8), (VANGUARD, 8), (ISLAND, 22), (PLAINS, 22))
P1_DECK = deck((ISLAND, 60))

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
               "by run backfill-20261013-0241)"),
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


# NOTE (run-0320 lesson): "playLand" is in this set. A PlayLand entry in
# the priority menu is an available action, not a pending decision; without
# this, the driver deadlocks whenever it must pass priority on a turn where
# it chose not to play its land (the menu never becomes "passable").
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
    """Capture the parsed-ability lowering for Vanish into Memory from the
    PINNED v0.108.0 card-data.json (the A2 evidence). Walks recursively:
    the Discard lives inside the CreateDelayedTrigger effect's own
    sub_ability chain, not the top-level sub chain (run-...-0250 lesson)."""
    e = CARD_DATA[VIM]
    abil = (e.get("abilities") or [{}])[0]
    effects = []

    def walk(node):
        if isinstance(node, dict):
            eff = node.get("effect")
            if isinstance(eff, dict):
                effects.append(eff)
                walk(eff)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(abil)
    out = {
        "mana_cost": e.get("mana_cost"),
        "ability_kind": abil.get("kind"),
        "effect_types": [f.get("type") for f in effects],
        "effects": effects,
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

# ------------------------------------------------------------- #9842 helpers
def pt_val(o, key):
    v = o.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def stack_has_spell(state, name):
    # object-based: stack ENTRY payloads do not reliably carry the card
    # name, but stack-zone objects do (#9841 driver note)
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("zone") or "").lower() == "stack" and \
                str(o.get("base_name") or o.get("name") or "").lower() == name:
            return True
    for entry in state.get("stack") or []:
        if name in json.dumps(entry).lower():
            return True
    return False


def exile_has(state, name):
    return any(str(o.get("base_name") or o.get("name") or "").lower() == name
               and str(o.get("zone") or "").lower() == "exile"
               for o in (state.get("objects") or {}).values())


def bf_creature(state, pid, name):
    for o in bf_oids(state, pid):
        if obj_lname(state, o) == name:
            return int(o)
    return None


def creature_target_opp(st, want_name, state):
    """Target opportunity whose candidates include the named battlefield
    creature. Returns (opp, rtype, stype, choice) or Nones."""
    opp, rtype, stype = target_opportunity(st)
    if opp is None:
        return None, None, None, None
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    for ch in cands:
        oid = _cand_reference(ch)
        if oid is None:
            continue
        o = get_obj(state, oid)
        if obj_lname(state, oid) == want_name and \
                str(o.get("zone") or "").lower() == "battlefield":
            return opp, rtype, stype, ch
    return None, None, None, None


async def play_land_prefer(c, state, pid, acts, tag, game):
    """Play one land per turn; prefer Plains until we have one, then Island
    until we have one (VIM needs {2}{W}{U}), then anything."""
    if game.lands_locked:
        return False
    turn = state.get("turn_number")
    if game.land_played_turn.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False

    def has(nm):
        return any(obj_lname(state, o) == nm for o in bf_oids(state, pid))

    def rank(o):
        nm = obj_lname(state, o)
        if nm == PLAINS and not has(PLAINS):
            return 0
        if nm == ISLAND and not has(ISLAND):
            return 1
        return 2

    for o in sorted(lands, key=rank):
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                game.land_played_turn[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


async def answer_discard_prompt(c, opp, tag, game, state):
    """Answer a P0 discard-choice opportunity during the upkeep window. n
    comes from the spec constraint (schema) or the expected toughness;
    picks lands first. Records the event verbatim."""
    iid = opp.get("interactionId")
    key = (tag, "discard", str(iid))
    if key in game.submitted:
        return False
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    try:
        n = int(con.get("min") or con.get("max") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = game.discard_expected  # fallback: the contract's toughness
    handset = {str(x) for x in hand_ids(state, 0)}
    cands = rdata.get("candidates") or rdata.get("choices") or []
    real = [ch for ch in cands if str(_cand_reference(ch)) in handset]
    if not real:
        return False

    def drank(ch):
        oid = _cand_reference(ch)
        nm = obj_lname(state, oid)
        if is_land(get_obj(state, oid)):
            return (0, nm)
        if nm in (VIM, VANGUARD):
            return (2, nm)
        return (1, nm)

    ordered = sorted(real, key=drank)
    picks = [ch.get("id") for ch in ordered[:n]]
    names = [obj_lname(state, _cand_reference(ch)) for ch in ordered[:n]]
    ev = {"iid": str(iid)[:16], "n_requested": n,
          "n_candidates": len(real),
          "candidate_names": [obj_lname(state, _cand_reference(ch))
                              for ch in real][:16],
          "picked": names,
          "rtype": resp.get("type"), "spec_type": spec.get("type")}
    game.discard_events.append(ev)
    say(f"[{tag}] DISCARD prompt: n_requested={n} candidates={len(real)} "
        f"picked={names}")
    wire("b9842_discard_prompt",
         {"who": tag, **ev, "raw": json.dumps(opp, default=str)[:4000]})
    with open(f"{EVDIR}/opportunity_discard_{str(iid).replace('.', '_')}.json",
              "w") as f:
        json.dump(opp, f, indent=1, default=str)
    rtype = resp.get("type")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec.get("type") or "select",
                            "data": {"choiceIds": picks}}}
    else:
        # single-choose firings: answer one; the engine re-prompts
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": picks[0]}}}
        game.discard_expected = max(0, game.discard_expected - 1)
    game.submitted.add(key)
    await interact_as(c, sub, tag)
    return True


def discard_prompt_opp(st, state):
    """Find a GENUINE discard-choice opportunity during the upkeep window.

    A choice is a discard candidate only if it is a pure hand-card candidate:
    its reference resolves to a P0 hand card AND it carries NO action surface.
    The ordinary priority menu also offers hand cards (playLand / castSpell /
    tapLandForMana choices); those all carry action surfaces and are excluded.
    Run-0310 lesson: treating playLand menu choices as discard candidates made
    the driver "answer the discard prompt" by playing an Island.
    Returns (opp, pure_choices) or (None, [])."""
    handset = {str(x) for x in hand_ids(state, 0)}
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        if not cands:
            continue
        pure = []
        for ch in cands:
            has_action = any(s.get("type") == "action"
                             for s in ch.get("surfaces", []) or [])
            if has_action:
                continue
            if str(_cand_reference(ch)) in handset:
                pure.append(ch)
        if pure:
            return opp, pure
    return None, []


# ---------------------------------------------------------------- Game class
class VanishGame(Game):
    def __init__(self):
        super().__init__("B9842", P0_DECK, P1_DECK)
        self.vanguard_cast = False   # Vanguard cast attempted
        self.vim_cast_attempted = False
        self.vim_cast_confirmed = False  # VIM seen on the stack
        self.target_answered = False
        self.target_chosen = None
        self.target_prompt_seen = False  # any target-shaped opportunity seen
        self.creatures_at_cast = None
        self.resolving = False       # VIM on stack -> resolution window
        self.stack_was_seen = False
        self.cast_turn = None
        self.pre_cast = None
        self.post_cast = None
        self.pre_upkeep = None
        self.post_upkeep = None
        self.upkeep_window = False   # P0's next-upkeep trigger window
        self.discard_events = []     # discard prompts answered
        self.upkeep_vi_log = []      # (kind_code, n_opps) per tick in window
        self.discard_expected = 1    # contract toughness (refined at pre_upkeep)
        self.pt_at_cast = None       # (power, toughness) of the Vanguard
        self.vanguard_oid = None
        self.done = False


def p0_keep(state, pid):
    hn = hand_lnames(state, pid)
    lands = sum(1 for nm in hn if nm in (PLAINS, ISLAND))
    return VIM in hn and VANGUARD in hn and lands >= 1


def p1_keep(state, pid):
    return True


async def p0_tick(c, st, acts, state, g):
    for rej in getattr(c, "rejections", []):
        say(f"[P0] REJECTION: {json.dumps(rej, default=str)[:300]}")
        wire("b9842_rejection", {"who": "P0", "rej": rej})
    c.rejections.clear()
    turn = state.get("turn_number") or 0
    phase = state.get("phase")
    if (not g.vim_cast_attempted and turn >= 16 and phase == "Untap"):
        g.say("VIM never cast by turn 16 -- finishing as blocked")
        g.note("VIM never became castable by turn 16 (lands/key cards); blocked")
        g.done = True
        return
    if await do_mulligan(c, acts, st, 0, "P0", g, p0_keep):
        return
    if await do_bottom(c, acts, st, 0, "P0", g, (VIM, VANGUARD)):
        return
    # Never run the generic cleanup-discard inside the upkeep window: the
    # delayed trigger's own discard prompt has the same shape (hand-card
    # candidates) and this tick was stealing it with malformed "select"
    # responses (run-...-0250: two invalid_interaction_response rejections).
    if not g.upkeep_window:
        if await do_discard_to_handsize(c, acts, st, 0, "P0", g, (VIM, VANGUARD)):
            return
    d = combat_tick(acts)
    if d:
        await submit_as_is(c, d)
        return
    answered = False
    # --- VIM spell target selection (allowed even while a cast is holding)
    if not g.target_answered and g.vim_cast_attempted:
        _topp, _trt, _tst = target_opportunity(st)
        if _topp is not None:
            g.target_prompt_seen = True
        opp, rtype, stype, ch = creature_target_opp(st, VANGUARD, state)
        if opp is not None and ch is not None:
            say("[P0] VIM target prompt: answering own Vanguard")
            wire("b9842_spell_target",
                 {"iid": str(opp.get("interactionId"))[:16],
                  "rtype": rtype, "stype": stype})
            await submit_target(c, opp, rtype, stype, ch, "P0", g)
            g.target_answered = True
            g.target_chosen = VANGUARD
            answered = True
            return
    # --- upkeep window: answer the delayed trigger's discard prompt.
    # Only PURE hand-card candidates (no action surface) count -- the
    # priority menu's playLand/castSpell choices are not a discard prompt
    # (run-0310 lesson). No land plays or casts inside the window either,
    # so the hand-size delta measures discards only.
    if g.upkeep_window and not answered:
        kind = vi_kind_code(st)
        n_opps = len(vi_ops(st))
        if not g.upkeep_vi_log or g.upkeep_vi_log[-1] != (kind, n_opps):
            g.upkeep_vi_log.append((kind, n_opps))
            wire("b9842_upkeep_vi", {"kind": kind, "n_opps": n_opps,
                                     "turn": turn, "phase": phase})
        opp, pure = discard_prompt_opp(st, state)
        if opp is not None:
            iid = opp.get("interactionId")
            key = ("P0", "upkeepopp", str(iid))
            if key not in g.submitted:
                g.submitted.add(key)
                if await answer_discard_prompt(c, opp, "P0", g, state):
                    answered = True
                    return
    holding = cast_guard_tick(g, state, "P0") == "hold"
    # --- cast Elite Vanguard (my main phase, any turn before VIM)
    if (not holding and not g.vanguard_cast and my_main(state, 0)
            and my_priority(acts) and not real_decision_pending(st)):
        oid = card_in_hand_oid(state, 0, VANGUARD)
        if oid is not None:
            if await cast_named(c, state, acts, VANGUARD, oid, "P0", g,
                                "vanguard_cast"):
                g.say(f"P0 cast Elite Vanguard (turn {turn})")
                answered = True
                return
    # --- cast Vanish into Memory (turn >= 3, 4 lands incl. W and U)
    if (not holding and not g.vim_cast_attempted and turn >= 3
            and my_main(state, 0) and my_priority(acts)
            and not real_decision_pending(st)):
        lands = [o for o in bf_oids(state, 0) if is_land(get_obj(state, o))]
        nplains = sum(1 for o in lands if obj_lname(state, o) == PLAINS)
        nisland = sum(1 for o in lands if obj_lname(state, o) == ISLAND)
        oid = card_in_hand_oid(state, 0, VIM)
        vg = bf_creature(state, 0, VANGUARD)
        if (oid is not None and vg is not None and len(lands) >= 4
                and nplains >= 1 and nisland >= 1):
            o = get_obj(state, vg)
            g.pt_at_cast = (pt_val(o, "power"), pt_val(o, "toughness"))
            g.vanguard_oid = vg
            g.creatures_at_cast = sum(
                1 for _oid, _o in (state.get("objects") or {}).items()
                if str(_o.get("zone") or "").lower() == "battlefield"
                and "Creature" in ((_o.get("card_types") or {}).get("core_types") or []))
            g.say(f"P0 main phase turn {turn}: exporting PRE_CAST "
                  f"(Vanguard {g.pt_at_cast[0]}/{g.pt_at_cast[1]}), casting VIM")
            g.pre_cast = await g.export("pre_cast", g.p0)
            g.cast_turn = turn
            if await cast_named(c, state, acts, VIM, oid, "P0", g,
                                "vim_cast"):
                g.vim_cast_attempted = True
                g.note(f"P0 cast Vanish into Memory targeting Vanguard "
                       f"(turn {turn}, {len(lands)} lands)")
                answered = True
                return
    # --- cast confirmation drives the resolution window
    if g.vim_cast_attempted and not g.vim_cast_confirmed:
        if stack_has_spell(state, VIM):
            g.vim_cast_confirmed = True
            g.resolving = True
            g.say("VIM on the stack; resolution window open")
    if g.resolving and stack_has_spell(state, VIM):
        g.stack_was_seen = True
    # --- post_cast export once VIM resolved
    if g.resolving and g.stack_was_seen and g.post_cast is None:
        if not stack_has_spell(state, VIM):
            g.post_cast = await g.export("post_cast", g.p0)
            g.resolving = False
            g.say("VIM resolved; exported POST_CAST")
            return
    # --- pre_upkeep export at P0's next turn (cast_turn + 2), before the
    # delayed trigger resolves
    if (g.post_cast is not None and g.pre_upkeep is None
            and g.cast_turn is not None and turn == g.cast_turn + 2
            and state.get("active_player") == 0
            and phase in ("Untap", "Upkeep")):
        g.pre_upkeep = await g.export("pre_upkeep", g.p0)
        g.upkeep_window = True
        tough = (g.pt_at_cast or (2, 1))[1]
        g.discard_expected = tough if tough and tough > 0 else 1
        g.say(f"exported PRE_UPKEEP (turn {turn}); upkeep window open; "
              f"expecting discard of {g.discard_expected}")
        return
    # --- post_upkeep export once the trigger resolved (Vanguard back) and
    # we're past the draw step
    if g.upkeep_window and g.pre_upkeep is not None and g.post_upkeep is None:
        vg_bf = bf_creature(state, 0, VANGUARD)
        if (vg_bf is not None and phase == "PreCombatMain"
                and not real_decision_pending(st)
                and not stack_has_spell(state, VIM)):
            g.post_upkeep = await g.export("post_upkeep", g.p0)
            g.upkeep_window = False
            g.done = True
            g.say("exported POST_UPKEEP; done")
            return
    # No land drops inside the upkeep window: the hand-size delta must
    # measure discards only (run-0310 lesson: a land drop polluted N_observed).
    if not g.upkeep_window:
        if await play_land_prefer(c, state, 0, acts, "P0", g):
            return
    if real_decision_pending(st) and not answered:
        describe_pending(st, "P0")
    if not holding and not real_decision_pending(st) and my_priority(acts):
        await pass_priority(c, st, acts, g)


async def p1_tick(c, st, acts, state, g):
    for rej in getattr(c, "rejections", []):
        say(f"[P1] REJECTION: {json.dumps(rej, default=str)[:300]}")
        wire("b9842_rejection", {"who": "P1", "rej": rej})
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
    if await play_land_prefer(c, state, 1, acts, "P1", g):
        return
    if real_decision_pending(st):
        describe_pending(st, "P1")
    if not real_decision_pending(st) and my_priority(acts):
        await pass_priority(c, st, acts, g)


async def run_game():
    g = VanishGame()
    await g.start()
    ok = await pump(g,
                    lambda c, st, acts, state: p0_tick(c, st, acts, state, g),
                    lambda c, st, acts, state: p1_tick(c, st, acts, state, g),
                    timeout_s=900, resolve_timeout_s=300)
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
                  f"stack={len(s.get('stack') or [])} vim_cast={g.vim_cast_attempted}")
        if g.vim_cast_attempted and not g.done and resolve_deadline is None:
            resolve_deadline = time.time() + (resolve_timeout_s or 300)
        if not g.vim_cast_attempted or g.done:
            resolve_deadline = None
        if resolve_deadline and time.time() > resolve_deadline:
            g.note("VIM cast but post_upkeep not reached in "
                   f"{resolve_timeout_s or 300}s; see wire log")
            return False
    g.note(f"global timeout ({timeout_s}s) hit")
    return False

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
    post = load("post_cast")
    pre_up = load("pre_upkeep")
    post_up = load("post_upkeep")
    if pre is not None:
        g.snaps["pre_cast"] = {"P0": snap(pre, 0), "P1": snap(pre, 1)}
    if post is not None:
        g.snaps["post_cast"] = {"P0": snap(post, 0), "P1": snap(post, 1)}
    if pre_up is not None:
        g.snaps["pre_upkeep"] = {"P0": snap(pre_up, 0), "P1": snap(pre_up, 1)}
    if post_up is not None:
        g.snaps["post_upkeep"] = {"P0": snap(post_up, 0), "P1": snap(post_up, 1)}

    def zone_of(state, name, zone):
        return any(str(o.get("base_name") or o.get("name") or "").lower() == name
                   and str(o.get("zone") or "").lower() == zone
                   for o in (state.get("objects") or {}).values())

    # ---- A1 setup ----
    if pre is not None:
        lands = [o for o in bf_oids(pre, 0) if is_land(get_obj(pre, o))]
        nplains = sum(1 for o in lands if obj_lname(pre, o) == PLAINS)
        nisland = sum(1 for o in lands if obj_lname(pre, o) == ISLAND)
        vg = bf_creature(pre, 0, VANGUARD)
        pt = g.pt_at_cast or (None, None)
        ok = (VIM in hand_lnames(pre, 0) and vg is not None
              and len(lands) >= 4 and nplains >= 1 and nisland >= 1
              and pt == (2, 1))
        a["A1_setup"] = "passed" if ok else "failed"
        n.append(f"A1 setup: vim_in_hand={VIM in hand_lnames(pre, 0)}, "
                 f"vanguard_on_bf={vg is not None}, lands={len(lands)} "
                 f"(plains={nplains}, island={nisland}), pt_at_cast={pt}: "
                 f"{'ok' if ok else 'SETUP FAILED'}")
    else:
        a["A1_setup"] = "failed"
        n.append("A1 setup: pre_cast missing -- cast never attempted")
    a1ok = a["A1_setup"] == "passed"

    # ---- A3 cast (reached the stack, targeted the Vanguard) ----
    # The engine may not offer a target prompt when there is a single legal
    # target (run-...-0250: no prompt; the Vanguard was exiled on resolution).
    # In that case the target is inferred from the exile outcome (A4).
    if getattr(g, "vim_cast_confirmed", False):
        if getattr(g, "target_answered", False) and \
                getattr(g, "target_chosen", None) == VANGUARD:
            a["A3_cast"] = "passed"
            n.append("A3 cast: passed -- VIM reached the stack; target prompt "
                     "answered with the Vanguard")
        elif not getattr(g, "target_prompt_seen", False) and \
                getattr(g, "creatures_at_cast", None) == 1:
            a["A3_cast"] = "passed"
            n.append("A3 cast: passed -- VIM reached the stack; no target "
                     "prompt was offered (single legal target on the "
                     "battlefield); the Vanguard was exiled on resolution "
                     "(see A4), so it was the target")
        else:
            a["A3_cast"] = "failed"
            n.append(f"A3 cast: FAILED -- VIM reached the stack but target "
                     f"evidence is ambiguous (prompt_seen="
                     f"{getattr(g, 'target_prompt_seen', None)}, answered="
                     f"{getattr(g, 'target_answered', None)}, "
                     f"creatures_at_cast={getattr(g, 'creatures_at_cast', None)})")
    elif getattr(g, "vim_cast_attempted", False):
        a["A3_cast"] = "failed"
        n.append("A3 cast: FAILED -- CastSpell submitted but VIM never "
                 "reached the stack.")
    else:
        a["A3_cast"] = "not-run"
        n.append("A3 cast: not-run (cast never attempted)")
    cast_ok = a["A3_cast"] == "passed"

    # ---- A4 exile + draw (on resolution) ----
    if pre is not None and post is not None and cast_ok:
        vg_exiled = zone_of(post, VANGUARD, "exile")
        vim_gy = in_gy(post, 0, VIM)
        h_pre = len(hand_ids(pre, 0))
        h_post = len(hand_ids(post, 0))
        l_pre = len(player_of(pre, 0).get("library", []) or [])
        l_post = len(player_of(post, 0).get("library", []) or [])
        # cast costs the spell (-1), draw adds power=2 -> net +1; library -2
        ok = (vg_exiled and vim_gy and (h_post - h_pre) == 1
              and (l_post - l_pre) == -2)
        a["A4_exile_draw"] = "passed" if ok else "failed"
        n.append(f"A4 exile+draw: {'passed' if ok else 'FAILED'} -- vanguard "
                 f"in exile: {vg_exiled}, vim in P0 gy: {vim_gy}, "
                 f"P0 hand {h_pre}->{h_post} (expect +1), library "
                 f"{l_pre}->{l_post} (expect -2)")
    elif cast_ok:
        a["A4_exile_draw"] = "failed"
        n.append("A4 exile+draw: FAILED -- post_cast missing")
    else:
        a["A4_exile_draw"] = "not-run"
        n.append("A4 exile+draw: not-run (cast not confirmed)")
    a4ok = a["A4_exile_draw"] == "passed"

    # ---- A5 return (delayed trigger at next upkeep) ----
    if pre_up is not None and post_up is not None:
        vg_exiled_pre = zone_of(pre_up, VANGUARD, "exile")
        vg_oid = bf_creature(post_up, 0, VANGUARD)
        ret = vg_oid is not None
        ctrl_ok = owner_ok = None
        if ret:
            o = get_obj(post_up, vg_oid)
            ctrl_ok = str(o.get("controller", "")) == "0"
            owner_ok = str(o.get("owner", o.get("controller", ""))) == "0"
        ok = vg_exiled_pre and ret and ctrl_ok and owner_ok
        a["A5_return"] = "passed" if ok else "failed"
        n.append(f"A5 return: {'passed' if ok else 'FAILED'} -- vanguard in "
                 f"exile at pre_upkeep: {vg_exiled_pre}, returned to BF: {ret}, "
                 f"controller=0: {ctrl_ok}, owner=0: {owner_ok}")
    elif cast_ok and a4ok:
        a["A5_return"] = "failed"
        n.append("A5 return: FAILED -- upkeep states missing (trigger window "
                 "never completed)")
    else:
        a["A5_return"] = "not-run"
        n.append("A5 return: not-run (resolution not reached)")
    a5ok = a["A5_return"] == "passed"

    # ---- A6 discard (THE reported bug) ----
    # N_observed = (H_pre_upkeep - H_post_upkeep) + 1 (the draw-step draw).
    # Expected: toughness = 1. Bug: 0 discards (no prompt / no discard).
    tough = (g.pt_at_cast or (2, 1))[1] or 1
    if pre_up is not None and post_up is not None and a5ok:
        h0 = len(hand_ids(pre_up, 0))
        h1 = len(hand_ids(post_up, 0))
        gy0 = len(player_of(pre_up, 0).get("graveyard", []) or [])
        gy1 = len(player_of(post_up, 0).get("graveyard", []) or [])
        n_obs = (h0 - h1) + 1
        gy_delta = gy1 - gy0
        prompts = len(g.discard_events)
        if n_obs == tough and gy_delta == tough:
            a["A6_discard"] = "passed"
            res = "passed"
        elif n_obs == 0 and gy_delta == 0:
            a["A6_discard"] = "failed"
            res = ("FAILED (= bug reproduced) -- 0 cards discarded; the "
                   "delayed trigger's Toughness discard amount resolved to 0")
        else:
            a["A6_discard"] = "failed"
            res = (f"FAILED (related failure) -- observed {n_obs} discards "
                   f"(expected {tough}); graveyard delta {gy_delta}")
        # where did the picked card(s) go? they must be in P0's graveyard
        picked = [nm for ev in g.discard_events for nm in ev.get("picked", [])]
        gy_names = [str((get_obj(post_up, o).get("base_name") or "")).lower()
                    for o in player_of(post_up, 0).get("graveyard", [])]
        misplaced = [nm for nm in picked if nm.lower() not in gy_names]
        zone_of_picked = {}
        for nm in misplaced:
            for oid, o in (post_up.get("objects") or {}).items():
                if str(o.get("base_name") or "").lower() == nm.lower() \
                        and str(o.get("zone") or "").lower() not in ("library", "hand"):
                    zone_of_picked[nm] = (str(o.get("zone")), str(oid))
                    break
        if misplaced:
            a["A6_discard"] = "failed"
            res = (f"FAILED (related failure) -- the discard prompt was "
                   f"answered (picked={picked}) but the card(s) did not "
                   f"reach the graveyard: {zone_of_picked}; "
                   f"N_observed={n_obs}, gy delta={gy_delta}")
        n.append(f"A6 discard: {res} -- P0 hand {h0}->{h1} (+1 draw step), "
                 f"N_observed={n_obs}, gy {gy0}->{gy1} (delta {gy_delta}), "
                 f"discard prompts answered: {prompts}, picked={picked}, "
                 f"misplaced={misplaced} {zone_of_picked}, toughness={tough}")
    elif a5ok:
        a["A6_discard"] = "failed"
        n.append("A6 discard: FAILED -- upkeep states missing")
    else:
        a["A6_discard"] = "not-run"
        n.append("A6 discard: not-run (return not established)")
    a6 = a["A6_discard"]

    # ---- A7 returned P/T ----
    if post_up is not None and a5ok:
        vg_oid = bf_creature(post_up, 0, VANGUARD)
        o = get_obj(post_up, vg_oid) if vg_oid is not None else {}
        p, t = pt_val(o, "power"), pt_val(o, "toughness")
        ok = (p, t) == (2, 1)
        a["A7_pt"] = "passed" if ok else "failed"
        n.append(f"A7 pt: {'passed' if ok else 'FAILED'} -- returned Vanguard "
                 f"is {p}/{t} (expected 2/1)")
    else:
        a["A7_pt"] = "not-run"
        n.append("A7 pt: not-run")

    # ---- A8 cleanup ----
    if post_up is not None:
        empty = len(post_up.get("stack") or []) == 0
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
    pe = parse_sum
    effects = pe.get("effects") or []
    types = pe.get("effect_types") or []

    def qty_of(eff_type, key="qty"):
        for f in effects:
            if f.get("type") == eff_type:
                return ((f.get("count") or {}).get(key) or {})
        return {}

    exile_ok = any(f.get("type") == "ChangeZone"
                   and f.get("destination") == "Exile"
                   and isinstance(f.get("target"), dict)
                   and "Creature" in json.dumps(f.get("target"))
                   for f in effects)
    draw_ok = qty_of("Draw").get("type") == "Power"
    trig_ok = any(f.get("type") == "CreateDelayedTrigger" for f in effects)
    disc_qty = qty_of("Discard")
    disc_ok = disc_qty.get("type") == "Toughness"
    ok = exile_ok and draw_ok and trig_ok and disc_ok
    ass["A2_parse"] = "passed" if ok else "failed"
    notes.append(
        f"A2 parse Vanish into Memory (pinned v0.108.0): ability_kind="
        f"{pe['ability_kind']}, effect_types={types}, "
        f"exile_creature={exile_ok}, draw_power={draw_ok}, "
        f"delayed_trigger={trig_ok}, discard_qty={json.dumps(disc_qty)[:70]}: "
        f"{'lowering matches oracle' if ok else 'PARSE SHAPE UNEXPECTED'}")

    a, n = eval_game(g)
    ass.update(a)
    notes.extend(n)

    # ---- verdict ----
    a1 = a.get("A1_setup") == "passed"
    a3 = a.get("A3_cast")
    a4 = a.get("A4_exile_draw")
    a5 = a.get("A5_return")
    a6 = a.get("A6_discard")
    if not a1:
        verdict = "blocked"
        notes.append("setup never completed; verdict blocked")
    elif a3 != "passed":
        verdict = "blocked"
        notes.append("VIM never reached the stack targeting the Vanguard; "
                     "the reported discard path is untestable -- blocked")
    elif a4 != "passed":
        verdict = "blocked"
        notes.append("resolution did not exile the Vanguard / draw power; "
                     "the upkeep trigger is unreachable -- blocked")
    elif a5 != "passed":
        verdict = "reproduced"
        notes.append("reproduced (related failure): the delayed trigger did "
                     "not return the Vanguard at P0's next upkeep -- the "
                     "return plumbing the discard depends on is broken")
    elif a6 == "failed":
        verdict = "reproduced"
        notes.append("reproduced: after the Vanguard returned at P0's next "
                     "upkeep, the discard clause discarded 0 cards instead "
                     "of toughness (1) -- exactly the reported bug")
    elif a6 == "passed":
        flow_ok = all(a.get(k) == "passed" for k in ("A7_pt", "A8_cleanup"))
        if flow_ok:
            verdict = "not-reproduced"
            notes.append("not-reproduced: Vanguard returned at next upkeep "
                         "and P0 discarded exactly toughness (1) on v0.108.0")
        else:
            verdict = "reproduced"
            notes.append("reproduced (related failure): discard count "
                         "correct but post state inconsistent -- see "
                         "A7/A8")
    else:
        verdict = "blocked"
        notes.append("upkeep window never completed; verdict blocked")
    notes.extend(g.notes)
    return ass, notes, verdict

# ---------------------------------------------------------------- render
def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 920
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    sv = run["server"]
    y = 20
    d.text((24, y), "Issue #9842 - Vanish into Memory delayed-trigger "
                     "discard (first validation)", fill=(235, 240, 250)); y += 28
    d.text((24, y), f"server {sv['server_version']} ({sv['build_commit']}) "
                     f"protocol {sv['protocol_version']} - run {run['run_id']} "
                     f"- 2026-10-13", fill=(140, 160, 180)); y += 26
    vc = {"reproduced": (255, 90, 90), "not-reproduced": (120, 220, 120),
          "blocked": (230, 200, 90)}.get(run["verdict"], (200, 200, 200))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vc); y += 32
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225)); y += 24
    labels = [
        ("A1_setup", "A1 setup: VIM in P0 hand, Vanguard 2/1 on BF, 4 lands (W+U)"),
        ("A2_parse", "A2 parse: VIM lowering (Exile; Draw Power; Upkeep trigger; Discard Toughness)"),
        ("A3_cast", "A3 cast: VIM reached the stack targeting the Vanguard"),
        ("A4_exile_draw", "A4 on resolution: Vanguard exiled, P0 drew 2 (= power)"),
        ("A5_return", "A5 next upkeep: Vanguard returned to BF under P0"),
        ("A6_discard", "A6 [the bug] P0 discarded 1 (= toughness); FAIL = reproduced"),
        ("A7_pt", "A7 returned Vanguard still 2/1"),
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
    d.text((24, y), "Discard events (upkeep window):",
           fill=(200, 210, 225)); y += 22
    for ev in obs.get("discard_events", [])[:4]:
        d.text((40, y), (f"n_requested={ev['n_requested']} "
                         f"candidates={ev['n_candidates']} "
                         f"picked={ev['picked']}")[:118],
               fill=(150, 165, 185)); y += 19
    if not obs.get("discard_events"):
        d.text((40, y), "(no discard prompt answered -- 0 discards if "
                        "A6 failed)", fill=(255, 150, 90)); y += 19
    y += 4
    d.text((24, y), "P0 hand/library/gy (from saved authoritative states):",
           fill=(200, 210, 225)); y += 22
    for nm in ("pre_cast", "post_cast", "pre_upkeep", "post_upkeep"):
        s = obs["snaps"].get(nm)
        if s:
            d.text((40, y), f"{nm}: P0 hand/lib/gy = {s['P0']}",
                   fill=(150, 165, 185)); y += 19
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 9842/" + run["run_id"],
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
    say("parse summary (vanish_into_memory): "
        + json.dumps({"ability_kind": pe["ability_kind"],
                      "effect_types": pe.get("effect_types")},
                     default=str)[:600])

    g, ok = await run_game()
    g.say(f"game finished ok={ok}")
    await g.close()

    if g.pre_upkeep is not None and g.post_upkeep is None:
        try:
            g.post_upkeep = await g.export("post_upkeep", g.p0)
            g.note("post_upkeep exported at loop end")
        except Exception as e:
            g.note(f"post_upkeep final export failed: {e}")

    ass, notes, verdict = evaluate(g, parse_sum)
    dur = time.time() - t_start
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": "runs/backfill-20261013-0241",
        "parse_summary": parse_sum,
        "driver": {"protocol_advertised": 134, "client": "driver/client.py",
                   "scenario": "driver/scenario_9842_01080.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_9842_01080.py", "rb").read()).hexdigest(),
        "decks": {
            "P0": [["vanish into memory", 8], ["elite vanguard", 8],
                   ["island", 22], ["plains", 22]],
            "P1": [["island", 60]],
        },
        "games": {"vanish_into_memory": {"ok": ok, "notes": g.notes}},
        "observations": {
            "snaps": g.snaps,
            "target_answered": g.target_answered,
            "target_chosen": g.target_chosen,
            "cast_turn": g.cast_turn,
            "pt_at_cast": g.pt_at_cast,
            "vanguard_oid": g.vanguard_oid,
            "discard_events": g.discard_events,
            "discard_expected": g.discard_expected,
            "upkeep_vi_log": g.upkeep_vi_log,
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Not tested on the original build; verdict is scoped to v0.108.0, "
            "not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
            "The delayed trigger's 'your next upkeep' resolves to the spell "
            "controller's next turn by construction (P0 is both); a control "
            "with the trigger owned by another player was not run.",
        ],
        "setup_line": "P0 8x vanish into memory + 8x elite vanguard + 22 island + "
                      "22 plains vs P1 60 island (P0 casts Elite Vanguard once a Plains is out, "
                      "Vanish into Memory targeting it on turn 3+ with 4 lands).",
        "contract_line": "Per the issue's Expected: exile the creature, draw "
                         "cards equal to its power (2); at the beginning of "
                         "P0's next upkeep return it to the battlefield under "
                         "its owner's control; if it returned, discard cards "
                         "equal to its toughness (1). The reported bug is "
                         "that the discard amount resolves to 0.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_9842_01080.py",
                f"{EVDIR}/scenario_9842_01080.py")
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
    pidfile = "/tmp/scenario_9842_01080.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit("another scenario_9842_01080 instance is alive "
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
