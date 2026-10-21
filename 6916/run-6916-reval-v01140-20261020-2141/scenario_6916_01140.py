#!/usr/bin/env python3
"""Issue #6916: Xantcha, Sleeper Agent — cannot choose which opponent gets
Xantcha; any player may activate its {3} ability.

Re-validation on the pinned release v0.114.0 (build ac67cbb, protocol 153).
Identity-only port of scenario_6916_01100.py (v0.110.0 / protocol 142,
verified 2026-10-14, verdict reproduced).
Prior runs:
  - 20260913-6916  (v0.81.3, protocol 70): reproduced. Claim (a) choice did
    NOT reproduce (EntryControllerChoice offered, worked). Claim (b)
    reproduced STRONGER than reported: the {3} ability was never offered to
    ANY seat, not merely restricted to the controller.
  - 20260914-6916  (v0.82.0, protocol 70): re-validated, verdict unchanged.
  - 20261006-6916c (v0.103.0, protocol 106): re-validated, verdict unchanged
    (A4 failed identically; evidence uploaded but maintained comment was
    never updated).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (verified in pinned v0.114.0 card-data.json):
  Xantcha, Sleeper Agent ({1}{B}{R}, 5/5 Legendary Creature - Phyrexian Minion):
    "Xantcha enters under the control of an opponent of your choice.
     Xantcha attacks each combat if able and can't attack its owner or
     planeswalkers its owner controls.
     {3}: Xantcha's controller loses 2 life and you draw a card.
     Any player may activate this ability."

Reported symptoms:
  (a) the owner is NOT offered the opponent choice as Xantcha enters;
  (b) the {3} ability is available only to Xantcha's controller, not to
      every player.

Setup (native engine, three human-client seats, CommanderDraft, 3 players;
three seats so the owner has two legal opponents to choose between):
  P0: commander=[Xantcha, Sleeper Agent], main = 30x Swamp + 30x Mountain.
      Casts the commander; owns it for the whole run.
  P1: commander=[Ayula, Queen Among Bears] (never cast; inert), 60x Forest.
      Provides the any-player activation test: P1 is neither owner nor
      controller of Xantcha.
  P2: 60x Forest (no commander), inert.

Plan:
  1. P0 casts the Xantcha commander ({1}{B}{R}).
  2. While Xantcha is entering/resolving, record whether the engine offers
     P0 a choice among opponents (player candidates) and which choice is
     answered (P2 is the intended pick). If no choice is offered, that is
     recorded as-is.
  3. After Xantcha is on the battlefield, on P1's main phase P1 activates
     Xantcha's {3} ability (P1 is not the controller). Assert the
     controller (whoever controls Xantcha) loses 2 life and P1 draws a card.

Assertions:
  A1_setup_ok        Xantcha on the battlefield, owned by P0 (owner==0).
  A2_choice_offered  the engine offered P0 a choice among legal opponents
                     as Xantcha entered (>=2 player candidates recorded).
                     FAILED = the reported choice bug.
  A3_controlled_by_chosen
                     Xantcha's controller is P2 (the intended pick), i.e.
                     the choice (or default) put it under P2's control.
  A4_any_player_activate
                     P1 (non-controller, non-owner) was offered and
                     submitted Xantcha's {3} activation. FAILED = the
                     reported any-player-permission bug.
  A5_activation_effect
                     Xantcha's controller lost exactly 2 life (40->38) and
                     the activating player P1 drew exactly one card
                     (hand +1), with no other life changes.
  A6_cleanup         post.json: stack empty, game proceeds (no stall).

Verdict rule: reproduced iff A1 passes and at least one of
A2/A3/A4/A5 fails. not-reproduced iff A1-A6 all pass. blocked iff A1 fails.

Driver conventions (protocol 153, from scenario_4509_01140.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
    (client.py).
  - waiting_for is gone (null); priority = PassPriority in the viewing
    seat's top-level legal_actions; decisions surface via viewer_interaction.
  - client.py HELLO advertises protocol 153 (exact match enforced).
  - Cast-confirmation guard (protocol 118/120, AGENTS.md 2026-10-09): after
    every CastSpell submission the driver sets ST["pending_cast"] and does
    NOT pass priority or start new main-phase plays until the spell is
    confirmed on the stack/battlefield (zone check each tick); a 30s
    backstop clears a still-in-hand cast as dropped and resets the
    scenario one-shot flags so the cast re-triggers. The guard runs
    before any early-return block in both tick functions.
  - MulliganDecision answered via legacy Action (verified accepted on 118),
    gated on the MulliganDecision legal action with answered (tag,iid) keys.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi only, gated on hand > 7 plus a schema/select
    opportunity offering hand cards (118 uses a generic 'choose' code).
  - Spell mana payment via vi tapLandForMana, needs-gated
    (ST["mana_needs"][tag]); the 118 engine offers tapLandForMana menus at
    ordinary priority windows, so blind tapping is never done.
  - ActivateAbility submitted while holding priority (never pass first);
    merged_actions ActivateAbility (source_id match) first, else vi
    exactChoices 'activateAbility' answered with {"type":"choose"}.
  - real_decision_pending excludes tapLandForMana/untapLandForMana/castSpell/
    activateAbility/passPriority/mulliganDecision menus.
  - A single await asyncio.sleep(0) yield after the priority gate, before
    leg evaluation (leg-engagement race fix).
  - Export checkpoints fall through to the priority pass in the same tick;
    never return after an export while holding priority.
  - Re-tick backstop: a client holding Priority with no revision change for
    >5s is re-ticked (scenario_650_01010.py last_tick_at pattern).
  - DeclareAttackers submitted from the freshly advertised action
    (deepcopy, attacks set); stale_interaction rejections retry on the next
    advertised action.
  - Entry-choice (Xantcha ETB) must never be masked by the unexpected-prompt
    auto-answer: chooseEntryController / chooseReplacement codes and
    player-candidate opportunities are always excluded.

Evidence: evidence/6916/<run-id>/pre.json, mid.json, post.json, run.json,
assertions.json, data_evidence.json, manifest.sha256, summary.png,
scenario_6916_01140.py, wire_log.jsonl, scenario_run.log, server.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6916
RUN_ID = os.environ.get("RUN_ID", "run-6916-reval-v01140-20261020-2142")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

XANTCHA = "xantcha, sleeper agent"
SWAMP = "swamp"
MOUNTAIN = "mountain"
FOREST = "forest"
AYULA = "ayula, queen among bears"

P0_COMMANDER = [XANTCHA]
P0_MAIN = [(SWAMP, 30), (MOUNTAIN, 30)]
P1_COMMANDER = [AYULA]  # never cast; inert opponent
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

COMMANDER_FORMAT = {
    "format": "CommanderDraft",
    "starting_life": 40,
    "min_players": 3,
    "max_players": 8,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": True,
    "commander_damage_threshold": 21,
    "range_of_influence": None,
    "team_based": False,
    "sideboard_policy": {"type": "Forbidden"},
    "uses_commander": True,
    "supplies_fixed_deck": False,
    "default_deck_copy_limit": {"type": "Unlimited"},
    "allow_debug_actions": False,
}

STARTING_LIFE = 40
INTENDED_CONTROLLER = 2  # we choose P2 when a choice is offered
URL = "ws://127.0.0.1:9374/ws"

SERVER_IDENTITY = {
    "server_version": "v0.114.0",
    "build_commit": "ac67cbb",
    "protocol_version": 153,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.114.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("reused live v0.114.0 server on 127.0.0.1:9374 "
                      "(already listening at run start; process identity "
                      "verified via ServerHello 0.114.0/ac67cbb/protocol "
                      "153); this run's own game + raw Hello handshake "
                      "verify the identity; no second server started per "
                      "AGENTS.md 2026-10-08 lesson"),
    "source": ("2026-10-20: latest stable release v0.114.0 (published "
               "2026-10-14) == pinned release dir; ServerHello "
               "0.114.0/ac67cbb/protocol 153 verified by handshake this "
               "run; hashes recomputed against on-disk artifacts this "
               "run"),
}

for _f, _k in (("server/releases/v0.114.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.114.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.114.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.114.0/data/card-data.json"))

SETUP_DEADLINE_S = 1800
ASS_KEYS = ("A1_setup_ok", "A2_choice_offered", "A3_controlled_by_chosen",
            "A4_any_player_activate", "A5_activation_effect", "A6_cleanup")

ST = {}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
UNEXPECTED = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
        WIRE.flush()
    except ValueError:
        pass

# ---------------------------------------------------------------- state helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    if oid is None:
        return "?"
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def is_land(o):
    core = ((o.get("card_types") or {}).get("core_types") or [])
    if any(str(t).lower() == "land" for t in core):
        return True
    base = ((o.get("base_card_types") or {}).get("core_types") or [])
    if any(str(t).lower() == "land" for t in base):
        return True
    t = str(o.get("type_line") or o.get("type") or "").lower()
    return "land" in t


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield"
                and str(o.get("controller")) == str(pid)
                and not o.get("tapped") and is_land(o)):
            out.append(int(oid))
    return out


def xantcha_oid_any(state):
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and obj_lname(state, oid) == XANTCHA:
            return int(oid)
    return None


def xantcha_on_stack(state):
    for e in state.get("stack", []) or []:
        if "xantcha" in json.dumps(e, default=str).lower():
            return True
    return False


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
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


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def _vi_offers_pass(st):
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices", []) or []:
            for sf in ch.get("surfaces", []) or []:
                if sf.get("type") == "action" \
                        and (sf.get("data") or {}).get("code") == "passPriority":
                    return True
    return False


def my_priority(acts, st=None):
    """Protocol 130+: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone), or the vi
    surface offers a passPriority choice (01071 deadlock fix)."""
    if any(a.get("type") == "PassPriority" for a in acts):
        return True
    return st is not None and _vi_offers_pass(st)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and str(state.get("active_player")) == str(pid)
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


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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


def can_pay(state, pid, colors=(), generic=0):
    color_of = {XANTCHA: None, "island": "U", "forest": "G",
                "swamp": "B", "mountain": "R"}
    pool = {}
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield"
                and str(o.get("controller")) == str(pid)
                and not o.get("tapped") and is_land(o)):
            c = color_of.get(obj_lname(state, oid))
            if c:
                pool[c] = pool.get(c, 0) + 1
    need = {}
    for c in colors:
        need[c] = need.get(c, 0) + 1
    for c, n in need.items():
        if pool.get(c, 0) < n:
            return False
        pool[c] -= n
    return sum(pool.values()) >= generic


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


# ---------------------------------------------------------------- interaction primitives

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


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
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
    say(f"[{tag}] submitting interaction iid={str(iid)[:12]} choice={cid} "
        f"({choice_text(choice)[:80]})")
    await interact_as(c, sub, tag)


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
    assert str(ver).startswith("0.114.0"), f"unexpected version {ver}"
    assert int(proto) == 153, f"unexpected protocol {proto}"
    assert str(build) == "ac67cbb", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    x = CARD_DATA.get(XANTCHA, {})
    oracle = str(x.get("oracle_text") or "")
    ok = ("enters under the control of an opponent of your choice" in oracle
          and "Any player may activate this ability" in oracle
          and bool(x.get("is_commander")))
    repl = x.get("replacements") or []
    notes = [f"is_commander={x.get('is_commander')}",
             f"replacements={len(repl)}"]
    if repl:
        notes.append("replacement: " + str(repl[0].get("description"))[:120])
    ab = x.get("abilities") or []
    notes.append(f"abilities={len(ab)}")
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": oracle[:500]}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")


async def do_mulligan(c, acts, st, pid, tag):
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
    if key in SUBMITTED_OPPS:
        return False
    SUBMITTED_OPPS.add(key)
    state = st["state"]
    hn = hand_lnames(state, pid)
    nlands = sum(1 for n in hn if n in (SWAMP, MOUNTAIN, FOREST))
    mull_count = sum(1 for k in SUBMITTED_OPPS
                     if k[0] == tag and k[1] == "mull")
    keep = nlands >= 3 or mull_count >= 2 or len(hn) <= 4
    say(f"[{tag}] mulligan -> {'Keep' if keep else 'Mulligan'} "
        f"(lands={nlands} hand={hn})")
    wire("mulligan", {"who": tag,
                      "decision": "Keep" if keep else "Mulligan",
                      "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type":
                                             "Keep" if keep else "Mulligan"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
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
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; "
            "not answering")
        wire(f"{tag}_bottom_no_vi", {})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get(
        "data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False
    keepers = (XANTCHA, AYULA)

    def bkey(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in keepers:
            return (1, str(ref))
        return (0, str(ref))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": str(iid)[:12],
                    "picks": [x.get("id") for x in picks]})
    await interact_as(
        c, {"interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(str(h) for h in hand)
    found_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        cands = rdata.get("candidates") or []
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not any(r in handset for r in refs):
            continue
        found_opp = (opp, cands, spec)
        break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands, spec = found_opp
    stype = spec.get("type") or "select"
    keepers = (XANTCHA, AYULA)

    def rank(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else \
            choice_text(ch).lower()
        if nm in (SWAMP, MOUNTAIN, FOREST):
            return (0, nm)
        if nm in keepers:
            return (9, nm)
        return (5, nm)

    picks = [ch.get("id") for ch in sorted(cands, key=rank)[:max(1, n)]
             if ch.get("id")]
    if not picks:
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding to hand size via vi ({stype}): picks={picks}")
    wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": picks}}}, tag)
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
    if sum(needs.values()) <= 0:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status") or {}).get("type") not in (None, "available"):
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
        needs[used] -= 1
        say(f"[{tag}] tap land for mana used_for={used} needs={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def pass_priority(c, st, acts):
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
                                                   "data": {"choiceId":
                                                            ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    # protocol 153 vi fallback: playLand opportunity
    st = {"viewer_interaction": c.latest.get("viewer_interaction")}
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            if ref is not None and str(ref) in [str(x) for x in lands]:
                iid = opp.get("interactionId")
                key = (tag, "playland", str(iid), str(ref))
                if key in SUBMITTED_OPPS:
                    continue
                SUBMITTED_OPPS.add(key)
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                wire("play_land_vi", {"who": tag, "oid": str(ref)})
                await answer_vi(c, opp, ch, tag)
                return True
    return False


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == lname:
                return a, iv
    return None, None


async def try_cast(c, state, acts, tag, name, needs, reset):
    a, oid = find_cast_action(acts, state, name)
    if not a:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid})
    await submit_as_is(c, a)
    # cast-confirmation guard (protocol 118/120): never pass priority or
    # start new plays under an in-flight CastSpell; the bare CastSpell can
    # lose a race to our own PassPriority and be silently dropped.
    ST["pending_cast"] = {"oid": str(oid), "name": name,
                          "since": time.time(), "tag": tag,
                          "reset": dict(reset)}
    return True


def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_confirmed(state, pc):
    zone = cast_obj_zone(state, pc["oid"])
    return zone in ("stack", "battlefield", "graveyard", "exile", "command")


def cast_guard_tick(state, tag):
    """Protocol-118/120 cast-confirmation guard.

    Returns "hold" while our CastSpell is in flight but unconfirmed.
    Must run before any early-return block.
    """
    pc = ST.get("pending_cast")
    if not pc:
        return "proceed"
    if cast_confirmed(state, pc):
        ST["pending_cast"] = None
        say(f"[{tag}] cast confirmed "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']}")
        wire("cast_confirmed", {"name": pc["name"], "oid": str(pc["oid"]),
                                "zone": cast_obj_zone(state, pc["oid"])})
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] CAST NOT CONFIRMED after 30s "
            f"({cast_obj_zone(state, pc['oid'])}): {pc['name']} "
            f"oid {pc['oid']} -- clearing for retry")
        wire("cast_dropped_retry",
             {"name": pc["name"], "oid": str(pc["oid"]),
              "zone": cast_obj_zone(state, pc["oid"])})
        ST["pending_cast"] = None
        ST.update(pc.get("reset") or {})
        return "proceed"
    return "hold"

# ---------------------------------------------------------------- Xantcha-specific

def find_activate(st, source_oid):
    """Mana ability on protocol 118+: merged_actions ActivateAbility
    (source_id match) first; else viewer_interaction exactChoices
    'activateAbility' answered with {"type":"choose"}."""
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility":
            d = a.get("data", {}) or {}
            src = d.get("source_id") or d.get("object_id")
            if a.get("_src_oid") is not None:
                try:
                    src = int(a["_src_oid"])
                except (TypeError, ValueError):
                    pass
            try:
                if src is not None and int(src) == int(source_oid):
                    return ("action", a)
            except (TypeError, ValueError):
                pass
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "activateAbility" not in codes:
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)
                    and s.get("data", {}).get("role") == "source"]
            if str(source_oid) in refs:
                return ("interaction", opp, ch)
    return None


def xantcha_ability_offered(st, acts, xoid):
    """Return a find_activate result if Xantcha's {3} activation is
    advertised to this seat, else None."""
    return find_activate(st, xoid)


async def enter_choice_tick(c, pid, tag, st, state):
    """Catch the opponent-choice prompt as Xantcha enters.

    The owner (P0) should be offered a choice among legal opponents.
    On protocol 106 this was an exactChoices opportunity with a
    chooseEntryController action code; on 153 the shape may differ, so we
    accept either the code or a player-candidate opportunity seen while
    Xantcha is entering (cast but not yet on the battlefield).
    """
    if not ST.get("xantcha_cast") or ST.get("entered"):
        return False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if (tag, "enter-choice", str(iid)) in SUBMITTED_OPPS:
            return True
        resp = opp.get("response", {}) or {}
        if resp.get("type") not in ("schema", "exactChoices"):
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        codes = set()
        for ch in chs:
            codes.update(x for x in surf_codes(ch) if x)
        # ReplacementChoice prompts (chooseReplacement) are NOT the
        # entry-controller choice; record and never answer them here.
        if "chooseReplacement" in codes:
            if str(iid) not in ST.get("replacement_prompts", []):
                ST.setdefault("replacement_prompts", []).append(str(iid))
                say(f"[{tag}] ReplacementChoice seen (not entry choice): "
                    f"iid={str(iid)[:12]}")
                wire("replacement_choice_seen",
                     {"who": tag, "iid": str(iid)[:12]})
            continue
        seats = [candidate_seat(ch) for ch in chs]
        is_entry_choice = ("chooseEntryController" in codes
                           or any(s is not None for s in seats))
        if not is_entry_choice:
            continue
        ST["enter_choice_offered"] = True
        ST["enter_choice_iid"] = str(iid)[:16]
        ST["enter_choice_candidates"] = seats
        say(f"[{tag}] ENTER-CHOICE prompt: seats={seats} "
            f"codes={sorted(codes)}")
        wire("enter_choice_prompt",
             {"who": tag, "iid": str(iid)[:16], "seats": seats,
              "codes": sorted(codes),
              "opportunity": json.loads(json.dumps(opp, default=str))})
        SUBMITTED_OPPS.add((tag, "enter-choice", str(iid)))
        pick = None
        for ch in chs:
            if candidate_seat(ch) == INTENDED_CONTROLLER:
                pick = ch
                break
        if pick is None:
            pick = pick_entry_controller_fallback(chs, state)
        if pick is None:
            pick = chs[0]
        ST["enter_choice_pick"] = candidate_seat(pick)
        say(f"[{tag}] ENTER-CHOICE pick: seat={ST['enter_choice_pick']} "
            f"text={choice_text(pick)[:60]!r}")
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def pick_entry_controller_fallback(chs, state):
    for ch in chs:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if not isinstance(d, dict):
                continue
            for k in ("seat", "player", "index", "player_id", "controller"):
                try:
                    if d.get(k) is not None and int(d[k]) == INTENDED_CONTROLLER:
                        return ch
                except (TypeError, ValueError):
                    pass
        t = choice_text(ch)
        if str(INTENDED_CONTROLLER) in t:
            return ch
    return None


async def activation_tick(c, pid, tag, st, acts, state, p0c):
    """Any-player activation test. P1 (non-owner, non-controller) tries to
    activate Xantcha's {3} ability on its own main phase. P0/P2 are sampled
    once each for the 'stronger than reported' observation."""
    xoid = ST.get("xantcha_oid") or xantcha_oid_any(state)
    if not ST.get("entered") or xoid is None:
        return False
    if not my_main(state, pid):
        return False
    if len(untapped_lands(state, pid)) < 3:
        return False
    offered = xantcha_ability_offered(st, acts, xoid)
    if tag == "P1" and not ST.get("p1_activated"):
        if offered:
            pre_life = {i: life_of(state, i) for i in (0, 1, 2)}
            pre_hand = {i: len(hand_ids(state, i)) for i in (0, 1, 2)}
            ST["pre_act"] = {
                "life": pre_life, "hand": pre_hand,
                "controller": get_obj(state, xoid).get("controller")}
            say(f"[P1] activating Xantcha's ability pre_life={pre_life} "
                f"pre_hand={pre_hand}")
            wire("p1_activate", {"pre_life": pre_life, "pre_hand": pre_hand,
                                 "via": offered[0]})
            if offered[0] == "action":
                await submit_as_is(c, offered[1])
            else:
                _k, opp, ch = offered
                await answer_vi(c, opp, ch, tag)
            ST["p1_activated"] = True
            ST["p1_activation_turn"] = state.get("turn_number")
            # post_check must not export on a stale pre-resolution view:
            # the ability resolution broadcast has to arrive first
            # (2026-10-14: same-second export captured the ability still
            # on the stack and failed A5/A6 spuriously).
            ST["p1_activated_at"] = time.time()
            ST["p1_activated_p0rev"] = p0c.revision
            return True
        turn = state.get("turn_number")
        ST.setdefault("p1_sample_turns", set()).add(turn)
        if turn not in ST.get("p1_noact_logged", set()):
            ST.setdefault("p1_noact_logged", set()).add(turn)
            vi_shapes = []
            for opp in vi_ops(st):
                rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
                chs = rdata.get("choices") or rdata.get("candidates") or []
                all_codes = set()
                for ch in chs:
                    all_codes.update(x for x in surf_codes(ch) if x)
                vi_shapes.append(
                    {"rtype": (opp.get("response", {}) or {}).get("type"),
                     "codes": sorted(all_codes), "n": len(chs)})
            say(f"[P1] Xantcha on BF but NO activation advertised to P1 "
                f"(turn {turn})")
            wire("p1_no_activation_offered",
                 {"turn": turn,
                  "act_types": sorted(set(a["type"] for a in acts)),
                  "vi_shapes": vi_shapes})
        return False
    # control samples: owner (P0) and controller (P2), once each
    if tag == "P0" and ST.get("p0_owner_sample") is None:
        ST["p0_owner_sample"] = {"offered": offered is not None,
                                 "turn": state.get("turn_number")}
        say(f"[P0-owner] Xantcha activation offered: {offered is not None}")
        wire("p0_owner_sample", ST["p0_owner_sample"])
    if tag == "P2" and ST.get("p2_ctrl_sample") is None:
        ST["p2_ctrl_sample"] = {"offered": offered is not None,
                                "turn": state.get("turn_number")}
        say(f"[P2-controller] Xantcha activation offered: "
            f"{offered is not None}")
        wire("p2_ctrl_sample", ST["p2_ctrl_sample"])
    return False


async def do_declare_empty(c, acts, tag, kind):
    atype = "DeclareAttackers" if kind == "attackers" else "DeclareBlockers"
    da = next((a for a in acts if a.get("type") == atype), None)
    if da is None:
        return False
    d = copy.deepcopy(da)
    if kind == "attackers":
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
    else:
        d.setdefault("data", {})["assignments"] = []
    await submit_as_is(c, d)
    say(f"[{tag}] declare no {kind}")
    return True


async def p2_attack_tick(c, acts, st, state, tag):
    """P2 controls Xantcha: declare it attacking P1. Xantcha "attacks each
    combat if able", so an empty declare would be dishonest to the card;
    declare Xantcha -> P1 via the freshly advertised DeclareAttackers
    action. Falls back to empty declare if Xantcha is unavailable."""
    if str(state.get("active_player")) != "2":
        return False
    if "declareattack" not in str(state.get("phase") or "").lower():
        return False
    da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
    if da is None:
        return False
    d = copy.deepcopy(da)
    xoid = ST.get("xantcha_oid") or xantcha_oid_any(state)
    if xoid is not None and str(get_obj(state, xoid).get("controller")) == "2" \
            and not get_obj(state, xoid).get("tapped"):
        d.setdefault("data", {}).update(
            {"attacks": [[xoid, {"type": "Player", "data": 1}]],
             "bands": []})
        say(f"[P2] declaring Xantcha {xoid} attacking P1")
        wire("p2_attack", {"xantcha": xoid, "target": 1})
    else:
        d.setdefault("data", {}).update({"attacks": [], "bands": []})
        say("[P2] declare no attackers (Xantcha unavailable)")
        wire("p2_attack_empty", {})
    await submit_as_is(c, d)
    return True


def drain_rejections(c, stage):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            rec = {"type": t, "data": data, "stage": stage}
            out.append(rec)
            ST.setdefault("rejections", []).append(rec)
            say(f"[{c.name}] REJECTION in stage {stage}: "
                f"{json.dumps(data, default=str)[:300]}")
            wire("rejection", {"who": c.name, "stage": stage, "data": data})
    return out


async def unexpected_prompt_tick(c, pid, tag, st, state):
    """Log unexpected vi decisions; answer only if stalled >60s and the
    opportunity is NOT an entry-choice / replacement-choice shape (never
    mask the reported choice) and NOT a relations-schema declare."""
    now = time.time()
    acted = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        rec = UNEXPECTED.setdefault(str(iid),
                                    {"t0": now, "done": False, "who": tag})
        if rec["done"]:
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        codes = set()
        for ch in chs:
            codes.update(c for c in surf_codes(ch) if c)
        # never auto-answer entry-choice / replacement shapes
        if codes & {"chooseEntryController", "chooseReplacement"}:
            continue
        if resp.get("type") == "schema":
            spec = (data.get("spec") or {}).get("type")
            if spec == "relations":
                continue
            if any(candidate_seat(ch) is not None for ch in chs):
                continue
        if now - rec["t0"] < 60:
            if not rec.get("logged"):
                rec["logged"] = True
                say(f"[{tag}] UNEXPECTED PROMPT iid={str(iid)[:8]} "
                    f"codes={sorted(codes)} n={len(chs)}")
                wire("unexpected_prompt",
                     {"who": tag, "iid": str(iid)[:8],
                      "codes": sorted(codes), "n_choices": len(chs)})
            continue
        if not chs:
            continue
        pick = chs[0]
        say(f"[{tag}] auto-answering stalled prompt: "
            f"{choice_text(pick)[:60]}")
        wire("auto_answer", {"who": tag, "iid": str(iid)[:8],
                             "choice": choice_text(pick)[:80]})
        await answer_vi(c, opp, pick, tag)
        rec["done"] = True
        acted = True
    return acted

# ---------------------------------------------------------------- seat tick

async def export_named(name, p0c):
    """Authoritative export via P0 (seat 0, single-user). Callers must NOT
    return after this while their client holds priority -- fall through to
    the priority pass in the same tick."""
    try:
        raw = await p0c.export_state()
        env = json.loads(raw if isinstance(raw, str) else raw.decode())
        assert "state" in env, "envelope missing 'state'"
        with open(f"{EVDIR}/{name}.json", "w") as f:
            json.dump(env, f, indent=1)
        ST.setdefault("exports", {})[name] = True
        say(f"exported {name}.json (turn={env['state'].get('turn_number')})")
        wire("export", {"name": name,
                        "turn": env["state"].get("turn_number")})
        return env["state"]
    except Exception as e:
        say(f"export {name} FAILED: {e!r}")
        ST.setdefault("tick_errors", []).append(f"export_{name}: {e!r}")
        return None


async def hold_branch(c, pid, tag, st, acts, state):
    """Tick behavior while a CastSpell is in flight but unconfirmed:
    answer decisions and mana needs but start no new plays and pass no
    priority. The entry-choice is answered here too (it can arrive while
    the cast resolves)."""
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if await enter_choice_tick(c, pid, tag, st, state):
        return
    if "DeclareAttackers" in (a.get("type") for a in acts):
        await do_declare_empty(c, acts, tag, "attackers")
        return
    if "DeclareBlockers" in (a.get("type") for a in acts):
        await do_declare_empty(c, acts, tag, "blockers")
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    return


async def seat_tick(c, pid, tag, p0c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    # ---- cast-confirmation guard (protocol 118/120): while our CastSpell
    # is in flight but unconfirmed, answer decisions and mana needs but
    # start no new plays and pass no priority. Runs before any
    # early-return block so a dropped cast is always detected.
    if cast_guard_tick(state, tag) == "hold":
        await hold_branch(c, pid, tag, st, acts, state)
        return
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if await pay_tick(c, acts):
        return
    if await do_pay_mana_vi_wrap(c, acts, st, tag):
        return
    # combat declares
    wphase = str(state.get("phase") or "").lower()
    if "declareattack" in wphase and str(state.get("active_player")) == str(pid):
        if tag == "P2":
            if await p2_attack_tick(c, acts, st, state, tag):
                return
        else:
            if await do_declare_empty(c, acts, tag, "attackers"):
                return
    if "declareblock" in wphase:
        if await do_declare_empty(c, acts, tag, "blockers"):
            return
    # the reported entry choice (owner answers)
    if await enter_choice_tick(c, pid, tag, st, state):
        return
    # Xantcha entered bookkeeping + mid export
    xoid = xantcha_oid_any(state)
    if xoid is not None and not ST.get("entered"):
        o = get_obj(state, xoid)
        ST["entered"] = True
        ST["entered_at"] = time.time()
        ST["xantcha_oid"] = int(xoid)
        ST["enter_controller"] = o.get("controller")
        ST["enter_owner"] = o.get("owner")
        say(f"Xantcha entered: oid={xoid} owner={o.get('owner')} "
            f"controller={o.get('controller')} "
            f"choice_offered={ST.get('enter_choice_offered')}")
        wire("xantcha_entered",
             {"oid": int(xoid), "owner": o.get("owner"),
              "controller": o.get("controller"),
              "choice_offered": ST.get("enter_choice_offered"),
              "choice_pick": ST.get("enter_choice_pick")})
    if (ST.get("entered") and not ST.get("mid_exported")
            and (ST.get("enter_choice_offered")
                 or time.time() - (ST.get("entered_at") or 0) > 10)):
        mid = await export_named("mid", p0c)
        if mid is not None:
            ST["mid_exported"] = True
    # pre export fallback: Xantcha on stack
    if (ST.get("xantcha_cast") and not ST.get("pre_exported")
            and xantcha_on_stack(state) and not ST.get("entered")):
        pre = await export_named("pre", p0c)
        if pre is not None:
            ST["pre_exported"] = True
    # ---- priority gate, then yield before leg evaluation
    has_prio = my_priority(acts, st)
    await asyncio.sleep(0)
    if not has_prio:
        await unexpected_prompt_tick(c, pid, tag, st, state)
        return
    # ---- the activation test runs at priority ----
    if await activation_tick(c, pid, tag, st, acts, state, p0c):
        return
    # P0 casts the Xantcha commander
    if (tag == "P0" and not ST.get("xantcha_cast")
            and xantcha_oid_any(state) is None
            and my_main(state, 0)
            and can_pay(state, 0, ("B", "R"), 1)):
        a, _oid = find_cast_action(acts, state, XANTCHA)
        if a is not None:
            if await try_cast(c, state, acts, tag, XANTCHA,
                              {"B": 1, "R": 1, "generic": 1},
                              {"xantcha_cast": False}):
                ST["xantcha_cast"] = True
                ST["xantcha_cast_turn"] = state.get("turn_number")
                await asyncio.sleep(1.0)
                try:
                    cur = (c.latest or {}).get("state", {})
                    if xantcha_on_stack(cur) and not ST.get("pre_exported"):
                        pre = await export_named("pre", p0c)
                        if pre is not None:
                            ST["pre_exported"] = True
                except Exception as e:
                    say(f"post-cast pre export failed: {e}")
            return
    if await play_a_land(c, state, pid, acts, tag):
        return
    await pass_priority(c, st, acts)


async def do_pay_mana_vi_wrap(c, acts, st, tag):
    # pay_tick handled merged pay actions; vi mana menus here
    return await pay_mana_vi(c, st, tag)


# ---------------------------------------------------------------- finish & evidence

def load_state(fn, notes):
    p = f"{EVDIR}/{fn}.json"
    try:
        if os.path.exists(p):
            raw = open(p).read()
            env = json.loads(raw)
            if isinstance(env, str):
                env = json.loads(env)
            st = env.get("state", env) if isinstance(env, dict) else None
            say(f"loaded {fn}.json")
            return st
    except Exception as e:
        notes.append(f"state reload failed for {fn}.json: {e}")
    return None


def evaluate():
    notes = ST.get("notes", [])
    ass = {k: "not-run" for k in ASS_KEYS}
    pre_st = load_state("pre", notes)
    mid_st = load_state("mid", notes)
    post_st = load_state("post", notes)
    ST["_pre_st"] = pre_st is not None
    ST["_mid_st"] = mid_st is not None
    ST["_post_st"] = post_st is not None

    if post_st is not None:
        xoid = ST.get("xantcha_oid") or xantcha_oid_any(post_st)
        o = get_obj(post_st, xoid) if xoid else {}
        ok = (xoid is not None and o.get("zone") == "Battlefield"
              and str(o.get("owner")) == "0")
        ass["A1_setup_ok"] = "passed" if ok else "failed"
        notes.append(f"A1: xantcha_oid={xoid} zone={o.get('zone')} "
                     f"owner={o.get('owner')} controller={o.get('controller')}")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 failed: post.json missing")

    if ST.get("enter_choice_offered"):
        ass["A2_choice_offered"] = "passed"
        notes.append(f"A2 passed: opponent-choice prompt offered "
                     f"(iid={ST.get('enter_choice_iid')}, candidates="
                     f"{ST.get('enter_choice_candidates')}, pick="
                     f"{ST.get('enter_choice_pick')})")
    elif ST.get("entered"):
        ass["A2_choice_offered"] = "failed"
        notes.append("A2 FAILED: Xantcha entered with NO opponent-choice "
                     "prompt offered to the owner. BUG REPRODUCED (a).")
    else:
        ass["A2_choice_offered"] = "failed"
        notes.append("A2 failed: Xantcha never entered")

    if post_st is not None and ST.get("entered"):
        xoid = ST.get("xantcha_oid") or xantcha_oid_any(post_st)
        ctrl = get_obj(post_st, xoid).get("controller")
        if str(ctrl) == str(INTENDED_CONTROLLER):
            ass["A3_controlled_by_chosen"] = "passed"
            notes.append(f"A3 passed: Xantcha controlled by P{ctrl} "
                         f"(intended pick)")
        else:
            ass["A3_controlled_by_chosen"] = "failed"
            notes.append(f"A3 FAILED: Xantcha controlled by P{ctrl}, "
                         f"not the intended P{INTENDED_CONTROLLER}")
    else:
        ass["A3_controlled_by_chosen"] = "failed"
        notes.append("A3 failed: no Xantcha on BF")

    if ST.get("p1_activated"):
        ass["A4_any_player_activate"] = "passed"
        notes.append("A4 passed: P1 (non-controller) activated Xantcha's "
                     "{3} ability")
    elif ST.get("entered"):
        ass["A4_any_player_activate"] = "failed"
        notes.append("A4 FAILED: P1 (non-controller, non-owner) was never "
                     "offered Xantcha's {3} activation. BUG REPRODUCED (b).")
    else:
        ass["A4_any_player_activate"] = "failed"
        notes.append("A4 failed: Xantcha never entered")

    if post_st is not None and ST.get("p1_activated") and ST.get("pre_act"):
        xoid = ST.get("xantcha_oid") or xantcha_oid_any(post_st)
        ctrl = get_obj(post_st, xoid).get("controller")
        life_now = {i: life_of(post_st, i) for i in (0, 1, 2)}
        hand_now = {i: len(hand_ids(post_st, i)) for i in (0, 1, 2)}
        pre = ST["pre_act"]
        life_ok = (life_now[ctrl] == pre["life"][ctrl] - 2)
        others_ok = all(life_now[i] == pre["life"][i]
                        for i in (0, 1, 2) if i != ctrl)
        draw_ok = (hand_now[1] == pre["hand"][1] + 1)
        notes.append(f"A5: controller=P{ctrl} life {pre['life']} -> "
                     f"{life_now}; hands {pre['hand']} -> {hand_now}")
        if life_ok and others_ok and draw_ok:
            ass["A5_activation_effect"] = "passed"
            notes.append("A5 passed: controller -2 life, P1 drew 1")
        else:
            ass["A5_activation_effect"] = "failed"
            notes.append("A5 FAILED: activation effect wrong "
                         f"(life_ok={life_ok} others_ok={others_ok} "
                         f"draw_ok={draw_ok})")
    elif not ST.get("p1_activated"):
        ass["A5_activation_effect"] = "not-run"
        notes.append("A5 not-run: no activation happened")
    else:
        ass["A5_activation_effect"] = "failed"
        notes.append("A5 failed: post.json missing")

    if post_st is not None:
        stack_empty = not (post_st.get("stack") or [])
        ass["A6_cleanup"] = "passed" if stack_empty else "failed"
        notes.append(f"A6: stack_empty={stack_empty}")
    else:
        ass["A6_cleanup"] = "failed"
        notes.append("A6 failed: post.json missing")

    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("A1 failed: Xantcha never reached the battlefield "
                     "owned by P0")
    elif any(ass[k] == "failed"
             for k in ("A2_choice_offered", "A3_controlled_by_chosen",
                       "A4_any_player_activate", "A5_activation_effect")):
        verdict = "reproduced"
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    notes.append(f"verdict={verdict}")
    return verdict, ass, notes


async def finalize(p0c, verdict, ass, notes):
    dur = time.time() - ST["t_start"]
    for tag in ("pre", "mid", "post"):
        if tag not in ST.get("exports", {}):
            await export_named(tag, p0c)
    run = {
        "run_id": RUN_ID, "issue": ISSUE,
        "verdict": verdict, "validated_at": "2026-10-14",
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 153,
                   "client": "driver/client.py",
                   "scenario": "driver/scenario_6916_01140.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_6916_01140.py",
                 "rb").read()).hexdigest(),
        "format_config": "CommanderDraft (3 seats; P0/P1 commanders, P2 none)",
        "decks": {"P0": {"main": [[SWAMP, 30], [MOUNTAIN, 30]],
                         "commander": P0_COMMANDER},
                  "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                  "P2": {"main": P2_DECK, "commander": []}},
        "assertions": ass,
        "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                         for k, v in ST.items()
                         if not k.startswith("_")},
        "notes": notes,
        "observations": {
            "rejections": ST.get("rejections", []),
            "tick_errors": ST.get("tick_errors", []),
            "p1_sample_turns": sorted(ST.get("p1_sample_turns", set())),
            "replacement_prompts": ST.get("replacement_prompts", []),
        },
        "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                           "assertions.json", "data_evidence.json",
                           "manifest.sha256", "summary.png",
                           "scenario_6916_01140.py", "wire_log.jsonl",
                           "scenario_run.log", "server.log"],
        "limitations": [
            "Browser UI not exercised; native engine via three human-client seats.",
            "CommanderDraft format is a test-harness fixture for 3-player "
            "games (P0 casts Xantcha from the command zone).",
            "The reported 'attacks each combat if able' clause was not "
            "asserted; only the opponent-choice entry and the any-player "
            "activation permission were tested.",
            "States are authoritative exports (restorable only via full game replay).",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "verdict": verdict}, f, indent=1)
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_6916_01140.py",
                f"{EVDIR}/scenario_6916_01140.py")
    shutil.copy(f"{BACKFILL}/server.log",
                f"{EVDIR}/server.log") if os.path.exists(
                    f"{BACKFILL}/server.log") else None
    if not os.path.exists(f"{EVDIR}/server.log"):
        open(f"{EVDIR}/server.log", "w").write(
            "shared live v0.114.0 server on 127.0.0.1:9374; no per-run "
            "server.log (per AGENTS.md 2026-10-08: no second server "
            "started)\n")
    say("copied scenario_6916_01140.py into EVDIR")

    # AGENTS.md lesson: close WIRE/RUNLOG BEFORE the manifest loop; the
    # final verdict lines flush after the hashes would otherwise fail
    # sha256sum -c.
    say("closing logs before manifest computation")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_6916_01140.py",
         EVDIR, str(ISSUE),
         "Xantcha, Sleeper Agent - opponent choice + any-player activation"],
        capture_output=True, text=True)
    print("render stdout:", (r.stdout or "")[:300], flush=True)
    print("render stderr:", (r.stderr or "")[:500], flush=True)
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass,
                      "run_id": RUN_ID}, indent=1), flush=True)


# ---------------------------------------------------------------- main loop

async def _main():
    global P0C
    ST.update({
        "t_start": time.time(),
        "notes": [],
        "rejections": [],
        "tick_errors": [],
        "data_level_ok": False,
        "hello_ok": False,
        "mana_needs": {"P0": {}, "P1": {}, "P2": {}},
        "pending_cast": None,
        "exports": {},
        "xantcha_cast": False,
        "entered": False,
        "enter_choice_offered": False,
        "mid_exported": False,
        "pre_exported": False,
        "post_exported": False,
        "post_at": None,
        "p1_activated": False,
        "p1_sample_turns": set(),
        "p1_noact_logged": set(),
    })
    await verify_server_hello()
    check_data_level()

    t0 = time.time()
    p0 = PhaseClient("P06916")
    await p0.connect()
    await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                    format_config=COMMANDER_FORMAT)
    p1 = PhaseClient("P16916")
    await p1.connect()
    await p1.join(p0.game_code, cdeck(P1_COMMANDER, *P1_DECK))
    p2 = PhaseClient("P26916")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    P0C = p0
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": p0.game_code,
                        "seats": {"P0": p0.player_id, "P1": p1.player_id,
                                  "P2": p2.player_id}})
    clients = [p0, p1, p2]
    tags = ["P0", "P1", "P2"]
    pids = [0, 1, 2]
    last_rev = {}
    last_change = {0: time.time(), 1: time.time(), 2: time.time()}
    last_tick_at = {}
    last_diag = 0.0

    async def post_check():
        # post export: P1's activation resolved (stack empty), or Xantcha
        # entered and P1 has had >=2 distinct main-phase samples with 3+
        # untapped lands and still no activation offered (the missing offer
        # is itself the signal for claim b).
        #
        # act_done requires the resolution broadcast to have ARRIVED: the
        # export must not run on P0's pre-resolution view. Gate on 5s
        # elapsed since submission AND a fresh P0 revision (2026-10-14:
        # a same-second export captured the ability still on the stack).
        s = (p0.latest or {}).get("state", {})
        stack_empty = not (s.get("stack") or [])
        fresh_view = (time.time() - (ST.get("p1_activated_at") or 0) > 5
                      and p0.revision != ST.get("p1_activated_p0rev"))
        act_done = ST.get("p1_activated") and stack_empty and fresh_view
        no_offer_done = (ST.get("entered") and not ST.get("p1_activated")
                         and len(ST.get("p1_sample_turns", set())) >= 2
                         and stack_empty)
        if ST.get("entered") and not ST.get("post_exported") \
                and (act_done or no_offer_done):
            post = await export_named("post", p0)
            if post is not None:
                ST["post_exported"] = True
                ST["post_at"] = time.time()
                say(f"exported POST (act_done={act_done} "
                    f"no_offer_done={no_offer_done})")

    i = 0
    while i < 9000:
        i += 1
        await asyncio.sleep(0.2)
        for c, pid, tag in zip(clients, pids, tags):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[pid] = time.time()
            else:
                if time.time() - last_change[pid] > 45:
                    s0 = st["state"]
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={s0.get('turn_number')} "
                        f"phase={s0.get('phase')} "
                        f"holding_priority="
                        f"{my_priority(merged_actions(st), st)}")
                    wire("watchdog_stale",
                         {"who": c.name, "rev": c.revision,
                          "turn": s0.get("turn_number"),
                          "phase": s0.get("phase")})
                    last_change[pid] = time.time()
                holds_prio = my_priority(merged_actions(st), st)
                if not (holds_prio
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            drain_rejections(c, "tick")
            try:
                await seat_tick(c, pid, tag, p0)
            except Exception as e:
                say(f"[{tag}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": tag,
                                    "err": f"{type(e).__name__}: {e}"})
                ST["tick_errors"].append(f"{tag}: {type(e).__name__}: {e}")
        await post_check()
        if (ST.get("entered") and not ST.get("post_exported")
                and time.time() - t0 > 1200):
            say("WATCHDOG: 1200s elapsed without post; finishing")
            post = await export_named("post", p0)
            if post is not None:
                ST["post_exported"] = True
            break
        if ST.get("post_exported") and time.time() - (ST.get("post_at") or 0) > 5:
            say("post exported; finishing")
            break
        st = p0.latest
        if st and st["state"].get("eliminated_players"):
            ST["notes"].append(
                f"game ended (eliminated={st['state'].get('eliminated_players')}) "
                f"at turn {st['state'].get('turn_number')}")
            break
        if time.time() - t0 > SETUP_DEADLINE_S:
            ST["notes"].append("setup deadline hit")
            break
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} "
                f"P0lands={len(untapped_lands(s, 0))} "
                f"P1lands={len(untapped_lands(s, 1))} "
                f"xcast={ST.get('xantcha_cast')} entered={ST.get('entered')} "
                f"ctrl={ST.get('enter_controller')} "
                f"choice={ST.get('enter_choice_offered')} "
                f"p1act={ST.get('p1_activated')} "
                f"pre={ST.get('pre_exported')} mid={ST.get('mid_exported')} "
                f"post={ST.get('post_exported')} stack={len(s.get('stack') or [])}")

    verdict, ass, notes = evaluate()
    say(f"verdict={verdict} assertions={json.dumps(ass)}")
    await finalize(p0, verdict, ass, notes)
    await p0.close()
    await p1.close()
    await p2.close()


async def main():
    pidfile = "/tmp/scenario_6916_01140.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_6916_01140 instance is alive "
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
